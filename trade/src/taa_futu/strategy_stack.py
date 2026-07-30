from __future__ import annotations

from dataclasses import replace
from datetime import date

import pandas as pd

from .config import Settings
from .strategy import latest_completed_signal


def active_stack_strategy(settings: Settings) -> str | None:
    active = (settings.stack_active_strategy or "").strip().lower()
    return active or None


def baseline_sleeve_enabled(settings: Settings) -> bool:
    return active_stack_strategy(settings) == "baseline" or settings.stack_baseline_enabled


def _exclusive_stack_allocations(active: str) -> tuple[float, float, float, float, float]:
    mapping = {
        "baseline": (1.0, 0.0, 0.0, 0.0, 0.0),
        "fusion": (0.0, 1.0, 0.0, 0.0, 0.0),
        "ofim": (0.0, 0.0, 1.0, 0.0, 0.0),
        "cascade": (0.0, 0.0, 0.0, 1.0, 0.0),
    }
    return mapping[active]


def stack_allocations(settings: Settings) -> tuple[float, float, float, float, float]:
    active = active_stack_strategy(settings)
    if active is not None:
        return _exclusive_stack_allocations(active)
    baseline_weight = settings.stack_baseline_weight if baseline_sleeve_enabled(settings) else 0.0
    fusion_weight = settings.stack_fusion_weight
    ofim_weight = settings.stack_ofim_weight
    cascade_weight = settings.stack_cascade_weight
    if baseline_weight < 0 or fusion_weight < 0 or ofim_weight < 0 or cascade_weight < 0:
        raise ValueError("策略组合权重不能为负数 / Stack weights must be non-negative.")
    total = baseline_weight + fusion_weight + ofim_weight + cascade_weight
    if total > 1.0 + 1e-9:
        raise ValueError("STACK_BASELINE_WEIGHT + STACK_FUSION_WEIGHT + STACK_OFIM_WEIGHT + STACK_CASCADE_WEIGHT 不能超过 1.0。")
    reserve_weight = max(0.0, 1.0 - total)
    return baseline_weight, fusion_weight, ofim_weight, cascade_weight, reserve_weight


def effective_fusion_universe(settings: Settings) -> tuple[str, ...]:
    if not baseline_sleeve_enabled(settings) or not settings.stack_isolate_baseline_symbols:
        return settings.fusion_universe
    blocked = set(settings.symbols)
    return tuple(code for code in settings.fusion_universe if code not in blocked)


def effective_fusion_settings(settings: Settings) -> Settings:
    return replace(settings, fusion_universe=effective_fusion_universe(settings))


def stack_target_weights(*sleeve_weights: dict[str, float]) -> dict[str, float]:
    merged: dict[str, float] = {}
    for weights in sleeve_weights:
        for code, weight in weights.items():
            merged[code] = merged.get(code, 0.0) + float(weight)
    return {code: round(weight, 6) for code, weight in merged.items() if weight > 0}


def stack_label(settings: Settings) -> str:
    active = active_stack_strategy(settings)
    if active == "baseline":
        return "独占插头 / Plug: Baseline Only"
    if active == "fusion":
        return "独占插头 / Plug: Fusion Only"
    if active == "ofim":
        return "独占插头 / Plug: OFIM Only"
    if active == "cascade":
        return "独占插头 / Plug: Claude/Cascade Only"
    baseline_weight, fusion_weight, ofim_weight, cascade_weight, reserve_weight = stack_allocations(settings)
    parts: list[str] = []
    if baseline_sleeve_enabled(settings) and baseline_weight > 0:
        parts.append(f"Baseline {baseline_weight:.0%}")
    if fusion_weight > 0:
        parts.append(f"Fusion {fusion_weight:.0%}")
    if ofim_weight > 0:
        parts.append(f"OFIM {ofim_weight:.0%}")
    if cascade_weight > 0:
        parts.append(f"Claude/Cascade {cascade_weight:.0%}")
    if reserve_weight > 0:
        parts.append(f"Cash {reserve_weight:.0%}")
    return " + ".join(parts) if parts else "No active sleeves"


def fetch_futu_daily_closes(
    trader,
    symbols: tuple[str, ...] | list[str],
    *,
    start: str,
    end: str | None = None,
) -> pd.DataFrame:
    close_frames: dict[str, pd.Series] = {}
    for symbol in symbols:
        history = trader.request_history_klines(
            symbol,
            start=start,
            end=end,
            ktype="K_DAY",
            session="RTH",
        )
        if history.empty:
            continue
        frame = history[["time_key", "close"]].copy()
        frame["date"] = pd.to_datetime(frame["time_key"]).dt.normalize()
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        series = frame.dropna(subset=["date", "close"]).drop_duplicates(subset=["date"], keep="last").set_index("date")["close"]
        close_frames[symbol] = series.sort_index()
    if not close_frames:
        return pd.DataFrame(columns=list(symbols))
    return pd.DataFrame(close_frames).sort_index().dropna(how="all")


def scaled_baseline_target_weights(
    daily_closes: pd.DataFrame,
    settings: Settings,
    *,
    reference_date: date,
) -> dict[str, float]:
    baseline_weight, _, _, _, _ = stack_allocations(settings)
    if not baseline_sleeve_enabled(settings) or baseline_weight <= 0:
        return {}
    snapshot = latest_completed_signal(
        daily_closes,
        lookback_months=settings.lookback_months,
        reference_date=reference_date,
    )
    return {code: round(weight * baseline_weight, 6) for code, weight in snapshot.weights.items()}
