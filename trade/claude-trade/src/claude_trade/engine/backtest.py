"""Regime-aware backtest for the Cascade strategy with realistic trade costs.

Uses synthetic GBM prices for quick offline testing (no exchange connections
required).  For production replay, use the market_logger JSONL files.

Cost model
----------
Equity (Futu HK, US market)
    Commission:   $0.0049 / share,  min $0.99
    Platform:     $0.0050 / share,  min $1.00
    Settlement:   $0.0030 / share,  min $1.00
    SEC sell fee: $0 (waived since 2023-05-15)
    TAF sell fee: $0.000119 / share, min $0.01, max $5.95
    Slippage:     5 bps per side (buy up / sell down)

Crypto (Binance)
    Taker fee:    0.10% per side (both buy and sell)
    Slippage:     10 bps per side

These defaults are calibrated to Futu HK + Binance retail as of 2026.
Override via run_cascade_backtest(..., equity_costs=..., crypto_costs=...).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from ..config import Settings


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

@dataclass
class EquityCostModel:
    """Per-trade cost parameters for Futu HK US-listed equities."""
    commission_per_share: float = 0.0049
    commission_min: float = 0.99
    platform_per_share: float = 0.0050
    platform_min: float = 1.00
    settlement_per_share: float = 0.0030
    settlement_min: float = 1.00
    # SEC sell fee waived since ~May 2023
    sec_sell_rate: float = 0.0
    # TAF sell fee
    taf_sell_per_share: float = 0.000119
    taf_sell_min: float = 0.01
    taf_sell_max: float = 5.95
    # Slippage: basis points per side (5 bps = 0.0005)
    slippage_bps: float = 5.0

    def round_trip_cost(self, shares: float, price: float, *, is_sell: bool = False) -> float:
        """Return total cost for ONE leg (buy or sell) in USD."""
        notional = shares * price

        comm = max(self.commission_per_share * shares, self.commission_min)
        plat = max(self.platform_per_share * shares, self.platform_min)
        sett = max(self.settlement_per_share * shares, self.settlement_min)
        sec = (self.sec_sell_rate * notional) if is_sell else 0.0
        taf = 0.0
        if is_sell:
            taf = min(max(self.taf_sell_per_share * shares, self.taf_sell_min), self.taf_sell_max)

        slip = notional * self.slippage_bps / 10_000.0
        return comm + plat + sett + sec + taf + slip


@dataclass
class CryptoCostModel:
    """Per-trade cost parameters for Binance."""
    taker_fee_rate: float = 0.001   # 0.10% taker
    slippage_bps: float = 10.0

    def round_trip_cost(self, notional: float) -> float:
        """Return total cost for ONE leg (buy or sell) in USD."""
        fee = notional * self.taker_fee_rate
        slip = notional * self.slippage_bps / 10_000.0
        return fee + slip


def _default_equity_costs() -> EquityCostModel:
    return EquityCostModel()


def _default_crypto_costs() -> CryptoCostModel:
    return CryptoCostModel()


# ---------------------------------------------------------------------------
# Synthetic price generator
# ---------------------------------------------------------------------------

def _generate_synthetic_prices(
    symbols: list[str],
    start_date: str,
    seed: int = 42,
) -> dict[str, pd.Series]:
    """Generate synthetic OHLCV-like close prices for backtesting.

    Parameters are calibrated to approximate historical annualised return
    and volatility for each asset class.
    """
    rng = np.random.default_rng(seed)
    start = pd.Timestamp(start_date)
    end = pd.Timestamp.now()
    dates = pd.date_range(start, end, freq="B")  # business days

    params: dict[str, dict[str, float]] = {
        "US.SPY":   {"mu": 0.10, "vol": 0.15, "start": 400.0},
        "US.EFA":   {"mu": 0.06, "vol": 0.18, "start": 70.0},
        "US.AGG":   {"mu": 0.02, "vol": 0.05, "start": 100.0},
        "US.GLD":   {"mu": 0.05, "vol": 0.14, "start": 170.0},
        "US.QQQ":   {"mu": 0.12, "vol": 0.20, "start": 420.0},
        "US.IEF":   {"mu": 0.02, "vol": 0.06, "start": 100.0},
        "US.TLT":   {"mu": 0.01, "vol": 0.12, "start": 100.0},
        "BTC/USDT": {"mu": 0.50, "vol": 0.80, "start": 30_000.0},
        "ETH/USDT": {"mu": 0.40, "vol": 0.90, "start": 2_000.0},
        "SOL/USDT": {"mu": 0.60, "vol": 1.00, "start": 80.0},
    }

    prices: dict[str, pd.Series] = {}
    for sym in symbols:
        p = params.get(sym, {"mu": 0.08, "vol": 0.20, "start": 100.0})
        n = len(dates)
        daily_mu = p["mu"] / 252
        daily_vol = p["vol"] / math.sqrt(252)
        log_returns = rng.normal(daily_mu - 0.5 * daily_vol ** 2, daily_vol, n)
        price = p["start"] * np.exp(np.cumsum(log_returns))
        prices[sym] = pd.Series(price, index=dates)
    return prices


# ---------------------------------------------------------------------------
# Regime detection (simplified, no live exchange connection)
# ---------------------------------------------------------------------------

def _detect_regime(cum_ret: pd.Series, btc_sym: str | None) -> str:
    """Simplified regime from momentum signals.

    Returns one of: CRISIS / CAUTIOUS / NEUTRAL / BULLISH / EUPHORIA
    """
    if btc_sym and btc_sym in cum_ret.index:
        btc_ret = float(cum_ret[btc_sym])
    else:
        # Fall back to equity aggregate
        equity_syms = [s for s in cum_ret.index if s.startswith("US.") and "AGG" not in s]
        btc_ret = float(cum_ret[equity_syms].mean()) if equity_syms else 0.0

    equity_syms = [s for s in cum_ret.index if s.startswith("US.") and "AGG" not in s]
    eq_ret = float(cum_ret[equity_syms].mean()) if equity_syms else 0.0

    if eq_ret < -0.10 and btc_ret < -0.20:
        return "CRISIS"
    if eq_ret < -0.05 or btc_ret < -0.10:
        return "CAUTIOUS"
    if btc_ret > 0.40 and eq_ret > 0.10:
        return "EUPHORIA"
    if btc_ret > 0.15 or eq_ret > 0.05:
        return "BULLISH"
    return "NEUTRAL"


_REGIME_BUDGETS: dict[str, dict[str, float]] = {
    "CRISIS":   {"equity": 0.10, "crypto": 0.00, "bond": 0.60, "cash": 0.30},
    "CAUTIOUS": {"equity": 0.20, "crypto": 0.05, "bond": 0.55, "cash": 0.20},
    "NEUTRAL":  {"equity": 0.30, "crypto": 0.10, "bond": 0.45, "cash": 0.15},
    "BULLISH":  {"equity": 0.40, "crypto": 0.20, "bond": 0.30, "cash": 0.10},
    "EUPHORIA": {"equity": 0.35, "crypto": 0.30, "bond": 0.20, "cash": 0.15},
}


# ---------------------------------------------------------------------------
# Core backtest loop
# ---------------------------------------------------------------------------

@dataclass
class CascadeBacktestResult:
    monthly_returns: pd.Series
    equity_curve: pd.Series
    summary: dict[str, Any]
    rebalance_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    regime_log: pd.DataFrame = field(default_factory=pd.DataFrame)


def _monthly_rebalance_with_costs(
    prices: dict[str, pd.Series],
    settings: Settings,
    initial_capital: float,
    equity_costs: EquityCostModel,
    crypto_costs: CryptoCostModel,
) -> CascadeBacktestResult:
    """Month-end rebalance simulation with realistic cost deduction."""
    price_df = pd.DataFrame(prices).dropna(how="all")
    monthly = price_df.resample("ME").last().dropna(how="all")
    if len(monthly) < 2:
        return CascadeBacktestResult(
            monthly_returns=pd.Series(dtype=float),
            equity_curve=pd.Series(dtype=float),
            summary={"error": "insufficient data"},
        )

    monthly_ret = monthly.pct_change().fillna(0.0)

    equity_syms = [s for s in price_df.columns if s.startswith("US.") and "AGG" not in s]
    bond_syms   = [s for s in price_df.columns if s in {"US.AGG", "US.IEF", "US.TLT"}]
    crypto_syms = [s for s in price_df.columns if "/" in s]
    btc_sym     = next((s for s in crypto_syms if "BTC" in s), None)

    lookback = 12  # months of momentum window

    portfolio_value = initial_capital
    positions: dict[str, float] = {}   # symbol → USD value held
    total_fees = 0.0

    pf_returns: list[float] = []
    pf_dates:   list[pd.Timestamp] = []
    rebalance_rows: list[dict[str, Any]] = []
    regime_rows:    list[dict[str, Any]] = []

    for i in range(lookback, len(monthly)):
        bar_date = monthly.index[i]
        window_ret = monthly_ret.iloc[i - lookback: i]

        # 12-month cumulative return per symbol
        cum_ret = (1 + window_ret).prod() - 1

        # Detect regime
        regime = _detect_regime(cum_ret, btc_sym)
        budgets = _REGIME_BUDGETS[regime]
        regime_rows.append({"date": bar_date, "regime": regime, **budgets})

        # Build target weights via top-1 momentum selection within each class
        weights: dict[str, float] = {}

        def _pick_top1(syms: list[str], budget: float) -> None:
            avail = [s for s in syms if s in cum_ret.index]
            if not avail or budget <= 0:
                return
            scored = cum_ret[avail].sort_values(ascending=False)
            # Only invest if positive momentum; else stay cash
            top = [s for s in scored.index if float(scored[s]) > 0]
            if top:
                weights[top[0]] = budget

        _pick_top1(equity_syms, budgets["equity"])
        _pick_top1(crypto_syms, budgets["crypto"])
        # Bond: always allocate if bond budget > 0
        avail_bonds = [s for s in bond_syms if s in cum_ret.index]
        if avail_bonds and budgets["bond"] > 0:
            best_bond = cum_ret[avail_bonds].sort_values(ascending=False).index[0]
            weights[best_bond] = budgets["bond"]

        # Convert target weights → dollar values
        target_values: dict[str, float] = {s: portfolio_value * w for s, w in weights.items()}

        # Compute trades needed (current positions vs target)
        cycle_fees = 0.0
        bar_prices = monthly.iloc[i]

        for sym in set(list(positions.keys()) + list(target_values.keys())):
            cur_val = positions.get(sym, 0.0)
            tgt_val = target_values.get(sym, 0.0)
            delta   = tgt_val - cur_val
            if abs(delta) < 1.0:  # ignore sub-dollar changes
                continue

            price = float(bar_prices.get(sym, 0.0))
            if price <= 0:
                continue

            is_sell = delta < 0
            trade_val = abs(delta)

            if "/" in sym:
                # Crypto
                fee = crypto_costs.round_trip_cost(trade_val)
            else:
                # Equity: convert to share count
                shares = trade_val / price
                fee = equity_costs.round_trip_cost(shares, price, is_sell=is_sell)

            cycle_fees += fee
            total_fees += fee

            side = "SELL" if is_sell else "BUY"
            rebalance_rows.append({
                "date": bar_date,
                "symbol": sym,
                "side": side,
                "trade_value_usd": round(trade_val, 2),
                "price": round(price, 4),
                "fee_usd": round(fee, 4),
                "regime": regime,
            })

        # Update positions to targets
        positions = dict(target_values)

        # Apply NEXT-month return to each position (signal at month i → hold until month i+1)
        if i + 1 < len(monthly_ret):
            next_ret_row = monthly_ret.iloc[i + 1]
            new_positions: dict[str, float] = {}
            for sym, val in positions.items():
                r = float(next_ret_row.get(sym, 0.0))
                new_positions[sym] = val * (1 + r)
            positions = new_positions

        # Portfolio value = sum of positions + uninvested cash - fees this cycle
        new_pf_value = sum(positions.values()) + portfolio_value * budgets.get("cash", 0.0) - cycle_fees
        # Prevent going negative
        new_pf_value = max(new_pf_value, 0.01)

        pf_ret = (new_pf_value - portfolio_value) / portfolio_value
        pf_returns.append(pf_ret)
        pf_dates.append(bar_date)
        portfolio_value = new_pf_value

    if not pf_returns:
        return CascadeBacktestResult(
            monthly_returns=pd.Series(dtype=float),
            equity_curve=pd.Series(dtype=float),
            summary={"error": "no returns computed"},
        )

    returns = pd.Series(pf_returns, index=pd.to_datetime(pf_dates))
    curve   = (1 + returns).cumprod()

    total_return = float(curve.iloc[-1] - 1)
    n_months     = len(returns)
    years        = n_months / 12
    cagr = float((1 + total_return) ** (1 / max(years, 0.01)) - 1) if total_return > -1 else -1.0
    vol  = float(returns.std(ddof=0) * math.sqrt(12))
    sharpe = float(returns.mean() * 12 / vol) if vol > 0 else 0.0
    drawdown = float((curve / curve.cummax() - 1).min())
    final_value = initial_capital * float(curve.iloc[-1])
    cost_drag_pct = total_fees / initial_capital * 100

    summary: dict[str, Any] = {
        "total_return":    total_return,
        "cagr":            cagr,
        "volatility":      vol,
        "sharpe":          sharpe,
        "max_drawdown":    drawdown,
        "final_value":     final_value,
        "initial_capital": initial_capital,
        "total_fees":      round(total_fees, 2),
        "cost_drag_pct":   round(cost_drag_pct, 3),
        "n_months":        n_months,
        "curve":           curve,
        "returns":         returns,
    }

    return CascadeBacktestResult(
        monthly_returns=returns,
        equity_curve=curve,
        summary=summary,
        rebalance_log=pd.DataFrame(rebalance_rows),
        regime_log=pd.DataFrame(regime_rows),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_cascade_backtest(
    settings: Settings,
    start_date: str = "2020-01-01",
    initial_capital: float | None = None,
    equity_costs: EquityCostModel | None = None,
    crypto_costs: CryptoCostModel | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    """Run a Cascade backtest with regime-adaptive allocation and trade costs.

    Parameters
    ----------
    settings:
        Loaded Settings object (used for symbol universe and initial_capital).
    start_date:
        First date of synthetic price history.
    initial_capital:
        Starting portfolio value in USD. Falls back to settings.initial_capital.
    equity_costs:
        Equity cost parameters; defaults to Futu HK US-market rates.
    crypto_costs:
        Crypto cost parameters; defaults to Binance retail rates.
    seed:
        Random seed for reproducible synthetic prices.

    Returns
    -------
    dict with keys:
        total_return, cagr, volatility, sharpe, max_drawdown,
        final_value, initial_capital, total_fees, cost_drag_pct,
        n_months, curve (pd.Series), returns (pd.Series),
        rebalance_log (pd.DataFrame), regime_log (pd.DataFrame)
    """
    capital      = initial_capital or settings.initial_capital
    eq_costs     = equity_costs  or _default_equity_costs()
    cr_costs     = crypto_costs  or _default_crypto_costs()

    # Build deduplicated symbol universe
    symbols = list(dict.fromkeys(list(settings.dm_universe) + list(settings.rsi_universe)))

    prices = _generate_synthetic_prices(symbols, start_date, seed=seed)
    result = _monthly_rebalance_with_costs(prices, settings, capital, eq_costs, cr_costs)
    # Merge supplementary DataFrames into summary for dashboard / CLI use
    out = dict(result.summary)
    out["rebalance_log"] = result.rebalance_log
    out["regime_log"]    = result.regime_log
    return out
