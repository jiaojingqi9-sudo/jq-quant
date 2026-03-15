from datetime import date

import pandas as pd

from taa_futu.backtest import run_backtest
from taa_futu.market_data import futu_code_to_yfinance
from taa_futu.strategy import latest_completed_signal


def test_futu_symbol_conversion() -> None:
    assert futu_code_to_yfinance("US.SPY") == "SPY"
    assert futu_code_to_yfinance("HK.00700") == "0700.HK"
    assert futu_code_to_yfinance("SH.000300") == "000300.SS"


def test_latest_completed_signal_uses_previous_completed_month() -> None:
    index = pd.to_datetime(
        [
            "2024-01-31",
            "2024-02-29",
            "2024-03-28",
            "2024-04-30",
            "2024-05-31",
            "2024-06-28",
        ]
    )
    prices = pd.DataFrame(
        {
            "US.SPY": [100, 102, 104, 106, 108, 110],
            "US.EFA": [100, 98, 96, 94, 92, 90],
        },
        index=index,
    )

    snapshot = latest_completed_signal(
        prices,
        lookback_months=3,
        reference_date=date(2024, 6, 15),
    )

    assert snapshot.signal_month == pd.Timestamp("2024-05-31")
    assert snapshot.weights == {"US.SPY": 1.0}


def test_backtest_keeps_positive_equity_curve() -> None:
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
            "US.SPY": [100, 101, 102, 103, 104, 105, 106],
            "US.EFA": [100, 99, 98, 97, 96, 95, 94],
        },
        index=index,
    )

    result = run_backtest(prices, lookback_months=3, benchmark_symbol="US.SPY")
    assert result.equity_curve.iloc[-1] > 0
    assert result.portfolio_value_curve.iloc[-1] > 0
    assert result.summary["final_portfolio_value"] > 0


def test_backtest_emits_rebalance_log_entries() -> None:
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

    result = run_backtest(prices, lookback_months=3, benchmark_symbol="US.SPY", initial_capital=500_000)
    assert not result.rebalance_log.empty
    assert set(result.rebalance_log["side"]) <= {"BUY", "SELL"}
    assert (result.rebalance_log["portfolio_value"] > 0).all()
