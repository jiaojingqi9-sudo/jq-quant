#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pandas as pd

from coinmarketcap_recorder import (
    CoinMarketCapError,
    CoinMarketCapSnapshotConfig,
    append_coinmarketcap_snapshot,
    fetch_coinmarketcap_quotes,
    parse_crypto_symbols,
)
from futu_us_breakout_screener import (
    ScreenerError,
    download_yfinance_ohlcv,
    ensure_opend_reachable,
    evaluate_history_detailed,
    evaluate_recent_weekly_high_candidate,
    fetch_industry_map,
    fetch_quick_candidates,
    liquidity_reject_reason,
)


CACHE_PATH = Path(__file__).resolve().parents[1] / "runtime" / "futu_stock_screener_cache.json"
CMC_RECORD_PATH = Path(__file__).resolve().parents[1] / "runtime" / "coinmarketcap_market_records.csv"

RESULT_COLUMNS = [
    "source",
    "code",
    "name",
    "industry",
    "last_date",
    "open",
    "close",
    "prev_close",
    "change_rate",
    "latest_high",
    "previous_record",
    "record_distance_pct",
    "high_to_52w_high_pct",
    "cur_to_52w_high_pct",
    "volume",
    "prev_volume",
    "volume_ratio",
    "streak_days",
]

DISPLAY_COLUMNS = [
    ("source", "模式", 86),
    ("code", "代码", 96),
    ("name", "名称", 150),
    ("industry", "行业", 120),
    ("last_date", "日期", 96),
    ("open", "开盘", 86),
    ("close", "收盘", 86),
    ("prev_close", "前收", 86),
    ("change_rate", "涨跌%", 78),
    ("latest_high", "最新高点", 96),
    ("previous_record", "前高纪录", 96),
    ("record_distance_pct", "距历史高点%", 104),
    ("high_to_52w_high_pct", "距52周高%", 96),
    ("cur_to_52w_high_pct", "收盘距52周高%", 120),
    ("volume", "成交量", 110),
    ("prev_volume", "前量", 110),
    ("volume_ratio", "量比", 70),
    ("streak_days", "连涨", 64),
]


class StockScreenerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Futu US Screener")
        self.geometry("1320x900")
        self.minsize(1120, 780)

        self.queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.result = pd.DataFrame(columns=RESULT_COLUMNS)
        self.view_result = self.result.copy()
        self.skip_messages: list[str] = []
        self.sort_column: str | None = None
        self.sort_descending = False
        self.active_targets: list[tuple[str, str]] | None = None
        self.current_scope: str | None = None
        self.previous_result_before_scan = pd.DataFrame(columns=RESULT_COLUMNS)
        self.previous_active_targets_before_scan: list[tuple[str, str]] | None = None

        self._configure_theme()
        self._build_variables()
        self._build_layout()
        self._load_cache()
        self._poll_queue()

    def _configure_theme(self) -> None:
        style = ttk.Style(self)
        available = set(style.theme_names())
        if "aqua" in available:
            style.theme_use("aqua")
        style.configure("Title.TLabel", font=("Helvetica Neue", 21, "bold"))
        style.configure("Subtitle.TLabel", foreground="#5f6b7a")
        style.configure("Section.TLabel", font=("Helvetica Neue", 13, "bold"))
        style.configure("Metric.TLabel", font=("Helvetica Neue", 18, "bold"))
        style.configure("Muted.TLabel", foreground="#667085")
        style.configure("Primary.TButton", padding=(14, 8))
        style.configure("Sidebar.TFrame", background="#f7f9fc")

    def _build_variables(self) -> None:
        self.host_var = tk.StringVar(value="127.0.0.1")
        self.port_var = tk.IntVar(value=11111)
        self.monthly_prefilter_var = tk.BooleanVar(value=True)
        self.monthly_near_high_pct_var = tk.DoubleVar(value=30.0)
        self.verify_history_var = tk.BooleanVar(value=True)
        self.up_days_var = tk.IntVar(value=14)
        self.new_high_var = tk.StringVar(value="high")
        self.near_high_pct_var = tk.DoubleVar(value=15.0)
        self.volume_mode_var = tk.StringVar(value="lower")
        self.start_var = tk.StringVar(value=(date.today() - timedelta(days=370)).isoformat())
        self.end_enabled_var = tk.BooleanVar(value=False)
        self.end_var = tk.StringVar(value=date.today().isoformat())
        self.sleep_var = tk.DoubleVar(value=0.8)
        self.crypto_record_var = tk.BooleanVar(value=False)
        self.cmc_api_key_var = tk.StringVar(
            value=os.getenv("COINMARKETCAP_API_KEY") or os.getenv("CMC_API_KEY") or ""
        )
        self.crypto_symbols_var = tk.StringVar(value="BTC, ETH, SOL, BNB, XRP")
        self.crypto_quote_var = tk.StringVar(value="USD")
        self.end_enabled_var.trace_add("write", lambda *_args: self._refresh_control_states())
        self.verify_history_var.trace_add("write", lambda *_args: self._refresh_control_states())
        self.monthly_prefilter_var.trace_add("write", lambda *_args: self._refresh_control_states())
        self.crypto_record_var.trace_add("write", lambda *_args: self._refresh_control_states())

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        sidebar = self._build_scrollable_sidebar()
        content = ttk.Frame(self, padding=(20, 18, 20, 18))
        content.grid(row=0, column=1, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(3, weight=1)

        self._build_sidebar(sidebar)
        self._build_content(content)
        self._refresh_control_states()

    def _build_scrollable_sidebar(self) -> ttk.Frame:
        shell = ttk.Frame(self, style="Sidebar.TFrame")
        shell.grid(row=0, column=0, sticky="nsw")
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)

        canvas = tk.Canvas(shell, width=380, highlightthickness=0, borderwidth=0, background="#f7f9fc")
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsw")
        scrollbar.grid(row=0, column=1, sticky="ns")

        sidebar = ttk.Frame(canvas, style="Sidebar.TFrame", padding=(18, 18, 18, 18))
        sidebar.columnconfigure(0, weight=1)
        window_id = canvas.create_window((0, 0), window=sidebar, anchor="nw")

        def refresh_scrollregion(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def resize_inner(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        sidebar.bind("<Configure>", refresh_scrollregion)
        canvas.bind("<Configure>", resize_inner)
        canvas.bind("<Enter>", lambda _event: self._bind_sidebar_mousewheel(canvas))
        canvas.bind("<Leave>", lambda _event: self._unbind_sidebar_mousewheel())
        return sidebar

    def _bind_sidebar_mousewheel(self, canvas: tk.Canvas) -> None:
        self.sidebar_canvas = canvas
        self.bind_all("<MouseWheel>", self._on_sidebar_mousewheel)

    def _unbind_sidebar_mousewheel(self) -> None:
        self.unbind_all("<MouseWheel>")

    def _on_sidebar_mousewheel(self, event) -> None:
        canvas = getattr(self, "sidebar_canvas", None)
        if canvas is None:
            return
        direction = -1 if event.delta > 0 else 1
        canvas.yview_scroll(direction * 3, "units")

    def _build_sidebar(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Futu US Screener", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(parent, text="全市场可查美股 · 连续阳线 · 逼近高点", style="Subtitle.TLabel").grid(
            row=1, column=0, sticky="w", pady=(0, 16)
        )

        row = 2
        row = self._section(parent, row, "连接")
        self._labeled_entry(parent, row, "Host", self.host_var)
        row += 1
        self._labeled_entry(parent, row, "Port", self.port_var)
        row += 1
        ttk.Button(parent, text="测试连接", command=self._test_connection).grid(row=row, column=0, sticky="ew", pady=(4, 16))
        row += 1

        row = self._section(parent, row, "股票池")
        ttk.Label(
            parent,
            text="首次从富途美股 STOCK 条件选股返回候选；筛完后的结果会作为下一轮股票池，点“重置股票池”回到全市场。",
            style="Muted.TLabel",
            wraplength=340,
        ).grid(row=row, column=0, sticky="w", pady=(0, 16))
        row += 1

        row = self._section(parent, row, "低频 K 预筛")
        self.monthly_prefilter_check = ttk.Checkbutton(
            parent,
            text="启用长期高点 + 最近 3 周预筛",
            variable=self.monthly_prefilter_var,
            command=self._refresh_control_states,
        )
        self.monthly_prefilter_check.grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1
        self._labeled_entry(parent, row, "预筛高点容忍%", self.monthly_near_high_pct_var)
        self.monthly_near_high_entry = self._last_entry
        row += 1
        ttk.Label(
            parent,
            text="长期月 K 只算历史高点；最近 3 周周 K 判断离高点多近，不判断连续阳线。默认 30% 用来放宽入口。",
            style="Muted.TLabel",
            wraplength=340,
        ).grid(row=row, column=0, sticky="w", pady=(0, 12))
        row += 1

        row = self._section(parent, row, "条件")
        self.verify_history_check = ttk.Checkbutton(
            parent,
            text="用日 K 精确验证",
            variable=self.verify_history_var,
            command=self._refresh_control_states,
        )
        self.verify_history_check.grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1
        self._labeled_entry(parent, row, "连续阳线天数", self.up_days_var)
        self.up_days_entry = self._last_entry
        row += 1
        volume_box = ttk.Frame(parent)
        volume_box.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(volume_box, text="成交量条件").pack(side="left", padx=(0, 10))
        self.volume_buttons = [
            ttk.Radiobutton(volume_box, text="缩量", variable=self.volume_mode_var, value="lower"),
            ttk.Radiobutton(volume_box, text="放量", variable=self.volume_mode_var, value="higher"),
            ttk.Radiobutton(volume_box, text="不限制", variable=self.volume_mode_var, value="none"),
        ]
        self.volume_buttons[0].pack(side="left")
        self.volume_buttons[1].pack(side="left", padx=(12, 0))
        self.volume_buttons[2].pack(side="left", padx=(12, 0))
        row += 1
        new_high_box = ttk.Frame(parent)
        new_high_box.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        ttk.Radiobutton(new_high_box, text="盘中高点口径", variable=self.new_high_var, value="high").pack(side="left")
        ttk.Radiobutton(new_high_box, text="收盘价口径", variable=self.new_high_var, value="close").pack(side="left", padx=(16, 0))
        row += 1
        self._labeled_entry(parent, row, "距历史高点容忍%", self.near_high_pct_var)
        row += 1
        ttk.Label(
            parent,
            text="15 = 最新高点/收盘价距离历史高点不超过 15%；0 = 必须创历史新高。",
            style="Muted.TLabel",
            wraplength=340,
        ).grid(row=row, column=0, sticky="w", pady=(0, 12))
        row += 1

        row = self._section(parent, row, "精确验证日 K")
        self._labeled_entry(parent, row, "日 K 开始日期", self.start_var)
        self.start_entry = self._last_entry
        row += 1
        self.end_check = ttk.Checkbutton(
            parent,
            text="指定结束日期",
            variable=self.end_enabled_var,
            command=self._refresh_control_states,
        )
        self.end_check.grid(row=row, column=0, sticky="w")
        row += 1
        self._labeled_entry(parent, row, "结束日期", self.end_var)
        self.end_entry = self._last_entry
        row += 1
        self._labeled_entry(parent, row, "批次间隔秒", self.sleep_var)
        self.sleep_entry = self._last_entry
        row += 1
        ttk.Label(
            parent,
            text="日 K 负责有效成交过滤、连续阳线、缩/放量；长期历史高点由低频 K 预筛记录提供。",
            style="Muted.TLabel",
            wraplength=340,
        ).grid(row=row, column=0, sticky="w", pady=(0, 12))
        row += 1

        row = self._section(parent, row, "加密数据记录")
        self.crypto_record_check = ttk.Checkbutton(
            parent,
            text="筛选时记录 CoinMarketCap 快照",
            variable=self.crypto_record_var,
            command=self._refresh_control_states,
        )
        self.crypto_record_check.grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1
        self._labeled_entry(parent, row, "CMC API Key", self.cmc_api_key_var, show="*")
        self.cmc_key_entry = self._last_entry
        row += 1
        self._labeled_entry(parent, row, "币种", self.crypto_symbols_var)
        self.crypto_symbols_entry = self._last_entry
        row += 1
        self._labeled_entry(parent, row, "计价", self.crypto_quote_var)
        self.crypto_quote_entry = self._last_entry
        row += 1
        ttk.Button(parent, text="测试 CMC", command=self._test_cmc_connection).grid(
            row=row, column=0, sticky="ew", pady=(0, 8)
        )
        row += 1
        ttk.Label(
            parent,
            text=f"筛选开始/结束各记录一次快照，追加到 {CMC_RECORD_PATH.name}；API Key 不写入缓存。",
            style="Muted.TLabel",
            wraplength=340,
        ).grid(row=row, column=0, sticky="w", pady=(0, 12))
        row += 1

        action_box = ttk.Frame(parent)
        action_box.grid(row=row, column=0, sticky="ew", pady=(14, 0))
        action_box.columnconfigure(0, weight=1)
        action_box.columnconfigure(1, weight=1)
        self.run_button = ttk.Button(action_box, text="运行筛选", style="Primary.TButton", command=self._start_scan)
        self.run_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.stop_button = ttk.Button(action_box, text="停止", command=self._stop_scan, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _build_content(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="选股结果", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="重置股票池", command=self._reset_pool).grid(row=0, column=1, sticky="e", padx=(0, 10))
        ttk.Button(header, text="恢复原始顺序", command=self._reset_sort).grid(row=0, column=2, sticky="e", padx=(0, 10))
        ttk.Button(header, text="保存 CSV", command=self._save_csv).grid(row=0, column=3, sticky="e")

        metrics = ttk.Frame(parent)
        metrics.grid(row=1, column=0, sticky="ew", pady=(16, 12))
        for col in range(4):
            metrics.columnconfigure(col, weight=1)
        self.hit_metric = self._metric(metrics, 0, "命中", "0")
        self.pool_metric = self._metric(metrics, 1, "预筛候选", "0")
        self.scan_metric = self._metric(metrics, 2, "本次扫描", "0")
        self.skip_metric = self._metric(metrics, 3, "跳过", "0")

        self.progress = ttk.Progressbar(parent, mode="determinate", maximum=100)
        self.progress.grid(row=2, column=0, sticky="ew", pady=(0, 10))

        table_frame = ttk.Frame(parent)
        table_frame.grid(row=3, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(table_frame, columns=[key for key, _title, _width in DISPLAY_COLUMNS], show="headings")
        for key, title, width in DISPLAY_COLUMNS:
            self.tree.heading(key, text=title, command=lambda column=key: self._sort_by_column(column))
            left_columns = {"source", "code", "name", "industry", "last_date"}
            self.tree.column(
                key,
                width=width,
                minwidth=56,
                stretch=key in {"name", "industry"},
                anchor="w" if key in left_columns else "e",
            )
        self.tree.grid(row=0, column=0, sticky="nsew")

        y_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        y_scrollbar.grid(row=0, column=1, sticky="ns")
        x_scrollbar = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        x_scrollbar.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=y_scrollbar.set, xscrollcommand=x_scrollbar.set)

        bottom = ttk.Frame(parent)
        bottom.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        bottom.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="准备就绪")
        ttk.Label(bottom, textvariable=self.status_var, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(bottom, text="查看跳过记录", command=self._show_skips).grid(row=0, column=1, sticky="e")

    def _section(self, parent: ttk.Frame, row: int, title: str) -> int:
        ttk.Label(parent, text=title, style="Section.TLabel").grid(row=row, column=0, sticky="w", pady=(8, 8))
        return row + 1

    def _labeled_entry(self, parent: ttk.Frame, row: int, label: str, variable: tk.Variable, *, show: str | None = None) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=label).grid(row=0, column=0, sticky="w", padx=(0, 10))
        entry = ttk.Entry(frame, textvariable=variable, width=18, show=show)
        entry.grid(row=0, column=1, sticky="ew")
        self._last_entry = entry

    def _metric(self, parent: ttk.Frame, column: int, label: str, value: str) -> tk.StringVar:
        frame = ttk.Frame(parent, padding=(12, 10, 12, 10), relief="solid")
        frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
        ttk.Label(frame, text=label, style="Muted.TLabel").pack(anchor="w")
        var = tk.StringVar(value=value)
        ttk.Label(frame, textvariable=var, style="Metric.TLabel").pack(anchor="w", pady=(4, 0))
        return var

    def _refresh_control_states(self) -> None:
        verify_history = self.verify_history_var.get()
        monthly_prefilter = self.monthly_prefilter_var.get()
        crypto_record = self.crypto_record_var.get()
        self.monthly_near_high_entry.configure(state="normal" if monthly_prefilter else "disabled")
        self.up_days_entry.configure(state="normal" if verify_history else "disabled")
        self.start_entry.configure(state="normal" if verify_history else "disabled")
        self.end_check.configure(state="normal" if verify_history else "disabled")
        self.end_entry.configure(state="normal" if verify_history and self.end_enabled_var.get() else "disabled")
        self.sleep_entry.configure(state="normal" if verify_history else "disabled")
        for button in getattr(self, "volume_buttons", []):
            button.configure(state="normal" if verify_history else "disabled")
        for entry in (getattr(self, "cmc_key_entry", None), getattr(self, "crypto_symbols_entry", None), getattr(self, "crypto_quote_entry", None)):
            if entry is not None:
                entry.configure(state="normal" if crypto_record else "disabled")

    def _test_connection(self) -> None:
        try:
            ensure_opend_reachable(self.host_var.get().strip(), int(self.port_var.get()))
        except Exception as exc:
            messagebox.showerror("连接失败", str(exc))
            return
        messagebox.showinfo("连接成功", "OpenD 已连接。")

    def _test_cmc_connection(self) -> None:
        try:
            api_key = self._read_cmc_api_key()
            symbols = parse_crypto_symbols(self.crypto_symbols_var.get())
            quote = self.crypto_quote_var.get().strip().upper() or "USD"
            payload = fetch_coinmarketcap_quotes(api_key, symbols, convert=quote, timeout_seconds=15)
            count = len(payload.get("data") or {})
        except Exception as exc:
            messagebox.showerror("CMC 连接失败", str(exc))
            return
        messagebox.showinfo("CMC 连接成功", f"已获取 {count} 个币种的 {quote} 快照。")

    def _start_scan(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            controls = self._read_controls()
            targets, scope = self._prepare_targets(controls)
        except Exception as exc:
            messagebox.showerror("无法开始", str(exc))
            return
        if scope == "active" and not controls["verify_history"]:
            messagebox.showinfo("需要日 K", "结果内再筛需要打开“用日 K 精确验证”。")
            return
        if not targets:
            messagebox.showinfo("当前股票池为空", "当前结果里没有可继续筛选的股票。点“重置股票池”后可重新从全市场筛。")
            return

        self.cancel_event.clear()
        self.current_scope = scope
        self.previous_result_before_scan = self.result.copy()
        self.previous_active_targets_before_scan = list(self.active_targets) if self.active_targets is not None else None
        self.result = pd.DataFrame(columns=RESULT_COLUMNS)
        self.view_result = self.result.copy()
        self.sort_column = None
        self.sort_descending = False
        self.skip_messages = []
        self._clear_tree()
        self._set_running(True)
        self.hit_metric.set("0")
        self.pool_metric.set("当前结果" if scope == "active" else "全市场")
        self.scan_metric.set("预筛中" if scope == "market" else str(len(targets)))
        self.skip_metric.set("0")
        self.progress.configure(value=0)
        self.status_var.set("开始筛选当前结果" if scope == "active" else "开始全市场筛选")

        self.worker = threading.Thread(target=self._scan_worker, args=(controls, targets, scope), daemon=True)
        self.worker.start()

    def _read_controls(self) -> dict:
        host = self.host_var.get().strip()
        port = int(self.port_var.get())
        start = self.start_var.get().strip()
        date.fromisoformat(start)
        end = self.end_var.get().strip() if self.end_enabled_var.get() else None
        if end:
            date.fromisoformat(end)
        up_days = int(self.up_days_var.get())
        if up_days < 1:
            raise ValueError("连续阳线天数必须至少为 1。")
        monthly_near_high_pct = float(self.monthly_near_high_pct_var.get())
        if monthly_near_high_pct < 0:
            raise ValueError("预筛高点容忍%不能为负数。")
        near_high_pct = float(self.near_high_pct_var.get())
        if near_high_pct < 0:
            raise ValueError("距历史高点容忍%不能为负数。")
        sleep_seconds = float(self.sleep_var.get())
        if sleep_seconds < 0:
            raise ValueError("批次间隔不能为负数。")
        crypto_symbols = parse_crypto_symbols(self.crypto_symbols_var.get())
        crypto_quote = self.crypto_quote_var.get().strip().upper() or "USD"
        return {
            "host": host,
            "port": port,
            "monthly_prefilter": bool(self.monthly_prefilter_var.get()),
            "monthly_near_high_pct": monthly_near_high_pct,
            "verify_history": bool(self.verify_history_var.get()),
            "up_days": up_days,
            "new_high_on": self.new_high_var.get(),
            "near_high_pct": near_high_pct,
            "volume_mode": self.volume_mode_var.get(),
            "start": start,
            "end": end,
            "sleep": sleep_seconds,
            "max_count": 1000,
            "crypto_record": bool(self.crypto_record_var.get()),
            "cmc_api_key": self._read_cmc_api_key(),
            "crypto_symbols": crypto_symbols,
            "crypto_quote": crypto_quote,
            "crypto_record_path": CMC_RECORD_PATH,
        }

    def _read_cmc_api_key(self) -> str:
        return (
            self.cmc_api_key_var.get().strip()
            or os.getenv("COINMARKETCAP_API_KEY", "").strip()
            or os.getenv("CMC_API_KEY", "").strip()
        )

    def _prepare_targets(self, controls: dict) -> tuple[list[tuple[str, str]], str]:
        ensure_opend_reachable(controls["host"], controls["port"])
        if self.active_targets is not None:
            return list(self.active_targets), "active"
        return [("US_MARKET", "全市场")], "market"

    def _scan_worker(self, controls: dict, targets: list[tuple[str, str]], scope: str) -> None:
        if scope == "active":
            self._scan_exact_worker(controls, targets, source_label="结果内再筛")
        else:
            self._scan_market_worker(controls)

    def _scan_exact_worker(self, controls: dict, targets: list[tuple[str, str]], source_label: str = "精确验证") -> None:
        import futu

        messages: list[str] = []
        rows: list[dict] = []
        quote_ctx = futu.OpenQuoteContext(host=controls["host"], port=controls["port"])
        try:
            rows, messages = self._scan_yfinance_targets(controls, targets, quote_ctx, futu, source_label=source_label)
        except Exception as exc:
            self.queue.put(("error", str(exc)))
        finally:
            quote_ctx.close()
            self.queue.put(("done", (self._finalize_result(rows), messages)))

    def _scan_market_worker(self, controls: dict) -> None:
        import futu

        rows: list[dict] = []
        messages: list[str] = []
        quote_ctx = futu.OpenQuoteContext(host=controls["host"], port=controls["port"])
        try:
            mode = "富途候选池"
            self.queue.put(("status", f"{mode}：调用富途条件选股，不拉富途历史 K"))

            def quick_progress(begin: int, all_count: int, page_count: int) -> None:
                done = min(begin + page_count, all_count) if all_count else begin + page_count
                self.queue.put(("status", f"{mode}返回 {done}/{all_count or '?'}"))

            quick_near_high_pct = controls["near_high_pct"]
            if controls["monthly_prefilter"]:
                quick_near_high_pct = max(quick_near_high_pct, controls["monthly_near_high_pct"])
            candidates, all_count = fetch_quick_candidates(
                quote_ctx,
                futu,
                near_high_pct=quick_near_high_pct,
                new_high_on=controls["new_high_on"],
                limit=0,
                page_size=200,
                require_up_day=False,
                require_lower_volume=False,
                skip_special_filter=False,
                progress=quick_progress,
            )
            self.queue.put(("pool_size", len(candidates)))
            targets = [(candidate.code, candidate.name) for candidate in candidates]
            rows, messages = self._scan_yfinance_targets(controls, targets, quote_ctx, futu, source_label="日K精筛")
        except Exception as exc:
            self.queue.put(("error", str(exc)))
        finally:
            quote_ctx.close()
            self.queue.put(("done", (self._finalize_result(rows), messages)))

    def _scan_yfinance_targets(
        self,
        controls: dict,
        targets: list[tuple[str, str]],
        quote_ctx,
        futu,
        *,
        source_label: str,
    ) -> tuple[list[dict], list[str]]:
        rows: list[dict] = []
        messages: list[str] = []
        if not targets:
            return rows, messages

        self.queue.put(("scan_total", len(targets)))
        target_names = {code: name for code, name in targets}
        monthly_candidates: dict[str, object | None] = {}
        funnel_counts: dict[str, int] = {
            "股票池": len(targets),
            "长期K下载失败": 0,
            "周K下载失败": 0,
            "周K高位淘汰": 0,
            "低频K通过": 0,
            "日K下载失败": 0,
            "流动性淘汰": 0,
            "日K验证": 0,
            "最终命中": 0,
        }
        self._record_crypto_snapshot(controls, "scan_start", messages)

        def finish_scan() -> tuple[list[dict], list[str]]:
            self._record_crypto_snapshot(controls, "scan_end", messages)
            self._append_funnel_summary(messages, funnel_counts)
            return rows, messages

        if controls["monthly_prefilter"]:
            monthly_threshold = max(controls["monthly_near_high_pct"], controls["near_high_pct"])

            def monthly_progress(done: int, total: int) -> None:
                self.queue.put(("stage_progress", (done, total, "长期K下载")))
                self.queue.put(("status", f"长期 K：下载 {done}/{total}"))

            monthly_histories = download_yfinance_ohlcv(
                targets,
                period="max",
                interval="1mo",
                batch_size=80,
                batch_sleep_seconds=controls["sleep"],
                progress=monthly_progress,
            )
            long_ready_targets: list[tuple[str, str]] = []
            for code, name in targets:
                if self.cancel_event.is_set():
                    self.queue.put(("status", "已停止"))
                    break
                history = monthly_histories.get(code)
                if history is None or history.empty:
                    funnel_counts["长期K下载失败"] += 1
                    messages.append(f"{code}: yfinance 长期K无数据")
                    continue
                long_ready_targets.append((code, name))

            def weekly_progress(done: int, total: int) -> None:
                self.queue.put(("stage_progress", (done, total, "周K下载")))
                self.queue.put(("status", f"最近 3 周周 K：下载 {done}/{total}"))

            weekly_histories = download_yfinance_ohlcv(
                long_ready_targets,
                period="3mo",
                interval="1wk",
                batch_size=80,
                batch_sleep_seconds=controls["sleep"],
                progress=weekly_progress,
            )
            total = len(long_ready_targets)
            for index, (code, name) in enumerate(long_ready_targets, start=1):
                if self.cancel_event.is_set():
                    self.queue.put(("status", "已停止"))
                    break
                weekly_history = weekly_histories.get(code)
                if weekly_history is None or weekly_history.empty:
                    funnel_counts["周K下载失败"] += 1
                    messages.append(f"{code}: yfinance 周K无数据")
                    continue
                candidate = evaluate_recent_weekly_high_candidate(
                    code,
                    name,
                    monthly_histories[code],
                    weekly_history,
                    near_high_pct=monthly_threshold,
                    new_high_on=controls["new_high_on"],
                    recent_weeks=3,
                )
                if candidate is None:
                    funnel_counts["周K高位淘汰"] += 1
                    continue
                monthly_candidates[code] = candidate
                funnel_counts["低频K通过"] += 1
                self.queue.put(("progress", (index, total, len(rows), len(messages))))
        else:
            monthly_candidates = {code: None for code, _name in targets}
            funnel_counts["低频K通过"] = len(monthly_candidates)

        selected_targets = [(code, target_names.get(code, "")) for code in monthly_candidates.keys()]
        self.queue.put(("scan_total", len(selected_targets)))
        self.queue.put(("status", f"低频 K 预筛通过 {len(selected_targets)}/{len(targets)}"))

        if not controls["verify_history"]:
            industry_map = self._safe_fetch_industries(quote_ctx, futu, [code for code, _name in selected_targets], messages)
            total = len(selected_targets)
            for index, (code, name) in enumerate(selected_targets, start=1):
                if self.cancel_event.is_set():
                    self.queue.put(("status", "已停止"))
                    break
                row = self._row_from_monthly_candidate(monthly_candidates.get(code), code, name, industry_map.get(code, ""))
                rows.append(row)
                self.queue.put(("match", row))
                self.queue.put(("progress", (index, total, len(rows), len(messages))))
            funnel_counts["最终命中"] = len(rows)
            return finish_scan()

        if not selected_targets:
            return finish_scan()

        def daily_progress(done: int, total: int) -> None:
            self.queue.put(("stage_progress", (done, total, "日K下载")))
            self.queue.put(("status", f"日 K 精筛：下载 {done}/{total}"))

        daily_histories = download_yfinance_ohlcv(
            selected_targets,
            period="1y",
            interval="1d",
            start=controls["start"],
            end=controls["end"],
            batch_size=80,
            batch_sleep_seconds=controls["sleep"],
            progress=daily_progress,
        )
        industry_map = self._safe_fetch_industries(quote_ctx, futu, [code for code, _name in selected_targets], messages)
        reject_counts: dict[str, int] = {}
        total = len(selected_targets)
        for index, (code, name) in enumerate(selected_targets, start=1):
            if self.cancel_event.is_set():
                self.queue.put(("status", "已停止"))
                break
            history = daily_histories.get(code)
            self.queue.put(("status", f"日 K 验证 {index}/{total} · {code} {name}".strip()))
            if history is None or history.empty:
                funnel_counts["日K下载失败"] += 1
                messages.append(f"{code}: yfinance 日K无数据")
                self.queue.put(("progress", (index, total, len(rows), len(messages))))
                continue

            liquidity_reason = liquidity_reject_reason(history)
            if liquidity_reason is not None:
                funnel_counts["流动性淘汰"] += 1
                reject_counts[liquidity_reason] = reject_counts.get(liquidity_reason, 0) + 1
                self.queue.put(("progress", (index, total, len(rows), len(messages))))
                continue

            monthly_candidate = monthly_candidates.get(code)
            previous_record = getattr(monthly_candidate, "previous_record", None)
            try:
                match, reason = evaluate_history_detailed(
                    code,
                    name,
                    history,
                    up_days=controls["up_days"],
                    new_high_on=controls["new_high_on"],
                    near_high_pct=controls["near_high_pct"],
                    volume_mode=controls["volume_mode"],
                    previous_record_override=previous_record,
                )
            except Exception as exc:
                funnel_counts["日K下载失败"] += 1
                messages.append(f"{code}: 日K验证失败: {exc}")
                self.queue.put(("progress", (index, total, len(rows), len(messages))))
                continue

            funnel_counts["日K验证"] += 1
            if match is None:
                reject_counts[reason or "未命中"] = reject_counts.get(reason or "未命中", 0) + 1
                self.queue.put(("progress", (index, total, len(rows), len(messages))))
                continue

            row = self._row_from_match(match, industry_map.get(code, ""), source_label)
            row.update(self._daily_52w_metrics(history))
            rows.append(row)
            self.queue.put(("match", row))
            self.queue.put(("progress", (index, total, len(rows), len(messages))))

        funnel_counts["最终命中"] = len(rows)
        for reason, count in sorted(reject_counts.items(), key=lambda item: item[0]):
            funnel_counts[f"日K淘汰-{reason}"] = count
        return finish_scan()

    def _safe_fetch_industries(self, quote_ctx, futu, codes: list[str], messages: list[str]) -> dict[str, str]:
        try:
            return fetch_industry_map(quote_ctx, futu, codes)
        except Exception as exc:
            messages.append(f"行业查询失败: {exc}")
            return {}

    def _record_crypto_snapshot(self, controls: dict, event: str, messages: list[str]) -> None:
        if not controls.get("crypto_record"):
            return
        api_key = str(controls.get("cmc_api_key") or "").strip()
        if not api_key:
            if event == "scan_start":
                messages.append("CMC: 未配置 API Key，跳过加密快照记录")
            return
        symbols = tuple(controls.get("crypto_symbols") or ())
        if not symbols:
            if event == "scan_start":
                messages.append("CMC: 未配置币种，跳过加密快照记录")
            return
        config = CoinMarketCapSnapshotConfig(
            api_key=api_key,
            symbols=symbols,
            convert=str(controls.get("crypto_quote") or "USD"),
            output_path=Path(controls.get("crypto_record_path") or CMC_RECORD_PATH),
        )
        try:
            count = append_coinmarketcap_snapshot(config, event=event)
        except CoinMarketCapError as exc:
            messages.append(f"CMC {event} 记录失败: {exc}")
            return
        except Exception as exc:
            messages.append(f"CMC {event} 记录失败: {exc}")
            return
        messages.append(f"CMC {event}: 已记录 {count} 条到 {Path(config.output_path).name}")

    def _load_industry_batch(
        self,
        quote_ctx,
        futu,
        targets: list[tuple[str, str]],
        start_index: int,
        industry_map: dict[str, str],
        messages: list[str],
    ) -> None:
        batch_targets = targets[start_index : start_index + 100]
        missing_codes = [code for code, _name in batch_targets if code not in industry_map]
        if not missing_codes:
            return
        industry_map.update({code: "" for code in missing_codes})
        industry_map.update(self._safe_fetch_industries(quote_ctx, futu, missing_codes, messages))

    def _row_from_match(self, match, industry: str = "", source: str = "精确验证") -> dict:
        row = {column: "" for column in RESULT_COLUMNS}
        row.update(asdict(match))
        row["source"] = source
        row["industry"] = industry
        if row.get("prev_close"):
            row["change_rate"] = (float(row["close"]) / float(row["prev_close"]) - 1.0) * 100.0
        return row

    def _row_from_monthly_candidate(self, candidate, code: str, name: str, industry: str = "") -> dict:
        row = {column: "" for column in RESULT_COLUMNS}
        row.update(
            {
                "source": "低频预筛",
                "code": code,
                "name": name,
                "industry": industry,
            }
        )
        if candidate is not None:
            row.update(
                {
                    "last_date": candidate.last_date,
                    "close": candidate.latest_value,
                    "latest_high": candidate.latest_value,
                    "previous_record": candidate.previous_record,
                    "record_distance_pct": candidate.record_distance_pct,
                    "volume": candidate.volume if candidate.volume is not None else "",
                }
            )
        return row

    def _daily_52w_metrics(self, history: pd.DataFrame) -> dict:
        if history.empty:
            return {}
        clean = history.copy()
        clean["time_key"] = pd.to_datetime(clean["time_key"], errors="coerce")
        for column in ("high", "close"):
            clean[column] = pd.to_numeric(clean[column], errors="coerce")
        clean = clean.dropna(subset=["time_key", "high", "close"]).sort_values("time_key")
        if clean.empty:
            return {}
        recent = clean.tail(252)
        high_52w = float(recent["high"].max())
        if high_52w <= 0:
            return {}
        latest = clean.iloc[-1]
        return {
            "high_to_52w_high_pct": (float(latest["high"]) / high_52w - 1.0) * 100.0,
            "cur_to_52w_high_pct": (float(latest["close"]) / high_52w - 1.0) * 100.0,
        }

    def _append_funnel_summary(self, messages: list[str], counts: dict[str, int]) -> None:
        ordered = [f"{key}: {value}" for key, value in counts.items()]
        messages.insert(0, "漏斗统计\n" + "\n".join(ordered))

    def _row_from_quick_candidate(self, candidate, controls: dict, industry: str = "") -> dict:
        distance = candidate.cur_to_52w_high_pct if controls["new_high_on"] == "close" else candidate.high_to_52w_high_pct
        return {
            "source": "全市场预筛",
            "code": candidate.code,
            "name": candidate.name,
            "industry": industry,
            "last_date": "",
            "open": "",
            "close": candidate.cur_price if candidate.cur_price is not None else "",
            "prev_close": "",
            "change_rate": candidate.change_rate if candidate.change_rate is not None else "",
            "latest_high": "",
            "previous_record": "",
            "record_distance_pct": distance if distance is not None else "",
            "high_to_52w_high_pct": candidate.high_to_52w_high_pct if candidate.high_to_52w_high_pct is not None else "",
            "cur_to_52w_high_pct": candidate.cur_to_52w_high_pct if candidate.cur_to_52w_high_pct is not None else "",
            "volume": candidate.volume if candidate.volume is not None else "",
            "prev_volume": candidate.prev_volume if candidate.prev_volume is not None else "",
            "volume_ratio": candidate.volume_ratio if candidate.volume_ratio is not None else "",
            "streak_days": "",
        }

    def _finalize_result(self, rows: list[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=RESULT_COLUMNS)
        result = pd.DataFrame(rows)
        for column in RESULT_COLUMNS:
            if column not in result.columns:
                result[column] = ""
        result = result[RESULT_COLUMNS]
        if "record_distance_pct" in result:
            result["_sort_distance"] = pd.to_numeric(result["record_distance_pct"], errors="coerce")
            result = result.sort_values(["source", "_sort_distance", "code"], ascending=[True, False, True], na_position="last")
            result = result.drop(columns=["_sort_distance"])
        return result.reset_index(drop=True)

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "match":
                    self._insert_row(payload)
                elif kind == "progress":
                    index, total, hits, skips = payload
                    self.progress.configure(value=(index / total) * 100 if total else 0)
                    self.hit_metric.set(str(hits))
                    self.skip_metric.set(str(skips))
                elif kind == "stage_progress":
                    done, total, label = payload
                    self.progress.configure(value=(done / total) * 100 if total else 0)
                    self.scan_metric.set(f"{label} {done}/{total}")
                elif kind == "pool_size":
                    self.pool_metric.set(str(payload))
                elif kind == "scan_total":
                    self.scan_metric.set(str(payload))
                elif kind == "error":
                    messagebox.showerror("扫描失败", str(payload))
                elif kind == "done":
                    result, messages = payload
                    self.skip_messages = messages
                    preserve_previous = self._should_preserve_previous_pool(result, messages)
                    if preserve_previous:
                        self.result = self.previous_result_before_scan.copy()
                        self.view_result = self.result.copy()
                        self.active_targets = (
                            list(self.previous_active_targets_before_scan)
                            if self.previous_active_targets_before_scan is not None
                            else self._targets_from_result(self.result)
                        )
                    else:
                        self.result = result
                        self.view_result = result.copy()
                    if not self.cancel_event.is_set() and not preserve_previous:
                        self.active_targets = self._targets_from_result(result)
                        self._save_cache()
                    elif not self.cancel_event.is_set() and preserve_previous:
                        self._save_cache()
                    self.sort_column = None
                    self.sort_descending = False
                    self._reload_tree(self.view_result)
                    self.hit_metric.set(str(len(result)))
                    self.skip_metric.set(str(len(messages)))
                    if self.cancel_event.is_set():
                        self.status_var.set("已停止")
                    elif preserve_previous:
                        self.hit_metric.set(str(len(self.result)))
                        self.status_var.set("本轮日 K 下载失败导致无命中，已保留上一轮股票池。")
                    elif result.empty:
                        self.status_var.set("扫描完成，无命中。未命中表示条件不满足；跳过只表示接口或数据失败。")
                    else:
                        scope_text = f"当前股票池已收窄到 {len(self.active_targets or [])} 只"
                        self.status_var.set(f"扫描完成，{scope_text}")
                    self._set_running(False)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _insert_row(self, row: dict) -> None:
        self.tree.insert("", "end", values=self._format_row(row))

    def _sort_by_column(self, column: str) -> None:
        if self.result.empty or column not in self.result.columns:
            return
        if self.sort_column == column:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_column = column
            self.sort_descending = False

        sorted_result = self.result.copy()
        sort_key = f"__sort_{column}"
        if column in self._numeric_columns():
            sorted_result[sort_key] = pd.to_numeric(sorted_result[column], errors="coerce")
        else:
            sorted_result[sort_key] = sorted_result[column].astype(str).str.casefold()
        sorted_result = sorted_result.sort_values(
            sort_key,
            ascending=not self.sort_descending,
            kind="mergesort",
            na_position="last",
        ).drop(columns=[sort_key])
        self.view_result = sorted_result.reset_index(drop=True)
        self._reload_tree(self.view_result)
        direction = "降序" if self.sort_descending else "升序"
        title = self._column_title(column)
        self.status_var.set(f"按 {title} {direction} 排序")

    def _reset_sort(self) -> None:
        self.sort_column = None
        self.sort_descending = False
        self.view_result = self.result.copy()
        self._reload_tree(self.view_result)
        self.status_var.set("已恢复原始顺序")

    def _reset_pool(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("正在扫描", "请先停止或等待当前扫描完成。")
            return
        self.active_targets = None
        self.result = pd.DataFrame(columns=RESULT_COLUMNS)
        self.view_result = self.result.copy()
        self.skip_messages = []
        self.sort_column = None
        self.sort_descending = False
        self._clear_tree()
        self.hit_metric.set("0")
        self.pool_metric.set("全市场")
        self.scan_metric.set("0")
        self.skip_metric.set("0")
        self.progress.configure(value=0)
        self.status_var.set("已重置股票池；下次将从全市场筛选")
        self._clear_cache()

    def _should_preserve_previous_pool(self, result: pd.DataFrame, messages: list[str]) -> bool:
        if self.current_scope != "active" or not result.empty:
            return False
        if self.previous_active_targets_before_scan is None:
            return False
        failure_markers = ("yfinance 日K无数据", "日K下载失败", "下载失败")
        return any(any(marker in message for marker in failure_markers) for message in messages)

    def _targets_from_result(self, result: pd.DataFrame) -> list[tuple[str, str]]:
        if result.empty or not {"code", "name"}.issubset(result.columns):
            return []
        records = result[["code", "name"]].dropna(subset=["code"]).drop_duplicates(subset=["code"]).to_dict("records")
        return [(str(row["code"]), str(row.get("name", ""))) for row in records if str(row["code"]).strip()]

    def _save_cache(self) -> None:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "result": self.result.fillna("").to_dict("records"),
                "active_targets": [[code, name] for code, name in (self.active_targets or [])],
                "skip_messages": self.skip_messages,
            }
            tmp_path = CACHE_PATH.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(CACHE_PATH)
        except Exception as exc:
            self.skip_messages.append(f"缓存保存失败: {exc}")

    def _load_cache(self) -> None:
        if not CACHE_PATH.exists():
            return
        try:
            payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            records = payload.get("result") or []
            result = pd.DataFrame(records)
            for column in RESULT_COLUMNS:
                if column not in result.columns:
                    result[column] = ""
            result = result[RESULT_COLUMNS] if not result.empty else pd.DataFrame(columns=RESULT_COLUMNS)

            raw_targets = payload.get("active_targets")
            if raw_targets is None:
                active_targets = self._targets_from_result(result)
            else:
                active_targets = [
                    (str(item[0]), str(item[1]) if len(item) > 1 else "")
                    for item in raw_targets
                    if isinstance(item, (list, tuple)) and item and str(item[0]).strip()
                ]

            self.result = result.reset_index(drop=True)
            self.view_result = self.result.copy()
            self.skip_messages = list(payload.get("skip_messages") or [])
            self.active_targets = active_targets
            self.sort_column = None
            self.sort_descending = False
            self._reload_tree(self.view_result)
            self.hit_metric.set(str(len(self.result)))
            self.pool_metric.set("已恢复")
            self.scan_metric.set(str(len(active_targets)))
            self.skip_metric.set(str(len(self.skip_messages)))
            self.progress.configure(value=0)
            updated_at = payload.get("updated_at") or "未知时间"
            self.status_var.set(f"已恢复上次结果（{updated_at}），当前股票池 {len(active_targets)} 只")
        except Exception as exc:
            self.status_var.set(f"缓存读取失败：{exc}")

    def _clear_cache(self) -> None:
        try:
            if CACHE_PATH.exists():
                CACHE_PATH.unlink()
            tmp_path = CACHE_PATH.with_suffix(".tmp")
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception as exc:
            self.skip_messages.append(f"缓存清理失败: {exc}")

    def _numeric_columns(self) -> set[str]:
        return {
            "close",
            "open",
            "prev_close",
            "change_rate",
            "latest_high",
            "previous_record",
            "record_distance_pct",
            "high_to_52w_high_pct",
            "cur_to_52w_high_pct",
            "volume",
            "prev_volume",
            "volume_ratio",
            "streak_days",
        }

    def _column_title(self, column: str) -> str:
        for key, title, _width in DISPLAY_COLUMNS:
            if key == column:
                return title
        return column

    def _reload_tree(self, result: pd.DataFrame) -> None:
        self._clear_tree()
        for row in result.to_dict("records"):
            self._insert_row(row)

    def _clear_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

    def _format_row(self, row: dict) -> list[str]:
        values: list[str] = []
        for key, _title, _width in DISPLAY_COLUMNS:
            value = row.get(key, "")
            if value == "" or pd.isna(value):
                values.append("")
            elif key in {"open", "close", "prev_close", "latest_high", "previous_record"}:
                values.append(f"{float(value):.2f}")
            elif key in {"record_distance_pct", "change_rate", "high_to_52w_high_pct", "cur_to_52w_high_pct"}:
                values.append(f"{float(value):.2f}%")
            elif key in {"volume", "prev_volume"}:
                values.append(f"{float(value):,.0f}")
            elif key == "volume_ratio":
                values.append(f"{float(value):.2f}")
            else:
                values.append(str(value))
        return values

    def _set_running(self, running: bool) -> None:
        self.run_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def _stop_scan(self) -> None:
        self.cancel_event.set()
        self.status_var.set("正在停止")

    def _save_csv(self) -> None:
        if self.view_result.empty:
            messagebox.showinfo("没有结果", "当前没有可保存的命中结果。")
            return
        path = filedialog.asksaveasfilename(
            title="保存选股结果",
            defaultextension=".csv",
            initialfile="futu_us_breakout_matches.csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        self.view_result.to_csv(Path(path), index=False)
        messagebox.showinfo("已保存", f"结果已保存到：\n{path}")

    def _show_skips(self) -> None:
        window = tk.Toplevel(self)
        window.title("跳过记录")
        window.geometry("780x420")
        window.transient(self)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="word")
        text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scrollbar.pack(side="right", fill="y")
        text.configure(yscrollcommand=scrollbar.set)
        if self.skip_messages:
            text.insert("1.0", "\n".join(self.skip_messages))
        else:
            text.insert("1.0", "没有跳过记录。")
        text.configure(state="disabled")


def main() -> None:
    app = StockScreenerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
