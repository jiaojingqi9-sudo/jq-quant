"""Tests for the embedded control-panel helpers added to dashboard_app.

These cover the pure-text helpers (``_set_env_value``) so a regression here
can't silently corrupt the user's .env when they click Plug Mode buttons.

The Streamlit render functions themselves can't be unit-tested without a
running streamlit context — those are smoke-checked via import.
"""

from __future__ import annotations

import sys


def _import_with_streamlit_stub():
    """Import ``taa_futu.dashboard_app``. Just imports the real module —
    the pure-text helpers (``_set_env_value``, ``_apply_pregate_mode``)
    don't need any streamlit runtime, they just need the module loaded.

    Earlier versions of this helper stubbed ``sys.modules['streamlit']``
    which poisoned downstream tests (e.g. ``test_dashboard_e2e.py``) that
    use ``streamlit.testing.v1.AppTest`` — the stub leaked through the
    whole pytest session. Now we rely on streamlit being installed; if
    it isn't, the import fails fast at the test level instead of
    silently corrupting state.
    """
    import importlib
    if "taa_futu.dashboard_app" in sys.modules:
        return sys.modules["taa_futu.dashboard_app"]
    return importlib.import_module("taa_futu.dashboard_app")


def test_set_env_value_replaces_existing_line() -> None:
    m = _import_with_streamlit_stub()
    text = "FOO=1\nBAR=2\n"
    out = m._set_env_value(text, "FOO", "10")
    assert out == "FOO=10\nBAR=2\n"


def test_set_env_value_appends_new_key() -> None:
    m = _import_with_streamlit_stub()
    text = "FOO=1\nBAR=2\n"
    out = m._set_env_value(text, "BAZ", "3")
    # appended at end, other keys untouched
    assert out.endswith("BAZ=3\n")
    assert "FOO=1\n" in out
    assert "BAR=2\n" in out


def test_set_env_value_handles_missing_trailing_newline() -> None:
    m = _import_with_streamlit_stub()
    out = m._set_env_value("FOO=1", "FOO", "5")
    assert out == "FOO=5"


def test_set_env_value_handles_empty_file() -> None:
    m = _import_with_streamlit_stub()
    out = m._set_env_value("", "FOO", "1")
    assert out == "FOO=1\n"


def test_set_env_value_does_not_match_prefix() -> None:
    """``STACK_BASELINE_WEIGHT=0.25`` must not be matched when we look up
    ``STACK_BASELINE`` — otherwise plug mode would corrupt the weight."""
    m = _import_with_streamlit_stub()
    text = "STACK_BASELINE_WEIGHT=0.25\nSTACK_BASELINE_ENABLED=true\n"
    out = m._set_env_value(text, "STACK_BASELINE_ENABLED", "false")
    # The weight line must be intact, only ENABLED flipped
    assert "STACK_BASELINE_WEIGHT=0.25\n" in out
    assert "STACK_BASELINE_ENABLED=false\n" in out


def test_apply_pregate_mode_unknown_returns_false() -> None:
    m = _import_with_streamlit_stub()
    ok, msg = m._apply_pregate_mode("nonsense")
    assert ok is False
    assert "nonsense" in msg


def test_apply_stack_weights_handles_zero_baseline() -> None:
    """Baseline=0 must flip STACK_BASELINE_ENABLED to false; non-zero → true."""
    m = _import_with_streamlit_stub()
    import tempfile
    from pathlib import Path
    from taa_futu import control_panel as cp

    with tempfile.TemporaryDirectory() as td:
        envp = Path(td) / ".env"
        envp.write_text("STACK_BASELINE_ENABLED=true\n", encoding="utf-8")
        # Point control_panel.ENV_FILE at our temp env file
        orig = cp.ENV_FILE
        cp.ENV_FILE = envp
        try:
            ok, msg = m._apply_stack_weights(0.0, 0.5, 0.3, 0.2)
            assert ok is True, msg
            text = envp.read_text(encoding="utf-8")
            assert "STACK_BASELINE_ENABLED=false" in text
            assert "STACK_BASELINE_WEIGHT=0.0000" in text
            assert "STACK_FUSION_WEIGHT=0.5000" in text
            assert "STACK_OFIM_WEIGHT=0.3000" in text
            assert "STACK_CASCADE_WEIGHT=0.2000" in text
            # Plug mode must be cleared so weights apply
            assert "STACK_ACTIVE_STRATEGY=\n" in text or "STACK_ACTIVE_STRATEGY=" in text.split("\n")
        finally:
            cp.ENV_FILE = orig


def test_apply_stack_weights_with_baseline_active() -> None:
    m = _import_with_streamlit_stub()
    import tempfile
    from pathlib import Path
    from taa_futu import control_panel as cp

    with tempfile.TemporaryDirectory() as td:
        envp = Path(td) / ".env"
        envp.write_text("", encoding="utf-8")
        orig = cp.ENV_FILE
        cp.ENV_FILE = envp
        try:
            ok, msg = m._apply_stack_weights(0.25, 0.25, 0.25, 0.25)
            assert ok is True, msg
            text = envp.read_text(encoding="utf-8")
            assert "STACK_BASELINE_ENABLED=true" in text
        finally:
            cp.ENV_FILE = orig


def test_stash_app_msg_writes_session_state(monkeypatch) -> None:
    """``_stash_app_msg`` must put the (ok, message) tuple into
    ``st.session_state['_app_control_msg']`` so the next rerun can show it.

    We use ``monkeypatch`` so the streamlit session_state is restored after
    this test — earlier versions mutated streamlit's real session_state
    directly and bled into the AppTest-based e2e suite.
    """
    m = _import_with_streamlit_stub()
    import streamlit as st

    fake_state = {}
    class _SS:
        def __setitem__(self, k, v): fake_state[k] = v
        def __getitem__(self, k): return fake_state[k]
        def __contains__(self, k): return k in fake_state
        def pop(self, k, default=None): return fake_state.pop(k, default)
        def get(self, k, default=None): return fake_state.get(k, default)

    monkeypatch.setattr(st, "session_state", _SS(), raising=False)
    # st.toast may be missing — _stash_app_msg already wraps it in try/except
    m._stash_app_msg(True, "started")
    assert fake_state.get("_app_control_msg") == (True, "started")
    m._stash_app_msg(False, "boom")
    assert fake_state.get("_app_control_msg") == (False, "boom")
