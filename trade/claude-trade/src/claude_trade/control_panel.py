"""Claude-Trade 控制台 / Control Panel

Tkinter GUI for managing the Claude-Trade / Cascade strategy engine.
Adapted from the original taa_futu control panel by Codex.

Double-click  启动量化交易控制台.command  to launch.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import signal
import socket
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk


# ── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT   = Path(__file__).resolve().parents[2]   # claude-trade/
SRC_ROOT    = REPO_ROOT / "src"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
ENV_FILE    = REPO_ROOT / ".env"
RUNTIME_DIR = REPO_ROOT / "runtime"

STATUS_FILE   = RUNTIME_DIR / "status.json"
PID_FILE      = RUNTIME_DIR / "engine.pid"
HISTORY_FILE  = RUNTIME_DIR / "account_history.jsonl"

FUTU_OPEND_APP = Path("/Applications/FutuOpenD.app")
LAUNCH_AGENTS  = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH     = LAUNCH_AGENTS / "com.claude_trade.engine.plist"
CAFFEINATE_BIN = Path("/usr/bin/caffeinate")


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def build_env() -> dict[str, str]:
    env = os.environ.copy()
    parts = [str(SRC_ROOT)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _read_status() -> dict:
    if not STATUS_FILE.exists():
        return {}
    try:
        return json.loads(STATUS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _read_history(n: int = 30) -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    rows = []
    try:
        lines = HISTORY_FILE.read_text("utf-8").splitlines()
        for line in lines[-n:]:
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return rows


def _initial_capital() -> float:
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("INITIAL_CAPITAL="):
                return float(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return 0.0


def _fmt_ts(raw: str) -> str:
    if not raw:
        return ""
    try:
        ts = datetime.fromisoformat(raw).astimezone(ZoneInfo("America/New_York"))
        return ts.strftime("%Y-%m-%d %H:%M:%S ET")
    except Exception:
        return raw


def _engine_pid() -> int:
    if not PID_FILE.exists():
        return 0
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return 0


def _engine_running() -> bool:
    pid = _engine_pid()
    return pid > 0 and is_pid_running(pid)


def _read_env_value(key: str, default: str = "") -> str:
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return default


def _update_env_values(updates: dict[str, str]) -> None:
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", "utf-8")
    lines = ENV_FILE.read_text("utf-8").splitlines()
    remaining = dict(updates)
    new_lines: list[str] = []
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            new_lines.append(line)
            continue
        key = line.split("=", 1)[0]
        if key in remaining:
            new_lines.append(f"{key}={remaining.pop(key)}")
        else:
            new_lines.append(line)
    for k, v in remaining.items():
        new_lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(new_lines).rstrip() + "\n", "utf-8")
    for k, v in updates.items():
        os.environ[k] = v


def _engine_start_args(*, dry_run: bool = True) -> list[str]:
    base = [str(VENV_PYTHON), "-m", "claude_trade.cli", "run"]
    if dry_run:
        base.append("--dry-run")
    if CAFFEINATE_BIN.exists():
        return [str(CAFFEINATE_BIN), "-i", "-m", "-s", *base]
    return base


# ══════════════════════════════════════════════════════════════════════════════
# ControlPanel
# ══════════════════════════════════════════════════════════════════════════════

class ControlPanel:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Claude-Trade  量化控制台 / Control Panel")
        self.root.geometry("1380x900")
        self.root.minsize(1100, 720)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self._wrapping_labels: list[ttk.Label] = []
        self._scroll_canvases: list[tk.Canvas] = []
        self._refresh_after_id: str | None = None
        self.main_pane: tk.PanedWindow | None = None
        self.dashboard_process: subprocess.Popen | None = None

        # ── Config vars
        self.opend_host    = tk.StringVar(value=_read_env_value("FUTU_HOST", "127.0.0.1"))
        self.opend_port    = tk.StringVar(value=_read_env_value("FUTU_PORT", "11111"))
        self.dashboard_port = tk.StringVar(value=_read_env_value("DASHBOARD_PORT", "8051"))
        self.start_date    = tk.StringVar(value="2020-01-01")
        self.end_date      = tk.StringVar(value=time.strftime("%Y-%m-%d"))

        # ── Status vars
        self.engine_status_var   = tk.StringVar(value="引擎 / Engine: 检查中…")
        self.connect_status_var  = tk.StringVar(value="连接 / Connectivity: 检查中…")
        self.portfolio_status_var = tk.StringVar(value="账户 / Portfolio: —")
        self.pnl_status_var      = tk.StringVar(value="盈亏 / P&L: —")
        self.regime_status_var   = tk.StringVar(value="市场制度 / Regime: —")
        self.alloc_status_var    = tk.StringVar(value="资金分配 / Allocation: —")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(300, self._drain_log_queue)
        self.root.after(600, self.refresh_status)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.main_pane = tk.PanedWindow(
            self.root, orient=tk.VERTICAL,
            sashwidth=10, sashrelief=tk.RAISED,
            showhandle=True, opaqueresize=True, bd=0,
        )
        self.main_pane.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        # ── Content area (top portion of main pane)
        content = ttk.Frame(self.main_pane, padding=4)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        vertical_pane = tk.PanedWindow(
            content, orient=tk.VERTICAL,
            sashwidth=10, sashrelief=tk.RAISED,
            showhandle=True, opaqueresize=True, bd=0,
        )
        vertical_pane.grid(row=0, column=0, sticky="nsew")
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-4>",   self._on_mousewheel, add="+")
        self.root.bind_all("<Button-5>",   self._on_mousewheel, add="+")

        # ── Top row: Status | Service | Config ────────────────────────────
        top_pane = tk.PanedWindow(
            vertical_pane, orient=tk.HORIZONTAL,
            sashwidth=10, sashrelief=tk.RAISED,
            showhandle=True, opaqueresize=True, bd=0,
        )
        vertical_pane.add(top_pane, minsize=280, stretch="always")

        # Status panel
        status_frame, status_inner = self._scrollable(top_pane, "状态 / Status")
        top_pane.add(status_frame, minsize=380, stretch="always")
        for idx, var in enumerate([
            self.engine_status_var, self.connect_status_var,
            self.portfolio_status_var, self.pnl_status_var,
            self.regime_status_var, self.alloc_status_var,
        ]):
            lbl = ttk.Label(status_inner, textvariable=var, justify="left", anchor="w")
            lbl.grid(row=idx, column=0, sticky="ew", pady=(0 if idx == 0 else 8, 0))
            self._wrapping_labels.append(lbl)

        # Service buttons
        svc_frame, svc_inner = self._scrollable(top_pane, "一键服务 / Quick Actions")
        top_pane.add(svc_frame, minsize=230, stretch="always")
        for idx, (label, cmd) in enumerate([
            ("一键启动 / One-Click Start",       self.one_click_start),
            ("启动引擎 (模拟盘) / Start (Paper)", self.start_engine_paper),
            ("启动引擎 (实盘)  / Start (LIVE)",  self.start_engine_live),
            ("停止引擎 / Stop Engine",            self.stop_engine),
            ("打开监控页 / Open Dashboard",       self.start_dashboard),
            ("刷新状态 / Refresh Status",         self.refresh_status),
        ]):
            ttk.Button(svc_inner, text=label, command=cmd).grid(
                row=idx, column=0, sticky="ew", pady=(0 if idx == 0 else 8, 0)
            )

        # Config panel
        cfg_frame, cfg_inner = self._scrollable(top_pane, "连接设置 / Connection & Config")
        cfg_inner.columnconfigure(1, weight=1)
        top_pane.add(cfg_frame, minsize=280, stretch="always")

        rows_cfg = [
            ("OpenD 地址 / Host",       self.opend_host),
            ("OpenD 端口 / Port",        self.opend_port),
            ("监控页端口 / Dashboard Port", self.dashboard_port),
        ]
        for i, (text, var) in enumerate(rows_cfg):
            ttk.Label(cfg_inner, text=text).grid(row=i, column=0, sticky="w", pady=(0 if i == 0 else 8, 0))
            ttk.Entry(cfg_inner, textvariable=var).grid(row=i, column=1, sticky="ew", padx=(10, 0), pady=(0 if i == 0 else 8, 0))

        ttk.Label(cfg_inner, text="交易环境 / Trade Mode").grid(row=3, column=0, sticky="w", pady=(14, 0))
        for i, (label, cmd) in enumerate([
            ("切到模拟盘 / Use SIMULATE",          self.arm_simulate),
            ("切到实盘手动 / Arm REAL Manual",     self.arm_real_manual),
            ("切到实盘自动 / Arm REAL Auto",       self.arm_real_auto),
        ]):
            ttk.Button(cfg_inner, text=label, command=cmd).grid(
                row=4+i, column=0, columnspan=2, sticky="ew", pady=(8, 0)
            )

        ttk.Label(cfg_inner, text="实用工具 / Utilities").grid(row=7, column=0, sticky="w", pady=(14, 0))
        for i, (label, cmd) in enumerate([
            ("单开 FutuOpenD",                     self.open_futu_opend),
            ("实盘就绪检查 / Check REAL Readiness", self.check_real_readiness),
            ("设置交易密码MD5 / Set Trade Pwd MD5", self.set_trade_password_md5),
            ("编辑配置文件 / Edit .env",           self.edit_env),
            ("安装开机自启 / Install Auto-Start",  self.install_login_auto_start),
            ("关闭开机自启 / Remove Auto-Start",   self.uninstall_login_auto_start),
        ]):
            ttk.Button(cfg_inner, text=label, command=cmd).grid(
                row=8+i, column=0, columnspan=2, sticky="ew", pady=(8, 0)
            )

        # ── Bottom row: Backtest ──────────────────────────────────────────
        mid_pane = tk.PanedWindow(
            vertical_pane, orient=tk.HORIZONTAL,
            sashwidth=10, sashrelief=tk.RAISED,
            showhandle=True, opaqueresize=True, bd=0,
        )
        vertical_pane.add(mid_pane, minsize=180, stretch="always")

        bt_frame, bt_inner = self._scrollable(mid_pane, "历史回测 / Backtest")
        bt_inner.columnconfigure(1, weight=1)
        mid_pane.add(bt_frame, minsize=320, stretch="always")
        ttk.Label(bt_inner, text="开始日期 / Start").grid(row=0, column=0, sticky="w")
        ttk.Entry(bt_inner, textvariable=self.start_date).grid(row=0, column=1, sticky="ew", padx=(10, 0))
        ttk.Label(bt_inner, text="结束日期 / End").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(bt_inner, textvariable=self.end_date).grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(8, 0))
        ttk.Button(bt_inner, text="运行回测 / Run Backtest", command=self.run_backtest).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0)
        )

        info_frame, info_inner = self._scrollable(mid_pane, "策略说明 / Strategy Info")
        mid_pane.add(info_frame, minsize=380, stretch="always")
        self._strategy_info_label = ttk.Label(
            info_inner, text="加载中…", justify="left", anchor="nw", wraplength=360,
        )
        self._strategy_info_label.grid(row=0, column=0, sticky="nsew")
        self._wrapping_labels.append(self._strategy_info_label)

        # ── Log panel (bottom of main pane)
        log_frame = ttk.LabelFrame(self.main_pane, text="输出日志 / Output Log", padding=12)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        self.main_pane.add(content,    minsize=420, stretch="always")
        self.main_pane.add(log_frame,  minsize=160, stretch="always")

        toolbar = ttk.Frame(log_frame)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(0, weight=1)
        ttk.Button(toolbar, text="放大 / Expand",  command=self._expand_log).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(toolbar, text="缩小 / Shrink",  command=self._shrink_log).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(toolbar, text="清空 / Clear",   command=self._clear_log).grid(row=0, column=3)

        self.log_text = tk.Text(log_frame, wrap="word", height=18, font=("Menlo", 12))
        self.log_text.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.insert("end", "已就绪 / Ready.\n点 [一键启动] 或选择操作开始。\n")
        self.log_text.configure(state="disabled")

        for inner in [status_inner]:
            inner.bind("<Configure>", self._update_wraplengths)

    # ── Scrollable section helper ─────────────────────────────────────────────

    def _scrollable(self, parent, title: str) -> tuple[ttk.LabelFrame, ttk.Frame]:
        frame = ttk.LabelFrame(parent, text=title, padding=12)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        canvas = tk.Canvas(frame, highlightthickness=0, bd=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        sb.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=sb.set)
        inner = ttk.Frame(canvas)
        inner.columnconfigure(0, weight=1)
        wid = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(wid, width=e.width))
        self._scroll_canvases.append(canvas)
        return frame, inner

    # ── Mouse wheel ───────────────────────────────────────────────────────────

    @staticmethod
    def _is_descendant(widget, ancestor) -> bool:
        cur = widget
        while cur is not None:
            if cur == ancestor:
                return True
            pname = cur.winfo_parent()
            if not pname:
                return False
            try:
                cur = cur.nametowidget(pname)
            except KeyError:
                return False
        return False

    def _on_mousewheel(self, event) -> str | None:
        target = self.root.winfo_containing(event.x_root, event.y_root)
        canvas = next((c for c in self._scroll_canvases if self._is_descendant(target, c)), None)
        if canvas is None:
            return None
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            dv = getattr(event, "delta", 0)
            delta = -1 if dv > 0 else 1
        canvas.yview_scroll(delta, "units")
        return "break"

    def _update_wraplengths(self, _event=None) -> None:
        for lbl in self._wrapping_labels:
            parent = lbl.nametowidget(lbl.winfo_parent())
            lbl.configure(wraplength=max(parent.winfo_width() - 28, 240))

    # ── Log helpers ───────────────────────────────────────────────────────────

    def log(self, msg: str) -> None:
        self.log_queue.put(msg.rstrip() + "\n")

    def _drain_log_queue(self) -> None:
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(300, self._drain_log_queue)

    def _expand_log(self) -> None:
        if self.main_pane:
            cur = self.main_pane.sash_coord(0)[1]
            self.main_pane.sash_place(0, 1, max(260, cur - 120))

    def _shrink_log(self) -> None:
        if self.main_pane:
            cur   = self.main_pane.sash_coord(0)[1]
            total = max(self.main_pane.winfo_height(), 600)
            self.main_pane.sash_place(0, 1, min(total - 140, cur + 120))

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "日志已清空 / Log cleared.\n")
        self.log_text.configure(state="disabled")

    # ── Async command runner ──────────────────────────────────────────────────

    def _run_async(self, title: str, command: list[str]) -> None:
        def worker():
            self.log(f"$ {' '.join(command)}")
            proc = subprocess.Popen(
                command, cwd=REPO_ROOT, env=build_env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            assert proc.stdout
            for line in proc.stdout:
                self.log(line.rstrip())
            rc = proc.wait()
            self.log(f"[{title}] {'完成' if rc == 0 else f'退出码 {rc}'}.")
            self.root.after(0, self.refresh_status)
        threading.Thread(target=worker, daemon=True).start()

    def _cli(self, *args: str) -> list[str]:
        return [str(VENV_PYTHON), "-m", "claude_trade.cli", *args]

    # ── Status refresh ────────────────────────────────────────────────────────

    def refresh_status(self) -> None:
        host = self.opend_host.get().strip() or "127.0.0.1"
        port = int(self.opend_port.get().strip() or "11111")
        dash_port = int(self.dashboard_port.get().strip() or "8051")

        running   = _engine_running()
        opend_ok  = is_port_open(host, port)
        dash_ok   = (self.dashboard_process is not None and self.dashboard_process.poll() is None) \
                    or is_port_open("127.0.0.1", dash_port)
        st        = _read_status()

        # ── Engine / connectivity line
        eng = "● 运行中 / running" if running else "○ 已停止 / stopped"
        opend_s = f"富途 {'✓ 已连接' if opend_ok else '✗ 未连接'}"
        dash_s  = f"监控页 {'✓ 运行中' if dash_ok else '○ 已停止'}"
        self.engine_status_var.set(
            f"引擎 / Engine: {eng}\n"
            f"模式: {'模拟盘 DRY-RUN' if st.get('mode') == 'dry_run' else '实盘 LIVE' if st.get('mode') == 'live' else '—'}  "
            f"| 策略: {', '.join(st.get('active_strategies', ['cascade']))}\n"
            f"更新: {_fmt_ts(st.get('updated_at', ''))}\n"
            f"周期: {st.get('cycle_count', '—')}  错误: {st.get('error_count', 0)}"
        )

        futu_on   = st.get("futu_online")
        crypto_on = st.get("crypto_online")
        mkt_open  = st.get("market_hours_open")
        f_s = ("✓ 富途在线" if futu_on else "✗ 富途离线") if futu_on is not None else opend_s
        c_s = ("✓ 加密在线" if crypto_on else "✗ 加密离线") if crypto_on is not None else "加密 ?"
        m_s = ("美股: 开市" if mkt_open else "美股: 休市") if mkt_open is not None else ""
        self.connect_status_var.set(f"连接 / Connectivity:\n{f_s}  |  {c_s}  |  {m_s}\n{dash_s}")

        # ── Portfolio / P&L
        acct    = float(st.get("account_value", 0))
        init    = _initial_capital()
        history = _read_history(30)

        portfolio_lines = [f"账户净值 / Account: ${acct:,.2f}"]
        if st.get("last_trade_at"):
            portfolio_lines.append(f"最近成交: {_fmt_ts(st['last_trade_at'])}")
        else:
            portfolio_lines.append("最近成交: 从未 / never")
        self.portfolio_status_var.set("账户 / Portfolio:\n" + "\n".join(portfolio_lines))

        pnl_lines: list[str] = []
        if init > 0:
            pnl = acct - init
            pct = pnl / init * 100
            sign = "+" if pnl >= 0 else ""
            pnl_lines.append(f"初始资金: ${init:,.2f}")
            pnl_lines.append(f"总盈亏:   {sign}${pnl:,.2f}  ({sign}{pct:.2f}%)")
        if len(history) >= 2:
            prev = history[-2].get("account_value", acct)
            chg  = acct - prev
            chg_pct = chg / prev * 100 if prev else 0
            sign = "+" if chg >= 0 else ""
            pnl_lines.append(f"本次变动: {sign}${chg:,.2f}  ({sign}{chg_pct:.2f}%)")
        if len(history) >= 3:
            vals   = [h.get("account_value", 0) for h in history]
            blocks = " ▁▂▃▄▅▆▇█"
            lo, hi = min(vals), max(vals)
            span   = hi - lo or 1.0
            spark  = "".join(blocks[int((v - lo) / span * 8)] for v in vals[-20:])
            pnl_lines.append(f"走势:     {spark}")
        self.pnl_status_var.set("盈亏 / P&L:\n" + ("\n".join(pnl_lines) if pnl_lines else "—  (等待首次运行)"))

        # ── Regime
        regime = st.get("regime", "—")
        score  = st.get("regime_score") or 0.0
        rd     = st.get("regime_details", {})
        det    = rd.get("details", {})
        regime_lines = [f"制度: {regime}  (得分 {score:+.3f})"]
        if rd:
            cp  = rd.get("crypto_pulse", 0.0)
            vol = rd.get("vol_regime", "—")
            ca  = rd.get("cross_asset_flow", 0.0)
            fs  = rd.get("funding_signal", 0.0)
            regime_lines.append(f"加密脉冲: {cp:+.3f}  跨资产: {ca:+.3f}")
            regime_lines.append(f"波动制度: {vol}       资金信号: {fs:+.3f}")
        vix = det.get("vix_level")
        if vix is not None:
            regime_lines.append(f"VIX: {vix:.1f}")
        fr = det.get("funding_rate")
        if fr is not None:
            regime_lines.append(f"资金费率: {fr:.4%}")
        bw = det.get("btc_weekend_return")
        if bw is not None:
            regime_lines.append(f"BTC 周末: {bw:+.2%}")
        self.regime_status_var.set("市场制度 / Regime:\n" + "\n".join(regime_lines))

        # ── Allocation
        budgets = st.get("asset_class_budgets", {})
        weights = st.get("target_weights", {})
        exp     = float(st.get("total_exposure", 0)) * 100
        alloc_lines: list[str] = [f"总仓位: {exp:.1f}%"]
        if budgets:
            for cls, label in [("equity","股票"),("crypto","加密"),("bond","债券")]:
                v = budgets.get(cls, 0.0)
                if v > 0.001:
                    bar = "█" * int(v * 20)
                    alloc_lines.append(f"{label}: {v*100:.1f}%  {bar}")
        if weights:
            alloc_lines.append("目标仓位:")
            for sym, w in sorted(weights.items(), key=lambda x: -x[1]):
                value = acct * w
                alloc_lines.append(f"  {sym:<16} {w*100:.1f}%  ${value:,.0f}")
        elif running:
            alloc_lines.append("(等待策略信号…)")
        self.alloc_status_var.set("资金分配 / Allocation:\n" + "\n".join(alloc_lines))

        # ── Strategy info panel
        trd_env = _read_env_value("FUTU_TRD_ENV", "SIMULATE")
        enable_real = _read_env_value("FUTU_ENABLE_REAL_TRADING", "false").lower() == "true"
        allow_auto  = _read_env_value("FUTU_ALLOW_AUTO_REAL",    "false").lower() == "true"
        pwd_set     = bool(_read_env_value("FUTU_UNLOCK_TRADE_PASSWORD_MD5"))
        active_strats = st.get("active_strategies") or [_read_env_value("ACTIVE_STRATEGIES", "cascade")]

        info = (
            f"交易环境: {trd_env}\n"
            f"实盘下单: {'开' if enable_real else '关'}  "
            f"实盘自动: {'开' if allow_auto else '关'}  "
            f"交易密码MD5: {'已配置' if pwd_set else '未配置'}\n\n"
            f"当前策略: {', '.join(active_strats)}\n\n"
            f"Cascade 策略说明:\n"
            f"  多因子制度感知配置策略，综合 BTC/ETH 加密信号、\n"
            f"  VIX 恐慌指数、跨资产资金流向判断市场制度，\n"
            f"  再按制度分配股票/加密/债券预算，动态配置目标仓位。\n\n"
            f"Universe: {_read_env_value('DM_UNIVERSE', '—')}"
        )
        self._strategy_info_label.configure(text=info)
        self._update_wraplengths()
        self._schedule_refresh()

    def _schedule_refresh(self, delay_ms: int = 5000) -> None:
        if self._refresh_after_id is not None:
            try:
                self.root.after_cancel(self._refresh_after_id)
            except ValueError:
                pass
        self._refresh_after_id = self.root.after(delay_ms, self.refresh_status)

    # ── Engine start / stop ───────────────────────────────────────────────────

    def start_engine_paper(self) -> None:
        if _engine_running():
            self.log("引擎已在运行 / Engine already running.")
            return
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        cmd = _engine_start_args(dry_run=True)
        proc = subprocess.Popen(
            cmd, cwd=REPO_ROOT, env=build_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
        self.log(f"已启动引擎 (模拟盘) pid={proc.pid}")

        def _reader():
            assert proc.stdout
            for line in proc.stdout:
                self.log(f"[engine] {line.rstrip()}")
            self.log(f"[engine] 已停止 / stopped (code {proc.wait()}).")
            self.root.after(0, self.refresh_status)
        threading.Thread(target=_reader, daemon=True).start()
        self.root.after(1000, self.refresh_status)

    def start_engine_live(self) -> None:
        if not self._confirm_live_trade("启动实盘引擎 / Start LIVE Engine"):
            return
        if _engine_running():
            self.log("引擎已在运行 / Engine already running.")
            return
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        cmd = _engine_start_args(dry_run=False)
        proc = subprocess.Popen(
            cmd, cwd=REPO_ROOT, env=build_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
        self.log(f"已启动引擎 (实盘) pid={proc.pid}")

        def _reader():
            assert proc.stdout
            for line in proc.stdout:
                self.log(f"[engine-live] {line.rstrip()}")
            self.log(f"[engine-live] 已停止 (code {proc.wait()}).")
            self.root.after(0, self.refresh_status)
        threading.Thread(target=_reader, daemon=True).start()
        self.root.after(1000, self.refresh_status)

    def stop_engine(self) -> None:
        pid = _engine_pid()
        if pid and is_pid_running(pid):
            os.kill(pid, signal.SIGTERM)
            self.log(f"已发送停止信号 / Sent SIGTERM to engine pid={pid}.")
        else:
            self.log("引擎未在运行 / Engine is not running.")
        self.root.after(1500, self.refresh_status)

    def one_click_start(self) -> None:
        self.open_futu_opend()
        self.start_engine_paper()
        self.start_dashboard()
        self.log("一键启动已触发 / One-click start triggered.")

    # ── Dashboard ─────────────────────────────────────────────────────────────

    def start_dashboard(self) -> None:
        dash_port = int(self.dashboard_port.get().strip() or "8051")
        if self.dashboard_process is not None and self.dashboard_process.poll() is None:
            self.open_dashboard_browser()
            return
        cmd = self._cli("dashboard", "--port", str(dash_port))
        self.dashboard_process = subprocess.Popen(
            cmd, cwd=REPO_ROOT, env=build_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.log(f"正在启动监控页 / Starting dashboard on port {dash_port}…")

        def _reader():
            assert self.dashboard_process and self.dashboard_process.stdout
            for line in self.dashboard_process.stdout:
                self.log(f"[dashboard] {line.rstrip()}")
            self.log(f"[dashboard] 已停止 (code {self.dashboard_process.wait()}).")
            self.root.after(0, self.refresh_status)
        threading.Thread(target=_reader, daemon=True).start()
        self.root.after(2500, self.open_dashboard_browser)
        self.root.after(800, self.refresh_status)

    def open_dashboard_browser(self) -> None:
        dash_port = int(self.dashboard_port.get().strip() or "8051")
        webbrowser.open(f"http://localhost:{dash_port}")
        self.log(f"已打开浏览器 / Opened http://localhost:{dash_port}")

    # ── Trade mode ────────────────────────────────────────────────────────────

    def arm_simulate(self) -> None:
        _update_env_values({
            "FUTU_TRD_ENV": "SIMULATE",
            "FUTU_ENABLE_REAL_TRADING": "false",
            "FUTU_ALLOW_AUTO_REAL": "false",
        })
        self.log("已切到模拟盘 / Switched to SIMULATE mode.")
        self.refresh_status()

    def arm_real_manual(self) -> None:
        if not messagebox.askyesno(
            "切到实盘手动 / Arm REAL Manual",
            "环境将切到 REAL，允许手动真实下单，但自动交易仍锁定。\n继续？\n\n"
            "Switches to REAL, enables manual live orders but keeps auto trading locked. Continue?",
        ):
            return
        _update_env_values({
            "FUTU_TRD_ENV": "REAL",
            "FUTU_ENABLE_REAL_TRADING": "true",
            "FUTU_ALLOW_AUTO_REAL": "false",
        })
        self.log("已切到实盘手动模式 / Armed REAL manual mode.")
        self.refresh_status()

    def arm_real_auto(self) -> None:
        if not messagebox.askyesno(
            "切到实盘自动 / Arm REAL Auto",
            "环境将切到 REAL 并启用自动真实交易。风险最高。\n继续？\n\n"
            "Switches to REAL and enables live auto trading. Highest risk mode. Continue?",
        ):
            return
        typed = simpledialog.askstring(
            "最终确认 / Final Confirmation",
            "输入 AUTO REAL 以确认 / Type AUTO REAL to confirm:", parent=self.root,
        )
        if typed != "AUTO REAL":
            self.log("已取消 / Cancelled.")
            return
        _update_env_values({
            "FUTU_TRD_ENV": "REAL",
            "FUTU_ENABLE_REAL_TRADING": "true",
            "FUTU_ALLOW_AUTO_REAL": "true",
        })
        self.log("已切到实盘自动模式 / Armed REAL auto mode.")
        self.refresh_status()

    def _confirm_live_trade(self, title: str) -> bool:
        trd_env = _read_env_value("FUTU_TRD_ENV", "SIMULATE")
        if trd_env != "REAL":
            return True
        if not messagebox.askyesno(title, "当前是实盘环境，将下真实订单。继续？\n\nLIVE mode — real orders will be submitted. Continue?"):
            return False
        typed = simpledialog.askstring("确认 / Confirm", "输入 REAL 确认 / Type REAL to confirm:", parent=self.root)
        return typed == "REAL"

    # ── Utilities ─────────────────────────────────────────────────────────────

    def open_futu_opend(self) -> None:
        if not FUTU_OPEND_APP.exists():
            messagebox.showerror("FutuOpenD", f"找不到应用 / Missing: {FUTU_OPEND_APP}")
            return
        subprocess.Popen(["open", "-a", str(FUTU_OPEND_APP)])
        self.log("已打开 FutuOpenD.")
        self.root.after(1200, self.refresh_status)

    def check_real_readiness(self) -> None:
        trd_env    = _read_env_value("FUTU_TRD_ENV", "SIMULATE")
        enable_real = _read_env_value("FUTU_ENABLE_REAL_TRADING", "false")
        allow_auto  = _read_env_value("FUTU_ALLOW_AUTO_REAL", "false")
        pwd_md5     = _read_env_value("FUTU_UNLOCK_TRADE_PASSWORD_MD5", "")
        lines = [
            f"FUTU_TRD_ENV:               {trd_env}",
            f"FUTU_ENABLE_REAL_TRADING:   {enable_real}",
            f"FUTU_ALLOW_AUTO_REAL:       {allow_auto}",
            f"交易密码MD5 已配置:          {'是' if pwd_md5 else '否'}",
        ]
        issues = []
        if trd_env != "REAL":
            issues.append("环境是 SIMULATE，不是 REAL")
        if enable_real.lower() != "true":
            issues.append("FUTU_ENABLE_REAL_TRADING 未开启")
        if not pwd_md5:
            issues.append("FUTU_UNLOCK_TRADE_PASSWORD_MD5 未配置")
        result = "\n".join(lines)
        if issues:
            result += "\n\n⚠ 问题:\n" + "\n".join(f"  • {i}" for i in issues)
        else:
            result += "\n\n✓ 实盘就绪 / REAL trading ready."
        messagebox.showinfo("实盘就绪检查 / REAL Readiness", result)
        self.log("实盘检查完成 / Real readiness check done.")

    def set_trade_password_md5(self) -> None:
        pwd = simpledialog.askstring(
            "设置交易密码MD5 / Set Trade Password MD5",
            "输入富途交易密码 (只保存MD5，不保存明文):\n\nEnter Futu trade password (only MD5 stored):",
            parent=self.root, show="*",
        )
        if not pwd:
            return
        md5 = hashlib.md5(pwd.encode("utf-8")).hexdigest()
        _update_env_values({"FUTU_UNLOCK_TRADE_PASSWORD_MD5": md5})
        self.log("交易密码MD5 已更新 / Trade password MD5 updated.")
        self.refresh_status()

    def edit_env(self) -> None:
        subprocess.Popen(["open", "-e", str(ENV_FILE)])
        self.log("已用 TextEdit 打开 .env (保存后重启引擎生效).")

    def install_login_auto_start(self) -> None:
        LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        args = _engine_start_args(dry_run=True)
        prog_args = "".join(f"    <string>{a}</string>\n" for a in args)
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.claude_trade.engine</string>
  <key>ProgramArguments</key>
  <array>
{prog_args.rstrip()}
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>{SRC_ROOT}</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>{REPO_ROOT}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{RUNTIME_DIR}/engine.log</string>
  <key>StandardErrorPath</key>
  <string>{RUNTIME_DIR}/engine.log</string>
</dict>
</plist>
"""
        PLIST_PATH.write_text(plist, "utf-8")
        subprocess.run(["launchctl", "unload", str(PLIST_PATH)], check=False)
        subprocess.run(["launchctl", "load",   str(PLIST_PATH)], check=True)
        self.log("开机自启已安装 / Login auto-start installed.")
        self.root.after(1000, self.refresh_status)

    def uninstall_login_auto_start(self) -> None:
        subprocess.run(["launchctl", "unload", str(PLIST_PATH)], check=False)
        if PLIST_PATH.exists():
            PLIST_PATH.unlink()
        self.log("开机自启已移除 / Login auto-start removed.")
        self.root.after(1000, self.refresh_status)

    # ── Backtest ──────────────────────────────────────────────────────────────

    def run_backtest(self) -> None:
        start = self.start_date.get().strip()
        self.log(f"运行回测 / Running backtest from {start}…")
        self._run_async("backtest", self._cli("backtest", "--start", start))

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        if self._refresh_after_id:
            try:
                self.root.after_cancel(self._refresh_after_id)
            except ValueError:
                pass
        if self.dashboard_process and self.dashboard_process.poll() is None:
            if messagebox.askyesno("退出 / Exit", "监控页仍在运行，先停止再退出？\nStop dashboard before exit?"):
                self.dashboard_process.terminate()
                self.root.after(300, self.root.destroy)
                return
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if not VENV_PYTHON.exists():
        raise SystemExit(f"缺少 Python 环境 / Missing venv: {VENV_PYTHON}")
    ControlPanel().run()


if __name__ == "__main__":
    main()
