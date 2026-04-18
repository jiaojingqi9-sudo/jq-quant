import pandas as pd

from taa_futu.config import Settings
from taa_futu.fusion_intraday import (
    build_target_weights,
    compute_benchmark_score,
    compute_symbol_feature,
)


def make_settings() -> Settings:
    return Settings(
        symbols=("US.SPY",),
        benchmark="US.SPY",
        start_date="2005-01-01",
        lookback_months=10,
        signal_timezone="America/New_York",
        fusion_universe=("US.SPY", "US.QQQ"),
        fusion_benchmark="US.SPY",
        fusion_lookback_bars=60,
        fusion_opening_range_minutes=15,
        fusion_top_k=2,
        fusion_entry_score=0.35,
        fusion_exit_score=0.20,
        fusion_max_position_weight=0.35,
        fusion_max_gross_exposure=0.90,
        fusion_min_rel_volume=1.10,
        fusion_max_spread_bps=15,
        fusion_order_book_depth=3,
        fusion_tick_window=50,
        ofim_universe=("US.AAPL",),
        ofim_benchmark="US.QQQ",
        ofim_lookback_bars=60,
        ofim_depth_tiers=((1, 5), (6, 20), (21, 60)),
        ofim_entry_threshold=0.20,
        ofim_exit_threshold=0.05,
        ofim_max_score=0.60,
        ofim_min_vol_acceleration=1.20,
        ofim_max_spread_bps=15.0,
        ofim_tick_window=100,
        ofim_order_book_depth=60,
        ofim_max_position_weight=0.15,
        ofim_max_gross_exposure=0.80,
        ofim_max_positions=5,
        ofim_crypto_universe=(),
        ofim_crypto_to_proxy=(),
        ofim_crypto_exchange="binance",
        ofim_crypto_api_key=None,
        ofim_crypto_api_secret=None,
        ofim_crypto_sandbox=False,
        stack_ofim_weight=0.0,
        futu_host="127.0.0.1",
        futu_port=11111,
        futu_trd_market="US",
        futu_trd_env="SIMULATE",
        futu_acc_id=None,
        futu_enable_real_trading=False,
        futu_allow_auto_real=False,
        futu_unlock_trade_password_md5=None,
        futu_price_buffer_bps=10,
        futu_fill_outside_rth=False,
        futu_api_retry_attempts=4,
        futu_api_retry_backoff_seconds=0.0,
        auto_trader_poll_seconds=60,
        auto_trader_market_timezone="America/New_York",
        auto_trader_start_time="09:45",
        auto_trader_end_time="15:55",
        auto_trader_order_cooldown_seconds=300,
    )


def make_bars(trend: float, final_volume_multiplier: float = 1.0) -> pd.DataFrame:
    rows = []
    base = 100.0
    for idx in range(60):
        price = base + trend * idx
        rows.append(
            {
                "close": price,
                "high": price + 0.15,
                "low": price - 0.15,
                "volume": (1000 + idx * 10) * (final_volume_multiplier if idx == 59 else 1.0),
            }
        )
    return pd.DataFrame(rows)


def make_snapshot(last_price: float, prev_close: float, spread: float = 0.01) -> pd.Series:
    return pd.Series(
        {
            "last_price": last_price,
            "prev_close_price": prev_close,
            "price_spread": spread,
            "bid_vol": 1200,
            "ask_vol": 900,
        }
    )


def make_ticks(direction: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker_direction": [direction] * 20,
            "volume": [100] * 20,
        }
    )


def test_benchmark_score_positive_in_uptrend() -> None:
    settings = make_settings()
    score = compute_benchmark_score(
        make_bars(0.2),
        make_snapshot(112.0, 109.0),
        {"Bid": [(1, 1500, 1)], "Ask": [(1, 700, 1)]},
        make_ticks("BUY"),
        settings,
    )
    assert score > 0


def test_symbol_feature_detects_good_setup() -> None:
    settings = make_settings()
    feature = compute_symbol_feature(
        code="US.QQQ",
        bars=make_bars(0.25, final_volume_multiplier=2.0),
        snapshot=make_snapshot(114.0, 110.0),
        order_book={"Bid": [(1, 1800, 1)], "Ask": [(1, 700, 1)]},
        ticks=make_ticks("BUY"),
        benchmark_score=0.6,
        settings=settings,
    )
    assert feature.score > settings.fusion_entry_score
    assert feature.eligible is True


def test_build_target_weights_caps_single_name() -> None:
    settings = make_settings()
    features = [
        compute_symbol_feature(
            code="US.SPY",
            bars=make_bars(0.2, final_volume_multiplier=2.0),
            snapshot=make_snapshot(112.0, 109.0),
            order_book={"Bid": [(1, 1500, 1)], "Ask": [(1, 700, 1)]},
            ticks=make_ticks("BUY"),
            benchmark_score=0.6,
            settings=settings,
        ),
        compute_symbol_feature(
            code="US.QQQ",
            bars=make_bars(0.22, final_volume_multiplier=2.2),
            snapshot=make_snapshot(114.0, 110.0),
            order_book={"Bid": [(1, 1600, 1)], "Ask": [(1, 800, 1)]},
            ticks=make_ticks("BUY"),
            benchmark_score=0.6,
            settings=settings,
        ),
    ]
    exposure, weights = build_target_weights(features, 0.6, set(), settings)
    assert exposure > 0
    assert sum(weights.values()) <= settings.fusion_max_gross_exposure + 1e-9
    assert all(weight <= settings.fusion_max_position_weight + 1e-9 for weight in weights.values())
