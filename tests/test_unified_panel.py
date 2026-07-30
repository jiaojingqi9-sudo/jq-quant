"""Tests for the unified-panel status readers.

The panel itself is a Tk UI that we cannot exercise in headless CI, but the
status-reader functions are pure logic over files/sockets and are exactly
where a regression would hurt (a broken reader silently shows ``idle`` and
the user blames the launcher).

We do NOT instantiate the Tk root here — that would require an X display.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest


@pytest.fixture
def panel(monkeypatch, tmp_path: Path):
    """Yield the unified_panel module with file paths swapped into a tmp_path
    sandbox so the readers operate on test fixtures, not the user's real
    runtime files."""
    from taa_futu import unified_panel as p

    auto_status = tmp_path / "auto_trader_status.json"
    watchdog_status = tmp_path / "watchdog_status.json"
    crypto_ofim_status = tmp_path / "crypto_ofim_status.json"
    crypto_perp_status = tmp_path / "crypto_perp_status.json"
    watcher_alive = tmp_path / "_watcher_alive.txt"

    monkeypatch.setattr(p, "AUTO_TRADER_STATUS", auto_status)
    monkeypatch.setattr(p, "WATCHDOG_STATUS", watchdog_status)
    monkeypatch.setattr(p, "CRYPTO_OFIM_STATUS", crypto_ofim_status)
    monkeypatch.setattr(p, "CRYPTO_PERP_STATUS", crypto_perp_status)
    monkeypatch.setattr(p, "FUTU_QUEUE_ALIVE", watcher_alive)
    yield p, {
        "auto": auto_status,
        "watchdog": watchdog_status,
        "ofim": crypto_ofim_status,
        "perp": crypto_perp_status,
        "watcher_alive": watcher_alive,
    }


# ─────────────────────────── 邮差 ────────────────────────────


def test_watcher_card_missing_file_is_fail(panel) -> None:
    p, _ = panel
    card = p.read_watcher_card()
    assert card.state == "fail"
    assert "未运行" in card.headline


def test_watcher_card_recent_heartbeat_is_ok(panel) -> None:
    p, paths = panel
    paths["watcher_alive"].write_text(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        encoding="utf-8",
    )
    card = p.read_watcher_card()
    assert card.state == "ok"


def test_watcher_card_stale_heartbeat_is_fail(panel) -> None:
    p, paths = panel
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    paths["watcher_alive"].write_text(old.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    card = p.read_watcher_card()
    assert card.state == "fail"


def test_watcher_card_warn_band(panel) -> None:
    p, paths = panel
    old = datetime.now(timezone.utc) - timedelta(seconds=20)
    paths["watcher_alive"].write_text(old.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    card = p.read_watcher_card()
    assert card.state == "warn"


# ─────────────────────────── auto_trader ────────────────────────────


def test_auto_trader_card_no_status_is_idle(panel) -> None:
    p, _ = panel
    card = p.read_auto_trader_card()
    assert card.state == "idle"


def test_auto_trader_card_dead_pid_is_fail(panel) -> None:
    p, paths = panel
    # PID 1 is init; os.kill(1, 0) succeeds for root, raises EPERM for users.
    # We need a pid that is guaranteed not alive — use a very large one.
    payload = {
        "pid": 999_999_999,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "action": "polling",
    }
    paths["auto"].write_text(json.dumps(payload), encoding="utf-8")
    card = p.read_auto_trader_card()
    assert card.state == "fail"
    assert "pid" in card.headline.lower() or "进程" in card.headline


def test_auto_trader_card_lockdown_counter_warns(panel) -> None:
    p, paths = panel
    import os
    payload = {
        "pid": os.getpid(),  # ourselves — definitely alive
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "action": "polling",
        "consecutive_transient_count": 2,
    }
    paths["auto"].write_text(json.dumps(payload), encoding="utf-8")
    card = p.read_auto_trader_card()
    assert card.state == "warn"
    assert "lockdown" in card.headline.lower()


# ─────────────────────────── crypto ────────────────────────────


def test_crypto_ofim_card_missing_is_idle(panel) -> None:
    p, _ = panel
    card = p.read_crypto_ofim_card()
    assert card.state == "idle"


def test_crypto_perp_card_running_is_ok(panel) -> None:
    p, paths = panel
    paths["perp"].write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "action": "polling",
    }), encoding="utf-8")
    card = p.read_crypto_perp_card()
    assert card.state == "ok"


# ─────────────────────────── helpers ────────────────────────────


def test_fmt_age_buckets() -> None:
    from taa_futu.unified_panel import _fmt_age

    assert _fmt_age(None) == "—"
    assert "s" in _fmt_age(30)
    assert "min" in _fmt_age(120)
    assert "h" in _fmt_age(7200)
    assert "d" in _fmt_age(200_000)


def test_state_color_maps_known_states() -> None:
    from taa_futu.unified_panel import _state_color, COLOR_OK, COLOR_FAIL

    assert _state_color("ok") == COLOR_OK
    assert _state_color("fail") == COLOR_FAIL
    assert _state_color("unknown_xyz") != ""  # fallback to idle color, not crash


def test_card_data_dataclass_fields() -> None:
    from taa_futu.unified_panel import CardData

    c = CardData("test", "ok", "headline", "detail")
    assert c.name == "test"
    assert c.state == "ok"
    assert c.headline == "headline"
    assert c.detail == "detail"


def test_open_command_file_missing_returns_false(tmp_path) -> None:
    from taa_futu.unified_panel import open_command_file

    ok, msg = open_command_file(tmp_path / "does_not_exist.command")
    assert ok is False
    assert "找不到" in msg or "missing" in msg.lower()


# ─────────── ControlPanel embed-mode contract ───────────


def test_control_panel_init_accepts_master_kwarg() -> None:
    """ControlPanel must accept ``master`` so it can be embedded in tabs.

    A regression here would break the 完整控制台 tab. We check the signature
    rather than instantiating to avoid needing a real Tk display in CI.
    """
    import inspect
    from taa_futu.control_panel import ControlPanel

    sig = inspect.signature(ControlPanel.__init__)
    assert "master" in sig.parameters, "ControlPanel.__init__ must accept master"
    # backward compatibility: master defaults to None so the legacy
    # ``ControlPanel().run()`` path still works
    assert sig.parameters["master"].default is None


def test_embedded_frame_class_absorbs_toplevel_methods() -> None:
    """The _EmbeddedFrame shim must implement all toplevel-only methods
    ControlPanel calls on ``self.root`` so embedding does not blow up."""
    from taa_futu.control_panel import _EmbeddedFrame

    required = [
        "title", "geometry", "minsize", "maxsize", "resizable",
        "protocol", "iconbitmap", "iconphoto", "wm_attributes",
        "deiconify", "iconify", "withdraw", "mainloop", "quit",
    ]
    for method in required:
        assert hasattr(_EmbeddedFrame, method), f"_EmbeddedFrame missing {method}"
        # Each must be a callable that does not raise on dummy args
        # (we can't call them without an instance, but checking it's a
        # method/function suffices)
        attr = getattr(_EmbeddedFrame, method)
        assert callable(attr), f"_EmbeddedFrame.{method} is not callable"
