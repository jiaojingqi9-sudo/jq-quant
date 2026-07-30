"""Tests for the Fusion Intraday pre-gate.

The pre-gate is a defensive, opt-in skip-only filter. These tests verify:

1. Disabled by default — returns input weights bit-for-bit.
2. Log-only mode — emits events but still returns input weights.
3. Active mode — drops symbols that fail thresholds, keeps the rest.
4. Robustness — exceptions in any branch fall back to input weights.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from taa_futu import fusion_pregate


@dataclass
class _StubFeature:
    code: str
    score: float = 0.5
    orderbook_imbalance: float = 0.0
    tick_imbalance: float = 0.0
    spread_bps: float = 5.0


class _StubSettings:
    def __init__(self, **overrides: Any) -> None:
        # Sensible defaults that match production config.py defaults.
        defaults = {
            "fusion_futu_pregate_enabled": False,
            "fusion_futu_pregate_log_only": True,
            "fusion_futu_pregate_min_ob_imbalance": 0.20,
            "fusion_futu_pregate_min_tick_imbalance": 0.15,
            "fusion_futu_pregate_max_spread_bps": 15.0,
        }
        defaults.update(overrides)
        for k, v in defaults.items():
            setattr(self, k, v)


def test_disabled_returns_input_unchanged():
    weights = {"US.NVDA": 0.5, "US.AAPL": 0.5}
    features = [_StubFeature("US.NVDA"), _StubFeature("US.AAPL")]
    settings = _StubSettings(fusion_futu_pregate_enabled=False)
    out = fusion_pregate.apply(weights, features=features, settings=settings)
    assert out == weights
    assert out is weights or out == weights  # identity not required, equality is


def test_log_only_mode_keeps_all_weights_even_when_thresholds_violated(tmp_path, monkeypatch):
    # Redirect the audit ledger to a tmp file so the test doesn't touch the
    # real runtime/stock_events.jsonl.
    from taa_futu import stock_events

    monkeypatch.setattr(stock_events, "STOCK_EVENTS_FILE", tmp_path / "evt.jsonl")
    weights = {"US.NVDA": 0.5, "US.AAPL": 0.5}
    features = [
        _StubFeature("US.NVDA", orderbook_imbalance=0.05),  # below threshold
        _StubFeature("US.AAPL", orderbook_imbalance=0.40, tick_imbalance=0.20),  # strong
    ]
    settings = _StubSettings(
        fusion_futu_pregate_enabled=True,
        fusion_futu_pregate_log_only=True,
    )
    out = fusion_pregate.apply(weights, features=features, settings=settings)
    # Log-only must NOT drop entries, even when thresholds are violated.
    assert out == weights
    # The audit ledger should contain one consolidated decision event.
    contents = (tmp_path / "evt.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(contents) == 1
    assert "fusion_pregate_decision" in contents[0]
    assert "US.NVDA" in contents[0]


def test_active_mode_drops_weak_symbols(tmp_path, monkeypatch):
    from taa_futu import stock_events

    monkeypatch.setattr(stock_events, "STOCK_EVENTS_FILE", tmp_path / "evt.jsonl")
    weights = {"US.NVDA": 0.5, "US.AAPL": 0.5}
    features = [
        # Strong: passes all filters.
        _StubFeature(
            "US.NVDA",
            orderbook_imbalance=0.40,
            tick_imbalance=0.25,
            spread_bps=5.0,
        ),
        # Weak: orderbook imbalance below threshold.
        _StubFeature(
            "US.AAPL",
            orderbook_imbalance=0.05,
            tick_imbalance=0.25,
            spread_bps=5.0,
        ),
    ]
    settings = _StubSettings(
        fusion_futu_pregate_enabled=True,
        fusion_futu_pregate_log_only=False,
    )
    out = fusion_pregate.apply(weights, features=features, settings=settings)
    assert "US.NVDA" in out
    assert "US.AAPL" not in out
    # The dropped symbol's original weight is gone, not redistributed.
    assert out["US.NVDA"] == 0.5


def test_active_mode_drops_score_orderflow_disagreement(tmp_path, monkeypatch):
    from taa_futu import stock_events

    monkeypatch.setattr(stock_events, "STOCK_EVENTS_FILE", tmp_path / "evt.jsonl")
    weights = {"US.X": 0.5}
    # Positive score but order-book imbalance is strongly negative.
    features = [
        _StubFeature(
            "US.X",
            score=0.6,
            orderbook_imbalance=-0.40,
            tick_imbalance=0.25,
            spread_bps=5.0,
        ),
    ]
    settings = _StubSettings(
        fusion_futu_pregate_enabled=True,
        fusion_futu_pregate_log_only=False,
    )
    out = fusion_pregate.apply(weights, features=features, settings=settings)
    assert out == {}


def test_missing_feature_does_not_drop_symbol(tmp_path, monkeypatch):
    from taa_futu import stock_events

    monkeypatch.setattr(stock_events, "STOCK_EVENTS_FILE", tmp_path / "evt.jsonl")
    weights = {"US.MYSTERY": 0.5}
    features: list[_StubFeature] = []  # no feature for this symbol
    settings = _StubSettings(
        fusion_futu_pregate_enabled=True,
        fusion_futu_pregate_log_only=False,
    )
    out = fusion_pregate.apply(weights, features=features, settings=settings)
    # When we have no feature, default is to pass (no_feature reason).
    assert out == weights


def test_apply_never_raises_even_on_broken_settings():
    weights = {"US.NVDA": 0.5}

    class _Broken:
        # Raise on any attribute access except the enabled flag.
        fusion_futu_pregate_enabled = True

        def __getattr__(self, item):
            raise RuntimeError(f"broken {item}")

    out = fusion_pregate.apply(weights, features=[], settings=_Broken())
    assert out == weights
