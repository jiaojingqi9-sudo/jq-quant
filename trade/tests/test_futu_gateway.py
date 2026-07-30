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


def test_us_overnight_status_failure_is_transient() -> None:
    assert FutuPaperTrader.is_transient_error("subscribe_realtime failed: 拉取美股夜盘状态失败。")


def test_call_with_retry_scrubs_sensitive_error_values() -> None:
    trader = FutuPaperTrader.__new__(FutuPaperTrader)
    secret = "0123456789abcdef0123456789abcdef"
    trader.settings = SimpleNamespace(
        futu_api_retry_attempts=1,
        futu_api_retry_backoff_seconds=0.0,
        futu_unlock_trade_password_md5=secret,
        futu_acc_id=123456,
    )
    trader._futu = SimpleNamespace(RET_OK=0)
    trader._reconnect_contexts = lambda: None

    with pytest.raises(FutuTradeError) as excinfo:
        trader._call_with_retry("bad_call", lambda: (1, f"acc=123456 pwd={secret}"))

    message = str(excinfo.value)
    assert secret not in message
    assert "123456" not in message
    assert "***md5***" in message or "***hash***" in message
    assert "***acc_id***" in message


def test_subscribe_push_lob_registers_handler_and_callback() -> None:
    class _FakeOrderBookHandlerBase:
        def on_recv_rsp(self, rsp_pb):
            return 0, rsp_pb

    class _FakeQuoteContext:
        def __init__(self) -> None:
            self.handler = None
            self.subscribe_kwargs = {}

        def set_handler(self, handler):
            self.handler = handler
            return 0

        def subscribe(self, code_list, subtype_list, **kwargs):
            self.subscribe_kwargs = {
                "code_list": code_list,
                "subtype_list": subtype_list,
                **kwargs,
            }
            return (0, None)

    quote_ctx = _FakeQuoteContext()
    trader = FutuPaperTrader.__new__(FutuPaperTrader)
    trader.settings = SimpleNamespace(futu_api_retry_attempts=1, futu_api_retry_backoff_seconds=0.0)
    trader._futu = SimpleNamespace(
        RET_OK=0,
        SubType=SimpleNamespace(ORDER_BOOK="ORDER_BOOK"),
        Session=SimpleNamespace(RTH="RTH"),
        OrderBookHandlerBase=_FakeOrderBookHandlerBase,
    )
    trader.quote_ctx = quote_ctx
    trader.trade_ctx = None
    trader._reconnect_contexts = lambda: None

    pushed: list[tuple[str, dict]] = []
    trader.subscribe_push_lob(["US.SPY"], lambda code, book: pushed.append((code, book)))
    quote_ctx.handler.on_recv_rsp({"code": "US.SPY", "Bid": [(100, 1, 1, {})], "Ask": [(101, 1, 1, {})]})

    assert quote_ctx.subscribe_kwargs["code_list"] == ["US.SPY"]
    assert quote_ctx.subscribe_kwargs["subtype_list"] == ["ORDER_BOOK"]
    assert quote_ctx.subscribe_kwargs["subscribe_push"] is True
    assert pushed[0][0] == "US.SPY"


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


def test_plan_rebalance_caps_order_notional_without_blocking_exits() -> None:
    trader = FutuPaperTrader.__new__(FutuPaperTrader)
    trader.settings = SimpleNamespace(
        futu_price_buffer_bps=0,
        auto_trader_min_order_value_usd=0.0,
        auto_trader_max_order_value_usd=550.0,
        trade_costs_enabled=False,
    )

    trader.resolve_trade_account = lambda: 1
    trader.get_account_info = lambda _acc_id: pd.Series({"total_assets": 2_000.0, "cash": 2_000.0})
    trader.get_positions = lambda _acc_id: pd.DataFrame(
        [
            {"code": "US.OLD", "qty": 20, "can_sell_qty": 20, "market_val": 2_000.0},
        ]
    )
    trader.get_snapshots = lambda _symbols: pd.DataFrame(
        [
            {"code": "US.OLD", "last_price": 100.0, "bid_price": 100.0, "ask_price": 100.0, "lot_size": 1, "price_spread": 0.01},
            {"code": "US.NEW", "last_price": 100.0, "bid_price": 100.0, "ask_price": 100.0, "lot_size": 1, "price_spread": 0.01},
        ]
    ).set_index("code")

    _account, planned = trader.plan_rebalance({"US.NEW": 1.0})

    assert [order.side for order in planned] == ["SELL", "BUY"]
    assert planned[0].code == "US.OLD"
    assert planned[0].quantity == 20
    assert planned[1].code == "US.NEW"
    assert planned[1].quantity == 5
