from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import logging
import json
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

import pandas as pd

from .ledger import FillEvent, project_fills

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .config import Settings


_BROKER_TOTAL_FEE_COLUMNS = (
    "fee_amount",
    "fees_total",
    "total_fee",
    "total_fees",
    "fee",
)
_BROKER_COMPONENT_COLUMNS = {
    "commission": ("commission", "commission_fee", "broker_commission"),
    "platform_fee": ("platform_fee", "platform_service_fee"),
    "settlement_fee": ("settlement_fee", "settlement"),
    "sec_fee": ("sec_fee", "sec_charges", "sec"),
    "taf_fee": ("taf_fee", "trading_activity_fee", "taf"),
}


@dataclass(frozen=True)
class TradeCostModel:
    profile: str
    commission_per_share: float
    commission_min: float
    commission_max_pct: float
    platform_per_share: float
    platform_min: float
    platform_max_pct: float
    settlement_per_share: float
    settlement_min: float
    settlement_max_pct: float
    sec_sell_rate: float
    sec_sell_min: float
    sec_zero_from: date | None
    taf_sell_per_share: float
    taf_sell_min: float
    taf_sell_max: float


@dataclass(frozen=True)
class TradeCostBreakdown:
    total: float = 0.0
    commission: float = 0.0
    platform_fee: float = 0.0
    settlement_fee: float = 0.0
    sec_fee: float = 0.0
    taf_fee: float = 0.0
    source: str = "none"

    def as_dict(self) -> dict[str, float | str]:
        return {
            "fees_total": float(self.total),
            "fee_commission": float(self.commission),
            "fee_platform": float(self.platform_fee),
            "fee_settlement": float(self.settlement_fee),
            "fee_sec": float(self.sec_fee),
            "fee_taf": float(self.taf_fee),
            "fee_source": self.source,
        }


def _optional_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(numeric):
        return None
    return numeric


def _parse_trade_date(timestamp: object) -> date | None:
    if timestamp in (None, "", "N/A"):
        return None
    try:
        return pd.Timestamp(timestamp).date()
    except (TypeError, ValueError):
        return None


def _bounded_fee(raw: float, minimum: float, maximum: float | None) -> float:
    fee = max(float(raw), 0.0)
    if fee <= 0:
        return 0.0
    if minimum > 0:
        fee = max(fee, float(minimum))
    if maximum is not None and maximum >= 0:
        fee = min(fee, float(maximum))
    return float(fee)


def build_trade_cost_model(settings: Settings) -> TradeCostModel | None:
    if not settings.trade_costs_enabled:
        return None
    sec_zero_from = _parse_trade_date(settings.trade_cost_sec_zero_from)
    return TradeCostModel(
        profile=settings.trade_cost_profile,
        commission_per_share=float(settings.trade_cost_commission_per_share),
        commission_min=float(settings.trade_cost_commission_min),
        commission_max_pct=float(settings.trade_cost_commission_max_pct),
        platform_per_share=float(settings.trade_cost_platform_per_share),
        platform_min=float(settings.trade_cost_platform_min),
        platform_max_pct=float(settings.trade_cost_platform_max_pct),
        settlement_per_share=float(settings.trade_cost_settlement_per_share),
        settlement_min=float(settings.trade_cost_settlement_min),
        settlement_max_pct=float(settings.trade_cost_settlement_max_pct),
        sec_sell_rate=float(settings.trade_cost_sec_sell_rate),
        sec_sell_min=float(settings.trade_cost_sec_sell_min),
        sec_zero_from=sec_zero_from,
        taf_sell_per_share=float(settings.trade_cost_taf_sell_per_share),
        taf_sell_min=float(settings.trade_cost_taf_sell_min),
        taf_sell_max=float(settings.trade_cost_taf_sell_max),
    )


def buffered_trade_price(last_price: float, side: str, slippage_bps: float) -> float:
    buffer = float(slippage_bps) / 10_000.0
    return float(last_price * (1 + buffer if str(side).upper() == "BUY" else 1 - buffer))


def broker_fee_total_from_row(row: Mapping[str, object] | pd.Series) -> float | None:
    total_columns = [column for column in _BROKER_TOTAL_FEE_COLUMNS if column in row]
    if total_columns:
        total = 0.0
        found = False
        for column in total_columns:
            value = _optional_float(row.get(column))
            if value is None:
                continue
            total += abs(value)
            found = True
        if found:
            return float(total)

    component_total = 0.0
    component_found = False
    for columns in _BROKER_COMPONENT_COLUMNS.values():
        for column in columns:
            if column not in row:
                continue
            value = _optional_float(row.get(column))
            if value is None:
                continue
            component_total += abs(value)
            component_found = True
    if component_found:
        return float(component_total)
    return None


def estimate_trade_cost(
    side: str,
    quantity: int | float,
    price: float,
    *,
    timestamp: object = None,
    model: TradeCostModel | None = None,
    broker_fee_total: float | None = None,
) -> TradeCostBreakdown:
    qty = float(quantity)
    last_price = float(price)
    if qty <= 0 or last_price <= 0:
        return TradeCostBreakdown()

    if broker_fee_total is not None:
        return TradeCostBreakdown(total=float(broker_fee_total), source="broker_reported")

    if model is None:
        return TradeCostBreakdown(source="disabled")

    notional = qty * last_price
    commission = _bounded_fee(
        qty * model.commission_per_share,
        model.commission_min,
        notional * model.commission_max_pct if model.commission_max_pct > 0 else None,
    )
    platform_fee = _bounded_fee(
        qty * model.platform_per_share,
        model.platform_min,
        notional * model.platform_max_pct if model.platform_max_pct > 0 else None,
    )
    settlement_fee = _bounded_fee(
        qty * model.settlement_per_share,
        model.settlement_min,
        notional * model.settlement_max_pct if model.settlement_max_pct > 0 else None,
    )

    side_name = str(side).upper()
    sec_fee = 0.0
    taf_fee = 0.0
    trade_date = _parse_trade_date(timestamp)
    sec_enabled = side_name == "SELL" and model.sec_sell_rate > 0
    if sec_enabled and model.sec_zero_from is not None and trade_date is not None and trade_date >= model.sec_zero_from:
        sec_enabled = False
    if sec_enabled:
        sec_fee = _bounded_fee(notional * model.sec_sell_rate, model.sec_sell_min, None)
    if side_name == "SELL" and model.taf_sell_per_share > 0:
        taf_fee = _bounded_fee(qty * model.taf_sell_per_share, model.taf_sell_min, model.taf_sell_max)

    total = commission + platform_fee + settlement_fee + sec_fee + taf_fee
    return TradeCostBreakdown(
        total=float(total),
        commission=float(commission),
        platform_fee=float(platform_fee),
        settlement_fee=float(settlement_fee),
        sec_fee=float(sec_fee),
        taf_fee=float(taf_fee),
        source=model.profile,
    )


def trade_cash_delta(
    side: str,
    quantity: int | float,
    price: float,
    *,
    timestamp: object = None,
    model: TradeCostModel | None = None,
    broker_fee_total: float | None = None,
) -> tuple[float, TradeCostBreakdown]:
    breakdown = estimate_trade_cost(
        side,
        quantity,
        price,
        timestamp=timestamp,
        model=model,
        broker_fee_total=broker_fee_total,
    )
    notional = float(quantity) * float(price)
    if str(side).upper() == "BUY":
        return -(notional + breakdown.total), breakdown
    return notional - breakdown.total, breakdown


def max_affordable_buy_quantity(
    cash: float,
    price: float,
    desired_quantity: int,
    *,
    timestamp: object = None,
    lot_size: int = 1,
    model: TradeCostModel | None = None,
) -> int:
    if cash <= 0 or price <= 0 or desired_quantity <= 0:
        return 0
    safe_lot = max(int(lot_size), 1)
    desired_lots = max(int(desired_quantity // safe_lot), 0)
    if desired_lots <= 0:
        return 0

    low = 0
    high = desired_lots
    while low < high:
        mid = (low + high + 1) // 2
        qty = mid * safe_lot
        required_cash = -trade_cash_delta("BUY", qty, price, timestamp=timestamp, model=model)[0]
        if required_cash <= cash + 1e-9:
            low = mid
        else:
            high = mid - 1
    return low * safe_lot


def with_trade_costs(
    frame: pd.DataFrame,
    settings: Settings,
    *,
    side_col: str = "side",
    qty_col: str = "qty",
    price_col: str = "price",
    timestamp_col: str = "timestamp",
) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    model = build_trade_cost_model(settings)
    rows = frame.copy()
    fee_rows: list[dict[str, float | str]] = []
    net_cash_flow: list[float] = []
    for row in rows.to_dict("records"):
        side = str(row.get(side_col, ""))
        qty = int(float(row.get(qty_col, 0) or 0))
        price = float(row.get(price_col, 0) or 0.0)
        timestamp = row.get(timestamp_col)
        broker_fee_total = broker_fee_total_from_row(row)
        cash_delta, breakdown = trade_cash_delta(
            side,
            qty,
            price,
            timestamp=timestamp,
            model=model,
            broker_fee_total=broker_fee_total,
        )
        fee_rows.append(breakdown.as_dict())
        net_cash_flow.append(float(cash_delta))

    fee_frame = pd.DataFrame(fee_rows, index=rows.index)
    rows = pd.concat([rows, fee_frame], axis=1)
    rows["net_cash_flow"] = net_cash_flow
    return rows


def estimate_realized_from_fills(
    order_history: pd.DataFrame,
    settings: Settings,
    *,
    code_col: str = "code",
    side_col: str = "trd_side",
    qty_col: str = "dealt_qty",
    price_col: str = "dealt_avg_price",
    timestamp_col: str = "updated_time",
) -> float:
    if order_history.empty:
        return 0.0
    rows = order_history.copy()
    rows["qty_num"] = pd.to_numeric(rows.get(qty_col), errors="coerce").fillna(0.0)
    rows["price_num"] = pd.to_numeric(rows.get(price_col), errors="coerce").fillna(0.0)
    rows = rows[rows["qty_num"] > 0].copy()
    if rows.empty:
        return 0.0

    if timestamp_col in rows.columns:
        rows["timestamp"] = pd.to_datetime(rows[timestamp_col])
        rows = rows.sort_values("timestamp", ascending=True)
        annotated = with_trade_costs(rows, settings, side_col=side_col, qty_col="qty_num", price_col="price_num", timestamp_col="timestamp")
    else:
        annotated = with_trade_costs(rows, settings, side_col=side_col, qty_col="qty_num", price_col="price_num", timestamp_col=timestamp_col)

    def row_scalar(row: pd.Series, column: str, default: object = "") -> object:
        value = row.get(column, default)
        if isinstance(value, pd.Series):
            non_null = value.dropna()
            return non_null.iloc[-1] if not non_null.empty else default
        return value

    events: list[FillEvent] = []
    for index, row in annotated.reset_index(drop=True).iterrows():
        events.append(
            FillEvent(
                ts=row_scalar(row, "timestamp", row_scalar(row, timestamp_col, "")),
                symbol=str(row_scalar(row, code_col, "")),
                side=str(row_scalar(row, side_col, "")),
                quantity=float(row_scalar(row, "qty_num", 0.0) or 0.0),
                price=float(row_scalar(row, "price_num", 0.0) or 0.0),
                fee=float(row_scalar(row, "fees_total", 0.0) or 0.0),
                event_id=str(row_scalar(row, "order_id", index)),
                source="stock_order_history",
            )
        )

    projection = project_fills(events)
    for warning in projection.warnings:
        _log.warning("estimate_realized_from_fills: %s", warning)
    return float(projection.realized_pnl)


def _load_jsonl_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            rows.append(record)
    return rows


def _epoch_fill_offset(epoch_path: Path | None) -> int:
    if epoch_path is None or not epoch_path.exists():
        return 0
    try:
        payload = json.loads(epoch_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    try:
        return max(0, int(payload.get("fills_count_at_reset", 0)))
    except (TypeError, ValueError):
        return 0


def _epoch_opening_lots(epoch_path: Path | None) -> dict[str, list[tuple[float, float]]]:
    if epoch_path is None or not epoch_path.exists():
        return {}
    try:
        payload = json.loads(epoch_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    snapshot = payload.get("account_snapshot") if isinstance(payload, dict) else {}
    positions = snapshot.get("positions") if isinstance(snapshot, dict) else None
    if not isinstance(positions, list):
        return {}
    lots: dict[str, list[tuple[float, float]]] = {}
    for row in positions:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("code") or "").strip().upper()
        qty = _optional_float(row.get("qty") or row.get("quantity")) or 0.0
        market_val = _optional_float(row.get("market_val")) or 0.0
        basis = market_val / qty if qty > 0 and market_val > 0 else 0.0
        if basis <= 0:
            for key in ("nominal_price", "last_price", "price", "cost_price", "average_cost", "avg_cost"):
                value = _optional_float(row.get(key))
                if value is not None and value > 0:
                    basis = value
                    break
        if symbol and qty > 0 and basis > 0:
            lots.setdefault(symbol, []).append((qty, basis))
    return lots


def build_stock_fills_ledger(
    fills_path: Path,
    *,
    epoch_path: Path | None = None,
):
    """Project the append-only stock fill journal into FIFO accounting state.

    This is the stock-side companion to the crypto ledger projection. It keeps
    broker/API details outside the accounting rulebook and uses the shared
    project_fills implementation.
    """

    records = _load_jsonl_records(fills_path)
    offset = _epoch_fill_offset(epoch_path)
    if offset:
        records = records[offset:]
    events: list[FillEvent] = []
    for index, row in enumerate(records, start=offset):
        events.append(
            FillEvent(
                ts=row.get("ts", ""),
                symbol=str(row.get("symbol", "")),
                side=str(row.get("side", "")),
                quantity=float(row.get("quantity", 0.0) or 0.0),
                price=float(row.get("price", 0.0) or 0.0),
                fee=float(row.get("fee", 0.0) or 0.0),
                event_id=str(row.get("event_id", index)),
                strategy=str(row.get("strategy", "")),
                source=str(row.get("source", "stock_fills")),
            )
        )
    projection = project_fills(events, opening_lots=_epoch_opening_lots(epoch_path))
    for warning in projection.warnings:
        _log.warning("build_stock_fills_ledger: %s", warning)
    return projection


def trade_log_total_fees(trade_log: pd.DataFrame) -> float:
    if trade_log.empty or "fees_total" not in trade_log.columns:
        return 0.0
    return float(pd.to_numeric(trade_log["fees_total"], errors="coerce").fillna(0.0).sum())
