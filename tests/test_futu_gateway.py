from types import SimpleNamespace

import pandas as pd
import pytest

from taa_futu.futu_gateway import FutuPaperTrader, FutuTradeError, FutuTransientError


def test_position_totals_merge_duplicate_rows() -> None:
    positions = pd.DataFrame(
        [
            {"code": "US.SPY", "qty": 514, "can_sell_qty": 514},
            {"code": "US.SPY", "qty": 0, "can_sell_qty": 0},
            {"code": "US.QQQ", "qty": 10, "can_sell_qty": 8},
        ]
    )

    totals = FutuPaperTrader._position_totals(positions)

    assert float(totals.loc["US.SPY", "qty"]) == 514
    assert float(totals.loc["US.SPY", "can_sell_qty"]) == 514
    assert float(totals.loc["US.QQQ", "qty"]) == 10
    assert float(totals.loc["US.QQQ", "can_sell_qty"]) == 8


def test_real_submit_guard_requires_explicit_enable() -> None:
    trader = FutuPaperTrader.__new__(FutuPaperTrader)
    trader.settings = SimpleNamespace(futu_trd_env="REAL", futu_enable_real_trading=False)

    with pytest.raises(FutuTradeError):
        trader._ensure_real_trading_enabled()


def test_call_with_retry_recovers_from_transient_error() -> None:
    trader = FutuPaperTrader.__new__(FutuPaperTrader)
    trader.settings = SimpleNamespace(futu_api_retry_attempts=3, futu_api_retry_backoff_seconds=0.0)
    trader._futu = SimpleNamespace(RET_OK=0)
    trader._reconnect_contexts = lambda: None
    calls = {"count": 0}

    def flaky_call():
        calls["count"] += 1
        if calls["count"] == 1:
            return (1, "PacketErr.Timeout")
        return (0, "ok")

    result = trader._call_with_retry("flaky_call", flaky_call)

    assert result == (0, "ok")
    assert calls["count"] == 2


def test_call_with_retry_raises_transient_after_retries() -> None:
    trader = FutuPaperTrader.__new__(FutuPaperTrader)
    trader.settings = SimpleNamespace(futu_api_retry_attempts=2, futu_api_retry_backoff_seconds=0.0)
    trader._futu = SimpleNamespace(RET_OK=0)
    trader._reconnect_contexts = lambda: None

    with pytest.raises(FutuTransientError):
        trader._call_with_retry("always_timeout", lambda: (1, "PacketErr.Timeout"))


def test_plan_rebalance_sells_first_and_reserves_transaction_costs() -> None:
    trader = FutuPaperTrader.__new__(FutuPaperTrader)
    trader.settings = SimpleNamespace(
        futu_price_buffer_bps=0,
        trade_costs_enabled=True,
        trade_cost_profile="test_per_share_only",
        trade_cost_commission_per_share=0.1,
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

    trader.resolve_trade_account = lambda: 1
    trader.get_account_info = lambda _acc_id: pd.Series({"total_assets": 100.0, "cash": 0.0})
    trader.get_positions = lambda _acc_id: pd.DataFrame(
        [
            {"code": "US.OLD", "qty": 10, "can_sell_qty": 10, "market_val": 100.0},
        ]
    )
    trader.get_snapshots = lambda _symbols: pd.DataFrame(
        [
            {"code": "US.OLD", "last_price": 10.0, "bid_price": 10.0, "ask_price": 10.0, "lot_size": 1, "price_spread": 0.01},
            {"code": "US.NEW", "last_price": 10.0, "bid_price": 10.0, "ask_price": 10.0, "lot_size": 1, "price_spread": 0.01},
        ]
    ).set_index("code")

    _account, planned = trader.plan_rebalance({"US.NEW": 1.0})

    assert [order.side for order in planned] == ["SELL", "BUY"]
    assert planned[0].code == "US.OLD"
    assert planned[0].quantity == 10
    assert planned[1].code == "US.NEW"
    assert planned[1].quantity == 9
