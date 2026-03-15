"""Market data logger for trading audit trail and replay.

Logs LOB, ticks, klines, snapshots, features, signals, plans, regimes,
and order decisions to per-day JSONL files under runtime/market_data/{YYYY-MM-DD}/.

Every public function is fire-and-forget: exceptions are silently swallowed
so that logging can never interrupt live trading.

Replay consumers can read these files to reconstruct a trading day bar-by-bar
with zero information leakage, producing backtest results that 100% match
what the live system would have traded.

Directory layout
----------------
runtime/market_data/
  2026-03-11/
    lob.jsonl              # order-book snapshots per cycle per symbol
    ticks.jsonl            # tick-by-tick trade data per cycle per symbol
    klines.jsonl           # last N 1-min bars per cycle per symbol
    snapshots.jsonl        # market snapshots per cycle per symbol
    features.jsonl         # strategy feature scores per cycle per symbol
    strategy_signals.jsonl # signals from each strategy per cycle
    portfolio_plan.jsonl   # combined portfolio plan per cycle
    regime.jsonl           # regime detection details per cycle
    orders.jsonl           # planned / submitted order decisions per cycle
    errors.jsonl           # diagnostic error records

Log retention
-------------
Call ``cleanup_old_logs(keep_days=N)`` (or rely on auto_trader's daily cleanup)
to delete day-directories older than N calendar days.  The function is
fire-and-forget: it never raises.
"""
from __future__ import annotations

import json
import logging
import shutil
import traceback
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

_log = logging.getLogger(__name__)

import pandas as pd

if TYPE_CHECKING:
    from ..risk.manager import PortfolioPlan
    from ..strategies.base import StrategySignal

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT.parent / "runtime"
MARKET_DATA_DIR = RUNTIME_DIR / "market_data"

# ET timezone used for trading-day folder names
_ET_ZONE = "America/New_York"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _trading_day_dir(ts: datetime | None = None) -> Path:
    """Return (and create) the market_data sub-directory for the current ET trading day."""
    if ts is None:
        ts = datetime.now(ZoneInfo("UTC"))
    et = ts.astimezone(ZoneInfo(_ET_ZONE))
    date_str = et.strftime("%Y-%m-%d")
    day_dir = MARKET_DATA_DIR / date_str
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def _ts(ts: datetime | None) -> str:
    return (ts or datetime.now(ZoneInfo("UTC"))).isoformat()


def _safe(fn, *args, **kwargs) -> None:  # noqa: ANN001
    """Call fn(*args, **kwargs), silently swallowing any exception."""
    try:
        fn(*args, **kwargs)
    except Exception:  # pragma: no cover – logging must never crash trading
        pass


# ---------------------------------------------------------------------------
# LOB  (order-book snapshot)
# ---------------------------------------------------------------------------


def _do_log_lob(code: str, order_book: dict[str, Any] | None, ts: datetime | None) -> None:
    if order_book is None:
        return
    record: dict[str, Any] = {
        "ts": _ts(ts),
        "type": "lob",
        "code": code,
        "bid": order_book.get("Bid", []),
        "ask": order_book.get("Ask", []),
    }
    _append_jsonl(_trading_day_dir(ts) / "lob.jsonl", record)


def log_lob(code: str, order_book: dict[str, Any] | None, ts: datetime | None = None) -> None:
    """Append one order-book snapshot record."""
    _safe(_do_log_lob, code, order_book, ts)


# ---------------------------------------------------------------------------
# Ticks  (逐笔)
# ---------------------------------------------------------------------------


def _do_log_ticks(code: str, ticks: pd.DataFrame, ts: datetime | None) -> None:
    if ticks.empty:
        return
    record: dict[str, Any] = {
        "ts": _ts(ts),
        "type": "ticks",
        "code": code,
        "rows": ticks.to_dict(orient="records"),
    }
    _append_jsonl(_trading_day_dir(ts) / "ticks.jsonl", record)


def log_ticks(code: str, ticks: pd.DataFrame, ts: datetime | None = None) -> None:
    """Append one tick-batch record (all ticks returned by get_rt_ticker)."""
    _safe(_do_log_ticks, code, ticks, ts)


# ---------------------------------------------------------------------------
# Klines  (1-min bars)
# ---------------------------------------------------------------------------


def _do_log_klines(code: str, bars: pd.DataFrame, timeframe: str, ts: datetime | None) -> None:
    if bars.empty:
        return
    record: dict[str, Any] = {
        "ts": _ts(ts),
        "type": "klines",
        "code": code,
        "tf": timeframe,
        # Log all rows — replay needs the full window, not just a tail.
        # Consumers can de-duplicate by (ts, code, tf) and keep the latest snapshot.
        "rows": bars.to_dict(orient="records"),
    }
    _append_jsonl(_trading_day_dir(ts) / "klines.jsonl", record)


def log_klines(
    code: str,
    bars: pd.DataFrame,
    timeframe: str = "1d",
    ts: datetime | None = None,
) -> None:
    """Append one kline-window record.

    Args:
        code: Symbol code (e.g. "US.SPY" or "BTC/USDT").
        bars: DataFrame with OHLCV columns.
        timeframe: Bar timeframe string (e.g. "1d", "1h"). Stored in the record
                   so dashboard / replay code can filter by timeframe.
        ts: Timestamp; defaults to now(UTC).
    """
    _safe(_do_log_klines, code, bars, timeframe, ts)


# ---------------------------------------------------------------------------
# Snapshot  (market quote)
# ---------------------------------------------------------------------------


def _do_log_snapshot(code: str, snapshot: pd.Series, ts: datetime | None) -> None:
    record: dict[str, Any] = {
        "ts": _ts(ts),
        "type": "snapshot",
        "code": code,
        "data": snapshot.to_dict(),
    }
    _append_jsonl(_trading_day_dir(ts) / "snapshots.jsonl", record)


def log_snapshot(code: str, snapshot: pd.Series, ts: datetime | None = None) -> None:
    """Append one market-snapshot record."""
    _safe(_do_log_snapshot, code, snapshot, ts)


# ---------------------------------------------------------------------------
# Feature  (strategy feature scores per symbol per cycle)
# ---------------------------------------------------------------------------


def _do_log_feature(feature_dict: dict[str, Any], ts: datetime | None) -> None:
    record: dict[str, Any] = {
        "ts": _ts(ts),
        "type": "feature",
        **feature_dict,
    }
    _append_jsonl(_trading_day_dir(ts) / "features.jsonl", record)


def log_feature(feature_dict: dict[str, Any], ts: datetime | None = None) -> None:
    """Append one feature record (scores for a single symbol in a single cycle).

    Args:
        feature_dict: Feature data as a dict (can be asdict of a dataclass).
        ts: Timestamp; defaults to now(UTC).
    """
    _safe(_do_log_feature, feature_dict, ts)


# ---------------------------------------------------------------------------
# Strategy Signal  (signals from each strategy per cycle)
# ---------------------------------------------------------------------------


def _do_log_strategy_signal(signal: StrategySignal, ts: datetime | None) -> None:
    record: dict[str, Any] = {
        "ts": _ts(ts),
        "type": "strategy_signal",
        "strategy_name": signal.strategy_name,
        "signal_timestamp": signal.timestamp.isoformat(),
        "target_weights": signal.target_weights,
        "scores": signal.scores,
        "metadata": signal.metadata,
    }
    _append_jsonl(_trading_day_dir(ts) / "strategy_signals.jsonl", record)


def log_strategy_signal(signal: StrategySignal, ts: datetime | None = None) -> None:
    """Append one strategy signal record.

    Args:
        signal: StrategySignal from a strategy compute_signal() call.
        ts: Timestamp; defaults to now(UTC).
    """
    _safe(_do_log_strategy_signal, signal, ts)


# ---------------------------------------------------------------------------
# Portfolio Plan  (combined plan per cycle)
# ---------------------------------------------------------------------------


def _do_log_portfolio_plan(plan: PortfolioPlan, ts: datetime | None) -> None:
    record: dict[str, Any] = {
        "ts": _ts(ts),
        "type": "portfolio_plan",
        "plan_timestamp": plan.timestamp.isoformat(),
        "regime": plan.regime,
        "raw_combined_weights": plan.raw_combined_weights,
        "final_weights": plan.final_weights,
        "total_exposure": plan.total_exposure,
        "num_signals": len(plan.strategy_signals),
        "metadata": plan.metadata,
    }
    _append_jsonl(_trading_day_dir(ts) / "portfolio_plan.jsonl", record)


def log_portfolio_plan(plan: PortfolioPlan, ts: datetime | None = None) -> None:
    """Append one portfolio plan record (final combined plan per cycle).

    Args:
        plan: PortfolioPlan from RiskManager.build_plan().
        ts: Timestamp; defaults to now(UTC).
    """
    _safe(_do_log_portfolio_plan, plan, ts)


# ---------------------------------------------------------------------------
# Regime  (regime detection details per cycle)
# ---------------------------------------------------------------------------


def _do_log_regime(regime: str, details: dict[str, Any], ts: datetime | None) -> None:
    record: dict[str, Any] = {
        "ts": _ts(ts),
        "type": "regime",
        "regime": regime,
        "details": details,
    }
    _append_jsonl(_trading_day_dir(ts) / "regime.jsonl", record)


def log_regime(regime: str, details: dict[str, Any] | None = None, ts: datetime | None = None) -> None:
    """Append one regime detection record.

    Args:
        regime: Regime string ("normal", "risk_off", "high_vol").
        details: Dict with indicators/values used for detection (vix, btc_vol, etc.).
        ts: Timestamp; defaults to now(UTC).
    """
    _safe(_do_log_regime, regime, details or {}, ts)


# ---------------------------------------------------------------------------
# Orders  (下单决定)
# ---------------------------------------------------------------------------


def _do_log_orders(
    planned_orders: list[Any],
    action: str,
    result_df: pd.DataFrame | None,
    ts: datetime | None,
) -> None:
    rows: list[dict[str, Any]] = []
    result_index: dict[str, dict[str, Any]] = {}
    if result_df is not None and not result_df.empty:
        for row in result_df.itertuples(index=False):
            symbol = str(getattr(row, "symbol", ""))
            result_index[symbol] = {
                "submit_status": str(getattr(row, "status", "")),
                "submit_detail": str(getattr(row, "detail", "")),
            }

    for order in planned_orders:
        # Support both PlannedOrder dataclass and dict
        if hasattr(order, "__dataclass_fields__"):
            row_dict = asdict(order)
        else:
            row_dict = dict(order) if isinstance(order, dict) else {"symbol": str(order)}

        row_dict["action"] = action
        symbol = row_dict.get("symbol", "")
        if symbol in result_index:
            row_dict.update(result_index[symbol])
        rows.append(row_dict)

    record: dict[str, Any] = {
        "ts": _ts(ts),
        "type": "orders",
        "action": action,
        "orders": rows,
    }
    _append_jsonl(_trading_day_dir(ts) / "orders.jsonl", record)


def log_orders(
    planned_orders: list[Any],
    action: str,
    result_df: pd.DataFrame | None = None,
    ts: datetime | None = None,
) -> None:
    """Append one order-decision record.

    Parameters
    ----------
    planned_orders:
        The list of PlannedOrder objects produced by rebalancing logic.
    action:
        ``"planned"`` before submit, ``"submitted"`` after submit.
    result_df:
        The DataFrame returned by exchange.submit_orders(), or None for dry runs.
    ts:
        Timestamp to use; defaults to now(UTC).
    """
    _safe(_do_log_orders, planned_orders, action, result_df, ts)


# ---------------------------------------------------------------------------
# Debug / diagnostic
# ---------------------------------------------------------------------------


def log_error(context: str, exc: BaseException | None = None, ts: datetime | None = None) -> None:
    """Log a structured error record (e.g. for failed data fetch). Never raises."""
    try:
        record: dict[str, Any] = {
            "ts": _ts(ts),
            "type": "error",
            "context": context,
            "detail": traceback.format_exc() if exc is not None else "",
        }
        _append_jsonl(_trading_day_dir(ts) / "errors.jsonl", record)
    except Exception:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Log rotation  (日志清理)
# ---------------------------------------------------------------------------


def cleanup_old_logs(keep_days: int = 30) -> int:
    """Delete market_data sub-directories older than *keep_days* calendar days.

    Sub-directories are expected to be named ``YYYY-MM-DD`` (the trading-day
    folder format used by :func:`_trading_day_dir`).  Any directory whose name
    parses as a date older than the cutoff is deleted recursively.

    Parameters
    ----------
    keep_days:
        Number of calendar days of logs to retain (default: 30).
        Pass 0 to delete all historical logs (keeps today only).

    Returns
    -------
    int
        Number of directories successfully deleted.

    Notes
    -----
    This function is fire-and-forget: all exceptions are swallowed so that
    a cleanup failure can never interrupt live trading.
    """
    deleted = 0
    try:
        if not MARKET_DATA_DIR.exists():
            return 0

        cutoff = datetime.now(ZoneInfo(_ET_ZONE)).date() - timedelta(days=keep_days)

        for day_dir in sorted(MARKET_DATA_DIR.iterdir()):
            if not day_dir.is_dir():
                continue
            try:
                dir_date_str = day_dir.name          # e.g. "2026-03-11"
                dir_date = datetime.strptime(dir_date_str, "%Y-%m-%d").date()
            except ValueError:
                continue  # skip directories with non-date names

            if dir_date < cutoff:
                try:
                    shutil.rmtree(day_dir)
                    deleted += 1
                    _log.info("Deleted old market-data log: %s", day_dir)
                except Exception as exc:
                    _log.warning("Could not delete %s: %s", day_dir, exc)

    except Exception as exc:  # pragma: no cover
        _log.warning("cleanup_old_logs failed: %s", exc)

    return deleted
