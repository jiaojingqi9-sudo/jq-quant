from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time as dt_time, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Settings, load_settings
from .cascade_sleeve import generate_live_cascade_plan
from .costs import broker_fee_total_from_row, build_trade_cost_model, estimate_trade_cost
from .fusion_intraday import FusionIntradayStrategy
from .ofim_intraday import OfimIntradayStrategy
from .futu_gateway import FutuPaperTrader, FutuTradeError, FutuTransientError, PlannedOrder
from .stock_events import append_stock_event
from .stock_runtime import (
    STOCK_FILLS_FILE,
    STOCK_LEDGER_EPOCH_FILE,
    append_stock_fill,
    load_stock_ledger_epoch,
    load_recorded_stock_fill_ids,
    load_stock_order_fill_cumulatives,
)
from .stock_ledger import build_stock_double_entry_ledger, reconcile_stock_ledger
from .stock_learning import append_order_memory
from . import describe_build, market_logger
from .strategy_experiment import strategy_symbol_sets
from .strategy_stack import (
    baseline_sleeve_enabled,
    effective_fusion_settings,
    fetch_futu_daily_closes,
    scaled_baseline_target_weights,
    stack_allocations,
    stack_label,
    stack_target_weights,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime"
AUTO_TRADER_STATUS_FILE = RUNTIME_DIR / "auto_trader_status.json"
AUTO_TRADER_PID_FILE = RUNTIME_DIR / "auto_trader.pid"
AUTO_TRADER_LOG_FILE = RUNTIME_DIR / "auto_trader.log"
AUTO_TRADER_STATE_FILE = RUNTIME_DIR / "auto_trader_state.json"


@dataclass
class AutoTraderState:
    last_signature: str = ""
    last_submit_at: datetime | None = None
    # Cascade is a daily strategy — cache its plan so it only recomputes once per
    # trading day instead of every 60-second cycle.  Reusing the plan also prevents
    # intraday oscillation caused by incomplete daily bars returned by Futu K-lines.
    cascade_plan: object = None   # CascadeSleevePlan | None
    cascade_plan_date: str = ""   # "YYYY-MM-DD" in market timezone
    # Track when each symbol was last bought so we can enforce a minimum hold time.
    # This prevents OFIM from entering a position and exiting it 60 seconds later.
    position_entry_times: dict = None  # code → datetime (UTC) of last BUY submission
    # Persistent OFIM instance so prev_order_books is maintained across polling cycles.
    # Kept on state (not as a module global) so AutoTraderState is fully self-contained
    # and multiple concurrent traders or tests don't share state accidentally.
    ofim_strategy: OfimIntradayStrategy | None = None
    recorded_fill_ids: set[str] | None = None
    recorded_order_fill_qty: dict[str, float] | None = None
    recorded_order_fill_notional: dict[str, float] | None = None
    recorded_order_fill_fees: dict[str, float] | None = None
    submitted_order_sources: dict[str, str] | None = None
    exit_signal_counts: dict[str, int] | None = None
    last_symbol_trade_time: dict[str, datetime] | None = None
    last_cycle_id: str = ""
    last_target_weights: dict[str, float] | None = None
    # last-cycle counters (reset to 0 at the start of every run_cycle).
    # These intentionally describe only the most recent cycle so the dashboard
    # and the status JSON can distinguish "this cycle's activity" from "all
    # activity since epoch". Cumulative counters live in the *_cumulative
    # fields below.
    last_planned_order_count: int = 0
    last_submitted_order_count: int = 0
    last_recorded_fill_count: int = 0
    # Cumulative counters since the auto_trader started (or since process
    # restart). They are NOT reset across cycles — only when the process
    # restarts. For long-running counters across restarts use the audit
    # ledger / fills journal which is persisted to disk.
    cumulative_planned_orders: int = 0
    cumulative_submitted_orders: int = 0
    cumulative_recorded_fills: int = 0
    # Consecutive transient_error counter for OpenD half-dead short-circuit.
    # Increments every cycle that ends in transient_error / transient FutuTradeError;
    # resets to 0 on any cycle that completes successfully (action != error/transient).
    # When this reaches settings.auto_trader_max_consecutive_transient, the main
    # loop emits an opend_lockdown status and skips the next cycle's run_cycle()
    # entirely so we never submit orders during a half-dead connection.
    consecutive_transient_count: int = 0

    def __post_init__(self):
        if self.position_entry_times is None:
            object.__setattr__(self, "position_entry_times", {})
        if self.recorded_fill_ids is None:
            object.__setattr__(self, "recorded_fill_ids", set())
        if self.recorded_order_fill_qty is None:
            object.__setattr__(self, "recorded_order_fill_qty", {})
        if self.recorded_order_fill_notional is None:
            object.__setattr__(self, "recorded_order_fill_notional", {})
        if self.recorded_order_fill_fees is None:
            object.__setattr__(self, "recorded_order_fill_fees", {})
        if self.submitted_order_sources is None:
            object.__setattr__(self, "submitted_order_sources", {})
        if self.exit_signal_counts is None:
            object.__setattr__(self, "exit_signal_counts", {})
        if self.last_symbol_trade_time is None:
            object.__setattr__(self, "last_symbol_trade_time", {})
        if self.last_target_weights is None:
            object.__setattr__(self, "last_target_weights", {})


def _is_transient_runtime_error(message: object) -> bool:
    return FutuPaperTrader.is_transient_error(message)


def _log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] {message}", flush=True)


def _parse_hhmm(raw: str) -> dt_time:
    hour_str, minute_str = raw.split(":", 1)
    return dt_time(hour=int(hour_str), minute=int(minute_str))


def _market_window_state(now_utc: datetime, settings: Settings) -> tuple[bool, str]:
    market_now = _market_now(now_utc, settings)
    if market_now.weekday() >= 5:
        return False, f"weekend ({market_now:%Y-%m-%d %H:%M:%S %Z})"

    start_time = _parse_hhmm(settings.auto_trader_start_time)
    end_time = _parse_hhmm(settings.auto_trader_end_time)
    if start_time <= market_now.time() <= end_time:
        return True, f"inside_window ({market_now:%Y-%m-%d %H:%M:%S %Z})"
    return False, f"outside_window ({market_now:%Y-%m-%d %H:%M:%S %Z})"


def _market_now(now_utc: datetime, settings: Settings) -> datetime:
    return now_utc.astimezone(ZoneInfo(settings.auto_trader_market_timezone))


def _market_day_bounds(now_utc: datetime, settings: Settings) -> tuple[str, str]:
    market_date = _market_now(now_utc, settings).date().isoformat()
    return market_date, market_date


def _filled_orders(order_history: pd.DataFrame) -> pd.DataFrame:
    if order_history.empty:
        return order_history
    rows = order_history.copy()
    rows["dealt_qty_num"] = pd.to_numeric(rows.get("dealt_qty"), errors="coerce").fillna(0.0)
    rows["dealt_price_num"] = pd.to_numeric(rows.get("dealt_avg_price"), errors="coerce").fillna(0.0)
    rows = rows[rows["dealt_qty_num"] > 0].copy()
    if rows.empty:
        return rows
    sort_columns = [column for column in ["updated_time", "create_time"] if column in rows.columns]
    if sort_columns:
        rows = rows.sort_values(sort_columns, ascending=True)
    return rows


def _position_quantity(positions: pd.DataFrame, code: str, column: str = "qty") -> int:
    if positions.empty or column not in positions.columns or "code" not in positions.columns:
        return 0
    rows = positions[positions["code"] == code]
    if rows.empty:
        return 0
    return int(pd.to_numeric(rows[column], errors="coerce").fillna(0.0).sum())


def _order_signature(orders: list[PlannedOrder]) -> str:
    normalized = [f"{order.code}|{order.side}|{order.quantity}|{order.limit_price:.4f}" for order in orders]
    return ";".join(sorted(normalized))


def _with_strategy_source(order: PlannedOrder, strategy_source: str) -> PlannedOrder:
    return replace(order, strategy_source=strategy_source)


def _risk_adjust_target_weights(
    target_weights: dict[str, float],
    settings: Settings,
    *,
    cycle_id: str | None = None,
) -> dict[str, float]:
    """Apply hard pre-trade exposure limits to strategy target weights."""
    cleaned: dict[str, float] = {}
    dropped: dict[str, object] = {}
    for code, raw_weight in target_weights.items():
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            dropped[str(code)] = raw_weight
            continue
        if not str(code).strip() or not math.isfinite(weight) or weight <= 0:
            if weight != 0:
                dropped[str(code)] = raw_weight
            continue
        cleaned[str(code)] = weight

    if dropped:
        append_stock_event("risk_target_dropped", {"targets": dropped}, cycle_id=cycle_id)

    max_weight = float(getattr(settings, "auto_trader_max_target_weight", 1.0) or 1.0)
    capped: dict[str, dict[str, float]] = {}
    if max_weight > 0:
        for code, weight in list(cleaned.items()):
            if weight > max_weight:
                capped[code] = {"raw": weight, "capped": max_weight}
                cleaned[code] = max_weight
    if capped:
        append_stock_event("risk_target_weight_capped", {"targets": capped}, cycle_id=cycle_id)

    gross_cap = float(getattr(settings, "auto_trader_max_target_gross_exposure", 1.0) or 1.0)
    gross = sum(cleaned.values())
    if gross_cap > 0 and gross > gross_cap + 1e-9:
        scale = gross_cap / gross
        cleaned = {
            code: round(weight * scale, 6)
            for code, weight in cleaned.items()
            if weight * scale > 0
        }
        append_stock_event(
            "risk_target_gross_scaled",
            {"raw_gross": gross, "cap": gross_cap, "scale": scale},
            cycle_id=cycle_id,
        )

    return cleaned


def _order_notional(order: PlannedOrder) -> float:
    return max(0.0, float(order.quantity) * float(order.limit_price))


def _apply_cycle_turnover_cap(
    orders: list[PlannedOrder],
    settings: Settings,
    *,
    cycle_id: str | None = None,
) -> list[PlannedOrder]:
    cap = float(getattr(settings, "auto_trader_max_cycle_turnover_usd", 0.0) or 0.0)
    if cap <= 0 or not orders:
        return orders

    retained: list[PlannedOrder] = []
    skipped: list[dict[str, object]] = []
    used = 0.0
    for order in orders:
        notional = _order_notional(order)
        is_full_exit = order.side == "SELL" and float(order.target_weight) <= 0
        if is_full_exit:
            retained.append(order)
            continue
        if used + notional <= cap + 1e-9:
            retained.append(order)
            used += notional
            continue
        skipped.append(
            {
                "code": order.code,
                "side": order.side,
                "quantity": order.quantity,
                "notional": round(notional, 2),
                "used": round(used, 2),
                "cap": round(cap, 2),
            }
        )
    if skipped:
        append_stock_event(
            "risk_cycle_turnover_skip",
            {"skipped": skipped, "used": round(used, 2), "cap": round(cap, 2)},
            cycle_id=cycle_id,
        )
    return retained


def _loss_guard_breached(
    account: pd.Series,
    settings: Settings,
    *,
    cycle_id: str | None = None,
) -> tuple[bool, str]:
    max_loss_usd = float(getattr(settings, "auto_trader_max_epoch_loss_usd", 0.0) or 0.0)
    max_loss_pct = float(getattr(settings, "auto_trader_max_epoch_loss_pct", 0.0) or 0.0)
    if max_loss_usd <= 0 and max_loss_pct <= 0:
        return False, ""
    epoch = load_stock_ledger_epoch(STOCK_LEDGER_EPOCH_FILE)
    snapshot = dict(epoch.get("account_snapshot") or {}) if isinstance(epoch, dict) else {}
    start_assets = float(snapshot.get("total_assets", 0.0) or 0.0)
    current_assets = float(account.get("total_assets", 0.0) or 0.0)
    if start_assets <= 0 or current_assets <= 0:
        return False, ""
    loss = max(0.0, start_assets - current_assets)
    loss_pct = loss / start_assets
    reasons: list[str] = []
    if max_loss_usd > 0 and loss >= max_loss_usd:
        reasons.append(f"loss_usd={loss:.2f}>={max_loss_usd:.2f}")
    if max_loss_pct > 0 and loss_pct >= max_loss_pct:
        reasons.append(f"loss_pct={loss_pct:.4%}>={max_loss_pct:.4%}")
    if not reasons:
        return False, ""
    detail = "; ".join(reasons)
    append_stock_event(
        "risk_loss_limit_breached",
        {"start_assets": start_assets, "current_assets": current_assets, "loss": loss, "loss_pct": loss_pct, "detail": detail},
        cycle_id=cycle_id,
    )
    return True, detail


def _parse_datetime(raw: object) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        try:
            parsed_ts = pd.Timestamp(raw)
        except (TypeError, ValueError):
            return None
        if pd.isna(parsed_ts):
            return None
        parsed = parsed_ts.to_pydatetime()
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _state_datetime_map(payload: dict[str, Any]) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for code, raw in payload.items():
        parsed = _parse_datetime(raw)
        if parsed is not None:
            out[str(code)] = parsed
    return out


def _state_float_map(payload: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, raw in payload.items():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            out[str(key)] = max(0.0, value)
    return out


def _load_state() -> AutoTraderState:
    state = AutoTraderState()
    if AUTO_TRADER_STATE_FILE.exists():
        try:
            payload = json.loads(AUTO_TRADER_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            state.last_signature = str(payload.get("last_signature") or "")
            state.last_submit_at = _parse_datetime(payload.get("last_submit_at"))
            state.position_entry_times.update(_state_datetime_map(dict(payload.get("position_entry_times") or {})))
            state.submitted_order_sources.update(
                {
                    str(order_id): str(source)
                    for order_id, source in dict(payload.get("submitted_order_sources") or {}).items()
                    if str(order_id).strip()
                }
            )
            state.exit_signal_counts.update(
                {
                    str(code): max(0, int(count))
                    for code, count in dict(payload.get("exit_signal_counts") or {}).items()
                    if str(code).strip()
                }
            )
            state.recorded_order_fill_qty.update(
                _state_float_map(dict(payload.get("recorded_order_fill_qty") or {}))
            )
            state.recorded_order_fill_notional.update(
                _state_float_map(dict(payload.get("recorded_order_fill_notional") or {}))
            )
            state.recorded_order_fill_fees.update(
                _state_float_map(dict(payload.get("recorded_order_fill_fees") or {}))
            )
            state.last_symbol_trade_time.update(_state_datetime_map(dict(payload.get("last_symbol_trade_time") or {})))
            # Cumulative counters survive auto_trader process restarts so the
            # dashboard's "since last reset" number is honest. They reset to 0
            # only when the user explicitly runs stock-system-reset (which
            # rewrites the ledger epoch — see write_stock_ledger_epoch).
            state.cumulative_planned_orders = max(0, int(payload.get("cumulative_planned_orders") or 0))
            state.cumulative_submitted_orders = max(0, int(payload.get("cumulative_submitted_orders") or 0))
            state.cumulative_recorded_fills = max(0, int(payload.get("cumulative_recorded_fills") or 0))
            state.consecutive_transient_count = max(0, int(payload.get("consecutive_transient_count") or 0))
    state.recorded_fill_ids.update(load_recorded_stock_fill_ids(STOCK_FILLS_FILE))
    for order_id, totals in load_stock_order_fill_cumulatives(STOCK_FILLS_FILE).items():
        state.recorded_order_fill_qty[order_id] = max(
            state.recorded_order_fill_qty.get(order_id, 0.0),
            float(totals.get("quantity", 0.0) or 0.0),
        )
        state.recorded_order_fill_notional[order_id] = max(
            state.recorded_order_fill_notional.get(order_id, 0.0),
            float(totals.get("notional", 0.0) or 0.0),
        )
        state.recorded_order_fill_fees[order_id] = max(
            state.recorded_order_fill_fees.get(order_id, 0.0),
            float(totals.get("fee", 0.0) or 0.0),
        )
    return state


def _save_state(state: AutoTraderState) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_signature": state.last_signature,
            "last_submit_at": state.last_submit_at.isoformat() if state.last_submit_at else None,
            "position_entry_times": {
                code: ts.isoformat() for code, ts in sorted(state.position_entry_times.items())
            },
            "submitted_order_sources": dict(sorted(state.submitted_order_sources.items())),
            "recorded_order_fill_qty": dict(sorted(state.recorded_order_fill_qty.items())),
            "recorded_order_fill_notional": dict(sorted(state.recorded_order_fill_notional.items())),
            "recorded_order_fill_fees": dict(sorted(state.recorded_order_fill_fees.items())),
            "exit_signal_counts": dict(sorted(state.exit_signal_counts.items())),
            "last_symbol_trade_time": {
                code: ts.isoformat() for code, ts in sorted(state.last_symbol_trade_time.items())
            },
            # Cumulative counters survive process restarts; see _load_state.
            "cumulative_planned_orders": state.cumulative_planned_orders,
            "cumulative_submitted_orders": state.cumulative_submitted_orders,
            "cumulative_recorded_fills": state.cumulative_recorded_fills,
            "consecutive_transient_count": state.consecutive_transient_count,
        }
        tmp = AUTO_TRADER_STATE_FILE.with_suffix(AUTO_TRADER_STATE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(AUTO_TRADER_STATE_FILE)
    except Exception:
        return


def _row_scalar(row: pd.Series, column: str, default: object = "") -> object:
    value = row.get(column, default)
    if isinstance(value, pd.Series):
        non_null = value.dropna()
        return non_null.iloc[-1] if not non_null.empty else default
    return value


def _normalize_side(raw: object) -> str:
    text = str(raw or "").upper()
    if "BUY" in text:
        return "BUY"
    if "SELL" in text:
        return "SELL"
    return text


def _fill_event_id(row: pd.Series, *, cumulative_quantity: float | None = None) -> str:
    order_id = str(_row_scalar(row, "order_id", "") or "").strip()
    if order_id:
        if cumulative_quantity is not None:
            return f"futu_fill:{order_id}:{cumulative_quantity:.8f}"
        return f"futu_order:{order_id}"
    payload = "|".join(
        str(_row_scalar(row, column, ""))
        for column in ("code", "trd_side", "dealt_qty_num", "dealt_price_num", "updated_time", "create_time")
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"futu_order:{digest}"


def _record_new_fills(
    trader: FutuPaperTrader,
    acc_id: int,
    settings: Settings,
    state: AutoTraderState,
    *,
    now_utc: datetime | None = None,
    cycle_id: str | None = None,
) -> int:
    now_utc = now_utc or datetime.now(UTC)
    market_date = _market_now(now_utc, settings).date()
    start = (market_date - timedelta(days=1)).isoformat()
    end = market_date.isoformat()
    try:
        history = trader.get_order_history(acc_id, start, end)
    except Exception as exc:
        append_stock_event("fill_sync_error", {"detail": f"{type(exc).__name__}: {exc}"}, cycle_id=cycle_id)
        return 0
    filled = _filled_orders(history)
    if filled.empty:
        return 0

    model = build_trade_cost_model(settings)
    recorded = 0
    for _, row in filled.iterrows():
        symbol = str(_row_scalar(row, "code", "") or "").strip()
        side = _normalize_side(_row_scalar(row, "trd_side", ""))
        cumulative_qty = float(_row_scalar(row, "dealt_qty_num", 0.0) or 0.0)
        cumulative_price = float(_row_scalar(row, "dealt_price_num", 0.0) or 0.0)
        if not symbol or side not in {"BUY", "SELL"} or cumulative_qty <= 0 or cumulative_price <= 0:
            continue
        cumulative_notional = cumulative_qty * cumulative_price
        ts = _row_scalar(row, "updated_time", _row_scalar(row, "create_time", now_utc.isoformat()))
        broker_fee = broker_fee_total_from_row(row)
        order_id = str(_row_scalar(row, "order_id", "") or "").strip()
        if order_id:
            previous_qty = float(state.recorded_order_fill_qty.get(order_id, 0.0) or 0.0)
            previous_notional = float(state.recorded_order_fill_notional.get(order_id, 0.0) or 0.0)
            previous_fee = float(state.recorded_order_fill_fees.get(order_id, 0.0) or 0.0)
            quantity = cumulative_qty - previous_qty
            if quantity <= 1e-9:
                continue
            incremental_notional = cumulative_notional - previous_notional
            price = incremental_notional / quantity if incremental_notional > 0 else cumulative_price
            broker_fee_delta = None
            if broker_fee is not None:
                broker_fee_delta = max(0.0, float(broker_fee) - previous_fee)
            event_id = _fill_event_id(row, cumulative_quantity=cumulative_qty)
        else:
            quantity = cumulative_qty
            price = cumulative_price
            broker_fee_delta = broker_fee
            event_id = _fill_event_id(row)
        if not event_id or event_id in state.recorded_fill_ids or quantity <= 0 or price <= 0:
            continue
        breakdown = estimate_trade_cost(
            side,
            quantity,
            price,
            timestamp=ts,
            model=model,
            broker_fee_total=broker_fee_delta,
        )
        record = {
            "ts": str(ts or now_utc.isoformat()),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "fee": float(breakdown.total),
            "fee_source": "broker_reported" if broker_fee_delta is not None else breakdown.source,
            "event_id": event_id,
            "order_id": order_id,
            "cumulative_quantity": cumulative_qty,
            "cumulative_notional": cumulative_notional,
            "strategy": state.submitted_order_sources.get(order_id, ""),
            "source": "futu_order_history",
        }
        append_stock_fill(record, fills_path=STOCK_FILLS_FILE)
        state.recorded_fill_ids.add(event_id)
        if order_id:
            state.recorded_order_fill_qty[order_id] = max(
                cumulative_qty,
                float(state.recorded_order_fill_qty.get(order_id, 0.0) or 0.0) + quantity,
            )
            state.recorded_order_fill_notional[order_id] = max(
                cumulative_notional,
                float(state.recorded_order_fill_notional.get(order_id, 0.0) or 0.0) + quantity * price,
            )
            state.recorded_order_fill_fees[order_id] = (
                float(state.recorded_order_fill_fees.get(order_id, 0.0) or 0.0) + float(breakdown.total)
            )
        recorded += 1
    if recorded:
        state.last_recorded_fill_count = recorded
        state.cumulative_recorded_fills += recorded
        append_stock_event("fills_recorded", {"count": recorded}, cycle_id=cycle_id)
        _save_state(state)
    return recorded


def _record_ledger_reconciliation(positions: pd.DataFrame, *, cycle_id: str | None = None) -> None:
    try:
        epoch = load_stock_ledger_epoch(STOCK_LEDGER_EPOCH_FILE)
        projection = build_stock_double_entry_ledger(STOCK_FILLS_FILE, epoch_path=STOCK_LEDGER_EPOCH_FILE)
        reconciliation = reconcile_stock_ledger(projection, positions=positions, epoch=epoch)
    except Exception as exc:
        append_stock_event("ledger_reconciliation_error", {"detail": f"{type(exc).__name__}: {exc}"}, cycle_id=cycle_id)
        return
    if not reconciliation.ok:
        append_stock_event(
            "ledger_reconciliation_break",
            {
                "journal_hash": projection.journal_hash,
                "breaks": [break_item.__dict__ for break_item in reconciliation.breaks],
            },
            cycle_id=cycle_id,
        )


def _oldest_open_order_age(open_orders: pd.DataFrame, now_utc: datetime) -> timedelta | None:
    """Return the age of the oldest open order, or None if timestamps are unavailable.

    Falls back from ``create_time`` to ``updated_time`` if the first column is
    absent or entirely unparseable.  Returns ``None`` only when no usable
    timestamp exists at all — callers treat that as "assume stale".
    """
    for col in ("create_time", "updated_time"):
        if col not in open_orders.columns:
            continue
        try:
            times = pd.to_datetime(open_orders[col], errors="coerce", utc=True)
            oldest = times.dropna().min()
            if not pd.isna(oldest):
                return now_utc - oldest.to_pydatetime()
        except Exception:
            continue
    return None


def _strategy_stack_target_weights(
    settings: Settings,
    trader: FutuPaperTrader,
    now_utc: datetime,
    positions: pd.DataFrame,
    state: "AutoTraderState | None" = None,
) -> tuple[dict[str, float], dict[str, object], dict[str, str]]:
    baseline_weight, fusion_weight, ofim_weight, cascade_weight, _reserve_weight = stack_allocations(settings)
    fusion_settings = effective_fusion_settings(settings)

    baseline_weights: dict[str, float] = {}
    diagnostics: dict[str, object] = {}
    if baseline_sleeve_enabled(settings) and baseline_weight > 0:
        baseline_start = max(
            pd.Timestamp(settings.start_date).date(),
            (_market_now(now_utc, settings).date() - timedelta(days=max(730, settings.lookback_months * 45))),
        ).isoformat()
        baseline_prices = fetch_futu_daily_closes(
            trader,
            settings.symbols,
            start=baseline_start,
        )
        baseline_weights = scaled_baseline_target_weights(
            baseline_prices,
            settings,
            reference_date=_market_now(now_utc, settings).date(),
        )

    scaled_fusion_weights: dict[str, float] = {}
    if fusion_weight > 0:
        fusion_positions = positions
        fusion_symbols = set(fusion_settings.fusion_universe)
        if not fusion_positions.empty and fusion_symbols:
            fusion_positions = fusion_positions[fusion_positions["code"].isin(fusion_symbols)].copy()
        held_symbols = set(fusion_positions["code"].tolist()) if not fusion_positions.empty else set()
        fusion_plan = FusionIntradayStrategy(fusion_settings).generate_plan(trader, held_symbols)
        diagnostics["fusion_benchmark_score"] = round(float(fusion_plan.benchmark_score), 6)
        diagnostics["fusion_targets"] = tuple(sorted(fusion_plan.target_weights))
        scaled_fusion_weights = {code: round(weight * fusion_weight, 6) for code, weight in fusion_plan.target_weights.items()}

    scaled_ofim_weights: dict[str, float] = {}
    if ofim_weight > 0 and (fusion_settings.ofim_universe or fusion_settings.ofim_crypto_universe):
        # Build the full set of OFIM-relevant symbols: equity universe + crypto proxies
        # (proxy ETFs are what Futu holds on behalf of crypto positions).
        ofim_equity_symbols = set(fusion_settings.ofim_universe)
        crypto_proxy_map: dict[str, str] = dict(fusion_settings.ofim_crypto_to_proxy or ())
        proxy_etf_symbols = set(crypto_proxy_map.values())
        ofim_trackable_symbols = ofim_equity_symbols | proxy_etf_symbols

        ofim_positions = positions
        if not ofim_positions.empty and ofim_trackable_symbols:
            ofim_positions = ofim_positions[ofim_positions["code"].isin(ofim_trackable_symbols)].copy()

        # held_symbols passed to OFIM: equity codes as-is, but proxy ETF positions are
        # reverse-mapped back to their crypto symbol so OFIM's exit logic triggers correctly.
        proxy_to_crypto: dict[str, str] = {v: k for k, v in crypto_proxy_map.items()}
        raw_held = set(ofim_positions["code"].tolist()) if not ofim_positions.empty else set()
        ofim_held_symbols: set[str] = set()
        for code in raw_held:
            ofim_held_symbols.add(proxy_to_crypto.get(code, code))

        # Reuse the persistent instance so prev_order_books is maintained across cycles.
        # The instance lives on state (not as a module global) so tests and multiple
        # concurrent traders don't accidentally share state.
        if state is None or state.ofim_strategy is None:
            ofim_instance = OfimIntradayStrategy(fusion_settings)
            if state is not None:
                state.ofim_strategy = ofim_instance
        else:
            ofim_instance = state.ofim_strategy
            # Settings may change between cycles (e.g. after a weight edit) — update them
            object.__setattr__(ofim_instance, "settings", fusion_settings)
        ofim_plan = ofim_instance.generate_plan(trader, ofim_held_symbols)
        diagnostics["ofim_benchmark_score"] = round(float(ofim_plan.benchmark_score), 6)
        diagnostics["ofim_targets"] = tuple(sorted(ofim_plan.target_weights))
        diagnostics["ofim_top"] = tuple(sorted(ofim_plan.target_weights, key=ofim_plan.target_weights.get, reverse=True)[:3])
        scaled_ofim_weights = {code: round(weight * ofim_weight, 6) for code, weight in ofim_plan.target_weights.items()}

    scaled_cascade_weights: dict[str, float] = {}
    if cascade_weight > 0:
        market_date = _market_now(now_utc, settings).date().isoformat()
        # Daily caching: Cascade is a daily strategy — reuse the same plan all day.
        # This prevents intraday churn from incomplete K-line bars re-running every 60 s.
        if (
            state is not None
            and state.cascade_plan is not None
            and state.cascade_plan_date == market_date
        ):
            cascade_plan = state.cascade_plan
            diagnostics["cascade_cached"] = True
        else:
            cascade_plan = generate_live_cascade_plan(settings, trader)
            if state is not None:
                state.cascade_plan = cascade_plan
                state.cascade_plan_date = market_date
            _log(
                f"cascade: generated new daily plan "
                f"(date={market_date}, regime={cascade_plan.regime_label}, "
                f"score={cascade_plan.regime_score:+.3f})"
            )
        diagnostics["cascade_regime"] = cascade_plan.regime_label
        diagnostics["cascade_score"] = round(float(cascade_plan.regime_score), 6)
        diagnostics["cascade_targets"] = tuple(sorted(cascade_plan.target_weights))
        if cascade_plan.note:
            diagnostics["cascade_note"] = cascade_plan.note
        scaled_cascade_weights = {
            code: round(weight * cascade_weight, 6) for code, weight in cascade_plan.target_weights.items()
        }

    strategy_targets = {
        "Baseline": dict(baseline_weights),
        "Fusion": dict(scaled_fusion_weights),
        "OFIM": dict(scaled_ofim_weights),
        "Claude/Cascade": dict(scaled_cascade_weights),
    }
    strategy_sets = strategy_symbol_sets(settings)
    tracked_symbols = set().union(*[set(weights) for weights in strategy_targets.values()])
    if not positions.empty and "code" in positions.columns:
        tracked_symbols.update(str(code) for code in positions["code"].tolist())
    source_map: dict[str, str] = {}
    for code in tracked_symbols:
        owners = [name for name, weights in strategy_targets.items() if float(weights.get(code, 0.0)) > 0]
        if not owners:
            owners = [name for name, symbols in strategy_sets.items() if code in symbols]
        if len(owners) == 1:
            source_map[code] = owners[0]
        elif len(owners) > 1:
            source_map[code] = "Shared/Overlap"
        else:
            source_map[code] = "Unclassified"

    return (
        stack_target_weights(baseline_weights, scaled_fusion_weights, scaled_ofim_weights, scaled_cascade_weights),
        diagnostics,
        source_map,
    )


def _stack_monitoring_detail(settings: Settings, diagnostics: dict[str, object]) -> str:
    detail_parts = [f"stack={stack_label(settings)}"]
    fusion_score = diagnostics.get("fusion_benchmark_score")
    if fusion_score is not None:
        detail_parts.append(f"fusion_bm={float(fusion_score):.4f}")
    ofim_score = diagnostics.get("ofim_benchmark_score")
    if ofim_score is not None:
        detail_parts.append(f"ofim_bm={float(ofim_score):.4f}")
    ofim_top = diagnostics.get("ofim_top")
    if ofim_top:
        detail_parts.append(f"ofim_top={','.join(str(s).replace('US.', '') for s in ofim_top)}")
    cascade_regime = diagnostics.get("cascade_regime")
    if cascade_regime:
        cascade_score = diagnostics.get("cascade_score")
        if cascade_score is not None:
            detail_parts.append(f"cascade_regime={cascade_regime}({float(cascade_score):+.3f})")
        else:
            detail_parts.append(f"cascade_regime={cascade_regime}")
    cascade_targets = diagnostics.get("cascade_targets")
    if cascade_targets is not None:
        tgt_str = ",".join(str(s).replace("US.", "") for s in cascade_targets) if cascade_targets else "none"
        detail_parts.append(f"cascade_targets={tgt_str}")
    cascade_note = diagnostics.get("cascade_note")
    if cascade_note:
        detail_parts.append(str(cascade_note))
    return " | ".join(detail_parts)


def _stack_runtime_detail(settings: Settings, market_detail: str) -> str:
    return f"stack={stack_label(settings)} | {market_detail}"


def _write_status(
    *,
    running: bool,
    action: str,
    detail: str,
    market_open: bool,
    settings: Settings,
    state: AutoTraderState,
) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    version, tag, commit = describe_build()
    payload = {
        "running": running,
        "pid": os.getpid(),
        "version": version,
        "tag": tag,
        "commit": commit,
        "updated_at": datetime.now(UTC).isoformat(),
        "action": action,
        "detail": detail,
        "market_open": market_open,
        "poll_seconds": settings.auto_trader_poll_seconds,
        "timezone": settings.auto_trader_market_timezone,
        "window_start": settings.auto_trader_start_time,
        "window_end": settings.auto_trader_end_time,
        "last_signature": state.last_signature,
        "last_submit_at": state.last_submit_at.isoformat() if state.last_submit_at else None,
        "last_cycle_id": state.last_cycle_id,
        "target_weights": state.last_target_weights,
        # ── Last-cycle counters (reset every cycle) ──
        # These four "last_cycle_*" fields are the canonical names. The legacy
        # plain-named fields below are kept as aliases so existing dashboards /
        # control panels do not break, but new readers should prefer the
        # explicit names.
        "last_cycle_planned": state.last_planned_order_count,
        "last_cycle_submitted": state.last_submitted_order_count,
        "last_cycle_recorded_fills": state.last_recorded_fill_count,
        # ── Cumulative counters (since process start; persisted via state file) ──
        "cumulative_planned_orders": state.cumulative_planned_orders,
        "cumulative_submitted_orders": state.cumulative_submitted_orders,
        "cumulative_recorded_fills": state.cumulative_recorded_fills,
        # ── Legacy aliases (kept for backwards compatibility) ──
        "planned_order_count": state.last_planned_order_count,         # alias of last_cycle_planned
        "submitted_order_count": state.last_submitted_order_count,     # alias of last_cycle_submitted
        "recorded_fill_count": len(state.recorded_fill_ids),           # NOTE: this is the dedup-set size, NOT cumulative fills count
        "last_recorded_fill_count": state.last_recorded_fill_count,    # alias of last_cycle_recorded_fills
        "exit_signal_counts": state.exit_signal_counts,
        "symbol_cooldown_count": len(state.last_symbol_trade_time),
        "consecutive_transient_count": state.consecutive_transient_count,
        "log_file": str(AUTO_TRADER_LOG_FILE),
    }
    AUTO_TRADER_STATUS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _register_pid() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if AUTO_TRADER_PID_FILE.exists():
        try:
            current_pid = int(AUTO_TRADER_PID_FILE.read_text(encoding="utf-8").strip())
        except ValueError:
            current_pid = 0
        if current_pid and _is_pid_running(current_pid):
            raise SystemExit(f"Auto trader is already running with pid {current_pid}.")
    AUTO_TRADER_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def validate_auto_trader_mode(settings: Settings, *, submit: bool) -> None:
    stack_allocations(settings)
    if settings.futu_trd_env == "SIMULATE" or not submit:
        return
    if not settings.futu_enable_real_trading:
        raise SystemExit("REAL trading is disabled. Set FUTU_ENABLE_REAL_TRADING=true first.")
    if not settings.futu_allow_auto_real:
        raise SystemExit("REAL auto trading is locked. Set FUTU_ALLOW_AUTO_REAL=true only after manual verification.")
    if not settings.futu_unlock_trade_password_md5:
        raise SystemExit("REAL auto trading requires FUTU_UNLOCK_TRADE_PASSWORD_MD5.")


def _cleanup_files() -> None:
    if AUTO_TRADER_PID_FILE.exists():
        AUTO_TRADER_PID_FILE.unlink()


def run_cycle(settings: Settings, state: AutoTraderState, *, submit: bool) -> tuple[str, str]:
    cycle_id = uuid4().hex[:12]
    now_utc = datetime.now(UTC)
    state.last_cycle_id = cycle_id
    state.last_planned_order_count = 0
    state.last_submitted_order_count = 0
    state.last_recorded_fill_count = 0
    append_stock_event(
        "cycle_started",
        {
            "submit": submit,
            "stack": stack_label(settings),
        },
        cycle_id=cycle_id,
    )
    market_open, window_detail = _market_window_state(now_utc, settings)
    if not market_open:
        append_stock_event("cycle_waiting", {"detail": window_detail}, cycle_id=cycle_id)
        return "waiting", _stack_runtime_detail(settings, window_detail)

    with FutuPaperTrader(settings) as trader:
        # API-level health probe BEFORE any side-effect call (orders, unlock).
        # __enter__ only does a TCP socket check; an OpenD that accepts the
        # socket but fails query_subscription is the "half-dead" state seen on
        # 2026-05-27 16:56–17:14 in auto_trader.log. Raising FutuTransientError
        # here causes the outer loop to mark the cycle transient_error and the
        # consecutive-transient short-circuit to take over after N cycles.
        # ``getattr`` so existing test mocks that don't implement healthcheck
        # are not forced to add the method just to keep working.
        _hc = getattr(trader, "healthcheck", None)
        if callable(_hc):
            _hc()
        acc_id = trader.resolve_trade_account()
        _record_new_fills(trader, acc_id, settings, state, now_utc=now_utc, cycle_id=cycle_id)
        open_orders = trader.get_open_orders(acc_id)
        if not open_orders.empty:
            stale_threshold = timedelta(minutes=settings.auto_trader_order_stale_minutes)
            oldest_age = _oldest_open_order_age(open_orders, now_utc)
            # Treat unreadable timestamps as stale so we never get stuck permanently.
            is_stale = oldest_age is None or oldest_age >= stale_threshold
            if is_stale:
                age_desc = f"{oldest_age.total_seconds():.0f}s" if oldest_age else "unknown"
                n_cancelled = trader.cancel_all_open_orders(acc_id)
                append_stock_event(
                    "stale_orders_cancelled",
                    {
                        "count": n_cancelled,
                        "oldest_age_seconds": oldest_age.total_seconds() if oldest_age else None,
                        "threshold_minutes": settings.auto_trader_order_stale_minutes,
                    },
                    cycle_id=cycle_id,
                )
                _log(
                    f"auto-cancelled {n_cancelled} stale open order(s) "
                    f"(oldest_age={age_desc}, threshold={settings.auto_trader_order_stale_minutes}min) "
                    "— proceeding with fresh cycle"
                )
                # Fall through: recompute targets with fresh prices immediately.
            else:
                age_s = int(oldest_age.total_seconds())
                threshold_s = int(stale_threshold.total_seconds())
                append_stock_event(
                    "waiting_for_fill",
                    {"open_orders": len(open_orders), "oldest_age_seconds": age_s, "threshold_seconds": threshold_s},
                    cycle_id=cycle_id,
                )
                return "waiting", (
                    f"waiting_for_fill: open_orders={len(open_orders)} "
                    f"oldest={age_s}s/{threshold_s}s"
                )

        positions = trader.get_positions(acc_id)
        _record_ledger_reconciliation(positions, cycle_id=cycle_id)
        ignored_symbols: set[str] = set()

        raw_stack_target_map, diagnostics, strategy_source_map = _strategy_stack_target_weights(settings, trader, now_utc, positions, state)
        stack_target_map = _risk_adjust_target_weights(raw_stack_target_map, settings, cycle_id=cycle_id)
        state.last_target_weights = dict(stack_target_map)
        append_stock_event(
            "plan_generated",
            {
                "target_weights": stack_target_map,
                "diagnostics": diagnostics,
                "held_symbols": sorted(positions["code"].tolist()) if not positions.empty and "code" in positions.columns else [],
            },
            cycle_id=cycle_id,
        )

        held_codes = set(positions["code"].tolist()) if not positions.empty and "code" in positions.columns else set()
        threshold = max(1, int(settings.auto_trader_exit_confirm_cycles))
        if threshold > 1:
            for code in sorted(held_codes):
                if float(stack_target_map.get(code, 0.0)) > 0:
                    state.exit_signal_counts.pop(code, None)
                    continue
                count = int(state.exit_signal_counts.get(code, 0)) + 1
                state.exit_signal_counts[code] = count
                if count < threshold:
                    ignored_symbols.add(code)
                    append_stock_event(
                        "exit_confirm_holding",
                        {"code": code, "count": count, "threshold": threshold},
                        cycle_id=cycle_id,
                    )
                    _log(f"exit-confirm: {code} count={count}/{threshold}, holding position")
            for code in list(state.exit_signal_counts):
                if code not in held_codes:
                    state.exit_signal_counts.pop(code, None)

        # Minimum hold time: prevent exiting a recently entered position.
        # If a BUY was submitted within AUTO_TRADER_MIN_HOLD_MINUTES minutes,
        # add that symbol to ignored_symbols so plan_rebalance cannot generate
        # a SELL for it. Full exits (target_weight already 0 AND no held position)
        # are unaffected because ignored_symbols only skips sell-side rebalancing.
        min_hold = timedelta(minutes=settings.auto_trader_min_hold_minutes)
        if min_hold.total_seconds() > 0 and state is not None:
            for code, entry_time in list(state.position_entry_times.items()):
                age = now_utc - entry_time
                # Always clean up symbols we no longer hold, regardless of hold-time window.
                # This covers the case where a position was closed via a SELL order that
                # didn't go through our SELL path (e.g. manual close or broker cancel).
                if _position_quantity(positions, code) == 0:
                    state.position_entry_times.pop(code, None)
                    continue
                if age < min_hold:
                    # Still within hold window and position is live — protect from exit
                    if code not in stack_target_map:
                        ignored_symbols.add(code)
                        remaining_s = int((min_hold - age).total_seconds())
                        _log(
                            f"hold-protect: keeping {code} position "
                            f"(entered {int(age.total_seconds())}s ago, "
                            f"hold={settings.auto_trader_min_hold_minutes}min, "
                            f"exits in {remaining_s}s)"
                        )
                else:
                    # Hold window expired — clean up so the dict doesn't grow unbounded
                    state.position_entry_times.pop(code, None)

        planned_orders: list[PlannedOrder] = []
        strategy_orders: list[PlannedOrder] = []
        account_for_orders: pd.Series | dict[str, Any] | None = None
        held_symbols = set()
        if not positions.empty:
            held_symbols = set(positions.loc[~positions["code"].isin(ignored_symbols), "code"].tolist())
        if stack_target_map or held_symbols:
            account, strategy_orders = trader.plan_rebalance(stack_target_map, ignore_symbols=ignored_symbols)
            account_for_orders = account
            strategy_orders = [
                _with_strategy_source(order, strategy_source_map.get(order.code, "Unclassified"))
                for order in strategy_orders
            ]
            loss_breached, loss_detail = _loss_guard_breached(account, settings, cycle_id=cycle_id)
            if loss_breached:
                before_count = len(strategy_orders)
                strategy_orders = [order for order in strategy_orders if order.side == "SELL"]
                append_stock_event(
                    "risk_loss_limit_filter",
                    {
                        "detail": loss_detail,
                        "orders_before": before_count,
                        "orders_after": len(strategy_orders),
                    },
                    cycle_id=cycle_id,
                )
                if before_count != len(strategy_orders):
                    _log(f"loss-guard: blocked new-risk orders ({loss_detail})")
            min_symbol_interval = timedelta(seconds=max(0, int(settings.auto_trader_min_symbol_interval_seconds)))
            if min_symbol_interval.total_seconds() > 0:
                filtered_orders: list[PlannedOrder] = []
                for order in strategy_orders:
                    last_trade = state.last_symbol_trade_time.get(order.code)
                    if last_trade is not None and (now_utc - last_trade) < min_symbol_interval:
                        remaining = int((min_symbol_interval - (now_utc - last_trade)).total_seconds())
                        append_stock_event(
                            "symbol_cooldown_skip",
                            {"code": order.code, "side": order.side, "remaining_seconds": max(0, remaining)},
                            cycle_id=cycle_id,
                        )
                        _log(f"symbol-cooldown: skipping {order.code} {order.side}, remaining={max(0, remaining)}s")
                        continue
                    filtered_orders.append(order)
                strategy_orders = filtered_orders
            strategy_orders = _apply_cycle_turnover_cap(strategy_orders, settings, cycle_id=cycle_id)
            planned_orders.extend(strategy_orders)
        state.last_planned_order_count = len(planned_orders)
        state.cumulative_planned_orders += len(planned_orders)

        if not planned_orders:
            detail_text = _stack_monitoring_detail(settings, diagnostics)
            append_stock_event(
                "cycle_completed",
                {"action": "monitoring", "detail": detail_text, "planned_orders": 0},
                cycle_id=cycle_id,
            )
            _save_state(state)
            if not stack_target_map and not held_symbols:
                return ("monitoring", f"no_entry_signal {detail_text}")
            return "monitoring", f"no_rebalance_needed {detail_text}"

        signature = _order_signature(planned_orders)
        if (
            signature == state.last_signature
            and state.last_submit_at is not None
            and (now_utc - state.last_submit_at).total_seconds() < settings.auto_trader_order_cooldown_seconds
        ):
            append_stock_event(
                "cycle_completed",
                {"action": "cooldown", "signature": signature, "planned_orders": len(planned_orders)},
                cycle_id=cycle_id,
            )
            _save_state(state)
            return "cooldown", f"duplicate_plan_within_{settings.auto_trader_order_cooldown_seconds}s"

        # Log the planned orders before any submission decision (下单决定落盘)
        market_logger.log_orders(planned_orders, "planned", ts=now_utc)
        append_order_memory(
            planned_orders,
            cycle_id=cycle_id,
            stage="planned",
            settings=settings,
            account=account_for_orders,
            positions=positions,
            target_weights=stack_target_map,
            diagnostics=diagnostics,
            now_utc=now_utc,
        )
        append_stock_event(
            "orders_planned",
            {
                "signature": signature,
                "orders": [order.__dict__ for order in planned_orders],
            },
            cycle_id=cycle_id,
        )

        if not submit:
            append_stock_event(
                "cycle_completed",
                {"action": "planned", "signature": signature, "planned_orders": len(planned_orders)},
                cycle_id=cycle_id,
            )
            _save_state(state)
            return (
                "planned",
                f"stack={stack_label(settings)} strategy_orders={len(strategy_orders)} "
                f"stack_symbols={len(stack_target_map)} signature={signature}",
            )

        result = trader.submit_orders(planned_orders)
        # Log submission outcome (whether each order was accepted or errored)
        market_logger.log_orders(planned_orders, "submitted", result_df=result, ts=now_utc)
        append_order_memory(
            planned_orders,
            cycle_id=cycle_id,
            stage="submitted",
            settings=settings,
            account=account_for_orders,
            positions=positions,
            target_weights=stack_target_map,
            diagnostics=diagnostics,
            result_df=result,
            now_utc=now_utc,
        )
        submitted = int((result["status"] == "submitted").sum()) if not result.empty else 0
        errored = int((result["status"] == "error").sum()) if not result.empty else 0
        state.last_submitted_order_count = submitted
        state.cumulative_submitted_orders += submitted
        state.last_signature = signature
        state.last_submit_at = now_utc
        submitted_by_code: set[str] = set()
        if not result.empty:
            for row in result.itertuples(index=False):
                if str(getattr(row, "status", "")).lower() != "submitted":
                    continue
                code = str(getattr(row, "code", ""))
                order_id = str(getattr(row, "detail", "")).strip()
                source = next((order.strategy_source for order in planned_orders if order.code == code), "Unclassified")
                if order_id:
                    state.submitted_order_sources[order_id] = source
                if code:
                    submitted_by_code.add(code)
        # Record entry times for BUY orders so the hold-time guard can protect them.
        # Clear entry times for SELL orders (position closed, protection no longer needed).
        for order in planned_orders:
            if order.code not in submitted_by_code:
                continue
            state.last_symbol_trade_time[order.code] = now_utc
            if order.side == "BUY":
                state.position_entry_times[order.code] = now_utc
            elif order.side == "SELL":
                state.position_entry_times.pop(order.code, None)
                state.exit_signal_counts.pop(order.code, None)
        _record_new_fills(trader, acc_id, settings, state, now_utc=now_utc, cycle_id=cycle_id)
        append_stock_event(
            "orders_submitted",
            {
                "signature": signature,
                "submitted": submitted,
                "errored": errored,
                "results": result.to_dict("records") if not result.empty else [],
            },
            cycle_id=cycle_id,
        )
        action_name = "submitted_with_errors" if errored else "submitted"
        transient_only = False
        if errored:
            error_rows = result.loc[result["status"] == "error", "detail"].astype(str)
            transient_only = bool(len(error_rows)) and error_rows.map(_is_transient_runtime_error).all()
        if submitted == 0 and errored > 0:
            action_name = "transient_error" if transient_only else "error"
        elif errored and transient_only:
            action_name = "submitted_with_transient_errors"
        error_detail = ""
        if errored:
            last_error = str(result.loc[result["status"] == "error", "detail"].iloc[-1])
            error_detail = f" submit_errors={errored} last_error={last_error}"
        append_stock_event(
            "cycle_completed",
            {
                "action": action_name,
                "signature": signature,
                "submitted": submitted,
                "errored": errored,
            },
            cycle_id=cycle_id,
        )
        _save_state(state)
        return (
            action_name,
            f"stack={stack_label(settings)} submitted_orders={submitted} strategy_orders={len(strategy_orders)} "
            f"stack_symbols={len(stack_target_map)} signature={signature}{error_detail}",
        )


def run_auto_trader(settings: Settings, *, submit: bool) -> None:
    validate_auto_trader_mode(settings, submit=submit)

    stop_requested = False
    state = _load_state()

    def _handle_signal(signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True
        _log(f"received signal {signum}, shutting down")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _register_pid()
    _write_status(
        running=True,
        action="starting",
        detail="auto trader booting",
        market_open=False,
        settings=settings,
        state=state,
    )
    _log("auto trader started")

    try:
        while not stop_requested:
            market_open, window_detail = _market_window_state(datetime.now(UTC), settings)
            _write_status(
                running=True,
                action="polling",
                detail=_stack_runtime_detail(settings, window_detail),
                market_open=market_open,
                settings=settings,
                state=state,
            )

            # ── Half-dead OpenD short-circuit ─────────────────────────────────
            # If the previous N cycles all ended in transient_error, skip the
            # next run_cycle entirely (no API calls, no submit). This prevents
            # the failure mode seen 2026-05-27 16:56–17:14 where OpenD's TCP
            # socket stayed open but every API call returned "Connection closed"
            # for ~18 minutes while the auto_trader kept submitting orders.
            # Reset the counter once the lockdown is observed so the next cycle
            # is allowed to try again (the watchdog handles deeper recovery).
            lockdown_limit = settings.auto_trader_max_consecutive_transient
            if lockdown_limit > 0 and state.consecutive_transient_count >= lockdown_limit:
                action = "opend_lockdown"
                detail = (
                    f"skipping run_cycle: {state.consecutive_transient_count} consecutive "
                    f"transient_error cycles >= AUTO_TRADER_MAX_CONSECUTIVE_TRANSIENT={lockdown_limit}. "
                    "Waiting for watchdog to restore OpenD or for the half-dead window to clear."
                )
                _log(f"opend_lockdown: {detail}")
                append_stock_event(
                    "opend_lockdown",
                    {"consecutive_transient_count": state.consecutive_transient_count, "limit": lockdown_limit},
                    cycle_id=state.last_cycle_id,
                )
                # Allow the next cycle to attempt recovery so we don't sit
                # locked forever; the watchdog plus the consecutive counter
                # will re-engage if OpenD is still half-dead.
                state.consecutive_transient_count = 0
                _write_status(
                    running=True,
                    action=action,
                    detail=detail,
                    market_open=market_open,
                    settings=settings,
                    state=state,
                )
                _save_state(state)
                sleep_until = time.time() + settings.auto_trader_poll_seconds
                while not stop_requested and time.time() < sleep_until:
                    time.sleep(1)
                continue

            try:
                action, detail = run_cycle(settings, state, submit=submit)
                _log(f"{action}: {detail}")
                # Successful cycle resets the transient counter so genuine
                # recovery does not stay penalised after one bad window.
                if action not in ("transient_error", "error"):
                    state.consecutive_transient_count = 0
            except FutuTransientError as exc:
                action = "transient_error"
                detail = str(exc)
                state.consecutive_transient_count += 1
                market_logger.log_error("auto_trader_transient_error", exc)
                append_stock_event(
                    "cycle_error",
                    {
                        "action": action,
                        "detail": detail,
                        "consecutive_transient_count": state.consecutive_transient_count,
                    },
                    cycle_id=state.last_cycle_id,
                )
                _log(f"transient_error: {detail} (consecutive={state.consecutive_transient_count})")
            except FutuTradeError as exc:
                detail = str(exc)
                if _is_transient_runtime_error(detail):
                    action = "transient_error"
                    state.consecutive_transient_count += 1
                    market_logger.log_error("auto_trader_transient_error", exc)
                    append_stock_event(
                        "cycle_error",
                        {
                            "action": action,
                            "detail": detail,
                            "consecutive_transient_count": state.consecutive_transient_count,
                        },
                        cycle_id=state.last_cycle_id,
                    )
                    _log(f"transient_error: {detail} (consecutive={state.consecutive_transient_count})")
                else:
                    action = "error"
                    # Non-transient errors don't increment the OpenD lockdown
                    # counter — they signal a different failure mode that the
                    # operator must handle. But they also don't reset it; the
                    # counter only clears on a clean cycle or after a lockdown.
                    market_logger.log_error("auto_trader_error", exc)
                    append_stock_event("cycle_error", {"action": action, "detail": detail}, cycle_id=state.last_cycle_id)
                    _log(f"error: {detail}")
            except Exception as exc:  # pragma: no cover - safety net for daemon process
                detail = f"{type(exc).__name__}: {exc}"
                if _is_transient_runtime_error(detail):
                    action = "transient_error"
                    state.consecutive_transient_count += 1
                    market_logger.log_error("auto_trader_transient_error", exc)
                    append_stock_event(
                        "cycle_error",
                        {
                            "action": action,
                            "detail": detail,
                            "consecutive_transient_count": state.consecutive_transient_count,
                        },
                        cycle_id=state.last_cycle_id,
                    )
                    _log(f"transient_error: {detail} (consecutive={state.consecutive_transient_count})")
                else:
                    action = "error"
                    market_logger.log_error("auto_trader_exception", exc)
                    append_stock_event("cycle_error", {"action": action, "detail": detail}, cycle_id=state.last_cycle_id)
                    _log(f"error: {detail}")

            _write_status(
                running=True,
                action=action,
                detail=(
                    detail
                    if action != "waiting"
                    else _stack_runtime_detail(settings, window_detail)
                    if detail.startswith(("stack=", "weekend", "outside_window"))
                    else detail
                ),
                market_open=market_open,
                settings=settings,
                state=state,
            )
            _save_state(state)

            sleep_until = time.time() + settings.auto_trader_poll_seconds
            while not stop_requested and time.time() < sleep_until:
                time.sleep(1)
    finally:
        _write_status(
            running=False,
            action="stopped",
            detail="auto trader stopped",
            market_open=False,
            settings=settings,
            state=state,
        )
        _cleanup_files()
        _save_state(state)
        _log("auto trader stopped")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Fusion intraday strategy in a continuous loop.")
    parser.add_argument("--dry-run", action="store_true", help="Monitor continuously but do not submit orders.")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    settings = load_settings()
    run_auto_trader(settings, submit=not args.dry_run)


if __name__ == "__main__":
    main()
