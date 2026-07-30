#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from coinmarketcap_historical_backfill import DEFAULT_OUTPUT_DIR, make_backfill_args, run_backfill
from coinmarketcap_recorder import CoinMarketCapError, fetch_coinmarketcap_key_info, fetch_coinmarketcap_quotes


DEFAULT_HISTORY_DAYS = 30
DEFAULT_SAFE_RPM = 580.0


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def recent_utc_window(days: int = DEFAULT_HISTORY_DAYS) -> tuple[str, str]:
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=max(1, int(days)))
    return iso_utc(start), iso_utc(end)


def parse_utc_text(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def first_number(*values: object) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def format_number(value: float | None) -> str:
    if value is None:
        return "未知"
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


class CryptoDataDownloaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Crypto Data Downloader")
        self.geometry("1040x720")
        self.minsize(900, 620)

        self.queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()

        self._configure_theme()
        self._build_variables()
        self._build_layout()
        self._poll_queue()

    def _configure_theme(self) -> None:
        style = ttk.Style(self)
        if "aqua" in set(style.theme_names()):
            style.theme_use("aqua")
        style.configure("Title.TLabel", font=("Helvetica Neue", 22, "bold"))
        style.configure("Section.TLabel", font=("Helvetica Neue", 13, "bold"))
        style.configure("Muted.TLabel", foreground="#667085")
        style.configure("Metric.TLabel", font=("Helvetica Neue", 18, "bold"))
        style.configure("Primary.TButton", padding=(14, 8))

    def _build_variables(self) -> None:
        default_start, default_end = recent_utc_window(DEFAULT_HISTORY_DAYS)
        self.api_key_var = tk.StringVar(value=os.getenv("COINMARKETCAP_API_KEY") or os.getenv("CMC_API_KEY") or "")
        self.top_n_var = tk.IntVar(value=100)
        self.ids_var = tk.StringVar(value="")
        self.symbols_var = tk.StringVar(value="")
        self.convert_var = tk.StringVar(value="USD")
        self.interval_var = tk.StringVar(value="5m")
        self.history_days_var = tk.IntVar(value=DEFAULT_HISTORY_DAYS)
        self.start_var = tk.StringVar(value=default_start)
        self.end_var = tk.StringVar(value=default_end)
        self.rpm_var = tk.DoubleVar(value=25.0)
        self.max_points_var = tk.IntVar(value=10000)
        self.force_var = tk.BooleanVar(value=False)
        self.output_dir_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        controls_shell = ttk.Frame(self)
        controls_shell.grid(row=0, column=0, sticky="nsw")
        controls_shell.columnconfigure(0, weight=1)
        controls_shell.rowconfigure(0, weight=1)

        controls_canvas = tk.Canvas(controls_shell, width=400, borderwidth=0, highlightthickness=0)
        controls_canvas.grid(row=0, column=0, sticky="ns")
        controls_scrollbar = ttk.Scrollbar(controls_shell, orient="vertical", command=controls_canvas.yview)
        controls_scrollbar.grid(row=0, column=1, sticky="ns")
        controls_canvas.configure(yscrollcommand=controls_scrollbar.set)

        controls = ttk.Frame(controls_canvas, padding=(22, 20, 18, 20))
        controls_window = controls_canvas.create_window((0, 0), window=controls, anchor="nw")
        controls.columnconfigure(0, weight=1)

        controls.bind(
            "<Configure>",
            lambda _event: controls_canvas.configure(scrollregion=controls_canvas.bbox("all")),
        )
        controls_canvas.bind(
            "<Configure>",
            lambda event: controls_canvas.itemconfigure(controls_window, width=event.width),
        )
        controls_canvas.bind("<Enter>", lambda _event: self._bind_controls_mousewheel(controls_canvas))
        controls_canvas.bind("<Leave>", lambda _event: self._unbind_controls_mousewheel())

        main = ttk.Frame(self, padding=(18, 20, 22, 20))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)

        self._build_controls(controls)
        self._bind_controls_scroll_events(controls, controls_canvas)
        self._build_main(main)

    def _bind_controls_scroll_events(self, widget: tk.Widget, canvas: tk.Canvas) -> None:
        widget.bind("<Enter>", lambda _event: self._bind_controls_mousewheel(canvas), add="+")
        widget.bind("<Leave>", lambda _event: self._unbind_controls_mousewheel(), add="+")
        for child in widget.winfo_children():
            self._bind_controls_scroll_events(child, canvas)

    def _bind_controls_mousewheel(self, canvas: tk.Canvas) -> None:
        self._controls_scroll_canvas = canvas
        self.bind_all("<MouseWheel>", self._on_controls_mousewheel)
        self.bind_all("<Button-4>", self._on_controls_mousewheel)
        self.bind_all("<Button-5>", self._on_controls_mousewheel)

    def _unbind_controls_mousewheel(self) -> None:
        self.unbind_all("<MouseWheel>")
        self.unbind_all("<Button-4>")
        self.unbind_all("<Button-5>")

    def _on_controls_mousewheel(self, event: tk.Event) -> str:
        canvas = getattr(self, "_controls_scroll_canvas", None)
        if canvas is None:
            return "break"
        if getattr(event, "num", None) == 4:
            units = -3
        elif getattr(event, "num", None) == 5:
            units = 3
        else:
            delta = getattr(event, "delta", 0)
            if abs(delta) >= 120:
                units = -int(delta / 120)
            else:
                units = -3 if delta > 0 else 3
        canvas.yview_scroll(units, "units")
        return "break"

    def _build_controls(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Crypto Data Downloader", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(parent, text="CoinMarketCap 历史高频数据下载", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(0, 16)
        )

        row = 2
        row = self._section(parent, row, "连接")
        self._labeled_entry(parent, row, "CMC API Key", self.api_key_var, show="*")
        row += 1
        connection_buttons = ttk.Frame(parent)
        connection_buttons.grid(row=row, column=0, sticky="ew", pady=(0, 16))
        connection_buttons.columnconfigure(0, weight=1)
        connection_buttons.columnconfigure(1, weight=1)
        connection_buttons.columnconfigure(2, weight=1)
        ttk.Button(connection_buttons, text="测试连接", command=self._test_connection).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.quota_button = ttk.Button(connection_buttons, text="读取额度", command=self._start_load_limits)
        self.quota_button.grid(row=0, column=1, sticky="ew", padx=6)
        self.probe_button = ttk.Button(connection_buttons, text="压测上限", command=self._start_limit_probe)
        self.probe_button.grid(row=0, column=2, sticky="ew", padx=(6, 0))
        row += 1

        row = self._section(parent, row, "范围")
        self._labeled_entry(parent, row, "Top N", self.top_n_var)
        row += 1
        self._labeled_entry(parent, row, "指定 ID", self.ids_var)
        row += 1
        self._labeled_entry(parent, row, "限定符号", self.symbols_var)
        row += 1
        self._labeled_entry(parent, row, "计价", self.convert_var)
        row += 1
        ttk.Label(
            parent,
            text="指定 ID 会覆盖 Top N，例如 1,1027。限定符号只在 Top N 池里过滤，例如 BTC,ETH。",
            style="Muted.TLabel",
            wraplength=330,
        ).grid(row=row, column=0, sticky="w", pady=(0, 14))
        row += 1

        row = self._section(parent, row, "时间与频率")
        combo_row = ttk.Frame(parent)
        combo_row.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        combo_row.columnconfigure(1, weight=1)
        ttk.Label(combo_row, text="频率").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.interval_combo = ttk.Combobox(
            combo_row,
            textvariable=self.interval_var,
            values=("5m", "10m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"),
            state="readonly",
            width=12,
        )
        self.interval_combo.grid(row=0, column=1, sticky="ew")
        row += 1
        window_row = ttk.Frame(parent)
        window_row.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        window_row.columnconfigure(1, weight=1)
        ttk.Label(window_row, text="最近天数").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(window_row, textvariable=self.history_days_var, width=12).grid(row=0, column=1, sticky="ew", padx=(0, 8))
        ttk.Button(window_row, text="套用窗口", command=self._apply_recent_window).grid(row=0, column=2, sticky="e")
        row += 1
        self._labeled_entry(parent, row, "开始 UTC", self.start_var)
        row += 1
        self._labeled_entry(parent, row, "结束 UTC", self.end_var)
        row += 1
        ttk.Label(
            parent,
            text="当前 key 实测高频历史只开放最近约 1 个月；升级套餐后可手动放大窗口。格式例：2026-04-01T00:00:00Z。",
            style="Muted.TLabel",
            wraplength=330,
        ).grid(row=row, column=0, sticky="w", pady=(0, 14))
        row += 1

        row = self._section(parent, row, "速度")
        self._labeled_entry(parent, row, "每分钟请求", self.rpm_var)
        row += 1
        self._labeled_entry(parent, row, "每次点数", self.max_points_var)
        row += 1
        ttk.Checkbutton(parent, text="强制重抓已完成币种", variable=self.force_var).grid(row=row, column=0, sticky="w", pady=(0, 14))
        row += 1

        row = self._section(parent, row, "保存")
        self._labeled_entry(parent, row, "输出目录", self.output_dir_var)
        row += 1
        ttk.Button(parent, text="打开目录", command=self._open_output_dir).grid(row=row, column=0, sticky="ew", pady=(0, 16))
        row += 1

        buttons = ttk.Frame(parent)
        buttons.grid(row=row, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        self.estimate_button = ttk.Button(buttons, text="估算", command=lambda: self._start(dry_run=True))
        self.estimate_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.download_button = ttk.Button(buttons, text="开始下载", style="Primary.TButton", command=lambda: self._start(dry_run=False))
        self.download_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        row += 1
        self.stop_button = ttk.Button(parent, text="停止", command=self._stop, state="disabled")
        self.stop_button.grid(row=row, column=0, sticky="ew", pady=(10, 0))

    def _build_main(self, parent: ttk.Frame) -> None:
        metrics = ttk.Frame(parent)
        metrics.grid(row=0, column=0, sticky="ew")
        for col in range(4):
            metrics.columnconfigure(col, weight=1)
        self.status_metric = self._metric(metrics, 0, "状态", "准备就绪")
        self.interval_metric = self._metric(metrics, 1, "频率", "5m")
        self.quota_metric = self._metric(metrics, 2, "额度", "未读取")
        self.output_metric = self._metric(metrics, 3, "输出", DEFAULT_OUTPUT_DIR.name)

        self.progress = ttk.Progressbar(parent, mode="indeterminate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(16, 12))

        ttk.Label(parent, text="运行日志", style="Section.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 8))
        log_frame = ttk.Frame(parent)
        log_frame.grid(row=3, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", height=18)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        footer = ttk.Frame(parent)
        footer.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        self.footer_var = tk.StringVar(value="先点估算，确认请求量后再开始下载。")
        ttk.Label(footer, textvariable=self.footer_var, style="Muted.TLabel").grid(row=0, column=0, sticky="w")

    def _section(self, parent: ttk.Frame, row: int, title: str) -> int:
        ttk.Label(parent, text=title, style="Section.TLabel").grid(row=row, column=0, sticky="w", pady=(8, 8))
        return row + 1

    def _labeled_entry(self, parent: ttk.Frame, row: int, label: str, variable: tk.Variable, *, show: str | None = None) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=label).grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(frame, textvariable=variable, width=22, show=show).grid(row=0, column=1, sticky="ew")

    def _metric(self, parent: ttk.Frame, column: int, label: str, value: str) -> tk.StringVar:
        frame = ttk.Frame(parent, padding=(12, 10, 12, 10), relief="solid")
        frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
        ttk.Label(frame, text=label, style="Muted.TLabel").pack(anchor="w")
        var = tk.StringVar(value=value)
        ttk.Label(frame, textvariable=var, style="Metric.TLabel").pack(anchor="w", pady=(4, 0))
        return var

    def _read_api_key(self) -> str:
        return self.api_key_var.get().strip() or os.getenv("COINMARKETCAP_API_KEY", "").strip() or os.getenv("CMC_API_KEY", "").strip()

    def _build_args(self, *, dry_run: bool):
        api_key = self._read_api_key()
        if not api_key:
            raise ValueError("请填写 CMC API Key。")
        top_n = int(self.top_n_var.get())
        history_days = int(self.history_days_var.get())
        rpm = float(self.rpm_var.get())
        max_points = int(self.max_points_var.get())
        if top_n < 1:
            raise ValueError("Top N 必须至少为 1。")
        if history_days < 1:
            raise ValueError("最近天数必须至少为 1。")
        if rpm <= 0:
            raise ValueError("每分钟请求必须大于 0。")
        if max_points < 1:
            raise ValueError("每次点数必须至少为 1。")
        start_text = self.start_var.get().strip()
        end_text = self.end_var.get().strip()
        if not end_text:
            end_text = iso_utc(datetime.now(timezone.utc))
        if not start_text:
            end_dt = parse_utc_text(end_text)
            start_text = iso_utc(end_dt - timedelta(days=history_days))
        return make_backfill_args(
            api_key=api_key,
            output_dir=Path(self.output_dir_var.get()).expanduser(),
            limit=top_n,
            symbols=self.symbols_var.get().strip() or None,
            ids=self.ids_var.get().strip() or None,
            convert=self.convert_var.get().strip().upper() or "USD",
            interval=self.interval_var.get().strip(),
            years=1,
            start=start_text,
            end=end_text,
            max_points=max_points,
            requests_per_minute=rpm,
            dry_run=dry_run,
            force=bool(self.force_var.get()),
            progress_callback=lambda message: self.queue.put(("log", message)),
            cancel_event=self.cancel_event,
        )

    def _test_connection(self) -> None:
        try:
            payload = fetch_coinmarketcap_quotes(self._read_api_key(), ("BTC",), convert=self.convert_var.get().strip().upper() or "USD")
            count = len(payload.get("data") or {})
        except Exception as exc:
            messagebox.showerror("连接失败", str(exc))
            return
        messagebox.showinfo("连接成功", f"CoinMarketCap 已返回 {count} 个币种。")

    def _apply_recent_window(self) -> None:
        try:
            days = int(self.history_days_var.get())
            if days < 1:
                raise ValueError("最近天数必须至少为 1。")
        except Exception as exc:
            messagebox.showerror("无法套用窗口", str(exc))
            return
        start_text, end_text = recent_utc_window(days)
        self.start_var.set(start_text)
        self.end_var.set(end_text)
        self.footer_var.set(f"已套用最近 {days} 天窗口。")

    def _start_load_limits(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        api_key = self._read_api_key()
        if not api_key:
            messagebox.showerror("无法读取额度", "请填写 CMC API Key。")
            return
        self.cancel_event.clear()
        self._set_running(True)
        self.status_metric.set("读额度")
        self.footer_var.set("正在读取官方额度，不做压测。")
        self._log(f"{datetime.now().strftime('%H:%M:%S')} 读取 CoinMarketCap 官方额度")
        self.worker = threading.Thread(target=self._load_limits_worker, args=(api_key,), daemon=True)
        self.worker.start()

    def _load_limits_worker(self, api_key: str) -> None:
        try:
            payload = fetch_coinmarketcap_key_info(api_key, timeout_seconds=15)
        except Exception as exc:
            self.queue.put(("quota_done", (False, str(exc), None)))
            return
        self.queue.put(("quota_done", (True, "额度读取完成", payload)))

    def _apply_key_info_payload(self, payload: dict) -> str:
        data = payload.get("data") or {}
        plan = data.get("plan") or {}
        usage = data.get("usage") or {}
        current_minute = usage.get("current_minute") or {}
        current_month = usage.get("current_month") or {}

        rate_limit = first_number(
            plan.get("rate_limit_minute"),
            plan.get("minute_request_limit"),
            data.get("rate_limit_minute"),
        )
        month_limit = first_number(plan.get("credit_limit_monthly"), data.get("credit_limit_monthly"))
        month_used = first_number(
            current_month.get("credits_used"),
            current_month.get("used"),
            usage.get("month_used"),
        )
        month_left = first_number(
            current_month.get("credits_left"),
            current_month.get("left"),
            usage.get("month_left"),
        )
        minute_left = first_number(
            current_minute.get("requests_left"),
            current_minute.get("left"),
            usage.get("current_minute_left"),
        )
        reset_at = (
            plan.get("credit_limit_monthly_reset")
            or plan.get("credit_limit_reset")
            or data.get("credit_limit_monthly_reset")
            or ""
        )

        if rate_limit is not None:
            safe_rpm = self._safe_rpm(rate_limit)
            self.rpm_var.set(float(safe_rpm))
            self.quota_metric.set(f"{int(rate_limit):,} rpm")
        else:
            safe_rpm = None
            self.quota_metric.set("未返回")

        self.history_days_var.set(DEFAULT_HISTORY_DAYS)
        start_text, end_text = recent_utc_window(DEFAULT_HISTORY_DAYS)
        self.start_var.set(start_text)
        self.end_var.set(end_text)

        parts = [
            f"官方分钟上限 {format_number(rate_limit)} rpm",
            f"安全下载速度 {format_number(safe_rpm)} rpm",
            f"月度 credits {format_number(month_used)} / {format_number(month_limit)}",
            f"剩余 {format_number(month_left)}",
        ]
        if minute_left is not None:
            parts.append(f"当前分钟剩余请求 {format_number(minute_left)}")
        if reset_at:
            parts.append(f"重置 {reset_at}")
        parts.append(f"已把历史窗口设为最近 {DEFAULT_HISTORY_DAYS} 天")
        return "；".join(parts)

    def _safe_rpm(self, rate_limit: float) -> int:
        if rate_limit <= 1:
            return 1
        if rate_limit >= 600:
            return int(min(rate_limit - 1, DEFAULT_SAFE_RPM))
        if rate_limit >= 60:
            return max(1, int(rate_limit * 0.95))
        return max(1, int(rate_limit * 0.90))

    def _start(self, *, dry_run: bool) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            args = self._build_args(dry_run=dry_run)
        except Exception as exc:
            messagebox.showerror("无法开始", str(exc))
            return
        self.cancel_event.clear()
        self._set_running(True)
        self.status_metric.set("估算中" if dry_run else "下载中")
        self.interval_metric.set(args.interval)
        self.output_metric.set(Path(args.output_dir).name)
        self.footer_var.set("正在估算请求量..." if dry_run else "正在下载，可停止并下次续传。")
        self._log(f"{datetime.now().strftime('%H:%M:%S')} {'估算' if dry_run else '开始下载'}")
        try:
            if parse_utc_text(args.start) < datetime.now(timezone.utc) - timedelta(days=35):
                self._log("提醒：当前 key 实测高频历史只开放最近约 1 个月，开始日期太早可能会被 CMC 拒绝。")
        except Exception:
            pass
        self.worker = threading.Thread(target=self._worker, args=(args, dry_run), daemon=True)
        self.worker.start()

    def _start_limit_probe(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            api_key = self._read_api_key()
            if not api_key:
                raise ValueError("请填写 CMC API Key。")
            max_rpm = float(self.rpm_var.get())
            if max_rpm <= 0:
                raise ValueError("每分钟请求必须大于 0。")
            convert = self.convert_var.get().strip().upper() or "USD"
        except Exception as exc:
            messagebox.showerror("无法测试上限", str(exc))
            return
        self.cancel_event.clear()
        self._set_running(True)
        self.status_metric.set("测速中")
        self.footer_var.set("正在阶梯测试 API 请求上限，会消耗少量 credits。")
        self._log(f"{datetime.now().strftime('%H:%M:%S')} 开始测试上限，目标最高 {max_rpm:g} rpm")
        self.worker = threading.Thread(target=self._limit_probe_worker, args=(api_key, convert, max_rpm), daemon=True)
        self.worker.start()

    def _limit_probe_worker(self, api_key: str, convert: str, max_rpm: float) -> None:
        stages = self._rate_probe_stages(max_rpm)
        passed: list[float] = []
        total_requests = 0
        try:
            for rpm in stages:
                if self.cancel_event.is_set():
                    self.queue.put(("done", (True, f"已停止测速。已通过 {passed[-1]:g} rpm" if passed else "已停止测速。")))
                    return
                duration = 20.0
                interval = 60.0 / rpm
                deadline = time.monotonic() + duration
                stage_count = 0
                self.queue.put(("log", f"测试 {rpm:g} rpm，持续约 {int(duration)} 秒"))
                while time.monotonic() < deadline:
                    if self.cancel_event.is_set():
                        self.queue.put(("done", (True, f"已停止测速。已通过 {passed[-1]:g} rpm" if passed else "已停止测速。")))
                        return
                    started = time.monotonic()
                    fetch_coinmarketcap_quotes(api_key, ("BTC",), convert=convert, timeout_seconds=15)
                    stage_count += 1
                    total_requests += 1
                    elapsed = time.monotonic() - started
                    sleep_seconds = max(0.0, interval - elapsed)
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                passed.append(rpm)
                self.queue.put(("log", f"通过 {rpm:g} rpm，本阶段请求 {stage_count} 次"))
        except Exception as exc:
            ceiling = passed[-1] if passed else 0
            self.queue.put(
                (
                    "done",
                    (
                        True,
                        f"测速停止：{exc}。已通过约 {ceiling:g} rpm；本次消耗约 {total_requests} 次请求。",
                    ),
                )
            )
            return
        self.queue.put(("done", (True, f"测速完成：最高测试到 {passed[-1]:g} rpm 未触发限速；本次消耗约 {total_requests} 次请求。")))

    def _rate_probe_stages(self, max_rpm: float) -> list[float]:
        base = [10, 20, 30, 45, 60, 90, 120, 180, 240, 300, 450, 600]
        stages = [float(value) for value in base if value <= max_rpm]
        if not stages or stages[-1] < max_rpm:
            stages.append(float(max_rpm))
        return stages

    def _worker(self, args, dry_run: bool) -> None:
        try:
            code = run_backfill(args)
        except CoinMarketCapError as exc:
            self.queue.put(("done", (False, str(exc))))
            return
        except Exception as exc:
            self.queue.put(("done", (False, str(exc))))
            return
        if code == 0:
            self.queue.put(("done", (True, "估算完成" if dry_run else "下载完成")))
        elif code == 2:
            self.queue.put(("done", (True, "已停止，可下次续传")))
        else:
            self.queue.put(("done", (False, f"下载失败，退出码 {code}")))

    def _stop(self) -> None:
        self.cancel_event.set()
        self.footer_var.set("正在停止，当前请求结束后会保存断点。")
        self.status_metric.set("停止中")

    def _set_running(self, running: bool) -> None:
        self.estimate_button.configure(state="disabled" if running else "normal")
        self.download_button.configure(state="disabled" if running else "normal")
        self.quota_button.configure(state="disabled" if running else "normal")
        self.probe_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        if running:
            self.progress.start(80)
        else:
            self.progress.stop()

    def _open_output_dir(self) -> None:
        path = Path(self.output_dir_var.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["open", str(path)])
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc))

    def _log(self, message: str) -> None:
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    message = str(payload)
                    self._log(message)
                    self.footer_var.set(message)
                elif kind == "done":
                    ok, message = payload
                    self._set_running(False)
                    self.status_metric.set("完成" if ok else "失败")
                    self.footer_var.set(str(message))
                    self._log(str(message))
                    if not ok:
                        messagebox.showerror("加密数据下载", str(message))
                elif kind == "quota_done":
                    ok, message, quota_payload = payload
                    self._set_running(False)
                    if ok and isinstance(quota_payload, dict):
                        detail = self._apply_key_info_payload(quota_payload)
                        self.status_metric.set("完成")
                        self.footer_var.set(detail)
                        self._log(detail)
                    else:
                        self.status_metric.set("失败")
                        self.footer_var.set(str(message))
                        self._log(str(message))
                        messagebox.showerror("读取额度失败", str(message))
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)


def main() -> None:
    app = CryptoDataDownloaderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
