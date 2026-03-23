from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv

DEFAULT_SYMBOLS = ("US.SPY", "US.EFA", "US.IEF", "US.VNQ", "US.DBC")


def _parse_symbols(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return DEFAULT_SYMBOLS
    symbols = tuple(part.strip() for part in raw.split(",") if part.strip())
    return symbols or DEFAULT_SYMBOLS


def _parse_optional_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    return int(raw)


def _parse_optional_str(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    symbols: tuple[str, ...]
    benchmark: str
    start_date: str
    lookback_months: int
    signal_timezone: str
    fusion_universe: tuple[str, ...]
    fusion_benchmark: str
    fusion_lookback_bars: int
    fusion_opening_range_minutes: int
    fusion_top_k: int
    fusion_entry_score: float
    fusion_exit_score: float
    fusion_max_position_weight: float
    fusion_max_gross_exposure: float
    fusion_min_rel_volume: float
    fusion_max_spread_bps: float
    fusion_order_book_depth: int
    fusion_tick_window: int
    ofim_universe: tuple[str, ...]
    ofim_benchmark: str
    ofim_lookback_bars: int
    ofim_depth_tiers: tuple[tuple[int, int], ...]
    ofim_entry_threshold: float
    ofim_exit_threshold: float
    ofim_max_score: float
    ofim_min_vol_acceleration: float
    ofim_max_spread_bps: float
    ofim_tick_window: int
    ofim_order_book_depth: int
    ofim_max_position_weight: float
    ofim_max_gross_exposure: float
    ofim_max_positions: int
    futu_host: str
    futu_port: int
    futu_trd_market: str
    futu_trd_env: str
    futu_acc_id: int | None
    futu_enable_real_trading: bool
    futu_allow_auto_real: bool
    futu_unlock_trade_password_md5: str | None
    futu_price_buffer_bps: int
    futu_fill_outside_rth: bool
    futu_api_retry_attempts: int
    futu_api_retry_backoff_seconds: float
    auto_trader_poll_seconds: int
    auto_trader_market_timezone: str
    auto_trader_start_time: str
    auto_trader_end_time: str
    auto_trader_order_cooldown_seconds: int
    auto_trader_order_stale_minutes: int = 5
    auto_trader_min_order_value_usd: float = 500.0
    auto_trader_min_hold_minutes: int = 10
    watchdog_min_interval_seconds: int = 240
    watchdog_max_interval_seconds: int = 540
    watchdog_outside_window_min_interval_seconds: int = 900
    watchdog_outside_window_max_interval_seconds: int = 1800
    watchdog_stale_status_seconds: int = 240
    watchdog_restart_cooldown_seconds: int = 120
    initial_capital: float = 1_000_000.0
    stack_baseline_enabled: bool = False
    stack_baseline_weight: float = 0.55
    stack_fusion_weight: float = 0.90
    stack_ofim_weight: float = 0.0
    stack_cascade_weight: float = 0.0
    stack_isolate_baseline_symbols: bool = True
    cascade_env_file: str = "claude-trade/.env"
    trade_costs_enabled: bool = False
    trade_cost_profile: str = "futu_hk_us_fixed"
    trade_cost_commission_per_share: float = 0.0049
    trade_cost_commission_min: float = 0.99
    trade_cost_commission_max_pct: float = 0.005
    trade_cost_platform_per_share: float = 0.005
    trade_cost_platform_min: float = 1.0
    trade_cost_platform_max_pct: float = 0.005
    trade_cost_settlement_per_share: float = 0.003
    trade_cost_settlement_min: float = 0.01
    trade_cost_settlement_max_pct: float = 0.01
    trade_cost_sec_sell_rate: float = 0.0000278
    trade_cost_sec_sell_min: float = 0.01
    trade_cost_sec_zero_from: str = "2025-05-14"
    trade_cost_taf_sell_per_share: float = 0.000166
    trade_cost_taf_sell_min: float = 0.01
    trade_cost_taf_sell_max: float = 8.30


def load_settings(env_file: str | Path = ".env") -> Settings:
    # The local .env file is the source of truth for this desktop app.
    # Use override=True so runtime processes pick up the latest values after UI changes.
    load_dotenv(dotenv_path=env_file, override=True)
    ofim_depth_raw = os.getenv("OFIM_DEPTH_TIERS", "1-5,6-20,21-60")
    ofim_depth_tiers: tuple[tuple[int, int], ...] = tuple(
        (
            int(part.split("-")[0]),
            int(part.split("-")[1]),
        )
        for part in [item.strip() for item in ofim_depth_raw.split(",") if item.strip()]
        if "-" in part
    )
    if not ofim_depth_tiers:
        ofim_depth_tiers = ((1, 5), (6, 20), (21, 60))

    return Settings(
        symbols=_parse_symbols(os.getenv("TAA_SYMBOLS")),
        benchmark=os.getenv("TAA_BENCHMARK", "US.SPY"),
        start_date=os.getenv("TAA_START_DATE", "2005-01-01"),
        lookback_months=int(os.getenv("TAA_LOOKBACK_MONTHS", "10")),
        signal_timezone=os.getenv("TAA_SIGNAL_TIMEZONE", "America/New_York"),
        fusion_universe=_parse_symbols(os.getenv("FUSION_UNIVERSE")),
        fusion_benchmark=os.getenv("FUSION_BENCHMARK", "US.SPY"),
        fusion_lookback_bars=int(os.getenv("FUSION_LOOKBACK_BARS", "60")),
        fusion_opening_range_minutes=int(os.getenv("FUSION_OPENING_RANGE_MINUTES", "15")),
        fusion_top_k=int(os.getenv("FUSION_TOP_K", "3")),
        fusion_entry_score=float(os.getenv("FUSION_ENTRY_SCORE", "0.35")),
        fusion_exit_score=float(os.getenv("FUSION_EXIT_SCORE", "0.20")),
        fusion_max_position_weight=float(os.getenv("FUSION_MAX_POSITION_WEIGHT", "0.35")),
        fusion_max_gross_exposure=float(os.getenv("FUSION_MAX_GROSS_EXPOSURE", "0.90")),
        fusion_min_rel_volume=float(os.getenv("FUSION_MIN_REL_VOLUME", "1.10")),
        fusion_max_spread_bps=float(os.getenv("FUSION_MAX_SPREAD_BPS", "15")),
        fusion_order_book_depth=int(os.getenv("FUSION_ORDER_BOOK_DEPTH", "3")),
        fusion_tick_window=int(os.getenv("FUSION_TICK_WINDOW", "50")),
        ofim_universe=_parse_symbols(os.getenv("OFIM_UNIVERSE")),
        ofim_benchmark=os.getenv("OFIM_BENCHMARK", "US.QQQ"),
        ofim_lookback_bars=int(os.getenv("OFIM_LOOKBACK_BARS", "60")),
        ofim_depth_tiers=ofim_depth_tiers,
        ofim_entry_threshold=float(os.getenv("OFIM_ENTRY_THRESHOLD", "0.20")),
        ofim_exit_threshold=float(os.getenv("OFIM_EXIT_THRESHOLD", "0.05")),
        ofim_max_score=float(os.getenv("OFIM_MAX_SCORE", "0.60")),
        ofim_min_vol_acceleration=float(os.getenv("OFIM_MIN_VOL_ACCELERATION", "1.20")),
        ofim_max_spread_bps=float(os.getenv("OFIM_MAX_SPREAD_BPS", "15")),
        ofim_tick_window=int(os.getenv("OFIM_TICK_WINDOW", "100")),
        ofim_order_book_depth=int(os.getenv("OFIM_ORDER_BOOK_DEPTH", "60")),
        ofim_max_position_weight=float(os.getenv("OFIM_MAX_POSITION_WEIGHT", "0.15")),
        ofim_max_gross_exposure=float(os.getenv("OFIM_MAX_GROSS_EXPOSURE", "0.80")),
        ofim_max_positions=int(os.getenv("OFIM_MAX_POSITIONS", "5")),
        futu_host=os.getenv("FUTU_HOST", "127.0.0.1"),
        futu_port=int(os.getenv("FUTU_PORT", "11111")),
        futu_trd_market=os.getenv("FUTU_TRD_MARKET", "US"),
        futu_trd_env=os.getenv("FUTU_TRD_ENV", "SIMULATE"),
        futu_acc_id=_parse_optional_int(os.getenv("FUTU_ACC_ID")),
        futu_enable_real_trading=_parse_bool(os.getenv("FUTU_ENABLE_REAL_TRADING"), default=False),
        futu_allow_auto_real=_parse_bool(os.getenv("FUTU_ALLOW_AUTO_REAL"), default=False),
        futu_unlock_trade_password_md5=_parse_optional_str(os.getenv("FUTU_UNLOCK_TRADE_PASSWORD_MD5")),
        futu_price_buffer_bps=int(os.getenv("FUTU_PRICE_BUFFER_BPS", "10")),
        futu_fill_outside_rth=_parse_bool(os.getenv("FUTU_FILL_OUTSIDE_RTH"), default=False),
        futu_api_retry_attempts=int(os.getenv("FUTU_API_RETRY_ATTEMPTS", "4")),
        futu_api_retry_backoff_seconds=float(os.getenv("FUTU_API_RETRY_BACKOFF_SECONDS", "1.0")),
        auto_trader_poll_seconds=int(os.getenv("AUTO_TRADER_POLL_SECONDS", "60")),
        auto_trader_market_timezone=os.getenv("AUTO_TRADER_MARKET_TIMEZONE", "America/New_York"),
        auto_trader_start_time=os.getenv("AUTO_TRADER_START_TIME", "09:45"),
        auto_trader_end_time=os.getenv("AUTO_TRADER_END_TIME", "15:55"),
        auto_trader_order_cooldown_seconds=int(os.getenv("AUTO_TRADER_ORDER_COOLDOWN_SECONDS", "300")),
        auto_trader_order_stale_minutes=int(os.getenv("AUTO_TRADER_ORDER_STALE_MINUTES", "5")),
        auto_trader_min_order_value_usd=float(os.getenv("AUTO_TRADER_MIN_ORDER_VALUE_USD", "500.0")),
        auto_trader_min_hold_minutes=int(os.getenv("AUTO_TRADER_MIN_HOLD_MINUTES", "10")),
        watchdog_min_interval_seconds=int(os.getenv("WATCHDOG_MIN_INTERVAL_SECONDS", "240")),
        watchdog_max_interval_seconds=int(os.getenv("WATCHDOG_MAX_INTERVAL_SECONDS", "540")),
        watchdog_outside_window_min_interval_seconds=int(os.getenv("WATCHDOG_OUTSIDE_WINDOW_MIN_INTERVAL_SECONDS", "900")),
        watchdog_outside_window_max_interval_seconds=int(os.getenv("WATCHDOG_OUTSIDE_WINDOW_MAX_INTERVAL_SECONDS", "1800")),
        watchdog_stale_status_seconds=int(os.getenv("WATCHDOG_STALE_STATUS_SECONDS", "240")),
        watchdog_restart_cooldown_seconds=int(os.getenv("WATCHDOG_RESTART_COOLDOWN_SECONDS", "120")),
        initial_capital=float(os.getenv("INITIAL_CAPITAL", "1000000.0")),
        stack_baseline_enabled=_parse_bool(os.getenv("STACK_BASELINE_ENABLED"), default=False),
        stack_baseline_weight=float(os.getenv("STACK_BASELINE_WEIGHT", "0.55")),
        stack_fusion_weight=float(os.getenv("STACK_FUSION_WEIGHT", "0.90")),
        stack_ofim_weight=float(os.getenv("STACK_OFIM_WEIGHT", "0.00")),
        stack_cascade_weight=float(os.getenv("STACK_CASCADE_WEIGHT", "0.00")),
        stack_isolate_baseline_symbols=_parse_bool(os.getenv("STACK_ISOLATE_BASELINE_SYMBOLS"), default=True),
        cascade_env_file=os.getenv("CASCADE_ENV_FILE", "claude-trade/.env"),
        trade_costs_enabled=_parse_bool(os.getenv("TRADE_COSTS_ENABLED"), default=True),
        trade_cost_profile=os.getenv("TRADE_COST_PROFILE", "futu_hk_us_fixed"),
        trade_cost_commission_per_share=float(os.getenv("TRADE_COST_COMMISSION_PER_SHARE", "0.0049")),
        trade_cost_commission_min=float(os.getenv("TRADE_COST_COMMISSION_MIN", "0.99")),
        trade_cost_commission_max_pct=float(os.getenv("TRADE_COST_COMMISSION_MAX_PCT", "0.005")),
        trade_cost_platform_per_share=float(os.getenv("TRADE_COST_PLATFORM_PER_SHARE", "0.005")),
        trade_cost_platform_min=float(os.getenv("TRADE_COST_PLATFORM_MIN", "1.00")),
        trade_cost_platform_max_pct=float(os.getenv("TRADE_COST_PLATFORM_MAX_PCT", "0.005")),
        trade_cost_settlement_per_share=float(os.getenv("TRADE_COST_SETTLEMENT_PER_SHARE", "0.003")),
        trade_cost_settlement_min=float(os.getenv("TRADE_COST_SETTLEMENT_MIN", "0.01")),
        trade_cost_settlement_max_pct=float(os.getenv("TRADE_COST_SETTLEMENT_MAX_PCT", "0.01")),
        trade_cost_sec_sell_rate=float(os.getenv("TRADE_COST_SEC_SELL_RATE", "0.0000278")),
        trade_cost_sec_sell_min=float(os.getenv("TRADE_COST_SEC_SELL_MIN", "0.01")),
        trade_cost_sec_zero_from=os.getenv("TRADE_COST_SEC_ZERO_FROM", "2025-05-14"),
        trade_cost_taf_sell_per_share=float(os.getenv("TRADE_COST_TAF_SELL_PER_SHARE", "0.000166")),
        trade_cost_taf_sell_min=float(os.getenv("TRADE_COST_TAF_SELL_MIN", "0.01")),
        trade_cost_taf_sell_max=float(os.getenv("TRADE_COST_TAF_SELL_MAX", "8.30")),
    )
