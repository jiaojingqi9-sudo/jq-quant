from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from taa_futu import crypto_research_loop
from taa_futu.crypto_backtest import CryptoBacktestProfile


def _write_dataset(path: Path, *, profitable: bool) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for idx in range(120):
        price = 100 + idx * 0.4 if profitable else 100.0
        rows.append(
            {
                "ts": (start + timedelta(minutes=idx)).isoformat(),
                "sleeve": "spot",
                "symbol": "BTCUSDT",
                "price": price,
                "score": 0.5,
                "eligible": True,
                "spread_bps": 0.1,
                "funding_rate": 0.0,
                "source": "test",
            }
        )
        rows.append(
            {
                "ts": (start + timedelta(minutes=idx)).isoformat(),
                "sleeve": "perp",
                "symbol": "BTCUSDT",
                "price": 140 - idx * 0.3 if profitable else 100.0,
                "score": -0.6,
                "eligible": True,
                "spread_bps": 0.1,
                "funding_rate": 0.0,
                "source": "test",
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _patch_artifacts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(crypto_research_loop, "TRIALS_FILE", tmp_path / "trials.jsonl")
    monkeypatch.setattr(crypto_research_loop, "BEST_CANDIDATE_FILE", tmp_path / "best_candidate.json")
    monkeypatch.setattr(crypto_research_loop, "LOCKED_TEST_REPORT_FILE", tmp_path / "locked_test_report.json")
    monkeypatch.setattr(crypto_research_loop, "RESEARCH_PATCH_REPORT_FILE", tmp_path / "research_patch_report.md")
    monkeypatch.setattr(crypto_research_loop, "PROFILE_DIR", tmp_path / "profiles")


def test_candidate_profiles_cover_fee_drag_conservative_cases_with_small_budget() -> None:
    profiles = crypto_research_loop._candidate_profiles(4)

    assert [profile.name for profile in profiles] == [
        "fee_drag_high_conviction",
        "fee_drag_sparse_signal",
        "perp_maker_cooldown",
        "market_cost_stress",
    ]
    assert max(profile.entry_threshold for profile in profiles) >= 0.60
    assert max(profile.min_trade_interval_bars for profile in profiles) >= 30


def test_selection_score_penalizes_inert_no_trade_profiles() -> None:
    no_trade = {
        "net_pnl": 0.0,
        "initial_equity": 10_000.0,
        "max_drawdown": 0.0,
        "trade_count": 0,
    }
    active_small_loss = {
        "net_pnl": -1.0,
        "initial_equity": 10_000.0,
        "max_drawdown": 0.0,
        "trade_count": 20,
    }

    assert crypto_research_loop._selection_score(active_small_loss) > crypto_research_loop._selection_score(no_trade)


def test_selection_score_penalizes_fee_and_slippage_drag() -> None:
    low_drag = {
        "net_pnl": 5.0,
        "gross_pnl": 8.0,
        "initial_equity": 10_000.0,
        "max_drawdown": 0.0,
        "trade_count": 20,
        "fees_paid": 1.0,
        "slippage_paid": 1.0,
        "funding_paid": 0.0,
    }
    high_drag = {
        **low_drag,
        "gross_pnl": 15.0,
        "fees_paid": 6.0,
        "slippage_paid": 4.0,
    }

    assert crypto_research_loop._selection_score(low_drag) > crypto_research_loop._selection_score(high_drag)


def test_research_loop_writes_failure_artifacts_without_live_promotion(monkeypatch, tmp_path: Path) -> None:
    _patch_artifacts(monkeypatch, tmp_path)
    data_file = tmp_path / "data" / "rows.jsonl"
    _write_dataset(data_file, profitable=False)

    result = crypto_research_loop.run_crypto_research_loop(
        max_trials=4,
        data_file=data_file,
        build_data_if_missing=False,
    )

    assert result["trial_count"] == 4
    assert (tmp_path / "trials.jsonl").exists()
    assert (tmp_path / "best_candidate.json").exists()
    assert (tmp_path / "locked_test_report.json").exists()
    assert (tmp_path / "research_patch_report.md").exists()
    best = json.loads((tmp_path / "best_candidate.json").read_text(encoding="utf-8"))
    locked = json.loads((tmp_path / "locked_test_report.json").read_text(encoding="utf-8"))
    assert best["live_auto_promotion"] is False
    assert locked["live_auto_promotion"] is False
    assert not (tmp_path / ".env").exists()


def test_research_loop_writes_profile_only_after_validation_and_locked_pass(monkeypatch, tmp_path: Path) -> None:
    _patch_artifacts(monkeypatch, tmp_path)
    data_file = tmp_path / "data" / "rows.jsonl"
    _write_dataset(data_file, profitable=True)
    monkeypatch.setattr(
        crypto_research_loop,
        "_candidate_profiles",
        lambda max_trials: [
            CryptoBacktestProfile(
                name="profitable_test_profile",
                entry_threshold=0.2,
                exit_threshold=0.1,
                max_holding_bars=500,
                min_trades=1,
                min_trade_interval_bars=1,
            )
        ],
    )

    result = crypto_research_loop.run_crypto_research_loop(
        max_trials=4,
        data_file=data_file,
        build_data_if_missing=False,
    )

    profile_path = result["artifacts"]["profile"]
    locked = json.loads((tmp_path / "locked_test_report.json").read_text(encoding="utf-8"))
    assert locked["passed_locked_test"] is True
    assert result["best_candidate"]["passed_validation"] is True
    assert profile_path
    assert Path(profile_path).exists()
    assert result["best_candidate"]["live_auto_promotion"] is False
