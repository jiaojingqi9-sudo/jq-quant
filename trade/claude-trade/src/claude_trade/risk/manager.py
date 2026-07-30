"""Risk management module for portfolio signal combination and position sizing.

Combines signals from multiple strategies into a final portfolio plan,
applies volatility targeting, and enforces risk limits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from ..config import Settings
from ..strategies.base import StrategySignal

if TYPE_CHECKING:
    from ..exchanges.base import BaseExchange


@dataclass(frozen=True)
class PortfolioPlan:
    """Final combined portfolio plan after risk management.

    Attributes:
        timestamp: When the plan was computed (UTC).
        strategy_signals: Raw signals from each strategy.
        raw_combined_weights: Weighted average before risk adjustment.
        final_weights: After risk limits applied.
        total_exposure: Sum of absolute values of final weights.
        regime: Market regime detected ("normal", "risk_off", "high_vol").
        metadata: Additional debug/audit info.
    """

    timestamp: datetime
    strategy_signals: list[StrategySignal]
    raw_combined_weights: dict[str, float]
    final_weights: dict[str, float]
    total_exposure: float
    regime: str
    metadata: dict[str, Any]


class RiskManager:
    """Manages risk across the trading portfolio.

    Responsibilities:
    1. Combine signals from multiple strategies
    2. Detect market regimes
    3. Apply risk limits and position constraints
    4. Volatility target scaling
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize risk manager with settings.

        Args:
            settings: Global trading configuration.
        """
        self.settings = settings

    def combine_signals(self, signals: list[StrategySignal]) -> dict[str, float]:
        """Combine signals from multiple strategies using configured weights.

        Each signal may declare a ``strategy_weight_hint`` in its metadata; if
        absent, falls back to the hard-coded config weights (DM / RSI / VB) and
        finally to equal weighting for unknown strategies.

        Args:
            signals: List of StrategySignal from different strategies.

        Returns:
            dict mapping symbol -> combined weight (sum ≤ 1.0).
        """
        if not signals:
            return {}

        # Config-based weights for the three built-in signal strategies
        _config_weights: dict[str, float] = {
            "dual_momentum":      self.settings.strategy_dm_weight,
            "rsi_mean_reversion": self.settings.strategy_rsi_weight,
            "volatility_breakout": self.settings.strategy_vb_weight,
        }

        # Resolve per-signal weight: hint → config → equal (1.0)
        def _resolve_weight(sig: StrategySignal) -> float:
            hint = sig.metadata.get("strategy_weight_hint")
            if hint is not None:
                return float(hint)
            return _config_weights.get(sig.strategy_name, 1.0)

        # Collect all symbols across all strategies
        all_symbols: set[str] = set()
        for signal in signals:
            all_symbols.update(signal.target_weights.keys())

        if not all_symbols:
            return {}

        # Weighted average
        combined: dict[str, float] = {}
        weight_sum = 0.0

        for signal in signals:
            strat_weight = _resolve_weight(signal)
            weight_sum += strat_weight
            for symbol in all_symbols:
                symbol_weight = signal.target_weights.get(symbol, 0.0)
                combined[symbol] = combined.get(symbol, 0.0) + strat_weight * symbol_weight

        # Normalize by total strategy weight
        if weight_sum > 0:
            combined = {sym: w / weight_sum for sym, w in combined.items()}

        return combined

    def _enforce_caps(self, weights: dict[str, float]) -> dict[str, float]:
        """Enforce the per-position cap and remove sub-1% weights.

        Does *not* apply regime adjustments — use ``apply_risk_limits()`` for
        full risk management.

        Args:
            weights: Input weights (may sum > 1.0 or have positions > cap).

        Returns:
            Weights with per-position cap applied; sub-1% positions zeroed.
        """
        if not weights:
            return {}

        max_pos = self.settings.max_position_pct
        capped = {sym: min(w, max_pos) for sym, w in weights.items() if w > 0}

        # Ensure sum ≤ 1.0
        total = sum(capped.values())
        if total > 1.0:
            scale = 1.0 / total
            capped = {sym: w * scale for sym, w in capped.items()}

        # Drop sub-1% positions (not worth the transaction cost)
        return {sym: w for sym, w in capped.items() if w >= 0.01}

    def detect_regime(self, exchange: BaseExchange) -> str:
        """Detect market regime based on volatility indicators.

        Checks:
        - If VIX > 25 (for US stocks): "risk_off"
        - If BTC 30-day realized vol > 80%: "high_vol"
        - Otherwise: "normal"

        Args:
            exchange: Exchange interface to fetch data.

        Returns:
            Regime string: "normal", "risk_off", or "high_vol".
        """
        regime = "normal"
        details = {}

        # Check for VIX spike (risk_off)
        try:
            vix_price = exchange.get_price("US.VIX")
            details["vix"] = vix_price
            if vix_price > 25:
                regime = "risk_off"
        except Exception:
            # VIX not available (e.g., crypto-only exchange)
            pass

        # Check BTC realized volatility (high_vol)
        if regime == "normal":
            try:
                btc_bars = exchange.get_ohlcv("BTC/USDT", "1d", 30)
                if not btc_bars.empty and len(btc_bars) >= 30:
                    close = btc_bars["close"].values
                    # Simple realized volatility: std of log returns
                    log_returns = np.diff(np.log(close))
                    realized_vol = np.std(log_returns) * np.sqrt(365)  # Annualized
                    details["btc_vol"] = realized_vol
                    if realized_vol > 0.80:
                        regime = "high_vol"
            except Exception:
                # BTC data not available
                pass

        return regime

    def apply_risk_limits(
        self, weights: dict[str, float], regime: str
    ) -> dict[str, float]:
        """Apply position limits and regime-based adjustments.

        Adjustments:
        - Max single position: max_position_pct (default 30%)
        - In risk_off regime: reduce all weights by 50%
        - In high_vol regime: reduce crypto weights by 50%
        - Ensure total exposure <= 1.0 (no leverage)
        - Round small weights to 0 (< 1% not worth trading)

        Args:
            weights: Raw combined weights before limits.
            regime: Market regime ("normal", "risk_off", "high_vol").

        Returns:
            dict mapping symbol -> limited weight.
        """
        if not weights:
            return {}

        limited = dict(weights)

        # Apply regime adjustments first
        if regime == "risk_off":
            # Reduce all positions by 50%
            limited = {sym: w * 0.5 for sym, w in limited.items()}
        elif regime == "high_vol":
            # Reduce crypto positions by 50%
            limited = {
                sym: (w * 0.5 if "/" in sym else w) for sym, w in limited.items()
            }

        # Enforce max position limit
        max_pos = self.settings.max_position_pct
        limited = {sym: min(w, max_pos) for sym, w in limited.items()}

        # Ensure total exposure <= 1.0
        total_exposure = sum(abs(w) for w in limited.values())
        if total_exposure > 1.0:
            scale = 1.0 / total_exposure
            limited = {sym: w * scale for sym, w in limited.items()}

        # Round small weights to 0 (< 1%)
        min_weight = 0.01
        limited = {sym: w if abs(w) >= min_weight else 0.0 for sym, w in limited.items()}

        # Remove zero weights for cleanliness
        limited = {sym: w for sym, w in limited.items() if w != 0.0}

        return limited

    def volatility_target(
        self, weights: dict[str, float], exchange: BaseExchange
    ) -> dict[str, float]:
        """Scale portfolio to target annual volatility.

        Computes portfolio volatility from individual asset volatilities
        and correlations, then scales weights to match target_annual_vol.

        Args:
            weights: Portfolio weights before volatility scaling.
            exchange: Exchange interface to fetch OHLCV data.

        Returns:
            dict mapping symbol -> scaled weight.
        """
        if not weights:
            return {}

        symbols = list(weights.keys())
        if len(symbols) == 0:
            return {}

        # Get OHLCV data for each symbol (1-day bars, 252 trading days)
        prices_dict = {}
        for symbol in symbols:
            try:
                bars = exchange.get_ohlcv(symbol, "1d", 252)
                if not bars.empty:
                    prices_dict[symbol] = bars["close"].values
            except Exception:
                # Symbol unavailable, skip
                pass

        if not prices_dict:
            # Can't compute correlations; return weights as-is
            return weights

        # Compute log returns for each symbol
        returns_dict = {}
        for symbol, prices in prices_dict.items():
            if len(prices) > 1:
                log_returns = np.diff(np.log(prices))
                returns_dict[symbol] = log_returns

        if len(returns_dict) < len(symbols) // 2:
            # Not enough data; return weights as-is
            return weights

        # Build correlation matrix
        returns_df = pd.DataFrame(returns_dict)
        corr_matrix = returns_df.corr().fillna(0)
        vol_vector = returns_df.std().values * np.sqrt(252)  # Annualized vols

        # Compute portfolio variance: w^T * Cov * w
        if len(vol_vector) > 0 and np.all(vol_vector > 0):
            cov_matrix = corr_matrix.values * np.outer(vol_vector, vol_vector)
            w_array = np.array([weights.get(sym, 0.0) for sym in corr_matrix.columns])

            portfolio_var = w_array @ cov_matrix @ w_array
            portfolio_vol = np.sqrt(max(portfolio_var, 0.0))

            # Scale to target
            target_vol = self.settings.target_annual_vol
            if portfolio_vol > 0.001:  # Avoid division by tiny numbers
                scale = target_vol / portfolio_vol
                scale = min(scale, 1.5)  # Cap scaling at 1.5x to avoid over-leverage
                return {sym: w * scale for sym, w in weights.items()}

        return weights

    def build_plan(
        self,
        signals: list[StrategySignal],
        exchange: "BaseExchange | None" = None,
    ) -> PortfolioPlan:
        """Build the final portfolio plan after all risk adjustments.

        Two-path pipeline
        -----------------
        **Full-portfolio path** — triggered when *all* active signals carry
        ``metadata["is_full_portfolio"] = True`` (e.g. Cascade running alone):
          - Trust the strategy's weights directly (it already handles regime,
            budgeting, and volatility targeting internally).
          - Apply only hard position caps via ``_enforce_caps()``.
          - Use the regime from the signal's metadata.

        **Signal-combination path** — triggered when one or more *signal*
        strategies are active (DM, RSI, VB, or any mix):
          1. Combine signals via ``combine_signals()``.
          2. Detect regime via ``detect_regime(exchange)``.
          3. Apply regime-based risk limits via ``apply_risk_limits()``.
          4. Scale to target vol via ``volatility_target()``.
          5. Enforce position caps.

        Args:
            signals:  List of StrategySignal from all active strategies.
            exchange: Primary exchange for regime detection and vol scaling.
                      May be ``None`` when running without live data.

        Returns:
            PortfolioPlan with final weights and full audit trail.
        """
        from datetime import timezone
        timestamp = datetime.now(timezone.utc)

        full_portfolio = [s for s in signals if s.metadata.get("is_full_portfolio")]
        signal_only   = [s for s in signals if not s.metadata.get("is_full_portfolio")]

        if full_portfolio and not signal_only:
            # ── Full-portfolio path ──────────────────────────────────────
            # Single authoritative allocation (Cascade or similar).
            # Only hard caps applied; no double risk management.
            sig = full_portfolio[0]
            raw_combined = dict(sig.target_weights)
            regime = sig.metadata.get("regime", "UNKNOWN")
            final_weights = self._enforce_caps(raw_combined)
            pipeline = "full_portfolio"
        else:
            # ── Signal-combination path ──────────────────────────────────
            raw_combined = self.combine_signals(signals)
            regime = self.detect_regime(exchange) if exchange is not None else "normal"
            risk_limited = self.apply_risk_limits(raw_combined, regime)
            vol_scaled   = self.volatility_target(risk_limited, exchange) if exchange is not None else risk_limited
            final_weights = self._enforce_caps(vol_scaled)
            pipeline = "signal_combination"

        total_exposure = sum(abs(w) for w in final_weights.values())

        metadata: dict[str, Any] = {
            "num_signals":     len(signals),
            "pipeline":        pipeline,
            "raw_symbols":     sorted(raw_combined.keys()),
            "final_symbols":   sorted(final_weights.keys()),
            "regime_detected": regime,
        }
        if full_portfolio:
            metadata["regime_score"] = full_portfolio[0].metadata.get("regime_score")

        return PortfolioPlan(
            timestamp=timestamp,
            strategy_signals=signals,
            raw_combined_weights=raw_combined,
            final_weights=final_weights,
            total_exposure=total_exposure,
            regime=regime,
            metadata=metadata,
        )
