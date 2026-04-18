from datetime import UTC, datetime

import pandas as pd

from taa_futu.auto_trader import (
    _is_transient_runtime_error,
    _market_window_state,
    _order_signature,
    validate_auto_trader_mode,
)
from taa_futu.config import Settings
from taa_futu.futu_gateway import PlannedOrder


def _settings() -> Settings:
    return Settings(
        symbols=("US.SPY",),
        benchmark="US.SPY",
        start_date="2020-01-01",
        lookback_months=10,
        signal_timezone="America/New_York",
        fusion_universe=("US.SPY", "US.QQQ"),
        fusion_benchmark="US.SPY",
        fusion_lookback_bars=60,
        fusion_opening_range_minutes=15,
        fusion_top_k=3,
        fusion_entry_score=0.35,
        fusion_exit_score=0.20,
        fusion_max_position_weight=0.35,
        fusion_max_gross_exposure=0.90,
        fusion_min_rel_volume=1.10,
        fusion_max_spread_bps=15.0,
        fusion_order_book_depth=3,
        fusion_tick_window=50,
        ofim_universe=("US.AAPL",),
        ofim_benchmark="US.QQQ",
        ofim_lookback_bars=60,
        ofim_depth_tiers=((1, 5), (6, 20), (21, 60)),
        ofim_entry_threshold=0.20,
        ofim_exit_threshold=0.05,
        ofim_max_score=0.60,
        ofim_min_vol_acceleration=1.20,
        ofim_max_spread_bps=15.0,
        ofim_tick_window=100,
        ofim_order_book_depth=60,
        ofim_max_position_weight=0.15,
        ofim_max_gross_exposure=0.80,
        ofim_max_positions=5,
        stack_ofim_weight=0.0,
        futu_host="127.0.0.1",
        futu_port=11111,
        futu_trd_market="US",
        futu_trd_env="SIMULATE",
        futu_acc_id=None,
        futu_enable_real_trading=False,
        futu_allow_auto_real=False,
        futu_unlock_trade_password_md5=None,
        futu_price_buffer_bps=10,
        futu_fill_outside_rth=False,
        futu_api_retry_attempts=4,
        futu_api_retry_backoff_seconds=0.0,
        auto_trader_poll_seconds=60,
        auto_trader_market_timezone="America/New_York",
        auto_trader_start_time="09:45",
        auto_trader_end_time="15:55",
        auto_trader_order_cooldown_seconds=300,
    )


def _real_settings() -> Settings:
    settings = _settings()
    return Settings(
        **{
            **settings.__dict__,
            "futu_trd_env": "REAL",
        }
    )


def test_market_window_detects_rth() -> None:
    market_open, detail = _market_window_state(datetime(2026, 3, 10, 15, 0, tzinfo=UTC), _settings())
    assert market_open is True
    assert "inside_window" in detail


def test_order_signature_is_stable() -> None:
    orders = [
        PlannedOrder("US.SPY", "BUY", 100, 600.0, 599.0, 0, 100, 0.5),
        PlannedOrder("US.QQQ", "SELL", 50, 500.0, 501.0, 50, 0, 0.0),
    ]
    reverse_orders = list(reversed(orders))
    assert _order_signature(orders) == _order_signature(reverse_orders)


def test_validate_auto_trader_mode_blocks_real_submit_without_opt_in() -> None:
    try:
        validate_auto_trader_mode(_real_settings(), submit=True)
    except SystemExit as exc:
        assert "FUTU_ENABLE_REAL_TRADING" in str(exc)
    else:  # pragma: no cover - defensive branch
        raise AssertionError("REAL submit should be blocked without explicit opt-in")


def test_transient_runtime_error_detects_timeout_markers() -> None:
    assert _is_transient_runtime_error("PacketErr.Timeout")
    assert _is_transient_runtime_error("place_order failed after 4 attempts: timed out")
    assert _is_transient_runtime_error("position_list_query failed: 此数据暂时还未准备好")
    assert not _is_transient_runtime_error("Configured FUTU_ACC_ID=1 not found.")
