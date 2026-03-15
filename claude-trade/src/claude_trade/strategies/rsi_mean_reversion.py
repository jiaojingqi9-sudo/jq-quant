from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .base import BaseStrategy, DataContext, StrategySignal
from . import StrategyRegistry

logger = logging.getLogger(__name__)


def _compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Compute RSI (Relative Strength Index).

    Args:
        closes: Series of closing prices.
        period: RSI period (default 14).

    Returns:
        Series of RSI values (0-100).
    """
    delta = closes.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


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


@StrategyRegistry.register("rsi_mean_reversion")
class RSIMeanReversionStrategy(BaseStrategy):
    """RSI Mean Reversion strategy for crypto markets.

    This strategy trades mean-reversion patterns using RSI, targeting oversold
    conditions with optional trend and volume filters. Well-suited for range-bound
    and cryptocurrency markets with high intraday volatility.

    **Key principles:**
    - Buy when RSI < oversold_threshold (default 30)
    - Sell when RSI > overbought_threshold (default 70)
    - Apply trend filter (price > 200-EMA) to avoid buying in downtrends
    - Apply volume filter to ensure sufficient liquidity
    - Size positions using risk percentage and ATR-based stops

    Reference: Wilder, J.W. (1978). "New Concepts in Technical Trading Systems".
    """

    #: Registry key.
    name = "rsi_mean_reversion"

    def compute_signal(self, ctx: DataContext) -> StrategySignal:
        """Compute RSI mean reversion signal for crypto universe."""
        exchange = ctx.crypto or ctx.primary
        timestamp = ctx.timestamp
        universe = self.settings.rsi_universe
        period = self.settings.rsi_period
        oversold = self.settings.rsi_oversold
        overbought = self.settings.rsi_overbought
        timeframe = self.settings.rsi_timeframe
        use_trend_filter = True
        trend_period = self.settings.rsi_trend_filter_period
        use_volume_filter = self.settings.rsi_volume_filter
        risk_pct = self.settings.risk_per_trade_pct

        logger.info(
            f"Computing RSI mean reversion signal for {len(universe)} symbols, "
            f"period={period}, oversold={oversold}, timeframe={timeframe}"
        )

        if not universe:
            logger.warning("Empty universe for RSI mean reversion")
            return StrategySignal(
                strategy_name=self.name,
                timestamp=timestamp,
                target_weights={},
                scores={},
                metadata={"error": "empty_universe"},
            )

        # Fetch data and compute signals
        scores: dict[str, float] = {}
        target_weights: dict[str, float] = {}
        lookback_bars = max(trend_period, period) + 50  # Extra bars for filters

        for symbol in universe:
            try:
                bars = exchange.get_ohlcv(symbol, timeframe, lookback_bars)
                if bars is None or len(bars) < period + 20:
                    logger.warning(f"{symbol}: insufficient data (need {period + 20} bars)")
                    scores[symbol] = 0.0
                    continue

                closes = bars["close"].dropna()
                if len(closes) < period + 20:
                    logger.warning(f"{symbol}: not enough closes after dropna")
                    scores[symbol] = 0.0
                    continue

                # Compute RSI
                rsi = _compute_rsi(closes, period)
                current_rsi = float(rsi.iloc[-1])

                # Current price and ATR
                current_price = float(closes.iloc[-1])
                atr = _compute_atr(bars, 14)
                current_atr = float(atr.iloc[-1])

                # Trend filter: check if price > 200-period EMA
                trend_passes = True
                if use_trend_filter and len(closes) >= trend_period:
                    ema_200 = closes.ewm(span=trend_period, adjust=False).mean()
                    ema_200_val = float(ema_200.iloc[-1])
                    trend_passes = current_price > ema_200_val
                    logger.debug(
                        f"{symbol}: price={current_price:.2f}, EMA200={ema_200_val:.2f}, "
                        f"trend_passes={trend_passes}"
                    )

                # Volume filter: check if recent volume > 20-period average volume
                volume_passes = True
                if use_volume_filter:
                    volumes = bars["volume"].dropna()
                    if len(volumes) >= 21:
                        avg_vol = float(volumes.iloc[-21:-1].mean())
                        current_vol = float(volumes.iloc[-1])
                        volume_passes = current_vol > avg_vol * 0.5  # At least 50% of average
                        logger.debug(
                            f"{symbol}: current_vol={current_vol:.0f}, avg_vol={avg_vol:.0f}, "
                            f"volume_passes={volume_passes}"
                        )

                # Entry signal: RSI < oversold AND filters pass
                buy_signal = (current_rsi < oversold) and trend_passes and volume_passes
                exit_signal = current_rsi > overbought

                # Position sizing based on ATR and risk
                if buy_signal:
                    if current_atr > 0 and current_price > 0:
                        # risk_amount = account_value * risk_pct
                        # position_size = risk_amount / (2 * ATR)
                        # weight = position_size * price / account_value
                        # Simplified: weight scales with risk_pct / atr_pct
                        atr_pct = current_atr / current_price
                        weight_scalar = risk_pct / max(atr_pct, 0.01)
                        weight = min(weight_scalar, 1.0)  # Cap at 100%
                    else:
                        weight = 0.0
                else:
                    weight = 0.0

                scores[symbol] = float(100 - current_rsi)  # Raw score (inverted RSI)
                if buy_signal:
                    target_weights[symbol] = round(weight, 6)

                logger.debug(
                    f"{symbol}: RSI={current_rsi:.1f}, buy_signal={buy_signal}, "
                    f"weight={weight:.2%}"
                )

            except Exception as e:
                logger.error(f"Error computing RSI for {symbol}: {e}")
                scores[symbol] = 0.0
                continue

        # Normalize weights if there are positions
        total_weight = sum(target_weights.values())
        if total_weight > 0 and total_weight > 1.0:
            # Normalize to sum <= 1.0
            target_weights = {
                sym: round(weight / total_weight, 6)
                for sym, weight in target_weights.items()
            }

        logger.info(f"RSI signals: {len(target_weights)} buy signals, total_weight={total_weight:.2%}")

        return StrategySignal(
            strategy_name=self.name,
            timestamp=timestamp,
            target_weights=target_weights,
            scores=scores,
            metadata={
                "rsi_period": period,
                "oversold": oversold,
                "overbought": overbought,
                "timeframe": timeframe,
                "trend_filter": use_trend_filter,
                "volume_filter": use_volume_filter,
                "buy_count": len(target_weights),
            },
        )
