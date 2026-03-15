import pandas as pd

from taa_futu.config import Settings
from taa_futu.strategy_experiment import build_strategy_ledger


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
        "自重置收益 / PnL Since Reset",
        "当前浮盈 / Unrealized",
        "交易成本 / Fees",
        "成交笔数 / Trades",
        "当前目标 / Targets",
    ]
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
