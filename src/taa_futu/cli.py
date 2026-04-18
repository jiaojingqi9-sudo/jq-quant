from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

import pandas as pd
from tabulate import tabulate

from .backtest import run_backtest
from .auto_trader import run_auto_trader
from . import market_logger
from .cascade_sleeve import cascade_trade_symbols, fetch_cascade_daily_frames, generate_live_cascade_plan
from .config import load_settings
from .costs import build_trade_cost_model
from .fusion_intraday import FusionIntradayStrategy
from .ofim_intraday import OfimIntradayStrategy
from .futu_gateway import FutuPaperTrader, FutuTradeError
from .intraday_replay import (
    _iter_day_dirs as _iter_logged_replay_days,
    run_fusion_replay as run_fusion_lob_replay,
    run_ofim_replay as run_ofim_lob_replay,
)
from .market_data import FutuQuoteDataProvider, HistoricalDataProvider, MarketDataError, YFinanceDataProvider
from .research import (
    run_account_replay,
    run_cascade_replay,
    run_exact_execution_replay,
    run_ofim_intraday_replay,
    run_fusion_intraday_replay,
    run_strategy_stack_replay,
)
from .strategy_stack import effective_fusion_settings, stack_allocations, stack_label
from .strategy import latest_completed_signal
from .watchdog import run_watchdog


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reproduce tactical asset allocation and route it into Futu trading.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backtest = subparsers.add_parser("backtest", help="Run a monthly backtest.")
    backtest.add_argument("--start", default=None)
    backtest.add_argument("--end", default=None)
    backtest.add_argument(
        "--strategy",
        choices=("baseline", "fusion", "ofim", "cascade", "stack", "account", "exact"),
        default="baseline",
        help="Choose baseline monthly, Fusion replay, OFIM replay, Claude/Cascade replay, stack replay, account replay, or exact execution replay.",
    )
    backtest.add_argument("--initial-capital", type=float, default=1_000_000.0)

    signals = subparsers.add_parser("signals", help="Show the latest completed-month signal.")
    signals.add_argument("--history-source", choices=("yfinance", "futu"), default="yfinance")
    signals.add_argument("--start", default=None)
    signals.add_argument("--end", default=None)

    paper = subparsers.add_parser("paper-trade", help="Plan or submit Futu orders.")
    paper.add_argument("--history-source", choices=("yfinance", "futu"), default="yfinance")
    paper.add_argument("--start", default=None)
    paper.add_argument("--end", default=None)
    paper.add_argument("--submit", action="store_true", help="Actually submit Futu orders.")

    fusion = subparsers.add_parser("fusion-intraday", help="Run the proprietary Futu intraday hybrid strategy.")
    fusion.add_argument("--submit", action="store_true", help="Actually submit Futu orders.")

    ofim = subparsers.add_parser("ofim-intraday", help="Run the OFIM intraday order-flow strategy (L2 heavy).")
    ofim.add_argument("--submit", action="store_true", help="Actually submit Futu orders.")

    cascade = subparsers.add_parser("cascade-strategy", help="Run the Claude/Cascade sleeve through the shared Futu routing.")
    cascade.add_argument("--submit", action="store_true", help="Actually submit Futu orders.")

    auto_fusion = subparsers.add_parser("auto-fusion", help="Run the Fusion intraday strategy continuously during market hours.")
    auto_fusion.add_argument("--dry-run", action="store_true", help="Monitor continuously but do not submit orders.")

    subparsers.add_parser("watchdog", help="Run the stability watchdog for the trading engine.")
    subparsers.add_parser("real-check", help="Check whether REAL trading is fully armed but do not submit any order.")
    subparsers.add_parser("reset-simulate", help="Reset the Futu paper-trading account to initial state (zero positions, full starting cash). Only valid in SIMULATE mode.")
    subparsers.add_parser("cancel-orders", help="Cancel all open (pending) orders on the account via Futu API.")
    flatten = subparsers.add_parser("flatten-all", help="Cancel all open orders then sell all positions to cash. Dry-run by default; add --submit to place real orders.")
    flatten.add_argument("--submit", action="store_true", help="Actually submit the SELL orders (default: dry-run only).")

    dashboard = subparsers.add_parser("dashboard", help="Launch the local monitoring and historical simulation dashboard.")
    dashboard.add_argument("--port", type=int, default=8501)

    return parser


def _history_provider(source: str, settings) -> HistoricalDataProvider:
    if source == "futu":
        return FutuQuoteDataProvider(host=settings.futu_host, port=settings.futu_port)
    return YFinanceDataProvider()


def _reference_date(settings) -> datetime.date:
    return datetime.now(ZoneInfo(settings.signal_timezone)).date()


def _print_table(rows: list[list[object]], headers: list[str]) -> None:
    print(tabulate(rows, headers=headers, tablefmt="github", floatfmt=".4f"), flush=True)


def _trade_destination_label(settings) -> str:
    return "REAL" if settings.futu_trd_env == "REAL" else "SIMULATE"


def _progress(message: str) -> None:
    print(message, flush=True)


def _parse_iso_date(raw: str | None, *, fallback: date | None = None) -> date | None:
    if not raw:
        return fallback
    return date.fromisoformat(raw)


def _logged_replay_days(start: str | None, end: str | None) -> list[Path]:
    if not start or not end:
        return []
    try:
        return _iter_logged_replay_days(start, end)
    except Exception:
        return []


def cmd_real_check(_args: argparse.Namespace) -> None:
    settings = load_settings()
    print(f"Trade env: {settings.futu_trd_env}")
    print(f"Market: {settings.futu_trd_market}")
    print(f"Configured FUTU_ACC_ID: {settings.futu_acc_id if settings.futu_acc_id is not None else 'auto'}")
    print(f"FUTU_ENABLE_REAL_TRADING: {settings.futu_enable_real_trading}")
    print(f"FUTU_ALLOW_AUTO_REAL: {settings.futu_allow_auto_real}")
    print(f"FUTU_UNLOCK_TRADE_PASSWORD_MD5 configured: {bool(settings.futu_unlock_trade_password_md5)}")

    with FutuPaperTrader(settings) as trader:
        accounts = trader.list_accounts()
        cols = [c for c in ["acc_id", "trd_env", "acc_type", "sim_acc_type", "trdmarket_auth", "acc_status", "acc_role"] if c in accounts.columns]
        if cols:
            _print_table(accounts[cols].fillna("").values.tolist(), cols)

        acc_id = trader.resolve_trade_account()
        print(f"Resolved trade account: {acc_id}")
        if settings.futu_trd_env == "REAL":
            if not settings.futu_enable_real_trading:
                print("REAL manual submit: BLOCKED by FUTU_ENABLE_REAL_TRADING=false")
                print("REAL auto run: BLOCKED by FUTU_ALLOW_AUTO_REAL=false")
                return
            if not settings.futu_unlock_trade_password_md5:
                print("REAL manual submit: BLOCKED by missing FUTU_UNLOCK_TRADE_PASSWORD_MD5")
                print("REAL auto run: BLOCKED by missing FUTU_UNLOCK_TRADE_PASSWORD_MD5")
                return
            account = trader.get_account_info(acc_id)
            print(f"REAL account reachable. total_assets={float(account['total_assets']):.2f}")
            print("REAL manual submit: READY")
            if settings.futu_allow_auto_real:
                print("REAL auto run: READY")
            else:
                print("REAL auto run: BLOCKED by FUTU_ALLOW_AUTO_REAL=false")
            return

        account = trader.get_account_info(acc_id)
        print(f"SIMULATE account reachable. total_assets={float(account['total_assets']):.2f}")


def cmd_reset_simulate(_args: argparse.Namespace) -> None:
    """The Futu OpenAPI v10 SDK does not expose a programmatic reset endpoint.
    Print instructions for resetting the simulated account manually in the Futu/Moomoo app.
    """
    print("=" * 60)
    print("模拟账户重置 / Simulate Account Reset")
    print("=" * 60)
    print()
    print("富途 OpenAPI v10 Python SDK 不支持通过 API 重置模拟账户。")
    print("请在富途牛牛 / Moomoo 客户端中手动操作：")
    print()
    print("  1. 打开富途牛牛 / Moomoo App")
    print("  2. 进入 「交易」→「模拟交易」")
    print("  3. 右上角「设置」→「重置账户」")
    print("  4. 确认重置（账户将恢复初始资金，持仓清零）")
    print("  5. 重置完成后重启自动交易程序")
    print()
    print("The Futu OpenAPI v10 Python SDK does not support resetting the")
    print("simulated account via API. Please reset manually in the Futu/Moomoo app:")
    print("  Trade → Simulate Trading → Settings (top-right) → Reset Account")
    print()


def cmd_cancel_orders(_args: argparse.Namespace) -> None:
    """Cancel every open (pending) order on the account via the Futu cancel_all_order API."""
    settings = load_settings()
    with FutuPaperTrader(settings) as trader:
        acc_id = trader.resolve_trade_account()
        n = trader.cancel_all_open_orders(acc_id)
        if n == 0:
            print("No open orders to cancel.")
        else:
            print(f"Cancelled {n} open order(s) successfully.")


def cmd_flatten_all(args: argparse.Namespace) -> None:
    """Cancel all open orders, then sell every current position to move the account to 100% cash.

    Step 1: cancel_all_open_orders (unblocks the auto-trader)
    Step 2: plan_rebalance({}) → SELL orders for every held symbol
    Dry-run by default; use --submit to actually place the orders.
    """
    settings = load_settings()
    with FutuPaperTrader(settings) as trader:
        acc_id = trader.resolve_trade_account()

        # Step 1: cancel any pending orders so plan_rebalance gets fresh state
        open_orders = trader.get_open_orders(acc_id)
        if not open_orders.empty:
            print(f"Cancelling {len(open_orders)} open order(s) first...")
            trader.cancel_all_open_orders(acc_id)
            print("Open orders cancelled.")
        else:
            print("No open orders to cancel.")

        positions = trader.get_positions(acc_id)
        account = trader.get_account_info(acc_id)

        if positions.empty:
            print("No open positions — account is already in cash.")
            return

        print(f"\n{_trade_destination_label(settings)} account  total_assets={float(account['total_assets']):.2f}")
        print(f"Open positions: {len(positions)}")
        position_rows = [
            [row["code"], int(float(row["qty"])), int(float(row.get("can_sell_qty", row["qty"]))),
             float(row.get("cost_price", 0)), float(row.get("market_val", 0))]
            for _, row in positions.iterrows()
        ]
        _print_table(position_rows, ["symbol", "qty", "can_sell_qty", "cost_price", "market_val"])
        print()

        # Step 2: sell everything
        _account, orders = trader.plan_rebalance({})

        if not orders:
            print("plan_rebalance returned no orders (positions may be unsellable right now).")
            return

        order_rows = [
            [o.code, o.side, o.quantity, o.limit_price, o.reference_price, o.current_qty, o.target_qty]
            for o in orders
        ]
        _print_table(order_rows, ["symbol", "side", "qty", "limit_price", "last_price", "current_qty", "target_qty"])

        if not args.submit:
            print()
            print(f"Dry run — {len(orders)} SELL order(s) planned but NOT submitted.")
            print(f"Re-run with --submit to place {_trade_destination_label(settings)} orders.")
            return

        print()
        print(f"Submitting {len(orders)} SELL order(s)...")
        results = trader.submit_orders(orders)
        _print_table(results.values.tolist(), list(results.columns))
        print("Done. All positions submitted for liquidation.")


def cmd_backtest(args: argparse.Namespace) -> None:
    settings = load_settings()
    start = args.start or settings.start_date
    end = args.end
    initial_capital = float(args.initial_capital)

    if args.strategy == "baseline":
        provider = YFinanceDataProvider()
        prices = provider.fetch_daily_closes(
            settings.symbols,
            start=start,
            end=end,
        )
        result = run_backtest(
            prices,
            settings.lookback_months,
            settings.benchmark,
            initial_capital,
            trade_cost_model=build_trade_cost_model(settings),
            slippage_bps=settings.futu_price_buffer_bps,
        )
        summary_rows = [
            ["strategy", "baseline"],
            ["final_portfolio_value", result.summary["final_portfolio_value"]],
            ["estimated_costs", result.summary.get("total_fees", 0.0)],
            ["strategy_total_return", result.summary["total_return"]],
            ["strategy_cagr", result.summary["cagr"]],
            ["strategy_volatility", result.summary["volatility"]],
            ["strategy_sharpe", result.summary["sharpe"]],
            ["strategy_max_drawdown", result.summary["max_drawdown"]],
            ["benchmark_total_return", float(result.benchmark_curve.iloc[-1] - 1)],
            ["benchmark_cagr", result.benchmark_curve.iloc[-1] ** (12 / len(result.benchmark_returns)) - 1],
        ]
        _print_table(summary_rows, ["metric", "value"])
        return

    with FutuPaperTrader(settings) as trader:
        if args.strategy == "fusion":
            lob_days = _logged_replay_days(start, end)
            if lob_days:
                _progress(
                    f"[fusion replay] using stored 40-level LOB data for {len(lob_days)} trading day(s): "
                    f"{lob_days[0].name} .. {lob_days[-1].name}"
                )
                _progress("[fusion replay] running exact intraday replay from runtime/market_data ...")
                replay = run_fusion_lob_replay(
                    start,
                    end,
                    settings,
                    initial_capital=initial_capital,
                    cost_model=build_trade_cost_model(settings),
                )
                summary_rows = [
                    ["strategy", "fusion_lob"],
                    *[[key, value] for key, value in replay.summary.items()],
                ]
                _print_table(summary_rows, ["metric", "value"])
                return
            symbols = [settings.fusion_benchmark, *settings.fusion_universe]
            unique_symbols = list(dict.fromkeys(symbols))
            start_date = _parse_iso_date(start)
            end_date = _parse_iso_date(end, fallback=datetime.now(ZoneInfo(settings.auto_trader_market_timezone)).date())
            if start_date and end_date:
                span_days = (end_date - start_date).days + 1
                _progress(
                    f"[fusion replay] loading minute bars for {len(unique_symbols)} symbols "
                    f"from {start_date.isoformat()} to {end_date.isoformat()} ({span_days} days)."
                )
                if span_days > 90:
                    _progress("[fusion replay] long minute-range replay can be slow. Progress will be printed symbol by symbol.")
            price_frames = {}
            for index, code in enumerate(unique_symbols, start=1):
                _progress(f"[fusion replay] {index}/{len(unique_symbols)} fetching {code} K_1M ...")
                frame = trader.request_history_klines(
                    code,
                    start=start,
                    end=end,
                    ktype="K_1M",
                    session="RTH",
                )
                price_frames[code] = frame
                _progress(f"[fusion replay] {index}/{len(unique_symbols)} fetched {code}: {len(frame)} rows")
            _progress("[fusion replay] running replay ...")
            replay = run_fusion_intraday_replay(price_frames, settings, initial_capital=initial_capital)
        elif args.strategy == "ofim":
            lob_days = _logged_replay_days(start, end)
            if lob_days:
                _progress(
                    f"[ofim replay] using stored 40-level LOB data for {len(lob_days)} trading day(s): "
                    f"{lob_days[0].name} .. {lob_days[-1].name}"
                )
                _progress("[ofim replay] running exact intraday replay from runtime/market_data ...")
                replay = run_ofim_lob_replay(
                    start,
                    end,
                    settings,
                    initial_capital=initial_capital,
                    cost_model=build_trade_cost_model(settings),
                )
                summary_rows = [
                    ["strategy", "ofim_lob"],
                    *[[key, value] for key, value in replay.summary.items()],
                ]
                _print_table(summary_rows, ["metric", "value"])
                return
            symbols = list(dict.fromkeys([settings.ofim_benchmark, *settings.ofim_universe]))
            _progress(f"[ofim replay] loading minute bars for {len(symbols)} symbols ...")
            price_frames = {}
            for index, code in enumerate(symbols, start=1):
                _progress(f"[ofim replay] {index}/{len(symbols)} fetching {code} K_1M ...")
                frame = trader.request_history_klines(
                    code,
                    start=start,
                    end=end,
                    ktype="K_1M",
                    session="RTH",
                )
                price_frames[code] = frame
                _progress(f"[ofim replay] {index}/{len(symbols)} fetched {code}: {len(frame)} rows")
            _progress("[ofim replay] running replay ...")
            replay = run_ofim_intraday_replay(price_frames, settings, initial_capital=initial_capital)
        elif args.strategy == "cascade":
            symbols = list(dict.fromkeys(cascade_trade_symbols(settings)))
            _progress(f"[cascade replay] loading daily bars for {len(symbols)} symbols ...")
            def _cascade_progress(stage: str, index: int, total: int, code: str, detail: str) -> None:
                if stage == "fetch_start":
                    _progress(f"[cascade replay] {index}/{total} fetching {code} K_DAY ...")
                elif stage == "fetch_ok":
                    source, rows = detail.split(":", 1)
                    _progress(f"[cascade replay] {index}/{total} fetched {code}: {rows} rows ({source})")
                elif stage == "fetch_warn":
                    _progress(f"[cascade replay] {index}/{total} futu fallback for {code}: {detail}")
                elif stage == "fetch_skip":
                    _progress(f"[cascade replay] {index}/{total} skipped {code}: {detail}")

            price_frames = fetch_cascade_daily_frames(
                trader,
                settings,
                start=start,
                end=end,
                progress=_cascade_progress,
            )
            _progress("[cascade replay] running replay ...")
            replay = run_cascade_replay(price_frames, settings, initial_capital=initial_capital)
        elif args.strategy == "stack":
            baseline_weight, fusion_weight, ofim_weight, cascade_weight, reserve_weight = stack_allocations(settings)
            fusion_settings = effective_fusion_settings(settings)
            _progress(f"[stack replay] current stack: {stack_label(settings)}")

            baseline_prices = pd.DataFrame()
            if settings.stack_baseline_enabled and baseline_weight > 0:
                _progress("[stack replay] fetching baseline daily bars ...")
                daily_series: dict[str, pd.Series] = {}
                for index, code in enumerate(settings.symbols, start=1):
                    _progress(f"[stack replay] baseline {index}/{len(settings.symbols)} fetching {code} K_DAY ...")
                    frame = trader.request_history_klines(
                        code,
                        start=start,
                        end=end,
                        ktype="K_DAY",
                        session="RTH",
                    )
                    if not frame.empty:
                        normalized = frame[["time_key", "close"]].copy()
                        normalized["date"] = pd.to_datetime(normalized["time_key"]).dt.normalize()
                        normalized["close"] = pd.to_numeric(normalized["close"], errors="coerce")
                        daily_series[code] = normalized.dropna(subset=["date", "close"]).drop_duplicates(subset=["date"], keep="last").set_index("date")["close"].sort_index()
                    _progress(f"[stack replay] baseline {index}/{len(settings.symbols)} fetched {code}: {len(frame)} rows")
                baseline_prices = pd.DataFrame(daily_series).sort_index().dropna(how="all") if daily_series else pd.DataFrame()

            fusion_symbols = [fusion_settings.fusion_benchmark, *fusion_settings.fusion_universe]
            ofim_symbols = [settings.ofim_benchmark, *settings.ofim_universe] if ofim_weight > 0 else []
            unique_symbols = list(dict.fromkeys([*fusion_symbols, *ofim_symbols]))
            start_date = _parse_iso_date(start)
            end_date = _parse_iso_date(end, fallback=datetime.now(ZoneInfo(settings.auto_trader_market_timezone)).date())
            if start_date and end_date:
                span_days = (end_date - start_date).days + 1
                _progress(
                    f"[stack replay] loading fusion/ofim minute bars for {len(unique_symbols)} symbols "
                    f"from {start_date.isoformat()} to {end_date.isoformat()} ({span_days} days)."
                )
            fusion_frames = {}
            for index, code in enumerate(unique_symbols, start=1):
                _progress(f"[stack replay] fusion/ofim {index}/{len(unique_symbols)} fetching {code} K_1M ...")
                frame = trader.request_history_klines(
                    code,
                    start=start,
                    end=end,
                    ktype="K_1M",
                    session="RTH",
                )
                fusion_frames[code] = frame
                _progress(f"[stack replay] fusion/ofim {index}/{len(unique_symbols)} fetched {code}: {len(frame)} rows")

            cascade_frames = {}
            if cascade_weight > 0:
                cascade_symbols = list(dict.fromkeys(cascade_trade_symbols(settings)))
                _progress(f"[stack replay] loading Claude/Cascade daily bars for {len(cascade_symbols)} symbols ...")
                def _stack_cascade_progress(stage: str, index: int, total: int, code: str, detail: str) -> None:
                    if stage == "fetch_start":
                        _progress(f"[stack replay] cascade {index}/{total} fetching {code} K_DAY ...")
                    elif stage == "fetch_ok":
                        source, rows = detail.split(":", 1)
                        _progress(f"[stack replay] cascade {index}/{total} fetched {code}: {rows} rows ({source})")
                    elif stage == "fetch_warn":
                        _progress(f"[stack replay] cascade {index}/{total} futu fallback for {code}: {detail}")
                    elif stage == "fetch_skip":
                        _progress(f"[stack replay] cascade {index}/{total} skipped {code}: {detail}")

                cascade_frames = fetch_cascade_daily_frames(
                    trader,
                    settings,
                    start=start,
                    end=end,
                    progress=_stack_cascade_progress,
                )

            _progress("[stack replay] running combined replay ...")
            replay = run_strategy_stack_replay(
                baseline_prices,
                fusion_frames,
                settings,
                initial_capital=initial_capital,
                cascade_price_frames=cascade_frames,
            )
        elif args.strategy == "account":
            _progress("[account replay] loading filled orders from Futu ...")
            acc_id = trader.resolve_trade_account()
            order_history = trader.get_order_history(acc_id, start, end or start)
            filled_order_history = (
                order_history[pd.to_numeric(order_history.get("dealt_qty"), errors="coerce").fillna(0) > 0].copy()
                if not order_history.empty
                else order_history
            )
            symbols = sorted(set(filled_order_history["code"].tolist())) if not filled_order_history.empty else []
            _progress(f"[account replay] found {len(filled_order_history)} filled orders across {len(symbols)} symbols.")
            price_frames = {}
            for index, code in enumerate(symbols, start=1):
                _progress(f"[account replay] {index}/{len(symbols)} fetching {code} bars ...")
                frame = trader.request_history_klines(
                    code,
                    start=start,
                    end=end or start,
                    ktype="K_1M",
                    session="RTH",
                )
                price_frames[code] = frame
                _progress(f"[account replay] {index}/{len(symbols)} fetched {code}: {len(frame)} rows")
            _progress("[account replay] running replay ...")
            replay = run_account_replay(filled_order_history, price_frames, settings)
        else:
            _progress("[exact replay] loading logged submitted orders from runtime/market_data ...")
            logged_orders = market_logger.load_order_records(start, end or start)
            submitted = logged_orders[
                (logged_orders.get("action", pd.Series(dtype=str)).astype(str) == "submitted")
                & (logged_orders.get("submit_status", pd.Series(dtype=str)).astype(str).str.lower() == "submitted")
            ].copy() if not logged_orders.empty else logged_orders
            _progress(f"[exact replay] found {len(submitted)} logged submitted orders.")
            acc_id = trader.resolve_trade_account()
            order_history = trader.get_order_history(acc_id, start, end or start)
            matched_ids = set(submitted.get("submit_detail", pd.Series(dtype=str)).astype(str).tolist()) if not submitted.empty else set()
            exact_history = (
                order_history[order_history["order_id"].astype(str).isin(matched_ids)].copy()
                if matched_ids and not order_history.empty and "order_id" in order_history.columns
                else order_history.iloc[0:0].copy()
            )
            symbols = sorted(set(exact_history["code"].tolist())) if not exact_history.empty else sorted(set(submitted.get("code", pd.Series(dtype=str)).tolist()))
            _progress(f"[exact replay] found {len(exact_history)} filled orders matched by order_id across {len(symbols)} symbols.")
            price_frames = {}
            for index, code in enumerate(symbols, start=1):
                _progress(f"[exact replay] {index}/{len(symbols)} fetching {code} bars ...")
                frame = trader.request_history_klines(
                    code,
                    start=start,
                    end=end or start,
                    ktype="K_1M",
                    session="RTH",
                )
                price_frames[code] = frame
                _progress(f"[exact replay] {index}/{len(symbols)} fetched {code}: {len(frame)} rows")
            _progress("[exact replay] running exact execution replay ...")
            replay = run_exact_execution_replay(start, end or start, order_history, price_frames, settings)

    summary_rows = [["strategy", args.strategy], *[[key, value] for key, value in replay.summary.items()]]
    _print_table(summary_rows, ["metric", "value"])
    if replay.trade_log.empty:
        print("No trades in selected range.")
    else:
        _print_table(replay.trade_log.fillna("").values.tolist(), list(replay.trade_log.columns))


def cmd_signals(args: argparse.Namespace) -> None:
    settings = load_settings()
    provider = _history_provider(args.history_source, settings)
    prices = provider.fetch_daily_closes(
        settings.symbols,
        start=args.start or settings.start_date,
        end=args.end,
    )
    snapshot = latest_completed_signal(
        prices,
        lookback_months=settings.lookback_months,
        reference_date=_reference_date(settings),
    )
    if not snapshot.weights:
        print(f"Latest completed month: {snapshot.signal_month.date()} | target = 100% cash")
        return

    rows = [[code, weight] for code, weight in snapshot.weights.items()]
    print(f"Latest completed month: {snapshot.signal_month.date()}")
    _print_table(rows, ["symbol", "target_weight"])


def cmd_paper_trade(args: argparse.Namespace) -> None:
    settings = load_settings()
    provider = _history_provider(args.history_source, settings)
    prices = provider.fetch_daily_closes(
        settings.symbols,
        start=args.start or settings.start_date,
        end=args.end,
    )
    snapshot = latest_completed_signal(
        prices,
        lookback_months=settings.lookback_months,
        reference_date=_reference_date(settings),
    )

    with FutuPaperTrader(settings) as trader:
        account, orders = trader.plan_rebalance(snapshot.weights)

        print(f"Signal month: {snapshot.signal_month.date()}")
        print(f"{_trade_destination_label(settings)} account total assets: {float(account['total_assets']):.2f}")
        print(f"Target cash weight: {1 - sum(snapshot.weights.values()):.4f}")

        if snapshot.weights:
            _print_table([[code, weight] for code, weight in snapshot.weights.items()], ["symbol", "target_weight"])
        else:
            print("Target positions: no active assets, move to cash.")

        if not orders:
            print("No orders required. Current holdings already match target weights.")
            return

        order_rows = [
            [
                order.code,
                order.side,
                order.quantity,
                order.limit_price,
                order.reference_price,
                order.current_qty,
                order.target_qty,
                order.target_weight,
            ]
            for order in orders
        ]
        _print_table(
            order_rows,
            ["symbol", "side", "qty", "limit_price", "last_price", "current_qty", "target_qty", "target_weight"],
        )

        if not args.submit:
            print(f"Dry run only. Re-run with --submit to place Futu {_trade_destination_label(settings)} orders.")
            return

        results = trader.submit_orders(orders)
        _print_table(results.values.tolist(), list(results.columns))


def cmd_fusion_intraday(args: argparse.Namespace) -> None:
    settings = load_settings()
    strategy = FusionIntradayStrategy(settings)

    with FutuPaperTrader(settings) as trader:
        acc_id = trader.resolve_trade_account()
        positions = trader.get_positions(acc_id)
        held_symbols = set(positions["code"].tolist()) if not positions.empty else set()

        plan = strategy.generate_plan(trader, held_symbols)
        print(f"Benchmark: {plan.benchmark}")
        print(f"Benchmark regime score: {plan.benchmark_score:.4f}")
        print(f"Target gross exposure: {plan.exposure:.4f}")

        feature_rows = [
            [
                feature.code,
                feature.score,
                feature.gap_pct,
                feature.momentum_5m,
                feature.vwap_distance,
                feature.rel_volume,
                feature.orderbook_imbalance,
                feature.tick_imbalance,
                feature.spread_bps,
                feature.reason,
            ]
            for feature in plan.features
        ]
        _print_table(
            feature_rows,
            ["symbol", "score", "gap_pct", "mom_5m", "vwap_dist", "rel_vol", "obi", "tick_imb", "spread_bps", "status"],
        )

        if plan.target_weights:
            print("Target weights:")
            _print_table([[code, weight] for code, weight in plan.target_weights.items()], ["symbol", "target_weight"])
        else:
            print("Target weights: no new exposure, stay in cash or flatten.")
        acc_id = trader.resolve_trade_account()
        account = trader.get_account_info(acc_id)
        print(f"{_trade_destination_label(settings)} account total assets: {float(account['total_assets']):.2f}")

        if not plan.target_weights and not held_symbols:
            print("No orders required. Fusion currently has no entry signal and there are no existing positions.")
            return

        account, orders = trader.plan_rebalance(plan.target_weights)
        if not orders:
            print("No orders required. Current holdings already match the Fusion target.")
            return

        order_rows = [
            [
                order.code,
                order.side,
                order.quantity,
                order.limit_price,
                order.reference_price,
                order.current_qty,
                order.target_qty,
                order.target_weight,
            ]
            for order in orders
        ]
        _print_table(
            order_rows,
            ["symbol", "side", "qty", "limit_price", "last_price", "current_qty", "target_qty", "target_weight"],
        )

        if not args.submit:
            print(f"Dry run only. Re-run with --submit to place Futu {_trade_destination_label(settings)} orders.")
            return

        results = trader.submit_orders(orders)
        _print_table(results.values.tolist(), list(results.columns))


def cmd_ofim_intraday(args: argparse.Namespace) -> None:
    settings = load_settings()
    strategy = OfimIntradayStrategy(settings)

    with FutuPaperTrader(settings) as trader:
        acc_id = trader.resolve_trade_account()
        positions = trader.get_positions(acc_id)
        held_symbols = set(positions["code"].tolist()) if not positions.empty else set()

        plan = strategy.generate_plan(trader, held_symbols)
        print(f"Strategy: {plan.strategy}")
        print(f"Target gross exposure: {plan.exposure:.4f}")

        feature_rows = [
            [
                feature.code,
                feature.score,
                feature.ofi_tier_1,
                feature.ofi_tier_2,
                feature.ofi_tier_3,
                feature.mom_3m,
                feature.mom_10m,
                feature.vol_accel,
                feature.tick_agg,
                feature.spread_bps,
                feature.reason,
            ]
            for feature in plan.features
        ]
        _print_table(
            feature_rows,
            [
                "symbol",
                "score",
                "ofi_1",
                "ofi_2",
                "ofi_3",
                "mom_3m",
                "mom_10m",
                "vol_accel",
                "tick_agg",
                "spread_bps",
                "status",
            ],
        )

        if plan.target_weights:
            print("Target weights:")
            _print_table([[code, weight] for code, weight in plan.target_weights.items()], ["symbol", "target_weight"])
        else:
            print("Target weights: no new exposure, stay in cash or flatten.")
        acc_id = trader.resolve_trade_account()
        account = trader.get_account_info(acc_id)
        print(f"{_trade_destination_label(settings)} account total assets: {float(account['total_assets']):.2f}")

        if not plan.target_weights and not held_symbols:
            print("No orders required. OFIM currently has no entry signal and there are no existing positions.")
            return

        _account, orders = trader.plan_rebalance(plan.target_weights)
        if not orders:
            print("No orders required. Current holdings already match the OFIM target.")
            return

        order_rows = [
            [
                order.code,
                order.side,
                order.quantity,
                order.limit_price,
                order.reference_price,
                order.current_qty,
                order.target_qty,
                order.target_weight,
            ]
            for order in orders
        ]
        _print_table(
            order_rows,
            ["symbol", "side", "qty", "limit_price", "last_price", "current_qty", "target_qty", "target_weight"],
        )

        if not args.submit:
            print(f"Dry run only. Re-run with --submit to place Futu {_trade_destination_label(settings)} orders.")
            return

        results = trader.submit_orders(orders)
        _print_table(results.values.tolist(), list(results.columns))


def cmd_cascade_strategy(args: argparse.Namespace) -> None:
    settings = load_settings()

    with FutuPaperTrader(settings) as trader:
        plan = generate_live_cascade_plan(settings, trader)
        print(f"Regime: {plan.regime_label}")
        print(f"Regime score: {plan.regime_score:.4f}")
        print(f"Target gross exposure: {plan.total_exposure:.4f}")
        if plan.note:
            print(plan.note)
        if plan.target_weights:
            _print_table([[code, weight] for code, weight in plan.target_weights.items()], ["symbol", "target_weight"])
        else:
            print("Target weights: no active Cascade exposure, keep cash.")

        acc_id = trader.resolve_trade_account()
        positions = trader.get_positions(acc_id)
        account = trader.get_account_info(acc_id)
        print(f"{_trade_destination_label(settings)} account total assets: {float(account['total_assets']):.2f}")

        held_symbols = set(positions["code"].tolist()) if not positions.empty else set()
        if not plan.target_weights and not held_symbols:
            print("No orders required. Cascade currently has no tradable targets and there are no existing positions.")
            return

        _account, orders = trader.plan_rebalance(plan.target_weights)
        if not orders:
            print("No orders required. Current holdings already match the Cascade target.")
            return

        order_rows = [
            [
                order.code,
                order.side,
                order.quantity,
                order.limit_price,
                order.reference_price,
                order.current_qty,
                order.target_qty,
                order.target_weight,
            ]
            for order in orders
        ]
        _print_table(
            order_rows,
            ["symbol", "side", "qty", "limit_price", "last_price", "current_qty", "target_qty", "target_weight"],
        )

        if not args.submit:
            print(f"Dry run only. Re-run with --submit to place Futu {_trade_destination_label(settings)} orders.")
            return

        results = trader.submit_orders(orders)
        _print_table(results.values.tolist(), list(results.columns))


def cmd_dashboard(args: argparse.Namespace) -> None:
    dashboard_path = Path(__file__).with_name("dashboard_app.py")
    env = os.environ.copy()
    pythonpath_parts = [str(Path(__file__).resolve().parents[1])]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(dashboard_path),
                "--server.port",
                str(args.port),
                "--browser.gatherUsageStats=false",
            ],
            check=True,
            env=env,
        )
    except KeyboardInterrupt:
        return


def cmd_auto_fusion(args: argparse.Namespace) -> None:
    settings = load_settings()
    run_auto_trader(settings, submit=not args.dry_run)


def cmd_watchdog(_args: argparse.Namespace) -> None:
    settings = load_settings()
    run_watchdog(settings)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    command_map = {
        "backtest": cmd_backtest,
        "signals": cmd_signals,
        "paper-trade": cmd_paper_trade,
        "fusion-intraday": cmd_fusion_intraday,
        "ofim-intraday": cmd_ofim_intraday,
        "cascade-strategy": cmd_cascade_strategy,
        "auto-fusion": cmd_auto_fusion,
        "watchdog": cmd_watchdog,
        "real-check": cmd_real_check,
        "reset-simulate": cmd_reset_simulate,
        "cancel-orders": cmd_cancel_orders,
        "flatten-all": cmd_flatten_all,
        "dashboard": cmd_dashboard,
    }
    try:
        command_map[args.command](args)
    except (FutuTradeError, MarketDataError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
