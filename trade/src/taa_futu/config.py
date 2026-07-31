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


def _parse_symbols_optional(raw: str | None) -> tuple[str, ...]:
    """Like _parse_symbols but returns an empty tuple when the env var is unset."""
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_crypto_proxy_map(raw: str | None) -> tuple[tuple[str, str], ...]:
    """Parse OFIM_CRYPTO_TO_PROXY='BTC/USDT:US.IBIT,ETH/USDT:US.ETHA' into a tuple of pairs."""
    if not raw:
        return ()
    pairs: list[tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if ":" not in item:
            continue
        crypto, proxy = item.split(":", 1)
        crypto = crypto.strip()
        proxy = proxy.strip()
        if crypto and proxy:
            pairs.append((crypto, proxy))
    return tuple(pairs)


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
    # ── OFIM crypto extension ─────────────────────────────────────────────────
    # Direct crypto symbols scored by OFIM using Binance LOB data.
    # Empty tuple = feature disabled (default).
    ofim_crypto_universe: tuple[str, ...]
    # Maps crypto symbols → Futu-tradeable proxy ETFs for execution.
    # Format: (("BTC/USDT", "US.IBIT"), ("ETH/USDT", "US.ETHA"), ...)
    ofim_crypto_to_proxy: tuple[tuple[str, str], ...]
    # Binance (or other ccxt-compatible) exchange credentials for crypto LOB data.
    ofim_crypto_exchange: str
    ofim_crypto_api_key: str | None
    ofim_crypto_api_secret: str | None
    ofim_crypto_sandbox: bool
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
    auto_trader_exit_confirm_cycles: int = 1
    # 每次同步成交时往回查几天。1 天会让跨周末的漏记永久丢失（周五收盘前的
    # 成交，周一查 [周日, 周一] 根本扫不到）。去重按 order_id 做，回溯多几天
    # 只是多扫一遍，不会重复入账。
    auto_trader_fill_lookback_days: int = 7
    auto_trader_min_symbol_interval_seconds: int = 0
    auto_trader_max_target_gross_exposure: float = 1.0
    auto_trader_max_target_weight: float = 1.0
    auto_trader_max_order_value_usd: float = 0.0
    auto_trader_max_cycle_turnover_usd: float = 0.0
    auto_trader_max_epoch_loss_usd: float = 0.0
    auto_trader_max_epoch_loss_pct: float = 0.0
    auto_trader_order_stale_minutes: int = 5
    auto_trader_min_order_value_usd: float = 500.0
    auto_trader_min_hold_minutes: int = 10
    auto_trader_rebalance_drift_pct: float = 1.0
    # 连续 N 次 transient_error 后短路：跳过下一个 cycle 的 submit 路径，
    # 避免 OpenD 半死（socket 在但 API connection closed）时盲发订单。
    # 0 = 关闭短路逻辑（保留旧行为：transient_error 也照常进 cycle）。
    auto_trader_max_consecutive_transient: int = 3
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
    stack_active_strategy: str | None = None
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
    # Fusion Intraday Futu pre-gate. All defaults are no-op / "log only".
    # Rollback is simply FUSION_FUTU_PREGATE_ENABLED=false (the default).
    fusion_futu_pregate_enabled: bool = False
    fusion_futu_pregate_log_only: bool = True
    fusion_futu_pregate_min_ob_imbalance: float = 0.20
    fusion_futu_pregate_min_tick_imbalance: float = 0.15
    fusion_futu_pregate_max_spread_bps: float = 15.0


def _validate_settings(s: "Settings") -> None:
    """Raise ValueError for obviously invalid configurations caught at load time."""
    allowed_active = {None, "baseline", "fusion", "ofim", "cascade"}
    if s.stack_active_strategy not in allowed_active:
        raise ValueError(
            "STACK_ACTIVE_STRATEGY 只能是 baseline / fusion / ofim / cascade，"
            f"当前是 {s.stack_active_strategy!r}。"
        )
    total_stack = (
        (s.stack_baseline_weight if s.stack_baseline_enabled else 0.0)
        + s.stack_fusion_weight
        + s.stack_ofim_weight
        + s.stack_cascade_weight
    )
    if s.stack_active_strategy is None and total_stack > 1.0 + 1e-6:
        raise ValueError(
            f"Strategy weights sum to {total_stack:.4f} which exceeds 1.0. "
            "Check STACK_BASELINE_WEIGHT / STACK_FUSION_WEIGHT / STACK_OFIM_WEIGHT / STACK_CASCADE_WEIGHT."
        )
    for name, val in (
        ("stack_baseline_weight", s.stack_baseline_weight),
        ("stack_fusion_weight", s.stack_fusion_weight),
        ("stack_ofim_weight", s.stack_ofim_weight),
        ("stack_cascade_weight", s.stack_cascade_weight),
    ):
        if val < 0:
            raise ValueError(f"{name} must be >= 0, got {val}.")
    if s.auto_trader_exit_confirm_cycles < 1:
        raise ValueError("AUTO_TRADER_EXIT_CONFIRM_CYCLES must be >= 1.")
    if s.auto_trader_min_symbol_interval_seconds < 0:
        raise ValueError("AUTO_TRADER_MIN_SYMBOL_INTERVAL_SECONDS must be >= 0.")
    if s.auto_trader_max_target_gross_exposure <= 0:
        raise ValueError("AUTO_TRADER_MAX_TARGET_GROSS_EXPOSURE must be > 0.")
    if s.auto_trader_max_target_weight <= 0:
        raise ValueError("AUTO_TRADER_MAX_TARGET_WEIGHT must be > 0.")
    if s.auto_trader_max_order_value_usd < 0:
        raise ValueError("AUTO_TRADER_MAX_ORDER_VALUE_USD must be >= 0.")
    if s.auto_trader_max_cycle_turnover_usd < 0:
        raise ValueError("AUTO_TRADER_MAX_CYCLE_TURNOVER_USD must be >= 0.")
    if s.auto_trader_max_epoch_loss_usd < 0:
        raise ValueError("AUTO_TRADER_MAX_EPOCH_LOSS_USD must be >= 0.")
    if s.auto_trader_max_epoch_loss_pct < 0:
        raise ValueError("AUTO_TRADER_MAX_EPOCH_LOSS_PCT must be >= 0.")
    if s.auto_trader_max_consecutive_transient < 0:
        raise ValueError("AUTO_TRADER_MAX_CONSECUTIVE_TRANSIENT must be >= 0.")
    try:
        from datetime import date as _date
        _date.fromisoformat(s.start_date)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"TAA_START_DATE='{s.start_date}' is not a valid ISO date: {exc}") from exc


def _resolve_env_file(env_file: str | Path, *, fallback_root: Path | None = None) -> Path:
    """Resolve the .env path, falling back to the repo root copy when the
    default cwd-relative ``.env`` is absent (e.g. CLI invoked from $HOME).

    Explicit paths are returned unchanged so tests and callers that pass a
    specific file keep exact behavior.
    """
    path = Path(env_file)
    if str(env_file) == ".env" and not path.exists():
        root = fallback_root or Path(__file__).resolve().parents[2]
        candidate = root / ".env"
        if candidate.exists():
            return candidate
    return path


def load_settings(env_file: str | Path = ".env") -> Settings:
    # The local .env file is the source of truth for this desktop app.
    # Use override=True so runtime processes pick up the latest values after UI changes.
    load_dotenv(dotenv_path=_resolve_env_file(env_file), override=True)
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

    settings = Settings(
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
        ofim_crypto_universe=_parse_symbols_optional(os.getenv("OFIM_CRYPTO_UNIVERSE")),
        ofim_crypto_to_proxy=_parse_crypto_proxy_map(os.getenv("OFIM_CRYPTO_TO_PROXY")),
        ofim_crypto_exchange=os.getenv("OFIM_CRYPTO_EXCHANGE", "binance"),
        ofim_crypto_api_key=_parse_optional_str(os.getenv("OFIM_CRYPTO_API_KEY")),
        ofim_crypto_api_secret=_parse_optional_str(os.getenv("OFIM_CRYPTO_API_SECRET")),
        ofim_crypto_sandbox=_parse_bool(os.getenv("OFIM_CRYPTO_SANDBOX"), default=False),
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
        auto_trader_fill_lookback_days=int(os.getenv("AUTO_TRADER_FILL_LOOKBACK_DAYS", "7")),
        auto_trader_order_cooldown_seconds=int(os.getenv("AUTO_TRADER_ORDER_COOLDOWN_SECONDS", "300")),
        auto_trader_exit_confirm_cycles=int(os.getenv("AUTO_TRADER_EXIT_CONFIRM_CYCLES", "1")),
        auto_trader_min_symbol_interval_seconds=int(os.getenv("AUTO_TRADER_MIN_SYMBOL_INTERVAL_SECONDS", "0")),
        auto_trader_max_target_gross_exposure=float(os.getenv("AUTO_TRADER_MAX_TARGET_GROSS_EXPOSURE", "1.0")),
        auto_trader_max_target_weight=float(os.getenv("AUTO_TRADER_MAX_TARGET_WEIGHT", "1.0")),
        auto_trader_max_order_value_usd=float(os.getenv("AUTO_TRADER_MAX_ORDER_VALUE_USD", "0")),
        auto_trader_max_cycle_turnover_usd=float(os.getenv("AUTO_TRADER_MAX_CYCLE_TURNOVER_USD", "0")),
        auto_trader_max_epoch_loss_usd=float(os.getenv("AUTO_TRADER_MAX_EPOCH_LOSS_USD", "0")),
        auto_trader_max_epoch_loss_pct=float(os.getenv("AUTO_TRADER_MAX_EPOCH_LOSS_PCT", "0")),
        auto_trader_order_stale_minutes=int(os.getenv("AUTO_TRADER_ORDER_STALE_MINUTES", "5")),
        auto_trader_min_order_value_usd=float(os.getenv("AUTO_TRADER_MIN_ORDER_VALUE_USD", "500.0")),
        auto_trader_min_hold_minutes=int(os.getenv("AUTO_TRADER_MIN_HOLD_MINUTES", "10")),
        auto_trader_rebalance_drift_pct=float(os.getenv("AUTO_TRADER_REBALANCE_DRIFT_PCT", "1.0")),
        auto_trader_max_consecutive_transient=int(os.getenv("AUTO_TRADER_MAX_CONSECUTIVE_TRANSIENT", "3")),
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
        stack_active_strategy=(
            _parse_optional_str(os.getenv("STACK_ACTIVE_STRATEGY")).lower()
            if _parse_optional_str(os.getenv("STACK_ACTIVE_STRATEGY"))
            else None
        ),
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
        # Fusion pre-gate — opt-in safety filter. Defaults preserve existing
        # behavior (gate disabled). If enabled, gate runs in log-only mode by
        # default so the user can audit decisions before letting them apply.
        fusion_futu_pregate_enabled=_parse_bool(os.getenv("FUSION_FUTU_PREGATE_ENABLED"), default=False),
        fusion_futu_pregate_log_only=_parse_bool(os.getenv("FUSION_FUTU_PREGATE_LOG_ONLY"), default=True),
        fusion_futu_pregate_min_ob_imbalance=float(os.getenv("FUSION_FUTU_PREGATE_MIN_OB_IMBALANCE", "0.20")),
        fusion_futu_pregate_min_tick_imbalance=float(os.getenv("FUSION_FUTU_PREGATE_MIN_TICK_IMBALANCE", "0.15")),
        fusion_futu_pregate_max_spread_bps=float(os.getenv("FUSION_FUTU_PREGATE_MAX_SPREAD_BPS", "15.0")),
    )
    _validate_settings(settings)

    # ── Learning-to-strategy override layer (opt-in, default OFF) ──────────────
    # When STRATEGY_OVERRIDES_ENABLED=true, apply human-approved parameter
    # overrides promoted from the learning lab (see strategy_overrides.py).
    # The applied settings are re-validated; any failure falls back to the base
    # .env settings, so a malformed override file can never break config loading.
    # With the flag off (default) this branch is a no-op and behavior is
    # byte-for-byte unchanged.
    if _parse_bool(os.getenv("STRATEGY_OVERRIDES_ENABLED"), default=False):
        try:
            from .strategy_overrides import apply_promoted_overrides

            candidate = apply_promoted_overrides(settings)
            _validate_settings(candidate)
            settings = candidate
        except Exception:
            pass

    return settings
