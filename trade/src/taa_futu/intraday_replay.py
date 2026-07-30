"""Intraday strategy replay backtest using real stored market data.

Architecture (low coupling)
---------------------------
Data layer  : ReplayDataStore — loads JSONL files, zero strategy knowledge.
Adapter     : ReplayTrader   — duck-types the live FutuPaperTrader interface.
Engine      : run_intraday_replay() — generic; accepts any generate_plan callable.
Wrappers    : run_fusion_replay() / run_ofim_replay() — inject the right strategy.

All JSONL files are written by market_logger during live trading:
  runtime/market_data/<YYYY-MM-DD>/lob.jsonl
  runtime/market_data/<YYYY-MM-DD>/klines.jsonl
  runtime/market_data/<YYYY-MM-DD>/ticks.jsonl
  runtime/market_data/<YYYY-MM-DD>/snapshots.jsonl
  runtime/market_data/<YYYY-MM-DD>/plan.jsonl   (ground-truth live plans)

Execution simulation
--------------------
Signal generated at cycle T  →  fill executed at cycle T+1 snapshot price.
BUY  fills at ask_price (or last_price + half_spread).
SELL fills at bid_price (or last_price − half_spread).
End-of-day positions are marked-to-market at last available snapshot price.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone as _tz
UTC = _tz.utc
from pathlib import Path
from typing import Any, Callable, Protocol

import pandas as pd

from .costs import TradeCostModel, build_trade_cost_model, estimate_trade_cost

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKET_DATA_DIR = REPO_ROOT / "runtime" / "market_data"

_MIN_ORDER_VALUE_USD = 100.0   # ignore rebalance legs below this notional


# ═══════════════════════════════════════════════════════════════════════════
# Public result types
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class IntradayReplayResult:
    """Outcome of one replay backtest run."""
    equity_curve: pd.Series        # portfolio value indexed by cycle timestamp
    trade_log: pd.DataFrame        # one row per fill
    plan_log: list[dict]           # raw plan output per cycle (target_weights, scores …)
    summary: dict[str, float]      # cagr, sharpe, max_drawdown, total_fees …


# ═══════════════════════════════════════════════════════════════════════════
# Trader protocol  — the interface ReplayTrader must satisfy
# ═══════════════════════════════════════════════════════════════════════════

class _TraderProtocol(Protocol):
    def subscribe_realtime(self, symbols: list[str]) -> None: ...
    def get_snapshots(self, symbols: list[str]) -> pd.DataFrame: ...
    def get_recent_klines(self, code: str, limit: int) -> pd.DataFrame: ...
    def get_recent_tickers(self, code: str, limit: int) -> pd.DataFrame: ...
    def get_order_book_safe(self, code: str, depth: int) -> dict: ...


# ═══════════════════════════════════════════════════════════════════════════
# Data loading helpers
# ═══════════════════════════════════════════════════════════════════════════

def _iter_day_dirs(start: str, end: str) -> list[Path]:
    start_date = pd.Timestamp(start).date()
    end_date   = pd.Timestamp(end).date()
    result: list[Path] = []
    for path in sorted(MARKET_DATA_DIR.glob("*")):
        if not path.is_dir():
            continue
        try:
            day = pd.Timestamp(path.name).date()
        except ValueError:
            continue
        if start_date <= day <= end_date:
            result.append(path)
    return result


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _normalize_klines(rows: list[dict]) -> pd.DataFrame:
    """Convert raw klines dicts to a normalised OHLCV DataFrame."""
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    # Futu uses 'time_key'; market_logger preserves the raw Futu column names.
    ts_col = "time_key" if "time_key" in df.columns else df.columns[0]
    df["timestamp"] = pd.to_datetime(df[ts_col], errors="coerce").dt.tz_localize(None)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = 0.0
    return (
        df[["timestamp", "open", "high", "low", "close", "volume"]]
        .dropna(subset=["timestamp", "close"])
        .sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="last")
        .reset_index(drop=True)
    )


def _normalize_ticks(rows: list[dict]) -> pd.DataFrame:
    """Convert raw tick dicts to a DataFrame compatible with _compute_tick_imbalance."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # Futu column names: ticker_direction → direction; price, volume already present.
    if "ticker_direction" in df.columns and "direction" not in df.columns:
        df["direction"] = df["ticker_direction"].map(
            {"BUY": "BUY", "SELL": "SELL", "NEUTRAL": "NEUTRAL"}
        ).fillna("NEUTRAL")
    for col in ("price", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# ReplayDataStore — one instance per trading day
# ═══════════════════════════════════════════════════════════════════════════

class ReplayDataStore:
    """Streams market data JSONL files for one trading day.

    Performance design
    ------------------
    LOB and snapshot records are small and go into a single chronologically
    sorted event stream processed by advance_to().

    Klines and ticks files are large (100s of MB/day) because the live trader
    logs the full historical window on every cycle.  Loading them into the main
    event stream makes the sort and replay very slow.  Instead we build a
    per-code sorted index of (ts, raw_rows) pairs and use bisect for O(log N)
    look-ups — results are byte-for-byte identical to the original design,
    loading is ~10× faster.
    """

    def __init__(self, day_dir: Path) -> None:
        self._day_dir = day_dir
        self._current_ts: str = ""

        # Rolling caches for LOB and snapshots (small files, event-driven)
        self._lob:       dict[str, dict] = {}
        self._snapshots: dict[str, dict] = {}

        # Per-code sorted index: list of (ts_str, raw_rows)
        self._klines_idx: dict[str, list[tuple[str, list[dict]]]] = {}
        self._ticks_idx:  dict[str, list[tuple[str, list[dict]]]] = {}

        # Normalised DataFrame cache — cleared when _current_ts changes
        self._klines_cache: dict[str, pd.DataFrame] = {}
        self._ticks_cache:  dict[str, pd.DataFrame] = {}

        # Main event stream: LOB + snapshots only
        self._events: list[tuple[str, str, dict]] = self._load_lob_snapshot_events()
        self._cursor: int = 0

        self._build_klines_index()
        self._build_ticks_index()

        logger.debug(
            "ReplayDataStore: %s — %d lob/snap events, %d klines codes, %d ticks codes",
            day_dir.name, len(self._events),
            len(self._klines_idx), len(self._ticks_idx),
        )

    # ── loading ─────────────────────────────────────────────────────────────

    def _load_lob_snapshot_events(self) -> list[tuple[str, str, dict]]:
        events: list[tuple[str, str, dict]] = []
        for fname in ("lob", "snapshots"):
            for rec in _load_jsonl(self._day_dir / f"{fname}.jsonl"):
                ts = rec.get("ts", "")
                if ts:
                    events.append((ts, fname, rec))
        events.sort(key=lambda x: x[0])
        return events

    def _build_klines_index(self) -> None:
        for rec in _load_jsonl(self._day_dir / "klines.jsonl"):
            ts = rec.get("ts", ""); code = rec.get("code", ""); rows = rec.get("rows", [])
            if ts and code and rows:
                self._klines_idx.setdefault(code, []).append((ts, rows))
        for lst in self._klines_idx.values():
            lst.sort(key=lambda x: x[0])

    def _build_ticks_index(self) -> None:
        for rec in _load_jsonl(self._day_dir / "ticks.jsonl"):
            ts = rec.get("ts", ""); code = rec.get("code", ""); rows = rec.get("rows", [])
            if ts and code and rows:
                self._ticks_idx.setdefault(code, []).append((ts, rows))
        for lst in self._ticks_idx.values():
            lst.sort(key=lambda x: x[0])

    # ── time advancement ────────────────────────────────────────────────────

    def advance_to(self, ts: str) -> None:
        if ts != self._current_ts:
            self._current_ts = ts
            self._klines_cache.clear()
            self._ticks_cache.clear()
        while self._cursor < len(self._events):
            evt_ts, evt_type, rec = self._events[self._cursor]
            if evt_ts > ts:
                break
            code = rec.get("code", "")
            if code:
                if evt_type == "lob":
                    self._lob[code] = rec
                elif evt_type == "snapshots":
                    self._snapshots[code] = rec.get("data", {})
            self._cursor += 1

    # ── bisect ──────────────────────────────────────────────────────────────

    @staticmethod
    def _bisect(lst: list[tuple[str, list[dict]]], ts: str) -> int:
        lo, hi = 0, len(lst)
        while lo < hi:
            mid = (lo + hi) // 2
            if lst[mid][0] <= ts:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1

    # ── accessors ───────────────────────────────────────────────────────────

    def get_lob(self, code: str) -> dict:
        return self._lob.get(code, {})

    def get_klines(self, code: str) -> pd.DataFrame:
        if code not in self._klines_cache:
            lst = self._klines_idx.get(code, [])
            empty = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
            idx = self._bisect(lst, self._current_ts) if lst else -1
            self._klines_cache[code] = _normalize_klines(lst[idx][1]) if idx >= 0 else empty
        return self._klines_cache[code]

    def get_ticks(self, code: str) -> pd.DataFrame:
        if code not in self._ticks_cache:
            lst = self._ticks_idx.get(code, [])
            idx = self._bisect(lst, self._current_ts) if lst else -1
            self._ticks_cache[code] = _normalize_ticks(lst[idx][1]) if idx >= 0 else pd.DataFrame()
        return self._ticks_cache[code]

    def get_snapshot(self, code: str) -> dict:
        return self._snapshots.get(code, {})

    def all_codes(self) -> set[str]:
        return set(self._snapshots) | set(self._klines_idx) | set(self._lob)

    @property
    def date(self) -> str:
        return self._day_dir.name


# ═══════════════════════════════════════════════════════════════════════════
# ReplayTrader — duck-typed adapter over ReplayDataStore
# ═══════════════════════════════════════════════════════════════════════════

class ReplayTrader:
    """Adapter that presents ReplayDataStore as a live-trader interface.

    Matches the duck-type contract consumed by FusionIntradayStrategy and
    OfimIntradayStrategy without importing either.
    """

    def __init__(self, store: ReplayDataStore, cycle_ts: str) -> None:
        store.advance_to(cycle_ts)
        self._store = store

    # --- live trader interface -----------------------------------------------

    def subscribe_realtime(self, symbols: list[str]) -> None:
        pass  # no-op in replay

    def get_snapshots(self, symbols: list[str]) -> pd.DataFrame:
        rows: dict[str, dict] = {}
        for code in symbols:
            snap = self._store.get_snapshot(code)
            if snap:
                rows[code] = snap
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).T

    def get_recent_klines(self, code: str, limit: int) -> pd.DataFrame:
        df = self._store.get_klines(code)
        return df.tail(limit).reset_index(drop=True)

    def get_recent_tickers(self, code: str, limit: int) -> pd.DataFrame:
        df = self._store.get_ticks(code)
        return df.tail(limit).reset_index(drop=True)

    def get_order_book_safe(self, code: str, depth: int) -> dict:
        lob = self._store.get_lob(code)
        if not lob:
            return {}
        return {
            "Bid": lob.get("bid", [])[:depth],
            "Ask": lob.get("ask", [])[:depth],
        }


# ═══════════════════════════════════════════════════════════════════════════
# Cycle extraction
# ═══════════════════════════════════════════════════════════════════════════

def _get_cycle_timestamps(day_dir: Path) -> list[str]:
    """Return sorted unique cycle timestamps from plan.jsonl for a day."""
    plan_path = day_dir / "plan.jsonl"
    # Archived days are gzipped in place, so plan.jsonl may only exist as
    # plan.jsonl.gz. Check both before falling back, otherwise an archived day
    # silently uses lob cycle boundaries instead of the real plan ones.
    if not plan_path.exists() and not plan_path.with_suffix(".jsonl.gz").exists():
        # Fall back to lob.jsonl cycle boundaries
        plan_path = day_dir / "lob.jsonl"
    tss: list[str] = []
    seen: set[str] = set()
    for rec in _load_jsonl(plan_path):
        ts = rec.get("ts", "")
        if ts and ts not in seen:
            seen.add(ts)
            tss.append(ts)
    return sorted(tss)


# ═══════════════════════════════════════════════════════════════════════════
# Execution simulation helpers
# ═══════════════════════════════════════════════════════════════════════════

def _fill_price(snap: dict, side: str) -> float:
    """Realistic fill price: ask for BUY, bid for SELL."""
    last = float(snap.get("last_price", 0.0) or 0.0)
    if last <= 0:
        return 0.0
    spread = float(snap.get("price_spread", 0.0) or 0.0)
    ask = float(snap.get("ask_price", 0.0) or last + spread / 2)
    bid = float(snap.get("bid_price", 0.0) or last - spread / 2)
    if side == "BUY":
        return ask if ask > 0 else last + spread / 2
    return bid if bid > 0 else last - spread / 2


def _mark_price(snap: dict) -> float:
    return float(snap.get("last_price", 0.0) or 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# Portfolio state
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class _PortfolioState:
    cash: float
    qty: dict[str, float] = field(default_factory=dict)        # code → shares
    pending: dict[str, float] = field(default_factory=dict)    # code → target_weight (next fill)
    entry_cycle: dict[str, int] = field(default_factory=dict)  # code → cycle index of entry (for min-hold)

    def market_value(self, prices: dict[str, float]) -> float:
        equity = sum(self.qty.get(c, 0.0) * p for c, p in prices.items())
        return self.cash + equity

    def execute_pending(
        self,
        next_store: ReplayDataStore,
        cost_model: TradeCostModel | None,
        trade_log_rows: list[dict],
        cycle_ts: str,
        *,
        min_rebalance_drift_pct: float = 0.0,
        min_hold_cycles: int = 0,
        cycle_index: int = 0,
    ) -> None:
        """Execute last cycle's pending orders at this cycle's prices.

        Anti-churn controls (both default 0 = original behavior):
        * ``min_rebalance_drift_pct``: skip a symbol whose target weight is
          within this fraction of its current weight — don't trade tiny drifts.
        * ``min_hold_cycles``: don't sell a position younger than this many
          cycles, to stop rapid in-and-out churn.
        """
        if not self.pending:
            return
        # Current portfolio value for sizing
        current_prices: dict[str, float] = {
            c: _mark_price(next_store.get_snapshot(c))
            for c in set(self.qty) | set(self.pending)
        }
        portfolio_value = self.market_value(current_prices)
        if portfolio_value <= 0:
            self.pending.clear()
            return

        # Sell first, buy second (free up cash)
        target_qty: dict[str, float] = {}
        for code, target_w in self.pending.items():
            price = _fill_price(next_store.get_snapshot(code), "BUY")
            if price <= 0:
                continue
            if min_rebalance_drift_pct > 0:
                cur_w = self.qty.get(code, 0.0) * current_prices.get(code, 0.0) / portfolio_value
                if abs(target_w - cur_w) < min_rebalance_drift_pct:
                    continue  # drift too small — leave this position untouched
            target_qty[code] = math.floor(target_w * portfolio_value / price)

        # --- sells ---
        for code, tgt in target_qty.items():
            current = self.qty.get(code, 0.0)
            delta = tgt - current
            if delta >= 0:
                continue
            if (min_hold_cycles > 0 and code in self.entry_cycle
                    and (cycle_index - self.entry_cycle[code]) < min_hold_cycles):
                continue  # held too briefly — respect the minimum hold
            price = _fill_price(next_store.get_snapshot(code), "SELL")
            if price <= 0:
                continue
            qty = abs(delta)
            notional = qty * price
            if notional < _MIN_ORDER_VALUE_USD and tgt > 0:
                continue
            breakdown = estimate_trade_cost("SELL", qty, price,
                                            timestamp=None, model=cost_model)
            self.cash += notional - breakdown.total
            self.qty[code] = max(0.0, current - qty)
            if self.qty[code] <= 1e-12:
                self.entry_cycle.pop(code, None)
            trade_log_rows.append({
                "ts": cycle_ts, "code": code, "side": "SELL",
                "qty": qty, "price": price, "notional": notional,
                "fees": breakdown.total,
            })

        # --- buys ---
        for code, tgt in target_qty.items():
            current = self.qty.get(code, 0.0)
            delta = tgt - current
            if delta <= 0:
                continue
            price = _fill_price(next_store.get_snapshot(code), "BUY")
            if price <= 0:
                continue
            qty = math.floor(delta)
            if qty <= 0:
                continue
            notional = qty * price
            if notional < _MIN_ORDER_VALUE_USD:
                continue
            if notional > self.cash:
                qty = math.floor(self.cash / price)
                if qty <= 0:
                    continue
                notional = qty * price
            breakdown = estimate_trade_cost("BUY", qty, price,
                                            timestamp=None, model=cost_model)
            if notional + breakdown.total > self.cash:
                continue
            self.cash -= notional + breakdown.total
            if current <= 1e-12:
                self.entry_cycle[code] = cycle_index
            self.qty[code] = current + qty
            trade_log_rows.append({
                "ts": cycle_ts, "code": code, "side": "BUY",
                "qty": qty, "price": price, "notional": notional,
                "fees": breakdown.total,
            })

        self.pending.clear()

    def liquidate(
        self,
        store: ReplayDataStore,
        cost_model: TradeCostModel | None,
        trade_log_rows: list[dict],
        cycle_ts: str,
    ) -> None:
        """Force-sell every open position to cash (used for flat-by-close).

        Sells at the current bid (via _fill_price) and books exit costs, so the
        no-overnight variant pays realistic round-trip fees.
        """
        for code, qty in list(self.qty.items()):
            if qty <= 0:
                continue
            price = _fill_price(store.get_snapshot(code), "SELL")
            if price <= 0:
                continue
            notional = qty * price
            breakdown = estimate_trade_cost("SELL", qty, price, timestamp=None, model=cost_model)
            self.cash += notional - breakdown.total
            self.qty[code] = 0.0
            trade_log_rows.append({
                "ts": cycle_ts, "code": code, "side": "SELL",
                "qty": qty, "price": price, "notional": notional,
                "fees": breakdown.total,
            })
        self.qty = {c: q for c, q in self.qty.items() if q > 1e-12}
        self.pending.clear()


# ═══════════════════════════════════════════════════════════════════════════
# Performance metrics
# ═══════════════════════════════════════════════════════════════════════════

def _performance_summary(equity: pd.Series, trade_log: pd.DataFrame) -> dict[str, float]:
    if equity.empty:
        return {}
    returns = equity.pct_change().dropna()
    vol = float(returns.std()) * math.sqrt(252 * 6.5 * 60)  # intraday annualised
    mean_ret = float(returns.mean())
    sharpe = mean_ret / vol * math.sqrt(252 * 6.5 * 60) if vol > 0 else 0.0
    total_ret = float(equity.iloc[-1] / equity.iloc[0] - 1) if equity.iloc[0] > 0 else 0.0
    running_max = equity.cummax()
    dd = float(((equity - running_max) / running_max).min()) if not running_max.empty else 0.0
    total_fees = float(trade_log["fees"].sum()) if not trade_log.empty and "fees" in trade_log.columns else 0.0
    return {
        "total_return":  round(total_ret,  6),
        "annualised_vol": round(vol,        6),
        "sharpe":         round(sharpe,     4),
        "max_drawdown":   round(dd,         6),
        "total_fees_usd": round(total_fees, 2),
        "n_trades":       len(trade_log),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Core replay engine  (strategy-agnostic)
# ═══════════════════════════════════════════════════════════════════════════

GeneratePlanFn = Callable[[ReplayTrader, set[str]], Any]
"""Callable matching (trader, held_symbols) → plan with .target_weights dict."""

ReplayProgressFn = Callable[[dict[str, Any]], None]
"""Callback receiving replay progress payloads."""


def run_intraday_replay(
    generate_plan_fn: GeneratePlanFn,
    start: str,
    end: str,
    *,
    initial_capital: float = 1_000_000.0,
    cost_model: TradeCostModel | None = None,
    suppress_logging: bool = True,
    progress_callback: ReplayProgressFn | None = None,
    flat_by_close: bool = False,
    min_rebalance_drift_pct: float = 0.0,
    min_hold_cycles: int = 0,
) -> IntradayReplayResult:
    """Replay an intraday strategy using stored real market data.

    flat_by_close: if True, force-liquidate all positions at the end of each
    trading day, so nothing is held overnight (the no-overnight variant).
    min_rebalance_drift_pct / min_hold_cycles: anti-churn execution controls
    (both default 0 = original behavior); see _PortfolioState.execute_pending.

    Parameters
    ----------
    generate_plan_fn:
        Callable ``(trader: ReplayTrader, held_symbols: set[str]) → plan``
        where ``plan.target_weights`` is ``dict[str, float]``.
        Typically ``FusionIntradayStrategy(settings).generate_plan`` or
        ``OfimIntradayStrategy(settings)._generate_plan_inner``.
    start / end:
        Date strings (ISO format, e.g. "2026-04-01").
    initial_capital:
        Starting portfolio value in USD.
    cost_model:
        Fee model; None uses the default TradeCostModel.
    suppress_logging:
        Silence market_logger writes during replay (avoids polluting live files).
    """
    day_dirs = _iter_day_dirs(start, end)
    if not day_dirs:
        logger.warning("run_intraday_replay: no market data found for %s … %s", start, end)
        return IntradayReplayResult(
            equity_curve=pd.Series(dtype=float),
            trade_log=pd.DataFrame(),
            plan_log=[],
            summary={},
        )

    portfolio = _PortfolioState(cash=float(initial_capital))
    equity_ts: list[str]   = []
    equity_vals: list[float] = []
    trade_log_rows: list[dict] = []
    plan_log: list[dict] = []
    started_at = time.perf_counter()
    total_days = len(day_dirs)

    # cost_model=None means fees are skipped — callers can pass
    # build_trade_cost_model(settings) for realistic fee simulation.

    # Suppress market_logger writes so replay doesn't write new files
    _log_patch = _build_log_suppressor() if suppress_logging else None

    try:
        if _log_patch:
            _log_patch.__enter__()

        prev_store: ReplayDataStore | None = None
        global_cycle = 0

        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "start",
                    "completed_days": 0,
                    "total_days": total_days,
                    "current_day": None,
                    "elapsed_seconds": 0.0,
                    "progress": 0.0,
                }
            )

        for day_index, day_dir in enumerate(day_dirs, start=1):
            logger.info("Replaying %s …", day_dir.name)
            store = ReplayDataStore(day_dir)
            cycle_tss = _get_cycle_timestamps(day_dir)
            if not cycle_tss:
                if progress_callback is not None:
                    elapsed = time.perf_counter() - started_at
                    progress_callback(
                        {
                            "phase": "day_complete",
                            "completed_days": day_index,
                            "total_days": total_days,
                            "current_day": day_dir.name,
                            "elapsed_seconds": elapsed,
                            "progress": day_index / total_days if total_days else 1.0,
                        }
                    )
                continue

            for idx, ts in enumerate(cycle_tss):
                global_cycle += 1
                # Execute last cycle's pending orders at this cycle's price
                if prev_store is not None and portfolio.pending:
                    portfolio.execute_pending(
                        store, cost_model, trade_log_rows, ts,
                        min_rebalance_drift_pct=min_rebalance_drift_pct,
                        min_hold_cycles=min_hold_cycles, cycle_index=global_cycle,
                    )
                elif idx > 0 and portfolio.pending:
                    # within same day — use current store but advanced to this ts
                    store.advance_to(ts)
                    portfolio.execute_pending(
                        store, cost_model, trade_log_rows, ts,
                        min_rebalance_drift_pct=min_rebalance_drift_pct,
                        min_hold_cycles=min_hold_cycles, cycle_index=global_cycle,
                    )

                # Build replay trader for this cycle
                trader = ReplayTrader(store, ts)
                held_symbols: set[str] = {
                    code for code, qty in portfolio.qty.items() if qty > 0
                }

                try:
                    plan = generate_plan_fn(trader, held_symbols)
                    target_weights = plan.target_weights if hasattr(plan, "target_weights") else {}
                except Exception as exc:
                    logger.warning("generate_plan failed at %s: %s", ts, exc)
                    target_weights = {}

                plan_log.append({"ts": ts, "target_weights": dict(target_weights)})

                # Queue orders for next cycle
                portfolio.pending = dict(target_weights)

                # Mark-to-market equity
                current_prices: dict[str, float] = {
                    c: _mark_price(store.get_snapshot(c))
                    for c in set(portfolio.qty) | set(target_weights)
                    if _mark_price(store.get_snapshot(c)) > 0
                }
                equity_ts.append(ts)
                equity_vals.append(portfolio.market_value(current_prices))

            if flat_by_close and cycle_tss:
                store.advance_to(cycle_tss[-1])
                portfolio.liquidate(store, cost_model, trade_log_rows, cycle_tss[-1])

            prev_store = store
            if progress_callback is not None:
                elapsed = time.perf_counter() - started_at
                progress_callback(
                    {
                        "phase": "day_complete",
                        "completed_days": day_index,
                        "total_days": total_days,
                        "current_day": day_dir.name,
                        "elapsed_seconds": elapsed,
                        "progress": day_index / total_days if total_days else 1.0,
                    }
                )

        # Execute any remaining pending orders at end of last day
        if portfolio.pending and equity_ts:
            last_store = ReplayDataStore(day_dirs[-1])
            last_store.advance_to(equity_ts[-1])
            portfolio.execute_pending(
                last_store, cost_model, trade_log_rows, equity_ts[-1],
                min_rebalance_drift_pct=min_rebalance_drift_pct,
                min_hold_cycles=min_hold_cycles, cycle_index=global_cycle,
            )
        if progress_callback is not None:
            elapsed = time.perf_counter() - started_at
            progress_callback(
                {
                    "phase": "complete",
                    "completed_days": total_days,
                    "total_days": total_days,
                    "current_day": day_dirs[-1].name if day_dirs else None,
                    "elapsed_seconds": elapsed,
                    "progress": 1.0,
                }
            )

    finally:
        if _log_patch:
            _log_patch.__exit__(None, None, None)

    equity_curve = pd.Series(
        equity_vals,
        index=pd.to_datetime(equity_ts, utc=True),
        dtype=float,
        name="portfolio_value",
    ).sort_index()

    trade_log = pd.DataFrame(trade_log_rows)
    if not trade_log.empty:
        trade_log["ts"] = pd.to_datetime(trade_log["ts"], utc=True, errors="coerce")

    summary = _performance_summary(equity_curve, trade_log)

    return IntradayReplayResult(
        equity_curve=equity_curve,
        trade_log=trade_log,
        plan_log=plan_log,
        summary=summary,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Log suppressor  (prevents replay from writing to live market_data files)
# ═══════════════════════════════════════════════════════════════════════════

def _build_log_suppressor():
    """Return a context manager that no-ops all market_logger write calls."""
    from contextlib import contextmanager
    from unittest.mock import patch
    from . import market_logger

    @contextmanager
    def _suppress():
        noop = lambda *a, **kw: None
        with (
            patch.object(market_logger, "log_lob",      noop),
            patch.object(market_logger, "log_ticks",    noop),
            patch.object(market_logger, "log_klines",   noop),
            patch.object(market_logger, "log_snapshot", noop),
            patch.object(market_logger, "log_feature",  noop),
            patch.object(market_logger, "log_plan",     noop),
            patch.object(market_logger, "log_orders",   noop),
        ):
            yield

    return _suppress()


# ═══════════════════════════════════════════════════════════════════════════
# Strategy-specific convenience wrappers
# ═══════════════════════════════════════════════════════════════════════════

def run_fusion_replay(
    start: str,
    end: str,
    settings,
    *,
    initial_capital: float = 1_000_000.0,
    cost_model: TradeCostModel | None = None,
    progress_callback: ReplayProgressFn | None = None,
) -> IntradayReplayResult:
    """Replay Fusion strategy using stored real 40-level LOB data.

    Parameters
    ----------
    settings:
        taa_futu Settings object (loaded from .env).
    """
    from .fusion_intraday import FusionIntradayStrategy  # local import: low coupling
    strategy = FusionIntradayStrategy(settings)
    return run_intraday_replay(
        strategy.generate_plan,
        start, end,
        initial_capital=initial_capital,
        cost_model=cost_model,
        progress_callback=progress_callback,
    )


def run_ofim_replay(
    start: str,
    end: str,
    settings,
    *,
    initial_capital: float = 1_000_000.0,
    cost_model: TradeCostModel | None = None,
    progress_callback: ReplayProgressFn | None = None,
    flat_by_close: bool = False,
    min_rebalance_drift_pct: float = 0.0,
    min_hold_cycles: int = 0,
) -> IntradayReplayResult:
    """Replay OFIM strategy using stored real 40-level LOB data.

    Note: crypto scoring (Binance LOB) is skipped in replay — only the
    equity portion of OFIM is replayed from stored Futu data.  Pure-equity
    replay is still far more accurate than the daily-bar backtest.
    """
    from .ofim_intraday import OfimIntradayStrategy  # local import: low coupling
    replay_settings = replace(
        settings,
        ofim_crypto_universe=(),
        ofim_crypto_to_proxy=(),
        ofim_crypto_api_key=None,
        ofim_crypto_api_secret=None,
        ofim_crypto_sandbox=False,
    )
    strategy = OfimIntradayStrategy(replay_settings)

    return run_intraday_replay(
        strategy.generate_plan,
        start, end,
        initial_capital=initial_capital,
        cost_model=cost_model,
        progress_callback=progress_callback,
        flat_by_close=flat_by_close,
        min_rebalance_drift_pct=min_rebalance_drift_pct,
        min_hold_cycles=min_hold_cycles,
    )


# ═══════════════════════════════════════════════════════════════════════════
# CLI convenience  (python -m taa_futu.intraday_replay)
# ═══════════════════════════════════════════════════════════════════════════

def _cli() -> None:
    import argparse
    from .config import load_settings

    parser = argparse.ArgumentParser(description="Intraday replay backtest")
    parser.add_argument("strategy", choices=["fusion", "ofim"], help="Strategy to replay")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end",   required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()

    settings = load_settings(args.env)

    if args.strategy == "fusion":
        result = run_fusion_replay(args.start, args.end, settings,
                                   initial_capital=args.capital)
    else:
        result = run_ofim_replay(args.start, args.end, settings,
                                 initial_capital=args.capital)

    print("\n=== Replay Result ===")
    for k, v in result.summary.items():
        if isinstance(v, float):
            print(f"  {k:25s}: {v:.4f}")
        else:
            print(f"  {k:25s}: {v}")

    if not result.trade_log.empty:
        print(f"\nTrade log ({len(result.trade_log)} fills):")
        print(result.trade_log.to_string(index=False))


if __name__ == "__main__":
    _cli()
