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

import gzip
import json
import traceback
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from .fusion_intraday import FusionFeature, FusionPlan
    from .futu_gateway import PlannedOrder

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime"
MARKET_DATA_DIR = RUNTIME_DIR / "market_data"
LOB_CACHE_FILE = RUNTIME_DIR / "lob_cache.json"

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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    paths = [path]
    gz_path = path.with_suffix(path.suffix + ".gz")
    if gz_path.exists():
        paths.append(gz_path)
    for candidate in paths:
        if not candidate.exists():
            continue
        opener = gzip.open if candidate.suffix == ".gz" else open
        with opener(candidate, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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


def market_data_disk_usage() -> dict[str, Any]:
    days: list[dict[str, Any]] = []
    total_bytes = 0
    if not MARKET_DATA_DIR.exists():
        return {"root": str(MARKET_DATA_DIR), "total_bytes": 0, "days": []}
    for day_dir in sorted(MARKET_DATA_DIR.glob("*")):
        if not day_dir.is_dir():
            continue
        size = sum(path.stat().st_size for path in day_dir.rglob("*") if path.is_file())
        total_bytes += size
        days.append({"date": day_dir.name, "bytes": size, "path": str(day_dir)})
    return {"root": str(MARKET_DATA_DIR), "total_bytes": total_bytes, "days": days}


def market_data_retention_plan(*, keep_days: int, today: date | None = None) -> dict[str, Any]:
    keep_days = max(1, int(keep_days))
    today = today or datetime.now(UTC).date()
    cutoff = today - timedelta(days=keep_days - 1)
    older: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for item in market_data_disk_usage()["days"]:
        try:
            day = pd.Timestamp(item["date"]).date()
        except (TypeError, ValueError):
            kept.append(item)
            continue
        if day < cutoff:
            older.append(item)
        else:
            kept.append(item)
    return {
        "keep_days": keep_days,
        "cutoff": cutoff.isoformat(),
        "older_bytes": sum(int(item["bytes"]) for item in older),
        "older_days": older,
        "kept_days": kept,
    }


# ---------------------------------------------------------------------------
# LOB  (order-book snapshot)
# ---------------------------------------------------------------------------

def _do_log_lob(code: str, order_book: dict[str, Any] | None, ts: datetime | None) -> None:
    if order_book is None:
        return
    stamp = _ts(ts)
    record: dict[str, Any] = {
        "ts": stamp,
        "type": "lob",
        "code": code,
        "bid": order_book.get("Bid", []),
        "ask": order_book.get("Ask", []),
    }
    _append_jsonl(_trading_day_dir(ts) / "lob.jsonl", record)
    cache = _read_json(LOB_CACHE_FILE)
    books = dict(cache.get("books") or {})
    books[code] = {
        "ts": stamp,
        "Bid": order_book.get("Bid", []),
        "Ask": order_book.get("Ask", []),
    }
    _write_json_atomic(
        LOB_CACHE_FILE,
        {
            "updated_at": stamp,
            "source": "stock_lob_logger",
            "books": books,
        },
    )


def log_lob(code: str, order_book: dict[str, Any] | None, ts: datetime | None = None) -> None:
    """Append one order-book snapshot record."""
    _safe(_do_log_lob, code, order_book, ts)


def load_lob_cache(code: str, *, max_age_seconds: int = 5) -> dict[str, Any] | None:
    payload = _read_json(LOB_CACHE_FILE)
    books = payload.get("books") if isinstance(payload, dict) else None
    if not isinstance(books, dict) or code not in books:
        return None
    book = books.get(code)
    if not isinstance(book, dict):
        return None
    try:
        ts = pd.Timestamp(book.get("ts"))
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    age = (pd.Timestamp.utcnow() - ts.tz_convert("UTC")).total_seconds()
    if age > max(0, int(max_age_seconds)):
        return None
    if not book.get("Bid") or not book.get("Ask"):
        return None
    return {"Bid": book.get("Bid", []), "Ask": book.get("Ask", [])}


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
        row_dict.setdefault("strategy_source", getattr(order, "strategy_source", "Unclassified"))
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
