from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .stock_runtime import (
    STOCK_FILLS_FILE,
    STOCK_JOURNAL_FILE,
    STOCK_LEDGER_EPOCH_FILE,
    load_stock_fill_records,
    load_stock_ledger_epoch,
)


@dataclass(frozen=True)
class StockLedgerPosting:
    account: str
    side: str
    amount: float
    currency: str = "USD"
    symbol: str = ""
    quantity: float = 0.0


@dataclass(frozen=True)
class StockJournalEntry:
    seq: int
    ts: str
    event_id: str
    event_type: str
    source: str
    description: str
    postings: tuple[StockLedgerPosting, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = ""
    event_hash: str = ""
    balanced: bool = True
    imbalance: float = 0.0


@dataclass(frozen=True)
class StockLedgerV2Projection:
    entries: tuple[StockJournalEntry, ...]
    cash_delta: float
    realized_gross_pnl: float
    fees_paid: float
    net_realized_pnl: float
    trade_count: int
    positions: dict[str, float] = field(default_factory=dict)
    avg_cost: dict[str, float] = field(default_factory=dict)
    journal_hash: str = ""
    chain_valid: bool = True
    imbalanced_entries: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class StockLedgerBreak:
    kind: str
    symbol: str
    expected: float
    actual: float
    difference: float


@dataclass(frozen=True)
class StockLedgerReconciliation:
    ok: bool
    breaks: tuple[StockLedgerBreak, ...] = ()
    checked_at: str = ""


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _round_money(value: float) -> float:
    return round(float(value), 8)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _posting_dict(posting: StockLedgerPosting) -> dict[str, object]:
    payload = asdict(posting)
    payload["amount"] = _round_money(float(payload["amount"]))
    payload["quantity"] = round(float(payload["quantity"]), 12)
    return payload


def _entry_hash(
    *,
    seq: int,
    ts: str,
    event_id: str,
    event_type: str,
    source: str,
    description: str,
    postings: Iterable[StockLedgerPosting],
    metadata: dict[str, Any],
    prev_hash: str,
) -> str:
    payload = {
        "seq": seq,
        "ts": ts,
        "event_id": event_id,
        "event_type": event_type,
        "source": source,
        "description": description,
        "postings": [_posting_dict(posting) for posting in postings],
        "metadata": metadata,
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _balance(postings: Iterable[StockLedgerPosting]) -> tuple[bool, float]:
    debit = 0.0
    credit = 0.0
    for posting in postings:
        side = str(posting.side).upper()
        if side == "DR":
            debit += float(posting.amount)
        elif side == "CR":
            credit += float(posting.amount)
    imbalance = abs(debit - credit)
    return imbalance <= 1e-6, imbalance


def _make_entry(
    *,
    seq: int,
    ts: str,
    event_id: str,
    event_type: str,
    source: str,
    description: str,
    postings: list[StockLedgerPosting],
    metadata: dict[str, Any],
    prev_hash: str,
) -> StockJournalEntry:
    balanced, imbalance = _balance(postings)
    event_hash = _entry_hash(
        seq=seq,
        ts=ts,
        event_id=event_id,
        event_type=event_type,
        source=source,
        description=description,
        postings=postings,
        metadata=metadata,
        prev_hash=prev_hash,
    )
    return StockJournalEntry(
        seq=seq,
        ts=ts,
        event_id=event_id,
        event_type=event_type,
        source=source,
        description=description,
        postings=tuple(postings),
        metadata=metadata,
        prev_hash=prev_hash,
        event_hash=event_hash,
        balanced=balanced,
        imbalance=_round_money(imbalance),
    )


def _epoch_positions(epoch: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = epoch.get("account_snapshot") if isinstance(epoch, dict) else {}
    if not isinstance(snapshot, dict):
        return []
    positions = snapshot.get("positions")
    return positions if isinstance(positions, list) else []


def _position_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("code") or "").strip().upper()


def _position_cost_price(row: dict[str, Any]) -> float:
    qty = _safe_float(row.get("qty") or row.get("quantity"))
    market_val = _safe_float(row.get("market_val"))
    if qty > 0 and market_val > 0:
        return market_val / qty
    for key in ("nominal_price", "last_price", "price", "cost_price", "average_cost", "avg_cost"):
        price = _safe_float(row.get(key))
        if price > 0:
            return price
    return 0.0


def _epoch_fill_offset(epoch_path: Path | None) -> int:
    if epoch_path is None:
        return 0
    epoch = load_stock_ledger_epoch(epoch_path)
    try:
        return max(0, int(epoch.get("fills_count_at_reset", 0)))
    except (TypeError, ValueError):
        return 0


def build_stock_double_entry_ledger(
    fills_path: Path = STOCK_FILLS_FILE,
    *,
    epoch_path: Path | None = STOCK_LEDGER_EPOCH_FILE,
) -> StockLedgerV2Projection:
    epoch = load_stock_ledger_epoch(epoch_path) if epoch_path is not None else {}
    records = load_stock_fill_records(fills_path)
    offset = _epoch_fill_offset(epoch_path)
    records = records[offset:] if offset else records

    lots: dict[str, deque[tuple[float, float]]] = {}
    entries: list[StockJournalEntry] = []
    warnings: list[str] = []
    prev_hash = hashlib.sha256(_canonical({"epoch": epoch or {}, "version": "stock-ledger-v2"}).encode("utf-8")).hexdigest()
    seq = 1

    opening_postings: list[StockLedgerPosting] = []
    snapshot = epoch.get("account_snapshot") if isinstance(epoch, dict) else {}
    if isinstance(snapshot, dict):
        cash = _safe_float(snapshot.get("cash"))
        if cash > 0:
            opening_postings.append(StockLedgerPosting("Assets:Cash:USD", "DR", cash))
            opening_postings.append(StockLedgerPosting("Equity:OpeningBalances", "CR", cash))
    for pos in _epoch_positions(epoch):
        if not isinstance(pos, dict):
            continue
        symbol = _position_symbol(pos)
        qty = _safe_float(pos.get("qty") or pos.get("quantity"))
        cost_price = _position_cost_price(pos)
        if not symbol or qty <= 0 or cost_price <= 0:
            continue
        lots.setdefault(symbol, deque()).append((qty, cost_price))
        value = qty * cost_price
        opening_postings.append(StockLedgerPosting(f"Assets:Inventory:{symbol}", "DR", value, symbol=symbol, quantity=qty))
        opening_postings.append(StockLedgerPosting("Equity:OpeningBalances", "CR", value, symbol=symbol, quantity=qty))
    if opening_postings:
        entry = _make_entry(
            seq=seq,
            ts=str(epoch.get("ts") or datetime.now(UTC).isoformat()),
            event_id="stock-ledger-epoch",
            event_type="EPOCH_OPENING",
            source="stock_ledger_epoch",
            description="Opening balances from stock ledger epoch",
            postings=opening_postings,
            metadata={"reason": epoch.get("reason", ""), "fills_count_at_reset": offset},
            prev_hash=prev_hash,
        )
        entries.append(entry)
        prev_hash = entry.event_hash
        seq += 1

    cash_delta = 0.0
    realized_gross = 0.0
    fees_paid = 0.0
    trade_count = 0

    for index, row in enumerate(records, start=offset):
        symbol = str(row.get("symbol") or "").strip().upper()
        side = str(row.get("side") or "").strip().upper()
        qty = _safe_float(row.get("quantity"))
        price = _safe_float(row.get("price"))
        fee = max(0.0, _safe_float(row.get("fee")))
        if not symbol or side not in {"BUY", "SELL"} or qty <= 0 or price <= 0:
            warnings.append(f"invalid fill skipped at fill_index={index}")
            continue
        gross = qty * price
        fees_paid += fee
        trade_count += 1
        ts = str(row.get("ts") or "")
        event_id = str(row.get("event_id") or f"stock_fill:{index}")
        source = str(row.get("source") or "stock_fills")
        postings: list[StockLedgerPosting] = []
        metadata = {
            "fill_index": index,
            "order_id": row.get("order_id", ""),
            "strategy": row.get("strategy", ""),
            "fee_source": row.get("fee_source", ""),
        }

        if side == "BUY":
            lots.setdefault(symbol, deque()).append((qty, price))
            cash_delta -= gross + fee
            postings.extend(
                [
                    StockLedgerPosting(f"Assets:Inventory:{symbol}", "DR", gross, symbol=symbol, quantity=qty),
                    StockLedgerPosting("Expenses:TradingFees", "DR", fee),
                    StockLedgerPosting("Assets:Cash:USD", "CR", gross + fee),
                ]
            )
        else:
            cash_delta += gross - fee
            symbol_lots = lots.setdefault(symbol, deque())
            remaining = qty
            matched_cost = 0.0
            matched_qty = 0.0
            while remaining > 1e-12 and symbol_lots:
                open_qty, open_price = symbol_lots[0]
                matched = min(remaining, open_qty)
                matched_cost += matched * open_price
                matched_qty += matched
                remaining -= matched
                open_qty -= matched
                if open_qty <= 1e-12:
                    symbol_lots.popleft()
                else:
                    symbol_lots[0] = (open_qty, open_price)
            matched_gross = matched_qty * price
            unmatched_gross = max(0.0, remaining * price)
            gross_pnl = matched_gross - matched_cost
            realized_gross += gross_pnl
            postings.append(StockLedgerPosting("Assets:Cash:USD", "DR", gross - fee))
            postings.append(StockLedgerPosting("Expenses:TradingFees", "DR", fee))
            if matched_cost > 0:
                postings.append(StockLedgerPosting(f"Assets:Inventory:{symbol}", "CR", matched_cost, symbol=symbol, quantity=matched_qty))
            if gross_pnl > 0:
                postings.append(StockLedgerPosting("Income:RealizedTradingGain", "CR", gross_pnl, symbol=symbol, quantity=matched_qty))
            elif gross_pnl < 0:
                postings.append(StockLedgerPosting("Expenses:RealizedTradingLoss", "DR", abs(gross_pnl), symbol=symbol, quantity=matched_qty))
            if remaining > 1e-12:
                postings.append(StockLedgerPosting(f"Suspense:UnmatchedSells:{symbol}", "CR", unmatched_gross, symbol=symbol, quantity=remaining))
                warnings.append(f"{symbol} SELL unmatched quantity {remaining:.8f}; posted to suspense")

        entry = _make_entry(
            seq=seq,
            ts=ts,
            event_id=event_id,
            event_type=f"FILL_{side}",
            source=source,
            description=f"{side} {qty:g} {symbol} @ {price:g}",
            postings=postings,
            metadata=metadata,
            prev_hash=prev_hash,
        )
        entries.append(entry)
        prev_hash = entry.event_hash
        seq += 1

    positions: dict[str, float] = {}
    avg_cost: dict[str, float] = {}
    for symbol, symbol_lots in lots.items():
        qty = sum(lot_qty for lot_qty, _price in symbol_lots)
        if qty <= 1e-10:
            continue
        cost = sum(lot_qty * price for lot_qty, price in symbol_lots)
        positions[symbol] = round(qty, 12)
        avg_cost[symbol] = round(cost / qty, 12)

    imbalanced = tuple(entry.seq for entry in entries if not entry.balanced)
    chain_valid = True
    expected_prev = hashlib.sha256(_canonical({"epoch": epoch or {}, "version": "stock-ledger-v2"}).encode("utf-8")).hexdigest()
    for entry in entries:
        if entry.prev_hash != expected_prev:
            chain_valid = False
            break
        recalculated = _entry_hash(
            seq=entry.seq,
            ts=entry.ts,
            event_id=entry.event_id,
            event_type=entry.event_type,
            source=entry.source,
            description=entry.description,
            postings=entry.postings,
            metadata=entry.metadata,
            prev_hash=entry.prev_hash,
        )
        if recalculated != entry.event_hash:
            chain_valid = False
            break
        expected_prev = entry.event_hash

    return StockLedgerV2Projection(
        entries=tuple(entries),
        cash_delta=float(cash_delta),
        realized_gross_pnl=float(realized_gross),
        fees_paid=float(fees_paid),
        net_realized_pnl=float(realized_gross - fees_paid),
        trade_count=trade_count,
        positions=dict(sorted(positions.items())),
        avg_cost=dict(sorted(avg_cost.items())),
        journal_hash=entries[-1].event_hash if entries else prev_hash,
        chain_valid=chain_valid and not imbalanced,
        imbalanced_entries=imbalanced,
        warnings=tuple(warnings),
    )


def write_stock_journal(
    projection: StockLedgerV2Projection,
    *,
    journal_path: Path = STOCK_JOURNAL_FILE,
) -> Path:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = journal_path.with_suffix(journal_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for entry in projection.entries:
            payload = asdict(entry)
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    tmp.replace(journal_path)
    return journal_path


def _broker_position_qty(positions: pd.DataFrame) -> dict[str, float]:
    if positions.empty or "code" not in positions.columns:
        return {}
    frame = positions.copy()
    qty_col = "qty" if "qty" in frame.columns else "quantity"
    if qty_col not in frame.columns:
        return {}
    frame[qty_col] = pd.to_numeric(frame[qty_col], errors="coerce").fillna(0.0)
    grouped = frame.groupby("code", as_index=True)[qty_col].sum()
    return {str(symbol).upper(): float(qty) for symbol, qty in grouped.items() if abs(float(qty)) > 1e-10}


def reconcile_stock_ledger(
    projection: StockLedgerV2Projection,
    *,
    positions: pd.DataFrame,
    account: pd.Series | dict[str, Any] | None = None,
    epoch: dict[str, Any] | None = None,
    quantity_tolerance: float = 1e-6,
    cash_tolerance: float = 5.0,
) -> StockLedgerReconciliation:
    breaks: list[StockLedgerBreak] = []
    broker_qty = _broker_position_qty(positions)
    for symbol in sorted(set(projection.positions) | set(broker_qty)):
        expected = float(projection.positions.get(symbol, 0.0))
        actual = float(broker_qty.get(symbol, 0.0))
        diff = actual - expected
        if abs(diff) > quantity_tolerance:
            breaks.append(StockLedgerBreak("position_qty", symbol, expected, actual, diff))

    snapshot = (epoch or {}).get("account_snapshot") if isinstance(epoch, dict) else {}
    start_cash = _safe_float(snapshot.get("cash")) if isinstance(snapshot, dict) else 0.0
    if account is not None and start_cash > 0:
        if isinstance(account, pd.Series):
            actual_cash = _safe_float(account.get("cash", account.get("cash_balance", 0.0)))
        else:
            actual_cash = _safe_float(account.get("cash", account.get("cash_balance", 0.0)))
        expected_cash = start_cash + projection.cash_delta
        diff = actual_cash - expected_cash
        if abs(diff) > cash_tolerance:
            breaks.append(StockLedgerBreak("cash", "USD", expected_cash, actual_cash, diff))

    return StockLedgerReconciliation(
        ok=not breaks and projection.chain_valid,
        breaks=tuple(breaks),
        checked_at=datetime.now(UTC).isoformat(),
    )
