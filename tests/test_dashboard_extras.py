"""Tests for the Crypto / Screener / Live-Signal Streamlit pages.

We can't drive Streamlit's render loop in headless CI, so we test the
non-render helpers — those are the parts a regression would silently break.
The page renderers themselves are smoke-checked by importing the module
(catches syntax / top-level import errors).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest


def test_module_imports_and_exposes_pages() -> None:
    from taa_futu import dashboard_extras as e

    assert isinstance(e.EXTRA_PAGE_OPTIONS, list)
    assert len(e.EXTRA_PAGE_OPTIONS) == 3
    # The three known pages must match the renderer dict
    assert set(e.EXTRA_PAGE_OPTIONS) == set(e.PAGE_RENDERERS.keys())
    # Labels must be the constants exported for the host to dispatch on
    assert e.PAGE_CRYPTO in e.EXTRA_PAGE_OPTIONS
    assert e.PAGE_SCREENER in e.EXTRA_PAGE_OPTIONS
    assert e.PAGE_LIVE_SIGNAL in e.EXTRA_PAGE_OPTIONS


def test_maybe_render_unknown_page_returns_false() -> None:
    from taa_futu.dashboard_extras import maybe_render

    # Calling with a label we don't own must NOT call any renderer; returns
    # False so the host falls back to its own dispatch.
    assert maybe_render("Some other page", settings=None) is False
    assert maybe_render("", settings=None) is False


def test_read_json_missing_returns_none(tmp_path: Path) -> None:
    from taa_futu.dashboard_extras import _read_json

    assert _read_json(tmp_path / "missing.json") is None


def test_read_json_invalid_returns_none(tmp_path: Path) -> None:
    from taa_futu.dashboard_extras import _read_json

    p = tmp_path / "broken.json"
    p.write_text("{this is not json", encoding="utf-8")
    assert _read_json(p) is None


def test_read_json_valid(tmp_path: Path) -> None:
    from taa_futu.dashboard_extras import _read_json

    p = tmp_path / "status.json"
    p.write_text(json.dumps({"action": "polling", "n": 7}), encoding="utf-8")
    data = _read_json(p)
    assert data == {"action": "polling", "n": 7}


def test_age_str_buckets() -> None:
    from taa_futu.dashboard_extras import _age_str

    now = datetime.now(timezone.utc)
    assert _age_str(None) == "—"
    assert "s" in _age_str(now - timedelta(seconds=10))
    assert "min" in _age_str(now - timedelta(minutes=10))
    assert "h" in _age_str(now - timedelta(hours=2))


def test_parse_ts_handles_naive_and_zulu() -> None:
    from taa_futu.dashboard_extras import _parse_ts

    a = _parse_ts("2026-05-27T12:00:00")
    b = _parse_ts("2026-05-27T12:00:00Z")
    c = _parse_ts("2026-05-27T12:00:00+00:00")
    assert a is not None and b is not None and c is not None
    # All three should normalise to UTC and represent the same moment
    assert a == b == c
    # Bad input returns None, never raises
    assert _parse_ts("not-a-date") is None
    assert _parse_ts(None) is None
    assert _parse_ts("") is None


def test_tail_jsonl_missing_returns_empty(tmp_path: Path) -> None:
    from taa_futu.dashboard_extras import _tail_jsonl

    assert _tail_jsonl(tmp_path / "absent.jsonl", n=5) == []


def test_tail_jsonl_returns_last_n_in_order(tmp_path: Path) -> None:
    from taa_futu.dashboard_extras import _tail_jsonl

    p = tmp_path / "events.jsonl"
    lines = [json.dumps({"i": i}) for i in range(100)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tail = _tail_jsonl(p, n=5)
    assert len(tail) == 5
    assert [row["i"] for row in tail] == [95, 96, 97, 98, 99]


def test_tail_jsonl_skips_malformed_lines(tmp_path: Path) -> None:
    from taa_futu.dashboard_extras import _tail_jsonl

    p = tmp_path / "events.jsonl"
    p.write_text(
        json.dumps({"i": 1}) + "\n"
        "broken line that's not json\n"
        + json.dumps({"i": 3}) + "\n",
        encoding="utf-8",
    )
    rows = _tail_jsonl(p, n=10)
    assert [r["i"] for r in rows] == [1, 3]


def test_state_badge_has_known_states() -> None:
    from taa_futu.dashboard_extras import _state_badge

    assert "OK" in _state_badge("ok")
    assert "FAIL" in _state_badge("fail")
    assert "WARN" in _state_badge("warn")
    assert "IDLE" in _state_badge("idle")
    # Unknown state must not crash; returns a placeholder
    assert _state_badge("xxx") != ""


def test_renderer_functions_callable() -> None:
    """Each registered renderer must be a callable taking one arg.

    We can't actually call them without a Streamlit context, but checking
    the signature prevents an empty dict from getting installed by mistake.
    """
    import inspect
    from taa_futu.dashboard_extras import PAGE_RENDERERS

    for label, fn in PAGE_RENDERERS.items():
        assert callable(fn), f"{label} renderer not callable"
        sig = inspect.signature(fn)
        # Each renderer accepts a positional ``settings`` arg
        params = list(sig.parameters.values())
        assert len(params) >= 1, f"{label} renderer should take settings"
