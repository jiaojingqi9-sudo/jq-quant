from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import random
import signal
import socket
import subprocess
import time
from typing import Any

from .auto_trader import _market_window_state, validate_auto_trader_mode
from .config import Settings, load_settings
from .futu_gateway import FutuPaperTrader


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
RUNTIME_DIR = REPO_ROOT / "runtime"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
FUTU_OPEND_APP = Path("/Applications/FutuOpenD.app")
AUTO_TRADER_STATUS_FILE = RUNTIME_DIR / "auto_trader_status.json"
AUTO_TRADER_PID_FILE = RUNTIME_DIR / "auto_trader.pid"
AUTO_TRADER_LOG_FILE = RUNTIME_DIR / "auto_trader.log"
WATCHDOG_STATUS_FILE = RUNTIME_DIR / "watchdog_status.json"
WATCHDOG_PID_FILE = RUNTIME_DIR / "watchdog.pid"
WATCHDOG_LOG_FILE = RUNTIME_DIR / "watchdog.log"
WATCHDOG_LAUNCH_AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.jiao.taa_futu_watchdog.plist"


@dataclass
class WatchdogState:
    restart_count: int = 0
    last_restart_at: datetime | None = None
    last_opend_launch_at: datetime | None = None
    data_quality_failures: int = 0


def _log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] {message}", flush=True)


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    pythonpath_parts = [str(SRC_ROOT)]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    return env


def _is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _parse_status_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _read_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _socket_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _pid_from_file(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return 0


def _auto_trader_health(settings: Settings, now_utc: datetime) -> tuple[bool, str, dict[str, Any]]:
    pid = _pid_from_file(AUTO_TRADER_PID_FILE)
    payload = _read_status(AUTO_TRADER_STATUS_FILE)

    if not pid or not _is_pid_running(pid):
        return False, "auto_trader_process_missing", payload

    if not payload:
        return False, "auto_trader_status_missing", payload

    updated_at = _parse_status_timestamp(str(payload.get("updated_at", "")))
    if updated_at is None:
        return False, "auto_trader_status_invalid", payload

    age_seconds = (now_utc - updated_at).total_seconds()
    if age_seconds > settings.watchdog_stale_status_seconds:
        return False, f"auto_trader_status_stale_{int(age_seconds)}s", payload

    if not payload.get("running"):
        return False, "auto_trader_not_running", payload

    action = str(payload.get("action", "unknown"))
    detail = str(payload.get("detail", ""))
    if action == "error":
        if FutuPaperTrader.is_transient_error(detail):
            return True, f"transient_error:{detail}", payload
        return False, f"auto_trader_error:{detail}", payload
    if action == "transient_error":
        return True, f"transient_error:{detail}", payload

    return True, f"{action}:{detail}".strip(":"), payload


def _wait_for_pid_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _is_pid_running(pid):
            return True
        time.sleep(0.25)
    return not _is_pid_running(pid)


def _stop_auto_trader_process() -> tuple[bool, str]:
    pid = _pid_from_file(AUTO_TRADER_PID_FILE)
    if not pid:
        return True, "auto_trader_pid_missing"
    if not _is_pid_running(pid):
        return True, f"auto_trader_pid_not_running:{pid}"

    os.kill(pid, signal.SIGTERM)
    if _wait_for_pid_exit(pid, 10):
        return True, f"auto_trader_stopped:{pid}"

    os.kill(pid, signal.SIGKILL)
    if _wait_for_pid_exit(pid, 2):
        return True, f"auto_trader_killed:{pid}"
    return False, f"auto_trader_stop_failed:{pid}"


def _start_auto_trader_process() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with AUTO_TRADER_LOG_FILE.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [str(VENV_PYTHON), "-m", "taa_futu.auto_trader"],
            cwd=REPO_ROOT,
            env=_build_env(),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    return process.pid


def _wait_for_auto_trader_payload(pid: int, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
    deadline = time.time() + max(0.0, timeout_seconds)
    payload: dict[str, Any] = {}
    while time.time() <= deadline:
        payload = _read_status(AUTO_TRADER_STATUS_FILE)
        if _pid_from_file(AUTO_TRADER_PID_FILE) == pid and _is_pid_running(pid):
            payload.setdefault("running", True)
            payload.setdefault("pid", pid)
            return payload
        time.sleep(0.2)
    payload.setdefault("pid", pid)
    payload.setdefault("running", _is_pid_running(pid))
    return payload


def _restart_auto_trader(settings: Settings, state: WatchdogState, now_utc: datetime) -> tuple[str, str, dict[str, Any]]:
    if (
        state.last_restart_at is not None
        and (now_utc - state.last_restart_at).total_seconds() < settings.watchdog_restart_cooldown_seconds
    ):
        cooldown = settings.watchdog_restart_cooldown_seconds - int((now_utc - state.last_restart_at).total_seconds())
        return "restart_cooldown", f"waiting_{max(cooldown, 0)}s_before_next_restart", _read_status(AUTO_TRADER_STATUS_FILE)

    stop_ok, stop_detail = _stop_auto_trader_process()
    pid = _start_auto_trader_process()
    state.restart_count += 1
    state.last_restart_at = now_utc
    return (
        "restarted_auto_trader",
        f"{stop_detail}; started_pid={pid}; restart_count={state.restart_count}",
        _wait_for_auto_trader_payload(pid),
    )


def _ensure_opend(settings: Settings, state: WatchdogState, now_utc: datetime) -> tuple[bool, str]:
    if _socket_open(settings.futu_host, settings.futu_port):
        return True, "opend_connected"

    if not FUTU_OPEND_APP.exists():
        return False, f"opend_offline_and_app_missing:{FUTU_OPEND_APP}"

    if (
        state.last_opend_launch_at is None
        or (now_utc - state.last_opend_launch_at).total_seconds() >= settings.watchdog_restart_cooldown_seconds
    ):
        subprocess.Popen(["open", "-a", str(FUTU_OPEND_APP)])
        state.last_opend_launch_at = now_utc
        return False, "opend_offline_launch_requested"

    cooldown = settings.watchdog_restart_cooldown_seconds - int((now_utc - state.last_opend_launch_at).total_seconds())
    return False, f"opend_offline_waiting_{max(cooldown, 0)}s"


def _next_sleep_seconds(settings: Settings, *, market_open: bool) -> int:
    low = settings.watchdog_min_interval_seconds if market_open else settings.watchdog_outside_window_min_interval_seconds
    high = settings.watchdog_max_interval_seconds if market_open else settings.watchdog_outside_window_max_interval_seconds
    if low > high:
        low, high = high, low
    return random.SystemRandom().randint(max(15, low), max(max(15, low), high))


def _data_quality_probe(settings: Settings) -> tuple[bool, str]:
    symbols = tuple(settings.fusion_universe or settings.symbols or (settings.benchmark,))
    symbol = symbols[0] if symbols else settings.benchmark
    last_detail = "not_run"
    for attempt in range(3):
        try:
            with FutuPaperTrader(settings) as trader:
                snapshots = trader.get_snapshots([symbol])
            if snapshots.empty or symbol not in snapshots.index:
                last_detail = f"snapshot_missing:{symbol}"
            else:
                last_price = float(snapshots.loc[symbol].get("last_price", 0.0) or 0.0)
                if last_price > 0:
                    return True, f"ok:{symbol}:{last_price:.4f}"
                last_detail = f"price_zero:{symbol}"
        except Exception as exc:
            last_detail = f"{type(exc).__name__}:{exc}"
        if attempt < 2:
            time.sleep(2)
    return False, last_detail


def _write_status(
    *,
    running: bool,
    action: str,
    detail: str,
    market_open: bool,
    market_window_detail: str,
    next_check_seconds: int,
    settings: Settings,
    state: WatchdogState,
    auto_payload: dict[str, Any] | None = None,
    opend_connected: bool,
) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    now_utc = datetime.now(UTC)
    payload = {
        "running": running,
        "pid": os.getpid(),
        "updated_at": now_utc.isoformat(),
        "action": action,
        "detail": detail,
        "market_open": market_open,
        "market_window_detail": market_window_detail,
        "next_check_seconds": next_check_seconds,
        "next_check_at": (now_utc.timestamp() + next_check_seconds),
        "timezone": settings.auto_trader_market_timezone,
        "window_start": settings.auto_trader_start_time,
        "window_end": settings.auto_trader_end_time,
        "opend_connected": opend_connected,
        "restart_count": state.restart_count,
        "data_quality_failures": state.data_quality_failures,
        "auto_trader_pid": _pid_from_file(AUTO_TRADER_PID_FILE),
        "auto_trader_running": bool(_pid_from_file(AUTO_TRADER_PID_FILE) and _is_pid_running(_pid_from_file(AUTO_TRADER_PID_FILE))),
        "auto_trader_action": (auto_payload or {}).get("action"),
        "auto_trader_detail": (auto_payload or {}).get("detail"),
        "log_file": str(WATCHDOG_LOG_FILE),
    }
    WATCHDOG_STATUS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _register_pid() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    current_pid = _pid_from_file(WATCHDOG_PID_FILE)
    if current_pid and _is_pid_running(current_pid):
        raise SystemExit(f"Watchdog is already running with pid {current_pid}.")
    WATCHDOG_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _cleanup_files() -> None:
    if WATCHDOG_PID_FILE.exists():
        WATCHDOG_PID_FILE.unlink()


def _run_cycle(settings: Settings, state: WatchdogState) -> tuple[str, str, bool, str, dict[str, Any], bool]:
    now_utc = datetime.now(UTC)
    market_open, window_detail = _market_window_state(now_utc, settings)
    try:
        validate_auto_trader_mode(settings, submit=True)
    except SystemExit as exc:
        auto_payload = _read_status(AUTO_TRADER_STATUS_FILE)
        return "blocked", str(exc), market_open, window_detail, auto_payload, _socket_open(settings.futu_host, settings.futu_port)
    opend_connected, opend_detail = _ensure_opend(settings, state, now_utc)
    if not opend_connected:
        auto_payload = _read_status(AUTO_TRADER_STATUS_FILE)
        state.data_quality_failures = 0
        return "waiting_opend", opend_detail, market_open, window_detail, auto_payload, False

    data_quality_detail = ""
    if market_open:
        data_ok, data_detail = _data_quality_probe(settings)
        if data_ok:
            state.data_quality_failures = 0
            data_quality_detail = f"; data_quality={data_detail}"
        else:
            state.data_quality_failures += 1
            if state.data_quality_failures >= 3:
                action, detail, auto_payload = _restart_auto_trader(settings, state, now_utc)
                return (
                    action,
                    f"reason=data_quality_failed:{data_detail}; failures={state.data_quality_failures}; {detail}",
                    market_open,
                    window_detail,
                    auto_payload,
                    True,
                )
            data_quality_detail = f"; data_quality_warning={data_detail}; failures={state.data_quality_failures}/3"

    healthy, health_detail, auto_payload = _auto_trader_health(settings, now_utc)
    if healthy:
        action = "data_quality_warning" if data_quality_detail.startswith("; data_quality_warning") else "healthy"
        return action, f"{health_detail}{data_quality_detail}", market_open, window_detail, auto_payload, True

    action, detail, auto_payload = _restart_auto_trader(settings, state, now_utc)
    return action, f"reason={health_detail}; {detail}", market_open, window_detail, auto_payload, True


def run_watchdog(settings: Settings) -> None:
    stop_requested = False
    state = WatchdogState()

    def _handle_signal(signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True
        _log(f"received signal {signum}, shutting down watchdog")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    _register_pid()
    _write_status(
        running=True,
        action="starting",
        detail="watchdog booting",
        market_open=False,
        market_window_detail="booting",
        next_check_seconds=0,
        settings=settings,
        state=state,
        auto_payload=_read_status(AUTO_TRADER_STATUS_FILE),
        opend_connected=_socket_open(settings.futu_host, settings.futu_port),
    )
    _log("watchdog started")

    try:
        while not stop_requested:
            try:
                action, detail, market_open, window_detail, auto_payload, opend_connected = _run_cycle(settings, state)
                _log(f"{action}: {detail}")
            except Exception as exc:  # pragma: no cover - safety net for daemon process
                action = "error"
                detail = f"{type(exc).__name__}: {exc}"
                market_open, window_detail = _market_window_state(datetime.now(UTC), settings)
                auto_payload = _read_status(AUTO_TRADER_STATUS_FILE)
                opend_connected = _socket_open(settings.futu_host, settings.futu_port)
                _log(f"error: {detail}")

            next_sleep_seconds = _next_sleep_seconds(settings, market_open=market_open)
            _write_status(
                running=True,
                action=action,
                detail=detail,
                market_open=market_open,
                market_window_detail=window_detail,
                next_check_seconds=next_sleep_seconds,
                settings=settings,
                state=state,
                auto_payload=auto_payload,
                opend_connected=opend_connected,
            )

            sleep_until = time.time() + next_sleep_seconds
            while not stop_requested and time.time() < sleep_until:
                time.sleep(1)
    finally:
        _write_status(
            running=False,
            action="stopped",
            detail="watchdog stopped",
            market_open=False,
            market_window_detail="stopped",
            next_check_seconds=0,
            settings=settings,
            state=state,
            auto_payload=_read_status(AUTO_TRADER_STATUS_FILE),
            opend_connected=_socket_open(settings.futu_host, settings.futu_port),
        )
        _cleanup_files()
        _log("watchdog stopped")


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="Monitor the auto trader and repair common runtime failures.")


def main() -> None:
    _build_parser().parse_args()
    settings = load_settings()
    run_watchdog(settings)


if __name__ == "__main__":
    main()
