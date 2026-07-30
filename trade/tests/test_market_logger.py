"""Tests for the market_logger module.

We verify that each log_* function:
  1. Creates the expected JSONL file in a temp directory.
  2. Writes valid JSON on each call.
  3. Appends correctly on successive calls.
  4. Never raises even when given bad input.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd

import taa_futu.market_logger as ml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _patch_dir(tmp_path: Path):
    """Context manager: redirect market_logger's MARKET_DATA_DIR to tmp_path."""
    return patch.multiple(ml, MARKET_DATA_DIR=tmp_path, LOB_CACHE_FILE=tmp_path / "lob_cache.json")


FIXED_TS = datetime(2026, 3, 11, 14, 30, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# log_lob
# ---------------------------------------------------------------------------

class TestLogLob:
    def test_writes_lob_jsonl(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_lob("US.SPY", {"Bid": [(1, 1000, 1)], "Ask": [(1, 800, 1)]}, ts=FIXED_TS)

        day_dir = tmp_path / "2026-03-11"
        assert (day_dir / "lob.jsonl").exists()
        records = _read_jsonl(day_dir / "lob.jsonl")
        assert len(records) == 1
        assert records[0]["type"] == "lob"
        assert records[0]["code"] == "US.SPY"
        assert records[0]["bid"] == [[1, 1000, 1]]

    def test_none_order_book_writes_nothing(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_lob("US.SPY", None, ts=FIXED_TS)
        assert not (tmp_path / "2026-03-11" / "lob.jsonl").exists()

    def test_appends_multiple_records(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_lob("US.SPY", {"Bid": [], "Ask": []}, ts=FIXED_TS)
            ml.log_lob("US.QQQ", {"Bid": [], "Ask": []}, ts=FIXED_TS)
        records = _read_jsonl(tmp_path / "2026-03-11" / "lob.jsonl")
        assert len(records) == 2
        assert {r["code"] for r in records} == {"US.SPY", "US.QQQ"}

    def test_preserves_full_l2_depth_for_replay(self, tmp_path: Path) -> None:
        book = {
            "Bid": [(100.0 - idx * 0.01, 100 + idx, 1) for idx in range(60)],
            "Ask": [(100.1 + idx * 0.01, 90 + idx, 1) for idx in range(60)],
        }
        with _patch_dir(tmp_path):
            ml.log_lob("US.SPY", book, ts=FIXED_TS)

        records = _read_jsonl(tmp_path / "2026-03-11" / "lob.jsonl")
        assert len(records[0]["bid"]) == 60
        assert len(records[0]["ask"]) == 60

    def test_lob_cache_roundtrip(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_lob("US.SPY", {"Bid": [(100.0, 10)], "Ask": [(100.1, 9)]})
            cached = ml.load_lob_cache("US.SPY", max_age_seconds=5)

        assert cached == {"Bid": [[100.0, 10]], "Ask": [[100.1, 9]]}

    def test_does_not_raise_on_bad_input(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_lob("X", {"Bid": object()}, ts=FIXED_TS)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# log_ticks
# ---------------------------------------------------------------------------

class TestLogTicks:
    def _make_ticks(self) -> pd.DataFrame:
        return pd.DataFrame({
            "ticker_direction": ["BUY", "SELL", "BUY"],
            "volume": [100, 200, 150],
            "price": [500.1, 499.9, 500.2],
        })

    def test_writes_ticks_jsonl(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_ticks("US.SPY", self._make_ticks(), ts=FIXED_TS)

        records = _read_jsonl(tmp_path / "2026-03-11" / "ticks.jsonl")
        assert len(records) == 1
        assert records[0]["type"] == "ticks"
        assert records[0]["code"] == "US.SPY"
        assert len(records[0]["rows"]) == 3

    def test_empty_dataframe_writes_nothing(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_ticks("US.SPY", pd.DataFrame(), ts=FIXED_TS)
        assert not (tmp_path / "2026-03-11" / "ticks.jsonl").exists()

    def test_logs_all_tick_rows_for_replay(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_ticks("US.SPY", self._make_ticks(), ts=FIXED_TS)

        records = _read_jsonl(tmp_path / "2026-03-11" / "ticks.jsonl")
        assert len(records[0]["rows"]) == 3


# ---------------------------------------------------------------------------
# log_klines
# ---------------------------------------------------------------------------

class TestLogKlines:
    def _make_bars(self) -> pd.DataFrame:
        return pd.DataFrame({
            "close": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "volume": [1000, 1100, 1200],
        })

    def test_writes_klines_jsonl(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_klines("US.QQQ", self._make_bars(), ts=FIXED_TS)

        records = _read_jsonl(tmp_path / "2026-03-11" / "klines.jsonl")
        assert len(records) == 1
        assert records[0]["type"] == "klines"
        assert records[0]["code"] == "US.QQQ"
        assert len(records[0]["rows"]) == 3

    def test_empty_bars_writes_nothing(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_klines("US.QQQ", pd.DataFrame(), ts=FIXED_TS)
        assert not (tmp_path / "2026-03-11" / "klines.jsonl").exists()

    def test_logs_all_kline_rows_for_replay(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_klines("US.QQQ", self._make_bars(), ts=FIXED_TS)

        records = _read_jsonl(tmp_path / "2026-03-11" / "klines.jsonl")
        assert len(records[0]["rows"]) == 3


# ---------------------------------------------------------------------------
# log_snapshot
# ---------------------------------------------------------------------------

class TestLogSnapshot:
    def test_writes_snapshot_jsonl(self, tmp_path: Path) -> None:
        snapshot = pd.Series({
            "last_price": 500.0,
            "prev_close_price": 498.0,
            "price_spread": 0.01,
            "bid_vol": 2000,
            "ask_vol": 1500,
        })
        with _patch_dir(tmp_path):
            ml.log_snapshot("US.SPY", snapshot, ts=FIXED_TS)

        records = _read_jsonl(tmp_path / "2026-03-11" / "snapshots.jsonl")
        assert len(records) == 1
        assert records[0]["type"] == "snapshot"
        assert records[0]["code"] == "US.SPY"
        assert records[0]["data"]["last_price"] == 500.0


# ---------------------------------------------------------------------------
# log_feature
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FakeFeature:
    code: str
    last_price: float
    gap_pct: float
    momentum_5m: float
    vwap_distance: float
    rel_volume: float
    breakout_pct: float
    orderbook_imbalance: float
    tick_imbalance: float
    spread_bps: float
    atr_pct: float
    score: float
    eligible: bool
    reason: str


class TestLogFeature:
    def _make_feature(self, code: str = "US.SPY", score: float = 0.45) -> _FakeFeature:
        return _FakeFeature(
            code=code, last_price=500.0, gap_pct=0.01, momentum_5m=0.005,
            vwap_distance=0.003, rel_volume=1.5, breakout_pct=0.008,
            orderbook_imbalance=0.2, tick_imbalance=0.3, spread_bps=2.0,
            atr_pct=0.01, score=score, eligible=True, reason="ok",
        )

    def test_writes_feature_jsonl(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_feature(self._make_feature(), ts=FIXED_TS)  # type: ignore[arg-type]

        records = _read_jsonl(tmp_path / "2026-03-11" / "features.jsonl")
        assert len(records) == 1
        assert records[0]["type"] == "feature"
        assert records[0]["code"] == "US.SPY"
        assert records[0]["score"] == 0.45
        assert records[0]["eligible"] is True

    def test_appends_multiple_features(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_feature(self._make_feature("US.SPY", 0.45), ts=FIXED_TS)  # type: ignore[arg-type]
            ml.log_feature(self._make_feature("US.QQQ", 0.60), ts=FIXED_TS)  # type: ignore[arg-type]

        records = _read_jsonl(tmp_path / "2026-03-11" / "features.jsonl")
        assert len(records) == 2
        scores = {r["code"]: r["score"] for r in records}
        assert scores == {"US.SPY": 0.45, "US.QQQ": 0.60}


# ---------------------------------------------------------------------------
# log_plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FakePlan:
    benchmark: str
    benchmark_score: float
    exposure: float
    target_weights: dict
    features: list


class TestLogPlan:
    def _make_plan(self) -> _FakePlan:
        return _FakePlan(
            benchmark="US.SPY",
            benchmark_score=0.55,
            exposure=0.80,
            target_weights={"US.QQQ": 0.35, "US.NVDA": 0.25},
            features=[],
        )

    def test_writes_plan_jsonl(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_plan(self._make_plan(), ts=FIXED_TS)  # type: ignore[arg-type]

        records = _read_jsonl(tmp_path / "2026-03-11" / "plan.jsonl")
        assert len(records) == 1
        rec = records[0]
        assert rec["type"] == "plan"
        assert rec["benchmark"] == "US.SPY"
        assert rec["benchmark_score"] == 0.55
        assert rec["target_weights"] == {"US.QQQ": 0.35, "US.NVDA": 0.25}

    def test_ts_is_present(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_plan(self._make_plan(), ts=FIXED_TS)  # type: ignore[arg-type]
        records = _read_jsonl(tmp_path / "2026-03-11" / "plan.jsonl")
        assert "ts" in records[0]
        assert "2026-03-11" in records[0]["ts"]


# ---------------------------------------------------------------------------
# log_orders
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FakeOrder:
    code: str
    side: str
    quantity: int
    limit_price: float
    reference_price: float
    current_qty: int
    target_qty: int
    target_weight: float


class TestLogOrders:
    def _make_order(self, code: str = "US.QQQ", side: str = "BUY") -> _FakeOrder:
        return _FakeOrder(
            code=code, side=side, quantity=10, limit_price=450.05,
            reference_price=450.0, current_qty=0, target_qty=10, target_weight=0.35,
        )

    def test_writes_planned_record(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_orders([self._make_order()], "planned", ts=FIXED_TS)  # type: ignore[list-item]

        records = _read_jsonl(tmp_path / "2026-03-11" / "orders.jsonl")
        assert len(records) == 1
        assert records[0]["action"] == "planned"
        assert len(records[0]["orders"]) == 1
        assert records[0]["orders"][0]["code"] == "US.QQQ"

    def test_writes_submitted_record_with_result(self, tmp_path: Path) -> None:
        result_df = pd.DataFrame([{
            "code": "US.QQQ",
            "side": "BUY",
            "quantity": 10,
            "limit_price": 450.05,
            "status": "submitted",
            "detail": "order_id_12345",
        }])
        with _patch_dir(tmp_path):
            ml.log_orders(
                [self._make_order()],  # type: ignore[list-item]
                "submitted",
                result_df=result_df,
                ts=FIXED_TS,
            )

        records = _read_jsonl(tmp_path / "2026-03-11" / "orders.jsonl")
        assert records[0]["orders"][0]["submit_status"] == "submitted"
        assert records[0]["orders"][0]["submit_detail"] == "order_id_12345"

    def test_empty_orders_list(self, tmp_path: Path) -> None:
        with _patch_dir(tmp_path):
            ml.log_orders([], "planned", ts=FIXED_TS)

        records = _read_jsonl(tmp_path / "2026-03-11" / "orders.jsonl")
        assert records[0]["orders"] == []

    def test_appends_planned_then_submitted(self, tmp_path: Path) -> None:
        order = self._make_order()  # type: ignore[assignment]
        result_df = pd.DataFrame([{"code": "US.QQQ", "status": "submitted", "detail": "42"}])
        with _patch_dir(tmp_path):
            ml.log_orders([order], "planned", ts=FIXED_TS)  # type: ignore[list-item]
            ml.log_orders([order], "submitted", result_df=result_df, ts=FIXED_TS)  # type: ignore[list-item]

        records = _read_jsonl(tmp_path / "2026-03-11" / "orders.jsonl")
        assert len(records) == 2
        assert records[0]["action"] == "planned"
        assert records[1]["action"] == "submitted"

    def test_load_order_records_flattens_order_rows(self, tmp_path: Path) -> None:
        result_df = pd.DataFrame([{"code": "US.QQQ", "status": "submitted", "detail": "42"}])
        with _patch_dir(tmp_path):
            ml.log_orders([self._make_order()], "submitted", result_df=result_df, ts=FIXED_TS)  # type: ignore[list-item]
            frame = ml.load_order_records("2026-03-11")

        assert len(frame) == 1
        assert frame.iloc[0]["action"] == "submitted"
        assert frame.iloc[0]["submit_detail"] == "42"
        assert frame.iloc[0]["code"] == "US.QQQ"


# ---------------------------------------------------------------------------
# Safety: bad inputs never raise
# ---------------------------------------------------------------------------

class TestSafety:
    def test_log_lob_never_raises(self) -> None:
        ml.log_lob(None, None)  # type: ignore[arg-type]

    def test_log_ticks_never_raises(self) -> None:
        ml.log_ticks(None, None)  # type: ignore[arg-type]

    def test_log_klines_never_raises(self) -> None:
        ml.log_klines(None, None)  # type: ignore[arg-type]

    def test_log_snapshot_never_raises(self) -> None:
        ml.log_snapshot(None, None)  # type: ignore[arg-type]

    def test_log_feature_never_raises(self) -> None:
        ml.log_feature(None)  # type: ignore[arg-type]

    def test_log_plan_never_raises(self) -> None:
        ml.log_plan(None)  # type: ignore[arg-type]

    def test_log_orders_never_raises(self) -> None:
        ml.log_orders(None, "planned")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Day boundary: verify files are created in the correct date folder
# ---------------------------------------------------------------------------

class TestDayBoundary:
    def test_file_goes_into_correct_date_dir(self, tmp_path: Path) -> None:
        ts_march_10 = datetime(2026, 3, 10, 20, 0, 0, tzinfo=UTC)  # 3 PM ET on March 10
        ts_march_11 = datetime(2026, 3, 11, 14, 30, 0, tzinfo=UTC)  # 10:30 AM ET on March 11

        with _patch_dir(tmp_path):
            ml.log_lob("US.SPY", {"Bid": [], "Ask": []}, ts=ts_march_10)
            ml.log_lob("US.SPY", {"Bid": [], "Ask": []}, ts=ts_march_11)

        assert (tmp_path / "2026-03-10" / "lob.jsonl").exists()
        assert (tmp_path / "2026-03-11" / "lob.jsonl").exists()


class TestMarketDataStatus:
    def test_retention_plan_is_read_only(self, tmp_path: Path) -> None:
        old_dir = tmp_path / "2026-03-01"
        keep_dir = tmp_path / "2026-03-10"
        old_dir.mkdir()
        keep_dir.mkdir()
        (old_dir / "lob.jsonl").write_text("{}\n", encoding="utf-8")
        (keep_dir / "lob.jsonl").write_text("{}\n", encoding="utf-8")

        with _patch_dir(tmp_path):
            plan = ml.market_data_retention_plan(keep_days=3, today=datetime(2026, 3, 10, tzinfo=UTC).date())

        assert plan["older_bytes"] > 0
        assert len(plan["older_days"]) == 1
        assert old_dir.exists()
        assert keep_dir.exists()
