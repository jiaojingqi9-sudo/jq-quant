from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from taa_futu import crypto_ofim_app


def test_jsonl_frame_reads_sparse_tail_without_full_file(tmp_path: Path) -> None:
    path = tmp_path / "large.jsonl"
    with path.open("wb") as fh:
        fh.write(b'{"i": 0}\n')
        fh.seek(32 * 1024 * 1024)
        fh.write(b'\n{"i": 1}\nnot json\n{"i": 2}\n')

    frame = crypto_ofim_app._jsonl_frame(path, tail=3)

    assert frame["i"].tolist() == [1, 2]


def test_jsonl_frame_ignores_missing_file(tmp_path: Path) -> None:
    frame = crypto_ofim_app._jsonl_frame(tmp_path / "missing.jsonl", tail=10)

    assert frame.empty


def test_app_pid_running_treats_ps_denial_as_running(monkeypatch) -> None:
    monkeypatch.setattr(crypto_ofim_app.os, "kill", lambda _pid, _sig: None)

    class _Denied:
        returncode = 126
        stdout = ""

    monkeypatch.setattr(crypto_ofim_app.subprocess, "run", lambda *_args, **_kwargs: _Denied())

    assert crypto_ofim_app._pid_running(123) is True


def test_watchdog_banner_is_idle_when_auto_is_stopped() -> None:
    state = crypto_ofim_app._watchdog_banner_state(
        auto_running=False,
        watchdog_pid=None,
        watchdog_running=False,
        watchdog={"health": "stopped", "detail": "watchdog stopped"},
    )

    assert state["class"] == "status-good"
    assert state["running"] == "standby"
    assert state["health"] == "ok"


def test_watchdog_banner_warns_when_auto_runs_unwatched() -> None:
    state = crypto_ofim_app._watchdog_banner_state(
        auto_running=True,
        watchdog_pid=None,
        watchdog_running=False,
        watchdog={"health": "stopped", "detail": "watchdog stopped"},
    )

    assert state["class"] == "status-warn"
    assert state["running"] == "stopped"


def test_start_auto_blocks_testnet_without_explicit_enable(monkeypatch) -> None:
    monkeypatch.delenv("CRYPTO_OFIM_AUTO_ENABLED", raising=False)
    monkeypatch.setattr(crypto_ofim_app, "_read_pid", lambda: None)
    monkeypatch.setattr(crypto_ofim_app, "_pid_running", lambda pid: False)
    monkeypatch.setattr(crypto_ofim_app, "load_crypto_ofim_settings", lambda _env: SimpleNamespace(mode="testnet"))

    def _fail_popen(*_args, **_kwargs):
        raise AssertionError("testnet auto-submit should not launch without CRYPTO_OFIM_AUTO_ENABLED=true")

    monkeypatch.setattr(crypto_ofim_app.subprocess, "Popen", _fail_popen)

    ok, message = crypto_ofim_app._start_auto(15)

    assert ok is False
    assert "CRYPTO_OFIM_AUTO_ENABLED=true" in message
