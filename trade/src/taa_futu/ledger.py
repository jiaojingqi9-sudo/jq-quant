from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Iterable, Mapping


@dataclass(frozen=True)
class FillEvent:
    """Immutable fill event used to derive portfolio/accounting state."""

    ts: object
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float = 0.0
    event_id: str = ""
    strategy: str = ""
    source: str = ""


@dataclass(frozen=True)
class LedgerProjection:
    cash_delta: float
    realized_pnl: float
    fees_paid: float
    trade_count: int
    positions: dict[str, float] = field(default_factory=dict)
    avg_cost: dict[str, float] = field(default_factory=dict)
    unmatched_sells: dict[str, float] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    audit_hash: str = ""


def _finite_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _normalized_events(events: Iterable[FillEvent], *, sort_events: bool) -> list[FillEvent]:
    clean: list[FillEvent] = []
    for event in events:
        symbol = str(event.symbol or "").strip().upper()
        side = str(event.side or "").strip().upper()
        qty = _finite_float(event.quantity)
        price = _finite_float(event.price)
        fee = max(0.0, _finite_float(event.fee))
        if not symbol or side not in {"BUY", "SELL"} or qty <= 0 or price <= 0:
            continue
        clean.append(
            FillEvent(
                ts=event.ts,
                symbol=symbol,
                side=side,
                quantity=qty,
                price=price,
                fee=fee,
                event_id=str(event.event_id or ""),
                strategy=str(event.strategy or ""),
                source=str(event.source or ""),
            )
        )
    if sort_events:
        clean.sort(key=lambda item: (str(item.ts or ""), item.event_id, item.symbol, item.side))
    return clean


def _normalized_opening_lots(
    opening_lots: Mapping[str, Iterable[tuple[float, float]]] | None,
) -> dict[str, tuple[tuple[float, float], ...]]:
    if not opening_lots:
        return {}
    normalized: dict[str, tuple[tuple[float, float], ...]] = {}
    for raw_symbol, raw_lots in opening_lots.items():
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol:
            continue
        clean_lots: list[tuple[float, float]] = []
        for raw_qty, raw_basis in raw_lots:
            qty = _finite_float(raw_qty)
            basis = _finite_float(raw_basis)
            if qty > 0 and basis > 0:
                clean_lots.append((qty, basis))
        if clean_lots:
            normalized[symbol] = tuple(clean_lots)
    return dict(sorted(normalized.items()))


def _audit_hash(events: list[FillEvent], opening_lots: dict[str, tuple[tuple[float, float], ...]] | None = None) -> str:
    event_payload = [
        {
            "ts": str(event.ts or ""),
            "symbol": event.symbol,
            "side": event.side,
            "quantity": round(event.quantity, 12),
            "price": round(event.price, 12),
            "fee": round(event.fee, 12),
            "event_id": event.event_id,
            "strategy": event.strategy,
            "source": event.source,
        }
        for event in events
    ]
    if opening_lots:
        payload: object = {
            "opening_lots": {
                symbol: [[round(qty, 12), round(basis, 12)] for qty, basis in lots]
                for symbol, lots in opening_lots.items()
            },
            "events": event_payload,
        }
    else:
        payload = event_payload
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def project_fills(
    events: Iterable[FillEvent],
    *,
    sort_events: bool = True,
    opening_lots: Mapping[str, Iterable[tuple[float, float]]] | None = None,
) -> LedgerProjection:
    """Project fills into cash, inventory and realized P&L using FIFO lots.

    This function intentionally has no broker/API dependencies. It is the single
    accounting rulebook shared by stock and crypto modules.
    """

    clean_events = _normalized_events(events, sort_events=sort_events)
    opening = _normalized_opening_lots(opening_lots)
    lots: dict[str, deque[tuple[float, float]]] = {
        symbol: deque(symbol_lots) for symbol, symbol_lots in opening.items()
    }
    cash_delta = 0.0
    realized = 0.0
    fees_paid = 0.0
    unmatched: dict[str, float] = {}

    for event in clean_events:
        qty = event.quantity
        gross = qty * event.price
        fees_paid += event.fee
        symbol_lots = lots.setdefault(event.symbol, deque())
        if event.side == "BUY":
            fee_per_unit = event.fee / qty if qty > 0 else 0.0
            basis_price = event.price + fee_per_unit
            symbol_lots.append((qty, basis_price))
            cash_delta -= gross + event.fee
            continue

        cash_delta += gross - event.fee
        sell_fee_per_unit = event.fee / qty if qty > 0 else 0.0
        remaining = qty
        while remaining > 1e-12 and symbol_lots:
            open_qty, open_basis = symbol_lots[0]
            matched = min(remaining, open_qty)
            realized += (event.price - sell_fee_per_unit - open_basis) * matched
            remaining -= matched
            open_qty -= matched
            if open_qty <= 1e-12:
                symbol_lots.popleft()
            else:
                symbol_lots[0] = (open_qty, open_basis)
        if remaining > 1e-12:
            unmatched[event.symbol] = unmatched.get(event.symbol, 0.0) + remaining

    positions: dict[str, float] = {}
    avg_cost: dict[str, float] = {}
    for symbol, symbol_lots in lots.items():
        qty = sum(lot_qty for lot_qty, _basis in symbol_lots)
        if qty <= 1e-10:
            continue
        cost = sum(lot_qty * basis for lot_qty, basis in symbol_lots)
        positions[symbol] = qty
        avg_cost[symbol] = cost / qty if qty > 0 else 0.0

    warnings = tuple(
        f"{symbol} SELL unmatched quantity {qty:.8f}; excluded from realized P&L"
        for symbol, qty in sorted(unmatched.items())
        if qty > 1e-10
    )
    return LedgerProjection(
        cash_delta=float(cash_delta),
        realized_pnl=float(realized),
        fees_paid=float(fees_paid),
        trade_count=len(clean_events),
        positions={symbol: round(qty, 12) for symbol, qty in sorted(positions.items())},
        avg_cost={symbol: round(cost, 12) for symbol, cost in sorted(avg_cost.items())},
        unmatched_sells={symbol: round(qty, 12) for symbol, qty in sorted(unmatched.items()) if qty > 1e-10},
        warnings=warnings,
        audit_hash=_audit_hash(clean_events, opening),
    )
