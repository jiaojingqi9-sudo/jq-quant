"""Market data logger for exact fusion replay.

Logs LOB, ticks, klines, snapshots, features, plans, and order decisions
to per-day JSONL files under runtime/market_data/{YYYY-MM-DD}/.

Every public function is fire-and-forget: exceptions are silently swallowed
so that logging can never interrupt live trading.

Replay consumers can read these files to reconstruct a trading day bar-by-bar
with zero information leakage, producing backtest results that 100% match
what the live system would have traded.

Directory layout
----------------
runtime/market_data/
  2026-03-11/
    lob.jsonl          # order-book snapshots per cycle per symbol
    ticks.jsonl        # tick-by-tick trade data per cycle per symbol
    klines.jsonl       # last N 1-min bars per cycle per symbol
    snapshots.jsonl    # market snapshots per cycle per symbol
    features.jsonl     # FusionFeature scores per cycle per symbol
    plan.jsonl         # FusionPlan (benchmark score + target weights) per cycle
    orders.jsonl       # planned / submitted order decisions per cycle
"""
from __future__ import annotations

import json
import traceback
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from .fusion_intraday import FusionFeature, FusionPlan
    from .futu_gateway import PlannedOrder

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime"
MARKET_DATA_DIR = RUNTIME_DIR / "market_data"

# ET timezone used for trading-day folder names
_ET_ZONE = "America/New_York"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _trading_day_dir(ts: datetime | None = None) -> Path:
    """Return (and create) the market_data sub-directory for the current ET trading day."""
    from zoneinfo import ZoneInfo  # lazy import — not available on all Python < 3.9 builds

    if ts is None:
        ts = datetime.now(UTC)
    et = ts.astimezone(ZoneInfo(_ET_ZONE))
    date_str = et.strftime("%Y-%m-%d")
    day_dir = MARKET_DATA_DIR / date_str
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _ts(ts: datetime | None) -> str:
    return (ts or datetime.now(UTC)).isoformat()


def _safe(fn, *args, **kwargs) -> None:  # noqa: ANN001
    """Call fn(*args, **kwargs), silently swallowing any exception."""
    try:
        fn(*args, **kwargs)
    except Exception:  # pragma: no cover – logging must never crash trading
        pass


def _iter_day_dirs(start: str, end: str | None = None) -> list[Path]:
    start_date = pd.Timestamp(start).date()
    end_date = pd.Timestamp(end or start).date()
    day_dirs: list[Path] = []
    for path in sorted(MARKET_DATA_DIR.glob("*")):
        if not path.is_dir():
            continue
        try:
            day = pd.Timestamp(path.name).date()
        except ValueError:
            continue
        if start_date <= day <= end_date:
            day_dirs.append(path)
    return day_dirs


def load_records(filename: str, start: str, end: str | None = None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for day_dir in _iter_day_dirs(start, end):
        file_rows = _load_jsonl(day_dir / f"{filename}.jsonl")
        for row in file_rows:
            row.setdefault("market_date", day_dir.name)
            rows.append(row)
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    if "ts" in frame.columns:
        frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
    return frame.sort_values("ts", na_position="last").reset_index(drop=True)


def load_order_records(start: str, end: str | None = None) -> pd.DataFrame:
    records = load_records("orders", start, end)
    if records.empty:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for record in records.itertuples(index=False):
        orders = getattr(record, "orders", []) or []
        for order in orders:
            if not isinstance(order, dict):
                continue
            rows.append(
                {
                    "ts": getattr(record, "ts", pd.NaT),
                    "market_date": getattr(record, "market_date", ""),
                    "action": getattr(record, "action", ""),
                    **order,
                }
            )
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    if "ts" in frame.columns:
        frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
    if "submit_detail" in frame.columns:
        frame["submit_detail"] = frame["submit_detail"].astype(str)
    return frame.sort_values(["ts", "code"], na_position="last").reset_index(drop=True)


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

def _do_log_klines(code: str, bars: pd.DataFrame, ts: datetime | None) -> None:
    if bars.empty:
        return
    record: dict[str, Any] = {
        "ts": _ts(ts),
        "type": "klines",
        "code": code,
        # Log all rows — replay needs the full window, not just a tail.
        # Consumers can de-duplicate by (ts, code) and keep the latest snapshot.
        "rows": bars.to_dict(orient="records"),
    }
    _append_jsonl(_trading_day_dir(ts) / "klines.jsonl", record)


def log_klines(code: str, bars: pd.DataFrame, ts: datetime | None = None) -> None:
    """Append one kline-window record."""
    _safe(_do_log_klines, code, bars, ts)


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
# Feature  (打分 — FusionFeature per symbol per cycle)
# ---------------------------------------------------------------------------

def _do_log_feature(feature: "FusionFeature", ts: datetime | None) -> None:
    record: dict[str, Any] = {
        "ts": _ts(ts),
        "type": "feature",
        **asdict(feature),
    }
    _append_jsonl(_trading_day_dir(ts) / "features.jsonl", record)


def log_feature(feature: "FusionFeature", ts: datetime | None = None) -> None:
    """Append one FusionFeature record (scores for a single symbol in a single cycle)."""
    _safe(_do_log_feature, feature, ts)


# ---------------------------------------------------------------------------
# Plan  (信号 — FusionPlan per cycle)
# ---------------------------------------------------------------------------

def _do_log_plan(plan: "FusionPlan", ts: datetime | None) -> None:
    # features list is already captured inside the plan; we log the full plan
    # so that benchmark_score + target_weights + per-symbol features are
    # all stored atomically in one record.
    record: dict[str, Any] = {
        "ts": _ts(ts),
        "type": "plan",
        "benchmark": plan.benchmark,
        "benchmark_score": plan.benchmark_score,
        "exposure": plan.exposure,
        "target_weights": plan.target_weights,
        "features": [asdict(f) for f in plan.features],
    }
    _append_jsonl(_trading_day_dir(ts) / "plan.jsonl", record)


def log_plan(plan: "FusionPlan", ts: datetime | None = None) -> None:
    """Append one FusionPlan record (full cycle output including all feature scores)."""
    _safe(_do_log_plan, plan, ts)


# ---------------------------------------------------------------------------
# Orders  (下单决定)
# ---------------------------------------------------------------------------

def _do_log_orders(
    planned_orders: list["PlannedOrder"],
    action: str,
    result_df: pd.DataFrame | None,
    ts: datetime | None,
) -> None:
    rows: list[dict[str, Any]] = []
    result_index: dict[str, dict[str, Any]] = {}
    if result_df is not None and not result_df.empty:
        for row in result_df.itertuples(index=False):
            code = str(getattr(row, "code", ""))
            result_index[code] = {
                "submit_status": str(getattr(row, "status", "")),
                "submit_detail": str(getattr(row, "detail", "")),
            }

    for order in planned_orders:
        row_dict = asdict(order)
        row_dict["action"] = action
        if order.code in result_index:
            row_dict.update(result_index[order.code])
        rows.append(row_dict)

    record: dict[str, Any] = {
        "ts": _ts(ts),
        "type": "orders",
        "action": action,
        "orders": rows,
    }
    _append_jsonl(_trading_day_dir(ts) / "orders.jsonl", record)


def log_orders(
    planned_orders: list["PlannedOrder"],
    action: str,
    result_df: pd.DataFrame | None = None,
    ts: datetime | None = None,
) -> None:
    """Append one order-decision record.

    Parameters
    ----------
    planned_orders:
        The list of PlannedOrder objects produced by plan_rebalance / auto_trader.
    action:
        ``"planned"`` before submit, ``"submitted"`` after submit.
    result_df:
        The DataFrame returned by trader.submit_orders(), or None for dry runs.
    ts:
        Timestamp to use; defaults to now(UTC).
    """
    _safe(_do_log_orders, planned_orders, action, result_df, ts)


# ---------------------------------------------------------------------------
# Debug / diagnostic
# ---------------------------------------------------------------------------

def log_error(context: str, exc: BaseException | None = None, ts: datetime | None = None) -> None:
    """Log a structured error record (e.g. for failed LOB fetch). Never raises."""
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
