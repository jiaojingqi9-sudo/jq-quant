"""Unit tests for the OFIM walk-forward research loop (pure logic only).

The replay-driven parts are exercised by a separate sandbox smoke run; these
tests cover the deterministic pieces: candidate generation, scoring, and the
guarantee that every tunable axis is in the safe override whitelist.
"""

from __future__ import annotations

import dataclasses

from taa_futu import ofim_research_loop as rl
from taa_futu.strategy_overrides import is_overridable


@dataclasses.dataclass(frozen=True)
class FakeSettings:
    ofim_entry_threshold: float = 0.20
    ofim_exit_threshold: float = 0.05
    ofim_max_positions: int = 5
    ofim_max_spread_bps: float = 15.0
    ofim_min_vol_acceleration: float = 1.20


def test_candidates_start_with_default():
    cands = rl.build_param_candidates(FakeSettings(), max_trials=99)
    assert cands[0].label == "default"
    assert cands[0].field is None


def test_candidates_are_single_axis_and_differ_from_current():
    cands = rl.build_param_candidates(FakeSettings(), max_trials=99)
    for c in cands[1:]:
        if c.field is not None:
            # Settings-field axis: exactly one field changed vs the base default
            assert getattr(c.settings, c.field) == c.value
            assert c.value != getattr(FakeSettings(), c.field)
        else:
            # execution axis: carried as replay_kwargs, base settings untouched
            assert c.replay_kwargs
            assert c.settings == FakeSettings()


def test_exec_axes_are_searched_first():
    """Anti-churn execution axes must appear before Settings-field axes so they
    are always included even at a small --max-trials."""
    cands = rl.build_param_candidates(FakeSettings(), max_trials=99)
    first_exec = next(i for i, c in enumerate(cands) if c.replay_kwargs)
    first_field = next(i for i, c in enumerate(cands) if c.field is not None)
    assert first_exec < first_field
    # every exec axis key is a real run_ofim_replay / run_intraday_replay kwarg
    assert {kw for kw, _ in rl.EXEC_AXES} == {"min_rebalance_drift_pct", "min_hold_cycles"}


def test_candidates_respect_max_trials():
    cands = rl.build_param_candidates(FakeSettings(), max_trials=4)
    assert len(cands) == 4


def test_every_tunable_axis_is_overridable():
    for field, _env, _values in rl.PARAM_AXES:
        assert is_overridable(field), field


def test_score_rewards_return_and_penalises_drawdown():
    a = rl.score_summary({"total_return": 0.05, "max_drawdown": -0.02})
    b = rl.score_summary({"total_return": 0.05, "max_drawdown": -0.10})
    assert a > b  # deeper drawdown scores worse for the same return
    assert rl.score_summary({}) == float("-inf")


def test_no_duplicate_axis_values():
    cands = rl.build_param_candidates(FakeSettings(), max_trials=99)
    keys = [(c.field, c.value) for c in cands[1:]]
    assert len(keys) == len(set(keys))


def test_flat_by_close_liquidates_all_positions_to_cash():
    """The no-overnight variant must sell every open position at the day's close."""
    from taa_futu.intraday_replay import _PortfolioState

    class _FakeStore:
        def get_snapshot(self, code):
            return {"last_price": 100.0, "bid_price": 99.0, "ask_price": 101.0, "price_spread": 2.0}

    p = _PortfolioState(cash=1000.0, qty={"US.AAPL": 10.0, "US.NVDA": 5.0})
    rows: list[dict] = []
    p.liquidate(_FakeStore(), None, rows, "2026-03-13 16:00:00")

    assert all(q <= 0 for q in p.qty.values())          # nothing left held
    assert p.cash > 1000.0                               # proceeds added to cash
    assert {r["side"] for r in rows} == {"SELL"}         # only sells
    assert len(rows) == 2 and not p.pending
