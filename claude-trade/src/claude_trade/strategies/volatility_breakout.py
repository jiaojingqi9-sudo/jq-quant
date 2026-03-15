from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .base import BaseStrategy, DataContext, StrategySignal
from . import StrategyRegistry

logger = logging.getLogger(__name__)


def _compute_atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute Average True Range (ATR).

    Args:
        bars: DataFrame with 'high', 'low', 'close' columns.
        period: ATR period (default 14).

    Returns:
        Series of ATR values.
    """
    high = bars["high"]
    low = bars["low"]
    close = bars["close"]

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr


@StrategyRegistry.register("volatility_breakout")
class VolatilityBreakoutStrategy(BaseStrategy):
    """Larry Williams Volatility Breakout strategy adapted for 24/7 crypto markets.

    This is a classic intraday/daily breakout strategy that trades on breakouts of
    the previous period's range. Adapted for crypto's 24/7 market structure with
    daily candle bars.

    **Algorithm:**
    1. Compute previous day's range = high - low
    2. Entry: price > today's open + k * range (k typically 0.5)
    3. Exit: end of current candle period (daily close)
    4. Position sizing: fixed fraction based on ATR
    5. Mostly binary: either fully in or fully out

    **Key features:**
    - Mean reversion bias (breakouts tend to reverse quickly)
    - Volatile market requirement (high range)
    - Short holding period (typically 1 day)
    - Can be extended to intraday with 4h or 1h candles

    Reference: Williams, L. (1981). "The Secret of Selecting Stocks for Immediate
    and Substantial Gains".
    """

    #: Registry key.
    name = "volatility_breakout"

    def compute_signal(self, ctx: DataContext) -> StrategySignal:
        """Compute volatility breakout signal for crypto universe."""
        exchange = ctx.crypto or ctx.primary
        timestamp = ctx.timestamp
        universe = self.settings.vb_universe
        k_factor = self.settings.vb_k_factor
        timeframe = self.settings.vb_timeframe
        risk_pct = self.settings.risk_per_trade_pct

        logger.info(
            f"Computing volatility breakout signal for {len(universe)} symbols, "
            f"k={k_factor}, timeframe={timeframe}"
        )

        if not universe:
            logger.warning("Empty universe for volatility breakout")
            return StrategySignal(
                strategy_name=self.name,
                timestamp=timestamp,
                target_weights={},
                scores={},
                metadata={"error": "empty_universe"},
            )

        # Fetch daily (or specified timeframe) bars
        lookback_bars = 50  # Enough for ATR and volume averages
        scores: dict[str, float] = {}
        target_weights: dict[str, float] = {}

        for symbol in universe:
            try:
                bars = exchange.get_ohlcv(symbol, timeframe, lookback_bars)
                if bars is None or len(bars) < 5:
                    logger.warning(f"{symbol}: insufficient data (need >5 bars)")
                    scores[symbol] = 0.0
                    continue

                # Extract OHLCV data
                opens = bars["open"]
                highs = bars["high"]
                lows = bars["low"]
                closes = bars["close"]
                volumes = bars.get("volume", pd.Series([np.nan] * len(bars)))

                if len(opens) < 2:
                    logger.warning(f"{symbol}: less than 2 bars")
                    scores[symbol] = 0.0
                    continue

                # Current bar's open and price
                current_open = float(opens.iloc[-1])
                current_price = float(closes.iloc[-1])

                # Previous bar's high/low (define the range)
                prev_high = float(highs.iloc[-2])
                prev_low = float(lows.iloc[-2])
                prev_range = prev_high - prev_low

                # Breakout threshold
                breakout_level = current_open + k_factor * prev_range

                # Entry signal: price > breakout_level
                breakout_signal = current_price > breakout_level

                # Volume filter: ensure recent volume > 20-bar average volume
                volume_passes = True
                if "volume" in bars.columns:
                    volumes_clean = volumes.dropna()
                    if len(volumes_clean) >= 21:
                        avg_vol = float(volumes_clean.iloc[-21:-1].mean())
                        current_vol = float(volumes_clean.iloc[-1])
                        volume_passes = current_vol > avg_vol * 0.5  # At least 50% of average
                        logger.debug(
                            f"{symbol}: current_vol={current_vol:.0f}, avg_vol={avg_vol:.0f}, "
                            f"volume_passes={volume_passes}"
                        )

                # ATR-based position sizing
                atr = _compute_atr(bars, 14)
                current_atr = float(atr.iloc[-1])

                # Position sizing: risk_pct / atr_pct
                if breakout_signal and volume_passes:
                    if current_atr > 0 and current_price > 0:
                        atr_pct = current_atr / current_price
                        weight_scalar = risk_pct / max(atr_pct, 0.01)
                        weight = min(weight_scalar, 1.0)  # Cap at 100%
                    else:
                        weight = 0.0
                else:
                    weight = 0.0

                # Score: how far above breakout level (positive) or below (negative)
                if current_price > 0:
                    score = (current_price - breakout_level) / current_price
                else:
                    score = 0.0

                scores[symbol] = round(score, 6)
                if breakout_signal and volume_passes:
                    target_weights[symbol] = round(weight, 6)

                logger.debug(
                    f"{symbol}: open={current_open:.2f}, price={current_price:.2f}, "
                    f"breakout_level={breakout_level:.2f}, signal={breakout_signal}, "
                    f"weight={weight:.2%}"
                )

            except Exception as e:
                logger.error(f"Error computing breakout for {symbol}: {e}")
                scores[symbol] = 0.0
                continue

        # Normalize weights if total exceeds 1.0
        total_weight = sum(target_weights.values())
        if total_weight > 0 and total_weight > 1.0:
            target_weights = {
                sym: round(weight / total_weight, 6)
                for sym, weight in target_weights.items()
            }

        logger.info(f"Breakout signals: {len(target_weights)} breakouts, total_weight={total_weight:.2%}")

        return StrategySignal(
            strategy_name=self.name,
            timestamp=timestamp,
            target_weights=target_weights,
            scores=scores,
            metadata={
                "k_factor": k_factor,
                "timeframe": timeframe,
                "breakout_count": len(target_weights),
            },
        )
