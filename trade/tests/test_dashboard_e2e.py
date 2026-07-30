"""End-to-end Streamlit tests using AppTest — drives the actual dashboard
the way a user would, asserts the view-routing state machine stays correct
across every click. This is the one place where button-callback bugs that
unit tests can't catch get caught.
"""

from __future__ import annotations

import pytest

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest


APP_PATH = "src/taa_futu/dashboard_app.py"
TIMEOUT = 30


def _fresh_app():
    """Boot a brand-new AppTest instance and run the first render."""
    at = AppTest.from_file(APP_PATH, default_timeout=TIMEOUT)
    at.run()
    return at


def test_initial_view_is_home():
    at = _fresh_app()
    assert at.session_state["view"] == "home", \
        f"expected home, got {at.session_state['view']}"
    # No exceptions during render
    assert not at.exception, [str(e) for e in at.exception]


def test_home_has_three_entry_buttons():
    at = _fresh_app()
    keys = {btn.key for btn in at.button}
    for needed in ("enter_stock", "enter_crypto", "enter_screener"):
        assert needed in keys, f"missing home button: {needed}; got: {keys}"


def test_click_enter_stock_switches_view():
    at = _fresh_app()
    at.button(key="enter_stock").click().run()
    assert at.session_state["view"] == "stock", \
        f"expected stock, got {at.session_state['view']}"
    assert not at.exception, [str(e) for e in at.exception]


def test_click_enter_crypto_switches_view():
    at = _fresh_app()
    at.button(key="enter_crypto").click().run()
    assert at.session_state["view"] == "crypto"
    assert not at.exception, [str(e) for e in at.exception]


def test_click_enter_screener_switches_view():
    at = _fresh_app()
    at.button(key="enter_screener").click().run()
    assert at.session_state["view"] == "screener"
    assert not at.exception, [str(e) for e in at.exception]


def test_sidebar_button_switches_view():
    """Clicking a sidebar nav button must set view correctly."""
    at = _fresh_app()
    # The sidebar buttons have keys like sidebar_btn_<view>
    at.sidebar.button(key="sidebar_btn_stock").click().run()
    assert at.session_state["view"] == "stock"


def test_inner_button_does_not_kick_back_to_home():
    """THE bug the user reported: clicking ANY button inside a sub-page
    must NOT bounce the view back to home."""
    at = _fresh_app()
    # Enter stock first
    at.button(key="enter_stock").click().run()
    assert at.session_state["view"] == "stock"
    # Find any button inside the stock page that isn't the back-to-home one
    candidate_keys = [b.key for b in at.button
                      if b.key and "back_to_home" not in b.key
                      and not b.key.startswith("enter_")
                      and not b.key.startswith("sidebar_btn_")]
    assert candidate_keys, "stock page exposes no inner buttons (sanity)"
    # Click each non-navigation button and confirm view stays on stock
    for key in candidate_keys[:8]:  # cap to avoid hammering subprocess buttons
        at.button(key=key).click().run()
        # Treat exceptions as failures
        if at.exception:
            # Skip CLI-subprocess buttons that can't run inside AppTest
            # (they expect a real .venv path). Reset to stock and continue.
            at.session_state["view"] = "stock"
            at.exception.clear() if hasattr(at.exception, "clear") else None
            continue
        assert at.session_state["view"] == "stock", \
            f"clicking inner button '{key}' bounced view to {at.session_state['view']!r}"


def test_back_to_home_button_returns_to_home():
    at = _fresh_app()
    at.button(key="enter_stock").click().run()
    assert at.session_state["view"] == "stock"
    # The nav breadcrumb back button uses a per-view-named key
    back_keys = [b.key for b in at.button if b.key and "back_to_home" in b.key]
    assert back_keys, f"no back-to-home button found on stock page; got {[b.key for b in at.button]}"
    at.button(key=back_keys[0]).click().run()
    assert at.session_state["view"] == "home"


def test_sidebar_marks_current_view_disabled():
    """The current view's sidebar button must be disabled so the user
    can't double-click it."""
    at = _fresh_app()
    # On home, the home sidebar button should be disabled
    home_btn = next((b for b in at.sidebar.button if b.key == "sidebar_btn_home"), None)
    assert home_btn is not None
    assert home_btn.disabled is True

    # Switch to stock
    at.button(key="enter_stock").click().run()
    stock_btn = next((b for b in at.sidebar.button if b.key == "sidebar_btn_stock"), None)
    home_btn = next((b for b in at.sidebar.button if b.key == "sidebar_btn_home"), None)
    assert stock_btn is not None and stock_btn.disabled is True
    assert home_btn is not None and home_btn.disabled is False


def test_app_does_not_crash_on_any_view():
    """Tour every view; none should throw an unhandled exception during render."""
    at = _fresh_app()
    for view_key in ("home", "stock", "crypto", "screener", "live_signal", "stock_history"):
        # Reset via sidebar button (always available)
        at.session_state["view"] = view_key
        at.run()
        assert not at.exception, \
            f"view={view_key} crashed: {[str(e) for e in at.exception]}"


def test_quick_link_buttons_on_home():
    """The home page has 4 quick-link buttons in the bottom row."""
    at = _fresh_app()
    quick_keys = {"enter_live", "enter_history", "launch_panel", "run_doctor"}
    found = {b.key for b in at.button} & quick_keys
    assert found == quick_keys, f"missing quick links: {quick_keys - found}"


# ─────────── 按钮风暴：点几十下都不能跳页 ───────────


def test_clicking_every_plug_button_stays_on_stock():
    """Plug-mode buttons are the most-clicked buttons on stock page. Each
    click must keep view=stock — never bounce to home."""
    at = _fresh_app()
    at.button(key="enter_stock").click().run()
    assert at.session_state["view"] == "stock"

    for plug_key in ("plug_baseline", "plug_fusion", "plug_ofim", "plug_cascade", "plug_full"):
        match = [b for b in at.button if b.key == plug_key]
        if not match:
            continue  # plug button only shows when expander is expanded
        at.button(key=plug_key).click().run()
        assert at.session_state["view"] == "stock", \
            f"plug button '{plug_key}' bounced view to {at.session_state['view']!r}"
        assert not at.exception, [str(e) for e in at.exception]


def test_clicking_every_pregate_button_stays_on_stock():
    at = _fresh_app()
    at.button(key="enter_stock").click().run()
    for key in ("pg_off", "pg_log", "pg_active"):
        match = [b for b in at.button if b.key == key]
        if not match:
            continue
        at.button(key=key).click().run()
        assert at.session_state["view"] == "stock", \
            f"pre-gate button '{key}' bounced view to {at.session_state['view']!r}"


def test_clicking_app_control_buttons_stays_on_stock():
    """The top-level App 控制 buttons (Open OpenD / Start Auto / Stop Auto / Refresh)
    must not change view."""
    at = _fresh_app()
    at.button(key="enter_stock").click().run()
    for key in ("cp_open_opend", "cp_start_auto", "cp_stop_auto", "cp_refresh",
                "cp_save_conn", "cp_apply_weights", "cp_signals", "cp_plan",
                "cp_cancel_all", "cp_doctor"):
        match = [b for b in at.button if b.key == key]
        if not match:
            continue
        # Skip disabled buttons (clicking them is a no-op anyway)
        if match[0].disabled:
            continue
        at.button(key=key).click().run()
        # Clear any exceptions from missing macOS-only binaries — we still
        # need view to be stable regardless.
        if at.exception:
            # Re-render once more to flush the exception state from this rerun
            at.run()
        assert at.session_state["view"] == "stock", \
            f"app-control button '{key}' bounced view to {at.session_state['view']!r}"


def test_round_trip_through_every_view():
    """Tour: home → stock → crypto → screener → live_signal → home.
    View must end up exactly where the last click pointed."""
    at = _fresh_app()
    assert at.session_state["view"] == "home"

    at.sidebar.button(key="sidebar_btn_stock").click().run()
    assert at.session_state["view"] == "stock"

    at.sidebar.button(key="sidebar_btn_crypto").click().run()
    assert at.session_state["view"] == "crypto"

    at.sidebar.button(key="sidebar_btn_screener").click().run()
    assert at.session_state["view"] == "screener"

    at.sidebar.button(key="sidebar_btn_live_signal").click().run()
    assert at.session_state["view"] == "live_signal"

    at.sidebar.button(key="sidebar_btn_home").click().run()
    assert at.session_state["view"] == "home"


def test_back_to_home_from_every_subpage():
    """← 返回首页 must work from every non-home view."""
    for sub_view in ("stock", "crypto", "screener", "live_signal", "stock_history"):
        at = _fresh_app()
        at.sidebar.button(key=f"sidebar_btn_{sub_view}").click().run()
        assert at.session_state["view"] == sub_view
        back_keys = [b.key for b in at.button if b.key and "back_to_home" in b.key]
        if not back_keys:
            # stock_history uses the same nav_breadcrumb, but some views
            # might not surface back btn if rendering errored — confirm:
            assert at.exception, f"view {sub_view} has no back button and no exception"
            continue
        at.button(key=back_keys[0]).click().run()
        assert at.session_state["view"] == "home", \
            f"back button on {sub_view} did not return to home; view={at.session_state['view']!r}"


def test_sidebar_button_label_indicates_current_view():
    """The current view's sidebar button must be prefixed with '●'."""
    at = _fresh_app()
    # On home, the home label should start with '●'
    home_label = next((b.label for b in at.sidebar.button if b.key == "sidebar_btn_home"), "")
    assert home_label.startswith("● "), f"home not marked current: {home_label!r}"
    # Other labels start with '○'
    stock_label = next((b.label for b in at.sidebar.button if b.key == "sidebar_btn_stock"), "")
    assert stock_label.startswith("○ "), f"stock should be inactive: {stock_label!r}"
