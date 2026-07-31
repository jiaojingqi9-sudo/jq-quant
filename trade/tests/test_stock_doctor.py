import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from taa_futu.stock_doctor import run_stock_system_doctor
from taa_futu.stock_ledger import StockLedgerBreak, StockLedgerReconciliation


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        stack_active_strategy=None,
        stack_baseline_enabled=True,
        stack_baseline_weight=0.25,
        stack_fusion_weight=0.25,
        stack_ofim_weight=0.25,
        stack_cascade_weight=0.25,
        watchdog_stale_status_seconds=240,
    )


def test_stock_doctor_flags_missing_unified_epoch(tmp_path) -> None:
    report = run_stock_system_doctor(
        _settings(),
        epoch_path=tmp_path / "stock_ledger_epoch.json",
        split_state_path=tmp_path / "strategy_split_state.json",
        review_packet_path=tmp_path / "stock_learning_review_packet.json",
        auto_status_path=tmp_path / "auto_trader_status.json",
        watchdog_status_path=tmp_path / "watchdog_status.json",
    )

    assert report.status == "fail"
    summaries = [finding.summary for finding in report.findings]
    assert any("股票事件账本还没有统一起点" in summary for summary in summaries)
    assert any(finding.fix_command == ".venv/bin/taa-futu stock-system-reset" for finding in report.findings)


def test_stock_doctor_accepts_coherent_runtime_contracts(tmp_path) -> None:
    now = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
    epoch = tmp_path / "stock_ledger_epoch.json"
    split = tmp_path / "strategy_split_state.json"
    packet = tmp_path / "stock_learning_review_packet.json"
    auto = tmp_path / "auto_trader_status.json"
    watchdog = tmp_path / "watchdog_status.json"
    # account_snapshot 必须带 total_assets：那是「Epoch 后总盈亏」的减数，
    # 也是界面与 Doctor 判定 Epoch 可用的依据。原来这里写空字典，等于把一份
    # 并不自洽的 Epoch 当成基准，于是线上那个真实故障被测试放行了——
    # 2026-07-31 查出：epoch 只有 cash 没有 total_assets，界面判「未设置」、
    # Doctor 判「已设置」，券商对账被整块禁用。
    epoch.write_text(
        json.dumps({
            "ts": now.isoformat(),
            "account_snapshot": {"total_assets": 1_000_000.0, "cash": 1_000_000.0,
                                 "market_val": 0.0, "position_count": 0, "positions": []},
            "fills_count_at_reset": 0,
        }),
        encoding="utf-8",
    )
    split.write_text(
        json.dumps(
            {
                "reset_at": now.isoformat(),
                "base_total_assets": 1_000_000.0,
                "strategies": {
                    "baseline": {"weight": 0.25, "start_cash": 250_000.0},
                    "fusion": {"weight": 0.25, "start_cash": 250_000.0},
                    "ofim": {"weight": 0.25, "start_cash": 250_000.0},
                    "cascade": {"weight": 0.25, "start_cash": 250_000.0},
                },
            }
        ),
        encoding="utf-8",
    )
    packet.write_text(
        json.dumps({"generated_at": now.isoformat(), "packet_id": "P1", "evidence": {"realized_outcomes": 1, "candidate_count": 0}}),
        encoding="utf-8",
    )
    auto.write_text(json.dumps({"updated_at": now.isoformat(), "action": "idle"}), encoding="utf-8")
    watchdog.write_text(json.dumps({"health": "healthy", "action": "healthy"}), encoding="utf-8")

    report = run_stock_system_doctor(
        _settings(),
        now_utc=now + timedelta(minutes=5),
        epoch_path=epoch,
        split_state_path=split,
        review_packet_path=packet,
        auto_status_path=auto,
        watchdog_status_path=watchdog,
    )

    assert report.status == "ok"
    assert report.ok is True


def test_stock_doctor_flags_split_weight_drift(tmp_path) -> None:
    now = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
    epoch = tmp_path / "stock_ledger_epoch.json"
    split = tmp_path / "strategy_split_state.json"
    packet = tmp_path / "stock_learning_review_packet.json"
    # account_snapshot 必须带 total_assets：那是「Epoch 后总盈亏」的减数，
    # 也是界面与 Doctor 判定 Epoch 可用的依据。原来这里写空字典，等于把一份
    # 并不自洽的 Epoch 当成基准，于是线上那个真实故障被测试放行了——
    # 2026-07-31 查出：epoch 只有 cash 没有 total_assets，界面判「未设置」、
    # Doctor 判「已设置」，券商对账被整块禁用。
    epoch.write_text(
        json.dumps({
            "ts": now.isoformat(),
            "account_snapshot": {"total_assets": 1_000_000.0, "cash": 1_000_000.0,
                                 "market_val": 0.0, "position_count": 0, "positions": []},
            "fills_count_at_reset": 0,
        }),
        encoding="utf-8",
    )
    split.write_text(
        json.dumps(
            {
                "reset_at": now.isoformat(),
                "strategies": {
                    "baseline": {"weight": 0.25, "start_cash": 250_000.0},
                    "fusion": {"weight": 0.0, "start_cash": 0.0},
                    "ofim": {"weight": 0.5, "start_cash": 500_000.0},
                    "cascade": {"weight": 0.25, "start_cash": 250_000.0},
                },
            }
        ),
        encoding="utf-8",
    )
    packet.write_text(json.dumps({"generated_at": now.isoformat(), "packet_id": "P1", "evidence": {}}), encoding="utf-8")

    report = run_stock_system_doctor(
        _settings(),
        now_utc=now,
        epoch_path=epoch,
        split_state_path=split,
        review_packet_path=packet,
        auto_status_path=tmp_path / "auto.json",
        watchdog_status_path=tmp_path / "watchdog.json",
    )

    assert report.status == "warn"
    assert any(finding.area == "strategy_split" and finding.status == "warn" for finding in report.findings)


def test_stock_doctor_does_not_recommend_reset_for_post_epoch_reconciliation_break(tmp_path) -> None:
    now = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
    epoch = tmp_path / "stock_ledger_epoch.json"
    split = tmp_path / "strategy_split_state.json"
    packet = tmp_path / "stock_learning_review_packet.json"
    # account_snapshot 必须带 total_assets：那是「Epoch 后总盈亏」的减数，
    # 也是界面与 Doctor 判定 Epoch 可用的依据。原来这里写空字典，等于把一份
    # 并不自洽的 Epoch 当成基准，于是线上那个真实故障被测试放行了——
    # 2026-07-31 查出：epoch 只有 cash 没有 total_assets，界面判「未设置」、
    # Doctor 判「已设置」，券商对账被整块禁用。
    epoch.write_text(
        json.dumps({
            "ts": now.isoformat(),
            "account_snapshot": {"total_assets": 1_000_000.0, "cash": 1_000_000.0,
                                 "market_val": 0.0, "position_count": 0, "positions": []},
            "fills_count_at_reset": 0,
        }),
        encoding="utf-8",
    )
    split.write_text(
        json.dumps(
            {
                "reset_at": now.isoformat(),
                "strategies": {
                    "baseline": {"weight": 0.25},
                    "fusion": {"weight": 0.25},
                    "ofim": {"weight": 0.25},
                    "cascade": {"weight": 0.25},
                },
            }
        ),
        encoding="utf-8",
    )
    packet.write_text(json.dumps({"generated_at": now.isoformat(), "packet_id": "P1", "evidence": {}}), encoding="utf-8")
    reconciliation = StockLedgerReconciliation(
        ok=False,
        breaks=(StockLedgerBreak("position_qty", "US.SPY", 10.0, 9.0, -1.0),),
    )

    report = run_stock_system_doctor(
        _settings(),
        now_utc=now,
        epoch_path=epoch,
        split_state_path=split,
        review_packet_path=packet,
        auto_status_path=tmp_path / "auto.json",
        watchdog_status_path=tmp_path / "watchdog.json",
        reconciliation=reconciliation,
    )

    finding = next(item for item in report.findings if item.area == "broker_reconciliation")
    assert finding.status == "warn"
    assert finding.fix_command == ""
