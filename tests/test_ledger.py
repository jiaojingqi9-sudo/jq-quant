from taa_futu.ledger import FillEvent, project_fills


def test_project_fills_fifo_realized_includes_buy_and_sell_fees() -> None:
    projection = project_fills(
        [
            FillEvent("2026-01-01T00:00:00Z", "BTCUSDT", "BUY", 1.0, 100.0, 0.10, event_id="1"),
            FillEvent("2026-01-01T00:01:00Z", "BTCUSDT", "SELL", 1.0, 110.0, 0.11, event_id="2"),
        ]
    )

    assert round(projection.cash_delta, 8) == 9.79
    assert round(projection.realized_pnl, 8) == 9.79
    assert round(projection.fees_paid, 8) == 0.21
    assert projection.positions == {}
    assert projection.unmatched_sells == {}


def test_project_fills_keeps_fifo_remaining_cost_basis() -> None:
    projection = project_fills(
        [
            FillEvent("2026-01-01T00:00:00Z", "ETHUSDT", "BUY", 2.0, 100.0, 2.0, event_id="1"),
            FillEvent("2026-01-01T00:01:00Z", "ETHUSDT", "BUY", 2.0, 120.0, 0.0, event_id="2"),
            FillEvent("2026-01-01T00:02:00Z", "ETHUSDT", "SELL", 3.0, 130.0, 3.0, event_id="3"),
        ]
    )

    assert round(projection.realized_pnl, 8) == 65.0
    assert projection.positions == {"ETHUSDT": 1.0}
    assert projection.avg_cost == {"ETHUSDT": 120.0}


def test_project_fills_surfaces_unmatched_sells_without_fabricating_pnl() -> None:
    projection = project_fills(
        [
            FillEvent("2026-01-01T00:00:00Z", "SOLUSDT", "SELL", 5.0, 20.0, 0.05, event_id="1"),
        ]
    )

    assert projection.realized_pnl == 0.0
    assert projection.unmatched_sells == {"SOLUSDT": 5.0}
    assert projection.warnings


def test_project_fills_matches_sells_against_opening_lots() -> None:
    projection = project_fills(
        [
            FillEvent("2026-01-01T00:01:00Z", "SOLUSDT", "SELL", 2.0, 22.0, 0.2, event_id="1"),
        ],
        opening_lots={"SOLUSDT": [(5.0, 20.0)]},
    )

    assert round(projection.realized_pnl, 8) == 3.8
    assert projection.positions == {"SOLUSDT": 3.0}
    assert projection.unmatched_sells == {}


def test_project_fills_audit_hash_is_stable_after_sorting() -> None:
    first = [
        FillEvent("2026-01-01T00:01:00Z", "BTCUSDT", "SELL", 1.0, 110.0, 0.11, event_id="2"),
        FillEvent("2026-01-01T00:00:00Z", "BTCUSDT", "BUY", 1.0, 100.0, 0.10, event_id="1"),
    ]
    second = list(reversed(first))

    assert project_fills(first).audit_hash == project_fills(second).audit_hash
