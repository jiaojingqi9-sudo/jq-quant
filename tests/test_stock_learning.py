import json
from pathlib import Path

import pandas as pd

from taa_futu.futu_gateway import PlannedOrder
from taa_futu.stock_learning import (
    append_order_memory,
    build_attribution_report,
    build_learning_review_packet,
    build_promotion_report,
    build_trade_outcomes,
    generate_strategy_candidates,
    run_learning_pipeline,
)
from taa_futu.stock_runtime import append_stock_fill


def test_append_order_memory_records_submission_context(tmp_path: Path) -> None:
    memory = tmp_path / "stock_order_memory.jsonl"
    orders = [PlannedOrder("US.SPY", "BUY", 10, 101.0, 100.0, 0, 10, 0.5, "Fusion")]
    result = pd.DataFrame(
        [{"code": "US.SPY", "side": "BUY", "quantity": 10, "limit_price": 101.0, "status": "submitted", "detail": "OID-1"}]
    )

    written = append_order_memory(
        orders,
        cycle_id="cycle-1",
        stage="submitted",
        account=pd.Series({"total_assets": 10_000.0, "cash": 9_000.0, "market_val": 1_000.0}),
        positions=pd.DataFrame([{"code": "US.SPY", "qty": 0, "market_val": 0.0}]),
        target_weights={"US.SPY": 0.5},
        diagnostics={"fusion_benchmark_score": 0.2},
        result_df=result,
        order_memory_path=memory,
    )

    row = json.loads(memory.read_text(encoding="utf-8").splitlines()[0])
    assert written == 1
    assert row["order_id"] == "OID-1"
    assert row["strategy_source"] == "Fusion"
    assert row["total_assets"] == 10_000.0
    assert row["diagnostics"]["fusion_benchmark_score"] == 0.2


def test_build_trade_outcomes_fifo_round_trip(tmp_path: Path) -> None:
    fills = tmp_path / "stock_fills.jsonl"
    memory = tmp_path / "stock_order_memory.jsonl"
    outcomes_path = tmp_path / "stock_trade_outcomes.jsonl"
    memory.write_text(
        json.dumps({"order_id": "BUY-1", "strategy_source": "Fusion"}) + "\n",
        encoding="utf-8",
    )
    append_stock_fill(
        {
            "ts": "2026-03-10T14:30:00Z",
            "symbol": "US.SPY",
            "side": "BUY",
            "quantity": 10,
            "price": 100.0,
            "fee": 0.10,
            "event_id": "buy-1",
            "order_id": "BUY-1",
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
            "event_id": "sell-1",
            "order_id": "SELL-1",
        },
        fills_path=fills,
    )

    outcomes = build_trade_outcomes(fills_path=fills, order_memory_path=memory, epoch_path=None, outcome_path=outcomes_path)

    assert len(outcomes) == 1
    assert round(outcomes[0]["net_pnl"], 6) == 9.8
    assert outcomes[0]["strategy"] == "Fusion"
    assert outcomes[0]["primary_reason"] == "profitable"
    assert outcomes_path.exists()


def test_attribution_candidates_and_promotion_gate(tmp_path: Path) -> None:
    outcomes = [
        {
            "strategy": "Fusion",
            "symbol": "US.SPY",
            "primary_reason": "fees_dominated",
            "gross_pnl": 0.05,
            "fees_paid": 1.0,
            "net_pnl": -0.95,
            "return_pct": -0.001,
        }
        for _ in range(3)
    ]

    report = build_attribution_report(outcomes, attribution_path=None)
    candidates = generate_strategy_candidates(report, settings=None, candidates_path=None)
    promotion = build_promotion_report(candidates, report, promotion_path=None)

    assert any(candidate["action_type"] == "raise_min_order_value" for candidate in candidates)
    assert all(decision["live_allowed"] is False for decision in promotion["decisions"])


def test_learning_pipeline_writes_all_artifacts(tmp_path: Path) -> None:
    fills = tmp_path / "stock_fills.jsonl"
    memory = tmp_path / "stock_order_memory.jsonl"
    outcomes = tmp_path / "stock_trade_outcomes.jsonl"
    attribution = tmp_path / "stock_attribution.json"
    candidates = tmp_path / "strategy_upgrade_candidates.jsonl"
    promotion = tmp_path / "strategy_promotion_report.json"
    review_packet = tmp_path / "stock_learning_review_packet.md"
    review_packet_json = tmp_path / "stock_learning_review_packet.json"
    append_stock_fill({"ts": "2026-01-01T00:00:00Z", "symbol": "US.SPY", "side": "BUY", "quantity": 1, "price": 100, "event_id": "b"}, fills_path=fills)
    append_stock_fill({"ts": "2026-01-02T00:00:00Z", "symbol": "US.SPY", "side": "SELL", "quantity": 1, "price": 101, "event_id": "s"}, fills_path=fills)

    result = run_learning_pipeline(
        fills_path=fills,
        order_memory_path=memory,
        epoch_path=None,
        outcomes_path=outcomes,
        attribution_path=attribution,
        candidates_path=candidates,
        promotion_path=promotion,
        review_packet_path=review_packet,
        review_packet_json_path=review_packet_json,
    )

    assert result.outcome_count == 1
    assert outcomes.exists()
    assert attribution.exists()
    assert candidates.exists()
    assert promotion.exists()
    assert review_packet.exists()
    assert review_packet_json.exists()
    assert result.review_packet_path == review_packet


def test_learning_review_packet_requires_manual_code_review(tmp_path: Path) -> None:
    order_memory = tmp_path / "stock_order_memory.jsonl"
    outcomes_path = tmp_path / "stock_trade_outcomes.jsonl"
    attribution = tmp_path / "stock_attribution.json"
    candidates_path = tmp_path / "strategy_upgrade_candidates.jsonl"
    promotion_path = tmp_path / "strategy_promotion_report.json"
    review_packet = tmp_path / "stock_learning_review_packet.md"
    review_packet_json = tmp_path / "stock_learning_review_packet.json"
    order_memory.write_text(json.dumps({"order_id": "OID-1", "strategy_source": "Fusion"}) + "\n", encoding="utf-8")
    outcome_rows = [
        {
            "outcome_id": "OUT-1",
            "strategy": "Fusion",
            "symbol": "US.SPY",
            "primary_reason": "fees_dominated",
            "gross_pnl": 0.05,
            "fees_paid": 1.0,
            "net_pnl": -0.95,
            "return_pct": -0.001,
        }
    ]
    outcomes_path.write_text(json.dumps(outcome_rows[0]) + "\n", encoding="utf-8")
    report = build_attribution_report(outcome_rows, attribution_path=attribution)
    candidates = generate_strategy_candidates(report, candidates_path=candidates_path)
    promotion = build_promotion_report(candidates, report, promotion_path=promotion_path)

    packet = build_learning_review_packet(
        report,
        candidates,
        promotion,
        outcome_rows,
        order_memory_path=order_memory,
        outcomes_path=outcomes_path,
        attribution_path=attribution,
        candidates_path=candidates_path,
        promotion_path=promotion_path,
        review_packet_path=review_packet,
        review_packet_json_path=review_packet_json,
    )

    markdown = review_packet.read_text(encoding="utf-8")
    payload = json.loads(review_packet_json.read_text(encoding="utf-8"))
    assert packet["approval_policy"]["code_auto_modification"] is False
    assert packet["approval_policy"]["live_auto_promotion"] is False
    assert "不要改 crypto 系统" in packet["codex_review_prompt"]
    assert "不允许直接修改代码" in markdown
    assert payload["artifacts"]["outcomes"]["sha256"]
