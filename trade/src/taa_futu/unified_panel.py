"""All Here 总控制台 — unified Tkinter launcher for the three live systems.

Scope (per user request 2026-05-27):

* **股票交易** (Stock Trading) — TAA Baseline + Fusion + OFIM + Cascade stack.
* **加密货币交易** (Crypto Trading) — independent Binance OFIM + Perp sleeves.
* **选股器** (Screener) — Futu desktop stock screener.
* **Live-Signal** — the new read-only multi-sleeve recommendation we added on
  the same day; queries the same stack without submitting orders.

Plus a permanent status bar showing **邮差** (futu_watcher) and **OpenD**
connectivity — these are infrastructure the three systems depend on, so they
are surfaced globally rather than buried in a sub-tab.

Design tenets:

* **Router, not rewrite.** Every launcher button shells out to the original
  ``.command`` file. We don't re-implement starter logic in Python; that
  duplication is the bug the user is actually trying to fix (40+ scattered
  launchers). The panel is the index, the ``.command`` files are the truth.
* **Read-only status.** Status cards parse existing status JSON / pid files /
  socket probes. The panel never writes to ``auto_trader_state.json`` or
  anything load-bearing.
* **No new daemons.** This file is a foreground Tk app — closing the window
  does not affect any background process.
* **Failure-tolerant.** Missing files, dead processes, broken JSON — every
  read is wrapped so the panel keeps refreshing even when something
  underneath is down.

Run via ``taa-futu-unified`` (registered in pyproject.toml) or directly:

    cd ~/All\\ here/trade && .venv/bin/python -m taa_futu.unified_panel
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable


# ─────────────────────────────────────────────────────────────────────────────
# Paths — derived from this file's location, not from a hardcoded workspace.
# Resolved at module load so a missing file fails loudly rather than at
# button-press time.
# ─────────────────────────────────────────────────────────────────────────────

# TRADE_DIR is derived from this file's own location so the repo runs from any
# folder on any machine. ALL_HERE is the surrounding workspace that holds the
# sibling components (futu_queue, news collector); override it with the
# ALL_HERE_ROOT environment variable if your layout differs.
TRADE_DIR = Path(os.environ.get("TAA_TRADE_ROOT") or Path(__file__).resolve().parents[2])
HOME = Path.home()
ALL_HERE = Path(os.environ.get("ALL_HERE_ROOT") or TRADE_DIR.parent)
RUNTIME_DIR = TRADE_DIR / "runtime"

AUTO_TRADER_STATUS = RUNTIME_DIR / "auto_trader_status.json"
WATCHDOG_STATUS = RUNTIME_DIR / "watchdog_status.json"
CRYPTO_OFIM_STATUS = RUNTIME_DIR / "crypto_ofim" / "status.json"
CRYPTO_PERP_STATUS = RUNTIME_DIR / "crypto_perp" / "status.json"

FUTU_QUEUE_ALIVE = ALL_HERE / "futu_queue" / "_watcher_alive.txt"

VENV_PYTHON = TRADE_DIR / ".venv" / "bin" / "python"

STOCK_LAUNCHERS = TRADE_DIR / "stock" / "launchers"
CRYPTO_LAUNCHERS = TRADE_DIR / "crypto" / "launchers"
CLAUDE_TRADE_DIR = TRADE_DIR / "claude-trade"

# Status colors that match the existing market_news Dashboard convention.
COLOR_OK = "#2ecc71"
COLOR_WARN = "#f1c40f"
COLOR_FAIL = "#e74c3c"
COLOR_IDLE = "#95a5a6"
COLOR_INFO = "#3498db"

REFRESH_MS = 5000  # status cards refresh every 5 seconds


# ─────────────────────────────────────────────────────────────────────────────
# Lightweight readers — never raise. Anything the panel can't parse is shown
# as "unknown" / idle so a single broken status file does not take down the UI.
# ─────────────────────────────────────────────────────────────────────────────


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


def _age_seconds(ts: datetime | None) -> float | None:
    if ts is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s 前"
    if seconds < 3600:
        return f"{seconds/60:.0f}min 前"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h 前"
    return f"{seconds/86400:.1f}d 前"


def _socket_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@dataclass
class CardData:
    """Boiled-down status one row at a time."""

    name: str
    state: str       # "ok" | "warn" | "fail" | "idle"
    headline: str    # short summary
    detail: str = "" # smaller secondary line


def _state_color(state: str) -> str:
    return {
        "ok": COLOR_OK,
        "warn": COLOR_WARN,
        "fail": COLOR_FAIL,
        "idle": COLOR_IDLE,
        "info": COLOR_INFO,
    }.get(state, COLOR_IDLE)


# ── Concrete status readers ─────────────────────────────────────────────────


def read_watcher_card() -> CardData:
    """邮差 (futu_watcher) — the queue daemon Claude uses to call Futu skills."""
    if not FUTU_QUEUE_ALIVE.exists():
        return CardData("邮差", "fail", "未运行", "找不到 _watcher_alive.txt — 双击 安装富途邮差.command")
    age = _age_seconds(_parse_ts(FUTU_QUEUE_ALIVE.read_text(encoding="utf-8").strip()))
    if age is None:
        return CardData("邮差", "warn", "心跳异常", "alive 文件无法解析时间戳")
    if age > 30:
        return CardData("邮差", "fail", f"{_fmt_age(age)} 心跳", "邮差停了 — 双击 重启邮差.command")
    if age > 10:
        return CardData("邮差", "warn", f"{_fmt_age(age)} 心跳", "心跳偏慢，留意")
    return CardData("邮差", "ok", "运行中", f"最近心跳 {_fmt_age(age)}")


def read_opend_card() -> CardData:
    """OpenD — Futu's local data/trading bridge on 127.0.0.1:11111."""
    if _socket_open("127.0.0.1", 11111):
        return CardData("OpenD", "ok", "已连接", "127.0.0.1:11111")
    return CardData("OpenD", "fail", "未连接", "请确保 FutuOpenD 已启动并登录")


def read_auto_trader_card() -> CardData:
    """股票 auto_trader — the long-running stack runner."""
    data = _read_json(AUTO_TRADER_STATUS)
    if not data:
        return CardData("auto_trader", "idle", "未启动", "可在「股票交易」tab 启动全天自动运行")
    age = _age_seconds(_parse_ts(data.get("updated_at")))
    action = data.get("action", "?")
    if not _pid_alive(data.get("pid")):
        return CardData("auto_trader", "fail", "进程已停 (pid 不存活)", f"上次状态: {action} · {_fmt_age(age)}")
    if age is not None and age > 240:
        return CardData("auto_trader", "warn", f"状态偏旧 {_fmt_age(age)}", f"action={action}")
    headline = action
    consecutive = data.get("consecutive_transient_count", 0)
    if consecutive and consecutive >= 1:
        return CardData(
            "auto_trader", "warn",
            f"{action} (lockdown {consecutive})",
            f"连续 transient {consecutive}/3 · {_fmt_age(age)}",
        )
    return CardData("auto_trader", "ok", headline, f"action={action} · {_fmt_age(age)}")


def read_watchdog_card() -> CardData:
    data = _read_json(WATCHDOG_STATUS)
    if not data:
        return CardData("watchdog", "idle", "未启动", "auto_trader 守护程序未运行")
    age = _age_seconds(_parse_ts(data.get("updated_at")))
    if not _pid_alive(data.get("pid")):
        return CardData("watchdog", "fail", "进程已停", f"上次状态: {data.get('action', '?')} · {_fmt_age(age)}")
    action = data.get("action", "?")
    if action == "error":
        return CardData("watchdog", "fail", "error", str(data.get("detail", ""))[:80])
    return CardData("watchdog", "ok", action, f"{_fmt_age(age)} · restart_count={data.get('restart_count', 0)}")


def read_crypto_ofim_card() -> CardData:
    data = _read_json(CRYPTO_OFIM_STATUS)
    if not data:
        return CardData("Crypto OFIM", "idle", "未启动", "Binance 现货 OFIM sleeve")
    age = _age_seconds(_parse_ts(data.get("updated_at") or data.get("ts")))
    action = data.get("action") or data.get("state") or "?"
    if action.lower() in ("error", "failed"):
        return CardData("Crypto OFIM", "fail", action, str(data.get("detail", ""))[:80])
    return CardData("Crypto OFIM", "ok", action, _fmt_age(age))


def read_crypto_perp_card() -> CardData:
    data = _read_json(CRYPTO_PERP_STATUS)
    if not data:
        return CardData("Crypto Perp", "idle", "未启动", "Binance USD-M Futures long/short sleeve")
    age = _age_seconds(_parse_ts(data.get("updated_at") or data.get("ts")))
    action = data.get("action") or data.get("state") or "?"
    if action.lower() in ("error", "failed"):
        return CardData("Crypto Perp", "fail", action, str(data.get("detail", ""))[:80])
    return CardData("Crypto Perp", "ok", action, _fmt_age(age))


# ─────────────────────────────────────────────────────────────────────────────
# Launcher invocation — the shell-out layer. Everything just calls the existing
# ``.command`` files via ``open`` so the user sees the same Terminal window
# they would get from double-clicking the file in Finder.
# ─────────────────────────────────────────────────────────────────────────────


def open_command_file(cmd_path: Path) -> tuple[bool, str]:
    """Open a .command file in Terminal (the macOS-native way to run it).

    Falls back to chmod+exec if ``open`` is unavailable (mostly for tests).
    Returns (ok, message) so the UI can display a status line.
    """
    if not cmd_path.exists():
        return False, f"找不到 {cmd_path.name}"
    try:
        # ``open`` on macOS routes .command through Terminal.app exactly the
        # same way a Finder double-click would.
        subprocess.Popen(
            ["open", str(cmd_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True, f"已启动 {cmd_path.name}"
    except FileNotFoundError:
        # Linux test environment fallback
        try:
            os.chmod(cmd_path, 0o755)
            subprocess.Popen([str(cmd_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, f"已启动 {cmd_path.name} (fallback)"
        except Exception as exc:
            return False, f"启动失败: {exc}"


def run_live_signal_subprocess(
    symbols: list[str], *, include_universe: bool = False, timeout: int = 90,
) -> dict[str, Any]:
    """Call ``taa-futu live-signal --json`` and return the parsed report.

    Returned dict either has ``ok=True`` + ``report`` (the parsed JSON) or
    ``ok=False`` + ``error``. Never raises — UI thread depends on that.
    """
    if not VENV_PYTHON.exists():
        return {"ok": False, "error": f"trade venv 缺失: {VENV_PYTHON}\n请先 cd ~/All\\ here/trade && uv venv && uv pip install -e .[dev]"}

    cmd = [str(VENV_PYTHON), "-m", "taa_futu.cli", "live-signal", "--json"]
    if not include_universe:
        cmd.append("--no-universe")
    for sym in symbols:
        sym = sym.strip()
        if not sym:
            continue
        cmd.extend(["--symbol", sym])

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(TRADE_DIR),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"live-signal 超时 ({timeout}s)"}
    except FileNotFoundError as exc:
        return {"ok": False, "error": f"无法启动 taa-futu: {exc}"}

    if completed.returncode != 0:
        return {
            "ok": False,
            "error": (completed.stderr or completed.stdout or "non-zero return").strip(),
        }
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"解析输出失败: {exc}\n{completed.stdout[:300]}"}
    return {"ok": True, "report": report}


# ─────────────────────────────────────────────────────────────────────────────
# UI building blocks
# ─────────────────────────────────────────────────────────────────────────────


class StatusBar(ttk.Frame):
    """Always-visible header showing 邮差 + OpenD (the infra everything needs)."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=(10, 6))
        self._cards: dict[str, tuple[tk.Canvas, ttk.Label, ttk.Label]] = {}
        for col, name in enumerate(("邮差", "OpenD")):
            container = ttk.Frame(self)
            container.grid(row=0, column=col, padx=8, sticky="w")
            dot = tk.Canvas(container, width=14, height=14, highlightthickness=0)
            dot.create_oval(2, 2, 12, 12, fill=COLOR_IDLE, outline="")
            dot.grid(row=0, column=0, rowspan=2, padx=(0, 6))
            head = ttk.Label(container, text=name, font=("Helvetica", 12, "bold"))
            head.grid(row=0, column=1, sticky="w")
            sub = ttk.Label(container, text="…", font=("Helvetica", 10), foreground="#555")
            sub.grid(row=1, column=1, sticky="w")
            self._cards[name] = (dot, head, sub)

        self.refresh_btn = ttk.Button(self, text="刷新", command=self._manual_refresh)
        self.refresh_btn.grid(row=0, column=10, padx=(40, 0), sticky="e")
        self.columnconfigure(9, weight=1)

        self._refresh_cb: Callable[[], None] | None = None

    def bind_refresh(self, cb: Callable[[], None]) -> None:
        self._refresh_cb = cb

    def _manual_refresh(self) -> None:
        if self._refresh_cb:
            self._refresh_cb()

    def update_card(self, name: str, card: CardData) -> None:
        if name not in self._cards:
            return
        dot, head, sub = self._cards[name]
        dot.delete("all")
        dot.create_oval(2, 2, 12, 12, fill=_state_color(card.state), outline="")
        head.config(text=f"{name}: {card.headline}")
        sub.config(text=card.detail)


class SystemCard(ttk.LabelFrame):
    """Reusable status card for a sub-system (auto_trader, watchdog, etc.)."""

    def __init__(self, master: tk.Misc, title: str) -> None:
        super().__init__(master, text=title, padding=8)
        self._dot = tk.Canvas(self, width=12, height=12, highlightthickness=0)
        self._dot.create_oval(1, 1, 11, 11, fill=COLOR_IDLE, outline="")
        self._dot.grid(row=0, column=0, padx=(0, 6), pady=2, sticky="w")
        self._headline = ttk.Label(self, text="—", font=("Helvetica", 11, "bold"))
        self._headline.grid(row=0, column=1, sticky="w")
        self._detail = ttk.Label(self, text="", foreground="#555", font=("Helvetica", 10))
        self._detail.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

    def update(self, card: CardData) -> None:
        self._dot.delete("all")
        self._dot.create_oval(1, 1, 11, 11, fill=_state_color(card.state), outline="")
        self._headline.config(text=card.headline)
        self._detail.config(text=card.detail)


def _grid_buttons(parent: tk.Misc, specs: list[tuple[str, Callable[[], None]]], cols: int = 3) -> None:
    """Lay out a label-callback grid of fixed-width buttons.

    Buttons are equally sized so the panel stays scannable regardless of how
    many launchers a sub-system exposes.
    """
    for idx, (label, cb) in enumerate(specs):
        r, c = divmod(idx, cols)
        btn = ttk.Button(parent, text=label, command=cb, width=22)
        btn.grid(row=r, column=c, padx=4, pady=4, sticky="ew")
    for c in range(cols):
        parent.columnconfigure(c, weight=1)


# ─────────────────────────────────────────────────────────────────────────────
# Tab: 股票交易
# ─────────────────────────────────────────────────────────────────────────────


class StockTab(ttk.Frame):
    """股票交易 — Baseline + Fusion + OFIM + Cascade stack."""

    def __init__(self, master: tk.Misc, app: "UnifiedPanel") -> None:
        super().__init__(master, padding=12)
        self.app = app

        intro = ttk.Label(
            self,
            text="股票交易 / Stock Trading — TAA Quant 四 sleeve 栈",
            font=("Helvetica", 13, "bold"),
        )
        intro.pack(anchor="w", pady=(0, 8))

        # ── Status row ──
        status_row = ttk.Frame(self)
        status_row.pack(fill="x", pady=(0, 10))
        self.card_auto = SystemCard(status_row, "auto_trader")
        self.card_auto.grid(row=0, column=0, padx=4, sticky="ew")
        self.card_watchdog = SystemCard(status_row, "watchdog")
        self.card_watchdog.grid(row=0, column=1, padx=4, sticky="ew")
        status_row.columnconfigure(0, weight=1)
        status_row.columnconfigure(1, weight=1)

        # ── Stack detail ──
        self.stack_label = ttk.Label(self, text="Stack: —", foreground="#444")
        self.stack_label.pack(anchor="w", pady=(0, 8))
        self.cycle_label = ttk.Label(self, text="今日活动: —", foreground="#444")
        self.cycle_label.pack(anchor="w", pady=(0, 12))

        # ── Launchers ──
        launchers = ttk.LabelFrame(self, text="启动器 / Launchers", padding=10)
        launchers.pack(fill="x", pady=(0, 10))

        # 2026-07-31 移除三个按钮，它们指向的启动器已随桌面整理移走：
        #   「打开 监控Dashboard」「打开 TAA App」——两者都只是起交易终端，
        #       现在桌面上的寻宝猫就是干这个的，而且用应用窗口而非浏览器标签页。
        #   「修复启动脚本」——它修的是桌面上的「启动量化交易控制台.command」，
        #       那个文件早已不在桌面，按钮点了也没有对象可修。
        _grid_buttons(launchers, [
            ("启动 量化交易控制台", lambda: self.app.run_launcher(STOCK_LAUNCHERS / "Launch_Trading_Control_Panel.command")),
            ("启动 全天自动运行", lambda: self.app.run_launcher(STOCK_LAUNCHERS / "Start_All_Day_Auto_Run.command")),
            ("停止 全天自动运行", lambda: self.app.run_launcher(STOCK_LAUNCHERS / "Stop_All_Day_Auto_Run.command")),
            ("取消所有挂单", lambda: self.app.run_launcher(STOCK_LAUNCHERS / "Cancel_All_Orders.command")),
            ("重启 Dashboard", lambda: self.app.run_launcher(STOCK_LAUNCHERS / "重启Dashboard.command")),
            ("Cascade 总控制台", lambda: self.app.run_launcher(CLAUDE_TRADE_DIR / "总控制台.command")),
        ])

        # ── Pre-gate radio ──
        pregate = ttk.LabelFrame(self, text="Fusion 富途盘前过滤 / Pre-gate", padding=10)
        pregate.pack(fill="x", pady=(0, 10))
        ttk.Label(pregate, text="选择后双击对应 .command（写入 .env，下个 cycle 生效）：").pack(anchor="w", pady=(0, 6))
        pregate_row = ttk.Frame(pregate)
        pregate_row.pack(anchor="w")
        ttk.Button(pregate_row, text="Off (关闭)", width=14,
                   command=lambda: self.app.run_launcher(STOCK_LAUNCHERS / "Pregate_Off.command")).pack(side="left", padx=4)
        ttk.Button(pregate_row, text="LogOnly (只记录)", width=18,
                   command=lambda: self.app.run_launcher(STOCK_LAUNCHERS / "Pregate_LogOnly.command")).pack(side="left", padx=4)
        ttk.Button(pregate_row, text="Active (生效)", width=14,
                   command=lambda: self.app.run_launcher(STOCK_LAUNCHERS / "Pregate_Active.command")).pack(side="left", padx=4)

        # ── Diagnostic ──
        diag = ttk.LabelFrame(self, text="诊断 / Diagnostics", padding=10)
        diag.pack(fill="x", pady=(0, 10))
        _grid_buttons(diag, [
            ("系统体检 Doctor", self._run_stock_doctor),
            ("打开 auto_trader.log", lambda: self.app.open_path(RUNTIME_DIR / "auto_trader.log")),
            ("打开 watchdog.log", lambda: self.app.open_path(RUNTIME_DIR / "watchdog.log")),
        ])

    def _run_stock_doctor(self) -> None:
        """Run stock-system-doctor in a Terminal so the user sees the report."""
        if not VENV_PYTHON.exists():
            self.app.toast(f"trade venv 缺失: {VENV_PYTHON}")
            return
        # Write a tiny inline shell so Terminal stays open with the result.
        helper = TRADE_DIR / "runtime" / "_unified_panel_doctor.command"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text(
            "#!/bin/bash\n"
            f"cd '{TRADE_DIR}'\n"
            f"'{VENV_PYTHON}' -m taa_futu.cli stock-system-doctor\n"
            "echo\n"
            "echo '按任意键关闭…'\n"
            "read -n 1\n",
            encoding="utf-8",
        )
        os.chmod(helper, 0o755)
        self.app.run_launcher(helper)

    def refresh(self) -> None:
        self.card_auto.update(read_auto_trader_card())
        self.card_watchdog.update(read_watchdog_card())
        # Stack + cycle activity from auto_trader_status.json
        data = _read_json(AUTO_TRADER_STATUS)
        if data:
            detail = str(data.get("detail", ""))
            if "stack=" in detail:
                stack_part = detail.split("stack=", 1)[1].split(" |", 1)[0]
                self.stack_label.config(text=f"Stack: {stack_part}")
            cycle_summary = (
                f"今日 cycle: planned={data.get('cumulative_planned_orders', '?')} "
                f"submitted={data.get('cumulative_submitted_orders', '?')} "
                f"fills={data.get('cumulative_recorded_fills', '?')} "
                f"· lockdown counter={data.get('consecutive_transient_count', 0)}/3"
            )
            self.cycle_label.config(text=cycle_summary)
        else:
            self.stack_label.config(text="Stack: 未启动")
            self.cycle_label.config(text="今日活动: —")


# ─────────────────────────────────────────────────────────────────────────────
# Tab: 完整控制台 (embeds the legacy ControlPanel inside the unified window)
# ─────────────────────────────────────────────────────────────────────────────


class FullControlPanelTab(ttk.Frame):
    """Embeds the legacy 1500x960 ControlPanel into a notebook tab.

    The ControlPanel constructor is expensive (loads .env, queries Futu OpenD
    status on a refresh tick, builds 100+ widgets), so we lazy-load it: the
    first time the user clicks this tab we run ``_ensure_loaded`` which
    constructs ``ControlPanel(master=self)`` and packs its root frame here.
    Subsequent tab switches are free.

    If ControlPanel fails to load (missing tcl/tk, missing venv, etc.) we
    show the error inline instead of crashing the host window.
    """

    def __init__(self, master: tk.Misc, app: "UnifiedPanel") -> None:
        super().__init__(master, padding=0)
        self.app = app
        self._control_panel: Any | None = None
        self._loaded = False

        self._placeholder = ttk.Frame(self, padding=24)
        self._placeholder.pack(fill="both", expand=True)
        ttk.Label(
            self._placeholder,
            text="🎛️ 完整 TAA Quant 控制台 (embedded)",
            font=("Helvetica", 16, "bold"),
        ).pack(anchor="w", pady=(0, 8))
        ttk.Label(
            self._placeholder,
            text=(
                "把原来双击 启动量化交易控制台.command 启动的独立 1500×960 窗口\n"
                "嵌入到这个 tab 里。第一次切到这里时加载（5–10 秒构建 UI）。\n\n"
                "切到这个 tab 即自动加载；如果加载失败，点 重试 / Retry。"
            ),
            justify="left",
            foreground="#444",
        ).pack(anchor="w", pady=(0, 12))
        ttk.Button(self._placeholder, text="立即加载 / Load now", command=self._ensure_loaded).pack(anchor="w")

        # Schedule a lazy load on the first time this tab gets shown. We
        # cannot detect "tab clicked" from inside the Frame; instead the host
        # binds <<NotebookTabChanged>> and calls ``_ensure_loaded`` for us.

    def refresh(self) -> None:
        # ControlPanel manages its own refresh tick once loaded; no-op here.
        pass

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True  # set first so a second concurrent call is a no-op
        # Replace the placeholder text mid-load so the user sees progress.
        for child in self._placeholder.winfo_children():
            child.destroy()
        ttk.Label(
            self._placeholder,
            text="加载中…构建 1500×960 控制台 UI",
            font=("Helvetica", 13),
        ).pack(pady=24)
        self.update_idletasks()

        try:
            # Deferred import to avoid pulling the whole ControlPanel stack
            # (which imports streamlit, plotly etc.) just to open the panel.
            from taa_futu.control_panel import ControlPanel  # type: ignore

            cp = ControlPanel(master=self)
            self._control_panel = cp
            # Replace placeholder with the embedded panel's frame
            self._placeholder.destroy()
            cp.root.pack(fill="both", expand=True)
        except Exception as exc:
            # Show the failure inline so the rest of the unified panel keeps
            # working — do not raise into the Tk main loop.
            self._loaded = False
            for child in self._placeholder.winfo_children():
                child.destroy()
            ttk.Label(
                self._placeholder,
                text=f"加载失败 / Load failed:\n{type(exc).__name__}: {exc}",
                foreground=COLOR_FAIL,
                justify="left",
            ).pack(anchor="w", pady=(0, 12))
            ttk.Button(self._placeholder, text="重试 / Retry", command=self._ensure_loaded).pack(anchor="w")


# ─────────────────────────────────────────────────────────────────────────────
# Tab: 加密货币交易
# ─────────────────────────────────────────────────────────────────────────────


class CryptoTab(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "UnifiedPanel") -> None:
        super().__init__(master, padding=12)
        self.app = app

        intro = ttk.Label(
            self,
            text="加密货币交易 / Crypto Trading — Binance Spot OFIM + USD-M Futures Perp",
            font=("Helvetica", 13, "bold"),
        )
        intro.pack(anchor="w", pady=(0, 8))

        status_row = ttk.Frame(self)
        status_row.pack(fill="x", pady=(0, 10))
        self.card_ofim = SystemCard(status_row, "Crypto OFIM (Spot)")
        self.card_ofim.grid(row=0, column=0, padx=4, sticky="ew")
        self.card_perp = SystemCard(status_row, "Crypto Perp (USD-M Futures)")
        self.card_perp.grid(row=0, column=1, padx=4, sticky="ew")
        status_row.columnconfigure(0, weight=1)
        status_row.columnconfigure(1, weight=1)

        # ── Launchers ──
        launchers = ttk.LabelFrame(self, text="启动器 / Launchers", padding=10)
        launchers.pack(fill="x", pady=(0, 10))
        _grid_buttons(launchers, [
            ("打开 Crypto OFIM App", lambda: self.app.run_launcher(CRYPTO_LAUNCHERS / "Open_Crypto_OFIM_App.command")),
            ("打开 Data Downloader", lambda: self.app.run_launcher(CRYPTO_LAUNCHERS / "Open_Crypto_Data_Downloader.command")),
        ])

        # ── CLI shortcuts via venv ──
        cli_box = ttk.LabelFrame(self, text="CLI 快捷 / CLI shortcuts", padding=10)
        cli_box.pack(fill="x", pady=(0, 10))
        _grid_buttons(cli_box, [
            ("OFIM Status", lambda: self._venv_cmd_in_terminal(["crypto-ofim-status"])),
            ("Perp Status", lambda: self._venv_cmd_in_terminal(["crypto-perp-status"])),
            ("OFIM Check", lambda: self._venv_cmd_in_terminal(["crypto-ofim-check"])),
            ("Perp Check", lambda: self._venv_cmd_in_terminal(["crypto-perp-check"])),
            ("OFIM Learning", lambda: self._venv_cmd_in_terminal(["crypto-learning-status"])),
            ("Perp Explain", lambda: self._venv_cmd_in_terminal(["crypto-perp-explain"])),
        ])

        # ── Logs ──
        logs = ttk.LabelFrame(self, text="日志 / Logs", padding=10)
        logs.pack(fill="x", pady=(0, 10))
        _grid_buttons(logs, [
            ("OFIM auto.log", lambda: self.app.open_path(RUNTIME_DIR / "crypto_ofim" / "auto.log")),
            ("Perp auto.log", lambda: self.app.open_path(RUNTIME_DIR / "crypto_perp" / "auto.log")),
            ("OFIM app.log", lambda: self.app.open_path(RUNTIME_DIR / "crypto_ofim" / "app.log")),
        ])

    def _venv_cmd_in_terminal(self, cli_args: list[str]) -> None:
        if not VENV_PYTHON.exists():
            self.app.toast(f"trade venv 缺失: {VENV_PYTHON}")
            return
        helper = TRADE_DIR / "runtime" / f"_unified_panel_crypto_{cli_args[0]}.command"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text(
            "#!/bin/bash\n"
            f"cd '{TRADE_DIR}'\n"
            f"'{VENV_PYTHON}' -m taa_futu.cli {' '.join(cli_args)}\n"
            "echo\n"
            "echo '按任意键关闭…'\n"
            "read -n 1\n",
            encoding="utf-8",
        )
        os.chmod(helper, 0o755)
        self.app.run_launcher(helper)

    def refresh(self) -> None:
        self.card_ofim.update(read_crypto_ofim_card())
        self.card_perp.update(read_crypto_perp_card())


# ─────────────────────────────────────────────────────────────────────────────
# Tab: 选股器
# ─────────────────────────────────────────────────────────────────────────────


class ScreenerTab(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "UnifiedPanel") -> None:
        super().__init__(master, padding=12)
        self.app = app

        intro = ttk.Label(
            self,
            text="选股器 / Screener — Futu desktop multi-factor stock screener",
            font=("Helvetica", 13, "bold"),
        )
        intro.pack(anchor="w", pady=(0, 8))

        info = ttk.Label(
            self,
            text=(
                "选股器是一个独立的 Tkinter 桌面 app，依赖 OpenD 拉富途板块/快照。\n"
                "运行依赖：OpenD 已登录、有美股/港股/A 股行情权限。"
            ),
            foreground="#444",
            justify="left",
        )
        info.pack(anchor="w", pady=(0, 12))

        launchers = ttk.LabelFrame(self, text="启动器 / Launchers", padding=10)
        launchers.pack(fill="x", pady=(0, 10))
        _grid_buttons(launchers, [
            ("打开 Stock Screener", lambda: self.app.run_launcher(STOCK_LAUNCHERS / "Open_Stock_Screener.command")),
            ("打开 AH 多因子扫描器", lambda: self.app.run_launcher(ALL_HERE / "news collector" / "scripts" / "AH_Multi_Factor_Scanner.command")),
            ("启用 动态 universe", lambda: self.app.run_launcher(ALL_HERE / "news collector" / "scripts" / "Enable_Dynamic_Universe.command")),
            ("关闭 动态 universe", lambda: self.app.run_launcher(ALL_HERE / "news collector" / "scripts" / "Disable_Dynamic_Universe.command")),
        ])

        # Files of interest produced by the screener
        outputs = ttk.LabelFrame(self, text="输出文件 / Outputs", padding=10)
        outputs.pack(fill="x", pady=(0, 10))
        news_live = ALL_HERE / "news collector" / "reports" / "live"
        _grid_buttons(outputs, [
            ("打开 reports/live 目录", lambda: self.app.open_path(news_live)),
            ("打开 dynamic universe 配置", lambda: self.app.open_path(ALL_HERE / "news collector" / "config" / "tech_universe_cn_hk.dynamic.json")),
        ], cols=2)

    def refresh(self) -> None:
        # No daemon to monitor — the screener is one-shot. Nothing to refresh.
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Tab: Live-Signal
# ─────────────────────────────────────────────────────────────────────────────


class LiveSignalTab(ttk.Frame):
    """Read-only multi-sleeve recommendation query.

    Wraps the ``taa-futu live-signal`` CLI we added today. Result is rendered
    as a sortable table so the user can compare the four sleeve scores at a
    glance.
    """

    DEFAULT_SYMBOLS = "US.NVDA, US.TSLA, US.QQQ, US.SPY"

    def __init__(self, master: tk.Misc, app: "UnifiedPanel") -> None:
        super().__init__(master, padding=12)
        self.app = app

        intro = ttk.Label(
            self,
            text="实时建议 / Live-Signal — 四 sleeve 综合读判（read-only，不下单）",
            font=("Helvetica", 13, "bold"),
        )
        intro.pack(anchor="w", pady=(0, 6))

        # Input row
        input_row = ttk.Frame(self)
        input_row.pack(fill="x", pady=(0, 8))
        ttk.Label(input_row, text="Symbols:").pack(side="left", padx=(0, 6))
        self.symbols_var = tk.StringVar(value=self.DEFAULT_SYMBOLS)
        self.entry = ttk.Entry(input_row, textvariable=self.symbols_var, width=60)
        self.entry.pack(side="left", padx=(0, 8))
        self.include_universe = tk.BooleanVar(value=False)
        ttk.Checkbutton(input_row, text="包括 universe view", variable=self.include_universe).pack(side="left")
        self.query_btn = ttk.Button(input_row, text="查询 / Query", command=self._kick_off_query)
        self.query_btn.pack(side="right")

        ttk.Label(
            self,
            text="多个 symbol 用逗号或空格分隔，例如 US.NVDA, US.TSLA。留空时回退到 FUSION_UNIVERSE。",
            foreground="#666",
            font=("Helvetica", 10),
        ).pack(anchor="w", pady=(0, 6))

        # Stack / generated_at line
        self.meta_label = ttk.Label(self, text="—", foreground="#444")
        self.meta_label.pack(anchor="w", pady=(0, 4))

        # Result table
        columns = ("symbol", "baseline", "fusion", "ofim", "cascade", "stack", "rec", "held")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=12)
        for col, label, w in [
            ("symbol", "symbol", 100),
            ("baseline", "baseline", 80),
            ("fusion", "fusion", 80),
            ("ofim", "ofim", 80),
            ("cascade", "cascade", 80),
            ("stack", "stack_weight", 100),
            ("rec", "recommendation", 120),
            ("held", "held", 60),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, pady=(4, 6))

        # Raw JSON pane (collapsible-ish via a small Text widget)
        raw_row = ttk.Frame(self)
        raw_row.pack(fill="x")
        ttk.Label(raw_row, text="原始 JSON / errors").pack(side="left")
        ttk.Button(raw_row, text="复制全部 JSON", command=self._copy_json).pack(side="right")
        self.json_text = tk.Text(self, height=8, wrap="word", font=("Menlo", 10))
        self.json_text.pack(fill="x", pady=(2, 0))
        self.json_text.insert("1.0", "（点击 查询 / Query 触发；首次约 5-15 秒）")
        self.json_text.config(state="disabled")
        self._last_json: str = ""

    def refresh(self) -> None:
        # The live-signal pane is on-demand; nothing to poll.
        pass

    def _kick_off_query(self) -> None:
        raw = self.symbols_var.get()
        symbols = [s.strip() for s in raw.replace(",", " ").split() if s.strip()]
        include = bool(self.include_universe.get())

        self.query_btn.config(state="disabled", text="查询中…")
        self.meta_label.config(text="查询中（5-15 秒）…")
        self.json_text.config(state="normal")
        self.json_text.delete("1.0", "end")
        self.json_text.insert("1.0", f"调用 taa-futu live-signal {' '.join(symbols) or '(default universe)'}\n")
        self.json_text.config(state="disabled")
        for item in self.tree.get_children():
            self.tree.delete(item)

        # Run in a background thread so the UI doesn't freeze during the
        # ~5-15s call (OpenD round-trip, Fusion/OFIM/Cascade scoring).
        def worker() -> None:
            result = run_live_signal_subprocess(symbols, include_universe=include)
            self.after(0, lambda: self._render(result))

        threading.Thread(target=worker, daemon=True).start()

    def _render(self, result: dict[str, Any]) -> None:
        self.query_btn.config(state="normal", text="查询 / Query")
        if not result.get("ok"):
            self.meta_label.config(text="查询失败")
            self.json_text.config(state="normal")
            self.json_text.delete("1.0", "end")
            self.json_text.insert("1.0", str(result.get("error", "unknown error")))
            self.json_text.config(state="disabled")
            return
        report = result["report"]
        self._last_json = json.dumps(report, ensure_ascii=False, indent=2)
        gen = report.get("generated_at", "?")
        stack = report.get("stack_label", "?")
        errors = report.get("errors", []) or []
        meta = f"生成: {gen}  ·  {stack}"
        if errors:
            meta += f"  ·  ⚠ degraded: {len(errors)} error(s)"
        self.meta_label.config(text=meta)

        by_symbol = report.get("by_symbol", {}) or {}
        # Show queried symbols first, then any extra symbols sleeves added
        queried = report.get("queried_symbols", []) or []
        ordered = list(queried) + [s for s in by_symbol.keys() if s not in queried]
        for sym in ordered:
            payload = by_symbol.get(sym, {}) or {}

            def _val(key: str) -> str:
                entry = payload.get(key)
                if not isinstance(entry, dict):
                    return "—"
                v = entry.get("scaled_weight") or entry.get("weight")
                if v is None:
                    return "—"
                return f"{float(v):.4f}"

            self.tree.insert("", "end", values=(
                sym,
                _val("baseline"),
                _val("fusion"),
                _val("ofim"),
                _val("cascade"),
                f"{float(payload.get('stack_target_weight') or 0.0):.4f}",
                payload.get("recommendation", "—"),
                "●" if payload.get("held") else "",
            ))

        self.json_text.config(state="normal")
        self.json_text.delete("1.0", "end")
        self.json_text.insert("1.0", self._last_json)
        self.json_text.config(state="disabled")

    def _copy_json(self) -> None:
        if not self._last_json:
            return
        self.clipboard_clear()
        self.clipboard_append(self._last_json)
        self.app.toast("已复制 JSON 到剪贴板")


# ─────────────────────────────────────────────────────────────────────────────
# Main app
# ─────────────────────────────────────────────────────────────────────────────


class UnifiedPanel(tk.Tk):
    """Top-level Tk root tying it all together.

    The window is intentionally not resizable below a useful size — the goal
    is that any 13" laptop opens it and sees all four tabs without scrolling.
    """

    def __init__(self) -> None:
        super().__init__()
        self.title("All Here 总控制台 · Unified Control")
        self.geometry("1100x780")
        self.minsize(960, 700)

        # ── Persistent status bar ──
        self.status_bar = StatusBar(self)
        self.status_bar.pack(fill="x")
        self.status_bar.bind_refresh(self._refresh_all)

        sep = ttk.Separator(self, orient="horizontal")
        sep.pack(fill="x", padx=8)

        # ── Tabs ──
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=6)

        self.tab_stock = StockTab(self.notebook, self)
        self.tab_full = FullControlPanelTab(self.notebook, self)
        self.tab_crypto = CryptoTab(self.notebook, self)
        self.tab_screener = ScreenerTab(self.notebook, self)
        self.tab_live = LiveSignalTab(self.notebook, self)

        self.notebook.add(self.tab_stock, text="📈 股票交易 (简洁)")
        self.notebook.add(self.tab_full, text="🎛️ 完整控制台")
        self.notebook.add(self.tab_crypto, text="💰 加密交易")
        self.notebook.add(self.tab_screener, text="🔍 选股器")
        self.notebook.add(self.tab_live, text="🤖 实时建议")

        # Lazy-load the embedded ControlPanel the first time the user clicks
        # the 完整控制台 tab. Building it eagerly at startup adds 5–10s and
        # makes the unified window unresponsive while OpenD probes run.
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Toast strip at the bottom for transient feedback
        self.toast_var = tk.StringVar(value="")
        toast = ttk.Label(self, textvariable=self.toast_var, foreground="#2c3e50", anchor="w")
        toast.pack(fill="x", padx=12, pady=(0, 6))

        # Kick off the periodic refresh
        self._refresh_all()
        self.after(REFRESH_MS, self._auto_refresh)

    # ── Wiring used by tabs ──

    def run_launcher(self, cmd_path: Path) -> None:
        ok, msg = open_command_file(cmd_path)
        self.toast(msg)
        # Schedule a refresh after the launcher's daemon has a moment to write
        # its status file.
        self.after(2000, self._refresh_all)
        self.after(6000, self._refresh_all)

    def open_path(self, path: Path) -> None:
        if not path.exists():
            self.toast(f"找不到 {path}")
            return
        try:
            subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.toast(f"已打开 {path.name}")
        except FileNotFoundError:
            self.toast(f"无法打开 (无 'open' 命令): {path}")

    def toast(self, msg: str) -> None:
        self.toast_var.set(msg)
        # Auto-clear after 5 s so the strip doesn't become a permanent prompt.
        self.after(5000, lambda: self.toast_var.set("") if self.toast_var.get() == msg else None)

    # ── Refresh plumbing ──

    def _refresh_all(self) -> None:
        try:
            self.status_bar.update_card("邮差", read_watcher_card())
            self.status_bar.update_card("OpenD", read_opend_card())
            self.tab_stock.refresh()
            self.tab_crypto.refresh()
            self.tab_screener.refresh()
            self.tab_live.refresh()
        except Exception as exc:
            # Defensive: a single buggy reader must not kill the UI loop.
            self.toast(f"刷新出错: {exc}")

    def _auto_refresh(self) -> None:
        self._refresh_all()
        self.after(REFRESH_MS, self._auto_refresh)

    def _on_tab_changed(self, _event: tk.Event) -> None:
        try:
            current = self.notebook.nametowidget(self.notebook.select())
        except (tk.TclError, KeyError):
            return
        if current is self.tab_full and not self.tab_full._loaded:
            # Defer one tick so the tab finishes rendering its placeholder
            # before we build the heavy ControlPanel UI on top.
            self.after(50, self.tab_full._ensure_loaded)


def main() -> None:
    app = UnifiedPanel()
    app.mainloop()


if __name__ == "__main__":
    main()
