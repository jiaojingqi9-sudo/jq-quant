"""Optional Fusion Intraday pre-gate.

This module is **strictly defensive**. It is only ever called from a single
line at the end of :func:`fusion_intraday.FusionIntradayStrategy.generate_plan`
and is allowed to do one thing only: **drop entries** from the
``target_weights`` dict produced by :func:`fusion_intraday.build_target_weights`.

What it does NOT do:

* It never adds new symbols.
* It never changes the weight of any symbol it keeps.
* It never modifies orders, only the symbol set going into order generation.
* It never raises — every code path returns the input weights unchanged on
  any failure.

What it does:

* When ``FUSION_FUTU_PREGATE_ENABLED=false`` (default), it is a no-op.
* When enabled, it applies a series of safety filters using data we already
  have in the ``FusionFeature`` list (so no extra Futu calls):

  * stricter Level-2 orderbook imbalance threshold
  * tick imbalance threshold
  * tighter spread threshold

* When ``FUSION_FUTU_PREGATE_LOG_ONLY=true`` (default), the gate computes its
  decisions and writes them to ``runtime/stock_events.jsonl`` as
  ``fusion_pregate_decision`` events, **but still returns the unmodified
  weights** so the user can review what the gate would have filtered before
  letting it take effect.
* When ``FUSION_FUTU_PREGATE_LOG_ONLY=false`` and the gate is enabled, the
  decisions actually apply: filtered symbols are dropped from the returned
  dict.

Rollback procedure: ``FUSION_FUTU_PREGATE_ENABLED=false``. That's it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from taa_futu.stock_events import append_stock_event


@dataclass(slots=True)
class PregateThresholds:
    min_orderbook_imbalance: float
    min_tick_imbalance: float
    max_spread_bps: float


def _read_thresholds(settings: Any) -> PregateThresholds:
    return PregateThresholds(
        min_orderbook_imbalance=float(getattr(settings, "fusion_futu_pregate_min_ob_imbalance", 0.20)),
        min_tick_imbalance=float(getattr(settings, "fusion_futu_pregate_min_tick_imbalance", 0.15)),
        max_spread_bps=float(getattr(settings, "fusion_futu_pregate_max_spread_bps", 15.0)),
    )


def _evaluate_feature(feature: Any, thresholds: PregateThresholds) -> tuple[bool, list[str]]:
    """Return ``(keep, reasons)``. ``keep=True`` means the symbol passes the
    gate. Reasons list any filter that fired (empty when keep=True).
    """

    reasons: list[str] = []
    ob = float(getattr(feature, "orderbook_imbalance", 0.0) or 0.0)
    tick = float(getattr(feature, "tick_imbalance", 0.0) or 0.0)
    spread = float(getattr(feature, "spread_bps", 0.0) or 0.0)

    if abs(ob) < thresholds.min_orderbook_imbalance:
        reasons.append(f"weak_ob_imbalance={ob:.3f}<{thresholds.min_orderbook_imbalance:.3f}")
    if abs(tick) < thresholds.min_tick_imbalance:
        reasons.append(f"weak_tick_imbalance={tick:.3f}<{thresholds.min_tick_imbalance:.3f}")
    if spread > thresholds.max_spread_bps:
        reasons.append(f"spread_too_wide={spread:.2f}>{thresholds.max_spread_bps:.2f}bps")

    # If price-side conviction and orderflow disagree (e.g. score positive but
    # orderbook imbalance negative), drop.
    score = float(getattr(feature, "score", 0.0) or 0.0)
    if score > 0 and ob < 0 and abs(ob) >= thresholds.min_orderbook_imbalance:
        reasons.append("score_vs_ob_disagreement")
    if score > 0 and tick < 0 and abs(tick) >= thresholds.min_tick_imbalance:
        reasons.append("score_vs_tick_disagreement")

    return (not reasons, reasons)


def apply(
    target_weights: dict[str, float],
    *,
    features: Iterable[Any],
    settings: Any,
    cycle_id: str | None = None,
) -> dict[str, float]:
    """Public entry point.

    The contract:

    * Returns a dict with a subset of the input keys.
    * If the gate is disabled or any exception fires, returns the input dict
      unchanged.
    """

    if not getattr(settings, "fusion_futu_pregate_enabled", False):
        return target_weights

    try:
        thresholds = _read_thresholds(settings)
        log_only = bool(getattr(settings, "fusion_futu_pregate_log_only", True))
        feature_index = {
            getattr(f, "code", None): f for f in features if getattr(f, "code", None)
        }
        decisions: dict[str, dict[str, Any]] = {}
        for symbol in list(target_weights.keys()):
            feature = feature_index.get(symbol)
            if feature is None:
                decisions[symbol] = {"decision": "pass", "reasons": ["no_feature"]}
                continue
            keep, reasons = _evaluate_feature(feature, thresholds)
            decisions[symbol] = {
                "decision": "pass" if keep else "filter",
                "reasons": reasons or ["ok"],
                "weight": target_weights[symbol],
                "score": float(getattr(feature, "score", 0.0) or 0.0),
                "orderbook_imbalance": float(getattr(feature, "orderbook_imbalance", 0.0) or 0.0),
                "tick_imbalance": float(getattr(feature, "tick_imbalance", 0.0) or 0.0),
                "spread_bps": float(getattr(feature, "spread_bps", 0.0) or 0.0),
            }

        # Emit one consolidated event per cycle so the journal stays compact.
        try:
            append_stock_event(
                "fusion_pregate_decision",
                {
                    "log_only": log_only,
                    "thresholds": {
                        "min_orderbook_imbalance": thresholds.min_orderbook_imbalance,
                        "min_tick_imbalance": thresholds.min_tick_imbalance,
                        "max_spread_bps": thresholds.max_spread_bps,
                    },
                    "symbol_count_in": len(target_weights),
                    "decisions": decisions,
                },
                cycle_id=cycle_id,
            )
        except Exception:
            # Audit-event emission must never block trading.
            pass

        if log_only:
            return target_weights

        # Actually apply the filter.
        return {
            symbol: weight
            for symbol, weight in target_weights.items()
            if decisions.get(symbol, {}).get("decision") == "pass"
        }
    except Exception:
        # Any failure → fall back to the input. The pre-gate must NEVER be the
        # reason a trading cycle fails.
        return target_weights
