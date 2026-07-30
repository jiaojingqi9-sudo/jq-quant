"""Learning-to-strategy promotion bridge (human-in-the-loop).

This module closes the *last* segment of the strategy learning loop without
breaking its core safety guarantee. The learning lab (``stock_learning.py``)
proposes research candidates and runs promotion gates, but by design it never
edits live parameters. This bridge lets a human **approve** an eligible
candidate, which writes a small, reversible, audited override into
``runtime/promoted_overrides.json``. Strategies pick the override up on the next
config load — but only when ``STRATEGY_OVERRIDES_ENABLED=true`` (default off), so
the system's behavior is byte-for-byte unchanged until the operator opts in.

Design guarantees:

* **Whitelist only.** Only strategy / risk threshold knobs can ever be
  overridden. Safety, credential, account and connection settings are never
  overridable, even if a candidate proposes them (see ``OVERRIDABLE_FIELDS`` and
  the ``FORBIDDEN_SUBSTRINGS`` defense-in-depth check).
* **Gated.** A candidate is only promotable if the promotion report marks it
  ``paper_allowed``. Live promotion is never automated here.
* **Reversible.** Every override records its source candidate, previous value,
  approver and timestamp, and can be reverted with one command.
* **Fail-safe.** Applying overrides is wrapped so that any malformed file falls
  back to the base ``.env`` settings rather than breaking config loading.

CLI::

    python -m taa_futu.strategy_overrides status
    python -m taa_futu.strategy_overrides promote --candidate-id <id> --approved-by you
    python -m taa_futu.strategy_overrides show
    python -m taa_futu.strategy_overrides revert --candidate-id <id>
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime"
DEFAULT_CANDIDATES_FILE = RUNTIME_DIR / "strategy_upgrade_candidates.jsonl"
DEFAULT_PROMOTION_FILE = RUNTIME_DIR / "strategy_promotion_report.json"
DEFAULT_OVERRIDES_FILE = RUNTIME_DIR / "promoted_overrides.json"

OVERRIDES_SCHEMA_VERSION = 1
OVERRIDES_ENABLED_ENV = "STRATEGY_OVERRIDES_ENABLED"

# Only these strategy / risk threshold knobs may ever be tuned by a promoted
# learning candidate. Field names match the ``Settings`` dataclass in config.py.
OVERRIDABLE_FIELDS: frozenset[str] = frozenset(
    {
        # baseline
        "lookback_months",
        # auto-trader execution thresholds
        "auto_trader_min_order_value_usd",
        "auto_trader_min_hold_minutes",
        "auto_trader_rebalance_drift_pct",
        "auto_trader_exit_confirm_cycles",
        "auto_trader_order_cooldown_seconds",
        "auto_trader_min_symbol_interval_seconds",
        # auto-trader risk caps (tightening is the intended use)
        "auto_trader_max_target_gross_exposure",
        "auto_trader_max_target_weight",
        "auto_trader_max_order_value_usd",
        "auto_trader_max_cycle_turnover_usd",
        "auto_trader_max_epoch_loss_usd",
        "auto_trader_max_epoch_loss_pct",
        # fusion intraday
        "fusion_entry_score",
        "fusion_exit_score",
        "fusion_max_position_weight",
        "fusion_max_gross_exposure",
        "fusion_min_rel_volume",
        "fusion_max_spread_bps",
        "fusion_top_k",
        # ofim intraday
        "ofim_entry_threshold",
        "ofim_exit_threshold",
        "ofim_max_score",
        "ofim_min_vol_acceleration",
        "ofim_max_spread_bps",
        "ofim_max_positions",
        "ofim_max_position_weight",
        "ofim_max_gross_exposure",
    }
)

# Defense in depth: a field is rejected if it contains any of these substrings,
# even if it somehow appears in the whitelist. These cover the real-money
# safety switches, credentials, account ids and connection settings.
FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "enable_real",
    "allow_auto_real",
    "unlock",
    "password",
    "api_key",
    "api_secret",
    "acc_id",
    "host",
    "port",
    "trd_env",
    "trd_market",
    "sandbox",
    "md5",
)


class PromotionError(RuntimeError):
    """Raised when a candidate cannot be promoted into an override."""


def param_to_field(param: str | None) -> str:
    """Map an env-var name (``AUTO_TRADER_MIN_ORDER_VALUE_USD``) to a Settings
    dataclass field (``auto_trader_min_order_value_usd``)."""
    return (param or "").strip().lower()


def is_overridable(field: str) -> bool:
    if field not in OVERRIDABLE_FIELDS:
        return False
    return not any(bad in field for bad in FORBIDDEN_SUBSTRINGS)


def overrides_enabled(env: dict[str, str] | None = None) -> bool:
    raw = (env or os.environ).get(OVERRIDES_ENABLED_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ── file helpers ──────────────────────────────────────────────────────────────


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def load_overrides(path: Path = DEFAULT_OVERRIDES_FILE) -> dict[str, Any]:
    doc = _read_json(path)
    if not doc:
        return {"schema_version": OVERRIDES_SCHEMA_VERSION, "updated_at": None, "overrides": {}}
    doc.setdefault("schema_version", OVERRIDES_SCHEMA_VERSION)
    doc.setdefault("overrides", {})
    if not isinstance(doc["overrides"], dict):
        doc["overrides"] = {}
    return doc


def _write_overrides(doc: dict[str, Any], path: Path = DEFAULT_OVERRIDES_FILE) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ── core operations ───────────────────────────────────────────────────────────


def _coerce_like(current: Any, value: Any) -> Any:
    """Coerce ``value`` to the runtime type of ``current`` (int/float only)."""
    if isinstance(current, bool):
        raise TypeError("boolean settings are not overridable")
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    raise TypeError(f"unsupported override target type: {type(current).__name__}")


def promote_candidate(
    candidate_id: str,
    *,
    approved_by: str,
    rationale: str | None = None,
    candidates_path: Path = DEFAULT_CANDIDATES_FILE,
    promotion_path: Path = DEFAULT_PROMOTION_FILE,
    overrides_path: Path = DEFAULT_OVERRIDES_FILE,
) -> dict[str, Any]:
    """Approve an eligible candidate and write it into the override file.

    Raises ``PromotionError`` if the candidate is missing, advisory-only,
    targets a non-whitelisted field, or has not passed the paper gate.
    """
    candidates = {c.get("candidate_id"): c for c in _read_jsonl(candidates_path)}
    cand = candidates.get(candidate_id)
    if cand is None:
        raise PromotionError(f"candidate_id {candidate_id!r} not found in {candidates_path}")

    param = cand.get("param") or ""
    proposed = cand.get("proposed_value")
    if not param or proposed is None:
        raise PromotionError(
            f"candidate {candidate_id} (action={cand.get('action_type')!r}) is advisory-only "
            "— it has no param/proposed_value and cannot become a numeric override. "
            "Use it as a research note (e.g. review a symbol in replay) instead."
        )

    field = param_to_field(param)
    if not is_overridable(field):
        raise PromotionError(
            f"param {param!r} maps to field {field!r}, which is NOT in the safe "
            "override whitelist (safety/credential/connection settings can never "
            "be overridden by the learning loop)."
        )

    decisions = {
        d.get("candidate_id"): d
        for d in (_read_json(promotion_path).get("decisions") or [])
        if isinstance(d, dict)
    }
    decision = decisions.get(candidate_id)
    paper_allowed = bool(decision.get("paper_allowed")) if decision else False
    if not paper_allowed:
        blockers = (decision or {}).get("blockers") or ["no_promotion_decision_found"]
        raise PromotionError(
            f"candidate {candidate_id} is not eligible for paper promotion: {blockers}"
        )

    entry = {
        "field": field,
        "param": param,
        "value": proposed,
        "previous_value": cand.get("current_value"),
        "source_candidate_id": candidate_id,
        "action_type": cand.get("action_type"),
        "confidence": cand.get("confidence"),
        "rationale": rationale or cand.get("rationale"),
        "promotion_decision": (decision or {}).get("decision"),
        # Live promotion is never automated here; the override is paper/research
        # scoped and only takes effect when STRATEGY_OVERRIDES_ENABLED=true.
        "scope": "paper",
        "approved_by": approved_by,
        "approved_at": _utc_now(),
        "evidence_digest": hashlib.sha256(
            json.dumps(cand.get("evidence") or {}, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16],
    }

    doc = load_overrides(overrides_path)
    doc["overrides"][field] = entry
    doc["updated_at"] = _utc_now()
    _write_overrides(doc, overrides_path)
    return entry


def apply_promoted_overrides(settings, *, overrides_path: Path = DEFAULT_OVERRIDES_FILE):
    """Return a copy of ``settings`` with whitelisted promoted overrides applied.

    Generic over any frozen dataclass (it only uses ``dataclasses.replace`` and
    ``dataclasses.fields``). Non-whitelisted or unknown fields are ignored, so a
    tampered override file can never reach a safety switch through this path.
    """
    doc = load_overrides(overrides_path)
    overrides = doc.get("overrides") or {}
    if not overrides:
        return settings

    valid_names = {f.name for f in dataclasses.fields(settings)}
    changes: dict[str, Any] = {}
    for field, entry in overrides.items():
        if field not in valid_names or not is_overridable(field):
            continue
        if not isinstance(entry, dict):
            continue
        current = getattr(settings, field)
        try:
            changes[field] = _coerce_like(current, entry.get("value"))
        except (TypeError, ValueError):
            continue

    if not changes:
        return settings
    return dataclasses.replace(settings, **changes)


def revert_override(
    *,
    field: str | None = None,
    candidate_id: str | None = None,
    overrides_path: Path = DEFAULT_OVERRIDES_FILE,
) -> list[str]:
    """Remove overrides by field name and/or source candidate id. Returns the
    list of removed field names."""
    doc = load_overrides(overrides_path)
    overrides = doc.get("overrides") or {}
    removed: list[str] = []
    if field and field in overrides:
        overrides.pop(field)
        removed.append(field)
    if candidate_id:
        for fname, entry in list(overrides.items()):
            if isinstance(entry, dict) and entry.get("source_candidate_id") == candidate_id:
                overrides.pop(fname)
                removed.append(fname)
    if removed:
        doc["updated_at"] = _utc_now()
        _write_overrides(doc, overrides_path)
    return removed


# ── CLI ───────────────────────────────────────────────────────────────────────


def _cmd_status(args: argparse.Namespace) -> int:
    doc = load_overrides(args.overrides)
    active = doc.get("overrides") or {}
    enabled = overrides_enabled()
    print(f"{OVERRIDES_ENABLED_ENV}={'true' if enabled else 'false'} "
          f"({'overrides ARE applied on config load' if enabled else 'overrides are recorded but NOT applied'})")
    print(f"override file: {args.overrides}")
    print(f"active overrides: {len(active)}")
    for field, entry in active.items():
        print(f"  - {field}: {entry.get('previous_value')} -> {entry.get('value')} "
              f"(candidate {entry.get('source_candidate_id')}, by {entry.get('approved_by')})")
    if active and not enabled:
        print(f"\nTo activate, set {OVERRIDES_ENABLED_ENV}=true in .env, then restart the engine.")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    print(json.dumps(load_overrides(args.overrides), ensure_ascii=False, indent=2))
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    try:
        entry = promote_candidate(
            args.candidate_id,
            approved_by=args.approved_by,
            rationale=args.rationale,
            candidates_path=args.candidates,
            promotion_path=args.promotion,
            overrides_path=args.overrides,
        )
    except PromotionError as exc:
        print(f"REFUSED: {exc}")
        return 2
    print("promoted:")
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    if not overrides_enabled():
        print(f"\nNote: this override is recorded but NOT yet applied. "
              f"Set {OVERRIDES_ENABLED_ENV}=true in .env and restart to activate.")
    return 0


def _cmd_revert(args: argparse.Namespace) -> int:
    removed = revert_override(
        field=args.field, candidate_id=args.candidate_id, overrides_path=args.overrides
    )
    print(f"reverted: {removed}" if removed else "nothing to revert")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m taa_futu.strategy_overrides",
        description="Human-in-the-loop bridge from learning candidates to live strategy overrides.",
    )
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES_FILE)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES_FILE)
    parser.add_argument("--promotion", type=Path, default=DEFAULT_PROMOTION_FILE)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show whether overrides are enabled and list active overrides.")
    sub.add_parser("show", help="Print the raw override document as JSON.")

    promote = sub.add_parser("promote", help="Approve an eligible candidate into an override.")
    promote.add_argument("--candidate-id", required=True)
    promote.add_argument("--approved-by", required=True, help="Your name, recorded for audit.")
    promote.add_argument("--rationale", default=None)

    revert = sub.add_parser("revert", help="Remove an override by field or source candidate id.")
    revert.add_argument("--field", default=None)
    revert.add_argument("--candidate-id", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "status": _cmd_status,
        "show": _cmd_show,
        "promote": _cmd_promote,
        "revert": _cmd_revert,
    }
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
