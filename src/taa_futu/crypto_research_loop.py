from __future__ import annotations

from dataclasses import asdict, replace
from itertools import product
import json
from pathlib import Path
from typing import Any, Iterable

from .crypto_backtest import (
    DATA_FILE,
    MANIFEST_FILE,
    RUNTIME_DIR,
    CryptoBacktestProfile,
    build_crypto_backtest_dataset,
    load_crypto_replay_frame,
    result_to_dict,
    run_crypto_backtest,
    split_time_series,
)


TRIALS_FILE = RUNTIME_DIR / "trials.jsonl"
BEST_CANDIDATE_FILE = RUNTIME_DIR / "best_candidate.json"
LOCKED_TEST_REPORT_FILE = RUNTIME_DIR / "locked_test_report.json"
RESEARCH_PATCH_REPORT_FILE = RUNTIME_DIR / "research_patch_report.md"
PROFILE_DIR = RUNTIME_DIR / "profiles"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    tmp.replace(path)
    return count


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _candidate_profiles(max_trials: int) -> list[CryptoBacktestProfile]:
    base = CryptoBacktestProfile()
    profiles: list[CryptoBacktestProfile] = []
    conservative_seeds = [
        replace(
            base,
            name="fee_drag_high_conviction",
            entry_threshold=0.50,
            exit_threshold=0.12,
            max_holding_bars=30,
            max_position_weight=0.15,
            max_gross_exposure=0.20,
            min_trade_interval_bars=30,
            order_style="maker_limit",
            edge_bps_per_score=85.0,
            cost_buffer_bps=10.0,
            slippage_bps=2.0,
        ),
        replace(
            base,
            name="fee_drag_sparse_signal",
            entry_threshold=0.60,
            exit_threshold=0.12,
            max_holding_bars=60,
            max_position_weight=0.15,
            max_gross_exposure=0.20,
            min_trade_interval_bars=30,
            order_style="maker_limit",
            edge_bps_per_score=85.0,
            cost_buffer_bps=10.0,
            slippage_bps=2.0,
        ),
        replace(
            base,
            name="perp_maker_cooldown",
            entry_threshold=0.40,
            exit_threshold=0.12,
            max_holding_bars=30,
            max_position_weight=0.10,
            max_gross_exposure=0.12,
            min_trade_interval_bars=20,
            order_style="maker_limit",
            edge_bps_per_score=85.0,
            cost_buffer_bps=10.0,
            slippage_bps=2.0,
        ),
        replace(
            base,
            name="market_cost_stress",
            entry_threshold=0.50,
            exit_threshold=0.12,
            max_holding_bars=16,
            max_position_weight=0.10,
            max_gross_exposure=0.20,
            min_trade_interval_bars=30,
            order_style="market",
            edge_bps_per_score=85.0,
            cost_buffer_bps=15.0,
            slippage_bps=5.0,
        ),
    ]
    for profile in conservative_seeds:
        profiles.append(profile)
        if len(profiles) >= max(1, int(max_trials)):
            return profiles
    grid = product(
        (0.18, 0.24, 0.32, 0.40, 0.50, 0.60),
        (0.05, 0.08, 0.12),
        (8, 16, 30, 60),
        (0.15, 0.25, 0.35),
        (0.20, 0.40, 0.60),
        (1, 3, 5, 10, 20, 30),
        ("maker_limit", "market"),
        (40.0, 60.0, 85.0),
        (4.0, 6.0, 10.0, 15.0),
    )
    for idx, (entry, exit_, max_holding, max_weight, gross, interval, order_style, edge, buffer) in enumerate(grid, start=1):
        if exit_ >= entry:
            continue
        gross = max(gross, max_weight)
        slippage = 2.0 if order_style == "maker_limit" else 5.0
        profiles.append(
            replace(
                base,
                name=f"trial_{idx:04d}",
                entry_threshold=entry,
                exit_threshold=exit_,
                max_holding_bars=max_holding,
                max_position_weight=max_weight,
                max_gross_exposure=gross,
                min_trade_interval_bars=interval,
                order_style=order_style,
                edge_bps_per_score=edge,
                cost_buffer_bps=buffer,
                slippage_bps=slippage,
            )
        )
        if len(profiles) >= max(1, int(max_trials)):
            break
    return profiles


def _combined_metric(result_payload: dict[str, Any]) -> dict[str, Any]:
    if "combined" in result_payload:
        return dict(result_payload["combined"])
    return dict(result_payload)


def _selection_score(metric: dict[str, Any]) -> float:
    net = float(metric.get("net_pnl", 0.0) or 0.0)
    initial = max(1.0, float(metric.get("initial_equity", 10_000.0) or 10_000.0))
    drawdown_penalty = float(metric.get("max_drawdown", 0.0) or 0.0) * initial * 0.35
    trade_count = int(metric.get("trade_count", 0) or 0)
    trade_bonus = min(20, trade_count) * 0.05
    inert_penalty = 10.0 if trade_count <= 0 else max(0, 5 - trade_count) * 0.5
    paid_costs = (
        max(0.0, float(metric.get("fees_paid", 0.0) or 0.0))
        + max(0.0, float(metric.get("slippage_paid", 0.0) or 0.0))
        + max(0.0, float(metric.get("funding_paid", 0.0) or 0.0))
    )
    gross = float(metric.get("gross_pnl", 0.0) or 0.0)
    cost_drag_penalty = paid_costs * 0.10
    if gross > 0:
        cost_drag_penalty += max(0.0, paid_costs / gross - 0.60) * initial * 0.01
    elif paid_costs > 0:
        cost_drag_penalty += paid_costs
    return net - drawdown_penalty + trade_bonus - inert_penalty - cost_drag_penalty


def _passes_gate(metric: dict[str, Any]) -> bool:
    return bool(metric.get("passed_gate"))


def _profile_public_payload(profile: CryptoBacktestProfile) -> dict[str, Any]:
    payload = asdict(profile)
    payload["research_only"] = True
    payload["live_auto_promotion"] = False
    payload["notes"] = "Generated by crypto research loop; do not copy to .env without human review."
    return payload


def _patch_report(
    *,
    best: dict[str, Any],
    locked: dict[str, Any],
    profile_path: Path | None,
) -> str:
    validation = _combined_metric(best.get("validation_result") or {})
    locked_combined = _combined_metric(locked.get("locked_test_result") or {})
    passed = bool(best.get("passed_validation") and locked.get("passed_locked_test"))
    lines = [
        "# Crypto Research Patch Report",
        "",
        "This report is research-only. It does not change `.env`, live strategy settings, auto trading, watchdogs or exchange state.",
        "",
        f"- Candidate: `{best.get('profile_name', 'none')}`",
        f"- Validation net PnL: `{validation.get('net_pnl', 0)}`",
        f"- Validation max drawdown: `{validation.get('max_drawdown', 0)}`",
        f"- Validation trades: `{validation.get('trade_count', 0)}`",
        f"- Locked-test net PnL: `{locked_combined.get('net_pnl', 0)}`",
        f"- Locked-test max drawdown: `{locked_combined.get('max_drawdown', 0)}`",
        f"- Locked-test trades: `{locked_combined.get('trade_count', 0)}`",
        f"- Passed promotion gate: `{passed}`",
        "",
    ]
    if passed and profile_path:
        lines.extend(
            [
                "## Research Profile",
                "",
                f"A research profile was written to `{profile_path}`.",
                "Use it only for manual review/backtest reruns. Do not auto-promote it to live/testnet trading.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "## No Patch Generated",
                "",
                "No research profile was promoted because validation and locked-test gates did not both pass.",
                "",
            ]
        )
    lines.extend(
        [
            "## Review Checklist",
            "",
            "- Confirm no locked-test data was used during trial selection.",
            "- Confirm fees, slippage and funding are nonzero where applicable.",
            "- Compare spot-only, perp-only and combined results before any manual trading change.",
            "- Treat this as evidence, not a profitability guarantee.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_crypto_research_loop(
    *,
    max_trials: int = 100,
    target: str = "out_of_sample_net_profit",
    data_file: Path = DATA_FILE,
    build_data_if_missing: bool = True,
) -> dict[str, Any]:
    if build_data_if_missing and not data_file.exists():
        build_crypto_backtest_dataset(include_public=True, include_local=True, data_file=data_file)
    frame = load_crypto_replay_frame(data_file)
    splits = split_time_series(frame)
    trials: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    best_score = float("-inf")
    for idx, profile in enumerate(_candidate_profiles(max_trials), start=1):
        train_result = result_to_dict(run_crypto_backtest(splits["train"], sleeve="both", profile=profile, split="train"))
        validation_result = result_to_dict(run_crypto_backtest(splits["validation"], sleeve="both", profile=profile, split="validation"))
        validation_metric = _combined_metric(validation_result)
        trial = {
            "trial": idx,
            "target": target,
            "profile_name": profile.name,
            "profile": asdict(profile),
            "train_result": train_result,
            "validation_result": validation_result,
            "passed_validation": _passes_gate(validation_metric),
            "selection_score": round(_selection_score(validation_metric), 8),
        }
        trials.append(trial)
        score = float(trial["selection_score"])
        if score > best_score:
            best = trial
            best_score = score
    _write_jsonl_atomic(TRIALS_FILE, trials)

    if best is None:
        best = {
            "profile_name": "none",
            "profile": asdict(CryptoBacktestProfile()),
            "train_result": {},
            "validation_result": {},
            "passed_validation": False,
            "selection_score": 0.0,
        }
    profile = CryptoBacktestProfile(**{key: value for key, value in best["profile"].items() if key in asdict(CryptoBacktestProfile())})
    locked_result = result_to_dict(run_crypto_backtest(splits["locked_test"], sleeve="both", profile=profile, split="locked_test"))
    locked_metric = _combined_metric(locked_result)
    locked_payload = {
        "target": target,
        "profile_name": profile.name,
        "locked_test_result": locked_result,
        "passed_locked_test": _passes_gate(locked_metric),
        "live_auto_promotion": False,
    }
    profile_path: Path | None = None
    if bool(best.get("passed_validation")) and locked_payload["passed_locked_test"]:
        profile_path = PROFILE_DIR / f"{profile.name}.json"
        _write_json_atomic(profile_path, _profile_public_payload(profile))

    best_payload = {
        **best,
        "locked_test_profile_path": str(profile_path) if profile_path else None,
        "manifest": _read_json(MANIFEST_FILE),
        "live_auto_promotion": False,
    }
    _write_json_atomic(BEST_CANDIDATE_FILE, best_payload)
    _write_json_atomic(LOCKED_TEST_REPORT_FILE, locked_payload)
    _write_text_atomic(RESEARCH_PATCH_REPORT_FILE, _patch_report(best=best_payload, locked=locked_payload, profile_path=profile_path))
    return {
        "trial_count": len(trials),
        "best_candidate": best_payload,
        "locked_test": locked_payload,
        "artifacts": {
            "trials": str(TRIALS_FILE),
            "best_candidate": str(BEST_CANDIDATE_FILE),
            "locked_test_report": str(LOCKED_TEST_REPORT_FILE),
            "research_patch_report": str(RESEARCH_PATCH_REPORT_FILE),
            "profile": str(profile_path) if profile_path else None,
        },
    }


def read_crypto_research_status() -> dict[str, Any]:
    best = _read_json(BEST_CANDIDATE_FILE)
    locked = _read_json(LOCKED_TEST_REPORT_FILE)
    manifest = _read_json(MANIFEST_FILE)
    trial_count = 0
    if TRIALS_FILE.exists():
        with TRIALS_FILE.open("r", encoding="utf-8", errors="ignore") as handle:
            trial_count = sum(1 for line in handle if line.strip())
    return {
        "trial_count": trial_count,
        "best_profile": best.get("profile_name", "none"),
        "best_validation": _combined_metric(best.get("validation_result") or {}) if best else {},
        "passed_validation": bool(best.get("passed_validation")),
        "locked_test": _combined_metric(locked.get("locked_test_result") or {}) if locked else {},
        "passed_locked_test": bool(locked.get("passed_locked_test")),
        "live_auto_promotion": False,
        "data_manifest": manifest,
        "artifacts": {
            "trials": str(TRIALS_FILE),
            "best_candidate": str(BEST_CANDIDATE_FILE),
            "locked_test_report": str(LOCKED_TEST_REPORT_FILE),
            "research_patch_report": str(RESEARCH_PATCH_REPORT_FILE),
        },
    }
