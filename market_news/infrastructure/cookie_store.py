from __future__ import annotations

from datetime import datetime, UTC
import json
from pathlib import Path
import shutil


def market_news_cookie_dir() -> Path:
    path = Path.home() / ".market_news"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def resolve_cookie_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def load_cookies(path: str | Path) -> dict[str, str]:
    cookie_path = resolve_cookie_path(path)
    payload = json.loads(cookie_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Cookie file must contain a JSON object: {cookie_path}")
    return {
        str(key): str(value)
        for key, value in payload.items()
        if str(value).strip()
    }


def install_cookie_file(source_path: str | Path, *, target_name: str) -> Path:
    source = resolve_cookie_path(source_path)
    target = market_news_cookie_dir() / target_name
    shutil.copyfile(source, target)
    target.chmod(0o600)
    # Fresh cookies mean any previous expiry flag is no longer valid
    clear_cookie_expired(target)
    return target


# ---------------------------------------------------------------------------
# Cookie expiry flag helpers
# ---------------------------------------------------------------------------
# When a collector detects an expired cookie it calls mark_cookie_expired().
# A small JSON file is written next to the cookie file (same name + ".expired").
# The CLI "cookies check" command reads these flags so the user gets a clear
# prompt to refresh — rather than silently seeing empty results for days.
# ---------------------------------------------------------------------------

def _expired_flag_path(cookie_path: str | Path) -> Path:
    return resolve_cookie_path(cookie_path).with_suffix(".expired")


def mark_cookie_expired(cookie_path: str | Path, *, reason: str = "") -> None:
    """Write an expiry sentinel alongside the cookie file (idempotent)."""
    flag = _expired_flag_path(cookie_path)
    # Don't overwrite if already flagged — preserve the original detection time
    if flag.exists():
        return
    flag.write_text(
        json.dumps(
            {
                "expired_at": datetime.now(tz=UTC).isoformat(),
                "reason": reason,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def clear_cookie_expired(cookie_path: str | Path) -> None:
    """Remove the expiry sentinel (called automatically when new cookies are installed)."""
    _expired_flag_path(cookie_path).unlink(missing_ok=True)


def is_cookie_expired(cookie_path: str | Path) -> tuple[bool, str]:
    """Return (expired, reason). expired=False when no flag file exists."""
    flag = _expired_flag_path(cookie_path)
    if not flag.exists():
        return False, ""
    try:
        data = json.loads(flag.read_text(encoding="utf-8"))
        return True, data.get("reason", "unknown")
    except Exception:
        return True, "flag file unreadable"
