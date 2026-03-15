from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SignalSnapshot:
    signal_month: pd.Timestamp
    reference_date: date
    weights: dict[str, float]


def monthly_closes(daily_closes: pd.DataFrame) -> pd.DataFrame:
    return daily_closes.sort_index().resample("ME").last().dropna(how="all")


def compute_target_weights(monthly_prices: pd.DataFrame, lookback_months: int) -> pd.DataFrame:
    moving_average = monthly_prices.rolling(lookback_months).mean()
    active = (monthly_prices > moving_average).where(moving_average.notna(), False)
    active = active.astype(float)
    denominator = active.sum(axis=1).replace(0, np.nan)
    weights = active.div(denominator, axis=0).fillna(0.0)
    return weights


def latest_completed_signal(
    daily_closes: pd.DataFrame,
    lookback_months: int,
    reference_date: date,
) -> SignalSnapshot:
    monthly_prices = monthly_closes(daily_closes)
    if monthly_prices.empty:
        raise ValueError("Not enough price history to compute signals.")

    latest_daily = daily_closes.dropna(how="all").index.max()
    if latest_daily is pd.NaT:
        raise ValueError("Daily prices are empty.")

    if latest_daily.to_period("M") == pd.Period(reference_date, freq="M"):
        monthly_prices = monthly_prices.iloc[:-1]

    if len(monthly_prices) < lookback_months:
        raise ValueError("Not enough completed monthly bars to compute the moving average.")

    weights = compute_target_weights(monthly_prices, lookback_months)
    latest_row = weights.iloc[-1]
    latest_weights = {
        symbol: round(float(weight), 6)
        for symbol, weight in latest_row.items()
        if weight > 0
    }
    return SignalSnapshot(
        signal_month=monthly_prices.index[-1],
        reference_date=reference_date,
        weights=latest_weights,
    )

