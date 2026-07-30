from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime"
STOCK_FILLS_FILE = RUNTIME_DIR / "stock_fills.jsonl"
STOCK_LEDGER_EPOCH_FILE = RUNTIME_DIR / "stock_ledger_epoch.json"
STOCK_JOURNAL_FILE = RUNTIME_DIR / "stock_journal.jsonl"


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def append_stock_fill(record: dict[str, Any], *, fills_path: Path = STOCK_FILLS_FILE) -> None:
    fills_path.parent.mkdir(parents=True, exist_ok=True)
    with fills_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")


def load_stock_fill_records(fills_path: Path = STOCK_FILLS_FILE) -> list[dict[str, Any]]:
    if not fills_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = fills_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_recorded_stock_fill_ids(fills_path: Path = STOCK_FILLS_FILE) -> set[str]:
    ids: set[str] = set()
    for row in load_stock_fill_records(fills_path):
        event_id = str(row.get("event_id") or "").strip()
        if event_id:
            ids.add(event_id)
    return ids


def load_stock_order_fill_cumulatives(
    fills_path: Path = STOCK_FILLS_FILE,
) -> dict[str, dict[str, float]]:
    """Return cumulative recorded quantity/notional/fees by broker order id."""

    cumulative: dict[str, dict[str, float]] = {}
    for row in load_stock_fill_records(fills_path):
        order_id = str(row.get("order_id") or "").strip()
        if not order_id:
            continue
        try:
            qty = float(row.get("quantity", 0.0) or 0.0)
            price = float(row.get("price", 0.0) or 0.0)
            fee = float(row.get("fee", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        bucket = cumulative.setdefault(order_id, {"quantity": 0.0, "notional": 0.0, "fee": 0.0})
        bucket["quantity"] += max(0.0, qty)
        bucket["notional"] += max(0.0, qty * price)
        bucket["fee"] += max(0.0, fee)
    return cumulative


def count_stock_fills(fills_path: Path = STOCK_FILLS_FILE) -> int:
    return len(load_stock_fill_records(fills_path))


def load_stock_ledger_epoch(epoch_path: Path = STOCK_LEDGER_EPOCH_FILE) -> dict[str, Any]:
    if not epoch_path.exists():
        return {}
    try:
        payload = json.loads(epoch_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_stock_ledger_epoch(
    *,
    reason: str,
    account_snapshot: dict[str, Any] | None = None,
    fills_path: Path = STOCK_FILLS_FILE,
    epoch_path: Path = STOCK_LEDGER_EPOCH_FILE,
) -> Path:
    epoch_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "reason": reason,
        "account_snapshot": account_snapshot or {},
        "fills_count_at_reset": count_stock_fills(fills_path),
    }
    tmp = epoch_path.with_suffix(epoch_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    tmp.replace(epoch_path)
    return epoch_path
