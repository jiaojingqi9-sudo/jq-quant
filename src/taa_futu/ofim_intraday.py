from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import math

import pandas as pd

from .config import Settings
from . import market_logger


def _compute_benchmark_score(
    bars: pd.DataFrame,
    snapshot: pd.Series,
    order_book: dict | None,
) -> float:
    """Simple market-regime score for the benchmark symbol.
    Combines 5m momentum, VWAP deviation, and shallow LOB imbalance.
    Range: roughly −1 to +1. Gate: <= −0.15 means clearly bearish → don't trade.
    """
    if bars.empty:
        return 0.0
    last = float(snapshot.get("last_price", 0.0) or 0.0)
    if last <= 0:
        return 0.0

    # 5-minute momentum
    mom_5m = float(last / bars["close"].iloc[-6] - 1) if len(bars) >= 6 else 0.0
    # VWAP deviation
    vol = bars["volume"].fillna(0)
    vwap = float((bars["close"] * vol).sum() / vol.sum()) if vol.sum() > 0 else last
    vwap_dev = float(last / vwap - 1) if vwap > 0 else 0.0
    # LOB imbalance (shallow, 1-5 levels)
    bid_vol = _tier_volume(order_book, "Bid", 1, 5)
    ask_vol = _tier_volume(order_book, "Ask", 1, 5)
    total_vol = bid_vol + ask_vol
    lob_imb = (bid_vol - ask_vol) / total_vol if total_vol > 0 else 0.0

    score = (
        0.50 * _clip(mom_5m, 0.004)
        + 0.30 * _clip(vwap_dev, 0.0025)
        + 0.20 * max(-1.0, min(1.0, lob_imb))
    )
    return round(float(score), 6)


def _clip(value: float, scale: float) -> float:
    if scale <= 0:
        return 0.0
    normalized = value / scale
    return max(-1.0, min(1.0, normalized))


def _level_volume(level: object) -> float:
    try:
        return float(level[1])
    except Exception:
        return 0.0


def _tier_volume(order_book: dict | None, side: str, start: int, end: int) -> float:
    if not order_book:
        return 0.0
    levels = order_book.get(side, []) or []
    if start <= 0 or end <= 0:
        return 0.0
    sliced = levels[max(0, start - 1) : max(0, end)]
    return sum(_level_volume(level) for level in sliced)


def compute_multi_level_ofi(
    order_book: dict | None,
    prev_order_book: dict | None,
    depth_tiers: tuple[tuple[int, int], ...],
) -> dict[str, float]:
    results: dict[str, float] = {}
    for idx, (start, end) in enumerate(depth_tiers, start=1):
        bid_vol = _tier_volume(order_book, "Bid", start, end)
        ask_vol = _tier_volume(order_book, "Ask", start, end)
        prev_bid = _tier_volume(prev_order_book, "Bid", start, end)
        prev_ask = _tier_volume(prev_order_book, "Ask", start, end)
        delta_bid = bid_vol - prev_bid
        delta_ask = ask_vol - prev_ask
        ofi_raw = delta_bid - delta_ask
        scale = max(bid_vol + ask_vol, 1.0)
        results[f"tier_{idx}"] = _clip(ofi_raw, scale)
    return results


def compute_volume_acceleration(bars_1m: pd.DataFrame) -> float:
    if len(bars_1m) < 30:
        return 1.0
    recent = bars_1m["volume"].iloc[-5:].mean()
    baseline = bars_1m["volume"].iloc[-30:-5].mean()
    if baseline <= 0:
        return 1.0
    return float(recent / baseline)


def compute_micro_momentum(bars_1m: pd.DataFrame) -> dict[str, float]:
    close = bars_1m["close"]
    last = float(close.iloc[-1]) if len(close) else 0.0
    return {
        "mom_3m": last / float(close.iloc[-4]) - 1 if len(close) >= 4 else 0.0,
        "mom_10m": last / float(close.iloc[-11]) - 1 if len(close) >= 11 else 0.0,
        "mom_30m": last / float(close.iloc[-31]) - 1 if len(close) >= 31 else 0.0,
    }


def compute_vwap_deviation(bars_1m: pd.DataFrame) -> float:
    volume = bars_1m["volume"].fillna(0)
    total_vol = volume.sum()
    if total_vol <= 0:
        return 0.0
    vwap = (bars_1m["close"] * volume).sum() / total_vol
    return float(bars_1m["close"].iloc[-1] / vwap - 1)


def compute_tick_aggression(ticks: pd.DataFrame) -> float:
    if ticks.empty:
        return 0.5
    buy_vol = ticks.loc[ticks["ticker_direction"] == "BUY", "volume"].sum()
    total = ticks["volume"].sum()
    if total <= 0:
        return 0.5
    return float(buy_vol / total)


def compute_spread_quality(snapshot: pd.Series) -> float:
    bid = float(snapshot.get("bid_price", 0) or 0.0)
    ask = float(snapshot.get("ask_price", 0) or 0.0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else float(snapshot.get("last_price", 0) or 0.0)
    if mid <= 0:
        return 9999.0
    return (ask - bid) / mid * 10_000


def _compute_atr_pct(bars: pd.DataFrame, window: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    prev_close = bars["close"].shift(1)
    true_range = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - prev_close).abs(),
            (bars["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = float(true_range.tail(window).mean())
    last_price = float(bars["close"].iloc[-1]) if len(bars) else 0.0
    if last_price <= 0:
        return 0.0
    return atr / last_price


def _weight_with_cap(score_map: dict[str, float], total_exposure: float, max_weight: float) -> dict[str, float]:
    if total_exposure <= 0 or not score_map:
        return {}
    remaining = score_map.copy()
    weights: dict[str, float] = {}
    budget = total_exposure
    while remaining and budget > 0:
        total_score = sum(remaining.values())
        if total_score <= 0:
            break
        capped: list[str] = []
        for code, score in remaining.items():
            proposed = budget * score / total_score
            if proposed > max_weight + 1e-9:
                weights[code] = max_weight
                budget -= max_weight
                capped.append(code)
        if not capped:
            for code, score in remaining.items():
                weights[code] = budget * score / total_score
            break
        for code in capped:
            remaining.pop(code, None)
    return {code: round(weight, 6) for code, weight in weights.items() if weight > 0}


@dataclass(frozen=True)
class OfimFeature:
    strategy: str
    code: str
    last_price: float
    ofi_tier_1: float
    ofi_tier_2: float
    ofi_tier_3: float
    vol_accel: float
    mom_3m: float
    mom_10m: float
    mom_30m: float
    vwap_dev: float
    tick_agg: float
    spread_bps: float
    score: float
    direction: int
    conviction: float
    eligible: bool
    reason: str


@dataclass(frozen=True)
class OfimPlan:
    strategy: str
    benchmark: str
    benchmark_score: float
    exposure: float
    target_weights: dict[str, float]
    features: list[OfimFeature]


def ofim_trade_symbols(settings: Settings) -> tuple[str, ...]:
    return settings.ofim_universe


class OfimIntradayStrategy:
    def __init__(self, settings: Settings, *, prev_order_books: dict[str, dict] | None = None) -> None:
        self.settings = settings
        self.prev_order_books: dict[str, dict] = prev_order_books or {}

    def _score_symbol(
        self,
        code: str,
        order_book: dict | None,
        bars_1m: pd.DataFrame,
        ticks: pd.DataFrame,
        snapshot: pd.Series,
    ) -> OfimFeature:
        prev_book = self.prev_order_books.get(code)
        ofi = compute_multi_level_ofi(order_book, prev_book, self.settings.ofim_depth_tiers)
        vol_accel = compute_volume_acceleration(bars_1m)
        momentum = compute_micro_momentum(bars_1m)
        vwap_dev = compute_vwap_deviation(bars_1m)
        tick_agg = compute_tick_aggression(ticks)
        spread = compute_spread_quality(snapshot)

        long_score = (
            0.25 * ofi.get("tier_2", 0.0)
            + 0.15 * ofi.get("tier_1", 0.0)
            + 0.10 * ofi.get("tier_3", 0.0)
            + 0.15 * _clip(momentum.get("mom_3m", 0.0), 0.005)
            + 0.10 * _clip(momentum.get("mom_10m", 0.0), 0.015)
            + 0.10 * _clip(vol_accel - 1.0, 2.0)
            + 0.10 * _clip(tick_agg - 0.5, 0.3)
            + 0.05 * _clip(vwap_dev, 0.005)
        )

        direction = 1 if long_score >= self.settings.ofim_entry_threshold else 0
        conviction = min(1.0, long_score / max(self.settings.ofim_max_score, 1e-9)) if direction else 0.0
        reasons: list[str] = []
        if spread > self.settings.ofim_max_spread_bps:
            reasons.append("spread_too_wide")
        if vol_accel < self.settings.ofim_min_vol_acceleration:
            reasons.append("volume_too_low")
        if direction == 0:
            reasons.append("score_below_entry")

        eligible = not reasons or (direction == 1 and all(r not in {"spread_too_wide", "volume_too_low"} for r in reasons))
        reason = ",".join(reasons) if reasons else "ok"
        return OfimFeature(
            strategy="OFIM",
            code=code,
            last_price=float(snapshot.get("last_price", 0.0) or 0.0),
            ofi_tier_1=ofi.get("tier_1", 0.0),
            ofi_tier_2=ofi.get("tier_2", 0.0),
            ofi_tier_3=ofi.get("tier_3", 0.0),
            vol_accel=vol_accel,
            mom_3m=momentum.get("mom_3m", 0.0),
            mom_10m=momentum.get("mom_10m", 0.0),
            mom_30m=momentum.get("mom_30m", 0.0),
            vwap_dev=vwap_dev,
            tick_agg=tick_agg,
            spread_bps=spread,
            score=round(long_score, 6),
            direction=direction,
            conviction=round(conviction, 6),
            eligible=eligible and direction == 1,
            reason=reason,
        )

    def generate_plan(self, trader, held_symbols: set[str]) -> OfimPlan:
        cycle_ts = datetime.now(UTC)
        symbols = list(dict.fromkeys(self.settings.ofim_universe))
        benchmark = self.settings.ofim_benchmark

        _empty = OfimPlan(
            strategy="OFIM",
            benchmark=benchmark,
            benchmark_score=0.0,
            exposure=0.0,
            target_weights={},
            features=[],
        )

        if not symbols:
            return _empty

        # ── 1. Subscribe & snapshot (benchmark + universe in one call) ────────
        all_symbols = list(dict.fromkeys([benchmark, *symbols]))
        trader.subscribe_realtime(all_symbols)
        snapshots = trader.get_snapshots(all_symbols)

        # ── 2. Benchmark regime score ─────────────────────────────────────────
        benchmark_score = 0.0
        if benchmark in snapshots.index:
            bm_bars = trader.get_recent_klines(benchmark, self.settings.ofim_lookback_bars)
            bm_snap = snapshots.loc[benchmark]
            bm_lob = trader.get_order_book_safe(benchmark, 5)
            benchmark_score = _compute_benchmark_score(bm_bars, bm_snap, bm_lob)
            market_logger.log_snapshot(benchmark, bm_snap, cycle_ts)
            market_logger.log_klines(benchmark, bm_bars, cycle_ts)
            market_logger.log_lob(benchmark, bm_lob, cycle_ts)

        # ── 3. Regime gate: only block in clearly bearish market ──────────────
        if benchmark_score <= -0.15:
            plan = OfimPlan(
                strategy="OFIM",
                benchmark=benchmark,
                benchmark_score=round(benchmark_score, 6),
                exposure=0.0,
                target_weights={},
                features=[],
            )
            market_logger.log_plan(plan, cycle_ts)
            return plan

        # ── 4. Score each universe symbol ─────────────────────────────────────
        for code in symbols:
            if code in snapshots.index:
                market_logger.log_snapshot(code, snapshots.loc[code], cycle_ts)

        features: list[OfimFeature] = []
        for code in symbols:
            bars = trader.get_recent_klines(code, self.settings.ofim_lookback_bars)
            order_book = trader.get_order_book_safe(code, self.settings.ofim_order_book_depth)
            ticks = trader.get_recent_tickers(code, self.settings.ofim_tick_window)

            market_logger.log_klines(code, bars, cycle_ts)
            market_logger.log_lob(code, order_book, cycle_ts)
            market_logger.log_ticks(code, ticks, cycle_ts)

            feature = self._score_symbol(
                code=code,
                order_book=order_book,
                bars_1m=bars,
                ticks=ticks,
                snapshot=snapshots.loc[code] if code in snapshots.index else pd.Series(dtype=object),
            )
            market_logger.log_feature(feature, cycle_ts)
            features.append(feature)
            if order_book:
                self.prev_order_books[code] = order_book

        # ── 5. Select candidates & compute weights ────────────────────────────
        # Exposure scales with benchmark strength: full at +1, half at 0
        exposure_scale = min(1.0, max(0.0, 0.5 + benchmark_score))
        max_exposure = self.settings.ofim_max_gross_exposure * exposure_scale

        candidates: dict[str, float] = {}
        for feature in features:
            if feature.code in held_symbols:
                if feature.score >= self.settings.ofim_exit_threshold:
                    candidates[feature.code] = max(feature.score, self.settings.ofim_exit_threshold)
            elif feature.eligible and feature.score >= self.settings.ofim_entry_threshold:
                candidates[feature.code] = feature.score

        ordered = dict(sorted(candidates.items(), key=lambda item: item[1], reverse=True)[: self.settings.ofim_max_positions])
        exposure = max_exposure if ordered else 0.0
        target_weights = _weight_with_cap(ordered, exposure, self.settings.ofim_max_position_weight)
        plan = OfimPlan(
            strategy="OFIM",
            benchmark=benchmark,
            benchmark_score=round(benchmark_score, 6),
            exposure=round(exposure, 6),
            target_weights=target_weights,
            features=sorted(features, key=lambda item: item.score, reverse=True),
        )
        market_logger.log_plan(plan, cycle_ts)
        return plan
