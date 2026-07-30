from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, TYPE_CHECKING

import pandas as pd

from .stock_runtime import STOCK_FILLS_FILE, STOCK_LEDGER_EPOCH_FILE, load_stock_fill_records, load_stock_ledger_epoch

if TYPE_CHECKING:
    from .config import Settings
    from .futu_gateway import PlannedOrder


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime"
STOCK_ORDER_MEMORY_FILE = RUNTIME_DIR / "stock_order_memory.jsonl"
STOCK_TRADE_OUTCOMES_FILE = RUNTIME_DIR / "stock_trade_outcomes.jsonl"
STOCK_ATTRIBUTION_FILE = RUNTIME_DIR / "stock_attribution.json"
STOCK_STRATEGY_CANDIDATES_FILE = RUNTIME_DIR / "strategy_upgrade_candidates.jsonl"
STOCK_PROMOTION_REPORT_FILE = RUNTIME_DIR / "strategy_promotion_report.json"
STOCK_LEARNING_REVIEW_PACKET_JSON_FILE = RUNTIME_DIR / "stock_learning_review_packet.json"
STOCK_LEARNING_REVIEW_PACKET_FILE = RUNTIME_DIR / "stock_learning_review_packet.md"


@dataclass(frozen=True)
class LearningPipelineResult:
    outcome_count: int
    candidate_count: int
    attribution_path: Path
    outcomes_path: Path
    candidates_path: Path
    promotion_path: Path
    review_packet_path: Path
    review_packet_json_path: Path


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, pd.Series):
        return value.to_dict()
    if isinstance(value, pd.DataFrame):
        return value.to_dict("records")
    return str(value)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default), encoding="utf-8")
    tmp.replace(path)


def _write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n")
    tmp.replace(path)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n")
            count += 1
    return count


def _load_jsonl(path: Path, *, tail: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    if tail is not None and tail > 0:
        lines = lines[-tail:]
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _safe_ts(value: object) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _hash_payload(payload: dict[str, Any], *, length: int = 16) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _file_sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _artifact_meta(path: Path) -> dict[str, Any]:
    exists = path.exists()
    return {
        "path": str(path),
        "exists": exists,
        "bytes": path.stat().st_size if exists and path.is_file() else 0,
        "sha256": _file_sha256(path) if exists and path.is_file() else "",
    }


def _account_snapshot(account: pd.Series | dict[str, Any] | None) -> dict[str, float]:
    if account is None:
        return {}
    getter = account.get
    return {
        "total_assets": _safe_float(getter("total_assets", 0.0)),
        "cash": _safe_float(getter("cash", getter("cash_balance", 0.0))),
        "market_val": _safe_float(getter("market_val", 0.0)),
    }


def _position_context(positions: pd.DataFrame | None, code: str) -> dict[str, float]:
    if positions is None or positions.empty or "code" not in positions.columns:
        return {"position_qty": 0.0, "position_market_val": 0.0}
    rows = positions[positions["code"] == code]
    if rows.empty:
        return {"position_qty": 0.0, "position_market_val": 0.0}
    out = {
        "position_qty": _safe_float(pd.to_numeric(rows.get("qty"), errors="coerce").fillna(0.0).sum() if "qty" in rows else 0.0),
        "position_market_val": _safe_float(pd.to_numeric(rows.get("market_val"), errors="coerce").fillna(0.0).sum() if "market_val" in rows else 0.0),
    }
    return out


def _settings_snapshot(settings: Settings | None) -> dict[str, Any]:
    if settings is None:
        return {}
    keys = (
        "stack_active_strategy",
        "stack_baseline_weight",
        "stack_fusion_weight",
        "stack_ofim_weight",
        "stack_cascade_weight",
        "fusion_entry_score",
        "fusion_exit_score",
        "fusion_max_spread_bps",
        "ofim_entry_threshold",
        "ofim_exit_threshold",
        "ofim_max_spread_bps",
        "auto_trader_min_order_value_usd",
        "auto_trader_min_hold_minutes",
        "auto_trader_rebalance_drift_pct",
        "auto_trader_max_target_gross_exposure",
        "auto_trader_max_target_weight",
        "auto_trader_max_order_value_usd",
        "auto_trader_max_cycle_turnover_usd",
    )
    return {key: getattr(settings, key, None) for key in keys if hasattr(settings, key)}


def _result_map(result_df: pd.DataFrame | None) -> dict[tuple[str, str, int, float], dict[str, Any]]:
    if result_df is None or result_df.empty:
        return {}
    out: dict[tuple[str, str, int, float], dict[str, Any]] = {}
    for row in result_df.to_dict("records"):
        key = (
            str(row.get("code", "")),
            str(row.get("side", "")).upper(),
            int(_safe_float(row.get("quantity"))),
            round(_safe_float(row.get("limit_price")), 8),
        )
        out[key] = row
    return out


def append_order_memory(
    orders: list[PlannedOrder],
    *,
    cycle_id: str,
    stage: str,
    settings: Settings | None = None,
    account: pd.Series | dict[str, Any] | None = None,
    positions: pd.DataFrame | None = None,
    target_weights: dict[str, float] | None = None,
    diagnostics: dict[str, Any] | None = None,
    result_df: pd.DataFrame | None = None,
    order_memory_path: Path = STOCK_ORDER_MEMORY_FILE,
    now_utc: datetime | None = None,
) -> int:
    stamp = (now_utc or datetime.now(UTC)).isoformat()
    result_by_key = _result_map(result_df)
    account_ctx = _account_snapshot(account)
    settings_ctx = _settings_snapshot(settings)
    rows: list[dict[str, Any]] = []
    for idx, order in enumerate(orders):
        key = (order.code, order.side.upper(), int(order.quantity), round(float(order.limit_price), 8))
        result = result_by_key.get(key, {})
        order_id = str(result.get("detail", "")).strip() if str(result.get("status", "")).lower() == "submitted" else ""
        notional = _safe_float(order.quantity) * _safe_float(order.limit_price)
        base = {
            "schema_version": 1,
            "record_type": "stock_order_memory",
            "ts": stamp,
            "cycle_id": cycle_id,
            "stage": stage,
            "sequence": idx,
            "code": order.code,
            "side": order.side,
            "quantity": int(order.quantity),
            "limit_price": float(order.limit_price),
            "reference_price": float(order.reference_price),
            "notional": float(notional),
            "current_qty": int(order.current_qty),
            "target_qty": int(order.target_qty),
            "target_weight": float(order.target_weight),
            "strategy_source": str(order.strategy_source or "Unclassified"),
            "order_id": order_id,
            "submit_status": str(result.get("status", "")),
            "submit_detail": str(result.get("detail", "")),
            "target_weights": target_weights or {},
            "diagnostics": diagnostics or {},
            "settings": settings_ctx,
            **account_ctx,
            **_position_context(positions, order.code),
        }
        base["decision_id"] = _hash_payload(
            {
                "cycle_id": cycle_id,
                "stage": stage,
                "sequence": idx,
                "code": order.code,
                "side": order.side,
                "quantity": order.quantity,
                "limit_price": order.limit_price,
                "order_id": order_id,
            }
        )
        rows.append(base)
    return _append_jsonl(order_memory_path, rows)


def load_order_memory(path: Path = STOCK_ORDER_MEMORY_FILE, *, tail: int | None = None) -> list[dict[str, Any]]:
    return _load_jsonl(path, tail=tail)


def _epoch_fill_offset(epoch_path: Path | None) -> int:
    if epoch_path is None:
        return 0
    epoch = load_stock_ledger_epoch(epoch_path)
    try:
        return max(0, int(epoch.get("fills_count_at_reset", 0)))
    except (TypeError, ValueError):
        return 0


def _memory_by_order_id(order_memory_path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in load_order_memory(order_memory_path):
        order_id = str(row.get("order_id") or "").strip()
        if order_id:
            out[order_id] = row
    return out


def _classify_outcome(outcome: dict[str, Any]) -> tuple[str, list[str]]:
    tags: list[str] = []
    net = _safe_float(outcome.get("net_pnl"))
    gross = _safe_float(outcome.get("gross_pnl"))
    fees = _safe_float(outcome.get("fees_paid"))
    hold_seconds = _safe_float(outcome.get("hold_seconds"))
    return_pct = _safe_float(outcome.get("return_pct"))
    strategy = str(outcome.get("strategy") or "")
    if net > 0:
        tags.append("profitable")
    if fees > 0 and fees >= max(abs(gross) * 0.5, 0.01) and net <= 0:
        tags.append("fees_dominated")
    if gross < 0:
        tags.append("signal_error")
    if hold_seconds and hold_seconds < 30 * 60 and net < 0:
        tags.append("early_exit_or_noise")
    if abs(return_pct) < 0.001 and fees > 0:
        tags.append("low_edge_trade")
    if strategy in {"Shared/Overlap", "Unclassified", ""}:
        tags.append("attribution_ambiguous")
    if not tags:
        tags.append("loss_unclassified" if net < 0 else "flat")
    primary = next((tag for tag in tags if tag != "attribution_ambiguous"), tags[0])
    return primary, tags


def build_trade_outcomes(
    *,
    fills_path: Path = STOCK_FILLS_FILE,
    order_memory_path: Path = STOCK_ORDER_MEMORY_FILE,
    epoch_path: Path | None = STOCK_LEDGER_EPOCH_FILE,
    outcome_path: Path | None = STOCK_TRADE_OUTCOMES_FILE,
) -> list[dict[str, Any]]:
    records = load_stock_fill_records(fills_path)
    offset = _epoch_fill_offset(epoch_path)
    records = records[offset:] if offset else records
    records = sorted(records, key=lambda row: (str(row.get("ts", "")), str(row.get("event_id", ""))))
    memory = _memory_by_order_id(order_memory_path)
    lots: dict[str, list[dict[str, Any]]] = {}
    outcomes: list[dict[str, Any]] = []

    for index, row in enumerate(records, start=offset):
        symbol = str(row.get("symbol", "")).strip().upper()
        side = str(row.get("side", "")).strip().upper()
        qty = _safe_float(row.get("quantity"))
        price = _safe_float(row.get("price"))
        fee = max(0.0, _safe_float(row.get("fee")))
        if not symbol or side not in {"BUY", "SELL"} or qty <= 0 or price <= 0:
            continue
        order_id = str(row.get("order_id") or "")
        context = memory.get(order_id, {})
        strategy = str(row.get("strategy") or context.get("strategy_source") or "Unclassified")
        event_id = str(row.get("event_id") or f"fill:{index}")
        ts = str(row.get("ts") or "")

        if side == "BUY":
            lots.setdefault(symbol, []).append(
                {
                    "remaining_qty": qty,
                    "entry_qty": qty,
                    "entry_price": price,
                    "entry_fee_per_unit": fee / qty if qty > 0 else 0.0,
                    "entry_ts": ts,
                    "entry_event_id": event_id,
                    "entry_order_id": order_id,
                    "strategy": strategy,
                    "context": context,
                }
            )
            continue

        remaining = qty
        sell_fee_per_unit = fee / qty if qty > 0 else 0.0
        symbol_lots = lots.setdefault(symbol, [])
        while remaining > 1e-12 and symbol_lots:
            lot = symbol_lots[0]
            matched = min(remaining, _safe_float(lot.get("remaining_qty")))
            if matched <= 0:
                symbol_lots.pop(0)
                continue
            entry_price = _safe_float(lot.get("entry_price"))
            buy_fee = _safe_float(lot.get("entry_fee_per_unit")) * matched
            sell_fee = sell_fee_per_unit * matched
            gross_pnl = (price - entry_price) * matched
            net_pnl = gross_pnl - buy_fee - sell_fee
            entry_ts = _safe_ts(lot.get("entry_ts"))
            exit_ts = _safe_ts(ts)
            hold_seconds = (exit_ts - entry_ts).total_seconds() if entry_ts is not None and exit_ts is not None else None
            capital = entry_price * matched + buy_fee
            outcome = {
                "schema_version": 1,
                "outcome_id": _hash_payload(
                    {
                        "entry_event_id": lot.get("entry_event_id"),
                        "exit_event_id": event_id,
                        "quantity": matched,
                    }
                ),
                "symbol": symbol,
                "strategy": str(lot.get("strategy") or strategy or "Unclassified"),
                "entry_ts": str(lot.get("entry_ts") or ""),
                "exit_ts": ts,
                "hold_seconds": hold_seconds,
                "quantity": matched,
                "entry_price": entry_price,
                "exit_price": price,
                "gross_pnl": gross_pnl,
                "fees_paid": buy_fee + sell_fee,
                "net_pnl": net_pnl,
                "return_pct": net_pnl / capital if capital > 0 else 0.0,
                "entry_event_id": lot.get("entry_event_id", ""),
                "exit_event_id": event_id,
                "entry_order_id": lot.get("entry_order_id", ""),
                "exit_order_id": order_id,
                "entry_context": lot.get("context") or {},
            }
            primary, tags = _classify_outcome(outcome)
            outcome["primary_reason"] = primary
            outcome["reason_tags"] = tags
            outcomes.append(outcome)
            remaining -= matched
            lot["remaining_qty"] = _safe_float(lot.get("remaining_qty")) - matched
            if _safe_float(lot.get("remaining_qty")) <= 1e-12:
                symbol_lots.pop(0)
        if remaining > 1e-12:
            outcome = {
                "schema_version": 1,
                "outcome_id": _hash_payload({"unmatched_exit_event_id": event_id, "quantity": remaining}),
                "symbol": symbol,
                "strategy": strategy,
                "entry_ts": "",
                "exit_ts": ts,
                "hold_seconds": None,
                "quantity": remaining,
                "entry_price": 0.0,
                "exit_price": price,
                "gross_pnl": 0.0,
                "fees_paid": sell_fee_per_unit * remaining,
                "net_pnl": -sell_fee_per_unit * remaining,
                "return_pct": 0.0,
                "entry_event_id": "",
                "exit_event_id": event_id,
                "entry_order_id": "",
                "exit_order_id": order_id,
                "primary_reason": "unmatched_sell",
                "reason_tags": ["unmatched_sell", "attribution_ambiguous"],
                "entry_context": {},
            }
            outcomes.append(outcome)
    if outcome_path is not None:
        _write_jsonl_atomic(outcome_path, outcomes)
    return outcomes


def load_trade_outcomes(path: Path = STOCK_TRADE_OUTCOMES_FILE, *, tail: int | None = None) -> list[dict[str, Any]]:
    return _load_jsonl(path, tail=tail)


def _metric_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    net_values = [_safe_float(row.get("net_pnl")) for row in rows]
    gross_values = [_safe_float(row.get("gross_pnl")) for row in rows]
    fee_values = [_safe_float(row.get("fees_paid")) for row in rows]
    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value < 0]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in net_values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return {
        "trades": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / count if count else 0.0,
        "gross_pnl": sum(gross_values),
        "fees_paid": sum(fee_values),
        "net_pnl": sum(net_values),
        "avg_net_pnl": sum(net_values) / count if count else 0.0,
        "avg_return_pct": sum(_safe_float(row.get("return_pct")) for row in rows) / count if count else 0.0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses else None,
        "max_drawdown_usd": max_drawdown,
    }


def _group_summary(outcomes: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in outcomes:
        grouped.setdefault(str(row.get(key) or "unknown"), []).append(row)
    return {name: _metric_summary(rows) for name, rows in sorted(grouped.items())}


def build_attribution_report(
    outcomes: list[dict[str, Any]],
    *,
    attribution_path: Path | None = STOCK_ATTRIBUTION_FILE,
) -> dict[str, Any]:
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "total": _metric_summary(outcomes),
        "by_strategy": _group_summary(outcomes, "strategy"),
        "by_symbol": _group_summary(outcomes, "symbol"),
        "by_reason": _group_summary(outcomes, "primary_reason"),
        "notes": [
            "Outcomes are realized FIFO round trips from stock_fills.jsonl.",
            "Candidates are research proposals; they are not live-trading approvals.",
        ],
    }
    if attribution_path is not None:
        _write_json_atomic(attribution_path, report)
    return report


def _candidate(
    *,
    action_type: str,
    rationale: str,
    evidence: dict[str, Any],
    param: str = "",
    current_value: Any = None,
    proposed_value: Any = None,
    confidence: float = 0.0,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "research",
        "action_type": action_type,
        "param": param,
        "current_value": current_value,
        "proposed_value": proposed_value,
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "rationale": rationale,
        "evidence": evidence,
        "safety_gate": "requires_replay_walk_forward_paper_and_manual_approval_before_live",
    }
    payload["candidate_id"] = _hash_payload(
        {
            "action_type": action_type,
            "param": param,
            "proposed_value": proposed_value,
            "evidence": evidence,
        },
        length=20,
    )
    return payload


def generate_strategy_candidates(
    report: dict[str, Any],
    *,
    settings: Settings | None = None,
    candidates_path: Path | None = STOCK_STRATEGY_CANDIDATES_FILE,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    total = dict(report.get("total") or {})
    total_trades = int(total.get("trades", 0) or 0)
    if total_trades < 10:
        candidates.append(
            _candidate(
                action_type="collect_more_data",
                rationale="样本量还不足，自动升级只应继续记录和观察。",
                evidence={"trades": total_trades, "minimum_recommended": 10},
                confidence=0.2,
            )
        )

    by_reason = dict(report.get("by_reason") or {})
    fees = dict(by_reason.get("fees_dominated") or {})
    if int(fees.get("trades", 0) or 0) >= 3 and float(fees.get("net_pnl", 0.0) or 0.0) < 0:
        current = getattr(settings, "auto_trader_min_order_value_usd", 500.0) if settings is not None else 500.0
        proposed = max(float(current) * 1.5, float(current) + 250.0)
        candidates.append(
            _candidate(
                action_type="raise_min_order_value",
                param="AUTO_TRADER_MIN_ORDER_VALUE_USD",
                current_value=current,
                proposed_value=round(proposed, 2),
                rationale="多笔交易被费用主导，建议提高最小订单金额，减少低边际小单。",
                evidence=fees,
                confidence=0.55,
            )
        )

    early = dict(by_reason.get("early_exit_or_noise") or {})
    if int(early.get("trades", 0) or 0) >= 3 and float(early.get("net_pnl", 0.0) or 0.0) < 0:
        current = getattr(settings, "auto_trader_min_hold_minutes", 10) if settings is not None else 10
        candidates.append(
            _candidate(
                action_type="raise_min_hold",
                param="AUTO_TRADER_MIN_HOLD_MINUTES",
                current_value=current,
                proposed_value=int(current) + 5,
                rationale="短持仓亏损较多，建议延长最小持有时间，减少噪声退出。",
                evidence=early,
                confidence=0.5,
            )
        )

    by_strategy = dict(report.get("by_strategy") or {})
    strategy_param = {
        "Fusion": "FUSION_ENTRY_SCORE",
        "OFIM": "OFIM_ENTRY_THRESHOLD",
    }
    for strategy, summary in by_strategy.items():
        summary = dict(summary or {})
        trades = int(summary.get("trades", 0) or 0)
        if trades >= 5 and float(summary.get("net_pnl", 0.0) or 0.0) < 0 and float(summary.get("win_rate", 0.0) or 0.0) < 0.4:
            param = strategy_param.get(strategy, "")
            current = getattr(settings, "fusion_entry_score", None) if strategy == "Fusion" and settings is not None else None
            if strategy == "OFIM" and settings is not None:
                current = getattr(settings, "ofim_entry_threshold", None)
            candidates.append(
                _candidate(
                    action_type="tighten_entry_threshold" if param else "review_strategy_allocation",
                    param=param,
                    current_value=current,
                    proposed_value=round(float(current) + 0.05, 4) if current is not None else None,
                    rationale=f"{strategy} 最近样本净亏且胜率偏低，建议先进入 research/paper 验证更严格入场。",
                    evidence={"strategy": strategy, **summary},
                    confidence=0.6 if param else 0.45,
                )
            )

    by_symbol = dict(report.get("by_symbol") or {})
    for symbol, summary in by_symbol.items():
        summary = dict(summary or {})
        if int(summary.get("trades", 0) or 0) >= 3 and float(summary.get("net_pnl", 0.0) or 0.0) < 0:
            candidates.append(
                _candidate(
                    action_type="review_universe_symbol",
                    rationale=f"{symbol} 多笔交易净贡献为负，建议在回放中测试移出或降低权重。",
                    evidence={"symbol": symbol, **summary},
                    confidence=0.4,
                )
            )

    deduped: dict[str, dict[str, Any]] = {candidate["candidate_id"]: candidate for candidate in candidates}
    out = list(deduped.values())
    if candidates_path is not None:
        _write_jsonl_atomic(candidates_path, out)
    return out


def build_promotion_report(
    candidates: list[dict[str, Any]],
    report: dict[str, Any],
    *,
    promotion_path: Path | None = STOCK_PROMOTION_REPORT_FILE,
    min_trades_for_paper: int = 30,
) -> dict[str, Any]:
    total_trades = int(dict(report.get("total") or {}).get("trades", 0) or 0)
    decisions: list[dict[str, Any]] = []
    for candidate in candidates:
        evidence = dict(candidate.get("evidence") or {})
        evidence_trades = int(evidence.get("trades", total_trades) or 0)
        decision = "needs_more_data"
        blockers: list[str] = []
        if total_trades < min_trades_for_paper:
            blockers.append(f"total_trades<{min_trades_for_paper}")
        if evidence_trades < max(5, min_trades_for_paper // 3):
            blockers.append("candidate_evidence_sample_too_small")
        if candidate.get("action_type") == "collect_more_data":
            blockers.append("data_collection_only")
        if not blockers:
            decision = "eligible_for_paper_replay"
        decisions.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "action_type": candidate.get("action_type"),
                "decision": decision,
                "blockers": blockers,
                "live_allowed": False,
                "paper_allowed": decision == "eligible_for_paper_replay",
            }
        )
    promotion = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": {
            "live_auto_promotion": False,
            "paper_auto_promotion": True,
            "required_before_live": [
                "walk_forward_replay",
                "purged_or_embargoed_validation",
                "cost_slippage_stress",
                "paper_trading",
                "manual_approval",
            ],
        },
        "total_trades": total_trades,
        "decisions": decisions,
    }
    if promotion_path is not None:
        _write_json_atomic(promotion_path, promotion)
    return promotion


def _top_outcomes(outcomes: list[dict[str, Any]], *, reverse: bool, limit: int = 10) -> list[dict[str, Any]]:
    rows = sorted(outcomes, key=lambda row: _safe_float(row.get("net_pnl")), reverse=reverse)[:limit]
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "outcome_id": row.get("outcome_id", ""),
                "symbol": row.get("symbol", ""),
                "strategy": row.get("strategy", ""),
                "entry_ts": row.get("entry_ts", ""),
                "exit_ts": row.get("exit_ts", ""),
                "quantity": row.get("quantity", 0),
                "net_pnl": row.get("net_pnl", 0.0),
                "return_pct": row.get("return_pct", 0.0),
                "primary_reason": row.get("primary_reason", ""),
                "reason_tags": row.get("reason_tags", []),
            }
        )
    return out


def _markdown_metric_table(rows: dict[str, Any]) -> str:
    keys = [
        "trades",
        "wins",
        "losses",
        "win_rate",
        "gross_pnl",
        "fees_paid",
        "net_pnl",
        "avg_net_pnl",
        "avg_return_pct",
        "profit_factor",
        "max_drawdown_usd",
    ]
    lines = ["| metric | value |", "| --- | --- |"]
    for key in keys:
        if key in rows:
            lines.append(f"| {key} | {rows.get(key)} |")
    return "\n".join(lines)


def _markdown_group_table(grouped: dict[str, Any], *, name_column: str, limit: int = 12) -> str:
    lines = [f"| {name_column} | trades | win_rate | net_pnl | fees_paid | max_drawdown_usd |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for name, summary in list(grouped.items())[:limit]:
        summary = dict(summary or {})
        lines.append(
            "| "
            + " | ".join(
                [
                    str(name),
                    str(summary.get("trades", 0)),
                    str(round(_safe_float(summary.get("win_rate")), 4)),
                    str(round(_safe_float(summary.get("net_pnl")), 6)),
                    str(round(_safe_float(summary.get("fees_paid")), 6)),
                    str(round(_safe_float(summary.get("max_drawdown_usd")), 6)),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _candidate_markdown(candidates: list[dict[str, Any]], promotion: dict[str, Any]) -> str:
    decisions = {
        str(row.get("candidate_id")): row for row in promotion.get("decisions", []) if isinstance(row, dict)
    }
    lines = [
        "| candidate_id | action | param | proposed | confidence | gate | rationale |",
        "| --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for item in candidates[:20]:
        decision = decisions.get(str(item.get("candidate_id")), {})
        rationale = str(item.get("rationale", "")).replace("\n", " ")[:180]
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item.get("candidate_id", "")),
                    str(item.get("action_type", "")),
                    str(item.get("param", "")),
                    str(item.get("proposed_value", "")),
                    str(item.get("confidence", "")),
                    str(decision.get("decision", "unreviewed")),
                    rationale,
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _render_review_packet_markdown(packet: dict[str, Any]) -> str:
    summary = dict(packet.get("summary") or {})
    artifacts = dict(packet.get("artifacts") or {})
    gates = dict(packet.get("approval_policy") or {})
    report = dict(packet.get("attribution_report") or {})
    candidates = list(packet.get("strategy_candidates") or [])
    promotion = dict(packet.get("promotion_report") or {})
    evidence = dict(packet.get("evidence") or {})

    lines = [
        "# Stock Strategy Learning Review Packet",
        "",
        f"- generated_at: `{packet.get('generated_at', '')}`",
        f"- packet_id: `{packet.get('packet_id', '')}`",
        f"- live_auto_promotion: `{gates.get('live_auto_promotion', False)}`",
        "- 结论：本文件只记录系统学到的证据和候选建议，不允许直接修改代码或实盘参数。",
        "",
        "## How to Use With Codex",
        "",
        str(packet.get("codex_review_prompt", "")),
        "",
        "## Summary",
        "",
        _markdown_metric_table(summary),
        "",
        "## Evidence Counts",
        "",
        f"- order_memory_records: `{evidence.get('order_memory_records', 0)}`",
        f"- realized_outcomes: `{evidence.get('realized_outcomes', 0)}`",
        f"- candidate_count: `{evidence.get('candidate_count', 0)}`",
        "",
        "## Strategy Attribution",
        "",
        _markdown_group_table(dict(report.get("by_strategy") or {}), name_column="strategy"),
        "",
        "## Reason Attribution",
        "",
        _markdown_group_table(dict(report.get("by_reason") or {}), name_column="reason"),
        "",
        "## Symbol Attribution",
        "",
        _markdown_group_table(dict(report.get("by_symbol") or {}), name_column="symbol"),
        "",
        "## Strategy Candidates",
        "",
        _candidate_markdown(candidates, promotion) if candidates else "No candidates.",
        "",
        "## Biggest Winners",
        "",
        "```json",
        json.dumps(packet.get("top_winners", []), ensure_ascii=False, indent=2, default=_json_default),
        "```",
        "",
        "## Biggest Losers",
        "",
        "```json",
        json.dumps(packet.get("top_losers", []), ensure_ascii=False, indent=2, default=_json_default),
        "```",
        "",
        "## Artifacts",
        "",
        "| artifact | path | bytes | sha256 |",
        "| --- | --- | ---: | --- |",
    ]
    for name, meta in artifacts.items():
        meta = dict(meta or {})
        lines.append(f"| {name} | `{meta.get('path', '')}` | {meta.get('bytes', 0)} | `{meta.get('sha256', '')}` |")
    lines.extend(
        [
            "",
            "## Review Checklist",
            "",
            "- 样本量是否足够，是否存在单日/单标的偶然性。",
            "- 候选是否只是拟合过去噪声，是否需要 purged/embargoed walk-forward 验证。",
            "- 成本、滑点、成交失败和 partial fill 是否已经纳入。",
            "- 是否先进入 replay/paper，而不是直接修改 live 策略。",
            "- 如果需要改代码，只改股票系统，不能改 crypto 系统。",
            "",
        ]
    )
    return "\n".join(lines)


def build_learning_review_packet(
    report: dict[str, Any],
    candidates: list[dict[str, Any]],
    promotion: dict[str, Any],
    outcomes: list[dict[str, Any]],
    *,
    order_memory_path: Path = STOCK_ORDER_MEMORY_FILE,
    outcomes_path: Path = STOCK_TRADE_OUTCOMES_FILE,
    attribution_path: Path = STOCK_ATTRIBUTION_FILE,
    candidates_path: Path = STOCK_STRATEGY_CANDIDATES_FILE,
    promotion_path: Path = STOCK_PROMOTION_REPORT_FILE,
    review_packet_path: Path | None = STOCK_LEARNING_REVIEW_PACKET_FILE,
    review_packet_json_path: Path | None = STOCK_LEARNING_REVIEW_PACKET_JSON_FILE,
) -> dict[str, Any]:
    order_memory = load_order_memory(order_memory_path)
    total = dict(report.get("total") or {})
    generated_at = datetime.now(UTC).isoformat()
    packet_seed = {
        "generated_at": generated_at,
        "summary": total,
        "candidates": [candidate.get("candidate_id") for candidate in candidates],
        "artifact_hashes": {
            "order_memory": _file_sha256(order_memory_path),
            "outcomes": _file_sha256(outcomes_path),
            "attribution": _file_sha256(attribution_path),
            "candidates": _file_sha256(candidates_path),
            "promotion": _file_sha256(promotion_path),
        },
    }
    packet = {
        "schema_version": 1,
        "record_type": "stock_learning_review_packet",
        "generated_at": generated_at,
        "packet_id": _hash_payload(packet_seed, length=20),
        "approval_policy": {
            "code_auto_modification": False,
            "live_auto_promotion": False,
            "review_required_before_code_change": True,
            "manual_reviewer": "human+Codex",
            "allowed_next_stage": "replay_or_paper_only",
        },
        "codex_review_prompt": (
            "请基于这份股票策略学习审阅包，评估候选改动是否值得修改代码。"
            "请先检查样本量、成本/滑点、过拟合风险、候选证据和 promotion gate；"
            "如果建议改代码，请限定在股票交易系统，不要改 crypto 系统。"
        ),
        "summary": total,
        "evidence": {
            "order_memory_records": len(order_memory),
            "realized_outcomes": len(outcomes),
            "candidate_count": len(candidates),
        },
        "attribution_report": report,
        "strategy_candidates": candidates,
        "promotion_report": promotion,
        "top_winners": _top_outcomes(outcomes, reverse=True),
        "top_losers": _top_outcomes(outcomes, reverse=False),
        "recent_order_memory": order_memory[-20:],
        "artifacts": {
            "order_memory": _artifact_meta(order_memory_path),
            "outcomes": _artifact_meta(outcomes_path),
            "attribution": _artifact_meta(attribution_path),
            "candidates": _artifact_meta(candidates_path),
            "promotion": _artifact_meta(promotion_path),
        },
        "limitations": [
            "This packet describes observed realized outcomes; it does not prove causality.",
            "Small samples and repeated strategy searches can create backtest overfitting.",
            "Live changes require replay, sample-out validation, paper trading and manual approval.",
        ],
    }
    if review_packet_json_path is not None:
        _write_json_atomic(review_packet_json_path, packet)
    if review_packet_path is not None:
        _write_text_atomic(review_packet_path, _render_review_packet_markdown(packet))
    return packet


def run_learning_pipeline(
    *,
    fills_path: Path = STOCK_FILLS_FILE,
    order_memory_path: Path = STOCK_ORDER_MEMORY_FILE,
    epoch_path: Path | None = STOCK_LEDGER_EPOCH_FILE,
    settings: Settings | None = None,
    outcomes_path: Path = STOCK_TRADE_OUTCOMES_FILE,
    attribution_path: Path = STOCK_ATTRIBUTION_FILE,
    candidates_path: Path = STOCK_STRATEGY_CANDIDATES_FILE,
    promotion_path: Path = STOCK_PROMOTION_REPORT_FILE,
    review_packet_path: Path = STOCK_LEARNING_REVIEW_PACKET_FILE,
    review_packet_json_path: Path = STOCK_LEARNING_REVIEW_PACKET_JSON_FILE,
) -> LearningPipelineResult:
    outcomes = build_trade_outcomes(
        fills_path=fills_path,
        order_memory_path=order_memory_path,
        epoch_path=epoch_path,
        outcome_path=outcomes_path,
    )
    report = build_attribution_report(outcomes, attribution_path=attribution_path)
    candidates = generate_strategy_candidates(report, settings=settings, candidates_path=candidates_path)
    promotion = build_promotion_report(candidates, report, promotion_path=promotion_path)
    build_learning_review_packet(
        report,
        candidates,
        promotion,
        outcomes,
        order_memory_path=order_memory_path,
        outcomes_path=outcomes_path,
        attribution_path=attribution_path,
        candidates_path=candidates_path,
        promotion_path=promotion_path,
        review_packet_path=review_packet_path,
        review_packet_json_path=review_packet_json_path,
    )
    return LearningPipelineResult(
        outcome_count=len(outcomes),
        candidate_count=len(candidates),
        attribution_path=attribution_path,
        outcomes_path=outcomes_path,
        candidates_path=candidates_path,
        promotion_path=promotion_path,
        review_packet_path=review_packet_path,
        review_packet_json_path=review_packet_json_path,
    )


def load_learning_report(path: Path = STOCK_ATTRIBUTION_FILE) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_strategy_candidates(path: Path = STOCK_STRATEGY_CANDIDATES_FILE, *, tail: int | None = None) -> list[dict[str, Any]]:
    return _load_jsonl(path, tail=tail)


def load_promotion_report(path: Path = STOCK_PROMOTION_REPORT_FILE) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_learning_review_packet(path: Path = STOCK_LEARNING_REVIEW_PACKET_JSON_FILE) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
