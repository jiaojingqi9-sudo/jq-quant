"""Read-only multi-sleeve signal endpoint for external queries.

Reuses the same sleeve strategies the live auto-trader runs, but stops short of
any side-effect:

* never calls ``place_order`` / ``unlock_trade`` / ``cancel_*``;
* never writes ``stock_events.jsonl`` / ``stock_fills.jsonl`` / state files;
* the only persistent side-effect is ``market_logger`` raw market-data logs,
  which the sleeve plan generators write unconditionally (already a documented
  side-effect of ``FusionIntradayStrategy.generate_plan``).

Designed so the Futu watcher queue can ask: "what does the current stack think
about US.NVDA right now?" and get a structured answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from typing import Any

from .config import Settings, load_settings
from .strategy_stack import (
    effective_fusion_settings,
    fetch_futu_daily_closes,
    scaled_baseline_target_weights,
    stack_allocations,
    stack_label,
)


@dataclass
class LiveSignalReport:
    generated_at: str
    stack_label: str
    sleeve_weights: dict[str, float]
    queried_symbols: list[str]
    by_symbol: dict[str, dict[str, Any]]
    universe_view: dict[str, Any]
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "stack_label": self.stack_label,
            "sleeve_weights": self.sleeve_weights,
            "queried_symbols": self.queried_symbols,
            "by_symbol": self.by_symbol,
            "universe_view": self.universe_view,
            "errors": self.errors,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)


def _empty_symbol_payload() -> dict[str, Any]:
    return {
        "baseline": None,
        "fusion": None,
        "ofim": None,
        "cascade": None,
        "held": False,
        "stack_target_weight": 0.0,
        "recommendation": "no_target",
        "evidence": [],
    }


def _classify(total: float) -> str:
    if total >= 0.10:
        return "buy_or_hold"
    if total > 0:
        return "light_hold"
    return "no_target"


def compute_live_signal(
    symbols: list[str] | tuple[str, ...] | None = None,
    settings: Settings | None = None,
    *,
    include_universe: bool = True,
) -> LiveSignalReport:
    """Run baseline + fusion + ofim + cascade read-only and merge by-symbol.

    Per sleeve we catch errors so a half-broken OpenD still returns whatever
    other sleeves managed to compute. ``errors`` lists what failed; callers
    should treat any non-empty ``errors`` as a degraded answer.
    """

    settings = settings or load_settings()
    now_iso = datetime.now(UTC).isoformat()

    baseline_w, fusion_w, ofim_w, cascade_w, _ = stack_allocations(settings)
    sleeve_weights = {
        "baseline": round(baseline_w, 4),
        "fusion": round(fusion_w, 4),
        "ofim": round(ofim_w, 4),
        "cascade": round(cascade_w, 4),
    }

    queried = [str(s).strip() for s in (symbols or ()) if str(s).strip()]
    if not queried:
        queried = list(settings.fusion_universe)

    by_symbol: dict[str, dict[str, Any]] = {s: _empty_symbol_payload() for s in queried}
    universe_view: dict[str, Any] = {
        "fusion_features": [],
        "fusion_benchmark_score": None,
        "fusion_exposure": None,
        "ofim_top": [],
        "ofim_benchmark_score": None,
        "cascade_targets": [],
        "cascade_regime_label": None,
        "cascade_regime_score": None,
    }
    errors: list[str] = []

    def ensure(symbol: str) -> dict[str, Any]:
        if symbol not in by_symbol:
            by_symbol[symbol] = _empty_symbol_payload()
        return by_symbol[symbol]

    # Imports are deferred so an absent Futu OpenD does not break test imports.
    from .futu_gateway import FutuPaperTrader, FutuTradeError, FutuTransientError
    from .fusion_intraday import FusionIntradayStrategy
    from .ofim_intraday import OfimIntradayStrategy
    from .cascade_sleeve import generate_live_cascade_plan

    try:
        with FutuPaperTrader(settings) as trader:
            # ── held positions (best-effort) ──────────────────────────────────
            held: set[str] = set()
            try:
                acc_id = trader.resolve_trade_account()
                positions = trader.get_positions(acc_id)
                if not positions.empty and "code" in positions.columns:
                    held = set(positions["code"].astype(str).tolist())
                    for code in held:
                        ensure(code)["held"] = True
            except (FutuTradeError, FutuTransientError) as exc:
                errors.append(f"positions: {exc}")
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(f"positions: {type(exc).__name__}: {exc}")

            # ── baseline sleeve ──────────────────────────────────────────────
            if baseline_w > 0:
                try:
                    closes = fetch_futu_daily_closes(
                        trader, settings.symbols, start=settings.start_date
                    )
                    weights = scaled_baseline_target_weights(
                        closes,
                        settings,
                        reference_date=datetime.now(UTC).date(),
                    )
                    for sym, w in weights.items():
                        payload = ensure(sym)
                        payload["baseline"] = {
                            "scaled_weight": float(w),
                            "raw_weight": float(w) / baseline_w if baseline_w > 0 else 0.0,
                            "in_universe": True,
                        }
                    for sym in queried:
                        if by_symbol[sym].get("baseline") is None:
                            by_symbol[sym]["baseline"] = {
                                "scaled_weight": 0.0,
                                "raw_weight": 0.0,
                                "in_universe": sym in settings.symbols,
                                "reason": "below_10mo_MA" if sym in settings.symbols else "not_in_baseline_universe",
                            }
                except Exception as exc:
                    errors.append(f"baseline: {type(exc).__name__}: {exc}")

            # ── fusion sleeve ───────────────────────────────────────────────
            if fusion_w > 0:
                try:
                    fusion_strategy = FusionIntradayStrategy(effective_fusion_settings(settings))
                    fusion_plan = fusion_strategy.generate_plan(trader, held)
                    universe_view["fusion_benchmark_score"] = fusion_plan.benchmark_score
                    universe_view["fusion_exposure"] = fusion_plan.exposure
                    if include_universe:
                        universe_view["fusion_features"] = [
                            {
                                "code": f.code,
                                "score": f.score,
                                "eligible": f.eligible,
                                "reason": f.reason,
                                "spread_bps": f.spread_bps,
                                "momentum_5m": f.momentum_5m,
                                "vwap_distance": f.vwap_distance,
                                "orderbook_imbalance": f.orderbook_imbalance,
                                "rel_volume": f.rel_volume,
                            }
                            for f in fusion_plan.features[:20]
                        ]

                    feature_by_code = {f.code: f for f in fusion_plan.features}
                    for sym, w in fusion_plan.target_weights.items():
                        feat = feature_by_code.get(sym)
                        ensure(sym)["fusion"] = {
                            "scaled_weight": round(w * fusion_w, 6),
                            "raw_weight": float(w),
                            "score": feat.score if feat else None,
                            "eligible": feat.eligible if feat else None,
                            "reason": feat.reason if feat else None,
                        }
                    for sym in queried:
                        if by_symbol[sym].get("fusion") is None:
                            feat = feature_by_code.get(sym)
                            by_symbol[sym]["fusion"] = {
                                "scaled_weight": 0.0,
                                "raw_weight": 0.0,
                                "score": feat.score if feat else None,
                                "eligible": feat.eligible if feat else None,
                                "reason": feat.reason if feat else "not_in_fusion_universe",
                            }
                except Exception as exc:
                    errors.append(f"fusion: {type(exc).__name__}: {exc}")

            # ── ofim sleeve ─────────────────────────────────────────────────
            if ofim_w > 0:
                try:
                    ofim_strategy = OfimIntradayStrategy(settings)
                    ofim_plan = ofim_strategy.generate_plan(trader, held)
                    universe_view["ofim_benchmark_score"] = getattr(ofim_plan, "benchmark_score", None)
                    if include_universe:
                        universe_view["ofim_top"] = [
                            {"code": k, "weight": round(v, 6)}
                            for k, v in sorted(
                                ofim_plan.target_weights.items(),
                                key=lambda kv: kv[1],
                                reverse=True,
                            )[:10]
                        ]
                    for sym, w in ofim_plan.target_weights.items():
                        ensure(sym)["ofim"] = {
                            "scaled_weight": round(w * ofim_w, 6),
                            "raw_weight": float(w),
                        }
                    for sym in queried:
                        if by_symbol[sym].get("ofim") is None:
                            by_symbol[sym]["ofim"] = {
                                "scaled_weight": 0.0,
                                "raw_weight": 0.0,
                                "reason": "not_in_ofim_target",
                            }
                except Exception as exc:
                    errors.append(f"ofim: {type(exc).__name__}: {exc}")

            # ── cascade sleeve ──────────────────────────────────────────────
            if cascade_w > 0:
                try:
                    cascade_plan = generate_live_cascade_plan(settings, trader=trader)
                    cascade_targets = dict(getattr(cascade_plan, "target_weights", {}) or {})
                    universe_view["cascade_regime_label"] = getattr(cascade_plan, "regime_label", None)
                    universe_view["cascade_regime_score"] = getattr(cascade_plan, "regime_score", None)
                    if include_universe:
                        universe_view["cascade_targets"] = [
                            {"code": k, "weight": round(v, 6)}
                            for k, v in cascade_targets.items()
                        ]
                    for sym, w in cascade_targets.items():
                        ensure(sym)["cascade"] = {
                            "scaled_weight": round(w * cascade_w, 6),
                            "raw_weight": float(w),
                        }
                    for sym in queried:
                        if by_symbol[sym].get("cascade") is None:
                            by_symbol[sym]["cascade"] = {
                                "scaled_weight": 0.0,
                                "raw_weight": 0.0,
                                "reason": "not_in_cascade_target",
                            }
                except Exception as exc:
                    errors.append(f"cascade: {type(exc).__name__}: {exc}")
    except Exception as exc:
        errors.append(f"trader: {type(exc).__name__}: {exc}")

    # Merge sleeve weights and classify recommendation per symbol
    for sym, payload in by_symbol.items():
        contributions: list[str] = []
        total = 0.0
        for sleeve in ("baseline", "fusion", "ofim", "cascade"):
            entry = payload.get(sleeve)
            if not entry:
                continue
            scaled = float(entry.get("scaled_weight") or 0.0)
            total += scaled
            if scaled > 0:
                contributions.append(f"{sleeve}={scaled:.4f}")
        payload["stack_target_weight"] = round(total, 6)
        payload["recommendation"] = _classify(total)
        payload["evidence"] = contributions or payload.get("evidence") or []
        if payload["held"] and total <= 0:
            payload["recommendation"] = "exit_candidate"

    return LiveSignalReport(
        generated_at=now_iso,
        stack_label=stack_label(settings),
        sleeve_weights=sleeve_weights,
        queried_symbols=queried,
        by_symbol=by_symbol,
        universe_view=universe_view,
        errors=errors,
    )
