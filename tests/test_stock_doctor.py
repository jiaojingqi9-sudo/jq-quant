import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from taa_futu import stock_doctor
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
    # 修复命令按当前系统生成（Windows 是 .venv\\Scripts\\），所以只断言子命令
    assert any(finding.fix_command.endswith("taa-futu stock-system-reset") for finding in report.findings)


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


# ── 修复命令的路径 ─────────────────────────────────────────────────────────
# 以前这里写死 ".venv/bin/taa-futu"。Windows 上 venv 的可执行文件在
# .venv\Scripts\ 下，照着界面上的命令敲会报「系统找不到指定的路径」。


def test_fix_command_points_at_the_running_venv(monkeypatch, tmp_path) -> None:
    binv = tmp_path / ".venv" / "bin"
    binv.mkdir(parents=True)
    (binv / "taa-futu").write_text("")
    monkeypatch.chdir(tmp_path)

    command = stock_doctor._fix("stock-system-reset", exe=str(binv / "python"), on_windows=False)
    assert command == ".venv/bin/taa-futu stock-system-reset"


def test_fix_command_uses_scripts_dir_on_windows(monkeypatch, tmp_path) -> None:
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "taa-futu.exe").write_text("")
    monkeypatch.chdir(tmp_path)

    command = stock_doctor._fix("stock-system-reset", exe=str(scripts / "python.exe"), on_windows=True)
    # 分隔符在 POSIX 上跑测试时仍是 /（Path 是 PosixPath），所以只断言实质：
    # 找的是 Scripts 目录、去掉了 .exe、子命令没丢。
    assert "Scripts" in command
    assert ".exe" not in command
    assert command.endswith("taa-futu stock-system-reset")


def test_fix_command_falls_back_per_platform(tmp_path) -> None:
    """解释器旁边没有 taa-futu 时按平台给默认值。"""
    assert stock_doctor._fix("x", exe=str(tmp_path / "python.exe"), on_windows=True) == ".venv\\Scripts\\taa-futu x"
    assert stock_doctor._fix("x", exe=str(tmp_path / "python"), on_windows=False) == ".venv/bin/taa-futu x"


# ── 两个起点的时间差 ───────────────────────────────────────────────────────
# 以前只比时间差：差超过 120 秒就报 warn 并让人跑 stock-system-reset。
# 可账本起点被有意回溯是常见做法（reason=broker_history_rebuild：让账本覆盖
# 账户真实开始以来的全部历史），分账实验从另一天起算。按旧规则，那份刻意
# 设置会一直挂着黄牌，而「修复」它要把账本起点改成今天、丢掉之前的归因起点。


def _write_pair(tmp_path, epoch_ts, epoch_reason, split_ts, split_reason):
    epoch = tmp_path / "stock_ledger_epoch.json"
    split = tmp_path / "strategy_split_state.json"
    epoch.write_text(json.dumps({
        "ts": epoch_ts.isoformat(),
        "reason": epoch_reason,
        "account_snapshot": {"total_assets": 1_000_000.0, "cash": 1_000_000.0,
                             "market_val": 0.0, "position_count": 0, "positions": []},
        "fills_count_at_reset": 0,
    }), encoding="utf-8")
    split.write_text(json.dumps({
        "reset_at": split_ts.isoformat(),
        "reason": split_reason,
        "base_total_assets": 1_000_000.0,
        "strategies": {name: {"weight": 0.25, "start_cash": 250_000.0}
                       for name in ("baseline", "fusion", "ofim", "cascade")},
    }), encoding="utf-8")
    return epoch, split


def _epoch_finding(tmp_path, **kwargs):
    now = datetime(2026, 6, 2, 3, 22, tzinfo=UTC)
    epoch, split = _write_pair(tmp_path, **kwargs)
    report = run_stock_system_doctor(
        _settings(),
        now_utc=now,
        epoch_path=epoch,
        split_state_path=split,
        review_packet_path=tmp_path / "packet.json",
        auto_status_path=tmp_path / "auto.json",
        watchdog_status_path=tmp_path / "watchdog.json",
    )
    return next(item for item in report.findings if item.area == "system_epoch")


def test_backdated_epoch_is_not_reported_as_a_fault(tmp_path) -> None:
    """账本起点回溯到 4/8、分账从 6/2 起——这是有意的，不该报 warn。"""
    finding = _epoch_finding(
        tmp_path,
        epoch_ts=datetime(2026, 4, 8, tzinfo=UTC),
        epoch_reason="broker_history_rebuild (opening cash inferred from current account)",
        split_ts=datetime(2026, 6, 2, 3, 22, tzinfo=UTC),
        split_reason="manual_stock_system_epoch",
    )
    assert finding.status == "info"
    assert finding.fix_command == ""       # 不该怂恿人去跑不可逆的 reset
    assert "2026-04-08" in finding.detail and "2026-06-02" in finding.detail


def test_two_reset_written_starts_that_drifted_are_still_flagged(tmp_path) -> None:
    """两份都出自 stock-system-reset 却对不上——那是真出了问题。"""
    finding = _epoch_finding(
        tmp_path,
        epoch_ts=datetime(2026, 5, 1, tzinfo=UTC),
        epoch_reason="manual_stock_system_epoch",
        split_ts=datetime(2026, 6, 2, 3, 22, tzinfo=UTC),
        split_reason="manual_stock_system_epoch",
    )
    assert finding.status == "warn"
    assert finding.fix_command.endswith("taa-futu stock-system-reset")


def test_same_moment_starts_are_ok(tmp_path) -> None:
    moment = datetime(2026, 6, 2, 3, 22, tzinfo=UTC)
    finding = _epoch_finding(
        tmp_path,
        epoch_ts=moment, epoch_reason="manual_stock_system_epoch",
        split_ts=moment, split_reason="manual_stock_system_epoch",
    )
    assert finding.status == "ok"
