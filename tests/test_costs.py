from datetime import date

import pandas as pd

from taa_futu.backtest import run_backtest
from taa_futu.config import Settings
from taa_futu.costs import build_trade_cost_model, estimate_realized_from_fills
from taa_futu.research import run_account_replay, run_strategy_stack_replay


def _settings() -> Settings:
    return Settings(
        symbols=("US.SPY", "US.EFA"),
        benchmark="US.SPY",
        start_date="2025-01-01",
        lookback_months=3,
        signal_timezone="America/New_York",
        fusion_universe=("US.SPY", "US.QQQ"),
        fusion_benchmark="US.SPY",
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
        stack_ofim_weight=0.0,
        futu_host="127.0.0.1",
        futu_port=11111,
        futu_trd_market="US",
        futu_trd_env="SIMULATE",
        futu_acc_id=None,
        futu_enable_real_trading=False,
        futu_allow_auto_real=False,
        futu_unlock_trade_password_md5=None,
        futu_price_buffer_bps=0,
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
        trade_costs_enabled=True,
        trade_cost_profile="test_per_share_only",
        trade_cost_commission_per_share=0.01,
        trade_cost_commission_min=0.0,
        trade_cost_commission_max_pct=1.0,
        trade_cost_platform_per_share=0.0,
        trade_cost_platform_min=0.0,
        trade_cost_platform_max_pct=0.0,
        trade_cost_settlement_per_share=0.0,
        trade_cost_settlement_min=0.0,
        trade_cost_settlement_max_pct=0.0,
        trade_cost_sec_sell_rate=0.0,
        trade_cost_sec_sell_min=0.0,
        trade_cost_sec_zero_from="2025-05-14",
        trade_cost_taf_sell_per_share=0.0,
        trade_cost_taf_sell_min=0.0,
        trade_cost_taf_sell_max=0.0,
    )


def test_estimate_realized_from_fills_subtracts_estimated_fees() -> None:
    settings = _settings()
    order_history = pd.DataFrame(
        [
            {
                "code": "US.SPY",
                "trd_side": "BUY",
                "dealt_qty": 10,
                "dealt_avg_price": 100.0,
                "updated_time": "2026-03-10 10:30:00",
            },
            {
                "code": "US.SPY",
                "trd_side": "SELL",
                "dealt_qty": 10,
                "dealt_avg_price": 101.0,
                "updated_time": "2026-03-10 11:00:00",
            },
        ]
    )

    realized = estimate_realized_from_fills(order_history, settings)

    assert round(realized, 4) == 9.8


def test_run_account_replay_includes_estimated_fees_in_net_pnl() -> None:
    settings = _settings()
    order_history = pd.DataFrame(
        [
            {
                "code": "US.SPY",
                "trd_side": "BUY",
                "dealt_qty": 10,
                "dealt_avg_price": 100.0,
                "updated_time": "2026-03-10 10:30:00",
            },
            {
                "code": "US.SPY",
                "trd_side": "SELL",
                "dealt_qty": 10,
                "dealt_avg_price": 101.0,
                "updated_time": "2026-03-10 11:00:00",
            },
        ]
    )
    bars = pd.DataFrame(
        {
            "time_key": pd.to_datetime(["2026-03-10 10:30:00", "2026-03-10 11:00:00"]),
            "open": [100.0, 101.0],
            "high": [100.2, 101.2],
            "low": [99.9, 100.9],
            "close": [100.0, 101.0],
            "volume": [1000, 1200],
        }
    )

    result = run_account_replay(order_history, {"US.SPY": bars}, settings)

    assert round(result.summary["net_pnl"], 4) == 9.8
    assert round(result.summary["estimated_realized"], 4) == 9.8
    assert round(result.summary["total_fees"], 4) == 0.2


def test_run_backtest_reports_total_fees_when_costs_enabled() -> None:
    settings = _settings()
    index = pd.to_datetime(
        [
            "2024-01-31",
            "2024-02-29",
            "2024-03-28",
            "2024-04-30",
            "2024-05-31",
            "2024-06-28",
            "2024-07-31",
        ]
    )
    prices = pd.DataFrame(
        {
            "US.SPY": [100, 102, 104, 106, 108, 110, 112],
            "US.EFA": [100, 99, 98, 97, 96, 95, 94],
        },
        index=index,
    )

    no_cost = run_backtest(prices, lookback_months=3, benchmark_symbol="US.SPY", initial_capital=100_000)
    with_cost = run_backtest(
        prices,
        lookback_months=3,
        benchmark_symbol="US.SPY",
        initial_capital=100_000,
        trade_cost_model=build_trade_cost_model(settings),
        slippage_bps=0.0,
    )

    assert with_cost.summary["total_fees"] > 0
    assert with_cost.summary["final_portfolio_value"] < no_cost.summary["final_portfolio_value"]


def test_strategy_stack_replay_uses_cost_adjusted_baseline_curve() -> None:
    settings = Settings(
        **{
            **_settings().__dict__,
            "symbols": ("US.SPY", "US.EFA"),
            "benchmark": "US.SPY",
            "lookback_months": 3,
            "fusion_universe": ("US.QQQ",),
            "stack_baseline_enabled": True,
            "stack_baseline_weight": 1.0,
            "stack_fusion_weight": 0.0,
            "stack_ofim_weight": 0.0,
        }
    )
    index = pd.to_datetime(
        [
            "2024-01-31",
            "2024-02-29",
            "2024-03-28",
            "2024-04-30",
            "2024-05-31",
            "2024-06-28",
            "2024-07-31",
        ]
    )
    prices = pd.DataFrame(
        {
            "US.SPY": [100, 102, 104, 106, 108, 110, 112],
            "US.EFA": [100, 99, 98, 97, 96, 95, 94],
        },
        index=index,
    )

    baseline = run_backtest(
        prices,
        lookback_months=3,
        benchmark_symbol="US.SPY",
        initial_capital=100_000,
        trade_cost_model=build_trade_cost_model(settings),
        slippage_bps=0.0,
    )
    stack = run_strategy_stack_replay(
        prices,
        {},
        settings,
        initial_capital=100_000.0,
    )

    assert stack.summary["total_fees"] > 0
    assert round(stack.summary["final_value"], 4) == round(baseline.summary["final_portfolio_value"], 4)
