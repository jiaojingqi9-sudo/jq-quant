"""OFIM walk-forward research loop (research-only, never edits live).

Mirrors the discipline of ``crypto_research_loop`` for the stock OFIM intraday
strategy: it searches a small, motivated parameter grid over a **train** window,
validates survivors on an out-of-sample **validation** window, and reports a
single unbiased number on a **locked test** window. It writes a report and,
optionally, a ``promoted_overrides.json`` candidate that plugs straight into the
human-in-the-loop override bridge (``strategy_overrides.py``). It never edits
``.env`` or live parameters by itself.

Why walk-forward + a locked test: tuning a strategy until the *backtest* looks
green is data-snooping. The only number you can trust is performance on data the
search never saw. This module makes that the headline metric.

Backtests run via :func:`intraday_replay.run_ofim_replay` on stored LOB data, so
they are deterministic and cost-aware (the same fee model as live).

CLI::

    python -m taa_futu.ofim_research_loop \
        --train 2026-03-11:2026-04-15 \
        --val   2026-04-17:2026-05-02 \
        --test  2026-05-05:2026-05-29 \
        --max-trials 16

Heavy days (1-4 GB of LOB JSONL) need RAM; run on a machine with >=16 GB.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .costs import build_trade_cost_model
from .intraday_replay import MARKET_DATA_DIR, _iter_day_dirs, run_ofim_replay

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime"
DEFAULT_REPORT_FILE = RUNTIME_DIR / "ofim_research_report.json"

# Single-axis perturbations around the current defaults. Each entry is
# (settings_field, env_name, [candidate values]). Single-axis (not full
# cartesian) keeps the trial count linear and interpretable. All fields are in
# the strategy_overrides whitelist so a winner can be promoted safely.
PARAM_AXES: tuple[tuple[str, str, tuple[float, ...]], ...] = (
    ("ofim_entry_threshold", "OFIM_ENTRY_THRESHOLD", (0.15, 0.20, 0.25, 0.30, 0.35)),
    ("ofim_exit_threshold", "OFIM_EXIT_THRESHOLD", (0.05, 0.10, 0.15)),
    ("ofim_max_positions", "OFIM_MAX_POSITIONS", (3, 5, 8)),
    ("ofim_max_spread_bps", "OFIM_MAX_SPREAD_BPS", (10.0, 15.0, 20.0)),
    ("ofim_min_vol_acceleration", "OFIM_MIN_VOL_ACCELERATION", (1.0, 1.2, 1.5)),
)


@dataclasses.dataclass(frozen=True)
class ParamCandidate:
    label: str
    field: str | None
    env_name: str | None
    value: Any
    settings: Settings
    replay_kwargs: dict = dataclasses.field(default_factory=dict)


# Execution-layer anti-churn controls (replay kwargs, not Settings fields).
# Listed first because over-trading is the diagnosed core problem, so these
# should always be searched even at a small --max-trials.
EXEC_AXES: tuple[tuple[str, tuple[float, ...]], ...] = (
    ("min_rebalance_drift_pct", (0.01, 0.02, 0.05)),
    ("min_hold_cycles", (10, 30, 60)),
)


def build_param_candidates(
    base: Settings, *, axes=PARAM_AXES, exec_axes=EXEC_AXES, max_trials: int = 24
) -> list[ParamCandidate]:
    """Default first, then anti-churn execution axes, then single-axis Settings
    perturbations that differ from the current value. Capped at ``max_trials``."""
    candidates: list[ParamCandidate] = [ParamCandidate("default", None, None, None, base)]
    for kw, values in exec_axes:
        for value in values:
            candidates.append(
                ParamCandidate(
                    label=f"{kw}={value}", field=None, env_name=None,
                    value=value, settings=base, replay_kwargs={kw: value},
                )
            )
            if len(candidates) >= max_trials:
                return candidates
    seen: set[tuple[str, Any]] = set()
    for field, env_name, values in axes:
        current = getattr(base, field, None)
        for value in values:
            if value == current:
                continue
            key = (field, value)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                ParamCandidate(
                    label=f"{field}={value}",
                    field=field,
                    env_name=env_name,
                    value=value,
                    settings=replace(base, **{field: value}),
                )
            )
            if len(candidates) >= max_trials:
                return candidates
    return candidates


def score_summary(summary: dict[str, float]) -> float:
    """Selection score: net return after costs, penalised by drawdown.

    ``total_return`` from the replay is already net of fees. ``max_drawdown`` is
    negative, so adding a fraction of it penalises drawdown-heavy paths.
    """
    if not summary:
        return float("-inf")
    net = float(summary.get("total_return", 0.0) or 0.0)
    max_dd = float(summary.get("max_drawdown", 0.0) or 0.0)
    return net + 0.5 * max_dd


def _metrics(summary: dict[str, float], n_trades: int) -> dict[str, Any]:
    return {
        "total_return": summary.get("total_return"),
        "sharpe": summary.get("sharpe"),
        "max_drawdown": summary.get("max_drawdown"),
        "total_fees_usd": summary.get("total_fees_usd"),
        "n_trades": n_trades,
        "score": round(score_summary(summary), 6),
    }


def evaluate_params(
    settings: Settings,
    start: str,
    end: str,
    *,
    initial_capital: float = 1_000_000.0,
    flat_by_close: bool = False,
    replay_kwargs: dict | None = None,
) -> dict[str, Any]:
    """Run one OFIM replay over [start, end] and return scored metrics."""
    result = run_ofim_replay(
        start,
        end,
        settings,
        initial_capital=initial_capital,
        cost_model=build_trade_cost_model(settings),
        flat_by_close=flat_by_close,
        **(replay_kwargs or {}),
    )
    n_trades = 0 if result.trade_log.empty else int(len(result.trade_log))
    return _metrics(result.summary, n_trades)


def _has_data(start: str, end: str) -> bool:
    return bool(_iter_day_dirs(start, end))


def run_ofim_research(
    *,
    train: tuple[str, str],
    val: tuple[str, str],
    test: tuple[str, str],
    env_file: str = ".env",
    max_trials: int = 24,
    top_k: int = 3,
    initial_capital: float = 1_000_000.0,
    report_path: Path = DEFAULT_REPORT_FILE,
    write_override: bool = False,
    flat_by_close: bool = False,
    progress=print,
) -> dict[str, Any]:
    """Search on train, validate survivors, lock-test the winner.

    Returns the report dict and writes it to ``report_path``. ``live_promotion``
    is always False — this is research only.
    """
    base = load_settings(env_file)
    candidates = build_param_candidates(base, max_trials=max_trials)
    by_label = {c.label: c for c in candidates}

    # ── 1. train: score every candidate ──────────────────────────────────────
    train_rows: list[dict[str, Any]] = []
    for i, cand in enumerate(candidates, start=1):
        progress(f"[train {i}/{len(candidates)}] {cand.label} ...")
        m = evaluate_params(cand.settings, *train, initial_capital=initial_capital,
                            flat_by_close=flat_by_close, replay_kwargs=cand.replay_kwargs)
        train_rows.append({"label": cand.label, "field": cand.field, "env_name": cand.env_name,
                           "value": cand.value, "replay_kwargs": cand.replay_kwargs, **m})
    train_rows.sort(key=lambda r: (r["score"] if r["score"] is not None else -1e9), reverse=True)

    # ── 2. validation: only the top-K survivors ───────────────────────────────
    survivors = train_rows[:top_k]
    for row in survivors:
        progress(f"[val] {row['label']} ...")
        cand = by_label[row["label"]]
        row["val"] = evaluate_params(cand.settings, *val, initial_capital=initial_capital,
                                     flat_by_close=flat_by_close, replay_kwargs=cand.replay_kwargs)

    def _val_score(row: dict[str, Any]) -> float:
        return row.get("val", {}).get("score", -1e9) if row.get("val") else -1e9

    survivors.sort(key=_val_score, reverse=True)
    best = survivors[0] if survivors else None

    # default's validation score as the bar to beat (does tuning even help OOS?)
    default_val = next((r.get("val") for r in survivors if r["label"] == "default"), None)

    # ── 3. locked test: one unbiased number for the winner ────────────────────
    locked = None
    if best is not None:
        progress(f"[test] winner = {best['label']} ...")
        cand = by_label[best["label"]]
        locked = evaluate_params(cand.settings, *test, initial_capital=initial_capital,
                                 flat_by_close=flat_by_close, replay_kwargs=cand.replay_kwargs)

    # ── 4. overfit guard ──────────────────────────────────────────────────────
    overfit_flags: list[str] = []
    if best is not None and best.get("val"):
        if best["score"] is not None and best["val"]["score"] is not None and best["val"]["score"] < 0:
            overfit_flags.append("winner_validation_score_negative")
        if locked and locked["score"] is not None and locked["score"] < 0:
            overfit_flags.append("winner_locked_test_score_negative")
        if (best["label"] != "default" and default_val and best["val"].get("score") is not None
                and default_val.get("score") is not None
                and best["val"]["score"] <= default_val["score"]):
            overfit_flags.append("tuning_did_not_beat_default_on_validation")

    report = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "windows": {"train": train, "val": val, "test": test},
        "mode": "flat_by_close" if flat_by_close else "hold_overnight",
        "max_trials": max_trials,
        "train_ranking": train_rows,
        "validation_survivors": survivors,
        "winner": best,
        "winner_locked_test": locked,
        "default_validation": default_val,
        "overfit_flags": overfit_flags,
        "recommended": (
            best["label"] if best and best["label"] != "default" and not overfit_flags else "keep_default"
        ),
        "live_promotion": False,
        "notes": [
            "total_return is net of the same fee model as live.",
            "Single-day-independent overnight gaps are included only where the "
            "window is contiguous; gaps between non-trading days are handled by "
            "the replay engine day-by-day.",
            "Promote a winner only via strategy_overrides.py after your own review.",
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if write_override and best and best["field"] and not overfit_flags:
        _write_override_candidate(best)
        report["override_written"] = True

    return report


def _write_override_candidate(winner: dict[str, Any]) -> Path:
    """Write the winner straight into promoted_overrides.json (whitelisted)."""
    from .strategy_overrides import DEFAULT_OVERRIDES_FILE, is_overridable, load_overrides, _utc_now

    field = winner["field"]
    if not is_overridable(field):
        raise ValueError(f"{field} is not overridable")
    doc = load_overrides(DEFAULT_OVERRIDES_FILE)
    doc["overrides"][field] = {
        "field": field,
        "param": winner.get("env_name"),
        "value": winner["value"],
        "source": "ofim_research_loop",
        "validation_score": winner.get("val", {}).get("score"),
        "approved_by": "ofim_research_loop(auto)",
        "approved_at": _utc_now(),
        "scope": "paper",
    }
    doc["updated_at"] = _utc_now()
    DEFAULT_OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_OVERRIDES_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return DEFAULT_OVERRIDES_FILE


def _parse_range(text: str) -> tuple[str, str]:
    start, _, end = text.partition(":")
    if not start or not end:
        raise argparse.ArgumentTypeError(f"range must be START:END, got {text!r}")
    return (start, end)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m taa_futu.ofim_research_loop",
                                     description="OFIM walk-forward research loop (research-only).")
    parser.add_argument("--train", type=_parse_range, required=True, help="START:END")
    parser.add_argument("--val", type=_parse_range, required=True, help="START:END")
    parser.add_argument("--test", type=_parse_range, required=True, help="START:END")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--max-trials", type=int, default=24)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--write-override", action="store_true",
                        help="If a non-default winner passes all gates, write it into promoted_overrides.json.")
    parser.add_argument("--flat-by-close", action="store_true",
                        help="Force-liquidate every position at each day's close (no overnight holds).")
    args = parser.parse_args(argv)

    for name, rng in (("train", args.train), ("val", args.val), ("test", args.test)):
        if not _has_data(*rng):
            print(f"WARNING: no market_data found for {name} window {rng[0]}..{rng[1]} under {MARKET_DATA_DIR}")

    report = run_ofim_research(
        train=args.train, val=args.val, test=args.test,
        env_file=args.env, max_trials=args.max_trials, top_k=args.top_k,
        initial_capital=args.capital, write_override=args.write_override,
        flat_by_close=args.flat_by_close,
    )
    print("\n=== OFIM research result ===")
    print(f"mode                : {report.get('mode')}")
    w = report.get("winner") or {}
    print(f"recommended         : {report['recommended']}")
    print(f"winner (train)      : {w.get('label')}  train_score={w.get('score')}")
    if w.get("val"):
        print(f"winner validation   : score={w['val'].get('score')} return={w['val'].get('total_return')}")
    if report.get("winner_locked_test"):
        lt = report["winner_locked_test"]
        print(f"winner LOCKED TEST  : score={lt.get('score')} return={lt.get('total_return')} trades={lt.get('n_trades')}")
    if report.get("overfit_flags"):
        print(f"overfit flags       : {report['overfit_flags']}")
    print(f"report              : {DEFAULT_REPORT_FILE}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
