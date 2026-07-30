import json
from datetime import UTC, datetime
from pathlib import Path

from taa_futu.crypto_learning import (
    append_order_memory,
    build_attribution_report,
    build_learning_review_packet,
    build_trade_outcomes,
    generate_upgrade_candidates,
    load_learning_review_packet,
    load_upgrade_candidates,
    run_learning_pipeline,
)
from taa_futu.crypto_ofim import CryptoOfimOrder


def _order(**overrides) -> CryptoOfimOrder:
    base = dict(
        ts="2026-03-10T14:30:00+00:00",
        mode="paper",
        symbol="BTCUSDT",
        side="BUY",
        quantity=1.0,
        price=100.0,
        notional=100.0,
        fee=0.1,
        status="planned",
        reason="rebalance_to_ofim_target",
        target_weight=0.5,
        current_value=0.0,
        target_value=100.0,
        response=None,
    )
    base.update(overrides)
    return CryptoOfimOrder(**base)


def test_append_order_memory_records_crypto_specific_fields(tmp_path: Path) -> None:
    memory = tmp_path / "crypto_order_memory.jsonl"

    count = append_order_memory(
        [_order()],
        cycle_id="cycle-1",
        stage="planned",
        settings=type("Settings", (), {"mode": "paper", "base_url": "https://api.binance.com", "slippage_bps": 5.0})(),
        plan=type("Plan", (), {"benchmark_score": 0.2, "market_sources": {"BTCUSDT": "ws_cache"}, "benchmark_trend": {"ok": True}})(),
        account={"equity": 1000.0, "cash": 900.0, "market_value": 100.0},
        order_memory_path=memory,
    )

    row = json.loads(memory.read_text(encoding="utf-8").splitlines()[0])
    assert count == 1
    assert row["record_type"] == "crypto_order_memory"
    assert row["exchange"] == "binance"
    assert row["venue"] == "binance_spot_global"
    assert row["instrument_type"] == "spot"
    assert row["leverage"] == 1.0
    assert row["funding_rate"] == 0.0
    assert row["market_regime_24h"] == "risk_on"


def test_load_upgrade_candidates_tails_large_jsonl(tmp_path: Path) -> None:
    candidates = tmp_path / "crypto_upgrade_candidates.jsonl"
    candidates.write_text(
        "\n".join(json.dumps({"candidate_id": f"c{i}"}) for i in range(5)) + "\n",
        encoding="utf-8",
    )

    rows = load_upgrade_candidates(candidates, tail=2)

    assert [row["candidate_id"] for row in rows] == ["c3", "c4"]


def test_append_order_memory_uses_request_latency_not_exchange_timestamp(tmp_path: Path) -> None:
    memory = tmp_path / "crypto_order_memory.jsonl"

    append_order_memory(
        [
            _order(
                status="submitted_testnet",
                response={
                    "orderId": 123,
                    "status": "FILLED",
                    "type": "MARKET",
                    "transactTime": 1778118169639,
                    "workingTime": 1778118169639,
                    "_request_latency_ms": 42.5,
                    "executedQty": "1.0",
                },
            )
        ],
        cycle_id="cycle-1",
        stage="submitted",
        order_memory_path=memory,
    )
    append_order_memory(
        [
            _order(
                status="submitted_testnet",
                response={
                    "orderId": 124,
                    "status": "FILLED",
                    "type": "MARKET",
                    "transactTime": 1778118169640,
                    "executedQty": "1.0",
                },
            )
        ],
        cycle_id="cycle-2",
        stage="submitted",
        order_memory_path=memory,
    )

    first, second = [json.loads(line) for line in memory.read_text(encoding="utf-8").splitlines()]
    assert first["exchange_latency_ms"] == 42.5
    assert first["exchange_event_time_ms"] == 1778118169639.0
    assert second["exchange_latency_ms"] is None
    assert second["exchange_event_time_ms"] == 1778118169640.0


def test_build_trade_outcomes_pairs_fifo_with_slippage_and_fees(tmp_path: Path) -> None:
    orders = tmp_path / "orders.jsonl"
    memory = tmp_path / "crypto_order_memory.jsonl"
    outcomes_path = tmp_path / "crypto_trade_outcomes.jsonl"
    epoch = tmp_path / "ledger_epoch.json"
    user_fills = tmp_path / "user_fills.jsonl"

    append_order_memory([_order(price=99.0)], cycle_id="c1", stage="planned", order_memory_path=memory)
    append_order_memory([_order(side="SELL", price=111.0, status="filled_paper")], cycle_id="c2", stage="filled", order_memory_path=memory)
    rows = [
        _order(status="filled_paper").__dict__,
        _order(ts="2026-03-10T15:30:00+00:00", side="SELL", price=110.0, notional=110.0, fee=0.1, status="filled_paper").__dict__,
    ]
    orders.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    outcomes = build_trade_outcomes(
        mode="paper",
        quote_asset="USDT",
        orders_path=orders,
        user_fills_path=user_fills,
        order_memory_path=memory,
        epoch_path=epoch,
        outcome_path=outcomes_path,
    )

    assert len(outcomes) == 1
    assert round(outcomes[0]["gross_pnl"], 6) == 10.0
    assert round(outcomes[0]["net_pnl"], 6) == 9.8
    assert outcomes[0]["fees"] == 0.2
    assert outcomes[0]["hold_seconds"] == 3600.0
    assert outcomes[0]["slippage_bps"] != 0


def test_build_trade_outcomes_estimates_zero_fee_testnet_commissions(tmp_path: Path) -> None:
    orders = tmp_path / "orders.jsonl"
    memory = tmp_path / "crypto_order_memory.jsonl"
    outcomes_path = tmp_path / "crypto_trade_outcomes.jsonl"
    epoch = tmp_path / "ledger_epoch.json"
    user_fills = tmp_path / "user_fills.jsonl"

    settings = type("Settings", (), {"mode": "testnet", "base_url": "https://testnet.binance.vision", "fee_rate": 0.001})()
    append_order_memory(
        [
            _order(
                mode="testnet",
                status="submitted_testnet",
                response={"orderId": 7, "status": "FILLED", "type": "MARKET", "executedQty": "1.0"},
            )
        ],
        cycle_id="buy",
        stage="submitted",
        settings=settings,
        order_memory_path=memory,
    )
    append_order_memory(
        [
            _order(
                ts="2026-03-10T15:30:00+00:00",
                mode="testnet",
                side="SELL",
                price=99.0,
                notional=99.0,
                status="submitted_testnet",
                response={"orderId": 8, "status": "FILLED", "type": "MARKET", "executedQty": "1.0"},
            )
        ],
        cycle_id="sell",
        stage="submitted",
        settings=settings,
        order_memory_path=memory,
    )
    rows = [
        _order(
            mode="testnet",
            status="submitted_testnet",
            fee=0.0,
            response={
                "orderId": 7,
                "status": "FILLED",
                "executedQty": "1.0",
                "cummulativeQuoteQty": "100.0",
                "fills": [{"price": "100.0", "qty": "1.0", "commission": "0.0", "commissionAsset": "BTC"}],
            },
        ).__dict__,
        _order(
            ts="2026-03-10T15:30:00+00:00",
            mode="testnet",
            side="SELL",
            price=99.0,
            notional=99.0,
            fee=0.0,
            status="submitted_testnet",
            response={
                "orderId": 8,
                "status": "FILLED",
                "executedQty": "1.0",
                "cummulativeQuoteQty": "99.0",
                "fills": [{"price": "99.0", "qty": "1.0", "commission": "0.0", "commissionAsset": "USDT"}],
            },
        ).__dict__,
    ]
    orders.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    outcomes = build_trade_outcomes(
        mode="testnet",
        quote_asset="USDT",
        orders_path=orders,
        user_fills_path=user_fills,
        order_memory_path=memory,
        epoch_path=epoch,
        outcome_path=outcomes_path,
    )

    assert len(outcomes) == 1
    assert round(outcomes[0]["fees"], 6) == 0.199
    assert round(outcomes[0]["net_pnl"], 6) == -1.199
    assert outcomes[0]["fees_estimated"] is True
    assert outcomes[0]["fee_sources"] == ["order_context_fee_rate"]
    assert outcomes[0]["estimated_fee_rate"] == 0.001


def test_build_trade_outcomes_matches_duplicate_testnet_order_ids_by_symbol_side(tmp_path: Path) -> None:
    memory = tmp_path / "crypto_order_memory.jsonl"
    user_fills = tmp_path / "user_fills.jsonl"
    orders = tmp_path / "orders.jsonl"
    epoch = tmp_path / "ledger_epoch.json"

    submitted_at = datetime(2026, 3, 10, tzinfo=UTC)
    for symbol, buy_price, sell_price in (("BTCUSDT", 100.0, 110.0), ("SOLUSDT", 50.0, 51.0)):
        append_order_memory(
            [
                _order(
                    mode="testnet",
                    symbol=symbol,
                    side="BUY",
                    price=buy_price,
                    notional=buy_price,
                    status="submitted_testnet",
                    response={"orderId": 7, "status": "FILLED", "type": "MARKET", "executedQty": "1.0", "_request_latency_ms": 20.0},
                )
            ],
            cycle_id=f"{symbol}-buy",
            stage="submitted",
            order_memory_path=memory,
            now_utc=submitted_at,
        )
        append_order_memory(
            [
                _order(
                    mode="testnet",
                    symbol=symbol,
                    side="SELL",
                    price=sell_price,
                    notional=sell_price,
                    status="submitted_testnet",
                    response={"orderId": 8, "status": "FILLED", "type": "MARKET", "executedQty": "1.0", "_request_latency_ms": 22.0},
                )
            ],
            cycle_id=f"{symbol}-sell",
            stage="submitted",
            order_memory_path=memory,
            now_utc=submitted_at,
        )

    fill_rows = [
        {
            "ts": "2026-03-10T00:00:01+00:00",
            "mode": "testnet",
            "symbol": symbol,
            "side": side,
            "quantity": 1.0,
            "price": price,
            "notional": price,
            "fee": 0.0,
            "event_id": f"{symbol}:{side}",
            "order_id": str(order_id),
        }
        for symbol, buy_price, sell_price in (("BTCUSDT", 100.0, 110.0), ("SOLUSDT", 50.0, 51.0))
        for side, price, order_id in (("BUY", buy_price, 7), ("SELL", sell_price, 8))
    ]
    user_fills.write_text("\n".join(json.dumps(row) for row in fill_rows) + "\n", encoding="utf-8")

    outcomes = build_trade_outcomes(
        mode="testnet",
        quote_asset="USDT",
        orders_path=orders,
        user_fills_path=user_fills,
        order_memory_path=memory,
        epoch_path=epoch,
        outcome_path=None,
    )

    by_symbol = {row["symbol"]: row for row in outcomes}
    assert set(by_symbol) == {"BTCUSDT", "SOLUSDT"}
    assert by_symbol["BTCUSDT"]["gross_pnl"] == 10.0
    assert by_symbol["SOLUSDT"]["gross_pnl"] == 1.0
    assert all(abs(row["slippage_bps"]) < 1e-9 for row in outcomes)


def test_build_trade_outcomes_keeps_order_log_events_with_duplicate_order_ids_across_symbols(tmp_path: Path) -> None:
    orders = tmp_path / "orders.jsonl"
    memory = tmp_path / "crypto_order_memory.jsonl"
    outcomes_path = tmp_path / "crypto_trade_outcomes.jsonl"
    epoch = tmp_path / "ledger_epoch.json"
    user_fills = tmp_path / "user_fills.jsonl"
    rows = []
    for symbol, buy_price, sell_price in (("BTCUSDT", 100.0, 110.0), ("SOLUSDT", 50.0, 51.0)):
        rows.extend(
            [
                _order(
                    ts="2026-03-10T00:00:01+00:00",
                    mode="testnet",
                    symbol=symbol,
                    side="BUY",
                    price=buy_price,
                    notional=buy_price,
                    status="submitted_testnet",
                    response={"orderId": 9, "status": "FILLED", "executedQty": "1.0", "cummulativeQuoteQty": str(buy_price)},
                ).__dict__,
                _order(
                    ts="2026-03-10T00:01:01+00:00",
                    mode="testnet",
                    symbol=symbol,
                    side="SELL",
                    price=sell_price,
                    notional=sell_price,
                    status="submitted_testnet",
                    response={"orderId": 10, "status": "FILLED", "executedQty": "1.0", "cummulativeQuoteQty": str(sell_price)},
                ).__dict__,
            ]
        )
    orders.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    outcomes = build_trade_outcomes(
        mode="testnet",
        quote_asset="USDT",
        orders_path=orders,
        user_fills_path=user_fills,
        order_memory_path=memory,
        epoch_path=epoch,
        outcome_path=outcomes_path,
    )

    assert {row["symbol"] for row in outcomes} == {"BTCUSDT", "SOLUSDT"}


def test_attribution_candidates_and_packet_are_review_only(tmp_path: Path) -> None:
    outcomes = [
        {
            "symbol": "BTCUSDT",
            "strategy": "OFIM",
            "venue": "binance_spot_global",
            "timeframe": "under_1h",
            "market_regime_24h": "neutral",
            "primary_reason": "signal_error",
            "net_pnl": -5.0,
            "gross_pnl": -4.0,
            "fees": 1.0,
            "return_pct": -0.05,
            "slippage_bps": 2.0,
            "reason_tags": ["signal_error", "fast_noise_loss"],
        }
        for _ in range(3)
    ]
    report = build_attribution_report(outcomes, order_memory=[], attribution_path=None)
    candidates = generate_upgrade_candidates(
        report,
        settings=type("Settings", (), {"entry_threshold": 0.2, "min_order_notional": 20.0, "max_spread_bps": 20.0})(),
        candidates_path=None,
    )

    assert report["by_reason_tag"]["fast_noise_loss"]["trades"] == 3
    assert any(item["action_type"] == "tighten_entry_threshold" for item in candidates)
    assert all(item["status"] == "research_only" for item in candidates)
    assert all("auto_modify_code" in item["forbidden_actions"] for item in candidates)


def test_generate_upgrade_candidates_surfaces_fast_noise_churn_cooldown(tmp_path: Path) -> None:
    outcomes = [
        {
            "symbol": "ETHUSDT",
            "strategy": "OFIM",
            "venue": "binance_spot_testnet",
            "timeframe": "under_5m",
            "market_regime_24h": "risk_off",
            "primary_reason": "fees_dominated",
            "reason_tags": ["fees_dominated", "signal_error", "fast_noise_loss"],
            "net_pnl": -2.5,
            "gross_pnl": -0.5,
            "fees": 2.0,
            "return_pct": -0.002,
            "slippage_bps": 1.0,
        }
        for _ in range(12)
    ]
    report = build_attribution_report(outcomes, order_memory=[], attribution_path=None)

    candidates = generate_upgrade_candidates(
        report,
        settings=type(
            "Settings",
            (),
            {
                "entry_threshold": 0.24,
                "min_order_notional": 20.0,
                "max_spread_bps": 20.0,
                "min_reentry_after_risk_off_seconds": 900,
            },
        )(),
        candidates_path=None,
    )

    cooldown = [item for item in candidates if item["action_type"] == "extend_risk_off_reentry_cooldown"]
    assert cooldown
    assert cooldown[0]["param"] == "CRYPTO_OFIM_MIN_REENTRY_AFTER_RISK_OFF_SECONDS"
    assert cooldown[0]["proposed_value"] == 1800
    assert cooldown[0]["evidence"]["trades"] == 12


def test_review_packet_markdown_surfaces_order_quality_risks(tmp_path: Path) -> None:
    packet_md = tmp_path / "packet.md"
    packet_json = tmp_path / "packet.json"
    report = {
        "total": {"trades": 12, "avg_slippage_bps": 12.5},
        "order_quality": {
            "records": 4,
            "rejected": 0,
            "partial_fill": 0,
            "stale_book": 0,
            "avg_exchange_latency_ms": None,
            "valid_exchange_latency_count": 0,
            "invalid_exchange_latency_count": 2,
        },
    }

    build_learning_review_packet(
        report,
        candidates=[],
        promotion={"decisions": []},
        outcomes=[],
        order_memory_path=tmp_path / "memory.jsonl",
        outcomes_path=tmp_path / "outcomes.jsonl",
        attribution_path=tmp_path / "attribution.json",
        candidates_path=tmp_path / "candidates.jsonl",
        promotion_path=tmp_path / "promotion.json",
        review_packet_path=packet_md,
        review_packet_json_path=packet_json,
    )
    markdown = packet_md.read_text(encoding="utf-8")

    assert "## Order Quality" in markdown
    assert "| invalid_exchange_latency_count | 2 |" in markdown
    assert "## Data Quality Risks" in markdown
    assert "`exchange_latency_ms` has 2 invalid historical rows" in markdown
    assert "Average slippage is 12.5 bps" in markdown


def test_attribution_report_estimates_submitted_order_memory_fee_drag(tmp_path: Path) -> None:
    memory = tmp_path / "crypto_order_memory.jsonl"
    settings = type("Settings", (), {"mode": "testnet", "base_url": "https://testnet.binance.vision", "fee_rate": 0.001})()
    append_order_memory(
        [
            _order(mode="testnet", symbol="BTCUSDT", status="submitted_testnet", fee=0.0, notional=100.0)
            for _ in range(20)
        ],
        cycle_id="submitted",
        stage="submitted",
        settings=settings,
        order_memory_path=memory,
    )
    rows = [json.loads(line) for line in memory.read_text(encoding="utf-8").splitlines()]

    report = build_attribution_report([], order_memory=rows, attribution_path=None)
    candidates = generate_upgrade_candidates(report, settings=settings, candidates_path=None)

    quality = report["order_quality"]
    assert quality["submitted_records"] == 20
    assert quality["submitted_notional"] == 2000.0
    assert quality["submitted_logged_fees"] == 0.0
    assert quality["submitted_estimated_fees"] == 2.0
    assert quality["submitted_cost_by_symbol"]["BTCUSDT"]["estimated_fees"] == 2.0
    assert any(item["action_type"] == "investigate_order_memory_fee_drag" for item in candidates)


def test_upgrade_candidates_surface_historical_epoch_fee_drag() -> None:
    report = {
        "total": {"trades": 0},
        "order_quality": {
            "submitted_records": 0,
            "submitted_estimated_fees": 0.0,
            "submitted_cost_by_symbol": {},
        },
        "order_quality_scope": {
            "scope": "ledger_epoch",
            "excluded_records": 40,
            "epoch_id": "epoch-new",
            "epoch_ts": "2026-03-10T15:00:00+00:00",
        },
        "historical_order_quality": {
            "submitted_records": 40,
            "submitted_estimated_fees": 4.0,
            "submitted_cost_by_symbol": {
                "BTCUSDT": {
                    "records": 30,
                    "notional": 3000.0,
                    "estimated_fees": 3.0,
                    "taker_records": 30,
                },
                "ETHUSDT": {
                    "records": 10,
                    "notional": 1000.0,
                    "estimated_fees": 1.0,
                    "taker_records": 10,
                },
            },
        },
    }

    candidates = generate_upgrade_candidates(report, candidates_path=None)

    historical = [item for item in candidates if item["action_type"] == "keep_spot_guarded_after_historical_fee_drag"]
    assert historical
    evidence = historical[0]["evidence"]
    assert evidence["current_epoch_submitted_records"] == 0
    assert evidence["historical_submitted_records"] == 40
    assert evidence["historical_submitted_estimated_fees"] == 4.0
    assert evidence["excluded_records"] == 40
    assert evidence["epoch_id"] == "epoch-new"
    assert evidence["top_symbols"][0]["symbol"] == "BTCUSDT"


def test_attribution_report_surfaces_perp_fee_drag(tmp_path: Path) -> None:
    perp_orders = tmp_path / "perp_orders.jsonl"
    perp_status = tmp_path / "perp_status.json"
    perp_state = tmp_path / "perp_state.json"
    packet_md = tmp_path / "packet.md"
    packet_json = tmp_path / "packet.json"

    perp_orders.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "ts": "2026-03-10T14:30:00+00:00",
                    "mode": "paper",
                    "symbol": "BTCUSDT",
                    "status": "filled",
                    "notional": 250.0,
                    "fee": 0.5,
                    "response": {"paper_realized_pnl": 0.1},
                },
                {
                    "ts": "2026-03-10T14:31:00+00:00",
                    "mode": "paper",
                    "symbol": "ETHUSDT",
                    "status": "filled",
                    "notional": 250.0,
                    "fee": 0.5,
                    "response": {"paper_realized_pnl": 0.2},
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    perp_status.write_text(
        json.dumps(
            {
                "updated_at": "2026-03-10T14:32:00+00:00",
                "status": "submitted",
                "mode": "paper",
                "reason": "perp_loss_guard_fees",
                "account": {
                    "realized_pnl": 0.3,
                    "fees_paid": 1.0,
                    "funding_paid": 0.1,
                    "net_pnl": -0.8,
                    "trade_count": 2,
                    "pnl_source": "local_perp_paper",
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_attribution_report(
        [],
        order_memory=[],
        perp_orders_path=perp_orders,
        perp_status_path=perp_status,
        perp_state_path=perp_state,
        attribution_path=None,
    )
    candidates = generate_upgrade_candidates(report, candidates_path=None)
    build_learning_review_packet(
        report,
        candidates,
        promotion={"decisions": []},
        outcomes=[],
        order_memory_path=tmp_path / "memory.jsonl",
        outcomes_path=tmp_path / "outcomes.jsonl",
        attribution_path=tmp_path / "attribution.json",
        candidates_path=tmp_path / "candidates.jsonl",
        promotion_path=tmp_path / "promotion.json",
        review_packet_path=packet_md,
        review_packet_json_path=packet_json,
    )

    perp = report["perp_order_evidence"]
    assert perp["filled_records"] == 2
    assert perp["net_pnl"] == -0.8
    assert perp["drag_reason"] == "fees_exceed_gross_pnl"
    assert perp["by_symbol"]["BTCUSDT"]["net_after_fees"] == -0.4
    assert any(item["action_type"] == "keep_perp_guarded_until_fee_drag_retest" for item in candidates)
    markdown = packet_md.read_text(encoding="utf-8")
    assert "## Perp PnL Evidence" in markdown
    assert "| net_pnl | -0.8 |" in markdown
    assert "Perp paper evidence is negative" in markdown


def test_run_learning_pipeline_writes_review_packet(tmp_path: Path) -> None:
    orders = tmp_path / "orders.jsonl"
    memory = tmp_path / "crypto_order_memory.jsonl"
    outcomes = tmp_path / "crypto_trade_outcomes.jsonl"
    attribution = tmp_path / "crypto_attribution.json"
    candidates = tmp_path / "crypto_upgrade_candidates.jsonl"
    promotion = tmp_path / "crypto_promotion_report.json"
    packet_md = tmp_path / "crypto_learning_review_packet.md"
    packet_json = tmp_path / "crypto_learning_review_packet.json"
    user_fills = tmp_path / "user_fills.jsonl"
    epoch = tmp_path / "ledger_epoch.json"
    rows = [
        _order(status="filled_paper").__dict__,
        _order(ts="2026-03-10T15:30:00+00:00", side="SELL", price=110.0, notional=110.0, fee=0.1, status="filled_paper").__dict__,
    ]
    orders.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    result = run_learning_pipeline(
        mode="paper",
        quote_asset="USDT",
        orders_path=orders,
        user_fills_path=user_fills,
        order_memory_path=memory,
        epoch_path=epoch,
        outcomes_path=outcomes,
        attribution_path=attribution,
        candidates_path=candidates,
        promotion_path=promotion,
        review_packet_path=packet_md,
        review_packet_json_path=packet_json,
    )
    packet = load_learning_review_packet(packet_json)

    assert result.outcome_count == 1
    assert packet["approval_policy"]["live_auto_promotion"] is False
    assert packet["approval_policy"]["code_auto_modification"] is False
    assert packet["artifacts"]["outcomes"]["sha256"]
    assert "Crypto Evidence-to-Review Packet" in packet_md.read_text(encoding="utf-8")


def test_run_learning_pipeline_scopes_order_quality_to_ledger_epoch(tmp_path: Path) -> None:
    orders = tmp_path / "orders.jsonl"
    memory = tmp_path / "crypto_order_memory.jsonl"
    outcomes = tmp_path / "crypto_trade_outcomes.jsonl"
    attribution = tmp_path / "crypto_attribution.json"
    candidates = tmp_path / "crypto_upgrade_candidates.jsonl"
    promotion = tmp_path / "crypto_promotion_report.json"
    packet_md = tmp_path / "crypto_learning_review_packet.md"
    packet_json = tmp_path / "crypto_learning_review_packet.json"
    user_fills = tmp_path / "user_fills.jsonl"
    epoch = tmp_path / "ledger_epoch.json"
    settings = type("Settings", (), {"mode": "testnet", "base_url": "https://testnet.binance.vision", "fee_rate": 0.001})()

    append_order_memory(
        [
            _order(
                ts="2026-03-10T14:30:00+00:00",
                mode="testnet",
                status="submitted_testnet",
                fee=0.0,
                notional=100.0,
            )
        ],
        cycle_id="old",
        stage="submitted",
        settings=settings,
        order_memory_path=memory,
        now_utc=datetime(2026, 3, 10, 14, 30, tzinfo=UTC),
    )
    append_order_memory(
        [
            _order(
                ts="2026-03-10T15:30:00+00:00",
                mode="testnet",
                status="submitted_testnet",
                fee=0.0,
                notional=200.0,
            )
        ],
        cycle_id="new",
        stage="submitted",
        settings=settings,
        order_memory_path=memory,
        now_utc=datetime(2026, 3, 10, 15, 30, tzinfo=UTC),
    )
    epoch.write_text(
        json.dumps(
            {
                "ts": "2026-03-10T15:00:00+00:00",
                "mode": "testnet",
                "quote_asset": "USDT",
                "reason": "manual_testnet_reconciliation_reset",
                "epoch_id": "epoch-new",
            }
        ),
        encoding="utf-8",
    )

    run_learning_pipeline(
        mode="testnet",
        quote_asset="USDT",
        settings=settings,
        orders_path=orders,
        user_fills_path=user_fills,
        order_memory_path=memory,
        epoch_path=epoch,
        outcomes_path=outcomes,
        attribution_path=attribution,
        candidates_path=candidates,
        promotion_path=promotion,
        review_packet_path=packet_md,
        review_packet_json_path=packet_json,
    )

    report = json.loads(attribution.read_text(encoding="utf-8"))
    quality = report["order_quality"]

    assert quality["submitted_records"] == 1
    assert quality["submitted_notional"] == 200.0
    assert quality["submitted_estimated_fees"] == 0.2
    assert report["order_quality_scope"]["scope"] == "ledger_epoch"
    assert report["order_quality_scope"]["excluded_records"] == 1
    assert report["order_quality_scope"]["epoch_id"] == "epoch-new"
    assert report["historical_order_quality"]["submitted_records"] == 2
    assert "pre-epoch rows" in packet_md.read_text(encoding="utf-8")
