from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Iterable
from urllib.parse import urlencode

from dotenv import load_dotenv
import pandas as pd
import requests

from .ofim_intraday import (
    _clip,
    _compute_benchmark_score,
    compute_micro_momentum,
    compute_multi_level_ofi,
    compute_spread_quality,
    compute_tick_aggression,
    compute_volume_acceleration,
    compute_vwap_deviation,
)


FUTURES_MAINNET_BASE_URL = "https://fapi.binance.com"
FUTURES_TESTNET_BASE_URL = "https://demo-fapi.binance.com"
BINANCE_USDM_COMMISSION_ENDPOINT_SOURCE = "binance_usdm_commission_rate_endpoint"
BINANCE_USDM_CONFIGURED_TAKER_RATE_SOURCE = "configured_usdm_taker_rate"
DEFAULT_USDM_TAKER_FEE_RATE = 0.0004
DEFAULT_USDM_MAKER_FEE_RATE = 0.0002
DEFAULT_PERP_MIN_EDGE_COST_RATIO = 2.0
DEFAULT_PERP_LOSS_GUARD_MAX_FEES = 10.0
DEFAULT_PERP_LOSS_GUARD_MAX_TRADES = 120
DEFAULT_PERP_SYMBOL_LOSS_GUARD_MAX_FEES = 5.0
DEFAULT_PERP_SYMBOL_LOSS_GUARD_MAX_TRADES = 60
DEFAULT_PERP_LOSS_GUARD_IDLE_POLL_SECONDS = 300
MAX_PERP_LOSS_GUARD_IDLE_POLL_SECONDS = 3600
RUNTIME_DIR = Path(__file__).resolve().parents[2] / "runtime" / "crypto_perp"
STATUS_FILE = RUNTIME_DIR / "status.json"
STATE_FILE = RUNTIME_DIR / "paper_state.json"
TESTNET_STATE_FILE = RUNTIME_DIR / "testnet_local_state.json"
ORDERS_FILE = RUNTIME_DIR / "orders.jsonl"
FEATURES_FILE = RUNTIME_DIR / "features.jsonl"
EVENTS_FILE = RUNTIME_DIR / "events.jsonl"
AUTO_PID_FILE = RUNTIME_DIR / "auto.pid"
AUTO_LOCK_FILE = RUNTIME_DIR / "auto.lock"
AUTO_LOG_FILE = RUNTIME_DIR / "auto.log"
DEFAULT_PERP_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def crypto_perp_guarded_idle_poll_seconds(payload: dict[str, Any], requested_interval: int | float) -> int:
    """Back off auto polling while loss guards are blocking all perp work."""
    try:
        base_interval = max(5, int(requested_interval))
    except (TypeError, ValueError):
        base_interval = 60
    if not isinstance(payload, dict):
        return base_interval

    context = payload.get("benchmark_context")
    if not isinstance(context, dict):
        context = {}
    reason = str(payload.get("reason") or context.get("reason") or "")
    if not reason.startswith("perp_loss_guard"):
        return base_interval
    if (
        payload.get("target_weights")
        or payload.get("planned_orders")
        or payload.get("submitted_orders")
        or payload.get("pending_order_updates")
    ):
        return base_interval

    raw_idle = os.getenv("CRYPTO_PERP_LOSS_GUARD_IDLE_POLL_SECONDS")
    try:
        idle_interval = int(raw_idle) if raw_idle is not None else DEFAULT_PERP_LOSS_GUARD_IDLE_POLL_SECONDS
    except ValueError:
        idle_interval = DEFAULT_PERP_LOSS_GUARD_IDLE_POLL_SECONDS
    idle_interval = min(MAX_PERP_LOSS_GUARD_IDLE_POLL_SECONDS, max(60, idle_interval))
    return max(base_interval, idle_interval)


def _parse_symbols(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return DEFAULT_PERP_SYMBOLS
    symbols = tuple(part.strip().upper().replace("/", "") for part in raw.split(",") if part.strip())
    return symbols or DEFAULT_PERP_SYMBOLS


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_signal_confirm_streak(raw: Any) -> dict[str, dict[str, int]]:
    if not isinstance(raw, dict):
        return {}
    parsed: dict[str, dict[str, int]] = {}
    for symbol, row in raw.items():
        if not isinstance(row, dict):
            continue
        direction = _safe_int(row.get("direction"))
        count = max(0, _safe_int(row.get("count")))
        if direction in {-1, 1} and count > 0:
            parsed[str(symbol)] = {"direction": direction, "count": count}
    return parsed


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _positive_min(*values: Decimal) -> Decimal:
    positives = [value for value in values if value > 0]
    return min(positives) if positives else Decimal("0")


def _positive_max(*values: Decimal) -> Decimal:
    positives = [value for value in values if value > 0]
    return max(positives) if positives else Decimal("0")


def _decimal_to_api_text(value: Decimal) -> str:
    text = format(value, "f").rstrip("0").rstrip(".")
    return text or "0"


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _write_status(payload: dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_crypto_perp_status() -> dict[str, Any]:
    if not STATUS_FILE.exists():
        return {"status": "not_started"}
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "corrupt_status"}


def _read_jsonl_tail(path: Path, *, tail: int = 20) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-tail:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _read_pid_file(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return True
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "stat="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return True
    if result.returncode != 0:
        return True
    status = result.stdout.strip()
    if not status:
        return True
    return bool(status) and not status.upper().startswith("Z")


class CryptoPerpAutoInstance:
    """Own the singleton runtime lock for the crypto perp auto loop."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self._lock_handle: Any | None = None

    def __enter__(self) -> "CryptoPerpAutoInstance":
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        existing_pid = _read_pid_file(AUTO_PID_FILE)
        if existing_pid and existing_pid != self.pid and _pid_running(existing_pid):
            raise CryptoPerpError(f"Crypto perp auto is already running with pid {existing_pid}.")
        if existing_pid and existing_pid != self.pid and AUTO_PID_FILE.exists():
            AUTO_PID_FILE.unlink()

        self._lock_handle = AUTO_LOCK_FILE.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_handle.seek(0)
            holder = self._lock_handle.read().strip()
            self._lock_handle.close()
            self._lock_handle = None
            suffix = f" with pid {holder}" if holder else ""
            raise CryptoPerpError(f"Crypto perp auto is already running{suffix}.") from exc

        existing_pid = _read_pid_file(AUTO_PID_FILE)
        if existing_pid and existing_pid != self.pid and _pid_running(existing_pid):
            self._release_lock()
            raise CryptoPerpError(f"Crypto perp auto is already running with pid {existing_pid}.")

        self._lock_handle.seek(0)
        self._lock_handle.truncate()
        self._lock_handle.write(str(self.pid))
        self._lock_handle.flush()
        os.fsync(self._lock_handle.fileno())
        AUTO_PID_FILE.write_text(str(self.pid), encoding="utf-8")
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if _read_pid_file(AUTO_PID_FILE) == self.pid and AUTO_PID_FILE.exists():
            AUTO_PID_FILE.unlink()
        self._release_lock()

    def _release_lock(self) -> None:
        if self._lock_handle is None:
            return
        try:
            self._lock_handle.seek(0)
            self._lock_handle.truncate()
            self._lock_handle.flush()
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_handle.close()
            self._lock_handle = None


def crypto_perp_auto_instance() -> CryptoPerpAutoInstance:
    return CryptoPerpAutoInstance()


def _state_file_for(settings: "CryptoPerpSettings") -> Path:
    return TESTNET_STATE_FILE if settings.mode == "testnet" else STATE_FILE


@dataclass(frozen=True)
class BinanceFuturesTradeRules:
    min_qty: Decimal
    max_qty: Decimal
    step_size: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class CryptoPerpSettings:
    mode: str
    base_url: str
    market_data_base_url: str
    api_key: str | None
    api_secret: str | None
    symbols: tuple[str, ...]
    benchmark: str
    quote_asset: str
    initial_cash: float
    active_capital: float
    active_capital_pct: float
    lookback_bars: int
    depth_limit: int
    trade_limit: int
    entry_threshold: float
    exit_threshold: float
    max_score: float
    min_vol_acceleration: float
    max_spread_bps: float
    max_abs_position_weight: float
    max_gross_exposure: float
    max_positions: int
    min_order_notional: float
    max_order_notional: float
    rebalance_threshold: float
    min_trade_interval_seconds: int
    leverage: int
    margin_type: str
    fee_rate: float
    maker_fee_rate: float
    slippage_bps: float
    order_style: str
    maker_order_ttl_seconds: int
    maker_price_offset_bps: float
    recv_window_ms: int
    testnet_validate_only: bool
    max_order_book_take_ratio: float = 0.20
    exit_confirm_cycles: int = 2
    signal_confirm_cycles: int = 3
    require_edge_over_cost: bool = True
    edge_bps_per_score: float = 60.0
    cost_buffer_bps: float = 6.0
    min_edge_cost_ratio: float = DEFAULT_PERP_MIN_EDGE_COST_RATIO
    hawkes_weight: float = 0.10
    min_hawkes_imbalance: float = 0.08
    cross_asset_ofi_weight: float = 0.15
    funding_interval_seconds: int = 28_800
    max_adverse_funding_rate: float = 0.0005
    maintenance_margin_rate: float = 0.005
    loss_guard_max_loss: float = 50.0
    loss_guard_max_fees: float = DEFAULT_PERP_LOSS_GUARD_MAX_FEES
    loss_guard_max_trades: int = DEFAULT_PERP_LOSS_GUARD_MAX_TRADES
    loss_guard_recent_window_seconds: int = 900
    loss_guard_max_recent_trades: int = 8
    loss_guard_max_recent_flips: int = 3
    loss_guard_symbol_max_loss: float = 15.0
    loss_guard_symbol_max_fees: float = DEFAULT_PERP_SYMBOL_LOSS_GUARD_MAX_FEES
    loss_guard_symbol_max_trades: int = DEFAULT_PERP_SYMBOL_LOSS_GUARD_MAX_TRADES

    @property
    def submit_label(self) -> str:
        if self.mode == "paper":
            return "LOCAL PERP PAPER"
        if self.testnet_validate_only:
            return "BINANCE USD-M FUTURES TESTNET VALIDATE_ONLY"
        return "BINANCE USD-M FUTURES TESTNET"

    @property
    def market_data_label(self) -> str:
        return "BINANCE USD-M FUTURES MAINNET PUBLIC"

    @property
    def signed_account_enabled(self) -> bool:
        return bool(self.api_key and self.api_secret)


@dataclass(frozen=True)
class CryptoPerpFeature:
    symbol: str
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
    abs_score: float
    conviction: float
    signal: str
    eligible: bool
    reason: str
    hawkes_imbalance: float = 0.0
    cross_asset_leader_score: float = 0.0


@dataclass(frozen=True)
class CryptoPerpPlan:
    mode: str
    benchmark: str
    benchmark_score: float
    gross_exposure: float
    target_weights: dict[str, float]
    features: list[CryptoPerpFeature]
    market_sources: dict[str, str] | None = None
    reason: str = "ok"
    benchmark_context: dict[str, Any] | None = None


@dataclass(frozen=True)
class CryptoPerpOrder:
    ts: str
    mode: str
    symbol: str
    side: str
    quantity: float
    price: float
    notional: float
    fee: float
    status: str
    reason: str
    reduce_only: bool
    target_weight: float
    current_value: float
    target_value: float
    leverage: int
    margin_type: str
    order_type: str = "MARKET"
    time_in_force: str | None = None
    response: dict[str, Any] | None = None


@dataclass
class CryptoPerpPaperState:
    cash: float
    positions: dict[str, float]
    avg_entry: dict[str, float]
    realized_pnl: float
    fees_paid: float
    funding_paid: float
    last_order_books: dict[str, dict[str, list[list[float]]]]
    last_trade_ts: dict[str, float]
    last_funding_ts: dict[str, float]
    exit_signal_streak: dict[str, int]
    signal_confirm_streak: dict[str, dict[str, int]]
    pending_orders: list[dict[str, Any]]
    created_at: str
    updated_at: str

    @classmethod
    def fresh(cls, settings: CryptoPerpSettings) -> "CryptoPerpPaperState":
        now = _utc_now()
        return cls(
            cash=float(settings.initial_cash),
            positions={},
            avg_entry={},
            realized_pnl=0.0,
            fees_paid=0.0,
            funding_paid=0.0,
            last_order_books={},
            last_trade_ts={},
            last_funding_ts={},
            exit_signal_streak={},
            signal_confirm_streak={},
            pending_orders=[],
            created_at=now,
            updated_at=now,
        )

    @classmethod
    def load(cls, settings: CryptoPerpSettings) -> "CryptoPerpPaperState":
        state_file = _state_file_for(settings)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        if not state_file.exists():
            state = cls.fresh(settings)
            state.save(settings)
            return state
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        return cls(
            cash=_safe_float(raw.get("cash"), settings.initial_cash),
            positions={str(k): _safe_float(v) for k, v in (raw.get("positions") or {}).items() if abs(_safe_float(v)) > 0},
            avg_entry={str(k): _safe_float(v) for k, v in (raw.get("avg_entry") or {}).items()},
            realized_pnl=_safe_float(raw.get("realized_pnl")),
            fees_paid=_safe_float(raw.get("fees_paid")),
            funding_paid=_safe_float(raw.get("funding_paid")),
            last_order_books=raw.get("last_order_books") or {},
            last_trade_ts={str(k): _safe_float(v) for k, v in (raw.get("last_trade_ts") or {}).items()},
            last_funding_ts={str(k): _safe_float(v) for k, v in (raw.get("last_funding_ts") or {}).items()},
            exit_signal_streak={str(k): max(0, _safe_int(v)) for k, v in (raw.get("exit_signal_streak") or {}).items()},
            signal_confirm_streak=_parse_signal_confirm_streak(raw.get("signal_confirm_streak")),
            pending_orders=[dict(row) for row in (raw.get("pending_orders") or []) if isinstance(row, dict)],
            created_at=str(raw.get("created_at") or _utc_now()),
            updated_at=str(raw.get("updated_at") or _utc_now()),
        )

    def save(self, settings: CryptoPerpSettings | None = None) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        self.updated_at = _utc_now()
        state_file = _state_file_for(settings) if settings is not None else STATE_FILE
        state_file.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")


class CryptoPerpError(RuntimeError):
    pass


class BinanceUsdMFuturesClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        recv_window_ms: int = 5000,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.recv_window_ms = recv_window_ms
        self.session = session or requests.Session()
        self._exchange_info_cache: dict[str, Any] | None = None
        self._rules_cache: dict[str, BinanceFuturesTradeRules] = {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        params = dict(params or {})
        headers: dict[str, str] = {}
        if signed:
            if not self.api_key or not self.api_secret:
                raise CryptoPerpError("Binance USD-M Futures signed request requires CRYPTO_PERP_API_KEY and CRYPTO_PERP_API_SECRET.")
            params.setdefault("recvWindow", self.recv_window_ms)
            params["timestamp"] = int(time.time() * 1000)
            query = urlencode(params, doseq=True)
            signature = hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
            params["signature"] = signature
            headers["X-MBX-APIKEY"] = self.api_key
        elif self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        attempts = 2 if method.upper() == "GET" else 1
        response = None
        last_exc: requests.RequestException | None = None
        for attempt in range(attempts):
            try:
                response = self.session.request(method, f"{self.base_url}{path}", params=params, headers=headers, timeout=15)
                break
            except requests.RequestException as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(0.35 * (attempt + 1))
        if response is None:
            raise CryptoPerpError(f"Binance USD-M Futures network error while calling {method} {path}: {last_exc}") from last_exc
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise CryptoPerpError(f"Binance USD-M Futures {method} {path} failed: {response.status_code} {detail}")
        try:
            return response.json()
        except ValueError:
            return response.text

    def ping(self) -> bool:
        self._request("GET", "/fapi/v1/ping")
        return True

    def server_time(self) -> int:
        data = self._request("GET", "/fapi/v1/time")
        return int(data.get("serverTime", 0))

    def exchange_info(self) -> dict[str, Any]:
        if self._exchange_info_cache is None:
            self._exchange_info_cache = self._request("GET", "/fapi/v1/exchangeInfo")
        return self._exchange_info_cache

    def exchange_symbols(self) -> set[str]:
        return {
            str(row.get("symbol") or "").upper()
            for row in self.exchange_info().get("symbols", [])
            if str(row.get("symbol") or "").strip()
            and str(row.get("status") or "TRADING").upper() == "TRADING"
            and str(row.get("contractType") or "PERPETUAL").upper() == "PERPETUAL"
        }

    def symbol_trade_rules(self, symbol: str) -> BinanceFuturesTradeRules:
        symbol = symbol.upper()
        if symbol in self._rules_cache:
            return self._rules_cache[symbol]
        rows = [row for row in self.exchange_info().get("symbols", []) if str(row.get("symbol") or "").upper() == symbol]
        if not rows:
            data = self._request("GET", "/fapi/v1/exchangeInfo", params={"symbol": symbol})
            rows = data.get("symbols") or []
        if not rows:
            raise CryptoPerpError(f"Binance futures exchangeInfo returned no symbol rules for {symbol}.")
        filters = {str(item.get("filterType")): item for item in rows[0].get("filters", [])}
        lot = filters.get("LOT_SIZE") or {}
        market_lot = filters.get("MARKET_LOT_SIZE") or {}
        min_notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
        lot_min_qty = _to_decimal(lot.get("minQty"))
        market_min_qty = _to_decimal(market_lot.get("minQty"))
        lot_max_qty = _to_decimal(lot.get("maxQty"))
        market_max_qty = _to_decimal(market_lot.get("maxQty"))
        lot_step = _to_decimal(lot.get("stepSize"))
        market_step = _to_decimal(market_lot.get("stepSize"))
        rules = BinanceFuturesTradeRules(
            min_qty=_positive_max(lot_min_qty, market_min_qty),
            max_qty=_positive_min(lot_max_qty, market_max_qty),
            step_size=market_step if market_step > 0 else lot_step,
            min_notional=_to_decimal(min_notional_filter.get("notional") or min_notional_filter.get("minNotional")),
        )
        self._rules_cache[symbol] = rules
        return rules

    def normalize_market_quantity(self, symbol: str, quantity: float, price: float) -> tuple[Decimal, str, str | None]:
        rules = self.symbol_trade_rules(symbol)
        qty = _to_decimal(quantity)
        order_price = _to_decimal(price)
        if qty <= 0:
            return Decimal("0"), "0", "quantity_zero"
        if rules.max_qty > 0 and qty > rules.max_qty:
            qty = rules.max_qty
        qty = _floor_to_step(qty, rules.step_size)
        if qty <= 0:
            return Decimal("0"), "0", "quantity_below_step"
        if rules.min_qty > 0 and qty < rules.min_qty:
            return qty, _decimal_to_api_text(qty), f"quantity_below_min_qty:{rules.min_qty}"
        if rules.min_notional > 0 and order_price > 0 and qty * order_price < rules.min_notional:
            return qty, _decimal_to_api_text(qty), f"notional_below_min_notional:{rules.min_notional}"
        return qty, _decimal_to_api_text(qty), None

    def klines(self, symbol: str, *, interval: str = "1m", limit: int = 60) -> pd.DataFrame:
        rows = self._request("GET", "/fapi/v1/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
        frame = pd.DataFrame(
            rows,
            columns=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_volume",
                "trade_count",
                "taker_buy_base",
                "taker_buy_quote",
                "ignore",
            ],
        )
        if frame.empty:
            return pd.DataFrame(columns=["time_key", "open", "high", "low", "close", "volume"])
        for col in ["open", "high", "low", "close", "volume"]:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame["time_key"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True).dt.strftime("%Y-%m-%d %H:%M:%S")
        return frame[["time_key", "open", "high", "low", "close", "volume"]].dropna()

    def depth(self, symbol: str, *, limit: int = 100) -> dict[str, list[list[float]]]:
        data = self._request("GET", "/fapi/v1/depth", params={"symbol": symbol, "limit": limit})
        return {
            "Bid": [[_safe_float(price), _safe_float(qty)] for price, qty in data.get("bids", [])],
            "Ask": [[_safe_float(price), _safe_float(qty)] for price, qty in data.get("asks", [])],
        }

    def recent_trades(self, symbol: str, *, limit: int = 100) -> pd.DataFrame:
        rows = self._request("GET", "/fapi/v1/trades", params={"symbol": symbol, "limit": limit})
        out: list[dict[str, Any]] = []
        for row in rows:
            direction = "SELL" if row.get("isBuyerMaker") else "BUY"
            out.append({"price": _safe_float(row.get("price")), "volume": _safe_float(row.get("qty")), "ticker_direction": direction, "ts_ms": _safe_int(row.get("time"))})
        return pd.DataFrame(out, columns=["price", "volume", "ticker_direction", "ts_ms"])

    def book_ticker(self, symbol: str) -> pd.Series:
        data = self._request("GET", "/fapi/v1/ticker/bookTicker", params={"symbol": symbol})
        bid = _safe_float(data.get("bidPrice"))
        ask = _safe_float(data.get("askPrice"))
        last = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
        return pd.Series({"last_price": last, "bid_price": bid, "ask_price": ask})

    def premium_index(self, symbol: str) -> dict[str, Any]:
        return self._request("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol})

    def account(self) -> dict[str, Any]:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else None
        rows = self._request("GET", "/fapi/v2/positionRisk", params=params, signed=True)
        return rows if isinstance(rows, list) else []

    def commission_rate(self, symbol: str) -> dict[str, Any]:
        return self._request("GET", "/fapi/v1/commissionRate", params={"symbol": symbol.upper()}, signed=True)

    def income_history(self, *, income_type: str, start_time_ms: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"incomeType": income_type, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        rows = self._request("GET", "/fapi/v1/income", params=params, signed=True)
        return rows if isinstance(rows, list) else []

    def change_margin_type(self, symbol: str, margin_type: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/fapi/v1/marginType",
            params={"symbol": symbol.upper(), "marginType": margin_type.upper()},
            signed=True,
        )

    def change_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return self._request(
            "POST",
            "/fapi/v1/leverage",
            params={"symbol": symbol.upper(), "leverage": int(leverage)},
            signed=True,
        )

    def market_order(
        self,
        symbol: str,
        side: str,
        *,
        quantity: float | Decimal | str,
        reduce_only: bool = False,
        validate_only: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "MARKET",
            "quantity": quantity if isinstance(quantity, str) else _decimal_to_api_text(_to_decimal(quantity)),
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        path = "/fapi/v1/order/test" if validate_only else "/fapi/v1/order"
        return self._request("POST", path, params=params, signed=True)

    def limit_order(
        self,
        symbol: str,
        side: str,
        *,
        quantity: float | Decimal | str,
        price: float | Decimal | str,
        reduce_only: bool = False,
        post_only: bool = True,
        validate_only: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "LIMIT",
            "timeInForce": "GTX" if post_only else "GTC",
            "quantity": quantity if isinstance(quantity, str) else _decimal_to_api_text(_to_decimal(quantity)),
            "price": price if isinstance(price, str) else _decimal_to_api_text(_to_decimal(price)),
            "newOrderRespType": "ACK",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        path = "/fapi/v1/order/test" if validate_only else "/fapi/v1/order"
        return self._request("POST", path, params=params, signed=True)


def load_crypto_perp_settings(env_file: str | Path = ".env") -> CryptoPerpSettings:
    load_dotenv(dotenv_path=env_file, override=True)
    mode = os.getenv("CRYPTO_PERP_MODE", "paper").strip().lower()
    if mode not in {"paper", "testnet"}:
        raise ValueError("CRYPTO_PERP_MODE must be paper or testnet. Live USD-M futures trading is intentionally not enabled.")
    margin_type = os.getenv("CRYPTO_PERP_MARGIN_TYPE", "ISOLATED").strip().upper()
    if margin_type not in {"ISOLATED", "CROSSED"}:
        raise ValueError("CRYPTO_PERP_MARGIN_TYPE must be ISOLATED or CROSSED.")
    order_style = os.getenv("CRYPTO_PERP_ORDER_STYLE", "maker_limit").strip().lower()
    if order_style not in {"maker_limit", "market"}:
        raise ValueError("CRYPTO_PERP_ORDER_STYLE must be maker_limit or market.")
    return CryptoPerpSettings(
        mode=mode,
        base_url=FUTURES_TESTNET_BASE_URL if mode == "testnet" else FUTURES_MAINNET_BASE_URL,
        market_data_base_url=FUTURES_MAINNET_BASE_URL,
        api_key=os.getenv("CRYPTO_PERP_API_KEY") or None,
        api_secret=os.getenv("CRYPTO_PERP_API_SECRET") or None,
        symbols=_parse_symbols(os.getenv("CRYPTO_PERP_SYMBOLS")),
        benchmark=os.getenv("CRYPTO_PERP_BENCHMARK", "BTCUSDT").strip().upper().replace("/", ""),
        quote_asset=os.getenv("CRYPTO_PERP_QUOTE_ASSET", "USDT").strip().upper(),
        initial_cash=float(os.getenv("CRYPTO_PERP_INITIAL_CASH", "10000")),
        active_capital=float(os.getenv("CRYPTO_PERP_ACTIVE_CAPITAL", "0")),
        active_capital_pct=float(os.getenv("CRYPTO_PERP_ACTIVE_CAPITAL_PCT", "0.15")),
        lookback_bars=int(os.getenv("CRYPTO_PERP_LOOKBACK_BARS", "60")),
        depth_limit=int(os.getenv("CRYPTO_PERP_DEPTH_LIMIT", "100")),
        trade_limit=int(os.getenv("CRYPTO_PERP_TRADE_LIMIT", "100")),
        entry_threshold=float(os.getenv("CRYPTO_PERP_ENTRY_THRESHOLD", "0.32")),
        exit_threshold=float(os.getenv("CRYPTO_PERP_EXIT_THRESHOLD", "0.12")),
        max_score=float(os.getenv("CRYPTO_PERP_MAX_SCORE", "0.60")),
        min_vol_acceleration=float(os.getenv("CRYPTO_PERP_MIN_VOL_ACCELERATION", "1.00")),
        max_spread_bps=float(os.getenv("CRYPTO_PERP_MAX_SPREAD_BPS", "20")),
        max_abs_position_weight=float(os.getenv("CRYPTO_PERP_MAX_ABS_POSITION_WEIGHT", "0.12")),
        max_gross_exposure=float(os.getenv("CRYPTO_PERP_MAX_GROSS_EXPOSURE", "0.12")),
        max_positions=int(os.getenv("CRYPTO_PERP_MAX_POSITIONS", "1")),
        min_order_notional=float(os.getenv("CRYPTO_PERP_MIN_ORDER_NOTIONAL", "20")),
        max_order_notional=float(os.getenv("CRYPTO_PERP_MAX_ORDER_NOTIONAL", "250")),
        rebalance_threshold=float(os.getenv("CRYPTO_PERP_REBALANCE_THRESHOLD", "0.05")),
        min_trade_interval_seconds=int(os.getenv("CRYPTO_PERP_MIN_TRADE_INTERVAL_SECONDS", "600")),
        leverage=max(1, min(3, int(os.getenv("CRYPTO_PERP_LEVERAGE", "1")))),
        margin_type=margin_type,
        fee_rate=max(0.0, float(os.getenv("CRYPTO_PERP_FEE_RATE", str(DEFAULT_USDM_TAKER_FEE_RATE)))),
        maker_fee_rate=max(0.0, float(os.getenv("CRYPTO_PERP_MAKER_FEE_RATE", str(DEFAULT_USDM_MAKER_FEE_RATE)))),
        slippage_bps=max(0.0, float(os.getenv("CRYPTO_PERP_SLIPPAGE_BPS", "2"))),
        order_style=order_style,
        maker_order_ttl_seconds=max(30, int(os.getenv("CRYPTO_PERP_MAKER_ORDER_TTL_SECONDS", "180"))),
        maker_price_offset_bps=max(0.0, float(os.getenv("CRYPTO_PERP_MAKER_PRICE_OFFSET_BPS", "0"))),
        recv_window_ms=int(os.getenv("CRYPTO_PERP_RECV_WINDOW_MS", "5000")),
        testnet_validate_only=_parse_bool(os.getenv("CRYPTO_PERP_TESTNET_VALIDATE_ONLY"), default=False),
        max_order_book_take_ratio=max(0.01, min(1.0, float(os.getenv("CRYPTO_PERP_MAX_ORDER_BOOK_TAKE_RATIO", "0.10")))),
        exit_confirm_cycles=max(1, int(os.getenv("CRYPTO_PERP_EXIT_CONFIRM_CYCLES", "3"))),
        signal_confirm_cycles=max(1, int(os.getenv("CRYPTO_PERP_SIGNAL_CONFIRM_CYCLES", "3"))),
        require_edge_over_cost=_parse_bool(os.getenv("CRYPTO_PERP_REQUIRE_EDGE_OVER_COST"), default=True),
        edge_bps_per_score=max(0.0, float(os.getenv("CRYPTO_PERP_EDGE_BPS_PER_SCORE", "60"))),
        cost_buffer_bps=max(0.0, float(os.getenv("CRYPTO_PERP_COST_BUFFER_BPS", "6"))),
        min_edge_cost_ratio=max(1.0, float(os.getenv("CRYPTO_PERP_MIN_EDGE_COST_RATIO", str(DEFAULT_PERP_MIN_EDGE_COST_RATIO)))),
        hawkes_weight=max(0.0, float(os.getenv("CRYPTO_PERP_HAWKES_WEIGHT", "0.10"))),
        min_hawkes_imbalance=max(0.0, float(os.getenv("CRYPTO_PERP_MIN_HAWKES_IMBALANCE", "0.08"))),
        cross_asset_ofi_weight=max(0.0, float(os.getenv("CRYPTO_PERP_CROSS_ASSET_OFI_WEIGHT", "0.15"))),
        funding_interval_seconds=max(60, int(os.getenv("CRYPTO_PERP_FUNDING_INTERVAL_SECONDS", "28800"))),
        max_adverse_funding_rate=max(0.0, float(os.getenv("CRYPTO_PERP_MAX_ADVERSE_FUNDING_RATE", "0.0003"))),
        maintenance_margin_rate=max(0.0, float(os.getenv("CRYPTO_PERP_MAINTENANCE_MARGIN_RATE", "0.005"))),
        loss_guard_max_loss=max(0.0, float(os.getenv("CRYPTO_PERP_LOSS_GUARD_MAX_LOSS", "50"))),
        loss_guard_max_fees=max(
            0.0,
            float(os.getenv("CRYPTO_PERP_LOSS_GUARD_MAX_FEES", str(DEFAULT_PERP_LOSS_GUARD_MAX_FEES))),
        ),
        loss_guard_max_trades=max(
            0,
            int(os.getenv("CRYPTO_PERP_LOSS_GUARD_MAX_TRADES", str(DEFAULT_PERP_LOSS_GUARD_MAX_TRADES))),
        ),
        loss_guard_recent_window_seconds=max(0, int(os.getenv("CRYPTO_PERP_LOSS_GUARD_RECENT_WINDOW_SECONDS", "900"))),
        loss_guard_max_recent_trades=max(0, int(os.getenv("CRYPTO_PERP_LOSS_GUARD_MAX_RECENT_TRADES", "8"))),
        loss_guard_max_recent_flips=max(0, int(os.getenv("CRYPTO_PERP_LOSS_GUARD_MAX_RECENT_FLIPS", "3"))),
        loss_guard_symbol_max_loss=max(0.0, float(os.getenv("CRYPTO_PERP_LOSS_GUARD_SYMBOL_MAX_LOSS", "15"))),
        loss_guard_symbol_max_fees=max(
            0.0,
            float(
                os.getenv(
                    "CRYPTO_PERP_LOSS_GUARD_SYMBOL_MAX_FEES",
                    str(DEFAULT_PERP_SYMBOL_LOSS_GUARD_MAX_FEES),
                )
            ),
        ),
        loss_guard_symbol_max_trades=max(
            0,
            int(
                os.getenv(
                    "CRYPTO_PERP_LOSS_GUARD_SYMBOL_MAX_TRADES",
                    str(DEFAULT_PERP_SYMBOL_LOSS_GUARD_MAX_TRADES),
                )
            ),
        ),
    )


def reset_crypto_perp_paper(settings: CryptoPerpSettings | None = None) -> CryptoPerpPaperState:
    settings = settings or load_crypto_perp_settings()
    state = CryptoPerpPaperState.fresh(settings)
    state.save(settings)
    _write_status({"status": "reset", "mode": settings.mode, "paper_cash": state.cash})
    return state


def _strategy_sizing_equity(settings: CryptoPerpSettings, equity: float) -> float:
    if settings.active_capital_pct > 0:
        return max(0.0, equity * settings.active_capital_pct)
    if settings.active_capital > 0:
        return min(max(0.0, settings.active_capital), max(0.0, equity))
    return max(0.0, equity)


def _signed_weight_with_cap(score_map: dict[str, float], total_exposure: float, max_abs_weight: float) -> dict[str, float]:
    if total_exposure <= 0 or not score_map:
        return {}
    remaining = {symbol: score for symbol, score in score_map.items() if abs(score) > 0}
    weights: dict[str, float] = {}
    budget = float(total_exposure)
    while remaining and budget > 1e-12:
        total_score = sum(abs(v) for v in remaining.values())
        if total_score <= 0:
            break
        capped: list[str] = []
        for symbol, score in remaining.items():
            proposed_abs = budget * abs(score) / total_score
            if proposed_abs > max_abs_weight + 1e-9:
                weights[symbol] = math.copysign(max_abs_weight, score)
                budget -= max_abs_weight
                capped.append(symbol)
        if not capped:
            for symbol, score in remaining.items():
                proposed_abs = budget * abs(score) / total_score
                weights[symbol] = math.copysign(proposed_abs, score)
            break
        for symbol in capped:
            remaining.pop(symbol, None)
    return {symbol: round(weight, 6) for symbol, weight in weights.items() if abs(weight) > 0}


def _parse_ts_seconds(raw: Any) -> float | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.timestamp()


def _perp_order_guard_stats(mode: str, window_seconds: int) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "trade_count": 0,
        "recent_trade_count": 0,
        "recent_flip_count": 0,
        "recent_symbols": [],
        "recent_last_side_by_symbol": {},
        "recent_window_seconds": max(0, int(window_seconds)),
    }
    if not ORDERS_FILE.exists():
        return stats

    now = datetime.now(UTC).timestamp()
    counted_statuses = {"filled", "submitted", "new"}
    latest_side_by_symbol: dict[str, str] = {}
    symbols: set[str] = set()
    try:
        lines = ORDERS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return stats
    for line in lines:
        try:
            order = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(order.get("mode") or "") != mode:
            continue
        status = str(order.get("status") or "").lower()
        if status not in counted_statuses:
            continue
        stats["trade_count"] += 1
        ts_seconds = _parse_ts_seconds(order.get("ts"))
        if stats["recent_window_seconds"] <= 0 or ts_seconds is None or now - ts_seconds > stats["recent_window_seconds"]:
            continue
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").upper()
        stats["recent_trade_count"] += 1
        if symbol:
            symbols.add(symbol)
        if symbol and side in {"BUY", "SELL"}:
            previous_side = latest_side_by_symbol.get(symbol)
            if previous_side and previous_side != side:
                stats["recent_flip_count"] += 1
            latest_side_by_symbol[symbol] = side
    stats["recent_symbols"] = sorted(symbols)
    stats["recent_last_side_by_symbol"] = dict(latest_side_by_symbol)
    return stats


def _perp_loss_guard_breach(settings: CryptoPerpSettings, account: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    stats = _perp_order_guard_stats(settings.mode, settings.loss_guard_recent_window_seconds)
    net_pnl = _safe_float(account.get("net_pnl"))
    fees_paid = _safe_float(account.get("fees_paid"))
    trade_count = int(_safe_float(account.get("trade_count"), stats["trade_count"]) or stats["trade_count"])
    max_loss = max(0.0, float(settings.loss_guard_max_loss))
    max_fees = max(0.0, float(settings.loss_guard_max_fees))
    max_trades = max(0, int(settings.loss_guard_max_trades))
    max_recent_trades = max(0, int(settings.loss_guard_max_recent_trades))
    max_recent_flips = max(0, int(settings.loss_guard_max_recent_flips))

    breaches: list[str] = []
    if max_loss > 0 and net_pnl <= -max_loss:
        breaches.append("loss")
    if max_fees > 0 and fees_paid >= max_fees:
        breaches.append("fees")
    if max_trades > 0 and trade_count >= max_trades:
        breaches.append("trade_count")
    if max_recent_trades > 0 and int(stats["recent_trade_count"]) >= max_recent_trades:
        breaches.append("recent_trades")
    if max_recent_flips > 0 and int(stats["recent_flip_count"]) >= max_recent_flips:
        breaches.append("recent_flips")
    if not breaches:
        return "", {}
    reason = "perp_loss_guard_" + "_".join(breaches)
    return reason, {
        "reason": reason,
        "breaches": breaches,
        "net_pnl": round(net_pnl, 8),
        "fees_paid": round(fees_paid, 8),
        "trade_count": trade_count,
        "max_loss": max_loss,
        "max_fees": max_fees,
        "max_trades": max_trades,
        "recent_window_seconds": stats["recent_window_seconds"],
        "recent_trade_count": stats["recent_trade_count"],
        "max_recent_trades": max_recent_trades,
        "recent_flip_count": stats["recent_flip_count"],
        "max_recent_flips": max_recent_flips,
        "recent_symbols": stats["recent_symbols"],
        "action": "block_new_entries_reduce_only",
    }


def _perp_symbol_loss_guard_breaches(settings: CryptoPerpSettings, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
    max_loss = max(0.0, float(settings.loss_guard_symbol_max_loss))
    max_fees = max(0.0, float(settings.loss_guard_symbol_max_fees))
    max_trades = max(0, int(settings.loss_guard_symbol_max_trades))
    if max_loss <= 0 and max_fees <= 0 and max_trades <= 0:
        return {}
    if not ORDERS_FILE.exists():
        return {}

    allowed_symbols = {str(symbol).upper() for symbol in symbols}
    stats: dict[str, dict[str, float]] = {}
    try:
        lines = ORDERS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            order = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(order.get("mode") or "") != settings.mode:
            continue
        if str(order.get("status") or "").lower() != "filled":
            continue
        symbol = str(order.get("symbol") or "").upper()
        if symbol not in allowed_symbols:
            continue
        response = order.get("response") if isinstance(order.get("response"), dict) else {}
        row = stats.setdefault(symbol, {"trade_count": 0.0, "fees": 0.0, "realized_pnl": 0.0})
        row["trade_count"] += 1.0
        row["fees"] += _safe_float(order.get("fee"))
        row["realized_pnl"] += _safe_float(response.get("paper_realized_pnl"))

    breaches_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol, row in stats.items():
        trade_count = int(row["trade_count"])
        fees = float(row["fees"])
        realized_pnl = float(row["realized_pnl"])
        net_pnl = realized_pnl - fees
        breaches: list[str] = []
        if max_loss > 0 and net_pnl <= -max_loss:
            breaches.append("loss")
        if max_fees > 0 and fees >= max_fees:
            breaches.append("fees")
        if max_trades > 0 and trade_count >= max_trades:
            breaches.append("trade_count")
        if not breaches:
            continue
        reason = "perp_symbol_loss_guard_" + "_".join(breaches)
        breaches_by_symbol[symbol] = {
            "reason": reason,
            "breaches": breaches,
            "net_pnl": round(net_pnl, 8),
            "realized_pnl": round(realized_pnl, 8),
            "fees_paid": round(fees, 8),
            "trade_count": trade_count,
            "max_loss": max_loss,
            "max_fees": max_fees,
            "max_trades": max_trades,
            "action": "block_symbol_new_entries_reduce_only",
        }
    return breaches_by_symbol


def _book_summary(book: dict[str, list[list[float]]] | None) -> dict[str, Any]:
    if not book:
        return {"bid_levels": 0, "ask_levels": 0}
    bids = book.get("Bid") or []
    asks = book.get("Ask") or []
    bid_qty = sum(_safe_float(row[1]) for row in bids[:5] if len(row) >= 2)
    ask_qty = sum(_safe_float(row[1]) for row in asks[:5] if len(row) >= 2)
    return {
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "top_bid": _safe_float(bids[0][0]) if bids else 0.0,
        "top_ask": _safe_float(asks[0][0]) if asks else 0.0,
        "top5_bid_qty": round(bid_qty, 8),
        "top5_ask_qty": round(ask_qty, 8),
    }


def _ticks_summary(ticks: pd.DataFrame) -> dict[str, Any]:
    if ticks.empty:
        return {"trade_count": 0}
    directions = ticks.get("ticker_direction")
    volume = pd.to_numeric(ticks.get("volume"), errors="coerce").fillna(0.0)
    buy_mask = directions.eq("BUY") if directions is not None else pd.Series(False, index=ticks.index)
    sell_mask = directions.eq("SELL") if directions is not None else pd.Series(False, index=ticks.index)
    return {
        "trade_count": int(len(ticks)),
        "buy_volume": round(float(volume[buy_mask].sum()), 8),
        "sell_volume": round(float(volume[sell_mask].sum()), 8),
        "total_volume": round(float(volume.sum()), 8),
    }


def _compute_hawkes_imbalance(ticks: pd.DataFrame, *, decay: float = 0.88) -> float:
    if ticks.empty or "ticker_direction" not in ticks:
        return 0.0
    directions = list(ticks["ticker_direction"].tail(100))
    signed_sum = 0.0
    weight_sum = 0.0
    for age, direction in enumerate(reversed(directions)):
        weight = decay**age
        sign = 1.0 if direction == "BUY" else (-1.0 if direction == "SELL" else 0.0)
        signed_sum += sign * weight
        weight_sum += abs(sign) * weight
    return signed_sum / weight_sum if weight_sum > 0 else 0.0


def _book_side_levels(book: dict[str, list[list[float]]] | None, side: str) -> list[list[float]]:
    if not book:
        return []
    return list(book.get("Ask" if side.upper() == "BUY" else "Bid") or [])


def _book_side_available_qty(book: dict[str, list[list[float]]] | None, side: str) -> float:
    return sum(max(0.0, _safe_float(row[1])) for row in _book_side_levels(book, side) if len(row) >= 2)


def _maker_limit_price(book: dict[str, list[list[float]]] | None, side: str, fallback_price: float, offset_bps: float = 0.0) -> float:
    levels = _book_side_levels(book, "SELL" if side.upper() == "BUY" else "BUY")
    price = _safe_float(levels[0][0]) if levels and len(levels[0]) >= 1 else fallback_price
    if price <= 0:
        return fallback_price
    offset = max(0.0, offset_bps) / 10_000.0
    if side.upper() == "BUY":
        return price * (1.0 - offset)
    return price * (1.0 + offset)


def _book_vwap(
    book: dict[str, list[list[float]]] | None,
    side: str,
    quantity: float,
    fallback_price: float,
    *,
    fallback_slippage_bps: float,
) -> tuple[float, float, dict[str, Any]]:
    if quantity <= 0 or fallback_price <= 0:
        return 0.0, 0.0, {"source": "invalid"}
    levels = _book_side_levels(book, side)
    remaining = float(quantity)
    cost = 0.0
    filled = 0.0
    worst_price = 0.0
    levels_used = 0
    for raw_level in levels:
        if len(raw_level) < 2 or remaining <= 1e-12:
            break
        price = _safe_float(raw_level[0])
        level_qty = max(0.0, _safe_float(raw_level[1]))
        if price <= 0 or level_qty <= 0:
            continue
        take = min(remaining, level_qty)
        cost += take * price
        filled += take
        remaining -= take
        worst_price = price
        levels_used += 1
    if remaining > 1e-12:
        synthetic_price = fallback_price * (
            1.0 + fallback_slippage_bps / 10_000.0
            if side.upper() == "BUY"
            else 1.0 - fallback_slippage_bps / 10_000.0
        )
        cost += remaining * synthetic_price
        filled += remaining
        worst_price = synthetic_price
    avg_price = cost / filled if filled > 0 else fallback_price
    if side.upper() == "BUY":
        slippage_bps = (avg_price / fallback_price - 1.0) * 10_000.0
    else:
        slippage_bps = (1.0 - avg_price / fallback_price) * 10_000.0
    return (
        avg_price,
        slippage_bps,
        {
            "source": "order_book" if levels else "fallback_slippage",
            "levels_used": levels_used,
            "fallback_qty": round(max(0.0, remaining), 12),
            "worst_price": round(worst_price, 8),
        },
    )


def _estimated_liquidation_price(
    *,
    entry_price: float,
    quantity: float,
    leverage: int,
    maintenance_margin_rate: float,
) -> float:
    if entry_price <= 0 or abs(quantity) <= 0 or leverage <= 0:
        return 0.0
    maint = max(0.0, maintenance_margin_rate)
    lev = max(1, leverage)
    if quantity > 0:
        denominator = max(1e-9, 1.0 - maint)
        return max(0.0, entry_price * (1.0 - 1.0 / lev) / denominator)
    denominator = max(1e-9, 1.0 + maint)
    return max(0.0, entry_price * (1.0 + 1.0 / lev) / denominator)


def _strategy_value(payload: dict[str, Any], key: str, default: Any) -> Any:
    settings = payload.get("strategy_settings") or {}
    context = payload.get("benchmark_context") or {}
    if key in settings:
        return settings[key]
    if key in context:
        return context[key]
    return default


def _feature_cost_explanation(feature: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    funding_rates = (payload.get("benchmark_context") or {}).get("funding_rates") or {}
    score = _safe_float(feature.get("score"))
    direction = 1 if score > 0 else -1
    order_style = str(_strategy_value(payload, "order_style", "market"))
    taker_fee = _safe_float(_strategy_value(payload, "fee_rate", DEFAULT_USDM_TAKER_FEE_RATE), DEFAULT_USDM_TAKER_FEE_RATE)
    maker_fee = _safe_float(_strategy_value(payload, "maker_fee_rate", DEFAULT_USDM_MAKER_FEE_RATE), DEFAULT_USDM_MAKER_FEE_RATE)
    fee_rate = maker_fee if order_style == "maker_limit" else taker_fee
    expected_edge_bps = abs(score) * _safe_float(_strategy_value(payload, "edge_bps_per_score", 60.0), 60.0)
    fee_bps = fee_rate * 2.0 * 10_000.0
    slippage_bps = 0.0 if order_style == "maker_limit" else _safe_float(_strategy_value(payload, "slippage_bps", 2.0), 2.0) * 2.0
    spread_bps = max(0.0, _safe_float(feature.get("spread_bps")))
    funding_rate = _safe_float(funding_rates.get(str(feature.get("symbol") or "")))
    adverse_funding_bps = max(0.0, funding_rate * direction * 10_000.0)
    buffer_bps = _safe_float(_strategy_value(payload, "cost_buffer_bps", 6.0), 6.0)
    min_edge_cost_ratio = max(1.0, _safe_float(_strategy_value(payload, "min_edge_cost_ratio", 1.0), 1.0))
    round_trip_cost_bps = fee_bps + slippage_bps + spread_bps + adverse_funding_bps + buffer_bps
    required_edge_bps = round_trip_cost_bps * min_edge_cost_ratio
    return {
        "expected_edge_bps": round(expected_edge_bps, 4),
        "required_edge_bps": round(required_edge_bps, 4),
        "round_trip_cost_bps": round(round_trip_cost_bps, 4),
        "min_edge_cost_ratio": round(min_edge_cost_ratio, 4),
        "cost_pass": bool(expected_edge_bps >= required_edge_bps),
        "fee_bps": round(fee_bps, 4),
        "slippage_bps": round(slippage_bps, 4),
        "spread_bps": round(spread_bps, 4),
        "adverse_funding_bps": round(adverse_funding_bps, 4),
        "buffer_bps": round(buffer_bps, 4),
    }


def explain_crypto_perp_status(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or read_crypto_perp_status()
    account = payload.get("account") or {}
    features = payload.get("features") or []
    target_weights = payload.get("target_weights") or {}
    planned_orders = payload.get("planned_orders") or []
    submitted_orders = payload.get("submitted_orders") or []
    pending_updates = payload.get("pending_order_updates") or []
    active_orders = submitted_orders or pending_updates or planned_orders
    order_style = str(_strategy_value(payload, "order_style", "market"))
    mode = str(payload.get("mode") or "unknown")
    pnl_source = str(account.get("pnl_source") or "unknown")
    fees_paid = _safe_float(account.get("fees_paid"))
    net_pnl = _safe_float(account.get("net_pnl"))
    realized = _safe_float(account.get("realized_pnl"))
    unrealized = _safe_float(account.get("unrealized_pnl"))
    funding_paid = _safe_float(account.get("funding_paid"))

    summary: list[str] = []
    summary.append(f"Mode={mode}; PnL source={pnl_source}. This is local paper unless pnl_source says Binance account.")
    summary.append(
        f"Net PnL {net_pnl:.4f} = realized {realized:.4f} + unrealized {unrealized:.4f} - fees {fees_paid:.4f} - funding_paid {funding_paid:.4f}."
    )
    summary.append(f"Execution style is {order_style}; cost gate is {'on' if _strategy_value(payload, 'require_edge_over_cost', True) else 'off'}.")
    if active_orders:
        summary.append(f"This cycle has {len(active_orders)} order/update row(s).")
    elif target_weights:
        summary.append("This cycle wants exposure but no new order was needed, usually because current position already matches or cooldown/pending order blocked it.")
    else:
        summary.append("This cycle has no signed target; the strategy is waiting or exiting existing exposure.")
    if fees_paid > abs(realized + unrealized) and fees_paid > 0:
        summary.append("Fees are still the dominant historical drag; judge new behavior by future fee growth, not the old sunk cost.")

    signal_rows: list[dict[str, Any]] = []
    entry_threshold = _safe_float(_strategy_value(payload, "entry_threshold", 0.0))
    for feature in features:
        score = _safe_float(feature.get("score"))
        signal = str(feature.get("signal") or "flat")
        cost = _feature_cost_explanation(feature, payload)
        notes: list[str] = []
        if abs(score) < entry_threshold:
            notes.append("below_entry_threshold")
        if "weak_hawkes_confirmation" in str(feature.get("reason") or ""):
            notes.append("weak_hawkes")
        if _safe_float(feature.get("cross_asset_leader_score")):
            notes.append("btc_leader_adjusted")
        if signal != "flat" and not cost["cost_pass"]:
            notes.append("cost_gate_blocks_entry")
        signal_rows.append(
            {
                "symbol": feature.get("symbol"),
                "signal": signal,
                "score": round(score, 6),
                "threshold": entry_threshold,
                "hawkes": feature.get("hawkes_imbalance"),
                "btc_leader": feature.get("cross_asset_leader_score"),
                **cost,
                "notes": ",".join(notes) if notes else "ok",
                "raw_reason": feature.get("reason"),
            }
        )

    order_rows: list[dict[str, Any]] = []
    for row in active_orders:
        response = row.get("response") or {}
        order_rows.append(
            {
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "status": row.get("status"),
                "reason": row.get("reason"),
                "order_type": row.get("order_type"),
                "time_in_force": row.get("time_in_force"),
                "reduce_only": row.get("reduce_only"),
                "notional": row.get("notional"),
                "fee": row.get("fee"),
                "fee_liquidity": response.get("fee_liquidity") or response.get("fee_source"),
                "plain": _plain_order_action(row),
            }
        )

    risk_rows: list[dict[str, Any]] = []
    for item in account.get("position_details") or []:
        risk_rows.append(
            {
                "symbol": item.get("symbol"),
                "side": item.get("side"),
                "qty": item.get("quantity"),
                "notional": item.get("notional"),
                "unrealized_pnl": item.get("unrealized_pnl"),
                "liquidation_distance_pct": item.get("liquidation_distance_pct"),
                "plain": f"{item.get('symbol')} {item.get('side')} notional {item.get('notional')}, unrealized {item.get('unrealized_pnl')}",
            }
        )

    recent_orders = _read_jsonl_tail(ORDERS_FILE, tail=8)
    return {
        "updated_at": payload.get("updated_at"),
        "summary": summary,
        "account": account,
        "targets": target_weights,
        "signals": signal_rows,
        "orders": order_rows,
        "risks": risk_rows,
        "recent_orders": recent_orders,
        "next_questions": [
            "Are fees still growing faster than realized plus unrealized PnL?",
            "Are orders mostly maker posted/filled rather than taker filled?",
            "Are signals blocked by cost gate, weak Hawkes, or entry threshold?",
        ],
    }


def _plain_order_action(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "")
    symbol = str(row.get("symbol") or "")
    side = str(row.get("side") or "")
    reason = str(row.get("reason") or "")
    reduce_only = bool(row.get("reduce_only"))
    order_type = str(row.get("order_type") or "")
    if status == "posted":
        return f"Posted maker {side} limit for {symbol}; waiting for market to cross it."
    if status == "filled" and reduce_only:
        return f"Reduced/closed {symbol} via {side}; reason={reason}."
    if status == "filled":
        return f"Opened/increased {symbol} via {side}; reason={reason}."
    if order_type == "LIMIT":
        return f"Plans maker {side} limit for {symbol}; no taker fill yet."
    return f"Plans {side} for {symbol}; reason={reason}."


class CryptoPerpEngine:
    def __init__(
        self,
        settings: CryptoPerpSettings,
        client: BinanceUsdMFuturesClient | None = None,
        market_client: BinanceUsdMFuturesClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or BinanceUsdMFuturesClient(
            base_url=settings.base_url,
            api_key=settings.api_key,
            api_secret=settings.api_secret,
            recv_window_ms=settings.recv_window_ms,
        )
        self.market_client = market_client or (
            client
            if client is not None
            else BinanceUsdMFuturesClient(
                base_url=settings.market_data_base_url,
                recv_window_ms=settings.recv_window_ms,
            )
        )
        self._commission_cache: dict[str, dict[str, Any]] = {}

    def check(self) -> dict[str, Any]:
        self.client.ping()
        self.market_client.ping()
        symbols = self.active_symbols()
        result = {
            "status": "ok",
            "mode": self.settings.mode,
            "execution_base_url": self.settings.base_url,
            "market_data_base_url": self.settings.market_data_base_url,
            "market_data_label": self.settings.market_data_label,
            "submit_label": self.settings.submit_label,
            "signed_account_enabled": self.settings.signed_account_enabled,
            "server_time": self.client.server_time(),
            "symbols": symbols,
            "leverage": self.settings.leverage,
            "margin_type": self.settings.margin_type,
        }
        if self.settings.mode == "testnet" and self.settings.signed_account_enabled:
            account = self.client.account()
            result["can_read_testnet_account"] = True
            result["total_margin_balance"] = account.get("totalMarginBalance")
        _write_status(result)
        return result

    def active_symbols(self) -> list[str]:
        symbols = list(dict.fromkeys([self.settings.benchmark, *self.settings.symbols]))
        try:
            exchange_symbols = self.market_client.exchange_symbols()
        except Exception:
            exchange_symbols = set()
        if exchange_symbols:
            symbols = [symbol for symbol in symbols if symbol in exchange_symbols]
        return symbols or list(self.settings.symbols)

    def _commission_rate(self, symbol: str, liquidity: str = "taker") -> tuple[float, str]:
        symbol = symbol.upper()
        liquidity = "maker" if str(liquidity).lower() == "maker" else "taker"
        if symbol in self._commission_cache:
            row = self._commission_cache[symbol]
            key = "makerCommissionRate" if liquidity == "maker" else "takerCommissionRate"
            fallback = self.settings.maker_fee_rate if liquidity == "maker" else self.settings.fee_rate
            return _safe_float(row.get(key), fallback), str(row.get("source") or BINANCE_USDM_COMMISSION_ENDPOINT_SOURCE)
        if self.settings.mode == "testnet" and self.settings.signed_account_enabled:
            try:
                row = dict(self.client.commission_rate(symbol))
                row["source"] = BINANCE_USDM_COMMISSION_ENDPOINT_SOURCE
                self._commission_cache[symbol] = row
                key = "makerCommissionRate" if liquidity == "maker" else "takerCommissionRate"
                fallback = self.settings.maker_fee_rate if liquidity == "maker" else self.settings.fee_rate
                return _safe_float(row.get(key), fallback), BINANCE_USDM_COMMISSION_ENDPOINT_SOURCE
            except Exception as exc:
                _append_jsonl(
                    EVENTS_FILE,
                    {
                        "ts": _utc_now(),
                        "event": "commission_rate_fallback",
                        "symbol": symbol,
                        "error": f"{type(exc).__name__}: {exc}",
                        "fallback_rate": self.settings.maker_fee_rate if liquidity == "maker" else self.settings.fee_rate,
                        "liquidity": liquidity,
                    },
                )
        if liquidity == "maker":
            return self.settings.maker_fee_rate, "configured_usdm_maker_rate"
        return self.settings.fee_rate, BINANCE_USDM_CONFIGURED_TAKER_RATE_SOURCE

    def _load_state_for_mode(self) -> CryptoPerpPaperState:
        state = CryptoPerpPaperState.load(self.settings)
        if self.settings.mode != "testnet" or not self.settings.signed_account_enabled:
            return state
        try:
            account = self.client.account()
        except Exception:
            return state
        positions: dict[str, float] = {}
        avg_entry: dict[str, float] = {}
        for row in account.get("positions", []):
            symbol = str(row.get("symbol") or "").upper()
            qty = _safe_float(row.get("positionAmt"))
            entry = _safe_float(row.get("entryPrice"))
            if symbol and abs(qty) > 0:
                positions[symbol] = qty
                if entry > 0:
                    avg_entry[symbol] = entry
        state.positions = positions
        state.avg_entry = avg_entry
        wallet = _safe_float(account.get("totalWalletBalance"), state.cash)
        if wallet > 0:
            state.cash = wallet
        state.save(self.settings)
        return state

    def _preserved_position_weight(
        self,
        state: CryptoPerpPaperState,
        symbol: str,
        qty: float,
        snapshots: dict[str, pd.Series],
    ) -> float:
        snapshot = snapshots.get(symbol)
        price = _safe_float(snapshot.get("last_price")) if snapshot is not None else 0.0
        if price <= 0:
            price = self._mark_price(symbol)
        if price <= 0:
            return 0.0
        equity = self._account_equity_from_state(state, {symbol: price})
        sizing_equity = _strategy_sizing_equity(self.settings, equity)
        if sizing_equity <= 0:
            return 0.0
        current_weight = qty * price / sizing_equity
        if abs(current_weight) <= 1e-12:
            return 0.0
        return round(math.copysign(min(abs(current_weight), self.settings.max_abs_position_weight), current_weight), 6)

    def _apply_signal_confirmation(
        self,
        state: CryptoPerpPaperState,
        target_weights: dict[str, float],
        snapshots: dict[str, pd.Series],
        *,
        update_signal_state: bool,
    ) -> dict[str, float]:
        required = max(1, int(self.settings.signal_confirm_cycles))
        if required <= 1:
            if update_signal_state and state.signal_confirm_streak:
                state.signal_confirm_streak = {}
                state.save(self.settings)
            return target_weights

        adjusted = dict(target_weights)
        next_streak = {symbol: dict(row) for symbol, row in state.signal_confirm_streak.items()}
        changed = False

        for symbol in list(next_streak):
            if symbol not in target_weights:
                next_streak.pop(symbol, None)
                changed = True

        for symbol, target_weight in list(target_weights.items()):
            if abs(target_weight) <= 1e-12:
                continue
            desired_direction = 1 if target_weight > 0 else -1
            current_qty = state.positions.get(symbol, 0.0)
            current_direction = 1 if current_qty > 0 else (-1 if current_qty < 0 else 0)
            if current_direction == desired_direction:
                if symbol in next_streak:
                    next_streak.pop(symbol, None)
                    changed = True
                continue

            prior = next_streak.get(symbol) or {}
            prior_count = _safe_int(prior.get("count")) if _safe_int(prior.get("direction")) == desired_direction else 0
            observed = prior_count + 1
            if update_signal_state:
                next_streak[symbol] = {"direction": desired_direction, "count": observed}
                changed = True
            if observed >= required:
                continue

            adjusted.pop(symbol, None)
            reason = "flip_confirmation_pending" if current_direction else "entry_confirmation_pending"
            preserved_weight = 0.0
            if current_direction:
                preserved_weight = self._preserved_position_weight(state, symbol, current_qty, snapshots)
                if abs(preserved_weight) > 1e-12:
                    adjusted[symbol] = preserved_weight
            _append_jsonl(
                EVENTS_FILE,
                {
                    "ts": _utc_now(),
                    "event": "perp_signal_delayed",
                    "mode": self.settings.mode,
                    "symbol": symbol,
                    "desired_direction": "long" if desired_direction > 0 else "short",
                    "current_direction": "long" if current_direction > 0 else ("short" if current_direction < 0 else "flat"),
                    "streak": observed,
                    "required": required,
                    "reason": reason,
                    "preserved_weight": preserved_weight,
                },
            )

        if update_signal_state and changed:
            state.signal_confirm_streak = next_streak
            state.save(self.settings)
        return adjusted

    def _apply_exit_confirmation(
        self,
        state: CryptoPerpPaperState,
        target_weights: dict[str, float],
        features: list[CryptoPerpFeature],
        snapshots: dict[str, pd.Series],
        funding_by_symbol: dict[str, float],
        *,
        update_exit_state: bool,
    ) -> dict[str, float]:
        if not state.positions:
            if update_exit_state and state.exit_signal_streak:
                state.exit_signal_streak = {}
                state.save(self.settings)
            return target_weights

        required = max(1, int(self.settings.exit_confirm_cycles))
        feature_by_symbol = {feature.symbol: feature for feature in features}
        next_streak = dict(state.exit_signal_streak)
        changed = False

        for symbol in list(next_streak):
            if abs(state.positions.get(symbol, 0.0)) <= 0:
                next_streak.pop(symbol, None)
                changed = True

        prices: dict[str, float] = {}
        for symbol, qty in state.positions.items():
            if abs(qty) <= 0:
                continue
            snapshot = snapshots.get(symbol)
            price = _safe_float(snapshot.get("last_price")) if snapshot is not None else 0.0
            if price <= 0:
                price = self._mark_price(symbol)
            prices[symbol] = price

        equity = self._account_equity_from_state(state, prices)
        sizing_equity = _strategy_sizing_equity(self.settings, equity)
        adjusted = dict(target_weights)

        for symbol, qty in state.positions.items():
            if abs(qty) <= 0:
                continue
            current_target = adjusted.get(symbol, 0.0)
            if abs(current_target) > 1e-12:
                if symbol in next_streak:
                    next_streak.pop(symbol, None)
                    changed = True
                continue

            feature = feature_by_symbol.get(symbol)
            score = feature.score if feature is not None else 0.0
            funding_rate = funding_by_symbol.get(symbol, 0.0)
            opposite_entry = (qty > 0 and score <= -self.settings.entry_threshold) or (qty < 0 and score >= self.settings.entry_threshold)
            adverse_funding = (qty > 0 and funding_rate > self.settings.max_adverse_funding_rate) or (
                qty < 0 and funding_rate < -self.settings.max_adverse_funding_rate
            )
            if opposite_entry or adverse_funding:
                if symbol in next_streak:
                    next_streak.pop(symbol, None)
                    changed = True
                _append_jsonl(
                    EVENTS_FILE,
                    {
                        "ts": _utc_now(),
                        "event": "perp_exit_immediate",
                        "mode": self.settings.mode,
                        "symbol": symbol,
                        "score": score,
                        "funding_rate": funding_rate,
                        "reason": "opposite_entry" if opposite_entry else "adverse_funding",
                    },
                )
                continue

            prior = max(0, _safe_int(next_streak.get(symbol)))
            observed = prior + 1
            if update_exit_state and next_streak.get(symbol) != observed:
                next_streak[symbol] = observed
                changed = True
            if observed >= required:
                _append_jsonl(
                    EVENTS_FILE,
                    {
                        "ts": _utc_now(),
                        "event": "perp_exit_confirmed",
                        "mode": self.settings.mode,
                        "symbol": symbol,
                        "score": score,
                        "streak": observed,
                        "required": required,
                    },
                )
                continue

            price = prices.get(symbol, 0.0)
            if price <= 0 or sizing_equity <= 0:
                continue
            current_weight = qty * price / sizing_equity
            if abs(current_weight) <= 1e-12:
                continue
            adjusted[symbol] = round(
                math.copysign(min(abs(current_weight), self.settings.max_abs_position_weight), current_weight),
                6,
            )
            _append_jsonl(
                EVENTS_FILE,
                {
                    "ts": _utc_now(),
                    "event": "perp_exit_delayed",
                    "mode": self.settings.mode,
                    "symbol": symbol,
                    "score": score,
                    "streak": observed,
                    "required": required,
                    "preserved_weight": adjusted[symbol],
                },
            )

        if update_exit_state and changed:
            state.exit_signal_streak = next_streak
            state.save(self.settings)
        return adjusted

    def _pretrade_cost_check(self, feature: CryptoPerpFeature, funding_rate: float) -> dict[str, Any]:
        direction = 1 if feature.score > 0 else -1
        expected_edge_bps = abs(feature.score) * self.settings.edge_bps_per_score
        fee_rate = self.settings.maker_fee_rate if self.settings.order_style == "maker_limit" else self.settings.fee_rate
        fee_bps = fee_rate * 2.0 * 10_000.0
        slippage_bps = 0.0 if self.settings.order_style == "maker_limit" else self.settings.slippage_bps * 2.0
        spread_bps = max(0.0, feature.spread_bps)
        adverse_funding_bps = max(0.0, funding_rate * direction * 10_000.0)
        round_trip_cost_bps = fee_bps + slippage_bps + spread_bps + adverse_funding_bps + self.settings.cost_buffer_bps
        min_edge_cost_ratio = max(1.0, float(self.settings.min_edge_cost_ratio))
        required_edge_bps = round_trip_cost_bps * min_edge_cost_ratio
        return {
            "passed": (not self.settings.require_edge_over_cost) or expected_edge_bps >= required_edge_bps,
            "expected_edge_bps": round(expected_edge_bps, 6),
            "required_edge_bps": round(required_edge_bps, 6),
            "round_trip_cost_bps": round(round_trip_cost_bps, 6),
            "min_edge_cost_ratio": round(min_edge_cost_ratio, 6),
            "fee_bps": round(fee_bps, 6),
            "slippage_bps": round(slippage_bps, 6),
            "spread_bps": round(spread_bps, 6),
            "adverse_funding_bps": round(adverse_funding_bps, 6),
            "cost_buffer_bps": round(self.settings.cost_buffer_bps, 6),
        }

    def generate_plan(self, state: CryptoPerpPaperState | None = None, *, update_exit_state: bool = True) -> CryptoPerpPlan:
        state = state or self._load_state_for_mode()
        guard_account = self.account_snapshot(state)
        guard_reason, guard_context = _perp_loss_guard_breach(self.settings, guard_account)
        if guard_reason:
            if update_exit_state and state.signal_confirm_streak:
                state.signal_confirm_streak = {}
                state.save(self.settings)
            _append_jsonl(
                EVENTS_FILE,
                {
                    "ts": _utc_now(),
                    "event": "perp_loss_guard_triggered",
                    "mode": self.settings.mode,
                    **guard_context,
                },
            )
            _append_jsonl(
                EVENTS_FILE,
                {
                    "ts": _utc_now(),
                    "event": "perp_plan_generated",
                    "mode": self.settings.mode,
                    "benchmark": self.settings.benchmark,
                    "benchmark_score": 0.0,
                    "target_weights": {},
                    "reason": guard_reason,
                    "benchmark_context": guard_context,
                },
            )
            return CryptoPerpPlan(
                mode=self.settings.mode,
                benchmark=self.settings.benchmark,
                benchmark_score=0.0,
                gross_exposure=0.0,
                target_weights={},
                features=[],
                market_sources={},
                reason=guard_reason,
                benchmark_context=guard_context,
            )
        active_symbols = self.active_symbols()
        snapshots: dict[str, pd.Series] = {}
        bars_by_symbol: dict[str, pd.DataFrame] = {}
        books: dict[str, dict[str, list[list[float]]]] = {}
        ticks_by_symbol: dict[str, pd.DataFrame] = {}
        market_sources: dict[str, str] = {}

        for symbol in active_symbols:
            bars_by_symbol[symbol] = self.market_client.klines(symbol, interval="1m", limit=self.settings.lookback_bars)
            snapshots[symbol] = self.market_client.book_ticker(symbol)
            books[symbol] = self.market_client.depth(symbol, limit=self.settings.depth_limit)
            ticks_by_symbol[symbol] = self.market_client.recent_trades(symbol, limit=self.settings.trade_limit)
            market_sources[symbol] = "futures_rest"
            _append_jsonl(
                EVENTS_FILE,
                {
                    "ts": _utc_now(),
                    "event": "perp_market_snapshot",
                    "mode": self.settings.mode,
                    "symbol": symbol,
                    "book": _book_summary(books[symbol]),
                    "ticks": _ticks_summary(ticks_by_symbol[symbol]),
                },
            )

        benchmark_score = 0.0
        benchmark = self.settings.benchmark
        if benchmark in snapshots:
            benchmark_score = _compute_benchmark_score(bars_by_symbol[benchmark], snapshots[benchmark], books[benchmark])
        funding_by_symbol: dict[str, float] = {}
        for symbol in active_symbols:
            try:
                funding_by_symbol[symbol] = _safe_float(self.market_client.premium_index(symbol).get("lastFundingRate"))
            except Exception:
                funding_by_symbol[symbol] = 0.0
        benchmark_context = {
            "score": round(benchmark_score, 6),
            "long_exposure_scale": round(max(0.0, min(1.0, 0.5 + benchmark_score)), 6),
            "short_exposure_scale": round(max(0.0, min(1.0, 0.5 - benchmark_score)), 6),
            "funding_rates": {symbol: round(rate, 8) for symbol, rate in funding_by_symbol.items()},
            "exit_confirm_cycles": self.settings.exit_confirm_cycles,
            "signal_confirm_cycles": self.settings.signal_confirm_cycles,
            "require_edge_over_cost": self.settings.require_edge_over_cost,
            "edge_bps_per_score": self.settings.edge_bps_per_score,
            "cost_buffer_bps": self.settings.cost_buffer_bps,
            "min_edge_cost_ratio": self.settings.min_edge_cost_ratio,
            "hawkes_weight": self.settings.hawkes_weight,
            "min_hawkes_imbalance": self.settings.min_hawkes_imbalance,
            "cross_asset_ofi_weight": self.settings.cross_asset_ofi_weight,
        }
        symbol_loss_guards = _perp_symbol_loss_guard_breaches(self.settings, active_symbols)
        if symbol_loss_guards:
            benchmark_context["symbol_loss_guard"] = {
                "blocked_symbols": sorted(symbol_loss_guards),
                "symbols": symbol_loss_guards,
            }

        features: list[CryptoPerpFeature] = []
        for symbol in active_symbols:
            feature = self._score_symbol(
                symbol,
                books.get(symbol),
                state.last_order_books.get(symbol),
                bars_by_symbol.get(symbol, pd.DataFrame()),
                ticks_by_symbol.get(symbol, pd.DataFrame()),
                snapshots.get(symbol, pd.Series(dtype=object)),
            )
            features.append(feature)

        features = self._apply_cross_asset_feature_adjustment(features)
        for feature in features:
            _append_jsonl(FEATURES_FILE, {"ts": _utc_now(), **asdict(feature)})

        state.last_order_books = books
        state.save(self.settings)

        raw_candidates: dict[str, float] = {}
        symbol_guard_skips: set[str] = set()
        for feature in features:
            score = feature.score
            current_qty = state.positions.get(feature.symbol, 0.0)
            symbol_guard = symbol_loss_guards.get(feature.symbol)
            if symbol_guard is not None:
                symbol_guard_skips.add(feature.symbol)
                _append_jsonl(
                    EVENTS_FILE,
                    {
                        "ts": _utc_now(),
                        "event": "perp_signal_skipped",
                        "mode": self.settings.mode,
                        "symbol": feature.symbol,
                        "score": score,
                        "reason": symbol_guard["reason"],
                        "symbol_loss_guard": symbol_guard,
                    },
                )
                continue
            desired_direction = 1 if score > 0 else -1
            current_direction = 1 if current_qty > 0 else (-1 if current_qty < 0 else 0)
            spread_ok = "spread_too_wide" not in feature.reason
            entry_ok = feature.eligible and abs(score) >= self.settings.entry_threshold
            hold_ok = spread_ok and (
                (current_qty > 0 and score >= self.settings.exit_threshold)
                or (current_qty < 0 and score <= -self.settings.exit_threshold)
            )
            funding_rate = funding_by_symbol.get(feature.symbol, 0.0)
            adverse_funding = (
                (score > 0 and funding_rate > self.settings.max_adverse_funding_rate)
                or (score < 0 and funding_rate < -self.settings.max_adverse_funding_rate)
            )
            cost_check = self._pretrade_cost_check(feature, funding_rate)
            needs_trade_cost_gate = entry_ok and current_direction != desired_direction
            cost_ok = (not needs_trade_cost_gate) or bool(cost_check["passed"])
            if (entry_ok or hold_ok) and not adverse_funding:
                if cost_ok:
                    raw_candidates[feature.symbol] = score
                else:
                    _append_jsonl(
                        EVENTS_FILE,
                        {
                            "ts": _utc_now(),
                            "event": "perp_signal_skipped",
                            "mode": self.settings.mode,
                            "symbol": feature.symbol,
                            "score": score,
                            "reason": "expected_edge_below_cost",
                            **cost_check,
                        },
                    )
            elif adverse_funding:
                _append_jsonl(
                    EVENTS_FILE,
                    {
                        "ts": _utc_now(),
                        "event": "perp_signal_skipped",
                        "mode": self.settings.mode,
                        "symbol": feature.symbol,
                        "score": score,
                        "funding_rate": funding_rate,
                        "reason": "adverse_funding",
                    },
                )
        ordered = dict(sorted(raw_candidates.items(), key=lambda item: abs(item[1]), reverse=True)[: self.settings.max_positions])
        directional_scores: dict[str, float] = {}
        for symbol, score in ordered.items():
            if score > 0:
                scale = max(0.0, min(1.0, 0.5 + benchmark_score))
            else:
                scale = max(0.0, min(1.0, 0.5 - benchmark_score))
            if scale <= 0:
                continue
            directional_scores[symbol] = math.copysign(abs(score) * scale, score)
        target_weights = _signed_weight_with_cap(
            directional_scores,
            self.settings.max_gross_exposure if directional_scores else 0.0,
            self.settings.max_abs_position_weight,
        )
        target_weights = self._apply_signal_confirmation(
            state,
            target_weights,
            snapshots,
            update_signal_state=update_exit_state,
        )
        target_weights = self._apply_exit_confirmation(
            state,
            target_weights,
            features,
            snapshots,
            funding_by_symbol,
            update_exit_state=update_exit_state,
        )
        for symbol in symbol_loss_guards:
            target_weights.pop(symbol, None)
        reason = "ok" if target_weights else ("perp_symbol_loss_guard" if symbol_guard_skips else "no_signed_signal")
        _append_jsonl(
            EVENTS_FILE,
            {
                "ts": _utc_now(),
                "event": "perp_plan_generated",
                "mode": self.settings.mode,
                "benchmark": benchmark,
                "benchmark_score": round(benchmark_score, 6),
                "target_weights": target_weights,
                "reason": reason,
                "benchmark_context": benchmark_context,
            },
        )
        return CryptoPerpPlan(
            mode=self.settings.mode,
            benchmark=benchmark,
            benchmark_score=round(benchmark_score, 6),
            gross_exposure=round(sum(abs(value) for value in target_weights.values()), 6),
            target_weights=target_weights,
            features=sorted(features, key=lambda item: abs(item.score), reverse=True),
            market_sources=market_sources,
            reason=reason,
            benchmark_context=benchmark_context,
        )

    def _feature_with_score(self, feature: CryptoPerpFeature, score: float, *, extra_reason: str | None = None, leader_score: float | None = None) -> CryptoPerpFeature:
        reasons = [part for part in feature.reason.split(",") if part and part != "ok" and part != "abs_score_below_entry"]
        abs_score = abs(score)
        if abs_score < self.settings.entry_threshold:
            reasons.append("abs_score_below_entry")
        if extra_reason:
            reasons.append(extra_reason)
        signal = "long" if score >= self.settings.entry_threshold else ("short" if score <= -self.settings.entry_threshold else "flat")
        eligible = "spread_too_wide" not in reasons and signal != "flat"
        conviction = min(1.0, max(0.0, abs_score / max(self.settings.max_score, 1e-9)))
        return replace(
            feature,
            score=round(score, 6),
            abs_score=round(abs_score, 6),
            conviction=round(conviction, 6),
            signal=signal,
            eligible=eligible,
            reason="ok" if not reasons else ",".join(dict.fromkeys(reasons)),
            cross_asset_leader_score=round(leader_score if leader_score is not None else feature.cross_asset_leader_score, 6),
        )

    def _apply_cross_asset_feature_adjustment(self, features: list[CryptoPerpFeature]) -> list[CryptoPerpFeature]:
        if self.settings.cross_asset_ofi_weight <= 0:
            return features
        leader = next((feature for feature in features if feature.symbol == self.settings.benchmark), None)
        if leader is None or abs(leader.score) < self.settings.exit_threshold:
            return features
        adjusted: list[CryptoPerpFeature] = []
        for feature in features:
            if feature.symbol == leader.symbol:
                adjusted.append(feature)
                continue
            score = feature.score + self.settings.cross_asset_ofi_weight * leader.score
            adjusted.append(self._feature_with_score(feature, score, extra_reason="cross_asset_benchmark_ofi", leader_score=leader.score))
        return adjusted

    def _score_symbol(
        self,
        symbol: str,
        order_book: dict | None,
        prev_order_book: dict | None,
        bars_1m: pd.DataFrame,
        ticks: pd.DataFrame,
        snapshot: pd.Series,
    ) -> CryptoPerpFeature:
        if bars_1m.empty:
            return CryptoPerpFeature(symbol, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.5, 9999.0, 0.0, 0.0, 0.0, "flat", False, "no_bars")
        deep_end = max(60, self.settings.depth_limit)
        ofi = compute_multi_level_ofi(order_book, prev_order_book, ((1, 5), (6, 20), (21, deep_end)))
        vol_accel = compute_volume_acceleration(bars_1m)
        momentum = compute_micro_momentum(bars_1m)
        vwap_dev = compute_vwap_deviation(bars_1m)
        tick_agg = compute_tick_aggression(ticks)
        hawkes_imbalance = _compute_hawkes_imbalance(ticks)
        spread = compute_spread_quality(snapshot)
        score = (
            0.25 * ofi.get("tier_2", 0.0)
            + 0.15 * ofi.get("tier_1", 0.0)
            + 0.10 * ofi.get("tier_3", 0.0)
            + 0.15 * _clip(momentum.get("mom_3m", 0.0), 0.005)
            + 0.10 * _clip(momentum.get("mom_10m", 0.0), 0.015)
            + 0.10 * _clip(vol_accel - 1.0, 2.0)
            + 0.10 * _clip(tick_agg - 0.5, 0.3)
            + 0.05 * _clip(vwap_dev, 0.005)
            + self.settings.hawkes_weight * _clip(hawkes_imbalance, 1.0)
        )
        reasons: list[str] = []
        min_vol = max(0.0, self.settings.min_vol_acceleration)
        if min_vol > 0 and vol_accel < min_vol:
            volume_gap = min(1.0, (min_vol - vol_accel) / max(min_vol, 1e-9))
            score *= max(0.75, 1.0 - volume_gap * 0.25)
            reasons.append("low_volume_soft_penalty")
        if abs(hawkes_imbalance) < self.settings.min_hawkes_imbalance:
            score *= 0.85
            reasons.append("weak_hawkes_confirmation")
        if spread > self.settings.max_spread_bps:
            reasons.append("spread_too_wide")
        abs_score = abs(score)
        if abs_score < self.settings.entry_threshold:
            reasons.append("abs_score_below_entry")
        signal = "long" if score >= self.settings.entry_threshold else ("short" if score <= -self.settings.entry_threshold else "flat")
        eligible = "spread_too_wide" not in reasons and signal != "flat"
        conviction = min(1.0, max(0.0, abs_score / max(self.settings.max_score, 1e-9)))
        return CryptoPerpFeature(
            symbol=symbol,
            last_price=float(snapshot.get("last_price", 0.0) or 0.0),
            ofi_tier_1=round(ofi.get("tier_1", 0.0), 6),
            ofi_tier_2=round(ofi.get("tier_2", 0.0), 6),
            ofi_tier_3=round(ofi.get("tier_3", 0.0), 6),
            vol_accel=round(vol_accel, 6),
            mom_3m=round(momentum.get("mom_3m", 0.0), 6),
            mom_10m=round(momentum.get("mom_10m", 0.0), 6),
            mom_30m=round(momentum.get("mom_30m", 0.0), 6),
            vwap_dev=round(vwap_dev, 6),
            tick_agg=round(tick_agg, 6),
            spread_bps=round(spread, 6),
            score=round(score, 6),
            abs_score=round(abs_score, 6),
            conviction=round(conviction, 6),
            signal=signal,
            eligible=eligible,
            reason="ok" if not reasons else ",".join(reasons),
            hawkes_imbalance=round(hawkes_imbalance, 6),
        )

    def _current_position_values(self, state: CryptoPerpPaperState, prices: dict[str, float]) -> dict[str, float]:
        return {symbol: qty * prices.get(symbol, 0.0) for symbol, qty in state.positions.items() if abs(qty) > 0 and prices.get(symbol, 0.0) > 0}

    def plan_orders(self, plan: CryptoPerpPlan, state: CryptoPerpPaperState | None = None) -> list[CryptoPerpOrder]:
        state = state or self._load_state_for_mode()
        symbols = list(dict.fromkeys([*plan.target_weights, *state.positions]))
        prices: dict[str, float] = {}
        books: dict[str, dict[str, list[list[float]]]] = {}
        for symbol in symbols:
            try:
                ticker = self.market_client.book_ticker(symbol)
                prices[symbol] = _safe_float(ticker.get("last_price"))
            except Exception:
                feature_price = next((feature.last_price for feature in plan.features if feature.symbol == symbol), 0.0)
                prices[symbol] = feature_price
            try:
                books[symbol] = self.market_client.depth(symbol, limit=self.settings.depth_limit)
            except Exception:
                books[symbol] = {}
        equity = self._account_equity_from_state(state, prices)
        sizing_equity = _strategy_sizing_equity(self.settings, equity)
        current_values = self._current_position_values(state, prices)
        orders: list[CryptoPerpOrder] = []
        now_epoch = time.time()
        order_guard_stats = _perp_order_guard_stats(self.settings.mode, self.settings.loss_guard_recent_window_seconds)
        projected_guard_trade_count = int(order_guard_stats.get("trade_count", 0) or 0)
        projected_guard_recent_trade_count = int(order_guard_stats.get("recent_trade_count", 0) or 0)
        projected_guard_recent_flip_count = int(order_guard_stats.get("recent_flip_count", 0) or 0)
        projected_recent_sides = {
            str(symbol): str(side).upper()
            for symbol, side in dict(order_guard_stats.get("recent_last_side_by_symbol") or {}).items()
        }
        projected_guard_fees = max(0.0, float(state.fees_paid or 0.0))
        max_guard_fees = max(0.0, float(self.settings.loss_guard_max_fees))
        max_guard_trades = max(0, int(self.settings.loss_guard_max_trades))
        max_guard_recent_trades = max(0, int(self.settings.loss_guard_max_recent_trades))
        max_guard_recent_flips = max(0, int(self.settings.loss_guard_max_recent_flips))

        def _projected_flip_increment(symbol: str, side: str) -> int:
            previous_side = projected_recent_sides.get(symbol)
            if previous_side in {"BUY", "SELL"} and side in {"BUY", "SELL"} and side != previous_side:
                return 1
            return 0

        def _record_projected_order(symbol: str, side: str) -> None:
            nonlocal projected_guard_recent_trade_count, projected_guard_recent_flip_count
            if self.settings.loss_guard_recent_window_seconds <= 0:
                return
            projected_guard_recent_trade_count += 1
            projected_guard_recent_flip_count += _projected_flip_increment(symbol, side)
            projected_recent_sides[symbol] = side

        def _projected_entry_loss_guard(fee: float, *, symbol: str, side: str) -> dict[str, Any]:
            projected_trade_count = projected_guard_trade_count + 1
            projected_fees = projected_guard_fees + max(0.0, float(fee or 0.0))
            projected_recent_trade_count = projected_guard_recent_trade_count + (
                1 if self.settings.loss_guard_recent_window_seconds > 0 else 0
            )
            projected_recent_flip_count = projected_guard_recent_flip_count + _projected_flip_increment(symbol, side)
            breaches: list[str] = []
            if max_guard_fees > 0 and projected_fees >= max_guard_fees:
                breaches.append("fees")
            if max_guard_trades > 0 and projected_trade_count >= max_guard_trades:
                breaches.append("trade_count")
            if max_guard_recent_trades > 0 and projected_recent_trade_count >= max_guard_recent_trades:
                breaches.append("recent_trades")
            if max_guard_recent_flips > 0 and projected_recent_flip_count >= max_guard_recent_flips:
                breaches.append("recent_flips")
            if not breaches:
                return {}
            return {
                "breaches": breaches,
                "current_fees": round(projected_guard_fees, 8),
                "projected_fees": round(projected_fees, 8),
                "max_fees": max_guard_fees,
                "current_trade_count": projected_guard_trade_count,
                "projected_trade_count": projected_trade_count,
                "max_trades": max_guard_trades,
                "recent_window_seconds": self.settings.loss_guard_recent_window_seconds,
                "current_recent_trade_count": projected_guard_recent_trade_count,
                "projected_recent_trade_count": projected_recent_trade_count,
                "max_recent_trades": max_guard_recent_trades,
                "current_recent_flip_count": projected_guard_recent_flip_count,
                "projected_recent_flip_count": projected_recent_flip_count,
                "max_recent_flips": max_guard_recent_flips,
            }

        pending_symbols = {str(row.get("symbol") or "").upper() for row in state.pending_orders if str(row.get("status") or "posted") == "posted"}
        for symbol in symbols:
            if symbol in pending_symbols:
                _append_jsonl(
                    EVENTS_FILE,
                    {
                        "ts": _utc_now(),
                        "event": "perp_order_skipped",
                        "symbol": symbol,
                        "reason": "pending_maker_order_exists",
                    },
                )
                continue
            price = prices.get(symbol, 0.0)
            if price <= 0:
                continue
            current_qty = state.positions.get(symbol, 0.0)
            current_value = current_values.get(symbol, 0.0)
            target_weight = plan.target_weights.get(symbol, 0.0)
            target_value = sizing_equity * target_weight
            desired_diff = target_value - current_value
            reason = "rebalance"
            reduce_only = False
            if current_value * target_value < 0:
                desired_diff = -current_value
                target_value = 0.0
                target_weight = 0.0
                reason = "sign_flip_close_first"
                reduce_only = True
            elif abs(target_weight) < 1e-12 and abs(current_value) > 0:
                reason = "target_exit"
                reduce_only = True
            elif current_value and desired_diff and current_value * desired_diff < 0:
                reduce_only = True
            elif abs(desired_diff) < max(self.settings.min_order_notional, self.settings.rebalance_threshold * max(abs(current_value), sizing_equity)):
                continue

            last_trade_ts = state.last_trade_ts.get(symbol, 0.0)
            if (
                not reduce_only
                and last_trade_ts
                and self.settings.min_trade_interval_seconds > 0
                and now_epoch - last_trade_ts < self.settings.min_trade_interval_seconds
            ):
                continue

            trade_notional = min(abs(desired_diff), self.settings.max_order_notional)
            if trade_notional < self.settings.min_order_notional:
                continue
            side = "BUY" if desired_diff > 0 else "SELL"
            available_qty = _book_side_available_qty(books.get(symbol), side)
            if available_qty > 0:
                max_qty_from_book = available_qty * self.settings.max_order_book_take_ratio
                trade_notional = min(trade_notional, max_qty_from_book * price)
                if trade_notional < self.settings.min_order_notional:
                    _append_jsonl(
                        EVENTS_FILE,
                        {
                            "ts": _utc_now(),
                            "event": "perp_order_skipped",
                            "symbol": symbol,
                            "side": side,
                            "reason": "visible_book_liquidity_below_min_order",
                            "available_qty": round(available_qty, 12),
                            "max_take_ratio": self.settings.max_order_book_take_ratio,
                        },
                    )
                    continue
            raw_qty = trade_notional / price
            try:
                normalized_qty, qty_text, skip_reason = self.client.normalize_market_quantity(symbol, raw_qty, price)
            except Exception:
                normalized_qty, qty_text, skip_reason = _to_decimal(raw_qty), _decimal_to_api_text(_to_decimal(raw_qty)), None
            if skip_reason:
                _append_jsonl(
                    EVENTS_FILE,
                    {
                        "ts": _utc_now(),
                        "event": "perp_order_skipped",
                        "symbol": symbol,
                        "side": side,
                        "reason": skip_reason,
                        "raw_qty": raw_qty,
                        "price": price,
                    },
                )
                continue
            qty = float(normalized_qty)
            fee_liquidity = "maker" if self.settings.order_style == "maker_limit" else "taker"
            order_type = "LIMIT" if fee_liquidity == "maker" else "MARKET"
            time_in_force = "GTX" if fee_liquidity == "maker" else None
            if fee_liquidity == "maker":
                est_price = _maker_limit_price(books.get(symbol), side, price, self.settings.maker_price_offset_bps)
                est_slippage_bps = 0.0
                execution_detail = {
                    "source": "maker_limit",
                    "post_only": True,
                    "fill_model": "pending_until_trade_or_quote_cross",
                    "ttl_seconds": self.settings.maker_order_ttl_seconds,
                }
            else:
                est_price, est_slippage_bps, execution_detail = _book_vwap(
                    books.get(symbol),
                    side,
                    qty,
                    price,
                    fallback_slippage_bps=self.settings.slippage_bps,
                )
            notional = qty * est_price
            if notional < self.settings.min_order_notional:
                continue
            fee_rate, _fee_source = self._commission_rate(symbol, fee_liquidity)
            fee = notional * fee_rate
            if not reduce_only:
                projected_guard = _projected_entry_loss_guard(fee, symbol=symbol, side=side)
                if projected_guard:
                    _append_jsonl(
                        EVENTS_FILE,
                        {
                            "ts": _utc_now(),
                            "event": "perp_order_skipped",
                            "symbol": symbol,
                            "side": side,
                            "reason": "projected_loss_guard_budget",
                            "fee": round(fee, 8),
                            **projected_guard,
                        },
                    )
                    continue
            orders.append(
                CryptoPerpOrder(
                    ts=_utc_now(),
                    mode=self.settings.mode,
                    symbol=symbol,
                    side=side,
                    quantity=qty,
                    price=round(est_price, 8),
                    notional=round(notional, 8),
                    fee=round(fee, 8),
                    status="planned",
                    reason=reason,
                    reduce_only=reduce_only,
                    target_weight=round(target_weight, 8),
                    current_value=round(current_value, 8),
                    target_value=round(target_value, 8),
                    leverage=self.settings.leverage,
                    margin_type=self.settings.margin_type,
                    order_type=order_type,
                    time_in_force=time_in_force,
                    response={
                        "quantity_text": qty_text,
                        "reference_price": round(price, 8),
                        "estimated_slippage_bps": round(est_slippage_bps, 6),
                        "fee_liquidity": fee_liquidity,
                        "fee_rate": fee_rate,
                        "execution_model": execution_detail,
                    },
                )
            )
            projected_guard_trade_count += 1
            projected_guard_fees += max(0.0, fee)
            _record_projected_order(symbol, side)
        return orders

    def submit_orders(self, orders: Iterable[CryptoPerpOrder], state: CryptoPerpPaperState | None = None) -> list[CryptoPerpOrder]:
        state = state or self._load_state_for_mode()
        submitted: list[CryptoPerpOrder] = []
        for order in orders:
            if self.settings.mode == "paper":
                if order.order_type.upper() == "LIMIT" and (order.response or {}).get("fee_liquidity") == "maker":
                    posted = self._post_paper_maker_order(state, order)
                    submitted.append(posted)
                    _append_jsonl(ORDERS_FILE, asdict(posted))
                else:
                    filled = self._apply_paper_fill(state, order)
                    submitted.append(filled)
                    _append_jsonl(ORDERS_FILE, asdict(filled))
                continue
            if not self.settings.signed_account_enabled:
                raise CryptoPerpError("CRYPTO_PERP_MODE=testnet requires CRYPTO_PERP_API_KEY and CRYPTO_PERP_API_SECRET before submitting futures orders.")
            try:
                self._ensure_symbol_risk_settings(order.symbol)
                if order.order_type.upper() == "LIMIT":
                    response = self.client.limit_order(
                        order.symbol,
                        order.side,
                        quantity=order.response.get("quantity_text") if order.response else order.quantity,
                        price=order.price,
                        reduce_only=order.reduce_only,
                        post_only=order.time_in_force == "GTX",
                        validate_only=self.settings.testnet_validate_only,
                    )
                else:
                    response = self.client.market_order(
                        order.symbol,
                        order.side,
                        quantity=order.response.get("quantity_text") if order.response else order.quantity,
                        reduce_only=order.reduce_only,
                        validate_only=self.settings.testnet_validate_only,
                    )
                filled_order = CryptoPerpOrder(
                    **{
                        **asdict(order),
                        "status": "submitted" if self.settings.testnet_validate_only else str(response.get("status") or "submitted").lower(),
                        "response": response,
                    }
                )
                submitted.append(filled_order)
                _append_jsonl(ORDERS_FILE, asdict(filled_order))
            except Exception as exc:
                failed = CryptoPerpOrder(
                    **{
                        **asdict(order),
                        "status": "error",
                        "response": {"error": f"{type(exc).__name__}: {exc}"},
                    }
                )
                submitted.append(failed)
                _append_jsonl(ORDERS_FILE, asdict(failed))
        state.save(self.settings)
        return submitted

    def _post_paper_maker_order(self, state: CryptoPerpPaperState, order: CryptoPerpOrder) -> CryptoPerpOrder:
        payload = asdict(order)
        payload.update(
            {
                "status": "posted",
                "posted_at_epoch": time.time(),
                "expires_at_epoch": time.time() + self.settings.maker_order_ttl_seconds,
            }
        )
        state.pending_orders.append(payload)
        state.save(self.settings)
        return CryptoPerpOrder(
            **{
                **asdict(order),
                "status": "posted",
                "fee": 0.0,
                "response": {
                    **(order.response or {}),
                    "paper_pending": True,
                    "expires_in_seconds": self.settings.maker_order_ttl_seconds,
                },
            }
        )

    def _paper_maker_order_crossed(self, order: dict[str, Any], book: dict[str, list[list[float]]], ticks: pd.DataFrame) -> bool:
        side = str(order.get("side") or "").upper()
        limit_price = _safe_float(order.get("price"))
        if limit_price <= 0:
            return False
        if side == "BUY":
            ask = _safe_float((book.get("Ask") or [[0]])[0][0])
            if ask > 0 and ask <= limit_price:
                return True
            prices = pd.to_numeric(ticks.get("price"), errors="coerce") if not ticks.empty and "price" in ticks else pd.Series(dtype=float)
            return bool((prices <= limit_price).any()) if not prices.empty else False
        bid = _safe_float((book.get("Bid") or [[0]])[0][0])
        if bid > 0 and bid >= limit_price:
            return True
        prices = pd.to_numeric(ticks.get("price"), errors="coerce") if not ticks.empty and "price" in ticks else pd.Series(dtype=float)
        return bool((prices >= limit_price).any()) if not prices.empty else False

    def process_paper_pending_orders(self, state: CryptoPerpPaperState | None = None) -> list[CryptoPerpOrder]:
        state = state or self._load_state_for_mode()
        if not state.pending_orders:
            return []
        now = time.time()
        updates: list[CryptoPerpOrder] = []
        remaining: list[dict[str, Any]] = []
        for row in state.pending_orders:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            if now >= _safe_float(row.get("expires_at_epoch")):
                order_fields = {k: v for k, v in row.items() if k in CryptoPerpOrder.__dataclass_fields__}
                expired = CryptoPerpOrder(**{**order_fields, "status": "expired", "fee": 0.0, "response": {**(row.get("response") or {}), "paper_expired": True}})
                updates.append(expired)
                _append_jsonl(ORDERS_FILE, asdict(expired))
                continue
            try:
                book = self.market_client.depth(symbol, limit=self.settings.depth_limit)
                ticks = self.market_client.recent_trades(symbol, limit=self.settings.trade_limit)
            except Exception:
                remaining.append(row)
                continue
            if self._paper_maker_order_crossed(row, book, ticks):
                order = CryptoPerpOrder(**{k: v for k, v in row.items() if k in CryptoPerpOrder.__dataclass_fields__})
                filled = self._apply_paper_fill(state, order)
                updates.append(filled)
                _append_jsonl(ORDERS_FILE, asdict(filled))
            else:
                remaining.append(row)
        state.pending_orders = remaining
        state.save(self.settings)
        return updates

    def _ensure_symbol_risk_settings(self, symbol: str) -> None:
        try:
            self.client.change_margin_type(symbol, self.settings.margin_type)
        except Exception as exc:
            text = str(exc).lower()
            if "no need to change margin type" not in text and "code': -4046" not in text and '"code": -4046' not in text:
                raise
        self.client.change_leverage(symbol, self.settings.leverage)

    def _apply_paper_fill(self, state: CryptoPerpPaperState, order: CryptoPerpOrder) -> CryptoPerpOrder:
        signed_qty_delta = order.quantity if order.side.upper() == "BUY" else -order.quantity
        fill_price = order.price
        notional = abs(signed_qty_delta) * fill_price
        fee_liquidity = (order.response or {}).get("fee_liquidity") or "taker"
        fee_rate, fee_source = self._commission_rate(order.symbol, str(fee_liquidity))
        fee = notional * fee_rate
        old_qty = state.positions.get(order.symbol, 0.0)
        old_avg = state.avg_entry.get(order.symbol, fill_price)
        new_qty = old_qty + signed_qty_delta
        realized = 0.0
        if abs(old_qty) > 0 and old_qty * signed_qty_delta < 0:
            closing_qty = min(abs(old_qty), abs(signed_qty_delta))
            realized = closing_qty * (fill_price - old_avg) * (1.0 if old_qty > 0 else -1.0)
            state.realized_pnl += realized
            state.cash += realized
        if abs(new_qty) < 1e-12:
            state.positions.pop(order.symbol, None)
            state.avg_entry.pop(order.symbol, None)
        elif abs(old_qty) < 1e-12 or old_qty * signed_qty_delta > 0:
            total_abs = abs(old_qty) + abs(signed_qty_delta)
            new_avg = ((abs(old_qty) * old_avg) + (abs(signed_qty_delta) * fill_price)) / max(total_abs, 1e-12)
            state.positions[order.symbol] = new_qty
            state.avg_entry[order.symbol] = new_avg
        elif old_qty * new_qty > 0:
            state.positions[order.symbol] = new_qty
            state.avg_entry[order.symbol] = old_avg
        else:
            state.positions[order.symbol] = new_qty
            state.avg_entry[order.symbol] = fill_price
        state.fees_paid += fee
        state.cash -= fee
        state.last_trade_ts[order.symbol] = time.time()
        state.save(self.settings)
        return CryptoPerpOrder(
            **{
                **asdict(order),
                "price": round(fill_price, 8),
                "notional": round(notional, 8),
                "fee": round(fee, 8),
                "status": "filled",
                "response": {
                    "paper_realized_pnl": round(realized, 8),
                    "fee_source": fee_source,
                    "position_after": round(state.positions.get(order.symbol, 0.0), 12),
                },
            }
        )

    def _account_equity_from_state(self, state: CryptoPerpPaperState, prices: dict[str, float]) -> float:
        unrealized = 0.0
        for symbol, qty in state.positions.items():
            price = prices.get(symbol, 0.0)
            avg = state.avg_entry.get(symbol, price)
            if price > 0 and avg > 0:
                unrealized += (price - avg) * qty
        return state.cash + unrealized

    def _mark_price(self, symbol: str) -> float:
        try:
            mark = _safe_float(self.market_client.premium_index(symbol).get("markPrice"))
            if mark > 0:
                return mark
        except Exception:
            pass
        try:
            return _safe_float(self.market_client.book_ticker(symbol).get("last_price"))
        except Exception:
            return 0.0

    def _mark_prices(self, symbols: Iterable[str]) -> dict[str, float]:
        return {symbol: self._mark_price(symbol) for symbol in symbols}

    def _apply_paper_funding(self, state: CryptoPerpPaperState) -> None:
        if self.settings.mode != "paper" and self.settings.signed_account_enabled:
            return
        now = time.time()
        changed = False
        for symbol, qty in list(state.positions.items()):
            if abs(qty) <= 0:
                continue
            last_ts = state.last_funding_ts.get(symbol)
            if not last_ts:
                state.last_funding_ts[symbol] = now
                changed = True
                continue
            elapsed = max(0.0, now - last_ts)
            if elapsed <= 0:
                continue
            try:
                premium = self.market_client.premium_index(symbol)
            except Exception:
                premium = {}
            rate = _safe_float(premium.get("lastFundingRate"))
            mark = _safe_float(premium.get("markPrice")) or self._mark_price(symbol)
            if mark <= 0:
                continue
            notional = abs(qty * mark)
            prorated = min(1.0, elapsed / max(60, self.settings.funding_interval_seconds))
            funding_fee = math.copysign(notional * abs(rate) * prorated, rate if qty > 0 else -rate)
            if abs(funding_fee) > 1e-10:
                state.cash -= funding_fee
                state.funding_paid += funding_fee
                _append_jsonl(
                    EVENTS_FILE,
                    {
                        "ts": _utc_now(),
                        "event": "perp_funding_accrued",
                        "mode": self.settings.mode,
                        "symbol": symbol,
                        "quantity": qty,
                        "mark_price": round(mark, 8),
                        "funding_rate": round(rate, 10),
                        "elapsed_seconds": round(elapsed, 2),
                        "funding_fee": round(funding_fee, 8),
                    },
                )
            state.last_funding_ts[symbol] = now
            changed = True
        if changed:
            state.save(self.settings)

    def _signed_account_snapshot(self) -> dict[str, Any] | None:
        if self.settings.mode != "testnet" or not self.settings.signed_account_enabled:
            return None
        account = self.client.account()
        risk_rows: list[dict[str, Any]]
        try:
            risk_rows = self.client.position_risk()
        except Exception:
            risk_rows = []
        risk_by_symbol = {str(row.get("symbol") or "").upper(): row for row in risk_rows}
        positions: dict[str, float] = {}
        position_details: list[dict[str, Any]] = []
        for row in account.get("positions", []):
            symbol = str(row.get("symbol") or "").upper()
            qty = _safe_float(row.get("positionAmt"))
            if not symbol or abs(qty) <= 0:
                continue
            risk = risk_by_symbol.get(symbol, {})
            entry = _safe_float(risk.get("entryPrice"), _safe_float(row.get("entryPrice")))
            mark = _safe_float(risk.get("markPrice"))
            if mark <= 0:
                try:
                    mark = _safe_float(self.market_client.book_ticker(symbol).get("last_price"))
                except Exception:
                    mark = 0.0
            liquidation = _safe_float(risk.get("liquidationPrice"))
            liquidation_distance_pct = 0.0
            if mark > 0 and liquidation > 0:
                liquidation_distance_pct = abs(mark - liquidation) / mark
            positions[symbol] = qty
            position_details.append(
                {
                    "symbol": symbol,
                    "quantity": round(qty, 12),
                    "side": "long" if qty > 0 else "short",
                    "entry_price": round(entry, 8),
                    "mark_price": round(mark, 8),
                    "notional": round(qty * mark, 8),
                    "unrealized_pnl": round(_safe_float(risk.get("unRealizedProfit"), _safe_float(row.get("unrealizedProfit"))), 8),
                    "leverage": _safe_int(risk.get("leverage"), _safe_int(row.get("leverage"), self.settings.leverage)),
                    "margin_type": str(risk.get("marginType") or ("isolated" if row.get("isolated") else "cross")),
                    "liquidation_price": round(liquidation, 8),
                    "liquidation_distance_pct": round(liquidation_distance_pct, 8),
                }
            )
        funding_paid = 0.0
        try:
            funding_rows = self.client.income_history(income_type="FUNDING_FEE", limit=100)
            funding_paid = sum(_safe_float(row.get("income")) for row in funding_rows)
        except Exception:
            funding_rows = []
        return {
            "cash": round(_safe_float(account.get("availableBalance")), 8),
            "wallet_balance": round(_safe_float(account.get("totalWalletBalance")), 8),
            "equity": round(_safe_float(account.get("totalMarginBalance")), 8),
            "unrealized_pnl": round(_safe_float(account.get("totalUnrealizedProfit")), 8),
            "realized_pnl": 0.0,
            "fees_paid": 0.0,
            "funding_paid": round(funding_paid, 8),
            "positions": dict(sorted(positions.items())),
            "position_details": position_details,
            "pnl_source": "binance_usdm_futures_account",
            "funding_rows": len(funding_rows),
        }

    def account_snapshot(self, state: CryptoPerpPaperState | None = None) -> dict[str, Any]:
        signed = self._signed_account_snapshot()
        order_stats = _perp_order_guard_stats(self.settings.mode, self.settings.loss_guard_recent_window_seconds)
        if signed is not None:
            signed["starting_equity"] = round(self.settings.initial_cash, 8)
            signed["net_pnl"] = round(_safe_float(signed.get("equity")) - self.settings.initial_cash, 8)
            signed["net_return_pct"] = round((signed["net_pnl"] / self.settings.initial_cash) if self.settings.initial_cash > 0 else 0.0, 8)
            signed["trade_count"] = order_stats["trade_count"]
            return signed
        state = state or self._load_state_for_mode()
        symbols = list(dict.fromkeys([*self.active_symbols(), *state.positions]))
        prices = self._mark_prices(symbols)
        position_details: list[dict[str, Any]] = []
        unrealized = 0.0
        gross_notional = 0.0
        for symbol, qty in sorted(state.positions.items()):
            price = prices.get(symbol, 0.0)
            avg = state.avg_entry.get(symbol, price)
            pnl = (price - avg) * qty if price > 0 and avg > 0 else 0.0
            unrealized += pnl
            gross_notional += abs(qty * price)
            liquidation_price = _estimated_liquidation_price(
                entry_price=avg,
                quantity=qty,
                leverage=self.settings.leverage,
                maintenance_margin_rate=self.settings.maintenance_margin_rate,
            )
            liquidation_distance_pct = abs(price - liquidation_price) / price if price > 0 and liquidation_price > 0 else 0.0
            position_details.append(
                {
                    "symbol": symbol,
                    "quantity": round(qty, 12),
                    "side": "long" if qty > 0 else "short",
                    "entry_price": round(avg, 8),
                    "mark_price": round(price, 8),
                    "notional": round(qty * price, 8),
                    "unrealized_pnl": round(pnl, 8),
                    "initial_margin": round(abs(qty * price) / max(1, self.settings.leverage), 8),
                    "maintenance_margin": round(abs(qty * price) * self.settings.maintenance_margin_rate, 8),
                    "leverage": self.settings.leverage,
                    "margin_type": self.settings.margin_type.lower(),
                    "liquidation_price": round(liquidation_price, 8),
                    "liquidation_distance_pct": round(liquidation_distance_pct, 8),
                }
            )
        equity = state.cash + unrealized
        return {
            "cash": round(state.cash, 8),
            "wallet_balance": round(state.cash, 8),
            "equity": round(equity, 8),
            "starting_equity": round(self.settings.initial_cash, 8),
            "net_pnl": round(equity - self.settings.initial_cash, 8),
            "net_return_pct": round((equity - self.settings.initial_cash) / self.settings.initial_cash, 8) if self.settings.initial_cash > 0 else 0.0,
            "realized_pnl": round(state.realized_pnl, 8),
            "unrealized_pnl": round(unrealized, 8),
            "fees_paid": round(state.fees_paid, 8),
            "funding_paid": round(state.funding_paid, 8),
            "trade_count": order_stats["trade_count"],
            "gross_notional": round(gross_notional, 8),
            "positions": dict(sorted(state.positions.items())),
            "position_details": position_details,
            "prices": prices,
            "pnl_source": "local_perp_paper" if self.settings.mode == "paper" else "local_projection_no_signed_account",
        }

    def market_regime(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for symbol in self.active_symbols():
            try:
                premium = self.market_client.premium_index(symbol)
            except Exception:
                premium = {}
            rows.append(
                {
                    "symbol": symbol,
                    "mark_price": _safe_float(premium.get("markPrice")),
                    "index_price": _safe_float(premium.get("indexPrice")),
                    "last_funding_rate": _safe_float(premium.get("lastFundingRate")),
                    "next_funding_time": premium.get("nextFundingTime"),
                }
            )
        return {
            "venue": "binance_usdm_futures",
            "rows": rows,
            "high_positive_funding": [row["symbol"] for row in rows if row["last_funding_rate"] > 0.0005],
            "high_negative_funding": [row["symbol"] for row in rows if row["last_funding_rate"] < -0.0005],
        }

    def run_once(self, *, submit: bool = False) -> dict[str, Any]:
        started = _utc_now()
        state = self._load_state_for_mode()
        pending_order_updates: list[CryptoPerpOrder] = []
        if submit and self.settings.mode == "paper":
            pending_order_updates = self.process_paper_pending_orders(state)
        if submit:
            self._apply_paper_funding(state)
        plan = self.generate_plan(state, update_exit_state=submit)
        planned_orders = self.plan_orders(plan, state)
        submitted_orders = self.submit_orders(planned_orders, state) if submit else []
        if submit:
            self._apply_paper_funding(state)
        account = self.account_snapshot(state)
        market_regime = self.market_regime()
        payload = {
            "ts": started,
            "updated_at": started,
            "status": "submitted" if submit else "planned",
            "mode": self.settings.mode,
            "execution_base_url": self.settings.base_url,
            "market_data_base_url": self.settings.market_data_base_url,
            "market_data_label": self.settings.market_data_label,
            "submit_label": self.settings.submit_label,
            "signed_account_enabled": self.settings.signed_account_enabled,
            "benchmark": plan.benchmark,
            "benchmark_score": plan.benchmark_score,
            "gross_exposure": plan.gross_exposure,
            "target_weights": plan.target_weights,
            "features": [asdict(feature) for feature in plan.features],
            "planned_orders": [asdict(order) for order in planned_orders],
            "pending_order_updates": [asdict(order) for order in pending_order_updates],
            "submitted_orders": [asdict(order) for order in submitted_orders],
            "account": account,
            "market_regime": market_regime,
            "leverage": self.settings.leverage,
            "margin_type": self.settings.margin_type,
            "strategy_settings": {
                "entry_threshold": self.settings.entry_threshold,
                "exit_threshold": self.settings.exit_threshold,
                "exit_confirm_cycles": self.settings.exit_confirm_cycles,
                "signal_confirm_cycles": self.settings.signal_confirm_cycles,
                "require_edge_over_cost": self.settings.require_edge_over_cost,
                "edge_bps_per_score": self.settings.edge_bps_per_score,
                "cost_buffer_bps": self.settings.cost_buffer_bps,
                "min_edge_cost_ratio": self.settings.min_edge_cost_ratio,
                "active_capital_pct": self.settings.active_capital_pct,
                "max_abs_position_weight": self.settings.max_abs_position_weight,
                "max_gross_exposure": self.settings.max_gross_exposure,
                "max_positions": self.settings.max_positions,
                "max_order_notional": self.settings.max_order_notional,
                "min_trade_interval_seconds": self.settings.min_trade_interval_seconds,
                "max_order_book_take_ratio": self.settings.max_order_book_take_ratio,
                "max_adverse_funding_rate": self.settings.max_adverse_funding_rate,
                "loss_guard_max_loss": self.settings.loss_guard_max_loss,
                "loss_guard_max_fees": self.settings.loss_guard_max_fees,
                "loss_guard_max_trades": self.settings.loss_guard_max_trades,
                "loss_guard_recent_window_seconds": self.settings.loss_guard_recent_window_seconds,
                "loss_guard_max_recent_trades": self.settings.loss_guard_max_recent_trades,
                "loss_guard_max_recent_flips": self.settings.loss_guard_max_recent_flips,
                "loss_guard_symbol_max_loss": self.settings.loss_guard_symbol_max_loss,
                "loss_guard_symbol_max_fees": self.settings.loss_guard_symbol_max_fees,
                "loss_guard_symbol_max_trades": self.settings.loss_guard_symbol_max_trades,
                "order_style": self.settings.order_style,
                "maker_fee_rate": self.settings.maker_fee_rate,
                "maker_order_ttl_seconds": self.settings.maker_order_ttl_seconds,
                "maker_price_offset_bps": self.settings.maker_price_offset_bps,
            },
            "reason": plan.reason,
            "benchmark_context": plan.benchmark_context,
        }
        _write_status(payload)
        return payload
