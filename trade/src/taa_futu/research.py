from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
import math

import pandas as pd

from . import market_logger
from .backtest import run_backtest
from .cascade_sleeve import cascade_trade_symbols, generate_replay_cascade_plan
from .config import Settings
from .costs import (
    build_trade_cost_model,
    buffered_trade_price,
    estimate_realized_from_fills,
    max_affordable_buy_quantity,
    trade_cash_delta,
    trade_log_total_fees,
    with_trade_costs,
)
from .fusion_intraday import build_target_weights, compute_benchmark_score, compute_symbol_feature
from .strategy_stack import baseline_sleeve_enabled, effective_fusion_settings, stack_allocations, stack_label


@dataclass(frozen=True)
class ReplayResult:
    name: str
    portfolio_value_curve: pd.Series
    benchmark_curve: pd.Series
    trade_log: pd.DataFrame
    summary: dict[str, float]
    note: str = ""


def normalize_kline(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    data = frame.copy()
    timestamp_col = "time_key" if "time_key" in data.columns else data.columns[0]
    data["timestamp"] = pd.to_datetime(data[timestamp_col])
    for column in ["open", "high", "low", "close", "volume"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return (
        data[["timestamp", "open", "high", "low", "close", "volume"]]
        .dropna(subset=["timestamp", "open", "high", "low", "close"])
        .sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="last")
        .reset_index(drop=True)
    )


def _intraday_bars_per_year() -> float:
    return 252.0 * 390.0


def _max_drawdown(curve: pd.Series) -> float:
    if curve.empty:
        return 0.0
    drawdown = curve / curve.cummax() - 1
    return float(drawdown.min())


def _optional_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(numeric):
        return None
    return numeric


def _period_summary(curve: pd.Series, initial_capital: float, trade_log: pd.DataFrame) -> dict[str, float]:
    total_fees = trade_log_total_fees(trade_log)
    if curve.empty:
        return {
            "final_value": initial_capital,
            "net_pnl": 0.0,
            "gross_pnl": 0.0,
            "total_return": 0.0,
            "volatility": 0.0,
            "max_drawdown": 0.0,
            "trade_count": 0.0,
            "total_fees": 0.0,
        }
    base_curve = curve / max(initial_capital, 1e-9)
    returns = base_curve.pct_change().fillna(0.0)
    volatility = float(returns.std(ddof=0) * math.sqrt(_intraday_bars_per_year())) if len(returns) > 1 else 0.0
    return {
        "final_value": float(curve.iloc[-1]),
        "net_pnl": float(curve.iloc[-1] - initial_capital),
        "gross_pnl": float(curve.iloc[-1] - initial_capital + total_fees),
        "total_return": float(curve.iloc[-1] / max(initial_capital, 1e-9) - 1),
        "volatility": volatility,
        "max_drawdown": _max_drawdown(curve),
        "trade_count": float(len(trade_log)),
        "total_fees": float(total_fees),
    }


def _pnl_summary(curve: pd.Series, trade_log: pd.DataFrame) -> dict[str, float]:
    total_fees = trade_log_total_fees(trade_log)
    if curve.empty:
        return {
            "final_value": 0.0,
            "net_pnl": 0.0,
            "gross_pnl": 0.0,
            "total_return": 0.0,
            "volatility": 0.0,
            "max_drawdown": 0.0,
            "worst_pnl": 0.0,
            "trade_count": 0.0,
            "total_fees": 0.0,
        }
    return {
        "final_value": float(curve.iloc[-1]),
        "net_pnl": float(curve.iloc[-1]),
        "gross_pnl": float(curve.iloc[-1] + total_fees),
        "total_return": 0.0,
        "volatility": 0.0,
        "max_drawdown": 0.0,
        "worst_pnl": float(curve.min()),
        "trade_count": float(len(trade_log)),
        "total_fees": float(total_fees),
    }


def _daily_summary(curve: pd.Series, initial_capital: float, trade_log: pd.DataFrame) -> dict[str, float]:
    total_fees = trade_log_total_fees(trade_log)
    if curve.empty:
        return {
            "final_value": initial_capital,
            "net_pnl": 0.0,
            "gross_pnl": 0.0,
            "total_return": 0.0,
            "volatility": 0.0,
            "max_drawdown": 0.0,
            "trade_count": 0.0,
            "total_fees": 0.0,
        }
    returns = curve.pct_change().fillna(0.0)
    volatility = float(returns.std(ddof=0) * math.sqrt(252.0)) if len(returns) > 1 else 0.0
    return {
        "final_value": float(curve.iloc[-1]),
        "net_pnl": float(curve.iloc[-1] - initial_capital),
        "gross_pnl": float(curve.iloc[-1] - initial_capital + total_fees),
        "total_return": float(curve.iloc[-1] / max(initial_capital, 1e-9) - 1),
        "volatility": volatility,
        "max_drawdown": _max_drawdown(curve),
        "trade_count": float(len(trade_log)),
        "total_fees": float(total_fees),
    }


def _daily_close_curve(series: pd.Series) -> pd.Series:
    if series.empty:
        return series
    curve = series.copy()
    curve.index = pd.to_datetime(curve.index).normalize()
    return curve.groupby(level=0).last().sort_index()


def _reindex_curve(series: pd.Series, master_index: pd.Index, initial_value: float) -> pd.Series:
    if master_index.empty:
        return pd.Series(dtype=float)
    if series.empty:
        return pd.Series(initial_value, index=master_index, dtype=float)
    return series.reindex(master_index).ffill().fillna(initial_value)


def _constant_curve(master_index: pd.Index, value: float) -> pd.Series:
    if master_index.empty:
        return pd.Series(dtype=float)
    return pd.Series(value, index=master_index, dtype=float)


def _baseline_daily_replay(
    daily_closes: pd.DataFrame,
    settings: Settings,
    *,
    initial_capital: float,
) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    prices = daily_closes.sort_index().dropna(how="all")
    if prices.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.DataFrame()
    monthly_result = run_backtest(
        prices,
        settings.lookback_months,
        settings.benchmark,
        initial_capital,
        trade_cost_model=build_trade_cost_model(settings),
        slippage_bps=settings.futu_price_buffer_bps,
    )
    trade_log = pd.DataFrame()
    if not monthly_result.rebalance_log.empty:
        trade_log = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(monthly_result.rebalance_log["rebalance_date"]),
                "code": monthly_result.rebalance_log["symbol"],
                "side": monthly_result.rebalance_log["side"],
                "qty": monthly_result.rebalance_log["trade_qty"],
                "price": monthly_result.rebalance_log["trade_price"],
                "notional": monthly_result.rebalance_log["trade_notional"],
                "strategy": "Baseline Strategy",
                "fees_total": monthly_result.rebalance_log["fees_total"],
                "fee_commission": monthly_result.rebalance_log["fee_commission"],
                "fee_platform": monthly_result.rebalance_log["fee_platform"],
                "fee_settlement": monthly_result.rebalance_log["fee_settlement"],
                "fee_sec": monthly_result.rebalance_log["fee_sec"],
                "fee_taf": monthly_result.rebalance_log["fee_taf"],
                "fee_source": monthly_result.rebalance_log["fee_source"],
            }
        )
    return monthly_result.portfolio_value_curve, monthly_result.benchmark_curve * float(initial_capital), trade_log

def _bar_snapshot(bars: pd.DataFrame, index: int) -> pd.Series:
    row = bars.iloc[index]
    prev_close = float(bars.iloc[index - 1]["close"]) if index > 0 else float(row["open"])
    last_price = float(row["close"])
    spread = max(last_price * 0.0002, 0.01)
    return pd.Series(
        {
            "last_price": last_price,
            "prev_close_price": prev_close,
            "price_spread": spread,
            "bid_vol": float(row.get("volume", 0.0) or 0.0) / 2.0,
            "ask_vol": float(row.get("volume", 0.0) or 0.0) / 2.0,
        }
    )


def _rebalance_to_weights(
    *,
    timestamp: pd.Timestamp,
    prices: dict[str, float],
    target_weights: dict[str, float],
    current_qty: dict[str, int],
    cash: float,
    slippage_bps: float,
    trade_cost_model,
) -> tuple[dict[str, int], float, list[dict[str, object]]]:
    positions_value = sum(current_qty.get(code, 0) * prices[code] for code in prices if code in current_qty)
    portfolio_value = float(cash + positions_value)
    if portfolio_value <= 0:
        return current_qty, cash, []

    desired_qty: dict[str, int] = {}
    for code, price in prices.items():
        target_weight = float(target_weights.get(code, 0.0))
        if target_weight <= 0 or price <= 0:
            desired_qty[code] = 0
            continue
        desired_qty[code] = int(max(0.0, math.floor(portfolio_value * target_weight / price)))

    trade_rows: list[dict[str, object]] = []
    updated_qty = dict(current_qty)
    updated_cash = float(cash)

    for code in prices:
        delta = desired_qty.get(code, 0) - updated_qty.get(code, 0)
        if delta >= 0:
            continue
        last_price = float(prices[code])
        executed_price = buffered_trade_price(last_price, "SELL", slippage_bps)
        qty = int(abs(delta))
        cash_delta, breakdown = trade_cash_delta(
            "SELL",
            qty,
            executed_price,
            timestamp=timestamp,
            model=trade_cost_model,
        )
        updated_cash += cash_delta
        updated_qty[code] = updated_qty.get(code, 0) - qty
        trade_rows.append(
            {
                "timestamp": timestamp,
                "code": code,
                "side": "SELL",
                "qty": qty,
                "price": executed_price,
                "notional": qty * executed_price,
                "target_weight": float(target_weights.get(code, 0.0)),
                **breakdown.as_dict(),
            }
        )

    for code in prices:
        delta = desired_qty.get(code, 0) - updated_qty.get(code, 0)
        if delta <= 0:
            continue
        last_price = float(prices[code])
        executed_price = buffered_trade_price(last_price, "BUY", slippage_bps)
        qty = int(delta)
        affordable_qty = max_affordable_buy_quantity(
            updated_cash,
            executed_price,
            qty,
            timestamp=timestamp,
            model=trade_cost_model,
        ) if executed_price > 0 else 0
        qty = min(qty, affordable_qty)
        if qty <= 0:
            continue
        cash_delta, breakdown = trade_cash_delta(
            "BUY",
            qty,
            executed_price,
            timestamp=timestamp,
            model=trade_cost_model,
        )
        updated_cash += cash_delta
        updated_qty[code] = updated_qty.get(code, 0) + qty
        trade_rows.append(
            {
                "timestamp": timestamp,
                "code": code,
                "side": "BUY",
                "qty": qty,
                "price": executed_price,
                "notional": qty * executed_price,
                "target_weight": float(target_weights.get(code, 0.0)),
                **breakdown.as_dict(),
            }
        )

    return updated_qty, float(updated_cash), trade_rows


def _value_curve(
    timeline: pd.Index,
    price_frames: dict[str, pd.DataFrame],
    quantities_by_time: list[dict[str, int]],
    cash_by_time: list[float],
) -> pd.Series:
    price_lookup = {
        code: frame.set_index("timestamp")["close"].reindex(timeline).ffill().bfill()
        for code, frame in price_frames.items()
    }
    values: list[float] = []
    for idx, timestamp in enumerate(timeline):
        positions_value = 0.0
        for code, qty in quantities_by_time[idx].items():
            if qty == 0:
                continue
            price = float(price_lookup[code].loc[timestamp])
            positions_value += qty * price
        values.append(float(cash_by_time[idx] + positions_value))
    return pd.Series(values, index=pd.to_datetime(timeline), name="portfolio_value")


def _benchmark_curve(frame: pd.DataFrame, timeline: pd.Index, initial_capital: float) -> pd.Series:
    if frame.empty or timeline.empty:
        return pd.Series(dtype=float)
    close = frame.set_index("timestamp")["close"].reindex(timeline).ffill().bfill()
    base = float(close.iloc[0]) if not close.empty else 1.0
    return close / max(base, 1e-9) * float(initial_capital)


def _empty_ticks() -> pd.DataFrame:
    return pd.DataFrame(columns=["price", "volume", "ticker_direction"])


def _price_at_or_before(frame: pd.DataFrame, timestamp: pd.Timestamp) -> float | None:
    series = frame.set_index("timestamp")["close"]
    value = series.asof(pd.Timestamp(timestamp))
    if pd.isna(value):
        return None
    return float(value)


def run_fusion_intraday_replay(
    price_frames: dict[str, pd.DataFrame],
    settings: Settings,
    *,
    initial_capital: float,
) -> ReplayResult:
    benchmark = settings.fusion_benchmark
    benchmark_frame = normalize_kline(price_frames.get(benchmark, pd.DataFrame()))
    if benchmark_frame.empty:
        return ReplayResult("Fusion Intraday", pd.Series(dtype=float), pd.Series(dtype=float), pd.DataFrame(), _period_summary(pd.Series(dtype=float), initial_capital, pd.DataFrame()), "缺少基准分钟线 / Missing benchmark bars.")

    normalized: dict[str, pd.DataFrame] = {}
    for code, frame in price_frames.items():
        normalized_frame = normalize_kline(frame)
        if not normalized_frame.empty:
            normalized[code] = normalized_frame
    if benchmark not in normalized:
        normalized[benchmark] = benchmark_frame
    timeline = pd.Index(benchmark_frame["timestamp"])
    if timeline.empty:
        return ReplayResult("Fusion Intraday", pd.Series(dtype=float), pd.Series(dtype=float), pd.DataFrame(), _period_summary(pd.Series(dtype=float), initial_capital, pd.DataFrame()), "分钟线为空 / Empty timeline.")

    quantities: dict[str, int] = {}
    cash = float(initial_capital)
    trade_rows: list[dict[str, object]] = []
    quantities_by_time: list[dict[str, int]] = []
    cash_by_time: list[float] = []
    last_session: date | None = None
    no_ticks = _empty_ticks()
    trade_cost_model = build_trade_cost_model(settings)

    for idx, timestamp in enumerate(timeline):
        current_day = pd.Timestamp(timestamp).date()
        if last_session is not None and current_day != last_session:
            prices = {
                code: float(frame.set_index("timestamp").loc[last_timestamp]["close"])
                for code, frame in normalized.items()
                if last_timestamp in frame.set_index("timestamp").index
            }
            quantities, cash, exit_trades = _rebalance_to_weights(
                timestamp=pd.Timestamp(last_timestamp),
                prices=prices,
                target_weights={},
                current_qty=quantities,
                cash=cash,
                slippage_bps=settings.futu_price_buffer_bps,
                trade_cost_model=trade_cost_model,
            )
            for row in exit_trades:
                row["strategy"] = "Fusion Intraday"
            trade_rows.extend(exit_trades)
        last_session = current_day
        last_timestamp = timestamp

        benchmark_window = normalized[benchmark][normalized[benchmark]["timestamp"] <= timestamp].tail(settings.fusion_lookback_bars)
        if len(benchmark_window) < max(settings.fusion_lookback_bars, 20):
            quantities_by_time.append(dict(quantities))
            cash_by_time.append(float(cash))
            continue

        benchmark_snapshot = _bar_snapshot(benchmark_window, len(benchmark_window) - 1)
        benchmark_score = compute_benchmark_score(
            benchmark_window,
            benchmark_snapshot,
            None,
            no_ticks,
            settings,
        )

        features = []
        for code in settings.fusion_universe:
            frame = normalized.get(code)
            if frame is None or frame.empty:
                continue
            window = frame[frame["timestamp"] <= timestamp].tail(settings.fusion_lookback_bars)
            if len(window) < max(settings.fusion_lookback_bars, 20):
                continue
            snapshot = _bar_snapshot(window, len(window) - 1)
            feature = compute_symbol_feature(
                code,
                window,
                snapshot,
                None,
                no_ticks,
                benchmark_score,
                settings,
            )
            features.append(feature)

        held_symbols = {code for code, qty in quantities.items() if qty > 0}
        _exposure, target_weights = build_target_weights(features, benchmark_score, held_symbols, settings)
        prices = {}
        for code, frame in normalized.items():
            if code == benchmark:
                continue
            price = _price_at_or_before(frame, pd.Timestamp(timestamp))
            if price is not None:
                prices[code] = price
        quantities, cash, rebalance_rows = _rebalance_to_weights(
            timestamp=pd.Timestamp(timestamp),
            prices=prices,
            target_weights=target_weights,
            current_qty=quantities,
            cash=cash,
            slippage_bps=settings.futu_price_buffer_bps,
            trade_cost_model=trade_cost_model,
        )
        for row in rebalance_rows:
            row["strategy"] = "Fusion Intraday"
            row["benchmark_score"] = round(float(benchmark_score), 6)
        trade_rows.extend(rebalance_rows)
        quantities_by_time.append(dict(quantities))
        cash_by_time.append(float(cash))

    prices = {
        code: float(frame["close"].iloc[-1])
        for code, frame in normalized.items()
        if code != benchmark and not frame.empty
    }
    quantities, cash, exit_rows = _rebalance_to_weights(
        timestamp=pd.Timestamp(timeline[-1]),
        prices=prices,
        target_weights={},
        current_qty=quantities,
        cash=cash,
        slippage_bps=settings.futu_price_buffer_bps,
        trade_cost_model=trade_cost_model,
    )
    for row in exit_rows:
        row["strategy"] = "Fusion Intraday"
    trade_rows.extend(exit_rows)
    if quantities_by_time:
        quantities_by_time[-1] = dict(quantities)
        cash_by_time[-1] = float(cash)

    symbol_frames = {code: frame for code, frame in normalized.items() if code != benchmark}
    curve = _value_curve(timeline, symbol_frames, quantities_by_time, cash_by_time)
    trade_log = pd.DataFrame(trade_rows)
    summary = _period_summary(curve, initial_capital, trade_log)
    return ReplayResult(
        name="Fusion Intraday",
        portfolio_value_curve=curve,
        benchmark_curve=_benchmark_curve(benchmark_frame, timeline, initial_capital),
        trade_log=trade_log,
        summary=summary,
        note="Fusion 回放是价格驱动近似版：保留 Gap / 动量 / VWAP / 量能 / 开盘区间逻辑，不使用历史 LOB 和逐笔重建。",
    )


def _ofim_proxy_settings(settings: Settings) -> Settings:
    return replace(
        settings,
        fusion_universe=settings.ofim_universe,
        fusion_benchmark=settings.ofim_benchmark,
        fusion_lookback_bars=settings.ofim_lookback_bars,
        fusion_entry_score=settings.ofim_entry_threshold,
        fusion_exit_score=settings.ofim_exit_threshold,
        fusion_max_position_weight=settings.ofim_max_position_weight,
        fusion_max_gross_exposure=settings.ofim_max_gross_exposure,
        fusion_max_spread_bps=settings.ofim_max_spread_bps,
        fusion_tick_window=settings.ofim_tick_window,
        fusion_order_book_depth=min(max(int(settings.ofim_order_book_depth), 1), 10),
    )


def run_ofim_intraday_replay(
    price_frames: dict[str, pd.DataFrame],
    settings: Settings,
    *,
    initial_capital: float,
) -> ReplayResult:
    replay = run_fusion_intraday_replay(price_frames, _ofim_proxy_settings(settings), initial_capital=initial_capital)
    trade_log = replay.trade_log.copy()
    if not trade_log.empty:
        trade_log["strategy"] = "OFIM Intraday"
    return ReplayResult(
        name="OFIM Intraday",
        portfolio_value_curve=replay.portfolio_value_curve,
        benchmark_curve=replay.benchmark_curve,
        trade_log=trade_log,
        summary=replay.summary,
        note="OFIM 回放目前复用 Fusion 近似回放引擎；后续会接入 OFIM 精确回放。",
    )


def run_cascade_replay(
    price_frames: dict[str, pd.DataFrame],
    settings: Settings,
    *,
    initial_capital: float,
) -> ReplayResult:
    normalized = {code: normalize_kline(frame) for code, frame in price_frames.items() if not frame.empty}
    normalized = {code: frame for code, frame in normalized.items() if not frame.empty}
    if not normalized:
        return ReplayResult(
            "Claude/Cascade",
            pd.Series(dtype=float),
            pd.Series(dtype=float),
            pd.DataFrame(),
            _daily_summary(pd.Series(dtype=float), initial_capital, pd.DataFrame()),
            "缺少 Cascade 日线 / Missing daily bars for Claude/Cascade replay.",
        )

    timeline_values = sorted({timestamp for frame in normalized.values() for timestamp in pd.to_datetime(frame["timestamp"]).tolist()})
    if not timeline_values:
        return ReplayResult(
            "Claude/Cascade",
            pd.Series(dtype=float),
            pd.Series(dtype=float),
            pd.DataFrame(),
            _daily_summary(pd.Series(dtype=float), initial_capital, pd.DataFrame()),
            "Cascade 没有可回放的时间轴 / No replay timeline for Claude/Cascade.",
        )

    timeline = pd.Index(pd.to_datetime(timeline_values))
    trade_cost_model = build_trade_cost_model(settings)
    cash = float(initial_capital)
    quantities: dict[str, int] = {}
    quantities_by_time: list[dict[str, int]] = []
    cash_by_time: list[float] = []
    trade_rows: list[dict[str, object]] = []
    last_note = ""

    tradable_symbols = set(cascade_trade_symbols(settings))

    for timestamp in timeline:
        plan = generate_replay_cascade_plan(normalized, settings, as_of=pd.Timestamp(timestamp))
        last_note = plan.note or last_note
        prices = {}
        for code, frame in normalized.items():
            if code not in tradable_symbols:
                continue
            price = _price_at_or_before(frame, pd.Timestamp(timestamp))
            if price is not None:
                prices[code] = price

        quantities, cash, rebalance_rows = _rebalance_to_weights(
            timestamp=pd.Timestamp(timestamp),
            prices=prices,
            target_weights=plan.target_weights,
            current_qty=quantities,
            cash=cash,
            slippage_bps=settings.futu_price_buffer_bps,
            trade_cost_model=trade_cost_model,
        )
        for row in rebalance_rows:
            row["strategy"] = "Claude/Cascade"
            row["regime"] = plan.regime_label
            row["regime_score"] = round(float(plan.regime_score), 6)
        trade_rows.extend(rebalance_rows)
        quantities_by_time.append(dict(quantities))
        cash_by_time.append(float(cash))

    symbol_frames = {code: frame for code, frame in normalized.items() if code in tradable_symbols}
    curve = _value_curve(timeline, symbol_frames, quantities_by_time, cash_by_time)
    benchmark_source = normalized["US.SPY"] if "US.SPY" in normalized else next(iter(normalized.values()))
    trade_log = pd.DataFrame(trade_rows)
    summary = _daily_summary(curve, initial_capital, trade_log)
    return ReplayResult(
        name="Claude/Cascade",
        portfolio_value_curve=curve,
        benchmark_curve=_benchmark_curve(benchmark_source, timeline, initial_capital),
        trade_log=trade_log,
        summary=summary,
        note=(
            "Claude/Cascade 回放按 claude-trade 的级联日频逻辑运行。"
            + (f" {last_note}" if last_note else "")
        ),
    )


def run_strategy_stack_replay(
    baseline_daily_closes: pd.DataFrame,
    fusion_price_frames: dict[str, pd.DataFrame],
    settings: Settings,
    *,
    initial_capital: float,
    cascade_price_frames: dict[str, pd.DataFrame] | None = None,
) -> ReplayResult:
    baseline_weight, fusion_weight, ofim_weight, cascade_weight, reserve_weight = stack_allocations(settings)
    fusion_settings = effective_fusion_settings(settings)

    baseline_initial = float(initial_capital) * baseline_weight if baseline_sleeve_enabled(settings) else 0.0
    fusion_initial = float(initial_capital) * fusion_weight
    ofim_initial = float(initial_capital) * ofim_weight
    cascade_initial = float(initial_capital) * cascade_weight
    reserve_initial = float(initial_capital) * reserve_weight

    baseline_curve = pd.Series(dtype=float)
    benchmark_curve = pd.Series(dtype=float)
    baseline_log = pd.DataFrame()
    if baseline_sleeve_enabled(settings) and baseline_initial > 0:
        baseline_curve, benchmark_curve, baseline_log = _baseline_daily_replay(
            baseline_daily_closes,
            settings,
            initial_capital=baseline_initial,
        )

    fusion_replay = run_fusion_intraday_replay(
        {
            code: frame
            for code, frame in fusion_price_frames.items()
            if code in {fusion_settings.fusion_benchmark, *fusion_settings.fusion_universe}
        },
        fusion_settings,
        initial_capital=fusion_initial,
    ) if fusion_initial > 0 else ReplayResult("Fusion Intraday", pd.Series(dtype=float), pd.Series(dtype=float), pd.DataFrame(), _period_summary(pd.Series(dtype=float), fusion_initial, pd.DataFrame()))
    fusion_curve = _daily_close_curve(fusion_replay.portfolio_value_curve)

    ofim_replay = run_ofim_intraday_replay(
        {
            code: frame
            for code, frame in fusion_price_frames.items()
            if code in {settings.ofim_benchmark, *settings.ofim_universe}
        },
        settings,
        initial_capital=ofim_initial,
    ) if ofim_initial > 0 else ReplayResult("OFIM Intraday", pd.Series(dtype=float), pd.Series(dtype=float), pd.DataFrame(), _period_summary(pd.Series(dtype=float), ofim_initial, pd.DataFrame()))
    ofim_curve = _daily_close_curve(ofim_replay.portfolio_value_curve)

    cascade_replay = run_cascade_replay(
        cascade_price_frames or {},
        settings,
        initial_capital=cascade_initial,
    ) if cascade_initial > 0 else ReplayResult("Claude/Cascade", pd.Series(dtype=float), pd.Series(dtype=float), pd.DataFrame(), _daily_summary(pd.Series(dtype=float), cascade_initial, pd.DataFrame()))
    cascade_curve = _daily_close_curve(cascade_replay.portfolio_value_curve)

    daily_curves = {
        "baseline": _daily_close_curve(baseline_curve),
        "fusion": fusion_curve,
        "ofim": ofim_curve,
        "cascade": cascade_curve,
        "reserve": pd.Series(dtype=float),
    }
    master_dates = sorted(
        {
            *pd.to_datetime(daily_curves["baseline"].index).tolist(),
            *pd.to_datetime(daily_curves["fusion"].index).tolist(),
            *pd.to_datetime(daily_curves["ofim"].index).tolist(),
            *pd.to_datetime(daily_curves["cascade"].index).tolist(),
            *pd.to_datetime(daily_curves["reserve"].index).tolist(),
        }
    )
    if not master_dates:
        return ReplayResult(
            name="Strategy Stack",
            portfolio_value_curve=pd.Series(dtype=float),
            benchmark_curve=pd.Series(dtype=float),
            trade_log=pd.DataFrame(),
            summary=_daily_summary(pd.Series(dtype=float), initial_capital, pd.DataFrame()),
            note="组合回测没有可用数据 / No data available for stack replay.",
        )

    master_index = pd.Index(pd.to_datetime(master_dates))
    combined_curve = (
        _reindex_curve(daily_curves["baseline"], master_index, baseline_initial)
        + _reindex_curve(daily_curves["fusion"], master_index, fusion_initial)
        + _reindex_curve(daily_curves["ofim"], master_index, ofim_initial)
        + _reindex_curve(daily_curves["cascade"], master_index, cascade_initial)
        + _constant_curve(master_index, reserve_initial)
    )

    benchmark_daily = _daily_close_curve(benchmark_curve)
    if benchmark_daily.empty and not fusion_replay.benchmark_curve.empty:
        benchmark_daily = _daily_close_curve(fusion_replay.benchmark_curve)
    if not benchmark_daily.empty:
        benchmark_daily = benchmark_daily / max(float(benchmark_daily.iloc[0]), 1e-9) * float(initial_capital)
        benchmark_daily = _reindex_curve(benchmark_daily, master_index, float(initial_capital))
    else:
        benchmark_daily = pd.Series(dtype=float)

    trade_log = pd.concat(
        [
            baseline_log,
            fusion_replay.trade_log,
            ofim_replay.trade_log,
            cascade_replay.trade_log,
        ],
        ignore_index=True,
    ) if any(not frame.empty for frame in [baseline_log, fusion_replay.trade_log, ofim_replay.trade_log, cascade_replay.trade_log]) else pd.DataFrame()
    if not trade_log.empty:
        trade_log = trade_log.sort_values("timestamp").reset_index(drop=True)

    summary = _daily_summary(combined_curve, float(initial_capital), trade_log)
    summary["baseline_alloc"] = round(baseline_weight, 6)
    summary["fusion_alloc"] = round(fusion_weight, 6)
    summary["ofim_alloc"] = round(ofim_weight, 6)
    summary["cascade_alloc"] = round(cascade_weight, 6)
    summary["reserve_alloc"] = round(reserve_weight, 6)
    return ReplayResult(
        name="Strategy Stack",
        portfolio_value_curve=combined_curve,
        benchmark_curve=benchmark_daily,
        trade_log=trade_log,
        summary=summary,
        note=(
            "组合回测按 sleeve 分仓运行："
            f"{stack_label(settings)}。Baseline 用月频趋势，Fusion 用分钟级近似回放，"
            "OFIM 复用分钟级近似回放代理，Claude/Cascade 用 claude-trade 的级联日频逻辑，"
            "剩余未分配仓位保留为现金。"
        ),
    )


def _filled_orders(order_history: pd.DataFrame) -> pd.DataFrame:
    if order_history.empty:
        return order_history
    rows = order_history.copy()
    rows["dealt_qty_num"] = pd.to_numeric(rows.get("dealt_qty"), errors="coerce").fillna(0.0)
    rows["dealt_price_num"] = pd.to_numeric(rows.get("dealt_avg_price"), errors="coerce").fillna(0.0)
    rows = rows[rows["dealt_qty_num"] > 0].copy()
    if rows.empty:
        return rows
    sort_columns = [column for column in ["updated_time", "create_time"] if column in rows.columns]
    if sort_columns:
        rows = rows.sort_values(sort_columns, ascending=True)
    rows["timestamp"] = pd.to_datetime(rows["updated_time"])
    return rows


def _classify_trade(code: str, settings: Settings) -> str:
    if code in settings.fusion_universe or code == settings.fusion_benchmark:
        return "Fusion Intraday"
    if code in settings.symbols:
        return "Baseline Strategy"
    return "Unclassified"


def run_account_replay(
    order_history: pd.DataFrame,
    price_frames: dict[str, pd.DataFrame],
    settings: Settings,
) -> ReplayResult:
    rows = _filled_orders(order_history)
    if rows.empty:
        return ReplayResult("Account Replay", pd.Series(dtype=float), pd.Series(dtype=float), pd.DataFrame(), _pnl_summary(pd.Series(dtype=float), pd.DataFrame()), "所选区间没有已成交订单 / No filled orders.")

    normalized = {code: normalize_kline(frame) for code, frame in price_frames.items() if not frame.empty}
    timeline_values = sorted({timestamp for frame in normalized.values() for timestamp in frame["timestamp"].tolist()})
    if not timeline_values:
        timeline_values = sorted(rows["timestamp"].tolist())
    timeline = pd.Index(pd.to_datetime(timeline_values))

    rows = with_trade_costs(
        rows,
        settings,
        side_col="trd_side",
        qty_col="dealt_qty_num",
        price_col="dealt_price_num",
        timestamp_col="timestamp",
    )

    cash = 0.0
    positions: dict[str, int] = {}
    trade_rows: list[dict[str, object]] = []
    quantities_by_time: list[dict[str, int]] = []
    cash_by_time: list[float] = []
    row_index = 0
    ordered_rows = list(rows.itertuples(index=False))

    for timestamp in timeline:
        while row_index < len(ordered_rows) and pd.Timestamp(getattr(ordered_rows[row_index], "timestamp")) <= pd.Timestamp(timestamp):
            row = ordered_rows[row_index]
            code = str(getattr(row, "code"))
            side = str(getattr(row, "trd_side"))
            qty = int(float(getattr(row, "dealt_qty_num")))
            price = float(getattr(row, "dealt_price_num"))
            fees_total = float(getattr(row, "fees_total", 0.0))
            if side == "BUY":
                cash -= qty * price + fees_total
                positions[code] = positions.get(code, 0) + qty
            elif side == "SELL":
                cash += qty * price - fees_total
                positions[code] = positions.get(code, 0) - qty
            trade_rows.append(
                {
                    "timestamp": pd.Timestamp(getattr(row, "timestamp")),
                    "code": code,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "notional": qty * price,
                    "strategy": _classify_trade(code, settings),
                    "fees_total": fees_total,
                    "fee_source": getattr(row, "fee_source", "disabled"),
                    "fee_commission": float(getattr(row, "fee_commission", 0.0)),
                    "fee_platform": float(getattr(row, "fee_platform", 0.0)),
                    "fee_settlement": float(getattr(row, "fee_settlement", 0.0)),
                    "fee_sec": float(getattr(row, "fee_sec", 0.0)),
                    "fee_taf": float(getattr(row, "fee_taf", 0.0)),
                }
            )
            row_index += 1
        quantities_by_time.append(dict(positions))
        cash_by_time.append(float(cash))

    curve = _value_curve(timeline, normalized, quantities_by_time, cash_by_time)
    trade_log = pd.DataFrame(trade_rows)
    summary = _pnl_summary(curve, trade_log)
    summary["estimated_realized"] = estimate_realized_from_fills(rows, settings, qty_col="dealt_qty_num", price_col="dealt_price_num", timestamp_col="timestamp")
    summary["ending_exposure"] = float(curve.iloc[-1] - cash_by_time[-1]) if cash_by_time else 0.0
    return ReplayResult(
        name="Account Replay",
        portfolio_value_curve=curve.rename("period_pnl"),
        benchmark_curve=pd.Series(dtype=float),
        trade_log=trade_log,
        summary=summary,
        note="账户复盘展示的是所选区间的净变动曲线，不是整账户绝对资产曲线；页面已按官方富途费率估算成交成本并计入净结果。",
    )


def _logged_submitted_orders(start: str, end: str | None = None) -> pd.DataFrame:
    logged_orders = market_logger.load_order_records(start, end)
    if logged_orders.empty:
        return logged_orders
    submitted = logged_orders.copy()
    submitted["action"] = submitted.get("action", "").astype(str)
    submitted["submit_status"] = submitted.get("submit_status", "").astype(str)
    submitted = submitted[
        (submitted["action"] == "submitted")
        & (submitted["submit_status"].str.lower() == "submitted")
    ].copy()
    if submitted.empty:
        return submitted
    submitted["order_id"] = submitted.get("submit_detail", "").astype(str).str.strip()
    submitted = submitted[submitted["order_id"] != ""].copy()
    return submitted.sort_values(["ts", "code"], na_position="last").reset_index(drop=True)


def run_exact_execution_replay(
    start: str,
    end: str | None,
    order_history: pd.DataFrame,
    price_frames: dict[str, pd.DataFrame],
    settings: Settings,
) -> ReplayResult:
    submitted = _logged_submitted_orders(start, end)
    if submitted.empty:
        return ReplayResult(
            name="Exact Execution Replay",
            portfolio_value_curve=pd.Series(dtype=float),
            benchmark_curve=pd.Series(dtype=float),
            trade_log=pd.DataFrame(),
            summary=_pnl_summary(pd.Series(dtype=float), pd.DataFrame()),
            note="所选区间没有实时落盘的已提交订单日志 / No logged submitted orders in the selected range.",
        )

    rows = _filled_orders(order_history)
    if rows.empty or "order_id" not in rows.columns:
        return ReplayResult(
            name="Exact Execution Replay",
            portfolio_value_curve=pd.Series(dtype=float),
            benchmark_curve=pd.Series(dtype=float),
            trade_log=pd.DataFrame(),
            summary=_pnl_summary(pd.Series(dtype=float), pd.DataFrame()),
            note="Futu 订单历史里没有可匹配的已成交 order_id / No filled order_id records available from Futu.",
        )

    filled = rows.copy()
    filled["order_id"] = filled["order_id"].astype(str)
    submitted_index = (
        submitted.drop_duplicates(subset=["order_id"], keep="last")
        .set_index("order_id")
        .sort_index()
    )
    matched = filled[filled["order_id"].isin(submitted_index.index)].copy()
    if matched.empty:
        return ReplayResult(
            name="Exact Execution Replay",
            portfolio_value_curve=pd.Series(dtype=float),
            benchmark_curve=pd.Series(dtype=float),
            trade_log=pd.DataFrame(),
            summary=_pnl_summary(pd.Series(dtype=float), pd.DataFrame()),
            note="实时落盘里有订单，但在 Futu 已成交历史里没有找到对应 order_id / Logged order IDs were not found in filled Futu history.",
        )

    normalized = {code: normalize_kline(frame) for code, frame in price_frames.items() if not frame.empty}
    timeline_values = sorted({timestamp for frame in normalized.values() for timestamp in frame["timestamp"].tolist()})
    if not timeline_values:
        timeline_values = sorted(matched["timestamp"].tolist())
    timeline = pd.Index(pd.to_datetime(timeline_values))

    matched = with_trade_costs(
        matched,
        settings,
        side_col="trd_side",
        qty_col="dealt_qty_num",
        price_col="dealt_price_num",
        timestamp_col="timestamp",
    )

    cash = 0.0
    positions: dict[str, int] = {}
    trade_rows: list[dict[str, object]] = []
    quantities_by_time: list[dict[str, int]] = []
    cash_by_time: list[float] = []
    row_index = 0
    ordered_rows = list(matched.itertuples(index=False))

    for timestamp in timeline:
        while row_index < len(ordered_rows) and pd.Timestamp(getattr(ordered_rows[row_index], "timestamp")) <= pd.Timestamp(timestamp):
            row = ordered_rows[row_index]
            code = str(getattr(row, "code"))
            side = str(getattr(row, "trd_side"))
            qty = int(float(getattr(row, "dealt_qty_num")))
            price = float(getattr(row, "dealt_price_num"))
            order_id = str(getattr(row, "order_id"))
            meta = submitted_index.loc[order_id]
            fees_total = float(getattr(row, "fees_total", 0.0))
            if side == "BUY":
                cash -= qty * price + fees_total
                positions[code] = positions.get(code, 0) + qty
            elif side == "SELL":
                cash += qty * price - fees_total
                positions[code] = positions.get(code, 0) - qty
            trade_rows.append(
                {
                    "timestamp": pd.Timestamp(getattr(row, "timestamp")),
                    "code": code,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "notional": qty * price,
                    "strategy": "Exact Execution Replay",
                    "order_id": order_id,
                    "decision_time": pd.Timestamp(meta["ts"]) if pd.notna(meta["ts"]) else pd.NaT,
                    "planned_limit": _optional_float(meta.get("limit_price")),
                    "reference_price": _optional_float(meta.get("reference_price")),
                    "fees_total": fees_total,
                    "fee_source": getattr(row, "fee_source", "disabled"),
                    "fee_commission": float(getattr(row, "fee_commission", 0.0)),
                    "fee_platform": float(getattr(row, "fee_platform", 0.0)),
                    "fee_settlement": float(getattr(row, "fee_settlement", 0.0)),
                    "fee_sec": float(getattr(row, "fee_sec", 0.0)),
                    "fee_taf": float(getattr(row, "fee_taf", 0.0)),
                }
            )
            row_index += 1
        quantities_by_time.append(dict(positions))
        cash_by_time.append(float(cash))

    curve = _value_curve(timeline, normalized, quantities_by_time, cash_by_time)
    trade_log = pd.DataFrame(trade_rows)
    summary = _pnl_summary(curve, trade_log)
    summary["estimated_realized"] = estimate_realized_from_fills(matched, settings, qty_col="dealt_qty_num", price_col="dealt_price_num", timestamp_col="timestamp")
    summary["ending_exposure"] = float(curve.iloc[-1] - cash_by_time[-1]) if cash_by_time else 0.0
    summary["logged_submitted_orders"] = float(len(submitted))
    summary["matched_filled_orders"] = float(len(matched))
    return ReplayResult(
        name="Exact Execution Replay",
        portfolio_value_curve=curve.rename("period_pnl"),
        benchmark_curve=pd.Series(dtype=float),
        trade_log=trade_log,
        summary=summary,
        note=(
            "精确执行复盘按 runtime/market_data 里的提交日志和 Futu 实际成交历史用 order_id 对齐。"
            "这不是价格近似回放，而是已执行订单的真实复盘；净结果已计入估算成交成本。"
        ),
    )
