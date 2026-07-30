import json
from pathlib import Path

import pandas as pd

from taa_futu.stock_ledger import (
    build_stock_double_entry_ledger,
    reconcile_stock_ledger,
    write_stock_journal,
)
from taa_futu.stock_runtime import append_stock_fill


def test_stock_double_entry_ledger_balances_and_hashes(tmp_path: Path) -> None:
    fills = tmp_path / "stock_fills.jsonl"
    append_stock_fill(
        {
            "ts": "2026-03-10T14:30:00Z",
            "symbol": "US.SPY",
            "side": "BUY",
            "quantity": 10,
            "price": 100.0,
            "fee": 0.10,
            "event_id": "1",
        },
        fills_path=fills,
    )
    append_stock_fill(
        {
            "ts": "2026-03-10T15:00:00Z",
            "symbol": "US.SPY",
            "side": "SELL",
            "quantity": 10,
            "price": 101.0,
            "fee": 0.10,
            "event_id": "2",
        },
        fills_path=fills,
    )

    projection = build_stock_double_entry_ledger(fills, epoch_path=None)

    assert projection.chain_valid is True
    assert projection.imbalanced_entries == ()
    assert round(projection.realized_gross_pnl, 6) == 10.0
    assert round(projection.fees_paid, 6) == 0.2
    assert round(projection.net_realized_pnl, 6) == 9.8
    assert projection.positions == {}
    assert len(projection.journal_hash) == 64


def test_stock_double_entry_ledger_uses_epoch_opening_positions(tmp_path: Path) -> None:
    fills = tmp_path / "stock_fills.jsonl"
    epoch = tmp_path / "stock_ledger_epoch.json"
    epoch.write_text(
        json.dumps(
            {
                "ts": "2026-03-10T14:00:00Z",
                "fills_count_at_reset": 0,
                "account_snapshot": {
                    "cash": 900.0,
                    "positions": [
                        {"code": "US.SPY", "qty": 10, "cost_price": 100.0},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    append_stock_fill(
        {
            "ts": "2026-03-10T15:00:00Z",
            "symbol": "US.SPY",
            "side": "SELL",
            "quantity": 5,
            "price": 110.0,
            "fee": 0.5,
            "event_id": "sell-1",
        },
        fills_path=fills,
    )

    projection = build_stock_double_entry_ledger(fills, epoch_path=epoch)

    assert projection.trade_count == 1
    assert projection.positions == {"US.SPY": 5.0}
    assert projection.avg_cost == {"US.SPY": 100.0}
    assert round(projection.net_realized_pnl, 6) == 49.5
    assert projection.entries[0].event_type == "EPOCH_OPENING"


def test_stock_double_entry_ledger_uses_epoch_market_value_as_opening_basis(tmp_path: Path) -> None:
    fills = tmp_path / "stock_fills.jsonl"
    epoch = tmp_path / "stock_ledger_epoch.json"
    epoch.write_text(
        json.dumps(
            {
                "ts": "2026-03-10T14:00:00Z",
                "fills_count_at_reset": 0,
                "account_snapshot": {
                    "cash": 0.0,
                    "positions": [
                        {"code": "US.SPY", "qty": 10, "cost_price": 50.0, "market_val": 1000.0},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    append_stock_fill(
        {
            "ts": "2026-03-10T15:00:00Z",
            "symbol": "US.SPY",
            "side": "SELL",
            "quantity": 5,
            "price": 110.0,
            "fee": 0.5,
            "event_id": "sell-1",
        },
        fills_path=fills,
    )

    projection = build_stock_double_entry_ledger(fills, epoch_path=epoch)

    assert projection.positions == {"US.SPY": 5.0}
    assert projection.avg_cost == {"US.SPY": 100.0}
    assert round(projection.net_realized_pnl, 6) == 49.5


def test_stock_ledger_reconciliation_allows_small_cash_rounding_break(tmp_path: Path) -> None:
    fills = tmp_path / "stock_fills.jsonl"
    append_stock_fill(
        {"ts": "2026-03-10T14:30:00Z", "symbol": "US.SPY", "side": "BUY", "quantity": 1, "price": 100.0, "event_id": "1"},
        fills_path=fills,
    )
    projection = build_stock_double_entry_ledger(fills, epoch_path=None)

    reconciliation = reconcile_stock_ledger(
        projection,
        positions=pd.DataFrame([{"code": "US.SPY", "qty": 1}]),
        account={"cash": 903.0},
        epoch={"account_snapshot": {"cash": 1000.0}},
    )

    assert reconciliation.ok is True


def test_stock_ledger_reconciliation_detects_position_break(tmp_path: Path) -> None:
    fills = tmp_path / "stock_fills.jsonl"
    append_stock_fill(
        {"ts": "2026-03-10T14:30:00Z", "symbol": "US.SPY", "side": "BUY", "quantity": 10, "price": 100.0, "event_id": "1"},
        fills_path=fills,
    )
    projection = build_stock_double_entry_ledger(fills, epoch_path=None)
    positions = pd.DataFrame([{"code": "US.SPY", "qty": 9}])

    reconciliation = reconcile_stock_ledger(projection, positions=positions)

    assert reconciliation.ok is False
    assert reconciliation.breaks[0].kind == "position_qty"
    assert reconciliation.breaks[0].difference == -1.0


def test_write_stock_journal_materializes_jsonl(tmp_path: Path) -> None:
    fills = tmp_path / "stock_fills.jsonl"
    journal = tmp_path / "stock_journal.jsonl"
    append_stock_fill(
        {"ts": "2026-03-10T14:30:00Z", "symbol": "US.SPY", "side": "BUY", "quantity": 1, "price": 100.0, "event_id": "1"},
        fills_path=fills,
    )
    projection = build_stock_double_entry_ledger(fills, epoch_path=None)

    write_stock_journal(projection, journal_path=journal)

    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["event_hash"] == projection.journal_hash
    assert rows[0]["balanced"] is True
