from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd

import taa_futu.market_logger as market_logger
from taa_futu.config import Settings
from taa_futu.research import (
    run_account_replay,
    run_cascade_replay,
    run_exact_execution_replay,
    run_strategy_stack_replay,
)


def _settings() -> Settings:
    return Settings(
        symbols=("US.SPY", "US.EFA", "US.IEF", "US.VNQ", "US.DBC"),
        benchmark="US.SPY",
        start_date="2025-01-01",
        lookback_months=10,
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
    )


def test_run_account_replay_estimates_day_trade_pnl() -> None:
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

    result = run_account_replay(order_history, {"US.SPY": bars}, _settings())

    assert result.summary["net_pnl"] == 10.0
    assert result.summary["estimated_realized"] == 10.0
    assert set(result.trade_log["strategy"]) == {"Fusion Intraday"}


def test_run_strategy_stack_replay_combines_sleeves() -> None:
    settings = Settings(
        **{
            **_settings().__dict__,
            "symbols": ("US.SPY", "US.EFA"),
            "benchmark": "US.SPY",
            "lookback_months": 2,
            "fusion_universe": ("US.QQQ",),
            "stack_baseline_enabled": True,
            "stack_baseline_weight": 0.5,
            "stack_fusion_weight": 0.4,
        }
    )

    baseline_dates = pd.date_range("2025-01-01", periods=80, freq="B")
    baseline_prices = pd.DataFrame(
        {
            "US.SPY": [100 + idx * 0.3 for idx in range(len(baseline_dates))],
            "US.EFA": [80 + idx * 0.2 for idx in range(len(baseline_dates))],
        },
        index=baseline_dates,
    )

    fusion_bars = pd.DataFrame(
        {
            "time_key": pd.date_range("2026-03-10 09:30:00", periods=45, freq="min"),
            "open": [100.0 + idx * 0.1 for idx in range(45)],
            "high": [100.2 + idx * 0.1 for idx in range(45)],
            "low": [99.8 + idx * 0.1 for idx in range(45)],
            "close": [100.1 + idx * 0.1 for idx in range(45)],
            "volume": [1000] * 44 + [2500],
        }
    )
    result = run_strategy_stack_replay(
        baseline_prices,
        {
            "US.SPY": fusion_bars,
            "US.QQQ": fusion_bars.assign(close=[110.1 + idx * 0.12 for idx in range(45)]),
        },
        settings,
        initial_capital=1_000_000.0,
    )

    assert result.summary["baseline_alloc"] == 0.5
    assert result.summary["fusion_alloc"] == 0.4
    assert result.summary["reserve_alloc"] == 0.1
    assert result.portfolio_value_curve.iloc[-1] > 0
    assert "Baseline Strategy" in set(result.trade_log["strategy"])


def test_run_cascade_replay_generates_daily_trade_log(tmp_path) -> None:
    env_file = tmp_path / "cascade.env"
    env_file.write_text(
        "\n".join(
            [
                "DM_UNIVERSE=US.SPY,US.EFA,US.AGG,US.GLD",
                "MAX_POSITION_PCT=0.30",
                "TARGET_ANNUAL_VOL=0.10",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = Settings(
        **{
            **_settings().__dict__,
            "cascade_env_file": str(env_file),
        }
    )

    daily_dates = pd.date_range("2025-01-01", periods=140, freq="B")

    def _frame(base: float, step: float) -> pd.DataFrame:
        close = [base + idx * step for idx in range(len(daily_dates))]
        return pd.DataFrame(
            {
                "timestamp": daily_dates,
                "open": close,
                "high": [value * 1.002 for value in close],
                "low": [value * 0.998 for value in close],
                "close": close,
                "volume": [1_000_000] * len(daily_dates),
            }
        )

    result = run_cascade_replay(
        {
            "US.SPY": _frame(100.0, 0.35),
            "US.EFA": _frame(80.0, 0.22),
            "US.AGG": _frame(95.0, 0.06),
            "US.GLD": _frame(70.0, 0.10),
            "US.VIX": _frame(18.0, 0.01),
        },
        settings,
        initial_capital=100_000.0,
    )

    assert result.portfolio_value_curve.iloc[-1] > 0
    assert result.summary["final_value"] > 0
    if not result.trade_log.empty:
        assert set(result.trade_log["strategy"]) == {"Claude/Cascade"}


def test_run_exact_execution_replay_matches_logged_order_ids(tmp_path) -> None:
    day_dir = tmp_path / "2026-03-10"
    day_dir.mkdir(parents=True)
    (day_dir / "orders.jsonl").write_text(
        "\n".join(
            [
                '{"ts":"2026-03-10T10:29:59+00:00","type":"orders","action":"submitted","orders":[{"code":"US.SPY","side":"BUY","quantity":10,"limit_price":100.1,"reference_price":100.0,"current_qty":0,"target_qty":10,"target_weight":0.1,"action":"submitted","submit_status":"submitted","submit_detail":"42"}]}',
                '{"ts":"2026-03-10T10:59:59+00:00","type":"orders","action":"submitted","orders":[{"code":"US.SPY","side":"SELL","quantity":10,"limit_price":101.0,"reference_price":101.0,"current_qty":10,"target_qty":0,"target_weight":0.0,"action":"submitted","submit_status":"submitted","submit_detail":"43"}]}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    order_history = pd.DataFrame(
        [
            {
                "order_id": "42",
                "code": "US.SPY",
                "trd_side": "BUY",
                "dealt_qty": 10,
                "dealt_avg_price": 100.0,
                "updated_time": "2026-03-10 10:30:00",
            },
            {
                "order_id": "43",
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

    with patch.object(market_logger, "MARKET_DATA_DIR", tmp_path):
        result = run_exact_execution_replay(
            "2026-03-10",
            "2026-03-10",
            order_history,
            {"US.SPY": bars},
            _settings(),
        )

    assert result.summary["net_pnl"] == 10.0
    assert result.summary["estimated_realized"] == 10.0
    assert result.summary["matched_filled_orders"] == 2.0
    assert set(result.trade_log["order_id"]) == {"42", "43"}
