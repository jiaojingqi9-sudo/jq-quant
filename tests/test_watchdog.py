from datetime import UTC, datetime, timedelta
from pathlib import Path
import json

from taa_futu.config import Settings
from taa_futu.watchdog import _auto_trader_health, _next_sleep_seconds


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


def test_next_sleep_seconds_stays_inside_market_range() -> None:
    settings = _settings()
    for _ in range(20):
        interval = _next_sleep_seconds(settings, market_open=True)
        assert settings.watchdog_min_interval_seconds <= interval <= settings.watchdog_max_interval_seconds


def test_auto_trader_health_flags_error_action(monkeypatch, tmp_path: Path) -> None:
    pid_file = tmp_path / "auto_trader.pid"
    status_file = tmp_path / "auto_trader_status.json"
    pid_file.write_text("12345", encoding="utf-8")
    status_file.write_text(
        json.dumps(
            {
                "running": True,
                "updated_at": datetime.now(UTC).isoformat(),
                "action": "error",
                "detail": "boom",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("taa_futu.watchdog.AUTO_TRADER_PID_FILE", pid_file)
    monkeypatch.setattr("taa_futu.watchdog.AUTO_TRADER_STATUS_FILE", status_file)
    monkeypatch.setattr("taa_futu.watchdog._is_pid_running", lambda pid: True)

    healthy, detail, _payload = _auto_trader_health(_settings(), datetime.now(UTC))

    assert healthy is False
    assert "auto_trader_error" in detail


def test_auto_trader_health_treats_transient_error_action_as_healthy(monkeypatch, tmp_path: Path) -> None:
    pid_file = tmp_path / "auto_trader.pid"
    status_file = tmp_path / "auto_trader_status.json"
    pid_file.write_text("12345", encoding="utf-8")
    status_file.write_text(
        json.dumps(
            {
                "running": True,
                "updated_at": datetime.now(UTC).isoformat(),
                "action": "error",
                "detail": "PacketErr.Timeout",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("taa_futu.watchdog.AUTO_TRADER_PID_FILE", pid_file)
    monkeypatch.setattr("taa_futu.watchdog.AUTO_TRADER_STATUS_FILE", status_file)
    monkeypatch.setattr("taa_futu.watchdog._is_pid_running", lambda pid: True)

    healthy, detail, _payload = _auto_trader_health(_settings(), datetime.now(UTC))

    assert healthy is True
    assert "transient_error" in detail


def test_auto_trader_health_flags_stale_status(monkeypatch, tmp_path: Path) -> None:
    pid_file = tmp_path / "auto_trader.pid"
    status_file = tmp_path / "auto_trader_status.json"
    pid_file.write_text("12345", encoding="utf-8")
    status_file.write_text(
        json.dumps(
            {
                "running": True,
                "updated_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
                "action": "monitoring",
                "detail": "ok",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("taa_futu.watchdog.AUTO_TRADER_PID_FILE", pid_file)
    monkeypatch.setattr("taa_futu.watchdog.AUTO_TRADER_STATUS_FILE", status_file)
    monkeypatch.setattr("taa_futu.watchdog._is_pid_running", lambda pid: True)

    healthy, detail, _payload = _auto_trader_health(_settings(), datetime.now(UTC))

    assert healthy is False
    assert "stale" in detail
