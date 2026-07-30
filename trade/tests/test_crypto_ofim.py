from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path

import pandas as pd
import pytest

from taa_futu import crypto_ofim
from taa_futu import crypto_ofim_stream
from taa_futu.crypto_ofim import (
    BinanceSpotClient,
    CryptoOfimError,
    CryptoOfimFeature,
    CryptoOfimOrder,
    CryptoOfimEngine,
    CryptoOfimSettings,
    CryptoPaperState,
    DEFAULT_CORE_USDT_SYMBOLS,
    DEFAULT_TIGHT_USDT_SYMBOLS,
    _is_transient_network_message,
    _sanitize_binance_error,
    crypto_ofim_guarded_idle_poll_seconds,
    estimate_crypto_ofim_request_weight,
    load_crypto_ofim_settings,
    reset_crypto_ofim_testnet_ledger_epoch,
)


def _settings(**overrides) -> CryptoOfimSettings:
    base = dict(
        mode="paper",
        base_url="https://api.binance.com",
        api_key=None,
        api_secret=None,
        symbols=("BTCUSDT", "ETHUSDT"),
        hot_universe=False,
        core_universe=False,
        hot_count=20,
        excluded_symbols=(),
        benchmark="BTCUSDT",
        quote_asset="USDT",
        initial_cash=10_000.0,
        active_capital=10_000.0,
        active_capital_pct=0.40,
        lookback_bars=60,
        depth_limit=100,
        trade_limit=100,
        entry_threshold=0.05,
        exit_threshold=0.01,
        max_score=0.60,
        min_vol_acceleration=0.80,
        max_spread_bps=20.0,
        max_position_weight=0.50,
        max_gross_exposure=0.80,
        max_positions=2,
        min_order_notional=20.0,
        max_order_notional=0.0,
        max_order_book_impact_bps=0.0,
        max_order_book_take_ratio=0.25,
        rebalance_threshold=0.0,
        exit_confirm_cycles=1,
        min_trade_interval_seconds=0,
        min_flip_interval_seconds=0,
        min_reentry_after_risk_off_seconds=0,
        min_holding_seconds=0,
        max_holding_seconds=600,
        fee_rate=0.001,
        slippage_bps=5.0,
        recv_window_ms=5000,
        testnet_validate_only=False,
        use_ws_cache=False,
        use_user_stream=False,
        require_edge_over_cost=False,
    )
    base.update(overrides)
    return CryptoOfimSettings(**base)


def _patch_runtime(monkeypatch, tmp_path: Path) -> None:
    runtime = tmp_path / "crypto_ofim"
    monkeypatch.setattr(crypto_ofim, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(crypto_ofim, "STATUS_FILE", runtime / "status.json")
    monkeypatch.setattr(crypto_ofim, "STATE_FILE", runtime / "paper_state.json")
    monkeypatch.setattr(crypto_ofim, "ORDERS_FILE", runtime / "orders.jsonl")
    monkeypatch.setattr(crypto_ofim, "FEATURES_FILE", runtime / "features.jsonl")
    monkeypatch.setattr(crypto_ofim, "EVENTS_FILE", runtime / "events.jsonl")
    monkeypatch.setattr(crypto_ofim, "ATTRIBUTION_FILE", runtime / "crypto_attribution.json")
    monkeypatch.setattr(crypto_ofim, "USER_STREAM_EVENTS_FILE", runtime / "user_stream_events.jsonl")
    monkeypatch.setattr(crypto_ofim, "USER_FILLS_FILE", runtime / "user_fills.jsonl")
    monkeypatch.setattr(crypto_ofim, "LEDGER_EPOCH_FILE", runtime / "ledger_epoch.json")
    monkeypatch.setattr(crypto_ofim, "LEDGER_RESET_BACKUP_DIR", runtime / "ledger_reset_backups")
    monkeypatch.setattr(crypto_ofim_stream, "STREAM_PID_FILE", runtime / "stream.pid")
    monkeypatch.setattr(crypto_ofim_stream, "STREAM_LOG_FILE", runtime / "stream.log")
    monkeypatch.setattr(crypto_ofim_stream, "STREAM_STATUS_FILE", runtime / "stream_status.json")
    monkeypatch.setattr(crypto_ofim_stream, "STREAM_CACHE_FILE", runtime / "ws_cache.json")
    monkeypatch.setattr(crypto_ofim_stream, "STREAM_EVENTS_FILE", runtime / "ws_events.jsonl")


def test_crypto_ofim_auto_instance_registers_pid_and_blocks_duplicate(monkeypatch, tmp_path: Path) -> None:
    runtime = tmp_path / "crypto_ofim"
    monkeypatch.setattr(crypto_ofim, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(crypto_ofim, "AUTO_PID_FILE", runtime / "auto.pid")
    monkeypatch.setattr(crypto_ofim, "AUTO_LOCK_FILE", runtime / "auto.lock")
    monkeypatch.setattr(crypto_ofim, "EVENTS_FILE", runtime / "events.jsonl")

    with crypto_ofim.crypto_ofim_auto_instance():
        assert (runtime / "auto.pid").read_text(encoding="utf-8") == str(os.getpid())
        assert (runtime / "auto.lock").read_text(encoding="utf-8") == str(os.getpid())
        with pytest.raises(CryptoOfimError, match="already running"):
            with crypto_ofim.crypto_ofim_auto_instance():
                pass

    assert not (runtime / "auto.pid").exists()
    assert (runtime / "auto.lock").read_text(encoding="utf-8") == ""


def test_crypto_ofim_auto_instance_allows_launcher_prewritten_current_pid(monkeypatch, tmp_path: Path) -> None:
    runtime = tmp_path / "crypto_ofim"
    monkeypatch.setattr(crypto_ofim, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(crypto_ofim, "AUTO_PID_FILE", runtime / "auto.pid")
    monkeypatch.setattr(crypto_ofim, "AUTO_LOCK_FILE", runtime / "auto.lock")
    monkeypatch.setattr(crypto_ofim, "EVENTS_FILE", runtime / "events.jsonl")
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text(str(os.getpid()), encoding="utf-8")

    with crypto_ofim.crypto_ofim_auto_instance():
        assert (runtime / "auto.pid").read_text(encoding="utf-8") == str(os.getpid())
        assert (runtime / "auto.lock").read_text(encoding="utf-8") == str(os.getpid())

    assert not (runtime / "auto.pid").exists()


def test_crypto_ofim_auto_instance_blocks_recent_active_hidden_loop(monkeypatch, tmp_path: Path) -> None:
    runtime = tmp_path / "crypto_ofim"
    monkeypatch.setattr(crypto_ofim, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(crypto_ofim, "AUTO_PID_FILE", runtime / "auto.pid")
    monkeypatch.setattr(crypto_ofim, "AUTO_LOCK_FILE", runtime / "auto.lock")
    monkeypatch.setattr(crypto_ofim, "EVENTS_FILE", runtime / "events.jsonl")
    monkeypatch.setattr(crypto_ofim, "_pid_running", lambda pid: pid in {os.getpid(), 456})
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text(str(os.getpid()), encoding="utf-8")
    (runtime / "events.jsonl").write_text(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event_type": "cycle_started",
                "cycle_id": "1778651000000-456",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CryptoOfimError, match="already running with pid 456"):
        with crypto_ofim.crypto_ofim_auto_instance():
            pass


def test_guarded_idle_poll_backs_off_loss_guard_zero_order_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRYPTO_OFIM_LOSS_GUARD_IDLE_POLL_SECONDS", raising=False)
    payload = {
        "plan_reason": "loss_guard_loss_cash_reconciliation_estimated_fees_trade_count",
        "target_weights": {},
        "planned_orders": [],
        "submitted_orders": [],
    }

    assert crypto_ofim_guarded_idle_poll_seconds(payload, 15) == 300


def test_guarded_idle_poll_keeps_base_interval_when_work_remains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRYPTO_OFIM_LOSS_GUARD_IDLE_POLL_SECONDS", "120")
    payload = {
        "plan_reason": "loss_guard_estimated_fees",
        "target_weights": {},
        "planned_orders": [{"symbol": "BTCUSDT"}],
        "submitted_orders": [],
    }

    assert crypto_ofim_guarded_idle_poll_seconds(payload, 15) == 15


def test_guarded_idle_poll_backs_off_risk_off_zero_order_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRYPTO_OFIM_RISK_OFF_IDLE_POLL_SECONDS", raising=False)
    payload = {
        "plan_reason": "benchmark_risk_off_cooldown",
        "target_weights": {},
        "planned_orders": [],
        "submitted_orders": [],
    }

    assert crypto_ofim_guarded_idle_poll_seconds(payload, 15) == 300


def test_guarded_idle_poll_keeps_base_interval_for_risk_off_exit_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRYPTO_OFIM_RISK_OFF_IDLE_POLL_SECONDS", "600")
    payload = {
        "plan_reason": "benchmark_risk_off",
        "target_weights": {},
        "planned_orders": [{"symbol": "BTCUSDT", "side": "SELL"}],
        "submitted_orders": [],
    }

    assert crypto_ofim_guarded_idle_poll_seconds(payload, 15) == 15


def test_crypto_pid_running_treats_eperm_as_running(monkeypatch) -> None:
    def _raise_eperm(_pid: int, _sig: int) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(crypto_ofim.os, "kill", _raise_eperm)
    monkeypatch.setattr(crypto_ofim_stream.os, "kill", _raise_eperm)

    assert crypto_ofim._pid_running(123) is True
    assert crypto_ofim_stream._pid_running(123) is True


def test_crypto_pid_running_treats_zombie_as_not_running(monkeypatch) -> None:
    monkeypatch.setattr(crypto_ofim.os, "kill", lambda _pid, _sig: None)

    class _Result:
        returncode = 0
        stdout = "Z"

    monkeypatch.setattr(crypto_ofim.subprocess, "run", lambda *_args, **_kwargs: _Result())

    assert crypto_ofim._pid_running(123) is False


def test_crypto_pid_running_treats_ps_denial_as_running(monkeypatch) -> None:
    monkeypatch.setattr(crypto_ofim.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(crypto_ofim_stream.os, "kill", lambda _pid, _sig: None)

    class _Denied:
        returncode = 126
        stdout = ""

    monkeypatch.setattr(crypto_ofim.subprocess, "run", lambda *_args, **_kwargs: _Denied())
    monkeypatch.setattr(crypto_ofim_stream.subprocess, "run", lambda *_args, **_kwargs: _Denied())

    assert crypto_ofim._pid_running(123) is True
    assert crypto_ofim_stream._pid_running(123) is True


class FakeBinanceClient:
    def __init__(self) -> None:
        self.prices = {"BTCUSDT": 100.0, "ETHUSDT": 50.0}

    def ping(self) -> bool:
        return True

    def server_time(self) -> int:
        return 123

    def account(self):
        return {
            "accountType": "SPOT",
            "balances": [
                {"asset": "USDT", "free": "10000", "locked": "0"},
                {"asset": "FAKE", "free": "5", "locked": "0"},
            ],
        }

    def tickers_24h(self):
        return [
            {"symbol": "BTCUSDT", "quoteVolume": "1000000", "lastPrice": "100"},
            {"symbol": "ETHUSDT", "quoteVolume": "800000", "lastPrice": "50"},
        ]

    def book_ticker(self, symbol: str) -> pd.Series:
        price = self.prices[symbol]
        return pd.Series({"last_price": price, "bid_price": price * 0.99995, "ask_price": price * 1.00005})

    def klines(self, symbol: str, *, interval: str = "1m", limit: int = 60) -> pd.DataFrame:
        start = self.prices[symbol] * 0.97
        closes = [start + i * 0.05 for i in range(limit)]
        volume = [50.0] * (limit - 5) + [180.0] * 5
        return pd.DataFrame(
            {
                "time_key": [f"2026-01-01 00:{i:02d}:00" for i in range(limit)],
                "open": closes,
                "high": [x * 1.001 for x in closes],
                "low": [x * 0.999 for x in closes],
                "close": closes,
                "volume": volume,
            }
        )

    def depth(self, symbol: str, *, limit: int = 100):
        price = self.prices[symbol]
        bids = [[price * (1 - i * 0.0001), 100.0] for i in range(1, 61)]
        asks = [[price * (1 + i * 0.0001), 10.0] for i in range(1, 61)]
        return {"Bid": bids, "Ask": asks}

    def recent_trades(self, symbol: str, *, limit: int = 100) -> pd.DataFrame:
        price = self.prices[symbol]
        return pd.DataFrame(
            [{"price": price, "volume": 1.0, "ticker_direction": "BUY"} for _ in range(limit)],
            columns=["price", "volume", "ticker_direction"],
        )


class FakeTestnetOrderClient(FakeBinanceClient):
    def __init__(self) -> None:
        super().__init__()
        self.submitted_quantities: list[str] = []

    def normalize_market_quantity(self, symbol: str, quantity: float, price: float):
        return Decimal("12"), "12", None

    def market_order(self, symbol: str, side: str, *, quantity=None, quote_order_qty=None, validate_only: bool = False):
        self.submitted_quantities.append(str(quantity))
        return {"symbol": symbol, "side": side, "status": "FILLED"}


class FakeFilledTestnetOrderClient(FakeTestnetOrderClient):
    def market_order(self, symbol: str, side: str, *, quantity=None, quote_order_qty=None, validate_only: bool = False):
        self.submitted_quantities.append(str(quantity))
        return {
            "symbol": symbol,
            "side": side,
            "status": "FILLED",
            "executedQty": "2.00000000",
            "cummulativeQuoteQty": "90.00000000",
            "fills": [{"price": "45.00000000", "qty": "2.00000000", "commission": "0.00000000", "commissionAsset": "BTC"}],
        }


class FakeTransientNetworkClient(FakeBinanceClient):
    def book_ticker(self, symbol: str) -> pd.Series:
        raise CryptoOfimError(
            "Binance temporary network error while calling GET /api/v3/ticker/bookTicker: "
            "HTTPSConnectionPool(host='testnet.binance.vision', port=443): Read timed out."
        )


class LiquidationClient(FakeBinanceClient):
    def account(self):
        return {
            "accountType": "SPOT",
            "balances": [
                {"asset": "USDT", "free": "1000", "locked": "0"},
                {"asset": "BTC", "free": "0.5", "locked": "0"},
                {"asset": "FAKE", "free": "10", "locked": "0"},
            ],
        }

    def exchange_symbols(self) -> set[str]:
        return {"BTCUSDT", "FAKEUSDT"}

    def book_tickers(self) -> dict[str, dict[str, float]]:
        return {
            "BTCUSDT": {"last_price": 100.0, "bid_price": 99.9, "ask_price": 100.1},
            "FAKEUSDT": {"last_price": 2.0, "bid_price": 1.9, "ask_price": 2.1},
        }

    def book_ticker(self, symbol: str) -> pd.Series:
        row = self.book_tickers()[symbol]
        return pd.Series(row)

    def normalize_market_quantity(self, symbol: str, quantity: float, price: float):
        qty = Decimal(str(quantity))
        return qty, format(qty, "f"), None


def test_load_crypto_ofim_settings_defaults_to_safe_paper(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CRYPTO_OFIM_MODE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    settings = load_crypto_ofim_settings(env_file)

    assert settings.mode == "paper"
    assert settings.api_key is None
    assert "BTCUSDT" in settings.symbols
    assert settings.base_url == crypto_ofim.MAINNET_BASE_URL
    assert settings.use_ws_cache is True
    assert settings.rebalance_threshold == 0.08
    assert settings.exit_confirm_cycles == 4
    assert settings.signal_confirm_cycles == 2
    assert settings.min_trade_interval_seconds == crypto_ofim.MIN_CONSERVATIVE_TRADE_INTERVAL_SECONDS
    assert settings.min_flip_interval_seconds == 300
    assert crypto_ofim.MIN_CONSERVATIVE_ENTRY_THRESHOLD == pytest.approx(0.49)
    assert crypto_ofim.MIN_CONSERVATIVE_ORDER_NOTIONAL == pytest.approx(101.25)
    assert crypto_ofim.MIN_CONSERVATIVE_TRADE_INTERVAL_SECONDS == 600
    assert crypto_ofim.MAX_CONSERVATIVE_ACTIVE_CAPITAL_PCT == pytest.approx(0.15)
    assert crypto_ofim.MAX_CONSERVATIVE_POSITION_WEIGHT == pytest.approx(0.25)
    assert crypto_ofim.MAX_CONSERVATIVE_GROSS_EXPOSURE == pytest.approx(0.50)
    assert crypto_ofim.MAX_CONSERVATIVE_POSITIONS == 1
    assert crypto_ofim.MAX_CONSERVATIVE_SPREAD_BPS == pytest.approx(8.192)
    assert crypto_ofim.MAX_CONSERVATIVE_ORDER_NOTIONAL == pytest.approx(2500.0)
    assert crypto_ofim.MIN_RISK_OFF_REENTRY_COOLDOWN_SECONDS == 115200
    assert settings.min_reentry_after_risk_off_seconds == crypto_ofim.MIN_RISK_OFF_REENTRY_COOLDOWN_SECONDS
    assert settings.min_holding_seconds == 300
    assert settings.market_data == "mainnet"
    assert settings.market_data_base_url == crypto_ofim.MAINNET_BASE_URL
    assert settings.entry_threshold == crypto_ofim.MIN_CONSERVATIVE_ENTRY_THRESHOLD
    assert settings.min_order_notional == crypto_ofim.MIN_CONSERVATIVE_ORDER_NOTIONAL
    assert settings.active_capital_pct == crypto_ofim.MAX_CONSERVATIVE_ACTIVE_CAPITAL_PCT
    assert settings.max_position_weight == crypto_ofim.MAX_CONSERVATIVE_POSITION_WEIGHT
    assert settings.max_gross_exposure == crypto_ofim.MAX_CONSERVATIVE_GROSS_EXPOSURE
    assert settings.max_positions == crypto_ofim.MAX_CONSERVATIVE_POSITIONS
    assert settings.max_spread_bps == crypto_ofim.MAX_CONSERVATIVE_SPREAD_BPS
    assert settings.benchmark_soft_risk_score == -0.15
    assert settings.benchmark_hard_risk_score == -0.45
    assert settings.benchmark_soft_sma_band_bps == 50
    assert settings.benchmark_soft_exposure_multiplier == 0.50
    assert settings.liquidate_all_testnet_assets is False
    assert settings.loss_guard_max_loss == 500
    assert settings.loss_guard_max_estimated_fees == crypto_ofim.DEFAULT_LOSS_GUARD_MAX_ESTIMATED_FEES
    assert settings.loss_guard_max_trades == crypto_ofim.DEFAULT_LOSS_GUARD_MAX_TRADES
    assert settings.loss_guard_recent_window_seconds == 900
    assert settings.loss_guard_max_recent_trades == 12
    assert settings.loss_guard_max_recent_risk_off_exits == 3
    assert settings.loss_guard_max_recent_flips == 3
    assert settings.loss_guard_symbol_max_loss == 100
    assert settings.loss_guard_symbol_max_estimated_fees == crypto_ofim.DEFAULT_SYMBOL_LOSS_GUARD_MAX_ESTIMATED_FEES
    assert settings.loss_guard_symbol_max_trades == crypto_ofim.DEFAULT_SYMBOL_LOSS_GUARD_MAX_TRADES


def test_load_crypto_ofim_settings_applies_conservative_churn_floors(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRYPTO_OFIM_ENTRY_THRESHOLD", "0.10")
    monkeypatch.setenv("CRYPTO_OFIM_MIN_ORDER_NOTIONAL", "10")
    monkeypatch.setenv("CRYPTO_OFIM_MAX_SPREAD_BPS", "25")
    monkeypatch.setenv("CRYPTO_OFIM_ACTIVE_CAPITAL_PCT", "0.40")
    monkeypatch.setenv("CRYPTO_OFIM_MAX_POSITION_WEIGHT", "0.50")
    monkeypatch.setenv("CRYPTO_OFIM_MAX_GROSS_EXPOSURE", "1.00")
    monkeypatch.setenv("CRYPTO_OFIM_MAX_POSITIONS", "4")
    monkeypatch.setenv("CRYPTO_OFIM_MAX_ORDER_NOTIONAL", "10000")
    monkeypatch.setenv("CRYPTO_OFIM_MIN_TRADE_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("CRYPTO_OFIM_MIN_REENTRY_AFTER_RISK_OFF_SECONDS", "60")
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    settings = load_crypto_ofim_settings(env_file)

    assert settings.entry_threshold == crypto_ofim.MIN_CONSERVATIVE_ENTRY_THRESHOLD
    assert settings.min_order_notional == crypto_ofim.MIN_CONSERVATIVE_ORDER_NOTIONAL
    assert settings.max_spread_bps == crypto_ofim.MAX_CONSERVATIVE_SPREAD_BPS
    assert settings.active_capital_pct == crypto_ofim.MAX_CONSERVATIVE_ACTIVE_CAPITAL_PCT
    assert settings.max_position_weight == crypto_ofim.MAX_CONSERVATIVE_POSITION_WEIGHT
    assert settings.max_gross_exposure == crypto_ofim.MAX_CONSERVATIVE_GROSS_EXPOSURE
    assert settings.max_positions == crypto_ofim.MAX_CONSERVATIVE_POSITIONS
    assert settings.max_order_notional == crypto_ofim.MAX_CONSERVATIVE_ORDER_NOTIONAL
    assert settings.min_trade_interval_seconds == crypto_ofim.MIN_CONSERVATIVE_TRADE_INTERVAL_SECONDS
    assert settings.min_reentry_after_risk_off_seconds == crypto_ofim.MIN_RISK_OFF_REENTRY_COOLDOWN_SECONDS


def test_reset_crypto_ofim_testnet_ledger_epoch_backs_up_and_resets_state(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        mode="testnet",
        base_url=crypto_ofim.TESTNET_BASE_URL,
        initial_cash=123.0,
    )
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    crypto_ofim.RUNTIME_DIR.mkdir(parents=True)
    crypto_ofim.LEDGER_EPOCH_FILE.write_text('{"old": "epoch"}', encoding="utf-8")
    crypto_ofim.STATUS_FILE.write_text('{"old": "status"}', encoding="utf-8")
    crypto_ofim._state_file_for(settings).write_text('{"old": "state"}', encoding="utf-8")

    result = reset_crypto_ofim_testnet_ledger_epoch(
        settings,
        reason="unit_test_epoch_reset",
        engine=engine,
    )

    assert result["orders_submitted"] is False
    assert result["mode"] == "testnet"
    assert result["state_cash"] == 10000.0
    assert result["epoch"]["balances"]["USDT"] == 10000.0
    assert result["epoch"]["balances"]["FAKE"] == 5.0
    state = CryptoPaperState.load(settings)
    assert state.cash == 10000.0
    assert state.positions == {}
    assert state.avg_cost == {}
    assert state.ledger_epoch_id == result["epoch"]["epoch_id"]
    backup_dir = Path(result["backup_dir"])
    # Regression guard: the backup must land inside the patched (tmp) runtime,
    # never in the real runtime directory.
    assert backup_dir.is_relative_to(tmp_path)
    assert (backup_dir / "ledger_epoch.json").exists()
    assert (backup_dir / "status.json").exists()
    assert (backup_dir / "testnet_state.json").exists()
    payload = json.loads(crypto_ofim.STATUS_FILE.read_text(encoding="utf-8"))
    assert payload["status"] == "testnet_ledger_reset"
    assert payload["submitted_orders"] == []
    assert payload["planned_orders"] == []


def test_reset_crypto_ofim_testnet_ledger_epoch_requires_testnet(tmp_path: Path) -> None:
    settings = _settings(mode="paper")

    with pytest.raises(CryptoOfimError, match="requires CRYPTO_OFIM_MODE=testnet"):
        reset_crypto_ofim_testnet_ledger_epoch(settings, backup=False)


def test_resolve_env_file_falls_back_to_repo_root_when_cwd_env_missing(
    monkeypatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text("CRYPTO_OFIM_MODE=testnet\n", encoding="utf-8")

    assert crypto_ofim._resolve_env_file(".env", fallback_root=root) == root / ".env"


def test_loss_guard_breach_hints_external_balance_change_on_zero_trade_reconciliation(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(mode="testnet", loss_guard_max_loss=500.0)
    account = {
        "primary_net_pnl": -21_413.82,
        "trade_count": 0,
        "cash_reconciliation": {
            "ok": False,
            "unexplained_quote_delta": -21_413.82,
            "ledger_cash_delta": 0.0,
        },
    }

    reason, detail = crypto_ofim._loss_guard_breach(settings, account)

    assert "cash_reconciliation" in detail["breaches"]
    assert detail["likely_cause"] == "external_balance_change"
    assert "crypto-ofim-ledger-reset" in detail["hint"]
    assert reason.startswith("loss_guard_")


def test_loss_guard_breach_no_external_hint_when_trades_explain_loss(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(mode="testnet", loss_guard_max_loss=500.0)
    account = {
        "primary_net_pnl": -600.0,
        "trade_count": 12,
        "cash_reconciliation": {
            "ok": False,
            "unexplained_quote_delta": -600.0,
            "ledger_cash_delta": -600.0,
        },
    }

    _reason, detail = crypto_ofim._loss_guard_breach(settings, account)

    assert "likely_cause" not in detail
    assert "hint" not in detail


def test_load_crypto_ofim_settings_testnet_uses_mainnet_market_data_by_default(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "CRYPTO_OFIM_MODE=testnet",
                "CRYPTO_OFIM_API_KEY=key",
                "CRYPTO_OFIM_API_SECRET=secret",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_crypto_ofim_settings(env_file)

    assert settings.mode == "testnet"
    assert settings.base_url == crypto_ofim.TESTNET_BASE_URL
    assert settings.market_data == "mainnet"
    assert settings.market_data_base_url == crypto_ofim.MAINNET_BASE_URL


def test_load_crypto_ofim_settings_ignores_market_data_env_override(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CRYPTO_OFIM_MARKET_DATA=testnet\n", encoding="utf-8")

    settings = load_crypto_ofim_settings(env_file)

    assert settings.market_data == "mainnet"
    assert settings.market_data_base_url == crypto_ofim.MAINNET_BASE_URL


def test_load_crypto_ofim_settings_uses_official_spot_fee_default(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CRYPTO_OFIM_FEE_RATE=0.0\n", encoding="utf-8")

    settings = load_crypto_ofim_settings(env_file)

    assert settings.fee_rate == crypto_ofim.BINANCE_OFFICIAL_SPOT_PUBLIC_STANDARD_TAKER_RATE


def test_load_crypto_ofim_settings_enables_entry_edge_cost_gate_by_default(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    settings = load_crypto_ofim_settings(env_file)

    assert settings.require_edge_over_cost is True
    assert settings.edge_bps_per_score == pytest.approx(150.0)
    assert settings.cost_buffer_bps == pytest.approx(6.0)
    assert settings.min_edge_cost_ratio == pytest.approx(1.25)


def test_load_crypto_ofim_settings_clamps_min_edge_cost_ratio(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CRYPTO_OFIM_MIN_EDGE_COST_RATIO=0.25\n", encoding="utf-8")

    settings = load_crypto_ofim_settings(env_file)

    assert settings.min_edge_cost_ratio == pytest.approx(1.0)


def test_entry_edge_cost_gate_requires_safety_margin() -> None:
    feature = CryptoOfimFeature(
        symbol="BTCUSDT",
        last_price=100.0,
        ofi_tier_1=0.0,
        ofi_tier_2=0.0,
        ofi_tier_3=0.0,
        vol_accel=1.0,
        mom_3m=0.0,
        mom_10m=0.0,
        mom_30m=0.0,
        vwap_dev=0.0,
        tick_agg=0.5,
        spread_bps=0.0,
        score=0.3,
        conviction=0.5,
        eligible=True,
        reason="ok",
    )
    settings = _settings(
        require_edge_over_cost=True,
        edge_bps_per_score=120.0,
        cost_buffer_bps=6.0,
        min_edge_cost_ratio=1.25,
    )

    passed, context = crypto_ofim._passes_entry_edge_cost_gate(settings, feature)

    assert passed is False
    assert context["estimated_edge_bps"] == pytest.approx(context["estimated_round_trip_cost_bps"])
    assert context["required_edge_bps"] == pytest.approx(45.0)


def test_entry_edge_cost_gate_allows_clear_cost_margin() -> None:
    feature = CryptoOfimFeature(
        symbol="BTCUSDT",
        last_price=100.0,
        ofi_tier_1=0.0,
        ofi_tier_2=0.0,
        ofi_tier_3=0.0,
        vol_accel=1.0,
        mom_3m=0.0,
        mom_10m=0.0,
        mom_30m=0.0,
        vwap_dev=0.0,
        tick_agg=0.5,
        spread_bps=0.0,
        score=0.3,
        conviction=0.5,
        eligible=True,
        reason="ok",
    )
    settings = _settings(
        require_edge_over_cost=True,
        edge_bps_per_score=180.0,
        cost_buffer_bps=6.0,
        min_edge_cost_ratio=1.25,
    )

    passed, context = crypto_ofim._passes_entry_edge_cost_gate(settings, feature)

    assert passed is True
    assert context["estimated_edge_bps"] > context["required_edge_bps"]


def test_effective_commission_rate_uses_official_components_and_discount() -> None:
    report = {
        "standardCommission": {"maker": "0.0004", "taker": "0.0005", "buyer": "0.0001", "seller": "0.0002"},
        "specialCommission": {"maker": "0", "taker": "0.0003", "buyer": "0", "seller": "0.0001"},
        "taxCommission": {"maker": "0", "taker": "0.00005", "buyer": "0", "seller": "0.00005"},
        "discount": {"enabledForAccount": True, "enabledForSymbol": True, "discount": "0.25"},
    }

    assert crypto_ofim._effective_commission_rate(report, side="SELL", liquidity="taker") == pytest.approx(0.001025)


def test_load_crypto_ofim_settings_core_sentinel_uses_liquid_core(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "CRYPTO_OFIM_SYMBOLS=CORE_USDT",
                "CRYPTO_OFIM_HOT_UNIVERSE=true",
                "CRYPTO_OFIM_CORE_UNIVERSE=false",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_crypto_ofim_settings(env_file)

    assert settings.core_universe is True
    assert settings.hot_universe is False
    assert settings.symbols == DEFAULT_CORE_USDT_SYMBOLS


def test_load_crypto_ofim_settings_tight_sentinel_uses_focused_liquid_pool(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "CRYPTO_OFIM_SYMBOLS=TIGHT_USDT",
                "CRYPTO_OFIM_HOT_UNIVERSE=true",
                "CRYPTO_OFIM_CORE_UNIVERSE=true",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_crypto_ofim_settings(env_file)

    assert settings.core_universe is False
    assert settings.hot_universe is False
    assert settings.symbols == DEFAULT_TIGHT_USDT_SYMBOLS


def test_api_budget_estimate_keeps_core_universe_inside_safe_rate_limit() -> None:
    settings = _settings(
        mode="testnet",
        symbols=DEFAULT_CORE_USDT_SYMBOLS,
        core_universe=True,
        hot_universe=False,
        depth_limit=100,
        max_positions=3,
    )

    budget = estimate_crypto_ofim_request_weight(settings)

    assert budget["symbol_count"] == len(DEFAULT_CORE_USDT_SYMBOLS)
    assert budget["depth_weight"] == 5
    assert budget["cycle_weight"] == 423
    assert budget["safe_poll_seconds"] <= 60


def test_plan_orders_skips_tiny_rebalance_noise(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(rebalance_threshold=0.02, min_order_notional=1.0)
    state = CryptoPaperState.fresh(settings)
    state.cash = 9_900.0
    state.positions = {"BTCUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.011,
        target_weights={"BTCUSDT": 0.011},
        features=[],
    )

    orders = engine.plan_orders(plan, state)

    assert orders == []


def test_plan_orders_bypasses_rebalance_threshold_for_target_zero_exit(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(rebalance_threshold=0.02, min_order_notional=1.0)
    state = CryptoPaperState.fresh(settings)
    state.cash = 9_900.0
    state.positions = {"BTCUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=-0.2,
        exposure=0.0,
        target_weights={},
        features=[],
        reason="benchmark_risk_off",
    )

    orders = engine.plan_orders(plan, state, cycle_id="risk-off-exit")

    assert len(orders) == 1
    assert orders[0].symbol == "BTCUSDT"
    assert orders[0].side == "SELL"
    assert orders[0].target_weight == 0.0
    assert orders[0].notional < 200.0
    assert "risk_off_exit_bypass_rebalance_threshold" in orders[0].reason
    rows = (
        [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
        if crypto_ofim.EVENTS_FILE.exists()
        else []
    )
    assert not any(
        row["event_type"] == "order_skipped"
        and row["symbol"] == "BTCUSDT"
        and row["reason"] == "below_rebalance_threshold"
        for row in rows
    )


def test_plan_orders_does_not_sell_current_position_for_tiny_rotation(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(rebalance_threshold=0.80)
    state = CryptoPaperState.fresh(settings)
    state.cash = 7_500.0
    state.positions = {"ETHUSDT": 50.0}
    state.avg_cost = {"ETHUSDT": 50.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.1,
        exposure=0.02,
        target_weights={"BTCUSDT": 0.02},
        features=[],
        reason="benchmark_soft_risk",
    )

    orders = engine.plan_orders(plan, state, cycle_id="tiny-rotation")

    assert orders == []
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event_type"] == "order_skipped"
        and row["symbol"] == "ETHUSDT"
        and row["reason"] == "below_rebalance_threshold"
        for row in rows
    )


def test_plan_orders_keeps_min_order_notional_for_target_zero_exit(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(rebalance_threshold=0.02, min_order_notional=1.0)
    state = CryptoPaperState.fresh(settings)
    state.cash = 9_999.5
    state.positions = {"BTCUSDT": 0.005}
    state.avg_cost = {"BTCUSDT": 100.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=-0.2,
        exposure=0.0,
        target_weights={},
        features=[],
        reason="benchmark_risk_off",
    )

    orders = engine.plan_orders(plan, state, cycle_id="dust-exit")

    assert orders == []
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event_type"] == "order_skipped"
        and row["symbol"] == "BTCUSDT"
        and row["reason"] == "capped_notional_below_min_order"
        for row in rows
    )


class BearishBenchmarkClient(FakeBinanceClient):
    def book_ticker(self, symbol: str) -> pd.Series:
        if symbol == "BTCUSDT":
            return pd.Series({"last_price": 100.0, "bid_price": 99.99, "ask_price": 100.01})
        return super().book_ticker(symbol)

    def klines(self, symbol: str, *, interval: str = "1m", limit: int = 60) -> pd.DataFrame:
        if symbol == "BTCUSDT":
            closes = [100.05] * limit
            return pd.DataFrame(
                {
                    "time_key": [f"2026-01-01 00:{i:02d}:00" for i in range(limit)],
                    "open": closes,
                    "high": [x * 1.001 for x in closes],
                    "low": [x * 0.999 for x in closes],
                    "close": closes,
                    "volume": [100.0] * limit,
                }
            )
        return super().klines(symbol, interval=interval, limit=limit)


class SevereBearishBenchmarkClient(BearishBenchmarkClient):
    def klines(self, symbol: str, *, interval: str = "1m", limit: int = 60) -> pd.DataFrame:
        if symbol == "BTCUSDT":
            closes = [101.0] * limit
            return pd.DataFrame(
                {
                    "time_key": [f"2026-01-01 00:{i:02d}:00" for i in range(limit)],
                    "open": closes,
                    "high": [x * 1.001 for x in closes],
                    "low": [x * 0.999 for x in closes],
                    "close": closes,
                    "volume": [100.0] * limit,
                }
            )
        return super().klines(symbol, interval=interval, limit=limit)


def test_generate_plan_scales_new_longs_when_benchmark_is_mildly_below_sma(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(entry_threshold=0.01, symbols=("BTCUSDT", "ETHUSDT"), max_positions=1)
    state = CryptoPaperState.fresh(settings)
    engine = CryptoOfimEngine(settings, client=BearishBenchmarkClient())

    plan = engine.generate_plan(state, cycle_id="trend-filter-test")

    assert plan.target_weights
    assert plan.reason == "benchmark_soft_risk"
    assert plan.benchmark_trend
    assert plan.benchmark_trend["reason"] == "benchmark_below_sma"
    assert plan.benchmark_trend["sma_gap_bps"] > -settings.benchmark_soft_sma_band_bps
    assert plan.exposure < settings.max_gross_exposure
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(row["event_type"] == "signal_scaled" and row["reason"] == "benchmark_soft_risk" for row in rows)


def test_generate_plan_blocks_new_longs_when_benchmark_is_far_below_sma(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(entry_threshold=0.01, symbols=("BTCUSDT", "ETHUSDT"), max_positions=1)
    state = CryptoPaperState.fresh(settings)
    engine = CryptoOfimEngine(settings, client=SevereBearishBenchmarkClient())

    plan = engine.generate_plan(state, cycle_id="hard-risk-off-test")

    assert plan.target_weights == {}
    assert plan.reason == "benchmark_risk_off"
    assert plan.benchmark_trend
    assert plan.benchmark_trend["sma_gap_bps"] < -settings.benchmark_soft_sma_band_bps
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(row["event_type"] == "signal_skipped" and row["reason"] == "benchmark_risk_off" for row in rows)


def test_generate_plan_respects_recent_benchmark_risk_off_cooldown(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        symbols=("BTCUSDT", "ETHUSDT"),
        max_positions=1,
        min_reentry_after_risk_off_seconds=900,
    )
    crypto_ofim.EVENTS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.EVENTS_FILE,
        {
            "ts": crypto_ofim._utc_now(),
            "event_type": "plan_generated",
            "mode": "paper",
            "reason": "benchmark_risk_off",
        },
    )
    state = CryptoPaperState.fresh(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="risk-off-cooldown-test")

    assert plan.target_weights == {}
    assert plan.reason == "benchmark_risk_off_cooldown"
    assert plan.benchmark_trend["risk_off_cooldown_active"] is True
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(row["event_type"] == "signal_skipped" and row["reason"] == "benchmark_risk_off_cooldown" for row in rows)


def test_generate_plan_respects_recent_risk_off_exit_cooldown_globally(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        symbols=("BTCUSDT", "ETHUSDT"),
        max_positions=1,
        min_reentry_after_risk_off_seconds=900,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": crypto_ofim._utc_now(),
            "mode": "paper",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "quantity": 1.0,
            "price": 100.0,
            "status": "filled_paper",
            "reason": f"rebalance_to_ofim_target; {crypto_ofim.RISK_OFF_EXIT_REASON}",
        },
    )
    state = CryptoPaperState.fresh(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="global-risk-off-exit-cooldown")

    assert plan.target_weights == {}
    assert plan.reason == "risk_off_exit_cooldown"
    assert plan.benchmark_trend["risk_off_cooldown_active"] is True
    assert plan.benchmark_trend["risk_off_cooldown_source"] == "risk_off_exit"
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(row["event_type"] == "signal_skipped" and row["reason"] == "risk_off_exit_cooldown" for row in rows)


def test_generate_plan_blocks_new_entries_when_edge_does_not_cover_cost(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        symbols=("BTCUSDT", "ETHUSDT"),
        max_positions=1,
        require_edge_over_cost=True,
        edge_bps_per_score=1.0,
        cost_buffer_bps=1_000.0,
    )
    state = CryptoPaperState.fresh(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="edge-cost-block")

    assert plan.target_weights == {}
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    skipped = [
        row
        for row in rows
        if row["event_type"] == "signal_skipped" and row["reason"] == "edge_below_cost"
    ]
    assert skipped
    assert all(row["estimated_edge_bps"] < row["estimated_round_trip_cost_bps"] for row in skipped)


def test_generate_plan_allows_entries_when_edge_covers_cost(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        symbols=("BTCUSDT", "ETHUSDT"),
        max_positions=1,
        require_edge_over_cost=True,
        edge_bps_per_score=10_000.0,
        cost_buffer_bps=0.0,
    )
    state = CryptoPaperState.fresh(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="edge-cost-allow")

    assert plan.target_weights


def test_generate_plan_requires_consecutive_spot_signals_for_new_entry(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        symbols=("BTCUSDT", "ETHUSDT"),
        max_positions=1,
        signal_confirm_cycles=2,
    )
    state = CryptoPaperState.fresh(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    first = engine.generate_plan(state, cycle_id="spot-confirm-1")
    second = engine.generate_plan(state, cycle_id="spot-confirm-2")

    assert first.target_weights == {}
    assert second.target_weights
    assert state.signal_confirm_streak
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event_type"] == "signal_skipped"
        and row["reason"] == "signal_confirmation_pending"
        and row["signal_confirm_cycles"] == 2
        for row in rows
    )


def test_generate_plan_signal_confirmation_keeps_existing_spot_position(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        symbols=("BTCUSDT", "ETHUSDT"),
        max_positions=1,
        signal_confirm_cycles=3,
    )
    state = CryptoPaperState.fresh(settings)
    state.positions = {"BTCUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="spot-confirm-held-position")

    assert plan.target_weights.get("BTCUSDT", 0.0) > 0


def test_generate_plan_skips_symbols_with_loss_guard_attribution(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        symbols=("BTCUSDT", "ETHUSDT"),
        max_positions=2,
        loss_guard_symbol_max_loss=100.0,
        loss_guard_symbol_max_estimated_fees=50.0,
        loss_guard_symbol_max_trades=10,
    )
    crypto_ofim.ATTRIBUTION_FILE.parent.mkdir(parents=True)
    crypto_ofim.ATTRIBUTION_FILE.write_text(
        json.dumps(
            {
                "by_symbol": {
                    "BTCUSDT": {
                        "net_pnl": -101.0,
                        "estimated_fees": 55.0,
                        "trades": 11,
                    },
                    "ETHUSDT": {
                        "net_pnl": 1.0,
                        "estimated_fees": 1.0,
                        "trades": 1,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    state = CryptoPaperState.fresh(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="symbol-loss-guard")

    assert "BTCUSDT" not in plan.target_weights
    assert "ETHUSDT" in plan.target_weights
    assert plan.benchmark_trend["symbol_loss_guard"]["blocked_symbols"] == ["BTCUSDT"]
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event_type"] == "signal_skipped"
        and row["symbol"] == "BTCUSDT"
        and row["reason"] == "symbol_loss_guard_loss_estimated_fees_trade_count"
        for row in rows
    )


def test_symbol_loss_guard_allows_reduce_only_exit(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        symbols=("BTCUSDT",),
        min_order_notional=1.0,
        rebalance_threshold=0.80,
        exit_confirm_cycles=4,
        loss_guard_symbol_max_loss=100.0,
        loss_guard_symbol_max_estimated_fees=50.0,
        loss_guard_symbol_max_trades=10,
    )
    crypto_ofim.ATTRIBUTION_FILE.parent.mkdir(parents=True)
    crypto_ofim.ATTRIBUTION_FILE.write_text(
        json.dumps(
            {
                "by_symbol": {
                    "BTCUSDT": {
                        "net_pnl": -101.0,
                        "estimated_fees": 55.0,
                        "trades": 11,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 9_900.0
    state.positions = {"BTCUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="symbol-loss-guard-exit")
    orders = engine.plan_orders(plan, state, cycle_id="symbol-loss-guard-exit")

    assert plan.target_weights == {}
    assert plan.reason == "symbol_loss_guard"
    assert plan.benchmark_trend["symbol_loss_guard"]["blocked_symbols"] == ["BTCUSDT"]
    assert len(orders) == 1
    assert orders[0].symbol == "BTCUSDT"
    assert orders[0].side == "SELL"
    assert crypto_ofim.RISK_OFF_EXIT_REASON in orders[0].reason


def test_generate_plan_loss_guard_blocks_new_entries(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        min_order_notional=1.0,
        loss_guard_max_estimated_fees=10.0,
        loss_guard_max_loss=0.0,
        loss_guard_max_trades=0,
    )
    state = CryptoPaperState.fresh(settings)
    state.fees_paid = 11.0
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="loss-guard")

    assert plan.target_weights == {}
    assert plan.reason == "loss_guard_estimated_fees"
    assert plan.benchmark_trend["action"] == "reduce_only_no_new_entries"
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(row["event_type"] == "loss_guard_triggered" for row in rows)


def test_generate_plan_learning_loss_guard_blocks_new_entries(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        loss_guard_max_estimated_fees=10.0,
        loss_guard_max_loss=100.0,
        loss_guard_max_trades=120,
    )
    crypto_ofim.ATTRIBUTION_FILE.parent.mkdir(parents=True)
    crypto_ofim.ATTRIBUTION_FILE.write_text(
        json.dumps(
            {
                "generated_at": "2026-05-12T03:32:40+00:00",
                "total": {
                    "net_pnl": -55_545.09,
                    "estimated_fees": 45_287.30,
                    "trades": 11_389,
                },
            }
        ),
        encoding="utf-8",
    )
    state = CryptoPaperState.fresh(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="learning-loss-guard")

    assert plan.target_weights == {}
    assert plan.reason == "loss_guard_learning_loss_estimated_fees_trade_count"
    assert plan.benchmark_trend["source"] == "crypto_attribution"
    assert plan.benchmark_trend["action"] == "reduce_only_no_new_entries"
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event_type"] == "loss_guard_triggered"
        and row["reason"] == "loss_guard_learning_loss_estimated_fees_trade_count"
        for row in rows
    )


def test_generate_plan_learning_loss_guard_uses_order_memory_fee_drag(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        loss_guard_max_estimated_fees=10.0,
        loss_guard_max_loss=0.0,
        loss_guard_max_trades=0,
    )
    crypto_ofim.ATTRIBUTION_FILE.parent.mkdir(parents=True)
    crypto_ofim.ATTRIBUTION_FILE.write_text(
        json.dumps(
            {
                "generated_at": "2026-05-14T03:32:40+00:00",
                "total": {"net_pnl": 0.0, "estimated_fees": 0.0, "trades": 0},
                "order_quality": {
                    "submitted_records": 48,
                    "submitted_estimated_fees": 15.25,
                },
            }
        ),
        encoding="utf-8",
    )
    state = CryptoPaperState.fresh(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="learning-order-memory-loss-guard")

    assert plan.target_weights == {}
    assert plan.reason == "loss_guard_learning_estimated_fees"
    assert plan.benchmark_trend["source"] == "crypto_attribution_order_memory"
    assert plan.benchmark_trend["estimated_fees"] == 15.25
    assert plan.benchmark_trend["trades"] == 48


def test_symbol_loss_guard_uses_order_memory_fee_drag_when_outcomes_empty(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        symbols=("BTCUSDT", "ETHUSDT"),
        loss_guard_symbol_max_loss=0.0,
        loss_guard_symbol_max_estimated_fees=10.0,
        loss_guard_symbol_max_trades=0,
    )
    crypto_ofim.ATTRIBUTION_FILE.parent.mkdir(parents=True)
    crypto_ofim.ATTRIBUTION_FILE.write_text(
        json.dumps(
            {
                "generated_at": "2026-05-14T03:32:40+00:00",
                "total": {"net_pnl": 0.0, "estimated_fees": 0.0, "trades": 0},
                "by_symbol": {},
                "order_quality": {
                    "submitted_cost_by_symbol": {
                        "BTCUSDT": {"estimated_fees": 12.5, "records": 25, "notional": 12_500.0},
                        "ETHUSDT": {"estimated_fees": 2.0, "records": 4, "notional": 2_000.0},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    blocked = crypto_ofim._symbol_loss_guard_breaches(settings, settings.symbols)

    assert set(blocked) == {"BTCUSDT"}
    assert blocked["BTCUSDT"]["reason"] == "symbol_loss_guard_estimated_fees"
    assert blocked["BTCUSDT"]["source"] == "crypto_attribution_order_memory"
    assert blocked["BTCUSDT"]["estimated_fees"] == pytest.approx(12.5)
    assert blocked["BTCUSDT"]["trades"] == 25


def test_loss_guard_allows_reduce_only_exit(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        min_order_notional=1.0,
        rebalance_threshold=0.80,
        loss_guard_max_estimated_fees=10.0,
        loss_guard_max_loss=0.0,
        loss_guard_max_trades=0,
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 9_900.0
    state.positions = {"BTCUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    state.fees_paid = 11.0
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="loss-guard-exit")
    orders = engine.plan_orders(plan, state, cycle_id="loss-guard-exit")

    assert plan.reason == "loss_guard_estimated_fees"
    assert len(orders) == 1
    assert orders[0].symbol == "BTCUSDT"
    assert orders[0].side == "SELL"
    assert "risk_off_exit_bypass_rebalance_threshold" in orders[0].reason


def test_learning_loss_guard_allows_reduce_only_exit(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        min_order_notional=1.0,
        rebalance_threshold=0.80,
        loss_guard_max_estimated_fees=10.0,
        loss_guard_max_loss=100.0,
        loss_guard_max_trades=120,
    )
    crypto_ofim.ATTRIBUTION_FILE.parent.mkdir(parents=True)
    crypto_ofim.ATTRIBUTION_FILE.write_text(
        json.dumps({"total": {"net_pnl": -101.0, "estimated_fees": 11.0, "trades": 121}}),
        encoding="utf-8",
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 9_900.0
    state.positions = {"BTCUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="learning-loss-guard-exit")
    orders = engine.plan_orders(plan, state, cycle_id="learning-loss-guard-exit")

    assert plan.reason == "loss_guard_learning_loss_estimated_fees_trade_count"
    assert len(orders) == 1
    assert orders[0].symbol == "BTCUSDT"
    assert orders[0].side == "SELL"
    assert crypto_ofim.RISK_OFF_EXIT_REASON in orders[0].reason


def test_testnet_learning_loss_guard_skips_signed_account_sync_when_flat(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        mode="testnet",
        api_key="key",
        api_secret="secret",
        symbols=("BTCUSDT",),
        benchmark="BTCUSDT",
        loss_guard_max_estimated_fees=10.0,
        loss_guard_max_loss=100.0,
        loss_guard_max_trades=120,
    )
    crypto_ofim.LEDGER_EPOCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_ofim.LEDGER_EPOCH_FILE.write_text(
        json.dumps(
            {
                "ts": "2026-01-01T00:00:00+00:00",
                "mode": "testnet",
                "quote_asset": "USDT",
                "balances": {"USDT": 1000.0},
            }
        ),
        encoding="utf-8",
    )
    crypto_ofim.ATTRIBUTION_FILE.write_text(
        json.dumps({"total": {"net_pnl": -101.0, "estimated_fees": 11.0, "trades": 121}}),
        encoding="utf-8",
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 1000.0
    state.positions = {}
    state.avg_cost = {}
    state.save(settings)

    class SignedAccountShouldNotBeCalledClient(FakeBinanceClient):
        def account(self):
            raise AssertionError("signed account polling should be skipped while flat under learning loss guard")

    engine = CryptoOfimEngine(settings, client=SignedAccountShouldNotBeCalledClient())

    payload = engine.run_once(submit=True)

    assert payload["plan_reason"] == "loss_guard_learning_loss_estimated_fees_trade_count"
    assert payload["planned_orders"] == []
    assert payload["submitted_orders"] == []
    assert payload["account"]["cash"] == pytest.approx(1000.0)
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event_type"] == "testnet_account_sync_skipped"
        and row["action"] == "guarded_flat_no_signed_account_poll"
        for row in rows
    )


def test_loss_guard_blocks_recent_trade_churn(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        loss_guard_max_loss=0.0,
        loss_guard_max_estimated_fees=0.0,
        loss_guard_max_trades=0,
        loss_guard_recent_window_seconds=900,
        loss_guard_max_recent_trades=3,
        loss_guard_max_recent_risk_off_exits=0,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    for idx, side in enumerate(("BUY", "SELL", "BUY")):
        crypto_ofim._append_jsonl(
            crypto_ofim.ORDERS_FILE,
            {
                "ts": crypto_ofim._utc_now(),
                "mode": "paper",
                "symbol": "BTCUSDT",
                "side": side,
                "quantity": 1.0,
                "price": 100.0 + idx,
                "status": "filled_paper",
                "reason": "rebalance_to_ofim_target",
            },
        )
    state = CryptoPaperState.fresh(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="recent-churn-guard")

    assert plan.target_weights == {}
    assert plan.reason == "loss_guard_recent_trades"
    assert plan.benchmark_trend["recent_trade_count"] == 3
    assert plan.benchmark_trend["recent_flip_count"] == 2


def test_loss_guard_blocks_recent_risk_off_exit_churn(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        loss_guard_max_loss=0.0,
        loss_guard_max_estimated_fees=0.0,
        loss_guard_max_trades=0,
        loss_guard_recent_window_seconds=900,
        loss_guard_max_recent_trades=0,
        loss_guard_max_recent_risk_off_exits=2,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    for symbol in ("BTCUSDT", "ETHUSDT"):
        crypto_ofim._append_jsonl(
            crypto_ofim.ORDERS_FILE,
            {
                "ts": crypto_ofim._utc_now(),
                "mode": "paper",
                "symbol": symbol,
                "side": "SELL",
                "quantity": 1.0,
                "price": 100.0,
                "status": "filled_paper",
                "reason": f"rebalance_to_ofim_target; {crypto_ofim.RISK_OFF_EXIT_REASON}",
            },
        )
    state = CryptoPaperState.fresh(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="recent-risk-off-guard")

    assert plan.target_weights == {}
    assert plan.reason == "loss_guard_recent_risk_off_exits"
    assert plan.benchmark_trend["recent_risk_off_exit_count"] == 2
    assert plan.benchmark_trend["recent_symbols"] == ["BTCUSDT", "ETHUSDT"]


def test_loss_guard_blocks_recent_flip_churn(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        loss_guard_max_loss=0.0,
        loss_guard_max_estimated_fees=0.0,
        loss_guard_max_trades=0,
        loss_guard_recent_window_seconds=900,
        loss_guard_max_recent_trades=0,
        loss_guard_max_recent_risk_off_exits=0,
        loss_guard_max_recent_flips=2,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    for side in ("BUY", "SELL", "BUY"):
        crypto_ofim._append_jsonl(
            crypto_ofim.ORDERS_FILE,
            {
                "ts": crypto_ofim._utc_now(),
                "mode": "paper",
                "symbol": "BTCUSDT",
                "side": side,
                "quantity": 1.0,
                "price": 100.0,
                "status": "filled_paper",
                "reason": "rebalance_to_ofim_target",
            },
        )
    state = CryptoPaperState.fresh(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state, cycle_id="recent-flip-guard")

    assert plan.target_weights == {}
    assert plan.reason == "loss_guard_recent_flips"
    assert plan.benchmark_trend["recent_flip_count"] == 2
    assert plan.benchmark_trend["max_recent_flips"] == 2


def test_run_once_paper_submit_updates_isolated_ledger(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings()
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    payload = engine.run_once(submit=True)
    state = CryptoPaperState.load(settings)

    assert payload["status"] == "submitted"
    assert state.cash < settings.initial_cash
    assert state.positions
    assert crypto_ofim.ORDERS_FILE.exists()
    memory_file = crypto_ofim.RUNTIME_DIR / "crypto_order_memory.jsonl"
    assert memory_file.exists()
    memory_rows = [json.loads(line) for line in memory_file.read_text(encoding="utf-8").splitlines()]
    assert {"planned", "filled"} <= {row["stage"] for row in memory_rows}


def test_run_once_writes_replayable_event_journal(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings()
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    payload = engine.run_once(submit=True)

    assert payload["cycle_id"]
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    event_types = {row["event_type"] for row in rows}
    assert {"cycle_started", "market_snapshot", "feature_scored", "plan_generated", "cycle_completed"} <= event_types
    assert "order_planned" in event_types
    assert "order_submitted" in event_types
    assert {row["cycle_id"] for row in rows} == {payload["cycle_id"]}


def test_run_once_status_includes_strategy_safety_settings(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.29,
        min_reentry_after_risk_off_seconds=7200,
        loss_guard_symbol_max_estimated_fees=50.0,
    )
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    payload = engine.run_once(submit=False)
    status = json.loads(crypto_ofim.STATUS_FILE.read_text(encoding="utf-8"))

    strategy_settings = payload["strategy_settings"]
    assert strategy_settings["entry_threshold"] == pytest.approx(0.29)
    assert strategy_settings["min_reentry_after_risk_off_seconds"] == 7200
    assert strategy_settings["loss_guard_max_estimated_fees"] == pytest.approx(
        crypto_ofim.DEFAULT_LOSS_GUARD_MAX_ESTIMATED_FEES
    )
    assert strategy_settings["loss_guard_symbol_max_estimated_fees"] == pytest.approx(50.0)
    assert strategy_settings["require_edge_over_cost"] is False
    assert status["strategy_settings"] == strategy_settings


def test_plan_orders_rotates_by_selling_before_buying(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(min_order_notional=1.0)
    state = CryptoPaperState.fresh(settings)
    state.cash = 0.0
    state.positions = {"ETHUSDT": 100.0}
    state.avg_cost = {"ETHUSDT": 50.0}
    state.save()
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.8,
        target_weights={"BTCUSDT": 0.5},
        features=[],
    )

    orders = engine.plan_orders(plan, state)

    assert [order.side for order in orders] == ["SELL", "BUY"]


def test_plan_orders_caps_testnet_sizing_to_active_capital_and_order_limit(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        active_capital=10_000.0,
        min_order_notional=1.0,
        max_order_notional=2_500.0,
        max_order_book_impact_bps=0.0,
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 500_000.0
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.5,
        target_weights={"BTCUSDT": 0.5},
        features=[],
    )

    orders = engine.plan_orders(plan, state)

    assert len(orders) == 1
    assert orders[0].target_value == 5_000.0
    assert orders[0].notional <= 2_500.0
    assert "capped_notional" in orders[0].reason


def test_plan_orders_respects_symbol_trade_cooldown(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(min_order_notional=1.0, min_trade_interval_seconds=120)
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": crypto_ofim._utc_now(),
            "mode": "paper",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1,
            "price": 100,
            "status": "filled_paper",
        },
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 10_000.0
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.8,
        target_weights={"BTCUSDT": 0.5},
        features=[],
    )

    orders = engine.plan_orders(plan, state, cycle_id="cooldown-test")

    assert orders == []
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(row["event_type"] == "order_skipped" and row["reason"] == "cooldown_active" for row in rows)


def test_plan_orders_blocks_new_entry_before_projected_fee_guard(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        min_order_notional=1.0,
        loss_guard_max_estimated_fees=5.0,
        loss_guard_max_trades=0,
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 10_000.0
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    monkeypatch.setattr(
        engine,
        "account_snapshot",
        lambda state=None: {
            "equity": 10_000.0,
            "prices": {"BTCUSDT": 100.0},
            "holdings_value": {},
            "estimated_fees_paid": 4.99,
            "fees_paid": 4.99,
            "trade_count": 0,
        },
    )
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.5,
        target_weights={"BTCUSDT": 0.5},
        features=[],
    )

    orders = engine.plan_orders(plan, state, cycle_id="projected-fee-guard")

    assert orders == []
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event_type"] == "order_skipped"
        and row["reason"] == "projected_loss_guard_budget"
        and row["breaches"] == ["estimated_fees"]
        for row in rows
    )


def test_plan_orders_reserves_round_trip_fee_before_new_entry(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        min_order_notional=1.0,
        active_capital_pct=1.0,
        loss_guard_max_estimated_fees=1.5,
        loss_guard_max_trades=0,
        slippage_bps=0.0,
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 10_000.0
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    monkeypatch.setattr(
        engine,
        "account_snapshot",
        lambda state=None: {
            "equity": 10_000.0,
            "prices": {"BTCUSDT": 100.0},
            "holdings_value": {},
            "estimated_fees_paid": 0.0,
            "fees_paid": 0.0,
            "trade_count": 0,
        },
    )
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.1,
        target_weights={"BTCUSDT": 0.1},
        features=[],
    )

    orders = engine.plan_orders(plan, state, cycle_id="projected-round-trip-fee-guard")

    assert orders == []
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    skipped = [
        row
        for row in rows
        if row["event_type"] == "order_skipped" and row["reason"] == "projected_loss_guard_budget"
    ]
    assert skipped
    assert skipped[-1]["fee"] == pytest.approx(1.0)
    assert skipped[-1]["reserved_exit_fee"] == pytest.approx(1.0)
    assert skipped[-1]["projected_estimated_fees"] == pytest.approx(2.0)


def test_plan_orders_blocks_new_entry_before_projected_trade_count_guard(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        min_order_notional=1.0,
        loss_guard_max_estimated_fees=0.0,
        loss_guard_max_trades=120,
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 10_000.0
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    monkeypatch.setattr(
        engine,
        "account_snapshot",
        lambda state=None: {
            "equity": 10_000.0,
            "prices": {"BTCUSDT": 100.0},
            "holdings_value": {},
            "estimated_fees_paid": 0.0,
            "fees_paid": 0.0,
            "trade_count": 119,
        },
    )
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.5,
        target_weights={"BTCUSDT": 0.5},
        features=[],
    )

    orders = engine.plan_orders(plan, state, cycle_id="projected-trade-count-guard")

    assert orders == []
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event_type"] == "order_skipped"
        and row["reason"] == "projected_loss_guard_budget"
        and row["breaches"] == ["trade_count"]
        for row in rows
    )


def test_plan_orders_blocks_new_entry_before_projected_recent_trade_guard(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        min_order_notional=1.0,
        loss_guard_max_estimated_fees=0.0,
        loss_guard_max_trades=0,
        loss_guard_recent_window_seconds=900,
        loss_guard_max_recent_trades=12,
        loss_guard_max_recent_flips=0,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_ofim.ORDERS_FILE.write_text(
        "".join(
            json.dumps(
                {
                    "ts": crypto_ofim._utc_now(),
                    "mode": "paper",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "status": "filled_paper",
                }
            )
            + "\n"
            for _ in range(11)
        ),
        encoding="utf-8",
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 10_000.0
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.5,
        target_weights={"BTCUSDT": 0.5},
        features=[],
    )

    orders = engine.plan_orders(plan, state, cycle_id="projected-recent-trades")

    assert orders == []
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event_type"] == "order_skipped"
        and row["reason"] == "projected_loss_guard_budget"
        and row["breaches"] == ["recent_trades"]
        and row["projected_recent_trade_count"] == 12
        for row in rows
    )


def test_plan_orders_blocks_new_entry_before_projected_recent_flip_guard(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        min_order_notional=1.0,
        loss_guard_max_estimated_fees=0.0,
        loss_guard_max_trades=0,
        loss_guard_recent_window_seconds=900,
        loss_guard_max_recent_trades=0,
        loss_guard_max_recent_flips=1,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": crypto_ofim._utc_now(),
            "mode": "paper",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "status": "filled_paper",
        },
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 10_000.0
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.5,
        target_weights={"BTCUSDT": 0.5},
        features=[],
    )

    orders = engine.plan_orders(plan, state, cycle_id="projected-recent-flips")

    assert orders == []
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event_type"] == "order_skipped"
        and row["reason"] == "projected_loss_guard_budget"
        and row["breaches"] == ["recent_flips"]
        and row["projected_recent_flip_count"] == 1
        for row in rows
    )


def test_plan_orders_respects_opposite_side_flip_cooldown(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(min_order_notional=1.0, min_flip_interval_seconds=300)
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": crypto_ofim._utc_now(),
            "mode": "paper",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "quantity": 1.0,
            "price": 100.0,
            "status": "filled_paper",
        },
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 10_000.0
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.5,
        target_weights={"BTCUSDT": 0.5},
        features=[],
    )

    orders = engine.plan_orders(plan, state, cycle_id="flip-cooldown")

    assert orders == []
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(row["event_type"] == "order_skipped" and row["reason"] == "flip_cooldown_active" for row in rows)


def test_plan_orders_risk_off_exit_bypasses_flip_cooldown(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        rebalance_threshold=0.02,
        min_order_notional=1.0,
        min_trade_interval_seconds=300,
        min_flip_interval_seconds=300,
        min_holding_seconds=300,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": crypto_ofim._utc_now(),
            "mode": "paper",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1.0,
            "price": 100.0,
            "status": "filled_paper",
        },
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 9_900.0
    state.positions = {"BTCUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=-0.2,
        exposure=0.0,
        target_weights={},
        features=[],
        reason="benchmark_risk_off",
    )

    orders = engine.plan_orders(plan, state, cycle_id="risk-off-flip-bypass")

    assert len(orders) == 1
    assert orders[0].side == "SELL"
    rows = (
        [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
        if crypto_ofim.EVENTS_FILE.exists()
        else []
    )
    assert not any(row["event_type"] == "order_skipped" and row["reason"] == "flip_cooldown_active" for row in rows)
    assert not any(row["event_type"] == "order_skipped" and row["reason"] == "cooldown_active" for row in rows)
    assert not any(row["event_type"] == "order_skipped" and row["reason"] == "min_holding_period_active" for row in rows)


def test_plan_orders_empty_signal_exit_respects_min_holding(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        rebalance_threshold=0.0,
        min_order_notional=1.0,
        min_holding_seconds=300,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": datetime.now(UTC).isoformat(),
            "mode": "paper",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1.0,
            "price": 100.0,
            "status": "filled_paper",
        },
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 9_900.0
    state.positions = {"BTCUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.0,
        target_weights={},
        features=[],
        reason="ok",
    )

    orders = engine.plan_orders(plan, state, cycle_id="empty-signal-min-holding")

    assert orders == []
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(row["event_type"] == "order_skipped" and row["reason"] == "min_holding_period_active" for row in rows)
    assert not any(
        row["event_type"] == "order_skipped" and row["reason"] == crypto_ofim.RISK_OFF_EXIT_REASON
        for row in rows
    )


def test_plan_orders_empty_signal_exit_uses_plain_rebalance_reason(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        rebalance_threshold=0.0,
        min_order_notional=1.0,
        min_holding_seconds=300,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": "2000-01-01T00:00:00+00:00",
            "mode": "paper",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1.0,
            "price": 100.0,
            "status": "filled_paper",
        },
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 9_900.0
    state.positions = {"BTCUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.0,
        target_weights={},
        features=[],
        reason="ok",
    )

    orders = engine.plan_orders(plan, state, cycle_id="empty-signal-plain-exit")

    assert len(orders) == 1
    assert orders[0].side == "SELL"
    assert orders[0].reason == "rebalance_to_ofim_target"


def test_plan_orders_respects_min_holding_period_for_routine_sell(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        rebalance_threshold=0.0,
        min_order_notional=1.0,
        min_holding_seconds=300,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": datetime.now(UTC).isoformat(),
            "mode": "paper",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 60.0,
            "price": 100.0,
            "fee": 0.0,
            "status": "filled_paper",
        },
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 4_000.0
    state.positions = {"BTCUSDT": 60.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.2,
        target_weights={"BTCUSDT": 0.2},
        features=[],
        reason="ok",
    )

    orders = engine.plan_orders(plan, state, cycle_id="min-holding")

    assert orders == []
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(row["event_type"] == "order_skipped" and row["reason"] == "min_holding_period_active" for row in rows)


def test_plan_orders_blocks_reentry_after_risk_off_exit(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        min_order_notional=1.0,
        min_flip_interval_seconds=0,
        min_reentry_after_risk_off_seconds=900,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": crypto_ofim._utc_now(),
            "mode": "paper",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "quantity": 1.0,
            "price": 100.0,
            "status": "filled_paper",
            "reason": "rebalance_to_ofim_target; risk_off_exit_bypass_rebalance_threshold",
        },
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 10_000.0
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())
    plan = crypto_ofim.CryptoOfimPlan(
        mode="paper",
        benchmark="BTCUSDT",
        benchmark_score=0.5,
        exposure=0.5,
        target_weights={"BTCUSDT": 0.5},
        features=[],
    )

    orders = engine.plan_orders(plan, state, cycle_id="risk-off-reentry-cooldown")

    assert orders == []
    rows = [json.loads(line) for line in crypto_ofim.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]
    assert any(
        row["event_type"] == "order_skipped" and row["reason"] == "risk_off_reentry_cooldown_active"
        for row in rows
    )


def test_account_snapshot_reports_starting_equity_and_return(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(initial_cash=10_000.0, active_capital=2_000.0)
    state = CryptoPaperState.fresh(settings)
    state.cash = 10_400.0
    state.positions = {"BTCUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 80.0}
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    snapshot = engine.account_snapshot(state)

    assert snapshot["starting_equity"] == 10_000.0
    assert snapshot["equity"] == 10_500.0
    assert snapshot["net_pnl"] == 500.0
    assert snapshot["net_return_pct"] == 0.05
    assert snapshot["active_capital"] == 2_000.0


def test_account_snapshot_uses_dynamic_active_capital_pct(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(initial_cash=10_000.0, active_capital=0.0, active_capital_pct=0.40)
    state = CryptoPaperState.fresh(settings)
    state.cash = 10_000.0
    state.positions = {"BTCUSDT": 10.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    snapshot = engine.account_snapshot(state)

    assert snapshot["equity"] == 11_000.0
    assert snapshot["active_capital"] == 4_400.0


def test_testnet_state_resyncs_when_ledger_epoch_changes(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(mode="testnet", api_key="key", api_secret="secret")
    state = CryptoPaperState.fresh(settings)
    state.ledger_epoch_id = "old-epoch"
    state.empty_target_streak = 3
    state.last_target_weights = {"BTCUSDT": 0.5}
    state.last_order_books = {"BTCUSDT": {"Bid": [[100.0, 1.0]], "Ask": [[100.1, 1.0]]}}
    state.save(settings)
    epoch = crypto_ofim.set_crypto_ofim_ledger_epoch(
        settings,
        reason="unit_test_reset",
        balances={"USDT": 10_000.0},
    )
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    loaded = engine._load_state_for_mode()

    assert loaded.ledger_epoch_id == epoch["epoch_id"]
    assert loaded.empty_target_streak == 0
    assert loaded.last_target_weights == {}
    assert loaded.last_order_books == {}


def test_testnet_liquidation_defaults_to_strategy_universe(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        mode="testnet",
        api_key="key",
        api_secret="secret",
        symbols=("BTCUSDT",),
        benchmark="BTCUSDT",
    )
    engine = CryptoOfimEngine(settings, client=LiquidationClient())

    result = engine.liquidate_testnet_to_quote(submit=False)

    assert result["liquidation_scope"] == "strategy_universe"
    assert [row["symbol"] for row in result["planned"]] == ["BTCUSDT"]
    assert any(row["symbol"] == "FAKEUSDT" and row["reason"] == "outside_liquidation_universe" for row in result["skipped"])


def test_testnet_liquidation_all_assets_requires_explicit_setting(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        mode="testnet",
        api_key="key",
        api_secret="secret",
        symbols=("BTCUSDT",),
        benchmark="BTCUSDT",
        liquidate_all_testnet_assets=True,
    )
    engine = CryptoOfimEngine(settings, client=LiquidationClient())

    result = engine.liquidate_testnet_to_quote(submit=False)

    assert result["liquidation_scope"] == "all_testnet_assets"
    assert {row["symbol"] for row in result["planned"]} == {"BTCUSDT", "FAKEUSDT"}


class NoSignalBinanceClient(FakeBinanceClient):
    def klines(self, symbol: str, *, interval: str = "1m", limit: int = 60) -> pd.DataFrame:
        price = self.prices[symbol]
        return pd.DataFrame(
            {
                "time_key": [f"2026-01-01 00:{i:02d}:00" for i in range(limit)],
                "open": [price] * limit,
                "high": [price] * limit,
                "low": [price] * limit,
                "close": [price] * limit,
                "volume": [50.0] * limit,
            }
        )

    def depth(self, symbol: str, *, limit: int = 100):
        price = self.prices[symbol]
        bids = [[price * (1 - i * 0.0001), 10.0] for i in range(1, 61)]
        asks = [[price * (1 + i * 0.0001), 10.0] for i in range(1, 61)]
        return {"Bid": bids, "Ask": asks}

    def recent_trades(self, symbol: str, *, limit: int = 100) -> pd.DataFrame:
        price = self.prices[symbol]
        return pd.DataFrame(
            [{"price": price, "volume": 1.0, "ticker_direction": "NEUTRAL"} for _ in range(limit)],
            columns=["price", "volume", "ticker_direction"],
        )


def test_max_holding_guard_bypasses_empty_signal_delay(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.5,
        exit_confirm_cycles=10,
        max_holding_seconds=60,
        min_order_notional=1.0,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": "2000-01-01T00:00:00+00:00",
            "mode": "paper",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1,
            "price": 100,
            "status": "filled_paper",
        },
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 9_900.0
    state.positions = {"BTCUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=NoSignalBinanceClient())

    plan = engine.generate_plan(state)
    orders = engine.plan_orders(plan, state)

    assert plan.target_weights == {}
    assert [order.side for order in orders] == ["SELL"]


def test_max_holding_guard_does_not_block_renewed_signal(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.01,
        exit_confirm_cycles=10,
        max_holding_seconds=60,
        min_order_notional=1.0,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": "2000-01-01T00:00:00+00:00",
            "mode": "paper",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1,
            "price": 100,
            "status": "filled_paper",
        },
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 9_900.0
    state.positions = {"BTCUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    plan = engine.generate_plan(state)

    assert "BTCUSDT" in plan.target_weights
    assert plan.reason == "ok"


def test_dust_stale_position_does_not_bypass_fresh_position_exit_delay(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        entry_threshold=0.5,
        exit_confirm_cycles=4,
        max_holding_seconds=60,
        min_order_notional=20.0,
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": "2000-01-01T00:00:00+00:00",
            "mode": "paper",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 0.01,
            "price": 100,
            "status": "filled_paper",
        },
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 9_949.0
    state.positions = {"BTCUSDT": 0.01, "ETHUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 100.0, "ETHUSDT": 50.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=NoSignalBinanceClient())

    plan = engine.generate_plan(state)
    orders = engine.plan_orders(plan, state)

    assert "ETHUSDT" in plan.target_weights
    assert all(order.symbol != "ETHUSDT" for order in orders)


def test_account_snapshot_reports_position_age_and_stale_state(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(max_holding_seconds=60)
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": "2000-01-01T00:00:00+00:00",
            "mode": "paper",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1,
            "price": 100,
            "fee": 0,
            "status": "filled_paper",
        },
    )
    state = CryptoPaperState.fresh(settings)
    state.cash = 9_900.0
    state.positions = {"BTCUSDT": 1.0}
    state.avg_cost = {"BTCUSDT": 100.0}
    state.save(settings)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    snapshot = engine.account_snapshot(state)

    assert snapshot["stale_position_count"] == 1
    assert snapshot["position_details"][0]["symbol"] == "BTCUSDT"
    assert snapshot["position_details"][0]["stale"] is True
    assert snapshot["position_details"][0]["age_seconds"] > settings.max_holding_seconds


def test_low_volume_is_soft_penalty_not_hard_reject() -> None:
    settings = _settings(entry_threshold=0.01, min_vol_acceleration=2.0)
    client = FakeBinanceClient()
    engine = CryptoOfimEngine(settings, client=client)
    low_volume_bars = client.klines("BTCUSDT")
    low_volume_bars["volume"] = 50.0

    feature = engine._score_symbol(
        "BTCUSDT",
        client.depth("BTCUSDT"),
        None,
        low_volume_bars,
        client.recent_trades("BTCUSDT"),
        client.book_ticker("BTCUSDT"),
    )

    assert "volume_too_low" not in feature.reason
    assert "low_volume_soft_penalty" in feature.reason


def test_submit_testnet_orders_uses_binance_quantity_rules(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeTestnetOrderClient()
    settings = _settings(mode="testnet", api_key="key", api_secret="secret")
    engine = CryptoOfimEngine(settings, client=client)
    order = CryptoOfimOrder(
        ts="2026-01-01T00:00:00+00:00",
        mode="testnet",
        symbol="BTCUSDT",
        side="BUY",
        quantity=12.345678,
        price=100.0,
        notional=1234.5678,
        fee=1.2345,
        status="planned",
        reason="unit_test",
        target_weight=0.1,
        current_value=0.0,
        target_value=1000.0,
    )

    submitted = engine.submit_testnet_orders([order])

    assert client.submitted_quantities == ["12"]
    assert submitted[0].quantity == 12.0
    assert submitted[0].notional == 1200.0
    assert submitted[0].status == "submitted_testnet"


def test_submit_testnet_orders_logs_actual_binance_fill_values(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    client = FakeFilledTestnetOrderClient()
    settings = _settings(mode="testnet", api_key="key", api_secret="secret")
    engine = CryptoOfimEngine(settings, client=client)
    order = CryptoOfimOrder(
        ts="2026-01-01T00:00:00+00:00",
        mode="testnet",
        symbol="BTCUSDT",
        side="BUY",
        quantity=12.345678,
        price=100.0,
        notional=1234.5678,
        fee=1.2345,
        status="planned",
        reason="unit_test",
        target_weight=0.1,
        current_value=0.0,
        target_value=1000.0,
    )

    submitted = engine.submit_testnet_orders([order])

    assert submitted[0].quantity == 2.0
    assert submitted[0].price == 45.0
    assert submitted[0].notional == 90.0
    assert submitted[0].fee == 0.0


class _FakeResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {}


class _FakeSession:
    def __init__(self) -> None:
        self.calls = []

    def request(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params or {}, "headers": headers or {}})
        return _FakeResponse()


def test_signed_request_adds_api_key_timestamp_and_signature() -> None:
    session = _FakeSession()
    client = BinanceSpotClient(
        base_url="https://testnet.binance.vision",
        api_key="key",
        api_secret="secret",
        session=session,
    )

    client.account()

    call = session.calls[-1]
    assert call["headers"]["X-MBX-APIKEY"] == "key"
    assert "timestamp" in call["params"]
    assert "signature" in call["params"]


class _ExchangeInfoResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "symbols": [
                {
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": "0.10000000", "maxQty": "1000000.00000000", "stepSize": "0.10000000"},
                        {"filterType": "MARKET_LOT_SIZE", "minQty": "1.00000000", "maxQty": "100.00000000", "stepSize": "1.00000000"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "5.00000000"},
                    ]
                }
            ]
        }


class _ExchangeInfoSession:
    def request(self, method, url, params=None, headers=None, timeout=None):
        return _ExchangeInfoResponse()


def test_market_quantity_rules_apply_market_lot_size_filter() -> None:
    client = BinanceSpotClient(base_url="https://testnet.binance.vision", session=_ExchangeInfoSession())

    quantity_dec, quantity_text, reject_reason = client.normalize_market_quantity("ZBTUSDT", 451685.5, 0.2)

    assert quantity_dec == Decimal("100")
    assert quantity_text == "100"
    assert reject_reason is None


def test_sanitize_binance_error_redacts_signature() -> None:
    raw = "GET /api/v3/account?recvWindow=5000&timestamp=1&signature=abc123def456 failed"

    sanitized = _sanitize_binance_error(raw)

    assert "signature=<redacted>" in sanitized
    assert "abc123def456" not in sanitized


def test_testnet_account_snapshot_estimates_realized_pnl_from_order_log(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(mode="testnet", api_key="key", api_secret="secret", symbols=("BTCUSDT",), benchmark="BTCUSDT")
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    for order in [
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "mode": "testnet",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1,
            "price": 100,
            "fee": 0.1,
            "status": "submitted_testnet",
        },
        {
            "ts": "2026-01-01T00:01:00+00:00",
            "mode": "testnet",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "quantity": 1,
            "price": 110,
            "fee": 0.11,
            "status": "submitted_testnet",
        },
    ]:
        crypto_ofim._append_jsonl(crypto_ofim.ORDERS_FILE, order)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    snapshot = engine.account_snapshot()

    assert snapshot["trade_count"] == 2
    assert snapshot["fees_paid"] == 0.21
    assert snapshot["realized_pnl"] == 9.79
    assert snapshot["extra_balance_count"] == 1


def test_testnet_account_snapshot_prefers_actual_binance_fills_over_estimates(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(mode="testnet", api_key="key", api_secret="secret", symbols=("BTCUSDT",), benchmark="BTCUSDT")
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    for order in [
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "mode": "testnet",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1,
            "price": 999,
            "fee": 999,
            "status": "submitted_testnet",
            "response": {
                "executedQty": "1.00000000",
                "cummulativeQuoteQty": "100.00000000",
                "fills": [{"price": "100.00000000", "qty": "1.00000000", "commission": "0.00000000", "commissionAsset": "BTC"}],
            },
        },
        {
            "ts": "2026-01-01T00:01:00+00:00",
            "mode": "testnet",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "quantity": 1,
            "price": 1,
            "fee": 999,
            "status": "submitted_testnet",
            "response": {
                "executedQty": "1.00000000",
                "cummulativeQuoteQty": "110.00000000",
                "fills": [{"price": "110.00000000", "qty": "1.00000000", "commission": "0.00000000", "commissionAsset": "USDT"}],
            },
        },
    ]:
        crypto_ofim._append_jsonl(crypto_ofim.ORDERS_FILE, order)
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    snapshot = engine.account_snapshot()

    assert snapshot["trade_count"] == 2
    assert snapshot["fees_paid"] == 0.0
    assert snapshot["estimated_fees_paid"] == 0.21
    assert snapshot["realized_pnl"] == 10.0
    assert snapshot["realized_pnl_after_estimated_fees"] == 9.79


def test_testnet_ledger_prefers_user_stream_fills_without_double_counting_orders(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(mode="testnet", api_key="key", api_secret="secret", symbols=("BTCUSDT",), benchmark="BTCUSDT")
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    buy_event = {
        "e": "executionReport",
        "E": 1760000000000,
        "s": "BTCUSDT",
        "S": "BUY",
        "x": "TRADE",
        "X": "FILLED",
        "i": 101,
        "t": 9001,
        "c": "buy-1",
        "l": "1.00000000",
        "L": "100.00000000",
        "n": "0.00000000",
        "N": "BTC",
        "T": 1760000000000,
    }
    sell_event = {
        "e": "executionReport",
        "E": 1760000060000,
        "s": "BTCUSDT",
        "S": "SELL",
        "x": "TRADE",
        "X": "FILLED",
        "i": 102,
        "t": 9002,
        "c": "sell-1",
        "l": "1.00000000",
        "L": "110.00000000",
        "n": "0.00000000",
        "N": "USDT",
        "T": 1760000060000,
    }
    crypto_ofim.record_crypto_ofim_user_stream_event(buy_event, mode="testnet", quote_asset="USDT")
    crypto_ofim.record_crypto_ofim_user_stream_event(sell_event, mode="testnet", quote_asset="USDT")
    for order_id, side, price in [(101, "BUY", 100), (102, "SELL", 110)]:
        crypto_ofim._append_jsonl(
            crypto_ofim.ORDERS_FILE,
            {
                "ts": "2026-01-01T00:00:00+00:00",
                "mode": "testnet",
                "symbol": "BTCUSDT",
                "side": side,
                "quantity": 1,
                "price": price,
                "fee": 0,
                "status": "submitted_testnet",
                "response": {
                    "orderId": order_id,
                    "executedQty": "1.00000000",
                    "cummulativeQuoteQty": f"{price:.8f}",
                    "fills": [{"price": f"{price:.8f}", "qty": "1.00000000", "commission": "0.00000000", "commissionAsset": "USDT"}],
                },
            },
        )
    engine = CryptoOfimEngine(settings, client=FakeBinanceClient())

    snapshot = engine.account_snapshot()

    assert snapshot["trade_count"] == 2
    assert snapshot["realized_pnl"] == 10.0
    assert snapshot["positions"] == {}


def test_ws_api_signature_params_are_sorted_and_do_not_include_signature_in_payload() -> None:
    params = crypto_ofim_stream._signed_ws_api_params(
        api_key="api-key",
        api_secret="secret",
        recv_window_ms=5000,
        now_ms=1700000000000,
    )

    expected_payload = "apiKey=api-key&recvWindow=5000&timestamp=1700000000000"
    expected_signature = hmac.new(
        b"secret",
        expected_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert params == {
        "apiKey": "api-key",
        "recvWindow": 5000,
        "timestamp": 1700000000000,
        "signature": expected_signature,
    }


def test_extract_user_stream_event_supports_ws_api_wrappers() -> None:
    event = {"e": "executionReport", "s": "BTCUSDT", "x": "TRADE"}

    assert crypto_ofim_stream._extract_user_stream_event(event) == event
    assert crypto_ofim_stream._extract_user_stream_event({"event": event}) == event
    assert crypto_ofim_stream._extract_user_stream_event({"data": event}) == event
    assert crypto_ofim_stream._extract_user_stream_event({"id": "1", "status": 200, "result": {"subscriptionId": 7}}) is None


def test_stream_events_skip_high_volume_debug_rows_by_default(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.delenv("CRYPTO_OFIM_STREAM_DEBUG_EVENTS", raising=False)

    crypto_ofim_stream._append_stream_event("trade", {"symbol": "BTCUSDT"})
    crypto_ofim_stream._append_stream_event("depth_delta", {"symbol": "BTCUSDT"})
    crypto_ofim_stream._append_stream_event("depth_gap", {"symbol": "BTCUSDT"})

    rows = [
        json.loads(line)
        for line in crypto_ofim_stream.STREAM_EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["event_type"] for row in rows] == ["depth_gap"]


def test_stream_events_can_enable_high_volume_debug_rows(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("CRYPTO_OFIM_STREAM_DEBUG_EVENTS", "true")

    crypto_ofim_stream._append_stream_event("trade", {"symbol": "BTCUSDT"})

    rows = [
        json.loads(line)
        for line in crypto_ofim_stream.STREAM_EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["event_type"] == "trade"


def test_stream_ws_max_queue_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("CRYPTO_OFIM_STREAM_WS_MAX_QUEUE", "999999")
    assert crypto_ofim_stream._stream_ws_max_queue() == 4096

    monkeypatch.setenv("CRYPTO_OFIM_STREAM_WS_MAX_QUEUE", "1")
    assert crypto_ofim_stream._stream_ws_max_queue() == 32


def test_stream_status_file_omits_heavy_books_and_trades(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    book = crypto_ofim_stream.LocalDepthBook(symbol="BTCUSDT")
    book.load_snapshot(
        {
            "lastUpdateId": 10,
            "bids": [["100.0", "1.0"]],
            "asks": [["101.0", "2.0"]],
        }
    )
    stream = object.__new__(crypto_ofim_stream.BinanceMarketStream)
    stream.settings = _settings(mode="testnet", use_ws_cache=True, symbols=("BTCUSDT",), benchmark="BTCUSDT")
    stream.depth_limit = 100
    stream.symbols = ["BTCUSDT"]
    stream.books = {"BTCUSDT": book}
    stream.trades = {"BTCUSDT": [{"price": 100.5, "volume": 1.0, "ticker_direction": "BUY"}]}
    stream.message_count = 12
    stream.user_event_count = 0
    stream.user_stream_status = "disabled"
    stream.last_user_event_at = None
    stream.started_at = "2026-01-01T00:00:00+00:00"

    stream.write_cache(status="running")

    cache = json.loads(crypto_ofim_stream.STREAM_CACHE_FILE.read_text(encoding="utf-8"))
    status = json.loads(crypto_ofim_stream.STREAM_STATUS_FILE.read_text(encoding="utf-8"))
    assert "books" in cache
    assert "trades" in cache
    assert "books" not in status
    assert "trades" not in status
    assert status["book_count"] == 1
    assert status["trade_buffer_count"] == 1


def test_ledger_epoch_filters_old_testnet_orders(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(mode="testnet", api_key="key", api_secret="secret", symbols=("BTCUSDT",), benchmark="BTCUSDT")
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    for ts, order_id, side, price in [
        ("2026-01-01T00:00:00+00:00", 1, "BUY", 100),
        ("2026-01-01T00:01:00+00:00", 2, "SELL", 110),
        ("2026-01-02T00:00:00+00:00", 3, "BUY", 200),
        ("2026-01-02T00:01:00+00:00", 4, "SELL", 230),
    ]:
        crypto_ofim._append_jsonl(
            crypto_ofim.ORDERS_FILE,
            {
                "ts": ts,
                "mode": "testnet",
                "symbol": "BTCUSDT",
                "side": side,
                "quantity": 1,
                "price": price,
                "fee": 0,
                "status": "submitted_testnet",
                "response": {
                    "orderId": order_id,
                    "executedQty": "1.00000000",
                    "cummulativeQuoteQty": f"{price:.8f}",
                    "fills": [{"price": f"{price:.8f}", "qty": "1.00000000", "commission": "0.00000000", "commissionAsset": "USDT"}],
                },
            },
        )
    crypto_ofim.LEDGER_EPOCH_FILE.write_text(
        json.dumps({"ts": "2026-01-02T00:00:00+00:00", "mode": "testnet", "quote_asset": "USDT"}),
        encoding="utf-8",
    )

    ledger = crypto_ofim._estimate_order_log_ledger("testnet", "USDT")

    assert ledger["trade_count"] == 2
    assert ledger["realized_pnl"] == 30.0


def test_testnet_account_snapshot_ignores_unexplained_faucet_balances(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(mode="testnet", api_key="key", api_secret="secret", symbols=("BTCUSDT",), benchmark="BTCUSDT")

    class FaucetClient(FakeBinanceClient):
        def account(self):
            return {
                "accountType": "SPOT",
                "balances": [
                    {"asset": "USDT", "free": "10000", "locked": "0"},
                    {"asset": "BTC", "free": "5", "locked": "0"},
                ],
            }

    engine = CryptoOfimEngine(settings, client=FaucetClient())

    snapshot = engine.account_snapshot()

    assert snapshot["positions"] == {}
    assert snapshot["market_value"] == 0.0
    assert snapshot["extra_balance_count"] == 1


def test_testnet_account_snapshot_clamps_positions_to_logged_strategy_fills(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(mode="testnet", api_key="key", api_secret="secret", symbols=("BTCUSDT",), benchmark="BTCUSDT")

    class FaucetClient(FakeBinanceClient):
        def account(self):
            return {
                "accountType": "SPOT",
                "balances": [
                    {"asset": "USDT", "free": "10000", "locked": "0"},
                    {"asset": "BTC", "free": "5", "locked": "0"},
                ],
            }

    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "mode": "testnet",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1,
            "price": 100,
            "fee": 0.1,
            "status": "submitted_testnet",
            "response": {
                "executedQty": "1.00000000",
                "cummulativeQuoteQty": "100.00000000",
                "fills": [{"price": "100.00000000", "qty": "1.00000000", "commission": "0.00000000", "commissionAsset": "BTC"}],
            },
        },
    )
    engine = CryptoOfimEngine(settings, client=FaucetClient())

    snapshot = engine.account_snapshot()

    assert snapshot["positions"] == {"BTCUSDT": 1.0}
    assert snapshot["market_value"] == 100.0
    assert snapshot["extra_balance_count"] == 0


def test_testnet_balance_audit_separates_faucet_assets_from_strategy_assets(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(mode="testnet", api_key="key", api_secret="secret", symbols=("BTCUSDT",), benchmark="BTCUSDT")

    class AuditClient(FakeBinanceClient):
        def account(self):
            return {
                "accountType": "SPOT",
                "balances": [
                    {"asset": "USDT", "free": "9900", "locked": "0"},
                    {"asset": "BTC", "free": "1", "locked": "0"},
                    {"asset": "FAKE", "free": "999", "locked": "0"},
                ],
            }

    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "mode": "testnet",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1,
            "price": 100,
            "fee": 0.0,
            "status": "submitted_testnet",
        },
    )
    engine = CryptoOfimEngine(settings, client=AuditClient())

    audit = engine.balance_audit()
    rows = {row["asset"]: row for row in audit["rows"]}

    assert rows["USDT"]["role"] == "QUOTE_CASH"
    assert rows["USDT"]["inferred_start_qty"] == 10000
    assert rows["BTC"]["role"] == "ACTIVE_UNIVERSE"
    assert rows["BTC"]["strategy_counted_qty"] == 1
    assert rows["FAKE"]["role"] == "TESTNET_UNUSED"
    assert rows["FAKE"]["ignored_testnet_qty"] == 999
    assert audit["summary"]["testnet_unused_count"] == 1


def test_testnet_account_snapshot_estimates_unrealized_pnl_from_order_log(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(mode="testnet", api_key="key", api_secret="secret", symbols=("BTCUSDT",), benchmark="BTCUSDT")

    class HoldingClient(FakeBinanceClient):
        def account(self):
            return {
                "accountType": "SPOT",
                "balances": [
                    {"asset": "USDT", "free": "9900", "locked": "0"},
                    {"asset": "BTC", "free": "1", "locked": "0"},
                ],
            }

        def book_ticker(self, symbol: str) -> pd.Series:
            return pd.Series({"last_price": 110.0, "bid_price": 109.99, "ask_price": 110.01})

    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "mode": "testnet",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1,
            "price": 100,
            "fee": 0.1,
            "status": "submitted_testnet",
        },
    )
    engine = CryptoOfimEngine(settings, client=HoldingClient())

    snapshot = engine.account_snapshot()

    assert snapshot["unrealized_pnl"] == 9.9


def test_testnet_account_snapshot_reports_cash_reconciliation_drift(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(mode="testnet", api_key="key", api_secret="secret", symbols=("BTCUSDT",), benchmark="BTCUSDT")
    crypto_ofim.LEDGER_EPOCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_ofim.LEDGER_EPOCH_FILE.write_text(
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "mode": "testnet", "quote_asset": "USDT", "balances": {"USDT": 1000.0}}),
        encoding="utf-8",
    )

    class CashShortfallClient(FakeBinanceClient):
        def account(self):
            return {
                "accountType": "SPOT",
                "balances": [
                    {"asset": "USDT", "free": "500", "locked": "0"},
                ],
            }

    engine = CryptoOfimEngine(settings, client=CashShortfallClient())

    snapshot = engine.account_snapshot()

    assert snapshot["cash_reconciliation"]["ok"] is False
    assert snapshot["cash_reconciliation"]["unexplained_quote_delta"] == -500.0
    assert any("cash reconciliation drift" in warning for warning in snapshot["ledger_warnings"])


def test_loss_guard_includes_cash_reconciliation_breach(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        mode="testnet",
        api_key="key",
        api_secret="secret",
        symbols=("BTCUSDT",),
        benchmark="BTCUSDT",
        loss_guard_max_loss=100.0,
        loss_guard_max_estimated_fees=0.0,
        loss_guard_max_trades=0,
    )
    crypto_ofim.LEDGER_EPOCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_ofim.LEDGER_EPOCH_FILE.write_text(
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "mode": "testnet", "quote_asset": "USDT", "balances": {"USDT": 1000.0}}),
        encoding="utf-8",
    )

    class CashShortfallClient(FakeBinanceClient):
        def account(self):
            return {
                "accountType": "SPOT",
                "balances": [
                    {"asset": "USDT", "free": "500", "locked": "0"},
                ],
            }

    engine = CryptoOfimEngine(settings, client=CashShortfallClient())
    state = engine._load_state_for_mode()

    plan = engine.generate_plan(state, cycle_id="cash-reconciliation-loss-guard")

    assert plan.target_weights == {}
    assert "cash_reconciliation" in plan.benchmark_trend["breaches"]
    assert plan.benchmark_trend["cash_reconciliation"]["unexplained_quote_delta"] == -500.0


def test_loss_guard_blocks_material_position_reconciliation_break(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        mode="testnet",
        api_key="key",
        api_secret="secret",
        symbols=("BTCUSDT",),
        benchmark="BTCUSDT",
        min_order_notional=20.0,
        loss_guard_max_loss=500.0,
        loss_guard_max_estimated_fees=0.0,
        loss_guard_max_trades=0,
    )
    crypto_ofim.LEDGER_EPOCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_ofim.LEDGER_EPOCH_FILE.write_text(
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "mode": "testnet", "quote_asset": "USDT", "balances": {"USDT": 1000.0}}),
        encoding="utf-8",
    )
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": "2026-01-01T00:01:00+00:00",
            "mode": "testnet",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1,
            "price": 100,
            "fee": 0.0,
            "status": "submitted_testnet",
            "response": {
                "executedQty": "1.00000000",
                "cummulativeQuoteQty": "100.00000000",
                "fills": [{"price": "100.00000000", "qty": "1.00000000", "commission": "0.00000000", "commissionAsset": "BTC"}],
            },
        },
    )

    class MissingPositionClient(FakeBinanceClient):
        def account(self):
            return {
                "accountType": "SPOT",
                "balances": [
                    {"asset": "USDT", "free": "900", "locked": "0"},
                    {"asset": "BTC", "free": "0", "locked": "0"},
                ],
            }

        def book_ticker(self, symbol: str) -> pd.Series:
            return pd.Series({"last_price": 100.0, "bid_price": 99.99, "ask_price": 100.01})

    engine = CryptoOfimEngine(settings, client=MissingPositionClient())
    state = engine._load_state_for_mode()

    plan = engine.generate_plan(state, cycle_id="position-reconciliation-loss-guard")

    assert plan.target_weights == {}
    assert plan.reason == "loss_guard_position_reconciliation"
    assert plan.benchmark_trend["breaches"] == ["position_reconciliation"]
    assert plan.benchmark_trend["position_reconciliation"][0]["symbol"] == "BTCUSDT"
    assert plan.benchmark_trend["position_reconciliation"][0]["estimated_notional"] == 100.0


def test_account_loss_guard_keeps_learning_loss_context(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        mode="testnet",
        api_key="key",
        api_secret="secret",
        symbols=("BTCUSDT",),
        benchmark="BTCUSDT",
        loss_guard_max_loss=100.0,
        loss_guard_max_estimated_fees=10.0,
        loss_guard_max_trades=120,
    )
    crypto_ofim.LEDGER_EPOCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_ofim.LEDGER_EPOCH_FILE.write_text(
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "mode": "testnet", "quote_asset": "USDT", "balances": {"USDT": 1000.0}}),
        encoding="utf-8",
    )
    crypto_ofim.ATTRIBUTION_FILE.write_text(
        json.dumps(
            {
                "generated_at": "2026-05-12T03:32:40+00:00",
                "total": {"net_pnl": -55_545.09, "estimated_fees": 45_287.30, "trades": 11_389},
            }
        ),
        encoding="utf-8",
    )

    class CashShortfallClient(FakeBinanceClient):
        def account(self):
            return {
                "accountType": "SPOT",
                "balances": [
                    {"asset": "USDT", "free": "500", "locked": "0"},
                ],
            }

    engine = CryptoOfimEngine(settings, client=CashShortfallClient())
    state = engine._load_state_for_mode()

    plan = engine.generate_plan(state, cycle_id="account-and-learning-loss-guard")

    assert plan.reason == "loss_guard_loss_cash_reconciliation"
    assert plan.benchmark_trend["cash_reconciliation"]["unexplained_quote_delta"] == -500.0
    learning = plan.benchmark_trend["learning_loss_guard"]
    assert learning["source"] == "crypto_attribution"
    assert learning["reason"] == "loss_guard_learning_loss_estimated_fees_trade_count"
    assert learning["estimated_fees"] == 45_287.3


def test_testnet_account_snapshot_reports_missing_logged_position(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(mode="testnet", api_key="key", api_secret="secret", symbols=("BTCUSDT",), benchmark="BTCUSDT")
    crypto_ofim.LEDGER_EPOCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_ofim.LEDGER_EPOCH_FILE.write_text(
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "mode": "testnet", "quote_asset": "USDT", "balances": {"USDT": 1000.0}}),
        encoding="utf-8",
    )
    crypto_ofim.ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_ofim._append_jsonl(
        crypto_ofim.ORDERS_FILE,
        {
            "ts": "2026-01-01T00:01:00+00:00",
            "mode": "testnet",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 1,
            "price": 100,
            "fee": 0.0,
            "status": "submitted_testnet",
            "response": {
                "executedQty": "1.00000000",
                "cummulativeQuoteQty": "100.00000000",
                "fills": [{"price": "100.00000000", "qty": "1.00000000", "commission": "0.00000000", "commissionAsset": "BTC"}],
            },
        },
    )

    class MissingPositionClient(FakeBinanceClient):
        def account(self):
            return {
                "accountType": "SPOT",
                "balances": [
                    {"asset": "USDT", "free": "900", "locked": "0"},
                    {"asset": "BTC", "free": "0", "locked": "0"},
                ],
            }

    engine = CryptoOfimEngine(settings, client=MissingPositionClient())

    snapshot = engine.account_snapshot()

    assert snapshot["position_reconciliation"] == [
        {"symbol": "BTCUSDT", "ledger_qty": 1.0, "strategy_counted_qty": 0.0, "missing_qty": 1.0, "extra_qty": 0.0}
    ]
    assert any("position shortfall" in warning for warning in snapshot["ledger_warnings"])


def test_run_once_marks_binance_timeout_as_transient(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings()
    engine = CryptoOfimEngine(settings, client=FakeTransientNetworkClient())

    with pytest.raises(CryptoOfimError):
        engine.run_once(submit=True)

    payload = json.loads(crypto_ofim.STATUS_FILE.read_text(encoding="utf-8"))
    assert payload["status"] == "transient_error"
    assert payload["updated_at"]
    assert payload["market_data"] == settings.market_data
    assert payload["market_data_base_url"] == settings.market_data_base_url
    assert payload["execution_base_url"] == settings.base_url
    assert payload["strategy_settings"]["entry_threshold"] == settings.entry_threshold
    assert "自动重试" in payload["error"]
    assert _is_transient_network_message(payload["raw_error"])


def test_local_depth_book_applies_updates_and_detects_gaps() -> None:
    book = crypto_ofim_stream.LocalDepthBook(symbol="BTCUSDT")
    book.load_snapshot(
        {
            "lastUpdateId": 10,
            "bids": [["100.0", "1.0"]],
            "asks": [["101.0", "2.0"]],
        }
    )

    assert book.apply_depth_update({"U": 8, "u": 10, "b": [], "a": []}) == "stale"
    assert book.apply_depth_update({"U": 11, "u": 12, "b": [["100.5", "3.0"]], "a": [["101.0", "0"]]}) == "applied"
    snapshot = book.snapshot(limit=10)

    assert snapshot["last_update_id"] == 12
    assert snapshot["best_bid"] == 100.5
    assert snapshot["Ask"] == []
    assert book.apply_depth_update({"U": 20, "u": 21, "b": [], "a": []}) == "gap"
    assert book.gap_count == 1


def test_crypto_stream_always_uses_mainnet_market_stream_with_testnet_user_stream() -> None:
    stream = object.__new__(crypto_ofim_stream.BinanceMarketStream)
    stream.settings = _settings(
        mode="testnet",
        base_url=crypto_ofim.TESTNET_BASE_URL,
        api_key="key",
        api_secret="secret",
        market_data="testnet",
    )

    assert stream.stream_base_url == crypto_ofim_stream.PROD_STREAM_BASE_URL
    assert stream.user_stream_base_url == crypto_ofim_stream.TESTNET_USER_STREAM_BASE_URL
    assert stream.ws_api_base_url == crypto_ofim_stream.TESTNET_WS_API_BASE_URL


def test_generate_plan_prefers_fresh_ws_cache(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(use_ws_cache=True, symbols=("BTCUSDT",), benchmark="BTCUSDT", hot_universe=False, max_positions=1)
    crypto_ofim_stream.STREAM_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_ofim_stream.STREAM_CACHE_FILE.write_text(
        json.dumps(
            {
                "status": "running",
                "market_data": "mainnet",
                "books": {
                    "BTCUSDT": {
                        "best_bid": 100.0,
                        "best_ask": 100.1,
                        "mid": 100.05,
                        "Bid": [[100.0, 100.0], [99.9, 100.0]],
                        "Ask": [[100.1, 10.0], [100.2, 10.0]],
                    }
                },
                "trades": {
                    "BTCUSDT": [
                        {"price": 100.05, "volume": 1.0, "ticker_direction": "BUY"},
                        {"price": 100.06, "volume": 2.0, "ticker_direction": "BUY"},
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class NoRestDepthClient(FakeBinanceClient):
        def depth(self, symbol: str, *, limit: int = 100):
            raise AssertionError("depth should come from ws cache")

        def recent_trades(self, symbol: str, *, limit: int = 100) -> pd.DataFrame:
            raise AssertionError("trades should come from ws cache")

        def book_ticker(self, symbol: str) -> pd.Series:
            raise AssertionError("book ticker should come from ws cache")

    plan = CryptoOfimEngine(settings, client=NoRestDepthClient()).generate_plan(CryptoPaperState.fresh(settings))

    assert plan.market_sources == {"BTCUSDT": "ws_cache"}
    assert plan.features[0].last_price == 100.05


def test_generate_plan_ignores_ws_cache_from_other_market_data(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(use_ws_cache=True, symbols=("BTCUSDT",), benchmark="BTCUSDT", hot_universe=False, max_positions=1, market_data="mainnet")
    crypto_ofim_stream.STREAM_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    crypto_ofim_stream.STREAM_CACHE_FILE.write_text(
        json.dumps(
            {
                "status": "running",
                "market_data": "testnet",
                "books": {
                    "BTCUSDT": {
                        "best_bid": 1.0,
                        "best_ask": 1.1,
                        "mid": 1.05,
                        "Bid": [[1.0, 100.0]],
                        "Ask": [[1.1, 10.0]],
                    }
                },
                "trades": {"BTCUSDT": [{"price": 1.05, "volume": 1.0, "ticker_direction": "BUY"}]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    plan = CryptoOfimEngine(settings, client=FakeBinanceClient()).generate_plan(CryptoPaperState.fresh(settings))

    assert plan.market_sources == {"BTCUSDT": "rest"}
    assert plan.features[0].last_price == 100.0


def test_generate_plan_uses_separate_market_data_client_for_rest_fallback(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    settings = _settings(
        mode="testnet",
        api_key="key",
        api_secret="secret",
        use_ws_cache=False,
        symbols=("BTCUSDT",),
        benchmark="BTCUSDT",
        hot_universe=False,
        max_positions=1,
        market_data="mainnet",
    )

    class ExecutionOnlyClient(FakeBinanceClient):
        def tickers_24h(self):
            raise AssertionError("hot universe should not use execution client")

        def book_ticker(self, symbol: str) -> pd.Series:
            raise AssertionError("market data should not use execution client")

        def klines(self, symbol: str, *, interval: str = "1m", limit: int = 60) -> pd.DataFrame:
            raise AssertionError("market data should not use execution client")

        def depth(self, symbol: str, *, limit: int = 100):
            raise AssertionError("market data should not use execution client")

        def recent_trades(self, symbol: str, *, limit: int = 100) -> pd.DataFrame:
            raise AssertionError("market data should not use execution client")

    plan = CryptoOfimEngine(
        settings,
        client=ExecutionOnlyClient(),
        market_client=FakeBinanceClient(),
    ).generate_plan(CryptoPaperState.fresh(settings))

    assert plan.market_sources == {"BTCUSDT": "rest"}
    assert plan.features[0].last_price == 100.0
