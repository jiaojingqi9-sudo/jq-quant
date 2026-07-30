from __future__ import annotations

from decimal import Decimal
import errno
import json
import math
import os
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from taa_futu import crypto_perp
from taa_futu.crypto_perp import (
    CryptoPerpEngine,
    CryptoPerpError,
    CryptoPerpOrder,
    CryptoPerpPaperState,
    CryptoPerpPlan,
    CryptoPerpSettings,
    crypto_perp_guarded_idle_poll_seconds,
    explain_crypto_perp_status,
    load_crypto_perp_settings,
)


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = tmp_path / "crypto_perp"
    monkeypatch.setattr(crypto_perp, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(crypto_perp, "STATUS_FILE", runtime / "status.json")
    monkeypatch.setattr(crypto_perp, "STATE_FILE", runtime / "paper_state.json")
    monkeypatch.setattr(crypto_perp, "TESTNET_STATE_FILE", runtime / "testnet_local_state.json")
    monkeypatch.setattr(crypto_perp, "ORDERS_FILE", runtime / "orders.jsonl")
    monkeypatch.setattr(crypto_perp, "FEATURES_FILE", runtime / "features.jsonl")
    monkeypatch.setattr(crypto_perp, "EVENTS_FILE", runtime / "events.jsonl")
    monkeypatch.setattr(crypto_perp, "AUTO_PID_FILE", runtime / "auto.pid")
    monkeypatch.setattr(crypto_perp, "AUTO_LOCK_FILE", runtime / "auto.lock")


def _settings(**overrides: Any) -> CryptoPerpSettings:
    values = {
        "mode": "paper",
        "base_url": crypto_perp.FUTURES_MAINNET_BASE_URL,
        "market_data_base_url": crypto_perp.FUTURES_MAINNET_BASE_URL,
        "api_key": None,
        "api_secret": None,
        "symbols": ("BTCUSDT",),
        "benchmark": "BTCUSDT",
        "quote_asset": "USDT",
        "initial_cash": 10000.0,
        "active_capital": 0.0,
        "active_capital_pct": 1.0,
        "lookback_bars": 60,
        "depth_limit": 100,
        "trade_limit": 100,
        "entry_threshold": 0.10,
        "exit_threshold": 0.05,
        "max_score": 0.60,
        "min_vol_acceleration": 0.0,
        "max_spread_bps": 50.0,
        "max_abs_position_weight": 0.10,
        "max_gross_exposure": 0.10,
        "max_positions": 1,
        "min_order_notional": 20.0,
        "max_order_notional": 5000.0,
        "rebalance_threshold": 0.01,
        "min_trade_interval_seconds": 0,
        "leverage": 1,
        "margin_type": "ISOLATED",
        "fee_rate": 0.0004,
        "maker_fee_rate": 0.0002,
        "slippage_bps": 0.0,
        "order_style": "market",
        "maker_order_ttl_seconds": 180,
        "maker_price_offset_bps": 0.0,
        "recv_window_ms": 5000,
        "testnet_validate_only": False,
        "min_edge_cost_ratio": 1.0,
        "signal_confirm_cycles": 1,
        "hawkes_weight": 0.10,
        "min_hawkes_imbalance": 0.08,
        "cross_asset_ofi_weight": 0.15,
    }
    values.update(overrides)
    return CryptoPerpSettings(**values)


def _bars_down() -> pd.DataFrame:
    rows = []
    for idx in range(60):
        close = 120.0 - idx * 0.25
        rows.append({"time_key": f"2026-01-01 00:{idx:02d}:00", "open": close + 0.1, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 100 + idx})
    return pd.DataFrame(rows)


def _book_bearish(mid: float = 100.0) -> dict[str, list[list[float]]]:
    return {
        "Bid": [[mid - 0.1 - idx * 0.01, 1.0] for idx in range(100)],
        "Ask": [[mid + 0.1 + idx * 0.01, 10.0] for idx in range(100)],
    }


def _ticks_sell() -> pd.DataFrame:
    return pd.DataFrame([{"price": 100.0, "volume": 1.0, "ticker_direction": "SELL"} for _ in range(100)])


def _bars_flat() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"time_key": f"2026-01-01 00:{idx:02d}:00", "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 100.0}
            for idx in range(60)
        ]
    )


def _book_balanced(mid: float = 100.0) -> dict[str, list[list[float]]]:
    return {
        "Bid": [[mid - 0.1 - idx * 0.01, 5.0] for idx in range(100)],
        "Ask": [[mid + 0.1 + idx * 0.01, 5.0] for idx in range(100)],
    }


def _ticks_balanced() -> pd.DataFrame:
    rows = []
    for idx in range(100):
        rows.append({"price": 100.0, "volume": 1.0, "ticker_direction": "BUY" if idx % 2 == 0 else "SELL"})
    return pd.DataFrame(rows)


class FakeFuturesClient:
    def __init__(self, *, price: float = 100.0) -> None:
        self.price = price
        self.orders: list[dict[str, Any]] = []
        self.margin_changes: list[tuple[str, str]] = []
        self.leverage_changes: list[tuple[str, int]] = []

    def ping(self) -> bool:
        return True

    def server_time(self) -> int:
        return 1

    def exchange_symbols(self) -> set[str]:
        return {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"}

    def klines(self, symbol: str, *, interval: str = "1m", limit: int = 60) -> pd.DataFrame:
        return _bars_down().tail(limit)

    def depth(self, symbol: str, *, limit: int = 100) -> dict[str, list[list[float]]]:
        book = _book_bearish(self.price)
        return {"Bid": book["Bid"][:limit], "Ask": book["Ask"][:limit]}

    def recent_trades(self, symbol: str, *, limit: int = 100) -> pd.DataFrame:
        return _ticks_sell().tail(limit)

    def book_ticker(self, symbol: str) -> pd.Series:
        return pd.Series({"last_price": self.price, "bid_price": self.price - 0.1, "ask_price": self.price + 0.1})

    def premium_index(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "markPrice": str(self.price), "indexPrice": str(self.price), "lastFundingRate": "0.0001"}

    def normalize_market_quantity(self, symbol: str, quantity: float, price: float) -> tuple[Decimal, str, str | None]:
        qty = Decimal(str(quantity)).quantize(Decimal("0.000001"))
        return qty, crypto_perp._decimal_to_api_text(qty), None

    def commission_rate(self, symbol: str) -> dict[str, str]:
        return {"symbol": symbol, "takerCommissionRate": "0.0004", "makerCommissionRate": "0.0002"}

    def account(self) -> dict[str, Any]:
        return {"totalWalletBalance": "10000", "totalMarginBalance": "10000", "totalUnrealizedProfit": "0", "availableBalance": "10000", "positions": []}

    def position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return []

    def income_history(self, *, income_type: str, start_time_ms: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return []

    def change_margin_type(self, symbol: str, margin_type: str) -> dict[str, Any]:
        self.margin_changes.append((symbol, margin_type))
        return {"code": 200}

    def change_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        self.leverage_changes.append((symbol, leverage))
        return {"symbol": symbol, "leverage": leverage}

    def market_order(self, symbol: str, side: str, *, quantity: Any, reduce_only: bool = False, validate_only: bool = False) -> dict[str, Any]:
        payload = {"symbol": symbol, "side": side, "quantity": quantity, "reduce_only": reduce_only, "validate_only": validate_only}
        self.orders.append(payload)
        return {"status": "FILLED", "orderId": len(self.orders), **payload}

    def limit_order(
        self,
        symbol: str,
        side: str,
        *,
        quantity: Any,
        price: Any,
        reduce_only: bool = False,
        post_only: bool = True,
        validate_only: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "reduce_only": reduce_only,
            "post_only": post_only,
            "validate_only": validate_only,
        }
        self.orders.append(payload)
        return {"status": "NEW", "orderId": len(self.orders), **payload}


class FlatFuturesClient(FakeFuturesClient):
    def klines(self, symbol: str, *, interval: str = "1m", limit: int = 60) -> pd.DataFrame:
        return _bars_flat().tail(limit)

    def depth(self, symbol: str, *, limit: int = 100) -> dict[str, list[list[float]]]:
        book = _book_balanced(self.price)
        return {"Bid": book["Bid"][:limit], "Ask": book["Ask"][:limit]}

    def recent_trades(self, symbol: str, *, limit: int = 100) -> pd.DataFrame:
        return _ticks_balanced().tail(limit)

    def premium_index(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "markPrice": str(self.price), "indexPrice": str(self.price), "lastFundingRate": "0.0"}


def test_crypto_perp_auto_instance_registers_pid_and_blocks_duplicate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    runtime = tmp_path / "crypto_perp"

    with crypto_perp.crypto_perp_auto_instance():
        assert (runtime / "auto.pid").read_text(encoding="utf-8") == str(os.getpid())
        assert (runtime / "auto.lock").read_text(encoding="utf-8") == str(os.getpid())
        with pytest.raises(CryptoPerpError, match="already running"):
            with crypto_perp.crypto_perp_auto_instance():
                pass

    assert not (runtime / "auto.pid").exists()
    assert (runtime / "auto.lock").read_text(encoding="utf-8") == ""


def test_crypto_perp_auto_instance_allows_launcher_prewritten_current_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    runtime = tmp_path / "crypto_perp"
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text(str(os.getpid()), encoding="utf-8")

    with crypto_perp.crypto_perp_auto_instance():
        assert (runtime / "auto.pid").read_text(encoding="utf-8") == str(os.getpid())
        assert (runtime / "auto.lock").read_text(encoding="utf-8") == str(os.getpid())

    assert not (runtime / "auto.pid").exists()


def test_crypto_perp_pid_running_treats_eperm_as_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_eperm(_pid: int, _sig: int) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(crypto_perp.os, "kill", _raise_eperm)

    assert crypto_perp._pid_running(123) is True


def test_crypto_perp_pid_running_treats_ps_denial_as_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto_perp.os, "kill", lambda _pid, _sig: None)

    class _Denied:
        returncode = 126
        stdout = ""

    monkeypatch.setattr(crypto_perp.subprocess, "run", lambda *_args, **_kwargs: _Denied())

    assert crypto_perp._pid_running(123) is True


def test_crypto_perp_guarded_idle_poll_backs_off_loss_guard_zero_order_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CRYPTO_PERP_LOSS_GUARD_IDLE_POLL_SECONDS", raising=False)
    payload = {
        "reason": "perp_loss_guard_fees_trade_count",
        "target_weights": {},
        "planned_orders": [],
        "submitted_orders": [],
        "pending_order_updates": [],
    }

    assert crypto_perp_guarded_idle_poll_seconds(payload, 120) == 300


def test_crypto_perp_guarded_idle_poll_keeps_base_interval_when_work_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRYPTO_PERP_LOSS_GUARD_IDLE_POLL_SECONDS", "900")
    payload = {
        "reason": "perp_loss_guard_fees_trade_count",
        "target_weights": {},
        "planned_orders": [{"symbol": "BTCUSDT"}],
        "submitted_orders": [],
        "pending_order_updates": [],
    }

    assert crypto_perp_guarded_idle_poll_seconds(payload, 120) == 120


def test_load_crypto_perp_settings_defaults_to_safe_local_paper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("CRYPTO_PERP_"):
            monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    settings = load_crypto_perp_settings(env_file)

    assert settings.mode == "paper"
    assert settings.base_url == crypto_perp.FUTURES_MAINNET_BASE_URL
    assert settings.market_data_base_url == crypto_perp.FUTURES_MAINNET_BASE_URL
    assert settings.leverage == 1
    assert settings.margin_type == "ISOLATED"
    assert settings.signed_account_enabled is False
    assert settings.active_capital_pct == pytest.approx(0.15)
    assert settings.entry_threshold == pytest.approx(0.32)
    assert settings.exit_threshold == pytest.approx(0.12)
    assert settings.exit_confirm_cycles == 3
    assert settings.signal_confirm_cycles == 3
    assert settings.require_edge_over_cost is True
    assert settings.edge_bps_per_score == pytest.approx(60.0)
    assert settings.cost_buffer_bps == pytest.approx(6.0)
    assert settings.min_edge_cost_ratio == pytest.approx(2.0)
    assert settings.hawkes_weight == pytest.approx(0.10)
    assert settings.min_hawkes_imbalance == pytest.approx(0.08)
    assert settings.cross_asset_ofi_weight == pytest.approx(0.15)
    assert settings.order_style == "maker_limit"
    assert settings.maker_fee_rate == pytest.approx(0.0002)
    assert settings.maker_order_ttl_seconds == 180
    assert settings.max_gross_exposure == pytest.approx(0.12)
    assert settings.max_positions == 1
    assert settings.max_order_notional == pytest.approx(250.0)
    assert settings.min_trade_interval_seconds == 600
    assert settings.loss_guard_max_loss == pytest.approx(50.0)
    assert settings.loss_guard_max_fees == pytest.approx(crypto_perp.DEFAULT_PERP_LOSS_GUARD_MAX_FEES)
    assert settings.loss_guard_max_trades == crypto_perp.DEFAULT_PERP_LOSS_GUARD_MAX_TRADES
    assert settings.loss_guard_symbol_max_loss == pytest.approx(15.0)
    assert settings.loss_guard_symbol_max_fees == pytest.approx(crypto_perp.DEFAULT_PERP_SYMBOL_LOSS_GUARD_MAX_FEES)
    assert settings.loss_guard_symbol_max_trades == crypto_perp.DEFAULT_PERP_SYMBOL_LOSS_GUARD_MAX_TRADES


def test_load_crypto_perp_settings_clamps_min_edge_cost_ratio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("CRYPTO_PERP_"):
            monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("CRYPTO_PERP_MIN_EDGE_COST_RATIO=0.25\n", encoding="utf-8")

    settings = load_crypto_perp_settings(env_file)

    assert settings.min_edge_cost_ratio == pytest.approx(1.0)


def test_generate_plan_can_emit_short_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(), client=client, market_client=client)

    plan = engine.generate_plan(CryptoPerpPaperState.fresh(engine.settings))

    assert plan.target_weights["BTCUSDT"] < 0
    assert plan.features[0].signal == "short"
    assert plan.features[0].hawkes_imbalance < 0
    assert plan.gross_exposure > 0


def test_cross_asset_benchmark_ofi_adjusts_altcoin_feature(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(symbols=("ETHUSDT",), benchmark="BTCUSDT", cross_asset_ofi_weight=0.50), client=client, market_client=client)

    plan = engine.generate_plan(CryptoPerpPaperState.fresh(engine.settings))
    eth_feature = next(feature for feature in plan.features if feature.symbol == "ETHUSDT")

    assert eth_feature.cross_asset_leader_score < 0
    assert "cross_asset_benchmark_ofi" in eth_feature.reason


def test_explain_crypto_perp_status_builds_plain_decision_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(), client=client, market_client=client)

    payload = engine.run_once(submit=False)
    explanation = explain_crypto_perp_status(payload)

    assert explanation["summary"]
    assert explanation["signals"]
    assert explanation["signals"][0]["expected_edge_bps"] >= 0
    assert "next_questions" in explanation


def test_cost_gate_blocks_new_entry_when_expected_edge_is_too_small(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(
        _settings(fee_rate=0.01, cost_buffer_bps=50.0, edge_bps_per_score=10.0),
        client=client,
        market_client=client,
    )

    plan = engine.generate_plan(CryptoPerpPaperState.fresh(engine.settings))

    assert plan.target_weights == {}
    events = (tmp_path / "crypto_perp" / "events.jsonl").read_text(encoding="utf-8")
    assert "expected_edge_below_cost" in events


def test_min_edge_cost_ratio_blocks_marginal_perp_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(
        _settings(min_edge_cost_ratio=100.0),
        client=client,
        market_client=client,
    )

    plan = engine.generate_plan(CryptoPerpPaperState.fresh(engine.settings))

    assert plan.target_weights == {}
    events = (tmp_path / "crypto_perp" / "events.jsonl").read_text(encoding="utf-8")
    assert "expected_edge_below_cost" in events
    assert "min_edge_cost_ratio" in events


def test_cost_gate_does_not_force_exit_for_same_direction_hold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(
        _settings(fee_rate=0.01, cost_buffer_bps=50.0, edge_bps_per_score=10.0),
        client=client,
        market_client=client,
    )
    state = CryptoPerpPaperState.fresh(engine.settings)
    state.positions = {"BTCUSDT": -2.0}
    state.avg_entry = {"BTCUSDT": 100.0}

    plan = engine.generate_plan(state)

    assert plan.target_weights["BTCUSDT"] < 0


def test_perp_loss_guard_blocks_new_entries_after_fee_drag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(loss_guard_max_fees=1.0), client=client, market_client=client)
    state = CryptoPerpPaperState.fresh(engine.settings)
    state.fees_paid = 1.25

    plan = engine.generate_plan(state)

    assert plan.target_weights == {}
    assert plan.gross_exposure == 0.0
    assert plan.reason == "perp_loss_guard_fees"
    assert plan.benchmark_context["action"] == "block_new_entries_reduce_only"
    events = (tmp_path / "crypto_perp" / "events.jsonl").read_text(encoding="utf-8")
    assert "perp_loss_guard_triggered" in events


def test_perp_loss_guard_still_allows_reduce_only_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(loss_guard_max_fees=1.0), client=client, market_client=client)
    state = CryptoPerpPaperState.fresh(engine.settings)
    state.fees_paid = 1.25
    state.positions = {"BTCUSDT": -2.0}
    state.avg_entry = {"BTCUSDT": 100.0}

    plan = engine.generate_plan(state)
    orders = engine.plan_orders(plan, state)

    assert plan.target_weights == {}
    assert len(orders) == 1
    assert orders[0].side == "BUY"
    assert orders[0].reduce_only is True
    assert orders[0].reason == "target_exit"


def test_perp_loss_guard_reduce_only_exit_bypasses_trade_cooldown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(
        _settings(loss_guard_max_fees=1.0, min_trade_interval_seconds=600),
        client=client,
        market_client=client,
    )
    state = CryptoPerpPaperState.fresh(engine.settings)
    state.fees_paid = 1.25
    state.positions = {"BTCUSDT": -2.0}
    state.avg_entry = {"BTCUSDT": 100.0}
    state.last_trade_ts = {"BTCUSDT": crypto_perp.time.time()}

    plan = engine.generate_plan(state)
    orders = engine.plan_orders(plan, state)

    assert plan.reason == "perp_loss_guard_fees"
    assert len(orders) == 1
    assert orders[0].side == "BUY"
    assert orders[0].reduce_only is True
    assert orders[0].reason == "target_exit"


def test_perp_symbol_loss_guard_blocks_new_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(
        _settings(
            loss_guard_max_loss=0.0,
            loss_guard_max_fees=0.0,
            loss_guard_max_trades=0,
            loss_guard_max_recent_trades=0,
            loss_guard_max_recent_flips=0,
            loss_guard_symbol_max_loss=0.0,
            loss_guard_symbol_max_fees=1.0,
            loss_guard_symbol_max_trades=0,
        ),
        client=client,
        market_client=client,
    )
    orders_file = tmp_path / "crypto_perp" / "orders.jsonl"
    orders_file.parent.mkdir(parents=True, exist_ok=True)
    orders_file.write_text(
        '{"mode":"paper","status":"filled","symbol":"BTCUSDT","fee":1.25,"response":{"paper_realized_pnl":0.0},"ts":"2026-01-01T00:00:00+00:00"}\n',
        encoding="utf-8",
    )

    plan = engine.generate_plan(CryptoPerpPaperState.fresh(engine.settings))

    assert plan.target_weights == {}
    assert plan.reason == "perp_symbol_loss_guard"
    guard = plan.benchmark_context["symbol_loss_guard"]
    assert guard["blocked_symbols"] == ["BTCUSDT"]
    assert guard["symbols"]["BTCUSDT"]["breaches"] == ["fees"]
    events = (tmp_path / "crypto_perp" / "events.jsonl").read_text(encoding="utf-8")
    assert "perp_symbol_loss_guard_fees" in events


def test_perp_symbol_loss_guard_allows_reduce_only_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(
        _settings(
            loss_guard_max_loss=0.0,
            loss_guard_max_fees=0.0,
            loss_guard_max_trades=0,
            loss_guard_max_recent_trades=0,
            loss_guard_max_recent_flips=0,
            loss_guard_symbol_max_loss=0.0,
            loss_guard_symbol_max_fees=1.0,
            loss_guard_symbol_max_trades=0,
        ),
        client=client,
        market_client=client,
    )
    orders_file = tmp_path / "crypto_perp" / "orders.jsonl"
    orders_file.parent.mkdir(parents=True, exist_ok=True)
    orders_file.write_text(
        '{"mode":"paper","status":"filled","symbol":"BTCUSDT","fee":1.25,"response":{"paper_realized_pnl":0.0},"ts":"2026-01-01T00:00:00+00:00"}\n',
        encoding="utf-8",
    )
    state = CryptoPerpPaperState.fresh(engine.settings)
    state.positions = {"BTCUSDT": -2.0}
    state.avg_entry = {"BTCUSDT": 100.0}

    plan = engine.generate_plan(state)
    orders = engine.plan_orders(plan, state)

    assert plan.reason == "perp_symbol_loss_guard"
    assert len(orders) == 1
    assert orders[0].side == "BUY"
    assert orders[0].reduce_only is True
    assert orders[0].reason == "target_exit"


def test_signal_confirmation_delays_new_entry_until_repeated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(signal_confirm_cycles=2, require_edge_over_cost=False), client=client, market_client=client)
    state = CryptoPerpPaperState.fresh(engine.settings)

    first_plan = engine.generate_plan(state)
    second_plan = engine.generate_plan(state)

    assert first_plan.target_weights == {}
    assert second_plan.target_weights["BTCUSDT"] < 0
    assert state.signal_confirm_streak["BTCUSDT"] == {"direction": -1, "count": 2}


def test_signal_confirmation_delays_sign_flip_and_preserves_position(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(signal_confirm_cycles=2, require_edge_over_cost=False), client=client, market_client=client)
    state = CryptoPerpPaperState.fresh(engine.settings)
    state.positions = {"BTCUSDT": 5.0}
    state.avg_entry = {"BTCUSDT": 100.0}

    first_plan = engine.generate_plan(state)
    second_plan = engine.generate_plan(state)

    assert first_plan.target_weights["BTCUSDT"] > 0
    assert second_plan.target_weights["BTCUSDT"] < 0


def test_exit_confirmation_delays_weak_flat_exit_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FlatFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(entry_threshold=0.24, exit_threshold=0.08, exit_confirm_cycles=2), client=client, market_client=client)
    state = CryptoPerpPaperState.fresh(engine.settings)
    state.positions = {"BTCUSDT": -2.0}
    state.avg_entry = {"BTCUSDT": 100.0}

    first_plan = engine.generate_plan(state)
    second_plan = engine.generate_plan(state)

    assert first_plan.target_weights["BTCUSDT"] == pytest.approx(-0.02)
    assert second_plan.target_weights == {}
    assert state.exit_signal_streak["BTCUSDT"] == 2


def test_plan_orders_sells_to_open_short(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(), client=client, market_client=client)
    state = CryptoPerpPaperState.fresh(engine.settings)
    plan = CryptoPerpPlan("paper", "BTCUSDT", -0.2, 0.1, {"BTCUSDT": -0.1}, [], reason="test")

    orders = engine.plan_orders(plan, state)

    assert len(orders) == 1
    assert orders[0].side == "SELL"
    assert orders[0].reduce_only is False
    assert orders[0].quantity == pytest.approx(10.0)
    assert orders[0].price < 100.0
    assert orders[0].response["estimated_slippage_bps"] > 0


def test_plan_orders_blocks_new_entry_before_projected_fee_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(loss_guard_max_fees=1.0, loss_guard_max_trades=0), client=client, market_client=client)
    state = CryptoPerpPaperState.fresh(engine.settings)
    state.fees_paid = 0.99
    plan = CryptoPerpPlan("paper", "BTCUSDT", -0.2, 0.1, {"BTCUSDT": -0.1}, [], reason="test")

    orders = engine.plan_orders(plan, state)

    assert orders == []
    events = [json.loads(line) for line in crypto_perp.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event"] == "perp_order_skipped"
        and row["reason"] == "projected_loss_guard_budget"
        and row["breaches"] == ["fees"]
        for row in events
    )


def test_plan_orders_blocks_new_entry_before_projected_trade_count_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(loss_guard_max_fees=0.0, loss_guard_max_trades=200), client=client, market_client=client)
    crypto_perp.ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_perp.ORDERS_FILE.write_text(
        "".join(
            '{"mode":"paper","status":"filled","symbol":"BTCUSDT","side":"SELL","fee":0.0,"ts":"2026-01-01T00:00:00+00:00"}\n'
            for _ in range(199)
        ),
        encoding="utf-8",
    )
    state = CryptoPerpPaperState.fresh(engine.settings)
    plan = CryptoPerpPlan("paper", "BTCUSDT", -0.2, 0.1, {"BTCUSDT": -0.1}, [], reason="test")

    orders = engine.plan_orders(plan, state)

    assert orders == []
    events = [json.loads(line) for line in crypto_perp.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event"] == "perp_order_skipped"
        and row["reason"] == "projected_loss_guard_budget"
        and row["breaches"] == ["trade_count"]
        for row in events
    )


def test_plan_orders_blocks_new_entry_before_projected_recent_trade_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(
        _settings(loss_guard_max_fees=0.0, loss_guard_max_trades=0, loss_guard_max_recent_trades=8),
        client=client,
        market_client=client,
    )
    crypto_perp.ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_perp.ORDERS_FILE.write_text(
        "".join(
            json.dumps(
                {
                    "mode": "paper",
                    "status": "filled",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "fee": 0.0,
                    "ts": crypto_perp._utc_now(),
                }
            )
            + "\n"
            for _ in range(7)
        ),
        encoding="utf-8",
    )
    state = CryptoPerpPaperState.fresh(engine.settings)
    plan = CryptoPerpPlan("paper", "BTCUSDT", -0.2, 0.1, {"BTCUSDT": -0.1}, [], reason="test")

    orders = engine.plan_orders(plan, state)

    assert orders == []
    events = [json.loads(line) for line in crypto_perp.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event"] == "perp_order_skipped"
        and row["reason"] == "projected_loss_guard_budget"
        and row["breaches"] == ["recent_trades"]
        and row["projected_recent_trade_count"] == 8
        for row in events
    )


def test_plan_orders_blocks_new_entry_before_projected_recent_flip_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(
        _settings(
            loss_guard_max_fees=0.0,
            loss_guard_max_trades=0,
            loss_guard_max_recent_trades=0,
            loss_guard_max_recent_flips=1,
        ),
        client=client,
        market_client=client,
    )
    crypto_perp.ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_perp._append_jsonl(
        crypto_perp.ORDERS_FILE,
        {
            "mode": "paper",
            "status": "filled",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "fee": 0.0,
            "ts": crypto_perp._utc_now(),
        },
    )
    state = CryptoPerpPaperState.fresh(engine.settings)
    plan = CryptoPerpPlan("paper", "BTCUSDT", -0.2, 0.1, {"BTCUSDT": -0.1}, [], reason="test")

    orders = engine.plan_orders(plan, state)

    assert orders == []
    events = [json.loads(line) for line in crypto_perp.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event"] == "perp_order_skipped"
        and row["reason"] == "projected_loss_guard_budget"
        and row["breaches"] == ["recent_flips"]
        and row["projected_recent_flip_count"] == 1
        for row in events
    )


def test_maker_limit_paper_posts_then_fills_on_later_cross(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(order_style="maker_limit"), client=client, market_client=client)
    state = CryptoPerpPaperState.fresh(engine.settings)
    plan = CryptoPerpPlan("paper", "BTCUSDT", -0.2, 0.1, {"BTCUSDT": -0.1}, [], reason="test")

    order = engine.plan_orders(plan, state)[0]
    submitted = engine.submit_orders([order], state)

    assert submitted[0].status == "posted"
    assert submitted[0].order_type == "LIMIT"
    assert submitted[0].time_in_force == "GTX"
    assert state.positions == {}
    assert len(state.pending_orders) == 1

    client.price = 101.0
    updates = engine.process_paper_pending_orders(state)

    assert updates[0].status == "filled"
    assert state.positions["BTCUSDT"] < 0
    assert state.fees_paid == pytest.approx(updates[0].notional * 0.0002)


def test_plan_orders_marks_partial_reductions_reduce_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(), client=client, market_client=client)
    state = CryptoPerpPaperState.fresh(engine.settings)
    state.positions = {"BTCUSDT": 5.0}
    state.avg_entry = {"BTCUSDT": 100.0}
    plan = CryptoPerpPlan("paper", "BTCUSDT", 0.2, 0.02, {"BTCUSDT": 0.02}, [], reason="trim")

    order = engine.plan_orders(plan, state)[0]

    assert order.side == "SELL"
    assert order.reduce_only is True
    assert order.reason == "rebalance"


def test_plan_orders_closes_before_sign_flip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(), client=client, market_client=client)
    state = CryptoPerpPaperState.fresh(engine.settings)
    state.positions = {"BTCUSDT": 5.0}
    state.avg_entry = {"BTCUSDT": 100.0}
    plan = CryptoPerpPlan("paper", "BTCUSDT", -0.2, 0.1, {"BTCUSDT": -0.1}, [], reason="test")

    orders = engine.plan_orders(plan, state)

    assert len(orders) == 1
    assert orders[0].side == "SELL"
    assert orders[0].reduce_only is True
    assert orders[0].reason == "sign_flip_close_first"
    assert orders[0].quantity == pytest.approx(5.0)


def test_paper_short_realizes_profit_when_covered_lower(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(), client=client, market_client=client)
    state = CryptoPerpPaperState.fresh(engine.settings)

    open_plan = CryptoPerpPlan("paper", "BTCUSDT", -0.2, 0.1, {"BTCUSDT": -0.1}, [], reason="test")
    open_order = engine.plan_orders(open_plan, state)[0]
    engine.submit_orders([open_order], state)
    client.price = 90.0
    close_plan = CryptoPerpPlan("paper", "BTCUSDT", 0.0, 0.0, {"BTCUSDT": 0.0}, [], reason="exit")
    close_order = engine.plan_orders(close_plan, state)[0]
    engine.submit_orders([close_order], state)

    assert state.positions.get("BTCUSDT", 0.0) == pytest.approx(0.0)
    assert state.realized_pnl == pytest.approx(97.55)
    assert state.fees_paid == pytest.approx(0.75982)
    assert state.cash == pytest.approx(10096.79018)


def test_paper_funding_accrual_pays_shorts_when_rate_positive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    engine = CryptoPerpEngine(_settings(funding_interval_seconds=100), client=client, market_client=client)
    state = CryptoPerpPaperState.fresh(engine.settings)
    state.positions = {"BTCUSDT": -2.0}
    state.avg_entry = {"BTCUSDT": 100.0}
    time_anchor = 1_000.0
    state.last_funding_ts = {"BTCUSDT": time_anchor}
    monkeypatch.setattr(crypto_perp.time, "time", lambda: time_anchor + 50.0)

    engine._apply_paper_funding(state)

    assert state.funding_paid == pytest.approx(-0.01)
    assert state.cash == pytest.approx(10000.01)


def test_account_snapshot_uses_mark_price_and_liquidation_estimate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=110.0)
    engine = CryptoPerpEngine(_settings(leverage=2), client=client, market_client=client)
    state = CryptoPerpPaperState.fresh(engine.settings)
    state.positions = {"BTCUSDT": -1.0}
    state.avg_entry = {"BTCUSDT": 100.0}

    account = engine.account_snapshot(state)

    position = account["position_details"][0]
    assert account["unrealized_pnl"] == pytest.approx(-10.0)
    assert position["mark_price"] == pytest.approx(110.0)
    assert position["liquidation_price"] == pytest.approx(149.25373134)
    assert position["liquidation_distance_pct"] > 0


def test_testnet_submit_uses_sell_for_short_and_reduce_only_for_cover(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFuturesClient(price=100.0)
    settings = _settings(
        mode="testnet",
        base_url=crypto_perp.FUTURES_TESTNET_BASE_URL,
        api_key="key",
        api_secret="secret",
        testnet_validate_only=False,
    )
    engine = CryptoPerpEngine(settings, client=client, market_client=client)
    short_order = CryptoPerpOrder(
        ts=crypto_perp._utc_now(),
        mode="testnet",
        symbol="BTCUSDT",
        side="SELL",
        quantity=2.0,
        price=100.0,
        notional=200.0,
        fee=0.08,
        status="planned",
        reason="rebalance",
        reduce_only=False,
        target_weight=-0.1,
        current_value=0.0,
        target_value=-200.0,
        leverage=1,
        margin_type="ISOLATED",
        response={"quantity_text": "2"},
    )
    cover_order = CryptoPerpOrder(
        **{
            **short_order.__dict__,
            "side": "BUY",
            "reduce_only": True,
            "reason": "target_exit",
            "response": {"quantity_text": "2"},
        }
    )

    submitted = engine.submit_orders([short_order, cover_order], CryptoPerpPaperState.fresh(settings))

    assert [row["side"] for row in client.orders] == ["SELL", "BUY"]
    assert [row["reduce_only"] for row in client.orders] == [False, True]
    assert submitted[0].status == "filled"
    assert client.margin_changes == [("BTCUSDT", "ISOLATED"), ("BTCUSDT", "ISOLATED")]
    assert client.leverage_changes == [("BTCUSDT", 1), ("BTCUSDT", 1)]
