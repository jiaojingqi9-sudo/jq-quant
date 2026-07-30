import json
from pathlib import Path

from taa_futu.costs import build_stock_fills_ledger
from taa_futu.stock_runtime import (
    append_stock_fill,
    load_recorded_stock_fill_ids,
    load_stock_order_fill_cumulatives,
    load_stock_ledger_epoch,
    write_stock_ledger_epoch,
)


def test_stock_fill_log_projects_fifo_ledger(tmp_path: Path) -> None:
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

    projection = build_stock_fills_ledger(fills)

    assert round(projection.realized_pnl, 6) == 9.8
    assert round(projection.fees_paid, 6) == 0.2
    assert projection.positions == {}
    assert load_recorded_stock_fill_ids(fills) == {"1", "2"}


def test_stock_ledger_epoch_filters_older_fills(tmp_path: Path) -> None:
    fills = tmp_path / "stock_fills.jsonl"
    epoch = tmp_path / "stock_ledger_epoch.json"
    for event_id, side, price in [("1", "BUY", 100.0), ("2", "SELL", 101.0), ("3", "BUY", 50.0)]:
        append_stock_fill(
            {
                "ts": f"2026-03-10T14:3{event_id}:00Z",
                "symbol": "US.SPY" if event_id != "3" else "US.QQQ",
                "side": side,
                "quantity": 10,
                "price": price,
                "fee": 0.0,
                "event_id": event_id,
            },
            fills_path=fills,
        )
    epoch.write_text(
        json.dumps({"ts": "2026-03-10T15:01:00Z", "fills_count_at_reset": 2}),
        encoding="utf-8",
    )

    projection = build_stock_fills_ledger(fills, epoch_path=epoch)

    assert projection.trade_count == 1
    assert projection.positions == {"US.QQQ": 10.0}


def test_stock_ledger_epoch_seeds_opening_positions_at_reset_mark(tmp_path: Path) -> None:
    fills = tmp_path / "stock_fills.jsonl"
    epoch = tmp_path / "stock_ledger_epoch.json"
    epoch.write_text(
        json.dumps(
            {
                "ts": "2026-03-10T15:01:00Z",
                "fills_count_at_reset": 0,
                "account_snapshot": {
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
            "ts": "2026-03-10T15:10:00Z",
            "symbol": "US.SPY",
            "side": "SELL",
            "quantity": 5,
            "price": 110.0,
            "fee": 0.5,
            "event_id": "sell-after-reset",
        },
        fills_path=fills,
    )

    projection = build_stock_fills_ledger(fills, epoch_path=epoch)

    assert projection.unmatched_sells == {}
    assert projection.positions == {"US.SPY": 5.0}
    assert round(projection.realized_pnl, 6) == 49.5


def test_write_stock_ledger_epoch_records_fill_count(tmp_path: Path) -> None:
    fills = tmp_path / "stock_fills.jsonl"
    epoch = tmp_path / "stock_ledger_epoch.json"
    append_stock_fill({"event_id": "1", "symbol": "US.SPY", "side": "BUY", "quantity": 1, "price": 1}, fills_path=fills)

    write_stock_ledger_epoch(
        reason="test",
        account_snapshot={"total_assets": 1000},
        fills_path=fills,
        epoch_path=epoch,
    )

    payload = load_stock_ledger_epoch(epoch)
    assert payload["reason"] == "test"
    assert payload["fills_count_at_reset"] == 1
    assert payload["account_snapshot"]["total_assets"] == 1000


def test_stock_order_fill_cumulatives_sum_incremental_records(tmp_path: Path) -> None:
    fills = tmp_path / "stock_fills.jsonl"
    append_stock_fill({"order_id": "42", "quantity": 5, "price": 100, "fee": 0.1}, fills_path=fills)
    append_stock_fill({"order_id": "42", "quantity": 5, "price": 102, "fee": 0.1}, fills_path=fills)

    cumulative = load_stock_order_fill_cumulatives(fills)

    assert cumulative["42"]["quantity"] == 10
    assert cumulative["42"]["notional"] == 1010
    assert round(cumulative["42"]["fee"], 6) == 0.2
