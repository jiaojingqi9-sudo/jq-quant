from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


def _parse_symbols(raw: str | None) -> tuple[str, ...]:
    """Parse comma-separated symbols into a tuple."""
    if not raw:
        return ()
    symbols = tuple(part.strip() for part in raw.split(",") if part.strip())
    return symbols


def _parse_optional_int(raw: str | None) -> int | None:
    """Parse optional integer from environment variable."""
    if raw is None or raw == "":
        return None
    return int(raw)


def _parse_optional_str(raw: str | None) -> str | None:
    """Parse optional string from environment variable."""
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    """Parse boolean from environment variable."""
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_float(raw: str | None, *, default: float) -> float:
    """Parse float from environment variable."""
    if raw is None:
        return default
    return float(raw)


@dataclass(frozen=True)
class Settings:
    """Configuration settings for claude-trade.

    All values are loaded from environment variables (or a .env file).
    See the project README / .env.example for documentation of each variable.
    """

    # ── General ───────────────────────────────────────────────────────────
    initial_capital: float
    risk_per_trade_pct: float
    max_position_pct: float
    target_annual_vol: float

    # ── Futu OpenD ────────────────────────────────────────────────────────
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

    # ── Crypto exchange ───────────────────────────────────────────────────
    crypto_exchange: str
    crypto_api_key: str | None
    crypto_api_secret: str | None
    crypto_passphrase: str | None
    crypto_sandbox: bool
    # Equity-listed symbols that proxy crypto exposure (e.g. BTC/ETH ETFs).
    # These are priced via Futu but counted as the "crypto" asset class so that
    # the crypto budget flows to them instead of being parked in bonds.
    # Example: US.IBIT (iShares Bitcoin Trust), US.ETHA (iShares Ethereum Trust)
    crypto_proxy_symbols: tuple[str, ...]

    # ── Dual Momentum universe ────────────────────────────────────────────
    dm_universe: tuple[str, ...]
    dm_lookback_months: int
    dm_rebalance_day: int
    dm_absolute_threshold: float
    dm_use_risk_free: str

    # ── RSI Mean Reversion ────────────────────────────────────────────────
    rsi_universe: tuple[str, ...]
    rsi_period: int
    rsi_oversold: int
    rsi_overbought: int
    rsi_timeframe: str
    rsi_volume_filter: bool
    rsi_trend_filter_period: int

    # ── Volatility Breakout ───────────────────────────────────────────────
    vb_universe: tuple[str, ...]
    vb_k_factor: float
    vb_timeframe: str

    # ── Strategy weights ──────────────────────────────────────────────────
    strategy_dm_weight: float
    strategy_rsi_weight: float
    strategy_vb_weight: float

    # ── Active strategy list ───────────────────────────────────────────────
    # Comma-separated strategy names loaded from the StrategyRegistry.
    # e.g.  ACTIVE_STRATEGIES=cascade
    #        ACTIVE_STRATEGIES=dual_momentum,rsi_mean_reversion
    # All named strategies must be registered (i.e., present in strategies/).
    # Default: ["cascade"] — the flagship full-portfolio strategy.
    active_strategies: list[str]

    # ── Auto trader ───────────────────────────────────────────────────────
    auto_trader_poll_seconds: int
    auto_trader_market_timezone: str
    # Minimum seconds between two actual order-submission rounds.
    # Set to 0 to disable cooldown (submit every cycle if weights changed).
    auto_trader_order_cooldown_seconds: int
    # Only submit equity (US.* / HK.*) orders during exchange trading hours.
    # Crypto orders (24/7) are never gated by this flag.
    auto_trader_equity_hours_only: bool
    # NYSE regular trading hours (used when equity_hours_only=True).
    auto_trader_equity_open: str   # "HH:MM"  in auto_trader_market_timezone
    auto_trader_equity_close: str  # "HH:MM"
    # Minimum fractional weight change required to rebalance a position.
    # e.g. 0.02 means skip orders where |target_weight - current_weight| < 2%.
    rebalance_min_change_pct: float

    # ── Market data logging ───────────────────────────────────────────────
    # Number of calendar days of JSONL logs to keep under runtime/market_data/.
    # Older directories are automatically cleaned up by the logger.
    log_retention_days: int

    # ── Dashboard ─────────────────────────────────────────────────────────
    dashboard_port: int


def load_settings(env_file: str | Path = ".env") -> Settings:
    """Load settings from environment variables and .env file."""
    # Desktop workflow expects edits in .env to take effect on the next
    # process start, even if the parent shell still has older variables set.
    load_dotenv(dotenv_path=env_file, override=True)

    return Settings(
        # ── General ───────────────────────────────────────────────────────
        initial_capital=float(os.getenv("INITIAL_CAPITAL", "5000")),
        risk_per_trade_pct=_parse_float(os.getenv("RISK_PER_TRADE_PCT"), default=0.02),
        max_position_pct=_parse_float(os.getenv("MAX_POSITION_PCT"), default=0.30),
        target_annual_vol=_parse_float(os.getenv("TARGET_ANNUAL_VOL"), default=0.10),

        # ── Futu OpenD ────────────────────────────────────────────────────
        futu_host=os.getenv("FUTU_HOST", "127.0.0.1"),
        futu_port=int(os.getenv("FUTU_PORT", "11111")),
        futu_trd_market=os.getenv("FUTU_TRD_MARKET", "US"),
        futu_trd_env=os.getenv("FUTU_TRD_ENV", "SIMULATE"),
        futu_acc_id=_parse_optional_int(os.getenv("FUTU_ACC_ID")),
        futu_enable_real_trading=_parse_bool(
            os.getenv("FUTU_ENABLE_REAL_TRADING"), default=False
        ),
        futu_allow_auto_real=_parse_bool(os.getenv("FUTU_ALLOW_AUTO_REAL"), default=False),
        futu_unlock_trade_password_md5=_parse_optional_str(
            os.getenv("FUTU_UNLOCK_TRADE_PASSWORD_MD5")
        ),
        futu_price_buffer_bps=int(os.getenv("FUTU_PRICE_BUFFER_BPS", "10")),
        futu_fill_outside_rth=_parse_bool(os.getenv("FUTU_FILL_OUTSIDE_RTH"), default=False),

        # ── Crypto exchange ───────────────────────────────────────────────
        crypto_exchange=os.getenv("CRYPTO_EXCHANGE", "binance"),
        crypto_api_key=_parse_optional_str(os.getenv("CRYPTO_API_KEY")),
        crypto_api_secret=_parse_optional_str(os.getenv("CRYPTO_API_SECRET")),
        crypto_passphrase=_parse_optional_str(os.getenv("CRYPTO_PASSPHRASE")),
        crypto_sandbox=_parse_bool(os.getenv("CRYPTO_SANDBOX"), default=True),
        crypto_proxy_symbols=_parse_symbols(
            os.getenv("CRYPTO_PROXY_SYMBOLS", "US.IBIT,US.ETHA")
        ),

        # ── Dual Momentum universe ────────────────────────────────────────
        dm_universe=_parse_symbols(
            os.getenv("DM_UNIVERSE", "US.SPY,US.EFA,US.AGG,US.GLD,BTC/USDT,ETH/USDT")
        ),
        dm_lookback_months=int(os.getenv("DM_LOOKBACK_MONTHS", "12")),
        dm_rebalance_day=int(os.getenv("DM_REBALANCE_DAY", "1")),
        dm_absolute_threshold=_parse_float(os.getenv("DM_ABSOLUTE_THRESHOLD"), default=0.0),
        dm_use_risk_free=os.getenv("DM_USE_RISK_FREE", "US.AGG"),

        # ── RSI Mean Reversion ────────────────────────────────────────────
        rsi_universe=_parse_symbols(
            os.getenv("RSI_UNIVERSE", "BTC/USDT,ETH/USDT,SOL/USDT")
        ),
        rsi_period=int(os.getenv("RSI_PERIOD", "14")),
        rsi_oversold=int(os.getenv("RSI_OVERSOLD", "30")),
        rsi_overbought=int(os.getenv("RSI_OVERBOUGHT", "70")),
        rsi_timeframe=os.getenv("RSI_TIMEFRAME", "4h"),
        rsi_volume_filter=_parse_bool(os.getenv("RSI_VOLUME_FILTER"), default=True),
        rsi_trend_filter_period=int(os.getenv("RSI_TREND_FILTER_PERIOD", "200")),

        # ── Volatility Breakout ───────────────────────────────────────────
        vb_universe=_parse_symbols(os.getenv("VB_UNIVERSE", "BTC/USDT,ETH/USDT")),
        vb_k_factor=_parse_float(os.getenv("VB_K_FACTOR"), default=0.5),
        vb_timeframe=os.getenv("VB_TIMEFRAME", "1d"),

        # ── Strategy weights ──────────────────────────────────────────────
        strategy_dm_weight=_parse_float(os.getenv("STRATEGY_DM_WEIGHT"), default=0.50),
        strategy_rsi_weight=_parse_float(os.getenv("STRATEGY_RSI_WEIGHT"), default=0.30),
        strategy_vb_weight=_parse_float(os.getenv("STRATEGY_VB_WEIGHT"), default=0.20),

        # ── Active strategies ─────────────────────────────────────────────
        active_strategies=[
            s.strip()
            for s in os.getenv("ACTIVE_STRATEGIES", "cascade").split(",")
            if s.strip()
        ],

        # ── Auto trader ───────────────────────────────────────────────────
        auto_trader_poll_seconds=int(os.getenv("AUTO_TRADER_POLL_SECONDS", "60")),
        auto_trader_market_timezone=os.getenv(
            "AUTO_TRADER_MARKET_TIMEZONE", "America/New_York"
        ),
        auto_trader_order_cooldown_seconds=int(
            os.getenv("AUTO_TRADER_ORDER_COOLDOWN_SECONDS", "300")
        ),
        auto_trader_equity_hours_only=_parse_bool(
            os.getenv("AUTO_TRADER_EQUITY_HOURS_ONLY"), default=True
        ),
        auto_trader_equity_open=os.getenv("AUTO_TRADER_EQUITY_OPEN", "09:30"),
        auto_trader_equity_close=os.getenv("AUTO_TRADER_EQUITY_CLOSE", "15:55"),
        rebalance_min_change_pct=_parse_float(
            os.getenv("REBALANCE_MIN_CHANGE_PCT"), default=0.02
        ),

        # ── Market data logging ───────────────────────────────────────────
        log_retention_days=int(os.getenv("LOG_RETENTION_DAYS", "30")),

        # ── Dashboard ─────────────────────────────────────────────────────
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8051")),
    )
