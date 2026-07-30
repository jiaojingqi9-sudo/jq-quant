from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime"

# The events journal file. Honour ``STOCK_EVENTS_FILE_OVERRIDE`` so test
# harnesses (conftest) can redirect writes into a tmp dir without having
# to monkeypatch every call site. Production daemons leave the env var
# unset and write to the canonical runtime path.
_default_events = RUNTIME_DIR / "stock_events.jsonl"
_override = os.environ.get("STOCK_EVENTS_FILE_OVERRIDE")
STOCK_EVENTS_FILE = Path(_override) if _override else _default_events


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return value.__dict__
    return str(value)


def append_stock_event(event_type: str, payload: dict[str, Any] | None = None, *, cycle_id: str | None = None) -> None:
    """Best-effort stock runtime event journal.

    Trading must never depend on this file. It exists so live decisions can be
    replayed and audited without scraping process logs.
    """

    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            **({"cycle_id": cycle_id} if cycle_id else {}),
            **(payload or {}),
        }
        with STOCK_EVENTS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
    except Exception:
        return


def load_stock_events(*, tail: int = 200) -> list[dict[str, Any]]:
    if not STOCK_EVENTS_FILE.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = STOCK_EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines[-max(1, int(tail)) :]:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
