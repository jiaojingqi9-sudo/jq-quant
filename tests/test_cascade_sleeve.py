from pathlib import Path

import pandas as pd

from taa_futu.cascade_sleeve import cascade_trade_symbols, generate_replay_cascade_plan
from taa_futu.config import Settings
from taa_futu.market_data import futu_code_to_yfinance


def _settings(env_file: Path) -> Settings:
    return Settings(
        symbols=("US.SPY", "US.EFA", "US.IEF", "US.VNQ", "US.DBC"),
        benchmark="US.SPY",
        start_date="2025-01-01",
        lookback_months=10,
        signal_timezone="America/New_York",
        fusion_universe=("US.SPY", "US.QQQ"),
        fusion_benchmark="US.SPY",
        fusion_lookback_bars=20,
        fusion_opening_range_minutes=15,
        fusion_top_k=2,
        fusion_entry_score=0.35,
        fusion_exit_score=0.20,
        fusion_max_position_weight=0.35,
        fusion_max_gross_exposure=0.90,
        fusion_min_rel_volume=1.10,
        fusion_max_spread_bps=15.0,
        fusion_order_book_depth=3,
        fusion_tick_window=50,
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
        cascade_env_file=str(env_file),
    )


def _daily_frame(start: str, periods: int, base: float, step: float) -> pd.DataFrame:
    dates = pd.date_range(start, periods=periods, freq="B")
    closes = [base + step * idx for idx in range(periods)]
    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": closes,
            "high": [value * 1.002 for value in closes],
            "low": [value * 0.998 for value in closes],
            "close": closes,
            "volume": [1_000_000] * periods,
        }
    )


def test_cascade_trade_symbols_include_required_support_series(tmp_path: Path) -> None:
    env_file = tmp_path / "cascade.env"
    env_file.write_text(
        "\n".join(
            [
                "DM_UNIVERSE=US.SPY,US.EFA,US.AGG,US.GLD,BTC/USDT,ETH/USDT",
                "DM_USE_RISK_FREE=US.AGG",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    symbols = cascade_trade_symbols(_settings(env_file))

    assert "US.SPY" in symbols
    assert "US.GLD" in symbols
    assert "US.VIX" in symbols
    assert "BTC/USDT" not in symbols


def test_vix_symbol_uses_yfinance_fallback_mapping() -> None:
    assert futu_code_to_yfinance("US.VIX") == "^VIX"


def test_generate_replay_cascade_plan_returns_futu_tradable_weights(tmp_path: Path) -> None:
    env_file = tmp_path / "cascade.env"
    env_file.write_text(
        "\n".join(
            [
                "DM_UNIVERSE=US.SPY,US.EFA,US.AGG,US.GLD,BTC/USDT,ETH/USDT",
                "MAX_POSITION_PCT=0.30",
                "TARGET_ANNUAL_VOL=0.10",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    settings = _settings(env_file)
    frames = {
        "US.SPY": _daily_frame("2025-01-01", 140, 100.0, 0.35),
        "US.EFA": _daily_frame("2025-01-01", 140, 80.0, 0.25),
        "US.AGG": _daily_frame("2025-01-01", 140, 90.0, 0.08),
        "US.GLD": _daily_frame("2025-01-01", 140, 70.0, 0.12),
        "US.VIX": _daily_frame("2025-01-01", 140, 18.0, 0.01),
    }

    plan = generate_replay_cascade_plan(frames, settings, as_of=pd.Timestamp("2025-07-31"))

    assert plan.target_weights
    assert all(symbol.startswith("US.") for symbol in plan.target_weights)
    assert plan.total_exposure > 0
    assert plan.regime_label in {"CRISIS", "CAUTIOUS", "NEUTRAL", "BULLISH", "EUPHORIA"}
