from dataclasses import replace
import json
import pandas as pd
from unittest.mock import patch

from taa_futu.config import Settings
from taa_futu.strategy_experiment import build_strategy_ledger, period_strategy_performance, write_strategy_split_state


def _settings() -> Settings:
    return Settings(
        symbols=("US.SPY", "US.EFA", "US.IEF", "US.VNQ", "US.DBC"),
        benchmark="US.SPY",
        start_date="2025-01-01",
        lookback_months=10,
        signal_timezone="America/New_York",
        fusion_universe=("US.SPY", "US.QQQ", "US.AAPL"),
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
        stack_baseline_weight=0.5,
        stack_fusion_weight=0.0,
        stack_cascade_weight=0.5,
    )


def test_build_strategy_ledger_uses_current_allowed_capital_columns() -> None:
    current_holdings = pd.DataFrame(
        [
            {"策略 / Strategy": "Baseline", "当前持仓市值 / Holdings": 100.0, "当前浮盈 / Unrealized": 5.0, "当前目标 / Targets": "SPY 50%"},
            {"策略 / Strategy": "Fusion", "当前持仓市值 / Holdings": 0.0, "当前浮盈 / Unrealized": 0.0, "当前目标 / Targets": "当前没有新目标仓位。"},
            {"策略 / Strategy": "Claude/Cascade", "当前持仓市值 / Holdings": 200.0, "当前浮盈 / Unrealized": -3.0, "当前目标 / Targets": "AGG 50%"},
        ]
    )
    period_performance = pd.DataFrame(
        [
            {"策略 / Strategy": "Baseline", "成交笔数 / Trades": 2, "交易成本 / Fees": 1.5, "区间已实现 / Realized": 10.0},
            {"策略 / Strategy": "Claude/Cascade", "成交笔数 / Trades": 1, "交易成本 / Fees": 0.5, "区间已实现 / Realized": -4.0},
        ]
    )

    ledger, overlap = build_strategy_ledger(
        settings=_settings(),
        split_state={},
        total_assets=1_000.0,
        current_holdings=current_holdings,
        period_performance=period_performance,
    )

    assert list(ledger.columns) == [
        "策略 / Strategy",
        "允许操作仓位 / Budget",
        "当前允许操作总现金 / Allowed Capital",
        "当前市值 / Holdings",
        "预算余量 / Budget Left",
        "净表现 / Net Performance",
        "当前浮盈 / Unrealized",
        "交易成本 / Fees",
        "成交笔数 / Trades",
        "账本状态 / Ledger Status",
        "当前目标 / Targets",
    ]
    assert set(ledger["策略 / Strategy"]) == {"Baseline", "Fusion", "OFIM", "Claude/Cascade"}
    assert overlap.empty


def test_build_strategy_ledger_allowed_capital_follows_current_weights() -> None:
    ledger, _ = build_strategy_ledger(
        settings=_settings(),
        split_state={},
        total_assets=1_000.0,
        current_holdings=pd.DataFrame(columns=["策略 / Strategy", "当前持仓市值 / Holdings", "当前浮盈 / Unrealized", "当前目标 / Targets"]),
        period_performance=pd.DataFrame(columns=["策略 / Strategy", "成交笔数 / Trades", "交易成本 / Fees", "区间已实现 / Realized"]),
    )

    baseline_row = ledger.loc[ledger["策略 / Strategy"] == "Baseline"].iloc[0]
    fusion_row = ledger.loc[ledger["策略 / Strategy"] == "Fusion"].iloc[0]
    cascade_row = ledger.loc[ledger["策略 / Strategy"] == "Claude/Cascade"].iloc[0]

    assert baseline_row["当前允许操作总现金 / Allowed Capital"] == 500.0
    assert fusion_row["当前允许操作总现金 / Allowed Capital"] == 0.0
    assert cascade_row["当前允许操作总现金 / Allowed Capital"] == 500.0


def test_write_strategy_split_state_uses_current_allocations(tmp_path) -> None:
    path = tmp_path / "strategy_split_state.json"

    write_strategy_split_state(
        settings=_settings(),
        total_assets=1_000.0,
        reason="test",
        path=path,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    strategies = payload["strategies"]
    assert payload["mode"] == "four_strategy_split"
    assert strategies["baseline"]["start_cash"] == 500.0
    assert strategies["fusion"]["start_cash"] == 0.0
    assert strategies["cascade"]["start_cash"] == 500.0


def test_build_strategy_ledger_blanks_performance_for_strategy_enabled_after_reset() -> None:
    settings = replace(_settings(), stack_fusion_weight=0.25, stack_baseline_weight=0.25, stack_cascade_weight=0.5)
    period_performance = pd.DataFrame(
        [
            {"策略 / Strategy": "Fusion", "成交笔数 / Trades": 10, "交易成本 / Fees": 50.0, "区间已实现 / Realized": 50_000.0},
        ]
    )

    ledger, _ = build_strategy_ledger(
        settings=settings,
        split_state={
            "reset_at": "2026-04-08T12:23:12Z",
            "strategies": {
                "baseline": {"weight": 0.25, "start_cash": 250_000.0},
                "fusion": {"weight": 0.0, "start_cash": 0.0},
                "ofim": {"weight": 0.5, "start_cash": 500_000.0},
                "cascade": {"weight": 0.25, "start_cash": 250_000.0},
            },
        },
        total_assets=1_000_000.0,
        current_holdings=pd.DataFrame(columns=["策略 / Strategy", "当前持仓市值 / Holdings", "当前浮盈 / Unrealized", "当前目标 / Targets"]),
        period_performance=period_performance,
    )

    fusion_row = ledger.loc[ledger["策略 / Strategy"] == "Fusion"].iloc[0]
    assert pd.isna(fusion_row["净表现 / Net Performance"])
    assert fusion_row["账本状态 / Ledger Status"] == "需重设起点 / Reset Required"


def test_period_strategy_performance_prefers_logged_strategy_source() -> None:
    filled_cost_view = pd.DataFrame(
        [
            {
                "code": "US.AAPL",
                "trd_side": "BUY",
                "dealt_qty": 10,
                "dealt_avg_price": 100.0,
                "updated_time": "2026-03-11 10:00:00",
                "order_id": "42",
                "fees_total": 1.0,
            }
        ]
    )
    logged_orders = pd.DataFrame(
        [
            {
                "ts": pd.Timestamp("2026-03-11T10:00:00Z"),
                "action": "submitted",
                "submit_status": "submitted",
                "submit_detail": "42",
                "strategy_source": "OFIM",
            }
        ]
    )

    with patch("taa_futu.strategy_experiment.load_order_records", return_value=logged_orders):
        summary = period_strategy_performance(
            filled_cost_view=filled_cost_view,
            settings=_settings(),
        )

    assert summary.iloc[0]["策略 / Strategy"] == "OFIM"
    assert summary.iloc[0]["成交笔数 / Trades"] == 1


def test_period_strategy_performance_uses_reset_weights_for_fallback_bucket() -> None:
    filled_cost_view = pd.DataFrame(
        [
            {
                "code": "US.AAPL",
                "trd_side": "BUY",
                "dealt_qty": 10,
                "dealt_avg_price": 100.0,
                "updated_time": "2026-03-11 10:00:00",
                "fees_total": 0.0,
            }
        ]
    )
    split_state = {
        "reset_at": "2026-03-11T00:00:00Z",
        "strategies": {
            "baseline": {"weight": 0.5},
            "fusion": {"weight": 0.0},
            "ofim": {"weight": 0.5},
            "cascade": {"weight": 0.0},
        },
    }

    with patch("taa_futu.strategy_experiment.load_order_records", return_value=pd.DataFrame()):
        summary = period_strategy_performance(
            filled_cost_view=filled_cost_view,
            settings=_settings(),
            split_state=split_state,
        )

    assert summary.iloc[0]["策略 / Strategy"] == "OFIM"
    assert summary.iloc[0]["成交笔数 / Trades"] == 1


def test_period_strategy_performance_uses_account_fifo_before_strategy_attribution() -> None:
    filled_cost_view = pd.DataFrame(
        [
            {
                "code": "US.SPY",
                "trd_side": "BUY",
                "dealt_qty": 1,
                "dealt_avg_price": 100.0,
                "updated_time": "2026-03-11 10:00:00",
                "order_id": "BUY-FUSION",
                "fees_total": 0.0,
            },
            {
                "code": "US.SPY",
                "trd_side": "SELL",
                "dealt_qty": 1,
                "dealt_avg_price": 50.0,
                "updated_time": "2026-03-11 10:05:00",
                "order_id": "SELL-OFIM",
                "fees_total": 0.0,
            },
            {
                "code": "US.SPY",
                "trd_side": "SELL",
                "dealt_qty": 1,
                "dealt_avg_price": 200.0,
                "updated_time": "2026-03-11 10:10:00",
                "order_id": "SELL-FUSION",
                "fees_total": 0.0,
            },
        ]
    )
    logged_orders = pd.DataFrame(
        [
            {
                "ts": pd.Timestamp("2026-03-11T10:00:00Z"),
                "action": "submitted",
                "submit_status": "submitted",
                "submit_detail": "BUY-FUSION",
                "strategy_source": "Fusion",
            },
            {
                "ts": pd.Timestamp("2026-03-11T10:05:00Z"),
                "action": "submitted",
                "submit_status": "submitted",
                "submit_detail": "SELL-OFIM",
                "strategy_source": "OFIM",
            },
            {
                "ts": pd.Timestamp("2026-03-11T10:10:00Z"),
                "action": "submitted",
                "submit_status": "submitted",
                "submit_detail": "SELL-FUSION",
                "strategy_source": "Fusion",
            },
        ]
    )

    with patch("taa_futu.strategy_experiment.load_order_records", return_value=logged_orders):
        summary = period_strategy_performance(
            filled_cost_view=filled_cost_view,
            settings=_settings(),
        )

    by_strategy = summary.set_index("策略 / Strategy")
    assert by_strategy.at["Fusion", "区间已实现 / Realized"] == -50.0
    assert by_strategy.at["OFIM", "区间已实现 / Realized"] == 0.0
