from __future__ import annotations

import json
import os
import errno
from dataclasses import replace
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

import pandas as pd
import streamlit as st

from taa_futu.crypto_ofim import (
    AUTO_LOG_FILE,
    AUTO_PID_FILE,
    DEFAULT_CORE_USDT_SYMBOLS,
    DEFAULT_TIGHT_USDT_SYMBOLS,
    EVENTS_FILE,
    FEATURES_FILE,
    ORDERS_FILE,
    USER_FILLS_FILE,
    USER_STREAM_EVENTS_FILE,
    CryptoOfimEngine,
    CryptoOfimError,
    _is_transient_network_message,
    ensure_crypto_ofim_auto_submit_allowed,
    estimate_crypto_ofim_request_weight,
    load_crypto_ofim_settings,
    read_crypto_ofim_status,
    reset_crypto_ofim_paper,
)
from taa_futu.crypto_perp import (
    FEATURES_FILE as PERP_FEATURES_FILE,
    ORDERS_FILE as PERP_ORDERS_FILE,
    CryptoPerpEngine,
    CryptoPerpError,
    explain_crypto_perp_status,
    load_crypto_perp_settings,
    read_crypto_perp_status,
    reset_crypto_perp_paper,
)
from taa_futu.crypto_ofim_watchdog import (
    WATCHDOG_PID_FILE,
    read_crypto_ofim_watchdog_status,
    start_crypto_ofim_watchdog_service,
    stop_crypto_ofim_watchdog_service,
)
from taa_futu.crypto_ofim_stream import (
    STREAM_EVENTS_FILE,
    STREAM_LOG_FILE,
    STREAM_PID_FILE,
    read_crypto_ofim_stream_status,
)
from taa_futu.crypto_learning import (
    CRYPTO_ATTRIBUTION_FILE,
    CRYPTO_LEARNING_REVIEW_PACKET_FILE,
    CRYPTO_LEARNING_REVIEW_PACKET_JSON_FILE,
    CRYPTO_ORDER_MEMORY_FILE,
    CRYPTO_PROMOTION_REPORT_FILE,
    CRYPTO_TRADE_OUTCOMES_FILE,
    CRYPTO_UPGRADE_CANDIDATES_FILE,
    load_learning_report,
    load_learning_review_packet,
    load_promotion_report,
    load_upgrade_candidates,
    run_learning_pipeline,
)
from taa_futu.crypto_research_loop import (
    BEST_CANDIDATE_FILE as CRYPTO_RESEARCH_BEST_CANDIDATE_FILE,
    LOCKED_TEST_REPORT_FILE as CRYPTO_RESEARCH_LOCKED_TEST_REPORT_FILE,
    RESEARCH_PATCH_REPORT_FILE as CRYPTO_RESEARCH_PATCH_REPORT_FILE,
    TRIALS_FILE as CRYPTO_RESEARCH_TRIALS_FILE,
    read_crypto_research_status,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _read_env_lines() -> list[str]:
    if not ENV_FILE.exists():
        return []
    return ENV_FILE.read_text(encoding="utf-8").splitlines()


def _save_env_values(values: dict[str, str], *, skip_blank_secret: bool = True) -> None:
    lines = _read_env_lines()
    existing: dict[str, int] = {}
    for idx, line in enumerate(lines):
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        existing[key] = idx

    for key, value in values.items():
        if skip_blank_secret and key in {"CRYPTO_OFIM_API_KEY", "CRYPTO_OFIM_API_SECRET", "CRYPTO_PERP_API_KEY", "CRYPTO_PERP_API_SECRET"} and not value:
            continue
        entry = f"{key}={value}"
        if key in existing:
            lines[existing[key]] = entry
        else:
            lines.append(entry)

    ENV_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _fmt_money(value: Any, quote: str = "USDT") -> str:
    try:
        return f"{float(value):,.2f} {quote}"
    except (TypeError, ValueError):
        return f"0.00 {quote}"


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


def _mask_secret(value: str | None, *, head: int = 4, tail: int = 4) -> str:
    if not value:
        return "未保存 / missing"
    text = str(value)
    if len(text) <= head + tail:
        return "已保存 / saved"
    return f"{text[:head]}...{text[-tail:]}"


def _tail_lines(path: Path, *, tail: int, block_size: int = 65536, max_scan_bytes: int = 16 * 1024 * 1024) -> list[str]:
    if tail <= 0 or not path.exists():
        return []
    try:
        file_size = path.stat().st_size
    except OSError:
        return []
    if file_size <= 0:
        return []

    chunks: list[bytes] = []
    lines_found = 0
    bytes_read = 0
    with path.open("rb") as fh:
        position = file_size
        while position > 0 and lines_found <= tail and bytes_read < max_scan_bytes:
            read_size = min(block_size, position, max_scan_bytes - bytes_read)
            position -= read_size
            fh.seek(position)
            chunk = fh.read(read_size)
            chunks.append(chunk)
            bytes_read += len(chunk)
            lines_found += chunk.count(b"\n")

    data = b"".join(reversed(chunks))
    return data.decode("utf-8", errors="ignore").splitlines()[-tail:]


def _jsonl_frame(path: Path, *, tail: int = 100) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for line in _tail_lines(path, tail=tail):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return pd.DataFrame(rows)


def _read_pid_file(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _read_pid() -> int | None:
    return _read_pid_file(AUTO_PID_FILE)


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


def _start_auto(poll_seconds: int) -> tuple[bool, str]:
    pid = _read_pid()
    if _pid_running(pid):
        return False, f"自动运行已经在跑，PID={pid}"
    try:
        ensure_crypto_ofim_auto_submit_allowed(load_crypto_ofim_settings(ENV_FILE), submit=True)
    except CryptoOfimError as exc:
        return False, str(exc)

    AUTO_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = AUTO_LOG_FILE.open("a", encoding="utf-8")
    env = os.environ.copy()
    src_root = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = src_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.Popen(
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
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    return True, f"已启动 Crypto OFIM 自动模拟，PID={proc.pid}"


def _start_watchdog(poll_seconds: int) -> tuple[bool, str]:
    pid = _read_pid_file(WATCHDOG_PID_FILE)
    if _pid_running(pid):
        return False, f"守护监控已经在跑，PID={pid}"
    interval = max(5, int(poll_seconds))
    ok, message = start_crypto_ofim_watchdog_service(
        poll_seconds=interval,
        check_seconds=30,
        stale_seconds=max(180, interval * 3 + 30),
        restart_cooldown_seconds=max(120, interval * 2),
    )
    if ok:
        return True, f"已启动守护监控服务。{message}"
    return False, f"守护监控启动失败：{message}"


def _start_stream(depth_limit: int) -> tuple[bool, str]:
    pid = _read_pid_file(STREAM_PID_FILE)
    if _pid_running(pid):
        return False, f"行情流已经在跑，PID={pid}"

    STREAM_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = STREAM_LOG_FILE.open("a", encoding="utf-8")
    env = os.environ.copy()
    src_root = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = src_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "taa_futu.cli",
            "crypto-ofim-stream",
            "--depth-limit",
            str(max(100, int(depth_limit))),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    STREAM_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    return True, f"已启动 WebSocket 行情流，PID={proc.pid}"


def _stop_auto() -> tuple[bool, str]:
    pid = _read_pid()
    if not _pid_running(pid):
        if AUTO_PID_FILE.exists():
            AUTO_PID_FILE.unlink()
        return False, "当前没有正在运行的 Crypto OFIM 自动模拟。"
    assert pid is not None
    os.kill(pid, signal.SIGTERM)
    time.sleep(0.3)
    if AUTO_PID_FILE.exists():
        AUTO_PID_FILE.unlink()
    return True, f"已停止 Crypto OFIM 自动模拟，PID={pid}"


def _stop_watchdog() -> tuple[bool, str]:
    service_ok, service_message = stop_crypto_ofim_watchdog_service()
    pid = _read_pid_file(WATCHDOG_PID_FILE)
    if not _pid_running(pid):
        if WATCHDOG_PID_FILE.exists():
            WATCHDOG_PID_FILE.unlink()
        return service_ok, service_message if service_ok else "当前没有正在运行的 Crypto OFIM 守护监控。"
    assert pid is not None
    os.kill(pid, signal.SIGTERM)
    time.sleep(0.3)
    if WATCHDOG_PID_FILE.exists():
        WATCHDOG_PID_FILE.unlink()
    return True, f"已停止 Crypto OFIM 守护监控，PID={pid}"


def _stop_stream() -> tuple[bool, str]:
    pid = _read_pid_file(STREAM_PID_FILE)
    if not _pid_running(pid):
        if STREAM_PID_FILE.exists():
            STREAM_PID_FILE.unlink()
        return False, "当前没有正在运行的 WebSocket 行情流。"
    assert pid is not None
    os.kill(pid, signal.SIGTERM)
    time.sleep(0.8)
    if _pid_running(pid):
        os.kill(pid, signal.SIGKILL)
    if STREAM_PID_FILE.exists():
        STREAM_PID_FILE.unlink()
    return True, f"已停止 WebSocket 行情流，PID={pid}"


def _card(label: str, value: str, hint: str = "") -> None:
    st.markdown(
        f"""
        <div class="metric-card">
          <div class="metric-label">{label}</div>
          <div class="metric-value">{value}</div>
          <div class="metric-hint">{hint}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _inject_style() -> None:
    st.markdown(
        """
        <style>
        .block-container { padding-top: 2.2rem; max-width: 1480px; }
        .metric-card {
            border: 1px solid #d7e3f1;
            background: linear-gradient(180deg, #ffffff 0%, #f7fbff 100%);
            border-radius: 18px;
            padding: 18px 20px;
            min-height: 118px;
            box-shadow: 0 14px 34px rgba(39, 68, 105, 0.06);
        }
        .metric-label { color: #64748b; font-size: 14px; font-weight: 700; }
        .metric-value { color: #142033; font-size: 34px; font-weight: 800; margin-top: 8px; }
        .metric-hint { color: #8a98aa; font-size: 13px; margin-top: 6px; }
        .status-good {
            border-radius: 14px; padding: 14px 16px; background: #e9f9ef; color: #137b35;
            border: 1px solid #c9efd4; font-weight: 700;
        }
        .status-warn {
            border-radius: 14px; padding: 14px 16px; background: #fff8db; color: #946400;
            border: 1px solid #f2e4a5; font-weight: 700;
        }
        .status-idle {
            border-radius: 14px; padding: 14px 16px; background: #f1f5f9; color: #475569;
            border: 1px solid #d8e0ea; font-weight: 700;
        }
        .muted-note { color: #6b7280; line-height: 1.65; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_settings(settings) -> None:
    st.subheader("连接与策略设置 / Connection & Strategy")
    with st.form("crypto_ofim_settings"):
        mode_label = "本地模拟盘 / Local Paper" if settings.mode == "paper" else "Binance Spot Testnet"
        mode = st.selectbox(
            "模式 / Mode",
            ["本地模拟盘 / Local Paper", "Binance Spot Testnet"],
            index=0 if mode_label.startswith("本地") else 1,
        )
        tight_selected = (not settings.hot_universe and not settings.core_universe and tuple(settings.symbols) == DEFAULT_TIGHT_USDT_SYMBOLS)
        universe = st.radio(
            "币种池 / Universe",
            [
                "精简高频 / Tight Liquid 5",
                "核心高流动性 / Core Liquid 10",
                "热门USDT自动池 / Hot USDT Universe",
                "自定义列表 / Custom Symbols",
            ],
            index=2 if settings.hot_universe else (1 if settings.core_universe else (0 if tight_selected else 3)),
            horizontal=True,
        )
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            initial_cash = st.number_input("本地模拟初始资金 / Local Paper Cash", min_value=0.0, value=float(settings.initial_cash), step=1000.0)
        with col2:
            active_capital = st.number_input(
                "绝对资金上限 / Hard Capital Cap",
                min_value=0.0,
                value=float(settings.active_capital),
                step=1000.0,
                help="资金保险丝。0 表示不设固定美元上限，改用右侧的账户比例动态预算。",
            )
        with col3:
            active_capital_pct = st.number_input(
                "账户预算比例 / Equity Budget %",
                min_value=0.0,
                max_value=1.0,
                value=min(1.0, max(0.0, float(getattr(settings, "active_capital_pct", 0.40)))),
                step=0.05,
                format="%.2f",
                help="按当前账户权益给策略多少预算。0.40 表示最多拿账户权益的 40% 做策略资金池。",
            )
        with col4:
            poll_seconds = st.number_input(
                "自动轮询秒数 / Auto Poll Seconds",
                min_value=5,
                max_value=3600,
                value=_env_int("CRYPTO_OFIM_AUTO_POLL_SECONDS", 60),
                step=5,
            )
        benchmark = st.text_input("基准 / Benchmark", value=settings.benchmark)

        if universe.startswith("精简"):
            hot_count = len(DEFAULT_TIGHT_USDT_SYMBOLS)
            symbols = ",".join(DEFAULT_TIGHT_USDT_SYMBOLS)
            st.caption("精简池只跑 BTC / ETH / SOL / BNB / XRP，适合更高频、更低噪声的 OFIM 测试。")
        elif universe.startswith("核心"):
            hot_count = min(settings.hot_count, 10)
            symbols = ",".join(settings.symbols)
            st.caption("核心池默认只跑 BTC / ETH / BNB / SOL / XRP / DOGE / ADA / LINK / AVAX / LTC，适合长时间稳定测试。")
        elif universe.startswith("热门"):
            hot_count = st.slider("热门币数量 / Hot Coin Count", min_value=5, max_value=50, value=min(max(settings.hot_count, 5), 50), step=1)
            symbols = ",".join(settings.symbols)
        else:
            hot_count = settings.hot_count
            symbols = st.text_input("自定义交易对 / Custom Symbols", value=",".join(settings.symbols))

        col4, col5, col6 = st.columns(3)
        with col4:
            depth_limit = st.selectbox("订单簿深度 / Depth", [100, 500, 1000, 5000], index=[100, 500, 1000, 5000].index(settings.depth_limit) if settings.depth_limit in {100, 500, 1000, 5000} else 0)
        with col5:
            lookback_bars = st.number_input("1分钟K线根数 / 1m Bars", min_value=20, max_value=1000, value=int(settings.lookback_bars), step=10)
        with col6:
            trade_limit = st.number_input("逐笔成交条数 / Recent Trades", min_value=20, max_value=1000, value=int(settings.trade_limit), step=10)
        col7, col8, col9 = st.columns(3)
        with col7:
            max_positions = st.number_input("最多同时持币 / Max Positions", min_value=1, max_value=20, value=max(1, min(int(settings.max_positions), 20)), step=1)
        with col8:
            rebalance_threshold = st.number_input(
                "最小调仓比例 / Rebalance Threshold",
                min_value=0.0,
                max_value=0.20,
                value=float(settings.rebalance_threshold),
                step=0.005,
                format="%.3f",
                help="目标仓位变化小于这个比例时不下小碎单。0.08 表示账户权益的 8%，可明显减少来回小调仓。",
            )
        with col9:
            exit_confirm_cycles = st.number_input(
                "空仓确认轮数 / Exit Confirm Cycles",
                min_value=1,
                max_value=10,
                value=max(1, int(settings.exit_confirm_cycles)),
                step=1,
                help="非风险熔断时，连续多少轮没有信号才清仓。4 比 2 更能减少 30-60 秒噪音导致的闪进闪出。",
            )
        col10, col11, col12 = st.columns(3)
        with col10:
            max_position_weight = st.number_input(
                "单币最大仓位 / Max Position Weight",
                min_value=0.05,
                max_value=1.00,
                value=min(1.0, max(0.05, float(settings.max_position_weight))),
                step=0.05,
                format="%.2f",
                help="单个币最多吃掉多少账户权益。0.50 表示最多半仓，避免一个币把账户压满。",
            )
        with col11:
            max_gross_exposure = st.number_input(
                "总仓位上限 / Max Gross Exposure",
                min_value=0.05,
                max_value=1.00,
                value=min(1.0, max(0.05, float(settings.max_gross_exposure))),
                step=0.05,
                format="%.2f",
                help="所有持币加起来最多占策略使用资金多少。0.50 表示最多半仓。",
            )
        with col12:
            max_order_notional = st.number_input(
                "单笔最大金额 / Max Order Notional",
                min_value=0.0,
                max_value=100000.0,
                value=max(0.0, float(settings.max_order_notional)),
                step=500.0,
                help="防止 Testnet 薄盘口被大额市价单打穿。0 表示不限制，不推荐。",
            )
        col11b, col12b, col13b = st.columns(3)
        with col11b:
            min_vol_acceleration = st.number_input(
                "成交量软门槛 / Volume Soft Gate",
                min_value=0.0,
                max_value=3.0,
                value=max(0.0, float(settings.min_vol_acceleration)),
                step=0.05,
                format="%.2f",
                help="低于这个值不会直接禁入，只会扣一点分；1.05 比 1.20 更适合常规高流动性币。",
            )
        with col12b:
            max_holding_seconds = st.number_input(
                "最大持仓秒数 / Max Holding Seconds",
                min_value=0,
                max_value=7200,
                value=max(0, int(settings.max_holding_seconds)),
                step=60,
                help="0 表示关闭。超过后如果信号已消失，跳过等待确认，直接计划平仓，防卡仓。",
            )
        with col13b:
            max_order_book_impact_bps = st.number_input(
                "盘口冲击上限bps / Book Impact Cap",
                min_value=0.0,
                max_value=200.0,
                value=max(0.0, float(settings.max_order_book_impact_bps)),
                step=5.0,
                help="只允许吃到距离当前价这么多 bps 内的盘口。25 bps = 0.25%。",
            )
        col13, col14, col15 = st.columns(3)
        with col13:
            min_trade_interval_seconds = st.number_input(
                "同币冷却秒数 / Same-Coin Cooldown",
                min_value=0,
                max_value=1800,
                value=max(0, int(settings.min_trade_interval_seconds)),
                step=30,
                help="同一个币刚交易后，至少等多久才允许再次交易。建议 60 秒起步，避免刚卖完又立刻买回。",
            )
        with col14:
            min_holding_seconds = st.number_input(
                "最短持仓秒数 / Min Holding Seconds",
                min_value=0,
                max_value=7200,
                value=max(0, int(settings.min_holding_seconds)),
                step=60,
                help="普通再平衡卖出前至少持有多久。risk-off 和 loss guard 减仓不会被这个限制阻止。",
            )
        with col15:
            max_order_book_take_ratio = st.number_input(
                "可吃盘口比例 / Book Take Ratio",
                min_value=0.01,
                max_value=1.00,
                value=min(1.0, max(0.01, float(settings.max_order_book_take_ratio))),
                step=0.05,
                format="%.2f",
                help="不要一次吃完可承接盘口。0.25 表示最多吃可承接盘口的 25%。",
            )
        use_ws_cache = st.toggle(
            "使用实时盘口缓存 / Use WebSocket Market Stream",
            value=bool(settings.use_ws_cache),
            help="打开后，自动策略优先读取本机 WebSocket 盘口/逐笔缓存；缓存不新鲜会自动退回 REST。推荐保持打开。",
        )
        estimate_symbols = (
            int(hot_count) + 1
            if universe.startswith("热门")
            else (
                len(DEFAULT_TIGHT_USDT_SYMBOLS)
                if universe.startswith("精简")
                else (len(DEFAULT_CORE_USDT_SYMBOLS) if universe.startswith("核心") else max(1, len([part for part in symbols.split(",") if part.strip()])))
            )
        )
        estimate_settings = replace(
            settings,
            depth_limit=int(depth_limit),
            trade_limit=int(trade_limit),
            hot_universe=universe.startswith("热门"),
            core_universe=universe.startswith("核心"),
            use_ws_cache=bool(use_ws_cache),
        )
        api_estimate = estimate_crypto_ofim_request_weight(estimate_settings, estimate_symbols)
        st.caption(
            "API 估算 / API Budget: "
            f"每轮约 {api_estimate['cycle_weight']} weight；"
            f"官方当前限制约 {api_estimate['limit_per_minute']}/min；"
            f"建议轮询 >= {api_estimate['safe_poll_seconds']}s。"
        )

        st.markdown("#### Binance Spot Testnet API")
        if mode.startswith("Binance"):
            st.info("当前会使用下面的 Spot Testnet API。它和你的真实 Binance 现货资金隔离。")
        else:
            st.caption("这里可以提前填 API；只有把模式切到 Binance Spot Testnet 后才会实际使用。")
        saved_key = bool(settings.api_key)
        saved_secret = bool(settings.api_secret)
        if saved_key and saved_secret:
            st.success(
                "本机已保存 Spot Testnet API。"
                f"Key: {_mask_secret(settings.api_key)}；Secret: {_mask_secret(settings.api_secret)}。"
                "下面两个框可以留空，系统会继续使用已保存的密钥。"
            )
        else:
            st.warning("本机还没有完整保存 Spot Testnet API Key/Secret。第一次使用需要填一次。")
        api_key = st.text_input(
            "Spot Testnet API Key",
            value="",
            type="password",
            placeholder="如需替换才粘贴新 API Key；留空继续使用已保存 key",
        )
        api_secret = st.text_input(
            "Spot Testnet API Secret",
            value="",
            type="password",
            placeholder="如需替换才粘贴新 Secret；留空继续使用已保存 secret",
        )
        validate_only = st.toggle(
            "只验证订单，不真正提交到 Testnet / Validate Only",
            value=bool(settings.testnet_validate_only),
            help="打开时只走 Binance 测试接口的下单校验；关闭后才会把模拟订单真正提交到 Spot Testnet。",
        )

        submitted = st.form_submit_button(
            "保存设置 / Save Settings",
            width="stretch",
            help="把上面的模式、币种池、API 和风控参数保存到 .env。自动运行下一轮会读取新设置。",
        )
        if submitted:
            values = {
                "CRYPTO_OFIM_MODE": "testnet" if mode.startswith("Binance") else "paper",
                "CRYPTO_OFIM_INITIAL_CASH": str(float(initial_cash)),
                "CRYPTO_OFIM_ACTIVE_CAPITAL": str(float(active_capital)),
                "CRYPTO_OFIM_ACTIVE_CAPITAL_PCT": str(float(active_capital_pct)),
                "CRYPTO_OFIM_BENCHMARK": benchmark.strip().upper().replace("/", ""),
                "CRYPTO_OFIM_SYMBOLS": "HOT_USDT" if universe.startswith("热门") else ("CORE_USDT" if universe.startswith("核心") else ("TIGHT_USDT" if universe.startswith("精简") else symbols)),
                "CRYPTO_OFIM_HOT_UNIVERSE": "true" if universe.startswith("热门") else "false",
                "CRYPTO_OFIM_CORE_UNIVERSE": "true" if universe.startswith("核心") else "false",
                "CRYPTO_OFIM_AUTO_POLL_SECONDS": str(int(poll_seconds)),
                "CRYPTO_OFIM_HOT_COUNT": str(int(hot_count)),
                "CRYPTO_OFIM_DEPTH_LIMIT": str(int(depth_limit)),
                "CRYPTO_OFIM_LOOKBACK_BARS": str(int(lookback_bars)),
                "CRYPTO_OFIM_TRADE_LIMIT": str(int(trade_limit)),
                "CRYPTO_OFIM_MAX_POSITION_WEIGHT": str(float(max_position_weight)),
                "CRYPTO_OFIM_MAX_GROSS_EXPOSURE": str(float(max_gross_exposure)),
                "CRYPTO_OFIM_MAX_POSITIONS": str(int(max_positions)),
                "CRYPTO_OFIM_MAX_ORDER_NOTIONAL": str(float(max_order_notional)),
                "CRYPTO_OFIM_MAX_ORDER_BOOK_IMPACT_BPS": str(float(max_order_book_impact_bps)),
                "CRYPTO_OFIM_MAX_ORDER_BOOK_TAKE_RATIO": str(float(max_order_book_take_ratio)),
                "CRYPTO_OFIM_MIN_VOL_ACCELERATION": str(float(min_vol_acceleration)),
                "CRYPTO_OFIM_REBALANCE_THRESHOLD": str(float(rebalance_threshold)),
                "CRYPTO_OFIM_EXIT_CONFIRM_CYCLES": str(int(exit_confirm_cycles)),
                "CRYPTO_OFIM_MIN_TRADE_INTERVAL_SECONDS": str(int(min_trade_interval_seconds)),
                "CRYPTO_OFIM_MIN_HOLDING_SECONDS": str(int(min_holding_seconds)),
                "CRYPTO_OFIM_MAX_HOLDING_SECONDS": str(int(max_holding_seconds)),
                "CRYPTO_OFIM_USE_WS_CACHE": "true" if use_ws_cache else "false",
                "CRYPTO_OFIM_TESTNET_VALIDATE_ONLY": "true" if validate_only else "false",
                "CRYPTO_OFIM_API_KEY": api_key.strip(),
                "CRYPTO_OFIM_API_SECRET": api_secret.strip(),
            }
            _save_env_values(values)
            st.success("设置已保存。页面会按新设置运行。")
            st.rerun()

    st.session_state["crypto_ofim_poll_seconds"] = int(poll_seconds)


def _render_actions(settings) -> dict[str, Any]:
    st.subheader("操作 / Actions")
    engine = CryptoOfimEngine(settings)
    payload: dict[str, Any] = {}

    def _show_result(ok: bool, message: str) -> None:
        if ok:
            st.success(message)
        else:
            st.warning(message)

    st.caption(
        "日常只用前三个按钮：守护运行负责启动行情流、自动交易和监控；停止全部会安全停掉后台；试算只看信号不下单。"
        "每个按钮右侧的提示会说明它具体做什么。"
    )
    primary = st.columns(3)
    try:
        if primary[0].button(
            "启动守护运行 / Guarded Start",
            width="stretch",
            type="primary",
            help="最推荐的启动方式：同时启动 WebSocket 行情流、自动交易和守护监控。守护会检查自动交易是否卡住，并在需要时重启。",
        ):
            poll_seconds = int(st.session_state.get("crypto_ofim_poll_seconds", 60))
            stream_ok, stream_msg = (False, "行情流未启用")
            if settings.use_ws_cache:
                stream_ok, stream_msg = _start_stream(settings.depth_limit)
            auto_ok, auto_msg = _start_auto(poll_seconds)
            guard_ok, guard_msg = _start_watchdog(poll_seconds)
            if stream_ok or auto_ok or guard_ok:
                st.success(f"{stream_msg}；{auto_msg}；{guard_msg}")
            else:
                st.warning(f"{stream_msg}；{auto_msg}；{guard_msg}")

        if primary[1].button(
            "停止全部 / Stop All",
            width="stretch",
            help="安全停掉自动交易和守护监控。不会撤销已成交订单，也不会重置账本。",
        ):
            guard_ok, guard_msg = _stop_watchdog()
            auto_ok, auto_msg = _stop_auto()
            stream_ok, stream_msg = _stop_stream()
            if auto_ok or guard_ok or stream_ok:
                st.success(f"{auto_msg}；{guard_msg}；{stream_msg}")
            else:
                st.warning(f"{auto_msg}；{guard_msg}；{stream_msg}")

        if primary[2].button(
            "试算一次 / Dry Run",
            width="stretch",
            help="只跑一轮行情读取和 OFIM 打分，生成计划但不提交订单。适合先检查策略现在想干什么。",
        ):
            payload = engine.run_once(submit=False)
            st.success("已完成试算，没有下单。")

        with st.expander("诊断与高级操作 / Diagnostics & Advanced", expanded=False):
            st.caption("这里的按钮不需要日常使用。连接检查用于排查 API；单次下单会按当前模式真实提交到本地模拟或 Binance Testnet。")
            diagnostic = st.columns(2)
            if diagnostic[0].button(
                "连接检查 / Check Connection",
                width="stretch",
                help="检查当前模式能否连上 Binance，并读取基础账户/行情数据。不下单。",
            ):
                payload = engine.check()
                st.success("Binance 连接正常。")
            if diagnostic[1].button(
                "模拟下单一次 / Submit Once",
                width="stretch",
                help="按当前策略计划提交一次订单。本地模拟会写本地账本；Spot Testnet 会提交到 Binance 测试网。谨慎使用。",
            ):
                payload = engine.run_once(submit=True)
                st.success("已按当前模式提交：本地模拟或 Spot Testnet。")

            st.divider()
            st.caption("组件级控制只用于排查问题。正常启动请用上面的“启动守护运行”。")
            advanced = st.columns(4)
            if advanced[0].button(
                "只启动自动 / Auto Only",
                width="stretch",
                help="只启动自动交易进程，不启动守护。排查守护问题时才用。",
            ):
                ok, message = _start_auto(int(st.session_state.get("crypto_ofim_poll_seconds", 60)))
                _show_result(ok, message)
            if advanced[1].button(
                "只启动守护 / Watchdog Only",
                width="stretch",
                help="只启动守护监控。它会检查自动交易是否存在和是否卡住。",
            ):
                ok, message = _start_watchdog(int(st.session_state.get("crypto_ofim_poll_seconds", 60)))
                _show_result(ok, message)
            if advanced[2].button(
                "只停止自动 / Stop Auto Only",
                width="stretch",
                help="只停自动交易，不停守护。一般不建议单独使用，因为守护可能会把自动交易重新拉起。",
            ):
                ok, message = _stop_auto()
                _show_result(ok, message)
            if advanced[3].button(
                "只停止守护 / Stop Watchdog Only",
                width="stretch",
                help="只停守护监控，自动交易继续运行。排查守护误重启时使用。",
            ):
                ok, message = _stop_watchdog()
                _show_result(ok, message)

            stream_controls = st.columns(2)
            if stream_controls[0].button(
                "只启动行情流 / Stream Only",
                width="stretch",
                help="只启动 WebSocket 盘口/逐笔行情缓存，不启动策略。用于排查 REST 超时或盘口数据。",
            ):
                ok, message = _start_stream(settings.depth_limit)
                _show_result(ok, message)
            if stream_controls[1].button(
                "只停止行情流 / Stop Stream Only",
                width="stretch",
                help="只停 WebSocket 行情缓存。策略会自动退回 REST，不会直接停止自动交易。",
            ):
                ok, message = _stop_stream()
                _show_result(ok, message)

            st.divider()
            st.markdown("##### 危险操作 / Dangerous")
            st.warning("这里会改变账本或 Testnet 持仓。除非你明确要重新开始实验，否则不要点。")
            if settings.mode == "testnet":
                st.caption(
                    "全换 USDT 会先停自动交易和守护，然后把可卖的非 USDT 测试币卖成 USDT，最后设置新的账本起点。"
                    "历史审计文件仍保留，但首页盈亏从新起点重新计算。"
                )
                confirm_liquidation = st.checkbox(
                    "我确认要操作 Binance Spot Testnet，不是实盘 / I confirm this is Testnet",
                    help="防止误点。当前程序不支持 crypto 实盘，但这里仍然要求确认，因为它会真实改变 Testnet 账户余额。",
                )
                liquidation_cols = st.columns(2)
                if liquidation_cols[0].button(
                    "试算全换 USDT / Plan USDT Sweep",
                    width="stretch",
                    help="只读取当前 Testnet 余额，计算哪些币可以卖成 USDT；不会提交订单。",
                ):
                    payload = engine.liquidate_testnet_to_quote(submit=False, reset_epoch=False)
                    st.success(f"试算完成：可卖 {payload.get('planned_count', 0)} 个币，跳过 {payload.get('skipped_count', 0)} 个。")
                    if payload.get("planned"):
                        st.dataframe(pd.DataFrame(payload["planned"]), width="stretch", hide_index=True)
                if liquidation_cols[1].button(
                    "全换 USDT + 重开账本 / Sweep + New Ledger",
                    width="stretch",
                    type="primary",
                    disabled=not confirm_liquidation,
                    help="提交 Testnet 卖出订单，尽量把非 USDT 余额换成 USDT，并从完成后设置新的盈亏起点。",
                ):
                    auto_ok, auto_msg = _stop_auto()
                    guard_ok, guard_msg = _stop_watchdog()
                    payload = engine.liquidate_testnet_to_quote(submit=True, reset_epoch=True)
                    st.success(
                        f"{auto_msg}；{guard_msg}；已提交 {payload.get('submitted_count', 0)} 个清仓订单，"
                        f"跳过 {payload.get('skipped_count', 0)} 个；新账本起点 {payload.get('epoch', {}).get('ts', '-')}"
                    )
                    if payload.get("submitted"):
                        st.dataframe(pd.DataFrame(payload["submitted"]), width="stretch", hide_index=True)
            if st.button(
                "重置本地账本 / Reset Paper",
                width="stretch",
                help="只重置本地模拟账本为初始现金；不会改 Binance Testnet 账户，也不会撤销历史真实测试网订单。",
            ):
                state = reset_crypto_ofim_paper(settings)
                st.success(f"本地模拟账本已重置为 {state.cash:,.2f} {settings.quote_asset}。")
                payload = read_crypto_ofim_status()

    except (CryptoOfimError, ValueError, OSError) as exc:
        if _is_transient_network_message(str(exc)):
            st.warning("Binance Spot Testnet 临时网络超时，系统会在下一轮自动重试。")
            with st.expander("原始错误 / Raw Error", expanded=False):
                st.code(str(exc))
        else:
            st.error(str(exc))
        payload = read_crypto_ofim_status()
    return payload or read_crypto_ofim_status()


def _watchdog_banner_state(
    *,
    auto_running: bool,
    watchdog_pid: int | None,
    watchdog_running: bool,
    watchdog: dict[str, Any],
) -> dict[str, str]:
    health = str(watchdog.get("health") or "not_started")
    detail = str(watchdog.get("detail") or "-")
    if watchdog_running:
        return {
            "class": "status-good" if health in {"healthy", "starting"} else "status-warn",
            "running": f"running PID={watchdog_pid}",
            "health": health,
            "detail": detail,
        }
    if auto_running:
        return {
            "class": "status-warn",
            "running": "stopped",
            "health": health,
            "detail": detail if detail != "-" else "auto is running without watchdog",
        }
    return {
        "class": "status-good",
        "running": "standby",
        "health": "ok",
        "detail": "auto stopped; watchdog not needed",
    }


def _render_status(settings, payload: dict[str, Any]) -> None:
    pid = _read_pid()
    running = _pid_running(pid)
    watchdog_pid = _read_pid_file(WATCHDOG_PID_FILE)
    watchdog_running = _pid_running(watchdog_pid)
    watchdog = read_crypto_ofim_watchdog_status()
    stream = read_crypto_ofim_stream_status()
    stream_running = bool(stream.get("running"))
    status = payload.get("status", "not_started")
    mode = payload.get("mode", settings.mode)
    market_data = payload.get("market_data_label") or settings.market_data_label
    execution_target = payload.get("submit_label") or settings.submit_label
    status_class = "status-good" if status not in {"error", "not_started", "transient_error"} else "status-warn"
    st.markdown(
        f"<div class='{status_class}'>状态 / Status: {status} | 模式 / Mode: {mode} | 自动运行 / Auto: {'running PID=' + str(pid) if running else 'stopped'}</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        f"信号行情 / Signal Market Data: {market_data} · 下单执行 / Execution: {execution_target}"
    )
    guard_state = _watchdog_banner_state(
        auto_running=running,
        watchdog_pid=watchdog_pid,
        watchdog_running=watchdog_running,
        watchdog=watchdog,
    )
    st.markdown(
        "<div class='{klass}'>守护监控 / Watchdog: {running} | 健康 / Health: {health} | "
        "动作 / Action: {action} | 说明 / Detail: {detail} | 重启次数 / Restarts: {restarts}</div>".format(
            klass=guard_state["class"],
            running=guard_state["running"],
            health=guard_state["health"],
            action=watchdog.get("action", "-"),
            detail=guard_state["detail"],
            restarts=watchdog.get("restart_count", 0),
        ),
        unsafe_allow_html=True,
    )
    stream_health = "healthy" if stream_running and stream.get("status") in {"running", "connecting"} else "stopped"
    stream_class = "status-good" if settings.use_ws_cache and stream_health == "healthy" else ("status-warn" if settings.use_ws_cache else "status-good")
    cache_age = stream.get("cache_age_seconds")
    cache_age_text = f"{float(cache_age):.1f}s" if isinstance(cache_age, (int, float)) else "-"
    st.markdown(
        "<div class='{klass}'>行情流 / Market Stream: {running} | 状态 / Status: {status} | "
        "数据源 / Source: {source} | 缓存年龄 / Cache Age: {age} | 盘口消息 / Market Messages: {messages} | "
        "账户流 / User Stream: {user_status} | 账户事件 / User Events: {user_events}</div>".format(
            klass=stream_class,
            running=f"running PID={stream.get('pid')}" if stream_running else "stopped",
            status=stream.get("status", "not_started"),
            source=stream.get("market_data", settings.market_data),
            age=cache_age_text,
            messages=stream.get("message_count", 0),
            user_status=stream.get("user_stream_status", "disabled"),
            user_events=stream.get("user_event_count", 0),
        ),
        unsafe_allow_html=True,
    )
    if settings.use_ws_cache and not stream_running:
        st.warning("实时盘口缓存已启用但行情流没在跑；策略会自动退回 REST。点“启动守护运行”会一起拉起行情流。")
    if payload.get("error"):
        if status == "transient_error" or _is_transient_network_message(str(payload.get("raw_error") or payload["error"])):
            st.warning(payload["error"])
            if payload.get("raw_error"):
                with st.expander("原始网络错误 / Raw Network Error", expanded=False):
                    st.code(str(payload["raw_error"]))
        else:
            st.error(payload["error"])
    if running and status in {"planned", "submitted"} and not payload.get("target_weights"):
        benchmark_score = payload.get("benchmark_score")
        reason = "当前没有目标仓位，策略选择空仓等待。"
        if benchmark_score is not None:
            reason += f" 基准分数 / Benchmark Score={float(benchmark_score):+.4f}。"
        trend = payload.get("benchmark_trend") or {}
        if trend.get("reason") == "benchmark_below_sma":
            reason += (
                f" 大盘过滤：{settings.benchmark} 当前价 {trend.get('last_price')} "
                f"低于 {trend.get('window')} 根1分钟均线 {trend.get('sma')}，暂停新开多。"
            )
        elif str(trend.get("reason") or "").startswith("loss_guard"):
            reason += (
                " 账户熔断：当前账本周期亏损/手续费/交易次数已经超过阈值，"
                "系统只允许平仓，不允许再开新仓。"
                f" 净亏={trend.get('primary_net_pnl', '-')}; "
                f"估算手续费={trend.get('estimated_fees_paid', '-')}; "
                f"成交数={trend.get('trade_count', '-')}."
            )
        st.info(reason + "这不是程序停掉；只是不满足进场条件或风险过滤打开了。")

    account = payload.get("account") or {}
    if not account:
        try:
            account = CryptoOfimEngine(settings).account_snapshot()
        except Exception:
            account = {}

    quote = settings.quote_asset
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        _card("起始资金 / Start Capital", _fmt_money(account.get("starting_equity", settings.initial_cash), quote), "本轮重开账本时的起点")
    with col2:
        _card("账户权益 / Account Equity", _fmt_money(account.get("primary_equity", account.get("equity", settings.initial_cash)), quote), "按 Binance 官方费率调整后")
    with col3:
        _card("净盈亏 / Net PnL", _fmt_money(account.get("primary_net_pnl", account.get("net_pnl", 0)), quote), "主口径：按 Binance 官方费率调整后")
    with col4:
        _card("收益率 / Return", _fmt_pct(account.get("primary_net_return_pct", account.get("net_return_pct", 0))), "主口径：按 Binance 官方费率调整后")

    col5, col6, col7, col8 = st.columns(4)
    with col5:
        budget_note = (
            f"按账户 {getattr(settings, 'active_capital_pct', 0.0):.0%} 动态预算"
            if getattr(settings, "active_capital_pct", 0.0) > 0
            else "按绝对上限预算"
        )
        _card("策略资金预算 / Strategy Budget", _fmt_money(account.get("active_capital", settings.active_capital), quote), budget_note)
    with col6:
        _card(f"{quote} 现金 / Cash", _fmt_money(account.get("cash", 0), quote), "账户里可用稳定币")
    with col7:
        _card("策略持币市值 / Strategy Coins", _fmt_money(account.get("market_value", 0), quote), "只统计本策略当前持仓")
    with col8:
        _card("浮动盈亏 / Unrealized", _fmt_money(account.get("unrealized_pnl", 0), quote), "当前还持有的币")

    col9, col10, col11, col12 = st.columns(4)
    with col9:
        _card("已实现净盈亏 / Realized Net", _fmt_money(account.get("realized_pnl_after_estimated_fees", account.get("realized_pnl", 0)), quote), "按 Binance 官方费率调整后")
    with col10:
        fee_value = account.get("estimated_fees_paid", account.get("fees_paid", 0))
        _card("官方费率估算 / Official Fees", _fmt_money(fee_value, quote), "按 Binance 官方费率模型估算")
    with col11:
        _card("成交笔数 / Trades", f"{int(account.get('trade_count') or 0)}", "Testnet 已成交记录")
    with col12:
        _card("可用策略现金 / Strategy Cash", _fmt_money(account.get("strategy_available_cash", 0), quote), "按资金上限还能动用多少")
    st.caption(
        f"风控尺寸：目标总仓位 {_fmt_pct(payload.get('exposure', 0))}；"
        f"单笔最大 {settings.max_order_notional:,.0f} {quote}；"
        f"盘口冲击上限 {settings.max_order_book_impact_bps:g} bps。"
    )
    trend = payload.get("benchmark_trend") or {}
    if trend:
        st.caption(
            f"大盘过滤 / Benchmark Filter: {settings.benchmark} "
            f"last={trend.get('last_price', '-')}，"
            f"SMA{trend.get('window', '-')}={trend.get('sma', '-')}，"
            f"状态={trend.get('reason', '-')}"
        )
    if settings.mode == "testnet" and account.get("extra_balance_count"):
        st.caption(
            "说明：Binance Spot Testnet 会给很多测试币。本页总权益只看 "
            f"{quote} 现金 + OFIM 策略当前持仓；其它测试币不计入策略权益，"
            f"当前检测到 {int(account.get('extra_balance_count') or 0)} 种非策略测试币余额。"
        )
    if settings.mode == "testnet":
        fee_source = account.get("estimated_fee_source") or "unknown"
        st.caption(
            "费用说明：Binance Spot Testnet 成交回报里的 commission 可能为 0；"
            f"本页额外按 Binance 官方费率模型估算真实交易费用。来源={fee_source}。"
        )
    api_budget = payload.get("api_budget") or estimate_crypto_ofim_request_weight(settings)
    st.caption(
        "API 强度 / API Load: "
        f"本配置预计每轮约 {api_budget.get('cycle_weight', 0)} request weight；"
        f"当前 Binance 返回的分钟上限按 {api_budget.get('limit_per_minute', 6000)} 估算。"
    )
    stale_count = int(account.get("stale_position_count") or 0)
    if stale_count:
        st.warning(
            f"检测到 {stale_count} 个持仓超过最大持仓时间。下一轮信号如果已经消失，系统会优先计划平仓，防止卡仓。"
        )


def _render_balance_audit(settings) -> None:
    if settings.mode != "testnet":
        st.info("本地模拟盘没有 Binance 测试网赠送币问题。")
        return
    refresh = st.button(
        "刷新账户币种审计 / Refresh Balance Audit",
        width="stretch",
        help="重新读取 Binance Testnet 账户里的所有非零币种，区分策略会管的币和测试网赠送但策略忽略的币。",
    )
    if refresh or "crypto_ofim_balance_audit" not in st.session_state:
        try:
            st.session_state["crypto_ofim_balance_audit"] = CryptoOfimEngine(settings).balance_audit()
        except Exception as exc:
            st.warning(f"暂时无法读取 Binance 余额审计：{exc}")

    audit = st.session_state.get("crypto_ofim_balance_audit") or {}
    rows = audit.get("rows") or []
    summary = audit.get("summary") or {}
    if not rows:
        st.info("还没有账户币种审计数据。")
        return

    st.caption(
        "这张表把 Binance Testnet 账户里的币分成三类：当前策略池、历史被策略交易过、测试网赠送但策略不管的杂币。"
        "策略权益只统计 USDT 现金和 strategy_counted_qty，不把没用测试币混进盈亏。"
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("当前非零币种 / Nonzero Assets", int(summary.get("current_nonzero_assets") or 0))
    c2.metric("当前策略池 / Active Pool", int(summary.get("active_universe_count") or 0))
    c3.metric("历史交易过 / Traded", int(summary.get("historically_traded_count") or 0))
    c4.metric("没用测试币 / Unused Testnet", int(summary.get("testnet_unused_count") or 0))

    role_filter = st.radio(
        "筛选 / Filter",
        ["全部 / All", "当前策略池 / Active", "历史交易过 / Traded", "没用测试币 / Unused"],
        horizontal=True,
    )
    frame = pd.DataFrame(rows)
    if role_filter.startswith("当前"):
        frame = frame[frame["role"].eq("ACTIVE_UNIVERSE")]
    elif role_filter.startswith("历史"):
        frame = frame[frame["role"].eq("HISTORICALLY_TRADED")]
    elif role_filter.startswith("没用"):
        frame = frame[frame["role"].eq("TESTNET_UNUSED")]

    show_cols = [
        "role",
        "asset",
        "symbol",
        "inferred_start_qty",
        "current_qty",
        "change_from_ofim_orders",
        "strategy_counted_qty",
        "ignored_testnet_qty",
    ]
    st.dataframe(frame[[col for col in show_cols if col in frame.columns]], width="stretch", hide_index=True)
    st.download_button(
        "下载完整币种审计 CSV / Download Balance Audit CSV",
        data=pd.DataFrame(rows).to_csv(index=False).encode("utf-8"),
        file_name="crypto_ofim_balance_audit.csv",
        mime="text/csv",
        width="stretch",
        help="把当前币种审计表下载成 CSV，方便以后核对初始持仓、当前余额和策略是否计入。",
    )


def _render_learning_lab(settings) -> None:
    st.subheader("Evidence-to-Review 学习闭环")
    st.caption(
        "这里只生成证据、归因和候选研究建议；不会自动改代码，也不会自动修改 live/testnet 策略参数。"
    )
    controls = st.columns([1, 1, 3])
    with controls[0]:
        if st.button("重建学习包 / Rebuild", width="stretch", key="rebuild-crypto-learning"):
            result = run_learning_pipeline(mode=settings.mode, quote_asset=settings.quote_asset, settings=settings)
            st.success(f"已生成 review packet: {result.review_packet_path}")
    with controls[1]:
        if st.button("刷新 / Refresh", width="stretch", key="refresh-crypto-learning"):
            st.rerun()
    controls[2].info(f"Review packet: {CRYPTO_LEARNING_REVIEW_PACKET_FILE}")

    report = load_learning_report(CRYPTO_ATTRIBUTION_FILE)
    packet = load_learning_review_packet(CRYPTO_LEARNING_REVIEW_PACKET_JSON_FILE)
    candidates = load_upgrade_candidates(CRYPTO_UPGRADE_CANDIDATES_FILE, tail=200)
    promotion = load_promotion_report(CRYPTO_PROMOTION_REPORT_FILE)
    total = dict(report.get("total") or {})
    metric_cols = st.columns(5)
    metric_cols[0].metric("已实现回合 / Outcomes", int(total.get("trades") or 0))
    metric_cols[1].metric("胜率 / Win Rate", _fmt_pct(total.get("win_rate", 0.0)))
    metric_cols[2].metric("净 PnL / Net", _fmt_money(total.get("net_pnl", 0.0), settings.quote_asset))
    metric_cols[3].metric("滑点 / Avg Slip", f"{float(total.get('avg_slippage_bps', 0.0) or 0.0):.2f} bps")
    metric_cols[4].metric("候选 / Candidates", len(candidates))

    st.info(
        "安全门禁 / Gate: live_auto_promotion=false, code_auto_modification=false。"
        f" Packet ID: {packet.get('packet_id', 'none')}"
    )
    if report:
        tabs = st.tabs(["归因 / Attribution", "候选 / Candidates", "门禁 / Gate", "Artifacts"])
        with tabs[0]:
            by_symbol = pd.DataFrame([{"symbol": key, **value} for key, value in dict(report.get("by_symbol") or {}).items()])
            by_reason = pd.DataFrame([{"reason": key, **value} for key, value in dict(report.get("by_reason") or {}).items()])
            by_venue = pd.DataFrame([{"venue": key, **value} for key, value in dict(report.get("by_venue") or {}).items()])
            sub = st.tabs(["Symbol", "Reason", "Venue"])
            with sub[0]:
                st.dataframe(by_symbol, width="stretch", hide_index=True)
            with sub[1]:
                st.dataframe(by_reason, width="stretch", hide_index=True)
            with sub[2]:
                st.dataframe(by_venue, width="stretch", hide_index=True)
        with tabs[1]:
            if candidates:
                st.dataframe(pd.DataFrame(candidates), width="stretch", hide_index=True)
            else:
                st.info("暂无候选。先积累更多 filled outcomes。")
        with tabs[2]:
            decisions = pd.DataFrame(promotion.get("decisions") or [])
            st.dataframe(decisions, width="stretch", hide_index=True)
            st.json(promotion.get("policy") or {})
        with tabs[3]:
            artifacts = pd.DataFrame(
                [
                    {"artifact": name, **dict(meta or {})}
                    for name, meta in dict(packet.get("artifacts") or {}).items()
                ]
            )
            st.dataframe(artifacts, width="stretch", hide_index=True)
            st.caption(f"Markdown: {CRYPTO_LEARNING_REVIEW_PACKET_FILE}")
            st.caption(f"JSON: {CRYPTO_LEARNING_REVIEW_PACKET_JSON_FILE}")
    else:
        st.info("还没有学习报告。点击上方按钮，或运行 `.venv/bin/taa-futu crypto-learning-build`。")

    st.divider()
    st.subheader("Research Replay / 自动研究回测")
    research = read_crypto_research_status()
    validation = research.get("best_validation") or {}
    locked = research.get("locked_test") or {}
    research_cols = st.columns(5)
    research_cols[0].metric("Trials", int(research.get("trial_count") or 0))
    research_cols[1].metric("Best", str(research.get("best_profile") or "none"))
    research_cols[2].metric("Validation Net", _fmt_money(validation.get("net_pnl", 0.0), settings.quote_asset))
    research_cols[3].metric("Locked Net", _fmt_money(locked.get("net_pnl", 0.0), settings.quote_asset))
    research_cols[4].metric("Locked Gate", "PASS" if research.get("passed_locked_test") else "NO")
    st.caption(
        "这里是研究回放结果，只读展示；不会启动自动交易、不会修改 `.env`，也不会自动上线策略。"
    )
    with st.expander("Research Artifacts / 研究文件", expanded=False):
        st.caption(f"Trials: {CRYPTO_RESEARCH_TRIALS_FILE}")
        st.caption(f"Best candidate: {CRYPTO_RESEARCH_BEST_CANDIDATE_FILE}")
        st.caption(f"Locked test: {CRYPTO_RESEARCH_LOCKED_TEST_REPORT_FILE}")
        st.caption(f"Patch report: {CRYPTO_RESEARCH_PATCH_REPORT_FILE}")
        if CRYPTO_RESEARCH_PATCH_REPORT_FILE.exists():
            st.code(CRYPTO_RESEARCH_PATCH_REPORT_FILE.read_text(encoding="utf-8")[:4000], language="markdown")

    with st.expander("原始学习日志 / Raw Learning Artifacts", expanded=False):
        raw_tabs = st.tabs(["Order Memory", "Outcomes", "Candidates"])
        with raw_tabs[0]:
            st.dataframe(_jsonl_frame(CRYPTO_ORDER_MEMORY_FILE, tail=200), width="stretch", hide_index=True)
        with raw_tabs[1]:
            st.dataframe(_jsonl_frame(CRYPTO_TRADE_OUTCOMES_FILE, tail=200), width="stretch", hide_index=True)
        with raw_tabs[2]:
            st.dataframe(_jsonl_frame(CRYPTO_UPGRADE_CANDIDATES_FILE, tail=200), width="stretch", hide_index=True)


def _render_perp_long_short_lab() -> None:
    st.subheader("USD-M 合约做多/做空")
    st.caption(
        "这是独立的 Binance USD-M Futures 袖子：信号用主网公开合约行情，执行只允许本地合约纸账本或 Binance Futures Testnet；不会改 Spot OFIM。"
    )
    try:
        perp_settings = load_crypto_perp_settings(ENV_FILE)
    except Exception as exc:
        st.error(f"合约配置读取失败：{exc}")
        return

    with st.expander("合约设置 / Perp Settings", expanded=False):
        with st.form("crypto_perp_settings"):
            mode = st.radio(
                "执行模式 / Execution Mode",
                ["Binance USD-M Futures Testnet", "Local Perp Paper"],
                index=0 if perp_settings.mode == "testnet" else 1,
                horizontal=True,
                help="Testnet 会走 Binance 官方 USD-M Futures 测试网；Local Perp Paper 只写本地 signed 账本。",
            )
            symbols = st.text_input("合约池 / Symbols", value=",".join(perp_settings.symbols))
            benchmark = st.text_input("基准 / Benchmark", value=perp_settings.benchmark)
            c1, c2, c3, c4 = st.columns(4)
            active_capital_pct = c1.number_input("资金比例 / Active Capital %", min_value=0.0, max_value=1.0, value=float(perp_settings.active_capital_pct), step=0.05)
            max_abs_weight = c2.number_input("单合约最大权重 / Max Abs Weight", min_value=0.01, max_value=1.0, value=float(perp_settings.max_abs_position_weight), step=0.01)
            max_gross = c3.number_input("总敞口上限 / Max Gross", min_value=0.01, max_value=2.0, value=float(perp_settings.max_gross_exposure), step=0.05)
            max_order = c4.number_input("单笔名义上限 / Max Order", min_value=5.0, max_value=100000.0, value=float(perp_settings.max_order_notional), step=100.0)
            r1, r2, r3, r4, r5 = st.columns(5)
            entry_threshold = r1.number_input("进场阈值 / Entry", min_value=0.05, max_value=1.0, value=float(perp_settings.entry_threshold), step=0.01)
            exit_threshold = r2.number_input("退出阈值 / Exit", min_value=0.0, max_value=1.0, value=float(perp_settings.exit_threshold), step=0.01)
            exit_confirm_cycles = r3.number_input("退出确认 / Exit Confirm", min_value=1, max_value=12, value=int(perp_settings.exit_confirm_cycles), step=1)
            signal_confirm_cycles = r4.number_input("信号确认 / Signal Confirm", min_value=1, max_value=12, value=int(perp_settings.signal_confirm_cycles), step=1)
            max_positions = r5.number_input("最多合约 / Max Positions", min_value=1, max_value=20, value=int(perp_settings.max_positions), step=1)
            p1, p2, p3, p4 = st.columns(4)
            min_trade_interval = p1.number_input(
                "最短交易间隔 / Min Interval",
                min_value=0,
                max_value=3600,
                value=int(perp_settings.min_trade_interval_seconds),
                step=30,
            )
            leverage = p2.number_input("杠杆 / Leverage", min_value=1, max_value=3, value=int(perp_settings.leverage), step=1)
            margin_type = p3.selectbox("保证金 / Margin", ["ISOLATED", "CROSSED"], index=0 if perp_settings.margin_type == "ISOLATED" else 1)
            validate_only = p4.toggle("只校验 / Validate Only", value=bool(perp_settings.testnet_validate_only))
            q1, q2, q3 = st.columns(3)
            max_take_ratio = q1.number_input("盘口吃单比例 / Book Take", min_value=0.01, max_value=1.0, value=float(perp_settings.max_order_book_take_ratio), step=0.01)
            max_adverse_funding = q2.number_input("逆向 Funding 上限", min_value=0.0, max_value=0.01, value=float(perp_settings.max_adverse_funding_rate), step=0.0001, format="%.5f")
            maintenance_margin = q3.number_input("维护保证金估算", min_value=0.0, max_value=0.10, value=float(perp_settings.maintenance_margin_rate), step=0.001, format="%.4f")
            e1, e2, e3 = st.columns(3)
            require_edge = e1.toggle("成本闸门 / Cost Gate", value=bool(perp_settings.require_edge_over_cost))
            edge_bps_per_score = e2.number_input("每分预期 bps", min_value=0.0, max_value=200.0, value=float(perp_settings.edge_bps_per_score), step=5.0)
            cost_buffer_bps = e3.number_input("成本缓冲 bps", min_value=0.0, max_value=100.0, value=float(perp_settings.cost_buffer_bps), step=1.0)
            x1, x2, x3, x4 = st.columns(4)
            order_style = x1.selectbox("下单方式 / Order Style", ["maker_limit", "market"], index=0 if perp_settings.order_style == "maker_limit" else 1)
            maker_fee_rate = x2.number_input("Maker 费率", min_value=0.0, max_value=0.01, value=float(perp_settings.maker_fee_rate), step=0.0001, format="%.5f")
            maker_ttl = x3.number_input("Maker TTL", min_value=30, max_value=3600, value=int(perp_settings.maker_order_ttl_seconds), step=30)
            maker_offset = x4.number_input("Maker 偏移 bps", min_value=0.0, max_value=20.0, value=float(perp_settings.maker_price_offset_bps), step=0.5)
            api_key = st.text_input(
                "Futures Testnet API Key",
                value="",
                placeholder=_mask_secret(perp_settings.api_key),
                help="留空表示继续用已保存的 CRYPTO_PERP_API_KEY。",
            )
            api_secret = st.text_input(
                "Futures Testnet API Secret",
                value="",
                type="password",
                placeholder="留空继续使用已保存 Secret",
            )
            if st.form_submit_button("保存合约设置 / Save Perp Settings", width="stretch"):
                _save_env_values(
                    {
                        "CRYPTO_PERP_MODE": "testnet" if mode.startswith("Binance") else "paper",
                        "CRYPTO_PERP_SYMBOLS": symbols.strip().upper().replace("/", ""),
                        "CRYPTO_PERP_BENCHMARK": benchmark.strip().upper().replace("/", ""),
                        "CRYPTO_PERP_ACTIVE_CAPITAL_PCT": str(float(active_capital_pct)),
                        "CRYPTO_PERP_MAX_ABS_POSITION_WEIGHT": str(float(max_abs_weight)),
                        "CRYPTO_PERP_MAX_GROSS_EXPOSURE": str(float(max_gross)),
                        "CRYPTO_PERP_MAX_POSITIONS": str(int(max_positions)),
                        "CRYPTO_PERP_MAX_ORDER_NOTIONAL": str(float(max_order)),
                        "CRYPTO_PERP_ENTRY_THRESHOLD": str(float(entry_threshold)),
                        "CRYPTO_PERP_EXIT_THRESHOLD": str(float(exit_threshold)),
                        "CRYPTO_PERP_EXIT_CONFIRM_CYCLES": str(int(exit_confirm_cycles)),
                        "CRYPTO_PERP_SIGNAL_CONFIRM_CYCLES": str(int(signal_confirm_cycles)),
                        "CRYPTO_PERP_MIN_TRADE_INTERVAL_SECONDS": str(int(min_trade_interval)),
                        "CRYPTO_PERP_LEVERAGE": str(int(leverage)),
                        "CRYPTO_PERP_MARGIN_TYPE": str(margin_type),
                        "CRYPTO_PERP_TESTNET_VALIDATE_ONLY": "true" if validate_only else "false",
                        "CRYPTO_PERP_MAX_ORDER_BOOK_TAKE_RATIO": str(float(max_take_ratio)),
                        "CRYPTO_PERP_MAX_ADVERSE_FUNDING_RATE": str(float(max_adverse_funding)),
                        "CRYPTO_PERP_MAINTENANCE_MARGIN_RATE": str(float(maintenance_margin)),
                        "CRYPTO_PERP_REQUIRE_EDGE_OVER_COST": "true" if require_edge else "false",
                        "CRYPTO_PERP_EDGE_BPS_PER_SCORE": str(float(edge_bps_per_score)),
                        "CRYPTO_PERP_COST_BUFFER_BPS": str(float(cost_buffer_bps)),
                        "CRYPTO_PERP_ORDER_STYLE": str(order_style),
                        "CRYPTO_PERP_MAKER_FEE_RATE": str(float(maker_fee_rate)),
                        "CRYPTO_PERP_MAKER_ORDER_TTL_SECONDS": str(int(maker_ttl)),
                        "CRYPTO_PERP_MAKER_PRICE_OFFSET_BPS": str(float(maker_offset)),
                        "CRYPTO_PERP_API_KEY": api_key.strip(),
                        "CRYPTO_PERP_API_SECRET": api_secret.strip(),
                    }
                )
                st.success("合约设置已保存。")
                st.rerun()

    status_payload = read_crypto_perp_status()
    action_payload: dict[str, Any] = {}
    engine = CryptoPerpEngine(perp_settings)
    controls = st.columns(4)
    try:
        if controls[0].button("合约试算 / Perp Dry Run", width="stretch", help="生成 long/short signed target 和计划订单，不提交。"):
            action_payload = engine.run_once(submit=False)
            st.success("合约试算完成，没有提交订单。")
        if controls[1].button("合约提交一次 / Perp Submit Once", width="stretch", help="按当前合约计划提交一次。本地模式写纸账本；Testnet 模式走 Binance USD-M Futures Testnet。"):
            action_payload = engine.run_once(submit=True)
            st.success("合约订单已按当前模式提交。")
        if controls[2].button("刷新合约账户 / Refresh Perp", width="stretch"):
            action_payload = {"account": engine.account_snapshot(), "market_regime": engine.market_regime(), "status": "refreshed", "mode": perp_settings.mode}
        if controls[3].button("重置合约本地账本 / Reset Perp Paper", width="stretch", help="只重置本地合约纸账本，不会影响 Binance Testnet。"):
            state = reset_crypto_perp_paper(perp_settings)
            action_payload = {"status": "reset", "mode": perp_settings.mode, "account": engine.account_snapshot(state)}
            st.success(f"合约本地账本已重置为 {state.cash:,.2f} {perp_settings.quote_asset}。")
    except (CryptoPerpError, ValueError, OSError) as exc:
        st.error(str(exc))

    payload = action_payload or status_payload
    account = payload.get("account") or {}
    if not account:
        try:
            account = engine.account_snapshot()
        except Exception:
            account = {}
    quote = perp_settings.quote_asset
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("模式 / Mode", perp_settings.mode)
    m2.metric("合约权益 / Equity", _fmt_money(account.get("equity", perp_settings.initial_cash), quote))
    m3.metric("净盈亏 / Net PnL", _fmt_money(account.get("net_pnl", 0), quote))
    m4.metric("浮盈亏 / Unrealized", _fmt_money(account.get("unrealized_pnl", 0), quote))
    m5.metric("Funding / Funding Paid", _fmt_money(account.get("funding_paid", 0), quote))
    st.caption(
        f"执行 / Execution: {perp_settings.submit_label}；"
        f"行情 / Market Data: {perp_settings.market_data_label}；"
        f"杠杆 {perp_settings.leverage}x，保证金 {perp_settings.margin_type}，"
        f"进场 {perp_settings.entry_threshold:g}x{perp_settings.signal_confirm_cycles}，退出 {perp_settings.exit_threshold:g}x{perp_settings.exit_confirm_cycles}，"
        f"成本闸门 {'开' if perp_settings.require_edge_over_cost else '关'}，"
        f"下单 {perp_settings.order_style}，"
        f"最多 {perp_settings.max_positions} 个合约，"
        f"签名账户 {'已配置' if perp_settings.signed_account_enabled else '未配置'}。"
    )
    watchdog = read_crypto_ofim_watchdog_status()
    if watchdog.get("perp_enabled"):
        st.caption(
            "合约自动运行 / Perp Auto: "
            f"{'running PID=' + str(watchdog.get('perp_pid')) if watchdog.get('perp_running') else 'stopped'}；"
            f"状态={watchdog.get('perp_status', '-')}；"
            f"说明={watchdog.get('perp_detail', '-')}"
        )
    else:
        st.caption("合约自动运行 / Perp Auto: 未启用。设置 `CRYPTO_PERP_AUTO_ENABLED=true` 后由 watchdog 自动拉起。")
    if perp_settings.mode == "testnet" and not perp_settings.signed_account_enabled:
        st.warning("还没有配置 Futures Testnet API Key/Secret：可以试算，但不能提交 Binance USD-M Futures Testnet 订单。")

    target_weights = payload.get("target_weights") or {}
    target_df = pd.DataFrame(
        [
            {"symbol": symbol, "signed_weight": weight, "direction": "long" if weight > 0 else "short"}
            for symbol, weight in sorted(target_weights.items(), key=lambda item: abs(item[1]), reverse=True)
        ]
    )
    features = pd.DataFrame(payload.get("features") or [])
    current_orders = pd.DataFrame(payload.get("submitted_orders") or payload.get("pending_order_updates") or payload.get("planned_orders") or [])
    positions = pd.DataFrame(account.get("position_details") or [])
    market_rows = pd.DataFrame((payload.get("market_regime") or {}).get("rows") or [])
    order_log = _jsonl_frame(PERP_ORDERS_FILE, tail=200)
    feature_log = _jsonl_frame(PERP_FEATURES_FILE, tail=200)
    explanation = explain_crypto_perp_status(payload)

    tabs = st.tabs(["Decision Explain", "Signed Targets", "Signals", "Orders", "Positions", "Funding/Regime", "Raw Logs"])
    with tabs[0]:
        for line in explanation.get("summary") or []:
            st.write(f"- {line}")
        explain_signals = pd.DataFrame(explanation.get("signals") or [])
        explain_orders = pd.DataFrame(explanation.get("orders") or [])
        explain_risks = pd.DataFrame(explanation.get("risks") or [])
        if not explain_signals.empty:
            st.markdown("**Signals / Why**")
            signal_cols = [
                "symbol",
                "signal",
                "score",
                "threshold",
                "expected_edge_bps",
                "required_edge_bps",
                "cost_pass",
                "hawkes",
                "btc_leader",
                "notes",
            ]
            st.dataframe(explain_signals[[col for col in signal_cols if col in explain_signals.columns]], width="stretch", hide_index=True)
        if not explain_orders.empty:
            st.markdown("**Orders / What It Is Doing**")
            order_cols = ["symbol", "side", "status", "order_type", "time_in_force", "reduce_only", "notional", "fee", "plain"]
            st.dataframe(explain_orders[[col for col in order_cols if col in explain_orders.columns]], width="stretch", hide_index=True)
        if not explain_risks.empty:
            st.markdown("**Positions / Risk**")
            risk_cols = ["symbol", "side", "qty", "notional", "unrealized_pnl", "liquidation_distance_pct", "plain"]
            st.dataframe(explain_risks[[col for col in risk_cols if col in explain_risks.columns]], width="stretch", hide_index=True)
    with tabs[1]:
        st.dataframe(target_df, width="stretch", hide_index=True)
    with tabs[2]:
        if not features.empty:
            show_cols = ["symbol", "signal", "score", "abs_score", "conviction", "reason", "hawkes_imbalance", "cross_asset_leader_score", "last_price", "ofi_tier_1", "tick_agg", "spread_bps"]
            st.dataframe(features[[col for col in show_cols if col in features.columns]], width="stretch", hide_index=True)
        else:
            st.info("还没有合约信号。点一次合约试算。")
    with tabs[3]:
        st.dataframe(current_orders, width="stretch", hide_index=True)
    with tabs[4]:
        st.dataframe(positions, width="stretch", hide_index=True)
    with tabs[5]:
        st.dataframe(market_rows, width="stretch", hide_index=True)
    with tabs[6]:
        raw_order_tab, raw_feature_tab = st.tabs(["Perp Orders", "Perp Features"])
        with raw_order_tab:
            st.dataframe(order_log, width="stretch", hide_index=True)
        with raw_feature_tab:
            st.dataframe(feature_log, width="stretch", hide_index=True)


def _render_tables(settings, payload: dict[str, Any]) -> None:
    st.subheader("信号与订单 / Signals & Orders")
    target_weights = payload.get("target_weights") or {}
    if target_weights:
        target_df = pd.DataFrame(
            [{"symbol": symbol, "target_weight": weight} for symbol, weight in sorted(target_weights.items(), key=lambda item: item[1], reverse=True)]
        )
    else:
        target_df = pd.DataFrame(columns=["symbol", "target_weight"])

    features = pd.DataFrame(payload.get("features") or [])
    orders = pd.DataFrame(payload.get("submitted_orders") or payload.get("planned_orders") or [])
    account = payload.get("account") or {}
    sections = [
        "目标仓位 / Targets",
        "OFIM 打分 / Scores",
        "本轮订单 / Current Orders",
        "历史订单 / Order Log",
        "当前持仓 / Positions",
        "账户币种 / Balances",
        "学习闭环 / Learning",
        "合约做空 / Perp Long-Short",
    ]
    section = st.radio(
        "视图 / View",
        sections,
        horizontal=True,
        key="crypto_ofim_main_view",
    )

    if section.startswith("目标仓位"):
        st.dataframe(target_df, width="stretch", hide_index=True)
    elif section.startswith("OFIM"):
        if not features.empty:
            show_cols = [
                "symbol",
                "score",
                "conviction",
                "eligible",
                "reason",
                "last_price",
                "ofi_tier_1",
                "ofi_tier_2",
                "ofi_tier_3",
                "vol_accel",
                "tick_agg",
                "spread_bps",
            ]
            st.dataframe(features[[col for col in show_cols if col in features.columns]], width="stretch", hide_index=True)
        else:
            st.info("还没有 OFIM 打分。先点一次试算。")
    elif section.startswith("本轮订单"):
        st.dataframe(orders, width="stretch", hide_index=True)
    elif section.startswith("历史订单"):
        log_orders = _jsonl_frame(ORDERS_FILE, tail=200)
        st.dataframe(log_orders, width="stretch", hide_index=True)
    elif section.startswith("当前持仓"):
        position_details = pd.DataFrame(account.get("position_details") or [])
        if not position_details.empty:
            position_details["age_minutes"] = pd.to_numeric(position_details.get("age_seconds"), errors="coerce") / 60.0
            show_cols = [
                "symbol",
                "quantity",
                "last_price",
                "market_value",
                "avg_cost",
                "unrealized_pnl",
                "age_minutes",
                "stale",
                "active_universe",
            ]
            st.dataframe(position_details[[col for col in show_cols if col in position_details.columns]], width="stretch", hide_index=True)
            st.caption("age_minutes 是这笔持仓从最近一次开仓成交开始到现在的分钟数；stale=true 表示已经进入防卡仓监控。")
        else:
            st.info("当前没有 OFIM 策略持仓。")
    elif section.startswith("账户币种"):
        _render_balance_audit(settings)
    elif section.startswith("学习闭环"):
        _render_learning_lab(settings)
    elif section.startswith("合约做空"):
        _render_perp_long_short_lab()

    show_raw_logs = st.toggle("显示最近原始日志 / Show Raw Logs", value=False, key="crypto_ofim_show_raw_logs")
    if show_raw_logs:
        feature_log = _jsonl_frame(FEATURES_FILE, tail=200)
        event_log = _jsonl_frame(EVENTS_FILE, tail=200)
        stream_event_log = _jsonl_frame(STREAM_EVENTS_FILE, tail=200)
        user_event_log = _jsonl_frame(USER_STREAM_EVENTS_FILE, tail=200)
        user_fill_log = _jsonl_frame(USER_FILLS_FILE, tail=200)
        raw_feature_tab, raw_event_tab, raw_stream_tab, raw_user_tab, raw_fill_tab = st.tabs(
            ["特征日志 / Features", "事件账本 / Events", "行情流 / Stream", "账户事件 / User", "真实成交 / Fills"]
        )
        with raw_feature_tab:
            st.dataframe(feature_log, width="stretch", hide_index=True)
        with raw_event_tab:
            st.dataframe(event_log, width="stretch", hide_index=True)
        with raw_stream_tab:
            st.dataframe(stream_event_log, width="stretch", hide_index=True)
        with raw_user_tab:
            st.dataframe(user_event_log, width="stretch", hide_index=True)
        with raw_fill_tab:
            st.dataframe(user_fill_log, width="stretch", hide_index=True)


def _render_live_view(settings, *, refresh_seconds: int) -> None:
    payload = read_crypto_ofim_status()
    top = st.columns([1, 5])
    if top[0].button(
        "刷新一次 / Refresh Now",
        width="stretch",
        help="手动读取最新状态。不会下单，不会重启程序，只刷新页面上的数字。",
    ):
        st.rerun()
    top[1].caption(f"状态、账户、信号和订单区域每 {refresh_seconds} 秒自动刷新一次；设置区不会自动改动。")
    _render_status(settings, payload)
    _render_tables(settings, payload)


def render_app_body(*, key_prefix: str = "") -> None:
    """The complete Crypto OFIM page body — safe to call inside another host
    Streamlit app that has already done its own ``st.set_page_config`` and
    ``st.title``.

    ``key_prefix`` lets the host disambiguate Streamlit widget keys when
    multiple panels share a session (e.g. the unified home page). The empty
    default keeps the standalone main() backward-compatible.
    """
    _inject_style()
    refresh_cols = st.columns([1, 3, 2])
    with refresh_cols[0]:
        auto_refresh_enabled = st.toggle(
            "自动刷新 / Auto Refresh", value=True,
            key=f"{key_prefix}crypto_ofim_auto_refresh",
        )
    with refresh_cols[1]:
        refresh_seconds = st.slider(
            "页面刷新秒数 / Page Refresh Seconds",
            min_value=10,
            max_value=120,
            value=30,
            step=5,
            help="只控制网页多久读取一次最新状态，不影响策略实际交易频率。",
            key=f"{key_prefix}crypto_ofim_refresh_seconds",
        )
    with refresh_cols[2]:
        st.caption("交易频率在设置里的 `自动轮询秒数` 控制；这里仅控制页面显示。")
    if auto_refresh_enabled:
        st.html(
            f"""
            <script>
            setTimeout(function() {{
                window.parent.location.reload();
            }}, {int(refresh_seconds) * 1000});
            </script>
            """
        )

    try:
        settings = load_crypto_ofim_settings(ENV_FILE)
    except Exception as exc:
        st.error(f"配置读取失败：{exc}")
        return

    with st.expander("设置 / Settings", expanded=True):
        _render_settings(settings)

    action_payload = _render_actions(settings)
    if action_payload:
        _render_status(settings, action_payload)
        _render_tables(settings, action_payload)
    else:
        _render_live_view(settings, refresh_seconds=int(refresh_seconds))

    with st.expander("怎么拿 API / How to get API", expanded=False):
        st.markdown(
            """
            **你截图里的页面就是正确页面。**

            1. 打开 Binance Spot Test Network: https://testnet.binance.vision/
            2. 如果还没有 Key，点 `Generate HMAC-SHA-256 Key`。
            3. 如果已经有 Key，点右侧 `Edit`，确认权限里有 `TRADE / USER_DATA / USER_STREAM`。
            4. 创建时页面会给 `API Key` 和 `Secret Key`；`Secret Key` 通常只显示一次。
            5. 回到本页，把模式切到 `Binance Spot Testnet`，填入 Key/Secret，保存。
            6. 先点 `连接检查`，再点 `试算`，最后再点 `模拟下单一次`。

            真实 Binance Global 的 API Key 不等于 Spot Testnet Key。测试阶段只用 Spot Testnet；
            以后真要接实盘，也不要打开提现权限。
            """
        )


def main() -> None:
    st.set_page_config(page_title="Crypto OFIM Binance App", page_icon="₿", layout="wide")
    st.title("Crypto OFIM Binance 独立模拟 App")
    st.markdown(
        "<div class='muted-note'>这条线独立于富途系统。Spot 袖子只做多；USD-M Futures 袖子支持 signed long/short。行情来自 Binance Global 公开市场数据，测试执行只走 Binance 官方测试网。</div>",
        unsafe_allow_html=True,
    )
    render_app_body()


if __name__ == "__main__":
    main()
