from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import errno
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.request

from .crypto_ofim import (
    AUTO_LOG_FILE,
    AUTO_PID_FILE,
    EVENTS_FILE,
    CryptoOfimError,
    TESTNET_BASE_URL,
    RUNTIME_DIR,
    STATUS_FILE,
    crypto_ofim_guarded_idle_poll_seconds,
    ensure_crypto_ofim_auto_submit_allowed,
    load_crypto_ofim_settings,
)
from .crypto_ofim_stream import (
    STREAM_CACHE_FILE,
    STREAM_LOG_FILE,
    STREAM_PID_FILE,
    STREAM_STATUS_FILE,
)
from .crypto_perp import (
    AUTO_LOG_FILE as PERP_AUTO_LOG_FILE,
    AUTO_PID_FILE as PERP_AUTO_PID_FILE,
    STATUS_FILE as PERP_STATUS_FILE,
    load_crypto_perp_settings,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
WATCHDOG_PID_FILE = RUNTIME_DIR / "watchdog.pid"
WATCHDOG_LOG_FILE = RUNTIME_DIR / "watchdog.log"
WATCHDOG_STATUS_FILE = RUNTIME_DIR / "watchdog_status.json"
WATCHDOG_LAUNCH_AGENT_LABEL = "com.jiao.taa_futu_crypto_ofim_watchdog"
WATCHDOG_LAUNCH_AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{WATCHDOG_LAUNCH_AGENT_LABEL}.plist"
WATCHDOG_LAUNCH_AGENT_LOG_FILE = RUNTIME_DIR / "watchdog.launchd.log"
APP_PID_FILE = REPO_ROOT / "runtime" / "crypto_ofim_app.pid"
APP_LOG_FILE = REPO_ROOT / "runtime" / "crypto_ofim_app.log"
APP_PORT = 8503
APP_LAUNCH_AGENT_LABEL = "com.jiao.taa_futu_crypto_ofim_app"
APP_LAUNCH_AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{APP_LAUNCH_AGENT_LABEL}.plist"


@dataclass
class CryptoWatchdogState:
    restart_count: int = 0
    last_restart_ts: float = 0.0
    last_stream_restart_ts: float = 0.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _pid_from_file(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return 0


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return True
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return True
    if result.returncode != 0:
        return True
    status = result.stdout.strip()
    if not status:
        return True
    return bool(status) and not status.upper().startswith("Z")


def _pid_listening_on_port(port: int) -> int:
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-Fp"],
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, ValueError):
        return 0
    if result.returncode != 0:
        return 0
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            try:
                return int(line[1:])
            except ValueError:
                continue
    return 0


def _app_process_pid(port: int = APP_PORT) -> int:
    port_pid = _pid_listening_on_port(port)
    if _pid_running(port_pid):
        return port_pid
    pid = _pid_from_file(APP_PID_FILE)
    if _pid_running(pid):
        return pid
    return 0


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _payload_count(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    return len(value) if isinstance(value, (dict, list)) else 0


def _loss_guard_detail(payload: dict[str, Any], *, prefix: str) -> str:
    reason = str(payload.get("plan_reason") or payload.get("reason") or "")
    context = payload.get("benchmark_trend") or payload.get("benchmark_context")
    if not reason and isinstance(context, dict):
        reason = str(context.get("reason") or "")
    if "loss_guard" not in reason:
        return ""

    target_count = _payload_count(payload, "target_weights")
    order_count = max(_payload_count(payload, "submitted_orders"), _payload_count(payload, "planned_orders"))
    if target_count or order_count:
        return ""
    return f"{prefix}_loss_guard_active:{reason} target_count=0 order_count=0"


def _file_age_seconds(path: Path) -> float | None:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_url(raw: Any) -> str:
    return str(raw or "").rstrip("/")


def _as_float(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in {float("inf"), float("-inf")}:
        return None
    return value


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _iter_recent_jsonl(path: Path, *, max_bytes: int = 512 * 1024) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - max_bytes)
            fh.seek(start)
            if start > 0:
                fh.readline()
            rows: list[dict[str, Any]] = []
            for raw in fh:
                try:
                    rows.append(json.loads(raw.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
            return rows
    except OSError:
        return []


def _recent_auto_cycle_pids(*, window_seconds: int) -> set[int]:
    cutoff = _utc_now().timestamp() - max(30, int(window_seconds))
    pids: set[int] = set()
    for row in _iter_recent_jsonl(EVENTS_FILE):
        event_type = str(row.get("event_type") or "")
        if event_type not in {"cycle_started", "cycle_completed", "loss_guard_triggered", "plan_generated"}:
            continue
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts.timestamp() < cutoff:
            continue
        cycle_id = str(row.get("cycle_id") or "")
        if "-" not in cycle_id:
            continue
        try:
            pids.add(int(cycle_id.rsplit("-", 1)[-1]))
        except ValueError:
            continue
    return pids


def _recent_running_auto_cycle_pid(*, window_seconds: int, exclude: set[int] | None = None) -> int:
    cutoff = _utc_now().timestamp() - max(30, int(window_seconds))
    excluded = exclude or set()
    for row in reversed(_iter_recent_jsonl(EVENTS_FILE)):
        event_type = str(row.get("event_type") or "")
        if event_type not in {"cycle_started", "cycle_completed", "loss_guard_triggered", "plan_generated"}:
            continue
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts.timestamp() < cutoff:
            continue
        cycle_id = str(row.get("cycle_id") or "")
        if "-" not in cycle_id:
            continue
        try:
            pid = int(cycle_id.rsplit("-", 1)[-1])
        except ValueError:
            continue
        if pid in excluded:
            continue
        if _pid_running(pid):
            return pid
    return 0


def _repair_auto_pid_file_from_recent_cycles(*, current_pid: int, window_seconds: int) -> tuple[int, str]:
    if current_pid and _pid_running(current_pid):
        return current_pid, ""
    replacement_pid = _recent_running_auto_cycle_pid(
        window_seconds=window_seconds,
        exclude={current_pid} if current_pid else set(),
    )
    if not replacement_pid:
        return 0, ""
    try:
        AUTO_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTO_PID_FILE.write_text(str(replacement_pid), encoding="utf-8")
    except OSError as exc:
        return replacement_pid, f"auto_pid_file_repair_failed:{type(exc).__name__}:{current_pid or 'missing'}->{replacement_pid}"
    return replacement_pid, f"auto_pid_file_repaired:{current_pid or 'missing'}->{replacement_pid}"


def _stop_duplicate_auto_processes(*, current_pid: int, window_seconds: int) -> tuple[list[int], list[str]]:
    stopped: list[int] = []
    failed: list[str] = []
    for pid in sorted(_recent_auto_cycle_pids(window_seconds=window_seconds)):
        if pid == current_pid or not _pid_running(pid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            if not _wait_pid_exit(pid, 3):
                os.kill(pid, signal.SIGKILL)
                if not _wait_pid_exit(pid, 2):
                    failed.append(f"{pid}:still_running")
                    continue
            stopped.append(pid)
        except OSError as exc:
            failed.append(f"{pid}:{type(exc).__name__}")
    return stopped, failed


def _append_duplicate_detail(detail: str, duplicate_detail: str) -> str:
    if not duplicate_detail:
        return detail
    return f"{detail}; {duplicate_detail}" if detail else duplicate_detail


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    pythonpath = [str(SRC_ROOT)]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    return env


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl_service() -> str:
    return f"{_launchctl_domain()}/{WATCHDOG_LAUNCH_AGENT_LABEL}"


def _launchctl_label_service(label: str) -> str:
    return f"{_launchctl_domain()}/{label}"


def _run_launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], check=False, capture_output=True, text=True)


def install_crypto_ofim_watchdog_launch_agent(
    *,
    poll_seconds: int = 60,
    check_seconds: int = 30,
    stale_seconds: int = 180,
    restart_cooldown_seconds: int = 120,
) -> Path:
    """Install the crypto watchdog as a per-user LaunchAgent.

    LaunchAgent keeps the watchdog outside Streamlit/Terminal process groups. That matters
    when the UI or a shell session is closed while the overnight monitor should keep running.
    """

    WATCHDOG_LAUNCH_AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    env = {
        "PYTHONPATH": str(SRC_ROOT),
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
    }
    payload = {
        "Label": WATCHDOG_LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(REPO_ROOT / ".venv" / "bin" / "python"),
            "-m",
            "taa_futu.cli",
            "crypto-ofim-watchdog",
            "--poll-seconds",
            str(max(5, int(poll_seconds))),
            "--check-seconds",
            str(max(5, int(check_seconds))),
            "--stale-seconds",
            str(max(30, int(stale_seconds))),
            "--restart-cooldown-seconds",
            str(max(30, int(restart_cooldown_seconds))),
        ],
        "EnvironmentVariables": env,
        "WorkingDirectory": str(REPO_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(WATCHDOG_LAUNCH_AGENT_LOG_FILE),
        "StandardErrorPath": str(WATCHDOG_LAUNCH_AGENT_LOG_FILE),
    }
    with WATCHDOG_LAUNCH_AGENT_PLIST.open("wb") as fh:
        plistlib.dump(payload, fh)
    return WATCHDOG_LAUNCH_AGENT_PLIST


def start_crypto_ofim_watchdog_service(
    *,
    poll_seconds: int = 60,
    check_seconds: int = 30,
    stale_seconds: int = 180,
    restart_cooldown_seconds: int = 120,
) -> tuple[bool, str]:
    pid = _pid_from_file(WATCHDOG_PID_FILE)
    if _pid_running(pid):
        return False, f"Crypto OFIM watchdog already running with pid {pid}."

    plist_path = install_crypto_ofim_watchdog_launch_agent(
        poll_seconds=poll_seconds,
        check_seconds=check_seconds,
        stale_seconds=stale_seconds,
        restart_cooldown_seconds=restart_cooldown_seconds,
    )
    _run_launchctl(["bootout", _launchctl_domain(), str(plist_path)])
    result = _run_launchctl(["bootstrap", _launchctl_domain(), str(plist_path)])
    if result.returncode != 0:
        fallback = _run_launchctl(["load", str(plist_path)])
        if fallback.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or fallback.stderr.strip() or fallback.stdout.strip()
            return False, f"LaunchAgent start failed: {detail}"
    _run_launchctl(["kickstart", "-k", _launchctl_service()])

    deadline = time.time() + 8
    while time.time() < deadline:
        pid = _pid_from_file(WATCHDOG_PID_FILE)
        if _pid_running(pid):
            return True, f"Crypto OFIM watchdog started by LaunchAgent, pid={pid}."
        time.sleep(0.25)
    return True, "Crypto OFIM watchdog LaunchAgent loaded; pid is still pending."


def stop_crypto_ofim_watchdog_service() -> tuple[bool, str]:
    stopped = False
    plist_path = WATCHDOG_LAUNCH_AGENT_PLIST
    if plist_path.exists():
        bootout = _run_launchctl(["bootout", _launchctl_domain(), str(plist_path)])
        unload = _run_launchctl(["unload", str(plist_path)])
        stopped = bootout.returncode == 0 or unload.returncode == 0

    pid = _pid_from_file(WATCHDOG_PID_FILE)
    if _pid_running(pid):
        os.kill(pid, signal.SIGTERM)
        stopped = True
        _wait_pid_exit(pid, 5)
    if WATCHDOG_PID_FILE.exists():
        WATCHDOG_PID_FILE.unlink()
    if stopped:
        return True, "Crypto OFIM watchdog LaunchAgent stopped."
    return False, "Crypto OFIM watchdog was not running."


def install_crypto_ofim_app_launch_agent(*, port: int = APP_PORT) -> Path:
    """Install a view-only app LaunchAgent.

    This service keeps the Streamlit dashboard alive without starting the
    trading watchdog or either auto-trading loop.
    """

    APP_LAUNCH_AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    APP_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    env = {
        "PYTHONPATH": str(SRC_ROOT),
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
    }
    payload = {
        "Label": APP_LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(REPO_ROOT / ".venv" / "bin" / "python"),
            "-m",
            "streamlit",
            "run",
            str(SRC_ROOT / "taa_futu" / "crypto_ofim_app.py"),
            "--server.port",
            str(int(port)),
            "--server.headless",
            "true",
            "--server.fileWatcherType",
            "none",
            "--browser.gatherUsageStats=false",
        ],
        "EnvironmentVariables": env,
        "WorkingDirectory": str(REPO_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(APP_LOG_FILE),
        "StandardErrorPath": str(APP_LOG_FILE),
    }
    with APP_LAUNCH_AGENT_PLIST.open("wb") as fh:
        plistlib.dump(payload, fh)
    return APP_LAUNCH_AGENT_PLIST


def start_crypto_ofim_app_service(*, port: int = APP_PORT) -> tuple[bool, str]:
    plist_path = install_crypto_ofim_app_launch_agent(port=port)
    _run_launchctl(["bootout", _launchctl_domain(), str(plist_path)])
    result = _run_launchctl(["bootstrap", _launchctl_domain(), str(plist_path)])
    if result.returncode != 0:
        fallback = _run_launchctl(["load", str(plist_path)])
        if fallback.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or fallback.stderr.strip() or fallback.stdout.strip()
            return False, f"Crypto OFIM app LaunchAgent start failed: {detail}"
    _run_launchctl(["kickstart", "-k", _launchctl_label_service(APP_LAUNCH_AGENT_LABEL)])

    deadline = time.time() + 20
    last_detail = "app_starting"
    while time.time() < deadline:
        healthy, detail = _app_health(port=port)
        last_detail = detail
        if healthy and detail.startswith("app_running"):
            return True, f"Crypto OFIM app started by LaunchAgent, pid={_app_process_pid(port)}, port={port}."
        time.sleep(0.5)
    return True, f"Crypto OFIM app LaunchAgent loaded; latest health={last_detail}."


def stop_crypto_ofim_app_service() -> tuple[bool, str]:
    stopped = False
    plist_path = APP_LAUNCH_AGENT_PLIST
    if plist_path.exists():
        bootout = _run_launchctl(["bootout", _launchctl_domain(), str(plist_path)])
        unload = _run_launchctl(["unload", str(plist_path)])
        stopped = bootout.returncode == 0 or unload.returncode == 0

    pid = _app_process_pid(APP_PORT)
    if _pid_running(pid):
        os.kill(pid, signal.SIGTERM)
        stopped = True
        _wait_pid_exit(pid, 5)
    if APP_PID_FILE.exists():
        APP_PID_FILE.unlink()
    if stopped:
        return True, "Crypto OFIM app LaunchAgent stopped."
    return False, "Crypto OFIM app was not running."


def read_crypto_ofim_app_status(*, port: int = APP_PORT) -> dict[str, Any]:
    pid = _app_process_pid(port)
    healthy, detail = _app_health(port=port)
    return {
        "running": _pid_running(pid),
        "pid": pid,
        "port": int(port),
        "url": f"http://localhost:{int(port)}",
        "healthy": healthy,
        "detail": detail,
        "launch_agent": APP_LAUNCH_AGENT_LABEL,
        "launch_agent_plist": str(APP_LAUNCH_AGENT_PLIST),
    }


def _log(message: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{stamp}] {message}"
    with WATCHDOG_LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _write_status(
    *,
    running: bool,
    health: str,
    action: str,
    detail: str,
    next_check_seconds: int,
    state: CryptoWatchdogState,
    auto_payload: dict[str, Any] | None = None,
    stream_payload: dict[str, Any] | None = None,
    stream_detail: str = "",
    app_detail: str = "",
    app_port: int = APP_PORT,
    perp_payload: dict[str, Any] | None = None,
    perp_detail: str = "",
    perp_enabled: bool = False,
) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    auto_pid = _pid_from_file(AUTO_PID_FILE)
    now = _utc_now()
    updated_at = _parse_ts((auto_payload or {}).get("updated_at"))
    age_seconds = (now - updated_at).total_seconds() if updated_at else None
    perp_updated_at = _parse_ts((perp_payload or {}).get("updated_at") or (perp_payload or {}).get("ts"))
    perp_age_seconds = (now - perp_updated_at).total_seconds() if perp_updated_at else None
    perp_pid = _pid_from_file(PERP_AUTO_PID_FILE)
    payload = {
        "running": running,
        "pid": os.getpid(),
        "updated_at": now.isoformat(),
        "health": health,
        "action": action,
        "detail": detail,
        "next_check_seconds": next_check_seconds,
        "restart_count": state.restart_count,
        "auto_pid": auto_pid,
        "auto_running": _pid_running(auto_pid),
        "auto_status": (auto_payload or {}).get("status"),
        "auto_mode": (auto_payload or {}).get("mode"),
        "auto_market_data": (auto_payload or {}).get("market_data"),
        "auto_market_data_base_url": (auto_payload or {}).get("market_data_base_url"),
        "auto_execution_base_url": (auto_payload or {}).get("execution_base_url"),
        "auto_updated_at": (auto_payload or {}).get("updated_at"),
        "auto_age_seconds": round(age_seconds, 2) if age_seconds is not None else None,
        "target_count": len((auto_payload or {}).get("target_weights") or {}),
        "order_count": len((auto_payload or {}).get("submitted_orders") or (auto_payload or {}).get("planned_orders") or []),
        "stream_pid": _pid_from_file(STREAM_PID_FILE),
        "stream_running": _pid_running(_pid_from_file(STREAM_PID_FILE)),
        "stream_status": (stream_payload or {}).get("status"),
        "stream_market_data": (stream_payload or {}).get("market_data"),
        "stream_market_data_base_url": (stream_payload or {}).get("market_data_base_url"),
        "stream_updated_at": (stream_payload or {}).get("updated_at"),
        "stream_cache_age_seconds": round(_file_age_seconds(STREAM_CACHE_FILE), 2) if _file_age_seconds(STREAM_CACHE_FILE) is not None else None,
        "stream_detail": stream_detail,
        "app_pid": _pid_from_file(APP_PID_FILE),
        "app_running": _pid_running(_pid_from_file(APP_PID_FILE)),
        "app_port": app_port,
        "app_detail": app_detail,
        "perp_enabled": perp_enabled,
        "perp_pid": perp_pid,
        "perp_running": _pid_running(perp_pid),
        "perp_status": (perp_payload or {}).get("status"),
        "perp_mode": (perp_payload or {}).get("mode"),
        "perp_submit_label": (perp_payload or {}).get("submit_label"),
        "perp_market_data_base_url": (perp_payload or {}).get("market_data_base_url"),
        "perp_execution_base_url": (perp_payload or {}).get("execution_base_url"),
        "perp_updated_at": (perp_payload or {}).get("updated_at") or (perp_payload or {}).get("ts"),
        "perp_age_seconds": round(perp_age_seconds, 2) if perp_age_seconds is not None else None,
        "perp_target_count": len((perp_payload or {}).get("target_weights") or {}),
        "perp_order_count": len((perp_payload or {}).get("submitted_orders") or (perp_payload or {}).get("planned_orders") or []),
        "perp_detail": perp_detail,
        "log_file": str(WATCHDOG_LOG_FILE),
    }
    WATCHDOG_STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_crypto_ofim_watchdog_status() -> dict[str, Any]:
    if not WATCHDOG_STATUS_FILE.exists():
        return {"running": False, "health": "not_started", "detail": "watchdog has not run yet"}
    return _read_json(WATCHDOG_STATUS_FILE)


def _auto_runtime_boundary_mismatch(payload: dict[str, Any], settings: Any) -> str:
    expected_mode = str(getattr(settings, "mode", "") or "")
    actual_mode = str(payload.get("mode") or "")
    if expected_mode and actual_mode != expected_mode:
        return f"auto_mode_mismatch_{actual_mode or 'missing'}_expected_{expected_mode}"

    expected_market_data = str(getattr(settings, "market_data", "") or "")
    actual_market_data = str(payload.get("market_data") or "")
    if expected_market_data and actual_market_data != expected_market_data:
        return f"auto_market_data_mismatch_{actual_market_data or 'missing'}_expected_{expected_market_data}"

    expected_market_base = _normalize_url(getattr(settings, "market_data_base_url", ""))
    actual_market_base = _normalize_url(payload.get("market_data_base_url"))
    if expected_market_base and actual_market_base != expected_market_base:
        return f"auto_market_data_base_url_mismatch_{actual_market_base or 'missing'}_expected_{expected_market_base}"

    if expected_mode == "testnet":
        expected_execution_base = _normalize_url(TESTNET_BASE_URL)
        actual_execution_base = _normalize_url(payload.get("execution_base_url"))
        if actual_execution_base != expected_execution_base:
            return f"auto_execution_base_url_mismatch_{actual_execution_base or 'missing'}_expected_{expected_execution_base}"
    return ""


def _auto_strategy_settings_guardrail_mismatches(payload: dict[str, Any], settings: Any) -> list[str]:
    strategy_settings = payload.get("strategy_settings")
    if not isinstance(strategy_settings, dict):
        return ["auto_strategy_settings_missing"]

    checks = (
        ("entry_threshold", "entry_threshold", "below_guardrail"),
        ("signal_confirm_cycles", "signal_confirm_cycles", "below_guardrail"),
        ("min_order_notional", "min_order_notional", "below_guardrail"),
        ("max_order_notional", "max_order_notional", "above_guardrail"),
        ("max_spread_bps", "max_spread_bps", "above_guardrail"),
        ("active_capital_pct", "active_capital_pct", "above_guardrail"),
        ("max_position_weight", "max_position_weight", "above_guardrail"),
        ("max_gross_exposure", "max_gross_exposure", "above_guardrail"),
        ("min_trade_interval_seconds", "min_trade_interval_seconds", "below_guardrail"),
        ("min_flip_interval_seconds", "min_flip_interval_seconds", "below_guardrail"),
        ("min_reentry_after_risk_off_seconds", "min_reentry_after_risk_off_seconds", "below_guardrail"),
        ("fee_rate", "fee_rate", "below_guardrail"),
        ("loss_guard_max_loss", "loss_guard_max_loss", "above_guardrail"),
        ("loss_guard_max_estimated_fees", "loss_guard_max_estimated_fees", "above_guardrail"),
        ("loss_guard_max_trades", "loss_guard_max_trades", "above_guardrail"),
        ("loss_guard_max_recent_trades", "loss_guard_max_recent_trades", "above_guardrail"),
        ("loss_guard_max_recent_risk_off_exits", "loss_guard_max_recent_risk_off_exits", "above_guardrail"),
        ("loss_guard_max_recent_flips", "loss_guard_max_recent_flips", "above_guardrail"),
        ("loss_guard_symbol_max_loss", "loss_guard_symbol_max_loss", "above_guardrail"),
        (
            "loss_guard_symbol_max_estimated_fees",
            "loss_guard_symbol_max_estimated_fees",
            "above_guardrail",
        ),
        ("loss_guard_symbol_max_trades", "loss_guard_symbol_max_trades", "above_guardrail"),
    )
    mismatches: list[str] = []
    tolerance = 1e-9
    for key, attr, direction in checks:
        expected = _as_float(getattr(settings, attr, None))
        if expected is None:
            continue
        actual = _as_float(strategy_settings.get(key))
        if actual is None:
            mismatches.append(f"auto_strategy_setting_{key}_missing")
            continue
        if direction == "above_guardrail":
            breached = actual > expected + tolerance
        else:
            breached = actual + tolerance < expected
        if breached:
            mismatches.append(f"auto_strategy_setting_{key}_{direction}_{actual:g}_expected_{expected:g}")
    return mismatches


def _auto_strategy_settings_guardrail_mismatch(payload: dict[str, Any], settings: Any) -> str:
    return ";".join(_auto_strategy_settings_guardrail_mismatches(payload, settings))


def _auto_effective_stale_seconds(
    stale_seconds: int,
    payload: dict[str, Any],
    *,
    poll_seconds: int | float = 60,
) -> int:
    base_stale_seconds = max(30, int(stale_seconds))
    try:
        idle_poll_seconds = int(crypto_ofim_guarded_idle_poll_seconds(payload, poll_seconds))
    except (TypeError, ValueError):
        return base_stale_seconds
    if idle_poll_seconds <= poll_seconds:
        return base_stale_seconds
    idle_grace_seconds = max(30, min(120, base_stale_seconds))
    return max(base_stale_seconds, idle_poll_seconds + idle_grace_seconds)


def _auto_health(
    stale_seconds: int,
    *,
    settings: Any | None = None,
    poll_seconds: int | float = 60,
) -> tuple[bool, str, dict[str, Any]]:
    pid = _pid_from_file(AUTO_PID_FILE)
    payload = _read_json(STATUS_FILE)
    guarded_detail = _loss_guard_detail(payload, prefix="auto")
    pid_repair_detail = ""
    if not pid or not _pid_running(pid):
        repaired_pid, pid_repair_detail = _repair_auto_pid_file_from_recent_cycles(
            current_pid=pid,
            window_seconds=max(60, min(300, int(stale_seconds))),
        )
        if repaired_pid:
            pid = repaired_pid
    if not pid or not _pid_running(pid):
        if guarded_detail:
            return True, _append_duplicate_detail(
                f"{guarded_detail}; auto_process_not_restarted",
                pid_repair_detail,
            ), payload
        return False, "auto_process_missing", payload
    duplicate_detail = pid_repair_detail
    stopped_duplicates, failed_duplicates = _stop_duplicate_auto_processes(
        current_pid=pid,
        window_seconds=max(60, min(300, int(stale_seconds))),
    )
    if stopped_duplicates:
        duplicate_detail = f"auto_duplicate_processes_stopped:{','.join(str(item) for item in stopped_duplicates)}"
    if failed_duplicates:
        failed_detail = f"auto_duplicate_process_stop_failed:{','.join(failed_duplicates)}"
        return False, _append_duplicate_detail(failed_detail, duplicate_detail), payload
    pid_age = _file_age_seconds(AUTO_PID_FILE)
    startup_grace = max(30, min(120, int(stale_seconds)))
    if not payload:
        if pid_age is not None and pid_age < startup_grace:
            return True, _append_duplicate_detail(f"auto_starting_status_pending pid={pid}", duplicate_detail), payload
        return False, "auto_status_missing", payload

    if payload.get("status") == "transient_error":
        return True, _append_duplicate_detail(
            f"transient_network:{payload.get('error', 'temporary network issue')}",
            duplicate_detail,
        ), payload

    if settings is not None:
        mismatch = _auto_runtime_boundary_mismatch(payload, settings)
        if mismatch:
            missing_boundary = "_missing_" in mismatch or mismatch.endswith("_missing")
            if missing_boundary and pid_age is not None and pid_age < startup_grace:
                return True, _append_duplicate_detail(f"auto_starting_runtime_boundary_pending pid={pid}", duplicate_detail), payload
            return False, mismatch, payload

        if guarded_detail:
            strategy_mismatch = _auto_strategy_settings_guardrail_mismatch(payload, settings)
            if strategy_mismatch:
                guarded_detail = (
                    f"{guarded_detail}; "
                    f"auto_guardrail_mismatch:{strategy_mismatch}; "
                    "auto_process_not_restarted_while_loss_guard_active"
                )
            return True, _append_duplicate_detail(guarded_detail, duplicate_detail), payload

        strategy_mismatch = _auto_strategy_settings_guardrail_mismatch(payload, settings)
        if strategy_mismatch:
            if strategy_mismatch.endswith("_missing") and pid_age is not None and pid_age < startup_grace:
                return True, _append_duplicate_detail(f"auto_starting_strategy_settings_pending pid={pid}", duplicate_detail), payload
            return False, strategy_mismatch, payload

    if guarded_detail:
        return True, _append_duplicate_detail(guarded_detail, duplicate_detail), payload

    updated_at = _parse_ts(payload.get("updated_at"))
    if updated_at is None:
        if pid_age is not None and pid_age < startup_grace:
            return True, _append_duplicate_detail(f"auto_starting_status_invalid pid={pid}", duplicate_detail), payload
        return False, "auto_status_invalid_timestamp", payload

    age = (_utc_now() - updated_at).total_seconds()
    effective_stale_seconds = _auto_effective_stale_seconds(
        stale_seconds,
        payload,
        poll_seconds=poll_seconds,
    )
    if age > effective_stale_seconds:
        if pid_age is not None and pid_age < startup_grace:
            return True, _append_duplicate_detail(f"auto_starting_status_stale_{int(age)}s pid={pid}", duplicate_detail), payload
        return False, f"auto_status_stale_{int(age)}s", payload

    if payload.get("status") == "error":
        return False, f"auto_error:{payload.get('error', 'unknown')}", payload

    return True, _append_duplicate_detail(
        f"{payload.get('status', 'unknown')} target_count={len(payload.get('target_weights') or {})}",
        duplicate_detail,
    ), payload


def _wait_pid_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _pid_running(pid):
            return True
        time.sleep(0.25)
    return not _pid_running(pid)


def _stop_auto_process() -> str:
    pid = _pid_from_file(AUTO_PID_FILE)
    if not pid:
        return "auto_pid_missing"
    if not _pid_running(pid):
        if AUTO_PID_FILE.exists():
            AUTO_PID_FILE.unlink()
        return f"auto_pid_not_running:{pid}"

    os.kill(pid, signal.SIGTERM)
    if _wait_pid_exit(pid, 8):
        if AUTO_PID_FILE.exists():
            AUTO_PID_FILE.unlink()
        return f"auto_stopped:{pid}"

    os.kill(pid, signal.SIGKILL)
    if AUTO_PID_FILE.exists():
        AUTO_PID_FILE.unlink()
    return f"auto_killed:{pid}"


def _start_auto_process(poll_seconds: int) -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with AUTO_LOG_FILE.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "taa_futu.cli",
                "crypto-ofim-auto",
                "--submit",
                "--poll-seconds",
                str(max(5, int(poll_seconds))),
            ],
            cwd=REPO_ROOT,
            env=_build_env(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    return process.pid


def _perp_runtime_boundary_mismatch(payload: dict[str, Any], settings: Any) -> str:
    expected_mode = str(getattr(settings, "mode", "") or "")
    actual_mode = str(payload.get("mode") or "")
    if expected_mode and actual_mode != expected_mode:
        return f"perp_mode_mismatch_{actual_mode or 'missing'}_expected_{expected_mode}"
    expected_market_base = _normalize_url(getattr(settings, "market_data_base_url", ""))
    actual_market_base = _normalize_url(payload.get("market_data_base_url"))
    if expected_market_base and actual_market_base != expected_market_base:
        return f"perp_market_data_base_url_mismatch_{actual_market_base or 'missing'}_expected_{expected_market_base}"
    expected_execution_base = _normalize_url(getattr(settings, "base_url", ""))
    actual_execution_base = _normalize_url(payload.get("execution_base_url"))
    if expected_execution_base and actual_execution_base != expected_execution_base:
        return f"perp_execution_base_url_mismatch_{actual_execution_base or 'missing'}_expected_{expected_execution_base}"
    return ""


def _perp_health(stale_seconds: int, *, settings: Any | None = None) -> tuple[bool, str, dict[str, Any]]:
    if settings is not None and getattr(settings, "mode", "") == "testnet" and not getattr(settings, "signed_account_enabled", False):
        return True, "perp_testnet_missing_key_submit_disabled", _read_json(PERP_STATUS_FILE)
    pid = _pid_from_file(PERP_AUTO_PID_FILE)
    payload = _read_json(PERP_STATUS_FILE)
    guarded_detail = _loss_guard_detail(payload, prefix="perp")
    if not pid or not _pid_running(pid):
        if guarded_detail:
            return True, f"{guarded_detail}; perp_process_not_restarted", payload
        return False, "perp_process_missing", payload
    pid_age = _file_age_seconds(PERP_AUTO_PID_FILE)
    startup_grace = max(30, min(120, int(stale_seconds)))
    if not payload:
        if pid_age is not None and pid_age < startup_grace:
            return True, f"perp_starting_status_pending pid={pid}", payload
        return False, "perp_status_missing", payload
    if guarded_detail:
        return True, guarded_detail, payload
    updated_at = _parse_ts(payload.get("updated_at") or payload.get("ts"))
    if updated_at is None:
        if pid_age is not None and pid_age < startup_grace:
            return True, f"perp_starting_status_invalid pid={pid}", payload
        return False, "perp_status_invalid_timestamp", payload
    age = (_utc_now() - updated_at).total_seconds()
    if age > stale_seconds:
        if pid_age is not None and pid_age < startup_grace:
            return True, f"perp_starting_status_stale_{int(age)}s pid={pid}", payload
        return False, f"perp_status_stale_{int(age)}s", payload
    if settings is not None:
        mismatch = _perp_runtime_boundary_mismatch(payload, settings)
        if mismatch:
            return False, mismatch, payload
    if payload.get("status") == "error":
        return False, f"perp_error:{payload.get('error', 'unknown')}", payload
    return True, f"{payload.get('status', 'unknown')} target_count={len(payload.get('target_weights') or {})}", payload


def _stop_perp_process() -> str:
    pid = _pid_from_file(PERP_AUTO_PID_FILE)
    if not pid:
        return "perp_pid_missing"
    if not _pid_running(pid):
        if PERP_AUTO_PID_FILE.exists():
            PERP_AUTO_PID_FILE.unlink()
        return f"perp_pid_not_running:{pid}"
    os.kill(pid, signal.SIGTERM)
    if _wait_pid_exit(pid, 8):
        if PERP_AUTO_PID_FILE.exists():
            PERP_AUTO_PID_FILE.unlink()
        return f"perp_stopped:{pid}"
    os.kill(pid, signal.SIGKILL)
    if PERP_AUTO_PID_FILE.exists():
        PERP_AUTO_PID_FILE.unlink()
    return f"perp_killed:{pid}"


def _start_perp_process(poll_seconds: int) -> int:
    PERP_AUTO_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PERP_AUTO_LOG_FILE.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "taa_futu.cli",
                "crypto-perp-auto",
                "--submit",
                "--poll-seconds",
                str(max(5, int(poll_seconds))),
            ],
            cwd=REPO_ROOT,
            env=_build_env(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    PERP_AUTO_PID_FILE.write_text(str(process.pid), encoding="utf-8")
    return process.pid


def _repair_perp_process(
    *,
    reason: str,
    poll_seconds: int,
    restart_cooldown_seconds: int,
    state: CryptoWatchdogState,
) -> str:
    now = time.time()
    if state.last_restart_ts and now - state.last_restart_ts < restart_cooldown_seconds:
        wait_for = int(restart_cooldown_seconds - (now - state.last_restart_ts))
        return f"perp_restart_cooldown reason={reason} wait={max(wait_for, 0)}s"
    stop_detail = _stop_perp_process()
    pid = _start_perp_process(poll_seconds)
    state.restart_count += 1
    state.last_restart_ts = now
    return f"perp_restarted reason={reason}; {stop_detail}; started_pid={pid}; restart_count={state.restart_count}"


def _stream_health(stale_seconds: int, *, settings: Any | None = None) -> tuple[bool, str, dict[str, Any]]:
    pid = _pid_from_file(STREAM_PID_FILE)
    payload = _read_json(STREAM_STATUS_FILE)
    if not pid or not _pid_running(pid):
        return False, "stream_process_missing", payload
    status = payload.get("status")
    if status == "error":
        return False, f"stream_error:{payload.get('detail', 'unknown')}", payload
    if status not in {"running", "connecting"}:
        return False, f"stream_status_{status or 'missing'}", payload
    if settings is not None:
        expected_market_data = str(getattr(settings, "market_data", "") or "")
        actual_market_data = str(payload.get("market_data") or "")
        if expected_market_data and actual_market_data != expected_market_data:
            detail = actual_market_data or "missing"
            return False, f"stream_market_data_mismatch_{detail}_expected_{expected_market_data}", payload
        expected_market_base = _normalize_url(getattr(settings, "market_data_base_url", ""))
        actual_market_base = _normalize_url(payload.get("market_data_base_url"))
        if expected_market_base and actual_market_base != expected_market_base:
            detail = actual_market_base or "missing"
            return False, f"stream_market_data_base_url_mismatch_{detail}_expected_{expected_market_base}", payload
    age = _file_age_seconds(STREAM_CACHE_FILE)
    if age is None:
        return False, "stream_cache_missing", payload
    if age > stale_seconds:
        return False, f"stream_cache_stale_{int(age)}s", payload
    return True, f"{status} cache_age={int(age)}s", payload


def _stop_stream_process() -> str:
    pid = _pid_from_file(STREAM_PID_FILE)
    if not pid:
        return "stream_pid_missing"
    if not _pid_running(pid):
        if STREAM_PID_FILE.exists():
            STREAM_PID_FILE.unlink()
        return f"stream_pid_not_running:{pid}"

    os.kill(pid, signal.SIGTERM)
    if _wait_pid_exit(pid, 8):
        if STREAM_PID_FILE.exists():
            STREAM_PID_FILE.unlink()
        return f"stream_stopped:{pid}"

    os.kill(pid, signal.SIGKILL)
    if STREAM_PID_FILE.exists():
        STREAM_PID_FILE.unlink()
    return f"stream_killed:{pid}"


def _start_stream_process(depth_limit: int) -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with STREAM_LOG_FILE.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "taa_futu.cli",
                "crypto-ofim-stream",
                "--depth-limit",
                str(max(100, int(depth_limit))),
            ],
            cwd=REPO_ROOT,
            env=_build_env(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    STREAM_PID_FILE.write_text(str(process.pid), encoding="utf-8")
    return process.pid


def _repair_stream_process(
    *,
    reason: str,
    depth_limit: int,
    state: CryptoWatchdogState,
    restart_cooldown_seconds: int = 60,
) -> str:
    now = time.time()
    if state.last_stream_restart_ts and now - state.last_stream_restart_ts < restart_cooldown_seconds:
        wait_for = int(restart_cooldown_seconds - (now - state.last_stream_restart_ts))
        return f"stream_restart_cooldown reason={reason} wait={max(wait_for, 0)}s"
    stop_detail = _stop_stream_process()
    pid = _start_stream_process(depth_limit)
    state.restart_count += 1
    state.last_stream_restart_ts = now
    return f"stream_restarted reason={reason}; {stop_detail}; started_pid={pid}; restart_count={state.restart_count}"


def _app_http_ok(port: int, timeout_seconds: float = 5.0) -> bool | None:
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{int(port)}/_stcore/health", method="GET")
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return 200 <= int(response.status) < 500
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, PermissionError) and getattr(reason, "errno", None) == errno.EPERM:
            return None
        return False
    except PermissionError as exc:
        if getattr(exc, "errno", None) == errno.EPERM:
            return None
        return False
    except (OSError, ValueError):
        return False


def _app_health(port: int = APP_PORT, *, startup_grace_seconds: int = 90) -> tuple[bool, str]:
    pid = _app_process_pid(port)
    if not pid or not _pid_running(pid):
        return False, "app_process_missing"
    http_ok = _app_http_ok(port)
    if http_ok is None:
        return True, f"app_running_http_check_blocked_port_{port}"
    if not http_ok:
        pid_age = _file_age_seconds(APP_PID_FILE)
        if pid_age is not None and pid_age < max(0, int(startup_grace_seconds)):
            return True, f"app_starting_http_unreachable_port_{port}_age={pid_age:.1f}s"
        return False, f"app_http_unreachable_port_{port}"
    return True, f"app_running port={port}"


def _stop_app_process() -> str:
    pid = _pid_from_file(APP_PID_FILE)
    if not pid:
        return "app_pid_missing"
    if not _pid_running(pid):
        if APP_PID_FILE.exists():
            APP_PID_FILE.unlink()
        return f"app_pid_not_running:{pid}"

    os.kill(pid, signal.SIGTERM)
    if _wait_pid_exit(pid, 8):
        if APP_PID_FILE.exists():
            APP_PID_FILE.unlink()
        return f"app_stopped:{pid}"

    os.kill(pid, signal.SIGKILL)
    if APP_PID_FILE.exists():
        APP_PID_FILE.unlink()
    return f"app_killed:{pid}"


def _start_app_process(port: int = APP_PORT) -> int:
    APP_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    app_path = SRC_ROOT / "taa_futu" / "crypto_ofim_app.py"
    with APP_LOG_FILE.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(app_path),
                "--server.port",
                str(int(port)),
                "--server.headless",
                "true",
                "--browser.gatherUsageStats=false",
            ],
            cwd=REPO_ROOT,
            env=_build_env(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    APP_PID_FILE.write_text(str(process.pid), encoding="utf-8")
    return process.pid


def _repair_app_process(*, reason: str, port: int, state: CryptoWatchdogState) -> str:
    stop_detail = _stop_app_process()
    pid = _start_app_process(port)
    state.restart_count += 1
    return f"app_restarted reason={reason}; {stop_detail}; started_pid={pid}; restart_count={state.restart_count}"


def _classify_auto_repair_detail(detail: str) -> tuple[str, str]:
    text = str(detail or "")
    if text.startswith("auto_submit_disabled") or "loss_guard_active:" in text:
        return "guarded", "idle"
    return "repairing", "repair"


def _classify_healthy_detail(detail: str) -> tuple[str, str]:
    if "loss_guard_active:" in str(detail or ""):
        return "guarded", "idle"
    return "healthy", "healthy"


def _repair_auto_process(
    *,
    reason: str,
    poll_seconds: int,
    restart_cooldown_seconds: int,
    state: CryptoWatchdogState,
) -> str:
    settings = load_crypto_ofim_settings()
    payload = _read_json(STATUS_FILE)
    guarded_detail = _loss_guard_detail(payload, prefix="auto")
    if guarded_detail:
        strategy_mismatch = _auto_strategy_settings_guardrail_mismatch(payload, settings)
        mismatch_detail = f"; auto_guardrail_mismatch:{strategy_mismatch}" if strategy_mismatch else ""
        return f"auto_guarded_idle_no_restart reason={reason}: {guarded_detail}{mismatch_detail}"

    try:
        ensure_crypto_ofim_auto_submit_allowed(settings, submit=True)
    except CryptoOfimError as exc:
        return f"auto_submit_disabled reason={reason}: {exc}"

    now = time.time()
    if state.last_restart_ts and now - state.last_restart_ts < restart_cooldown_seconds:
        wait_for = int(restart_cooldown_seconds - (now - state.last_restart_ts))
        return f"restart_cooldown reason={reason} wait={max(wait_for, 0)}s"

    stop_detail = _stop_auto_process()
    pid = _start_auto_process(poll_seconds)
    state.restart_count += 1
    state.last_restart_ts = now
    return f"restarted reason={reason}; {stop_detail}; started_pid={pid}; restart_count={state.restart_count}"


def _register_pid() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    current = _pid_from_file(WATCHDOG_PID_FILE)
    if current and current != os.getpid() and _pid_running(current):
        raise SystemExit(f"Crypto OFIM watchdog is already running with pid {current}.")
    WATCHDOG_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _cleanup_pid() -> None:
    if WATCHDOG_PID_FILE.exists():
        WATCHDOG_PID_FILE.unlink()


def run_crypto_ofim_watchdog(
    *,
    poll_seconds: int = 60,
    check_seconds: int = 30,
    stale_seconds: int = 180,
    restart_cooldown_seconds: int = 120,
) -> None:
    settings = load_crypto_ofim_settings()
    try:
        app_port = int(os.getenv("CRYPTO_OFIM_APP_PORT", str(APP_PORT)))
    except ValueError:
        app_port = APP_PORT
    state = CryptoWatchdogState()
    stop_requested = False

    def _handle_signal(signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True
        _log(f"received signal {signum}, stopping watchdog")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    _register_pid()
    perp_enabled = _parse_bool(os.getenv("CRYPTO_PERP_AUTO_ENABLED"), default=False)
    _write_status(
        running=True,
        health="starting",
        action="starting",
        detail="crypto watchdog booting",
        next_check_seconds=0,
        state=state,
        auto_payload=_read_json(STATUS_FILE),
        stream_payload=_read_json(STREAM_STATUS_FILE),
        app_detail="app_starting",
        app_port=app_port,
        perp_payload=_read_json(PERP_STATUS_FILE),
        perp_detail="perp_starting" if perp_enabled else "perp_disabled",
        perp_enabled=perp_enabled,
    )
    _log("crypto OFIM watchdog started")

    try:
        while not stop_requested:
            next_sleep = max(5, int(check_seconds))
            try:
                settings = load_crypto_ofim_settings()
                stream_payload = _read_json(STREAM_STATUS_FILE)
                stream_detail = "stream_disabled"
                if settings.use_ws_cache:
                    stream_ok, stream_detail, stream_payload = _stream_health(max(15, int(stale_seconds)), settings=settings)
                    if not stream_ok:
                        stream_detail = _repair_stream_process(
                            reason=stream_detail,
                            depth_limit=settings.depth_limit,
                            state=state,
                            restart_cooldown_seconds=max(30, int(restart_cooldown_seconds)),
                        )
                        stream_payload = _read_json(STREAM_STATUS_FILE)
                healthy, detail, auto_payload = _auto_health(
                    max(30, int(stale_seconds)),
                    settings=settings,
                    poll_seconds=max(5, int(poll_seconds)),
                )
                if healthy:
                    health, action = _classify_healthy_detail(detail)
                else:
                    detail = _repair_auto_process(
                        reason=detail,
                        poll_seconds=max(5, int(poll_seconds)),
                        restart_cooldown_seconds=max(30, int(restart_cooldown_seconds)),
                        state=state,
                    )
                    health, action = _classify_auto_repair_detail(detail)
                    auto_payload = _read_json(STATUS_FILE)
                perp_enabled = _parse_bool(os.getenv("CRYPTO_PERP_AUTO_ENABLED"), default=False)
                perp_payload = _read_json(PERP_STATUS_FILE)
                perp_detail = "perp_disabled"
                if perp_enabled:
                    perp_settings = load_crypto_perp_settings()
                    perp_ok, perp_detail, perp_payload = _perp_health(max(30, int(stale_seconds)), settings=perp_settings)
                    if not perp_ok:
                        perp_detail = _repair_perp_process(
                            reason=perp_detail,
                            poll_seconds=max(5, int(os.getenv("CRYPTO_PERP_AUTO_POLL_SECONDS", str(poll_seconds)))),
                            restart_cooldown_seconds=max(30, int(restart_cooldown_seconds)),
                            state=state,
                        )
                        perp_payload = _read_json(PERP_STATUS_FILE)
                        if health == "healthy":
                            health = "repairing"
                            action = "repair"
                            detail = perp_detail
                        else:
                            detail = f"{detail}; {perp_detail}"
                    elif "loss_guard_active:" in perp_detail:
                        perp_health, perp_action = _classify_healthy_detail(perp_detail)
                        if health == "healthy":
                            health = perp_health
                            action = perp_action
                            detail = perp_detail
                        else:
                            detail = f"{detail}; {perp_detail}"
                app_ok, app_detail = _app_health(app_port)
                if not app_ok:
                    app_detail = _repair_app_process(reason=app_detail, port=app_port, state=state)
                    if healthy:
                        health = "repairing"
                        action = "repair"
                        detail = app_detail
                    else:
                        detail = f"{detail}; {app_detail}"
                _log(f"{action}: {detail}; {stream_detail}; {app_detail}; {perp_detail}")
            except Exception as exc:  # pragma: no cover - daemon safety net
                health = "error"
                action = "error"
                detail = f"{type(exc).__name__}: {exc}"
                auto_payload = _read_json(STATUS_FILE)
                stream_payload = _read_json(STREAM_STATUS_FILE)
                perp_payload = _read_json(PERP_STATUS_FILE)
                stream_detail = "watchdog_error"
                app_detail = "watchdog_error"
                perp_detail = "watchdog_error"
                perp_enabled = _parse_bool(os.getenv("CRYPTO_PERP_AUTO_ENABLED"), default=False)
                _log(detail)

            _write_status(
                running=True,
                health=health,
                action=action,
                detail=detail,
                next_check_seconds=next_sleep,
                state=state,
                auto_payload=auto_payload,
                stream_payload=stream_payload,
                stream_detail=stream_detail,
                app_detail=app_detail,
                app_port=app_port,
                perp_payload=perp_payload,
                perp_detail=perp_detail,
                perp_enabled=perp_enabled,
            )

            deadline = time.time() + next_sleep
            while not stop_requested and time.time() < deadline:
                time.sleep(1)
    finally:
        _write_status(
            running=False,
            health="stopped",
            action="stopped",
            detail="watchdog stopped",
            next_check_seconds=0,
            state=state,
            auto_payload=_read_json(STATUS_FILE),
            stream_payload=_read_json(STREAM_STATUS_FILE),
            app_detail="watchdog_stopped",
            app_port=app_port,
            perp_payload=_read_json(PERP_STATUS_FILE),
            perp_detail="watchdog_stopped",
            perp_enabled=_parse_bool(os.getenv("CRYPTO_PERP_AUTO_ENABLED"), default=False),
        )
        _cleanup_pid()
        _log("crypto OFIM watchdog stopped")
