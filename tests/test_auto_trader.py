from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pandas as pd

from taa_futu.auto_trader import (
    AutoTraderState,
    _apply_cycle_turnover_cap,
    _is_transient_runtime_error,
    _load_state,
    _loss_guard_breached,
    _market_window_state,
    _order_signature,
    _record_new_fills,
    _risk_adjust_target_weights,
    run_cycle,
    validate_auto_trader_mode,
)
from taa_futu.config import Settings
from taa_futu.futu_gateway import PlannedOrder


def _settings() -> Settings:
    return Settings(
        symbols=("US.SPY",),
        benchmark="US.SPY",
        start_date="2020-01-01",
        lookback_months=10,
        signal_timezone="America/New_York",
        fusion_universe=("US.SPY", "US.QQQ"),
        fusion_benchmark="US.SPY",
        fusion_lookback_bars=60,
        fusion_opening_range_minutes=15,
        fusion_top_k=3,
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
    )


def _real_settings() -> Settings:
    settings = _settings()
    return Settings(
        **{
            **settings.__dict__,
            "futu_trd_env": "REAL",
        }
    )


def test_market_window_detects_rth() -> None:
    market_open, detail = _market_window_state(datetime(2026, 3, 10, 15, 0, tzinfo=UTC), _settings())
    assert market_open is True
    assert "inside_window" in detail


def test_order_signature_is_stable() -> None:
    orders = [
        PlannedOrder("US.SPY", "BUY", 100, 600.0, 599.0, 0, 100, 0.5),
        PlannedOrder("US.QQQ", "SELL", 50, 500.0, 501.0, 50, 0, 0.0),
    ]
    reverse_orders = list(reversed(orders))
    assert _order_signature(orders) == _order_signature(reverse_orders)


def test_validate_auto_trader_mode_blocks_real_submit_without_opt_in() -> None:
    try:
        validate_auto_trader_mode(_real_settings(), submit=True)
    except SystemExit as exc:
        assert "FUTU_ENABLE_REAL_TRADING" in str(exc)
    else:  # pragma: no cover - defensive branch
        raise AssertionError("REAL submit should be blocked without explicit opt-in")


def test_transient_runtime_error_detects_timeout_markers() -> None:
    assert _is_transient_runtime_error("PacketErr.Timeout")
    assert _is_transient_runtime_error("place_order failed after 4 attempts: timed out")
    assert _is_transient_runtime_error("position_list_query failed: 此数据暂时还未准备好")
    assert _is_transient_runtime_error("subscribe_realtime failed: 拉取美股夜盘状态失败。")
    assert not _is_transient_runtime_error("Configured FUTU_ACC_ID=1 not found.")


class _FillHistoryTrader:
    def get_order_history(self, _acc_id: int, _start: str, _end: str) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "order_id": "42",
                    "code": "US.SPY",
                    "trd_side": "BUY",
                    "dealt_qty": 10,
                    "dealt_avg_price": 100.0,
                    "updated_time": "2026-03-10 10:30:00",
                }
            ]
        )


def test_record_new_fills_appends_deduped_stock_fill_log(monkeypatch, tmp_path: Path) -> None:
    from taa_futu import auto_trader

    fills = tmp_path / "stock_fills.jsonl"
    monkeypatch.setattr(auto_trader, "STOCK_FILLS_FILE", fills)
    state = AutoTraderState()

    first = _record_new_fills(_FillHistoryTrader(), 1, _settings(), state, now_utc=datetime(2026, 3, 10, 15, 0, tzinfo=UTC))
    second = _record_new_fills(_FillHistoryTrader(), 1, _settings(), state, now_utc=datetime(2026, 3, 10, 15, 1, tzinfo=UTC))

    assert first == 1
    assert second == 0
    rows = [line for line in fills.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert "futu_fill:42:10.00000000" in rows[0]
    assert state.recorded_order_fill_qty["42"] == 10


class _PartialFillHistoryTrader:
    def __init__(self) -> None:
        self.cumulative_qty = 5
        self.avg_price = 100.0

    def get_order_history(self, _acc_id: int, _start: str, _end: str) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "order_id": "43",
                    "code": "US.SPY",
                    "trd_side": "BUY",
                    "dealt_qty": self.cumulative_qty,
                    "dealt_avg_price": self.avg_price,
                    "updated_time": "2026-03-10 10:30:00",
                }
            ]
        )


def test_record_new_fills_records_incremental_partial_fills(monkeypatch, tmp_path: Path) -> None:
    from taa_futu import auto_trader

    fills = tmp_path / "stock_fills.jsonl"
    monkeypatch.setattr(auto_trader, "STOCK_FILLS_FILE", fills)
    state = AutoTraderState()
    trader = _PartialFillHistoryTrader()

    first = _record_new_fills(trader, 1, _settings(), state, now_utc=datetime(2026, 3, 10, 15, 0, tzinfo=UTC))
    trader.cumulative_qty = 10
    trader.avg_price = 101.0
    second = _record_new_fills(trader, 1, _settings(), state, now_utc=datetime(2026, 3, 10, 15, 1, tzinfo=UTC))

    rows = [json.loads(line) for line in fills.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert first == 1
    assert second == 1
    assert [row["quantity"] for row in rows] == [5.0, 5.0]
    assert rows[1]["price"] == 102.0
    assert state.recorded_order_fill_qty["43"] == 10


def test_load_state_restores_persistent_state_without_recursion(monkeypatch, tmp_path: Path) -> None:
    from taa_futu import auto_trader

    state_file = tmp_path / "auto_trader_state.json"
    fills = tmp_path / "stock_fills.jsonl"
    state_file.write_text(
        """
        {
          "last_signature": "abc",
          "last_submit_at": "2026-03-10T15:00:00+00:00",
          "position_entry_times": {"US.SPY": "2026-03-10T14:55:00+00:00"},
          "submitted_order_sources": {"42": "Fusion"},
          "exit_signal_counts": {"US.SPY": 2},
          "last_symbol_trade_time": {"US.SPY": "2026-03-10T15:00:00+00:00"}
        }
        """,
        encoding="utf-8",
    )
    fills.write_text(
        '{"event_id":"futu_order:42","order_id":"42","symbol":"US.SPY","side":"BUY","quantity":1,"price":100}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(auto_trader, "AUTO_TRADER_STATE_FILE", state_file)
    monkeypatch.setattr(auto_trader, "STOCK_FILLS_FILE", fills)

    state = _load_state()

    assert state.last_signature == "abc"
    assert state.last_submit_at == datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
    assert state.position_entry_times["US.SPY"] == datetime(2026, 3, 10, 14, 55, tzinfo=UTC)
    assert state.submitted_order_sources["42"] == "Fusion"
    assert state.exit_signal_counts["US.SPY"] == 2
    assert "futu_order:42" in state.recorded_fill_ids
    assert state.recorded_order_fill_qty["42"] == 1.0


def test_risk_adjust_target_weights_caps_and_scales() -> None:
    settings = type(_settings())(
        **{
            **_settings().__dict__,
            "auto_trader_max_target_weight": 0.50,
            "auto_trader_max_target_gross_exposure": 0.80,
        }
    )

    adjusted = _risk_adjust_target_weights({"US.SPY": 0.90, "US.QQQ": 0.50, "US.BAD": float("nan")}, settings)

    assert set(adjusted) == {"US.SPY", "US.QQQ"}
    assert adjusted["US.SPY"] == adjusted["US.QQQ"]
    assert sum(adjusted.values()) <= 0.80 + 1e-9


def test_cycle_turnover_cap_preserves_full_exit_and_skips_new_risk() -> None:
    settings = type(_settings())(**{**_settings().__dict__, "auto_trader_max_cycle_turnover_usd": 1_000.0})
    orders = [
        PlannedOrder("US.OLD", "SELL", 100, 50.0, 50.0, 100, 0, 0.0),
        PlannedOrder("US.SPY", "BUY", 20, 60.0, 60.0, 0, 20, 0.5),
        PlannedOrder("US.QQQ", "BUY", 10, 80.0, 80.0, 0, 10, 0.3),
    ]

    capped = _apply_cycle_turnover_cap(orders, settings)

    assert [order.code for order in capped] == ["US.OLD", "US.QQQ"]


def test_loss_guard_breaches_when_epoch_loss_limit_hit(monkeypatch, tmp_path: Path) -> None:
    from taa_futu import auto_trader

    epoch = tmp_path / "stock_ledger_epoch.json"
    epoch.write_text(
        json.dumps({"account_snapshot": {"total_assets": 1000.0}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(auto_trader, "STOCK_LEDGER_EPOCH_FILE", epoch)
    settings = type(_settings())(**{**_settings().__dict__, "auto_trader_max_epoch_loss_usd": 100.0})

    breached, detail = _loss_guard_breached(pd.Series({"total_assets": 850.0}), settings)

    assert breached is True
    assert "loss_usd" in detail


class _CycleTrader:
    last_ignore_symbols: set[str] = set()

    def __init__(self, _settings) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def resolve_trade_account(self) -> int:
        return 1

    def get_order_history(self, _acc_id: int, _start: str, _end: str) -> pd.DataFrame:
        return pd.DataFrame()

    def get_open_orders(self, _acc_id: int) -> pd.DataFrame:
        return pd.DataFrame()

    def get_positions(self, _acc_id: int) -> pd.DataFrame:
        return pd.DataFrame([{"code": "US.SPY", "qty": 10, "can_sell_qty": 10, "market_val": 1000.0}])

    def plan_rebalance(self, _target_weights, *, ignore_symbols=None):
        self.__class__.last_ignore_symbols = set(ignore_symbols or set())
        if "US.SPY" in self.__class__.last_ignore_symbols:
            return pd.Series({"total_assets": 1000.0}), []
        return pd.Series({"total_assets": 1000.0}), [
            PlannedOrder("US.SPY", "SELL", 10, 99.0, 100.0, 10, 0, 0.0)
        ]


def test_run_cycle_requires_exit_confirmation_before_sell(monkeypatch) -> None:
    from taa_futu import auto_trader

    settings = _settings()
    settings = type(settings)(**{**settings.__dict__, "auto_trader_exit_confirm_cycles": 2})
    state = AutoTraderState()
    monkeypatch.setattr(auto_trader, "FutuPaperTrader", _CycleTrader)
    monkeypatch.setattr(auto_trader, "_market_window_state", lambda _now, _settings: (True, "inside_window"))
    monkeypatch.setattr(auto_trader, "_strategy_stack_target_weights", lambda *_args: ({}, {}, {"US.SPY": "Fusion"}))

    action1, _detail1 = run_cycle(settings, state, submit=False)
    action2, _detail2 = run_cycle(settings, state, submit=False)

    assert action1 == "monitoring"
    assert state.exit_signal_counts["US.SPY"] >= 1
    assert action2 == "planned"


class _CooldownTrader(_CycleTrader):
    def get_positions(self, _acc_id: int) -> pd.DataFrame:
        return pd.DataFrame()

    def plan_rebalance(self, _target_weights, *, ignore_symbols=None):
        self.__class__.last_ignore_symbols = set(ignore_symbols or set())
        return pd.Series({"total_assets": 1000.0}), [
            PlannedOrder("US.SPY", "BUY", 10, 101.0, 100.0, 0, 10, 1.0)
        ]


def test_run_cycle_skips_symbol_inside_per_symbol_cooldown(monkeypatch) -> None:
    from taa_futu import auto_trader

    settings = _settings()
    settings = type(settings)(**{**settings.__dict__, "auto_trader_min_symbol_interval_seconds": 300})
    state = AutoTraderState()
    state.last_symbol_trade_time["US.SPY"] = datetime.now(UTC) - timedelta(seconds=60)
    monkeypatch.setattr(auto_trader, "FutuPaperTrader", _CooldownTrader)
    monkeypatch.setattr(auto_trader, "_market_window_state", lambda _now, _settings: (True, "inside_window"))
    monkeypatch.setattr(auto_trader, "_strategy_stack_target_weights", lambda *_args: ({"US.SPY": 1.0}, {}, {"US.SPY": "Fusion"}))

    action, detail = run_cycle(settings, state, submit=False)

    assert action == "monitoring"
    assert "no_rebalance_needed" in detail


# ───── New tests for OpenD half-dead short-circuit + cumulative counters ─────


def test_state_has_new_counters_and_lockdown_field() -> None:
    """AutoTraderState carries cumulative + consecutive_transient fields."""
    state = AutoTraderState()
    assert state.cumulative_planned_orders == 0
    assert state.cumulative_submitted_orders == 0
    assert state.cumulative_recorded_fills == 0
    assert state.consecutive_transient_count == 0


def test_settings_has_max_consecutive_transient() -> None:
    """The new short-circuit threshold exists on Settings with a non-negative value."""
    settings = _settings()
    assert getattr(settings, "auto_trader_max_consecutive_transient", -1) >= 0


def test_state_roundtrip_persists_cumulative_and_lockdown_counters(tmp_path, monkeypatch) -> None:
    """Cumulative counters must survive a process restart (save → load)."""
    from taa_futu import auto_trader

    state_file = tmp_path / "auto_trader_state.json"
    monkeypatch.setattr(auto_trader, "AUTO_TRADER_STATE_FILE", state_file)
    # Also point STOCK_FILLS_FILE at an empty location so _load_state's
    # rehydration of recorded_fill_ids does not error on a missing file.
    monkeypatch.setattr(auto_trader, "STOCK_FILLS_FILE", tmp_path / "stock_fills.jsonl")

    state = AutoTraderState()
    state.cumulative_planned_orders = 17
    state.cumulative_submitted_orders = 12
    state.cumulative_recorded_fills = 5
    state.consecutive_transient_count = 2
    auto_trader._save_state(state)

    loaded = auto_trader._load_state()
    assert loaded.cumulative_planned_orders == 17
    assert loaded.cumulative_submitted_orders == 12
    assert loaded.cumulative_recorded_fills == 5
    assert loaded.consecutive_transient_count == 2


def test_status_payload_exposes_last_cycle_and_cumulative(tmp_path, monkeypatch) -> None:
    """status JSON must publish both the explicit cycle/cumulative names AND
    the legacy aliases so existing dashboards do not break."""
    from taa_futu import auto_trader

    status_file = tmp_path / "auto_trader_status.json"
    monkeypatch.setattr(auto_trader, "AUTO_TRADER_STATUS_FILE", status_file)
    monkeypatch.setattr(auto_trader, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(auto_trader, "AUTO_TRADER_LOG_FILE", tmp_path / "auto_trader.log")

    state = AutoTraderState()
    state.last_planned_order_count = 4
    state.last_submitted_order_count = 3
    state.last_recorded_fill_count = 2
    state.cumulative_planned_orders = 40
    state.cumulative_submitted_orders = 30
    state.cumulative_recorded_fills = 20
    state.consecutive_transient_count = 1

    auto_trader._write_status(
        running=True,
        action="monitoring",
        detail="test",
        market_open=True,
        settings=_settings(),
        state=state,
    )
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    # Explicit new names
    assert payload["last_cycle_planned"] == 4
    assert payload["last_cycle_submitted"] == 3
    assert payload["last_cycle_recorded_fills"] == 2
    assert payload["cumulative_planned_orders"] == 40
    assert payload["cumulative_submitted_orders"] == 30
    assert payload["cumulative_recorded_fills"] == 20
    assert payload["consecutive_transient_count"] == 1
    # Legacy aliases must still mirror last-cycle values
    assert payload["planned_order_count"] == 4
    assert payload["submitted_order_count"] == 3
    assert payload["last_recorded_fill_count"] == 2


def test_consecutive_transient_count_loads_from_legacy_state(tmp_path, monkeypatch) -> None:
    """A non-zero counter loaded from disk reflects the prior process's state."""
    from taa_futu import auto_trader

    state_file = tmp_path / "auto_trader_state.json"
    state_file.write_text(json.dumps({
        "last_signature": "",
        "last_submit_at": None,
        "position_entry_times": {},
        "submitted_order_sources": {},
        "recorded_order_fill_qty": {},
        "recorded_order_fill_notional": {},
        "recorded_order_fill_fees": {},
        "exit_signal_counts": {},
        "last_symbol_trade_time": {},
        "cumulative_planned_orders": 8,
        "cumulative_submitted_orders": 6,
        "cumulative_recorded_fills": 4,
        "consecutive_transient_count": 3,
    }), encoding="utf-8")
    monkeypatch.setattr(auto_trader, "AUTO_TRADER_STATE_FILE", state_file)
    monkeypatch.setattr(auto_trader, "STOCK_FILLS_FILE", tmp_path / "stock_fills.jsonl")
    loaded = auto_trader._load_state()
    assert loaded.consecutive_transient_count == 3
    assert loaded.cumulative_planned_orders == 8


def test_old_state_file_missing_new_fields_loads_with_defaults(tmp_path, monkeypatch) -> None:
    """Older state files (without cumulative_*) must still load with zeros."""
    from taa_futu import auto_trader

    state_file = tmp_path / "auto_trader_state.json"
    state_file.write_text(json.dumps({
        "last_signature": "",
        "last_submit_at": None,
        "position_entry_times": {},
        "submitted_order_sources": {},
        "recorded_order_fill_qty": {},
        "recorded_order_fill_notional": {},
        "recorded_order_fill_fees": {},
        "exit_signal_counts": {},
        "last_symbol_trade_time": {},
    }), encoding="utf-8")
    monkeypatch.setattr(auto_trader, "AUTO_TRADER_STATE_FILE", state_file)
    monkeypatch.setattr(auto_trader, "STOCK_FILLS_FILE", tmp_path / "stock_fills.jsonl")
    loaded = auto_trader._load_state()
    assert loaded.consecutive_transient_count == 0
    assert loaded.cumulative_planned_orders == 0
    assert loaded.cumulative_submitted_orders == 0
    assert loaded.cumulative_recorded_fills == 0


def test_healthcheck_existence_on_futu_paper_trader() -> None:
    """The OpenD half-dead probe is reachable from FutuPaperTrader."""
    from taa_futu.futu_gateway import FutuPaperTrader

    assert hasattr(FutuPaperTrader, "healthcheck")
    assert callable(FutuPaperTrader.healthcheck)
