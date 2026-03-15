from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .cascade_sleeve import cascade_trade_symbols
from .costs import estimate_realized_from_fills, trade_log_total_fees
from .strategy_stack import effective_fusion_settings, stack_allocations


REPO_ROOT = Path(__file__).resolve().parents[2]
SPLIT_STATE_FILE = REPO_ROOT / "runtime" / "strategy_split_state.json"
STRATEGY_NAMES = ("Baseline", "Fusion", "Claude/Cascade")
STRATEGY_STATE_KEYS = {
    "Baseline": "baseline",
    "Fusion": "fusion",
    "Claude/Cascade": "cascade",
}


def load_strategy_split_state() -> dict[str, object]:
    if not SPLIT_STATE_FILE.exists():
        return {}
    try:
        return json.loads(SPLIT_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def filter_fills_since_reset(filled_cost_view: pd.DataFrame, split_state: dict[str, object], settings) -> pd.DataFrame:
    if filled_cost_view.empty:
        return filled_cost_view
    reset_at = split_state.get("reset_at") if isinstance(split_state, dict) else None
    if not reset_at:
        return filled_cost_view
    reset_ts = pd.to_datetime(reset_at, errors="coerce", utc=True)
    if pd.isna(reset_ts):
        return filled_cost_view
    reset_local = reset_ts.tz_convert(settings.auto_trader_market_timezone).tz_localize(None)

    rows = filled_cost_view.copy()
    updated_ts = pd.to_datetime(rows.get("updated_time"), errors="coerce")
    if updated_ts is None:
        return rows
    rows = rows.loc[updated_ts >= reset_local].copy()
    return rows.reset_index(drop=True)


def strategy_allocation_map(settings) -> dict[str, float]:
    baseline_weight, fusion_weight, cascade_weight, _reserve_weight = stack_allocations(settings)
    return {
        "Baseline": float(baseline_weight),
        "Fusion": float(fusion_weight),
        "Claude/Cascade": float(cascade_weight),
    }


def split_state_weight_map(split_state: dict[str, object]) -> dict[str, float]:
    strategies = dict(split_state.get("strategies", {})) if isinstance(split_state, dict) else {}
    return {
        "Baseline": float(((strategies.get("baseline") or {}).get("weight")) or 0.0),
        "Fusion": float(((strategies.get("fusion") or {}).get("weight")) or 0.0),
        "Claude/Cascade": float(((strategies.get("cascade") or {}).get("weight")) or 0.0),
    }


def split_state_matches_current(split_state: dict[str, object], settings) -> bool:
    current = strategy_allocation_map(settings)
    reset = split_state_weight_map(split_state)
    return all(abs(float(current[name]) - float(reset[name])) <= 1e-9 for name in STRATEGY_NAMES)


def strategy_symbol_sets(settings) -> dict[str, set[str]]:
    fusion_settings = effective_fusion_settings(settings)
    return {
        "Baseline": set(settings.symbols),
        "Fusion": set(fusion_settings.fusion_universe) | {settings.fusion_benchmark},
        "Claude/Cascade": set(cascade_trade_symbols(settings)),
    }


def strategy_targets_map(
    *,
    baseline_targets: dict[str, float],
    fusion_targets: dict[str, float],
    cascade_targets: dict[str, float],
) -> dict[str, dict[str, float]]:
    return {
        "Baseline": dict(baseline_targets),
        "Fusion": dict(fusion_targets),
        "Claude/Cascade": dict(cascade_targets),
    }


def top_target_summary(target_weights: dict[str, float], *, limit: int = 4) -> str:
    if not target_weights:
        return "当前没有新目标仓位。"
    ordered = sorted(target_weights.items(), key=lambda item: item[1], reverse=True)[:limit]
    return " / ".join(f"{code.replace('US.', '')} {weight:.0%}" for code, weight in ordered)


def _position_metric_series(positions: pd.DataFrame, primary: str, fallback: str) -> pd.Series:
    if positions.empty:
        return pd.Series(dtype=float)
    primary_series = pd.to_numeric(positions.get(primary), errors="coerce")
    fallback_series = pd.to_numeric(positions.get(fallback), errors="coerce")
    if primary_series is None:
        return fallback_series.fillna(0.0)
    if fallback_series is None:
        return primary_series.fillna(0.0)
    return primary_series.where(primary_series.notna(), fallback_series).fillna(0.0)


def _strategy_live_shares(
    code: str,
    *,
    combined_targets: dict[str, float],
    strategy_targets: dict[str, dict[str, float]],
    strategy_sets: dict[str, set[str]],
) -> dict[str, float]:
    combined_weight = float(combined_targets.get(code, 0.0))
    if combined_weight > 0:
        shares = {
            name: float(weights.get(code, 0.0)) / combined_weight
            for name, weights in strategy_targets.items()
            if float(weights.get(code, 0.0)) > 0
        }
        total = sum(shares.values())
        if total > 0:
            return {name: share / total for name, share in shares.items() if share > 0}

    owners = [name for name, symbols in strategy_sets.items() if code in symbols]
    if len(owners) == 1:
        return {owners[0]: 1.0}
    return {"Shared/Overlap": 1.0} if owners else {}


def current_strategy_holdings(
    *,
    settings,
    positions: pd.DataFrame,
    total_assets: float,
    combined_targets: dict[str, float],
    baseline_targets: dict[str, float],
    fusion_targets: dict[str, float],
    cascade_targets: dict[str, float],
) -> pd.DataFrame:
    strategy_sets = strategy_symbol_sets(settings)
    strategy_targets = strategy_targets_map(
        baseline_targets=baseline_targets,
        fusion_targets=fusion_targets,
        cascade_targets=cascade_targets,
    )
    allocation_map = strategy_allocation_map(settings)
    rows = {
        name: {
            "策略 / Strategy": name,
            "允许操作仓位 / Budget": f"{allocation_map[name]:.0%}",
            "当前目标 / Targets": top_target_summary(strategy_targets[name]),
            "目标市值 / Target Value": total_assets * sum(float(v) for v in strategy_targets[name].values()),
            "当前持仓市值 / Holdings": 0.0,
            "当前浮盈 / Unrealized": 0.0,
        }
        for name in STRATEGY_NAMES
    }

    market_value_series = (
        pd.to_numeric(positions.get("market_val"), errors="coerce").fillna(0.0) if not positions.empty else pd.Series(dtype=float)
    )
    unrealized_series = _position_metric_series(positions, "unrealized_pl", "pl_val")

    for index, row in positions.iterrows():
        code = str(row.get("code", ""))
        market_value = float(market_value_series.iloc[index]) if index < len(market_value_series) else 0.0
        unrealized = float(unrealized_series.iloc[index]) if index < len(unrealized_series) else 0.0
        shares = _strategy_live_shares(
            code,
            combined_targets=combined_targets,
            strategy_targets=strategy_targets,
            strategy_sets=strategy_sets,
        )
        for strategy_name, share in shares.items():
            if strategy_name not in rows:
                continue
            rows[strategy_name]["当前持仓市值 / Holdings"] = float(rows[strategy_name]["当前持仓市值 / Holdings"]) + market_value * share
            rows[strategy_name]["当前浮盈 / Unrealized"] = float(rows[strategy_name]["当前浮盈 / Unrealized"]) + unrealized * share

    return pd.DataFrame([rows[name] for name in STRATEGY_NAMES])


def strategy_bucket_from_symbol(code: str, settings) -> str:
    strategy_sets = strategy_symbol_sets(settings)
    owners = [name for name, symbols in strategy_sets.items() if code in symbols]
    if len(owners) == 1:
        return owners[0]
    if len(owners) > 1:
        return "Shared/Overlap"
    return "Unclassified"


def period_strategy_performance(*, filled_cost_view: pd.DataFrame, settings) -> pd.DataFrame:
    if filled_cost_view.empty:
        return pd.DataFrame(
            columns=[
                "策略 / Strategy",
                "成交笔数 / Trades",
                "交易成本 / Fees",
                "区间已实现 / Realized",
            ]
        )

    rows = filled_cost_view.copy()
    if "code" in rows.columns:
        rows["strategy_bucket"] = rows["code"].astype(str).map(lambda code: strategy_bucket_from_symbol(code, settings))
    else:
        rows["strategy_bucket"] = "Unclassified"

    summary_rows: list[dict[str, object]] = []
    for strategy_name in (*STRATEGY_NAMES, "Shared/Overlap", "Unclassified"):
        subset = rows[rows["strategy_bucket"] == strategy_name].copy()
        if subset.empty:
            continue
        summary_rows.append(
            {
                "策略 / Strategy": strategy_name,
                "成交笔数 / Trades": int(len(subset)),
                "交易成本 / Fees": trade_log_total_fees(subset),
                "区间已实现 / Realized": estimate_realized_from_fills(
                    subset,
                    settings,
                    qty_col="dealt_qty",
                    price_col="dealt_avg_price",
                    timestamp_col="updated_time",
                ),
            }
        )
    return pd.DataFrame(summary_rows)


def build_strategy_ledger(
    *,
    settings,
    split_state: dict[str, object],
    total_assets: float,
    current_holdings: pd.DataFrame,
    period_performance: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    allocation_map = strategy_allocation_map(settings)
    strategies = dict(split_state.get("strategies", {})) if isinstance(split_state, dict) else {}

    holdings_indexed = current_holdings.set_index("策略 / Strategy") if not current_holdings.empty else pd.DataFrame()
    period_indexed = period_performance.set_index("策略 / Strategy") if not period_performance.empty else pd.DataFrame()

    ledger_rows: list[dict[str, object]] = []
    for strategy_name in STRATEGY_NAMES:
        state_key = STRATEGY_STATE_KEYS[strategy_name]
        start_cash = float(((strategies.get(state_key) or {}).get("start_cash")) or total_assets * allocation_map[strategy_name])
        allowed_capital = total_assets * allocation_map[strategy_name]
        holdings = float(holdings_indexed.at[strategy_name, "当前持仓市值 / Holdings"]) if strategy_name in getattr(holdings_indexed, "index", []) else 0.0
        unrealized = float(holdings_indexed.at[strategy_name, "当前浮盈 / Unrealized"]) if strategy_name in getattr(holdings_indexed, "index", []) else 0.0
        realized = float(period_indexed.at[strategy_name, "区间已实现 / Realized"]) if strategy_name in getattr(period_indexed, "index", []) else 0.0
        fees = float(period_indexed.at[strategy_name, "交易成本 / Fees"]) if strategy_name in getattr(period_indexed, "index", []) else 0.0
        trades = int(period_indexed.at[strategy_name, "成交笔数 / Trades"]) if strategy_name in getattr(period_indexed, "index", []) else 0
        current_value = start_cash + realized + unrealized
        budget_left = allowed_capital - holdings
        ledger_rows.append(
            {
                "策略 / Strategy": strategy_name,
                "允许操作仓位 / Budget": f"{allocation_map[strategy_name]:.0%}",
                "当前允许操作总现金 / Allowed Capital": allowed_capital,
                "当前市值 / Holdings": holdings,
                "预算余量 / Budget Left": budget_left,
                "自重置收益 / PnL Since Reset": current_value - start_cash,
                "当前浮盈 / Unrealized": unrealized,
                "交易成本 / Fees": fees,
                "成交笔数 / Trades": trades,
                "当前目标 / Targets": (
                    str(holdings_indexed.at[strategy_name, "当前目标 / Targets"])
                    if strategy_name in getattr(holdings_indexed, "index", [])
                    else "当前没有新目标仓位。"
                ),
            }
        )

    overlap = period_performance[period_performance["策略 / Strategy"].isin(["Shared/Overlap", "Unclassified"])].copy()
    return pd.DataFrame(ledger_rows), overlap
