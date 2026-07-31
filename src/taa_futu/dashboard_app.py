from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import gc
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
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

from taa_futu import describe_build
from taa_futu.backtest import run_backtest
try:
    from taa_futu.cascade_sleeve import (
        CascadeSleevePlan,
        cascade_trade_symbols,
        fetch_cascade_daily_frames,
        generate_live_cascade_plan,
    )
except ImportError:
    import taa_futu.cascade_sleeve as _cascade_sleeve

    CascadeSleevePlan = _cascade_sleeve.CascadeSleevePlan
    cascade_trade_symbols = _cascade_sleeve.cascade_trade_symbols
    fetch_cascade_daily_frames = getattr(_cascade_sleeve, "fetch_cascade_daily_frames", None)
    generate_live_cascade_plan = _cascade_sleeve.generate_live_cascade_plan
from taa_futu.config import load_settings
from taa_futu.costs import (
    build_stock_fills_ledger,
    build_trade_cost_model,
    estimate_realized_from_fills,
    trade_log_total_fees,
    with_trade_costs,
)
from taa_futu.fusion_intraday import FusionIntradayStrategy, FusionPlan
from taa_futu.ofim_intraday import OfimIntradayStrategy, OfimPlan
from taa_futu.futu_gateway import FutuPaperTrader, FutuTradeError, FutuTransientError
from taa_futu.auto_trader import validate_auto_trader_mode
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
# LOB-exact replay engine (uses stored 40-level order-book data)
try:
    from taa_futu.intraday_replay import (
        run_fusion_replay as _run_fusion_lob_replay,
        run_ofim_replay as _run_ofim_lob_replay,
    )
except Exception:
    _run_fusion_lob_replay = None
    _run_ofim_lob_replay = None
from taa_futu.strategy_stack import (
    active_stack_strategy,
    baseline_sleeve_enabled,
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
    write_strategy_split_state,
)
from taa_futu.stock_events import load_stock_events
from taa_futu.stock_doctor import run_stock_system_doctor
from taa_futu.stock_ledger import build_stock_double_entry_ledger, reconcile_stock_ledger
from taa_futu.stock_learning import (
    STOCK_LEARNING_REVIEW_PACKET_FILE,
    load_learning_report,
    load_learning_review_packet,
    load_promotion_report,
    load_strategy_candidates,
    load_trade_outcomes,
    run_learning_pipeline,
)
from taa_futu.stock_runtime import (
    STOCK_FILLS_FILE,
    STOCK_LEDGER_EPOCH_FILE,
    load_stock_ledger_epoch,
    write_stock_ledger_epoch,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
FUTU_OPEND_APP = Path("/Applications/FutuOpenD.app")
AUTO_TRADER_STATUS_FILE = REPO_ROOT / "runtime" / "auto_trader_status.json"
AUTO_TRADER_PID_FILE = REPO_ROOT / "runtime" / "auto_trader.pid"
AUTO_TRADER_LOG_FILE = REPO_ROOT / "runtime" / "auto_trader.log"
WATCHDOG_STATUS_FILE = REPO_ROOT / "runtime" / "watchdog_status.json"
WATCHDOG_PID_FILE = REPO_ROOT / "runtime" / "watchdog.pid"
WATCHDOG_LOG_FILE = REPO_ROOT / "runtime" / "watchdog.log"
WATCHDOG_LAUNCH_AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.jiao.taa_futu_watchdog.plist"
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


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if pd.notna(result) else default


def _history_provider(name: str, settings) -> HistoricalDataProvider:
    if name == "futu":
        return FutuQuoteDataProvider(host=settings.futu_host, port=settings.futu_port)
    return YFinanceDataProvider()


def _format_currency(value: object) -> str:
    return f"{_safe_float(value):,.2f}"


def _format_pct(value: object) -> str:
    return f"{_safe_float(value) * 100:.2f}%"


def _dashboard_display_tail(frame: pd.DataFrame, *, max_rows: int = 500) -> pd.DataFrame:
    if frame.empty or len(frame) <= max_rows:
        return frame
    return frame.tail(max_rows).copy()


def _stock_account_start_date(today: date) -> date:
    raw = os.getenv("STOCK_ACCOUNT_START_DATE") or os.getenv("ACCOUNT_START_DATE") or "2026-04-01"
    try:
        parsed = pd.Timestamp(raw).date()
    except Exception:
        parsed = date(2026, 4, 1)
    return min(parsed, today)


def _recent_error_frame(days: int = 3) -> pd.DataFrame:
    end = datetime.now().date()
    start = end - timedelta(days=max(1, int(days)) - 1)
    errors = market_logger.load_records("errors", start.isoformat(), end.isoformat())
    if errors.empty:
        return errors
    if "detail" in errors.columns:
        errors["detail_preview"] = errors["detail"].astype(str).str.slice(0, 240)
    return errors


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
        "OFIM": set(settings.ofim_universe),
        "Claude/Cascade": cascade_symbols,
    }


def _owner_group_definitions(settings) -> dict[str, dict[str, object]]:
    strategy_sets = _strategy_symbol_sets(settings)
    return {
        "我的策略组 / Ours": {
            "symbols": set(strategy_sets["Baseline"]) | set(strategy_sets["Fusion"]) | set(strategy_sets["OFIM"]),
            "sleeves": ("Baseline", "Fusion", "OFIM"),
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
    ofim_targets: dict[str, float],
    cascade_targets: dict[str, float],
) -> dict[str, dict[str, float]]:
    return {
        "我的策略组 / Ours": stack_target_weights(baseline_targets, fusion_targets, ofim_targets),
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
    ofim_targets: dict[str, float],
    cascade_targets: dict[str, float],
    sleeve_allocations: dict[str, float],
) -> pd.DataFrame:
    owner_defs = _owner_group_definitions(settings)
    owner_sets = {name: set(defn["symbols"]) for name, defn in owner_defs.items()}
    owner_targets = _owner_group_targets(
        baseline_targets=baseline_targets,
        fusion_targets=fusion_targets,
        ofim_targets=ofim_targets,
        cascade_targets=cascade_targets,
    )
    rows: dict[str, dict[str, object]] = {}
    group_weights = {
        "我的策略组 / Ours": float(sleeve_allocations["baseline"]) + float(sleeve_allocations["fusion"]) + float(sleeve_allocations["ofim"]),
        "Claude/Cascade": float(sleeve_allocations["cascade"]),
    }
    group_components = {
        "我的策略组 / Ours": (
            f"Baseline {float(sleeve_allocations['baseline']):.0%} + "
            f"Fusion {float(sleeve_allocations['fusion']):.0%} + "
            f"OFIM {float(sleeve_allocations['ofim']):.0%}"
        ),
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
        "我的策略组 / Ours": "Baseline + Fusion + OFIM",
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


@dataclass(frozen=True)
class StockPerformanceSummary:
    base_assets: float
    epoch_start_value: float
    ledger_has_epoch: bool
    net_change: float
    net_change_pct: float | None
    gross_change: float
    gross_change_pct: float | None
    fee_total: float
    trade_count: int
    realized: float
    unrealized: float
    ledger_since_epoch: float | None
    ledger_realized: float
    ledger_unrealized: float


def _ledger_attr(primary: object, fallback: object, name: str, default: float = 0.0) -> float:
    value = getattr(primary, name, getattr(fallback, name, default))
    return _safe_float(value, default)


def _stock_performance_summary(
    *,
    total_assets: float,
    base_assets: float,
    stock_ledger_epoch: dict,
    stock_ledger_v2: object,
    stock_ledger_projection: object,
    estimated_fee_total: float,
    estimated_realized: float,
    estimated_unrealized: float,
    broker_realized: float | None,
    broker_unrealized: float | None,
    selected_range_trade_count: int,
) -> StockPerformanceSummary:
    epoch_snapshot = dict((stock_ledger_epoch or {}).get("account_snapshot") or {})
    epoch_start_value = _safe_float(epoch_snapshot.get("total_assets"))
    ledger_has_epoch = bool((stock_ledger_epoch or {}).get("ts") and epoch_start_value > 0)
    ledger_realized = _ledger_attr(stock_ledger_v2, stock_ledger_projection, "net_realized_pnl", 0.0)
    ledger_fees = _ledger_attr(stock_ledger_v2, stock_ledger_projection, "fees_paid", 0.0)
    ledger_trade_count = int(_ledger_attr(stock_ledger_v2, stock_ledger_projection, "trade_count", 0.0))
    ledger_since_epoch = total_assets - epoch_start_value if ledger_has_epoch else None
    ledger_unrealized = (ledger_since_epoch - ledger_realized) if ledger_since_epoch is not None else 0.0

    performance_base = base_assets
    net_change = total_assets - performance_base
    net_change_pct = (net_change / performance_base) if performance_base > 0 else None
    fee_total = estimated_fee_total
    gross_change = net_change + fee_total
    gross_change_pct = (gross_change / performance_base) if performance_base > 0 else None
    realized = broker_realized if broker_realized is not None else estimated_realized
    unrealized = broker_unrealized if broker_unrealized is not None else estimated_unrealized
    trade_count = selected_range_trade_count

    return StockPerformanceSummary(
        base_assets=performance_base,
        epoch_start_value=epoch_start_value,
        ledger_has_epoch=ledger_has_epoch,
        net_change=net_change,
        net_change_pct=net_change_pct,
        gross_change=gross_change,
        gross_change_pct=gross_change_pct,
        fee_total=fee_total,
        trade_count=trade_count,
        realized=realized,
        unrealized=unrealized,
        ledger_since_epoch=ledger_since_epoch,
        ledger_realized=ledger_realized,
        ledger_unrealized=ledger_unrealized,
    )


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
    if FutuPaperTrader.is_transient_error(text):
        return True
    markers = (
        "查询未完成订单请求超时",
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


def _runtime_error_banner_level(current_action: str, current_detail: object, latest_age_min: float | None) -> str:
    if current_action == "error" and not _is_transient_status_message(current_detail):
        return "error"
    if (
        current_action == "transient_error"
        or _is_transient_status_message(current_detail)
        or (latest_age_min is not None and latest_age_min <= 10)
    ):
        return "warning"
    return "info"


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


def _build_runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    pythonpath_parts = [str(SRC_ROOT)]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    return env


def _pid_from_file(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return 0


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _watchdog_command() -> list[str]:
    python = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
    return [str(python), "-m", "taa_futu.watchdog"]


def _stock_auto_running() -> bool:
    return _is_pid_running(_pid_from_file(WATCHDOG_PID_FILE)) or _is_pid_running(_pid_from_file(AUTO_TRADER_PID_FILE))


def _safe_subprocess_run(cmd: list[str], **kwargs):
    """Wrap subprocess.run so a missing binary (e.g. launchctl on non-macOS,
    or open in CI) returns a synthetic non-zero result instead of crashing
    the Streamlit rerun with a stack trace."""
    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError as exc:
        class _Stub:
            returncode = 127
            stdout = ""
            stderr = f"binary not found: {exc.filename or cmd[0]}"
        return _Stub()


def _start_stock_auto_runtime(settings) -> tuple[bool, str]:
    if _stock_auto_running():
        return True, "自动运行已经在跑 / Auto runtime is already running."
    try:
        validate_auto_trader_mode(settings, submit=True)
    except SystemExit as exc:
        return False, f"启动被安全锁拦住 / Start blocked by safety guard: {exc}"

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if WATCHDOG_LAUNCH_AGENT_PLIST.exists():
        result = _safe_subprocess_run(
            ["launchctl", "load", str(WATCHDOG_LAUNCH_AGENT_PLIST)],
            check=False, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, "已通过开机守护服务启动自动运行 / Started through LaunchAgent watchdog."
        return False, f"LaunchAgent 启动失败 / LaunchAgent failed: {result.stderr.strip() or result.stdout.strip()}"

    try:
        with WATCHDOG_LOG_FILE.open("a", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                _watchdog_command(),
                cwd=REPO_ROOT,
                env=_build_runtime_env(),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
        return True, f"已启动自动运行守护 / Started auto-run watchdog with pid {process.pid}."
    except (FileNotFoundError, OSError) as exc:
        return False, f"启动失败 / Start failed: {exc}"


def _stop_stock_auto_runtime() -> tuple[bool, str]:
    _safe_subprocess_run(
        ["launchctl", "unload", str(WATCHDOG_LAUNCH_AGENT_PLIST)],
        check=False, capture_output=True,
    )
    stopped: list[str] = []
    for label, path in (("watchdog", WATCHDOG_PID_FILE), ("auto trader", AUTO_TRADER_PID_FILE)):
        pid = _pid_from_file(path)
        if _is_pid_running(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                stopped.append(f"{label} pid {pid}")
            except OSError:
                pass

    for pattern in ("taa_futu.auto_trader", "taa_futu.watchdog"):
        _safe_subprocess_run(["pkill", "-f", pattern], check=False, capture_output=True)

    if stopped:
        return True, f"已发送停止信号 / Sent stop signal to {' and '.join(stopped)}."
    return True, "自动运行和守护监控当前都没在跑 / Auto runtime and watchdog were already stopped."


def _open_futu_opend() -> tuple[bool, str]:
    if not FUTU_OPEND_APP.exists():
        return False, "找不到 FutuOpenD.app，请确认已安装到 /Applications。"
    result = _safe_subprocess_run(["open", str(FUTU_OPEND_APP)], check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return True, "已打开 FutuOpenD / Opened FutuOpenD."
    return False, result.stderr.strip() if isinstance(result.stderr, str) else "打开 FutuOpenD 失败 / Failed to open FutuOpenD."


def _dashboard_port_open(port: int = 8501) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _stash_app_msg(ok: bool, message: str) -> None:
    """Stash the button-click outcome so the *next* rerun can display it.

    Streamlit's button click already triggers a rerun on its own. If the
    callback calls ``st.success(msg)`` and then ``st.rerun()``, the
    success message is rendered into the dying rerun and immediately
    discarded — the user sees nothing happen. Stashing into session_state
    and rendering on the next rerun is the supported pattern.
    """
    st.session_state["_app_control_msg"] = (bool(ok), str(message))
    # Toast is non-disruptive and survives the rerun on Streamlit 1.27+
    try:
        st.toast(message, icon="✅" if ok else "❌")
    except Exception:
        # toast doesn't exist on older streamlit — session_state still
        # gives the user the persistent banner below.
        pass


def _render_app_controls(settings) -> None:
    st.markdown("**App 控制 / App Controls**")

    # Show the previous click's feedback BEFORE rendering new buttons so
    # the user sees that their last action did something. The message
    # is consumed (popped) so it doesn't stick around forever.
    last_msg = st.session_state.pop("_app_control_msg", None)
    if last_msg is not None:
        ok, message = last_msg
        (st.success if ok else st.error)(message)

    cols = st.columns(4)
    if cols[0].button("打开 OpenD / Open FutuOpenD", use_container_width=True):
        ok, message = _open_futu_opend()
        _stash_app_msg(ok, message)
        # No st.rerun() — st.button already triggered one.
    if cols[1].button("启动自动运行 / Start Auto", use_container_width=True):
        ok, message = _start_stock_auto_runtime(settings)
        _stash_app_msg(ok, message)
    if cols[2].button("停止自动运行 / Stop Auto", use_container_width=True):
        ok, message = _stop_stock_auto_runtime()
        _stash_app_msg(ok, message)
    if cols[3].button("刷新页面 / Refresh", use_container_width=True):
        st.rerun()

    # 演示模式下不探测真实端口——本机恰好开着 OpenD 时这行会显示「已连接」，
    # 而页面顶部的横幅写着「未连接富途」，自相矛盾。截图发出去更是误导。
    from taa_futu.demo_gateway import demo_enabled
    if demo_enabled():
        st.caption("OpenD: 演示模式 / demo | 监控页 / Dashboard: 演示模式 / demo | "
                   "自动运行 / Auto Runtime: 演示模式 / demo")
    else:
        dashboard_state = "运行中 / running" if _dashboard_port_open(8501) else "未检测到 / not detected"
        auto_state = "运行中 / running" if _stock_auto_running() else "已停止 / stopped"
        opend_state = "已连接 / connected" if _dashboard_port_open(getattr(settings, "futu_port", 11111)) else "未连接 / not connected"
        st.caption(
            f"OpenD: {opend_state} | 监控页 / Dashboard: {dashboard_state} | 自动运行 / Auto Runtime: {auto_state}"
        )

    _render_embedded_control_panel(settings)


def _render_embedded_control_panel(settings) -> None:
    """Full streamlit replica of the Tkinter ControlPanel — every routine
    operation is inlined so the user never has to leave the unified app.

    Eight stacked sub-sections:
        1. 系统状态 / System Status (read-only cards)
        2. 一键服务 / One-Click Services (Open OpenD / Start-Stop Auto / Dashboard ops)
        3. 连接设置 / Connection (OpenD host/port, Dashboard port — writes .env)
        4. Strategy Stack (5 plug buttons + 4 weight sliders)
        5. Fusion Pre-gate (3 mode buttons)
        6. 回测 / Backtest (date range + run + tail output)
        7. 信号 / Signals (taa-futu signals + paper-trade plan)
        8. 订单与工具 / Orders & Tools (cancel-all / Doctor / Tkinter fallback)

    The heavy logic stays in CLI subcommands — each button is a wrapper
    around ``taa-futu <subcommand>`` so we share a single source of truth
    with the legacy command-line workflow.
    """
    with st.expander("🎛️ 完整控制台 / Embedded Control Panel", expanded=False):

        # ─────────── 1. 系统状态 / System Status ───────────
        st.markdown("##### 1. 系统状态 / System Status")
        status_cols = st.columns(4)
        opend_ok = _dashboard_port_open(getattr(settings, "futu_port", 11111))
        dash_ok = _dashboard_port_open(8501)
        auto_ok = _stock_auto_running()
        watchdog_pid = _pid_from_file(WATCHDOG_PID_FILE)
        watchdog_ok = _is_pid_running(watchdog_pid)
        status_cols[0].markdown(("🟢" if opend_ok else "🔴") + f" **OpenD**: {'连接 / connected' if opend_ok else '断开 / down'}")
        status_cols[1].markdown(("🟢" if dash_ok else "⚪") + f" **Dashboard**: {'8501 已起' if dash_ok else '未起'}")
        status_cols[2].markdown(("🟢" if auto_ok else "⚪") + f" **Auto Run**: {'运行 / running' if auto_ok else '停止 / stopped'}")
        status_cols[3].markdown(("🟢" if watchdog_ok else "⚪") + f" **Watchdog**: {'pid=' + str(watchdog_pid) if watchdog_ok else '未启'}")
        stack_active = getattr(settings, "stack_active_strategy", None) or "(无独占, 走权重)"
        st.caption(
            f"Stack: active={stack_active} | "
            f"权重 Baseline={settings.stack_baseline_weight:.2f}, Fusion={settings.stack_fusion_weight:.2f}, "
            f"OFIM={settings.stack_ofim_weight:.2f}, Cascade={settings.stack_cascade_weight:.2f} | "
            f"交易环境: {settings.futu_trd_env}"
        )

        st.divider()

        # ─────────── 2. 一键服务 / One-Click Services ───────────
        st.markdown("##### 2. 一键服务 / One-Click Services")
        s1, s2, s3, s4 = st.columns(4)
        if s1.button("打开 OpenD App", use_container_width=True, key="cp_open_opend"):
            ok, msg = _open_futu_opend()
            _stash_app_msg(ok, msg)
        if s2.button("启动自动运行", use_container_width=True, key="cp_start_auto",
                     disabled=auto_ok, type="primary" if not auto_ok else "secondary"):
            ok, msg = _start_stock_auto_runtime(settings)
            _stash_app_msg(ok, msg)
        if s3.button("停止自动运行", use_container_width=True, key="cp_stop_auto",
                     disabled=not auto_ok):
            ok, msg = _stop_stock_auto_runtime()
            _stash_app_msg(ok, msg)
        if s4.button("刷新页面", use_container_width=True, key="cp_refresh"):
            st.rerun()

        st.divider()

        # ─────────── 3. 连接设置 / Connection ───────────
        st.markdown("##### 3. 连接设置 / Connection")
        c1, c2, c3 = st.columns(3)
        new_host = c1.text_input(
            "OpenD Host", value=getattr(settings, "futu_host", "127.0.0.1"), key="cp_opend_host",
        )
        new_port = c2.number_input(
            "OpenD Port", value=int(getattr(settings, "futu_port", 11111)),
            min_value=1, max_value=65535, key="cp_opend_port",
        )
        new_dash_port = c3.number_input(
            "Dashboard Port (web 浏览器版)", value=8501, min_value=1, max_value=65535, key="cp_dash_port",
        )
        if st.button("保存连接设置 → .env", key="cp_save_conn"):
            try:
                from taa_futu.control_panel import ENV_FILE
                text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
                text = _set_env_value(text, "FUTU_HOST", str(new_host).strip())
                text = _set_env_value(text, "FUTU_PORT", str(int(new_port)))
                ENV_FILE.write_text(text, encoding="utf-8")
                _stash_app_msg(True, f"已写入 .env (FUTU_HOST={new_host}, FUTU_PORT={new_port})")
            except Exception as exc:
                _stash_app_msg(False, f"写入失败: {type(exc).__name__}: {exc}")

        st.divider()

        # ─────────── 4. Strategy Stack ───────────
        st.markdown("##### 4. Strategy Stack —— 独占插头 + 权重")
        plug_cols = st.columns(5)
        if plug_cols[0].button("单跑 Baseline", use_container_width=True, key="plug_baseline"):
            ok, msg = _apply_plug_mode("baseline")
            _stash_app_msg(ok, msg)
        if plug_cols[1].button("单跑 Fusion", use_container_width=True, key="plug_fusion"):
            ok, msg = _apply_plug_mode("fusion")
            _stash_app_msg(ok, msg)
        if plug_cols[2].button("单跑 OFIM", use_container_width=True, key="plug_ofim"):
            ok, msg = _apply_plug_mode("ofim")
            _stash_app_msg(ok, msg)
        if plug_cols[3].button("单跑 Cascade", use_container_width=True, key="plug_cascade"):
            ok, msg = _apply_plug_mode("cascade")
            _stash_app_msg(ok, msg)
        if plug_cols[4].button("满栈 25%×4", use_container_width=True, key="plug_full"):
            ok, msg = _apply_full_stack()
            _stash_app_msg(ok, msg)

        # Weight sliders — let the user push any blend
        w_cols = st.columns(4)
        w_b = w_cols[0].slider("Baseline %", 0.0, 1.0, float(settings.stack_baseline_weight), 0.05, key="cp_wb")
        w_f = w_cols[1].slider("Fusion %", 0.0, 1.0, float(settings.stack_fusion_weight), 0.05, key="cp_wf")
        w_o = w_cols[2].slider("OFIM %", 0.0, 1.0, float(settings.stack_ofim_weight), 0.05, key="cp_wo")
        w_c = w_cols[3].slider("Cascade %", 0.0, 1.0, float(settings.stack_cascade_weight), 0.05, key="cp_wc")
        total = w_b + w_f + w_o + w_c
        if total > 1.0001:
            st.warning(f"四个权重总和 {total:.2f} > 1.0，多出来的会被风控砍。")
        else:
            st.caption(f"四个权重总和 {total:.2f} (差额 {1-total:.2f} 留作现金 / cash reserve)")
        if st.button("应用自定义权重 → .env", key="cp_apply_weights"):
            ok, msg = _apply_stack_weights(w_b, w_f, w_o, w_c)
            _stash_app_msg(ok, msg)

        st.divider()

        # ─────────── 5. Fusion Pre-gate ───────────
        st.markdown("##### 5. Fusion 富途盘前过滤 / Pre-gate")
        pg_cols = st.columns(3)
        cur = "active" if getattr(settings, "fusion_futu_pregate_enabled", False) and not getattr(settings, "fusion_futu_pregate_log_only", True) \
            else ("log_only" if getattr(settings, "fusion_futu_pregate_enabled", False) else "off")
        if pg_cols[0].button("Off 关闭", use_container_width=True, key="pg_off",
                             type="primary" if cur == "off" else "secondary"):
            ok, msg = _apply_pregate_mode("off")
            _stash_app_msg(ok, msg)
        if pg_cols[1].button("LogOnly 只记录", use_container_width=True, key="pg_log",
                             type="primary" if cur == "log_only" else "secondary"):
            ok, msg = _apply_pregate_mode("log_only")
            _stash_app_msg(ok, msg)
        if pg_cols[2].button("Active 生效", use_container_width=True, key="pg_active",
                             type="primary" if cur == "active" else "secondary"):
            ok, msg = _apply_pregate_mode("active")
            _stash_app_msg(ok, msg)
        st.caption(f"当前: **{cur}** （阈值改在 .env 里的 FUSION_FUTU_PREGATE_* 行）")

        st.divider()

        # ─────────── 6. 回测 / Backtest ───────────
        st.markdown("##### 6. 回测 / Backtest")
        b1, b2, b3, b4 = st.columns([1, 1, 1, 1.2])
        from datetime import date as _date, timedelta as _td
        bt_start = b1.date_input("起始 / Start", value=_date.today() - _td(days=365 * 3), key="cp_bt_start")
        bt_end = b2.date_input("结束 / End", value=_date.today(), key="cp_bt_end")
        bt_strategy = b3.selectbox("策略 / Strategy", ["baseline", "fusion", "ofim", "stack"], index=0, key="cp_bt_strategy")
        if b4.button("运行回测 / Run Backtest", type="primary", use_container_width=True, key="cp_run_bt"):
            with st.spinner("运行回测中…可能 5-30 秒"):
                res = _run_cli_inline([
                    "backtest",
                    "--strategy", str(bt_strategy),
                    "--start", str(bt_start),
                    "--end", str(bt_end),
                ], timeout=120)
            _stash_app_msg(res["ok"], "回测完成" if res["ok"] else "回测失败")
            st.code(res["text"][-4000:] or "(空)", language="text")

        st.divider()

        # ─────────── 7. 信号 / Signals ───────────
        st.markdown("##### 7. 信号 / Signals")
        sg1, sg2 = st.columns(2)
        if sg1.button("查看月度 baseline 信号", use_container_width=True, key="cp_signals"):
            with st.spinner("taa-futu signals …"):
                res = _run_cli_inline(["signals"], timeout=30)
            _stash_app_msg(res["ok"], "信号查询完成" if res["ok"] else "信号查询失败")
            st.code(res["text"] or "(空)", language="text")
        if sg2.button("预演订单 / Plan Orders (dry-run)", use_container_width=True, key="cp_plan"):
            with st.spinner("taa-futu paper-trade（不提交）…"):
                res = _run_cli_inline(["paper-trade"], timeout=60)
            _stash_app_msg(res["ok"], "预演完成" if res["ok"] else "预演失败")
            st.code(res["text"] or "(空)", language="text")

        st.divider()

        # ─────────── 8. 订单与工具 / Orders & Tools ───────────
        st.markdown("##### 8. 订单与工具 / Orders & Tools")
        t1, t2, t3 = st.columns(3)
        if t1.button("取消所有挂单 / Cancel All Open", use_container_width=True, key="cp_cancel_all",
                     help="撤掉当前所有未成交的挂单（不平仓）"):
            with st.spinner("taa-futu cancel-orders …"):
                res = _run_cli_inline(["cancel-orders"], timeout=30)
            _stash_app_msg(res["ok"], res["text"][:200] or "已撤")
        if t2.button("启动桌面控制台 (Tkinter)", use_container_width=True, key="cp_launch_tk"):
            ok, msg = _open_tkinter_panel()
            _stash_app_msg(ok, msg)
        if t3.button("系统体检 Doctor", use_container_width=True, key="cp_doctor"):
            res = _run_doctor_inline()
            _stash_app_msg(res["ok"], res["text"][:200])
            with st.expander("Doctor 完整输出", expanded=True):
                st.code(res["text"], language="text")

        # ── Real-trading guard: surface the safety locks so the user knows
        # what is actually live vs. simulated.
        st.caption(
            f"🔒 实盘保护 / REAL guards: "
            f"FUTU_TRD_ENV=**{settings.futu_trd_env}** · "
            f"FUTU_ENABLE_REAL_TRADING=**{getattr(settings, 'futu_enable_real_trading', False)}** · "
            f"FUTU_ALLOW_AUTO_REAL=**{getattr(settings, 'futu_allow_auto_real', False)}** —— "
            f"三层都为真才会真下单；改这些需要手动编辑 .env。"
        )


def _apply_stack_weights(b: float, f: float, o: float, c: float) -> tuple[bool, str]:
    """Apply custom 4-sleeve weights and disable plug mode."""
    try:
        from taa_futu.control_panel import ENV_FILE
        text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
        # Disable any active plug so the weights actually apply
        text = _set_env_value(text, "STACK_ACTIVE_STRATEGY", "")
        # Baseline needs both _ENABLED and _WEIGHT
        text = _set_env_value(text, "STACK_BASELINE_ENABLED", "true" if b > 0 else "false")
        text = _set_env_value(text, "STACK_BASELINE_WEIGHT", f"{b:.4f}")
        text = _set_env_value(text, "STACK_FUSION_WEIGHT", f"{f:.4f}")
        text = _set_env_value(text, "STACK_OFIM_WEIGHT", f"{o:.4f}")
        text = _set_env_value(text, "STACK_CASCADE_WEIGHT", f"{c:.4f}")
        ENV_FILE.write_text(text, encoding="utf-8")
        return True, f"已应用权重: Baseline {b:.2f}, Fusion {f:.2f}, OFIM {o:.2f}, Cascade {c:.2f}"
    except Exception as exc:
        return False, f"应用权重失败: {type(exc).__name__}: {exc}"


def _run_cli_inline(args: list[str], *, timeout: int = 60) -> dict:
    """Subprocess to taa-futu and return text output for display."""
    python = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
    try:
        completed = subprocess.run(
            [str(python), "-m", "taa_futu.cli", *args],
            cwd=str(REPO_ROOT),
            env=_build_runtime_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": completed.returncode == 0,
            "text": (completed.stdout + ("\nSTDERR:\n" + completed.stderr if completed.stderr else "")).strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": f"超时 {timeout}s"}
    except Exception as exc:
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}


def _apply_plug_mode(strategy: str) -> tuple[bool, str]:
    """Write STACK_ACTIVE_STRATEGY into trade/.env so the next auto_trader
    cycle picks it up. Read-only against state files; only touches .env."""
    try:
        from taa_futu.control_panel import ENV_FILE
        text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
        new_text = _set_env_value(text, "STACK_ACTIVE_STRATEGY", strategy)
        ENV_FILE.write_text(new_text, encoding="utf-8")
        return True, f"已切到独占插头: {strategy}（下个 cycle 生效）"
    except Exception as exc:
        return False, f"切换插头失败: {type(exc).__name__}: {exc}"


def _apply_full_stack() -> tuple[bool, str]:
    """Disable plug mode and set Baseline/Fusion/OFIM/Cascade = 0.25 each."""
    try:
        from taa_futu.control_panel import ENV_FILE
        text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
        text = _set_env_value(text, "STACK_ACTIVE_STRATEGY", "")
        text = _set_env_value(text, "STACK_BASELINE_ENABLED", "true")
        text = _set_env_value(text, "STACK_BASELINE_WEIGHT", "0.25")
        text = _set_env_value(text, "STACK_FUSION_WEIGHT", "0.25")
        text = _set_env_value(text, "STACK_OFIM_WEIGHT", "0.25")
        text = _set_env_value(text, "STACK_CASCADE_WEIGHT", "0.25")
        ENV_FILE.write_text(text, encoding="utf-8")
        return True, "已切到满栈 25%×4（下个 cycle 生效）"
    except Exception as exc:
        return False, f"切换满栈失败: {type(exc).__name__}: {exc}"


def _apply_pregate_mode(mode: str) -> tuple[bool, str]:
    """Wire .env to the requested pre-gate mode: off / log_only / active."""
    try:
        from taa_futu.control_panel import ENV_FILE
        text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
        if mode == "off":
            text = _set_env_value(text, "FUSION_FUTU_PREGATE_ENABLED", "false")
            text = _set_env_value(text, "FUSION_FUTU_PREGATE_LOG_ONLY", "true")
        elif mode == "log_only":
            text = _set_env_value(text, "FUSION_FUTU_PREGATE_ENABLED", "true")
            text = _set_env_value(text, "FUSION_FUTU_PREGATE_LOG_ONLY", "true")
        elif mode == "active":
            text = _set_env_value(text, "FUSION_FUTU_PREGATE_ENABLED", "true")
            text = _set_env_value(text, "FUSION_FUTU_PREGATE_LOG_ONLY", "false")
        else:
            return False, f"未知 pre-gate 模式: {mode}"
        ENV_FILE.write_text(text, encoding="utf-8")
        return True, f"Fusion pre-gate → {mode}（下个 cycle 生效）"
    except Exception as exc:
        return False, f"pre-gate 切换失败: {type(exc).__name__}: {exc}"


def _set_env_value(text: str, key: str, value: str) -> str:
    """Append or replace ``KEY=value`` line in a .env file body. Robust to
    missing trailing newline and to keys that don't exist yet."""
    import re
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    line = f"{key}={value}"
    if pattern.search(text):
        return pattern.sub(line, text)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


def _open_tkinter_panel() -> tuple[bool, str]:
    """Spawn the legacy Tkinter ControlPanel as a separate process."""
    python = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
    try:
        subprocess.Popen(
            [str(python), "-m", "taa_futu.control_panel"],
            cwd=str(REPO_ROOT),
            env=_build_runtime_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True, "已启动桌面控制台 / Tkinter ControlPanel started"
    except Exception as exc:
        return False, f"启动失败: {type(exc).__name__}: {exc}"


def _run_doctor_inline() -> dict:
    """Run stock-system-doctor and capture output for inline display."""
    python = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
    try:
        completed = subprocess.run(
            [str(python), "-m", "taa_futu.cli", "stock-system-doctor"],
            cwd=str(REPO_ROOT),
            env=_build_runtime_env(),
            capture_output=True,
            text=True,
            timeout=20,
        )
        return {
            "ok": completed.returncode == 0,
            "text": (completed.stdout + ("\nSTDERR:\n" + completed.stderr if completed.stderr else "")).strip() or "(空)",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "doctor 超时（20s）"}
    except Exception as exc:
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}


def _cached_live_value(cached_payload: object, key: str, default):
    if isinstance(cached_payload, dict):
        cached_value = cached_payload.get(key)
        if cached_value is not None:
            return cached_value
    return default


def _safe_live_fetch(cached_payload: object, key: str, fn, default):
    try:
        return fn()
    except FutuTransientError:
        return _cached_live_value(cached_payload, key, default)


def _empty_fusion_plan(settings) -> FusionPlan:
    return FusionPlan(
        benchmark=settings.fusion_benchmark,
        benchmark_score=0.0,
        exposure=0.0,
        target_weights={},
        features=[],
    )


def _empty_ofim_plan(settings) -> OfimPlan:
    return OfimPlan(
        strategy="OFIM",
        benchmark=settings.ofim_benchmark,
        benchmark_score=0.0,
        exposure=0.0,
        target_weights={},
        features=[],
    )


def _empty_cascade_plan() -> CascadeSleevePlan:
    return CascadeSleevePlan(
        target_weights={},
        total_exposure=0.0,
        regime_label="N/A",
        regime_score=0.0,
        note="",
    )


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
  /* ── JQ Quant 统一设计令牌 ────────────────────────────────────────────
     与新闻看板 (news collector/market_news/services/reporting.py) 共用同一套
     值，两者显示在同一个窗口里，配色必须一致。

     这里原本有 28 个硬编码颜色，其中 7 个近乎相同的深蓝黑、4 个近乎相同的
     灰、9 个近乎相同的边框灰。差一两个色值的边框并排出现时，眼睛察觉得到
     不齐，却说不出哪里不对——这正是「看着不协调」的来源之一。归并成下面
     8 个令牌后，改配色只动这一处。

     涨跌沿用中港习惯：红涨绿跌。这与欧美相反，改动时务必保持。 */
  :root {
    --jq-bg-top: #f6f9fc;
    --jq-bg-bottom: #eef3f8;
    --jq-surface: #ffffff;
    --jq-surface-soft: #f4f8fb;
    --jq-line: #d9e3ee;
    --jq-ink: #182534;
    --jq-muted: #6b7d90;
    --jq-up: #ff4d67;
    --jq-down: #20c997;
    --jq-radius-lg: 16px;
    --jq-radius-md: 12px;
    --jq-shadow: 0 10px 24px rgba(24, 37, 52, 0.07);
  }
  .stApp {
    background:
      radial-gradient(circle at top left, rgba(97, 132, 177, 0.10), transparent 24%),
      linear-gradient(180deg, var(--jq-bg-top) 0%, var(--jq-bg-bottom) 100%);
    color: var(--jq-ink);
  }
  [data-testid="stSidebar"] {
    background: linear-gradient(180deg, var(--jq-bg-top) 0%, var(--jq-bg-bottom) 100%);
    border-right: 1px solid var(--jq-line);
  }
  .terminal-shell {
    border: 1px solid var(--jq-line);
    border-radius: var(--jq-radius-lg);
    background: linear-gradient(180deg, rgba(255,255,255,0.98) 0%, rgba(247,250,253,0.98) 100%);
    box-shadow: var(--jq-shadow);
    padding: 14px 16px;
  }
  .terminal-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 16px;
    border-bottom: 1px solid var(--jq-line);
    padding-bottom: 10px;
    margin-bottom: 12px;
  }
  .terminal-title {
    font-size: 30px;
    font-weight: 800;
    color: var(--jq-ink);
    letter-spacing: 0.01em;
  }
  .terminal-subtitle {
    margin-top: 4px;
    color: var(--jq-muted);
    font-size: 12px;
  }
  .terminal-price {
    font-size: 38px;
    font-weight: 800;
    line-height: 1;
  }
  .terminal-up {
    color: var(--jq-up);
  }
  .terminal-down {
    color: var(--jq-down);
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
    border: 1px solid var(--jq-line);
    color: var(--jq-muted);
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
    border-radius: var(--jq-radius-md);
    background: rgba(247, 250, 253, 0.98);
    border: 1px solid var(--jq-line);
  }
  .terminal-mini-label {
    color: var(--jq-muted);
    font-size: 11px;
    margin-bottom: 4px;
  }
  .terminal-mini-value {
    color: var(--jq-ink);
    font-size: 18px;
    font-weight: 700;
  }
  .terminal-panel-title {
    font-size: 18px;
    font-weight: 700;
    color: var(--jq-ink);
    margin-bottom: 10px;
  }
  .terminal-caption {
    color: var(--jq-muted);
    font-size: 12px;
  }
  .terminal-divider {
    height: 1px;
    background: var(--jq-line);
    margin: 12px 0;
  }
  div[data-testid="stDataFrame"] {
    border: 1px solid var(--jq-line);
    border-radius: 14px;
    overflow: hidden;
    background: var(--jq-surface);
  }
  div[data-testid="stMetric"] {
    border: 1px solid var(--jq-line);
    border-radius: 14px;
    background: var(--jq-surface);
    padding: 8px 10px;
  }
  div[data-testid="stMetricValue"] {
    color: var(--jq-ink);
  }
  div[data-testid="stMetricLabel"] {
    color: var(--jq-muted);
    white-space: normal !important;
    overflow-wrap: anywhere !important;
    line-height: 1.2 !important;
    font-size: 0.94rem !important;
  }
  .stSelectbox label, .stDateInput label, .stSegmentedControl label, .stToggle label, .stTabs [data-baseweb="tab"] {
    color: var(--jq-ink) !important;
  }
  .stMarkdown, .stCaption, .stAlert {
    color: var(--jq-ink);
  }
  [data-testid="stSidebarCollapseButton"] button,
  [data-testid="stSidebarCollapsedControl"] button,
  [data-testid="collapsedControl"] button {
    background: var(--jq-surface) !important;
    border: 1px solid var(--jq-line) !important;
    border-radius: 10px !important;
    box-shadow: 0 4px 12px rgba(26, 42, 62, 0.10) !important;
    color: var(--jq-ink) !important;
  }
  [data-testid="stSidebarCollapseButton"] button:hover,
  [data-testid="stSidebarCollapsedControl"] button:hover,
  [data-testid="collapsedControl"] button:hover {
    background: var(--jq-surface-soft) !important;
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
    height = 736
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
    background:
      radial-gradient(circle at top, rgba(40, 67, 91, 0.22), transparent 34%),
      linear-gradient(180deg, #0b1118 0%, #0f1721 100%);
    border: 1px solid #213243;
    border-radius: 20px;
    overflow: hidden;
    color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', sans-serif;
    box-shadow:
      0 18px 42px rgba(5, 10, 17, 0.18),
      inset 0 1px 0 rgba(255, 255, 255, 0.02);
  }
  #top-__ID__ {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 12px 6px 12px;
    border-bottom: 1px solid #192836;
    background: rgba(10, 15, 22, 0.96);
  }
  #title-__ID__ {
    font-size: 13px;
    font-weight: 700;
    color: #f3f7fb;
    letter-spacing: 0.01em;
  }
  #subtitle-__ID__ {
    margin-top: 2px;
    font-size: 9px;
    color: #86a0b5;
  }
  #meta-__ID__ {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 5px;
    align-content: flex-start;
  }
  .chip-__ID__ {
    padding: 2px 7px;
    border-radius: 999px;
    background: rgba(35, 50, 65, 0.90);
    color: #b9ccda;
    font-size: 8px;
    line-height: 1.2;
    white-space: nowrap;
    border: 1px solid rgba(95, 125, 152, 0.26);
  }
  #legend-__ID__ {
    padding: 6px 10px 5px 10px;
    border-bottom: 1px solid #172532;
    background: rgba(9, 15, 21, 0.92);
    font: 500 10px ui-monospace, SFMono-Regular, Menlo, monospace;
    line-height: 1.4;
    color: #cddae5;
    min-height: 48px;
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
        background: { color: "#0d141c" },
        textColor: "#d4e0ea",
        attributionLogo: false,
        panes: {
          separatorColor: "#233646",
          separatorHoverColor: "#5e7d99",
          enableResize: true,
        },
      },
      grid: {
        vertLines: { color: "rgba(40, 58, 73, 0.55)", visible: true },
        horzLines: { color: "rgba(40, 58, 73, 0.55)", visible: true },
      },
      crosshair: {
        mode: crosshairMode,
        vertLine: { color: "#7d91a2", width: 1, labelBackgroundColor: "#314759" },
        horzLine: { color: "#7d91a2", width: 1, labelBackgroundColor: "#314759" },
      },
      rightPriceScale: {
        borderColor: "#2b4052",
        scaleMargins: { top: 0.04, bottom: 0.08 },
      },
      leftPriceScale: { visible: false },
      timeScale: {
        borderColor: "#2b4052",
        timeVisible: payload.hasIntraday,
        secondsVisible: false,
        rightOffset: 2,
        barSpacing: payload.hasIntraday ? 8.8 : 12.5,
        minBarSpacing: 1.15,
        lockVisibleTimeRangeOnResize: true,
        allowBoldLabels: true,
        fixLeftEdge: true,
        fixRightEdge: true,
        rightBarStaysOnScroll: true,
        shiftVisibleRangeOnNewBar: false,
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
          lineColor: "#57a6ff",
          lineWidth: 2,
          topColor: "rgba(87, 166, 255, 0.28)",
          bottomColor: "rgba(87, 166, 255, 0.01)",
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerRadius: 4,
        }, 0)
      : addSeriesCompat(chart, "candlestick", {
          upColor: "#ff5f73",
          downColor: "#16c39a",
          borderUpColor: "#ff6d7f",
          borderDownColor: "#1ec8a1",
          wickUpColor: "#ff8896",
          wickDownColor: "#49d1b0",
          priceLineVisible: false,
          lastValueVisible: false,
        }, 0);
    mainSeries.setData(
      payload.mainSeries === "line"
        ? payload.candles.map((bar) => ({ time: bar.time, value: bar.close }))
        : payload.candles
    );
    if (typeof mainSeries.priceScale === "function") {
      mainSeries.priceScale().applyOptions({
        autoScale: true,
        scaleMargins: { top: 0.06, bottom: 0.10 },
      });
    }
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
        lineWidth: overlay.name.includes("VWAP") ? 2.2 : (overlay.name.includes("MA") ? 1.55 : 1.7),
        lineStyle: lineStyle(overlay.style || "solid"),
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        priceLineVisible: false,
        autoscaleInfoProvider: () => null,
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
      if (panes[0] && typeof panes[0].setHeight === "function") panes[0].setHeight(496);
      if (panes[1] && typeof panes[1].setHeight === "function") panes[1].setHeight(112);
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
        applyInitialView();
      }
    });

    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(shellNode);

    const totalBars = payload.candles.length;
    const minLogical = -0.35;
    const maxLogical = totalBars - 1 + 2.25;
    const minVisibleBars = payload.hasIntraday ? 36 : 40;
    const maxVisibleBars = Math.max(totalBars + 2.6, minVisibleBars + 2);
    let isAdjustingRange = false;

    function normaliseRange(range) {
      if (!range || !Number.isFinite(range.from) || !Number.isFinite(range.to)) return null;
      let span = Math.max(range.to - range.from, minVisibleBars);
      span = Math.min(span, maxVisibleBars);
      let from = range.from;
      let to = from + span;
      if (from < minLogical) {
        from = minLogical;
        to = from + span;
      }
      if (to > maxLogical) {
        to = maxLogical;
        from = to - span;
      }
      if (from < minLogical) {
        from = minLogical;
      }
      return { from, to };
    }

    function setVisibleRangeSafely(range) {
      if (!range) return;
      const timeScale = chart.timeScale();
      if (!timeScale || typeof timeScale.setVisibleLogicalRange !== "function") return;
      isAdjustingRange = true;
      timeScale.setVisibleLogicalRange(range);
      window.requestAnimationFrame(() => {
        isAdjustingRange = false;
      });
    }

    function applyInitialView() {
      const timeScale = chart.timeScale();
      if (!timeScale || typeof timeScale.setVisibleLogicalRange !== "function") return;
      const desiredBars = payload.hasIntraday
        ? Math.min(Math.max(Math.round(totalBars * 0.10), 120), 300)
        : Math.min(Math.max(Math.round(totalBars * 0.55), 90), 260);
      setVisibleRangeSafely(normaliseRange({ from: maxLogical - desiredBars, to: maxLogical }));
    }

    if (typeof chart.timeScale().subscribeVisibleLogicalRangeChange === "function") {
      chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (isAdjustingRange) return;
        const nextRange = normaliseRange(range);
        if (!nextRange || !range) return;
        const changed =
          Math.abs(nextRange.from - range.from) > 0.001 ||
          Math.abs(nextRange.to - range.to) > 0.001;
        if (changed) {
          setVisibleRangeSafely(nextRange);
        }
      });
    }

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
      const maxSpan = Math.max(Math.min(payload.candles.length + 2.6, 680), minSpan * 2);
      nextSpan = Math.max(minSpan, Math.min(maxSpan, nextSpan));

      const leftRatio = (anchorLogical - range.from) / span;
      const rightRatio = (range.to - anchorLogical) / span;
      let nextFrom = anchorLogical - nextSpan * leftRatio;
      let nextTo = anchorLogical + nextSpan * rightRatio;

      const lowerBound = minLogical;
      const upperBound = maxLogical;
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

    window.requestAnimationFrame(() => {
      applyInitialView();
      window.setTimeout(applyInitialView, 80);
    });

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
        height=816,
    )


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
                        ("MA10", "ma10", "#8fd27f"),
                        ("VWAP", "vwap", "#57a6ff"),
                        ("开盘区间高点 / OR High", "opening_range_high", "#ff9f40"),
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
                        ("MA20", "ma20", "#6ea8ff"),
                        ("MA60", "ma60", "#4cb26e"),
                    ],
                    order_markers=context.buy_sell_points,
                    price_line_label="日线现价 / Last",
                    marker_alignment="daily",
                    lower_panel=LOWER_PANEL_OPTIONS[daily_lower_label],
                )
    st.markdown("</div>", unsafe_allow_html=True)


def render_live_monitor(settings) -> None:
    _inject_terminal_css()
    st.subheader("高级实时监控 / Advanced Live Monitor")
    def _current_settings():
        return load_settings()

    live_settings = _current_settings()

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
            value=int(st.session_state.get("live_auto_refresh_seconds", 30) or 30),
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

    env_label = "真实盘 / REAL" if live_settings.futu_trd_env == "REAL" else "模拟盘 / SIMULATE"
    st.caption(f"当前环境: {env_label}。页面已经收成总览、图表、订单三块；默认先给你看最必要的数据。")
    _render_app_controls(live_settings)

    default_end = date.today()
    default_start = _stock_account_start_date(default_end)
    default_base_assets = float(st.session_state.get("live_base_assets", live_settings.initial_capital))
    with st.expander("监控设置 / Filters & Base", expanded=False):
        st.caption("这里控制账户收益起点、订单筛选和净值基准，不会改变自动运行。默认账户起点是 2026-04-01。")
        control_cols = st.columns([1, 1, 1, 1])
        with control_cols[0]:
            start = st.date_input(
                "账户/订单开始日期 / Account Start",
                value=default_start,
                key="live_account_start",
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
        st.session_state.pop("live_payload", None)
        st.session_state.pop("live_payload_key", None)
        gc.collect()
        st.rerun()

    payload_key = f"{start.isoformat()}::{end.isoformat()}"

    def _load_live_payload() -> dict[str, object]:
        # 临时计时：股票页首次渲染约 55 秒，需要定位耗时落在哪一步。
        # 设 JQ_PROFILE_LIVE=1 时把各阶段耗时写到 runtime/live_payload_timing.json。
        import time as _t
        _prof_on = (os.environ.get("JQ_PROFILE_LIVE") or "").strip() in {"1", "true", "yes"}
        _marks: list[tuple[str, float]] = []
        _t0 = _t.perf_counter()

        def _mark(label: str) -> None:
            if _prof_on:
                _marks.append((label, round(_t.perf_counter() - _t0, 2)))

        current_settings = _current_settings()
        cached_payload = st.session_state.get("live_payload")
        _mark("准备设置")
        with FutuPaperTrader(current_settings) as trader:
            _mark("连接 OpenD")
            # resolve_trade_account can fail if FutuOpenD connection drops mid-session.
            # Fall back to the cached acc_id so the rest of _safe_live_fetch calls
            # can still return stale data instead of letting the whole load explode.
            try:
                acc_id = trader.resolve_trade_account()
            except FutuTransientError:
                cached_acc_id = _cached_live_value(cached_payload, "acc_id", None)
                if cached_acc_id is None:
                    raise  # no cache yet — let the fragment's error handler show the warning
                acc_id = cached_acc_id
            _mark("解析账户")
            account = _safe_live_fetch(cached_payload, "account", lambda: trader.get_account_info(acc_id), pd.Series(dtype=object))
            _mark("账户信息")
            positions = _safe_live_fetch(cached_payload, "positions", lambda: trader.get_positions(acc_id), pd.DataFrame())
            _mark("持仓")
            open_orders = _safe_live_fetch(cached_payload, "open_orders", lambda: trader.get_open_orders(acc_id), pd.DataFrame())
            _mark("挂单")
            order_history = _safe_live_fetch(
                cached_payload,
                "order_history",
                lambda: trader.get_order_history(acc_id, start.isoformat(), end.isoformat()),
                pd.DataFrame(),
            )
            _mark("历史委托")
            market_now = datetime.now(ZoneInfo(current_settings.auto_trader_market_timezone))
            split_state = load_strategy_split_state()
            held_symbols = set(positions["code"].tolist()) if not positions.empty else set()
            baseline_weight, fusion_weight, ofim_weight, cascade_weight, reserve_weight = stack_allocations(current_settings)
            fusion_settings = effective_fusion_settings(current_settings)

            baseline_targets: dict[str, float] = {}
            if baseline_sleeve_enabled(current_settings) and baseline_weight > 0:
                baseline_start = max(
                    pd.Timestamp(current_settings.start_date).date(),
                    (market_now.date() - timedelta(days=max(730, current_settings.lookback_months * 45))),
                ).isoformat()
                _mark("准备baseline")
                baseline_prices = fetch_futu_daily_closes(
                    trader,
                    current_settings.symbols,
                    start=baseline_start,
                )
                baseline_targets = scaled_baseline_target_weights(
                    baseline_prices,
                    current_settings,
                    reference_date=market_now.date(),
                )

            fusion_positions = positions
            fusion_symbols = set(fusion_settings.fusion_universe)
            if not fusion_positions.empty and fusion_symbols:
                fusion_positions = fusion_positions[fusion_positions["code"].isin(fusion_symbols)].copy()
            fusion_held_symbols = set(fusion_positions["code"].tolist()) if not fusion_positions.empty else set()
            fusion_plan = _safe_live_fetch(
                cached_payload,
                "fusion_plan",
                lambda: FusionIntradayStrategy(fusion_settings).generate_plan(trader, fusion_held_symbols),
                _empty_fusion_plan(fusion_settings),
            )
            fusion_scaled_targets = {
                code: round(weight * fusion_weight, 6)
                for code, weight in fusion_plan.target_weights.items()
            }

            ofim_plan = None
            ofim_scaled_targets: dict[str, float] = {}
            if ofim_weight > 0:
                ofim_plan = _safe_live_fetch(
                    cached_payload,
                    "ofim_plan",
                    lambda: OfimIntradayStrategy(fusion_settings).generate_plan(trader, fusion_held_symbols),
                    _empty_ofim_plan(fusion_settings),
                )
                ofim_scaled_targets = {
                    code: round(weight * ofim_weight, 6)
                    for code, weight in ofim_plan.target_weights.items()
                }

            cascade_plan = None
            cascade_scaled_targets: dict[str, float] = {}
            if cascade_weight > 0:
                cascade_plan = _safe_live_fetch(
                    cached_payload,
                    "cascade_plan",
                    lambda: generate_live_cascade_plan(current_settings, trader),
                    _empty_cascade_plan(),
                )
                cascade_scaled_targets = {
                    code: round(weight * cascade_weight, 6)
                    for code, weight in cascade_plan.target_weights.items()
                }

            combined_targets = stack_target_weights(
                baseline_targets,
                fusion_scaled_targets,
                ofim_scaled_targets,
                cascade_scaled_targets,
            )
            filled_order_history = order_history[
                pd.to_numeric(order_history.get("dealt_qty"), errors="coerce").fillna(0) > 0
            ].copy() if not order_history.empty else order_history
            filled_cost_view = with_trade_costs(
                filled_order_history,
                current_settings,
                side_col="trd_side",
                qty_col="dealt_qty",
                price_col="dealt_avg_price",
                timestamp_col="updated_time",
            ) if not filled_order_history.empty else filled_order_history
            estimated_realized = estimate_realized_from_fills(filled_order_history, current_settings)
            estimated_fee_total = trade_log_total_fees(filled_cost_view)
            estimated_unrealized = _calculate_unrealized_from_positions(positions)
            stock_ledger_epoch = load_stock_ledger_epoch()
            stock_ledger_projection = build_stock_fills_ledger(STOCK_FILLS_FILE, epoch_path=STOCK_LEDGER_EPOCH_FILE)
            stock_ledger_v2 = build_stock_double_entry_ledger(STOCK_FILLS_FILE, epoch_path=STOCK_LEDGER_EPOCH_FILE)
            stock_ledger_reconciliation = reconcile_stock_ledger(
                stock_ledger_v2,
                positions=positions,
                account=account,
                epoch=stock_ledger_epoch,
            )
            broker_realized = _optional_float(account.get("realized_pl"))
            broker_unrealized = _optional_float(account.get("unrealized_pl"))
            experiment_history = pd.DataFrame()
            experiment_filled_cost_view = pd.DataFrame()
            if split_state.get("reset_at"):
                split_start = pd.Timestamp(split_state["reset_at"], tz="UTC").tz_convert(current_settings.auto_trader_market_timezone).date().isoformat()
                experiment_history = _safe_live_fetch(
                    cached_payload,
                    "experiment_history",
                    lambda: trader.get_order_history(acc_id, split_start, market_now.date().isoformat()),
                    pd.DataFrame(),
                )
                experiment_filled = experiment_history[
                    pd.to_numeric(experiment_history.get("dealt_qty"), errors="coerce").fillna(0) > 0
                ].copy() if not experiment_history.empty else experiment_history
                experiment_filled_cost_view = with_trade_costs(
                    experiment_filled,
                    current_settings,
                    side_col="trd_side",
                    qty_col="dealt_qty",
                    price_col="dealt_avg_price",
                    timestamp_col="updated_time",
                ) if not experiment_filled.empty else experiment_filled
                experiment_filled_cost_view = filter_fills_since_reset(experiment_filled_cost_view, split_state, current_settings)
            position_view = _positions_view(positions)
            watchlist_view = _watchlist_view(fusion_plan, fusion_scaled_targets)
            feature_map = {feature.code: feature for feature in fusion_plan.features}
            all_symbols = list(
                dict.fromkeys(
                    [
                        *(positions["code"].tolist() if not positions.empty else []),
                        *combined_targets.keys(),
                        *(watchlist_view["标的 / Symbol"].tolist() if not watchlist_view.empty else []),
                        current_settings.fusion_benchmark,
                    ]
                )
            )
            _mark("各 sleeve 计算")
            snapshots_frame = _safe_live_fetch(
                cached_payload,
                "snapshots_frame",
                lambda: trader.get_snapshots(all_symbols) if all_symbols else pd.DataFrame(),
                pd.DataFrame(),
            )
            _mark("行情快照")
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
            _mark("组装完成")
            if _prof_on:
                try:
                    import json as _json
                    _out = Path("runtime") / "live_payload_timing.json"
                    _out.parent.mkdir(parents=True, exist_ok=True)
                    _steps = []
                    _prev = 0.0
                    for _lbl, _at in _marks:
                        _steps.append({"阶段": _lbl, "累计秒": _at, "本步秒": round(_at - _prev, 2)})
                        _prev = _at
                    _out.write_text(_json.dumps(
                        {"总耗时": _marks[-1][1] if _marks else 0, "分步": _steps},
                        ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass
            return {
                "payload_key": payload_key,
                "acc_id": acc_id,
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
                "ofim_plan": ofim_plan,
                "ofim_scaled_targets": ofim_scaled_targets,
                "cascade_plan": cascade_plan,
                "cascade_scaled_targets": cascade_scaled_targets,
                "combined_targets": combined_targets,
                "stack_allocations": {
                    "baseline": baseline_weight,
                    "fusion": fusion_weight,
                    "ofim": ofim_weight,
                    "cascade": cascade_weight,
                    "reserve": reserve_weight,
                },
                "settings_snapshot": current_settings,
                "feature_map": feature_map,
                "all_symbols": all_symbols,
                "snapshots_frame": snapshots_frame,
                "terminal_watchlist": pd.DataFrame(watchlist_rows),
                "estimated_realized": estimated_realized,
                "estimated_fee_total": estimated_fee_total,
                "estimated_unrealized": estimated_unrealized,
                "stock_ledger_projection": stock_ledger_projection,
                "stock_ledger_epoch": stock_ledger_epoch,
                "stock_ledger_v2": stock_ledger_v2,
                "stock_ledger_reconciliation": stock_ledger_reconciliation,
                "broker_realized": broker_realized,
                "broker_unrealized": broker_unrealized,
                "split_state": split_state,
                "experiment_history": experiment_history,
                "experiment_filled_cost_view": experiment_filled_cost_view,
            }


    def _get_live_payload(*, force_fetch: bool = False) -> dict[str, object]:
        payload = st.session_state.get("live_payload")
        if not force_fetch and isinstance(payload, dict) and payload.get("payload_key") == payload_key:
            return payload
        if isinstance(payload, dict) and payload.get("payload_key") != payload_key:
            st.session_state.pop("live_payload", None)
            st.session_state.pop("live_payload_key", None)
            gc.collect()
        payload = _load_live_payload()
        st.session_state["live_payload"] = payload
        st.session_state["live_payload_key"] = payload_key
        return payload

    @st.fragment(run_every=run_every)
    def _render_status_and_metrics() -> None:
        current_settings = _current_settings()
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
        recent_errors = _recent_error_frame(days=3)
        if not recent_errors.empty and "ts" in recent_errors.columns:
            recent_ts = pd.to_datetime(recent_errors["ts"], errors="coerce", utc=True)
            last_hour_mask = recent_ts >= (pd.Timestamp.utcnow() - pd.Timedelta(hours=1))
            last_hour_count = int(last_hour_mask.sum())
            if last_hour_count:
                latest_error_ts = recent_ts[last_hour_mask].max()
                latest_age_min = (
                    (pd.Timestamp.utcnow() - latest_error_ts).total_seconds() / 60
                    if pd.notna(latest_error_ts)
                    else None
                )
                auto_payload = {}
                try:
                    auto_payload = json.loads(AUTO_TRADER_STATUS_FILE.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    auto_payload = {}
                current_action = str(auto_payload.get("action", ""))
                current_detail = auto_payload.get("detail", "")
                age_text = f"，最近一次约 {latest_age_min:.0f} 分钟前" if latest_age_min is not None else ""
                error_text = f"最近 60 分钟接口/运行错误 {last_hour_count} 条{age_text}。"
                banner_level = _runtime_error_banner_level(current_action, current_detail, latest_age_min)
                if banner_level == "error":
                    st.error(error_text + " 当前自动交易状态仍是 error。")
                elif banner_level == "warning":
                    st.warning(error_text + " 系统正在恢复或重试。")
                else:
                    st.info(error_text + " 当前自动交易状态已恢复正常。")

        try:
            payload = _get_live_payload(force_fetch=True)
        except FutuTransientError as exc:
            cached_payload = st.session_state.get("live_payload")
            if isinstance(cached_payload, dict) and cached_payload.get("payload_key") == payload_key:
                st.warning(f"实时账户数据刷新失败，已沿用上一帧 / Using cached data: {exc}")
                payload = cached_payload
            else:
                st.warning(f"实时账户数据暂时刷新失败 / Live data refresh failed: {exc}")
                return
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
        stock_ledger_projection = payload["stock_ledger_projection"]
        stock_ledger_epoch = payload["stock_ledger_epoch"]
        stock_ledger_v2 = payload["stock_ledger_v2"]
        stock_ledger_reconciliation = payload["stock_ledger_reconciliation"]
        broker_realized = payload["broker_realized"]
        broker_unrealized = payload["broker_unrealized"]
        open_orders = payload["open_orders"]
        fusion_plan = payload["fusion_plan"]
        baseline_targets = payload["baseline_targets"]
        fusion_scaled_targets = payload["fusion_scaled_targets"]
        ofim_plan = payload["ofim_plan"]
        ofim_scaled_targets = payload["ofim_scaled_targets"]
        cascade_plan = payload["cascade_plan"]
        cascade_scaled_targets = payload["cascade_scaled_targets"]
        split_state = payload["split_state"]
        _cur_bw, _cur_fw, _cur_ow, _cur_cw, _cur_rw = stack_allocations(current_settings)
        active_plug = active_stack_strategy(current_settings)
        sleeve_allocations: dict[str, float] = {
            "baseline": _cur_bw,
            "fusion":   _cur_fw,
            "ofim":     _cur_ow,
            "cascade":  _cur_cw,
            "reserve":  _cur_rw,
        }
        doctor_report = run_stock_system_doctor(current_settings, reconciliation=stock_ledger_reconciliation)
        doctor_findings = [item for item in doctor_report.findings if item.status in {"warn", "fail"}]
        # 演示模式下体检必然失败：它查的是本机运行时文件（成交流水、账本 epoch、
        # 自动交易状态），干净 clone 里一个都没有。那不是故障，是「还没跑起来」，
        # 报红只会让第一次打开的人以为程序坏了。
        from taa_futu.demo_gateway import demo_enabled
        if demo_enabled():
            st.caption("股票系统 Doctor: 演示模式下跳过——它检查的是本机运行时文件，"
                       "干净安装时本来就没有。")
        elif doctor_report.status == "fail":
            st.error("股票系统 Doctor: 有关键胶水断点，需要先处理。")
        elif doctor_report.status == "warn":
            st.warning("股票系统 Doctor: 有运行契约不一致，建议处理后再解读策略收益。")
        else:
            st.success("股票系统 Doctor: 核心运行契约一致。")
        with st.expander("股票系统 Doctor / Stock System Doctor", expanded=doctor_report.status != "ok"):
            st.dataframe(pd.DataFrame(doctor_report.to_dict()["findings"]), use_container_width=True, hide_index=True)
            fix_commands = [item.fix_command for item in doctor_findings if item.fix_command]
            if fix_commands:
                st.code("\n".join(dict.fromkeys(fix_commands)), language="bash")
        experiment_filled_cost_view = payload["experiment_filled_cost_view"]
        total_assets = _safe_float(account.get("total_assets"))
        base_assets_value = float(st.session_state.get("live_base_assets", base_assets))
        selected_range_trade_count = len(filled_order_history)
        performance_summary = _stock_performance_summary(
            total_assets=total_assets,
            base_assets=base_assets_value,
            stock_ledger_epoch=stock_ledger_epoch,
            stock_ledger_v2=stock_ledger_v2,
            stock_ledger_projection=stock_ledger_projection,
            estimated_fee_total=estimated_fee_total,
            estimated_realized=estimated_realized,
            estimated_unrealized=estimated_unrealized,
            broker_realized=broker_realized,
            broker_unrealized=broker_unrealized,
            selected_range_trade_count=selected_range_trade_count,
        )
        net_change = performance_summary.net_change
        net_change_pct = performance_summary.net_change_pct
        displayed_unrealized = performance_summary.unrealized
        ledger_has_epoch = performance_summary.ledger_has_epoch
        epoch_start_value = performance_summary.epoch_start_value
        ledger_realized = performance_summary.ledger_realized
        ledger_since_epoch = performance_summary.ledger_since_epoch
        ledger_unrealized = performance_summary.ledger_unrealized
        if active_plug is not None:
            plug_titles = {
                "baseline": "Baseline",
                "fusion": "Fusion",
                "ofim": "OFIM",
                "cascade": "Claude/Cascade",
            }
            st.info(
                "当前运行模式 / Current Mode: "
                f"独占插头 / Exclusive Plug -> {plug_titles.get(active_plug, active_plug)}。"
                " 自动盘只跑这一套策略，其余策略不参与组合执行。"
            )
        else:
            st.caption("当前运行模式 / Current Mode: 自定义混合组合。多套策略会按权重一起参与执行。")
        metrics_top = st.columns(4)
        metrics_top[0].metric("总资产 / Assets", _format_currency(total_assets))
        metrics_top[1].metric(
            "账户总盈亏 / Account Total PnL",
            _format_currency(net_change),
            f"{net_change_pct:+.2%}" if net_change_pct is not None else None,
        )
        metrics_top[2].metric("现金 / Cash", _format_currency(account.get("cash")))
        metrics_top[3].metric("持仓市值 / Market Value", _format_currency(account.get("market_val")))

        metrics_bottom = st.columns(4)
        metrics_bottom[0].metric("当前浮盈 / Unrealized", _format_currency(displayed_unrealized))
        metrics_bottom[1].metric(
            "毛盈亏(费前) / Gross PnL",
            _format_currency(performance_summary.gross_change),
            f"{performance_summary.gross_change_pct:+.2%}" if performance_summary.gross_change_pct is not None else None,
        )
        metrics_bottom[2].metric("交易成本 / Fees", _format_currency(performance_summary.fee_total))
        metrics_bottom[3].metric("成交笔数 / Trades", str(performance_summary.trade_count))
        if performance_summary.ledger_has_epoch:
            st.caption(
                "账户总盈亏、交易成本和成交笔数使用上方账户开始日期 / Account Start 口径；"
                "工程账本 Epoch 只用于下方审计。"
                f"当前 Base Assets: {_format_currency(performance_summary.base_assets)}；"
                f"Epoch 起点资产: {_format_currency(epoch_start_value)}。"
            )
        else:
            st.caption(
                "账户总盈亏、交易成本和成交笔数使用上方账户开始日期 / Account Start 口径。"
                "设置工程账本 Epoch 后，下方审计区会单独显示 Epoch 后收益。"
            )

        compact_summary = st.columns([1.15, 1.0])
        if active_plug is not None:
            compact_summary[0].info(f"当前分配 / Current Split: {stack_label(current_settings)}")
        else:
            compact_summary[0].info(
                "当前分配 / Current Split: "
                f"Baseline {float(sleeve_allocations['baseline']):.0%} + Fusion {float(sleeve_allocations['fusion']):.0%} + "
                f"OFIM {float(sleeve_allocations['ofim']):.0%} + "
                f"Claude/Cascade {float(sleeve_allocations['cascade']):.0%}"
            )
        compact_summary[1].info(f"目标仓位 Top / Top Targets: {_top_target_summary(combined_targets)}")

        current_breakdown = current_strategy_holdings(
            settings=current_settings,
            positions=positions,
            total_assets=total_assets,
            combined_targets=combined_targets,
            baseline_targets=baseline_targets,
            fusion_targets=fusion_scaled_targets,
            ofim_targets=ofim_scaled_targets,
            cascade_targets=cascade_scaled_targets,
        )
        period_breakdown = period_strategy_performance(
            filled_cost_view=experiment_filled_cost_view,
            settings=current_settings,
            split_state=split_state,
        )
        strategy_ledger, overlap_breakdown = build_strategy_ledger(
            settings=current_settings,
            split_state=split_state,
            total_assets=total_assets,
            current_holdings=current_breakdown,
            period_performance=period_breakdown,
        )

        st.markdown("**四策略独立账本 / Four-Strategy Ledger**")
        reset_caption = ""
        if split_state.get("reset_at"):
            reset_caption = f"本次独立记账起点 / Reset At: {split_state['reset_at']}"
        st.caption(f"这里看四套策略（Baseline / Fusion / OFIM / Cascade）各自当前允许操作总现金、市值、预算余量、收益、成本和成交笔数。{reset_caption}")
        st.info(
            f"账户余留现金 / Account Remaining Cash: {_format_currency(account.get('cash'))}。"
            " 这张表里的“当前允许操作总现金”会直接跟着控制台当前权重变化。"
        )
        if split_state and not split_state_matches_current(split_state, settings):
            reset_weights = split_state_weight_map(split_state)
            current_mix_text = (
                stack_label(current_settings)
                if active_plug is not None
                else (
                    f"Baseline {float(sleeve_allocations['baseline']):.0%} / Fusion {float(sleeve_allocations['fusion']):.0%} / "
                    f"OFIM {float(sleeve_allocations['ofim']):.0%} / Claude {float(sleeve_allocations['cascade']):.0%}"
                )
            )
            st.caption(
                "说明：预算列按你现在的控制台配置实时显示；"
                " 收益列仍按最近一次清仓重置后的起点累计。"
                f" 上次重置时是 Baseline {reset_weights['Baseline']:.0%} / Fusion {reset_weights['Fusion']:.0%} / "
                f"OFIM {reset_weights['OFIM']:.0%} / Claude {reset_weights['Claude/Cascade']:.0%}；"
                f" 现在配置是 {current_mix_text}。"
            )
            st.warning(
                "四策略分账起点和当前权重不一致。请用下方统一起点按钮重设股票系统起点，"
                "否则单个策略净表现只能作为历史归因参考，不能当成当前收益。"
            )
        st.dataframe(
            strategy_ledger,
            use_container_width=True,
            hide_index=True,
            column_config={
                "当前目标 / Targets": st.column_config.TextColumn(width="large"),
            },
        )
        reset_required = (
            strategy_ledger["账本状态 / Ledger Status"].astype(str).str.contains("Reset Required", regex=False).any()
            if "账本状态 / Ledger Status" in strategy_ledger.columns
            else False
        )
        if reset_required:
            st.warning(
                "有策略是在上次四策略分账重置之后才启用的，净表现没有合法起点，所以已置空。"
                " 请在账本诊断区把当前账户设为新的股票账本起点，之后再比较各策略收益。"
            )
        strategy_net_total = (
            pd.to_numeric(strategy_ledger.get("净表现 / Net Performance"), errors="coerce").fillna(0.0).sum()
            if not strategy_ledger.empty
            else 0.0
        )
        unattributed_realized = (
            pd.to_numeric(overlap_breakdown.get("区间已实现 / Realized"), errors="coerce").fillna(0.0).sum()
            if not overlap_breakdown.empty
            else 0.0
        )
        split_base_assets = _safe_float(split_state.get("base_total_assets"), base_assets_value) if isinstance(split_state, dict) else base_assets_value
        account_reset_net = total_assets - split_base_assets if split_base_assets > 0 else net_change
        strategy_reconciled_net = strategy_net_total + unattributed_realized
        strategy_reconcile_diff = strategy_reconciled_net - account_reset_net
        reconcile_text = (
            f"策略净表现合计 / Strategy Sum: {_format_currency(strategy_net_total)}；"
            f"未归因 realized / Unattributed: {_format_currency(unattributed_realized)}；"
            f"账户 reset 后净变化 / Account Since Reset: {_format_currency(account_reset_net)}；"
            f"差额 / Diff: {_format_currency(strategy_reconcile_diff)}。"
        )
        if abs(strategy_reconcile_diff) > max(25.0, abs(account_reset_net) * 0.02):
            st.warning(
                reconcile_text
                + " 这表示还有未归因成交、reset 前持仓、经纪商历史不足或数据同步差异；不要把上表单独当成最终收益。"
            )
        else:
            st.caption(reconcile_text)
        if not overlap_breakdown.empty:
            st.caption("另有部分成交属于重叠标的 / Shared-Overlap。为了不误导，这些单子没有硬塞给某一套策略。")
            st.dataframe(overlap_breakdown, use_container_width=True, hide_index=True)

        st.markdown("**股票事件账本 / Stock Event Ledger**")
        if not ledger_has_epoch:
            st.info(
                "这块是本地股票审计账本，不会下单。当前还没有设置账本 Epoch，所以本地账本 expected=0，"
                "券商实际持仓会被看成初始化差异。展开下面的“账本诊断与危险操作”，点击重开 Epoch 后，"
                "它会把当前账户快照作为账本起点，之后才开始做有效对账。"
            )
        else:
            st.caption(
                "口径说明：上方“账户总盈亏”是 `总资产 - 参考初始资产`；股票事件账本是 "
                f"`总资产 - Epoch 起点资产`。当前参考初始资产为 {_format_currency(base_assets_value)}，"
                f"Epoch 起点资产为 {_format_currency(epoch_start_value)}，两者是两套不同口径。"
            )
        ledger_cols = st.columns(4)
        ledger_cols[0].metric("Epoch / 起点", str((stock_ledger_epoch or {}).get("ts", "未设置 / Not Set"))[:19])
        ledger_cols[1].metric("起点资产 / Start Value", _format_currency(epoch_start_value) if ledger_has_epoch else "未设置 / Not Set")
        ledger_cols[2].metric("净已实现 / Net Realized", _format_currency(ledger_realized))
        ledger_cols[3].metric("审计哈希 / Audit", str(getattr(stock_ledger_projection, "audit_hash", "")) or "-")
        ledger_cols_2 = st.columns(4)
        ledger_cols_2[0].metric("成交数 / Trades", str(getattr(stock_ledger_v2, "trade_count", getattr(stock_ledger_projection, "trade_count", 0))))
        ledger_cols_2[1].metric("费用 / Fees", _format_currency(getattr(stock_ledger_v2, "fees_paid", getattr(stock_ledger_projection, "fees_paid", 0.0))))
        ledger_cols_2[2].metric("未实现/其他 / Residual", _format_currency(ledger_unrealized) if ledger_has_epoch else "待初始化 / Pending")
        ledger_cols_2[3].metric(
            "Epoch后总盈亏 / Since Epoch PnL",
            (
                _format_currency(ledger_since_epoch)
                if ledger_since_epoch is not None
                else "待初始化 / Pending"
            ),
        )
        st.markdown("**工程级双分录账本 / Double-Entry Audit Ledger**")
        audit_cols = st.columns(4)
        audit_cols[0].metric("链状态 / Chain", "OK" if getattr(stock_ledger_v2, "chain_valid", False) else "BREAK")
        audit_cols[1].metric("净已实现 / Net Realized", _format_currency(getattr(stock_ledger_v2, "net_realized_pnl", 0.0)))
        audit_cols[2].metric("分录数 / Entries", str(len(getattr(stock_ledger_v2, "entries", ()))))
        audit_cols[3].metric("Journal Hash", str(getattr(stock_ledger_v2, "journal_hash", ""))[:16] or "-")
        if not ledger_has_epoch:
            st.info("券商对账 / Broker Reconciliation: 尚未启用。请先设置股票账本 Epoch。")
        elif getattr(stock_ledger_reconciliation, "ok", False):
            st.success("券商对账 / Broker Reconciliation: OK")
        else:
            breaks = getattr(stock_ledger_reconciliation, "breaks", ())
            st.warning(f"券商对账 / Broker Reconciliation: {len(breaks)} break(s)")
            if breaks:
                st.dataframe(pd.DataFrame([break_item.__dict__ for break_item in breaks]), use_container_width=True, hide_index=True)
        with st.expander("账本诊断与危险操作 / Ledger Diagnostics & Dangerous", expanded=False):
            st.caption(
                f"Fill log: {STOCK_FILLS_FILE} | Epoch: {STOCK_LEDGER_EPOCH_FILE}. "
                "这里不会修改券商账户，只会把本地股票账本的统计起点改成当前时间。"
            )
            if getattr(stock_ledger_projection, "warnings", ()):
                for warning in stock_ledger_projection.warnings:
                    st.warning(warning)
            if getattr(stock_ledger_v2, "warnings", ()):
                for warning in stock_ledger_v2.warnings:
                    st.warning(warning)
            if st.button("把当前账户设为统一股票系统起点 / Set Current Account As Stock System Epoch", key="reset-stock-ledger-epoch"):
                position_snapshot = []
                if not positions.empty:
                    for row in positions.to_dict("records"):
                        item = {
                            "code": row.get("code"),
                            "qty": _safe_float(row.get("qty")),
                            "can_sell_qty": _safe_float(row.get("can_sell_qty")),
                            "market_val": _safe_float(row.get("market_val")),
                        }
                        for cost_col in ("cost_price", "average_cost", "nominal_price"):
                            if cost_col in row:
                                item[cost_col] = _safe_float(row.get(cost_col))
                        if item.get("code"):
                            position_snapshot.append(item)
                snapshot = {
                    "total_assets": total_assets,
                    "cash": _safe_float(account.get("cash")),
                    "market_val": _safe_float(account.get("market_val")),
                    "position_count": len(position_snapshot),
                    "positions": position_snapshot,
                }
                write_stock_ledger_epoch(reason="dashboard_manual_reset", account_snapshot=snapshot)
                write_strategy_split_state(settings=current_settings, total_assets=total_assets, reason="dashboard_manual_reset")
                st.success("股票事件账本和四策略分账起点都已设置为当前账户快照。")
                st.rerun()

        st.caption(
            "一句话理解：总盈亏看整个账户现在比初始资金多了多少；现金和持仓市值一起组成总资产；"
            "当前浮盈/其他是账本总盈亏扣掉已实现后的残差；总览交易成本和成交数来自工程账本。"
        )
        st.caption(f"当前未完成订单 {len(open_orders)} 笔。所选日期的 broker 订单统计已经挪到订单页里，不再占首页主位置。")

        if not filled_order_history.empty:
            st.info(
                f"所选日期范围内已有 {selected_range_trade_count} 笔 broker 已成交订单。"
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
        ofim_scaled_targets = payload["ofim_scaled_targets"]
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
        current_settings = payload.get("settings_snapshot", settings)

        selected_symbol = st.session_state.get("selected_symbol") or (all_symbols[0] if all_symbols else settings.fusion_benchmark)
        if selected_symbol not in all_symbols:
            all_symbols = [selected_symbol, *all_symbols]

        chart_tab, overview_tab, orders_tab, learning_tab, ops_tab, crypto_tab = st.tabs(
            ["K线 / Chart", "总览 / Overview", "订单 / Orders", "学习实验室 / Learning", "运行日志 / Ops", "🔗 加密策略 / Crypto"]
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
            our_weight = float(sleeve_allocations["baseline"]) + float(sleeve_allocations["fusion"]) + float(sleeve_allocations["ofim"])
            our_targets = stack_target_weights(baseline_targets, fusion_scaled_targets, ofim_scaled_targets)
            _bw = float(sleeve_allocations['baseline'])
            _fw = float(sleeve_allocations['fusion'])
            _ow = float(sleeve_allocations['ofim'])
            _cw = float(sleeve_allocations['cascade'])
            _our_parts = []
            if _bw > 0: _our_parts.append(f"Baseline {_bw:.0%}")
            if _fw > 0: _our_parts.append(f"Fusion {_fw:.0%}")
            if _ow > 0: _our_parts.append(f"OFIM {_ow:.0%}")
            _our_breakdown = " + ".join(_our_parts) if _our_parts else "无 / none"
            st.info(
                "当前比较模式 / Current comparison mode: "
                f"我的策略组 {our_weight:.0%} ({_our_breakdown}) "
                f"vs Claude/Cascade {_cw:.0%}。"
            )
            strategy_cols = st.columns(2)
            _extra_parts = [_our_breakdown]
            if _fw > 0:
                _extra_parts.append(f"Fusion 基准 {float(getattr(fusion_plan, 'benchmark_score', 0.0)):+.4f}")
            strategy_cols[0].info(
                _strategy_live_summary(
                    "我的策略组 / Ours",
                    our_weight,
                    f"当前目标: {_top_target_summary(our_targets)}",
                    extra=" | ".join(_extra_parts),
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
            filled_grouped = _with_owner_group(filled_cost_view, settings)
            order_summary = st.columns(4)
            order_summary[0].metric("已成交笔数 / Filled Trades", str(len(filled_order_history)))
            order_summary[1].metric("估算成本 / Est. Fees", _format_currency(trade_log_total_fees(filled_cost_view)))
            order_summary[2].metric("所选区间估算已实现 / Est. Realized", _format_currency(estimated_realized))
            order_summary[3].metric("未完成订单 / Open Orders", str(len(open_orders)))

            st.markdown("**两家策略成交分账 / Two-Group Trade Breakdown**")
            st.caption("这里按两家来拆：我的策略组 = Baseline + Fusion + OFIM，另一边是 Claude/Cascade。")
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
                        st.dataframe(_orders_view(_dashboard_display_tail(subset)), use_container_width=True, hide_index=True)

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
                    st.dataframe(_orders_view(_dashboard_display_tail(filled_cost_view)), use_container_width=True, hide_index=True)
            with order_tabs[1]:
                if open_orders.empty:
                    st.info("当前没有未完成订单 / No open orders.")
                else:
                    st.dataframe(_orders_view(open_orders), use_container_width=True, hide_index=True)
            with order_tabs[2]:
                if order_history.empty:
                    st.info("所选日期内没有订单 / No orders in the selected date range.")
                else:
                    st.dataframe(_orders_view(_dashboard_display_tail(order_history)), use_container_width=True, hide_index=True)

        with learning_tab:
            st.markdown("**策略学习实验室 / Strategy Learning Lab**")
            st.caption(
                "这里只做证据生成和候选建议，不会直接修改 live 策略。候选必须经过回放、样本外、paper 和人工确认。"
            )
            if st.button("重建学习报告 / Rebuild Learning Report", key="rebuild-stock-learning"):
                result = run_learning_pipeline(settings=current_settings)
                st.success(f"学习报告已重建：outcomes={result.outcome_count}, candidates={result.candidate_count}")
                st.rerun()
            learning_report = load_learning_report()
            review_packet = load_learning_review_packet()
            learning_candidates = load_strategy_candidates()
            promotion_report = load_promotion_report()
            outcomes = load_trade_outcomes(tail=200)
            total = dict(learning_report.get("total") or {})
            learn_cols = st.columns(4)
            learn_cols[0].metric("已归因交易 / Outcomes", str(total.get("trades", 0)))
            learn_cols[1].metric("胜率 / Win Rate", _format_pct(total.get("win_rate", 0.0)))
            learn_cols[2].metric("净贡献 / Net PnL", _format_currency(total.get("net_pnl", 0.0)))
            learn_cols[3].metric("候选改动 / Candidates", str(len(learning_candidates)))
            if review_packet:
                st.caption(
                    f"审阅包 / Review Packet: {STOCK_LEARNING_REVIEW_PACKET_FILE} "
                    f"(packet_id={review_packet.get('packet_id', 'unknown')})"
                )
                if STOCK_LEARNING_REVIEW_PACKET_FILE.exists():
                    with st.expander("查看给 Codex 的审阅包 / View Review Packet", expanded=False):
                        st.markdown(STOCK_LEARNING_REVIEW_PACKET_FILE.read_text(encoding="utf-8"))
            if learning_report:
                by_strategy = pd.DataFrame(
                    [{"strategy": key, **value} for key, value in dict(learning_report.get("by_strategy") or {}).items()]
                )
                by_reason = pd.DataFrame(
                    [{"reason": key, **value} for key, value in dict(learning_report.get("by_reason") or {}).items()]
                )
                by_symbol = pd.DataFrame(
                    [{"symbol": key, **value} for key, value in dict(learning_report.get("by_symbol") or {}).items()]
                )
                sub_tabs = st.tabs(["策略归因 / Strategy", "亏盈原因 / Reasons", "标的 / Symbols", "候选 / Candidates", "最近结果 / Outcomes"])
                with sub_tabs[0]:
                    if by_strategy.empty:
                        st.info("还没有策略归因。")
                    else:
                        st.dataframe(by_strategy, use_container_width=True, hide_index=True)
                with sub_tabs[1]:
                    if by_reason.empty:
                        st.info("还没有原因归因。")
                    else:
                        st.dataframe(by_reason, use_container_width=True, hide_index=True)
                with sub_tabs[2]:
                    if by_symbol.empty:
                        st.info("还没有标的归因。")
                    else:
                        st.dataframe(by_symbol, use_container_width=True, hide_index=True)
                with sub_tabs[3]:
                    if not learning_candidates:
                        st.info("当前没有候选改动。")
                    else:
                        st.dataframe(pd.DataFrame(learning_candidates), use_container_width=True, hide_index=True)
                    decisions = promotion_report.get("decisions", []) if isinstance(promotion_report, dict) else []
                    if decisions:
                        st.markdown("**晋级门禁 / Promotion Gate**")
                        st.dataframe(pd.DataFrame(decisions), use_container_width=True, hide_index=True)
                    st.warning("Live 自动晋级始终关闭；这里最多允许进入 paper/replay 验证。")
                with sub_tabs[4]:
                    if not outcomes:
                        st.info("还没有已实现交易结果。")
                    else:
                        st.dataframe(pd.DataFrame(outcomes), use_container_width=True, hide_index=True)
            else:
                st.info("还没有学习报告。点击上方按钮，或运行 `.venv/bin/taa-futu stock-learning-build`。")

        with ops_tab:
            st.markdown("**错误日志 / Error Log**")
            errors = _recent_error_frame(days=3)
            if errors.empty:
                st.info("最近 3 天没有市场数据错误日志。")
            else:
                columns = [column for column in ["ts", "market_date", "context", "detail_preview"] if column in errors.columns]
                st.dataframe(errors.loc[:, columns], use_container_width=True, hide_index=True)
                with st.expander("完整错误详情 / Full Error Details", expanded=False):
                    detail_columns = [column for column in ["ts", "context", "detail"] if column in errors.columns]
                    st.dataframe(errors.loc[:, detail_columns], use_container_width=True, hide_index=True)

            st.markdown("**股票运行事件 / Stock Runtime Events**")
            events = pd.DataFrame(load_stock_events(tail=200))
            if events.empty:
                st.info("股票运行事件日志还没有记录。")
            else:
                visible_cols = [
                    column
                    for column in ["ts", "event_type", "cycle_id", "action", "detail", "count", "signature"]
                    if column in events.columns
                ]
                st.dataframe(events.loc[:, visible_cols], use_container_width=True, hide_index=True)

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
            "Fusion 日内回放 (LOB实盘) / Fusion LOB Replay",
            "OFIM 日内回放 (LOB实盘) / OFIM LOB Replay",
            "Fusion 日内回放 (近似) / Fusion Approx Replay",
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

    def _render_intraday_replay_result(result, *, strategy_name: str = "Intraday Replay") -> None:
        """Render an IntradayReplayResult from intraday_replay.run_*_replay()."""
        s = result.summary
        cols = st.columns(4)
        cols[0].metric("总收益 / Total Return",  f"{s.get('total_return', 0):.2%}")
        cols[1].metric("Sharpe",                  f"{s.get('sharpe', 0):.2f}")
        cols[2].metric("最大回撤 / Max Drawdown", f"{s.get('max_drawdown', 0):.2%}")
        cols[3].metric("成交笔数 / Trades",        str(int(s.get("n_trades", 0))))
        cols2 = st.columns(2)
        cols2[0].metric("年化波动 / Ann. Vol",    f"{s.get('annualised_vol', 0):.2%}")
        cols2[1].metric("总费用 / Total Fees",    f"${s.get('total_fees_usd', 0):,.2f}")

        if not result.equity_curve.empty:
            st.markdown("**权益曲线 / Equity Curve**")
            st.line_chart(result.equity_curve.rename(strategy_name), use_container_width=True)

        st.markdown("**成交记录 / Trade Log**")
        if result.trade_log.empty:
            st.info("所选区间内没有成交记录 / No fills in the selected range.")
        else:
            tl = result.trade_log.copy()
            if "ts" in tl.columns:
                tl["ts"] = tl["ts"].astype(str).str[:19]
            st.dataframe(tl, use_container_width=True, hide_index=True)
            csv = tl.to_csv(index=False).encode()
            st.download_button("下载成交记录 CSV / Download Trade Log CSV",
                               data=csv, file_name=f"{strategy_name.replace(' ', '_')}_trades.csv")

        with st.expander("信号日志 / Plan Log (最近 50 条)", expanded=False):
            if result.plan_log:
                import json as _json
                st.json(result.plan_log[-50:])
            else:
                st.info("无信号记录。")

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
        baseline_weight, fusion_weight, ofim_weight, cascade_weight, reserve_weight = stack_allocations(settings)
        st.info(
            "组合回测按当前 stack 配置运行。"
            f"当前组合 / Current Stack: {stack_label(settings)}。"
        )
        try:
            with FutuPaperTrader(settings) as trader:
                baseline_prices = pd.DataFrame()
                if baseline_sleeve_enabled(settings) and baseline_weight > 0:
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
                ofim_symbols = [settings.ofim_benchmark, *settings.ofim_universe] if ofim_weight > 0 else []
                minute_symbols = list(dict.fromkeys([*fusion_symbols, *ofim_symbols]))
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
                    for code in minute_symbols
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

    if mode == "Fusion 日内回放 (LOB实盘) / Fusion LOB Replay":
        st.success("✅ 使用 **实盘 40 档 LOB + 逐笔 + 1分钟 K 线** 回放，与实盘信号完全一致。数据来自 runtime/market_data/。")
        if _run_fusion_lob_replay is None:
            st.error("intraday_replay 模块加载失败，请重启 dashboard。")
            return
        from taa_futu.intraday_replay import _iter_day_dirs
        available = _iter_day_dirs(start.isoformat(), end.isoformat())
        if not available:
            st.warning(f"所选日期范围 {start} ~ {end} 内没有存储的 LOB 数据。请选择有实盘记录的日期（最早从 2026-03-11 开始）。")
            return
        st.caption(f"找到 {len(available)} 个交易日的 LOB 数据：{available[0].name} ~ {available[-1].name}")
        progress_box = st.empty()
        progress_bar = st.progress(0)
        progress_note = st.empty()

        def _progress(payload: dict[str, object]) -> None:
            total_days = max(1, int(payload.get("total_days", 0) or 0))
            completed_days = int(payload.get("completed_days", 0) or 0)
            current_day = str(payload.get("current_day") or "—")
            elapsed = float(payload.get("elapsed_seconds", 0.0) or 0.0)
            progress = float(payload.get("progress", 0.0) or 0.0)
            avg_per_day = elapsed / completed_days if completed_days > 0 else 0.0
            remaining_days = max(total_days - completed_days, 0)
            remaining = avg_per_day * remaining_days if avg_per_day > 0 else 0.0
            pct = max(0, min(100, int(round(progress * 100))))
            progress_bar.progress(pct)
            progress_box.info(
                f"Fusion 回放进度：{completed_days}/{total_days} 个交易日 | 当前 / Current: {current_day} | "
                f"已用时 / Elapsed: {elapsed/60:.1f} min | 预计剩余 / ETA: {remaining/60:.1f} min"
            )
            progress_note.caption(
                "进度按交易日更新。每个交易日内部还会逐轮读取 40 档 LOB、逐笔和 1 分钟 K 线，所以某一天内停一会儿是正常的。"
            )
        with st.spinner("正在用实盘 LOB 数据回放 Fusion …"):
            try:
                result = _run_fusion_lob_replay(
                    start.isoformat(), end.isoformat(), settings,
                    initial_capital=float(initial_capital),
                    cost_model=build_trade_cost_model(settings),
                    progress_callback=_progress,
                )
            except Exception as exc:
                st.error(f"回放失败: {exc}")
                return
        progress_bar.progress(100)
        progress_box.success("Fusion LOB 回放已完成。")
        _render_intraday_replay_result(result, strategy_name="Fusion LOB Replay")
        return

    if mode == "OFIM 日内回放 (LOB实盘) / OFIM LOB Replay":
        st.success("✅ 使用 **实盘 40 档 LOB + 逐笔 + 1分钟 K 线** 回放 OFIM（仅股票，不含 Binance 加密数据）。")
        if _run_ofim_lob_replay is None:
            st.error("intraday_replay 模块加载失败，请重启 dashboard。")
            return
        from taa_futu.intraday_replay import _iter_day_dirs
        available = _iter_day_dirs(start.isoformat(), end.isoformat())
        if not available:
            st.warning(f"所选日期范围 {start} ~ {end} 内没有存储的 LOB 数据。")
            return
        st.caption(f"找到 {len(available)} 个交易日的 LOB 数据：{available[0].name} ~ {available[-1].name}")
        progress_box = st.empty()
        progress_bar = st.progress(0)
        progress_note = st.empty()

        def _progress(payload: dict[str, object]) -> None:
            total_days = max(1, int(payload.get("total_days", 0) or 0))
            completed_days = int(payload.get("completed_days", 0) or 0)
            current_day = str(payload.get("current_day") or "—")
            elapsed = float(payload.get("elapsed_seconds", 0.0) or 0.0)
            progress = float(payload.get("progress", 0.0) or 0.0)
            avg_per_day = elapsed / completed_days if completed_days > 0 else 0.0
            remaining_days = max(total_days - completed_days, 0)
            remaining = avg_per_day * remaining_days if avg_per_day > 0 else 0.0
            pct = max(0, min(100, int(round(progress * 100))))
            progress_bar.progress(pct)
            progress_box.info(
                f"OFIM 回放进度：{completed_days}/{total_days} 个交易日 | 当前 / Current: {current_day} | "
                f"已用时 / Elapsed: {elapsed/60:.1f} min | 预计剩余 / ETA: {remaining/60:.1f} min"
            )
            progress_note.caption(
                "进度按交易日更新。OFIM 会逐轮读取 40 档 LOB、逐笔和 1 分钟 K 线；当天内部没有进度跳动时，不代表卡死。"
            )
        with st.spinner("正在用实盘 LOB 数据回放 OFIM …"):
            try:
                result = _run_ofim_lob_replay(
                    start.isoformat(), end.isoformat(), settings,
                    initial_capital=float(initial_capital),
                    cost_model=build_trade_cost_model(settings),
                    progress_callback=_progress,
                )
            except Exception as exc:
                st.error(f"回放失败: {exc}")
                return
        progress_bar.progress(100)
        progress_box.success("OFIM LOB 回放已完成。")
        _render_intraday_replay_result(result, strategy_name="OFIM LOB Replay")
        return

    if mode == "Fusion 日内回放 (近似) / Fusion Approx Replay":
        st.warning("近似版：用 1 分钟历史 K 线回放核心逻辑，不含真实 L2 订单簿和逐笔数据。建议优先使用上面的 LOB 实盘回放。")
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
    version, tag, commit = describe_build()
    st.set_page_config(page_title="TAA Futu 控制终端 / Trading Terminal", layout="wide", initial_sidebar_state="expanded")

    # The news workspace embeds a board that ships its own header and metrics.
    # Printing the terminal title above it stacked two headers and pushed the
    # actual news ~600px down the page, so that view opts out of the app header.
    def _render_header() -> None:
        st.title("JQ Quant · 控制终端")
        st.caption(
            f"市场 / Market: {settings.futu_trd_market} | 交易环境 / Trade Env: {settings.futu_trd_env} | OpenD: {settings.futu_host}:{settings.futu_port}"
        )
        st.caption(f"版本 / Version: {version} | 标签 / Tag: {tag} | 提交 / Commit: {commit}")

    _inject_terminal_css()
    _render_sidebar_toolbar()

    # 导航与分发交给外壳（taa_futu.shell），功能清单由注册表生成。
    #
    # 这里曾经是一份手工维护的 SIDEBAR_OPTIONS 加一串 if/elif：每加一个功能都要
    # 改三处（常量、清单、分发），漏改一处就出现「侧边栏有按钮但点了没反应」。
    # 现在功能自己登记，外壳照单渲染，加功能不必碰这个文件。
    #
    # 侧边栏仍然用普通按钮而非 radio：radio 有「控件状态 vs 程序状态」的竞态，
    # 子页里点任何按钮触发 rerun 时，radio 会用它记住的旧值覆盖 session_state，
    # 把用户弹回首页。session_state["view"] 是唯一事实来源，控件只写不读。
    from taa_futu.plugin import Feature, registry
    from taa_futu.shell import run_unified

    # 股票与历史模拟的实现就在本模块里，由本模块直接登记，不放进 features/ 包。
    #
    # 原因：本文件是 Streamlit 直接执行的脚本。若在 features/ 里写模块去
    # `from taa_futu.dashboard_app import render_live_monitor`，Python 会把这
    # 5700 行的模块再完整导入一次（一份是被执行的脚本，一份是普通模块），
    # 模块级代码跑两遍——实测让端到端测试 30 秒超时。用本地函数引用登记则
    # 完全绕开这个问题。
    def _render_stock(s):
        from taa_futu.dashboard_extras import render_nav_breadcrumb
        render_nav_breadcrumb("📈 股票交易 / Stock Trading")
        render_live_monitor(s)

    def _render_history(s):
        from taa_futu.dashboard_extras import render_nav_breadcrumb
        render_nav_breadcrumb("📊 历史模拟 / Historical Simulation")
        render_historical_simulation(s)

    registry.register(Feature(
        id="stock", label="股票交易 / Stock Trading", icon="📈", order=10,
        summary=(
            "TAA + Fusion + OFIM + Cascade 四 sleeve 量化 stack，模拟盘自动运行。\n\n"
            "看：实时监控 / 持仓 / 订单 / 日内信号；\n"
            "做：启停自动运行 / pre-gate 切换 / 调整 stack 权重。"
        ),
        render=_render_stock,
    ))
    registry.register(Feature(
        id="stock_history", label="历史模拟 / Historical Sim", icon="📊", order=70,
        summary="用历史数据回放策略，检验参数与假设。",
        # 原本就是首页底部的快捷链接，key 沿用 enter_history
        placement="quick", home_button_key="enter_history",
        render=_render_history,
    ))

    # 首页底部的工具动作。它们不是「功能」（没有自己的页面），所以不进注册表，
    # 由宿主单独登记——这正是把「功能」和「动作」分开的价值。
    from taa_futu.shell import register_quick_action
    from taa_futu.dashboard_extras import STOCK_LAUNCHERS, _open_command, _run_cli

    def _launch_panel() -> None:
        _open_command(STOCK_LAUNCHERS / "Launch_Trading_Control_Panel.command")

    def _run_doctor() -> None:
        with st.spinner("stock-system-doctor…"):
            res = _run_cli(["stock-system-doctor"], timeout=30)
        st.code(res["stdout"] or res["stderr"] or "(空)", language="text")

    register_quick_action("🎛️ 启动桌面控制台", "launch_panel", _launch_panel)
    register_quick_action("📋 系统体检 Doctor", "run_doctor", _run_doctor)

    run_unified(settings, render_header=_render_header)


if __name__ == "__main__":
    main()
