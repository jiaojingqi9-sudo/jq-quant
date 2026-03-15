from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
import sys
from unittest.mock import patch

import pandas as pd

from .config import Settings
from .futu_gateway import FutuPaperTrader
from .market_data import MarketDataError, YFinanceDataProvider


REPO_ROOT = Path(__file__).resolve().parents[2]
CASCADE_ROOT = REPO_ROOT / "claude-trade"
CASCADE_SRC = CASCADE_ROOT / "src"
CASCADE_ENV_FALLBACK = CASCADE_ROOT / ".env.example"


@dataclass(frozen=True)
class CascadeSleevePlan:
    target_weights: dict[str, float]
    total_exposure: float
    regime_label: str
    regime_score: float
    note: str = ""
    # ── Extended fields (populated from full CascadePlan) ─────────────────
    # Regime sub-signals
    crypto_pulse: float = 0.0
    vol_regime: str = ""
    cross_asset_flow: float = 0.0
    funding_signal: float = 0.0
    regime_details: dict = None          # vix_level, funding_rate, btc_weekend_return, …
    # Full target weights including crypto legs (before Futu filtering)
    all_target_weights: dict = None      # symbol → weight (BTC/USDT, ETH/USDT included)
    all_asset_class_budgets: dict = None # equity / crypto / bond budgets
    # Per-asset scores
    asset_scores: list = None            # list of AssetScore dicts

    def __post_init__(self):
        # Replace None defaults with empty collections to avoid mutable defaults
        if self.regime_details is None:
            object.__setattr__(self, "regime_details", {})
        if self.all_target_weights is None:
            object.__setattr__(self, "all_target_weights", {})
        if self.all_asset_class_budgets is None:
            object.__setattr__(self, "all_asset_class_budgets", {})
        if self.asset_scores is None:
            object.__setattr__(self, "asset_scores", [])


def _ensure_cascade_import_path() -> None:
    src = str(CASCADE_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)


def _normalize_history(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    data = frame.copy()
    timestamp_col = "time_key" if "time_key" in data.columns else data.columns[0]
    data["timestamp"] = pd.to_datetime(data[timestamp_col]).dt.tz_localize(None)
    for column in ["open", "high", "low", "close", "volume"]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
        else:
            data[column] = 0.0
    return (
        data[["timestamp", "open", "high", "low", "close", "volume"]]
        .dropna(subset=["timestamp", "open", "high", "low", "close"])
        .sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="last")
        .reset_index(drop=True)
    )


def _close_frame_to_ohlcv_frame(symbol: str, closes: pd.DataFrame) -> pd.DataFrame:
    if closes.empty or symbol not in closes.columns:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    series = pd.to_numeric(closes[symbol], errors="coerce").dropna()
    if series.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(series.index).tz_localize(None),
            "open": series.values,
            "high": series.values,
            "low": series.values,
            "close": series.values,
            "volume": 0.0,
        }
    )
    return frame.reset_index(drop=True)


def _cascade_env_path(settings: Settings) -> Path:
    configured = Path(settings.cascade_env_file)
    if not configured.is_absolute():
        configured = REPO_ROOT / configured
    if configured.exists():
        return configured
    if CASCADE_ENV_FALLBACK.exists():
        return CASCADE_ENV_FALLBACK
    raise FileNotFoundError(f"Missing Cascade env file: {configured}")


def _load_cascade_runtime(settings: Settings):
    _ensure_cascade_import_path()
    from claude_trade.config import load_settings as load_cascade_settings
    import claude_trade.strategies.cascade as cascade_module
    from claude_trade.strategies.cascade import CascadeStrategy

    cascade_settings = load_cascade_settings(_cascade_env_path(settings))
    cascade_settings = replace(
        cascade_settings,
        futu_host=settings.futu_host,
        futu_port=settings.futu_port,
        futu_trd_market=settings.futu_trd_market,
        futu_trd_env=settings.futu_trd_env,
        futu_acc_id=settings.futu_acc_id,
        futu_enable_real_trading=settings.futu_enable_real_trading,
        futu_allow_auto_real=settings.futu_allow_auto_real,
        futu_unlock_trade_password_md5=settings.futu_unlock_trade_password_md5,
        futu_price_buffer_bps=settings.futu_price_buffer_bps,
        futu_fill_outside_rth=settings.futu_fill_outside_rth,
    )
    return cascade_settings, CascadeStrategy, cascade_module


def cascade_trade_symbols(settings: Settings) -> tuple[str, ...]:
    cascade_settings, _strategy_cls, _module = _load_cascade_runtime(settings)
    market_prefix = f"{settings.futu_trd_market}."
    requested = [symbol for symbol in cascade_settings.dm_universe if symbol.startswith(market_prefix)]
    extras = [cascade_settings.dm_use_risk_free, "US.SPY", "US.GLD", "US.VIX"]
    for symbol in extras:
        if isinstance(symbol, str) and symbol.startswith(market_prefix):
            requested.append(symbol)
    return tuple(dict.fromkeys(requested))


def fetch_cascade_daily_frames(
    trader: FutuPaperTrader,
    settings: Settings,
    *,
    start: str,
    end: str,
    progress: Callable[[str, int, int, str, str], None] | None = None,
) -> dict[str, pd.DataFrame]:
    symbols = list(dict.fromkeys(cascade_trade_symbols(settings)))
    frames: dict[str, pd.DataFrame] = {}
    yfinance_provider = YFinanceDataProvider()

    for index, code in enumerate(symbols, start=1):
        if progress is not None:
            progress("fetch_start", index, len(symbols), code, "futu")
        try:
            frame = trader.request_history_klines(
                code,
                start=start,
                end=end,
                ktype="K_DAY",
                session="RTH",
            )
            normalized = _normalize_history(frame)
            if not normalized.empty:
                frames[code] = normalized
                if progress is not None:
                    progress("fetch_ok", index, len(symbols), code, f"futu:{len(normalized)}")
                continue
        except Exception as exc:
            if progress is not None:
                progress("fetch_warn", index, len(symbols), code, str(exc))

        try:
            fallback = yfinance_provider.fetch_daily_closes([code], start=start, end=end)
            normalized = _close_frame_to_ohlcv_frame(code, fallback)
            if not normalized.empty:
                frames[code] = normalized
                if progress is not None:
                    progress("fetch_ok", index, len(symbols), code, f"yfinance:{len(normalized)}")
                continue
        except (MarketDataError, Exception) as exc:
            if progress is not None:
                progress("fetch_skip", index, len(symbols), code, str(exc))

    return frames


def cascade_summary_line(settings: Settings) -> str:
    try:
        cascade_settings, _strategy_cls, _module = _load_cascade_runtime(settings)
        visible = [symbol.replace("US.", "") for symbol in cascade_trade_symbols(settings) if symbol.startswith("US.")]
        if len(visible) > 8:
            visible_text = " / ".join(visible[:8]) + f" / ... 共{len(visible)}只"
        else:
            visible_text = " / ".join(visible) if visible else "无"
        return (
            "Claude/Cascade 是什么: 级联日频策略。"
            f"当前用的是 claude-trade 里的 DM universe，Futu 可交易标的 {visible_text}，"
            f"单标的上限 {cascade_settings.max_position_pct:.0%}，目标年化波动 {cascade_settings.target_annual_vol:.0%}。"
        )
    except Exception as exc:
        return f"Claude/Cascade 是什么: 外部策略已发现，但当前读取失败 / unavailable ({exc})。"


def _to_sleeve_plan(raw_plan, settings: Settings) -> CascadeSleevePlan:
    market_prefix = f"{settings.futu_trd_market}."
    target_weights = {
        symbol: round(float(weight), 6)
        for symbol, weight in raw_plan.target_weights.items()
        if symbol.startswith(market_prefix) and float(weight) > 0
    }
    filtered_exposure = round(sum(target_weights.values()), 6)
    raw_exposure = round(float(getattr(raw_plan, "total_exposure", filtered_exposure)), 6)
    note = ""
    if raw_exposure > filtered_exposure + 1e-9:
        note = "非富途可交易部分保持现金 / Non-Futu legs stay in cash."

    # ── Regime sub-signals ────────────────────────────────────────────────
    regime = raw_plan.regime
    crypto_pulse      = float(getattr(regime, "crypto_pulse",      0.0))
    vol_regime        = str(getattr(regime,   "vol_regime",        ""))
    cross_asset_flow  = float(getattr(regime, "cross_asset_flow",  0.0))
    funding_signal    = float(getattr(regime, "funding_signal",    0.0))
    regime_details    = dict(getattr(regime,  "details",           {}))

    # ── Full weights (all legs, including crypto) ─────────────────────────
    all_target_weights = {
        sym: round(float(w), 6)
        for sym, w in raw_plan.target_weights.items()
        if float(w) > 0
    }

    # ── Asset class budgets ───────────────────────────────────────────────
    all_asset_class_budgets = dict(getattr(raw_plan, "asset_class_budgets", {}))

    # ── Per-asset scores (serialise to plain dicts for pickling) ──────────
    raw_scores = getattr(raw_plan, "asset_scores", [])
    asset_scores = []
    for s in raw_scores:
        if hasattr(s, "__dict__"):
            asset_scores.append({k: v for k, v in s.__dict__.items()})
        elif hasattr(s, "_asdict"):
            asset_scores.append(s._asdict())
        else:
            try:
                import dataclasses
                asset_scores.append(dataclasses.asdict(s))
            except Exception:
                asset_scores.append({"symbol": str(s)})

    return CascadeSleevePlan(
        target_weights=target_weights,
        total_exposure=filtered_exposure,
        regime_label=str(regime.label),
        regime_score=float(regime.score),
        note=note,
        crypto_pulse=crypto_pulse,
        vol_regime=vol_regime,
        cross_asset_flow=cross_asset_flow,
        funding_signal=funding_signal,
        regime_details=regime_details,
        all_target_weights=all_target_weights,
        all_asset_class_budgets=all_asset_class_budgets,
        asset_scores=asset_scores,
    )


class _LiveCascadeExchange:
    def __init__(self, trader: FutuPaperTrader, settings: Settings) -> None:
        self.trader = trader
        self.settings = settings
        self._history_cache: dict[tuple[str, str, int], pd.DataFrame] = {}
        self._price_cache: dict[str, float] = {}

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        if timeframe not in {"1d", "1D", "1day", "1_day"}:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        cache_key = (symbol, "K_DAY", limit)
        if cache_key not in self._history_cache:
            days = max(365, limit * 4)
            start = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
            frame = self.trader.request_history_klines(
                symbol,
                start=start,
                ktype="K_DAY",
                session="RTH",
            )
            self._history_cache[cache_key] = _normalize_history(frame).tail(limit).reset_index(drop=True)
        return self._history_cache[cache_key].copy()

    def get_price(self, symbol: str) -> float:
        if symbol not in self._price_cache:
            snapshot = self.trader.get_snapshots([symbol])
            self._price_cache[symbol] = float(snapshot.loc[symbol, "last_price"])
        return self._price_cache[symbol]


class _HistoricalCascadeExchange:
    def __init__(self, frames: dict[str, pd.DataFrame], as_of: pd.Timestamp) -> None:
        self.frames = {symbol: _normalize_history(frame) for symbol, frame in frames.items()}
        self.as_of = pd.Timestamp(as_of).tz_localize(None)

    def _visible(self, symbol: str) -> pd.DataFrame:
        frame = self.frames.get(symbol, pd.DataFrame())
        if frame.empty:
            return frame
        return frame[frame["timestamp"] <= self.as_of].copy()

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        if timeframe not in {"1d", "1D", "1day", "1_day"}:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        return self._visible(symbol).tail(limit).reset_index(drop=True)

    def get_price(self, symbol: str) -> float:
        visible = self._visible(symbol)
        if visible.empty:
            return 0.0
        return float(visible["close"].iloc[-1])


@contextmanager
def _patched_cascade_now(cascade_module, reference_time: datetime):
    real_datetime = cascade_module.datetime

    class _ReplayDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return reference_time.replace(tzinfo=None)
            return reference_time.astimezone(tz)

    with patch.object(cascade_module, "datetime", _ReplayDateTime):
        yield


class _YFinanceCryptoExchange:
    """Read-only crypto price adapter using yfinance (already in taa_futu deps).

    Provides BTC/ETH OHLCV to the Cascade strategy without requiring ccxt or
    a Binance connection.  Symbol mapping: "BTC/USDT" → "BTC-USD", etc.
    Funding rate is not available via yfinance so returns None (strategy
    handles this gracefully).

    If ccxt IS installed and reachable, _CcxtDataExchange is preferred instead
    (it also supplies the funding-rate signal).
    """

    # Map ccxt-style symbols → yfinance tickers
    _YF_MAP: dict[str, str] = {
        "BTC/USDT": "BTC-USD",
        "ETH/USDT": "ETH-USD",
        "SOL/USDT": "SOL-USD",
        "BNB/USDT": "BNB-USD",
        "BTC-USD":  "BTC-USD",
        "ETH-USD":  "ETH-USD",
    }

    # Map timeframe strings to yfinance interval / period arguments
    _TF_MAP: dict[str, tuple[str, str]] = {
        "1d": ("1d", "1y"),
        "1D": ("1d", "1y"),
        "4h": ("1h", "60d"),   # yfinance max 60 days for intraday
        "1h": ("1h", "60d"),
    }

    def __init__(self) -> None:
        import logging
        self._log = logging.getLogger(__name__)
        self._cache: dict[str, pd.DataFrame] = {}

    def _yf_ticker(self, symbol: str) -> str:
        return self._YF_MAP.get(symbol, symbol.replace("/", "-").split(":")[0])

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        cache_key = f"{symbol}:{timeframe}:{limit}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            import yfinance as yf
            ticker = self._yf_ticker(symbol)
            interval, period = self._TF_MAP.get(timeframe, ("1d", "1y"))
            df = yf.download(ticker, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df.empty:
                return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
            df = df.reset_index()
            # yfinance column names may be capitalised; normalise
            df.columns = [c.lower() if isinstance(c, str) else c[0].lower()
                          for c in df.columns]
            date_col = next((c for c in df.columns if c in ("date", "datetime", "index")), df.columns[0])
            df = df.rename(columns={date_col: "timestamp"})
            for col in ("open", "high", "low", "close", "volume"):
                if col not in df.columns:
                    df[col] = 0.0
            df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
            result = df[["timestamp", "open", "high", "low", "close", "volume"]].tail(limit).reset_index(drop=True)
            self._cache[cache_key] = result
            return result
        except Exception as exc:
            self._log.warning("yfinance get_ohlcv %s failed: %s", symbol, exc)
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    def get_price(self, symbol: str) -> float:
        df = self.get_ohlcv(symbol, "1d", 2)
        if df.empty:
            return 0.0
        return float(df["close"].iloc[-1])

    def fetch_funding_rate(self, symbol: str) -> float | None:
        """yfinance doesn't have funding rates — return None (graceful degradation)."""
        return None

    def disconnect(self) -> None:
        pass


def generate_live_cascade_plan(settings: Settings, trader: FutuPaperTrader) -> CascadeSleevePlan:
    import logging
    _log = logging.getLogger(__name__)

    cascade_settings, strategy_cls, _module = _load_cascade_runtime(settings)
    exchange = _LiveCascadeExchange(trader, settings)

    # ── Crypto price data: prefer ccxt (has funding rates), fall back to yfinance
    crypto_exchange = None
    try:
        import ccxt  # optional – not in taa_futu base deps
        exchange_name = getattr(cascade_settings, "crypto_exchange", "binance").lower()
        ex_cls = getattr(ccxt, exchange_name)
        cfg: dict = {"enableRateLimit": True, "sandbox": False}
        api_key    = getattr(cascade_settings, "crypto_api_key",    None)
        api_secret = getattr(cascade_settings, "crypto_api_secret", None)
        if api_key:
            cfg["apiKey"] = api_key
        if api_secret:
            cfg["secret"] = api_secret

        class _CcxtAdapter:
            def __init__(self, ex):
                self._ex = ex
                self._log = logging.getLogger(__name__)
            def get_ohlcv(self, symbol, timeframe, limit):
                try:
                    rows = self._ex.fetch_ohlcv(symbol, timeframe, limit=limit)
                    df = pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume"])
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                    return df
                except Exception as exc:
                    self._log.warning("ccxt get_ohlcv %s: %s", symbol, exc)
                    return pd.DataFrame(columns=["timestamp","open","high","low","close","volume"])
            def get_price(self, symbol):
                try:
                    return float(self._ex.fetch_ticker(symbol)["last"])
                except Exception:
                    return 0.0
            def fetch_funding_rate(self, symbol):
                try:
                    fr = self._ex.fetch_funding_rate(symbol)
                    return float(fr.get("fundingRate", 0.0)) if fr else None
                except Exception:
                    return None
            def disconnect(self):
                try:
                    self._ex.close()
                except Exception:
                    pass

        raw_ex = ex_cls(cfg)
        # Quick connectivity check (public endpoint)
        raw_ex.fetch_ticker("BTC/USDT")
        crypto_exchange = _CcxtAdapter(raw_ex)
        _log.info("Cascade: using ccxt/%s for crypto data.", exchange_name)

    except ImportError:
        pass  # ccxt not installed → fall through to yfinance
    except Exception as exc:
        _log.info("Cascade: ccxt unavailable (%s), falling back to yfinance.", exc)

    if crypto_exchange is None:
        try:
            crypto_exchange = _YFinanceCryptoExchange()
            _log.info("Cascade: using yfinance for BTC/ETH price data.")
        except Exception as exc:
            _log.warning("Cascade: yfinance crypto adapter failed (%s) — degraded.", exc)

    try:
        plan = strategy_cls(cascade_settings).run_cycle(
            crypto_exchange=crypto_exchange,
            futu_exchange=exchange,
        )
    finally:
        if crypto_exchange is not None:
            try:
                crypto_exchange.disconnect()
            except Exception:
                pass

    return _to_sleeve_plan(plan, settings)


def generate_replay_cascade_plan(
    price_frames: dict[str, pd.DataFrame],
    settings: Settings,
    *,
    as_of: pd.Timestamp,
) -> CascadeSleevePlan:
    cascade_settings, strategy_cls, cascade_module = _load_cascade_runtime(settings)
    exchange = _HistoricalCascadeExchange(price_frames, as_of)
    strategy = strategy_cls(cascade_settings)
    reference_time = pd.Timestamp(as_of).to_pydatetime()
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=UTC)
    with _patched_cascade_now(cascade_module, reference_time):
        plan = strategy.run_cycle(crypto_exchange=None, futu_exchange=exchange)
    return _to_sleeve_plan(plan, settings)
