from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import os
import hashlib
import queue
import socket
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser
import signal
from zoneinfo import ZoneInfo
from tkinter import simpledialog

from .cascade_sleeve import cascade_summary_line
from .config import load_settings
from .strategy_stack import stack_allocations, stack_label


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
DASHBOARD_APP = SRC_ROOT / "taa_futu" / "dashboard_app.py"
FUTU_OPEND_APP = Path("/Applications/FutuOpenD.app")
ENV_FILE = REPO_ROOT / ".env"
ENV_EXAMPLE_FILE = REPO_ROOT / ".env.example"
RUNTIME_DIR = REPO_ROOT / "runtime"
AUTO_TRADER_STATUS_FILE = RUNTIME_DIR / "auto_trader_status.json"
AUTO_TRADER_PID_FILE = RUNTIME_DIR / "auto_trader.pid"
AUTO_TRADER_LOG_FILE = RUNTIME_DIR / "auto_trader.log"
WATCHDOG_STATUS_FILE = RUNTIME_DIR / "watchdog_status.json"
WATCHDOG_PID_FILE = RUNTIME_DIR / "watchdog.pid"
WATCHDOG_LOG_FILE = RUNTIME_DIR / "watchdog.log"
WATCHDOG_LAUNCH_AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.jiao.taa_futu_watchdog.plist"
LEGACY_AUTO_TRADER_LAUNCH_AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.jiao.taa_futu_auto_trader.plist"
CAFFEINATE_BIN = Path("/usr/bin/caffeinate")

# ── Claude-Trade engine (separate repo / separate venv) ───────────────────────
CT_REPO_ROOT    = REPO_ROOT.parent / "claude-trade"
CT_VENV_PYTHON  = CT_REPO_ROOT / ".venv" / "bin" / "python"
CT_PID_FILE     = CT_REPO_ROOT / "runtime" / "engine.pid"
CT_STATUS_FILE  = CT_REPO_ROOT / "runtime" / "status.json"
CT_LOG_FILE     = CT_REPO_ROOT / "runtime" / "engine.log"
CT_RUNTIME_DIR  = CT_REPO_ROOT / "runtime"
CT_SRC_ROOT     = CT_REPO_ROOT / "src"
CT_DASHBOARD_PORT = 8051


def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_env() -> dict[str, str]:
    env = os.environ.copy()
    pythonpath_parts = [str(SRC_ROOT)]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _format_status_timestamp(raw: str) -> str:
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _is_transient_status_message(message: object) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    markers = (
        "packeterr.timeout",
        "timeout",
        "connection closed",
        "查询未完成订单请求超时",
        "请求超时",
        "transient_error",
    )
    return any(marker in text for marker in markers)


def _friendly_runtime_status(action: str, detail: object) -> tuple[str, str, str]:
    raw_detail = str(detail or "").strip()
    if action == "transient_error" or _is_transient_status_message(raw_detail):
        return (
            "接口波动 / transient",
            "富途接口瞬时超时 / api timeout",
            f"不是你电脑断网。是 OpenD/富途接口短暂断开，系统会自动重试。原始信息: {raw_detail or 'N/A'}",
        )
    if action == "error":
        return ("异常 / error", "异常 / error", raw_detail)
    return ("正常 / healthy", action, raw_detail)


def _parse_status_timestamp(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed


def _watchdog_program_arguments() -> list[str]:
    base = [str(VENV_PYTHON), "-m", "taa_futu.watchdog"]
    if CAFFEINATE_BIN.exists():
        return [str(CAFFEINATE_BIN), "-i", "-m", "-s", *base]
    return base


def _study_mode_label(mode: str) -> str:
    mapping = {
        "baseline": "基线月频回测",
        "fusion": "Fusion 日内回放",
        "cascade": "Claude/Cascade 回放",
        "stack": "组合整体回测",
        "account": "账户真实复盘",
        "exact": "精确执行复盘",
    }
    return mapping.get(mode, mode)


def _manual_strategy_label(mode: str) -> str:
    mapping = {
        "baseline": "Baseline 月频手动下单",
        "fusion": "Fusion 日内手动下单",
        "cascade": "Claude/Cascade 手动下单",
    }
    return mapping.get(mode, mode)


def _short_symbols(symbols: tuple[str, ...], *, limit: int | None = None) -> str:
    visible = tuple(code.replace("US.", "") for code in symbols)
    if limit is not None and len(visible) > limit:
        head = " / ".join(visible[:limit])
        return f"{head} / ... 共{len(visible)}只"
    return " / ".join(visible)


def _baseline_summary(settings) -> str:
    symbols = _short_symbols(settings.symbols)
    return (
        "基线策略: 月频 5ETF 趋势。"
        f"基准 {settings.benchmark.replace('US.', '')}，"
        f"{settings.lookback_months} 个月均线，"
        f"标的 {symbols}。"
    )


def _fusion_summary(settings) -> str:
    universe = _short_symbols(settings.fusion_universe, limit=8)
    return (
        "Fusion策略: 美股日内多因子。"
        f"基准 {settings.fusion_benchmark.replace('US.', '')}，"
        f"回看 {settings.fusion_lookback_bars} 根 1分钟K，"
        f"观察池 {universe}。"
    )


def _cascade_summary(settings) -> str:
    return cascade_summary_line(settings)


def _auto_stack_summary(settings) -> str:
    baseline_weight, fusion_weight, cascade_weight, reserve_weight = stack_allocations(settings)
    sleeves: list[tuple[str, float, str | None]] = []

    if settings.stack_baseline_enabled and baseline_weight > 0:
        sleeves.append(("基线 Baseline", baseline_weight, None))
    if fusion_weight > 0:
        sleeves.append(("Fusion 日内", fusion_weight, None))
    if cascade_weight > 0:
        sleeves.append(("Claude/Cascade", cascade_weight, "只执行 Futu 可交易部分；crypto 预算保留现金。"))
    if reserve_weight > 0:
        sleeves.append(("现金预留", reserve_weight, None))

    if not sleeves:
        return "现在后台自动运行: 没有启用任何模块。"

    mode_label = "自定义组合"
    if len(sleeves) == 1 and sleeves[0][0] == "Fusion 日内" and abs(fusion_weight - 1.0) <= 1e-9:
        mode_label = "Fusion Only"
    elif len(sleeves) == 1 and sleeves[0][0] == "基线 Baseline" and abs(baseline_weight - 1.0) <= 1e-9:
        mode_label = "Baseline Only"
    elif len(sleeves) == 1 and sleeves[0][0] == "Claude/Cascade" and abs(cascade_weight - 1.0) <= 1e-9:
        mode_label = "Cascade Only"
    elif settings.stack_baseline_enabled and abs(baseline_weight - 0.55) <= 1e-9 and abs(fusion_weight - 0.35) <= 1e-9 and abs(cascade_weight) <= 1e-9 and abs(reserve_weight - 0.10) <= 1e-9:
        mode_label = "Full Stack"
    elif settings.stack_baseline_enabled and abs(baseline_weight - 0.25) <= 1e-9 and abs(fusion_weight - 0.25) <= 1e-9 and abs(cascade_weight - 0.50) <= 1e-9 and abs(reserve_weight) <= 1e-9:
        mode_label = "我的策略组 50% + Claude 50%"

    if len(sleeves) == 1:
        headline = f"现在后台自动运行: 只跑 1 个模块，即 {sleeves[0][0]}。"
    else:
        headline = f"现在后台自动运行: {len(sleeves)} 个模块一起跑。"

    mode_text = f"当前自动盘模式: {mode_label}"
    ratio_text = "占比: " + " + ".join(f"{name} {weight:.0%}" for name, weight, _ in sleeves)
    detail_lines = [f"{idx}. {name}: {weight:.0%}" for idx, (name, weight, _) in enumerate(sleeves, start=1)]
    for _, _, note in sleeves:
        if note:
            detail_lines.append(note)
    return "\n".join([mode_text, headline, ratio_text, *detail_lines])


def _cost_summary(settings) -> str:
    if not settings.trade_costs_enabled:
        return "当前费用模型: 关闭。回测和复盘不会扣估算交易成本。"
    return (
        "当前费用模型: 已开启。"
        f" 费用档案 {settings.trade_cost_profile}，"
        " 会把佣金 / 平台费 / 结算费 / SEC / TAF 按配置估算进回测和历史复盘。"
    )


def _ct_engine_pid() -> int:
    if not CT_PID_FILE.exists():
        return 0
    try:
        return int(CT_PID_FILE.read_text().strip())
    except Exception:
        return 0


def _ct_engine_running() -> bool:
    pid = _ct_engine_pid()
    return pid > 0 and is_pid_running(pid)


def _ct_status_text() -> str:
    if CT_STATUS_FILE.exists():
        try:
            payload = json.loads(CT_STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        action = str(payload.get("action", ""))
        detail = str(payload.get("detail", "") or "")[:80]
        regime = str(payload.get("regime_label", "") or "")
        updated_at = _format_status_timestamp(str(payload.get("updated_at", "")))
        if action:
            regime_part = f" | {regime}" if regime else ""
            ts_part = f" | {updated_at}" if updated_at else ""
            return f"Cascade引擎 / CT Engine: 运行中 / running | {action}{regime_part}{ts_part} | {detail}"
    if _ct_engine_running():
        return f"Cascade引擎 / CT Engine: 运行中 / running (pid={_ct_engine_pid()})"
    return "Cascade引擎 / CT Engine: 已停止 / stopped"


def _ct_build_env() -> dict[str, str]:
    env = os.environ.copy()
    parts = [str(CT_SRC_ROOT)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _ct_engine_args(*, dry_run: bool) -> list[str]:
    base = [str(CT_VENV_PYTHON), "-m", "claude_trade.cli", "run"]
    if dry_run:
        base.append("--dry-run")
    if CAFFEINATE_BIN.exists():
        return [str(CAFFEINATE_BIN), "-i", "-m", "-s", *base]
    return base


class ControlPanel:
    def __init__(self) -> None:
        self._restart_dashboard_on_boot = os.getenv("TAA_FUTU_RESTART_DASHBOARD", "0") == "1"
        self.root = tk.Tk()
        self.root.title("TAA + Futu 控制台 / Control Panel")
        self.root.geometry("1500x960")
        self.root.minsize(1240, 780)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.dashboard_process: subprocess.Popen[str] | None = None
        self._wrapping_labels: list[ttk.Label] = []
        self._status_refresh_after_id: str | None = None
        self.main_pane: tk.PanedWindow | None = None
        self.content_vertical_pane: tk.PanedWindow | None = None
        self.top_pane: tk.PanedWindow | None = None
        self.middle_pane: tk.PanedWindow | None = None
        self._scroll_canvases: list[tk.Canvas] = []
        self._pane_constraint_after_id: str | None = None

        self.opend_host = tk.StringVar(value="127.0.0.1")
        self.opend_port = tk.StringVar(value="11111")
        self.dashboard_port = tk.StringVar(value="8501")
        self.start_date = tk.StringVar(value="2015-01-01")
        self.end_date = tk.StringVar(value=time.strftime("%Y-%m-%d"))
        self.backtest_strategy = tk.StringVar(value="baseline")
        self.manual_strategy = tk.StringVar(value="fusion")
        initial_settings = load_settings(REPO_ROOT / ".env")
        self.stack_baseline_enabled = tk.BooleanVar(value=initial_settings.stack_baseline_enabled)
        self.stack_baseline_weight = tk.StringVar(value=f"{initial_settings.stack_baseline_weight:.2f}")
        self.stack_fusion_weight = tk.StringVar(value=f"{initial_settings.stack_fusion_weight:.2f}")
        self.stack_cascade_weight = tk.StringVar(value=f"{initial_settings.stack_cascade_weight:.2f}")

        self.opend_status = tk.StringVar(value="OpenD 状态 / Status: 检查中 / checking...")
        self.dashboard_status = tk.StringVar(value="监控页 / Dashboard: 已停止 / stopped")
        self.auto_status = tk.StringVar(value="自动运行 / Auto Run: 已停止 / stopped")
        self.watchdog_status = tk.StringVar(value="守护监控 / Watchdog: 已停止 / stopped")
        self.ct_engine_status = tk.StringVar(value="Cascade引擎 / CT Engine: 检查中 / checking...")
        self.trade_mode_status = tk.StringVar(value="交易模式 / Trade Mode: 检查中 / checking...")
        self.strategy_status = tk.StringVar(value="当前策略 / Strategy: 检查中 / checking...")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(300, self._drain_log_queue)
        self.root.after(500, self.refresh_status)
        if self._restart_dashboard_on_boot:
            self.root.after(1200, self.start_dashboard)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.main_pane = tk.PanedWindow(
            self.root,
            orient=tk.VERTICAL,
            sashwidth=16,
            sashrelief=tk.RAISED,
            showhandle=True,
            handlesize=14,
            handlepad=4,
            sashcursor="sb_v_double_arrow",
            opaqueresize=False,
            bg="#d8d8d8",
            bd=0,
        )
        self.main_pane.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        content = ttk.Frame(self.main_pane, padding=4)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        self.content_vertical_pane = tk.PanedWindow(
            content,
            orient=tk.VERTICAL,
            sashwidth=16,
            sashrelief=tk.RAISED,
            showhandle=True,
            handlesize=14,
            handlepad=4,
            sashcursor="sb_v_double_arrow",
            opaqueresize=False,
            bg="#d8d8d8",
            bd=0,
        )
        self.content_vertical_pane.grid(row=0, column=0, sticky="nsew")
        self.root.bind_all("<MouseWheel>", self._on_global_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_global_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_global_mousewheel, add="+")

        top_pane = tk.PanedWindow(
            self.content_vertical_pane,
            orient=tk.HORIZONTAL,
            sashwidth=16,
            sashrelief=tk.RAISED,
            showhandle=True,
            handlesize=14,
            handlepad=4,
            sashcursor="sb_h_double_arrow",
            opaqueresize=False,
            bg="#d8d8d8",
            bd=0,
        )
        self.top_pane = top_pane
        self.content_vertical_pane.add(top_pane, minsize=260, stretch="always")

        status_frame, status_inner = self._create_scrollable_section(top_pane, "状态 / Status")
        top_pane.add(status_frame, minsize=360, stretch="always")
        for idx, text_var in enumerate([self.opend_status, self.dashboard_status, self.ct_engine_status, self.trade_mode_status, self.strategy_status, self.auto_status, self.watchdog_status]):
            label = ttk.Label(status_inner, textvariable=text_var, justify="left", anchor="w")
            label.grid(row=idx, column=0, sticky="ew", pady=(0 if idx == 0 else 8, 0))
            self._wrapping_labels.append(label)

        service_frame, service_inner = self._create_section(top_pane, "一键服务 / One-Click Service")
        top_pane.add(service_frame, minsize=240, stretch="always")
        service_buttons = [
            ("一键启动 / One-Click Start", self.one_click_start),
            ("一键重启 / Restart All", self.restart_console_and_dashboard),
            ("打开监控页 / Open Dashboard", self.start_dashboard),
            ("停止监控页 / Stop Dashboard", self.stop_dashboard),
            ("打开 OpenD / Open FutuOpenD", self.open_futu_opend),
            ("刷新状态 / Refresh Status", self.refresh_status),
        ]
        for idx, (text, command) in enumerate(service_buttons):
            ttk.Button(service_inner, text=text, command=command).grid(row=idx, column=0, sticky="ew", pady=(0 if idx == 0 else 8, 0))

        config_frame, config_inner = self._create_scrollable_section(top_pane, "连接设置 / Connection")
        config_inner.columnconfigure(1, weight=1)
        top_pane.add(config_frame, minsize=260, stretch="always")
        ttk.Label(config_inner, text="OpenD 地址 / Host").grid(row=0, column=0, sticky="w")
        ttk.Entry(config_inner, textvariable=self.opend_host).grid(row=0, column=1, sticky="ew", padx=(10, 0))
        ttk.Label(config_inner, text="OpenD 端口 / Port").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(config_inner, textvariable=self.opend_port).grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(10, 0))
        ttk.Label(config_inner, text="监控页端口 / Dashboard Port").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(config_inner, textvariable=self.dashboard_port).grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=(10, 0))
        ttk.Label(config_inner, text="常用组合 / Quick Stack").grid(row=3, column=0, sticky="w", pady=(14, 0))
        ttk.Button(config_inner, text="单跑 Fusion / Fusion Only", command=self.use_fusion_only).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(config_inner, text="我的策略组 50% + Claude 50%", command=self.use_fusion_cascade_split).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(config_inner, text="三策略组合 / Full Stack", command=self.use_full_stack).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(config_inner, text="交易环境 / Trade Mode").grid(row=7, column=0, sticky="w", pady=(14, 0))
        ttk.Button(config_inner, text="切到模拟盘 / Use SIMULATE", command=self.arm_simulate_mode).grid(row=8, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(config_inner, text="切到实盘手动 / Arm REAL Manual", command=self.arm_real_manual_mode).grid(row=9, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(config_inner, text="切到实盘自动 / Arm REAL Auto", command=self.arm_real_auto_mode).grid(row=10, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(config_inner, text="实用工具 / Utilities").grid(row=11, column=0, sticky="w", pady=(14, 0))
        ttk.Button(config_inner, text="实盘就绪检查 / Check REAL Readiness", command=self.check_real_readiness).grid(row=12, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(config_inner, text="设置交易密码MD5 / Set Trade Pwd MD5", command=self.set_trade_password_md5).grid(row=13, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(config_inner, text="安装开机自启 / Install Login Auto Start", command=self.install_login_auto_start).grid(row=14, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(config_inner, text="关闭开机自启 / Remove Login Auto Start", command=self.uninstall_login_auto_start).grid(row=15, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(config_inner, text="高级组合 / Advanced Stack").grid(row=16, column=0, sticky="w", pady=(14, 0))
        ttk.Checkbutton(config_inner, text="启用基线 sleeve / Enable Baseline Sleeve", variable=self.stack_baseline_enabled).grid(row=16, column=1, sticky="w", padx=(10, 0), pady=(14, 0))
        ttk.Label(config_inner, text="基线权重 / Baseline Weight").grid(row=17, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(config_inner, textvariable=self.stack_baseline_weight).grid(row=17, column=1, sticky="ew", padx=(10, 0), pady=(10, 0))
        ttk.Label(config_inner, text="Fusion 权重 / Fusion Weight").grid(row=18, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(config_inner, textvariable=self.stack_fusion_weight).grid(row=18, column=1, sticky="ew", padx=(10, 0), pady=(10, 0))
        ttk.Label(config_inner, text="Cascade 权重 / Cascade Weight").grid(row=19, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(config_inner, textvariable=self.stack_cascade_weight).grid(row=19, column=1, sticky="ew", padx=(10, 0), pady=(10, 0))
        ttk.Button(config_inner, text="应用组合配置 / Apply Stack Config", command=self.apply_stack_config).grid(row=20, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        middle_pane = tk.PanedWindow(
            self.content_vertical_pane,
            orient=tk.HORIZONTAL,
            sashwidth=16,
            sashrelief=tk.RAISED,
            showhandle=True,
            handlesize=14,
            handlepad=4,
            sashcursor="sb_h_double_arrow",
            opaqueresize=False,
            bg="#d8d8d8",
            bd=0,
        )
        self.middle_pane = middle_pane
        self.content_vertical_pane.add(middle_pane, minsize=220, stretch="always")

        research_frame, research_inner = self._create_section(middle_pane, "历史模拟 / Historical Simulation")
        research_inner.columnconfigure(1, weight=1)
        middle_pane.add(research_frame, minsize=320, stretch="always")
        ttk.Label(research_inner, text="开始日期 / Start Date").grid(row=0, column=0, sticky="w")
        ttk.Entry(research_inner, textvariable=self.start_date).grid(row=0, column=1, sticky="ew", padx=(10, 0))
        ttk.Label(research_inner, text="结束日期 / End Date").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(research_inner, textvariable=self.end_date).grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(10, 0))
        ttk.Label(research_inner, text="回测方法 / Study Mode").grid(row=2, column=0, sticky="w", pady=(10, 0))
        strategy_combo = ttk.Combobox(
            research_inner,
            textvariable=self.backtest_strategy,
            state="readonly",
            values=[
                "baseline",
                "fusion",
                "cascade",
                "stack",
                "account",
                "exact",
            ],
        )
        strategy_combo.grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=(10, 0))
        ttk.Button(research_inner, text="运行回测 / Run Backtest", command=self.run_backtest).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Button(research_inner, text="查看月度信号 / Show Monthly Signal", command=self.run_signals).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        baseline_frame, baseline_inner = self._create_section(middle_pane, "基线策略 / Baseline Strategy")
        middle_pane.add(baseline_frame, minsize=260, stretch="always")
        ttk.Button(baseline_inner, text="预演订单 / Plan Orders", command=self.run_paper_trade).grid(row=0, column=0, sticky="ew")
        ttk.Button(baseline_inner, text="提交订单 / Submit Orders", command=self.submit_paper_trade).grid(row=1, column=0, sticky="ew", pady=(8, 0))
        baseline_note = ttk.Label(
            baseline_inner,
            text="用于月频 ETF 策略。先预演，再提交。适合看中长期调仓，不适合日内盯盘。",
            justify="left",
            anchor="w",
            wraplength=320,
        )
        baseline_note.grid(row=2, column=0, sticky="ew", pady=(12, 0))

        fusion_frame, fusion_inner = self._create_section(middle_pane, "专属日内策略 / Fusion Intraday")
        middle_pane.add(fusion_frame, minsize=300, stretch="always")
        ttk.Button(fusion_inner, text="试运行 / Run Dry-Run", command=self.run_fusion).grid(row=0, column=0, sticky="ew")
        ttk.Button(fusion_inner, text="提交订单 / Submit Orders", command=self.submit_fusion).grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(fusion_inner, text="启动自动运行 / Start Auto Run", command=self.start_auto_fusion).grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(fusion_inner, text="停止自动运行 / Stop Auto Run", command=self.stop_auto_fusion).grid(row=3, column=0, sticky="ew", pady=(8, 0))
        fusion_note = ttk.Label(
            fusion_inner,
            text="这是我这边的日内策略。需要信号才会下单；没信号时会等，不会为了凑交易去乱动。",
            justify="left",
            anchor="w",
            wraplength=340,
        )
        fusion_note.grid(row=4, column=0, sticky="ew", pady=(12, 0))

        cascade_frame, cascade_inner = self._create_section(middle_pane, "Claude 策略 / Claude-Cascade")
        middle_pane.add(cascade_frame, minsize=300, stretch="always")
        ttk.Button(cascade_inner, text="试运行 / Run Dry-Run", command=self.run_cascade).grid(row=0, column=0, sticky="ew")
        ttk.Button(cascade_inner, text="提交订单 / Submit Orders", command=self.submit_cascade).grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Separator(cascade_inner, orient="horizontal").grid(row=2, column=0, sticky="ew", pady=(12, 4))
        ttk.Label(cascade_inner, text="Cascade 独立引擎 / Standalone Engine", anchor="w").grid(row=3, column=0, sticky="ew")
        ttk.Button(cascade_inner, text="▶ 启动引擎 (试运行) / Start Engine Dry-Run", command=self.ct_start_engine_dryrun).grid(row=4, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(cascade_inner, text="▶ 启动引擎 (实盘) / Start Engine Live", command=self.ct_start_engine_live).grid(row=5, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(cascade_inner, text="■ 停止引擎 / Stop Engine", command=self.ct_stop_engine).grid(row=6, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(cascade_inner, text="🌐 打开 Cascade 监控页 / Open CT Dashboard", command=self.ct_open_dashboard).grid(row=7, column=0, sticky="ew", pady=(6, 0))
        cascade_note = ttk.Label(
            cascade_inner,
            text="上方两个按钮操作 taa_futu 里的 Cascade 路径（无独立引擎）。下方四个按钮控制 claude-trade 独立引擎及其专属监控页（端口 8051）。",
            justify="left",
            anchor="w",
            wraplength=340,
        )
        cascade_note.grid(row=8, column=0, sticky="ew", pady=(10, 0))

        log_frame = ttk.LabelFrame(self.main_pane, text="输出日志 / Output", padding=12)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        self.main_pane.add(content, minsize=420, stretch="always")
        self.main_pane.add(log_frame, minsize=180, stretch="always")

        log_toolbar = ttk.Frame(log_frame)
        log_toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        log_toolbar.columnconfigure(0, weight=1)
        ttk.Button(log_toolbar, text="放大日志 / Log +", command=self.expand_log_panel).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(log_toolbar, text="缩小日志 / Log -", command=self.shrink_log_panel).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(log_toolbar, text="清空日志 / Clear", command=self.clear_log).grid(row=0, column=3)

        self.log_text = tk.Text(log_frame, wrap="word", height=22, font=("Menlo", 12))
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.insert("end", "已就绪 / Ready.\n请先点“一键启动 / One-Click Start”。\n")
        self.log_text.configure(state="disabled")

        status_inner.bind("<Configure>", self._update_wraplengths)
        self.root.bind("<Configure>", self._schedule_root_constraints, add="+")
        for pane in [self.main_pane, self.content_vertical_pane, self.top_pane, self.middle_pane]:
            if pane is not None:
                pane.bind("<ButtonRelease-1>", self._schedule_pane_constraints, add="+")
        self.root.after(120, self._enforce_pane_limits)

    def _create_section(self, parent: tk.Misc, title: str) -> tuple[ttk.LabelFrame, ttk.Frame]:
        frame = ttk.LabelFrame(parent, text=title, padding=12)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        inner = ttk.Frame(frame)
        inner.grid(row=0, column=0, sticky="nsew")
        inner.columnconfigure(0, weight=1)
        return frame, inner

    def _create_scrollable_section(self, parent: tk.Misc, title: str) -> tuple[ttk.LabelFrame, ttk.Frame]:
        frame = ttk.LabelFrame(parent, text=title, padding=12)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        canvas = tk.Canvas(frame, highlightthickness=0, bd=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        scrollbar.grid_remove()

        def _sync_scrollbar(first: str, last: str) -> None:
            scrollbar.set(first, last)
            try:
                first_value = float(first)
                last_value = float(last)
            except ValueError:
                scrollbar.grid(row=0, column=1, sticky="ns")
                return
            if first_value <= 0.0 and last_value >= 1.0:
                scrollbar.grid_remove()
            else:
                scrollbar.grid(row=0, column=1, sticky="ns")

        canvas.configure(yscrollcommand=_sync_scrollbar)

        inner = ttk.Frame(canvas)
        inner.columnconfigure(0, weight=1)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_scrollregion(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.yview_moveto(min(canvas.yview()[0], 1.0))

        def _sync_width(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        inner.bind("<Configure>", _sync_scrollregion)
        canvas.bind("<Configure>", _sync_width)
        self._scroll_canvases.append(canvas)
        return frame, inner

    @staticmethod
    def _widget_is_descendant(widget: tk.Misc | None, ancestor: tk.Misc | None) -> bool:
        current = widget
        while current is not None:
            if current == ancestor:
                return True
            parent_name = current.winfo_parent()
            if not parent_name:
                return False
            try:
                current = current.nametowidget(parent_name)
            except KeyError:
                return False
        return False

    def _on_global_mousewheel(self, event) -> str | None:
        target = self.root.winfo_containing(event.x_root, event.y_root)
        canvas = next((item for item in self._scroll_canvases if self._widget_is_descendant(target, item)), None)
        if canvas is None:
            return None
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta_value = getattr(event, "delta", 0)
            if delta_value == 0:
                return None
            delta = -1 if delta_value > 0 else 1
        canvas.yview_scroll(delta, "units")
        return "break"

    def _update_wraplengths(self, _event=None) -> None:
        for label in self._wrapping_labels:
            parent = label.nametowidget(label.winfo_parent())
            wraplength = max(parent.winfo_width() - 28, 240)
            label.configure(wraplength=wraplength)

    def _schedule_pane_constraints(self, _event=None) -> None:
        if self._pane_constraint_after_id is not None:
            try:
                self.root.after_cancel(self._pane_constraint_after_id)
            except ValueError:
                pass
        self._pane_constraint_after_id = self.root.after(12, self._enforce_pane_limits)

    def _schedule_root_constraints(self, _event=None) -> None:
        if self._pane_constraint_after_id is not None:
            try:
                self.root.after_cancel(self._pane_constraint_after_id)
            except ValueError:
                pass
        self._pane_constraint_after_id = self.root.after(80, self._enforce_pane_limits)

    def _pane_min_sizes(self, pane: tk.PanedWindow | None, fallback: list[int]) -> list[int]:
        if pane is None:
            return fallback
        panes = pane.panes()
        min_sizes: list[int] = []
        for index, child in enumerate(panes):
            fallback_size = fallback[index] if index < len(fallback) else fallback[-1]
            try:
                minsize = int(pane.panecget(child, "minsize"))
            except (tk.TclError, ValueError):
                minsize = fallback_size
            min_sizes.append(max(minsize, fallback_size))
        return min_sizes or fallback

    def _effective_min_sizes(self, total: int, sash_width: int, min_sizes: list[int]) -> list[int]:
        available = max(total - sash_width * max(len(min_sizes) - 1, 0), 1)
        required = sum(min_sizes)
        if required <= available:
            return min_sizes
        scale = available / required
        scaled = [max(60, int(size * scale)) for size in min_sizes]
        diff = available - sum(scaled)
        if diff != 0 and scaled:
            scaled[-1] = max(60, scaled[-1] + diff)
        return scaled

    def _clamp_horizontal_pane(self, pane: tk.PanedWindow | None, fallback_min_sizes: list[int]) -> None:
        if pane is None:
            return
        pane.update_idletasks()
        total = pane.winfo_width()
        min_sizes = self._pane_min_sizes(pane, fallback_min_sizes)
        if total <= 0 or len(min_sizes) <= 1:
            return
        sash_width = int(str(pane.cget("sashwidth")) or "10")
        mins = self._effective_min_sizes(total, sash_width, min_sizes)
        sash_count = len(mins) - 1
        previous_edge = 0
        for index in range(sash_count):
            try:
                current = pane.sash_coord(index)[0]
            except tk.TclError:
                return
            lower = previous_edge + mins[index]
            remaining_min = sum(mins[index + 1 :])
            remaining_sashes = sash_width * (sash_count - index)
            upper = total - remaining_min - remaining_sashes
            clamped = max(lower, min(current, upper))
            pane.sash_place(index, clamped, 1)
            previous_edge = clamped + sash_width

    def _clamp_vertical_pane(self, pane: tk.PanedWindow | None, fallback_min_sizes: list[int]) -> None:
        if pane is None:
            return
        pane.update_idletasks()
        total = pane.winfo_height()
        min_sizes = self._pane_min_sizes(pane, fallback_min_sizes)
        if total <= 0 or len(min_sizes) <= 1:
            return
        sash_width = int(str(pane.cget("sashwidth")) or "10")
        mins = self._effective_min_sizes(total, sash_width, min_sizes)
        sash_count = len(mins) - 1
        previous_edge = 0
        for index in range(sash_count):
            try:
                current = pane.sash_coord(index)[1]
            except tk.TclError:
                return
            lower = previous_edge + mins[index]
            remaining_min = sum(mins[index + 1 :])
            remaining_sashes = sash_width * (sash_count - index)
            upper = total - remaining_min - remaining_sashes
            clamped = max(lower, min(current, upper))
            pane.sash_place(index, 1, clamped)
            previous_edge = clamped + sash_width

    def _enforce_pane_limits(self) -> None:
        self._pane_constraint_after_id = None
        self._clamp_vertical_pane(self.main_pane, [420, 180])
        self._clamp_vertical_pane(self.content_vertical_pane, [260, 220])
        self._clamp_horizontal_pane(self.top_pane, [360, 240, 260])
        self._clamp_horizontal_pane(self.middle_pane, [320, 260, 300, 300])

    def expand_log_panel(self) -> None:
        if self.main_pane is None:
            return
        self.root.update_idletasks()
        current = self.main_pane.sash_coord(0)[1]
        self.main_pane.sash_place(0, 1, max(280, current - 120))
        self._schedule_pane_constraints()

    def shrink_log_panel(self) -> None:
        if self.main_pane is None:
            return
        self.root.update_idletasks()
        current = self.main_pane.sash_coord(0)[1]
        total = max(self.main_pane.winfo_height(), 600)
        self.main_pane.sash_place(0, 1, min(total - 160, current + 120))
        self._schedule_pane_constraints()

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "日志已清空 / Log cleared.\n")
        self.log_text.configure(state="disabled")

    def log(self, message: str) -> None:
        self.log_queue.put(message.rstrip() + "\n")

    def _drain_log_queue(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(300, self._drain_log_queue)

    def refresh_status(self) -> None:
        host = self.opend_host.get().strip() or "127.0.0.1"
        port = int(self.opend_port.get().strip() or "11111")
        dashboard_port = int(self.dashboard_port.get().strip() or "8501")
        settings = self._current_settings()

        if is_port_open(host, port):
            self.opend_status.set(f"OpenD 状态 / Status: 已连接 / connected ({host}:{port})")
        else:
            self.opend_status.set(f"OpenD 状态 / Status: 未连接 / offline ({host}:{port})")

        dashboard_running = self.dashboard_process is not None and self.dashboard_process.poll() is None
        if dashboard_running or is_port_open("127.0.0.1", dashboard_port):
            self.dashboard_status.set(f"监控页 / Dashboard: 运行中 / running (http://localhost:{dashboard_port})")
        else:
            self.dashboard_status.set("监控页 / Dashboard: 已停止 / stopped")

        unlock_ready = "已配置 / set" if settings.futu_unlock_trade_password_md5 else "未配置 / missing"
        auto_real = "打开 / on" if settings.futu_allow_auto_real else "关闭 / off"
        try:
            baseline_weight, fusion_weight, cascade_weight, reserve_weight = stack_allocations(settings)
            stack_text = stack_label(settings)
        except ValueError as exc:
            baseline_weight, fusion_weight, cascade_weight, reserve_weight = 0.0, 0.0, 0.0, 0.0
            stack_text = f"配置错误 / invalid ({exc})"
        self.trade_mode_status.set(
            f"交易模式 / Trade Mode: {settings.futu_trd_env} | 实盘下单 / REAL Submit: "
            f"{'打开 / on' if settings.futu_enable_real_trading else '关闭 / off'} | "
            f"实盘自动 / REAL Auto: {auto_real} | 交易密码MD5: {unlock_ready}\n"
            f"组合 / Stack: {stack_text} | 基线开关 / Baseline: "
            f"{'开 / on' if settings.stack_baseline_enabled else '关 / off'} "
            f"({baseline_weight:.0%}) | Fusion ({fusion_weight:.0%}) | "
            f"Cascade ({cascade_weight:.0%}) | 预留 / Reserve ({reserve_weight:.0%})"
        )
        self.strategy_status.set(self._strategy_status_text(settings))
        self.auto_status.set(self._auto_status_text())
        self.watchdog_status.set(self._watchdog_status_text())
        self.ct_engine_status.set(_ct_status_text())
        self._schedule_status_refresh()

    def _strategy_status_text(self, settings) -> str:
        study_mode = self.backtest_strategy.get().strip() or "baseline"
        manual_mode = self.manual_strategy.get().strip() or "fusion"
        current_backtest = f"现在点“运行回测”会测: {_study_mode_label(study_mode)}。"
        current_manual = f"现在点“预演 / 提交订单”会操作: {_manual_strategy_label(manual_mode)}。"
        return (
            f"{current_backtest}\n"
            f"{current_manual}\n"
            f"{_auto_stack_summary(settings)}\n"
            f"{_baseline_summary(settings)}\n"
            f"{_fusion_summary(settings)}\n"
            f"{_cascade_summary(settings)}\n"
            f"{_cost_summary(settings)}"
        )

    def _schedule_status_refresh(self, delay_ms: int = 5_000) -> None:
        if self._status_refresh_after_id is not None:
            try:
                self.root.after_cancel(self._status_refresh_after_id)
            except ValueError:
                pass
        self._status_refresh_after_id = self.root.after(delay_ms, self.refresh_status)

    def _auto_status_text(self) -> str:
        if AUTO_TRADER_STATUS_FILE.exists():
            try:
                payload = json.loads(AUTO_TRADER_STATUS_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            detail = payload.get("detail", "")
            action = payload.get("action", "unknown")
            running = bool(payload.get("running"))
            updated_at = _format_status_timestamp(str(payload.get("updated_at", "")))
            updated_dt = _parse_status_timestamp(str(payload.get("updated_at", "")))
            poll_seconds = int(payload.get("poll_seconds", 60) or 60)
            stale_after = max(180, poll_seconds * 3)
            if updated_dt is not None:
                age_seconds = (datetime.now(updated_dt.tzinfo) - updated_dt).total_seconds()
                if age_seconds > stale_after:
                    return (
                        "自动运行 / Auto Run: 状态陈旧 / stale "
                        f"| 超过 {int(age_seconds)}s 未更新 / no heartbeat | {action} | {detail}"
                    )
            if running:
                health, action_label, detail_label = _friendly_runtime_status(action, detail)
                updated_text = f" | 更新时间 / Updated {updated_at}" if updated_at else ""
                return f"自动运行 / Auto Run: 运行中 / running | {health} | {action_label} | {detail_label}{updated_text}"
        if AUTO_TRADER_PID_FILE.exists():
            try:
                pid = int(AUTO_TRADER_PID_FILE.read_text(encoding="utf-8").strip())
            except ValueError:
                pid = 0
            if pid and is_pid_running(pid):
                return f"自动运行 / Auto Run: 运行中 / running (pid={pid})"
        return "自动运行 / Auto Run: 已停止 / stopped"

    def _watchdog_status_text(self) -> str:
        if WATCHDOG_STATUS_FILE.exists():
            try:
                payload = json.loads(WATCHDOG_STATUS_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            detail = payload.get("detail", "")
            action = payload.get("action", "unknown")
            running = bool(payload.get("running"))
            updated_at = _format_status_timestamp(str(payload.get("updated_at", "")))
            updated_dt = _parse_status_timestamp(str(payload.get("updated_at", "")))
            next_check = payload.get("next_check_seconds")
            stale_after = max(600, int(next_check) * 2 if isinstance(next_check, (int, float)) else 600)
            if updated_dt is not None:
                age_seconds = (datetime.now(updated_dt.tzinfo) - updated_dt).total_seconds()
                if age_seconds > stale_after:
                    return (
                        "守护监控 / Watchdog: 状态陈旧 / stale "
                        f"| 超过 {int(age_seconds)}s 未更新 / no heartbeat | {action} | {detail}"
                    )
            if running:
                health, action_label, detail_label = _friendly_runtime_status(action, detail)
                updated_text = f" | 更新时间 / Updated {updated_at}" if updated_at else ""
                next_text = f" | 下次检查 / Next ~{next_check}s" if isinstance(next_check, (int, float)) else ""
                return f"守护监控 / Watchdog: 运行中 / running | {health} | {action_label} | {detail_label}{next_text}{updated_text}"
        if WATCHDOG_PID_FILE.exists():
            try:
                pid = int(WATCHDOG_PID_FILE.read_text(encoding="utf-8").strip())
            except ValueError:
                pid = 0
            if pid and is_pid_running(pid):
                return f"守护监控 / Watchdog: 运行中 / running (pid={pid})"
        return "守护监控 / Watchdog: 已停止 / stopped"

    def _run_command_async(self, title: str, command: list[str], env: dict[str, str] | None = None) -> None:
        def worker() -> None:
            self.log(f"$ {' '.join(command)}")
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=env or build_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert process.stdout is not None
            for line in process.stdout:
                self.log(line.rstrip())
            return_code = process.wait()
            if return_code == 0:
                self.log(f"[{title}] 已完成 / finished successfully.")
            else:
                self.log(f"[{title}] 退出，状态码 / exited with code {return_code}.")
            self.root.after(0, self.refresh_status)

        threading.Thread(target=worker, daemon=True).start()

    def _python_cli(self, *args: str) -> list[str]:
        return [str(VENV_PYTHON), "-m", "taa_futu.cli", *args]

    def open_futu_opend(self) -> None:
        if not FUTU_OPEND_APP.exists():
            messagebox.showerror("FutuOpenD", f"缺少应用 / Missing app: {FUTU_OPEND_APP}")
            return
        subprocess.Popen(["open", "-a", str(FUTU_OPEND_APP)])
        self.log("已打开 FutuOpenD / Opened FutuOpenD.")
        self.root.after(1200, self.refresh_status)

    def open_dashboard_browser(self) -> None:
        dashboard_port = int(self.dashboard_port.get().strip() or "8501")
        webbrowser.open(f"http://localhost:{dashboard_port}")
        self.log(f"已打开浏览器 / Opened browser at http://localhost:{dashboard_port}")

    def open_auto_log(self) -> None:
        if not AUTO_TRADER_LOG_FILE.exists():
            self.log("自动日志不存在 / Auto log does not exist yet.")
            return
        subprocess.Popen(["open", str(AUTO_TRADER_LOG_FILE)])
        self.log(f"已打开自动日志 / Opened auto log: {AUTO_TRADER_LOG_FILE}")

    def open_watchdog_log(self) -> None:
        if not WATCHDOG_LOG_FILE.exists():
            self.log("守护日志不存在 / Watchdog log does not exist yet.")
            return
        subprocess.Popen(["open", str(WATCHDOG_LOG_FILE)])
        self.log(f"已打开守护日志 / Opened watchdog log: {WATCHDOG_LOG_FILE}")

    def _ensure_env_file(self) -> None:
        if ENV_FILE.exists():
            return
        if ENV_EXAMPLE_FILE.exists():
            ENV_FILE.write_text(ENV_EXAMPLE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
            return
        ENV_FILE.write_text("", encoding="utf-8")

    def _update_env_values(self, updates: dict[str, str]) -> None:
        self._ensure_env_file()
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        remaining = dict(updates)
        new_lines: list[str] = []
        for line in lines:
            if "=" not in line or line.lstrip().startswith("#"):
                new_lines.append(line)
                continue
            key, _value = line.split("=", 1)
            if key in remaining:
                new_lines.append(f"{key}={remaining.pop(key)}")
            else:
                new_lines.append(line)
        for key, value in remaining.items():
            new_lines.append(f"{key}={value}")
        ENV_FILE.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
        for key, value in updates.items():
            os.environ[key] = value

    def _runtime_processes_running(self) -> bool:
        pid_files = [WATCHDOG_PID_FILE, AUTO_TRADER_PID_FILE]
        for pid_file in pid_files:
            if not pid_file.exists():
                continue
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                pid = 0
            if pid and is_pid_running(pid):
                return True
        return False

    def _stop_auto_runtime_processes(self, *, log_when_idle: bool = True) -> bool:
        subprocess.run(["launchctl", "unload", str(WATCHDOG_LAUNCH_AGENT_PLIST)], check=False)

        stop_messages: list[str] = []

        if WATCHDOG_PID_FILE.exists():
            try:
                pid = int(WATCHDOG_PID_FILE.read_text(encoding="utf-8").strip())
            except ValueError:
                pid = 0
            if pid and is_pid_running(pid):
                os.kill(pid, signal.SIGTERM)
                stop_messages.append(f"watchdog pid {pid}")

        if AUTO_TRADER_PID_FILE.exists():
            try:
                pid = int(AUTO_TRADER_PID_FILE.read_text(encoding="utf-8").strip())
            except ValueError:
                pid = 0
            if pid and is_pid_running(pid):
                os.kill(pid, signal.SIGTERM)
                stop_messages.append(f"auto trader pid {pid}")

        if stop_messages:
            self.log(f"已发送停止信号 / Sent stop signal to {' and '.join(stop_messages)}.")
            return True

        if log_when_idle:
            self.log("自动运行和守护监控都未运行 / Auto run and watchdog are both stopped.")
        return False

    def _start_auto_runtime_no_prompt(self) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        if WATCHDOG_LAUNCH_AGENT_PLIST.exists():
            subprocess.run(["launchctl", "load", str(WATCHDOG_LAUNCH_AGENT_PLIST)], check=False)
            self.log("已按新配置重新加载守护监控 / Reloaded watchdog with the new configuration.")
        else:
            with WATCHDOG_LOG_FILE.open("a", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    _watchdog_program_arguments(),
                    cwd=REPO_ROOT,
                    env=build_env(),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    text=True,
                )
            self.log(f"已按新配置重启自动运行守护 / Restarted auto-run watchdog with pid {process.pid}.")
        self.refresh_status()

    def _restart_auto_runtime_if_running(self, reason: str) -> None:
        if not self._runtime_processes_running():
            return
        self.log(
            f"检测到自动运行仍在使用旧配置，因“{reason}”自动重启后台以应用新配置 / "
            f"Auto runtime is restarting to apply the new config: {reason}."
        )
        self._stop_auto_runtime_processes(log_when_idle=False)
        self.root.after(1800, self._start_auto_runtime_no_prompt)

    def _apply_stack_env(
        self,
        *,
        baseline_enabled: bool,
        baseline_weight: float,
        fusion_weight: float,
        cascade_weight: float,
    ) -> None:
        total = (baseline_weight if baseline_enabled else 0.0) + fusion_weight + cascade_weight
        if total > 1.0 + 1e-9:
            raise ValueError("基线权重 + Fusion 权重 + Cascade 权重不能超过 1.00。")
        updates = {
            "STACK_BASELINE_ENABLED": str(baseline_enabled).lower(),
            "STACK_BASELINE_WEIGHT": f"{baseline_weight:.4f}",
            "STACK_FUSION_WEIGHT": f"{fusion_weight:.4f}",
            "STACK_CASCADE_WEIGHT": f"{cascade_weight:.4f}",
            "STACK_ISOLATE_BASELINE_SYMBOLS": "true",
        }
        self._update_env_values(updates)
        self.stack_baseline_enabled.set(baseline_enabled)
        self.stack_baseline_weight.set(f"{baseline_weight:.2f}")
        self.stack_fusion_weight.set(f"{fusion_weight:.2f}")
        self.stack_cascade_weight.set(f"{cascade_weight:.2f}")

    def apply_stack_config(self) -> None:
        try:
            baseline_weight = float(self.stack_baseline_weight.get().strip() or "0")
            fusion_weight = float(self.stack_fusion_weight.get().strip() or "0")
            cascade_weight = float(self.stack_cascade_weight.get().strip() or "0")
            self._apply_stack_env(
                baseline_enabled=bool(self.stack_baseline_enabled.get()),
                baseline_weight=baseline_weight,
                fusion_weight=fusion_weight,
                cascade_weight=cascade_weight,
            )
        except ValueError as exc:
            messagebox.showerror("组合配置错误 / Stack Config Error", str(exc))
            return
        self.log("已应用组合配置 / Applied stack configuration.")
        self._restart_auto_runtime_if_running("组合权重已修改")
        self.refresh_status()

    def use_fusion_only(self) -> None:
        self._apply_stack_env(baseline_enabled=False, baseline_weight=0.0, fusion_weight=1.0, cascade_weight=0.0)
        self.log("已切到单跑 Fusion / Switched to Fusion-only stack.")
        self._restart_auto_runtime_if_running("已切到 Fusion Only")
        self.refresh_status()

    def use_fusion_cascade_split(self) -> None:
        self._apply_stack_env(baseline_enabled=True, baseline_weight=0.25, fusion_weight=0.25, cascade_weight=0.5)
        self.log("已切到我的策略组 50% + Claude 50% / Switched to Ours 50% + Claude 50%.")
        self._restart_auto_runtime_if_running("已切到我的策略组 50% + Claude 50%")
        self.refresh_status()

    def use_full_stack(self) -> None:
        self._apply_stack_env(baseline_enabled=True, baseline_weight=0.55, fusion_weight=0.35, cascade_weight=0.0)
        self.log("已切到三策略组合 / Switched to the full stack.")
        self._restart_auto_runtime_if_running("已切到三策略组合")
        self.refresh_status()

    def arm_simulate_mode(self) -> None:
        self._update_env_values(
            {
                "FUTU_TRD_ENV": "SIMULATE",
                "FUTU_ENABLE_REAL_TRADING": "false",
                "FUTU_ALLOW_AUTO_REAL": "false",
            }
        )
        self.log("已切到模拟盘模式 / Switched to SIMULATE mode.")
        self._restart_auto_runtime_if_running("交易环境已切到 SIMULATE")
        self.refresh_status()

    def arm_real_manual_mode(self) -> None:
        if not messagebox.askyesno(
            "切到实盘手动 / Arm REAL Manual",
            "这会把环境切到 REAL，并允许手动真实下单，但不会放开真实自动交易。\n继续吗？\n\nThis switches the environment to REAL and allows manual live orders, but keeps live auto trading locked.\nContinue?",
        ):
            return
        self._update_env_values(
            {
                "FUTU_TRD_ENV": "REAL",
                "FUTU_ENABLE_REAL_TRADING": "true",
                "FUTU_ALLOW_AUTO_REAL": "false",
            }
        )
        self.log("已切到实盘手动模式 / Armed REAL manual mode.")
        self._restart_auto_runtime_if_running("交易环境已切到 REAL 手动")
        self.refresh_status()

    def arm_real_auto_mode(self) -> None:
        if not messagebox.askyesno(
            "切到实盘自动 / Arm REAL Auto",
            "这会把环境切到 REAL，并允许真实自动交易。\n这一步风险最高。\n继续吗？\n\nThis switches the environment to REAL and enables live auto trading.\nThis is the highest-risk mode.\nContinue?",
        ):
            return
        typed = simpledialog.askstring(
            "最终确认 / Final Confirmation",
            "请输入 AUTO REAL 作为最终确认。\n\nType AUTO REAL to arm live auto trading.",
            parent=self.root,
        )
        if typed != "AUTO REAL":
            self.log("已取消切换到真实自动模式 / Cancelled REAL auto mode.")
            return
        self._update_env_values(
            {
                "FUTU_TRD_ENV": "REAL",
                "FUTU_ENABLE_REAL_TRADING": "true",
                "FUTU_ALLOW_AUTO_REAL": "true",
            }
        )
        self.log("已切到实盘自动模式 / Armed REAL auto mode.")
        self._restart_auto_runtime_if_running("交易环境已切到 REAL 自动")
        self.refresh_status()

    def set_trade_password_md5(self) -> None:
        raw_password = simpledialog.askstring(
            "设置交易密码MD5 / Set Trade Password MD5",
            "请输入富途交易密码。程序只会把 MD5 写入 .env，不保存明文。\n\nEnter your Futu trade password. The app stores only the MD5 in .env, not the plain password.",
            parent=self.root,
            show="*",
        )
        if not raw_password:
            self.log("未更新交易密码MD5 / Trade password MD5 not changed.")
            return
        password_md5 = hashlib.md5(raw_password.encode('utf-8')).hexdigest()
        self._update_env_values({"FUTU_UNLOCK_TRADE_PASSWORD_MD5": password_md5})
        self.log("已更新交易密码MD5 / Updated trade password MD5 in .env.")
        self.refresh_status()

    def install_login_auto_start(self) -> None:
        plist_dir = WATCHDOG_LAUNCH_AGENT_PLIST.parent
        plist_dir.mkdir(parents=True, exist_ok=True)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        program_arguments = "".join(f"    <string>{arg}</string>\n" for arg in _watchdog_program_arguments())
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.jiao.taa_futu_watchdog</string>
  <key>ProgramArguments</key>
  <array>
{program_arguments.rstrip()}
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
  <string>{WATCHDOG_LOG_FILE}</string>
  <key>StandardErrorPath</key>
  <string>{WATCHDOG_LOG_FILE}</string>
</dict>
</plist>
"""
        if LEGACY_AUTO_TRADER_LAUNCH_AGENT_PLIST.exists():
            subprocess.run(["launchctl", "unload", str(LEGACY_AUTO_TRADER_LAUNCH_AGENT_PLIST)], check=False)
            LEGACY_AUTO_TRADER_LAUNCH_AGENT_PLIST.unlink()
        WATCHDOG_LAUNCH_AGENT_PLIST.write_text(plist_content, encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(WATCHDOG_LAUNCH_AGENT_PLIST)], check=False)
        subprocess.run(["launchctl", "load", str(WATCHDOG_LAUNCH_AGENT_PLIST)], check=True)
        self.log("已安装开机自启，现由守护监控负责稳定性 / Installed login auto start with watchdog protection.")
        self.root.after(1000, self.refresh_status)

    def uninstall_login_auto_start(self) -> None:
        subprocess.run(["launchctl", "unload", str(WATCHDOG_LAUNCH_AGENT_PLIST)], check=False)
        if WATCHDOG_LAUNCH_AGENT_PLIST.exists():
            WATCHDOG_LAUNCH_AGENT_PLIST.unlink()
        if LEGACY_AUTO_TRADER_LAUNCH_AGENT_PLIST.exists():
            subprocess.run(["launchctl", "unload", str(LEGACY_AUTO_TRADER_LAUNCH_AGENT_PLIST)], check=False)
            LEGACY_AUTO_TRADER_LAUNCH_AGENT_PLIST.unlink()
        self.log("已移除开机自启 / Removed login auto start.")
        self.root.after(1000, self.refresh_status)

    def start_dashboard(self) -> None:
        dashboard_port = int(self.dashboard_port.get().strip() or "8501")
        if self.dashboard_process is not None and self.dashboard_process.poll() is None:
            self.open_dashboard_browser()
            self.refresh_status()
            return

        command = [
            str(VENV_PYTHON),
            "-m",
            "streamlit",
            "run",
            str(DASHBOARD_APP),
            "--server.port",
            str(dashboard_port),
            "--browser.gatherUsageStats=false",
        ]
        self.dashboard_process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=build_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.log(f"正在启动监控页 / Starting dashboard on port {dashboard_port}...")

        def reader() -> None:
            assert self.dashboard_process is not None
            assert self.dashboard_process.stdout is not None
            for line in self.dashboard_process.stdout:
                self.log(f"[dashboard] {line.rstrip()}")
            return_code = self.dashboard_process.wait()
            self.log(f"[dashboard] 已停止 / stopped with code {return_code}.")
            self.root.after(0, self.refresh_status)

        threading.Thread(target=reader, daemon=True).start()
        self.root.after(1800, self.open_dashboard_browser)
        self.root.after(600, self.refresh_status)

    def stop_dashboard(self) -> None:
        if self.dashboard_process is None or self.dashboard_process.poll() is not None:
            self.log("监控页未运行 / Dashboard is not running.")
            self.refresh_status()
            return

        self.dashboard_process.terminate()
        self.log("正在停止监控页 / Stopping dashboard...")
        self.root.after(1200, self.refresh_status)

    def start_auto_fusion(self) -> None:
        if WATCHDOG_PID_FILE.exists():
            try:
                pid = int(WATCHDOG_PID_FILE.read_text(encoding="utf-8").strip())
            except ValueError:
                pid = 0
            if pid and is_pid_running(pid):
                self.log(f"守护监控已在运行 / Watchdog already running with pid {pid}.")
                self.refresh_status()
                return

        block = self._real_auto_run_block_reason()
        if block:
            messagebox.showwarning("自动运行已锁定 / Auto Run Locked", block)
            self.log(block)
            self.refresh_status()
            return

        destination = self._trade_destination_label()
        if not messagebox.askyesno(
            "启动自动运行 / Start Auto Run",
            "这会启动守护监控，它会在主要交易时段按不固定间隔检查程序健康状态，异常时自动重启交易引擎，并自动向 "
            f"{destination} 提交订单。\n确定启动吗？\n\nThis starts the watchdog. It checks health at irregular intervals during core market hours, repairs failures, and auto-submits orders to {destination}.\nStart now?",
        ):
            return

        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        if WATCHDOG_LAUNCH_AGENT_PLIST.exists():
            subprocess.run(["launchctl", "load", str(WATCHDOG_LAUNCH_AGENT_PLIST)], check=False)
            self.log("已通过开机守护服务启动自动运行 / Started auto run through the login watchdog service.")
        else:
            with WATCHDOG_LOG_FILE.open("a", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    _watchdog_program_arguments(),
                    cwd=REPO_ROOT,
                    env=build_env(),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    text=True,
                )
            self.log(f"已启动自动运行守护 / Started auto-run watchdog with pid {process.pid}.")
        self.root.after(1000, self.refresh_status)

    def stop_auto_fusion(self) -> None:
        subprocess.run(["launchctl", "unload", str(WATCHDOG_LAUNCH_AGENT_PLIST)], check=False)

        stop_messages: list[str] = []

        if WATCHDOG_PID_FILE.exists():
            try:
                pid = int(WATCHDOG_PID_FILE.read_text(encoding="utf-8").strip())
            except ValueError:
                pid = 0
            if pid and is_pid_running(pid):
                os.kill(pid, signal.SIGTERM)
                stop_messages.append(f"watchdog pid {pid}")

        if AUTO_TRADER_PID_FILE.exists():
            try:
                pid = int(AUTO_TRADER_PID_FILE.read_text(encoding="utf-8").strip())
            except ValueError:
                pid = 0
            if pid and is_pid_running(pid):
                os.kill(pid, signal.SIGTERM)
                stop_messages.append(f"auto trader pid {pid}")

        if stop_messages:
            self.log(f"已发送停止信号 / Sent stop signal to {' and '.join(stop_messages)}.")
        else:
            self.log("自动运行和守护监控都未运行 / Auto run and watchdog are both stopped.")
        self.root.after(1500, self.refresh_status)

    def one_click_start(self) -> None:
        self.open_futu_opend()
        self.start_dashboard()
        self.log("已触发一键启动 / One-click start triggered.")
        self.log('提示: 如需同时启动 Cascade 独立引擎，点 Claude 策略面板里的"启动引擎"按钮。')

    def restart_console_and_dashboard(self) -> None:
        dashboard_port = int(self.dashboard_port.get().strip() or "8501")
        was_dashboard_running = (
            (self.dashboard_process is not None and self.dashboard_process.poll() is None)
            or is_port_open("127.0.0.1", dashboard_port)
        )
        if self.dashboard_process is not None and self.dashboard_process.poll() is None:
            try:
                self.dashboard_process.terminate()
            except OSError:
                pass
            self.log("正在停止当前监控页 / Stopping current dashboard before restart...")

        restart_env = build_env()
        restart_env["TAA_FUTU_RESTART_DASHBOARD"] = "1" if was_dashboard_running else "0"

        restart_command = [
            str(VENV_PYTHON),
            "-m",
            "taa_futu.control_panel",
        ]
        subprocess.Popen(
            restart_command,
            cwd=REPO_ROOT,
            env=restart_env,
            start_new_session=True,
            text=True,
        )
        self.log("正在重启控制台 / Restarting control panel...")
        self.root.after(200, self.root.destroy)

    def _current_settings(self):
        return load_settings(REPO_ROOT / ".env")

    def _trade_destination_label(self) -> str:
        return "富途真实盘 / Futu REAL" if self._current_settings().futu_trd_env == "REAL" else "富途模拟盘 / Futu SIMULATE"

    def _real_submit_block_reason(self) -> str | None:
        settings = self._current_settings()
        if settings.futu_trd_env != "REAL":
            return None
        if not settings.futu_enable_real_trading:
            return "当前环境已切到 REAL，但 FUTU_ENABLE_REAL_TRADING=false，真实下单仍被锁定。"
        if not settings.futu_unlock_trade_password_md5:
            return "当前环境已切到 REAL，但缺少 FUTU_UNLOCK_TRADE_PASSWORD_MD5，无法完成交易解锁。"
        return None

    def _real_auto_run_block_reason(self) -> str | None:
        block = self._real_submit_block_reason()
        if block:
            return block
        settings = self._current_settings()
        if settings.futu_trd_env == "REAL" and not settings.futu_allow_auto_real:
            return "当前环境已切到 REAL，但 FUTU_ALLOW_AUTO_REAL=false，真实自动交易仍被锁定。"
        return None

    def _confirm_submit(self, strategy_name: str) -> bool:
        block = self._real_submit_block_reason()
        if block:
            messagebox.showwarning("真实交易已锁定 / REAL Trading Locked", block)
            self.log(block)
            return False
        settings = self._current_settings()
        destination = self._trade_destination_label()
        if settings.futu_trd_env == "REAL":
            if not messagebox.askyesno(
                "第一次确认 / First Confirmation",
                f"{strategy_name} 将向 {destination} 下真实订单。\n这不是模拟交易。\n继续吗？\n\n{strategy_name} will submit LIVE orders to {destination}.\nThis is not paper trading.\nContinue?",
            ):
                return False
            if not messagebox.askyesno(
                "第二次确认 / Second Confirmation",
                "请再次确认：你已经核对过账户、标的、数量和时间窗口。\n继续提交真实订单吗？\n\nPlease confirm again that account, symbol, size, and timing were checked.\nSubmit LIVE orders?",
            ):
                return False
            typed = simpledialog.askstring(
                "最终确认 / Final Confirmation",
                "请输入 REAL 作为最终确认。\n\nType REAL to confirm LIVE order submission.",
                parent=self.root,
            )
            if typed != "REAL":
                self.log("已取消真实订单提交 / LIVE order submission cancelled.")
                return False
        return messagebox.askyesno(
            "确认提交 / Confirm Submit",
            f"{strategy_name} 将向 {destination} 发送订单。\n确定继续吗？\n\n{strategy_name} will send orders to {destination}.\nContinue?",
        )

    def _confirm_standalone_override(self, strategy_name: str) -> bool:
        settings = self._current_settings()
        try:
            baseline_weight, fusion_weight, cascade_weight, reserve_weight = stack_allocations(settings)
        except ValueError:
            return True
        stack_active = settings.stack_baseline_enabled or reserve_weight > 0 or fusion_weight < 0.999 or cascade_weight > 0.001 or baseline_weight > 0.001
        if not stack_active:
            return True
        return messagebox.askyesno(
            "独立策略会覆盖组合 / Standalone Override Warning",
            f"当前自动盘按组合运行：{stack_label(settings)}。\n"
            f"如果你现在单独提交 {strategy_name}，会绕过组合分仓器，直接改整个账户。\n继续吗？\n\n"
            f"Current auto stack is {stack_label(settings)}.\n"
            f"Submitting standalone {strategy_name} bypasses the sleeve allocator and can overwrite whole-account targets.\nContinue?",
        )

    def check_real_readiness(self) -> None:
        self._run_command_async("real-check", self._python_cli("real-check"))

    def run_backtest(self) -> None:
        strategy = self.backtest_strategy.get().strip() or "baseline"
        try:
            start_date = datetime.fromisoformat(self.start_date.get().strip()).date()
            end_date = datetime.fromisoformat(self.end_date.get().strip()).date()
        except ValueError:
            start_date = None
            end_date = None
        if strategy == "fusion" and start_date and end_date:
            span_days = (end_date - start_date).days + 1
            self.log(
                f"Fusion 日内回放会抓分钟级数据。当前区间 {span_days} 天，可能需要较长时间；下面日志会逐只股票显示进度。"
            )
        if strategy == "cascade" and start_date and end_date:
            span_days = (end_date - start_date).days + 1
            self.log(
                f"Claude/Cascade 回放会抓日线数据。当前区间 {span_days} 天，日志会按标的显示进度。"
            )
        if strategy == "stack" and start_date and end_date:
            span_days = (end_date - start_date).days + 1
            self.log(
                f"组合回测会同时抓 Baseline 日线、Fusion 分钟线和 Claude/Cascade 日线。当前区间 {span_days} 天，日志会按组件显示进度。"
            )
        self._run_command_async(
            "backtest",
            self._python_cli(
                "backtest",
                "--strategy",
                strategy,
                "--start",
                self.start_date.get().strip(),
                "--end",
                self.end_date.get().strip(),
            ),
        )

    def run_signals(self) -> None:
        self._run_command_async(
            "signals",
            self._python_cli("signals", "--start", self.start_date.get().strip(), "--end", self.end_date.get().strip()),
        )

    def run_manual_strategy(self) -> None:
        strategy = self.manual_strategy.get().strip() or "fusion"
        if strategy == "baseline":
            self.run_paper_trade()
            return
        if strategy == "cascade":
            self.run_cascade()
            return
        self.run_fusion()

    def submit_manual_strategy(self) -> None:
        strategy = self.manual_strategy.get().strip() or "fusion"
        if strategy == "baseline":
            self.submit_paper_trade()
            return
        if strategy == "cascade":
            self.submit_cascade()
            return
        self.submit_fusion()

    def run_paper_trade(self) -> None:
        self._run_command_async("paper-trade", self._python_cli("paper-trade"))

    def submit_paper_trade(self) -> None:
        if not self._confirm_standalone_override("Baseline Strategy"):
            return
        if not self._confirm_submit("Baseline Strategy"):
            return
        self._run_command_async("paper-trade-submit", self._python_cli("paper-trade", "--submit"))

    def run_fusion(self) -> None:
        self._run_command_async("fusion-intraday", self._python_cli("fusion-intraday"))

    def submit_fusion(self) -> None:
        if not self._confirm_standalone_override("Fusion Intraday"):
            return
        if not self._confirm_submit("Fusion Intraday"):
            return
        self._run_command_async("fusion-intraday-submit", self._python_cli("fusion-intraday", "--submit"))

    def run_cascade(self) -> None:
        self._run_command_async("cascade-strategy", self._python_cli("cascade-strategy"))

    def submit_cascade(self) -> None:
        if not self._confirm_standalone_override("Claude/Cascade"):
            return
        if not self._confirm_submit("Claude/Cascade"):
            return
        self._run_command_async("cascade-strategy-submit", self._python_cli("cascade-strategy", "--submit"))

    # ── Claude-Trade standalone engine ────────────────────────────────────────

    def ct_start_engine_dryrun(self) -> None:
        if _ct_engine_running():
            self.log(f"Cascade 引擎已在运行 / Engine already running (pid={_ct_engine_pid()}).")
            self.refresh_status()
            return
        if not CT_VENV_PYTHON.exists():
            messagebox.showerror("Cascade 引擎", f"找不到 claude-trade 的 Python 环境:\n{CT_VENV_PYTHON}\n请先在 claude-trade 目录安装依赖。")
            return
        CT_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with CT_LOG_FILE.open("a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                _ct_engine_args(dry_run=True),
                cwd=CT_REPO_ROOT,
                env=_ct_build_env(),
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
        self.log(f"已启动 Cascade 引擎 (试运行/dry-run) pid={proc.pid}. 日志: {CT_LOG_FILE}")
        self.root.after(1200, self.refresh_status)

    def ct_start_engine_live(self) -> None:
        if _ct_engine_running():
            self.log(f"Cascade 引擎已在运行 / Engine already running (pid={_ct_engine_pid()}).")
            self.refresh_status()
            return
        if not CT_VENV_PYTHON.exists():
            messagebox.showerror("Cascade 引擎", f"找不到 claude-trade 的 Python 环境:\n{CT_VENV_PYTHON}")
            return
        if not messagebox.askyesno(
            "启动 Cascade 引擎 (实盘) / Start Engine Live",
            "这将启动 claude-trade 独立引擎，连接真实或模拟富途账户。\n确定启动吗？\n\nThis starts the claude-trade engine against your Futu account.\nStart now?",
        ):
            return
        CT_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with CT_LOG_FILE.open("a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                _ct_engine_args(dry_run=False),
                cwd=CT_REPO_ROOT,
                env=_ct_build_env(),
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
        self.log(f"已启动 Cascade 引擎 (实盘/live) pid={proc.pid}. 日志: {CT_LOG_FILE}")
        self.root.after(1200, self.refresh_status)

    def ct_stop_engine(self) -> None:
        pid = _ct_engine_pid()
        if pid and is_pid_running(pid):
            os.kill(pid, signal.SIGTERM)
            self.log(f"已向 Cascade 引擎 pid={pid} 发送停止信号 / Sent SIGTERM to CT engine.")
        else:
            self.log("Cascade 引擎未运行 / CT engine is not running.")
        self.root.after(1200, self.refresh_status)

    def ct_open_dashboard(self) -> None:
        if not CT_VENV_PYTHON.exists():
            messagebox.showerror("Cascade 监控页", f"找不到 claude-trade 的 Python 环境:\n{CT_VENV_PYTHON}")
            return
        if is_port_open("127.0.0.1", CT_DASHBOARD_PORT):
            webbrowser.open(f"http://localhost:{CT_DASHBOARD_PORT}")
            self.log(f"已打开 Cascade 监控页 / Opened CT dashboard at http://localhost:{CT_DASHBOARD_PORT}")
            return
        cmd = [str(CT_VENV_PYTHON), "-m", "claude_trade.cli", "dashboard", "--port", str(CT_DASHBOARD_PORT)]
        subprocess.Popen(
            cmd,
            cwd=CT_REPO_ROOT,
            env=_ct_build_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.log(f"正在启动 Cascade 监控页 / Starting CT dashboard on port {CT_DASHBOARD_PORT}…")
        self.root.after(2500, lambda: webbrowser.open(f"http://localhost:{CT_DASHBOARD_PORT}"))

    def on_close(self) -> None:
        if self._status_refresh_after_id is not None:
            try:
                self.root.after_cancel(self._status_refresh_after_id)
            except ValueError:
                pass
            self._status_refresh_after_id = None
        if self.dashboard_process is not None and self.dashboard_process.poll() is None:
            if messagebox.askyesno("退出 / Exit", "监控页仍在运行，是否先停止再退出？\nDashboard is still running. Stop it and exit?"):
                self.stop_dashboard()
                self.root.after(300, self.root.destroy)
                return
            return
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if not VENV_PYTHON.exists():
        raise SystemExit(f"缺少 Python 环境 / Missing Python environment: {VENV_PYTHON}")
    ControlPanel().run()


if __name__ == "__main__":
    main()
