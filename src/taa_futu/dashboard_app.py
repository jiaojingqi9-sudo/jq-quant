from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from plotly.subplots import make_subplots

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from taa_futu.backtest import run_backtest
try:
    from taa_futu.cascade_sleeve import cascade_trade_symbols, fetch_cascade_daily_frames, generate_live_cascade_plan
except ImportError:
    import taa_futu.cascade_sleeve as _cascade_sleeve

    cascade_trade_symbols = _cascade_sleeve.cascade_trade_symbols
    fetch_cascade_daily_frames = getattr(_cascade_sleeve, "fetch_cascade_daily_frames", None)
    generate_live_cascade_plan = _cascade_sleeve.generate_live_cascade_plan
from taa_futu.config import load_settings
from taa_futu.costs import (
    build_trade_cost_model,
    estimate_realized_from_fills,
    trade_log_total_fees,
    with_trade_costs,
)
from taa_futu.fusion_intraday import FusionIntradayStrategy
from taa_futu.futu_gateway import FutuPaperTrader, FutuTradeError
from taa_futu import market_logger
from taa_futu.market_data import FutuQuoteDataProvider, HistoricalDataProvider, MarketDataError, YFinanceDataProvider
try:
    from taa_futu.research import (
        ReplayResult,
        run_account_replay,
        run_cascade_replay,
        run_exact_execution_replay,
        run_fusion_intraday_replay,
        run_strategy_stack_replay,
    )
except ImportError:
    import taa_futu.research as _research

    ReplayResult = _research.ReplayResult
    run_account_replay = _research.run_account_replay
    run_cascade_replay = getattr(_research, "run_cascade_replay", None)
    run_exact_execution_replay = _research.run_exact_execution_replay
    run_fusion_intraday_replay = _research.run_fusion_intraday_replay
    run_strategy_stack_replay = _research.run_strategy_stack_replay
from taa_futu.strategy_stack import (
    effective_fusion_settings,
    fetch_futu_daily_closes,
    scaled_baseline_target_weights,
    stack_allocations,
    stack_label,
    stack_target_weights,
)
from taa_futu.strategy_experiment import (
    build_strategy_ledger,
    current_strategy_holdings,
    filter_fills_since_reset,
    load_strategy_split_state,
    period_strategy_performance,
    split_state_matches_current,
    split_state_weight_map,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
AUTO_TRADER_STATUS_FILE = REPO_ROOT / "runtime" / "auto_trader_status.json"
WATCHDOG_STATUS_FILE = REPO_ROOT / "runtime" / "watchdog_status.json"
MARKET_TIMEZONES = {
    "US": "America/New_York",
    "HK": "Asia/Hong_Kong",
    "CN": "Asia/Shanghai",
    "SG": "Asia/Singapore",
    "JP": "Asia/Tokyo",
    "AU": "Australia/Sydney",
    "CA": "America/Toronto",
    "MY": "Asia/Kuala_Lumpur",
}
INTRADAY_INTERVAL_OPTIONS = {
    "1分钟 / 1m": "K_1M",
    "5分钟 / 5m": "K_5M",
    "15分钟 / 15m": "K_15M",
    "30分钟 / 30m": "K_30M",
    "60分钟 / 60m": "K_60M",
}
INTRADAY_RANGE_OPTIONS = {
    "1日 / 1D": 1,
    "3日 / 3D": 3,
    "5日 / 5D": 5,
    "10日 / 10D": 10,
    "20日 / 20D": 20,
    "自定义 / Custom": None,
}
DAILY_RANGE_OPTIONS = {
    "3月 / 3M": 90,
    "6月 / 6M": 180,
    "1年 / 1Y": 365,
    "2年 / 2Y": 730,
    "5年 / 5Y": 1825,
    "自定义 / Custom": None,
}
MAX_INTRADAY_DAYS = {
    "K_1M": 20,
    "K_5M": 60,
    "K_15M": 180,
    "K_30M": 365,
    "K_60M": 730,
}
LOWER_PANEL_OPTIONS = {
    "成交量 / Volume": "volume",
    "MACD": "macd",
    "RSI": "rsi",
    "KDJ": "kdj",
}
LIGHTWEIGHT_CHARTS_SCRIPT_URL = (
    "https://unpkg.com/lightweight-charts@5.0.8/dist/lightweight-charts.standalone.production.js"
)


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _history_provider(name: str, settings) -> HistoricalDataProvider:
    if name == "futu":
        return FutuQuoteDataProvider(host=settings.futu_host, port=settings.futu_port)
    return YFinanceDataProvider()


def _format_currency(value: object) -> str:
    return f"{_safe_float(value):,.2f}"


def _format_pct(value: object) -> str:
    return f"{_safe_float(value) * 100:.2f}%"


def _top_target_summary(target_weights: dict[str, float], *, limit: int = 4) -> str:
    if not target_weights:
        return "当前没有新目标仓位。"
    ordered = sorted(target_weights.items(), key=lambda item: item[1], reverse=True)[:limit]
    return " / ".join(f"{code.replace('US.', '')} {weight:.0%}" for code, weight in ordered)


def _strategy_live_summary(name: str, weight: float, summary: str, extra: str | None = None) -> str:
    if weight <= 0:
        return f"{name}: 当前未启用。"
    parts = [f"{name}: 当前占比 {weight:.0%}。", summary]
    if extra:
        parts.append(extra)
    return " ".join(part for part in parts if part)


def _runtime_health_label(status_text: str) -> str:
    if "异常 / error" in status_text:
        return "异常 / error"
    if "已停止 / stopped" in status_text:
        return "已停止 / stopped"
    if "正常 / healthy" in status_text:
        return "正常 / healthy"
    return "待机 / standby"


def _strategy_runtime_display(weight: float, status_text: str) -> tuple[str, str]:
    if weight <= 0:
        return ("未启用 / disabled", "当前这套策略没有分到仓位。")
    if "已停止 / stopped" in status_text:
        return ("已配置，未运行 / configured, not running", "组合权重已生效，但自动运行当前没开。")
    if "异常 / error" in status_text:
        return ("运行异常 / runtime error", "这套策略已启用，但自动运行当前报错。")
    if "正常 / healthy" in status_text:
        return ("运行中 / running", "这套策略已启用，后台自动运行正常。")
    return ("待机 / standby", "这套策略已启用，正在等待下一次轮询。")


def _strategy_symbol_sets(settings) -> dict[str, set[str]]:
    try:
        cascade_symbols = set(cascade_trade_symbols(settings))
    except Exception:
        cascade_symbols = set()
    fusion_symbols = set(effective_fusion_settings(settings).fusion_universe)
    fusion_symbols.add(settings.fusion_benchmark)
    return {
        "Baseline": set(settings.symbols),
        "Fusion": fusion_symbols,
        "Claude/Cascade": cascade_symbols,
    }


def _owner_group_definitions(settings) -> dict[str, dict[str, object]]:
    strategy_sets = _strategy_symbol_sets(settings)
    return {
        "我的策略组 / Ours": {
            "symbols": set(strategy_sets["Baseline"]) | set(strategy_sets["Fusion"]),
            "sleeves": ("Baseline", "Fusion"),
        },
        "Claude/Cascade": {
            "symbols": set(strategy_sets["Claude/Cascade"]),
            "sleeves": ("Claude/Cascade",),
        },
    }


def _owner_group_targets(
    *,
    baseline_targets: dict[str, float],
    fusion_targets: dict[str, float],
    cascade_targets: dict[str, float],
) -> dict[str, dict[str, float]]:
    return {
        "我的策略组 / Ours": stack_target_weights(baseline_targets, fusion_targets),
        "Claude/Cascade": dict(cascade_targets),
    }


def _live_owner_shares(
    code: str,
    *,
    combined_targets: dict[str, float],
    owner_targets: dict[str, dict[str, float]],
    owner_sets: dict[str, set[str]],
) -> dict[str, float]:
    combined_weight = float(combined_targets.get(code, 0.0))
    if combined_weight > 0:
        shares = {
            name: float(weights.get(code, 0.0)) / combined_weight
            for name, weights in owner_targets.items()
            if float(weights.get(code, 0.0)) > 0
        }
        total = sum(shares.values())
        if total > 0:
            return {name: share / total for name, share in shares.items() if share > 0}

    owners = [name for name, symbols in owner_sets.items() if code in symbols]
    if len(owners) == 1:
        return {owners[0]: 1.0}
    return {"Shared/Overlap": 1.0} if owners else {}


def _current_strategy_breakdown(
    *,
    settings,
    positions: pd.DataFrame,
    total_assets: float,
    combined_targets: dict[str, float],
    baseline_targets: dict[str, float],
    fusion_targets: dict[str, float],
    cascade_targets: dict[str, float],
    sleeve_allocations: dict[str, float],
) -> pd.DataFrame:
    owner_defs = _owner_group_definitions(settings)
    owner_sets = {name: set(defn["symbols"]) for name, defn in owner_defs.items()}
    owner_targets = _owner_group_targets(
        baseline_targets=baseline_targets,
        fusion_targets=fusion_targets,
        cascade_targets=cascade_targets,
    )
    rows: dict[str, dict[str, object]] = {}
    group_weights = {
        "我的策略组 / Ours": float(sleeve_allocations["baseline"]) + float(sleeve_allocations["fusion"]),
        "Claude/Cascade": float(sleeve_allocations["cascade"]),
    }
    group_components = {
        "我的策略组 / Ours": f"Baseline {float(sleeve_allocations['baseline']):.0%} + Fusion {float(sleeve_allocations['fusion']):.0%}",
        "Claude/Cascade": f"Cascade {float(sleeve_allocations['cascade']):.0%}",
    }
    for name in ("我的策略组 / Ours", "Claude/Cascade"):
        target_map = owner_targets[name]
        rows[name] = {
            "策略组 / Group": name,
            "组内构成 / Components": group_components[name],
            "占比 / Weight": f"{group_weights[name]:.0%}",
            "当前目标 / Targets": _top_target_summary(target_map),
            "目标市值 / Target Value": total_assets * sum(float(v) for v in target_map.values()),
            "估算持仓市值 / Est. Holdings": 0.0,
            "估算浮盈 / Est. Unrealized": 0.0,
        }
    shared_row = {
        "策略组 / Group": "Shared/Overlap",
        "组内构成 / Components": "跨组重叠 / cross-group overlap",
        "占比 / Weight": "—",
        "当前目标 / Targets": "重叠标的 / overlapping symbols",
        "目标市值 / Target Value": 0.0,
        "估算持仓市值 / Est. Holdings": 0.0,
        "估算浮盈 / Est. Unrealized": 0.0,
    }

    unrealized_series = _position_metric_series(positions, "unrealized_pl", "pl_val")
    market_value_series = pd.to_numeric(positions.get("market_val"), errors="coerce").fillna(0.0) if not positions.empty else pd.Series(dtype=float)

    for index, row in positions.iterrows():
        code = str(row.get("code", ""))
        market_value = float(market_value_series.iloc[index]) if index < len(market_value_series) else 0.0
        unrealized = float(unrealized_series.iloc[index]) if index < len(unrealized_series) and pd.notna(unrealized_series.iloc[index]) else 0.0
        shares = _live_owner_shares(
            code,
            combined_targets=combined_targets,
            owner_targets=owner_targets,
            owner_sets=owner_sets,
        )
        for strategy_name, share in shares.items():
            target_row = rows.get(strategy_name, shared_row)
            target_row["估算持仓市值 / Est. Holdings"] = float(target_row["估算持仓市值 / Est. Holdings"]) + market_value * share
            target_row["估算浮盈 / Est. Unrealized"] = float(target_row["估算浮盈 / Est. Unrealized"]) + unrealized * share
            if strategy_name == "Shared/Overlap":
                shared_row = target_row
            else:
                rows[strategy_name] = target_row

    frame_rows = list(rows.values())
    if float(shared_row["估算持仓市值 / Est. Holdings"]) > 0 or float(shared_row["估算浮盈 / Est. Unrealized"]) != 0:
        frame_rows.append(shared_row)
    return pd.DataFrame(frame_rows)


def _strategy_owner_from_symbol(code: str, settings) -> str:
    owner_defs = _owner_group_definitions(settings)
    owners = [name for name, definition in owner_defs.items() if code in definition["symbols"]]
    if len(owners) == 1:
        return owners[0]
    if len(owners) > 1:
        return "Shared/Overlap"
    return "Unclassified"


def _period_strategy_breakdown(*, filled_cost_view: pd.DataFrame, settings) -> pd.DataFrame:
    if filled_cost_view.empty:
        return pd.DataFrame(
            columns=[
                "策略组 / Group",
                "组内构成 / Components",
                "成交笔数 / Trades",
                "估算成本 / Fees",
                "区间已实现 / Realized",
            ]
        )

    rows = filled_cost_view.copy()
    if "code" in rows.columns:
        rows["strategy_bucket"] = rows["code"].astype(str).map(lambda code: _strategy_owner_from_symbol(code, settings))
    else:
        rows["strategy_bucket"] = "Unclassified"

    summary_rows: list[dict[str, object]] = []
    component_map = {
        "我的策略组 / Ours": "Baseline + Fusion",
        "Claude/Cascade": "Cascade",
        "Shared/Overlap": "跨组重叠 / cross-group overlap",
        "Unclassified": "未分类 / unclassified",
    }
    ordered_names = ["我的策略组 / Ours", "Claude/Cascade", "Shared/Overlap", "Unclassified"]
    for strategy_name in ordered_names:
        subset = rows[rows["strategy_bucket"] == strategy_name].copy()
        if subset.empty:
            continue
        realized = estimate_realized_from_fills(
            subset,
            settings,
            qty_col="dealt_qty",
            price_col="dealt_avg_price",
            timestamp_col="updated_time",
        )
        summary_rows.append(
            {
                "策略组 / Group": strategy_name,
                "组内构成 / Components": component_map[strategy_name],
                "成交笔数 / Trades": int(len(subset)),
                "估算成本 / Fees": trade_log_total_fees(subset),
                "区间已实现 / Realized": realized,
            }
        )
    return pd.DataFrame(summary_rows)


def _with_owner_group(rows: pd.DataFrame, settings) -> pd.DataFrame:
    if rows.empty or "code" not in rows.columns:
        return rows.copy()
    frame = rows.copy()
    frame["策略组 / Group"] = frame["code"].astype(str).map(lambda code: _strategy_owner_from_symbol(code, settings))
    return frame


def _format_metric_value(value: object) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.4f}"
    return str(value)


def _optional_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(numeric):
        return None
    return numeric


def _position_metric_series(positions: pd.DataFrame, primary: str, *fallbacks: str) -> pd.Series:
    if positions.empty:
        return pd.Series(dtype=float)
    candidates = (primary, *fallbacks)
    values = pd.Series([None] * len(positions), index=positions.index, dtype=object)
    for column in candidates:
        if column not in positions.columns:
            continue
        numeric = pd.to_numeric(positions[column], errors="coerce")
        values = values.where(values.notna(), numeric)
    return pd.to_numeric(values, errors="coerce")


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


def _calculate_unrealized_from_positions(positions: pd.DataFrame) -> float:
    if positions.empty:
        return 0.0
    unrealized = _position_metric_series(positions, "unrealized_pl", "pl_val")
    if unrealized.empty:
        return 0.0
    valid = unrealized.dropna()
    if not valid.empty:
        return float(valid.sum())

    if {"qty", "nominal_price", "cost_price"}.issubset(positions.columns):
        qty = pd.to_numeric(positions["qty"], errors="coerce").fillna(0.0)
        price = pd.to_numeric(positions["nominal_price"], errors="coerce").fillna(0.0)
        cost = pd.to_numeric(positions["cost_price"], errors="coerce").fillna(0.0)
        return float(((price - cost) * qty).sum())
    return 0.0


def _live_monitor_run_every(enabled: bool, seconds: int) -> str | None:
    if not enabled:
        return None
    return f"{max(2, int(seconds))}s"


def _auto_trader_status_text() -> str:
    if not AUTO_TRADER_STATUS_FILE.exists():
        return "自动运行 / Auto Run: 未启动 / stopped"
    try:
        payload = json.loads(AUTO_TRADER_STATUS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "自动运行 / Auto Run: 状态文件损坏 / invalid status file"

    running = "运行中 / running" if payload.get("running") else "已停止 / stopped"
    action = payload.get("action", "unknown")
    detail = payload.get("detail", "")
    updated_at = str(payload.get("updated_at", ""))
    if updated_at:
        try:
            updated_at = datetime.fromisoformat(updated_at).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    health, action_label, detail_label = _friendly_runtime_status(action, detail)
    updated_text = f" | 更新时间 / Updated {updated_at}" if updated_at else ""
    return f"自动运行 / Auto Run: {running} | {health} | {action_label} | {detail_label}{updated_text}"


def _watchdog_status_text() -> str:
    if not WATCHDOG_STATUS_FILE.exists():
        return "守护监控 / Watchdog: 未启动 / stopped"
    try:
        payload = json.loads(WATCHDOG_STATUS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "守护监控 / Watchdog: 状态文件损坏 / invalid status file"

    running = "运行中 / running" if payload.get("running") else "已停止 / stopped"
    action = payload.get("action", "unknown")
    detail = payload.get("detail", "")
    next_check = payload.get("next_check_seconds")
    updated_at = str(payload.get("updated_at", ""))
    if updated_at:
        try:
            updated_at = datetime.fromisoformat(updated_at).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    health, action_label, detail_label = _friendly_runtime_status(action, detail)
    next_text = f" | 下次检查 / Next ~{next_check}s" if isinstance(next_check, (int, float)) else ""
    updated_text = f" | 更新时间 / Updated {updated_at}" if updated_at else ""
    return f"守护监控 / Watchdog: {running} | {health} | {action_label} | {detail_label}{next_text}{updated_text}"


@dataclass(frozen=True)
class SymbolContext:
    snapshot: pd.Series
    ticks: pd.DataFrame
    order_book: dict | None
    buy_sell_points: pd.DataFrame
    action_label: str
    best_bid: float
    best_ask: float
    best_bid_size: float
    best_ask_size: float
    spread: float
    mid_price: float
    total_bid_depth: float
    total_ask_depth: float
    lob_imbalance: float


def _inject_terminal_css() -> None:
    st.markdown(
        """
<style>
  .stApp {
    background:
      radial-gradient(circle at top left, rgba(97, 132, 177, 0.10), transparent 24%),
      linear-gradient(180deg, #f6f9fc 0%, #eef3f8 100%);
    color: #182534;
  }
  [data-testid="stSidebar"] {
    background: linear-gradient(180deg, #f3f7fb 0%, #ebf1f7 100%);
    border-right: 1px solid #d8e2ec;
  }
  .terminal-shell {
    border: 1px solid #d8e3ee;
    border-radius: 18px;
    background: linear-gradient(180deg, rgba(255,255,255,0.98) 0%, rgba(247,250,253,0.98) 100%);
    box-shadow: 0 10px 28px rgba(34, 51, 84, 0.08);
    padding: 14px 16px;
  }
  .terminal-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 16px;
    border-bottom: 1px solid #e4ebf3;
    padding-bottom: 10px;
    margin-bottom: 12px;
  }
  .terminal-title {
    font-size: 30px;
    font-weight: 800;
    color: #162434;
    letter-spacing: 0.01em;
  }
  .terminal-subtitle {
    margin-top: 4px;
    color: #6b7d90;
    font-size: 12px;
  }
  .terminal-price {
    font-size: 38px;
    font-weight: 800;
    line-height: 1;
  }
  .terminal-up {
    color: #ff4d67;
  }
  .terminal-down {
    color: #20c997;
  }
  .terminal-chip-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 12px;
  }
  .terminal-chip {
    padding: 5px 10px;
    border-radius: 999px;
    background: rgba(242, 247, 252, 0.96);
    border: 1px solid #d6e2ee;
    color: #42576c;
    font-size: 11px;
    line-height: 1.2;
  }
  .terminal-mini-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px 16px;
    margin-bottom: 12px;
  }
  .terminal-mini-card {
    padding: 10px 12px;
    border-radius: 12px;
    background: rgba(247, 250, 253, 0.98);
    border: 1px solid #dbe5ef;
  }
  .terminal-mini-label {
    color: #6e8196;
    font-size: 11px;
    margin-bottom: 4px;
  }
  .terminal-mini-value {
    color: #172535;
    font-size: 18px;
    font-weight: 700;
  }
  .terminal-panel-title {
    font-size: 18px;
    font-weight: 700;
    color: #162434;
    margin-bottom: 10px;
  }
  .terminal-caption {
    color: #66798d;
    font-size: 12px;
  }
  .terminal-divider {
    height: 1px;
    background: #e2e8f0;
    margin: 12px 0;
  }
  div[data-testid="stDataFrame"] {
    border: 1px solid #d9e3ee;
    border-radius: 14px;
    overflow: hidden;
    background: #ffffff;
  }
  div[data-testid="stMetric"] {
    border: 1px solid #dae4ef;
    border-radius: 14px;
    background: #ffffff;
    padding: 8px 10px;
  }
  div[data-testid="stMetricValue"] {
    color: #152231;
  }
  div[data-testid="stMetricLabel"] {
    color: #66788d;
    white-space: normal !important;
    overflow-wrap: anywhere !important;
    line-height: 1.2 !important;
    font-size: 0.94rem !important;
  }
  .stSelectbox label, .stDateInput label, .stSegmentedControl label, .stToggle label, .stTabs [data-baseweb="tab"] {
    color: #1b2a3a !important;
  }
  .stMarkdown, .stCaption, .stAlert {
    color: #1b2a3a;
  }
  [data-testid="stSidebarCollapseButton"] button,
  [data-testid="stSidebarCollapsedControl"] button,
  [data-testid="collapsedControl"] button {
    background: #ffffff !important;
    border: 1px solid #d6e0eb !important;
    border-radius: 10px !important;
    box-shadow: 0 4px 12px rgba(26, 42, 62, 0.10) !important;
    color: #1c2b3a !important;
  }
  [data-testid="stSidebarCollapseButton"] button:hover,
  [data-testid="stSidebarCollapsedControl"] button:hover,
  [data-testid="collapsedControl"] button:hover {
    background: #f4f8fb !important;
  }
</style>
""",
        unsafe_allow_html=True,
    )


def _sidebar_toggle_script(sequence: int) -> str:
    return f"""
<script>
const seq = {sequence};
if (window.parent.__taaSidebarToggleSeq !== seq) {{
  window.parent.__taaSidebarToggleSeq = seq;
  const doc = window.parent.document;
  const toggleButton =
    doc.querySelector('[data-testid="stSidebarCollapseButton"] button') ||
    doc.querySelector('[data-testid="stSidebarCollapsedControl"] button') ||
    doc.querySelector('[data-testid="collapsedControl"] button') ||
    Array.from(doc.querySelectorAll('button')).find((button) => {{
      const label = (button.getAttribute('aria-label') || '').toLowerCase();
      return label.includes('sidebar') || label.includes('侧边栏');
    }});
  if (toggleButton) {{
    toggleButton.click();
  }}
}}
</script>
"""


def _render_sidebar_toolbar() -> None:
    toolbar_cols = st.columns([0.82, 0.18])
    with toolbar_cols[1]:
        if st.button("收起/展开侧栏 / Toggle Sidebar", key="toggle-sidebar-toolbar", use_container_width=True):
            st.session_state["sidebar_toggle_seq"] = st.session_state.get("sidebar_toggle_seq", 0) + 1

    toggle_seq = int(st.session_state.get("sidebar_toggle_seq", 0) or 0)
    if toggle_seq:
        components.html(_sidebar_toggle_script(toggle_seq), height=0, width=0)


def _symbol_display_name(code: str, snapshot: pd.Series | None = None) -> str:
    if snapshot is not None:
        name_value = str(snapshot.get("name", "") or "").strip()
        if name_value and name_value.lower() != "nan":
            return name_value
    return code.split(".", 1)[-1]


def _positions_view(positions: pd.DataFrame) -> pd.DataFrame:
    if positions.empty:
        return positions
    frame = positions.copy()
    if "unrealized_pl" in frame.columns or "pl_val" in frame.columns:
        frame["unrealized_pl"] = _position_metric_series(frame, "unrealized_pl", "pl_val")
    if "realized_pl" in frame.columns:
        frame["realized_pl"] = _position_metric_series(frame, "realized_pl")
    if "today_pl_val" in frame.columns:
        frame["today_pl_val"] = _position_metric_series(frame, "today_pl_val")
    columns = [
        "code",
        "qty",
        "nominal_price",
        "market_val",
        "today_pl_val",
        "unrealized_pl",
        "realized_pl",
        "pl_ratio",
    ]
    frame = frame.loc[:, [column for column in columns if column in frame.columns]].copy()
    return frame.rename(
        columns={
            "code": "标的 / Symbol",
            "qty": "数量 / Qty",
            "nominal_price": "最新价 / Last",
            "market_val": "市值 / Market Value",
            "today_pl_val": "当日盈亏 / Today PnL",
            "unrealized_pl": "浮动盈亏 / Unrealized",
            "realized_pl": "已实现盈亏 / Realized",
            "pl_ratio": "收益率 / PnL Ratio",
        }
    )


def _orders_view(orders: pd.DataFrame) -> pd.DataFrame:
    if orders.empty:
        return orders
    columns = [
        "code",
        "trd_side",
        "order_status",
        "qty",
        "dealt_qty",
        "dealt_avg_price",
        "price",
        "create_time",
        "updated_time",
        "order_id",
        "fees_total",
        "fee_source",
        "last_err_msg",
    ]
    frame = orders.loc[:, [column for column in columns if column in orders.columns]].copy()
    return frame.rename(
        columns={
            "code": "标的 / Symbol",
            "trd_side": "方向 / Side",
            "order_status": "状态 / Status",
            "qty": "委托数量 / Order Qty",
            "dealt_qty": "已成交数量 / Filled Qty",
            "dealt_avg_price": "成交均价 / Avg Fill Price",
            "price": "委托价 / Limit Price",
            "create_time": "创建时间 / Created At",
            "updated_time": "更新时间 / Updated At",
            "order_id": "订单号 / Order ID",
            "fees_total": "估算费用 / Est. Fees",
            "fee_source": "费用来源 / Fee Source",
            "last_err_msg": "错误 / Error",
        }
    )


def _calculate_realized_from_fills(order_history: pd.DataFrame) -> float:
    if order_history.empty:
        return 0.0

    rows = order_history.copy()
    rows["dealt_qty_num"] = pd.to_numeric(rows.get("dealt_qty"), errors="coerce").fillna(0.0)
    rows["dealt_price_num"] = pd.to_numeric(rows.get("dealt_avg_price"), errors="coerce").fillna(0.0)
    rows = rows[rows["dealt_qty_num"] > 0].copy()
    if rows.empty:
        return 0.0

    sort_columns = [column for column in ["updated_time", "create_time"] if column in rows.columns]
    if sort_columns:
        rows = rows.sort_values(sort_columns, ascending=True)

    inventory: dict[str, list[tuple[float, float]]] = {}
    realized = 0.0
    for row in rows.itertuples(index=False):
        code = str(getattr(row, "code"))
        side = str(getattr(row, "trd_side"))
        qty = float(getattr(row, "dealt_qty_num"))
        price = float(getattr(row, "dealt_price_num"))
        inventory.setdefault(code, [])
        if side == "BUY":
            inventory[code].append((qty, price))
            continue
        if side != "SELL":
            continue
        remaining = qty
        while remaining > 0 and inventory[code]:
            open_qty, open_price = inventory[code][0]
            matched = min(remaining, open_qty)
            realized += (price - open_price) * matched
            remaining -= matched
            open_qty -= matched
            if open_qty <= 0:
                inventory[code].pop(0)
            else:
                inventory[code][0] = (open_qty, open_price)
    return float(realized)


def _watchlist_view(plan, target_weights: dict[str, float]) -> pd.DataFrame:
    if not plan.features:
        return pd.DataFrame()
    rows = []
    for feature in plan.features:
        rows.append(
            {
                "标的 / Symbol": feature.code,
                "当前价 / Last": feature.last_price,
                "评分 / Score": feature.score,
                "目标仓位 / Target Weight": target_weights.get(feature.code, 0.0),
                "Gap%": feature.gap_pct,
                "5m动量 / 5m Mom": feature.momentum_5m,
                "VWAP偏离 / VWAP Dist": feature.vwap_distance,
                "相对量 / Rel Vol": feature.rel_volume,
                "盘口失衡 / OBI": feature.orderbook_imbalance,
                "逐笔失衡 / Tick Imb": feature.tick_imbalance,
                "点差bps / Spread": feature.spread_bps,
                "状态 / Status": feature.reason,
            }
        )
    return pd.DataFrame(rows)


def _normalize_kline(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    data = frame.copy()
    timestamp_col = "time_key" if "time_key" in data.columns else data.columns[0]
    data["timestamp"] = pd.to_datetime(data[timestamp_col])
    for column in ["open", "high", "low", "close", "volume"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data[["timestamp", "open", "high", "low", "close", "volume"]].dropna(subset=["timestamp", "open", "high", "low", "close"])


def _volume_colors(bars: pd.DataFrame) -> list[str]:
    return ["#cf3c3c" if close >= open_ else "#1d9a6c" for open_, close in zip(bars["open"], bars["close"], strict=False)]


def _build_price_ticks(values: pd.Series) -> list[float]:
    if values.empty:
        return []
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return []
    step = max(1, len(numeric) // 6)
    ticks = numeric.iloc[::step].tolist()
    if numeric.iloc[-1] not in ticks:
        ticks.append(float(numeric.iloc[-1]))
    return sorted(set(float(value) for value in ticks))


def _market_timezone(market: str) -> ZoneInfo:
    return ZoneInfo(MARKET_TIMEZONES.get(market, "UTC"))


def _history_window_from_days(days: int, *, end_on: date | None = None) -> tuple[date, date]:
    end_value = end_on or date.today()
    start_value = end_value - timedelta(days=max(days, 1))
    return start_value, end_value


def _clamp_intraday_start(start_value: date, end_value: date, ktype: str) -> tuple[date, bool]:
    max_days = MAX_INTRADAY_DAYS.get(ktype, 20)
    span_days = (end_value - start_value).days
    if span_days <= max_days:
        return start_value, False
    return end_value - timedelta(days=max_days), True


def _epoch_seconds(series: pd.Series, market: str) -> list[int]:
    timestamps = pd.to_datetime(series)
    market_tz = _market_timezone(market)
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(market_tz)
    else:
        timestamps = timestamps.dt.tz_convert(market_tz)
    utc_series = timestamps.dt.tz_convert("UTC")
    return [int(value.timestamp()) for value in utc_series]


def _display_timestamps(series: pd.Series, market: str) -> pd.Series:
    timestamps = pd.to_datetime(series)
    market_tz = _market_timezone(market)
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(market_tz)
    else:
        timestamps = timestamps.dt.tz_convert(market_tz)
    return timestamps


def _display_timestamp_strings(series: pd.Series, market: str) -> list[str]:
    timestamps = _display_timestamps(series, market)
    has_intraday = ((timestamps.dt.hour != 0) | (timestamps.dt.minute != 0) | (timestamps.dt.second != 0)).any()
    format_string = "%Y-%m-%d %H:%M" if has_intraday else "%Y-%m-%d"
    return [value.strftime(format_string) for value in timestamps]


def _visible_order_markers(
    order_markers: pd.DataFrame | None,
    bars: pd.DataFrame,
    *,
    align_mode: str = "bar",
) -> pd.DataFrame:
    if order_markers is None or order_markers.empty or bars.empty:
        return pd.DataFrame(columns=["timestamp", "price", "label", "side"])

    markers = order_markers.copy()
    markers["timestamp"] = pd.to_datetime(markers["timestamp"])
    bar_times = pd.Series(pd.to_datetime(bars["timestamp"]).drop_duplicates().sort_values().tolist())
    if bar_times.empty:
        return pd.DataFrame(columns=["timestamp", "price", "label", "side"])

    start_time = bar_times.iloc[0]
    end_time = bar_times.iloc[-1]
    markers = markers[(markers["timestamp"] >= start_time) & (markers["timestamp"] <= end_time + timedelta(days=1))].copy()
    if markers.empty:
        return pd.DataFrame(columns=["timestamp", "price", "label", "side"])

    if align_mode == "daily":
        lookup = {timestamp.normalize(): timestamp for timestamp in bar_times}
        markers["timestamp"] = markers["timestamp"].dt.normalize().map(lookup)
        markers = markers.dropna(subset=["timestamp"])
        return markers[(markers["timestamp"] >= start_time) & (markers["timestamp"] <= end_time)].copy()

    if len(bar_times) > 1:
        interval = pd.Series(bar_times).diff().dropna().median()
        tolerance = max(interval * 2, pd.Timedelta(minutes=1))
    else:
        tolerance = pd.Timedelta(hours=24)

    bar_frame = pd.DataFrame({"bar_timestamp": bar_times})
    aligned = pd.merge_asof(
        markers.sort_values("timestamp"),
        bar_frame.sort_values("bar_timestamp"),
        left_on="timestamp",
        right_on="bar_timestamp",
        direction="backward",
        tolerance=tolerance,
    )
    aligned["timestamp"] = aligned["bar_timestamp"]
    aligned = aligned.dropna(subset=["timestamp"])
    return aligned[(aligned["timestamp"] >= start_time) & (aligned["timestamp"] <= end_time)].copy()


def _compressed_order_markers(
    order_markers: pd.DataFrame | None,
    bars: pd.DataFrame,
    *,
    align_mode: str = "bar",
) -> pd.DataFrame:
    visible = _visible_order_markers(order_markers, bars, align_mode=align_mode)
    if visible.empty:
        return pd.DataFrame(columns=["timestamp", "side", "count"])

    rows = visible.copy()
    rows["side"] = rows["side"].astype(str).str.upper()
    rows = rows[rows["side"].isin(["BUY", "SELL"])].copy()
    if rows.empty:
        return pd.DataFrame(columns=["timestamp", "side", "count"])

    compressed = (
        rows.groupby(["timestamp", "side"], as_index=False)
        .size()
        .rename(columns={"size": "count"})
        .sort_values(["timestamp", "side"])
        .reset_index(drop=True)
    )
    return compressed


def _marker_count_text(side: str, count: int) -> str:
    prefix = "B" if str(side).upper() == "BUY" else "S"
    if count <= 1:
        return prefix
    if count <= 9:
        return f"{prefix}{count}"
    return f"{prefix}9+"


def _price_precision(series: pd.Series) -> int:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return 2
    reference = float(values.abs().median())
    if reference >= 100:
        return 2
    if reference >= 1:
        return 3
    if reference >= 0.1:
        return 4
    return 6


def _line_series_points(rows: pd.DataFrame, column: str, market: str) -> list[dict[str, float | int]]:
    if column not in rows.columns:
        return []
    values = pd.to_numeric(rows[column], errors="coerce")
    timestamps = _epoch_seconds(rows["timestamp"], market)
    points: list[dict[str, float | int]] = []
    for timestamp_value, numeric_value in zip(timestamps, values, strict=False):
        if pd.isna(numeric_value):
            continue
        points.append({"time": int(timestamp_value), "value": round(float(numeric_value), 6)})
    return points


def _constant_line_points(rows: pd.DataFrame, market: str, level: float) -> list[dict[str, float | int]]:
    return [{"time": timestamp_value, "value": float(level)} for timestamp_value in _epoch_seconds(rows["timestamp"], market)]


def _decorate_indicator_columns(rows: pd.DataFrame) -> pd.DataFrame:
    frame = rows.copy()
    close = pd.to_numeric(frame["close"], errors="coerce")
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    frame["macd_dif"] = ema12 - ema26
    frame["macd_dea"] = frame["macd_dif"].ewm(span=9, adjust=False).mean()
    frame["macd_hist"] = (frame["macd_dif"] - frame["macd_dea"]) * 2

    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    average_gain = gains.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    average_loss = losses.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = average_gain / average_loss.replace(0, pd.NA)
    frame["rsi14"] = 100 - (100 / (1 + rs))

    rolling_low = low.rolling(9, min_periods=1).min()
    rolling_high = high.rolling(9, min_periods=1).max()
    denominator = (rolling_high - rolling_low).replace(0, pd.NA)
    rsv = ((close - rolling_low) / denominator * 100).fillna(50.0)
    frame["kdj_k"] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    frame["kdj_d"] = frame["kdj_k"].ewm(alpha=1 / 3, adjust=False).mean()
    frame["kdj_j"] = frame["kdj_k"] * 3 - frame["kdj_d"] * 2
    return frame


def _lower_panel_payload(rows: pd.DataFrame, *, market: str, lower_panel: str) -> dict[str, object]:
    panel_key = lower_panel or "volume"
    timestamps = _epoch_seconds(rows["timestamp"], market)
    close = pd.to_numeric(rows["close"], errors="coerce")
    open_values = pd.to_numeric(rows["open"], errors="coerce")
    volume = pd.to_numeric(rows["volume"], errors="coerce").fillna(0)

    volume_points = [
        {
            "time": int(timestamp_value),
            "value": float(volume_value),
            "color": "#ff5b6e" if float(close_value) >= float(open_value) else "#1fc8a5",
        }
        for timestamp_value, volume_value, open_value, close_value in zip(
            timestamps,
            volume,
            open_values,
            close,
            strict=False,
        )
    ]
    if panel_key == "volume":
        return {
            "key": "volume",
            "title": "成交量 / Volume",
            "histogram": {
                "name": "成交量 / Volume",
                "priceFormat": "volume",
                "data": volume_points,
            },
            "lines": [],
            "references": [],
        }

    if panel_key == "macd":
        macd_hist = pd.to_numeric(rows.get("macd_hist"), errors="coerce").fillna(0)
        histogram_points = []
        previous_hist = macd_hist.shift(1).fillna(macd_hist)
        for timestamp_value, current_value, previous_value in zip(
            timestamps,
            macd_hist,
            previous_hist,
            strict=False,
        ):
            is_positive = float(current_value) >= 0
            stronger = abs(float(current_value)) >= abs(float(previous_value))
            if is_positive:
                color_value = "#ff5b6e" if stronger else "#ff9ea8"
            else:
                color_value = "#1fc8a5" if stronger else "#7ee0cd"
            histogram_points.append(
                {"time": int(timestamp_value), "value": round(float(current_value), 6), "color": color_value}
            )
        return {
            "key": "macd",
            "title": "MACD",
            "histogram": {
                "name": "MACD Hist",
                "priceFormat": "price",
                "data": histogram_points,
            },
            "lines": [
                {"name": "DIF", "color": "#f6c85f", "style": "solid", "data": _line_series_points(rows, "macd_dif", market)},
                {"name": "DEA", "color": "#4c78a8", "style": "solid", "data": _line_series_points(rows, "macd_dea", market)},
            ],
            "references": [
                {"name": "Zero", "color": "#5d7285", "style": "dashed", "data": _constant_line_points(rows, market, 0.0)}
            ],
        }

    if panel_key == "rsi":
        return {
            "key": "rsi",
            "title": "RSI(14)",
            "histogram": None,
            "lines": [
                {"name": "RSI14", "color": "#f6c85f", "style": "solid", "data": _line_series_points(rows, "rsi14", market)}
            ],
            "references": [
                {"name": "RSI 70", "color": "#6c8295", "style": "dashed", "data": _constant_line_points(rows, market, 70.0)},
                {"name": "RSI 30", "color": "#6c8295", "style": "dashed", "data": _constant_line_points(rows, market, 30.0)},
            ],
        }

    return {
        "key": "kdj",
        "title": "KDJ(9,3,3)",
        "histogram": None,
        "lines": [
            {"name": "K", "color": "#f6c85f", "style": "solid", "data": _line_series_points(rows, "kdj_k", market)},
            {"name": "D", "color": "#4c78a8", "style": "solid", "data": _line_series_points(rows, "kdj_d", market)},
            {"name": "J", "color": "#d06fd1", "style": "solid", "data": _line_series_points(rows, "kdj_j", market)},
        ],
        "references": [
            {"name": "KDJ 80", "color": "#6c8295", "style": "dashed", "data": _constant_line_points(rows, market, 80.0)},
            {"name": "KDJ 20", "color": "#6c8295", "style": "dashed", "data": _constant_line_points(rows, market, 20.0)},
        ],
    }


def _lightweight_chart_html(
    bars: pd.DataFrame,
    *,
    market: str,
    symbol: str,
    chart_id: str,
    title: str,
    subtitle: str,
    overlays: list[tuple[str, str, str]] | None = None,
    order_markers: pd.DataFrame | None = None,
    action_marker: pd.DataFrame | None = None,
    price_line_label: str | None = None,
    marker_alignment: str = "bar",
    lower_panel: str = "volume",
    main_series: str = "candles",
) -> str:
    rows = bars.copy()
    rows = rows.dropna(subset=["timestamp", "open", "high", "low", "close"]).reset_index(drop=True)
    if rows.empty:
        return "<div style='padding:18px;color:#c7d3df;background:#0b1016;border:1px solid #1a2833;border-radius:14px;'>没有 K 线数据 / No K-line data.</div>"
    timestamps = _epoch_seconds(rows["timestamp"], market)
    display_times = _display_timestamp_strings(rows["timestamp"], market)
    has_intraday = any(" " in value for value in display_times)
    price_precision = _price_precision(rows["close"])

    candle_rows = [
        {
            "time": int(timestamp_value),
            "open": round(float(open_value), price_precision),
            "high": round(float(high_value), price_precision),
            "low": round(float(low_value), price_precision),
            "close": round(float(close_value), price_precision),
            "volume": float(volume_value),
            "displayTime": display_time,
        }
        for timestamp_value, display_time, open_value, high_value, low_value, close_value, volume_value in zip(
            timestamps,
            display_times,
            rows["open"],
            rows["high"],
            rows["low"],
            rows["close"],
            rows["volume"].fillna(0),
            strict=False,
        )
    ]

    overlay_rows = []
    for label, column, color_value in overlays or []:
        if column not in rows.columns:
            continue
        overlay_rows.append(
            {
                "name": label,
                "color": color_value,
                "style": "dashed" if "开盘区间" in label else "solid",
                "data": _line_series_points(rows, column, market),
            }
        )

    chart_markers: list[dict[str, object]] = []
    visible_markers = _compressed_order_markers(order_markers, rows, align_mode=marker_alignment)
    if not visible_markers.empty:
        marker_times = _epoch_seconds(visible_markers["timestamp"], market)
        for timestamp_value, side_value, count_value in zip(
            marker_times,
            visible_markers["side"],
            visible_markers["count"],
            strict=False,
        ):
            side_text = str(side_value).upper()
            count = int(count_value)
            chart_markers.append(
                {
                    "time": int(timestamp_value),
                    "position": "belowBar" if side_text == "BUY" else "aboveBar",
                    "color": "#ff5b6e" if side_text == "BUY" else "#1fc8a5",
                    "shape": "arrowUp" if side_text == "BUY" else "arrowDown",
                    "text": _marker_count_text(side_text, count),
                }
            )

    visible_action = _visible_order_markers(action_marker, rows, align_mode=marker_alignment)
    if not visible_action.empty:
        action_times = _epoch_seconds(visible_action["timestamp"], market)
        for timestamp_value in action_times:
            chart_markers.append(
                {
                    "time": int(timestamp_value),
                    "position": "inBar",
                    "color": "#ffb000",
                    "shape": "circle",
                    "text": "",
                }
            )

    lower_panel_payload = _lower_panel_payload(rows, market=market, lower_panel=lower_panel)
    payload = {
        "symbol": symbol,
        "title": title,
        "subtitle": subtitle,
        "marketTimeZone": MARKET_TIMEZONES.get(market, "UTC"),
        "hasIntraday": has_intraday,
        "pricePrecision": price_precision,
        "lastPrice": round(float(rows["close"].iloc[-1]), price_precision),
        "lastLabel": price_line_label or "现价 / Last",
        "mainSeries": main_series,
        "candles": candle_rows,
        "overlays": overlay_rows,
        "markers": chart_markers,
        "lowerPanel": lower_panel_payload,
        "interactionHint": "双指平移 · Pinch 缩放 · 双击重置 / Two-finger pan · Pinch to zoom · Double click to reset",
        "branding": "Powered by TradingView Lightweight Charts",
    }

    payload_json = json.dumps(payload, ensure_ascii=False)
    chart_dom_id = chart_id.replace(".", "-").replace(" ", "-")
    height = 660
    template = """
<style>
  html, body {
    margin: 0;
    padding: 0;
    background: #0b1016;
    overflow: hidden;
    overscroll-behavior: contain;
  }
  #shell-__ID__ {
    width: 100%;
    height: __HEIGHT__px;
    background: linear-gradient(180deg, #090f15 0%, #0f1720 100%);
    border: 1px solid #1a2833;
    border-radius: 18px;
    overflow: hidden;
    color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', sans-serif;
  }
  #top-__ID__ {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 7px 12px 6px 12px;
    border-bottom: 1px solid #16242e;
    background: rgba(8, 12, 17, 0.94);
  }
  #title-__ID__ {
    font-size: 13px;
    font-weight: 700;
    color: #f3f7fb;
  }
  #subtitle-__ID__ {
    margin-top: 1px;
    font-size: 9px;
    color: #7f95aa;
  }
  #meta-__ID__ {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 4px;
    align-content: flex-start;
  }
  .chip-__ID__ {
    padding: 2px 6px;
    border-radius: 999px;
    background: rgba(35, 50, 65, 0.86);
    color: #a9bfd0;
    font-size: 8px;
    line-height: 1.2;
    white-space: nowrap;
  }
  #legend-__ID__ {
    padding: 5px 10px 4px 10px;
    border-bottom: 1px solid #14212c;
    background: rgba(9, 15, 21, 0.9);
    font: 500 9px ui-monospace, SFMono-Regular, Menlo, monospace;
    line-height: 1.35;
    color: #c5d3df;
  }
  #chart-__ID__ {
    width: 100%;
    height: __CHART_HEIGHT__px;
    touch-action: pan-x pan-y pinch-zoom;
  }
</style>
<div id="shell-__ID__">
  <div id="top-__ID__">
    <div>
      <div id="title-__ID__"></div>
      <div id="subtitle-__ID__"></div>
    </div>
    <div id="meta-__ID__"></div>
  </div>
  <div id="legend-__ID__"></div>
  <div id="chart-__ID__"></div>
</div>
<script>
(function() {
  const payload = __PAYLOAD__;
  const domId = "__ID__";
  const shellNode = document.getElementById(`shell-${domId}`);
  const chartNode = document.getElementById(`chart-${domId}`);
  const titleNode = document.getElementById(`title-${domId}`);
  const subtitleNode = document.getElementById(`subtitle-${domId}`);
  const metaNode = document.getElementById(`meta-${domId}`);
  const legendNode = document.getElementById(`legend-${domId}`);
  const scriptUrl = "__SCRIPT_URL__";

  function renderHeader() {
    if (titleNode) {
      titleNode.textContent = `${payload.symbol} · ${payload.title}`;
    }
    if (subtitleNode) {
      subtitleNode.textContent = payload.subtitle;
    }
    if (metaNode) {
      metaNode.innerHTML = [
        `<span class="chip-${domId}">${payload.lowerPanel.title}</span>`,
      ].join("");
    }
  }

  function lineStyle(styleName) {
    const styleEnum = window.LightweightCharts && window.LightweightCharts.LineStyle ? window.LightweightCharts.LineStyle : {};
    if (styleName === "dashed") return styleEnum.Dashed ?? 2;
    if (styleName === "dotted") return styleEnum.Dotted ?? 1;
    return styleEnum.Solid ?? 0;
  }

  function addSeriesCompat(chart, seriesType, options, paneIndex) {
    const definitions = {
      candlestick: window.LightweightCharts && window.LightweightCharts.CandlestickSeries,
      line: window.LightweightCharts && window.LightweightCharts.LineSeries,
      histogram: window.LightweightCharts && window.LightweightCharts.HistogramSeries,
      area: window.LightweightCharts && window.LightweightCharts.AreaSeries,
    };
    const legacyMethods = {
      candlestick: "addCandlestickSeries",
      line: "addLineSeries",
      histogram: "addHistogramSeries",
      area: "addAreaSeries",
    };
    if (typeof chart.addSeries === "function" && definitions[seriesType]) {
      return chart.addSeries(definitions[seriesType], options, paneIndex);
    }
    if (typeof chart[legacyMethods[seriesType]] === "function") {
      return chart[legacyMethods[seriesType]](options);
    }
    throw new Error(`Unsupported series type: ${seriesType}`);
  }

  function formatNumber(value, precision) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "N/A";
    return Number(value).toLocaleString(undefined, {
      minimumFractionDigits: precision,
      maximumFractionDigits: precision,
    });
  }

  function formatVolume(value) {
    return Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
  }

  function formatTimeLabel(epochSeconds, includeIntraday) {
    if (epochSeconds === undefined || epochSeconds === null) return "";
    const formatter = new Intl.DateTimeFormat("zh-CN", includeIntraday ? {
      timeZone: payload.marketTimeZone,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    } : {
      timeZone: payload.marketTimeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    return formatter.format(new Date(Number(epochSeconds) * 1000)).replace(",", "");
  }

  function extractValue(point) {
    if (point === null || point === undefined) return null;
    if (typeof point === "number") return point;
    if (typeof point === "object") {
      if (Object.prototype.hasOwnProperty.call(point, "value")) return point.value;
      if (Object.prototype.hasOwnProperty.call(point, "close")) return point.close;
    }
    return null;
  }

  function setLegend(state) {
    if (!legendNode || !state) return;
    const openValue = Number(state.open || 0);
    const highValue = Number(state.high || 0);
    const lowValue = Number(state.low || 0);
    const closeValue = Number(state.close || 0);
    const volumeValue = Number(state.volume || 0);
    const deltaValue = closeValue - openValue;
    const deltaPct = openValue ? (deltaValue / openValue) * 100 : 0;
    const deltaColor = deltaValue >= 0 ? "#ff5b6e" : "#1fc8a5";
    const overlayText = (state.overlayText || []).join(" · ");
    const lowerText = (state.lowerText || []).join(" · ");
    legendNode.innerHTML = `
      <div>${state.displayTime || ""}</div>
      <div>
        O <span style="color:#f3f7fb;">${formatNumber(openValue, payload.pricePrecision)}</span>
        H <span style="color:#f3f7fb;">${formatNumber(highValue, payload.pricePrecision)}</span>
        L <span style="color:#f3f7fb;">${formatNumber(lowValue, payload.pricePrecision)}</span>
        C <span style="color:#f3f7fb;">${formatNumber(closeValue, payload.pricePrecision)}</span>
        <span style="color:${deltaColor};margin-left:8px;">${deltaValue >= 0 ? "+" : ""}${formatNumber(deltaValue, payload.pricePrecision)} (${deltaPct.toFixed(2)}%)</span>
        · V <span style="color:#f3f7fb;">${formatVolume(volumeValue)}</span>
      </div>
      <div>${overlayText || "&nbsp;"}</div>
      <div>${lowerText || "&nbsp;"}</div>
    `;
  }

  function renderChart() {
    if (!window.LightweightCharts || !chartNode || !payload.candles.length) return;
    renderHeader();
    const crosshairMode = window.LightweightCharts.CrosshairMode ? window.LightweightCharts.CrosshairMode.Normal : 0;
    const chart = window.LightweightCharts.createChart(chartNode, {
      autoSize: true,
      layout: {
        background: { color: "#0b1016" },
        textColor: "#c5d3df",
        attributionLogo: false,
        panes: {
          separatorColor: "#20303c",
          separatorHoverColor: "#476583",
          enableResize: true,
        },
      },
      grid: {
        vertLines: { color: "#14212c", visible: true },
        horzLines: { color: "#14212c", visible: true },
      },
      crosshair: {
        mode: crosshairMode,
        vertLine: { color: "#617688", width: 1, labelBackgroundColor: "#243645" },
        horzLine: { color: "#617688", width: 1, labelBackgroundColor: "#243645" },
      },
      rightPriceScale: {
        borderColor: "#223241",
        scaleMargins: { top: 0.08, bottom: 0.12 },
      },
      leftPriceScale: { visible: false },
      timeScale: {
        borderColor: "#223241",
        timeVisible: payload.hasIntraday,
        secondsVisible: false,
        rightOffset: 10,
        barSpacing: payload.hasIntraday ? 7 : 11,
        minBarSpacing: 0.35,
        lockVisibleTimeRangeOnResize: true,
        allowBoldLabels: true,
        tickMarkFormatter: (time) => formatTimeLabel(time, payload.hasIntraday),
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      handleScale: {
        mouseWheel: true,
        pinch: true,
        axisPressedMouseMove: { time: true, price: false },
        axisDoubleClickReset: true,
      },
      kineticScroll: { mouse: true, touch: true },
    });

    const candleByTime = new Map(payload.candles.map((bar) => [Number(bar.time), bar]));
    const mainSeries = payload.mainSeries === "line"
      ? addSeriesCompat(chart, "area", {
          lineColor: "#4c78a8",
          lineWidth: 2,
          topColor: "rgba(76, 120, 168, 0.38)",
          bottomColor: "rgba(76, 120, 168, 0.02)",
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerRadius: 4,
        }, 0)
      : addSeriesCompat(chart, "candlestick", {
          upColor: "#ff5b6e",
          downColor: "#1fc8a5",
          borderUpColor: "#ff5b6e",
          borderDownColor: "#1fc8a5",
          wickUpColor: "#ff5b6e",
          wickDownColor: "#1fc8a5",
          priceLineVisible: false,
          lastValueVisible: false,
        }, 0);
    mainSeries.setData(
      payload.mainSeries === "line"
        ? payload.candles.map((bar) => ({ time: bar.time, value: bar.close }))
        : payload.candles
    );
    if (typeof mainSeries.createPriceLine === "function") {
      mainSeries.createPriceLine({
        price: payload.lastPrice,
        color: "#c99a53",
        lineWidth: 1,
        lineStyle: lineStyle("dashed"),
        axisLabelVisible: true,
        title: payload.lastLabel,
      });
    }

    const overlayRefs = [];
    payload.overlays.forEach((overlay) => {
      const lineSeries = addSeriesCompat(chart, "line", {
        color: overlay.color,
        lineWidth: overlay.name.includes("VWAP") ? 2 : 1.5,
        lineStyle: lineStyle(overlay.style || "solid"),
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        priceLineVisible: false,
      }, 0);
      lineSeries.setData(overlay.data);
      overlayRefs.push({ label: overlay.name, series: lineSeries });
    });

    if (payload.markers.length) {
      if (typeof window.LightweightCharts.createSeriesMarkers === "function") {
        window.LightweightCharts.createSeriesMarkers(mainSeries, payload.markers);
      } else if (typeof mainSeries.setMarkers === "function") {
        mainSeries.setMarkers(payload.markers);
      }
    }

    const lowerRefs = [];
    if (payload.lowerPanel.histogram && payload.lowerPanel.histogram.data.length) {
      const histogramSeries = addSeriesCompat(chart, "histogram", {
        priceFormat: payload.lowerPanel.histogram.priceFormat === "volume"
          ? { type: "volume" }
          : { type: "price", precision: 4, minMove: 0.0001 },
        priceLineVisible: false,
        lastValueVisible: false,
      }, 1);
      histogramSeries.setData(payload.lowerPanel.histogram.data);
      if (typeof histogramSeries.priceScale === "function") {
        histogramSeries.priceScale().applyOptions({ scaleMargins: { top: 0.15, bottom: 0.04 } });
      }
      lowerRefs.push({ label: payload.lowerPanel.histogram.name, series: histogramSeries, precision: 4 });
    }

    payload.lowerPanel.lines.forEach((lineDef) => {
      const studyLine = addSeriesCompat(chart, "line", {
        color: lineDef.color,
        lineWidth: 2,
        lineStyle: lineStyle(lineDef.style || "solid"),
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        priceLineVisible: false,
      }, 1);
      studyLine.setData(lineDef.data);
      lowerRefs.push({ label: lineDef.name, series: studyLine, precision: 2 });
    });

    payload.lowerPanel.references.forEach((lineDef) => {
      const referenceLine = addSeriesCompat(chart, "line", {
        color: lineDef.color,
        lineWidth: 1,
        lineStyle: lineStyle(lineDef.style || "dashed"),
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        priceLineVisible: false,
      }, 1);
      referenceLine.setData(lineDef.data);
    });

    if (typeof chart.panes === "function") {
      const panes = chart.panes();
      if (panes[0] && typeof panes[0].setHeight === "function") panes[0].setHeight(395);
      if (panes[1] && typeof panes[1].setHeight === "function") panes[1].setHeight(95);
    }

    const lastBar = payload.candles[payload.candles.length - 1];
    const buildState = (timeValue, candlePoint, seriesData) => ({
      displayTime: timeValue ? formatTimeLabel(timeValue, payload.hasIntraday) : lastBar.displayTime,
      open: candlePoint && candlePoint.open !== undefined ? candlePoint.open : lastBar.open,
      high: candlePoint && candlePoint.high !== undefined ? candlePoint.high : lastBar.high,
      low: candlePoint && candlePoint.low !== undefined ? candlePoint.low : lastBar.low,
      close: candlePoint && candlePoint.close !== undefined ? candlePoint.close : lastBar.close,
      volume: candlePoint && candlePoint.volume !== undefined ? candlePoint.volume : lastBar.volume,
      overlayText: overlayRefs
        .map((ref) => {
          const point = seriesData && seriesData.get(ref.series);
          const value = extractValue(point);
          return value === null ? null : `${ref.label} ${formatNumber(value, payload.pricePrecision)}`;
        })
        .filter(Boolean),
      lowerText: lowerRefs
        .map((ref) => {
          const point = seriesData && seriesData.get(ref.series);
          const value = extractValue(point);
          return value === null ? null : `${ref.label} ${formatNumber(value, ref.precision || 2)}`;
        })
        .filter(Boolean),
    });

    setLegend(buildState(lastBar.time, lastBar, null));
    chart.subscribeCrosshairMove((param) => {
      if (!param || param.time === undefined || param.time === null) {
        setLegend(buildState(lastBar.time, lastBar, null));
        return;
      }
      const mainPoint = param.seriesData ? param.seriesData.get(mainSeries) : null;
      const candlePoint = candleByTime.get(Number(param.time)) || mainPoint;
      setLegend(buildState(param.time, candlePoint, param.seriesData));
    });

    chartNode.addEventListener("dblclick", () => {
      if (chart && chart.timeScale && typeof chart.timeScale().fitContent === "function") {
        chart.timeScale().fitContent();
      }
    });

    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(shellNode);

    function logicalRange() {
      const timeScale = chart && typeof chart.timeScale === "function" ? chart.timeScale() : null;
      if (!timeScale || typeof timeScale.getVisibleLogicalRange !== "function") return null;
      const range = timeScale.getVisibleLogicalRange();
      if (!range || !Number.isFinite(range.from) || !Number.isFinite(range.to)) return null;
      return { timeScale, range };
    }

    function zoomAround(factor, anchorCoordinate) {
      if (!Number.isFinite(factor) || factor <= 0) return;
      const logical = logicalRange();
      if (!logical) return;

      const { timeScale, range } = logical;
      const span = range.to - range.from;
      if (!Number.isFinite(span) || span <= 0.001) return;

      const rect = chartNode.getBoundingClientRect();
      const width = Math.max(rect.width, 1);
      const clampedAnchor = Math.max(0, Math.min(width, anchorCoordinate));

      let anchorLogical = typeof timeScale.coordinateToLogical === "function"
        ? timeScale.coordinateToLogical(clampedAnchor)
        : null;
      if (!Number.isFinite(anchorLogical)) {
        anchorLogical = range.from + span * (clampedAnchor / width);
      }

      const zoomFactor = Math.max(0.25, Math.min(4.0, factor));
      let nextSpan = span / zoomFactor;
      const minSpan = payload.hasIntraday ? 12 : 24;
      const maxSpan = Math.max(payload.candles.length * 3, minSpan * 2);
      nextSpan = Math.max(minSpan, Math.min(maxSpan, nextSpan));

      const leftRatio = (anchorLogical - range.from) / span;
      const rightRatio = (range.to - anchorLogical) / span;
      let nextFrom = anchorLogical - nextSpan * leftRatio;
      let nextTo = anchorLogical + nextSpan * rightRatio;

      const lowerBound = -8;
      const upperBound = payload.candles.length + 12;
      if (nextFrom < lowerBound) {
        nextTo += lowerBound - nextFrom;
        nextFrom = lowerBound;
      }
      if (nextTo > upperBound) {
        nextFrom -= nextTo - upperBound;
        nextTo = upperBound;
      }
      if (typeof timeScale.setVisibleLogicalRange === "function") {
        timeScale.setVisibleLogicalRange({ from: nextFrom, to: nextTo });
      }
    }

    chartNode.addEventListener("wheel", (event) => {
      if (!(event.ctrlKey || event.metaKey)) return;
      event.preventDefault();
      event.stopPropagation();
      const rect = chartNode.getBoundingClientRect();
      const anchor = (event.clientX || (rect.left + rect.width / 2)) - rect.left;
      const factor = Math.exp((-event.deltaY || 0) * 0.0068);
      zoomAround(factor, anchor);
    }, { passive: false });

    let gestureScale = null;
    chartNode.addEventListener("gesturestart", (event) => {
      event.preventDefault();
      gestureScale = Number(event.scale || 1);
    }, { passive: false });
    chartNode.addEventListener("gesturechange", (event) => {
      if (gestureScale === null) return;
      event.preventDefault();
      const rect = chartNode.getBoundingClientRect();
      const anchor = (event.clientX || (rect.left + rect.width / 2)) - rect.left;
      const nextScale = Number(event.scale || 1);
      const deltaScale = nextScale / Math.max(gestureScale, 0.0001);
      const factor = Math.pow(deltaScale, 2.2);
      zoomAround(factor, anchor);
      gestureScale = nextScale;
    }, { passive: false });
    chartNode.addEventListener("gestureend", () => {
      gestureScale = null;
    }, { passive: false });
  }

  renderHeader();
  if (window.LightweightCharts && typeof window.LightweightCharts.createChart === "function") {
    renderChart();
    return;
  }

  const existing = document.querySelector('script[data-lightweight-charts]');
  if (!existing) {
    const script = document.createElement("script");
    script.src = scriptUrl;
    script.async = true;
    script.dataset.lightweightCharts = "true";
    script.onload = renderChart;
    document.head.appendChild(script);
    return;
  }

  const wait = () => {
    if (window.LightweightCharts && typeof window.LightweightCharts.createChart === "function") {
      renderChart();
    } else {
      window.setTimeout(wait, 60);
    }
  };
  wait();
})();
</script>
"""
    return (
        template.replace("__PAYLOAD__", payload_json)
        .replace("__ID__", chart_dom_id)
        .replace("__SCRIPT_URL__", LIGHTWEIGHT_CHARTS_SCRIPT_URL)
        .replace("__HEIGHT__", str(height))
        .replace("__CHART_HEIGHT__", str(height - 82))
    )


def _lightweight_chart_component(
    bars: pd.DataFrame,
    *,
    market: str,
    symbol: str,
    chart_id: str,
    title: str,
    subtitle: str,
    overlays: list[tuple[str, str, str]] | None = None,
    order_markers: pd.DataFrame | None = None,
    action_marker: pd.DataFrame | None = None,
    price_line_label: str | None = None,
    marker_alignment: str = "bar",
    lower_panel: str = "volume",
    main_series: str = "candles",
) -> None:
    components.html(
        _lightweight_chart_html(
            bars,
            market=market,
            symbol=symbol,
            chart_id=chart_id,
            title=title,
            subtitle=subtitle,
            overlays=overlays,
            order_markers=order_markers,
            action_marker=action_marker,
            price_line_label=price_line_label,
            marker_alignment=marker_alignment,
            lower_panel=lower_panel,
            main_series=main_series,
        ),
        height=740,
    )


def _candlestick_chart(
    bars: pd.DataFrame,
    *,
    overlays: list[tuple[str, str, str]] | None = None,
    order_markers: pd.DataFrame | None = None,
    action_marker: pd.DataFrame | None = None,
    chart_id: str,
) -> go.Figure:
    data = bars.copy()
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.78, 0.22],
        vertical_spacing=0.04,
    )

    figure.add_trace(
        go.Candlestick(
            x=data["timestamp"],
            open=data["open"],
            high=data["high"],
            low=data["low"],
            close=data["close"],
            name="K线 / Candles",
            increasing_line_color="#cf3c3c",
            increasing_fillcolor="#cf3c3c",
            decreasing_line_color="#1d9a6c",
            decreasing_fillcolor="#1d9a6c",
            whiskerwidth=0.4,
        ),
        row=1,
        col=1,
    )

    for label, column, color_value in overlays or []:
        if column not in data.columns:
            continue
        figure.add_trace(
            go.Scatter(
                x=data["timestamp"],
                y=data[column],
                mode="lines",
                name=label,
                line={"color": color_value, "width": 2},
                hovertemplate="%{x|%Y-%m-%d %H:%M}<br>" + f"{label}: " + "%{y:.2f}<extra></extra>",
            ),
            row=1,
            col=1,
        )

    if order_markers is not None and not order_markers.empty:
        buy_markers = order_markers[order_markers["side"] == "BUY"]
        sell_markers = order_markers[order_markers["side"] == "SELL"]
        if not buy_markers.empty:
            figure.add_trace(
                go.Scatter(
                    x=buy_markers["timestamp"],
                    y=buy_markers["price"],
                    mode="markers+text",
                    name="买点 / Buy",
                    text=["B"] * len(buy_markers),
                    textposition="top center",
                    marker={"symbol": "triangle-up", "size": 12, "color": "#cf3c3c", "line": {"width": 1, "color": "#ffffff"}},
                    hovertemplate="%{x|%Y-%m-%d %H:%M}<br>买点 / Buy: %{y:.2f}<extra></extra>",
                ),
                row=1,
                col=1,
            )
        if not sell_markers.empty:
            figure.add_trace(
                go.Scatter(
                    x=sell_markers["timestamp"],
                    y=sell_markers["price"],
                    mode="markers+text",
                    name="卖点 / Sell",
                    text=["S"] * len(sell_markers),
                    textposition="bottom center",
                    marker={"symbol": "triangle-down", "size": 12, "color": "#1d9a6c", "line": {"width": 1, "color": "#ffffff"}},
                    hovertemplate="%{x|%Y-%m-%d %H:%M}<br>卖点 / Sell: %{y:.2f}<extra></extra>",
                ),
                row=1,
                col=1,
            )

    if action_marker is not None and not action_marker.empty:
        figure.add_trace(
            go.Scatter(
                x=action_marker["timestamp"],
                y=action_marker["price"],
                mode="markers+text",
                name="策略动作 / Action",
                text=action_marker["label"],
                textposition="top right",
                marker={"symbol": "diamond", "size": 14, "color": "#ff9f1a", "line": {"width": 1, "color": "#ffffff"}},
                hovertemplate="%{x|%Y-%m-%d %H:%M}<br>%{text}: %{y:.2f}<extra></extra>",
            ),
            row=1,
            col=1,
        )

    figure.add_trace(
        go.Bar(
            x=data["timestamp"],
            y=data["volume"],
            name="成交量 / Volume",
            marker={"color": _volume_colors(data)},
            opacity=0.55,
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>成交量 / Volume: %{y:.0f}<extra></extra>",
        ),
        row=2,
        col=1,
    )

    latest_close = float(data["close"].iloc[-1])
    figure.update_layout(
        height=690,
        margin={"l": 10, "r": 10, "t": 12, "b": 10},
        plot_bgcolor="#0d141b",
        paper_bgcolor="#0d141b",
        font={"color": "#d7e2ea", "size": 12},
        hovermode="closest",
        dragmode="zoom",
        showlegend=True,
        uirevision=chart_id,
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.01,
            "xanchor": "left",
            "x": 0.0,
            "bgcolor": "rgba(13,20,27,0.78)",
        },
        xaxis_rangeslider_visible=False,
    )
    figure.update_xaxes(
        showgrid=True,
        gridcolor="#1e2a34",
        showspikes=False,
        fixedrange=False,
        type="date",
        zeroline=False,
        showline=True,
        linecolor="#2b3944",
        tickfont={"color": "#92a4b3"},
    )
    figure.update_yaxes(
        side="right",
        showgrid=True,
        gridcolor="#1e2a34",
        zeroline=False,
        showspikes=False,
        fixedrange=False,
        showline=True,
        linecolor="#2b3944",
        tickfont={"color": "#92a4b3"},
        row=1,
        col=1,
    )
    figure.update_yaxes(
        side="right",
        showgrid=True,
        gridcolor="#15202a",
        zeroline=False,
        showline=True,
        linecolor="#2b3944",
        tickfont={"color": "#8293a0"},
        row=2,
        col=1,
        title_text="量 / Vol",
    )
    figure.update_yaxes(
        tickmode="array",
        tickvals=_build_price_ticks(data["close"]),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=[data["timestamp"].iloc[0], data["timestamp"].iloc[-1]],
            y=[latest_close, latest_close],
            mode="lines",
            name="现价 / Last",
            line={"color": "#a98e6a", "width": 1, "dash": "dot"},
            hoverinfo="skip",
            showlegend=False,
        ),
        row=1,
        col=1,
    )
    return figure


def _order_book_side_frame(levels: list[tuple | list], side: str) -> pd.DataFrame:
    rows = []
    cumulative = 0.0
    for idx, level in enumerate(levels, start=1):
        if not level:
            continue
        price = float(level[0]) if len(level) > 0 else 0.0
        size = float(level[1]) if len(level) > 1 else 0.0
        orders = int(level[2]) if len(level) > 2 else 0
        cumulative += size
        rows.append(
            {
                "level": idx,
                "price": price,
                "size": size,
                "orders": orders,
                "cum_size": cumulative,
                "side": side,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["level", "price", "size", "orders", "cum_size", "side"])
    return frame


def _lob_ladder_view(order_book: dict | None, depth: int) -> pd.DataFrame:
    if not order_book:
        return pd.DataFrame()
    bids = _order_book_side_frame(order_book.get("Bid", [])[:depth], "bid")
    asks = _order_book_side_frame(order_book.get("Ask", [])[:depth], "ask")
    rows = []
    max_levels = max(len(bids), len(asks))
    for idx in range(max_levels):
        ask = asks.iloc[idx] if idx < len(asks) else None
        bid = bids.iloc[idx] if idx < len(bids) else None
        rows.append(
            {
                "卖价 / Ask": ask["price"] if ask is not None else None,
                "卖量 / Ask Size": ask["size"] if ask is not None else None,
                "卖单数 / Ask Orders": ask["orders"] if ask is not None else None,
                "卖累计 / Ask Cum": ask["cum_size"] if ask is not None else None,
                "档位 / Level": idx + 1,
                "买价 / Bid": bid["price"] if bid is not None else None,
                "买量 / Bid Size": bid["size"] if bid is not None else None,
                "买单数 / Bid Orders": bid["orders"] if bid is not None else None,
                "买累计 / Bid Cum": bid["cum_size"] if bid is not None else None,
            }
        )
    return pd.DataFrame(rows)


def _lob_depth_chart(order_book: dict | None, depth: int, symbol: str) -> go.Figure:
    figure = go.Figure()
    if not order_book:
        figure.update_layout(
            height=420,
            plot_bgcolor="#0d141b",
            paper_bgcolor="#0d141b",
            font={"color": "#d7e2ea"},
        )
        return figure

    bids = _order_book_side_frame(order_book.get("Bid", [])[:depth], "bid")
    asks = _order_book_side_frame(order_book.get("Ask", [])[:depth], "ask")

    if not bids.empty:
        figure.add_trace(
            go.Bar(
                x=-bids["size"],
                y=bids["price"],
                orientation="h",
                name="买盘深度 / Bid Depth",
                marker={"color": "#1d9a6c"},
                hovertemplate="Bid %{y:.2f}<br>Size %{customdata[0]:,.0f}<br>Orders %{customdata[1]:,.0f}<br>Cum %{customdata[2]:,.0f}<extra></extra>",
                customdata=bids[["size", "orders", "cum_size"]],
            )
        )
    if not asks.empty:
        figure.add_trace(
            go.Bar(
                x=asks["size"],
                y=asks["price"],
                orientation="h",
                name="卖盘深度 / Ask Depth",
                marker={"color": "#cf3c3c"},
                hovertemplate="Ask %{y:.2f}<br>Size %{customdata[0]:,.0f}<br>Orders %{customdata[1]:,.0f}<br>Cum %{customdata[2]:,.0f}<extra></extra>",
                customdata=asks[["size", "orders", "cum_size"]],
            )
        )

    figure.update_layout(
        height=420,
        barmode="overlay",
        bargap=0.08,
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        plot_bgcolor="#0d141b",
        paper_bgcolor="#0d141b",
        font={"color": "#d7e2ea", "size": 12},
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.01,
            "xanchor": "left",
            "x": 0.0,
            "bgcolor": "rgba(13,20,27,0.78)",
        },
        uirevision=f"{symbol}-lob",
    )
    figure.update_xaxes(
        title_text="买量 < 0 | 深度 / Depth | > 0 卖量",
        showgrid=True,
        gridcolor="#1e2a34",
        zeroline=True,
        zerolinecolor="#d0b17f",
        tickfont={"color": "#92a4b3"},
    )
    figure.update_yaxes(
        title_text="价格 / Price",
        side="right",
        showgrid=True,
        gridcolor="#15202a",
        tickfont={"color": "#92a4b3"},
    )
    return figure


def _build_order_markers(order_history: pd.DataFrame, code: str) -> pd.DataFrame:
    if order_history.empty:
        return pd.DataFrame(columns=["timestamp", "price", "label", "side"])
    rows = order_history[order_history["code"] == code].copy()
    rows = rows[pd.to_numeric(rows["dealt_qty"], errors="coerce").fillna(0) > 0]
    if rows.empty:
        return pd.DataFrame(columns=["timestamp", "price", "label", "side"])
    rows["timestamp"] = pd.to_datetime(rows["updated_time"])
    rows["price"] = pd.to_numeric(rows["dealt_avg_price"], errors="coerce").where(
        pd.to_numeric(rows["dealt_avg_price"], errors="coerce") > 0,
        pd.to_numeric(rows["price"], errors="coerce"),
    )
    rows["side"] = rows["trd_side"].astype(str)
    rows["label"] = rows["side"].map({"BUY": "买点 / Buy", "SELL": "卖点 / Sell"}).fillna(rows["side"])
    return rows[["timestamp", "price", "label", "side"]].dropna(subset=["timestamp", "price"])


def _order_book_view(order_book: dict | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not order_book:
        empty = pd.DataFrame(columns=["价格 / Price", "数量 / Size", "订单数 / Orders"])
        return empty, empty

    def _side_frame(levels: list[tuple | list]) -> pd.DataFrame:
        rows = []
        for level in levels:
            if not level:
                continue
            rows.append(
                {
                    "价格 / Price": level[0] if len(level) > 0 else None,
                    "数量 / Size": level[1] if len(level) > 1 else None,
                    "订单数 / Orders": level[2] if len(level) > 2 else None,
                }
            )
        return pd.DataFrame(rows, columns=["价格 / Price", "数量 / Size", "订单数 / Orders"])

    bid = _side_frame(order_book.get("Bid", []))
    ask = _side_frame(order_book.get("Ask", []))
    return bid, ask


def _ticks_view(ticks: pd.DataFrame) -> pd.DataFrame:
    if ticks.empty:
        return ticks
    columns = [column for column in ["time", "price", "volume", "ticker_direction"] if column in ticks.columns]
    frame = ticks.loc[:, columns].copy()
    return frame.rename(
        columns={
            "time": "时间 / Time",
            "price": "价格 / Price",
            "volume": "数量 / Volume",
            "ticker_direction": "方向 / Direction",
        }
    )


PLOTLY_CONFIG = {
    "displaylogo": False,
    "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"],
    "scrollZoom": True,
}


def _action_label(selected_symbol: str, feature_map: dict[str, object], plan, held_symbols: set[str]) -> tuple[str, str]:
    feature = feature_map.get(selected_symbol)
    if selected_symbol in plan.target_weights and selected_symbol in held_symbols:
        return "持有 / Hold", "#ff9f1a"
    if selected_symbol in plan.target_weights:
        return "买点 / Buy", "#cf3c3c"
    if selected_symbol in held_symbols:
        return "卖点 / Exit", "#1d9a6c"
    if feature is not None and getattr(feature, "eligible", False):
        return "观察 / Watch", "#4c78a8"
    return "观望 / Observe", "#7f8c8d"


def _selected_symbol_from_table(event, table: pd.DataFrame, symbol_column: str) -> str | None:
    selection = getattr(event, "selection", None)
    rows = getattr(selection, "rows", None) if selection is not None else None
    if rows is None and isinstance(event, dict):
        rows = event.get("selection", {}).get("rows", [])
    rows = rows or []
    if not rows:
        return None
    return str(table.iloc[rows[0]][symbol_column])


def _build_symbol_context(
    trader: FutuPaperTrader,
    settings,
    selected_symbol: str,
    selected_feature,
    plan,
    held_symbols: set[str],
    order_history: pd.DataFrame,
    *,
    depth: int,
) -> SymbolContext:
    trader.subscribe_types([selected_symbol], ["K_1M", "K_DAY", "ORDER_BOOK", "TICKER"])
    snapshot = trader.get_snapshots([selected_symbol]).loc[selected_symbol]
    ticks = trader.get_recent_tickers(selected_symbol, settings.fusion_tick_window)
    order_book = trader.get_order_book_safe(selected_symbol, depth)
    buy_sell_points = _build_order_markers(order_history, selected_symbol)
    action_label, _ = _action_label(selected_symbol, {selected_symbol: selected_feature} if selected_feature else {}, plan, held_symbols)
    bid_levels = order_book.get("Bid", []) if order_book else []
    ask_levels = order_book.get("Ask", []) if order_book else []
    best_bid = float(bid_levels[0][0]) if bid_levels else 0.0
    best_ask = float(ask_levels[0][0]) if ask_levels else 0.0
    best_bid_size = float(bid_levels[0][1]) if bid_levels else 0.0
    best_ask_size = float(ask_levels[0][1]) if ask_levels else 0.0
    spread = best_ask - best_bid if best_bid and best_ask else _safe_float(snapshot.get("price_spread"))
    mid_price = (best_bid + best_ask) / 2 if best_bid and best_ask else _safe_float(snapshot.get("last_price"))
    total_bid_depth = sum(float(level[1]) for level in bid_levels[:depth]) if bid_levels else 0.0
    total_ask_depth = sum(float(level[1]) for level in ask_levels[:depth]) if ask_levels else 0.0
    lob_imbalance = (
        (total_bid_depth - total_ask_depth) / (total_bid_depth + total_ask_depth)
        if (total_bid_depth + total_ask_depth) > 0
        else 0.0
    )
    return SymbolContext(
        snapshot=snapshot,
        ticks=ticks,
        order_book=order_book,
        buy_sell_points=buy_sell_points,
        action_label=action_label,
        best_bid=best_bid,
        best_ask=best_ask,
        best_bid_size=best_bid_size,
        best_ask_size=best_ask_size,
        spread=spread,
        mid_price=mid_price,
        total_bid_depth=total_bid_depth,
        total_ask_depth=total_ask_depth,
        lob_imbalance=lob_imbalance,
    )


def _render_terminal_symbol_header(selected_symbol: str, context: SymbolContext) -> None:
    last_price = _safe_float(context.snapshot.get("last_price"))
    prev_close = max(_safe_float(context.snapshot.get("prev_close_price")), 1e-9)
    change_value = last_price - prev_close
    change_pct = last_price / prev_close - 1
    direction_class = "terminal-up" if change_value >= 0 else "terminal-down"
    st.markdown(
        f"""
<div class="terminal-shell">
  <div class="terminal-header">
    <div>
      <div class="terminal-title">{selected_symbol.split('.', 1)[-1]} {_symbol_display_name(selected_symbol, context.snapshot)}</div>
      <div class="terminal-subtitle">图表 / Chart · 行情 / Quote · L2 / Order Book · 逐笔 / Ticks</div>
    </div>
    <div style="text-align:right;">
      <div class="terminal-price {direction_class}">{last_price:,.3f}</div>
      <div class="terminal-subtitle {direction_class}">{change_value:+.3f} · {change_pct:+.2%}</div>
    </div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def _render_terminal_watchlist(
    watchlist_rows: pd.DataFrame,
    positions: pd.DataFrame,
    selected_symbol: str,
) -> str:
    st.markdown('<div class="terminal-shell">', unsafe_allow_html=True)
    st.markdown('<div class="terminal-panel-title">自选 / Watchlist</div>', unsafe_allow_html=True)
    mode = st.segmented_control(
        "列表 / List",
        options=["全部 / All", "持仓 / Held", "观察 / Watch"],
        default="全部 / All",
        selection_mode="single",
        key="terminal-watchlist-mode",
    )

    held_symbols = set(positions["标的 / Symbol"].tolist()) if not positions.empty else set()
    left_view = watchlist_rows.copy()
    if left_view.empty and positions.empty:
        st.info("当前没有可展示标的 / No symbols to display.")
        st.markdown("</div>", unsafe_allow_html=True)
        return selected_symbol

    if not left_view.empty:
        left_view["名称代码 / Symbol"] = left_view["标的 / Symbol"].astype(str)
        left_view["最新价 / Last"] = pd.to_numeric(left_view["当前价 / Last"], errors="coerce")
        left_view["涨跌幅 / Change"] = pd.to_numeric(left_view["Gap%"], errors="coerce")
        left_view = left_view[["名称代码 / Symbol", "最新价 / Last", "涨跌幅 / Change", "状态 / Status"]]
    else:
        left_view = pd.DataFrame(columns=["名称代码 / Symbol", "最新价 / Last", "涨跌幅 / Change", "状态 / Status"])

    if mode == "持仓 / Held":
        left_view = left_view[left_view["名称代码 / Symbol"].isin(held_symbols)]
    elif mode == "观察 / Watch":
        left_view = left_view[~left_view["名称代码 / Symbol"].isin(held_symbols)]

    if left_view.empty:
        st.info("当前筛选下没有标的 / No symbols under this filter.")
        st.markdown("</div>", unsafe_allow_html=True)
        return selected_symbol

    event = st.dataframe(
        left_view,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="terminal_watchlist_table",
    )
    picked = _selected_symbol_from_table(event, left_view, "名称代码 / Symbol")
    st.markdown("</div>", unsafe_allow_html=True)
    return picked or selected_symbol


def _render_terminal_quote_panel(
    *,
    selected_symbol: str,
    context: SymbolContext,
    position_row: pd.Series | None,
    selected_feature,
    filled_order_history: pd.DataFrame,
    lob_depth: int,
) -> None:
    snapshot = context.snapshot
    last_price = _safe_float(snapshot.get("last_price"))
    prev_close = max(_safe_float(snapshot.get("prev_close_price")), 1e-9)
    change_value = last_price - prev_close
    change_pct = last_price / prev_close - 1
    direction_class = "terminal-up" if change_value >= 0 else "terminal-down"
    st.markdown(
        f"""
<div class="terminal-shell">
  <div class="terminal-panel-title">{selected_symbol.split('.', 1)[-1]} {_symbol_display_name(selected_symbol, snapshot)}</div>
  <div class="terminal-price {direction_class}" style="margin-bottom:8px;">{last_price:,.3f}</div>
  <div class="terminal-caption {direction_class}" style="margin-bottom:12px;">{change_value:+.3f} · {change_pct:+.2%}</div>
  <div class="terminal-mini-grid">
    <div class="terminal-mini-card"><div class="terminal-mini-label">最高 / High</div><div class="terminal-mini-value">{_safe_float(snapshot.get("high_price")):.3f}</div></div>
    <div class="terminal-mini-card"><div class="terminal-mini-label">最低 / Low</div><div class="terminal-mini-value">{_safe_float(snapshot.get("low_price")):.3f}</div></div>
    <div class="terminal-mini-card"><div class="terminal-mini-label">今开 / Open</div><div class="terminal-mini-value">{_safe_float(snapshot.get("open_price")):.3f}</div></div>
    <div class="terminal-mini-card"><div class="terminal-mini-label">昨收 / Prev Close</div><div class="terminal-mini-value">{_safe_float(snapshot.get("prev_close_price")):.3f}</div></div>
    <div class="terminal-mini-card"><div class="terminal-mini-label">成交量 / Volume</div><div class="terminal-mini-value">{_safe_float(snapshot.get("volume")):,.0f}</div></div>
    <div class="terminal-mini-card"><div class="terminal-mini-label">成交额 / Turnover</div><div class="terminal-mini-value">{_safe_float(snapshot.get("turnover")):,.0f}</div></div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )
    st.markdown('<div class="terminal-chip-row">', unsafe_allow_html=True)
    st.markdown(f'<span class="terminal-chip">策略动作 / Action: {context.action_label}</span>', unsafe_allow_html=True)
    st.markdown(f'<span class="terminal-chip">持仓 / Position: {int(_safe_float(position_row.get("qty"))) if position_row is not None else 0}</span>', unsafe_allow_html=True)
    st.markdown(f'<span class="terminal-chip">Spread: {context.spread:.4f}</span>', unsafe_allow_html=True)
    st.markdown(f'<span class="terminal-chip">LOB x{lob_depth}: {context.lob_imbalance:.2%}</span>', unsafe_allow_html=True)
    if selected_feature is not None:
        st.markdown(f'<span class="terminal-chip">Score: {getattr(selected_feature, "score", 0.0):.4f}</span>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    right_tab = st.segmented_control(
        "右侧面板 / Right Panel",
        options=["逐笔成交 / Ticks", "成交记录 / Fills", "深度摆盘 / Depth"],
        default="逐笔成交 / Ticks",
        selection_mode="single",
        key=f"{selected_symbol}-right-mode",
    )
    if right_tab == "逐笔成交 / Ticks":
        st.dataframe(_ticks_view(context.ticks), use_container_width=True, hide_index=True)
    elif right_tab == "成交记录 / Fills":
        current_fills = filled_order_history[filled_order_history["code"] == selected_symbol] if not filled_order_history.empty else filled_order_history
        if current_fills.empty:
            st.info("当前股票暂无成交记录 / No fills for current symbol.")
        else:
            st.dataframe(_orders_view(current_fills), use_container_width=True, hide_index=True)
    else:
        ladder_view = _lob_ladder_view(context.order_book, lob_depth)
        if ladder_view.empty:
            st.info("当前拿不到 L2 摆盘 / No L2 ladder data.")
        else:
            st.dataframe(ladder_view, use_container_width=True, hide_index=True)
        bid_view, ask_view = _order_book_view(context.order_book)
        mini_cols = st.columns(2)
        with mini_cols[0]:
            st.caption("买盘 / Bid")
            st.dataframe(bid_view.head(8), use_container_width=True, hide_index=True)
        with mini_cols[1]:
            st.caption("卖盘 / Ask")
            st.dataframe(ask_view.head(8), use_container_width=True, hide_index=True)


def _render_terminal_chart_panel(
    trader: FutuPaperTrader,
    settings,
    *,
    selected_symbol: str,
    selected_feature,
    context: SymbolContext,
) -> None:
    exchange_today = datetime.now(_market_timezone(settings.futu_trd_market)).date()
    st.markdown('<div class="terminal-shell">', unsafe_allow_html=True)
    intraday_tab, daily_tab = st.tabs(["分时 / Intraday", "日K / Daily"])
    with intraday_tab:
        intraday_controls = st.columns([0.95, 0.95, 0.95, 0.95, 0.8])
        with intraday_controls[0]:
            intraday_interval_label = st.selectbox(
                "周期 / Interval",
                options=list(INTRADAY_INTERVAL_OPTIONS.keys()),
                index=0,
                key=f"{selected_symbol}-terminal-intraday-interval",
            )
        with intraday_controls[1]:
            intraday_range_label = st.selectbox(
                "范围 / Range",
                options=list(INTRADAY_RANGE_OPTIONS.keys()),
                index=2,
                key=f"{selected_symbol}-terminal-intraday-range",
            )
        with intraday_controls[2]:
            intraday_style_label = st.selectbox(
                "主图 / Main",
                options=["K线 / Candles", "分时线 / Line"],
                index=0,
                key=f"{selected_symbol}-terminal-intraday-main-view",
            )
        with intraday_controls[3]:
            intraday_lower_label = st.selectbox(
                "副图 / Study",
                options=list(LOWER_PANEL_OPTIONS.keys()),
                index=0,
                key=f"{selected_symbol}-terminal-intraday-lower",
            )
        with intraday_controls[4]:
            include_extended = st.toggle(
                "盘前后 / Ext",
                value=False,
                key=f"{selected_symbol}-terminal-intraday-ext",
                disabled=settings.futu_trd_market != "US",
            )

        intraday_ktype = INTRADAY_INTERVAL_OPTIONS[intraday_interval_label]
        intraday_start, intraday_end = _history_window_from_days(INTRADAY_RANGE_OPTIONS[intraday_range_label] or 5, end_on=exchange_today)
        if intraday_range_label == "自定义 / Custom":
            custom_cols = st.columns(2)
            with custom_cols[0]:
                intraday_start = st.date_input("开始 / Start", value=exchange_today - timedelta(days=5), key=f"{selected_symbol}-terminal-intraday-start")
            with custom_cols[1]:
                intraday_end = st.date_input("结束 / End", value=exchange_today, key=f"{selected_symbol}-terminal-intraday-end")
        if intraday_start > intraday_end:
            st.error("分钟历史开始日期不能晚于结束日期 / Intraday start must be earlier than end.")
        else:
            intraday_start, was_clamped = _clamp_intraday_start(intraday_start, intraday_end, intraday_ktype)
            if was_clamped:
                st.info(f"{intraday_interval_label} 为了稳定性只保留最近 {MAX_INTRADAY_DAYS[intraday_ktype]} 天。")
            intraday_bars = _normalize_kline(
                trader.request_history_klines(
                    selected_symbol,
                    start=intraday_start.isoformat(),
                    end=intraday_end.isoformat(),
                    ktype=intraday_ktype,
                    extended_time=bool(include_extended and settings.futu_trd_market == "US"),
                    session="ALL" if include_extended and settings.futu_trd_market == "US" else "RTH",
                )
            )
            if intraday_bars.empty:
                st.info("没有可用的分钟 K 线 / No intraday K-line data.")
            else:
                intraday_bars["vwap"] = (
                    (intraday_bars["close"] * intraday_bars["volume"]).cumsum()
                    / intraday_bars["volume"].replace(0, pd.NA).cumsum()
                ).ffill().fillna(intraday_bars["close"])
                intraday_bars["ma5"] = intraday_bars["close"].rolling(5).mean()
                intraday_bars["ma10"] = intraday_bars["close"].rolling(10).mean()
                intraday_bars["ma20"] = intraday_bars["close"].rolling(20).mean()
                interval_minutes = {"K_1M": 1, "K_5M": 5, "K_15M": 15, "K_30M": 30, "K_60M": 60}
                opening_bars = max(1, settings.fusion_opening_range_minutes // interval_minutes.get(intraday_ktype, 1))
                intraday_bars["opening_range_high"] = intraday_bars["high"].head(min(opening_bars, len(intraday_bars))).max()
                intraday_bars = _decorate_indicator_columns(intraday_bars)
                action_marker = pd.DataFrame([{"timestamp": intraday_bars["timestamp"].iloc[-1], "price": intraday_bars["close"].iloc[-1], "label": context.action_label}])
                _lightweight_chart_component(
                    intraday_bars,
                    market=settings.futu_trd_market,
                    symbol=selected_symbol,
                    chart_id=f"{selected_symbol}-terminal-intraday-{intraday_ktype}",
                    title=f"{intraday_interval_label} 图",
                    subtitle=f"{intraday_start.isoformat()} ~ {intraday_end.isoformat()}",
                    overlays=[
                        ("MA10", "ma10", "#9dd866"),
                        ("VWAP", "vwap", "#4c78a8"),
                        ("开盘区间高点 / OR High", "opening_range_high", "#f58518"),
                    ],
                    order_markers=context.buy_sell_points,
                    action_marker=action_marker,
                    price_line_label="现价 / Last",
                    marker_alignment="bar",
                    lower_panel=LOWER_PANEL_OPTIONS[intraday_lower_label],
                    main_series="line" if intraday_style_label == "分时线 / Line" else "candles",
                )
                action_detail = context.action_label
                if selected_feature is not None:
                    action_detail = (
                        f"{action_detail} | Score {getattr(selected_feature, 'score', 0.0):.4f}"
                        f" | RelVol {getattr(selected_feature, 'rel_volume', 0.0):.2f}"
                    )
                st.caption(f"当前动作 / Current action: {action_detail}")

    with daily_tab:
        daily_controls = st.columns([1.0, 0.9, 0.8])
        with daily_controls[0]:
            daily_range_label = st.selectbox(
                "范围 / Range",
                options=list(DAILY_RANGE_OPTIONS.keys()),
                index=2,
                key=f"{selected_symbol}-terminal-daily-range",
            )
        with daily_controls[1]:
            daily_lower_label = st.selectbox(
                "副图 / Study",
                options=list(LOWER_PANEL_OPTIONS.keys()),
                index=1,
                key=f"{selected_symbol}-terminal-daily-lower",
            )
        with daily_controls[2]:
            st.metric("评分 / Score", f"{getattr(selected_feature, 'score', 0.0):.4f}" if selected_feature is not None else "N/A")
        daily_start, daily_end = _history_window_from_days(DAILY_RANGE_OPTIONS[daily_range_label] or 365, end_on=exchange_today)
        if daily_range_label == "自定义 / Custom":
            custom_daily_cols = st.columns(2)
            with custom_daily_cols[0]:
                daily_start = st.date_input("日线开始 / Daily Start", value=exchange_today - timedelta(days=365), key=f"{selected_symbol}-terminal-daily-start")
            with custom_daily_cols[1]:
                daily_end = st.date_input("日线结束 / Daily End", value=exchange_today, key=f"{selected_symbol}-terminal-daily-end")
        if daily_start > daily_end:
            st.error("日线开始日期不能晚于结束日期 / Daily start must be earlier than end.")
        else:
            daily_bars = _normalize_kline(
                trader.request_history_klines(
                    selected_symbol,
                    start=daily_start.isoformat(),
                    end=daily_end.isoformat(),
                    ktype="K_DAY",
                )
            )
            if daily_bars.empty:
                st.info("没有可用的日线 K 线 / No daily K-line data.")
            else:
                daily_bars["ma5"] = daily_bars["close"].rolling(5).mean()
                daily_bars["ma10"] = daily_bars["close"].rolling(10).mean()
                daily_bars["ma20"] = daily_bars["close"].rolling(20).mean()
                daily_bars["ma60"] = daily_bars["close"].rolling(60).mean()
                daily_bars = _decorate_indicator_columns(daily_bars)
                _lightweight_chart_component(
                    daily_bars,
                    market=settings.futu_trd_market,
                    symbol=selected_symbol,
                    chart_id=f"{selected_symbol}-terminal-daily",
                    title="日线图",
                    subtitle=f"{daily_start.isoformat()} ~ {daily_end.isoformat()}",
                    overlays=[
                        ("MA20", "ma20", "#4c78a8"),
                        ("MA60", "ma60", "#54a24b"),
                    ],
                    order_markers=context.buy_sell_points,
                    price_line_label="日线现价 / Last",
                    marker_alignment="daily",
                    lower_panel=LOWER_PANEL_OPTIONS[daily_lower_label],
                )
    st.markdown("</div>", unsafe_allow_html=True)


def _render_symbol_detail(
    trader: FutuPaperTrader,
    settings,
    selected_symbol: str,
    selected_feature,
    plan,
    held_symbols: set[str],
    position_row: pd.Series | None,
    order_history: pd.DataFrame,
) -> None:
    trader.subscribe_types([selected_symbol], ["K_1M", "K_DAY", "ORDER_BOOK", "TICKER"])
    exchange_today = datetime.now(_market_timezone(settings.futu_trd_market)).date()
    depth_options = [10, 20, 30, 50]
    default_depth = depth_options.index(20) if 20 in depth_options else 0
    lob_depth = st.segmented_control(
        "LOB 深度 / L2 Depth",
        options=depth_options,
        default=depth_options[default_depth],
        selection_mode="single",
        key=f"{selected_symbol}-lob-depth",
    )
    lob_depth = int(lob_depth or depth_options[default_depth])
    snapshot = trader.get_snapshots([selected_symbol]).loc[selected_symbol]
    ticks = trader.get_recent_tickers(selected_symbol, settings.fusion_tick_window)
    order_book = trader.get_order_book_safe(selected_symbol, lob_depth)
    buy_sell_points = _build_order_markers(order_history, selected_symbol)
    action_label, _action_color = _action_label(selected_symbol, {selected_symbol: selected_feature} if selected_feature else {}, plan, held_symbols)
    bid_levels = order_book.get("Bid", []) if order_book else []
    ask_levels = order_book.get("Ask", []) if order_book else []
    best_bid = float(bid_levels[0][0]) if bid_levels else 0.0
    best_ask = float(ask_levels[0][0]) if ask_levels else 0.0
    best_bid_size = float(bid_levels[0][1]) if bid_levels else 0.0
    best_ask_size = float(ask_levels[0][1]) if ask_levels else 0.0
    spread = best_ask - best_bid if best_bid and best_ask else 0.0
    mid_price = (best_bid + best_ask) / 2 if best_bid and best_ask else _safe_float(snapshot.get("last_price"))
    total_bid_depth = sum(float(level[1]) for level in bid_levels[:lob_depth]) if bid_levels else 0.0
    total_ask_depth = sum(float(level[1]) for level in ask_levels[:lob_depth]) if ask_levels else 0.0
    lob_imbalance = (
        (total_bid_depth - total_ask_depth) / (total_bid_depth + total_ask_depth)
        if (total_bid_depth + total_ask_depth) > 0
        else 0.0
    )

    metric_cols = st.columns(6)
    metric_cols[0].metric("最新价 / Last", f"{_safe_float(snapshot.get('last_price')):.2f}")
    metric_cols[1].metric("涨跌幅 / Change", _format_pct(_safe_float(snapshot.get("last_price")) / max(_safe_float(snapshot.get("prev_close_price")), 1e-9) - 1))
    metric_cols[2].metric("买卖差 / Spread", f"{spread:.4f}" if spread else f"{_safe_float(snapshot.get('price_spread')):.4f}")
    metric_cols[3].metric("策略动作 / Action", action_label)
    metric_cols[4].metric("持仓数量 / Position Qty", str(int(_safe_float(position_row.get("qty")))) if position_row is not None else "0")
    metric_cols[5].metric("策略评分 / Score", f"{getattr(selected_feature, 'score', 0.0):.4f}")

    lob_metrics = st.columns(6)
    lob_metrics[0].metric("最优买价 / Best Bid", f"{best_bid:.2f}" if best_bid else "N/A", f"{best_bid_size:,.0f}" if best_bid_size else None)
    lob_metrics[1].metric("最优卖价 / Best Ask", f"{best_ask:.2f}" if best_ask else "N/A", f"{best_ask_size:,.0f}" if best_ask_size else None)
    lob_metrics[2].metric("中间价 / Mid", f"{mid_price:.2f}" if mid_price else "N/A")
    lob_metrics[3].metric(f"买盘深度 / Bid x{lob_depth}", f"{total_bid_depth:,.0f}")
    lob_metrics[4].metric(f"卖盘深度 / Ask x{lob_depth}", f"{total_ask_depth:,.0f}")
    lob_metrics[5].metric("LOB 失衡 / Imbalance", f"{lob_imbalance:.2%}")

    lob_chart_col, lob_ladder_col = st.columns([1.1, 0.9])
    with lob_chart_col:
        st.markdown("**L2 深度分布 / L2 Depth Profile**")
        st.plotly_chart(
            _lob_depth_chart(order_book, lob_depth, selected_symbol),
            use_container_width=True,
            config=PLOTLY_CONFIG,
            key=f"{selected_symbol}-lob-chart",
        )
    with lob_ladder_col:
        st.markdown("**L2 梯队盘口 / L2 Ladder**")
        ladder_view = _lob_ladder_view(order_book, lob_depth)
        if ladder_view.empty:
            st.info("当前拿不到 L2 摆盘 / No L2 ladder data.")
        else:
            st.dataframe(ladder_view, use_container_width=True, hide_index=True)

    intraday_tab, daily_tab = st.tabs(["1分钟 K线 / 1-Minute K-Line", "日线 K线 / Daily K-Line"])
    with intraday_tab:
        intraday_controls = st.columns([0.9, 0.9, 0.85, 0.9, 0.9, 0.8])
        with intraday_controls[0]:
            intraday_interval_label = st.selectbox(
                "分钟周期 / Interval",
                options=list(INTRADAY_INTERVAL_OPTIONS.keys()),
                index=0,
                key=f"{selected_symbol}-intraday-interval",
            )
        with intraday_controls[1]:
            intraday_range_label = st.selectbox(
                "分钟历史 / Intraday History",
                options=list(INTRADAY_RANGE_OPTIONS.keys()),
                index=2,
                key=f"{selected_symbol}-intraday-range",
            )
        with intraday_controls[2]:
            include_extended = st.toggle(
                "盘前盘后 / Ext Hours",
                value=False,
                key=f"{selected_symbol}-intraday-ext",
                disabled=settings.futu_trd_market != "US",
            )
        with intraday_controls[3]:
            intraday_style_label = st.selectbox(
                "主图样式 / Main View",
                options=["K线 / Candles", "分时线 / Line"],
                index=0,
                key=f"{selected_symbol}-intraday-main-view",
            )
        with intraday_controls[4]:
            intraday_lower_label = st.selectbox(
                "副图指标 / Lower Study",
                options=list(LOWER_PANEL_OPTIONS.keys()),
                index=0,
                key=f"{selected_symbol}-intraday-lower-panel",
            )
        with intraday_controls[5]:
            st.metric("当前视图 / View", intraday_interval_label)

        intraday_ktype = INTRADAY_INTERVAL_OPTIONS[intraday_interval_label]
        intraday_start, intraday_end = _history_window_from_days(INTRADAY_RANGE_OPTIONS[intraday_range_label] or 5, end_on=exchange_today)
        if intraday_range_label == "自定义 / Custom":
            custom_cols = st.columns(2)
            with custom_cols[0]:
                intraday_start = st.date_input(
                    "分钟开始 / Intraday Start",
                    value=exchange_today - timedelta(days=5),
                    key=f"{selected_symbol}-intraday-custom-start",
                )
            with custom_cols[1]:
                intraday_end = st.date_input(
                    "分钟结束 / Intraday End",
                    value=exchange_today,
                    key=f"{selected_symbol}-intraday-custom-end",
                )

        if intraday_start > intraday_end:
            st.error("分钟历史开始日期不能晚于结束日期 / Intraday start must be earlier than end.")
            intraday_bars = pd.DataFrame()
        else:
            intraday_start, was_clamped = _clamp_intraday_start(intraday_start, intraday_end, intraday_ktype)
            if was_clamped:
                st.info(
                    f"为了保证分钟图稳定与速度，{intraday_interval_label} 最长只保留最近 {MAX_INTRADAY_DAYS[intraday_ktype]} 天。"
                )
            intraday_bars = _normalize_kline(
                trader.request_history_klines(
                    selected_symbol,
                    start=intraday_start.isoformat(),
                    end=intraday_end.isoformat(),
                    ktype=intraday_ktype,
                    extended_time=bool(include_extended and settings.futu_trd_market == "US"),
                    session="ALL" if include_extended and settings.futu_trd_market == "US" else "RTH",
                )
            )

        if intraday_bars.empty:
            st.info("没有可用的分钟 K 线 / No intraday K-line data.")
        else:
            intraday_bars["vwap"] = (
                (intraday_bars["close"] * intraday_bars["volume"]).cumsum()
                / intraday_bars["volume"].replace(0, pd.NA).cumsum()
            ).ffill().fillna(intraday_bars["close"])
            intraday_bars["ma5"] = intraday_bars["close"].rolling(5).mean()
            intraday_bars["ma10"] = intraday_bars["close"].rolling(10).mean()
            intraday_bars["ma20"] = intraday_bars["close"].rolling(20).mean()
            interval_minutes = {"K_1M": 1, "K_5M": 5, "K_15M": 15, "K_30M": 30, "K_60M": 60}
            opening_bars = max(1, settings.fusion_opening_range_minutes // interval_minutes.get(intraday_ktype, 1))
            opening_range_high = intraday_bars["high"].head(min(opening_bars, len(intraday_bars))).max()
            intraday_bars["opening_range_high"] = opening_range_high
            intraday_bars = _decorate_indicator_columns(intraday_bars)
            action_marker = pd.DataFrame(
                [{"timestamp": intraday_bars["timestamp"].iloc[-1], "price": intraday_bars["close"].iloc[-1], "label": action_label}]
            )
            _lightweight_chart_component(
                intraday_bars,
                market=settings.futu_trd_market,
                symbol=selected_symbol,
                chart_id=f"{selected_symbol}-intraday-{intraday_ktype}",
                title=f"{intraday_interval_label} 专业看盘图",
                subtitle=f"历史范围 {intraday_start.isoformat()} ~ {intraday_end.isoformat()} | 右键/滚轮缩放更稳定",
                overlays=[
                    ("MA5", "ma5", "#f6c85f"),
                    ("MA10", "ma10", "#9dd866"),
                    ("MA20", "ma20", "#d06fd1"),
                    ("VWAP", "vwap", "#4c78a8"),
                    ("开盘区间高点 / OR High", "opening_range_high", "#f58518"),
                ],
                order_markers=buy_sell_points,
                action_marker=action_marker,
                price_line_label="现价 / Last",
                marker_alignment="bar",
                lower_panel=LOWER_PANEL_OPTIONS[intraday_lower_label],
                main_series="line" if intraday_style_label == "分时线 / Line" else "candles",
            )
            st.caption("分钟图支持 K 线和分时线切换，触摸板现在用双指滚动/缩放和 Pinch，更接近看盘终端。")

    with daily_tab:
        daily_controls = st.columns([1.0, 0.95, 0.9, 0.95])
        with daily_controls[0]:
            daily_range_label = st.selectbox(
                "日线历史 / Daily History",
                options=list(DAILY_RANGE_OPTIONS.keys()),
                index=2,
                key=f"{selected_symbol}-daily-range",
            )
        with daily_controls[1]:
            st.metric("历史终点 / End", exchange_today.isoformat())
        with daily_controls[2]:
            daily_lower_label = st.selectbox(
                "副图指标 / Lower Study",
                options=list(LOWER_PANEL_OPTIONS.keys()),
                index=1,
                key=f"{selected_symbol}-daily-lower-panel",
            )
        with daily_controls[3]:
            st.metric("股票 / Symbol", selected_symbol)

        daily_start, daily_end = _history_window_from_days(DAILY_RANGE_OPTIONS[daily_range_label] or 365, end_on=exchange_today)
        if daily_range_label == "自定义 / Custom":
            custom_daily_cols = st.columns(2)
            with custom_daily_cols[0]:
                daily_start = st.date_input(
                    "日线开始 / Daily Start",
                    value=exchange_today - timedelta(days=365),
                    key=f"{selected_symbol}-daily-custom-start",
                )
            with custom_daily_cols[1]:
                daily_end = st.date_input(
                    "日线结束 / Daily End",
                    value=exchange_today,
                    key=f"{selected_symbol}-daily-custom-end",
                )

        if daily_start > daily_end:
            st.error("日线开始日期不能晚于结束日期 / Daily start must be earlier than end.")
            daily_bars = pd.DataFrame()
        else:
            daily_bars = _normalize_kline(
                trader.request_history_klines(
                    selected_symbol,
                    start=daily_start.isoformat(),
                    end=daily_end.isoformat(),
                    ktype="K_DAY",
                )
            )

        if daily_bars.empty:
            st.info("没有可用的日线 K 线 / No daily K-line data.")
        else:
            daily_bars["ma5"] = daily_bars["close"].rolling(5).mean()
            daily_bars["ma10"] = daily_bars["close"].rolling(10).mean()
            daily_bars["ma20"] = daily_bars["close"].rolling(20).mean()
            daily_bars["ma60"] = daily_bars["close"].rolling(60).mean()
            daily_bars = _decorate_indicator_columns(daily_bars)
            _lightweight_chart_component(
                daily_bars,
                market=settings.futu_trd_market,
                symbol=selected_symbol,
                chart_id=f"{selected_symbol}-daily-history",
                title="日线历史 K 线",
                subtitle=f"历史范围 {daily_start.isoformat()} ~ {daily_end.isoformat()} | 支持长期历史查看",
                overlays=[
                    ("MA5", "ma5", "#f6c85f"),
                    ("MA10", "ma10", "#9dd866"),
                    ("MA20", "ma20", "#4c78a8"),
                    ("MA60", "ma60", "#54a24b"),
                ],
                order_markers=buy_sell_points,
                price_line_label="日线现价 / Last",
                marker_alignment="daily",
                lower_panel=LOWER_PANEL_OPTIONS[daily_lower_label],
            )
            st.caption("日线图现在支持长期历史区间、买卖点标注，以及成交量 / MACD / RSI / KDJ 副图切换。")

    st.markdown("**逐笔 / Recent Ticks**")
    tick_col, book_col = st.columns([0.9, 1.1])
    with tick_col:
        st.dataframe(_ticks_view(ticks), use_container_width=True, hide_index=True)
    with book_col:
        st.caption("你当前的 L2 数据已生效，这里显示的是富途 API 实际返回的多档摆盘，不是我们本地模拟出来的。")
        try:
            bid_view, ask_view = _order_book_view(order_book)
            bid_col, ask_col = st.columns(2)
            with bid_col:
                st.caption("买盘简表 / Bid Snapshot")
                st.dataframe(bid_view.head(10), use_container_width=True, hide_index=True)
            with ask_col:
                st.caption("卖盘简表 / Ask Snapshot")
                st.dataframe(ask_view.head(10), use_container_width=True, hide_index=True)
        except Exception as exc:
            st.warning(f"盘口解析失败 / Order book parse failed: {exc}")

    if selected_feature is not None:
        st.markdown("**策略诊断 / Strategy Diagnostics**")
        diagnostic = pd.DataFrame(
            [
                ["评分 / Score", _format_metric_value(selected_feature.score)],
                ["Gap%", _format_metric_value(selected_feature.gap_pct)],
                ["5m动量 / 5m Mom", _format_metric_value(selected_feature.momentum_5m)],
                ["VWAP偏离 / VWAP Dist", _format_metric_value(selected_feature.vwap_distance)],
                ["相对量 / Rel Vol", _format_metric_value(selected_feature.rel_volume)],
                ["盘口失衡 / OBI", _format_metric_value(selected_feature.orderbook_imbalance)],
                ["逐笔失衡 / Tick Imb", _format_metric_value(selected_feature.tick_imbalance)],
                ["点差bps / Spread", _format_metric_value(selected_feature.spread_bps)],
                ["状态 / Status", _format_metric_value(selected_feature.reason)],
            ],
            columns=["项目 / Metric", "值 / Value"],
        )
        st.dataframe(diagnostic, use_container_width=True, hide_index=True)


def render_live_monitor(settings) -> None:
    _inject_terminal_css()
    st.subheader("高级实时监控 / Advanced Live Monitor")
    auto_refresh_cols = st.columns([0.8, 0.8, 1.6])
    with auto_refresh_cols[0]:
        auto_refresh_enabled = st.toggle(
            "自动刷新 / Auto Refresh",
            value=st.session_state.get("live_auto_refresh_enabled", True),
            key="live_auto_refresh_enabled",
        )
    with auto_refresh_cols[1]:
        auto_refresh_seconds = st.select_slider(
            "频率 / Interval",
            options=[2, 3, 5, 10, 15, 30, 60],
            value=int(st.session_state.get("live_auto_refresh_seconds", 5) or 5),
            key="live_auto_refresh_seconds",
            format_func=lambda value: f"{value}s",
        )
    with auto_refresh_cols[2]:
        if auto_refresh_enabled:
            st.caption(
                f"实时监控会自动刷新，当前频率 {auto_refresh_seconds}s。"
                " 日期筛选和当前股票选择会保留，不用再手动点刷新。"
            )
        else:
            st.caption("自动刷新已关闭。你仍然可以用右侧按钮手动刷新这一块页面。")

    run_every = _live_monitor_run_every(auto_refresh_enabled, int(auto_refresh_seconds))

    env_label = "真实盘 / REAL" if settings.futu_trd_env == "REAL" else "模拟盘 / SIMULATE"
    st.caption(f"当前环境: {env_label}。页面已经收成总览、图表、订单三块；默认先给你看最必要的数据。")

    default_end = date.today()
    default_start = default_end - timedelta(days=7)
    default_base_assets = float(st.session_state.get("live_base_assets", 1_000_000.0 if settings.futu_trd_env != "REAL" else 0.0))
    with st.expander("监控设置 / Filters & Base", expanded=False):
        st.caption("这里只控制订单筛选和净值基准，不会改变自动运行。")
        control_cols = st.columns([1, 1, 1, 1])
        with control_cols[0]:
            start = st.date_input(
                "订单开始日期 / Order History Start",
                value=default_start,
                key="live_start",
            )
        with control_cols[1]:
            end = st.date_input(
                "订单结束日期 / Order History End",
                value=default_end,
                key="live_end",
            )
        with control_cols[2]:
            base_assets = st.number_input(
                "参考初始资产 / Base Assets",
                min_value=0.0,
                value=default_base_assets,
                step=10_000.0,
                key="live_base_assets",
                help="用于计算账户净变化。模拟盘默认可填 1,000,000。",
            )
        with control_cols[3]:
            st.write("")
            refresh = st.button("立即刷新 / Refresh Now", use_container_width=True, key="live-monitor-refresh")

    if start > end:
        st.error("开始日期必须早于结束日期 / Start date must be earlier than end date.")
        return
    if refresh:
        st.session_state["live_payload_key"] = None
        st.rerun()

    payload_key = f"{start.isoformat()}::{end.isoformat()}"

    def _load_live_payload() -> dict[str, object]:
        with FutuPaperTrader(settings) as trader:
            acc_id = trader.resolve_trade_account()
            account = trader.get_account_info(acc_id)
            positions = trader.get_positions(acc_id)
            open_orders = trader.get_open_orders(acc_id)
            order_history = trader.get_order_history(acc_id, start.isoformat(), end.isoformat())
            market_now = datetime.now(ZoneInfo(settings.auto_trader_market_timezone))
            split_state = load_strategy_split_state()
            held_symbols = set(positions["code"].tolist()) if not positions.empty else set()
            baseline_weight, fusion_weight, cascade_weight, reserve_weight = stack_allocations(settings)
            fusion_settings = effective_fusion_settings(settings)

            baseline_targets: dict[str, float] = {}
            if settings.stack_baseline_enabled and baseline_weight > 0:
                baseline_start = max(
                    pd.Timestamp(settings.start_date).date(),
                    (market_now.date() - timedelta(days=max(730, settings.lookback_months * 45))),
                ).isoformat()
                baseline_prices = fetch_futu_daily_closes(
                    trader,
                    settings.symbols,
                    start=baseline_start,
                )
                baseline_targets = scaled_baseline_target_weights(
                    baseline_prices,
                    settings,
                    reference_date=market_now.date(),
                )

            fusion_positions = positions
            fusion_symbols = set(fusion_settings.fusion_universe)
            if not fusion_positions.empty and fusion_symbols:
                fusion_positions = fusion_positions[fusion_positions["code"].isin(fusion_symbols)].copy()
            fusion_held_symbols = set(fusion_positions["code"].tolist()) if not fusion_positions.empty else set()
            fusion_plan = FusionIntradayStrategy(fusion_settings).generate_plan(trader, fusion_held_symbols)
            fusion_scaled_targets = {
                code: round(weight * fusion_weight, 6)
                for code, weight in fusion_plan.target_weights.items()
            }

            cascade_plan = None
            cascade_scaled_targets: dict[str, float] = {}
            if cascade_weight > 0:
                cascade_plan = generate_live_cascade_plan(settings, trader)
                cascade_scaled_targets = {
                    code: round(weight * cascade_weight, 6)
                    for code, weight in cascade_plan.target_weights.items()
                }

            combined_targets = stack_target_weights(
                baseline_targets,
                fusion_scaled_targets,
                cascade_scaled_targets,
            )
            filled_order_history = order_history[
                pd.to_numeric(order_history.get("dealt_qty"), errors="coerce").fillna(0) > 0
            ].copy() if not order_history.empty else order_history
            filled_cost_view = with_trade_costs(
                filled_order_history,
                settings,
                side_col="trd_side",
                qty_col="dealt_qty",
                price_col="dealt_avg_price",
                timestamp_col="updated_time",
            ) if not filled_order_history.empty else filled_order_history
            estimated_realized = estimate_realized_from_fills(filled_order_history, settings)
            estimated_fee_total = trade_log_total_fees(filled_cost_view)
            estimated_unrealized = _calculate_unrealized_from_positions(positions)
            broker_realized = _optional_float(account.get("realized_pl"))
            broker_unrealized = _optional_float(account.get("unrealized_pl"))
            experiment_filled_cost_view = pd.DataFrame()
            if split_state.get("reset_at"):
                split_start = pd.Timestamp(split_state["reset_at"], tz="UTC").tz_convert(settings.auto_trader_market_timezone).date().isoformat()
                experiment_history = trader.get_order_history(acc_id, split_start, market_now.date().isoformat())
                experiment_filled = experiment_history[
                    pd.to_numeric(experiment_history.get("dealt_qty"), errors="coerce").fillna(0) > 0
                ].copy() if not experiment_history.empty else experiment_history
                experiment_filled_cost_view = with_trade_costs(
                    experiment_filled,
                    settings,
                    side_col="trd_side",
                    qty_col="dealt_qty",
                    price_col="dealt_avg_price",
                    timestamp_col="updated_time",
                ) if not experiment_filled.empty else experiment_filled
                experiment_filled_cost_view = filter_fills_since_reset(experiment_filled_cost_view, split_state, settings)
            position_view = _positions_view(positions)
            watchlist_view = _watchlist_view(fusion_plan, fusion_scaled_targets)
            feature_map = {feature.code: feature for feature in fusion_plan.features}
            all_symbols = list(
                dict.fromkeys(
                    [
                        *(positions["code"].tolist() if not positions.empty else []),
                        *combined_targets.keys(),
                        *(watchlist_view["标的 / Symbol"].tolist() if not watchlist_view.empty else []),
                        settings.fusion_benchmark,
                    ]
                )
            )
            snapshots_frame = trader.get_snapshots(all_symbols) if all_symbols else pd.DataFrame()
            watchlist_rows = []
            for code in all_symbols:
                snapshot_row = snapshots_frame.loc[code] if not snapshots_frame.empty and code in snapshots_frame.index else pd.Series(dtype=object)
                last_price = _safe_float(snapshot_row.get("last_price"))
                prev_close = max(_safe_float(snapshot_row.get("prev_close_price")), 1e-9)
                watchlist_rows.append(
                    {
                        "标的 / Symbol": code,
                        "名称 / Name": _symbol_display_name(code, snapshot_row),
                        "当前价 / Last": last_price,
                        "Gap%": last_price / prev_close - 1 if last_price and prev_close else 0.0,
                        "状态 / Status": "held" if code in held_symbols else feature_map.get(code).reason if code in feature_map else "watch",
                    }
                )
            return {
                "payload_key": payload_key,
                "account": account,
                "positions": positions,
                "position_view": position_view,
                "open_orders": open_orders,
                "order_history": order_history,
                "filled_order_history": filled_order_history,
                "filled_cost_view": filled_cost_view,
                "held_symbols": held_symbols,
                "fusion_plan": fusion_plan,
                "baseline_targets": baseline_targets,
                "fusion_scaled_targets": fusion_scaled_targets,
                "cascade_plan": cascade_plan,
                "cascade_scaled_targets": cascade_scaled_targets,
                "combined_targets": combined_targets,
                "stack_allocations": {
                    "baseline": baseline_weight,
                    "fusion": fusion_weight,
                    "cascade": cascade_weight,
                    "reserve": reserve_weight,
                },
                "feature_map": feature_map,
                "all_symbols": all_symbols,
                "terminal_watchlist": pd.DataFrame(watchlist_rows),
                "estimated_realized": estimated_realized,
                "estimated_fee_total": estimated_fee_total,
                "estimated_unrealized": estimated_unrealized,
                "broker_realized": broker_realized,
                "broker_unrealized": broker_unrealized,
                "split_state": split_state,
                "experiment_filled_cost_view": experiment_filled_cost_view,
            }

    def _get_live_payload(*, force_fetch: bool = False) -> dict[str, object]:
        payload = st.session_state.get("live_payload")
        if not force_fetch and isinstance(payload, dict) and payload.get("payload_key") == payload_key:
            return payload
        payload = _load_live_payload()
        st.session_state["live_payload"] = payload
        st.session_state["live_payload_key"] = payload_key
        return payload

    @st.fragment(run_every=run_every)
    def _render_status_and_metrics() -> None:
        watchdog_status_text = _watchdog_status_text()
        if "异常 / error" in watchdog_status_text:
            st.error(watchdog_status_text)
        elif "正常 / healthy" in watchdog_status_text:
            st.success(watchdog_status_text)
        else:
            st.info(watchdog_status_text)
        auto_status_text = _auto_trader_status_text()
        if "异常 / error" in auto_status_text:
            st.error(auto_status_text)
        elif "正常 / healthy" in auto_status_text:
            st.success(auto_status_text)
        else:
            st.info(auto_status_text)

        try:
            payload = _get_live_payload(force_fetch=True)
        except (FutuTradeError, MarketDataError) as exc:
            st.error(str(exc))
            return

        account = payload["account"]
        positions = payload["positions"]
        combined_targets = payload["combined_targets"]
        filled_order_history = payload["filled_order_history"]
        filled_cost_view = payload["filled_cost_view"]
        estimated_realized = float(payload["estimated_realized"])
        estimated_fee_total = float(payload["estimated_fee_total"])
        estimated_unrealized = float(payload["estimated_unrealized"])
        broker_realized = payload["broker_realized"]
        broker_unrealized = payload["broker_unrealized"]
        open_orders = payload["open_orders"]
        fusion_plan = payload["fusion_plan"]
        baseline_targets = payload["baseline_targets"]
        fusion_scaled_targets = payload["fusion_scaled_targets"]
        cascade_plan = payload["cascade_plan"]
        cascade_scaled_targets = payload["cascade_scaled_targets"]
        sleeve_allocations = payload["stack_allocations"]
        split_state = payload["split_state"]
        experiment_filled_cost_view = payload["experiment_filled_cost_view"]
        total_assets = _safe_float(account.get("total_assets"))
        base_assets_value = float(st.session_state.get("live_base_assets", base_assets))
        net_change = total_assets - base_assets_value
        net_change_pct = (net_change / base_assets_value) if base_assets_value > 0 else None

        filled_trade_count = len(filled_order_history)
        displayed_unrealized = broker_unrealized if broker_unrealized is not None else estimated_unrealized
        displayed_realized = broker_realized if broker_realized is not None else estimated_realized
        st.markdown("**当前三策略 / Three Strategies**")
        st.caption("这里直接拆成三套：Baseline、Fusion、Claude/Cascade。清仓重置后，这三套会从各自的起始现金重新开始记账。")
        strategy_cols = st.columns(3)

        baseline_runtime, baseline_note = _strategy_runtime_display(float(sleeve_allocations["baseline"]), auto_status_text)
        with strategy_cols[0]:
            st.markdown("**Baseline**")
            st.write(f"配置 / Config: {'已启用 / enabled' if float(sleeve_allocations['baseline']) > 0 else '未启用 / disabled'}")
            st.write(f"执行 / Runtime: {baseline_runtime}")
            st.write(f"占比 / Weight: {float(sleeve_allocations['baseline']):.0%}")
            st.caption(f"当前目标 / Targets: {_top_target_summary(baseline_targets)}")
            st.caption(baseline_note)

        fusion_runtime, fusion_note = _strategy_runtime_display(float(sleeve_allocations["fusion"]), auto_status_text)
        with strategy_cols[1]:
            st.markdown("**Fusion**")
            st.write(f"配置 / Config: {'已启用 / enabled' if float(sleeve_allocations['fusion']) > 0 else '未启用 / disabled'}")
            st.write(f"执行 / Runtime: {fusion_runtime}")
            st.write(f"占比 / Weight: {float(sleeve_allocations['fusion']):.0%}")
            st.caption(f"当前目标 / Targets: {_top_target_summary(fusion_scaled_targets)}")
            st.caption(f"基准分数 / Benchmark: {float(getattr(fusion_plan, 'benchmark_score', 0.0)):+.4f}")
            st.caption(fusion_note)

        cascade_runtime, cascade_note = _strategy_runtime_display(float(sleeve_allocations["cascade"]), auto_status_text)
        with strategy_cols[2]:
            st.markdown("**Claude/Cascade**")
            st.write(f"配置 / Config: {'已启用 / enabled' if float(sleeve_allocations['cascade']) > 0 else '未启用 / disabled'}")
            st.write(f"执行 / Runtime: {cascade_runtime}")
            st.write(f"占比 / Weight: {float(sleeve_allocations['cascade']):.0%}")
            st.caption(f"当前目标 / Targets: {_top_target_summary(cascade_scaled_targets)}")
            if cascade_plan is not None:
                st.caption(f"Regime / 状态: {cascade_plan.regime_label} ({float(cascade_plan.regime_score):+.3f})")
            else:
                st.caption("Regime / 状态: 暂无 / N/A")
            st.caption(cascade_note)

        metrics_top = st.columns(4)
        metrics_top[0].metric("总资产 / Assets", _format_currency(total_assets))
        metrics_top[1].metric(
            "总盈亏 / Net PnL",
            _format_currency(net_change),
            f"{net_change_pct:+.2%}" if net_change_pct is not None else None,
        )
        metrics_top[2].metric("现金 / Cash", _format_currency(account.get("cash")))
        metrics_top[3].metric("持仓市值 / Market Value", _format_currency(account.get("market_val")))

        metrics_bottom = st.columns(3)
        metrics_bottom[0].metric("当前浮盈 / Unrealized", _format_currency(displayed_unrealized))
        metrics_bottom[1].metric("交易成本 / Fees", _format_currency(estimated_fee_total))
        metrics_bottom[2].metric("成交笔数 / Trades", str(filled_trade_count))

        compact_summary = st.columns([1.15, 1.0])
        compact_summary[0].info(
            "当前分配 / Current Split: "
            f"Baseline {float(sleeve_allocations['baseline']):.0%} + Fusion {float(sleeve_allocations['fusion']):.0%} + "
            f"Claude/Cascade {float(sleeve_allocations['cascade']):.0%}"
        )
        compact_summary[1].info(f"目标仓位 Top / Top Targets: {_top_target_summary(combined_targets)}")

        current_breakdown = current_strategy_holdings(
            settings=settings,
            positions=positions,
            total_assets=total_assets,
            combined_targets=combined_targets,
            baseline_targets=baseline_targets,
            fusion_targets=fusion_scaled_targets,
            cascade_targets=cascade_scaled_targets,
        )
        period_breakdown = period_strategy_performance(
            filled_cost_view=experiment_filled_cost_view,
            settings=settings,
        )
        strategy_ledger, overlap_breakdown = build_strategy_ledger(
            settings=settings,
            split_state=split_state,
            total_assets=total_assets,
            current_holdings=current_breakdown,
            period_performance=period_breakdown,
        )

        st.markdown("**三策略独立账本 / Three-Strategy Ledger**")
        reset_caption = ""
        if split_state.get("reset_at"):
            reset_caption = f"本次独立记账起点 / Reset At: {split_state['reset_at']}"
        st.caption(f"这里看三套策略各自当前允许操作总现金、市值、预算余量、收益、成本和成交笔数。{reset_caption}")
        st.info(
            f"账户余留现金 / Account Remaining Cash: {_format_currency(account.get('cash'))}。"
            " 这张表里的“当前允许操作总现金”会直接跟着控制台当前权重变化。"
        )
        if split_state and not split_state_matches_current(split_state, settings):
            reset_weights = split_state_weight_map(split_state)
            st.caption(
                "说明：预算列按你现在的控制台配置实时显示；"
                " 收益列仍按最近一次清仓重置后的起点累计。"
                f" 上次重置时是 Baseline {reset_weights['Baseline']:.0%} / Fusion {reset_weights['Fusion']:.0%} / Claude {reset_weights['Claude/Cascade']:.0%}；"
                f" 现在配置是 Baseline {float(sleeve_allocations['baseline']):.0%} / Fusion {float(sleeve_allocations['fusion']):.0%} / Claude {float(sleeve_allocations['cascade']):.0%}。"
            )
        st.dataframe(strategy_ledger, use_container_width=True, hide_index=True)
        if not overlap_breakdown.empty:
            st.caption("另有部分成交属于重叠标的 / Shared-Overlap。为了不误导，这些单子没有硬塞给某一套策略。")
            st.dataframe(overlap_breakdown, use_container_width=True, hide_index=True)

        st.caption(
            "一句话理解：总盈亏看整个账户现在比初始资金多了多少；现金和持仓市值一起组成总资产；"
            "当前浮盈只看你手里还拿着的仓位；交易成本是这段区间估算出来的费用。"
        )
        st.caption(f"当前未完成订单 {len(open_orders)} 笔。区间已实现已经挪到订单页里，不再占首页主位置。")

        if not filled_order_history.empty:
            st.info(
                f"所选日期范围内已有 {filled_trade_count} 笔已成交订单。"
                " 下面的“订单 / Orders”页会把成交记录和费用估算拆开给你看。"
            )
        if broker_realized is None:
            broker_name = "富途真实盘" if settings.futu_trd_env == "REAL" else "富途模拟盘"
            st.caption(
                f"{broker_name} 当前返回的账户字段 `realized_pl` 是 `N/A`，"
                f"页面已改为按所选日期范围内的成交记录估算净已实现盈亏，当前估算值为 {_format_currency(estimated_realized)}，"
                f"其中估算成交成本约为 {_format_currency(estimated_fee_total)}。"
                " 这更接近真实净结果，但仍不保证包含券商侧全部调整。"
            )
        if broker_unrealized is None:
            broker_name = "富途真实盘" if settings.futu_trd_env == "REAL" else "富途模拟盘"
            st.caption(
                f"{broker_name} 当前返回的账户字段 `unrealized_pl` 是 `N/A`，"
                f"页面已改为按当前持仓 `pl_val / cost_price` 估算浮动盈亏，当前估算值为 {_format_currency(estimated_unrealized)}。"
            )

    def _render_crypto_strategy_tab(
        cascade_plan,
        cascade_scaled_targets: dict,
        sleeve_allocations: dict,
        positions,
        settings,
    ) -> None:
        """Render the full crypto strategy detail tab."""
        import math

        cascade_weight = float(sleeve_allocations.get("cascade", 0))

        st.caption(
            "Cascade 策略的完整加密信号、资产评分和仓位详情。"
            " Claude/Cascade 同时做股票+加密+债券，这里专门把加密相关的内容单独列出来。"
        )

        if cascade_plan is None:
            st.warning(
                "Cascade 策略暂未加载 / Cascade strategy not loaded. "
                f"当前 Claude/Cascade 权重: {cascade_weight:.0%}。"
                " 需要权重 > 0 并成功运行一次才能显示数据。"
            )
            return

        # ── 1. Regime 制度感知 ───────────────────────────────────────────
        st.markdown("### 📡 市场制度感知 / Market Regime")
        regime_colour = {
            "CRISIS":   "🔴", "CAUTIOUS": "🟠", "NEUTRAL": "🟡",
            "BULLISH":  "🟢", "EUPHORIA": "🟣",
        }
        icon = regime_colour.get(cascade_plan.regime_label, "⚪")
        st.info(
            f"{icon} **制度 / Regime: {cascade_plan.regime_label}**  "
            f"综合得分 {float(cascade_plan.regime_score):+.4f}"
        )

        # Use getattr with defaults so old cached objects (missing new fields) don't crash
        _cp_pulse  = float(getattr(cascade_plan, "crypto_pulse",     0.0))
        _cp_vol    = str(getattr(cascade_plan,   "vol_regime",       ""))
        _cp_cross  = float(getattr(cascade_plan, "cross_asset_flow", 0.0))
        _cp_fund   = float(getattr(cascade_plan, "funding_signal",   0.0))

        regime_cols = st.columns(4)
        regime_cols[0].metric(
            "加密脉冲 / Crypto Pulse",
            f"{_cp_pulse:+.3f}",
            help="BTC 短期动量 + ETH/BTC 相对趋势合成，正数看多加密",
        )
        regime_cols[1].metric(
            "波动制度 / Vol Regime",
            _cp_vol or "—",
            help="low / normal / high，基于 VIX + BTC 波动率",
        )
        regime_cols[2].metric(
            "跨资产流 / Cross-Asset Flow",
            f"{_cp_cross:+.3f}",
            help="BTC 周末涨跌 → 周一股票预测 + BTC-SPY 相关系数",
        )
        regime_cols[3].metric(
            "资金费率信号 / Funding Signal",
            f"{_cp_fund:+.3f}",
            help="永续合约资金费率：极负 → 逆向买入，极正 → 减仓",
        )

        details = dict(getattr(cascade_plan, "regime_details", None) or {})
        if details:
            det_cols = st.columns(4)
            vix = details.get("vix_level")
            fr  = details.get("funding_rate")
            bw  = details.get("btc_weekend_return")
            vs  = details.get("vol_score")
            ca  = details.get("crypto_data_available")
            if vix is not None:
                det_cols[0].metric("VIX", f"{float(vix):.1f}")
            if fr is not None:
                det_cols[1].metric("资金费率 / Funding Rate", f"{float(fr):.4%}")
            if bw is not None:
                det_cols[2].metric("BTC 周末收益 / BTC Weekend", f"{float(bw):+.2%}")
            if vs is not None:
                det_cols[3].metric("波动分 / Vol Score", f"{float(vs):+.3f}")
            if ca is not None and not ca:
                st.warning(
                    "⚠️ 加密数据不可用 / Crypto data unavailable — 策略正在降级模式运行。"
                    " 请检查 Binance 连接或网络。"
                )

        # ── 2. 资产类别预算 / Asset Class Budgets ─────────────────────────
        st.markdown("### 💰 资产类别预算 / Asset Class Budgets")
        budgets = dict(getattr(cascade_plan, "all_asset_class_budgets", None) or {})
        if budgets:
            budget_cols = st.columns(3)
            labels = {"equity": "股票 Equity", "crypto": "加密 Crypto", "bond": "债券 Bond"}
            icons  = {"equity": "📈", "crypto": "🔗", "bond": "📄"}
            for i, cls in enumerate(["equity", "crypto", "bond"]):
                v = float(budgets.get(cls, 0.0))
                budget_cols[i].metric(
                    f"{icons[cls]} {labels[cls]}",
                    f"{v:.0%}",
                    help=f"当前制度 {cascade_plan.regime_label} 下 {cls} 的预算占比",
                )
            # Bar chart of budgets
            try:
                import plotly.graph_objects as go
                fig_b = go.Figure(go.Bar(
                    x=[labels.get(k, k) for k in budgets],
                    y=[v * 100 for v in budgets.values()],
                    marker_color=["#1f77b4", "#ff7f0e", "#2ca02c"],
                    text=[f"{v:.0%}" for v in budgets.values()],
                    textposition="auto",
                ))
                fig_b.update_layout(
                    title="资产类别预算 / Asset Class Budget Allocation",
                    yaxis_title="预算占比 / Budget %",
                    height=250, margin=dict(l=40, r=20, t=40, b=30),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig_b, use_container_width=True)
            except Exception:
                pass
        else:
            st.info("预算数据暂无 / Budget data not available.")

        # ── 3. 全策略目标仓位（含加密）/ All Target Weights incl. Crypto ─
        st.markdown("### 🎯 完整目标仓位 / Full Target Weights (all legs)")
        all_tw = dict(getattr(cascade_plan, "all_target_weights", None) or {})
        if all_tw:
            tw_rows = []
            for sym, w in sorted(all_tw.items(), key=lambda x: -x[1]):
                is_crypto = "/" in sym
                is_bond   = sym in {"US.AGG", "US.IEF", "US.TLT", "HK.AGG"}
                cls = "🔗 加密" if is_crypto else ("📄 债券" if is_bond else "📈 股票")
                in_futu  = sym in (cascade_scaled_targets or {})
                tw_rows.append({
                    "标的 / Symbol":   sym,
                    "类别 / Class":    cls,
                    "策略目标权重 / Strategy Weight": f"{w:.2%}",
                    "富途执行权重 / Futu Weight": (
                        f"{cascade_scaled_targets[sym]:.2%}" if in_futu else "（加密/非富途 / crypto/non-Futu）"
                    ),
                })
            st.dataframe(
                tw_rows,
                use_container_width=True,
                hide_index=True,
            )
            crypto_legs = {s: w for s, w in all_tw.items() if "/" in s}
            if crypto_legs:
                st.caption(
                    "🔗 **加密仓位说明**: 加密标的（如 BTC/USDT）当前由 Cascade 策略信号控制，"
                    "但通过富途下单的是对应的加密 ETF / 股票代替品。"
                    " 若未来接入加密交易所，则直接下单。"
                )
        else:
            st.info("目标仓位暂无数据 / No target weights data.")

        # ── 4. 当前加密相关持仓 / Current Crypto-related Positions ────────
        st.markdown("### 📊 加密相关持仓 / Crypto-related Holdings")
        if not positions.empty:
            CRYPTO_RELATED = {
                "US.IBIT", "US.FBTC", "US.BTCO", "US.BITB",   # BTC ETFs
                "US.ETHA", "US.CETH",                          # ETH ETFs
                "US.COIN", "US.MSTR", "US.MARA", "US.RIOT",   # crypto stocks
                "US.GBTC", "US.GDLC",
            }
            # also match symbols in cascade all_target_weights for crypto class
            cascade_crypto_futu = {
                sym for sym in (cascade_scaled_targets or {})
                if sym in CRYPTO_RELATED
            }
            crypto_pos = positions[
                positions["code"].isin(CRYPTO_RELATED | cascade_crypto_futu)
            ].copy()
            if crypto_pos.empty:
                st.info(
                    "当前没有加密相关持仓。"
                    " Cascade 策略当前加密目标仓位: "
                    + (", ".join(f"{s} {w:.2%}" for s, w in (cascade_scaled_targets or {}).items()
                                 if s in CRYPTO_RELATED) or "无")
                )
            else:
                st.dataframe(_positions_view(crypto_pos), use_container_width=True, hide_index=True)
        else:
            st.info("当前没有持仓 / No positions.")

        # ── 5. 资产评分 / Asset Scores ────────────────────────────────────
        scores = list(getattr(cascade_plan, "asset_scores", None) or [])
        if scores:
            st.markdown("### 🏆 资产评分 / Asset Scores")
            st.caption(
                "Cascade 策略对所有候选资产的打分。"
                " final_score = 动量得分 / 波动率 + 资金费率修正（加密专用）。"
                " eligible=True 才会被选入组合。"
            )
            score_rows = []
            for s in sorted(scores, key=lambda x: -float(x.get("final_score", 0))):
                sym = s.get("symbol", "")
                score_rows.append({
                    "标的 / Symbol":      sym,
                    "类别 / Asset Class": s.get("asset_class", ""),
                    "动量分 / Momentum":  f"{float(s.get('momentum_score', 0)):+.4f}",
                    "波动调整 / Vol-Adj": f"{float(s.get('vol_adjusted_score', 0)):+.4f}",
                    "资金修正 / Funding": f"{float(s.get('funding_override', 0)):+.4f}",
                    "综合分 / Final":     f"{float(s.get('final_score', 0)):+.4f}",
                    "入选 / Eligible":    "✅" if s.get("eligible") else "❌",
                    "原因 / Reason":      s.get("reason", ""),
                })

            crypto_scores = [r for r in score_rows if r["类别 / Asset Class"] == "crypto"]
            equity_scores = [r for r in score_rows if r["类别 / Asset Class"] == "equity"]
            bond_scores   = [r for r in score_rows if r["类别 / Asset Class"] == "bond"]

            score_sub_tabs = st.tabs(["全部 / All", "🔗 加密 / Crypto", "📈 股票 / Equity", "📄 债券 / Bond"])
            with score_sub_tabs[0]:
                st.dataframe(score_rows, use_container_width=True, hide_index=True)
            with score_sub_tabs[1]:
                if crypto_scores:
                    st.dataframe(crypto_scores, use_container_width=True, hide_index=True)
                else:
                    st.info("暂无加密资产评分 / No crypto asset scores.")
            with score_sub_tabs[2]:
                if equity_scores:
                    st.dataframe(equity_scores, use_container_width=True, hide_index=True)
                else:
                    st.info("暂无股票评分 / No equity scores.")
            with score_sub_tabs[3]:
                if bond_scores:
                    st.dataframe(bond_scores, use_container_width=True, hide_index=True)
                else:
                    st.info("暂无债券评分 / No bond scores.")
        else:
            st.info("资产评分暂无数据（需要策略完整运行后才有）/ Asset scores not available yet.")

        # ── 6. 策略说明 / Strategy Description ────────────────────────────
        with st.expander("📖 Cascade 策略原理 / How Cascade Works", expanded=False):
            st.markdown("""
**Cascade 是一个跨资产制度感知策略 / Cross-asset Regime-Adaptive Strategy**

运作分四阶段：

**Phase 1 SENSE 感知制度**
- 从加密市场（BTC/ETH）读取 24/7 实时信号
- 加密脉冲 = BTC 4h 动量 + ETH/BTC 相对趋势
- 资金费率 = 永续合约市场情绪指标
- 波动制度 = VIX + BTC 波动率
- 跨资产流 = BTC 周末收益预测周一股票方向

**Phase 2 ALLOCATE 分配预算**
- 根据制度状态分配股票/加密/债券预算
- CRISIS → 100% 债券；BULLISH → 40% 股票 + 35% 加密 + 25% 债券

**Phase 3 SELECT 选股选币**
- 在每个类别内用动量得分 / 波动率排名
- 加密特有：资金费率异常时做逆向修正

**Phase 4 SIZE 仓位管理**
- 波动率目标（年化 15%）+ 最大单个持仓 30%
- 制度风险系数控制整体仓位大小
            """)

    @st.fragment(run_every=run_every)
    def _render_workspace() -> None:
        try:
            payload = _get_live_payload()
        except (FutuTradeError, MarketDataError) as exc:
            st.error(str(exc))
            return

        positions = payload["positions"]
        position_view = payload["position_view"]
        fusion_plan = payload["fusion_plan"]
        baseline_targets = payload["baseline_targets"]
        fusion_scaled_targets = payload["fusion_scaled_targets"]
        cascade_plan = payload["cascade_plan"]
        cascade_scaled_targets = payload["cascade_scaled_targets"]
        combined_targets = payload["combined_targets"]
        sleeve_allocations = payload["stack_allocations"]
        feature_map = payload["feature_map"]
        terminal_watchlist = payload["terminal_watchlist"]
        held_symbols = payload["held_symbols"]
        order_history = payload["order_history"]
        open_orders = payload["open_orders"]
        filled_order_history = payload["filled_order_history"]
        filled_cost_view = payload["filled_cost_view"]
        estimated_realized = float(payload["estimated_realized"])
        broker_realized = payload["broker_realized"]
        all_symbols = list(payload["all_symbols"])

        selected_symbol = st.session_state.get("selected_symbol") or (all_symbols[0] if all_symbols else settings.fusion_benchmark)
        if selected_symbol not in all_symbols:
            all_symbols = [selected_symbol, *all_symbols]

        chart_tab, overview_tab, orders_tab, crypto_tab = st.tabs(
            ["K线 / Chart", "总览 / Overview", "订单 / Orders", "🔗 加密策略 / Crypto"]
        )

        with chart_tab:
            st.caption("页面打开后默认先看 K 线。总览和订单放到后面，需要时再看。")
            chart_controls = st.columns([1.2, 0.8, 0.7])
            with chart_controls[0]:
                selected_symbol = st.selectbox(
                    "当前股票 / Current Symbol",
                    options=all_symbols,
                    index=all_symbols.index(selected_symbol),
                    key="terminal-current-symbol",
                )
            with chart_controls[1]:
                lob_depth = st.segmented_control(
                    "L2 深度 / Depth",
                    options=[10, 20, 30, 50],
                    default=20,
                    selection_mode="single",
                    key="terminal-lob-depth",
                )
                lob_depth = int(lob_depth or 20)
            with chart_controls[2]:
                st.metric("组合目标 / Stack Target", f"{combined_targets.get(selected_symbol, 0.0):.2%}")

            st.session_state["selected_symbol"] = selected_symbol
            selected_feature = feature_map.get(selected_symbol)
            position_row = None
            if not positions.empty and selected_symbol in positions["code"].values:
                position_row = positions.set_index("code").loc[selected_symbol]

            try:
                with FutuPaperTrader(settings) as trader:
                    context = _build_symbol_context(
                        trader,
                        settings,
                        selected_symbol,
                        selected_feature,
                        fusion_plan,
                        held_symbols,
                        order_history,
                        depth=lob_depth,
                    )
                    _render_terminal_symbol_header(selected_symbol, context)
                    summary_cols = st.columns(4)
                    last_price = _safe_float(context.snapshot.get("last_price"))
                    prev_close = max(_safe_float(context.snapshot.get("prev_close_price")), 1e-9)
                    summary_cols[0].metric("现价 / Last", _format_currency(last_price))
                    summary_cols[1].metric("涨跌幅 / Change", _format_pct(last_price / prev_close - 1))
                    summary_cols[2].metric("动作 / Action", context.action_label)
                    summary_cols[3].metric(
                        "持仓 / Position",
                        str(int(_safe_float(position_row.get("qty")))) if position_row is not None else "0",
                    )
                    _render_terminal_chart_panel(
                        trader,
                        settings,
                        selected_symbol=selected_symbol,
                        selected_feature=selected_feature,
                        context=context,
                    )

                    with st.expander("盘口与逐笔 / L2 & Ticks", expanded=False):
                        tick_col, book_col = st.columns([0.85, 1.15])
                        with tick_col:
                            st.markdown("**逐笔 / Recent Ticks**")
                            st.dataframe(_ticks_view(context.ticks), use_container_width=True, hide_index=True)
                        with book_col:
                            st.markdown("**L2 深度 / L2 Depth**")
                            ladder_view = _lob_ladder_view(context.order_book, lob_depth)
                            if ladder_view.empty:
                                st.info("当前拿不到 L2 摆盘 / No L2 ladder data.")
                            else:
                                st.dataframe(ladder_view, use_container_width=True, hide_index=True)

                    with st.expander("为什么这只票现在没下单 / Why This Symbol Is Not Trading", expanded=False):
                        if selected_feature is None:
                            st.info("这只股票当前没有 Fusion 特征。通常表示它不在本轮可执行候选里。")
                        else:
                            diagnostic = pd.DataFrame(
                                [
                                    ["评分 / Score", _format_metric_value(selected_feature.score)],
                                    ["Gap%", _format_metric_value(selected_feature.gap_pct)],
                                    ["5m动量 / 5m Mom", _format_metric_value(selected_feature.momentum_5m)],
                                    ["VWAP偏离 / VWAP Dist", _format_metric_value(selected_feature.vwap_distance)],
                                    ["相对量 / Rel Vol", _format_metric_value(selected_feature.rel_volume)],
                                    ["盘口失衡 / OBI", _format_metric_value(selected_feature.orderbook_imbalance)],
                                    ["逐笔失衡 / Tick Imb", _format_metric_value(selected_feature.tick_imbalance)],
                                    ["点差bps / Spread", _format_metric_value(selected_feature.spread_bps)],
                                    ["当前状态 / Status", _format_metric_value(selected_feature.reason)],
                                ],
                                columns=["项目 / Metric", "值 / Value"],
                            )
                            st.dataframe(diagnostic, use_container_width=True, hide_index=True)
            except (FutuTradeError, MarketDataError) as exc:
                st.error(str(exc))
                return

        with overview_tab:
            our_weight = float(sleeve_allocations["baseline"]) + float(sleeve_allocations["fusion"])
            our_targets = stack_target_weights(baseline_targets, fusion_scaled_targets)
            st.info(
                "当前比较模式 / Current comparison mode: "
                f"我的策略组 {our_weight:.0%} (Baseline {float(sleeve_allocations['baseline']):.0%} + Fusion {float(sleeve_allocations['fusion']):.0%}) "
                f"vs Claude/Cascade {float(sleeve_allocations['cascade']):.0%}。"
            )
            strategy_cols = st.columns(2)
            strategy_cols[0].info(
                _strategy_live_summary(
                    "我的策略组 / Ours",
                    our_weight,
                    f"当前目标: {_top_target_summary(our_targets)}",
                    extra=(
                        f"内部构成 Baseline {float(sleeve_allocations['baseline']):.0%} + "
                        f"Fusion {float(sleeve_allocations['fusion']):.0%} | "
                        f"Fusion 基准 {float(getattr(fusion_plan, 'benchmark_score', 0.0)):+.4f}"
                    ),
                )
            )
            cascade_extra = None
            if cascade_plan is not None:
                _cp = float(getattr(cascade_plan, "crypto_pulse",     0.0))
                _vr = str(getattr(cascade_plan,   "vol_regime",       ""))
                _ca = float(getattr(cascade_plan, "cross_asset_flow", 0.0))
                _fs = float(getattr(cascade_plan, "funding_signal",   0.0))
                _det = dict(getattr(cascade_plan, "regime_details", None) or {})
                _bud = dict(getattr(cascade_plan, "all_asset_class_budgets", None) or {})
                # regime line
                cascade_extra = (
                    f"Regime {cascade_plan.regime_label} "
                    f"({float(cascade_plan.regime_score):+.3f})"
                )
                # sub-signals
                sig_parts = []
                if _cp != 0.0:
                    sig_parts.append(f"加密脉冲 {_cp:+.3f}")
                if _vr:
                    sig_parts.append(f"波动 {_vr}")
                if _ca != 0.0:
                    sig_parts.append(f"跨资产 {_ca:+.3f}")
                if _fs != 0.0:
                    sig_parts.append(f"资金费率信号 {_fs:+.3f}")
                if sig_parts:
                    cascade_extra += "\n" + " | ".join(sig_parts)
                # VIX / funding rate / BTC weekend
                det_parts = []
                if _det.get("vix_level") is not None:
                    det_parts.append(f"VIX {float(_det['vix_level']):.1f}")
                if _det.get("funding_rate") is not None:
                    det_parts.append(f"资金费率 {float(_det['funding_rate']):.4%}")
                if _det.get("btc_weekend_return") is not None:
                    det_parts.append(f"BTC周末 {float(_det['btc_weekend_return']):+.2%}")
                if _det.get("crypto_data_available") is False:
                    det_parts.append("⚠️ 加密数据不可用")
                if det_parts:
                    cascade_extra += "\n" + " | ".join(det_parts)
                # budgets
                if _bud:
                    bud_parts = [
                        f"股票 {float(_bud.get('equity',0)):.0%}",
                        f"加密 {float(_bud.get('crypto',0)):.0%}",
                        f"债券 {float(_bud.get('bond',0)):.0%}",
                    ]
                    cascade_extra += "\n预算: " + " / ".join(bud_parts)
                if cascade_plan.note:
                    cascade_extra += f"\n{cascade_plan.note}"
            strategy_cols[1].info(
                _strategy_live_summary(
                    "Claude/Cascade",
                    float(sleeve_allocations["cascade"]),
                    f"当前目标: {_top_target_summary(cascade_scaled_targets)}",
                    extra=cascade_extra,
                )
            )
            top_cols = st.columns([1.15, 0.85])
            with top_cols[0]:
                st.markdown("**当前持仓 / Current Positions**")
                if position_view.empty:
                    st.info("当前没有持仓 / No positions.")
                else:
                    st.dataframe(position_view, use_container_width=True, hide_index=True)
            with top_cols[1]:
                st.markdown("**当前目标仓位 / Current Targets**")
                if combined_targets:
                    weight_frame = pd.DataFrame(
                        [{"标的 / Symbol": code, "目标仓位 / Target Weight": weight} for code, weight in combined_targets.items()]
                    )
                    st.dataframe(weight_frame, use_container_width=True, hide_index=True)
                else:
                    st.info("当前没有新入场目标 / No current target entries.")

            with st.expander("观察池状态 / Watchlist Status", expanded=False):
                if terminal_watchlist.empty:
                    st.info("当前没有可展示的观察标的 / No watchlist symbols to show.")
                else:
                    watchlist_view = terminal_watchlist.rename(
                        columns={
                            "当前价 / Last": "最新价 / Last",
                            "Gap%": "涨跌幅 / Change",
                        }
                    )
                    columns = [column for column in ["标的 / Symbol", "最新价 / Last", "涨跌幅 / Change", "状态 / Status"] if column in watchlist_view.columns]
                    st.dataframe(watchlist_view.loc[:, columns], use_container_width=True, hide_index=True)

        with orders_tab:
            displayed_realized = broker_realized if broker_realized is not None else estimated_realized
            filled_grouped = _with_owner_group(filled_cost_view, settings)
            order_summary = st.columns(4)
            order_summary[0].metric("已成交笔数 / Filled Trades", str(len(filled_order_history)))
            order_summary[1].metric("估算成本 / Est. Fees", _format_currency(trade_log_total_fees(filled_cost_view)))
            order_summary[2].metric("区间已实现 / Realized", _format_currency(displayed_realized))
            order_summary[3].metric("未完成订单 / Open Orders", str(len(open_orders)))

            st.markdown("**两家策略成交分账 / Two-Group Trade Breakdown**")
            st.caption("这里按两家来拆：我的策略组 = Baseline + Fusion，另一边是 Claude/Cascade。")
            trade_group_cols = st.columns(2)
            for column, group_name in zip(trade_group_cols, ["我的策略组 / Ours", "Claude/Cascade"]):
                with column:
                    subset = filled_grouped[filled_grouped["策略组 / Group"] == group_name].copy() if not filled_grouped.empty else pd.DataFrame()
                    realized = estimate_realized_from_fills(
                        subset,
                        settings,
                        qty_col="dealt_qty",
                        price_col="dealt_avg_price",
                        timestamp_col="updated_time",
                    ) if not subset.empty else 0.0
                    fees = trade_log_total_fees(subset) if not subset.empty else 0.0
                    st.markdown(f"**{group_name}**")
                    metrics = st.columns(3)
                    metrics[0].metric("成交笔数 / Trades", str(len(subset)))
                    metrics[1].metric("交易成本 / Fees", _format_currency(fees))
                    metrics[2].metric("区间已实现 / Realized", _format_currency(realized))
                    if subset.empty:
                        st.info("所选日期内这组还没有可归因的已成交订单。")
                    else:
                        st.dataframe(_orders_view(subset), use_container_width=True, hide_index=True)

            shared_subset = filled_grouped[filled_grouped["策略组 / Group"] == "Shared/Overlap"].copy() if not filled_grouped.empty else pd.DataFrame()
            if not shared_subset.empty:
                st.caption(
                    f"另有 {len(shared_subset)} 笔成交属于重叠标的 / Shared-Overlap，"
                    "这类单子当前只能近似归因，暂时不硬塞给某一组。"
                )

            order_tabs = st.tabs(["已成交 / Filled", "未完成 / Open", "全部历史 / All"])
            with order_tabs[0]:
                if filled_order_history.empty:
                    st.info("所选日期内没有已成交订单 / No filled orders in the selected date range.")
                else:
                    st.dataframe(_orders_view(filled_cost_view), use_container_width=True, hide_index=True)
            with order_tabs[1]:
                if open_orders.empty:
                    st.info("当前没有未完成订单 / No open orders.")
                else:
                    st.dataframe(_orders_view(open_orders), use_container_width=True, hide_index=True)
            with order_tabs[2]:
                if order_history.empty:
                    st.info("所选日期内没有订单 / No orders in the selected date range.")
                else:
                    st.dataframe(_orders_view(order_history), use_container_width=True, hide_index=True)

        with crypto_tab:
            _render_crypto_strategy_tab(
                cascade_plan=cascade_plan,
                cascade_scaled_targets=cascade_scaled_targets,
                sleeve_allocations=sleeve_allocations,
                positions=positions,
                settings=settings,
            )

    _render_status_and_metrics()
    _render_workspace()


def render_historical_simulation(settings) -> None:
    st.subheader("历史模拟 / Historical Simulation")
    st.caption("这部分是研究回放，不会向富途下单。你现在可以单独测 baseline、fusion、Claude/Cascade、account，也可以直接看当前组合 stack 的整体结果。")

    default_start = max(pd.Timestamp(settings.start_date).date(), date.today() - timedelta(days=30))
    default_end = date.today()
    mode = st.selectbox(
        "研究模式 / Study Mode",
        options=[
            "基线月频回测 / Baseline Monthly",
            "策略组合回测 / Strategy Stack Replay",
            "Claude/Cascade 回放 / Claude-Cascade Replay",
            "Fusion 日内回放 / Fusion Intraday Replay",
            "精确执行复盘 / Exact Execution Replay",
            "真实账户复盘 / Account Replay",
        ],
        index=0,
        key="study_mode",
    )

    common_cols = st.columns(3)
    with common_cols[0]:
        start = st.date_input("开始日期 / Start Date", value=default_start, key="bt_start")
    with common_cols[1]:
        end = st.date_input("结束日期 / End Date", value=default_end, key="bt_end")
    with common_cols[2]:
        initial_capital = st.number_input("初始资金 / Initial Capital", min_value=10_000.0, value=1_000_000.0, step=10_000.0)

    if start > end:
        st.error("开始日期必须早于结束日期 / Start date must be earlier than end date.")
        return

    def _render_replay_result(result: ReplayResult) -> None:
        if result.name == "Account Replay":
            metric_top = st.columns(3)
            metric_top[0].metric("区间净变动 / Net Change", _format_currency(result.summary.get("net_pnl")))
            metric_top[1].metric("净已实现 / Realized Net", _format_currency(result.summary.get("estimated_realized")))
            metric_top[2].metric("估算费用 / Est. Fees", _format_currency(result.summary.get("total_fees")))
            metric_bottom = st.columns(3)
            metric_bottom[0].metric("结束敞口 / Ending Exposure", _format_currency(result.summary.get("ending_exposure")))
            metric_bottom[1].metric("成交笔数 / Filled Trades", str(int(result.summary.get("trade_count", 0))))
            metric_bottom[2].metric("期间最低点 / Worst PnL", _format_currency(result.summary.get("worst_pnl")))
            curve = result.portfolio_value_curve.rename("区间净变动 / Period PnL").to_frame()
        else:
            metric_top = st.columns(4)
            metric_top[0].metric("期末资产 / Final Value", _format_currency(result.summary.get("final_value")))
            metric_top[1].metric("净收益 / Net PnL", _format_currency(result.summary.get("net_pnl")))
            metric_top[2].metric("估算费用 / Est. Fees", _format_currency(result.summary.get("total_fees")))
            metric_top[3].metric("成交笔数 / Trades", str(int(result.summary.get("trade_count", 0))))
            metric_bottom = st.columns(3)
            metric_bottom[0].metric("总收益 / Total Return", _format_pct(result.summary.get("total_return")))
            metric_bottom[1].metric("波动率 / Volatility", _format_pct(result.summary.get("volatility")))
            metric_bottom[2].metric("最大回撤 / Max Drawdown", _format_pct(result.summary.get("max_drawdown")))
            curve = result.portfolio_value_curve.rename("策略曲线 / Strategy").to_frame()
            if not result.benchmark_curve.empty:
                curve["基准曲线 / Benchmark"] = result.benchmark_curve

        st.markdown("**曲线 / Curve**")
        st.line_chart(curve, use_container_width=True)

        if result.note:
            st.info(result.note)

        st.markdown("**交易日志 / Trade Log**")
        if result.trade_log.empty:
            st.info("所选区间内没有交易记录 / No trades in the selected range.")
        else:
            st.dataframe(result.trade_log, use_container_width=True, hide_index=True)
            if "strategy" in result.trade_log.columns:
                breakdown = (
                    result.trade_log.groupby("strategy")["notional"]
                    .agg(["count", "sum"])
                    .reset_index()
                    .rename(columns={"strategy": "方法 / Method", "count": "成交笔数 / Trades", "sum": "成交额 / Notional"})
                )
                st.markdown("**方法拆分 / Method Breakdown**")
                st.dataframe(breakdown, use_container_width=True, hide_index=True)

    if mode == "基线月频回测 / Baseline Monthly":
        baseline_cols = st.columns(2)
        with baseline_cols[0]:
            history_source = st.selectbox("数据来源 / History Source", options=["yfinance", "futu"], index=0)
        with baseline_cols[1]:
            lookback_months = st.number_input("均线回看月数 / Lookback Months", min_value=2, max_value=24, value=settings.lookback_months)

        symbols_raw = st.text_input("标的代码 / Symbols", value=",".join(settings.symbols))
        symbols = tuple(part.strip() for part in symbols_raw.split(",") if part.strip())
        if not symbols:
            st.error("至少需要一个标的 / At least one symbol is required.")
            return
        default_benchmark_index = symbols.index(settings.benchmark) if settings.benchmark in symbols else 0
        benchmark = st.selectbox("基准 / Benchmark", options=list(symbols), index=default_benchmark_index)

        try:
            provider = _history_provider(history_source, settings)
            prices = provider.fetch_daily_closes(symbols, start.isoformat(), end.isoformat())
            result = run_backtest(
                prices,
                int(lookback_months),
                benchmark,
                float(initial_capital),
                trade_cost_model=build_trade_cost_model(settings),
                slippage_bps=settings.futu_price_buffer_bps,
            )
        except (MarketDataError, ValueError, KeyError) as exc:
            st.error(str(exc))
            return

        metric_top = st.columns(3)
        metric_top[0].metric("期末资产 / Final Value", _format_currency(result.summary.get("final_portfolio_value")))
        metric_top[1].metric("净收益 / Net PnL", _format_currency(result.summary.get("final_portfolio_value", 0.0) - float(initial_capital)))
        metric_top[2].metric("估算费用 / Est. Fees", _format_currency(result.summary.get("total_fees")))
        metric_bottom = st.columns(4)
        metric_bottom[0].metric("总收益 / Total Return", _format_pct(result.summary.get("total_return")))
        metric_bottom[1].metric("CAGR", _format_pct(result.summary.get("cagr")))
        metric_bottom[2].metric("波动率 / Volatility", _format_pct(result.summary.get("volatility")))
        metric_bottom[3].metric("最大回撤 / Max Drawdown", _format_pct(result.summary.get("max_drawdown")))

        curve = pd.DataFrame(
            {
                "策略净值 / Strategy": result.portfolio_value_curve,
                "基准净值 / Benchmark": float(initial_capital) * result.benchmark_curve,
            }
        )
        st.markdown("**收益曲线 / Equity Curve**")
        st.line_chart(curve, use_container_width=True)

        monthly_returns = result.monthly_returns.rename("月度收益 / Monthly Return").to_frame()
        st.markdown("**月度收益 / Monthly Returns**")
        st.bar_chart(monthly_returns, use_container_width=True)

        latest_weights = result.weights.iloc[-1]
        latest_weights = latest_weights[latest_weights > 0].sort_values(ascending=False)
        st.markdown("**最新目标仓位 / Latest Target Weights**")
        if latest_weights.empty:
            st.info("最新信号为空仓 / Latest signal is 100% cash.")
        else:
            st.dataframe(
                latest_weights.rename("目标权重 / Target Weight").reset_index().rename(columns={"index": "标的 / Symbol"}),
                use_container_width=True,
                hide_index=True,
            )

        st.markdown("**调仓日志 / Rebalance Log**")
        if result.rebalance_log.empty:
            st.info("所选区间内没有产生调仓记录 / No rebalance trades generated in the selected interval.")
        else:
            st.dataframe(result.rebalance_log, use_container_width=True, hide_index=True)
            csv_bytes = result.rebalance_log.to_csv(index=False).encode("utf-8")
            st.download_button("下载调仓日志 CSV / Download Rebalance Log CSV", data=csv_bytes, file_name="rebalance_log.csv")
        return

    if mode == "策略组合回测 / Strategy Stack Replay":
        baseline_weight, fusion_weight, cascade_weight, reserve_weight = stack_allocations(settings)
        st.info(
            "组合回测按当前 stack 配置运行。"
            f"当前组合 / Current Stack: {stack_label(settings)}。"
        )
        try:
            with FutuPaperTrader(settings) as trader:
                baseline_prices = pd.DataFrame()
                if settings.stack_baseline_enabled and baseline_weight > 0:
                    daily_series: dict[str, pd.Series] = {}
                    for code in settings.symbols:
                        frame = trader.request_history_klines(
                            code,
                            start=start.isoformat(),
                            end=end.isoformat(),
                            ktype="K_DAY",
                            session="RTH",
                        )
                        if frame.empty:
                            continue
                        normalized = frame[["time_key", "close"]].copy()
                        normalized["date"] = pd.to_datetime(normalized["time_key"]).dt.normalize()
                        normalized["close"] = pd.to_numeric(normalized["close"], errors="coerce")
                        daily_series[code] = normalized.dropna(subset=["date", "close"]).drop_duplicates(subset=["date"], keep="last").set_index("date")["close"].sort_index()
                    baseline_prices = pd.DataFrame(daily_series).sort_index().dropna(how="all") if daily_series else pd.DataFrame()

                fusion_settings = effective_fusion_settings(settings)
                fusion_symbols = [fusion_settings.fusion_benchmark, *fusion_settings.fusion_universe]
                fusion_frames = {
                    code: _normalize_kline(
                        trader.request_history_klines(
                            code,
                            start=start.isoformat(),
                            end=end.isoformat(),
                            ktype="K_1M",
                            session="RTH",
                        )
                    )
                    for code in fusion_symbols
                }
                cascade_frames = {}
                if cascade_weight > 0:
                    if fetch_cascade_daily_frames is None:
                        st.error("Claude/Cascade 数据模块当前没有加载完整。请重启监控页 / Dashboard once.")
                        return
                    cascade_frames = fetch_cascade_daily_frames(
                        trader,
                        settings,
                        start=start.isoformat(),
                        end=end.isoformat(),
                    )
            replay = run_strategy_stack_replay(
                baseline_prices,
                fusion_frames,
                settings,
                initial_capital=float(initial_capital),
                cascade_price_frames=cascade_frames,
            )
        except (FutuTradeError, ValueError, KeyError) as exc:
            st.error(str(exc))
            return
        _render_replay_result(replay)
        return

    if mode == "Claude/Cascade 回放 / Claude-Cascade Replay":
        st.info("Claude/Cascade 回放会按 claude-trade 的级联日频逻辑运行，并只使用当前 Futu 这边可交易的日线标的。")
        if run_cascade_replay is None:
            st.error("Claude/Cascade 回放模块当前没有加载完整。请重启监控页 / Dashboard once.")
            return
        if fetch_cascade_daily_frames is None:
            st.error("Claude/Cascade 数据模块当前没有加载完整。请重启监控页 / Dashboard once.")
            return
        try:
            with FutuPaperTrader(settings) as trader:
                price_frames = fetch_cascade_daily_frames(
                    trader,
                    settings,
                    start=start.isoformat(),
                    end=end.isoformat(),
                )
            replay = run_cascade_replay(price_frames, settings, initial_capital=float(initial_capital))
        except (FutuTradeError, ValueError, KeyError) as exc:
            st.error(str(exc))
            return
        _render_replay_result(replay)
        return

    if mode == "Fusion 日内回放 / Fusion Intraday Replay":
        st.warning("Fusion 回放是价格驱动近似版，用 1 分钟历史 K 线回放核心逻辑，不会重建历史 L2 和逐笔。")
        try:
            with FutuPaperTrader(settings) as trader:
                symbols = [settings.fusion_benchmark, *settings.fusion_universe]
                price_frames = {
                    code: _normalize_kline(
                        trader.request_history_klines(
                            code,
                            start=start.isoformat(),
                            end=end.isoformat(),
                            ktype="K_1M",
                            session="RTH",
                        )
                    )
                    for code in symbols
                }
            replay = run_fusion_intraday_replay(price_frames, settings, initial_capital=float(initial_capital))
        except (FutuTradeError, ValueError, KeyError) as exc:
            st.error(str(exc))
            return
        _render_replay_result(replay)
        return

    if mode == "精确执行复盘 / Exact Execution Replay":
        st.info("精确执行复盘会读取 runtime/market_data 里的已提交订单日志，再用 Futu 实际成交历史按 order_id 对齐。")
        try:
            with FutuPaperTrader(settings) as trader:
                acc_id = trader.resolve_trade_account()
                order_history = trader.get_order_history(acc_id, start.isoformat(), end.isoformat())
                logged_orders = market_logger.load_order_records(start.isoformat(), end.isoformat())
                submitted = (
                    logged_orders[
                        (logged_orders.get("action", pd.Series(dtype=str)).astype(str) == "submitted")
                        & (logged_orders.get("submit_status", pd.Series(dtype=str)).astype(str).str.lower() == "submitted")
                    ].copy()
                    if not logged_orders.empty
                    else logged_orders
                )
                matched_ids = set(submitted.get("submit_detail", pd.Series(dtype=str)).astype(str).tolist()) if not submitted.empty else set()
                exact_history = (
                    order_history[order_history["order_id"].astype(str).isin(matched_ids)].copy()
                    if matched_ids and not order_history.empty and "order_id" in order_history.columns
                    else order_history.iloc[0:0].copy()
                )
                symbols = (
                    sorted(set(exact_history["code"].tolist()))
                    if not exact_history.empty
                    else sorted(set(submitted.get("code", pd.Series(dtype=str)).tolist()))
                )
                price_frames = {
                    code: _normalize_kline(
                        trader.request_history_klines(
                            code,
                            start=start.isoformat(),
                            end=end.isoformat(),
                            ktype="K_1M",
                            session="RTH",
                        )
                    )
                    for code in symbols
                }
            replay = run_exact_execution_replay(start.isoformat(), end.isoformat(), order_history, price_frames, settings)
        except (FutuTradeError, ValueError, KeyError) as exc:
            st.error(str(exc))
            return
        _render_replay_result(replay)
        return

    account_env_label = "真实账户" if settings.futu_trd_env == "REAL" else "模拟账户"
    st.info(f"{account_env_label}复盘使用的是富途账户实际已成交订单，不是论文回测。所以你昨天账户有变化时，这里会直接反映出来。")
    try:
        with FutuPaperTrader(settings) as trader:
            acc_id = trader.resolve_trade_account()
            order_history = trader.get_order_history(acc_id, start.isoformat(), end.isoformat())
            filled_order_history = (
                order_history[pd.to_numeric(order_history.get("dealt_qty"), errors="coerce").fillna(0) > 0].copy()
                if not order_history.empty
                else order_history
            )
            symbols = sorted(set(filled_order_history["code"].tolist())) if not filled_order_history.empty else []
            day_span = max(1, (end - start).days)
            ktype = "K_1M" if day_span <= MAX_INTRADAY_DAYS["K_1M"] else "K_DAY"
            price_frames = {
                code: _normalize_kline(
                    trader.request_history_klines(
                        code,
                        start=start.isoformat(),
                        end=end.isoformat(),
                        ktype=ktype,
                        session="RTH",
                    )
                )
                for code in symbols
            }
        replay = run_account_replay(filled_order_history, price_frames, settings)
    except (FutuTradeError, ValueError, KeyError) as exc:
        st.error(str(exc))
        return
    _render_replay_result(replay)


def main() -> None:
    settings = load_settings()
    st.set_page_config(page_title="TAA Futu 控制终端 / Trading Terminal", layout="wide", initial_sidebar_state="expanded")
    st.title("TAA + Futu 控制终端 / Trading Terminal")
    st.caption(
        f"市场 / Market: {settings.futu_trd_market} | 交易环境 / Trade Env: {settings.futu_trd_env} | OpenD: {settings.futu_host}:{settings.futu_port}"
    )
    _inject_terminal_css()
    _render_sidebar_toolbar()

    page = st.sidebar.radio("页面 / View", options=["高级实时监控 / Advanced Live Monitor", "历史模拟 / Historical Simulation"])
    if page == "高级实时监控 / Advanced Live Monitor":
        render_live_monitor(settings)
    else:
        render_historical_simulation(settings)


if __name__ == "__main__":
    main()
