"""加密交易 / 选股器 / 实时建议 三个页面的渲染实现。

这个模块只提供渲染函数，不负责导航。谁在什么时候调它们由注册表决定：
``features/crypto.py`` ``features/screener.py`` ``features/live_signal.py``
各自登记一个 Feature，外壳 ``shell.py`` 从注册表生成侧边栏与首页。

* ``crypto``      → :func:`render_crypto_trading_full`
* ``screener``    → :func:`render_screener_full`
* ``live_signal`` → :func:`render_live_signal`

本文件里曾经还有一份 ``render_home`` / ``render_view`` / ``SIDEBAR_OPTIONS``
的手写分发，是插件架构之前的实现。注册表上线后它就没有调用方了，
2026-07-31 删除——留着会让人以为导航有两套。现在只有一套，入口在
``shell.py``。

股票交易与历史模拟不放这里：它们由 ``dashboard_app.py`` 自己登记，
原因见 ``features/_host_owned.py``。

所有渲染函数对实盘状态只读，**从不下单**。需要执行命令时一律通过
``subprocess`` 调 ``taa-futu`` 命令行入口。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
from typing import Any

import pandas as pd
import streamlit as st


# Session-state keys for view routing.
VIEW_KEY = "view"
VIEW_HOME = "home"
VIEW_STOCK = "stock"
VIEW_STOCK_HISTORY = "stock_history"
VIEW_CRYPTO = "crypto"
VIEW_SCREENER = "screener"
VIEW_LIVE_SIGNAL = "live_signal"
VIEW_NEWS = "news"


# ── Paths ───────────────────────────────────────────────────────────────────
# TRADE_DIR is derived from this file's own location so the repo runs from any
# folder on any machine. ALL_HERE is the surrounding workspace that holds the
# sibling components (futu_queue, news collector); override it with the
# ALL_HERE_ROOT environment variable if your layout differs.
TRADE_DIR = Path(os.environ.get("TAA_TRADE_ROOT") or Path(__file__).resolve().parents[2])
HOME = Path.home()
ALL_HERE = Path(os.environ.get("ALL_HERE_ROOT") or TRADE_DIR.parent)
RUNTIME_DIR = TRADE_DIR / "runtime"
VENV_PYTHON = TRADE_DIR / ".venv" / "bin" / "python"

CRYPTO_OFIM_DIR = RUNTIME_DIR / "crypto_ofim"
CRYPTO_PERP_DIR = RUNTIME_DIR / "crypto_perp"
CRYPTO_OFIM_STATUS = CRYPTO_OFIM_DIR / "status.json"
CRYPTO_PERP_STATUS = CRYPTO_PERP_DIR / "status.json"
CRYPTO_OFIM_EVENTS = CRYPTO_OFIM_DIR / "events.jsonl"
CRYPTO_PERP_EVENTS = CRYPTO_PERP_DIR / "events.jsonl"

AUTO_TRADER_STATUS = RUNTIME_DIR / "auto_trader_status.json"
WATCHDOG_STATUS = RUNTIME_DIR / "watchdog_status.json"
FUTU_QUEUE_ALIVE = ALL_HERE / "futu_queue" / "_watcher_alive.txt"

STOCK_LAUNCHERS = TRADE_DIR / "stock" / "launchers"
CRYPTO_LAUNCHERS = TRADE_DIR / "crypto" / "launchers"
NEWS_COLLECTOR = ALL_HERE / "news collector"
NEWS_SCAN_LIVE = NEWS_COLLECTOR / "reports" / "live"


# ─────────────────────────── helpers ───────────────────────────


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _age_str(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    secs = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
    if secs < 60:
        return f"{secs:.0f}s 前"
    if secs < 3600:
        return f"{secs/60:.0f}min 前"
    return f"{secs/3600:.1f}h 前"


def _socket_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tail_jsonl(path: Path, n: int = 20) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 4096
            chunks: list[bytes] = []
            collected_lines = 0
            offset = size
            while offset > 0 and collected_lines <= n + 5:
                step = min(block, offset)
                offset -= step
                f.seek(offset)
                chunk = f.read(step)
                chunks.append(chunk)
                collected_lines += chunk.count(b"\n")
            data = b"".join(reversed(chunks))
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()][-n:]
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _state_badge(state: str) -> str:
    return {
        "ok": "🟢 OK",
        "warn": "🟡 WARN",
        "fail": "🔴 FAIL",
        "idle": "⚪ IDLE",
    }.get(state, "⚪ —")


def _go_to(view: str) -> None:
    """Switch the host main() to ``view`` and rerun.

    Streamlit's sidebar radio is keyed (``sidebar_view_radio``) so user clicks
    on the sidebar persist across reruns. The catch: that same persistence
    will silently stomp ``st.session_state["view"]`` back to whatever label
    the sidebar last remembered, undoing the home-page button click.

    Fix: pop the sidebar widget key so the next rerun reinitialises the radio
    from its ``index=`` argument (which we compute from the freshly-set view).
    Directly overwriting the key after the widget has been instantiated is
    not supported in current Streamlit and silently no-ops on some versions.
    """
    st.session_state[VIEW_KEY] = view
    st.session_state.pop("sidebar_view_radio", None)
    st.rerun()


def _run_cli(args: list[str], *, timeout: int = 120) -> dict[str, Any]:
    if not VENV_PYTHON.exists():
        return {"ok": False, "stdout": "", "stderr": f"trade venv 缺失: {VENV_PYTHON}", "returncode": -1}
    cmd = [str(VENV_PYTHON), "-m", "taa_futu.cli", *args]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(TRADE_DIR),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"超时 {timeout}s", "returncode": -1}
    except FileNotFoundError as exc:
        return {"ok": False, "stdout": "", "stderr": str(exc), "returncode": -1}
    return {
        "ok": completed.returncode == 0,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "returncode": completed.returncode,
    }


def _open_command(cmd_path: Path) -> None:
    if not cmd_path.exists():
        st.error(f"找不到 {cmd_path}")
        return
    try:
        subprocess.Popen(["open", str(cmd_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        st.success(f"已启动 {cmd_path.name}")
    except FileNotFoundError:
        st.error("无 'open' 命令；只能在 macOS 上启动 .command")


# ─────────────────────────── 通用 widgets ───────────────────────────


def render_top_status_bar() -> None:
    """常驻状态栏：邮差、OpenD、以及三个自动运行进程。

    只读文件，唯一的网络动作是对 OpenD 端口做 1.5 秒 socket 探测。
    """
    from taa_futu.demo_gateway import demo_enabled
    if demo_enabled():
        # 演示模式下不探测真实端口。否则会出现「横幅说未连接富途、状态栏却
        # 显示 OpenD 已连接」这种自相矛盾——本机恰好开着 OpenD 时就会这样，
        # 截图发出去更是误导。
        cols = st.columns([1, 1, 1, 1, 1])
        for col, label in zip(cols, ("邮差", "OpenD", "股票 auto", "加密 OFIM", "加密 Perp")):
            col.markdown(f"**{label}**: 🧪 演示")
        return

    cols = st.columns([1, 1, 1, 1, 1])
    # 邮差
    if FUTU_QUEUE_ALIVE.exists():
        ts = _parse_ts(FUTU_QUEUE_ALIVE.read_text(encoding="utf-8").strip())
        age_secs = (datetime.now(timezone.utc) - ts).total_seconds() if ts else None
        if age_secs is not None and age_secs < 30:
            cols[0].markdown(f"**邮差**: 🟢 {_age_str(ts)}")
        elif age_secs is not None and age_secs < 120:
            cols[0].markdown(f"**邮差**: 🟡 {_age_str(ts)}")
        else:
            cols[0].markdown(f"**邮差**: 🔴 stale")
    else:
        cols[0].markdown("**邮差**: ⚪ 未运行")
    # OpenD
    if _socket_open("127.0.0.1", 11111):
        cols[1].markdown("**OpenD**: 🟢 已连接")
    else:
        cols[1].markdown("**OpenD**: 🔴 未连接")
    # auto_trader
    at = _read_json(AUTO_TRADER_STATUS) or {}
    if at:
        cols[2].markdown(f"**股票 auto**: 🟢 {at.get('action', '?')}")
    else:
        cols[2].markdown("**股票 auto**: ⚪ 未启动")
    # crypto OFIM
    co = _read_json(CRYPTO_OFIM_STATUS) or {}
    if co:
        cols[3].markdown(f"**加密 OFIM**: 🟢 {co.get('action') or co.get('state') or '?'}")
    else:
        cols[3].markdown("**加密 OFIM**: ⚪ 未启动")
    # crypto Perp
    cp = _read_json(CRYPTO_PERP_STATUS) or {}
    if cp:
        cols[4].markdown(f"**加密 Perp**: 🟢 {cp.get('action') or cp.get('state') or '?'}")
    else:
        cols[4].markdown("**加密 Perp**: ⚪ 未启动")


def render_nav_breadcrumb(current_label: str) -> None:
    """Compact navigation row shown at the top of every non-home view."""
    cols = st.columns([1, 4, 1])
    if cols[0].button("← 返回首页", use_container_width=True, key=f"back_to_home_{current_label}"):
        _go_to(VIEW_HOME)
    cols[1].markdown(f"### {current_label}")
    if cols[2].button("🔄 刷新", use_container_width=True, key=f"refresh_{current_label}"):
        st.rerun()


# ─────────────────────────── 加密交易 完整版 ───────────────────────────


def render_crypto_trading_full(settings) -> None:
    """Full Crypto OFIM page — reuses the standalone app's body so the
    user gets everything the dedicated app gives them, without leaving
    the unified terminal."""
    render_nav_breadcrumb("💰 加密货币交易 / Crypto Trading")

    # ── Always-visible control panel (replaces the standalone .command
    # files we used to ship). All daemon control + mode switching lives
    # here so we don't litter ~/All here/ with one-shot scripts.
    _render_crypto_control_panel()

    tabs = st.tabs([
        "Spot OFIM 现货",
        "USD-M Perp 永续",
        "状态快照 / Quick Status",
    ])

    with tabs[0]:
        st.caption("以下是 Crypto OFIM Binance App 的完整功能 — 设置、连接、试算、模拟下单、信号、订单。")

        # ── Loss-guard banner: detect external Testnet balance reset ──
        # Binance Spot Testnet wipes account balances periodically. When
        # that happens our ledger thinks we lost money but trade_count is 0,
        # so Loss Guard locks the system into reduce_only mode. Surface this
        # specifically so the user doesn't think it's a strategy loss.
        ofim_status = _read_json(CRYPTO_OFIM_STATUS) or {}
        trend = ofim_status.get("benchmark_trend") or {}
        likely_cause = str(trend.get("likely_cause") or "")
        if likely_cause == "external_balance_change":
            cr = trend.get("cash_reconciliation") or {}
            st.warning(
                "🛑 **Loss Guard 已触发 — 但这不是策略亏损**\n\n"
                f"账本起点 `{cr.get('epoch_quote_cash', '?')} USDT` ≠ "
                f"Binance 实际余额 `{cr.get('actual_quote_cash', '?')} USDT`，"
                f"成交数: **{trend.get('trade_count', '?')}**（零成交说明不是策略行为）\n\n"
                "**最可能原因**：Binance Spot Testnet 周期性把账户重置到 10,000 USDT。"
                " 解决：点下方"
                " **🔄 重置账本起点 (testnet)** 按钮，让账本对齐到当前真实余额。"
            )
            rcol1, rcol2 = st.columns([1, 3])
            if rcol1.button("🔄 重置账本起点 (testnet)", type="primary", key="hub_ofim_ledger_reset"):
                with st.spinner("crypto-ofim-ledger-reset …"):
                    res = _run_cli(
                        ["crypto-ofim-ledger-reset", "--reason", "ui_testnet_balance_reset"],
                        timeout=30,
                    )
                if res["ok"]:
                    st.success("已重置账本起点 ✓ — Loss Guard 应该会在下个 cycle 解除")
                else:
                    st.error("重置失败")
                st.code(res["stdout"] or res["stderr"] or "(空)", language="text")
            rcol2.caption(
                "等价 CLI: `cd ~/All\\ here/trade && .venv/bin/taa-futu crypto-ofim-ledger-reset --reason testnet_balance_reset`"
            )

        try:
            from taa_futu.crypto_ofim_app import render_app_body
            render_app_body(key_prefix="hub_")
        except Exception as exc:
            st.error(f"加载 Crypto OFIM App 失败: {type(exc).__name__}: {exc}")
            st.caption("作为兜底，可以双击启动独立 App：")
            if st.button("打开 Crypto OFIM App (独立窗口)"):
                _open_command(CRYPTO_LAUNCHERS / "Open_Crypto_OFIM_App.command")

    with tabs[1]:
        _render_perp_quick(settings)

    with tabs[2]:
        _render_crypto_quick_status()


def _render_crypto_control_panel() -> None:
    """Streamlit-native replacement for the per-script .command files we
    used to scatter across ``~/All here/``. Lives inside the crypto page
    so daemon control + mode switching is one click away — no extra files
    on disk, no Terminal needed."""
    with st.expander("🎛️ Crypto 控制台 / Daemon & Mode Controls", expanded=False):
        # ── Daemon status snapshot ──
        ofim_pid_file = RUNTIME_DIR / "crypto_ofim" / "auto.pid"
        perp_pid_file = RUNTIME_DIR / "crypto_perp" / "auto.pid"
        ofim_pid = _read_pid_file(ofim_pid_file)
        perp_pid = _read_pid_file(perp_pid_file)
        ofim_running = _pid_alive_simple(ofim_pid)
        perp_running = _pid_alive_simple(perp_pid)

        # Current OFIM mode
        env_file = TRADE_DIR / ".env"
        cur_mode = _read_env_value(env_file, "CRYPTO_OFIM_MODE") or "paper"

        c1, c2, c3 = st.columns(3)
        c1.markdown(f"**OFIM**: {'🟢 running' if ofim_running else '⚪ stopped'} "
                    f"{'(pid=' + str(ofim_pid) + ')' if ofim_running else ''}")
        c2.markdown(f"**Perp**: {'🟢 running' if perp_running else '⚪ stopped'} "
                    f"{'(pid=' + str(perp_pid) + ')' if perp_running else ''}")
        c3.markdown(f"**OFIM mode**: `{cur_mode}`")

        st.divider()

        # ── OFIM mode switch ──
        st.markdown("**现货 OFIM mode 切换**")
        mc1, mc2, mc3 = st.columns(3)
        if mc1.button(
            "切到 paper (本地账本)" + (" ← 当前" if cur_mode == "paper" else ""),
            type="primary" if cur_mode != "paper" else "secondary",
            disabled=(cur_mode == "paper"),
            use_container_width=True, key="ofim_mode_paper",
        ):
            with st.spinner("切换 → paper + reset 账本 + 重启 daemon …"):
                ok, msg = _switch_ofim_mode("paper", reset_paper=True, restart_daemon=True)
            (st.success if ok else st.error)(msg)
        if mc2.button(
            "切到 testnet (Binance 测试网)" + (" ← 当前" if cur_mode == "testnet" else ""),
            type="primary" if cur_mode != "testnet" else "secondary",
            disabled=(cur_mode == "testnet"),
            use_container_width=True, key="ofim_mode_testnet",
        ):
            with st.spinner("切换 → testnet + 重启 daemon …"):
                ok, msg = _switch_ofim_mode("testnet", reset_paper=False, restart_daemon=True)
            (st.success if ok else st.error)(msg)
        mc3.caption("切换会自动备份当前 state + reset paper 账本（如切到 paper）+ 重启 OFIM daemon")

        st.divider()

        # ── Daemon start/stop ──
        st.markdown("**daemon 启停 / Daemon control**")
        d1, d2, d3, d4 = st.columns(4)
        if d1.button("🔄 重启 OFIM", use_container_width=True, key="ofim_restart",
                     disabled=False, help="kill 旧 daemon, nohup 启动新的"):
            with st.spinner("restart OFIM …"):
                ok, msg = _restart_crypto_daemon("ofim")
            (st.success if ok else st.error)(msg)
        if d2.button("⏹ 停止 OFIM", use_container_width=True, key="ofim_stop",
                     disabled=not ofim_running):
            ok, msg = _stop_crypto_daemon("ofim")
            (st.success if ok else st.warning)(msg)
        if d3.button("🔄 重启 Perp", use_container_width=True, key="perp_restart"):
            with st.spinner("restart Perp …"):
                ok, msg = _restart_crypto_daemon("perp")
            (st.success if ok else st.error)(msg)
        if d4.button("⏹ 停止 Perp", use_container_width=True, key="perp_stop",
                     disabled=not perp_running):
            ok, msg = _stop_crypto_daemon("perp")
            (st.success if ok else st.warning)(msg)


def _read_pid_file(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return 0


def _pid_alive_simple(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_env_value(env_file: Path, key: str) -> str | None:
    if not env_file.exists():
        return None
    try:
        for ln in env_file.read_text(encoding="utf-8").splitlines():
            if ln.startswith(f"{key}="):
                return ln.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _write_env_value(env_file: Path, key: str, value: str) -> None:
    import re
    text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    pat = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    new = pat.sub(f"{key}={value}", text) if pat.search(text) else (
        text + ("\n" if text and not text.endswith("\n") else "") + f"{key}={value}\n"
    )
    env_file.write_text(new, encoding="utf-8")


def _backup_crypto_state(daemon: str, label: str) -> Path:
    from datetime import datetime as _dt
    runtime = RUNTIME_DIR / ("crypto_ofim" if daemon == "ofim" else "crypto_perp")
    backup_root = runtime / "mode_switch_backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    ts = _dt.now().strftime("%Y%m%dT%H%M%S")
    dst = backup_root / f"{ts}_{label}"
    dst.mkdir(parents=True, exist_ok=True)
    import shutil
    for name in ("paper_state.json", "testnet_state.json", "status.json", "ledger_epoch.json"):
        src = runtime / name
        if src.exists():
            try:
                shutil.copy2(src, dst / name)
            except OSError:
                pass
    env = TRADE_DIR / ".env"
    if env.exists():
        try:
            import shutil
            shutil.copy2(env, dst / ".env")
        except OSError:
            pass
    return dst


def _switch_ofim_mode(target_mode: str, *, reset_paper: bool, restart_daemon: bool) -> tuple[bool, str]:
    if target_mode not in {"paper", "testnet"}:
        return False, f"invalid mode: {target_mode}"
    env_file = TRADE_DIR / ".env"
    try:
        backup_dir = _backup_crypto_state("ofim", f"to_{target_mode}")
    except Exception as exc:
        return False, f"backup failed: {exc}"

    try:
        _write_env_value(env_file, "CRYPTO_OFIM_MODE", target_mode)
    except Exception as exc:
        return False, f"write .env failed: {exc}"

    msgs = [f"✓ CRYPTO_OFIM_MODE = {target_mode} (备份在 {backup_dir})"]

    if reset_paper and target_mode == "paper":
        res = _run_cli(["crypto-ofim-reset"], timeout=30)
        if res["ok"]:
            msgs.append("✓ paper 账本已 reset 到 10000 USDT")
        else:
            msgs.append(f"⚠ reset 失败: {res['stderr'] or res['stdout'][:200]}")

    if restart_daemon:
        ok, daemon_msg = _restart_crypto_daemon("ofim")
        msgs.append(("✓ " if ok else "⚠ ") + daemon_msg)
        return ok, "\n".join(msgs)

    return True, "\n".join(msgs)


def _restart_crypto_daemon(daemon: str) -> tuple[bool, str]:
    """Stop the named crypto daemon, then nohup-start a new one detached
    from this streamlit process so it survives dashboard restarts."""
    if daemon not in {"ofim", "perp"}:
        return False, f"unknown daemon: {daemon}"
    stop_ok, stop_msg = _stop_crypto_daemon(daemon)

    # Start fresh
    cli_cmd = "crypto-ofim-auto" if daemon == "ofim" else "crypto-perp-auto"
    runtime = RUNTIME_DIR / (f"crypto_{daemon}")
    runtime.mkdir(parents=True, exist_ok=True)
    pid_file = runtime / "auto.pid"
    log_file = runtime / "auto.log"

    if not VENV_PYTHON.exists():
        return False, f"venv not found: {VENV_PYTHON}"

    try:
        # Open log in append mode and pass as stdout/stderr
        log_fh = log_file.open("a", encoding="utf-8")
        proc = subprocess.Popen(
            [str(VENV_PYTHON), "-m", "taa_futu.cli", cli_cmd],
            cwd=str(TRADE_DIR),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # detach from streamlit
        )
        pid_file.write_text(str(proc.pid), encoding="utf-8")
        return True, f"{stop_msg}; started new {daemon} pid={proc.pid}"
    except Exception as exc:
        return False, f"{stop_msg}; failed to start: {type(exc).__name__}: {exc}"


def _stop_crypto_daemon(daemon: str) -> tuple[bool, str]:
    if daemon not in {"ofim", "perp"}:
        return False, f"unknown daemon: {daemon}"
    pid_file = RUNTIME_DIR / f"crypto_{daemon}" / "auto.pid"
    killed = []
    pid = _read_pid_file(pid_file)
    if pid and _pid_alive_simple(pid):
        try:
            import signal as _signal
            os.kill(pid, _signal.SIGTERM)
            killed.append(str(pid))
        except OSError:
            pass
        # Wait up to 5s for graceful exit
        import time as _time
        for _ in range(5):
            if not _pid_alive_simple(pid):
                break
            _time.sleep(1)
        if _pid_alive_simple(pid):
            try:
                os.kill(pid, _signal.SIGKILL)
            except OSError:
                pass
    # pkill fallback
    try:
        pattern = "taa_futu.cli " + ("crypto-ofim-auto" if daemon == "ofim" else "crypto-perp-auto")
        subprocess.run(["pkill", "-f", pattern], check=False, capture_output=True)
    except FileNotFoundError:
        pass
    if pid_file.exists():
        try:
            pid_file.unlink()
        except OSError:
            pass
    if killed:
        return True, f"stopped {daemon} pid={killed[0]}"
    return True, f"{daemon} was already stopped"


def _render_perp_quick(settings) -> None:
    st.subheader("USD-M Futures Perp — 状态 + CLI")
    data = _read_json(CRYPTO_PERP_STATUS)
    if not data:
        st.info("未启动 — 没有 status.json。点 `Run Once (paper)` 或 `Start Auto` 启动一轮。")
    else:
        age = _age_str(_parse_ts(data.get("updated_at") or data.get("ts")))
        action = data.get("action") or data.get("state") or "?"
        st.markdown(f"**状态**: {_state_badge('ok' if action.lower() not in ('error', 'failed') else 'fail')} `{action}` · {age}")
        for key, label in [
            ("paper_equity", "Paper Equity"),
            ("paper_pnl", "Paper P&L"),
            ("paper_positions", "Positions"),
            ("regime", "Regime"),
            ("cycle_id", "Cycle ID"),
        ]:
            if key in data and data[key] is not None:
                st.markdown(f"- **{label}**: `{data[key]}`")
        with st.expander("原始 status.json"):
            st.json(data)

    with st.expander("最近事件 / Recent events", expanded=False):
        events = _tail_jsonl(CRYPTO_PERP_EVENTS, n=20)
        if events:
            df = pd.DataFrame(events)
            st.dataframe(df, use_container_width=True, height=300)
        else:
            st.caption("无事件 / no events")

    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Status", use_container_width=True, key="perp_full_status"):
        res = _run_cli(["crypto-perp-status"], timeout=30)
        st.code(res["stdout"] or res["stderr"] or "(空)", language="text")
    if c2.button("Check (连接)", use_container_width=True, key="perp_full_check"):
        res = _run_cli(["crypto-perp-check"], timeout=30)
        st.code(res["stdout"] or res["stderr"] or "(空)", language="text")
    if c3.button("Explain (最近决策)", use_container_width=True, key="perp_full_explain"):
        res = _run_cli(["crypto-perp-explain"], timeout=30)
        st.code(res["stdout"] or res["stderr"] or "(空)", language="text")
    if c4.button("Run Once (paper)", use_container_width=True, key="perp_full_once"):
        with st.spinner("crypto-perp-once …"):
            res = _run_cli(["crypto-perp-once"], timeout=180)
        st.code((res["stdout"] or "") + ("\nSTDERR:\n" + res["stderr"] if res["stderr"] else ""), language="text")


def _render_crypto_quick_status() -> None:
    st.subheader("两个 sleeve 的快照对比 / Side-by-side")
    cols = st.columns(2)
    with cols[0]:
        st.markdown("### Spot OFIM")
        data = _read_json(CRYPTO_OFIM_STATUS) or {}
        if data:
            st.json(data)
        else:
            st.caption("未启动")
    with cols[1]:
        st.markdown("### USD-M Perp")
        data = _read_json(CRYPTO_PERP_STATUS) or {}
        if data:
            st.json(data)
        else:
            st.caption("未启动")


# ─────────────────────────── 选股器 完整版 ───────────────────────────


def render_screener_full(settings) -> None:
    """Online stock screener — multi-factor pick on top of live-signal,
    plus AH multi-factor scanner outputs and a hatch to the desktop app."""
    render_nav_breadcrumb("🔍 选股器 / Screener")

    tabs = st.tabs([
        "在线选股 / Online Pick",
        "AH 多因子扫描",
        "桌面 App / Native",
    ])

    with tabs[0]:
        _render_online_pick(settings)

    with tabs[1]:
        _render_ah_scan()

    with tabs[2]:
        _render_native_screener()


def _render_online_pick(settings) -> None:
    """Live multi-sleeve pick: run live-signal on the full universe and let
    the user filter / sort the result inline."""
    st.subheader("在线选股 / Live-signal driven Pick")
    st.caption(
        "用四 sleeve（baseline+fusion+ofim+cascade）实时评分 universe，按 stack 综合权重排序。"
        "Read-only：不下单、不影响任何在跑的 auto_trader。"
    )

    fusion_uni = list(getattr(settings, "fusion_universe", ()) or ())
    ofim_uni = list(getattr(settings, "ofim_universe", ()) or ())

    cols = st.columns([2, 1, 1, 1])
    with cols[0]:
        symbols_raw = st.text_area(
            "Universe (留空 = 默认 FUSION_UNIVERSE)",
            value=", ".join(fusion_uni),
            height=80,
            key="screener_universe",
        )
    with cols[1]:
        min_total = st.number_input(
            "最低 stack_weight",
            value=0.0,
            min_value=0.0,
            max_value=1.0,
            step=0.01,
            key="screener_min_total",
        )
    with cols[2]:
        only_eligible = st.checkbox("仅 fusion eligible", value=False, key="screener_only_eligible")
    with cols[3]:
        run = st.button("跑选股 / Pick", type="primary", use_container_width=True)

    if not run:
        st.info("点 跑选股 / Pick 后内部调用 `taa-futu live-signal --json`，约 5-15 秒。")
        return

    symbols = [s.strip() for s in symbols_raw.replace(",", " ").split() if s.strip()]
    args = ["live-signal", "--json"]
    for sym in symbols:
        args.extend(["--symbol", sym])

    with st.spinner(f"跑 live-signal × {len(symbols) or 'default universe'} 标的…"):
        res = _run_cli(args, timeout=120)

    if not res["ok"]:
        st.error("查询失败")
        st.code(res["stderr"] or res["stdout"] or "(空)", language="text")
        return

    try:
        report = json.loads(res["stdout"])
    except json.JSONDecodeError as exc:
        st.error(f"解析 JSON 失败: {exc}")
        return

    by_symbol = report.get("by_symbol", {}) or {}
    rows = []
    for sym, payload in by_symbol.items():
        if not isinstance(payload, dict):
            continue
        total = float(payload.get("stack_target_weight") or 0.0)
        if total < min_total:
            continue
        fusion_entry = payload.get("fusion") or {}
        fusion_eligible = bool(fusion_entry.get("eligible"))
        if only_eligible and not fusion_eligible:
            continue
        rows.append({
            "symbol": sym,
            "stack_weight": total,
            "fusion_score": fusion_entry.get("score"),
            "fusion_eligible": "✓" if fusion_eligible else "",
            "fusion_reason": fusion_entry.get("reason"),
            "ofim_weight": float((payload.get("ofim") or {}).get("scaled_weight") or 0.0),
            "cascade_weight": float((payload.get("cascade") or {}).get("scaled_weight") or 0.0),
            "baseline_weight": float((payload.get("baseline") or {}).get("scaled_weight") or 0.0),
            "recommendation": payload.get("recommendation", "—"),
            "held": "●" if payload.get("held") else "",
        })

    if not rows:
        st.warning("没有标的命中筛选条件 — 试着降低最低 stack_weight 阈值")
        return

    df = pd.DataFrame(rows).sort_values("stack_weight", ascending=False)
    st.dataframe(
        df.style.format({
            "stack_weight": "{:.4f}",
            "fusion_score": "{:.4f}",
            "ofim_weight": "{:.4f}",
            "cascade_weight": "{:.4f}",
            "baseline_weight": "{:.4f}",
        }),
        use_container_width=True,
        hide_index=True,
        height=480,
    )
    st.caption(f"命中 {len(rows)} 个；queried_symbols={len(report.get('queried_symbols', []))}，errors={len(report.get('errors', []))}")
    if report.get("errors"):
        with st.expander("⚠ degraded — errors"):
            for e in report["errors"]:
                st.code(e)


def _render_ah_scan() -> None:
    """AH multi-factor scan results — connects to news collector's output."""
    st.subheader("AH 多因子扫描 / AH Multi-Factor Scan")
    st.caption(
        "扫描动态 universe、A 股连板、缩量上涨、近三年新高。"
        "扫描在 `news collector/` 跑（独立进程），结果落到 reports/live/scan_*.json。"
    )

    bc1, bc2, bc3 = st.columns(3)
    if bc1.button("跑一次 AH 扫描", use_container_width=True):
        _open_command(NEWS_COLLECTOR / "scripts" / "AH_Multi_Factor_Scanner.command")
        st.info("已在 Terminal 启动 — 约 30-90 秒完成 — 完了刷新页面看新文件")
    if bc2.button("启用动态 universe", use_container_width=True):
        _open_command(NEWS_COLLECTOR / "scripts" / "Enable_Dynamic_Universe.command")
    if bc3.button("禁用动态 universe", use_container_width=True):
        _open_command(NEWS_COLLECTOR / "scripts" / "Disable_Dynamic_Universe.command")

    st.divider()
    if not NEWS_SCAN_LIVE.exists():
        st.info(f"扫描输出目录不存在: {NEWS_SCAN_LIVE}")
        return

    scans = sorted(NEWS_SCAN_LIVE.glob("scan_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not scans:
        st.info("还没有 scan_*.json — 先跑一次 AH 扫描")
        return

    pick = st.selectbox(
        "选择扫描文件 / Pick scan file",
        options=scans[:30],
        format_func=lambda p: f"{p.name} ({datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m-%d %H:%M')})",
        key="screener_scan_pick",
    )
    if pick:
        data = _read_json(pick)
        if not data:
            st.warning("无法解析该文件")
            return
        for name in ("universe", "limit_up_streak", "volume_shrink_up", "near_ath"):
            section = data.get(name)
            if not section:
                continue
            st.markdown(f"#### {name}")
            rows = section if isinstance(section, list) else section.get("items") or []
            if rows:
                df = pd.DataFrame(rows)
                st.dataframe(df, use_container_width=True, height=260)
            else:
                st.caption("空")
        with st.expander("原始 JSON"):
            st.json(data)


def _render_native_screener() -> None:
    """Launch the desktop Tkinter screener (cannot be embedded in Streamlit)."""
    st.subheader("桌面选股 App / Native Screener")
    st.caption(
        "futu_stock_screener_desktop.py 是一个独立的 Tkinter 桌面 app（拉富途板块 + 实时筛选）。"
        "它不能直接嵌进 Streamlit 浏览器页面 — 点下面按钮会开一个新的窗口。"
    )
    # 原来这里还有第二个按钮「打开 TAA Quant Trading App」，2026-07-31 去掉：
    # 它开的是旧的 TAA Quant Trading.app，而你现在正看的这个页面就是它，
    # 桌面上的寻宝猫也是它。留着只会让人以为还有个别的程序。
    if st.button("打开 Stock Screener", use_container_width=True):
        _open_command(STOCK_LAUNCHERS / "Open_Stock_Screener.command")


# ─────────────────────────── 实时建议（独立子页） ───────────────────────────


def render_live_signal(settings) -> None:
    render_nav_breadcrumb("🤖 实时建议 / Live Signal")
    st.caption("四 sleeve 综合读判 (read-only, 不下单). 复用 auto_trader 的同一套评分但不通过下单链路.")

    default_syms = "US.NVDA, US.TSLA, US.QQQ, US.SPY"
    raw = st.text_input(
        "Symbols (逗号或空格分隔，留空回退到 FUSION_UNIVERSE)",
        value=st.session_state.get("live_signal_symbols", default_syms),
        key="live_signal_symbols",
    )
    cols = st.columns([0.3, 0.3, 0.4])
    with cols[0]:
        include_universe = st.checkbox("包括 universe view", value=False, key="live_signal_include_universe")
    with cols[1]:
        compact = st.checkbox("仅显示有目标权重的", value=False, key="live_signal_compact")
    with cols[2]:
        do_query = st.button("查询 / Query", type="primary", use_container_width=True)

    symbols = [s.strip() for s in raw.replace(",", " ").split() if s.strip()]

    if not do_query:
        st.info("点 查询 / Query 后实时调用 `taa-futu live-signal --json`，约 5-15 秒返回。")
        return

    with st.spinner(f"查询 {len(symbols) or 'universe'} 个标的 — 内部跑 baseline+fusion+ofim+cascade…"):
        args = ["live-signal", "--json"]
        if not include_universe:
            args.append("--no-universe")
        for sym in symbols:
            args.extend(["--symbol", sym])
        res = _run_cli(args, timeout=120)

    if not res["ok"]:
        st.error("查询失败")
        st.code(res["stderr"] or res["stdout"] or "(空)", language="text")
        return

    try:
        report = json.loads(res["stdout"])
    except json.JSONDecodeError as exc:
        st.error(f"解析 JSON 失败: {exc}")
        return

    gen = report.get("generated_at", "?")
    stack = report.get("stack_label", "?")
    errors = report.get("errors", []) or []
    h_left, h_right = st.columns([0.75, 0.25])
    with h_left:
        st.markdown(f"**生成时间**: `{gen}`")
        st.markdown(f"**Stack**: {stack}")
    with h_right:
        if errors:
            st.warning(f"⚠ degraded · {len(errors)} error(s)")
        else:
            st.success("✓ healthy")

    by_symbol = report.get("by_symbol", {}) or {}
    queried = report.get("queried_symbols", []) or []
    ordered = list(queried) + [s for s in by_symbol.keys() if s not in queried]

    def _w(payload: dict, sleeve: str) -> float:
        entry = (payload or {}).get(sleeve)
        if not isinstance(entry, dict):
            return 0.0
        return float(entry.get("scaled_weight") or entry.get("weight") or 0.0)

    rows = []
    for sym in ordered:
        payload = by_symbol.get(sym) or {}
        total = float(payload.get("stack_target_weight") or 0.0)
        if compact and total == 0.0 and not payload.get("held"):
            continue
        rows.append({
            "symbol": sym,
            "baseline": _w(payload, "baseline"),
            "fusion": _w(payload, "fusion"),
            "ofim": _w(payload, "ofim"),
            "cascade": _w(payload, "cascade"),
            "stack_weight": total,
            "recommendation": payload.get("recommendation", "—"),
            "held": "●" if payload.get("held") else "",
        })
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(
            df.style.format({
                "baseline": "{:.4f}",
                "fusion": "{:.4f}",
                "ofim": "{:.4f}",
                "cascade": "{:.4f}",
                "stack_weight": "{:.4f}",
            }),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("没有标的命中")

    if include_universe:
        uv = report.get("universe_view") or {}
        st.divider()
        st.markdown("### Universe View")
        cols = st.columns(3, gap="medium")
        with cols[0]:
            st.markdown(f"**Fusion benchmark score**: `{uv.get('fusion_benchmark_score', '—')}`")
            ff = uv.get("fusion_features") or []
            if ff:
                st.dataframe(pd.DataFrame(ff), use_container_width=True, height=280)
        with cols[1]:
            st.markdown(f"**OFIM benchmark**: `{uv.get('ofim_benchmark_score', '—')}`")
            ot = uv.get("ofim_top") or []
            if ot:
                st.dataframe(pd.DataFrame(ot), use_container_width=True, height=280)
        with cols[2]:
            st.markdown(f"**Cascade regime**: `{uv.get('cascade_regime_label', '—')}`")
            ct = uv.get("cascade_targets") or []
            if ct:
                st.dataframe(pd.DataFrame(ct), use_container_width=True, height=280)

    with st.expander("原始 JSON"):
        st.json(report)


# Legacy compatibility: keep these names so any caller still using the
# pre-home dispatch (e.g. older dashboard_app.main()) continues working.
PAGE_CRYPTO = "💰 加密交易 / Crypto Trading"
PAGE_SCREENER = "🔍 选股器 / Screener"
PAGE_LIVE_SIGNAL = "🤖 实时建议 / Live Signal"
