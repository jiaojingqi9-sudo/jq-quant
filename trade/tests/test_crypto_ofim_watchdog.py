from __future__ import annotations

from datetime import UTC, datetime, timedelta
import errno
import json
import os
import plistlib
from pathlib import Path
import time
from types import SimpleNamespace

from taa_futu import crypto_ofim_watchdog


def _patch_runtime(monkeypatch, tmp_path: Path) -> Path:
    runtime = tmp_path / "crypto_ofim"
    monkeypatch.setattr(crypto_ofim_watchdog, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(crypto_ofim_watchdog, "AUTO_PID_FILE", runtime / "auto.pid")
    monkeypatch.setattr(crypto_ofim_watchdog, "EVENTS_FILE", runtime / "events.jsonl")
    monkeypatch.setattr(crypto_ofim_watchdog, "STATUS_FILE", runtime / "status.json")
    monkeypatch.setattr(crypto_ofim_watchdog, "WATCHDOG_PID_FILE", runtime / "watchdog.pid")
    monkeypatch.setattr(crypto_ofim_watchdog, "WATCHDOG_LOG_FILE", runtime / "watchdog.log")
    monkeypatch.setattr(crypto_ofim_watchdog, "WATCHDOG_STATUS_FILE", runtime / "watchdog_status.json")
    monkeypatch.setattr(crypto_ofim_watchdog, "STREAM_PID_FILE", runtime / "stream.pid")
    monkeypatch.setattr(crypto_ofim_watchdog, "STREAM_CACHE_FILE", runtime / "ws_cache.json")
    monkeypatch.setattr(crypto_ofim_watchdog, "STREAM_STATUS_FILE", runtime / "stream_status.json")
    monkeypatch.setattr(crypto_ofim_watchdog, "APP_PID_FILE", runtime / "app.pid")
    monkeypatch.setattr(crypto_ofim_watchdog, "APP_LOG_FILE", runtime / "app.log")
    monkeypatch.setattr(crypto_ofim_watchdog, "APP_LAUNCH_AGENT_PLIST", runtime / "app.plist")
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_listening_on_port", lambda port: 0)
    monkeypatch.setattr(crypto_ofim_watchdog, "PERP_AUTO_PID_FILE", runtime / "perp_auto.pid")
    monkeypatch.setattr(crypto_ofim_watchdog, "PERP_AUTO_LOG_FILE", runtime / "perp_auto.log")
    monkeypatch.setattr(crypto_ofim_watchdog, "PERP_STATUS_FILE", runtime / "perp_status.json")
    return runtime


def test_watchdog_pid_running_treats_eperm_as_running(monkeypatch) -> None:
    def _raise_eperm(_pid: int, _sig: int) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(crypto_ofim_watchdog.os, "kill", _raise_eperm)

    assert crypto_ofim_watchdog._pid_running(123) is True


def test_watchdog_pid_running_treats_ps_denial_as_running(monkeypatch) -> None:
    monkeypatch.setattr(crypto_ofim_watchdog.os, "kill", lambda _pid, _sig: None)

    class _Denied:
        returncode = 126
        stdout = ""

    monkeypatch.setattr(crypto_ofim_watchdog.subprocess, "run", lambda *_args, **_kwargs: _Denied())

    assert crypto_ofim_watchdog._pid_running(123) is True


def test_auto_health_treats_empty_target_planned_as_healthy(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 123)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "status": "planned",
                "target_weights": {},
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, payload = crypto_ofim_watchdog._auto_health(stale_seconds=180)

    assert healthy is True
    assert "target_count=0" in detail
    assert payload["status"] == "planned"


def test_auto_health_stops_recent_duplicate_auto_process(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid in {123, 456})
    monkeypatch.setattr(crypto_ofim_watchdog, "_wait_pid_exit", lambda pid, timeout: pid == 456)
    killed: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        killed.append((pid, sig))

    monkeypatch.setattr(crypto_ofim_watchdog.os, "kill", _fake_kill)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    now = datetime.now(UTC).isoformat()
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": now,
                "status": "planned",
                "target_weights": {},
            }
        ),
        encoding="utf-8",
    )
    (runtime / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": now, "event_type": "cycle_started", "cycle_id": "1778629000000-123"}),
                json.dumps({"ts": now, "event_type": "cycle_started", "cycle_id": "1778629000001-456"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    healthy, detail, payload = crypto_ofim_watchdog._auto_health(stale_seconds=180)

    assert healthy is True
    assert "auto_duplicate_processes_stopped:456" in detail
    assert killed == [(456, crypto_ofim_watchdog.signal.SIGTERM)]
    assert payload["status"] == "planned"


def test_auto_health_marks_loss_guard_as_guarded(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 123)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "status": "planned",
                "plan_reason": "loss_guard_estimated_fees_trade_count",
                "target_weights": {},
                "submitted_orders": [],
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, payload = crypto_ofim_watchdog._auto_health(stale_seconds=180)

    assert healthy is True
    assert detail.startswith("auto_loss_guard_active:loss_guard_estimated_fees_trade_count")
    assert crypto_ofim_watchdog._classify_healthy_detail(detail) == ("guarded", "idle")
    assert payload["plan_reason"] == "loss_guard_estimated_fees_trade_count"


def test_auto_health_reports_loss_guard_with_stale_strategy_guardrails(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 123)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "status": "planned",
                "mode": "testnet",
                "market_data": "mainnet",
                "market_data_base_url": "https://api.binance.com",
                "execution_base_url": "https://testnet.binance.vision",
                "plan_reason": "loss_guard_estimated_fees_trade_count",
                "target_weights": {},
                "submitted_orders": [],
                "strategy_settings": {
                    "entry_threshold": 0.29,
                    "min_order_notional": 20,
                    "max_spread_bps": 20,
                    "min_reentry_after_risk_off_seconds": 7200,
                    "fee_rate": 0.001,
                },
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, payload = crypto_ofim_watchdog._auto_health(
        stale_seconds=180,
        settings=SimpleNamespace(
            mode="testnet",
            market_data="mainnet",
            market_data_base_url="https://api.binance.com",
            entry_threshold=0.34,
            min_order_notional=30,
            max_spread_bps=16,
            min_reentry_after_risk_off_seconds=14400,
            fee_rate=0.001,
        ),
    )

    assert healthy is True
    assert detail.startswith("auto_loss_guard_active:loss_guard_estimated_fees_trade_count")
    assert "auto_guardrail_mismatch:auto_strategy_setting_entry_threshold_below_guardrail_0.29_expected_0.34" in detail
    assert "auto_process_not_restarted_while_loss_guard_active" in detail
    assert crypto_ofim_watchdog._classify_healthy_detail(detail) == ("guarded", "idle")
    assert payload["plan_reason"] == "loss_guard_estimated_fees_trade_count"


def test_auto_health_keeps_loss_guard_when_strategy_guardrails_match(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 123)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "status": "planned",
                "mode": "testnet",
                "market_data": "mainnet",
                "market_data_base_url": "https://api.binance.com",
                "execution_base_url": "https://testnet.binance.vision",
                "plan_reason": "loss_guard_estimated_fees_trade_count",
                "target_weights": {},
                "submitted_orders": [],
                "strategy_settings": {
                    "entry_threshold": 0.34,
                    "min_order_notional": 30,
                    "max_spread_bps": 16,
                    "min_reentry_after_risk_off_seconds": 14400,
                    "fee_rate": 0.001,
                },
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, payload = crypto_ofim_watchdog._auto_health(
        stale_seconds=180,
        settings=SimpleNamespace(
            mode="testnet",
            market_data="mainnet",
            market_data_base_url="https://api.binance.com",
            entry_threshold=0.34,
            min_order_notional=30,
            max_spread_bps=16,
            min_reentry_after_risk_off_seconds=14400,
            fee_rate=0.001,
        ),
    )

    assert healthy is True
    assert detail.startswith("auto_loss_guard_active:loss_guard_estimated_fees_trade_count")
    assert crypto_ofim_watchdog._classify_healthy_detail(detail) == ("guarded", "idle")
    assert payload["strategy_settings"]["entry_threshold"] == 0.34


def test_auto_strategy_guardrail_mismatch_reports_all_churn_fields() -> None:
    payload = {
        "strategy_settings": {
            "entry_threshold": 0.39,
            "min_order_notional": 45,
            "max_order_notional": 7500,
            "max_spread_bps": 12.8,
            "active_capital_pct": 0.40,
            "max_position_weight": 0.35,
            "max_gross_exposure": 0.75,
            "min_trade_interval_seconds": 300,
            "min_flip_interval_seconds": 60,
            "min_reentry_after_risk_off_seconds": 28800,
            "fee_rate": 0.0005,
            "loss_guard_max_loss": 750,
            "loss_guard_max_estimated_fees": 100,
            "loss_guard_max_trades": 120,
            "loss_guard_max_recent_trades": 24,
            "loss_guard_max_recent_risk_off_exits": 9,
            "loss_guard_max_recent_flips": 8,
            "loss_guard_symbol_max_loss": 200,
            "loss_guard_symbol_max_estimated_fees": 50,
            "loss_guard_symbol_max_trades": 80,
        }
    }
    settings = SimpleNamespace(
        entry_threshold=0.49,
        signal_confirm_cycles=2,
        min_order_notional=101.25,
        max_order_notional=2500,
        max_spread_bps=8.192,
        active_capital_pct=0.15,
        max_position_weight=0.25,
        max_gross_exposure=0.5,
        min_trade_interval_seconds=600,
        min_flip_interval_seconds=300,
        min_reentry_after_risk_off_seconds=115200,
        fee_rate=0.001,
        loss_guard_max_loss=500,
        loss_guard_max_estimated_fees=25,
        loss_guard_max_trades=80,
        loss_guard_max_recent_trades=12,
        loss_guard_max_recent_risk_off_exits=3,
        loss_guard_max_recent_flips=3,
        loss_guard_symbol_max_loss=100,
        loss_guard_symbol_max_estimated_fees=10,
        loss_guard_symbol_max_trades=40,
    )

    mismatches = crypto_ofim_watchdog._auto_strategy_settings_guardrail_mismatches(payload, settings)

    assert mismatches == [
        "auto_strategy_setting_entry_threshold_below_guardrail_0.39_expected_0.49",
        "auto_strategy_setting_signal_confirm_cycles_missing",
        "auto_strategy_setting_min_order_notional_below_guardrail_45_expected_101.25",
        "auto_strategy_setting_max_order_notional_above_guardrail_7500_expected_2500",
        "auto_strategy_setting_max_spread_bps_above_guardrail_12.8_expected_8.192",
        "auto_strategy_setting_active_capital_pct_above_guardrail_0.4_expected_0.15",
        "auto_strategy_setting_max_position_weight_above_guardrail_0.35_expected_0.25",
        "auto_strategy_setting_max_gross_exposure_above_guardrail_0.75_expected_0.5",
        "auto_strategy_setting_min_trade_interval_seconds_below_guardrail_300_expected_600",
        "auto_strategy_setting_min_flip_interval_seconds_below_guardrail_60_expected_300",
        "auto_strategy_setting_min_reentry_after_risk_off_seconds_below_guardrail_28800_expected_115200",
        "auto_strategy_setting_fee_rate_below_guardrail_0.0005_expected_0.001",
        "auto_strategy_setting_loss_guard_max_loss_above_guardrail_750_expected_500",
        "auto_strategy_setting_loss_guard_max_estimated_fees_above_guardrail_100_expected_25",
        "auto_strategy_setting_loss_guard_max_trades_above_guardrail_120_expected_80",
        "auto_strategy_setting_loss_guard_max_recent_trades_above_guardrail_24_expected_12",
        "auto_strategy_setting_loss_guard_max_recent_risk_off_exits_above_guardrail_9_expected_3",
        "auto_strategy_setting_loss_guard_max_recent_flips_above_guardrail_8_expected_3",
        "auto_strategy_setting_loss_guard_symbol_max_loss_above_guardrail_200_expected_100",
        "auto_strategy_setting_loss_guard_symbol_max_estimated_fees_above_guardrail_50_expected_10",
        "auto_strategy_setting_loss_guard_symbol_max_trades_above_guardrail_80_expected_40",
    ]
    assert ";" in crypto_ofim_watchdog._auto_strategy_settings_guardrail_mismatch(payload, settings)


def test_auto_health_does_not_restart_missing_process_after_loss_guard(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda _pid: False)
    runtime.mkdir(parents=True)
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "status": "planned",
                "benchmark_trend": {"reason": "loss_guard_loss_cash_reconciliation_estimated_fees_trade_count"},
                "target_weights": {},
                "planned_orders": [],
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, _payload = crypto_ofim_watchdog._auto_health(stale_seconds=180)

    assert healthy is True
    assert "auto_process_not_restarted" in detail


def test_auto_health_repairs_stale_pid_file_from_recent_running_cycle(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 456)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    now = datetime.now(UTC).isoformat()
    (runtime / "events.jsonl").write_text(
        json.dumps({"ts": now, "event_type": "cycle_started", "cycle_id": "1778651000000-456"}) + "\n",
        encoding="utf-8",
    )
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": now,
                "status": "planned",
                "plan_reason": "loss_guard_estimated_fees_trade_count",
                "target_weights": {},
                "planned_orders": [],
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, payload = crypto_ofim_watchdog._auto_health(stale_seconds=180)

    assert healthy is True
    assert "auto_pid_file_repaired:123->456" in detail
    assert detail.startswith("auto_loss_guard_active:loss_guard_estimated_fees_trade_count")
    assert (runtime / "auto.pid").read_text(encoding="utf-8") == "456"
    assert payload["status"] == "planned"


def test_auto_health_does_not_restart_stale_loss_guard(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 123)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
                "status": "planned",
                "plan_reason": "loss_guard_estimated_fees_trade_count",
                "target_weights": {},
                "submitted_orders": [],
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, _payload = crypto_ofim_watchdog._auto_health(stale_seconds=30)

    assert healthy is True
    assert detail.startswith("auto_loss_guard_active:loss_guard_estimated_fees_trade_count")
    assert crypto_ofim_watchdog._classify_healthy_detail(detail) == ("guarded", "idle")


def test_auto_health_reports_stale_status(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 123)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    old_mtime = time.time() - 600
    os.utime(runtime / "auto.pid", (old_mtime, old_mtime))
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
                "status": "planned",
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, _payload = crypto_ofim_watchdog._auto_health(stale_seconds=30)

    assert healthy is False
    assert detail.startswith("auto_status_stale_")


def test_auto_health_allows_guarded_idle_sleep_before_stale_restart(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.delenv("CRYPTO_OFIM_RISK_OFF_IDLE_POLL_SECONDS", raising=False)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 123)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    old_mtime = time.time() - 600
    os.utime(runtime / "auto.pid", (old_mtime, old_mtime))
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": (datetime.now(UTC) - timedelta(minutes=4)).isoformat(),
                "status": "planned",
                "plan_reason": "benchmark_risk_off_cooldown",
                "target_weights": {},
                "planned_orders": [],
                "submitted_orders": [],
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, _payload = crypto_ofim_watchdog._auto_health(stale_seconds=180, poll_seconds=15)

    assert healthy is True
    assert detail == "planned target_count=0"


def test_auto_health_stales_after_guarded_idle_grace(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.delenv("CRYPTO_OFIM_RISK_OFF_IDLE_POLL_SECONDS", raising=False)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 123)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    old_mtime = time.time() - 600
    os.utime(runtime / "auto.pid", (old_mtime, old_mtime))
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": (datetime.now(UTC) - timedelta(minutes=8)).isoformat(),
                "status": "planned",
                "plan_reason": "benchmark_risk_off_cooldown",
                "target_weights": {},
                "planned_orders": [],
                "submitted_orders": [],
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, _payload = crypto_ofim_watchdog._auto_health(stale_seconds=180, poll_seconds=15)

    assert healthy is False
    assert detail.startswith("auto_status_stale_")


def test_auto_health_gives_new_auto_process_startup_grace(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 123)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
                "status": "planned",
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, _payload = crypto_ofim_watchdog._auto_health(stale_seconds=30)

    assert healthy is True
    assert detail.startswith("auto_starting_status_stale_")


def test_auto_health_treats_fresh_transient_network_error_as_healthy(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 123)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "status": "transient_error",
                "error": "Binance Spot Testnet 临时网络超时，系统会在下一轮自动重试。",
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, payload = crypto_ofim_watchdog._auto_health(stale_seconds=180)

    assert healthy is True
    assert detail.startswith("transient_network:")
    assert payload["status"] == "transient_error"


def test_auto_health_prioritizes_transient_network_over_runtime_boundary(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 123)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "status": "transient_error",
                "mode": "testnet",
                "error": "Binance Spot Testnet 临时网络超时，系统会在下一轮自动重试。",
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, payload = crypto_ofim_watchdog._auto_health(
        stale_seconds=180,
        settings=SimpleNamespace(
            mode="testnet",
            market_data="mainnet",
            market_data_base_url="https://api.binance.com",
        ),
    )

    assert healthy is True
    assert detail.startswith("transient_network:")
    assert payload["status"] == "transient_error"


def test_read_watchdog_status_defaults_to_not_started(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)

    status = crypto_ofim_watchdog.read_crypto_ofim_watchdog_status()

    assert status["running"] is False
    assert status["health"] == "not_started"


def test_install_watchdog_launch_agent_writes_safe_plist(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    plist_path = tmp_path / "LaunchAgents" / "com.test.crypto_watchdog.plist"
    monkeypatch.setattr(crypto_ofim_watchdog, "WATCHDOG_LAUNCH_AGENT_PLIST", plist_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "WATCHDOG_LAUNCH_AGENT_LOG_FILE", runtime / "watchdog.launchd.log")

    written = crypto_ofim_watchdog.install_crypto_ofim_watchdog_launch_agent(
        poll_seconds=15,
        check_seconds=10,
        stale_seconds=90,
        restart_cooldown_seconds=45,
    )

    assert written == plist_path
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["Label"] == crypto_ofim_watchdog.WATCHDOG_LAUNCH_AGENT_LABEL
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    args = payload["ProgramArguments"]
    assert "crypto-ofim-watchdog" in args
    assert args[args.index("--poll-seconds") + 1] == "15"
    assert payload["EnvironmentVariables"]["PYTHONPATH"].endswith("/src")


def test_install_app_launch_agent_is_view_only(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    plist_path = tmp_path / "LaunchAgents" / "com.test.crypto_app.plist"
    monkeypatch.setattr(crypto_ofim_watchdog, "APP_LAUNCH_AGENT_PLIST", plist_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "APP_LOG_FILE", runtime / "app.log")

    written = crypto_ofim_watchdog.install_crypto_ofim_app_launch_agent(port=8503)

    assert written == plist_path
    payload = plistlib.loads(plist_path.read_bytes())
    args = payload["ProgramArguments"]
    assert payload["Label"] == crypto_ofim_watchdog.APP_LAUNCH_AGENT_LABEL
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert "streamlit" in args
    assert "run" in args
    assert "crypto_ofim_app.py" in " ".join(args)
    assert args[args.index("--server.fileWatcherType") + 1] == "none"
    assert "crypto-ofim-app" not in args
    assert "crypto-ofim-auto" not in args
    assert "crypto-perp-auto" not in args
    assert str(payload["StandardOutPath"]).endswith("app.log")


def test_stream_health_restarts_when_market_data_mismatches(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 456)
    runtime.mkdir(parents=True)
    (runtime / "stream.pid").write_text("456", encoding="utf-8")
    (runtime / "stream_status.json").write_text(
        json.dumps(
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "status": "running",
                "market_data": "testnet",
            }
        ),
        encoding="utf-8",
    )
    (runtime / "ws_cache.json").write_text("{}", encoding="utf-8")

    healthy, detail, payload = crypto_ofim_watchdog._stream_health(
        stale_seconds=180,
        settings=SimpleNamespace(market_data="mainnet"),
    )

    assert healthy is False
    assert detail == "stream_market_data_mismatch_testnet_expected_mainnet"
    assert payload["market_data"] == "testnet"


def test_repair_stream_process_uses_restart_cooldown(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    starts: list[int] = []

    monkeypatch.setattr(crypto_ofim_watchdog, "_stop_stream_process", lambda: "stream_pid_missing")

    def _fake_start(_depth_limit: int) -> int:
        starts.append(_depth_limit)
        return 456 + len(starts)

    monkeypatch.setattr(crypto_ofim_watchdog, "_start_stream_process", _fake_start)
    state = crypto_ofim_watchdog.CryptoWatchdogState()

    first = crypto_ofim_watchdog._repair_stream_process(
        reason="stream_process_missing",
        depth_limit=100,
        state=state,
        restart_cooldown_seconds=60,
    )
    second = crypto_ofim_watchdog._repair_stream_process(
        reason="stream_process_missing",
        depth_limit=100,
        state=state,
        restart_cooldown_seconds=60,
    )

    assert first.startswith("stream_restarted reason=stream_process_missing")
    assert second.startswith("stream_restart_cooldown reason=stream_process_missing")
    assert starts == [100]


def test_app_health_reports_missing_process(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)

    healthy, detail = crypto_ofim_watchdog._app_health(port=8503)

    assert healthy is False
    assert detail == "app_process_missing"


def test_app_health_uses_port_listener_when_pid_file_missing(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_listening_on_port", lambda port: 789)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 789)
    monkeypatch.setattr(crypto_ofim_watchdog, "_app_http_ok", lambda port: port == 8503)

    healthy, detail = crypto_ofim_watchdog._app_health(port=8503)

    assert healthy is True
    assert detail == "app_running port=8503"


def test_app_health_requires_http_response(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    runtime.mkdir(parents=True)
    (runtime / "app.pid").write_text("789", encoding="utf-8")
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 789)
    monkeypatch.setattr(crypto_ofim_watchdog, "_app_http_ok", lambda port: port == 8503)

    healthy, detail = crypto_ofim_watchdog._app_health(port=8503)

    assert healthy is True
    assert detail == "app_running port=8503"


def test_app_http_ok_reports_permission_blocked_as_unknown(monkeypatch) -> None:
    def _raise_permission(*_args, **_kwargs):
        raise crypto_ofim_watchdog.urllib.error.URLError(PermissionError(errno.EPERM, "Operation not permitted"))

    monkeypatch.setattr(crypto_ofim_watchdog.urllib.request, "urlopen", _raise_permission)

    assert crypto_ofim_watchdog._app_http_ok(8503) is None


def test_app_health_treats_permission_blocked_http_as_running(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    runtime.mkdir(parents=True)
    (runtime / "app.pid").write_text("789", encoding="utf-8")
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 789)
    monkeypatch.setattr(crypto_ofim_watchdog, "_app_http_ok", lambda port: None)

    healthy, detail = crypto_ofim_watchdog._app_health(port=8503)

    assert healthy is True
    assert detail == "app_running_http_check_blocked_port_8503"


def test_app_health_allows_streamlit_startup_grace(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    runtime.mkdir(parents=True)
    (runtime / "app.pid").write_text("789", encoding="utf-8")
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 789)
    monkeypatch.setattr(crypto_ofim_watchdog, "_app_http_ok", lambda port: False)

    healthy, detail = crypto_ofim_watchdog._app_health(port=8503, startup_grace_seconds=90)

    assert healthy is True
    assert detail.startswith("app_starting_http_unreachable_port_8503_age=")


def test_app_health_restarts_after_startup_grace(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    runtime.mkdir(parents=True)
    pid_file = runtime / "app.pid"
    pid_file.write_text("789", encoding="utf-8")
    old_mtime = time.time() - 180
    os.utime(pid_file, (old_mtime, old_mtime))
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 789)
    monkeypatch.setattr(crypto_ofim_watchdog, "_app_http_ok", lambda port: False)

    healthy, detail = crypto_ofim_watchdog._app_health(port=8503, startup_grace_seconds=90)

    assert healthy is False
    assert detail == "app_http_unreachable_port_8503"


def test_watchdog_status_includes_app_fields(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    runtime.mkdir(parents=True)
    (runtime / "auto.pid").write_text("123", encoding="utf-8")
    (runtime / "stream.pid").write_text("456", encoding="utf-8")
    (runtime / "app.pid").write_text("789", encoding="utf-8")
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid in {123, 456, 789})

    crypto_ofim_watchdog._write_status(
        running=True,
        health="healthy",
        action="healthy",
        detail="ok",
        next_check_seconds=30,
        state=crypto_ofim_watchdog.CryptoWatchdogState(),
        app_detail="app_running port=8503",
        app_port=8503,
    )

    payload = json.loads((runtime / "watchdog_status.json").read_text(encoding="utf-8"))
    assert payload["app_pid"] == 789
    assert payload["app_running"] is True
    assert payload["app_port"] == 8503
    assert payload["app_detail"] == "app_running port=8503"


def test_repair_auto_process_blocks_testnet_without_explicit_enable(monkeypatch, tmp_path: Path) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.delenv("CRYPTO_OFIM_AUTO_ENABLED", raising=False)
    monkeypatch.setattr(crypto_ofim_watchdog, "load_crypto_ofim_settings", lambda: SimpleNamespace(mode="testnet"))

    def _fail_start(*_args, **_kwargs):
        raise AssertionError("watchdog must not start testnet auto-submit without explicit enable")

    monkeypatch.setattr(crypto_ofim_watchdog, "_start_auto_process", _fail_start)
    monkeypatch.setattr(crypto_ofim_watchdog, "_stop_auto_process", _fail_start)

    detail = crypto_ofim_watchdog._repair_auto_process(
        reason="auto_process_missing",
        poll_seconds=15,
        restart_cooldown_seconds=30,
        state=crypto_ofim_watchdog.CryptoWatchdogState(),
    )

    assert detail.startswith("auto_submit_disabled")
    assert "CRYPTO_OFIM_AUTO_ENABLED=true" in detail


def test_auto_submit_disabled_repair_classifies_as_guarded_idle() -> None:
    health, action = crypto_ofim_watchdog._classify_auto_repair_detail(
        "auto_submit_disabled reason=auto_process_missing: CRYPTO_OFIM_AUTO_ENABLED=true is required"
    )

    assert health == "guarded"
    assert action == "idle"


def test_repair_auto_process_does_not_restart_loss_guarded_idle_status(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("CRYPTO_OFIM_AUTO_ENABLED", "true")
    monkeypatch.setattr(
        crypto_ofim_watchdog,
        "load_crypto_ofim_settings",
        lambda: SimpleNamespace(
            mode="testnet",
            market_data="mainnet",
            market_data_base_url="https://api.binance.com",
            entry_threshold=0.49,
            min_order_notional=101.25,
            max_spread_bps=8.192,
            min_reentry_after_risk_off_seconds=115200,
            fee_rate=0.001,
        ),
    )

    def _fail_start(*_args, **_kwargs):
        raise AssertionError("loss-guarded idle status must not restart auto-submit")

    monkeypatch.setattr(crypto_ofim_watchdog, "_start_auto_process", _fail_start)
    monkeypatch.setattr(crypto_ofim_watchdog, "_stop_auto_process", _fail_start)
    runtime.mkdir(parents=True)
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "status": "planned",
                "mode": "testnet",
                "market_data": "mainnet",
                "market_data_base_url": "https://api.binance.com",
                "execution_base_url": "https://testnet.binance.vision",
                "plan_reason": "loss_guard_loss_cash_reconciliation_estimated_fees_trade_count",
                "target_weights": {},
                "planned_orders": [],
                "strategy_settings": {
                    "entry_threshold": 0.29,
                    "min_order_notional": 20.0,
                    "max_spread_bps": 20.0,
                    "min_reentry_after_risk_off_seconds": 7200,
                    "fee_rate": 0.001,
                },
            }
        ),
        encoding="utf-8",
    )

    detail = crypto_ofim_watchdog._repair_auto_process(
        reason="auto_process_missing",
        poll_seconds=15,
        restart_cooldown_seconds=30,
        state=crypto_ofim_watchdog.CryptoWatchdogState(),
    )

    assert detail.startswith("auto_guarded_idle_no_restart reason=auto_process_missing")
    assert "auto_loss_guard_active:loss_guard_loss_cash_reconciliation_estimated_fees_trade_count" in detail
    assert "auto_guardrail_mismatch:auto_strategy_setting_entry_threshold_below_guardrail_0.29_expected_0.49" in detail
    assert crypto_ofim_watchdog._classify_auto_repair_detail(detail) == ("guarded", "idle")


def test_perp_health_treats_fresh_status_as_healthy(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 321)
    runtime.mkdir(parents=True)
    (runtime / "perp_auto.pid").write_text("321", encoding="utf-8")
    (runtime / "perp_status.json").write_text(
        json.dumps(
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "status": "submitted",
                "mode": "paper",
                "market_data_base_url": "https://fapi.binance.com",
                "execution_base_url": "https://fapi.binance.com",
                "target_weights": {"BTCUSDT": -0.1},
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, payload = crypto_ofim_watchdog._perp_health(
        stale_seconds=180,
        settings=SimpleNamespace(
            mode="paper",
            market_data_base_url="https://fapi.binance.com",
            base_url="https://fapi.binance.com",
        ),
    )

    assert healthy is True
    assert "target_count=1" in detail
    assert payload["target_weights"]["BTCUSDT"] == -0.1


def test_perp_health_marks_loss_guard_as_guarded(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 321)
    runtime.mkdir(parents=True)
    (runtime / "perp_auto.pid").write_text("321", encoding="utf-8")
    (runtime / "perp_status.json").write_text(
        json.dumps(
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "status": "submitted",
                "mode": "paper",
                "reason": "perp_loss_guard_fees_trade_count",
                "market_data_base_url": "https://fapi.binance.com",
                "execution_base_url": "https://fapi.binance.com",
                "target_weights": {},
                "submitted_orders": [],
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, payload = crypto_ofim_watchdog._perp_health(
        stale_seconds=180,
        settings=SimpleNamespace(
            mode="paper",
            market_data_base_url="https://fapi.binance.com",
            base_url="https://fapi.binance.com",
        ),
    )

    assert healthy is True
    assert detail.startswith("perp_loss_guard_active:perp_loss_guard_fees_trade_count")
    assert crypto_ofim_watchdog._classify_healthy_detail(detail) == ("guarded", "idle")
    assert payload["reason"] == "perp_loss_guard_fees_trade_count"


def test_perp_health_does_not_restart_stale_loss_guard(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 321)
    runtime.mkdir(parents=True)
    (runtime / "perp_auto.pid").write_text("321", encoding="utf-8")
    (runtime / "perp_status.json").write_text(
        json.dumps(
            {
                "updated_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
                "status": "submitted",
                "mode": "paper",
                "reason": "perp_loss_guard_fees_trade_count",
                "market_data_base_url": "https://fapi.binance.com",
                "execution_base_url": "https://fapi.binance.com",
                "target_weights": {},
                "submitted_orders": [],
            }
        ),
        encoding="utf-8",
    )

    healthy, detail, _payload = crypto_ofim_watchdog._perp_health(
        stale_seconds=30,
        settings=SimpleNamespace(
            mode="paper",
            market_data_base_url="https://fapi.binance.com",
            base_url="https://fapi.binance.com",
        ),
    )

    assert healthy is True
    assert detail.startswith("perp_loss_guard_active:perp_loss_guard_fees_trade_count")
    assert crypto_ofim_watchdog._classify_healthy_detail(detail) == ("guarded", "idle")


def test_watchdog_status_includes_perp_fields(monkeypatch, tmp_path: Path) -> None:
    runtime = _patch_runtime(monkeypatch, tmp_path)
    runtime.mkdir(parents=True)
    (runtime / "perp_auto.pid").write_text("321", encoding="utf-8")
    monkeypatch.setattr(crypto_ofim_watchdog, "_pid_running", lambda pid: pid == 321)

    crypto_ofim_watchdog._write_status(
        running=True,
        health="healthy",
        action="healthy",
        detail="ok",
        next_check_seconds=30,
        state=crypto_ofim_watchdog.CryptoWatchdogState(),
        perp_payload={
            "updated_at": datetime.now(UTC).isoformat(),
            "status": "submitted",
            "mode": "paper",
            "target_weights": {"BTCUSDT": -0.1},
        },
        perp_detail="submitted target_count=1",
        perp_enabled=True,
    )

    payload = json.loads((runtime / "watchdog_status.json").read_text(encoding="utf-8"))
    assert payload["perp_enabled"] is True
    assert payload["perp_pid"] == 321
    assert payload["perp_running"] is True
    assert payload["perp_status"] == "submitted"
    assert payload["perp_target_count"] == 1
