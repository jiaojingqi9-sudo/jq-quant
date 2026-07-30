from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pandas as pd

from taa_futu import crypto_backtest
from taa_futu.crypto_backtest import CryptoBacktestProfile, run_crypto_backtest, split_time_series


def _rows(*, sleeve: str, prices: list[float], score: float, symbol: str = "BTCUSDT", funding_rate: float = 0.0) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return pd.DataFrame(
        [
            {
                "ts": (start + timedelta(minutes=idx)).isoformat(),
                "sleeve": sleeve,
                "symbol": symbol,
                "price": price,
                "score": score,
                "eligible": True,
                "spread_bps": 0.1,
                "funding_rate": funding_rate,
                "source": "test",
            }
            for idx, price in enumerate(prices)
        ]
    )


def test_crypto_time_split_is_ordered_without_leakage() -> None:
    frame = _rows(sleeve="spot", prices=[100 + idx for idx in range(100)], score=0.5)

    splits = split_time_series(frame)

    assert len(splits["train"]["ts"].unique()) == 60
    assert len(splits["validation"]["ts"].unique()) == 20
    assert len(splits["locked_test"]["ts"].unique()) == 20
    assert splits["train"]["ts"].max() < splits["validation"]["ts"].min()
    assert splits["validation"]["ts"].max() < splits["locked_test"]["ts"].min()


def test_spot_backtest_charges_fees_and_stays_long_only() -> None:
    prices = [100 + idx * 0.5 for idx in range(80)]
    frame = _rows(sleeve="spot", prices=prices, score=0.5)
    profile = CryptoBacktestProfile(
        entry_threshold=0.2,
        exit_threshold=0.1,
        max_holding_bars=500,
        min_trades=1,
        edge_bps_per_score=100.0,
    )

    result = run_crypto_backtest(frame, sleeve="spot", profile=profile)

    assert result.net_pnl > 0
    assert result.fees_paid > 0
    assert result.trade_count >= 1


def test_spot_backtest_blocks_entries_when_edge_does_not_cover_cost() -> None:
    prices = [100 + idx * 0.5 for idx in range(80)]
    frame = _rows(sleeve="spot", prices=prices, score=0.5)
    profile = CryptoBacktestProfile(
        entry_threshold=0.2,
        exit_threshold=0.1,
        max_holding_bars=500,
        min_trades=1,
        edge_bps_per_score=1.0,
        cost_buffer_bps=1_000.0,
    )

    result = run_crypto_backtest(frame, sleeve="spot", profile=profile)

    assert result.trade_count == 0
    assert result.fees_paid == 0


def test_perp_backtest_can_profit_from_short() -> None:
    prices = [140 - idx * 0.5 for idx in range(80)]
    frame = _rows(sleeve="perp", prices=prices, score=-0.6)
    profile = CryptoBacktestProfile(entry_threshold=0.2, exit_threshold=0.1, max_holding_bars=500, min_trades=1)

    result = run_crypto_backtest(frame, sleeve="perp", profile=profile)

    assert result.net_pnl > 0
    assert result.trade_count >= 1


def test_perp_backtest_includes_funding_and_execution_costs() -> None:
    prices = [100.0 for _ in range(80)]
    frame = _rows(sleeve="perp", prices=prices, score=0.6, funding_rate=0.0003)
    profile = CryptoBacktestProfile(
        entry_threshold=0.2,
        exit_threshold=0.1,
        max_holding_bars=500,
        min_trades=1,
        order_style="market",
        slippage_bps=5.0,
    )

    result = run_crypto_backtest(frame, sleeve="perp", profile=profile)

    assert result.fees_paid > 0
    assert result.slippage_paid > 0
    assert result.funding_paid > 0
    assert result.net_pnl < result.gross_pnl


def test_backtest_gate_blocks_cost_drag_dominated_positive_results() -> None:
    profile = CryptoBacktestProfile(min_trades=1, max_cost_drag_ratio=0.60)

    failures = crypto_backtest._gate_failure_reasons(
        profile=profile,
        net_pnl=1.0,
        gross_pnl=10.0,
        max_drawdown=0.0,
        trade_count=3,
        fees_paid=4.0,
        slippage_paid=3.0,
        funding_paid=0.0,
    )

    assert "cost_drag_over_limit" in failures


def test_backtest_dataset_manifest_records_hash_and_gaps(monkeypatch, tmp_path: Path) -> None:
    spot_features = tmp_path / "spot_features.jsonl"
    perp_features = tmp_path / "perp_features.jsonl"
    spot_features.write_text(
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "symbol": "BTCUSDT", "last_price": 100, "score": 0.5, "eligible": True}) + "\n",
        encoding="utf-8",
    )
    perp_features.write_text(
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "symbol": "BTCUSDT", "last_price": 100, "score": -0.5, "eligible": True}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(crypto_backtest, "SPOT_FEATURES_FILE", spot_features)
    monkeypatch.setattr(crypto_backtest, "PERP_FEATURES_FILE", perp_features)
    data_file = tmp_path / "data" / "rows.jsonl"
    manifest_file = tmp_path / "data" / "manifest.json"

    manifest = crypto_backtest.build_crypto_backtest_dataset(
        symbols=["BTCUSDT"],
        include_public=False,
        include_local=True,
        data_file=data_file,
        manifest_file=manifest_file,
    )

    assert manifest["row_count"] == 2
    assert manifest["sha256"]
    assert data_file.exists()
    assert manifest_file.exists()
