import pandas as pd

from taa_futu.ofim_intraday import (
    _compute_atr_pct,
    _compute_benchmark_score,
    compute_micro_momentum,
    compute_volume_acceleration,
    compute_vwap_deviation,
)


def test_ofim_bar_features_tolerate_duplicate_ohlcv_columns() -> None:
    rows = []
    for idx in range(35):
        close = 100.0 + idx * 0.1
        rows.append([close, close + 50.0, 1_000.0 + idx, 1.0, close + 0.5, close - 0.5])
    bars = pd.DataFrame(rows, columns=["close", "close", "volume", "volume", "high", "low"])

    assert compute_volume_acceleration(bars) > 0
    assert set(compute_micro_momentum(bars)) == {"mom_3m", "mom_10m", "mom_30m"}
    assert abs(compute_vwap_deviation(bars)) < 1
    assert _compute_atr_pct(bars) > 0

    score = _compute_benchmark_score(
        bars,
        pd.Series({"last_price": 103.4}),
        {"Bid": [(103.3, 100)], "Ask": [(103.5, 80)]},
    )
    assert -1 <= score <= 1
