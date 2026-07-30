from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import math
from typing import Any

import pandas as pd

from .config import Settings
from . import market_logger

_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Binance / ccxt crypto data adapter
# ─────────────────────────────────────────────────────────────────────────────

class _CryptoDataAdapter:
    """Thin wrapper around a ccxt exchange that mirrors the data-access methods
    used by OfimIntradayStrategy for Futu symbols, so the scoring logic is
    exchange-agnostic.

    Falls back to yfinance for OHLCV when ccxt is unavailable, but order-book
    and tick data require ccxt — those fall back to empty structures gracefully.
    """

    def __init__(self, exchange_name: str, api_key: str | None, api_secret: str | None, *, sandbox: bool = False) -> None:
        self._ex: Any | None = None
        self._exchange_name = exchange_name
        self._api_key = api_key
        self._api_secret = api_secret
        self._sandbox = sandbox
        self._connected = False

    def connect(self) -> bool:
        try:
            import ccxt
            cfg: dict = {"enableRateLimit": True}
            if self._sandbox:
                cfg["sandbox"] = True
            if self._api_key:
                cfg["apiKey"] = self._api_key
            if self._api_secret:
                cfg["secret"] = self._api_secret
            self._ex = getattr(ccxt, self._exchange_name)(cfg)
            # Quick connectivity check using a public endpoint
            self._ex.fetch_ticker("BTC/USDT")
            self._connected = True
            _log.info("OFIM crypto adapter: connected to %s via ccxt.", self._exchange_name)
            return True
        except ImportError:
            _log.info("OFIM crypto adapter: ccxt not installed, crypto OHLCV will use yfinance (no LOB/tick data).")
            return False
        except Exception as exc:
            _log.warning("OFIM crypto adapter: ccxt connection failed (%s), falling back to yfinance.", exc)
            return False

    def disconnect(self) -> None:
        if self._ex is not None:
            try:
                self._ex.close()
            except Exception:
                pass
        self._connected = False

    # ── OHLCV (1-minute bars) ────────────────────────────────────────────────

    def get_recent_klines(self, symbol: str, num: int) -> pd.DataFrame:
        """Return a DataFrame with columns [time_key, open, high, low, close, volume]."""
        if self._connected and self._ex is not None:
            try:
                rows = self._ex.fetch_ohlcv(symbol, "1m", limit=num)
                if rows:
                    df = pd.DataFrame(rows, columns=["ts_ms", "open", "high", "low", "close", "volume"])
                    df["time_key"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True).dt.strftime("%Y-%m-%d %H:%M:%S")
                    return df[["time_key", "open", "high", "low", "close", "volume"]]
            except Exception as exc:
                _log.debug("ccxt get_recent_klines %s: %s", symbol, exc)

        # yfinance fallback: 1m intraday data (last 7 days max)
        try:
            import yfinance as yf
            yf_symbol = symbol.split("/")[0] + "-USD"
            df = yf.download(yf_symbol, period="1d", interval="1m", progress=False, auto_adjust=True)
            if df.empty:
                return pd.DataFrame(columns=["time_key", "open", "high", "low", "close", "volume"])
            df = df.reset_index()
            df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
            ts_col = next((c for c in df.columns if c in ("datetime", "date", "index")), df.columns[0])
            df = df.rename(columns={ts_col: "time_key"})
            df["time_key"] = pd.to_datetime(df["time_key"]).dt.strftime("%Y-%m-%d %H:%M:%S")
            return df[["time_key", "open", "high", "low", "close", "volume"]].tail(num)
        except Exception as exc:
            _log.debug("yfinance get_recent_klines %s: %s", symbol, exc)
        return pd.DataFrame(columns=["time_key", "open", "high", "low", "close", "volume"])

    # ── Order book ───────────────────────────────────────────────────────────

    def get_order_book_safe(self, symbol: str, depth: int) -> dict | None:
        """Return order book in the same dict format Futu uses: {"Bid": [...], "Ask": [...]}."""
        if not self._connected or self._ex is None:
            return None
        try:
            raw = self._ex.fetch_order_book(symbol, limit=depth)
            # ccxt format: {"bids": [[price, vol], ...], "asks": [[price, vol], ...]}
            # Futu format: {"Bid": [(price, vol, ...), ...], "Ask": [...]}
            # We store as plain 2-tuples which is what _tier_volume expects.
            bids = [(row[0], row[1]) for row in (raw.get("bids") or [])]
            asks = [(row[0], row[1]) for row in (raw.get("asks") or [])]
            return {"Bid": bids, "Ask": asks}
        except Exception as exc:
            _log.debug("ccxt get_order_book_safe %s: %s", symbol, exc)
            return None

    # ── Recent ticks ─────────────────────────────────────────────────────────

    def get_recent_tickers(self, symbol: str, num: int) -> pd.DataFrame:
        """Return a DataFrame with columns [price, volume, ticker_direction]."""
        if not self._connected or self._ex is None:
            return pd.DataFrame(columns=["price", "volume", "ticker_direction"])
        try:
            trades = self._ex.fetch_trades(symbol, limit=num)
            rows = []
            for t in trades:
                side = "BUY" if t.get("side") == "buy" else "SELL"
                rows.append({"price": float(t.get("price", 0)), "volume": float(t.get("amount", 0)), "ticker_direction": side})
            return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["price", "volume", "ticker_direction"])
        except Exception as exc:
            _log.debug("ccxt get_recent_tickers %s: %s", symbol, exc)
            return pd.DataFrame(columns=["price", "volume", "ticker_direction"])

    # ── Snapshot (last price, bid/ask) ───────────────────────────────────────

    def get_snapshots(self, symbols: list[str]) -> pd.DataFrame:
        """Return a DataFrame indexed by symbol with columns [last_price, bid_price, ask_price]."""
        rows = {}
        for symbol in symbols:
            row = {"last_price": 0.0, "bid_price": 0.0, "ask_price": 0.0}
            if self._connected and self._ex is not None:
                try:
                    t = self._ex.fetch_ticker(symbol)
                    row["last_price"] = float(t.get("last") or 0.0)
                    row["bid_price"] = float(t.get("bid") or 0.0)
                    row["ask_price"] = float(t.get("ask") or 0.0)
                except Exception as exc:
                    _log.debug("ccxt get_snapshots %s: %s", symbol, exc)
            elif not self._connected:
                # yfinance fallback for price only
                try:
                    import yfinance as yf
                    yf_symbol = symbol.split("/")[0] + "-USD"
                    info = yf.Ticker(yf_symbol).fast_info
                    row["last_price"] = float(getattr(info, "last_price", 0) or 0.0)
                except Exception:
                    pass
            rows[symbol] = row
        return pd.DataFrame(rows).T


def _compute_benchmark_score(
    bars: pd.DataFrame,
    snapshot: pd.Series,
    order_book: dict | None,
) -> float:
    """Simple market-regime score for the benchmark symbol.
    Combines 5m momentum, VWAP deviation, and shallow LOB imbalance.
    Range: roughly −1 to +1. Gate: <= −0.15 means clearly bearish → don't trade.
    """
    if bars.empty:
        return 0.0
    last = float(snapshot.get("last_price", 0.0) or 0.0)
    if last <= 0:
        return 0.0

    # 5-minute momentum
    close = _numeric_column(bars, "close")
    mom_5m = last / _safe_series_float(close.iloc[-6]) - 1 if len(close) >= 6 and _safe_series_float(close.iloc[-6]) > 0 else 0.0
    # VWAP deviation
    vol = _numeric_column(bars, "volume").fillna(0.0)
    total_vol = _safe_series_float(vol.sum())
    vwap = _safe_series_float((close * vol).sum() / total_vol) if total_vol > 0 else last
    vwap_dev = float(last / vwap - 1) if vwap > 0 else 0.0
    # LOB imbalance (shallow, 1-5 levels)
    bid_vol = _tier_volume(order_book, "Bid", 1, 5)
    ask_vol = _tier_volume(order_book, "Ask", 1, 5)
    total_vol = bid_vol + ask_vol
    lob_imb = (bid_vol - ask_vol) / total_vol if total_vol > 0 else 0.0

    score = (
        0.50 * _clip(mom_5m, 0.004)
        + 0.30 * _clip(vwap_dev, 0.0025)
        + 0.20 * max(-1.0, min(1.0, lob_imb))
    )
    return round(float(score), 6)


def _clip(value: float, scale: float) -> float:
    if scale <= 0:
        return 0.0
    normalized = value / scale
    return max(-1.0, min(1.0, normalized))


def _numeric_column(frame: pd.DataFrame, column: str) -> pd.Series:
    if frame is None or frame.empty or column not in frame.columns:
        return pd.Series(dtype=float)
    values = frame.loc[:, column]
    if isinstance(values, pd.DataFrame):
        values = values.iloc[:, 0]
    return pd.to_numeric(values, errors="coerce")


def _safe_series_float(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _level_volume(level: object) -> float:
    try:
        return float(level[1])
    except Exception:
        return 0.0


def _tier_volume(order_book: dict | None, side: str, start: int, end: int) -> float:
    if not order_book:
        return 0.0
    levels = order_book.get(side, []) or []
    if start <= 0 or end <= 0:
        return 0.0
    sliced = levels[max(0, start - 1) : max(0, end)]
    return sum(_level_volume(level) for level in sliced)


def compute_multi_level_ofi(
    order_book: dict | None,
    prev_order_book: dict | None,
    depth_tiers: tuple[tuple[int, int], ...],
) -> dict[str, float]:
    results: dict[str, float] = {}
    for idx, (start, end) in enumerate(depth_tiers, start=1):
        bid_vol = _tier_volume(order_book, "Bid", start, end)
        ask_vol = _tier_volume(order_book, "Ask", start, end)
        prev_bid = _tier_volume(prev_order_book, "Bid", start, end)
        prev_ask = _tier_volume(prev_order_book, "Ask", start, end)
        delta_bid = bid_vol - prev_bid
        delta_ask = ask_vol - prev_ask
        ofi_raw = delta_bid - delta_ask
        scale = max(bid_vol + ask_vol, 1.0)
        results[f"tier_{idx}"] = _clip(ofi_raw, scale)
    return results


def compute_volume_acceleration(bars_1m: pd.DataFrame) -> float:
    volume = _numeric_column(bars_1m, "volume").fillna(0.0)
    if len(volume) < 30:
        return 1.0
    recent = _safe_series_float(volume.iloc[-5:].mean())
    baseline = _safe_series_float(volume.iloc[-30:-5].mean())
    if baseline <= 0:
        return 1.0
    return float(recent / baseline)


def compute_micro_momentum(bars_1m: pd.DataFrame) -> dict[str, float]:
    close = _numeric_column(bars_1m, "close")
    last = _safe_series_float(close.iloc[-1]) if len(close) else 0.0
    return {
        "mom_3m": last / _safe_series_float(close.iloc[-4]) - 1 if len(close) >= 4 and _safe_series_float(close.iloc[-4]) > 0 else 0.0,
        "mom_10m": last / _safe_series_float(close.iloc[-11]) - 1 if len(close) >= 11 and _safe_series_float(close.iloc[-11]) > 0 else 0.0,
        "mom_30m": last / _safe_series_float(close.iloc[-31]) - 1 if len(close) >= 31 and _safe_series_float(close.iloc[-31]) > 0 else 0.0,
    }


def compute_vwap_deviation(bars_1m: pd.DataFrame) -> float:
    close = _numeric_column(bars_1m, "close")
    volume = _numeric_column(bars_1m, "volume").fillna(0.0)
    total_vol = _safe_series_float(volume.sum())
    if total_vol <= 0:
        return 0.0
    vwap = _safe_series_float((close * volume).sum() / total_vol)
    last = _safe_series_float(close.iloc[-1]) if len(close) else 0.0
    return float(last / vwap - 1) if last > 0 and vwap > 0 else 0.0


def compute_tick_aggression(ticks: pd.DataFrame) -> float:
    if ticks.empty:
        return 0.5
    buy_vol = ticks.loc[ticks["ticker_direction"] == "BUY", "volume"].sum()
    total = ticks["volume"].sum()
    if total <= 0:
        return 0.5
    return float(buy_vol / total)


def compute_spread_quality(snapshot: pd.Series) -> float:
    bid = float(snapshot.get("bid_price", 0) or 0.0)
    ask = float(snapshot.get("ask_price", 0) or 0.0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else float(snapshot.get("last_price", 0) or 0.0)
    if mid <= 0:
        return 9999.0
    return (ask - bid) / mid * 10_000


def _compute_atr_pct(bars: pd.DataFrame, window: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    close = _numeric_column(bars, "close")
    high = _numeric_column(bars, "high")
    low = _numeric_column(bars, "low")
    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = _safe_series_float(true_range.tail(window).mean())
    last_price = _safe_series_float(close.iloc[-1]) if len(close) else 0.0
    if last_price <= 0:
        return 0.0
    return atr / last_price


def _weight_with_cap(score_map: dict[str, float], total_exposure: float, max_weight: float) -> dict[str, float]:
    if total_exposure <= 0 or not score_map:
        return {}
    remaining = score_map.copy()
    weights: dict[str, float] = {}
    budget = total_exposure
    while remaining and budget > 0:
        total_score = sum(remaining.values())
        if total_score <= 0:
            break
        capped: list[str] = []
        for code, score in remaining.items():
            proposed = budget * score / total_score
            if proposed > max_weight + 1e-9:
                weights[code] = max_weight
                budget -= max_weight
                capped.append(code)
        if not capped:
            for code, score in remaining.items():
                weights[code] = budget * score / total_score
            break
        for code in capped:
            remaining.pop(code, None)
    return {code: round(weight, 6) for code, weight in weights.items() if weight > 0}


@dataclass(frozen=True)
class OfimFeature:
    strategy: str
    code: str
    last_price: float
    ofi_tier_1: float
    ofi_tier_2: float
    ofi_tier_3: float
    vol_accel: float
    mom_3m: float
    mom_10m: float
    mom_30m: float
    vwap_dev: float
    tick_agg: float
    spread_bps: float
    score: float
    direction: int
    conviction: float
    eligible: bool
    reason: str


@dataclass(frozen=True)
class OfimPlan:
    strategy: str
    benchmark: str
    benchmark_score: float
    exposure: float
    target_weights: dict[str, float]
    features: list[OfimFeature]


def ofim_trade_symbols(settings: Settings) -> tuple[str, ...]:
    return settings.ofim_universe


class OfimIntradayStrategy:
    def __init__(self, settings: Settings, *, prev_order_books: dict[str, dict] | None = None) -> None:
        self.settings = settings
        self.prev_order_books: dict[str, dict] = prev_order_books or {}

    def _score_symbol(
        self,
        code: str,
        order_book: dict | None,
        bars_1m: pd.DataFrame,
        ticks: pd.DataFrame,
        snapshot: pd.Series,
    ) -> OfimFeature:
        prev_book = self.prev_order_books.get(code)
        ofi = compute_multi_level_ofi(order_book, prev_book, self.settings.ofim_depth_tiers)
        vol_accel = compute_volume_acceleration(bars_1m)
        momentum = compute_micro_momentum(bars_1m)
        vwap_dev = compute_vwap_deviation(bars_1m)
        tick_agg = compute_tick_aggression(ticks)
        spread = compute_spread_quality(snapshot)

        long_score = (
            0.25 * ofi.get("tier_2", 0.0)
            + 0.15 * ofi.get("tier_1", 0.0)
            + 0.10 * ofi.get("tier_3", 0.0)
            + 0.15 * _clip(momentum.get("mom_3m", 0.0), 0.005)
            + 0.10 * _clip(momentum.get("mom_10m", 0.0), 0.015)
            + 0.10 * _clip(vol_accel - 1.0, 2.0)
            + 0.10 * _clip(tick_agg - 0.5, 0.3)
            + 0.05 * _clip(vwap_dev, 0.005)
        )

        direction = 1 if long_score >= self.settings.ofim_entry_threshold else 0
        conviction = min(1.0, long_score / max(self.settings.ofim_max_score, 1e-9)) if direction else 0.0
        reasons: list[str] = []
        if spread > self.settings.ofim_max_spread_bps:
            reasons.append("spread_too_wide")
        if vol_accel < self.settings.ofim_min_vol_acceleration:
            reasons.append("volume_too_low")
        if direction == 0:
            reasons.append("score_below_entry")

        eligible = not reasons or (direction == 1 and all(r not in {"spread_too_wide", "volume_too_low"} for r in reasons))
        reason = ",".join(reasons) if reasons else "ok"
        return OfimFeature(
            strategy="OFIM",
            code=code,
            last_price=float(snapshot.get("last_price", 0.0) or 0.0),
            ofi_tier_1=ofi.get("tier_1", 0.0),
            ofi_tier_2=ofi.get("tier_2", 0.0),
            ofi_tier_3=ofi.get("tier_3", 0.0),
            vol_accel=vol_accel,
            mom_3m=momentum.get("mom_3m", 0.0),
            mom_10m=momentum.get("mom_10m", 0.0),
            mom_30m=momentum.get("mom_30m", 0.0),
            vwap_dev=vwap_dev,
            tick_agg=tick_agg,
            spread_bps=spread,
            score=round(long_score, 6),
            direction=direction,
            conviction=round(conviction, 6),
            eligible=eligible and direction == 1,
            reason=reason,
        )

    def generate_plan(self, trader, held_symbols: set[str]) -> OfimPlan:
        cycle_ts = datetime.now(UTC)
        # Equity symbols come from ofim_universe; crypto symbols come from
        # ofim_crypto_universe.  Both are scored together and ranked.
        equity_symbols = list(dict.fromkeys(self.settings.ofim_universe))
        crypto_symbols = list(dict.fromkeys(getattr(self.settings, "ofim_crypto_universe", ())))
        all_scored_symbols = equity_symbols + [s for s in crypto_symbols if s not in equity_symbols]
        benchmark = self.settings.ofim_benchmark

        _empty = OfimPlan(
            strategy="OFIM",
            benchmark=benchmark,
            benchmark_score=0.0,
            exposure=0.0,
            target_weights={},
            features=[],
        )

        if not all_scored_symbols:
            return _empty

        # ── 1. Connect crypto adapter if crypto symbols are configured ────────
        crypto_adapter: _CryptoDataAdapter | None = None
        if crypto_symbols:
            crypto_adapter = _CryptoDataAdapter(
                exchange_name=getattr(self.settings, "ofim_crypto_exchange", "binance"),
                api_key=getattr(self.settings, "ofim_crypto_api_key", None),
                api_secret=getattr(self.settings, "ofim_crypto_api_secret", None),
                sandbox=getattr(self.settings, "ofim_crypto_sandbox", False),
            )
            crypto_adapter.connect()  # failure is non-fatal — returns False and logs

        try:
            return self._generate_plan_inner(
                trader=trader,
                held_symbols=held_symbols,
                equity_symbols=equity_symbols,
                crypto_symbols=crypto_symbols,
                all_scored_symbols=all_scored_symbols,
                crypto_adapter=crypto_adapter,
                cycle_ts=cycle_ts,
                empty=_empty,
            )
        finally:
            if crypto_adapter is not None:
                crypto_adapter.disconnect()

    def _generate_plan_inner(
        self,
        trader,
        held_symbols: set[str],
        equity_symbols: list[str],
        crypto_symbols: list[str],
        all_scored_symbols: list[str],
        crypto_adapter: _CryptoDataAdapter | None,
        cycle_ts: datetime,
        empty: OfimPlan,
    ) -> OfimPlan:
        benchmark = self.settings.ofim_benchmark

        # ── 2. Subscribe & snapshot (benchmark + equity universe) ─────────────
        futu_symbols = list(dict.fromkeys([benchmark, *equity_symbols]))
        trader.subscribe_realtime(futu_symbols)
        if hasattr(trader, "subscribe_push_lob"):
            try:
                trader.subscribe_push_lob(futu_symbols)
            except Exception as exc:
                market_logger.log_error("ofim_lob_push_subscribe", exc)
        futu_snapshots = trader.get_snapshots(futu_symbols)

        # Build unified snapshots: futu + crypto
        crypto_snapshots = (
            crypto_adapter.get_snapshots(crypto_symbols)
            if crypto_adapter is not None and crypto_symbols
            else pd.DataFrame(columns=["last_price", "bid_price", "ask_price"])
        )
        snapshots = pd.concat([futu_snapshots, crypto_snapshots])

        # ── 3. Benchmark regime score ──────────────────────────────────────────
        benchmark_score = 0.0
        if benchmark in snapshots.index:
            bm_bars = trader.get_recent_klines(benchmark, self.settings.ofim_lookback_bars)
            bm_snap = snapshots.loc[benchmark]
            bm_lob = market_logger.load_lob_cache(benchmark, max_age_seconds=5) or trader.get_order_book_safe(benchmark, 5)
            benchmark_score = _compute_benchmark_score(bm_bars, bm_snap, bm_lob)
            market_logger.log_snapshot(benchmark, bm_snap, cycle_ts)
            market_logger.log_klines(benchmark, bm_bars, cycle_ts)
            market_logger.log_lob(benchmark, bm_lob, cycle_ts)

        # ── 4. Regime gate ────────────────────────────────────────────────────
        if benchmark_score <= -0.15:
            plan = OfimPlan(
                strategy="OFIM",
                benchmark=benchmark,
                benchmark_score=round(benchmark_score, 6),
                exposure=0.0,
                target_weights={},
                features=[],
            )
            market_logger.log_plan(plan, cycle_ts)
            return plan

        # ── 5. Log snapshots ──────────────────────────────────────────────────
        for code in all_scored_symbols:
            if code in snapshots.index:
                market_logger.log_snapshot(code, snapshots.loc[code], cycle_ts)

        # ── 6. Score each symbol (equity via Futu, crypto via Binance) ────────
        features: list[OfimFeature] = []
        for code in all_scored_symbols:
            is_crypto = "/" in code
            if is_crypto and crypto_adapter is not None:
                bars = crypto_adapter.get_recent_klines(code, self.settings.ofim_lookback_bars)
                order_book = crypto_adapter.get_order_book_safe(code, self.settings.ofim_order_book_depth)
                ticks = crypto_adapter.get_recent_tickers(code, self.settings.ofim_tick_window)
            elif is_crypto:
                # Crypto configured but adapter failed — skip this symbol
                _log.debug("OFIM: skipping crypto symbol %s (no adapter)", code)
                continue
            else:
                bars = trader.get_recent_klines(code, self.settings.ofim_lookback_bars)
                order_book = market_logger.load_lob_cache(code, max_age_seconds=5) or trader.get_order_book_safe(
                    code,
                    self.settings.ofim_order_book_depth,
                )
                ticks = trader.get_recent_tickers(code, self.settings.ofim_tick_window)

            market_logger.log_klines(code, bars, cycle_ts)
            market_logger.log_lob(code, order_book, cycle_ts)
            market_logger.log_ticks(code, ticks, cycle_ts)

            feature = self._score_symbol(
                code=code,
                order_book=order_book,
                bars_1m=bars,
                ticks=ticks,
                snapshot=snapshots.loc[code] if code in snapshots.index else pd.Series(dtype=object),
            )
            market_logger.log_feature(feature, cycle_ts)
            features.append(feature)
            if order_book:
                self.prev_order_books[code] = order_book

        # ── 7. Select candidates & compute weights ────────────────────────────
        # Exposure scales with benchmark strength: full at +1, half at 0
        exposure_scale = min(1.0, max(0.0, 0.5 + benchmark_score))
        max_exposure = self.settings.ofim_max_gross_exposure * exposure_scale

        candidates: dict[str, float] = {}
        for feature in features:
            # For held_symbols tracking, crypto uses its own symbol but execution
            # will be remapped to the proxy ETF — treat the crypto symbol directly here.
            if feature.code in held_symbols:
                if feature.score >= self.settings.ofim_exit_threshold:
                    candidates[feature.code] = max(feature.score, self.settings.ofim_exit_threshold)
            elif feature.eligible and feature.score >= self.settings.ofim_entry_threshold:
                candidates[feature.code] = feature.score

        ordered = dict(sorted(candidates.items(), key=lambda item: item[1], reverse=True)[: self.settings.ofim_max_positions])
        exposure = max_exposure if ordered else 0.0
        raw_weights = _weight_with_cap(ordered, exposure, self.settings.ofim_max_position_weight)

        # ── 8. Remap crypto symbols to proxy ETFs for Futu execution ─────────
        crypto_proxy_map: dict[str, str] = dict(
            getattr(self.settings, "ofim_crypto_to_proxy", ()) or ()
        )
        target_weights: dict[str, float] = {}
        for code, weight in raw_weights.items():
            proxy = crypto_proxy_map.get(code)
            if proxy:
                # Merge into proxy (two crypto symbols could map to the same ETF)
                target_weights[proxy] = round(target_weights.get(proxy, 0.0) + weight, 6)
            else:
                target_weights[code] = weight

        plan = OfimPlan(
            strategy="OFIM",
            benchmark=benchmark,
            benchmark_score=round(benchmark_score, 6),
            exposure=round(exposure, 6),
            target_weights=target_weights,
            features=sorted(features, key=lambda item: item.score, reverse=True),
        )
        market_logger.log_plan(plan, cycle_ts)
        return plan
