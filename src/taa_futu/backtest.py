from __future__ import annotations

from dataclasses import dataclass
import math

import pandas as pd

from .costs import (
    TradeCostModel,
    buffered_trade_price,
    estimate_trade_cost,
    max_affordable_buy_quantity,
    trade_log_total_fees,
)
from .strategy import compute_target_weights, monthly_closes


@dataclass(frozen=True)
class BacktestResult:
    monthly_returns: pd.Series
    equity_curve: pd.Series
    portfolio_value_curve: pd.Series
    benchmark_returns: pd.Series
    benchmark_curve: pd.Series
    weights: pd.DataFrame
    rebalance_log: pd.DataFrame
    summary: dict[str, float]


def _cagr(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    periods = len(returns)
    total_return = float((1 + returns).prod())
    if total_return <= 0 or periods == 0:
        return -1.0
    years = periods / 12
    return total_return ** (1 / years) - 1


def _annualized_vol(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    return float(returns.std(ddof=0) * math.sqrt(12))


def _max_drawdown(curve: pd.Series) -> float:
    if curve.empty:
        return 0.0
    drawdown = curve / curve.cummax() - 1
    return float(drawdown.min())


def performance_summary(returns: pd.Series, curve: pd.Series) -> dict[str, float]:
    vol = _annualized_vol(returns)
    sharpe = float(returns.mean() * 12 / vol) if vol > 0 else 0.0
    return {
        "total_return": float(curve.iloc[-1] - 1),
        "cagr": _cagr(returns),
        "volatility": vol,
        "sharpe": sharpe,
        "max_drawdown": _max_drawdown(curve),
    }


def _simulate_rebalances(
    monthly_prices: pd.DataFrame,
    weights: pd.DataFrame,
    initial_capital: float,
    *,
    trade_cost_model: TradeCostModel | None = None,
    slippage_bps: float = 0.0,
) -> tuple[pd.Series, pd.DataFrame]:
    if monthly_prices.empty:
        return (
            pd.Series(dtype=float),
            pd.DataFrame(
                columns=[
                    "rebalance_date",
                    "symbol",
                    "side",
                    "trade_price",
                    "trade_qty",
                    "trade_notional",
                    "pre_trade_weight",
                    "target_weight",
                    "pre_trade_position_qty",
                    "post_trade_position_qty",
                    "portfolio_value",
                    "fees_total",
                    "fee_commission",
                    "fee_platform",
                    "fee_settlement",
                    "fee_sec",
                    "fee_taf",
                    "fee_source",
                ]
            ),
        )

    quantities = pd.Series(0.0, index=monthly_prices.columns, dtype=float)
    cash = float(initial_capital)
    rows: list[dict[str, float | str]] = []
    curve_values: list[float] = []
    curve_index: list[pd.Timestamp] = []

    for rebalance_date, target_weights in weights.fillna(0.0).iterrows():
        prices = monthly_prices.loc[rebalance_date].astype(float)
        portfolio_value = float(cash + (quantities * prices).sum())
        if portfolio_value <= 0:
            continue

        target_quantities = (target_weights * portfolio_value / prices).replace([math.inf, -math.inf], 0.0).fillna(0.0)
        delta_quantities = target_quantities - quantities

        for symbol, delta_qty in delta_quantities.items():
            if abs(delta_qty) <= 1e-12:
                continue
            if delta_qty >= 0:
                continue

            price = float(prices[symbol])
            trade_price = buffered_trade_price(price, "SELL", slippage_bps)
            pre_trade_qty = float(quantities[symbol])
            post_trade_qty = float(target_quantities[symbol])
            pre_trade_weight = float(pre_trade_qty * price / portfolio_value) if portfolio_value > 0 else 0.0
            target_weight = float(target_weights[symbol])
            qty = float(abs(delta_qty))
            notional = float(qty * trade_price)
            breakdown = estimate_trade_cost(
                "SELL",
                qty,
                trade_price,
                timestamp=rebalance_date,
                model=trade_cost_model,
            )
            cash += notional - breakdown.total
            quantities[symbol] = max(0.0, pre_trade_qty - qty)

            rows.append(
                {
                    "rebalance_date": rebalance_date,
                    "symbol": symbol,
                    "side": "SELL",
                    "trade_price": trade_price,
                    "trade_qty": qty,
                    "trade_notional": notional,
                    "pre_trade_weight": pre_trade_weight,
                    "target_weight": target_weight,
                    "pre_trade_position_qty": pre_trade_qty,
                    "post_trade_position_qty": float(quantities[symbol]),
                    "portfolio_value": portfolio_value,
                    **breakdown.as_dict(),
                }
            )

        for symbol, delta_qty in delta_quantities.items():
            if abs(delta_qty) <= 1e-12:
                continue
            if delta_qty <= 0:
                continue

            price = float(prices[symbol])
            trade_price = buffered_trade_price(price, "BUY", slippage_bps)
            pre_trade_qty = float(quantities[symbol])
            pre_trade_weight = float(pre_trade_qty * price / portfolio_value) if portfolio_value > 0 else 0.0
            target_weight = float(target_weights[symbol])
            requested_qty = int(math.floor(float(delta_qty)))
            affordable_qty = max_affordable_buy_quantity(
                cash,
                trade_price,
                requested_qty,
                timestamp=rebalance_date,
                model=trade_cost_model,
            )
            if affordable_qty <= 0:
                continue
            breakdown = estimate_trade_cost(
                "BUY",
                affordable_qty,
                trade_price,
                timestamp=rebalance_date,
                model=trade_cost_model,
            )
            notional = float(affordable_qty * trade_price)
            cash -= notional + breakdown.total
            quantities[symbol] = pre_trade_qty + affordable_qty

            rows.append(
                {
                    "rebalance_date": rebalance_date,
                    "symbol": symbol,
                    "side": "BUY",
                    "trade_price": trade_price,
                    "trade_qty": float(affordable_qty),
                    "trade_notional": notional,
                    "pre_trade_weight": pre_trade_weight,
                    "target_weight": target_weight,
                    "pre_trade_position_qty": pre_trade_qty,
                    "post_trade_position_qty": float(quantities[symbol]),
                    "portfolio_value": portfolio_value,
                    **breakdown.as_dict(),
                }
            )

        post_trade_value = float(cash + (quantities * prices).sum())
        curve_index.append(pd.Timestamp(rebalance_date))
        curve_values.append(post_trade_value)

    if not rows:
        return (
            pd.Series(curve_values, index=pd.to_datetime(curve_index), dtype=float),
            pd.DataFrame(
                columns=[
                    "rebalance_date",
                    "symbol",
                    "side",
                    "trade_price",
                    "trade_qty",
                    "trade_notional",
                    "pre_trade_weight",
                    "target_weight",
                    "pre_trade_position_qty",
                    "post_trade_position_qty",
                    "portfolio_value",
                    "fees_total",
                    "fee_commission",
                    "fee_platform",
                    "fee_settlement",
                    "fee_sec",
                    "fee_taf",
                    "fee_source",
                ]
            ),
        )

    rebalance_log = pd.DataFrame(rows)
    rebalance_log["rebalance_date"] = pd.to_datetime(rebalance_log["rebalance_date"])
    curve = pd.Series(curve_values, index=pd.to_datetime(curve_index), dtype=float)
    return curve, rebalance_log


def run_backtest(
    daily_closes: pd.DataFrame,
    lookback_months: int,
    benchmark_symbol: str,
    initial_capital: float = 1_000_000.0,
    *,
    trade_cost_model: TradeCostModel | None = None,
    slippage_bps: float = 0.0,
) -> BacktestResult:
    monthly_prices = monthly_closes(daily_closes)
    weights = compute_target_weights(monthly_prices, lookback_months)
    portfolio_value_curve, rebalance_log = _simulate_rebalances(
        monthly_prices,
        weights,
        initial_capital,
        trade_cost_model=trade_cost_model,
        slippage_bps=slippage_bps,
    )
    if portfolio_value_curve.empty and not monthly_prices.empty:
        portfolio_value_curve = pd.Series(float(initial_capital), index=monthly_prices.index, dtype=float)
    strategy_curve = portfolio_value_curve / max(float(initial_capital), 1e-9)
    strategy_returns = strategy_curve.pct_change().fillna(0.0)

    monthly_returns = monthly_prices.pct_change().fillna(0.0)
    benchmark_returns = monthly_returns[benchmark_symbol]
    benchmark_curve = (1 + benchmark_returns).cumprod()
    summary = performance_summary(strategy_returns, strategy_curve)
    summary["final_portfolio_value"] = float(portfolio_value_curve.iloc[-1]) if not portfolio_value_curve.empty else 0.0
    summary["total_fees"] = trade_log_total_fees(rebalance_log)
    summary["gross_pnl"] = float(summary["final_portfolio_value"] - initial_capital + summary["total_fees"])
    return BacktestResult(
        monthly_returns=strategy_returns,
        equity_curve=strategy_curve,
        portfolio_value_curve=portfolio_value_curve,
        benchmark_returns=benchmark_returns,
        benchmark_curve=benchmark_curve,
        weights=weights,
        rebalance_log=rebalance_log,
        summary=summary,
    )
