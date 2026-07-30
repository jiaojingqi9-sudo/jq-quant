from taa_futu.config import Settings
from taa_futu.strategy_stack import active_stack_strategy, stack_allocations, stack_label


def _settings(**overrides) -> Settings:
    base = dict(
        symbols=("US.SPY", "US.EFA", "US.IEF", "US.VNQ", "US.DBC"),
        benchmark="US.SPY",
        start_date="2025-01-01",
        lookback_months=10,
        signal_timezone="America/New_York",
        fusion_universe=("US.QQQ", "US.AAPL"),
        fusion_benchmark="US.QQQ",
        fusion_lookback_bars=20,
        fusion_opening_range_minutes=15,
        fusion_top_k=2,
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
        ofim_crypto_universe=(),
        ofim_crypto_to_proxy=(),
        ofim_crypto_exchange="binance",
        ofim_crypto_api_key=None,
        ofim_crypto_api_secret=None,
        ofim_crypto_sandbox=False,
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
        watchdog_min_interval_seconds=240,
        watchdog_max_interval_seconds=540,
        watchdog_outside_window_min_interval_seconds=900,
        watchdog_outside_window_max_interval_seconds=1800,
        watchdog_stale_status_seconds=240,
        watchdog_restart_cooldown_seconds=120,
        stack_baseline_enabled=True,
        stack_baseline_weight=0.25,
        stack_fusion_weight=0.25,
        stack_ofim_weight=0.25,
        stack_cascade_weight=0.25,
        stack_active_strategy=None,
    )
    base.update(overrides)
    return Settings(**base)


def test_active_stack_strategy_normalizes_value() -> None:
    settings = _settings(stack_active_strategy="FUSION")

    assert active_stack_strategy(settings) == "fusion"


def test_stack_allocations_use_active_strategy_one_hot() -> None:
    settings = _settings(stack_active_strategy="ofim")

    assert stack_allocations(settings) == (0.0, 0.0, 1.0, 0.0, 0.0)


def test_stack_label_mentions_exclusive_plug_mode() -> None:
    settings = _settings(stack_active_strategy="baseline")

    assert "Plug" in stack_label(settings)
    assert "Baseline" in stack_label(settings)
