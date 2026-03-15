"""Cascade Strategy — 级联策略

An original, cross-asset regime-adaptive strategy designed for small capital
multi-market trading.  Combines signals from crypto (24/7), equities, and
volatility markets to detect information cascades and exploit lead-lag
relationships across asset classes.

Core thesis
-----------
Crypto markets trade around the clock and price information before equities
open.  By reading the "crypto pulse" we can anticipate equity moves, and by
reading the "vol regime" we can adapt our trading style in real-time.

Academic basis
--------------
* Weekend crypto → Monday equity lead-lag (ScienceDirect 2025)
* Funding-rate extremes as contrarian reversal signals (Amberdata / Coinbase research)
* VIX term-structure regimes (Cboe research; backwardation ≈ 16% of time → fear)
* Cross-asset momentum persistence (Asness, Moskowitz, Pedersen 2013)
* Volatility targeting improves Sharpe (Moreira & Muir 2017)

Architecture
------------
Phase 1  SENSE   → detect market regime from 4 independent signal clusters
Phase 2  ALLOCATE→ set asset-class budgets per regime
Phase 3  SELECT  → rank & pick best assets within each class
Phase 4  SIZE    → volatility-target + half-Kelly position sizing
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from .base import BaseStrategy, DataContext, StrategySignal

if TYPE_CHECKING:
    from ..config import Settings
    from ..exchanges.base import BaseExchange

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RegimeState:
    """Result of the SENSE phase."""
    label: str                       # CRISIS / CAUTIOUS / NEUTRAL / BULLISH / EUPHORIA
    score: float                     # −1.0 (extreme fear) … +1.0 (extreme greed)
    crypto_pulse: float              # −1 … +1
    vol_regime: str                  # low / normal / high
    cross_asset_flow: float          # −1 … +1
    funding_signal: float            # −1 (extreme long) … +1 (extreme short → contrarian buy)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AssetScore:
    """Score for a single asset within its class."""
    symbol: str
    asset_class: str                 # equity / crypto / bond
    momentum_score: float            # 0 … 1
    vol_adjusted_score: float        # momentum / vol
    funding_override: float          # −1 … +1  (crypto only, 0 for others)
    final_score: float
    eligible: bool
    reason: str


@dataclass(frozen=True)
class CascadePlan:
    """Complete output of one Cascade strategy cycle."""
    timestamp: datetime
    regime: RegimeState
    asset_scores: list[AssetScore]
    asset_class_budgets: dict[str, float]   # equity→0.40, crypto→0.35, bond→0.25
    target_weights: dict[str, float]        # symbol → portfolio weight
    total_exposure: float
    metadata: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if abs(b) > 1e-12 else default


def _annualized_vol(daily_returns: pd.Series, trading_days: int = 365) -> float:
    """Annualized vol.  Uses 365 days (crypto) by default; pass 252 for equities."""
    if len(daily_returns) < 5:
        return 0.50  # assume 50% vol when data is scarce
    return float(daily_returns.std(ddof=0) * math.sqrt(trading_days))


def _momentum(prices: pd.Series, lookback_days: int) -> float:
    """Simple price momentum: current / past − 1."""
    if len(prices) < lookback_days + 1:
        return 0.0
    past = float(prices.iloc[-(lookback_days + 1)])
    if past <= 0:
        return 0.0
    return float(prices.iloc[-1] / past - 1)


def _rsi(prices: pd.Series, period: int = 14) -> float:
    """Wilder RSI."""
    if len(prices) < period + 1:
        return 50.0
    delta = prices.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = float(gain.rolling(period).mean().iloc[-1])
    avg_loss = float(loss.rolling(period).mean().iloc[-1])
    if avg_loss < 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _is_crypto(symbol: str, settings=None) -> bool:
    """True for direct crypto pairs (BTC/USDT) AND equity-listed crypto proxies (US.IBIT)."""
    if "/" in symbol:
        return True
    if settings is not None:
        proxy = getattr(settings, "crypto_proxy_symbols", ())
        return symbol in proxy
    return False


def _is_crypto_proxy(symbol: str, settings=None) -> bool:
    """True only for equity-listed crypto proxies (US.IBIT, US.ETHA, …).
    These are priced via Futu but classified as the crypto asset class."""
    if settings is None:
        return False
    return symbol in getattr(settings, "crypto_proxy_symbols", ())


def _is_equity(symbol: str, settings=None) -> bool:
    if not (symbol.startswith("US.") or symbol.startswith("HK.")):
        return False
    return not _is_bond(symbol) and not _is_crypto_proxy(symbol, settings)


def _is_bond(symbol: str) -> bool:
    return symbol in {"US.AGG", "US.IEF", "US.TLT", "US.SHY", "US.BND"}


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1: SENSE  — detect market regime
# ═══════════════════════════════════════════════════════════════════════════

def _crypto_pulse(
    btc_prices: pd.Series,
    eth_prices: pd.Series,
    funding_rate: float | None,
) -> tuple[float, float]:
    """Compute crypto pulse (−1…+1) and funding signal (−1…+1).

    Crypto pulse = weighted combination of BTC 4h momentum + ETH/BTC trend.
    Funding signal = contrarian: extreme positive funding → bearish (−1),
                     extreme negative funding → bullish (+1).
    """
    # BTC short-term momentum (last 6 × 4h bars ≈ 1 day)
    btc_mom_1d = _momentum(btc_prices, 1) if len(btc_prices) > 1 else 0.0
    btc_mom_7d = _momentum(btc_prices, 7) if len(btc_prices) > 7 else 0.0

    # ETH/BTC relative trend (risk appetite proxy within crypto)
    eth_btc_ratio = 0.0
    if len(eth_prices) > 7 and len(btc_prices) > 7:
        eth_mom = _momentum(eth_prices, 7)
        btc_mom = _momentum(btc_prices, 7)
        eth_btc_ratio = _clip((eth_mom - btc_mom) * 10, -1, 1)

    pulse = _clip(
        0.50 * _clip(btc_mom_1d * 50, -1, 1)      # fast signal
        + 0.30 * _clip(btc_mom_7d * 10, -1, 1)     # medium signal
        + 0.20 * eth_btc_ratio                       # risk appetite
    )

    # Funding signal (contrarian)
    # Normal range: −0.01% … +0.03% per 8h
    # Extreme positive (>0.05%) → too many longs → bearish
    # Extreme negative (<−0.02%) → too many shorts → bullish
    funding_sig = 0.0
    if funding_rate is not None:
        if funding_rate > 0.0005:
            funding_sig = _clip(-1.0 * (funding_rate - 0.0003) / 0.0005)
        elif funding_rate < -0.0002:
            funding_sig = _clip(1.0 * (-funding_rate - 0.0001) / 0.0003)
        else:
            funding_sig = 0.0

    return pulse, funding_sig


def _vol_regime(
    btc_returns: pd.Series,
    vix_level: float | None,
) -> tuple[str, float]:
    """Classify volatility regime and compute a vol-fear score (−1…+1).

    Returns (regime_label, vol_score) where vol_score > 0 means calm/bullish
    and vol_score < 0 means fearful.
    """
    btc_vol = _annualized_vol(btc_returns, 365)

    # VIX score
    vix_score = 0.0
    if vix_level is not None:
        if vix_level < 15:
            vix_score = 0.5    # calm
        elif vix_level < 25:
            vix_score = 0.0    # normal
        elif vix_level < 35:
            vix_score = -0.5   # fear
        else:
            vix_score = -1.0   # panic

    # BTC vol score
    if btc_vol < 0.40:
        btc_vol_score = 0.4
        btc_label = "low"
    elif btc_vol < 0.70:
        btc_vol_score = 0.0
        btc_label = "normal"
    else:
        btc_vol_score = -0.6
        btc_label = "high"

    combined = 0.5 * vix_score + 0.5 * btc_vol_score
    if vix_level is not None and vix_level > 30 and btc_vol > 0.70:
        regime = "high"
    elif vix_level is not None and vix_level < 15 and btc_vol < 0.40:
        regime = "low"
    else:
        regime = btc_label

    return regime, combined


def _cross_asset_flow(
    btc_weekend_return: float | None,
    gold_spy_ratio_trend: float | None,
    btc_spy_correlation: float | None,
) -> float:
    """Cross-asset flow signal (−1…+1).

    * BTC weekend return predicts Monday equity direction (academic paper).
    * Rising Gold/SPY ratio → risk-off.
    * High BTC-SPY correlation → systematic risk-on/off regime.
    """
    signals: list[float] = []

    if btc_weekend_return is not None:
        # BTC weekend return: positive → bullish for Monday equities
        signals.append(_clip(btc_weekend_return * 20, -1, 1))

    if gold_spy_ratio_trend is not None:
        # Rising Gold/SPY ratio → risk-off (negative)
        signals.append(_clip(-gold_spy_ratio_trend * 15, -1, 1))

    if btc_spy_correlation is not None:
        # When BTC and SPY are highly correlated AND both falling → risk-off
        # When highly correlated AND both rising → risk-on
        # For now, just note the correlation exists (neutral on direction)
        pass

    if not signals:
        return 0.0
    return _clip(sum(signals) / len(signals))


def _classify_regime(composite_score: float) -> str:
    """Map composite score to a named regime."""
    if composite_score <= -0.60:
        return "CRISIS"
    if composite_score <= -0.25:
        return "CAUTIOUS"
    if composite_score <= 0.25:
        return "NEUTRAL"
    if composite_score <= 0.60:
        return "BULLISH"
    return "EUPHORIA"


def sense_regime(
    btc_prices: pd.Series,
    eth_prices: pd.Series,
    funding_rate: float | None,
    btc_daily_returns: pd.Series,
    vix_level: float | None,
    btc_weekend_return: float | None,
    gold_spy_ratio_trend: float | None,
    btc_spy_correlation: float | None,
) -> RegimeState:
    """Phase 1: aggregate all signal clusters into a single RegimeState.

    Degraded mode (no crypto data)
    --------------------------------
    When BTC/ETH price series are empty (crypto exchange offline / unreachable),
    the crypto_pulse and funding_signal are unavailable.  In this situation we:

    1. Set pulse = 0, funding_sig = 0.
    2. Reweight the remaining signals so that vol and cross-asset flow carry
       more weight (0.55 / 0.45 instead of 0.25 / 0.25).
    3. Apply a conservative bias of −0.10 to the composite score to reflect the
       *information uncertainty* (we can't see crypto, so we assume it might be
       bad news).  This makes the system lean CAUTIOUS rather than NEUTRAL when
       data quality is degraded.

    The ``details`` dict records ``crypto_data_available`` so callers (CLI,
    dashboard, status.json) can surface the degraded-mode warning.
    """
    crypto_available = len(btc_prices) > 7

    pulse, funding_sig = _crypto_pulse(btc_prices, eth_prices, funding_rate)
    vol_label, vol_score = _vol_regime(btc_daily_returns, vix_level)
    flow = _cross_asset_flow(btc_weekend_return, gold_spy_ratio_trend, btc_spy_correlation)

    if crypto_available:
        # Full signal: crypto pulse 35%, funding 15%, vol 25%, cross-asset 25%
        composite = (
            0.35 * pulse
            + 0.15 * funding_sig
            + 0.25 * vol_score
            + 0.25 * flow
        )
    else:
        # Degraded: vol 55%, cross-asset 45%, small conservative bias
        # (pulse and funding_sig are both 0 when crypto data is absent)
        composite = (
            0.55 * vol_score
            + 0.45 * flow
            - 0.10            # uncertainty penalty → lean CAUTIOUS
        )
        logger.warning(
            "SENSE: no crypto price data — running in DEGRADED regime mode "
            "(vol_score=%.3f flow=%.3f composite_before_bias=%.3f). "
            "Connect a crypto exchange for full signal quality.",
            vol_score, flow, 0.55 * vol_score + 0.45 * flow,
        )

    label = _classify_regime(composite)
    return RegimeState(
        label=label,
        score=round(composite, 4),
        crypto_pulse=round(pulse, 4),
        vol_regime=vol_label,
        cross_asset_flow=round(flow, 4),
        funding_signal=round(funding_sig, 4),
        details={
            "vix_level": vix_level,
            "funding_rate": funding_rate,
            "btc_weekend_return": btc_weekend_return,
            "vol_score": round(vol_score, 4),
            "crypto_data_available": crypto_available,
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2: ALLOCATE  — asset-class budgets per regime
# ═══════════════════════════════════════════════════════════════════════════

# Regime → (equity_budget, crypto_budget, bond_budget)
REGIME_BUDGETS: dict[str, tuple[float, float, float]] = {
    "CRISIS":   (0.00, 0.00, 1.00),
    "CAUTIOUS": (0.20, 0.10, 0.70),
    "NEUTRAL":  (0.30, 0.20, 0.50),
    "BULLISH":  (0.40, 0.35, 0.25),
    "EUPHORIA": (0.25, 0.15, 0.60),
}


def allocate_budgets(regime: RegimeState) -> dict[str, float]:
    """Phase 2: return asset-class budgets based on regime."""
    eq, cr, bo = REGIME_BUDGETS.get(regime.label, (0.20, 0.15, 0.65))
    return {"equity": eq, "crypto": cr, "bond": bo}


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3: SELECT  — score & rank individual assets
# ═══════════════════════════════════════════════════════════════════════════

def score_asset(
    symbol: str,
    prices: pd.Series,
    funding_rate: float | None,
    settings: "Settings",
) -> AssetScore:
    """Score a single asset for inclusion in the portfolio.

    Equities: momentum + inverse-vol weighting.
    Crypto:   momentum + funding-rate override (contrarian).
    Bonds:    always eligible as safe haven; scored by momentum so the best
              bond is selected (AGG vs TLT vs IEF) rather than picking
              arbitrarily. Score capped at 0.70 so bonds never out-rank
              strongly trending equity/crypto.
    """
    if _is_bond(symbol):
        # Bonds are always eligible, but use momentum to pick the best one.
        mom_30 = _momentum(prices, 30) if len(prices) > 30 else 0.0
        mom_90 = _momentum(prices, 90) if len(prices) > 90 else 0.0
        mom_raw = 0.4 * mom_30 + 0.6 * mom_90
        # Cap bond score at 0.70 — they are safe-haven, not high-conviction bets
        bond_score = float(min(0.35 + _clip(mom_raw * 3, -0.35, 0.35), 0.70))
        bond_score = max(bond_score, 0.10)   # always at least 0.10 (safe haven floor)
        return AssetScore(
            symbol=symbol, asset_class="bond",
            momentum_score=round(bond_score, 4),
            vol_adjusted_score=round(bond_score, 4),
            funding_override=0.0,
            final_score=round(bond_score, 4),
            eligible=True, reason="safe_haven",
        )

    # Momentum: blend of 1-month and 3-month
    mom_30 = _momentum(prices, 30)
    mom_90 = _momentum(prices, 90)
    momentum_raw = 0.4 * mom_30 + 0.6 * mom_90
    momentum_score = _clip(momentum_raw * 5, 0, 1)  # scale and clip to [0,1]

    # Volatility adjustment
    # Crypto proxies (IBIT, ETHA) use equity vol convention (252 days) since
    # they trade on NYSE hours; pure crypto pairs use 365-day annualisation.
    is_direct_crypto = "/" in symbol
    if len(prices) > 30:
        daily_ret = prices.pct_change().dropna()
        ann_vol = _annualized_vol(daily_ret, 365 if is_direct_crypto else 252)
    else:
        ann_vol = 0.50
    vol_adjusted = _safe_div(momentum_score, max(ann_vol, 0.05), momentum_score)
    vol_adjusted = _clip(vol_adjusted, 0, 2)

    # Funding rate override — only for direct crypto pairs, not ETF proxies.
    funding_override = 0.0
    asset_class = "crypto" if _is_crypto(symbol, settings) else "equity"
    if is_direct_crypto and funding_rate is not None:
        if funding_rate < -0.0003:
            # Extreme negative funding → contrarian buy boost
            funding_override = _clip((-funding_rate - 0.0001) / 0.0005, 0, 1) * 0.3
        elif funding_rate > 0.0005:
            # Extreme positive funding → reduce attractiveness
            funding_override = _clip(-(funding_rate - 0.0003) / 0.0005, -1, 0) * 0.3

    # Absolute momentum filter
    reasons: list[str] = []
    if momentum_raw < 0 and funding_override <= 0:
        reasons.append("negative_momentum")
    if ann_vol > 1.5:
        reasons.append("extreme_volatility")

    final = vol_adjusted + funding_override
    eligible = not reasons and final > 0.05

    return AssetScore(
        symbol=symbol,
        asset_class=asset_class,
        momentum_score=round(momentum_score, 4),
        vol_adjusted_score=round(vol_adjusted, 4),
        funding_override=round(funding_override, 4),
        final_score=round(final, 4),
        eligible=eligible,
        reason=",".join(reasons) if reasons else "ok",
    )


def select_assets(
    scores: list[AssetScore],
    budgets: dict[str, float],
    max_per_class: int = 3,
    max_position: float = 0.30,
) -> dict[str, float]:
    """Phase 3: pick top assets within each class and assign weights.

    Within each asset class:
    1. Filter to eligible assets only.
    2. Sort by final_score descending.
    3. Take top ``max_per_class``.
    4. Weight proportionally by final_score, scaled to class budget.
    5. Cap each position at ``max_position``.

    Budget redistribution
    ---------------------
    If an asset class has no eligible assets (e.g. crypto exchange is offline,
    or all crypto assets have negative momentum), its budget is *not* silently
    dropped.  Instead it is redistributed to the bond allocation, which acts as
    the system's safety-net / cash-equivalent.  This prevents a scenario where
    Cascade loses 25-35% of nominal exposure just because Binance is unreachable.
    """
    weights: dict[str, float] = {}
    unallocated_budget = 0.0

    for asset_class, budget in budgets.items():
        if budget <= 0:
            continue

        candidates = [s for s in scores if s.asset_class == asset_class and s.eligible]
        if not candidates:
            # No eligible assets → accumulate for redistribution to bonds
            logger.debug(
                "No eligible assets in class '%s' (budget=%.0f%%) — will redirect to bonds.",
                asset_class, budget * 100,
            )
            unallocated_budget += budget
            continue

        candidates.sort(key=lambda s: s.final_score, reverse=True)
        top = candidates[:max_per_class]

        total_score = sum(s.final_score for s in top)
        if total_score <= 0:
            unallocated_budget += budget
            continue

        for s in top:
            raw_weight = budget * s.final_score / total_score
            capped = min(raw_weight, max_position)
            if capped > 0.01:  # ignore tiny positions (fee drag)
                weights[s.symbol] = round(capped, 6)

    # Redistribute unallocated budget to existing bond positions (or best bond)
    if unallocated_budget > 0.005:
        bond_symbols = [s.symbol for s in scores if s.asset_class == "bond" and s.eligible]
        if bond_symbols:
            # Find best bond already in weights, or fall back to highest-scored bond
            held_bonds = [sym for sym in bond_symbols if sym in weights]
            if held_bonds:
                best_bond = max(held_bonds, key=lambda sym: weights[sym])
            else:
                # Pick highest-scored bond from candidates
                best_bond_score = max(
                    (s for s in scores if s.asset_class == "bond" and s.eligible),
                    key=lambda s: s.final_score,
                )
                best_bond = best_bond_score.symbol

            extra = min(unallocated_budget, max_position - weights.get(best_bond, 0.0))
            if extra > 0.01:
                weights[best_bond] = round(weights.get(best_bond, 0.0) + extra, 6)
                logger.info(
                    "Redistributed %.0f%% unallocated budget → %s (total %.0f%%)",
                    unallocated_budget * 100, best_bond, weights[best_bond] * 100,
                )

    return weights


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4: SIZE  — volatility targeting + half-Kelly
# ═══════════════════════════════════════════════════════════════════════════

def volatility_target_weights(
    weights: dict[str, float],
    price_data: dict[str, pd.Series],
    target_vol: float = 0.10,
    max_scalar: float = 1.5,
) -> dict[str, float]:
    """Scale portfolio to target annual volatility.

    Simple approach: estimate portfolio vol as weighted average of individual
    vols (ignoring correlations for robustness), then scale.
    """
    if not weights:
        return {}

    portfolio_vol_sq = 0.0
    for symbol, weight in weights.items():
        prices = price_data.get(symbol)
        if prices is None or len(prices) < 20:
            individual_vol = 0.50
        else:
            ret = prices.pct_change().dropna()
            td = 365 if "/" in symbol else 252  # "/" → direct crypto pair; ETF proxies use equity convention (252)
            individual_vol = _annualized_vol(ret, td)
        portfolio_vol_sq += (weight * individual_vol) ** 2

    portfolio_vol = math.sqrt(portfolio_vol_sq) if portfolio_vol_sq > 0 else 0.30
    scalar = min(target_vol / max(portfolio_vol, 0.01), max_scalar)

    # Don't scale up in CRISIS-like situations
    if scalar > 1.0 and portfolio_vol > 0.60:
        scalar = 1.0

    return {
        symbol: round(weight * scalar, 6)
        for symbol, weight in weights.items()
        if weight * scalar > 0.005
    }


# ═══════════════════════════════════════════════════════════════════════════
# Main strategy class
# ═══════════════════════════════════════════════════════════════════════════

# Import here (not at top) to avoid circular import when __init__.py
# eagerly imports cascade.py while the registry class is still being defined.
from . import StrategyRegistry  # noqa: E402


@StrategyRegistry.register("cascade")
class CascadeStrategy(BaseStrategy):
    """Cascade: cross-asset regime-adaptive strategy.

    Implements the full 4-phase pipeline (SENSE → ALLOCATE → SELECT → SIZE)
    and produces a **complete** portfolio allocation.  The ``StrategySignal``
    carries ``metadata["is_full_portfolio"] = True`` so that ``RiskManager``
    skips the signal-combination step and applies only hard position caps.

    The ``run_cycle()`` method is kept as the internal implementation.
    External callers should use the standard ``compute_signal(ctx)`` interface.
    """

    #: Registry key and signal identifier.
    name = "cascade"

    def __init__(self, settings: "Settings") -> None:
        super().__init__(settings)

    # ------------------------------------------------------------------
    # Standard BaseStrategy interface
    # ------------------------------------------------------------------

    def compute_signal(self, ctx: DataContext) -> StrategySignal:
        """Compute a full portfolio plan and wrap it as a StrategySignal.

        Parameters
        ----------
        ctx:
            DataContext for this cycle; ``ctx.futu`` is the equity exchange
            and ``ctx.crypto`` is the crypto exchange (either may be None).

        Returns
        -------
        StrategySignal
            ``target_weights`` reflects the full Cascade allocation.
            ``metadata["is_full_portfolio"]`` is ``True``.
        """
        plan = self.run_cycle(
            crypto_exchange=ctx.crypto,
            futu_exchange=ctx.futu,
        )

        scores = {s.symbol: s.final_score for s in plan.asset_scores}
        crypto_avail = plan.regime.details.get("crypto_data_available", True)

        return StrategySignal(
            strategy_name=self.name,
            timestamp=plan.timestamp,
            target_weights=plan.target_weights,
            scores=scores,
            metadata={
                # Full-portfolio flag — tells RiskManager to trust these weights
                "is_full_portfolio":       True,
                # Regime info (surfaced in status.json and dashboard)
                "regime":                  plan.regime.label,
                "regime_score":            plan.regime.score,
                "regime_details":          plan.regime.details,
                # Asset-class budgets (for dashboard pie chart)
                "asset_class_budgets":     plan.asset_class_budgets,
                "total_exposure":          plan.total_exposure,
                # Data quality flags
                "crypto_data_available":   crypto_avail,
                # Forward raw plan metadata (vix, funding_rate, etc.)
                **plan.metadata,
            },
        )

    # ------------------------------------------------------------------
    # Data fetching helpers
    # ------------------------------------------------------------------

    def _fetch_prices(
        self,
        symbol: str,
        exchange: "BaseExchange",
        timeframe: str = "1d",
        limit: int = 120,
    ) -> pd.Series:
        """Fetch daily close prices for a symbol."""
        try:
            ohlcv = exchange.get_ohlcv(symbol, timeframe, limit)
            if ohlcv.empty:
                return pd.Series(dtype=float)
            # De-duplicate timestamps before indexing (Futu may return dupes on reconnect)
            ohlcv = ohlcv.drop_duplicates(subset=["timestamp"], keep="last")
            series = ohlcv.set_index("timestamp")["close"].sort_index()
            # Normalise index to tz-naive UTC-midnight so cross-asset intersections work
            idx = pd.DatetimeIndex(series.index)
            if idx.tz is not None:
                idx = idx.tz_localize(None)
            series.index = idx.normalize()
            return series
        except Exception as exc:
            logger.warning("Failed to fetch %s prices: %s", symbol, exc)
            return pd.Series(dtype=float)

    def _fetch_funding_rate(self, exchange: "BaseExchange", symbol: str) -> float | None:
        """Fetch current funding rate for a crypto perpetual."""
        try:
            if hasattr(exchange, "fetch_funding_rate"):
                return exchange.fetch_funding_rate(symbol)
        except Exception:
            pass
        return None

    def _fetch_vix(self, futu_exchange: "BaseExchange | None") -> float | None:
        """Try to get VIX level from Futu.  Returns None if unavailable."""
        if futu_exchange is None:
            return None
        try:
            price = futu_exchange.get_price("US.VIX")
            return price if price > 0 else None
        except Exception:
            return None

    def _compute_weekend_return(self, btc_prices: pd.Series) -> float | None:
        """Compute BTC return from Friday close to current (if it's Monday)."""
        if len(btc_prices) < 4:
            return None
        now = datetime.now(timezone.utc)
        if now.weekday() != 0:  # Only meaningful on Monday
            return None
        # Approximate: return over last 3 days (Fri→Mon)
        return _momentum(btc_prices, 3)

    def _compute_gold_spy_trend(
        self,
        futu_exchange: "BaseExchange | None",
    ) -> float | None:
        """20-day trend of Gold/SPY ratio."""
        if futu_exchange is None:
            return None
        try:
            gold = self._fetch_prices("US.GLD", futu_exchange, "1d", 30)
            spy = self._fetch_prices("US.SPY", futu_exchange, "1d", 30)
            if len(gold) < 20 or len(spy) < 20:
                return None
            ratio = gold / spy.reindex(gold.index, method="ffill")
            ratio = ratio.dropna()
            if len(ratio) < 20:
                return None
            return float(ratio.iloc[-1] / ratio.iloc[-20] - 1)
        except Exception:
            return None

    def _compute_btc_spy_correlation(
        self,
        btc_prices: pd.Series,
        futu_exchange: "BaseExchange | None",
    ) -> float | None:
        """30-day rolling correlation between BTC and SPY daily returns."""
        if futu_exchange is None or len(btc_prices) < 35:
            return None
        try:
            spy = self._fetch_prices("US.SPY", futu_exchange, "1d", 60)
            if len(spy) < 35:
                return None
            btc_ret = btc_prices.pct_change().dropna()
            spy_ret = spy.pct_change().dropna()
            # Align dates
            common = btc_ret.index.intersection(spy_ret.index)
            if len(common) < 20:
                return None
            corr = float(btc_ret.loc[common].tail(30).corr(spy_ret.loc[common].tail(30)))
            return corr if not math.isnan(corr) else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    def run_cycle(
        self,
        crypto_exchange: "BaseExchange | None" = None,
        futu_exchange: "BaseExchange | None" = None,
    ) -> CascadePlan:
        """Execute one full Cascade strategy cycle.

        Parameters
        ----------
        crypto_exchange:
            Exchange for crypto data & trading (via ccxt).
        futu_exchange:
            Exchange for US equity data & trading (via Futu OpenD).
            Can be None if only trading crypto.
        """
        now = datetime.now(timezone.utc)

        # ── Phase 1: SENSE ──────────────────────────────────────────
        btc_prices = pd.Series(dtype=float)
        eth_prices = pd.Series(dtype=float)
        funding_rate: float | None = None

        if crypto_exchange is not None:
            btc_prices = self._fetch_prices("BTC/USDT", crypto_exchange, "1d", 120)
            eth_prices = self._fetch_prices("ETH/USDT", crypto_exchange, "1d", 120)
            funding_rate = self._fetch_funding_rate(crypto_exchange, "BTC/USDT:USDT")

        btc_daily_returns = btc_prices.pct_change().dropna() if len(btc_prices) > 1 else pd.Series(dtype=float)
        vix_level = self._fetch_vix(futu_exchange)
        weekend_ret = self._compute_weekend_return(btc_prices)
        gold_spy_trend = self._compute_gold_spy_trend(futu_exchange)
        btc_spy_corr = self._compute_btc_spy_correlation(btc_prices, futu_exchange)

        regime = sense_regime(
            btc_prices=btc_prices,
            eth_prices=eth_prices,
            funding_rate=funding_rate,
            btc_daily_returns=btc_daily_returns,
            vix_level=vix_level,
            btc_weekend_return=weekend_ret,
            gold_spy_ratio_trend=gold_spy_trend,
            btc_spy_correlation=btc_spy_corr,
        )
        logger.info("Regime: %s (score=%.3f)", regime.label, regime.score)

        # ── Phase 2: ALLOCATE ───────────────────────────────────────
        budgets = allocate_budgets(regime)
        logger.info("Budgets: equity=%.0f%% crypto=%.0f%% bond=%.0f%%",
                     budgets["equity"] * 100, budgets["crypto"] * 100, budgets["bond"] * 100)

        # ── Phase 3: SELECT ─────────────────────────────────────────
        # Build full universe from settings
        all_symbols: list[str] = []
        all_symbols.extend(self.settings.dm_universe)

        # Fetch prices and score each asset
        asset_scores: list[AssetScore] = []
        price_data: dict[str, pd.Series] = {}

        for symbol in all_symbols:
            # Routing: crypto proxies (US.IBIT, US.ETHA) use Futu for pricing
            # even though they count as the crypto asset class.
            if _is_crypto_proxy(symbol, self.settings):
                exchange = futu_exchange
            elif "/" in symbol:          # pure crypto pair (BTC/USDT)
                exchange = crypto_exchange
            else:                        # equity / bond
                exchange = futu_exchange
            if exchange is None:
                continue

            prices = self._fetch_prices(symbol, exchange, "1d", 120)
            if prices.empty:
                continue
            price_data[symbol] = prices

            # Per-symbol funding rate — only for direct crypto pairs
            sym_funding = None
            if "/" in symbol and crypto_exchange is not None:
                perp = symbol.split("/")[0] + "/USDT:USDT"
                sym_funding = self._fetch_funding_rate(crypto_exchange, perp)

            score = score_asset(symbol, prices, sym_funding, self.settings)
            asset_scores.append(score)

        target_weights = select_assets(
            asset_scores,
            budgets,
            max_per_class=3,
            max_position=self.settings.max_position_pct,
        )

        # ── Phase 4: SIZE ───────────────────────────────────────────
        target_weights = volatility_target_weights(
            target_weights,
            price_data,
            target_vol=self.settings.target_annual_vol,
        )

        total_exposure = sum(target_weights.values())
        # Hard cap: never exceed 100% for small accounts (no leverage)
        if total_exposure > 1.0:
            scale = 1.0 / total_exposure
            target_weights = {s: round(w * scale, 6) for s, w in target_weights.items()}
            total_exposure = sum(target_weights.values())

        logger.info("Targets: %s (exposure=%.1f%%)", target_weights, total_exposure * 100)

        return CascadePlan(
            timestamp=now,
            regime=regime,
            asset_scores=sorted(asset_scores, key=lambda s: s.final_score, reverse=True),
            asset_class_budgets=budgets,
            target_weights=target_weights,
            total_exposure=round(total_exposure, 6),
            metadata={
                "vix": vix_level,
                "funding_rate": funding_rate,
                "weekend_ret": weekend_ret,
                "gold_spy_trend": gold_spy_trend,
                "btc_spy_corr": btc_spy_corr,
            },
        )
