from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .base import BaseStrategy, DataContext, StrategySignal
from . import StrategyRegistry

logger = logging.getLogger(__name__)


@StrategyRegistry.register("dual_momentum")
class DualMomentumStrategy(BaseStrategy):
    """Dual Momentum strategy based on Gary Antonacci's research.

    This flagship strategy combines:
    - **Relative Momentum**: Rank assets by trailing returns; invest in top performers.
    - **Absolute Momentum**: Only invest if trailing return exceeds risk-free rate.
    - **Volatility Scaling**: Scale positions inversely by realized volatility.

    Reference: Antonacci, G. (2014). "Dual Momentum Investing".

    The strategy operates monthly on a fixed rebalance day (e.g., 1st of month).
    """

    #: Registry key.
    name = "dual_momentum"

    def compute_signal(self, ctx: DataContext) -> StrategySignal:
        """Compute dual momentum signal using ``ctx.primary`` exchange."""
        exchange = ctx.primary
        timestamp = ctx.timestamp
        universe = self.settings.dm_universe
        lookback_months = self.settings.dm_lookback_months
        skip_last_month = True  # Avoid reversal effect
        target_annual_vol = self.settings.target_annual_vol

        logger.info(
            f"Computing dual momentum signal for {len(universe)} symbols, "
            f"lookback={lookback_months}m, target_vol={target_annual_vol:.1%}"
        )

        if not universe:
            logger.warning("Empty universe for dual momentum")
            return StrategySignal(
                strategy_name=self.name,
                timestamp=timestamp,
                target_weights={},
                scores={},
                metadata={"error": "empty_universe"},
            )

        if len(universe) < 2:
            logger.warning(
                "Dual momentum universe has only %d symbol(s); need at least 2 "
                "for meaningful relative momentum ranking.",
                len(universe),
            )

        # Fetch daily OHLCV for ~370 days to compute monthly returns
        lookback_days = int(lookback_months * 30.5) + 30
        prices_by_symbol: dict[str, pd.DataFrame] = {}
        trailing_returns: dict[str, float] = {}
        realized_vols: dict[str, float] = {}

        for symbol in universe:
            try:
                bars = exchange.get_ohlcv(symbol, "1d", lookback_days)
                if bars is None or len(bars) == 0:
                    logger.warning(f"No data for {symbol}")
                    continue

                prices_by_symbol[symbol] = bars
                closes = bars["close"].dropna()

                if len(closes) < lookback_months + 2:
                    logger.warning(
                        f"{symbol}: only {len(closes)} days available, "
                        f"need {lookback_months + 2} for {lookback_months}m lookback"
                    )
                    continue

                # Compute monthly closes and trailing return
                monthly_closes = closes.resample("ME").last()
                if len(monthly_closes) < lookback_months + 1:
                    logger.warning(f"{symbol}: only {len(monthly_closes)} months available")
                    continue

                # Skip the last incomplete month if requested
                if skip_last_month:
                    monthly_closes = monthly_closes[:-1]

                if len(monthly_closes) < lookback_months + 1:
                    logger.warning(f"{symbol}: not enough completed months after skip")
                    continue

                price_now = float(monthly_closes.iloc[-1])
                price_lookback = float(monthly_closes.iloc[-(lookback_months + 1)])

                if price_lookback <= 0 or price_now <= 0:
                    logger.warning(f"{symbol}: invalid prices (<=0)")
                    continue

                trailing_return = (price_now / price_lookback) - 1.0
                trailing_returns[symbol] = trailing_return

                # Compute realized volatility from daily returns
                daily_returns = closes.pct_change().dropna()
                if len(daily_returns) > 0:
                    realized_vol = float(daily_returns.std() * np.sqrt(252))
                    realized_vols[symbol] = max(realized_vol, 0.01)  # Floor at 1%
                else:
                    realized_vols[symbol] = 0.10  # Default

                logger.debug(
                    f"{symbol}: trailing_return={trailing_return:.2%}, "
                    f"realized_vol={realized_vols[symbol]:.2%}"
                )

            except Exception as e:
                logger.error(f"Error fetching data for {symbol}: {e}")
                continue

        if not trailing_returns:
            logger.warning("No valid symbols with trailing returns")
            return StrategySignal(
                strategy_name=self.name,
                timestamp=timestamp,
                target_weights={},
                scores={},
                metadata={"error": "no_valid_data"},
            )

        # Relative momentum: rank by trailing return.
        # top_k is configurable; default 2 (top 2 assets) but capped to available
        # symbols so a small universe never causes an empty selection.
        relative_momentum_count = max(1, min(
            getattr(self.settings, "dm_top_k", 2),
            len(trailing_returns),
        ))
        ranked = sorted(trailing_returns.items(), key=lambda x: x[1], reverse=True)
        relative_momentum_symbols = [sym for sym, _ in ranked[:relative_momentum_count]]

        logger.info(
            f"Relative momentum ranking (top {relative_momentum_count}): "
            f"{relative_momentum_symbols}"
        )

        # Absolute momentum filter: check against risk-free rate proxy
        risk_free_symbol = self.settings.dm_use_risk_free
        absolute_threshold = self.settings.dm_absolute_threshold

        if risk_free_symbol and risk_free_symbol in trailing_returns:
            absolute_threshold = trailing_returns[risk_free_symbol]
            logger.debug(f"Using {risk_free_symbol} return ({absolute_threshold:.2%}) as threshold")

        filtered_symbols = [
            sym for sym in relative_momentum_symbols
            if trailing_returns[sym] > absolute_threshold
        ]

        logger.info(
            f"After absolute momentum filter (>{absolute_threshold:.2%}): "
            f"{filtered_symbols}"
        )

        # If no symbol passes absolute momentum, go to cash/bonds
        if not filtered_symbols:
            logger.info("No symbols pass absolute momentum filter, returning 0 weights")
            return StrategySignal(
                strategy_name=self.name,
                timestamp=timestamp,
                target_weights={},
                scores={sym: trailing_returns[sym] for sym in relative_momentum_symbols},
                metadata={
                    "lookback_months": lookback_months,
                    "relative_momentum_symbols": relative_momentum_symbols,
                    "absolute_threshold": absolute_threshold,
                    "filtered_count": 0,
                },
            )

        # Volatility weighting: scale inversely by realized vol
        vol_weights: dict[str, float] = {}
        for symbol in filtered_symbols:
            vol = realized_vols.get(symbol, 0.10)
            scalar = target_annual_vol / vol
            scalar = min(scalar, 2.0)  # Cap at 2x leverage
            vol_weights[symbol] = scalar
            logger.debug(f"{symbol}: vol_scalar={scalar:.2f} (vol={vol:.2%})")

        # Normalize weights
        total_weight = sum(vol_weights.values())
        if total_weight <= 0:
            logger.warning("Zero total vol weight")
            return StrategySignal(
                strategy_name=self.name,
                timestamp=timestamp,
                target_weights={},
                scores={sym: trailing_returns[sym] for sym in filtered_symbols},
                metadata={"error": "zero_total_weight"},
            )

        target_weights = {
            sym: round(weight / total_weight, 6)
            for sym, weight in vol_weights.items()
        }

        logger.info(f"Final target weights: {target_weights}")

        return StrategySignal(
            strategy_name=self.name,
            timestamp=timestamp,
            target_weights=target_weights,
            scores={sym: trailing_returns[sym] for sym in universe},
            metadata={
                "lookback_months": lookback_months,
                "relative_momentum_symbols": relative_momentum_symbols,
                "absolute_threshold": absolute_threshold,
                "filtered_count": len(filtered_symbols),
                "realized_vols": realized_vols,
            },
        )
