from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_DIR = REPO_ROOT / "runtime" / "audit"


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        return value.to_dict("records")
    if isinstance(value, pd.Series):
        return value.to_dict()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dict__"):
        return value.__dict__
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_audit_event(market_date: str, payload: dict[str, Any]) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_DIR / f"{market_date}.jsonl"
    record = {
        "logged_at": datetime.now(UTC).isoformat(),
        **payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=_json_default, ensure_ascii=False) + "\n")
    return path


def load_audit_events(start: str, end: str | None = None) -> pd.DataFrame:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    start_date = pd.Timestamp(start).date()
    end_date = pd.Timestamp(end or start).date()
    rows: list[dict[str, Any]] = []
    for path in sorted(AUDIT_DIR.glob("*.jsonl")):
        try:
            day = pd.Timestamp(path.stem).date()
        except ValueError:
            continue
        if day < start_date or day > end_date:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    if "logged_at" in frame.columns:
        frame["logged_at"] = pd.to_datetime(frame["logged_at"], errors="coerce")
    return frame.sort_values(["timestamp", "logged_at"], na_position="last").reset_index(drop=True)
