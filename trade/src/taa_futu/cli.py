from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import math
from pathlib import Path
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd
from tabulate import tabulate

from .backtest import run_backtest
from .auto_trader import run_auto_trader
from . import market_logger
from .cascade_sleeve import cascade_trade_symbols, fetch_cascade_daily_frames, generate_live_cascade_plan
from .config import load_settings
from .crypto_ofim import (
    CryptoOfimError,
    CryptoOfimEngine,
    crypto_ofim_auto_instance,
    crypto_ofim_guarded_idle_poll_seconds,
    ensure_crypto_ofim_auto_submit_allowed,
    load_crypto_ofim_settings,
    read_crypto_ofim_status,
    reset_crypto_ofim_paper,
    reset_crypto_ofim_testnet_ledger_epoch,
)
from .crypto_perp import (
    CryptoPerpEngine,
    CryptoPerpError,
    crypto_perp_auto_instance,
    crypto_perp_guarded_idle_poll_seconds,
    explain_crypto_perp_status,
    load_crypto_perp_settings,
    read_crypto_perp_status,
    reset_crypto_perp_paper,
)
from .crypto_ofim_watchdog import (
    read_crypto_ofim_app_status,
    read_crypto_ofim_watchdog_status,
    run_crypto_ofim_watchdog,
    start_crypto_ofim_app_service,
    stop_crypto_ofim_app_service,
)
from .crypto_ofim_stream import read_crypto_ofim_stream_status, run_crypto_ofim_stream
from .crypto_learning import (
    CRYPTO_ATTRIBUTION_FILE,
    CRYPTO_LEARNING_REVIEW_PACKET_FILE,
    CRYPTO_LEARNING_REVIEW_PACKET_JSON_FILE,
    CRYPTO_ORDER_MEMORY_FILE,
    CRYPTO_PROMOTION_REPORT_FILE,
    CRYPTO_TRADE_OUTCOMES_FILE,
    CRYPTO_UPGRADE_CANDIDATES_FILE,
    load_learning_report as load_crypto_learning_report,
    load_learning_review_packet as load_crypto_learning_review_packet,
    load_promotion_report as load_crypto_promotion_report,
    load_upgrade_candidates,
    run_learning_pipeline as run_crypto_learning_pipeline,
)
from .crypto_backtest import (
    DATA_FILE as CRYPTO_REPLAY_DATA_FILE,
    MANIFEST_FILE as CRYPTO_REPLAY_MANIFEST_FILE,
    build_crypto_backtest_dataset,
    result_to_dict as crypto_backtest_result_to_dict,
    run_crypto_backtest,
)
from .crypto_research_loop import (
    BEST_CANDIDATE_FILE as CRYPTO_RESEARCH_BEST_CANDIDATE_FILE,
    LOCKED_TEST_REPORT_FILE as CRYPTO_RESEARCH_LOCKED_TEST_REPORT_FILE,
    RESEARCH_PATCH_REPORT_FILE as CRYPTO_RESEARCH_PATCH_REPORT_FILE,
    TRIALS_FILE as CRYPTO_RESEARCH_TRIALS_FILE,
    read_crypto_research_status,
    run_crypto_research_loop,
)
from .costs import build_stock_fills_ledger, build_trade_cost_model
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
from .strategy_stack import baseline_sleeve_enabled, effective_fusion_settings, stack_allocations, stack_label
from .strategy import latest_completed_signal
from .strategy_experiment import write_strategy_split_state
from .stock_runtime import (
    STOCK_FILLS_FILE,
    STOCK_JOURNAL_FILE,
    STOCK_LEDGER_EPOCH_FILE,
    load_stock_ledger_epoch,
    write_stock_ledger_epoch,
)
from .cli_hint import venv_command
from .stock_doctor import run_stock_system_doctor
from .stock_reconciliation_log import (
    RECON_LOG_FILE,
    append_snapshot,
    build_snapshot,
    load_snapshots,
    with_daily_delta,
)
from .stock_ledger import build_stock_double_entry_ledger, reconcile_stock_ledger, write_stock_journal
from .stock_learning import (
    STOCK_ATTRIBUTION_FILE,
    STOCK_LEARNING_REVIEW_PACKET_FILE,
    STOCK_LEARNING_REVIEW_PACKET_JSON_FILE,
    STOCK_ORDER_MEMORY_FILE,
    STOCK_PROMOTION_REPORT_FILE,
    STOCK_STRATEGY_CANDIDATES_FILE,
    STOCK_TRADE_OUTCOMES_FILE,
    load_learning_report,
    load_learning_review_packet,
    load_promotion_report,
    load_strategy_candidates,
    run_learning_pipeline,
)
from .watchdog import run_watchdog


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime"


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

    live_signal = subparsers.add_parser(
        "live-signal",
        help="Read-only: ask the current stack what every sleeve thinks about each symbol right now.",
    )
    live_signal.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Symbol code (e.g. US.NVDA). Repeatable. Falls back to FUSION_UNIVERSE.",
    )
    live_signal.add_argument(
        "symbols_positional",
        nargs="*",
        help="Positional symbols (e.g. US.NVDA US.TSLA). Equivalent to --symbol, supports legacy callers.",
    )
    live_signal.add_argument(
        "--no-universe",
        action="store_true",
        help="Skip the universe view (fusion features, ofim top, cascade targets) to shrink the response.",
    )
    live_signal.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON document to stdout instead of a human-readable table.",
    )

    subparsers.add_parser("crypto-ofim-check", help="Check independent Binance crypto OFIM connectivity.")
    subparsers.add_parser("crypto-ofim-status", help="Show independent crypto OFIM paper/testnet status.")
    subparsers.add_parser("crypto-ofim-reset", help="Reset the independent crypto OFIM local paper ledger.")
    crypto_ofim_ledger_reset = subparsers.add_parser(
        "crypto-ofim-ledger-reset",
        help="Reset Binance Spot Testnet accounting epoch without submitting orders.",
    )
    crypto_ofim_ledger_reset.add_argument("--reason", default="manual_testnet_ledger_reset", help="Audit reason recorded in the new ledger epoch.")
    crypto_ofim_ledger_reset.add_argument("--no-backup", action="store_true", help="Skip backing up overwritten ledger/state/status files.")
    crypto_ofim_liquidate = subparsers.add_parser("crypto-ofim-liquidate", help="Plan or submit selling all Binance Testnet non-quote assets to USDT.")
    crypto_ofim_liquidate.add_argument("--submit", action="store_true", help="Actually submit liquidation orders to Binance Spot Testnet.")
    crypto_ofim_liquidate.add_argument("--reset-epoch", action="store_true", help="After submitted liquidation, set a fresh ledger start point.")
    crypto_ofim_once = subparsers.add_parser("crypto-ofim-once", help="Run independent crypto OFIM once.")
    crypto_ofim_once.add_argument(
        "--submit",
        action="store_true",
        help="Submit to the configured crypto mode. paper=local ledger only; testnet=Binance Spot Testnet.",
    )
    crypto_ofim_auto = subparsers.add_parser("crypto-ofim-auto", help="Run independent crypto OFIM continuously.")
    crypto_ofim_auto.add_argument("--submit", action="store_true", help="Submit every cycle to paper/testnet mode.")
    crypto_ofim_auto.add_argument("--poll-seconds", type=int, default=60, help="Seconds between crypto OFIM cycles.")
    crypto_ofim_watchdog = subparsers.add_parser("crypto-ofim-watchdog", help="Monitor and restart independent crypto OFIM auto trading.")
    crypto_ofim_watchdog.add_argument("--poll-seconds", type=int, default=60, help="Auto trading poll interval to use after restart.")
    crypto_ofim_watchdog.add_argument("--check-seconds", type=int, default=30, help="Seconds between watchdog health checks.")
    crypto_ofim_watchdog.add_argument("--stale-seconds", type=int, default=180, help="Restart auto trading if status is older than this.")
    crypto_ofim_watchdog.add_argument("--restart-cooldown-seconds", type=int, default=120, help="Minimum seconds between watchdog restarts.")
    subparsers.add_parser("crypto-ofim-watchdog-status", help="Show independent crypto OFIM watchdog status.")
    crypto_ofim_stream = subparsers.add_parser("crypto-ofim-stream", help="Run independent crypto OFIM WebSocket market-data stream.")
    crypto_ofim_stream.add_argument("--depth-limit", type=int, default=None, help="Depth levels to keep in the local cache.")
    subparsers.add_parser("crypto-ofim-stream-status", help="Show independent crypto OFIM market stream status.")
    crypto_ofim_app = subparsers.add_parser("crypto-ofim-app", help="Launch the one-page Crypto OFIM Binance app.")
    crypto_ofim_app.add_argument("--port", type=int, default=8503)
    crypto_ofim_app_start = subparsers.add_parser("crypto-ofim-app-start", help="Start the Crypto OFIM app-only LaunchAgent.")
    crypto_ofim_app_start.add_argument("--port", type=int, default=8503)
    subparsers.add_parser("crypto-ofim-app-stop", help="Stop the Crypto OFIM app-only LaunchAgent.")
    crypto_ofim_app_status = subparsers.add_parser("crypto-ofim-app-status", help="Show Crypto OFIM app-only status.")
    crypto_ofim_app_status.add_argument("--port", type=int, default=8503)
    subparsers.add_parser("crypto-perp-check", help="Check independent Binance USD-M Futures long/short sleeve connectivity.")
    subparsers.add_parser("crypto-perp-status", help="Show independent crypto USD-M Futures long/short sleeve status.")
    subparsers.add_parser("crypto-perp-explain", help="Explain the latest crypto USD-M Futures long/short decision in plain language.")
    subparsers.add_parser("crypto-perp-reset", help="Reset the independent crypto USD-M Futures local paper ledger.")
    crypto_perp_once = subparsers.add_parser("crypto-perp-once", help="Run independent crypto USD-M Futures long/short sleeve once.")
    crypto_perp_once.add_argument(
        "--submit",
        action="store_true",
        help="Submit to the configured perp mode. paper=local signed ledger; testnet=Binance USD-M Futures Testnet.",
    )
    crypto_perp_auto = subparsers.add_parser("crypto-perp-auto", help="Run independent crypto USD-M Futures long/short sleeve continuously.")
    crypto_perp_auto.add_argument("--submit", action="store_true", help="Submit every cycle to paper/testnet mode.")
    crypto_perp_auto.add_argument("--poll-seconds", type=int, default=60, help="Seconds between crypto perp cycles.")
    subparsers.add_parser("crypto-learning-build", help="Build crypto Evidence-to-Review outcomes, attribution, candidates and promotion report.")
    subparsers.add_parser("crypto-learning-export", help="Build a human/Codex crypto learning review packet.")
    subparsers.add_parser("crypto-learning-status", help="Show the latest crypto Evidence-to-Review report.")
    crypto_backtest_build_data = subparsers.add_parser("crypto-backtest-build-data", help="Build crypto research replay data from local logs and Binance public history.")
    crypto_backtest_build_data.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT", help="Comma-separated symbols to include.")
    crypto_backtest_build_data.add_argument("--days", type=int, default=14, help="Public Binance 1m history window to fetch.")
    crypto_backtest_build_data.add_argument("--local-only", action="store_true", help="Only use local runtime logs; do not fetch public data.")
    crypto_backtest_build_data.add_argument("--public-only", action="store_true", help="Only fetch public Binance data; do not include local runtime logs.")
    crypto_backtest_run = subparsers.add_parser("crypto-backtest-run", help="Run crypto Spot/Perp research replay backtest.")
    crypto_backtest_run.add_argument("--sleeve", choices=("spot", "perp", "both"), default="both")
    crypto_backtest_run.add_argument("--profile", default="default")
    crypto_backtest_run.add_argument("--split", choices=("train", "validation", "locked_test", "all"), default="all")
    crypto_research_loop = subparsers.add_parser("crypto-research-loop", help="Run crypto research-only parameter search and locked-test report.")
    crypto_research_loop.add_argument("--max-trials", type=int, default=100)
    crypto_research_loop.add_argument("--target", default="out_of_sample_net_profit")
    crypto_research_loop.add_argument("--no-build-data", action="store_true", help="Do not build replay data if missing.")
    subparsers.add_parser("crypto-research-status", help="Show latest crypto research loop status.")

    cascade = subparsers.add_parser("cascade-strategy", help="Run the Claude/Cascade sleeve through the shared Futu routing.")
    cascade.add_argument("--submit", action="store_true", help="Actually submit Futu orders.")

    auto_fusion = subparsers.add_parser("auto-fusion", help="Run the Fusion intraday strategy continuously during market hours.")
    auto_fusion.add_argument("--dry-run", action="store_true", help="Monitor continuously but do not submit orders.")

    subparsers.add_parser("watchdog", help="Run the stability watchdog for the trading engine.")
    subparsers.add_parser("real-check", help="Check whether REAL trading is fully armed but do not submit any order.")
    reset_simulate = subparsers.add_parser("reset-simulate", help="Show Futu paper account reset steps; optionally mark a fresh stock ledger epoch.")
    reset_simulate.add_argument("--mark-epoch", action="store_true", help="After manual reset, record a fresh stock ledger accounting epoch now.")
    subparsers.add_parser("stock-status", help="Show stock auto trader, watchdog and ledger status.")
    subparsers.add_parser("stock-system-reset", help="Record one coherent stock system epoch for ledger and four-strategy split accounting.")
    stock_doctor = subparsers.add_parser("stock-system-doctor", help="Check stock runtime glue: epochs, split ledger, learning packet, auto trader and watchdog.")
    stock_doctor.add_argument("--json", action="store_true", help="Print the doctor report as JSON.")
    subparsers.add_parser("stock-ledger-reset", help="Record a fresh stock ledger accounting epoch from the current Futu account snapshot.")
    subparsers.add_parser("stock-ledger-status", help="Show stock fill-ledger projection since the current epoch.")
    subparsers.add_parser("stock-ledger-audit", help="Build the double-entry stock journal and write runtime/stock_journal.jsonl.")
    subparsers.add_parser("stock-recon-snapshot", help="Record one daily broker-vs-ledger reconciliation row (read-only).")
    recon_history = subparsers.add_parser("stock-recon-history", help="Show the daily reconciliation gap and its day-over-day change.")
    recon_history.add_argument("--days", type=int, default=30, help="How many recent rows to show.")
    subparsers.add_parser("stock-learning-build", help="Build stock order outcomes, attribution, strategy candidates and promotion report.")
    subparsers.add_parser("stock-learning-export", help="Build a human/Codex review packet from the latest stock learning evidence.")
    subparsers.add_parser("stock-learning-status", help="Show the latest stock strategy learning report.")
    stock_log_status = subparsers.add_parser("stock-market-log-status", help="Show bulky stock market-data JSONL log usage. Read-only; never deletes replay data.")
    stock_log_status.add_argument("--keep-days", type=int, default=10, help="Show how much data is older than this many calendar days.")
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


def _current_account_snapshot(settings) -> dict[str, object]:
    with FutuPaperTrader(settings) as trader:
        acc_id = trader.resolve_trade_account()
        account = trader.get_account_info(acc_id)
        positions = trader.get_positions(acc_id)
    position_rows: list[dict[str, object]] = []
    if not positions.empty:
        keep_columns = [
            "code",
            "qty",
            "can_sell_qty",
            "market_val",
            "cost_price",
            "average_cost",
            "nominal_price",
        ]
        for row in positions.to_dict("records"):
            cleaned: dict[str, object] = {}
            for column in keep_columns:
                if column in row:
                    value = row.get(column)
                    if isinstance(value, (int, float)):
                        cleaned[column] = float(value)
                    elif value is not None:
                        cleaned[column] = value
            if cleaned.get("code"):
                position_rows.append(cleaned)
    return {
        "acc_id": acc_id,
        "total_assets": float(account.get("total_assets", 0.0) or 0.0),
        "cash": float(account.get("cash", account.get("cash_balance", 0.0)) or 0.0),
        "market_val": float(account.get("market_val", 0.0) or 0.0),
        "position_count": 0 if positions.empty else int(len(positions)),
        "positions": position_rows,
    }


def _read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def cmd_reset_simulate(args: argparse.Namespace) -> None:
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
    print("After the manual reset, run:")
    print(f"  {venv_command('reset-simulate --mark-epoch')}")
    print("or:")
    print(f"  {venv_command('stock-ledger-reset')}")
    print()

    if not args.mark_epoch:
        return
    settings = load_settings()
    snapshot = _current_account_snapshot(settings)
    epoch_path = write_stock_ledger_epoch(reason="manual_reset", account_snapshot=snapshot)
    print(f"Stock ledger epoch recorded: {epoch_path}")
    print(f"Epoch account snapshot: total_assets={snapshot.get('total_assets', 0.0):.2f}")


def cmd_stock_ledger_reset(_args: argparse.Namespace) -> None:
    settings = load_settings()
    snapshot = _current_account_snapshot(settings)
    epoch_path = write_stock_ledger_epoch(reason="manual_epoch_reset", account_snapshot=snapshot)
    print(f"Stock ledger epoch recorded: {epoch_path}")
    _print_table([[key, value] for key, value in snapshot.items()], ["snapshot_metric", "value"])


def cmd_stock_system_reset(_args: argparse.Namespace) -> None:
    settings = load_settings()
    snapshot = _current_account_snapshot(settings)
    ledger_path = write_stock_ledger_epoch(reason="manual_stock_system_epoch", account_snapshot=snapshot)
    split_path = write_strategy_split_state(
        settings=settings,
        total_assets=float(snapshot.get("total_assets", 0.0) or 0.0),
        reason="manual_stock_system_epoch",
    )
    rows = [
        ["stock_ledger_epoch", ledger_path],
        ["strategy_split_state", split_path],
        ["total_assets", round(float(snapshot.get("total_assets", 0.0) or 0.0), 6)],
        ["cash", round(float(snapshot.get("cash", 0.0) or 0.0), 6)],
        ["market_val", round(float(snapshot.get("market_val", 0.0) or 0.0), 6)],
        ["position_count", snapshot.get("position_count", 0)],
    ]
    _print_table(rows, ["stock_system_epoch", "value"])


def cmd_stock_system_doctor(args: argparse.Namespace) -> None:
    settings = load_settings()
    report = run_stock_system_doctor(settings)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), flush=True)
        return
    rows = [
        [item.status, item.area, item.summary, item.fix_command]
        for item in report.findings
    ]
    _print_table(rows, ["status", "area", "summary", "fix"])
    details = [[item.area, item.detail] for item in report.findings if item.detail]
    if details:
        print()
        _print_table(details, ["area", "detail"])
    print(f"\nstock_system_doctor_status={report.status}", flush=True)


def cmd_stock_ledger_status(_args: argparse.Namespace) -> None:
    projection = build_stock_fills_ledger(STOCK_FILLS_FILE, epoch_path=STOCK_LEDGER_EPOCH_FILE)
    journal = build_stock_double_entry_ledger(STOCK_FILLS_FILE, epoch_path=STOCK_LEDGER_EPOCH_FILE)
    epoch = load_stock_ledger_epoch()
    rows = [
        ["epoch_ts", epoch.get("ts", "none")],
        ["fills_count_at_reset", epoch.get("fills_count_at_reset", 0)],
        ["trade_count", projection.trade_count],
        ["realized_pnl", round(projection.realized_pnl, 6)],
        ["fees_paid", round(projection.fees_paid, 6)],
        ["cash_delta", round(projection.cash_delta, 6)],
        ["open_positions", len(projection.positions)],
        ["audit_hash", projection.audit_hash],
        ["journal_hash", journal.journal_hash],
        ["double_entry_chain_valid", journal.chain_valid],
        ["double_entry_net_realized", round(journal.net_realized_pnl, 6)],
        ["double_entry_gross_realized", round(journal.realized_gross_pnl, 6)],
        ["double_entry_imbalanced", len(journal.imbalanced_entries)],
    ]
    _print_table(rows, ["ledger_metric", "value"])
    if projection.positions:
        _print_table(
            [[symbol, qty, projection.avg_cost.get(symbol, 0.0)] for symbol, qty in projection.positions.items()],
            ["symbol", "quantity", "avg_cost"],
        )
    if projection.warnings:
        print("Warnings:")
        for warning in projection.warnings:
            print(f"- {warning}")
    if journal.warnings:
        print("Double-entry warnings:")
        for warning in journal.warnings:
            print(f"- {warning}")


def cmd_stock_ledger_audit(_args: argparse.Namespace) -> None:
    settings = load_settings()
    epoch = load_stock_ledger_epoch()
    journal = build_stock_double_entry_ledger(STOCK_FILLS_FILE, epoch_path=STOCK_LEDGER_EPOCH_FILE)
    journal_path = write_stock_journal(journal, journal_path=STOCK_JOURNAL_FILE)
    rows = [
        ["journal_path", journal_path],
        ["entries", len(journal.entries)],
        ["trade_count", journal.trade_count],
        ["journal_hash", journal.journal_hash],
        ["chain_valid", journal.chain_valid],
        ["imbalanced_entries", len(journal.imbalanced_entries)],
        ["net_realized_pnl", round(journal.net_realized_pnl, 6)],
        ["fees_paid", round(journal.fees_paid, 6)],
    ]
    try:
        with FutuPaperTrader(settings) as trader:
            acc_id = trader.resolve_trade_account()
            positions = trader.get_positions(acc_id)
            account = trader.get_account_info(acc_id)
        reconciliation = reconcile_stock_ledger(journal, positions=positions, account=account, epoch=epoch)
        rows.append(["reconciliation_ok", reconciliation.ok])
        rows.append(["reconciliation_breaks", len(reconciliation.breaks)])
    except Exception as exc:
        rows.append(["reconciliation_ok", f"not_checked: {type(exc).__name__}: {exc}"])
    _print_table(rows, ["audit_metric", "value"])


def cmd_stock_recon_snapshot(_args: argparse.Namespace) -> None:
    """记一行当天的券商 vs 账本对账快照。只读券商，只往日志追一行。

    单独做成一条命令而不是塞进 doctor：doctor 是「现在有没有毛病」，这个是
    「差额是从哪天开始长出来的」。后者要的是一条不间断的逐日序列，必须每天
    定时跑一次，跟人什么时候想起来体检没关系。
    """
    settings = load_settings()
    epoch = load_stock_ledger_epoch()
    projection = build_stock_fills_ledger(STOCK_FILLS_FILE, epoch_path=STOCK_LEDGER_EPOCH_FILE)
    with FutuPaperTrader(settings) as trader:
        acc_id = trader.resolve_trade_account()
        positions = trader.get_positions(acc_id)
        account = trader.get_account_info(acc_id)
    reconciliation = reconcile_stock_ledger(projection, positions=positions, account=account, epoch=epoch)
    account_dict = account.to_dict() if hasattr(account, "to_dict") else dict(account)
    snapshot = build_snapshot(
        account=account_dict,
        projection=projection,
        epoch=epoch,
        reconciliation=reconciliation,
    )
    path = append_snapshot(snapshot)
    rows = [[key, value] for key, value in snapshot.to_dict().items()]
    _print_table(rows, ["recon_field", "value"])
    print(f"\nwritten: {path}", flush=True)


def cmd_stock_recon_history(args: argparse.Namespace) -> None:
    rows = with_daily_delta(load_snapshots())
    if not rows:
        print(f"还没有对账快照。先跑 stock-recon-snapshot。日志位置：{RECON_LOG_FILE}", flush=True)
        return
    tail = rows[-max(1, int(getattr(args, "days", 30))):]
    table = [
        [
            r["date"],
            f"{_finite(r.get('broker_total_assets')):,.2f}",
            f"{_finite(r.get('cash_gap')):,.2f}",
            "-" if r.get("cash_gap_delta") is None else f"{_finite(r.get('cash_gap_delta')):+,.2f}",
            "-" if r.get("trades_today") is None else str(r.get("trades_today")),
            "跳变" if r.get("jump") else ("持仓不符" if int(r.get("position_break_count") or 0) else ""),
        ]
        for r in tail
    ]
    _print_table(table, ["date", "broker_assets", "cash_gap", "gap_delta", "trades", "flag"])
    first, last = rows[0], rows[-1]
    print(
        f"\n{first['date']} → {last['date']}：现金差 {_finite(first.get('cash_gap')):,.2f}"
        f" → {_finite(last.get('cash_gap')):,.2f}",
        flush=True,
    )


def _finite(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def cmd_stock_status(_args: argparse.Namespace) -> None:
    auto_status = _read_json_file(RUNTIME_DIR / "auto_trader_status.json")
    watchdog_status = _read_json_file(RUNTIME_DIR / "watchdog_status.json")
    epoch = load_stock_ledger_epoch()
    projection = build_stock_fills_ledger(STOCK_FILLS_FILE, epoch_path=STOCK_LEDGER_EPOCH_FILE)
    journal = build_stock_double_entry_ledger(STOCK_FILLS_FILE, epoch_path=STOCK_LEDGER_EPOCH_FILE)
    rows = [
        ["auto_trader.running", auto_status.get("running", "-")],
        ["auto_trader.action", auto_status.get("action", "-")],
        ["auto_trader.detail", str(auto_status.get("detail", "-"))[:100]],
        ["auto_trader.updated_at", auto_status.get("updated_at", "-")],
        ["auto_trader.cycle_id", auto_status.get("last_cycle_id", "-")],
        ["watchdog.running", watchdog_status.get("running", "-")],
        ["watchdog.action", watchdog_status.get("action", "-")],
        ["watchdog.opend_connected", watchdog_status.get("opend_connected", "-")],
        ["watchdog.restart_count", watchdog_status.get("restart_count", "-")],
        ["ledger.epoch_ts", epoch.get("ts", "none")],
        ["ledger.trade_count", projection.trade_count],
        ["ledger.realized_pnl", round(projection.realized_pnl, 6)],
        ["ledger.fees_paid", round(projection.fees_paid, 6)],
        ["ledger.audit_hash", projection.audit_hash],
        ["ledger_v2.journal_hash", journal.journal_hash],
        ["ledger_v2.chain_valid", journal.chain_valid],
        ["ledger_v2.net_realized_pnl", round(journal.net_realized_pnl, 6)],
    ]
    _print_table(rows, ["key", "value"])


def cmd_stock_learning_build(_args: argparse.Namespace) -> None:
    settings = load_settings()
    result = run_learning_pipeline(settings=settings)
    rows = [
        ["order_memory", STOCK_ORDER_MEMORY_FILE],
        ["outcomes", result.outcomes_path],
        ["attribution", result.attribution_path],
        ["candidates", result.candidates_path],
        ["promotion", result.promotion_path],
        ["review_packet", result.review_packet_path],
        ["review_packet_json", result.review_packet_json_path],
        ["outcome_count", result.outcome_count],
        ["candidate_count", result.candidate_count],
    ]
    _print_table(rows, ["learning_artifact", "value"])


def cmd_stock_learning_export(_args: argparse.Namespace) -> None:
    settings = load_settings()
    result = run_learning_pipeline(settings=settings)
    rows = [
        ["review_packet", result.review_packet_path],
        ["review_packet_json", result.review_packet_json_path],
        ["source_outcomes", result.outcomes_path],
        ["source_attribution", result.attribution_path],
        ["source_candidates", result.candidates_path],
        ["source_promotion", result.promotion_path],
    ]
    _print_table(rows, ["export_artifact", "value"])
    print("\n把 review_packet 这份 Markdown 发给 Codex 评估；JSON 留作可校验证据包。", flush=True)


def cmd_stock_learning_status(_args: argparse.Namespace) -> None:
    report = load_learning_report(STOCK_ATTRIBUTION_FILE)
    packet = load_learning_review_packet(STOCK_LEARNING_REVIEW_PACKET_JSON_FILE)
    candidates = load_strategy_candidates(STOCK_STRATEGY_CANDIDATES_FILE)
    promotion = load_promotion_report(STOCK_PROMOTION_REPORT_FILE)
    total = dict(report.get("total") or {})
    rows = [
        ["generated_at", report.get("generated_at", "none")],
        ["review_packet_id", packet.get("packet_id", "none")],
        ["review_packet", STOCK_LEARNING_REVIEW_PACKET_FILE if STOCK_LEARNING_REVIEW_PACKET_FILE.exists() else "missing"],
        ["trades", total.get("trades", 0)],
        ["win_rate", round(float(total.get("win_rate", 0.0) or 0.0), 4)],
        ["net_pnl", round(float(total.get("net_pnl", 0.0) or 0.0), 6)],
        ["fees_paid", round(float(total.get("fees_paid", 0.0) or 0.0), 6)],
        ["candidates", len(candidates)],
        ["promotion_decisions", len(promotion.get("decisions", []) if isinstance(promotion, dict) else [])],
        ["live_auto_promotion", False],
    ]
    _print_table(rows, ["learning_metric", "value"])
    if candidates:
        _print_table(
            [
                [
                    item.get("candidate_id", ""),
                    item.get("action_type", ""),
                    item.get("param", ""),
                    item.get("proposed_value", ""),
                    str(item.get("rationale", ""))[:90],
                ]
                for item in candidates[:10]
            ],
            ["candidate_id", "action", "param", "proposed", "rationale"],
        )


def _format_bytes(num_bytes: int | float) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            return f"{value:,.1f} {unit}"
        value /= 1024.0
    return f"{value:,.1f} TB"


def cmd_stock_market_log_status(args: argparse.Namespace) -> None:
    usage = market_logger.market_data_disk_usage()
    plan = market_logger.market_data_retention_plan(keep_days=args.keep_days)
    print(f"Market data root: {usage['root']}")
    print(f"Current size: {_format_bytes(usage['total_bytes'])}")
    print(f"Read-only view. No files are removed.")
    print(f"Older-than window: keep-days={plan['keep_days']} (cutoff {plan['cutoff']})")
    print(f"Older data: {_format_bytes(plan['older_bytes'])} across {len(plan['older_days'])} day folder(s)")
    rows = [
        [item["date"], _format_bytes(item["bytes"]), item["path"]]
        for item in sorted(plan["older_days"], key=lambda row: row["date"])[:20]
    ]
    if rows:
        print("\nOldest folders")
        _print_table(rows, ["date", "size", "path"])


def cmd_crypto_learning_build(_args: argparse.Namespace) -> None:
    settings = load_crypto_ofim_settings()
    result = run_crypto_learning_pipeline(
        mode=settings.mode,
        quote_asset=settings.quote_asset,
        settings=settings,
    )
    rows = [
        ["order_memory", CRYPTO_ORDER_MEMORY_FILE],
        ["outcomes", result.outcomes_path],
        ["attribution", result.attribution_path],
        ["candidates", result.candidates_path],
        ["promotion", result.promotion_path],
        ["review_packet", result.review_packet_path],
        ["review_packet_json", result.review_packet_json_path],
        ["outcome_count", result.outcome_count],
        ["candidate_count", result.candidate_count],
        ["live_auto_promotion", False],
    ]
    _print_table(rows, ["crypto_learning_artifact", "value"])


def cmd_crypto_learning_export(_args: argparse.Namespace) -> None:
    settings = load_crypto_ofim_settings()
    result = run_crypto_learning_pipeline(
        mode=settings.mode,
        quote_asset=settings.quote_asset,
        settings=settings,
    )
    rows = [
        ["review_packet", result.review_packet_path],
        ["review_packet_json", result.review_packet_json_path],
        ["source_outcomes", result.outcomes_path],
        ["source_attribution", result.attribution_path],
        ["source_candidates", result.candidates_path],
        ["source_promotion", result.promotion_path],
    ]
    _print_table(rows, ["crypto_export_artifact", "value"])
    print("\n把 crypto_learning_review_packet.md 发给 Codex 评估；JSON 留作 sha256 可校验证据包。", flush=True)


def cmd_crypto_learning_status(_args: argparse.Namespace) -> None:
    report = load_crypto_learning_report(CRYPTO_ATTRIBUTION_FILE)
    packet = load_crypto_learning_review_packet(CRYPTO_LEARNING_REVIEW_PACKET_JSON_FILE)
    candidates = load_upgrade_candidates(CRYPTO_UPGRADE_CANDIDATES_FILE)
    promotion = load_crypto_promotion_report(CRYPTO_PROMOTION_REPORT_FILE)
    total = dict(report.get("total") or {})
    rows = [
        ["generated_at", report.get("generated_at", "none")],
        ["review_packet_id", packet.get("packet_id", "none")],
        ["review_packet", CRYPTO_LEARNING_REVIEW_PACKET_FILE if CRYPTO_LEARNING_REVIEW_PACKET_FILE.exists() else "missing"],
        ["order_memory", CRYPTO_ORDER_MEMORY_FILE if CRYPTO_ORDER_MEMORY_FILE.exists() else "missing"],
        ["trades", total.get("trades", 0)],
        ["win_rate", round(float(total.get("win_rate", 0.0) or 0.0), 4)],
        ["net_pnl", round(float(total.get("net_pnl", 0.0) or 0.0), 6)],
        ["fees", round(float(total.get("fees", 0.0) or 0.0), 6)],
        ["avg_slippage_bps", round(float(total.get("avg_slippage_bps", 0.0) or 0.0), 6)],
        ["candidates", len(candidates)],
        ["promotion_decisions", len(promotion.get("decisions", []) if isinstance(promotion, dict) else [])],
        ["live_auto_promotion", False],
    ]
    _print_table(rows, ["crypto_learning_metric", "value"])
    if candidates:
        _print_table(
            [
                [
                    item.get("candidate_id", ""),
                    item.get("action_type", ""),
                    item.get("param", ""),
                    item.get("proposed_value", ""),
                    str(item.get("rationale", ""))[:90],
                ]
                for item in candidates[:10]
            ],
            ["candidate_id", "action", "param", "proposed", "rationale"],
        )


def cmd_crypto_backtest_build_data(args: argparse.Namespace) -> None:
    symbols = [part.strip().upper() for part in str(args.symbols or "").split(",") if part.strip()]
    include_public = not bool(args.local_only)
    include_local = not bool(args.public_only)
    manifest = build_crypto_backtest_dataset(
        symbols=symbols,
        days=args.days,
        include_public=include_public,
        include_local=include_local,
    )
    rows = [
        ["data_file", CRYPTO_REPLAY_DATA_FILE],
        ["manifest", CRYPTO_REPLAY_MANIFEST_FILE],
        ["sha256", manifest.get("sha256", "")],
        ["row_count", manifest.get("row_count", 0)],
        ["start_ts", manifest.get("start_ts", "")],
        ["end_ts", manifest.get("end_ts", "")],
        ["symbols", ",".join(manifest.get("symbols", []))],
        ["errors", len(manifest.get("errors", []))],
    ]
    _print_table(rows, ["crypto_backtest_data", "value"])


def cmd_crypto_backtest_run(args: argparse.Namespace) -> None:
    result = crypto_backtest_result_to_dict(
        run_crypto_backtest(
            sleeve=args.sleeve,
            profile=args.profile,
            split=args.split,
        )
    )
    if "combined" in result:
        rows = [
            [
                sleeve,
                payload.get("split", args.split),
                payload.get("net_pnl", 0),
                payload.get("max_drawdown", 0),
                payload.get("trade_count", 0),
                payload.get("fees_paid", 0),
                payload.get("funding_paid", 0),
                payload.get("passed_gate", False),
                ",".join(payload.get("failure_reasons", [])),
            ]
            for sleeve, payload in result.items()
        ]
    else:
        rows = [
            [
                result.get("sleeve", args.sleeve),
                result.get("split", args.split),
                result.get("net_pnl", 0),
                result.get("max_drawdown", 0),
                result.get("trade_count", 0),
                result.get("fees_paid", 0),
                result.get("funding_paid", 0),
                result.get("passed_gate", False),
                ",".join(result.get("failure_reasons", [])),
            ]
        ]
    _print_table(rows, ["sleeve", "split", "net_pnl", "max_drawdown", "trades", "fees", "funding", "passed", "failures"])


def cmd_crypto_research_loop(args: argparse.Namespace) -> None:
    payload = run_crypto_research_loop(
        max_trials=args.max_trials,
        target=args.target,
        build_data_if_missing=not args.no_build_data,
    )
    best = payload.get("best_candidate", {})
    locked = payload.get("locked_test", {})
    rows = [
        ["trial_count", payload.get("trial_count", 0)],
        ["best_profile", best.get("profile_name", "none")],
        ["passed_validation", best.get("passed_validation", False)],
        ["passed_locked_test", locked.get("passed_locked_test", False)],
        ["trials", CRYPTO_RESEARCH_TRIALS_FILE],
        ["best_candidate", CRYPTO_RESEARCH_BEST_CANDIDATE_FILE],
        ["locked_test_report", CRYPTO_RESEARCH_LOCKED_TEST_REPORT_FILE],
        ["research_patch_report", CRYPTO_RESEARCH_PATCH_REPORT_FILE],
        ["live_auto_promotion", False],
    ]
    _print_table(rows, ["crypto_research_metric", "value"])


def cmd_crypto_research_status(_args: argparse.Namespace) -> None:
    status = read_crypto_research_status()
    validation = status.get("best_validation") or {}
    locked = status.get("locked_test") or {}
    manifest = status.get("data_manifest") or {}
    rows = [
        ["trial_count", status.get("trial_count", 0)],
        ["best_profile", status.get("best_profile", "none")],
        ["validation_net_pnl", validation.get("net_pnl", 0)],
        ["validation_max_drawdown", validation.get("max_drawdown", 0)],
        ["validation_trades", validation.get("trade_count", 0)],
        ["passed_validation", status.get("passed_validation", False)],
        ["locked_net_pnl", locked.get("net_pnl", 0)],
        ["locked_max_drawdown", locked.get("max_drawdown", 0)],
        ["locked_trades", locked.get("trade_count", 0)],
        ["passed_locked_test", status.get("passed_locked_test", False)],
        ["data_rows", manifest.get("row_count", 0)],
        ["research_patch_report", CRYPTO_RESEARCH_PATCH_REPORT_FILE],
        ["live_auto_promotion", False],
    ]
    _print_table(rows, ["crypto_research_metric", "value"])


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
            if baseline_sleeve_enabled(settings) and baseline_weight > 0:
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


def cmd_live_signal(args: argparse.Namespace) -> None:
    """Read-only stack judgement for external callers (incl. Futu watcher skill).

    Never submits orders, never writes ledger / events / state. Reuses the
    same sleeve plan generators that ``auto_trader`` runs, but stops at the
    weight-merge step.
    """
    from .live_signal import compute_live_signal

    settings = load_settings()
    symbols = list(dict.fromkeys(list(args.symbols_positional or []) + list(args.symbol or [])))
    report = compute_live_signal(
        symbols=symbols,
        settings=settings,
        include_universe=not args.no_universe,
    )

    if args.json:
        print(report.to_json(indent=2))
        return

    print(f"# Live signal @ {report.generated_at}")
    print(f"# Stack: {report.stack_label}")
    if report.errors:
        print(f"# Errors (degraded): {'; '.join(report.errors)}")
    print()
    rows = []
    for sym in report.queried_symbols:
        payload = report.by_symbol.get(sym, {})
        rows.append([
            sym,
            f"{payload.get('stack_target_weight', 0.0):.4f}",
            payload.get("recommendation", "?"),
            "held" if payload.get("held") else "",
            ", ".join(payload.get("evidence") or []),
        ])
    _print_table(rows, ["symbol", "stack_weight", "recommendation", "held", "evidence"])

    # Also print symbols added by sleeves but not queried
    extra = [s for s in report.by_symbol.keys() if s not in report.queried_symbols]
    if extra:
        print()
        print(f"# Additional symbols the sleeves want ({len(extra)}):")
        extra_rows = []
        for sym in extra:
            payload = report.by_symbol[sym]
            extra_rows.append([
                sym,
                f"{payload.get('stack_target_weight', 0.0):.4f}",
                payload.get("recommendation", "?"),
                "held" if payload.get("held") else "",
                ", ".join(payload.get("evidence") or []),
            ])
        _print_table(extra_rows, ["symbol", "stack_weight", "recommendation", "held", "evidence"])


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


def _print_crypto_ofim_payload(payload: dict) -> None:
    print(f"Mode: {payload.get('mode', 'unknown')}")
    print(f"Status: {payload.get('status', 'unknown')}")
    benchmark_trend = payload.get("benchmark_trend") if isinstance(payload.get("benchmark_trend"), dict) else {}
    plan_reason = payload.get("plan_reason") or benchmark_trend.get("reason")
    if plan_reason:
        print(f"Plan reason: {plan_reason}")
    if payload.get("submit_label"):
        print(f"Submit target: {payload['submit_label']}")
    if payload.get("market_data_label") or payload.get("market_data"):
        print(f"Market data: {payload.get('market_data_label') or payload.get('market_data')}")
    if payload.get("execution_base_url"):
        print(f"Execution API: {payload['execution_base_url']}")
    if payload.get("market_data_base_url"):
        print(f"Market data API: {payload['market_data_base_url']}")
    account = payload.get("account") or {}
    if account:
        rows = [
            ["starting_equity", account.get("starting_equity", 0.0)],
            ["cash", account.get("cash", 0.0)],
            ["market_value", account.get("market_value", 0.0)],
            ["equity", account.get("primary_equity", account.get("equity", 0.0))],
            ["net_pnl", account.get("primary_net_pnl", account.get("net_pnl", 0.0))],
            ["net_return_pct", account.get("primary_net_return_pct", account.get("net_return_pct", 0.0))],
            ["pnl_source", account.get("primary_pnl_source", "")],
            ["active_capital", account.get("active_capital", 0.0)],
            ["strategy_available_cash", account.get("strategy_available_cash", 0.0)],
            ["realized_pnl", account.get("realized_pnl_after_estimated_fees", account.get("realized_pnl", 0.0))],
            ["unrealized_pnl", account.get("unrealized_pnl", 0.0)],
            ["official_fee_estimate", account.get("estimated_fees_paid", account.get("fees_paid", 0.0))],
            ["estimated_fee_source", account.get("estimated_fee_source", "")],
        ]
        _print_table(rows, ["account_metric", "value"])
        positions = account.get("positions") or {}
        if positions:
            _print_table([[symbol, qty] for symbol, qty in positions.items()], ["symbol", "quantity"])
    if payload.get("target_weights"):
        _print_table(
            [[symbol, weight] for symbol, weight in payload["target_weights"].items()],
            ["target_symbol", "target_weight"],
        )
    features = payload.get("features") or []
    if features:
        feature_rows = [
            [
                item.get("symbol"),
                item.get("score"),
                item.get("ofi_tier_1"),
                item.get("ofi_tier_2"),
                item.get("ofi_tier_3"),
                item.get("vol_accel"),
                item.get("tick_agg"),
                item.get("spread_bps"),
                item.get("reason"),
            ]
            for item in features[:10]
        ]
        _print_table(
            feature_rows,
            ["symbol", "score", "ofi_1", "ofi_2", "ofi_3", "vol_accel", "tick_agg", "spread_bps", "status"],
        )
    orders = payload.get("submitted_orders") or payload.get("planned_orders") or []
    if orders:
        order_rows = [
            [
                item.get("symbol"),
                item.get("side"),
                item.get("quantity"),
                item.get("price"),
                item.get("notional"),
                item.get("fee"),
                item.get("status"),
            ]
            for item in orders
        ]
        _print_table(order_rows, ["symbol", "side", "qty", "price", "notional", "fee", "status"])


def _print_crypto_perp_payload(payload: dict) -> None:
    print(f"Mode: {payload.get('mode', 'unknown')}")
    print(f"Status: {payload.get('status', 'unknown')}")
    benchmark_context = payload.get("benchmark_context") if isinstance(payload.get("benchmark_context"), dict) else {}
    reason = payload.get("reason") or benchmark_context.get("reason")
    if reason:
        print(f"Reason: {reason}")
    if payload.get("submit_label"):
        print(f"Submit target: {payload['submit_label']}")
    if payload.get("market_data_label"):
        print(f"Market data: {payload['market_data_label']}")
    if payload.get("execution_base_url"):
        print(f"Execution API: {payload['execution_base_url']}")
    if payload.get("market_data_base_url"):
        print(f"Market data API: {payload['market_data_base_url']}")
    print(f"Signed account: {bool(payload.get('signed_account_enabled'))}")
    account = payload.get("account") or {}
    if account:
        rows = [
            ["equity", account.get("equity", 0.0)],
            ["wallet_balance", account.get("wallet_balance", 0.0)],
            ["cash", account.get("cash", 0.0)],
            ["net_pnl", account.get("net_pnl", 0.0)],
            ["net_return_pct", account.get("net_return_pct", 0.0)],
            ["realized_pnl", account.get("realized_pnl", 0.0)],
            ["unrealized_pnl", account.get("unrealized_pnl", 0.0)],
            ["fees_paid", account.get("fees_paid", 0.0)],
            ["funding_paid", account.get("funding_paid", 0.0)],
            ["pnl_source", account.get("pnl_source", "")],
        ]
        _print_table(rows, ["perp_account_metric", "value"])
        positions = account.get("position_details") or []
        if positions:
            _print_table(
                [
                    [
                        item.get("symbol"),
                        item.get("side"),
                        item.get("quantity"),
                        item.get("entry_price"),
                        item.get("mark_price"),
                        item.get("unrealized_pnl"),
                        item.get("liquidation_price"),
                        item.get("liquidation_distance_pct"),
                    ]
                    for item in positions
                ],
                ["symbol", "side", "qty", "entry", "mark", "unrealized", "liq_price", "liq_dist_pct"],
            )
    if payload.get("target_weights"):
        _print_table(
            [[symbol, weight, "long" if weight > 0 else "short"] for symbol, weight in payload["target_weights"].items()],
            ["target_symbol", "signed_weight", "direction"],
        )
    features = payload.get("features") or []
    if features:
        _print_table(
            [
                [
                    item.get("symbol"),
                    item.get("signal"),
                    item.get("score"),
                    item.get("conviction"),
                    item.get("ofi_tier_1"),
                    item.get("tick_agg"),
                    item.get("spread_bps"),
                    item.get("reason"),
                ]
                for item in features[:10]
            ],
            ["symbol", "signal", "score", "conviction", "ofi_1", "tick_agg", "spread_bps", "status"],
        )
    orders = payload.get("submitted_orders") or payload.get("planned_orders") or []
    if orders:
        _print_table(
            [
                [
                    item.get("symbol"),
                    item.get("side"),
                    item.get("quantity"),
                    item.get("price"),
                    item.get("notional"),
                    item.get("reduce_only"),
                    item.get("status"),
                    item.get("reason"),
                ]
                for item in orders
            ],
            ["symbol", "side", "qty", "price", "notional", "reduce_only", "status", "reason"],
        )


def cmd_crypto_ofim_check(_args: argparse.Namespace) -> None:
    settings = load_crypto_ofim_settings()
    result = CryptoOfimEngine(settings).check()
    rows = [[key, value] for key, value in result.items() if key != "symbols"]
    _print_table(rows, ["metric", "value"])
    _print_table([[symbol] for symbol in result.get("symbols", [])], ["symbol"])


def cmd_crypto_ofim_status(_args: argparse.Namespace) -> None:
    _print_crypto_ofim_payload(read_crypto_ofim_status())


def cmd_crypto_ofim_reset(_args: argparse.Namespace) -> None:
    settings = load_crypto_ofim_settings()
    state = reset_crypto_ofim_paper(settings)
    print(f"Crypto OFIM local paper ledger reset. cash={state.cash:.2f} {settings.quote_asset}")


def cmd_crypto_ofim_ledger_reset(args: argparse.Namespace) -> None:
    result = reset_crypto_ofim_testnet_ledger_epoch(
        reason=str(args.reason or "manual_testnet_ledger_reset"),
        backup=not bool(args.no_backup),
    )
    rows = [
        ["status", result["status"]],
        ["mode", result["mode"]],
        ["orders_submitted", result["orders_submitted"]],
        ["execution_base_url", result["execution_base_url"]],
        ["market_data_base_url", result["market_data_base_url"]],
        ["epoch_ts", result["epoch"].get("ts")],
        ["epoch_id", result["epoch"].get("epoch_id")],
        ["state_cash", result["state_cash"]],
        ["backup_dir", result["backup_dir"]],
        ["positive_non_quote_balance_count", result["positive_non_quote_balance_count"]],
    ]
    _print_table(rows, ["crypto_testnet_ledger_reset", "value"])


def cmd_crypto_ofim_liquidate(args: argparse.Namespace) -> None:
    settings = load_crypto_ofim_settings()
    result = CryptoOfimEngine(settings).liquidate_testnet_to_quote(
        submit=bool(args.submit),
        reset_epoch=bool(args.reset_epoch),
    )
    print(f"Crypto OFIM liquidation status: {result['status']}")
    print(f"planned={result['planned_count']} submitted={result['submitted_count']} skipped={result['skipped_count']}")
    source_rows = result.get("submitted") or result.get("planned") or []
    rows = [
        [
            row.get("symbol"),
            row.get("side"),
            row.get("quantity"),
            row.get("price"),
            row.get("notional"),
            row.get("status", "planned"),
            row.get("reason", ""),
        ]
        for row in source_rows[:40]
    ]
    if rows:
        _print_table(rows, ["symbol", "side", "qty", "price", "notional", "status", "reason"])
        if len(source_rows) > len(rows):
            print(f"... output truncated: showing {len(rows)} of {len(source_rows)} rows.")
    if result.get("epoch"):
        print(f"New ledger epoch: {result['epoch'].get('ts')}")


def cmd_crypto_ofim_once(args: argparse.Namespace) -> None:
    settings = load_crypto_ofim_settings()
    payload = CryptoOfimEngine(settings).run_once(submit=args.submit)
    _print_crypto_ofim_payload(payload)


def cmd_crypto_ofim_auto(args: argparse.Namespace) -> None:
    try:
        with crypto_ofim_auto_instance():
            settings = load_crypto_ofim_settings()
            ensure_crypto_ofim_auto_submit_allowed(settings, submit=args.submit)
            engine = CryptoOfimEngine(settings)
            interval = max(5, int(args.poll_seconds))
            print(
                f"Crypto OFIM auto started. mode={settings.mode} submit={args.submit} interval={interval}s. "
                "Press Ctrl+C to stop.",
                flush=True,
            )
            try:
                while True:
                    started = time.time()
                    try:
                        payload = engine.run_once(submit=args.submit)
                        next_interval = crypto_ofim_guarded_idle_poll_seconds(payload, interval)
                        print(
                            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"{payload.get('status')} target={payload.get('target_weights', {})} "
                            f"orders={len(payload.get('submitted_orders') or payload.get('planned_orders') or [])} "
                            f"sleep={next_interval}s",
                            flush=True,
                        )
                    except Exception as exc:
                        next_interval = interval
                        print(
                            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"cycle_error={type(exc).__name__}: {exc}",
                            flush=True,
                        )
                    sleep_for = max(0.0, next_interval - (time.time() - started))
                    time.sleep(sleep_for)
            except KeyboardInterrupt:
                print("Crypto OFIM auto stopped.")
    except CryptoOfimError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(1) from exc


def cmd_crypto_ofim_watchdog(args: argparse.Namespace) -> None:
    run_crypto_ofim_watchdog(
        poll_seconds=args.poll_seconds,
        check_seconds=args.check_seconds,
        stale_seconds=args.stale_seconds,
        restart_cooldown_seconds=args.restart_cooldown_seconds,
    )


def cmd_crypto_ofim_watchdog_status(_args: argparse.Namespace) -> None:
    status = read_crypto_ofim_watchdog_status()
    rows = [[key, value] for key, value in status.items()]
    _print_table(rows, ["watchdog_metric", "value"])


def cmd_crypto_ofim_stream(args: argparse.Namespace) -> None:
    run_crypto_ofim_stream(depth_limit=args.depth_limit)


def cmd_crypto_ofim_stream_status(_args: argparse.Namespace) -> None:
    status = read_crypto_ofim_stream_status()
    rows = [[key, value] for key, value in status.items() if key not in {"books", "trades"}]
    _print_table(rows, ["stream_metric", "value"])


def cmd_crypto_ofim_app(args: argparse.Namespace) -> None:
    app_path = Path(__file__).with_name("crypto_ofim_app.py")
    app_pid_file = Path(__file__).resolve().parents[2] / "runtime" / "crypto_ofim_app.pid"
    env = os.environ.copy()
    pythonpath_parts = [str(Path(__file__).resolve().parents[1])]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(args.port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats=false",
    ]
    process: subprocess.Popen[str] | None = None
    try:
        app_pid_file.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(cmd, env=env)
        app_pid_file.write_text(str(process.pid), encoding="utf-8")
        process.wait()
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, cmd)
    except KeyboardInterrupt:
        return
    finally:
        try:
            if process and app_pid_file.exists() and app_pid_file.read_text(encoding="utf-8").strip() == str(process.pid):
                app_pid_file.unlink()
        except Exception:
            pass


def cmd_crypto_ofim_app_start(args: argparse.Namespace) -> None:
    ok, message = start_crypto_ofim_app_service(port=args.port)
    print(message)
    if not ok:
        raise SystemExit(1)


def cmd_crypto_ofim_app_stop(_args: argparse.Namespace) -> None:
    ok, message = stop_crypto_ofim_app_service()
    print(message)
    if not ok:
        raise SystemExit(1)


def cmd_crypto_ofim_app_status(args: argparse.Namespace) -> None:
    status = read_crypto_ofim_app_status(port=args.port)
    rows = [[key, value] for key, value in status.items()]
    _print_table(rows, ["app_metric", "value"])


def cmd_crypto_perp_check(_args: argparse.Namespace) -> None:
    settings = load_crypto_perp_settings()
    result = CryptoPerpEngine(settings).check()
    rows = [[key, value] for key, value in result.items() if key != "symbols"]
    _print_table(rows, ["metric", "value"])
    _print_table([[symbol] for symbol in result.get("symbols", [])], ["symbol"])


def cmd_crypto_perp_status(_args: argparse.Namespace) -> None:
    _print_crypto_perp_payload(read_crypto_perp_status())


def cmd_crypto_perp_explain(_args: argparse.Namespace) -> None:
    explanation = explain_crypto_perp_status(read_crypto_perp_status())
    print(f"Updated at: {explanation.get('updated_at')}")
    print("\nSummary")
    for line in explanation.get("summary") or []:
        print(f"- {line}")
    if explanation.get("signals"):
        print("\nSignals")
        _print_table(
            [
                [
                    row.get("symbol"),
                    row.get("signal"),
                    row.get("score"),
                    row.get("threshold"),
                    row.get("expected_edge_bps"),
                    row.get("required_edge_bps"),
                    row.get("cost_pass"),
                    row.get("hawkes"),
                    row.get("btc_leader"),
                    row.get("notes"),
                ]
                for row in explanation["signals"]
            ],
            ["symbol", "signal", "score", "entry", "edge_bps", "cost_bps", "cost_ok", "hawkes", "btc_leader", "notes"],
        )
    if explanation.get("orders"):
        print("\nOrders")
        _print_table(
            [
                [
                    row.get("symbol"),
                    row.get("side"),
                    row.get("status"),
                    row.get("order_type"),
                    row.get("time_in_force"),
                    row.get("reduce_only"),
                    row.get("notional"),
                    row.get("fee"),
                    row.get("plain"),
                ]
                for row in explanation["orders"]
            ],
            ["symbol", "side", "status", "type", "tif", "reduce_only", "notional", "fee", "plain"],
        )
    if explanation.get("risks"):
        print("\nPositions/Risk")
        _print_table(
            [
                [
                    row.get("symbol"),
                    row.get("side"),
                    row.get("qty"),
                    row.get("notional"),
                    row.get("unrealized_pnl"),
                    row.get("liquidation_distance_pct"),
                ]
                for row in explanation["risks"]
            ],
            ["symbol", "side", "qty", "notional", "unrealized", "liq_dist_pct"],
        )
    print("\nNext Questions")
    for line in explanation.get("next_questions") or []:
        print(f"- {line}")


def cmd_crypto_perp_reset(_args: argparse.Namespace) -> None:
    settings = load_crypto_perp_settings()
    state = reset_crypto_perp_paper(settings)
    print(f"Crypto USD-M Futures local paper ledger reset. cash={state.cash:.2f} {settings.quote_asset}")


def cmd_crypto_perp_once(args: argparse.Namespace) -> None:
    settings = load_crypto_perp_settings()
    payload = CryptoPerpEngine(settings).run_once(submit=args.submit)
    _print_crypto_perp_payload(payload)


def cmd_crypto_perp_auto(args: argparse.Namespace) -> None:
    try:
        with crypto_perp_auto_instance():
            settings = load_crypto_perp_settings()
            engine = CryptoPerpEngine(settings)
            interval = max(5, int(args.poll_seconds))
            print(
                f"Crypto USD-M Futures long/short auto started. mode={settings.mode} submit={args.submit} interval={interval}s. "
                "Press Ctrl+C to stop.",
                flush=True,
            )
            try:
                while True:
                    started = time.time()
                    next_interval = interval
                    try:
                        payload = engine.run_once(submit=args.submit)
                        next_interval = crypto_perp_guarded_idle_poll_seconds(payload, interval)
                        print(
                            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"{payload.get('status')} target={payload.get('target_weights', {})} "
                            f"orders={len(payload.get('submitted_orders') or payload.get('planned_orders') or [])} "
                            f"sleep={next_interval}s",
                            flush=True,
                        )
                    except Exception as exc:
                        print(
                            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"cycle_error={type(exc).__name__}: {exc}",
                            flush=True,
                        )
                    time.sleep(max(0.0, next_interval - (time.time() - started)))
            except KeyboardInterrupt:
                print("Crypto USD-M Futures long/short auto stopped.")
    except CryptoPerpError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(1) from exc


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
        "live-signal": cmd_live_signal,
        "paper-trade": cmd_paper_trade,
        "fusion-intraday": cmd_fusion_intraday,
        "ofim-intraday": cmd_ofim_intraday,
        "crypto-ofim-check": cmd_crypto_ofim_check,
        "crypto-ofim-status": cmd_crypto_ofim_status,
        "crypto-ofim-reset": cmd_crypto_ofim_reset,
        "crypto-ofim-ledger-reset": cmd_crypto_ofim_ledger_reset,
        "crypto-ofim-liquidate": cmd_crypto_ofim_liquidate,
        "crypto-ofim-once": cmd_crypto_ofim_once,
        "crypto-ofim-auto": cmd_crypto_ofim_auto,
        "crypto-ofim-watchdog": cmd_crypto_ofim_watchdog,
        "crypto-ofim-watchdog-status": cmd_crypto_ofim_watchdog_status,
        "crypto-ofim-stream": cmd_crypto_ofim_stream,
        "crypto-ofim-stream-status": cmd_crypto_ofim_stream_status,
        "crypto-ofim-app": cmd_crypto_ofim_app,
        "crypto-ofim-app-start": cmd_crypto_ofim_app_start,
        "crypto-ofim-app-stop": cmd_crypto_ofim_app_stop,
        "crypto-ofim-app-status": cmd_crypto_ofim_app_status,
        "crypto-perp-check": cmd_crypto_perp_check,
        "crypto-perp-status": cmd_crypto_perp_status,
        "crypto-perp-explain": cmd_crypto_perp_explain,
        "crypto-perp-reset": cmd_crypto_perp_reset,
        "crypto-perp-once": cmd_crypto_perp_once,
        "crypto-perp-auto": cmd_crypto_perp_auto,
        "crypto-learning-build": cmd_crypto_learning_build,
        "crypto-learning-export": cmd_crypto_learning_export,
        "crypto-learning-status": cmd_crypto_learning_status,
        "crypto-backtest-build-data": cmd_crypto_backtest_build_data,
        "crypto-backtest-run": cmd_crypto_backtest_run,
        "crypto-research-loop": cmd_crypto_research_loop,
        "crypto-research-status": cmd_crypto_research_status,
        "cascade-strategy": cmd_cascade_strategy,
        "auto-fusion": cmd_auto_fusion,
        "watchdog": cmd_watchdog,
        "real-check": cmd_real_check,
        "reset-simulate": cmd_reset_simulate,
        "stock-status": cmd_stock_status,
        "stock-system-reset": cmd_stock_system_reset,
        "stock-system-doctor": cmd_stock_system_doctor,
        "stock-ledger-reset": cmd_stock_ledger_reset,
        "stock-ledger-status": cmd_stock_ledger_status,
        "stock-ledger-audit": cmd_stock_ledger_audit,
        "stock-recon-snapshot": cmd_stock_recon_snapshot,
        "stock-recon-history": cmd_stock_recon_history,
        "stock-learning-build": cmd_stock_learning_build,
        "stock-learning-export": cmd_stock_learning_export,
        "stock-learning-status": cmd_stock_learning_status,
        "stock-market-log-status": cmd_stock_market_log_status,
        "cancel-orders": cmd_cancel_orders,
        "flatten-all": cmd_flatten_all,
        "dashboard": cmd_dashboard,
    }
    try:
        command_map[args.command](args)
    except (FutuTradeError, MarketDataError, CryptoOfimError, CryptoPerpError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
