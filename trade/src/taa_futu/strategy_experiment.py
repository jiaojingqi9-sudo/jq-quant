from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .cascade_sleeve import cascade_trade_symbols
from .market_logger import load_order_records
from .strategy_stack import effective_fusion_settings, stack_allocations


REPO_ROOT = Path(__file__).resolve().parents[2]
SPLIT_STATE_FILE = REPO_ROOT / "runtime" / "strategy_split_state.json"
STRATEGY_NAMES = ("Baseline", "Fusion", "OFIM", "Claude/Cascade")
STRATEGY_STATE_KEYS = {
    "Baseline": "baseline",
    "Fusion": "fusion",
    "OFIM": "ofim",
    "Claude/Cascade": "cascade",
}


def load_strategy_split_state() -> dict[str, object]:
    if not SPLIT_STATE_FILE.exists():
        return {}
    try:
        return json.loads(SPLIT_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def write_strategy_split_state(
    *,
    settings,
    total_assets: float,
    reason: str,
    path: Path = SPLIT_STATE_FILE,
    now_utc: datetime | None = None,
) -> Path:
    allocation_map = strategy_allocation_map(settings)
    stamp = (now_utc or datetime.now(UTC)).isoformat()
    payload = {
        "reset_at": stamp,
        "base_total_assets": float(total_assets),
        "mode": "four_strategy_split",
        "reason": reason,
        "strategies": {
            STRATEGY_STATE_KEYS[name]: {
                "weight": float(allocation_map[name]),
                "start_cash": float(total_assets) * float(allocation_map[name]),
            }
            for name in STRATEGY_NAMES
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default), encoding="utf-8")
    tmp.replace(path)
    return path


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
    baseline_weight, fusion_weight, ofim_weight, cascade_weight, _reserve_weight = stack_allocations(settings)
    return {
        "Baseline": float(baseline_weight),
        "Fusion": float(fusion_weight),
        "OFIM": float(ofim_weight),
        "Claude/Cascade": float(cascade_weight),
    }


def split_state_weight_map(split_state: dict[str, object]) -> dict[str, float]:
    strategies = dict(split_state.get("strategies", {})) if isinstance(split_state, dict) else {}
    return {
        "Baseline": float(((strategies.get("baseline") or {}).get("weight")) or 0.0),
        "Fusion": float(((strategies.get("fusion") or {}).get("weight")) or 0.0),
        "OFIM": float(((strategies.get("ofim") or {}).get("weight")) or 0.0),
        "Claude/Cascade": float(((strategies.get("cascade") or {}).get("weight")) or 0.0),
    }


def split_state_matches_current(split_state: dict[str, object], settings) -> bool:
    current = strategy_allocation_map(settings)
    reset = split_state_weight_map(split_state)
    return all(abs(float(current[name]) - float(reset[name])) <= 1e-9 for name in STRATEGY_NAMES)


def strategy_symbol_sets(settings, allocation_map: dict[str, float] | None = None) -> dict[str, set[str]]:
    """Return the set of symbols "owned" by each strategy for attribution purposes.

    Strategies with 0% allocation are excluded from ownership so that their
    universe symbols don't create spurious "Shared/Overlap" conflicts when a
    sibling strategy (e.g. OFIM replacing Fusion) shares the same universe.
    """
    fusion_settings = effective_fusion_settings(settings)
    effective_allocations = allocation_map or strategy_allocation_map(settings)
    baseline_w = float(effective_allocations.get("Baseline", 0.0))
    fusion_w = float(effective_allocations.get("Fusion", 0.0))
    ofim_w = float(effective_allocations.get("OFIM", 0.0))
    cascade_w = float(effective_allocations.get("Claude/Cascade", 0.0))
    return {
        "Baseline": set(settings.symbols) if baseline_w > 0 else set(),
        "Fusion": (set(fusion_settings.fusion_universe) | {settings.fusion_benchmark}) if fusion_w > 0 else set(),
        "OFIM": (
            set(fusion_settings.ofim_universe)
            | {proxy for _crypto, proxy in tuple(fusion_settings.ofim_crypto_to_proxy or ())}
        ) if ofim_w > 0 else set(),
        "Claude/Cascade": set(cascade_trade_symbols(settings)) if cascade_w > 0 else set(),
    }


def strategy_targets_map(
    *,
    baseline_targets: dict[str, float],
    fusion_targets: dict[str, float],
    ofim_targets: dict[str, float],
    cascade_targets: dict[str, float],
) -> dict[str, dict[str, float]]:
    return {
        "Baseline": dict(baseline_targets),
        "Fusion": dict(fusion_targets),
        "OFIM": dict(ofim_targets),
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
    ofim_targets: dict[str, float],
    cascade_targets: dict[str, float],
) -> pd.DataFrame:
    strategy_sets = strategy_symbol_sets(settings)
    strategy_targets = strategy_targets_map(
        baseline_targets=baseline_targets,
        fusion_targets=fusion_targets,
        ofim_targets=ofim_targets,
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


def strategy_bucket_from_symbol(code: str, settings, allocation_map: dict[str, float] | None = None) -> str:
    strategy_sets = strategy_symbol_sets(settings, allocation_map=allocation_map)
    owners = [name for name, symbols in strategy_sets.items() if code in symbols]
    if len(owners) == 1:
        return owners[0]
    if len(owners) > 1:
        return "Shared/Overlap"
    return "Unclassified"


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if pd.isna(number):
        return default
    return float(number)


def _normalized_side(value: object) -> str:
    text = str(value or "").strip().upper()
    if text.endswith("BUY") or text == "BUY":
        return "BUY"
    if text.endswith("SELL") or text == "SELL":
        return "SELL"
    return text


def _bucket_realized_performance(
    rows: pd.DataFrame,
    *,
    side_col: str = "trd_side",
    qty_col: str = "dealt_qty",
    price_col: str = "dealt_avg_price",
    timestamp_col: str = "updated_time",
) -> pd.DataFrame:
    """Attribute realized PnL with one account-level FIFO pass.

    Running FIFO independently inside each strategy bucket can create impossible
    numbers when one strategy's order closes inventory opened by another
    strategy. This pass keeps the account inventory global and attributes the
    realized outcome to the opening lot's strategy bucket.
    """
    if rows.empty:
        return pd.DataFrame(
            columns=[
                "策略 / Strategy",
                "成交笔数 / Trades",
                "交易成本 / Fees",
                "区间已实现 / Realized",
            ]
        )

    work = rows.copy()
    if timestamp_col in work.columns:
        work["_strategy_ts"] = pd.to_datetime(work[timestamp_col], errors="coerce")
        sort_columns = ["_strategy_ts"]
        if "code" in work.columns:
            sort_columns.append("code")
        work = work.sort_values(sort_columns, na_position="last").copy()

    buckets = (*STRATEGY_NAMES, "Shared/Overlap", "Unclassified")
    stats: dict[str, dict[str, float]] = {
        bucket: {"trades": 0.0, "fees": 0.0, "realized": 0.0}
        for bucket in buckets
    }
    lots: dict[str, deque[dict[str, float | str]]] = {}

    for _, row in work.iterrows():
        symbol = str(row.get("code", "")).strip().upper()
        side = _normalized_side(row.get(side_col, ""))
        qty = _safe_float(row.get(qty_col))
        price = _safe_float(row.get(price_col))
        fee = max(0.0, _safe_float(row.get("fees_total")))
        bucket = str(row.get("strategy_bucket") or "Unclassified")
        if bucket not in stats:
            bucket = "Unclassified"
        if not symbol or side not in {"BUY", "SELL"} or qty <= 0 or price <= 0:
            continue

        stats[bucket]["trades"] += 1
        stats[bucket]["fees"] += fee
        symbol_lots = lots.setdefault(symbol, deque())
        if side == "BUY":
            symbol_lots.append(
                {
                    "quantity": qty,
                    "basis": price + fee / qty if qty > 0 else price,
                    "bucket": bucket,
                }
            )
            continue

        remaining = qty
        sell_fee_per_unit = fee / qty if qty > 0 else 0.0
        while remaining > 1e-12 and symbol_lots:
            lot = symbol_lots[0]
            open_qty = _safe_float(lot.get("quantity"))
            if open_qty <= 0:
                symbol_lots.popleft()
                continue
            matched = min(remaining, open_qty)
            entry_bucket = str(lot.get("bucket") or "Unclassified")
            if entry_bucket not in stats:
                entry_bucket = "Unclassified"
            stats[entry_bucket]["realized"] += (price - sell_fee_per_unit - _safe_float(lot.get("basis"))) * matched
            remaining -= matched
            open_qty -= matched
            if open_qty <= 1e-12:
                symbol_lots.popleft()
            else:
                lot["quantity"] = open_qty

    out: list[dict[str, object]] = []
    for bucket in buckets:
        values = stats[bucket]
        if values["trades"] <= 0 and abs(values["fees"]) <= 1e-12 and abs(values["realized"]) <= 1e-12:
            continue
        out.append(
            {
                "策略 / Strategy": bucket,
                "成交笔数 / Trades": int(values["trades"]),
                "交易成本 / Fees": float(values["fees"]),
                "区间已实现 / Realized": float(values["realized"]),
            }
        )
    return pd.DataFrame(out)


def _logged_strategy_source_map(start: str, end: str | None = None) -> dict[str, str]:
    logged_orders = load_order_records(start, end)
    if logged_orders.empty:
        return {}
    submitted = logged_orders.copy()
    for column in ("action", "submit_status", "submit_detail", "strategy_source"):
        if column not in submitted.columns:
            submitted[column] = ""
        submitted[column] = submitted[column].astype(str)
    submitted = submitted[
        (submitted["action"] == "submitted")
        & (submitted["submit_status"].str.lower() == "submitted")
        & (submitted["submit_detail"].str.strip() != "")
        & (submitted["strategy_source"].str.strip() != "")
    ].copy()
    if submitted.empty:
        return {}
    submitted["order_id"] = submitted["submit_detail"].str.strip()
    submitted = submitted.drop_duplicates(subset=["order_id"], keep="last")
    return {
        str(row["order_id"]): str(row["strategy_source"])
        for _, row in submitted.iterrows()
        if str(row["strategy_source"]).strip()
    }


def period_strategy_performance(*, filled_cost_view: pd.DataFrame, settings, split_state: dict[str, object] | None = None) -> pd.DataFrame:
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
    attribution_allocations = strategy_allocation_map(settings)
    start = None
    if split_state:
        reset_allocations = split_state_weight_map(split_state)
        if any(float(v) > 0 for v in reset_allocations.values()):
            attribution_allocations = reset_allocations
        reset_at = split_state.get("reset_at") if isinstance(split_state, dict) else None
        if reset_at:
            reset_ts = pd.to_datetime(reset_at, errors="coerce", utc=True)
            if not pd.isna(reset_ts):
                start = reset_ts.tz_convert(settings.auto_trader_market_timezone).date().isoformat()
    end = None
    if "updated_time" in rows.columns:
        updated_time = pd.to_datetime(rows["updated_time"], errors="coerce")
        if not updated_time.dropna().empty:
            end = updated_time.dropna().max().date().isoformat()
    strategy_source_map = _logged_strategy_source_map(start or settings.start_date, end)

    order_id_series = rows.get("order_id")
    if order_id_series is not None:
        rows["strategy_bucket"] = (
            order_id_series.astype(str).map(lambda order_id: strategy_source_map.get(order_id.strip(), "")).astype(str)
        )
    else:
        rows["strategy_bucket"] = ""

    if "code" in rows.columns:
        fallback_buckets = rows["code"].astype(str).map(
            lambda code: strategy_bucket_from_symbol(code, settings, allocation_map=attribution_allocations)
        )
        rows["strategy_bucket"] = rows["strategy_bucket"].where(rows["strategy_bucket"].str.strip() != "", fallback_buckets)
    else:
        rows["strategy_bucket"] = rows["strategy_bucket"].where(rows["strategy_bucket"].str.strip() != "", "Unclassified")

    return _bucket_realized_performance(rows)


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
        state = dict(strategies.get(state_key) or {})
        reset_weight = _safe_float(state.get("weight"))
        has_split_epoch = bool(split_state.get("reset_at")) if isinstance(split_state, dict) else False
        if "start_cash" in state:
            start_cash = _safe_float(state.get("start_cash"))
        else:
            start_cash = total_assets * allocation_map[strategy_name]
        allowed_capital = total_assets * allocation_map[strategy_name]
        holdings = float(holdings_indexed.at[strategy_name, "当前持仓市值 / Holdings"]) if strategy_name in getattr(holdings_indexed, "index", []) else 0.0
        unrealized = float(holdings_indexed.at[strategy_name, "当前浮盈 / Unrealized"]) if strategy_name in getattr(holdings_indexed, "index", []) else 0.0
        realized = float(period_indexed.at[strategy_name, "区间已实现 / Realized"]) if strategy_name in getattr(period_indexed, "index", []) else 0.0
        fees = float(period_indexed.at[strategy_name, "交易成本 / Fees"]) if strategy_name in getattr(period_indexed, "index", []) else 0.0
        trades = int(period_indexed.at[strategy_name, "成交笔数 / Trades"]) if strategy_name in getattr(period_indexed, "index", []) else 0
        current_value = start_cash + realized + unrealized
        needs_epoch_reset = has_split_epoch and reset_weight <= 0 and start_cash <= 0 and (
            allocation_map[strategy_name] > 0 or trades > 0 or holdings > 0 or abs(realized) > 1e-9 or abs(unrealized) > 1e-9
        )
        ledger_status = "需重设起点 / Reset Required" if needs_epoch_reset else "OK"
        net_performance: object = pd.NA if needs_epoch_reset else float(current_value - start_cash)
        budget_left = allowed_capital - holdings
        ledger_rows.append(
            {
                "策略 / Strategy": strategy_name,
                "允许操作仓位 / Budget": f"{allocation_map[strategy_name]:.0%}",
                "当前允许操作总现金 / Allowed Capital": allowed_capital,
                "当前市值 / Holdings": holdings,
                "预算余量 / Budget Left": budget_left,
                "净表现 / Net Performance": net_performance,
                "当前浮盈 / Unrealized": unrealized,
                "交易成本 / Fees": fees,
                "成交笔数 / Trades": trades,
                "账本状态 / Ledger Status": ledger_status,
                "当前目标 / Targets": (
                    str(holdings_indexed.at[strategy_name, "当前目标 / Targets"])
                    if strategy_name in getattr(holdings_indexed, "index", [])
                    else "当前没有新目标仓位。"
                ),
            }
        )

    overlap = period_performance[period_performance["策略 / Strategy"].isin(["Shared/Overlap", "Unclassified"])].copy()
    return pd.DataFrame(ledger_rows), overlap
