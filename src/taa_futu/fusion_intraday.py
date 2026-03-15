from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import math

import pandas as pd

from .config import Settings
from . import market_logger


def _clip(value: float, scale: float) -> float:
    if scale <= 0:
        return 0.0
    normalized = value / scale
    return max(-1.0, min(1.0, normalized))


def _compute_vwap(bars: pd.DataFrame) -> float:
    volume = bars["volume"].fillna(0)
    if volume.sum() <= 0:
        return float(bars["close"].iloc[-1])
    return float((bars["close"] * volume).sum() / volume.sum())


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
    last_price = float(bars["close"].iloc[-1])
    if last_price <= 0:
        return 0.0
    return atr / last_price


def _compute_rel_volume(bars: pd.DataFrame) -> float:
    if len(bars) < 21:
        return 1.0
    baseline = bars["volume"].iloc[-21:-1].median()
    if baseline <= 0:
        return 1.0
    return float(bars["volume"].iloc[-1] / baseline)


def _compute_breakout_pct(bars: pd.DataFrame, opening_range_minutes: int) -> float:
    opening_range = bars.head(min(opening_range_minutes, len(bars)))
    opening_high = float(opening_range["high"].max())
    if opening_high <= 0:
        return 0.0
    return float(bars["close"].iloc[-1] / opening_high - 1)


def _compute_order_book_imbalance(order_book: dict | None, snapshot: pd.Series, depth: int) -> float:
    if order_book and order_book.get("Bid") and order_book.get("Ask"):
        bid_volume = sum(level[1] for level in order_book["Bid"][:depth])
        ask_volume = sum(level[1] for level in order_book["Ask"][:depth])
    else:
        bid_volume = float(snapshot.get("bid_vol", 0.0) or 0.0)
        ask_volume = float(snapshot.get("ask_vol", 0.0) or 0.0)

    total = bid_volume + ask_volume
    if total <= 0:
        return 0.0
    return float((bid_volume - ask_volume) / total)


def _compute_tick_imbalance(ticks: pd.DataFrame) -> float:
    if ticks.empty:
        return 0.0
    sign_map = {"BUY": 1.0, "SELL": -1.0, "NEUTRAL": 0.0}
    signed = ticks["ticker_direction"].map(sign_map).fillna(0.0) * ticks["volume"].fillna(0.0)
    volume = ticks["volume"].fillna(0.0).sum()
    if volume <= 0:
        return 0.0
    return float(signed.sum() / volume)


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
            remaining.pop(code)

    return {code: round(weight, 6) for code, weight in weights.items() if weight > 0}


@dataclass(frozen=True)
class FusionFeature:
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


@dataclass(frozen=True)
class FusionPlan:
    benchmark: str
    benchmark_score: float
    exposure: float
    target_weights: dict[str, float]
    features: list[FusionFeature]


def compute_benchmark_score(
    bars: pd.DataFrame,
    snapshot: pd.Series,
    order_book: dict | None,
    ticks: pd.DataFrame,
    settings: Settings,
) -> float:
    last_price = float(snapshot["last_price"])
    vwap = _compute_vwap(bars)
    momentum_5m = float(last_price / bars["close"].iloc[-6] - 1) if len(bars) >= 6 else 0.0
    vwap_distance = float(last_price / vwap - 1) if vwap > 0 else 0.0
    orderbook_imbalance = _compute_order_book_imbalance(order_book, snapshot, settings.fusion_order_book_depth)
    tick_imbalance = _compute_tick_imbalance(ticks)
    return (
        0.40 * _clip(momentum_5m, 0.004)
        + 0.30 * _clip(vwap_distance, 0.0025)
        + 0.20 * _clip(orderbook_imbalance, 0.35)
        + 0.10 * _clip(tick_imbalance, 0.35)
    )


def compute_symbol_feature(
    code: str,
    bars: pd.DataFrame,
    snapshot: pd.Series,
    order_book: dict | None,
    ticks: pd.DataFrame,
    benchmark_score: float,
    settings: Settings,
) -> FusionFeature:
    last_price = float(snapshot["last_price"])
    prev_close = float(snapshot["prev_close_price"])
    gap_pct = float(last_price / prev_close - 1) if prev_close > 0 else 0.0
    momentum_5m = float(last_price / bars["close"].iloc[-6] - 1) if len(bars) >= 6 else 0.0
    vwap = _compute_vwap(bars)
    vwap_distance = float(last_price / vwap - 1) if vwap > 0 else 0.0
    rel_volume = _compute_rel_volume(bars)
    breakout_pct = _compute_breakout_pct(bars, settings.fusion_opening_range_minutes)
    orderbook_imbalance = _compute_order_book_imbalance(order_book, snapshot, settings.fusion_order_book_depth)
    tick_imbalance = _compute_tick_imbalance(ticks)
    spread = float(snapshot.get("price_spread", 0.01) or 0.01)
    spread_bps = spread / last_price * 10_000 if last_price > 0 else 10_000.0
    atr_pct = _compute_atr_pct(bars)

    components = {
        "gap": 0.18 * _clip(gap_pct, 0.03),
        "momentum": 0.20 * _clip(momentum_5m, 0.01),
        "vwap": 0.17 * _clip(vwap_distance, 0.005),
        "rel_volume": 0.15 * _clip(rel_volume - 1.0, 1.0),
        "breakout": 0.10 * _clip(breakout_pct, 0.01),
        "orderbook": 0.12 * _clip(orderbook_imbalance, 0.40),
        "ticks": 0.08 * _clip(tick_imbalance, 0.40),
    }
    score = sum(components.values()) * max(0.0, benchmark_score + 0.25)

    reasons: list[str] = []
    if benchmark_score <= 0:
        reasons.append("market_regime_off")
    if spread_bps > settings.fusion_max_spread_bps:
        reasons.append("spread_too_wide")
    if rel_volume < settings.fusion_min_rel_volume:
        reasons.append("rel_volume_too_low")
    if vwap_distance <= 0:
        reasons.append("below_vwap")
    if momentum_5m <= 0:
        reasons.append("weak_5m_momentum")
    if breakout_pct < -0.001:
        reasons.append("below_opening_range")

    return FusionFeature(
        code=code,
        last_price=last_price,
        gap_pct=gap_pct,
        momentum_5m=momentum_5m,
        vwap_distance=vwap_distance,
        rel_volume=rel_volume,
        breakout_pct=breakout_pct,
        orderbook_imbalance=orderbook_imbalance,
        tick_imbalance=tick_imbalance,
        spread_bps=spread_bps,
        atr_pct=atr_pct,
        score=round(score, 6),
        eligible=not reasons,
        reason=",".join(reasons) if reasons else "ok",
    )


def build_target_weights(
    features: list[FusionFeature],
    benchmark_score: float,
    held_symbols: set[str],
    settings: Settings,
) -> tuple[float, dict[str, float]]:
    if benchmark_score <= 0:
        return 0.0, {}

    exposure = settings.fusion_max_gross_exposure * min(1.0, max(0.0, 0.5 + benchmark_score / 1.5))
    candidates: dict[str, float] = {}
    for feature in features:
        if feature.code in held_symbols:
            if feature.score >= settings.fusion_exit_score and feature.spread_bps <= settings.fusion_max_spread_bps:
                candidates[feature.code] = max(feature.score, settings.fusion_exit_score)
        elif feature.eligible and feature.score >= settings.fusion_entry_score:
            candidates[feature.code] = feature.score

    top_candidates = dict(sorted(candidates.items(), key=lambda item: item[1], reverse=True)[: settings.fusion_top_k])
    return exposure, _weight_with_cap(top_candidates, exposure, settings.fusion_max_position_weight)


class FusionIntradayStrategy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def generate_plan(self, trader, held_symbols: set[str]) -> FusionPlan:
        cycle_ts = datetime.now(UTC)

        symbols = list(dict.fromkeys((self.settings.fusion_benchmark, *self.settings.fusion_universe)))
        trader.subscribe_realtime(symbols)
        snapshots = trader.get_snapshots(symbols)

        # Log snapshots for every symbol in this cycle
        for code in symbols:
            if code in snapshots.index:
                market_logger.log_snapshot(code, snapshots.loc[code], cycle_ts)

        # Benchmark data + logging
        benchmark_bars = trader.get_recent_klines(self.settings.fusion_benchmark, self.settings.fusion_lookback_bars)
        benchmark_ticks = trader.get_recent_tickers(self.settings.fusion_benchmark, self.settings.fusion_tick_window)
        benchmark_order_book = trader.get_order_book_safe(self.settings.fusion_benchmark, self.settings.fusion_order_book_depth)
        market_logger.log_klines(self.settings.fusion_benchmark, benchmark_bars, cycle_ts)
        market_logger.log_ticks(self.settings.fusion_benchmark, benchmark_ticks, cycle_ts)
        market_logger.log_lob(self.settings.fusion_benchmark, benchmark_order_book, cycle_ts)

        benchmark_score = compute_benchmark_score(
            benchmark_bars,
            snapshots.loc[self.settings.fusion_benchmark],
            benchmark_order_book,
            benchmark_ticks,
            self.settings,
        )

        features: list[FusionFeature] = []
        for code in self.settings.fusion_universe:
            bars = trader.get_recent_klines(code, self.settings.fusion_lookback_bars)
            order_book = trader.get_order_book_safe(code, self.settings.fusion_order_book_depth)
            ticks = trader.get_recent_tickers(code, self.settings.fusion_tick_window)

            # Log raw market data for each universe symbol
            market_logger.log_klines(code, bars, cycle_ts)
            market_logger.log_lob(code, order_book, cycle_ts)
            market_logger.log_ticks(code, ticks, cycle_ts)

            feature = compute_symbol_feature(
                code=code,
                bars=bars,
                snapshot=snapshots.loc[code],
                order_book=order_book,
                ticks=ticks,
                benchmark_score=benchmark_score,
                settings=self.settings,
            )
            # Log the computed feature (打分) immediately after calculation
            market_logger.log_feature(feature, cycle_ts)
            features.append(feature)

        exposure, target_weights = build_target_weights(features, benchmark_score, held_symbols, self.settings)
        plan = FusionPlan(
            benchmark=self.settings.fusion_benchmark,
            benchmark_score=round(benchmark_score, 6),
            exposure=round(exposure, 6),
            target_weights=target_weights,
            features=sorted(features, key=lambda item: item.score, reverse=True),
        )
        # Log the complete plan (信号) — this is the single source of truth for replay
        market_logger.log_plan(plan, cycle_ts)
        return plan

