from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
import re
import shutil
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
from .ledger import FillEvent, project_fills
from .crypto_learning import append_order_memory as append_crypto_order_memory


MAINNET_BASE_URL = "https://api.binance.com"
TESTNET_BASE_URL = "https://testnet.binance.vision"
BINANCE_OFFICIAL_SPOT_PUBLIC_FEE_SOURCE = "binance_official_public_spot_vip0_standard"
BINANCE_OFFICIAL_SPOT_PUBLIC_STANDARD_MAKER_RATE = 0.001
BINANCE_OFFICIAL_SPOT_PUBLIC_STANDARD_TAKER_RATE = 0.001
DEFAULT_LOSS_GUARD_MAX_ESTIMATED_FEES = 25.0
DEFAULT_LOSS_GUARD_MAX_TRADES = 80
DEFAULT_SYMBOL_LOSS_GUARD_MAX_ESTIMATED_FEES = 10.0
DEFAULT_SYMBOL_LOSS_GUARD_MAX_TRADES = 40
MIN_CONSERVATIVE_ENTRY_THRESHOLD = 0.49
MIN_CONSERVATIVE_ORDER_NOTIONAL = 101.25
MIN_RISK_OFF_REENTRY_COOLDOWN_SECONDS = 115200
MIN_CONSERVATIVE_TRADE_INTERVAL_SECONDS = 600
MAX_CONSERVATIVE_ACTIVE_CAPITAL_PCT = 0.15
MAX_CONSERVATIVE_POSITION_WEIGHT = 0.25
MAX_CONSERVATIVE_GROSS_EXPOSURE = 0.50
MAX_CONSERVATIVE_POSITIONS = 1
MAX_CONSERVATIVE_SPREAD_BPS = 8.192
MAX_CONSERVATIVE_ORDER_NOTIONAL = 2500.0
DEFAULT_LOSS_GUARD_IDLE_POLL_SECONDS = 300
MAX_LOSS_GUARD_IDLE_POLL_SECONDS = 3600
DEFAULT_RISK_OFF_IDLE_POLL_SECONDS = 300
MAX_RISK_OFF_IDLE_POLL_SECONDS = 3600
RUNTIME_DIR = Path(__file__).resolve().parents[2] / "runtime" / "crypto_ofim"
STATUS_FILE = RUNTIME_DIR / "status.json"
STATE_FILE = RUNTIME_DIR / "paper_state.json"
ORDERS_FILE = RUNTIME_DIR / "orders.jsonl"
FEATURES_FILE = RUNTIME_DIR / "features.jsonl"
EVENTS_FILE = RUNTIME_DIR / "events.jsonl"
ATTRIBUTION_FILE = RUNTIME_DIR / "crypto_attribution.json"
USER_STREAM_EVENTS_FILE = RUNTIME_DIR / "user_stream_events.jsonl"
USER_FILLS_FILE = RUNTIME_DIR / "user_fills.jsonl"
LEDGER_EPOCH_FILE = RUNTIME_DIR / "ledger_epoch.json"
LEDGER_RESET_BACKUP_DIR = RUNTIME_DIR / "ledger_reset_backups"
AUTO_PID_FILE = RUNTIME_DIR / "auto.pid"
AUTO_LOG_FILE = RUNTIME_DIR / "auto.log"
AUTO_LOCK_FILE = RUNTIME_DIR / "auto.lock"
RISK_OFF_EXIT_REASON = "risk_off_exit_bypass_rebalance_threshold"
URGENT_REDUCE_ONLY_PLAN_REASONS = {"benchmark_risk_off", "symbol_loss_guard"}
HOT_SYMBOL_SENTINELS = {"HOT", "HOT_USDT", "AUTO", "POPULAR"}
CORE_SYMBOL_SENTINELS = {"CORE", "CORE_USDT", "LIQUID", "LIQUID_USDT"}
TIGHT_SYMBOL_SENTINELS = {"TIGHT", "TIGHT_USDT", "TIGHT_CORE", "HFT_USDT", "FAST_USDT"}
DEFAULT_TIGHT_USDT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
)
DEFAULT_CORE_USDT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "LTCUSDT",
)
DEFAULT_EXCLUDED_BASES = {
    "USDC",
    "FDUSD",
    "TUSD",
    "BUSD",
    "DAI",
    "USDP",
    "USD1",
    "RLUSD",
    "UST",
    "USTC",
    "EUR",
    "TRY",
    "BRL",
    "GBP",
    "AUD",
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _state_file_for(settings: "CryptoOfimSettings") -> Path:
    if settings.mode == "testnet":
        return RUNTIME_DIR / "testnet_state.json"
    return STATE_FILE


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _guarded_idle_interval(
    *,
    env_var: str,
    default_seconds: int,
    max_seconds: int,
    base_interval: int,
) -> int:
    raw_idle = os.getenv(env_var)
    try:
        idle_interval = int(raw_idle) if raw_idle is not None else default_seconds
    except ValueError:
        idle_interval = default_seconds
    idle_interval = min(max_seconds, max(60, idle_interval))
    return max(base_interval, idle_interval)


def crypto_ofim_guarded_idle_poll_seconds(payload: dict[str, Any], requested_interval: int | float) -> int:
    """Back off auto polling when guards have already blocked all spot work."""
    try:
        base_interval = max(5, int(requested_interval))
    except (TypeError, ValueError):
        base_interval = 60
    if not isinstance(payload, dict):
        return base_interval

    benchmark_trend = payload.get("benchmark_trend")
    if not isinstance(benchmark_trend, dict):
        benchmark_trend = {}
    reason = str(payload.get("plan_reason") or benchmark_trend.get("reason") or "")
    if payload.get("target_weights") or payload.get("planned_orders") or payload.get("submitted_orders"):
        return base_interval

    if reason.startswith("loss_guard"):
        return _guarded_idle_interval(
            env_var="CRYPTO_OFIM_LOSS_GUARD_IDLE_POLL_SECONDS",
            default_seconds=DEFAULT_LOSS_GUARD_IDLE_POLL_SECONDS,
            max_seconds=MAX_LOSS_GUARD_IDLE_POLL_SECONDS,
            base_interval=base_interval,
        )
    if reason in {"benchmark_risk_off", "benchmark_risk_off_cooldown"}:
        return _guarded_idle_interval(
            env_var="CRYPTO_OFIM_RISK_OFF_IDLE_POLL_SECONDS",
            default_seconds=DEFAULT_RISK_OFF_IDLE_POLL_SECONDS,
            max_seconds=MAX_RISK_OFF_IDLE_POLL_SECONDS,
            base_interval=base_interval,
        )
    return base_interval


def _read_pid_file(path: Path) -> int:
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


def _iter_recent_event_rows(*, max_bytes: int = 512 * 1024) -> list[dict[str, Any]]:
    if not EVENTS_FILE.exists():
        return []
    try:
        with EVENTS_FILE.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - max_bytes)
            fh.seek(start)
            if start > 0:
                fh.readline()
            rows: list[dict[str, Any]] = []
            for raw in fh:
                try:
                    rows.append(json.loads(raw.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
            return rows
    except OSError:
        return []


def _recent_active_auto_cycle_pids(*, window_seconds: int = 300) -> set[int]:
    cutoff = datetime.now(UTC).timestamp() - max(30, int(window_seconds))
    active: set[int] = set()
    for row in _iter_recent_event_rows():
        event_type = str(row.get("event_type") or "")
        if event_type not in {"cycle_started", "cycle_completed", "loss_guard_triggered", "plan_generated"}:
            continue
        ts_seconds = _parse_order_ts_seconds(row.get("ts"))
        if ts_seconds is None or ts_seconds < cutoff:
            continue
        cycle_id = str(row.get("cycle_id") or "")
        if "-" not in cycle_id:
            continue
        try:
            pid = int(cycle_id.rsplit("-", 1)[-1])
        except ValueError:
            continue
        if pid != os.getpid() and _pid_running(pid):
            active.add(pid)
    return active


class CryptoOfimAutoInstance:
    """Own the singleton runtime lock for the crypto OFIM auto loop."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self._lock_handle: Any | None = None

    def __enter__(self) -> "CryptoOfimAutoInstance":
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        recent_active_pids = _recent_active_auto_cycle_pids()
        if recent_active_pids:
            pid = sorted(recent_active_pids)[0]
            raise CryptoOfimError(f"Crypto OFIM auto is already running with pid {pid}.")

        existing_pid = _read_pid_file(AUTO_PID_FILE)
        if existing_pid and existing_pid != self.pid and _pid_running(existing_pid):
            raise CryptoOfimError(f"Crypto OFIM auto is already running with pid {existing_pid}.")
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
            raise CryptoOfimError(f"Crypto OFIM auto is already running{suffix}.") from exc

        existing_pid = _read_pid_file(AUTO_PID_FILE)
        if existing_pid and existing_pid != self.pid and _pid_running(existing_pid):
            self._release_lock()
            raise CryptoOfimError(f"Crypto OFIM auto is already running with pid {existing_pid}.")

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


def crypto_ofim_auto_instance() -> CryptoOfimAutoInstance:
    return CryptoOfimAutoInstance()


def _parse_symbols(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
    raw_upper = raw.strip().upper()
    if raw_upper in TIGHT_SYMBOL_SENTINELS:
        return DEFAULT_TIGHT_USDT_SYMBOLS
    if raw_upper in CORE_SYMBOL_SENTINELS:
        return DEFAULT_CORE_USDT_SYMBOLS
    symbols = tuple(part.strip().upper().replace("/", "") for part in raw.split(",") if part.strip())
    return symbols or ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")


def _parse_optional_symbols(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip().upper().replace("/", "") for part in raw.split(",") if part.strip())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _parse_spot_signal_confirm_streak(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    parsed: dict[str, int] = {}
    for symbol, value in raw.items():
        count = max(0, int(_safe_float(value)))
        if count > 0:
            parsed[str(symbol).upper()] = count
    return parsed


def _zero_commission_section() -> dict[str, str]:
    return {"maker": "0", "taker": "0", "buyer": "0", "seller": "0"}


def _official_public_spot_commission(symbol: str) -> dict[str, Any]:
    return {
        "symbol": str(symbol or "").upper(),
        "source": BINANCE_OFFICIAL_SPOT_PUBLIC_FEE_SOURCE,
        "standardCommission": {
            "maker": f"{BINANCE_OFFICIAL_SPOT_PUBLIC_STANDARD_MAKER_RATE:.8f}",
            "taker": f"{BINANCE_OFFICIAL_SPOT_PUBLIC_STANDARD_TAKER_RATE:.8f}",
            "buyer": "0.00000000",
            "seller": "0.00000000",
        },
        "specialCommission": _zero_commission_section(),
        "taxCommission": _zero_commission_section(),
        "discount": {
            "enabledForAccount": False,
            "enabledForSymbol": False,
            "discountAsset": "BNB",
            "discount": "0.00000000",
        },
    }


def _commission_component(section: dict[str, Any], *, side: str, liquidity: str) -> float:
    side_key = "buyer" if side.upper() == "BUY" else "seller"
    return max(0.0, _safe_float(section.get(liquidity))) + max(0.0, _safe_float(section.get(side_key)))


def _effective_commission_rate(report: dict[str, Any], *, side: str, liquidity: str = "taker") -> float:
    standard = _commission_component(dict(report.get("standardCommission") or {}), side=side, liquidity=liquidity)
    discount = dict(report.get("discount") or {})
    if discount.get("enabledForAccount") and discount.get("enabledForSymbol"):
        standard *= max(0.0, 1.0 - max(0.0, _safe_float(discount.get("discount"))))
    special = _commission_component(dict(report.get("specialCommission") or {}), side=side, liquidity=liquidity)
    tax = _commission_component(dict(report.get("taxCommission") or {}), side=side, liquidity=liquidity)
    return max(0.0, standard + special + tax)


def _round_qty(qty: float) -> float:
    if qty <= 0:
        return 0.0
    return math.floor(qty * 1_000_000) / 1_000_000


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


def _base_asset(symbol: str, quote_asset: str) -> str:
    return symbol[: -len(quote_asset)] if symbol.endswith(quote_asset) else symbol


def _depth_request_weight(limit: int) -> int:
    if limit <= 100:
        return 5
    if limit <= 500:
        return 25
    if limit <= 1000:
        return 50
    return 250


def estimate_crypto_ofim_request_weight(settings: "CryptoOfimSettings", symbol_count: int | None = None) -> dict[str, Any]:
    """Conservative Binance REST request-weight estimate for one OFIM cycle."""
    count = symbol_count
    if count is None:
        if settings.hot_universe:
            count = max(1, min(100, settings.hot_count)) + 1
        else:
            count = len(set(settings.symbols))
    count = max(1, int(count))
    discovery_weight = 80 if settings.hot_universe else 0
    account_weight = 40 if settings.mode == "testnet" else 0
    if settings.use_ws_cache:
        # Normal path with the local WebSocket stream: REST only supplies
        # klines; order book and trades come from the local cache.
        per_symbol = 2
        depth_weight = 0
        market_source = "ws_cache"
    else:
        per_symbol = 2 + 2 + _depth_request_weight(settings.depth_limit) + 25 + 2 + 2
        depth_weight = _depth_request_weight(settings.depth_limit)
        market_source = "rest"
    order_weight = max(0, min(settings.max_positions, count))
    cycle = discovery_weight + account_weight + count * per_symbol + order_weight
    return {
        "symbol_count": count,
        "depth_weight": depth_weight,
        "per_symbol_weight": per_symbol,
        "cycle_weight": cycle,
        "limit_per_minute": 6000,
        "safe_poll_seconds": max(5, math.ceil(cycle * 60 / 4500)),
        "market_source": market_source,
    }


@dataclass(frozen=True)
class BinanceSymbolTradeRules:
    min_qty: Decimal
    max_qty: Decimal
    step_size: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class CryptoOfimSettings:
    mode: str
    base_url: str
    api_key: str | None
    api_secret: str | None
    symbols: tuple[str, ...]
    hot_universe: bool
    core_universe: bool
    hot_count: int
    excluded_symbols: tuple[str, ...]
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
    max_position_weight: float
    max_gross_exposure: float
    max_positions: int
    min_order_notional: float
    max_order_notional: float
    max_order_book_impact_bps: float
    max_order_book_take_ratio: float
    rebalance_threshold: float
    exit_confirm_cycles: int
    min_trade_interval_seconds: int
    min_flip_interval_seconds: int
    min_reentry_after_risk_off_seconds: int
    min_holding_seconds: int
    max_holding_seconds: int
    fee_rate: float
    slippage_bps: float
    recv_window_ms: int
    testnet_validate_only: bool
    use_ws_cache: bool = True
    use_user_stream: bool = True
    loss_guard_max_loss: float = 500.0
    loss_guard_max_estimated_fees: float = DEFAULT_LOSS_GUARD_MAX_ESTIMATED_FEES
    loss_guard_max_trades: int = DEFAULT_LOSS_GUARD_MAX_TRADES
    loss_guard_recent_window_seconds: int = 900
    loss_guard_max_recent_trades: int = 12
    loss_guard_max_recent_risk_off_exits: int = 3
    loss_guard_max_recent_flips: int = 3
    loss_guard_symbol_max_loss: float = 100.0
    loss_guard_symbol_max_estimated_fees: float = DEFAULT_SYMBOL_LOSS_GUARD_MAX_ESTIMATED_FEES
    loss_guard_symbol_max_trades: int = DEFAULT_SYMBOL_LOSS_GUARD_MAX_TRADES
    market_data: str = "mainnet"
    require_edge_over_cost: bool = True
    edge_bps_per_score: float = 150.0
    cost_buffer_bps: float = 6.0
    min_edge_cost_ratio: float = 1.25
    benchmark_soft_risk_score: float = -0.15
    benchmark_hard_risk_score: float = -0.45
    benchmark_soft_sma_band_bps: float = 50.0
    benchmark_soft_exposure_multiplier: float = 0.50
    liquidate_all_testnet_assets: bool = False
    signal_confirm_cycles: int = 1

    @property
    def submit_label(self) -> str:
        if self.mode == "paper":
            return "LOCAL PAPER"
        if self.testnet_validate_only:
            return "BINANCE TESTNET VALIDATE_ONLY"
        return "BINANCE TESTNET"

    @property
    def market_data_base_url(self) -> str:
        return MAINNET_BASE_URL

    @property
    def market_data_label(self) -> str:
        return "BINANCE MAINNET PUBLIC"


@dataclass(frozen=True)
class CryptoOfimFeature:
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
    conviction: float
    eligible: bool
    reason: str


@dataclass(frozen=True)
class CryptoOfimPlan:
    mode: str
    benchmark: str
    benchmark_score: float
    exposure: float
    target_weights: dict[str, float]
    features: list[CryptoOfimFeature]
    market_sources: dict[str, str] | None = None
    reason: str = "ok"
    benchmark_trend: dict[str, Any] | None = None


@dataclass(frozen=True)
class CryptoOfimOrder:
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
    target_weight: float
    current_value: float
    target_value: float
    response: dict[str, Any] | None = None


@dataclass
class CryptoPaperState:
    cash: float
    positions: dict[str, float]
    avg_cost: dict[str, float]
    realized_pnl: float
    fees_paid: float
    last_order_books: dict[str, dict[str, list[list[float]]]]
    empty_target_streak: int
    last_target_weights: dict[str, float]
    created_at: str
    updated_at: str
    ledger_epoch_id: str = ""
    signal_confirm_streak: dict[str, int] = field(default_factory=dict)

    @classmethod
    def fresh(cls, settings: CryptoOfimSettings) -> "CryptoPaperState":
        now = _utc_now()
        return cls(
            cash=float(settings.initial_cash),
            positions={},
            avg_cost={},
            realized_pnl=0.0,
            fees_paid=0.0,
            last_order_books={},
            empty_target_streak=0,
            last_target_weights={},
            created_at=now,
            updated_at=now,
            ledger_epoch_id=_current_ledger_epoch_id(),
            signal_confirm_streak={},
        )

    @classmethod
    def load(cls, settings: CryptoOfimSettings) -> "CryptoPaperState":
        state_file = _state_file_for(settings)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        if not state_file.exists():
            state = cls.fresh(settings)
            state.save(settings)
            return state
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        return cls(
            cash=_safe_float(raw.get("cash"), settings.initial_cash),
            positions={str(k): _safe_float(v) for k, v in (raw.get("positions") or {}).items()},
            avg_cost={str(k): _safe_float(v) for k, v in (raw.get("avg_cost") or {}).items()},
            realized_pnl=_safe_float(raw.get("realized_pnl")),
            fees_paid=_safe_float(raw.get("fees_paid")),
            last_order_books=raw.get("last_order_books") or {},
            empty_target_streak=int(_safe_float(raw.get("empty_target_streak"), 0.0)),
            last_target_weights={
                str(k): _safe_float(v)
                for k, v in (raw.get("last_target_weights") or {}).items()
                if _safe_float(v) > 0
            },
            created_at=str(raw.get("created_at") or _utc_now()),
            updated_at=str(raw.get("updated_at") or _utc_now()),
            ledger_epoch_id=str(raw.get("ledger_epoch_id") or _current_ledger_epoch_id()),
            signal_confirm_streak=_parse_spot_signal_confirm_streak(raw.get("signal_confirm_streak")),
        )

    def save(self, settings: CryptoOfimSettings | None = None) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        self.updated_at = _utc_now()
        state_file = _state_file_for(settings) if settings is not None else STATE_FILE
        state_file.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")


class CryptoOfimError(RuntimeError):
    pass


def crypto_ofim_auto_submit_enabled() -> bool:
    return _parse_bool(os.getenv("CRYPTO_OFIM_AUTO_ENABLED"), default=False)


def ensure_crypto_ofim_auto_submit_allowed(settings: CryptoOfimSettings, *, submit: bool) -> None:
    if submit and settings.mode == "testnet" and not crypto_ofim_auto_submit_enabled():
        raise CryptoOfimError(
            "CRYPTO_OFIM_AUTO_ENABLED=true is required before testnet auto-submit can start. "
            "Single-cycle planning remains available without this flag."
        )


def _is_transient_network_message(message: str) -> bool:
    text = str(message or "").lower()
    return any(
        marker in text
        for marker in (
            "network error",
            "read timed out",
            "timed out",
            "timeout",
            "connection aborted",
            "connection reset",
            "connection closed",
            "temporarily unavailable",
            "remote end closed connection",
        )
    )


def _friendly_transient_network_error() -> str:
    return "Binance Spot Testnet 临时网络超时，系统会在下一轮自动重试。"


def _sanitize_binance_error(message: Any) -> str:
    text = str(message or "")
    return re.sub(r"([?&]signature=)[^&\\s)]+", r"\1<redacted>", text)


class BinanceSpotClient:
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
        self._rules_cache: dict[str, BinanceSymbolTradeRules] = {}
        self._exchange_symbols_cache: set[str] | None = None
        self._exchange_info_cache: dict[str, Any] | None = None

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
                raise CryptoOfimError("Binance signed request requires CRYPTO_OFIM_API_KEY and CRYPTO_OFIM_API_SECRET.")
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
            detail = _sanitize_binance_error(last_exc)
            raise CryptoOfimError(f"Binance temporary network error while calling {method} {path}: {detail}") from last_exc
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise CryptoOfimError(f"Binance {method} {path} failed: {response.status_code} {detail}")
        try:
            return response.json()
        except ValueError:
            return response.text

    def ping(self) -> bool:
        self._request("GET", "/api/v3/ping")
        return True

    def server_time(self) -> int:
        data = self._request("GET", "/api/v3/time")
        return int(data.get("serverTime", 0))

    def tickers_24h(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/v3/ticker/24hr")
        return data if isinstance(data, list) else []

    def account(self) -> dict[str, Any]:
        return self._request("GET", "/api/v3/account", signed=True)

    def account_commission(self, symbol: str) -> dict[str, Any]:
        return self._request("GET", "/api/v3/account/commission", params={"symbol": symbol.upper()}, signed=True)

    def exchange_info(self) -> dict[str, Any]:
        if self._exchange_info_cache is None:
            self._exchange_info_cache = self._request("GET", "/api/v3/exchangeInfo")
        return self._exchange_info_cache

    def exchange_symbols(self) -> set[str]:
        if self._exchange_symbols_cache is not None:
            return set(self._exchange_symbols_cache)
        data = self.exchange_info()
        symbols = {
            str(row.get("symbol") or "").upper()
            for row in data.get("symbols", [])
            if str(row.get("symbol") or "").strip() and str(row.get("status") or "TRADING").upper() == "TRADING"
        }
        self._exchange_symbols_cache = symbols
        return set(symbols)

    def start_user_data_stream(self) -> str:
        data = self._request("POST", "/api/v3/userDataStream")
        listen_key = str(data.get("listenKey") or "")
        if not listen_key:
            raise CryptoOfimError("Binance user data stream did not return a listenKey.")
        return listen_key

    def keepalive_user_data_stream(self, listen_key: str) -> None:
        self._request("PUT", "/api/v3/userDataStream", params={"listenKey": listen_key})

    def close_user_data_stream(self, listen_key: str) -> None:
        self._request("DELETE", "/api/v3/userDataStream", params={"listenKey": listen_key})

    def symbol_trade_rules(self, symbol: str) -> BinanceSymbolTradeRules:
        symbol = symbol.upper()
        if symbol in self._rules_cache:
            return self._rules_cache[symbol]

        symbols = [
            row for row in self.exchange_info().get("symbols", [])
            if str(row.get("symbol") or "").upper() == symbol
        ]
        if not symbols:
            data = self._request("GET", "/api/v3/exchangeInfo", params={"symbol": symbol})
            symbols = data.get("symbols") or []
        if not symbols:
            raise CryptoOfimError(f"Binance exchangeInfo returned no symbol rules for {symbol}.")
        filters = {str(item.get("filterType")): item for item in symbols[0].get("filters", [])}
        lot = filters.get("LOT_SIZE") or {}
        market_lot = filters.get("MARKET_LOT_SIZE") or {}
        min_notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
        lot_min_qty = _to_decimal(lot.get("minQty"))
        market_min_qty = _to_decimal(market_lot.get("minQty"))
        lot_max_qty = _to_decimal(lot.get("maxQty"))
        market_max_qty = _to_decimal(market_lot.get("maxQty"))
        lot_step = _to_decimal(lot.get("stepSize"))
        market_step = _to_decimal(market_lot.get("stepSize"))
        rules = BinanceSymbolTradeRules(
            min_qty=_positive_max(lot_min_qty, market_min_qty),
            max_qty=_positive_min(lot_max_qty, market_max_qty),
            step_size=market_step if market_step > 0 else lot_step,
            min_notional=_to_decimal(min_notional_filter.get("minNotional")),
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
        rows = self._request("GET", "/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
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
        data = self._request("GET", "/api/v3/depth", params={"symbol": symbol, "limit": limit})
        return {
            "Bid": [[_safe_float(price), _safe_float(qty)] for price, qty in data.get("bids", [])],
            "Ask": [[_safe_float(price), _safe_float(qty)] for price, qty in data.get("asks", [])],
        }

    def depth_snapshot(self, symbol: str, *, limit: int = 1000) -> dict[str, Any]:
        """Raw Binance depth snapshot for local WebSocket book recovery."""
        return self._request("GET", "/api/v3/depth", params={"symbol": symbol, "limit": limit})

    def recent_trades(self, symbol: str, *, limit: int = 100) -> pd.DataFrame:
        rows = self._request("GET", "/api/v3/trades", params={"symbol": symbol, "limit": limit})
        out: list[dict[str, Any]] = []
        for row in rows:
            # Binance isBuyerMaker=True means buyer was maker, so aggressive side was SELL.
            direction = "SELL" if row.get("isBuyerMaker") else "BUY"
            out.append(
                {
                    "price": _safe_float(row.get("price")),
                    "volume": _safe_float(row.get("qty")),
                    "ticker_direction": direction,
                }
            )
        return pd.DataFrame(out, columns=["price", "volume", "ticker_direction"])

    def book_ticker(self, symbol: str) -> pd.Series:
        data = self._request("GET", "/api/v3/ticker/bookTicker", params={"symbol": symbol})
        bid = _safe_float(data.get("bidPrice"))
        ask = _safe_float(data.get("askPrice"))
        last = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
        return pd.Series({"last_price": last, "bid_price": bid, "ask_price": ask})

    def book_tickers(self) -> dict[str, dict[str, float]]:
        data = self._request("GET", "/api/v3/ticker/bookTicker")
        rows = data if isinstance(data, list) else []
        out: dict[str, dict[str, float]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            bid = _safe_float(row.get("bidPrice"))
            ask = _safe_float(row.get("askPrice"))
            last = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
            if symbol and last > 0:
                out[symbol] = {"last_price": last, "bid_price": bid, "ask_price": ask}
        return out

    def market_order(
        self,
        symbol: str,
        side: str,
        *,
        quantity: float | Decimal | str | None = None,
        quote_order_qty: float | Decimal | str | None = None,
        validate_only: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": symbol, "side": side.upper(), "type": "MARKET"}
        if quote_order_qty is not None:
            params["quoteOrderQty"] = quote_order_qty if isinstance(quote_order_qty, str) else _decimal_to_api_text(_to_decimal(quote_order_qty))
        elif quantity is not None:
            params["quantity"] = quantity if isinstance(quantity, str) else _decimal_to_api_text(_to_decimal(quantity))
        else:
            raise CryptoOfimError("market_order requires quantity or quote_order_qty.")
        path = "/api/v3/order/test" if validate_only else "/api/v3/order"
        started = time.perf_counter()
        result = self._request("POST", path, params=params, signed=True)
        latency_ms = (time.perf_counter() - started) * 1000.0
        if isinstance(result, dict):
            result = dict(result)
            result["_request_latency_ms"] = round(latency_ms, 3)
        return result


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


def load_crypto_ofim_settings(env_file: str | Path = ".env") -> CryptoOfimSettings:
    load_dotenv(dotenv_path=_resolve_env_file(env_file), override=True)
    mode = os.getenv("CRYPTO_OFIM_MODE", "paper").strip().lower()
    if mode not in {"paper", "testnet"}:
        raise ValueError("CRYPTO_OFIM_MODE must be paper or testnet. Live trading is intentionally not enabled here.")
    base_url = TESTNET_BASE_URL if mode == "testnet" else MAINNET_BASE_URL
    raw_symbols = os.getenv("CRYPTO_OFIM_SYMBOLS", "TIGHT_USDT")
    raw_symbols_upper = (raw_symbols or "").strip().upper()
    hot_universe = _parse_bool(os.getenv("CRYPTO_OFIM_HOT_UNIVERSE"), default=False)
    core_universe = _parse_bool(os.getenv("CRYPTO_OFIM_CORE_UNIVERSE"), default=False)
    tight_universe = raw_symbols_upper in TIGHT_SYMBOL_SENTINELS
    if tight_universe:
        core_universe = False
        hot_universe = False
    if raw_symbols_upper in CORE_SYMBOL_SENTINELS:
        core_universe = True
        hot_universe = False
    if raw_symbols_upper in HOT_SYMBOL_SENTINELS:
        hot_universe = True
        core_universe = False
    hot_count = int(os.getenv("CRYPTO_OFIM_HOT_COUNT", "5"))
    symbols = (
        DEFAULT_TIGHT_USDT_SYMBOLS
        if tight_universe
        else DEFAULT_CORE_USDT_SYMBOLS if core_universe else _parse_symbols(raw_symbols if not hot_universe else os.getenv("CRYPTO_OFIM_FALLBACK_SYMBOLS"))
    )
    fee_rate = BINANCE_OFFICIAL_SPOT_PUBLIC_STANDARD_TAKER_RATE
    settings = CryptoOfimSettings(
        mode=mode,
        base_url=base_url,
        api_key=(os.getenv("CRYPTO_OFIM_API_KEY") or "").strip() or None,
        api_secret=(os.getenv("CRYPTO_OFIM_API_SECRET") or "").strip() or None,
        symbols=symbols,
        hot_universe=hot_universe,
        core_universe=core_universe,
        hot_count=hot_count,
        excluded_symbols=_parse_optional_symbols(os.getenv("CRYPTO_OFIM_EXCLUDED_SYMBOLS")),
        benchmark=(os.getenv("CRYPTO_OFIM_BENCHMARK") or "BTCUSDT").strip().upper().replace("/", ""),
        quote_asset=(os.getenv("CRYPTO_OFIM_QUOTE_ASSET") or "USDT").strip().upper(),
        initial_cash=float(os.getenv("CRYPTO_OFIM_INITIAL_CASH", "10000")),
        active_capital=max(0.0, float(os.getenv("CRYPTO_OFIM_ACTIVE_CAPITAL", os.getenv("CRYPTO_OFIM_INITIAL_CASH", "10000")))),
        active_capital_pct=max(
            0.0,
            min(
                MAX_CONSERVATIVE_ACTIVE_CAPITAL_PCT,
                float(os.getenv("CRYPTO_OFIM_ACTIVE_CAPITAL_PCT", str(MAX_CONSERVATIVE_ACTIVE_CAPITAL_PCT))),
            ),
        ),
        lookback_bars=int(os.getenv("CRYPTO_OFIM_LOOKBACK_BARS", "60")),
        depth_limit=int(os.getenv("CRYPTO_OFIM_DEPTH_LIMIT", "100")),
        trade_limit=int(os.getenv("CRYPTO_OFIM_TRADE_LIMIT", "100")),
        entry_threshold=max(
            MIN_CONSERVATIVE_ENTRY_THRESHOLD,
            float(os.getenv("CRYPTO_OFIM_ENTRY_THRESHOLD", str(MIN_CONSERVATIVE_ENTRY_THRESHOLD))),
        ),
        exit_threshold=float(os.getenv("CRYPTO_OFIM_EXIT_THRESHOLD", "0.10")),
        max_score=float(os.getenv("CRYPTO_OFIM_MAX_SCORE", "0.60")),
        min_vol_acceleration=float(os.getenv("CRYPTO_OFIM_MIN_VOL_ACCELERATION", "1.05")),
        max_spread_bps=min(
            MAX_CONSERVATIVE_SPREAD_BPS,
            max(0.0, float(os.getenv("CRYPTO_OFIM_MAX_SPREAD_BPS", str(MAX_CONSERVATIVE_SPREAD_BPS)))),
        ),
        max_position_weight=min(
            MAX_CONSERVATIVE_POSITION_WEIGHT,
            max(
                0.0,
                float(os.getenv("CRYPTO_OFIM_MAX_POSITION_WEIGHT", str(MAX_CONSERVATIVE_POSITION_WEIGHT))),
            ),
        ),
        max_gross_exposure=min(
            MAX_CONSERVATIVE_GROSS_EXPOSURE,
            max(
                0.0,
                float(os.getenv("CRYPTO_OFIM_MAX_GROSS_EXPOSURE", str(MAX_CONSERVATIVE_GROSS_EXPOSURE))),
            ),
        ),
        max_positions=min(
            MAX_CONSERVATIVE_POSITIONS,
            max(0, int(os.getenv("CRYPTO_OFIM_MAX_POSITIONS", str(MAX_CONSERVATIVE_POSITIONS)))),
        ),
        min_order_notional=max(
            MIN_CONSERVATIVE_ORDER_NOTIONAL,
            float(os.getenv("CRYPTO_OFIM_MIN_ORDER_NOTIONAL", str(MIN_CONSERVATIVE_ORDER_NOTIONAL))),
        ),
        max_order_notional=min(
            MAX_CONSERVATIVE_ORDER_NOTIONAL,
            max(0.0, float(os.getenv("CRYPTO_OFIM_MAX_ORDER_NOTIONAL", str(MAX_CONSERVATIVE_ORDER_NOTIONAL)))),
        ),
        max_order_book_impact_bps=float(os.getenv("CRYPTO_OFIM_MAX_ORDER_BOOK_IMPACT_BPS", "25")),
        max_order_book_take_ratio=float(os.getenv("CRYPTO_OFIM_MAX_ORDER_BOOK_TAKE_RATIO", "0.25")),
        rebalance_threshold=float(os.getenv("CRYPTO_OFIM_REBALANCE_THRESHOLD", "0.08")),
        exit_confirm_cycles=max(1, int(os.getenv("CRYPTO_OFIM_EXIT_CONFIRM_CYCLES", "4"))),
        min_trade_interval_seconds=max(
            MIN_CONSERVATIVE_TRADE_INTERVAL_SECONDS,
            int(os.getenv("CRYPTO_OFIM_MIN_TRADE_INTERVAL_SECONDS", str(MIN_CONSERVATIVE_TRADE_INTERVAL_SECONDS))),
        ),
        min_flip_interval_seconds=max(0, int(os.getenv("CRYPTO_OFIM_MIN_FLIP_INTERVAL_SECONDS", "300"))),
        min_reentry_after_risk_off_seconds=max(
            MIN_RISK_OFF_REENTRY_COOLDOWN_SECONDS,
            int(
                os.getenv(
                    "CRYPTO_OFIM_MIN_REENTRY_AFTER_RISK_OFF_SECONDS",
                    str(MIN_RISK_OFF_REENTRY_COOLDOWN_SECONDS),
                )
            ),
        ),
        min_holding_seconds=max(0, int(os.getenv("CRYPTO_OFIM_MIN_HOLDING_SECONDS", "300"))),
        max_holding_seconds=max(0, int(os.getenv("CRYPTO_OFIM_MAX_HOLDING_SECONDS", "600"))),
        fee_rate=fee_rate,
        slippage_bps=float(os.getenv("CRYPTO_OFIM_SLIPPAGE_BPS", "5")),
        recv_window_ms=int(os.getenv("CRYPTO_OFIM_RECV_WINDOW_MS", "5000")),
        testnet_validate_only=_parse_bool(os.getenv("CRYPTO_OFIM_TESTNET_VALIDATE_ONLY"), default=False),
        use_ws_cache=_parse_bool(os.getenv("CRYPTO_OFIM_USE_WS_CACHE"), default=True),
        use_user_stream=_parse_bool(os.getenv("CRYPTO_OFIM_USE_USER_STREAM"), default=(mode == "testnet")),
        loss_guard_max_loss=max(0.0, float(os.getenv("CRYPTO_OFIM_LOSS_GUARD_MAX_LOSS", "500"))),
        loss_guard_max_estimated_fees=max(
            0.0,
            float(
                os.getenv(
                    "CRYPTO_OFIM_LOSS_GUARD_MAX_ESTIMATED_FEES",
                    str(DEFAULT_LOSS_GUARD_MAX_ESTIMATED_FEES),
                )
            ),
        ),
        loss_guard_max_trades=max(
            0,
            int(os.getenv("CRYPTO_OFIM_LOSS_GUARD_MAX_TRADES", str(DEFAULT_LOSS_GUARD_MAX_TRADES))),
        ),
        loss_guard_recent_window_seconds=max(0, int(os.getenv("CRYPTO_OFIM_LOSS_GUARD_RECENT_WINDOW_SECONDS", "900"))),
        loss_guard_max_recent_trades=max(0, int(os.getenv("CRYPTO_OFIM_LOSS_GUARD_MAX_RECENT_TRADES", "12"))),
        loss_guard_max_recent_risk_off_exits=max(0, int(os.getenv("CRYPTO_OFIM_LOSS_GUARD_MAX_RECENT_RISK_OFF_EXITS", "3"))),
        loss_guard_max_recent_flips=max(0, int(os.getenv("CRYPTO_OFIM_LOSS_GUARD_MAX_RECENT_FLIPS", "3"))),
        loss_guard_symbol_max_loss=max(0.0, float(os.getenv("CRYPTO_OFIM_LOSS_GUARD_SYMBOL_MAX_LOSS", "100"))),
        loss_guard_symbol_max_estimated_fees=max(
            0.0,
            float(
                os.getenv(
                    "CRYPTO_OFIM_LOSS_GUARD_SYMBOL_MAX_ESTIMATED_FEES",
                    str(DEFAULT_SYMBOL_LOSS_GUARD_MAX_ESTIMATED_FEES),
                )
            ),
        ),
        loss_guard_symbol_max_trades=max(
            0,
            int(
                os.getenv(
                    "CRYPTO_OFIM_LOSS_GUARD_SYMBOL_MAX_TRADES",
                    str(DEFAULT_SYMBOL_LOSS_GUARD_MAX_TRADES),
                )
            ),
        ),
        market_data="mainnet",
        require_edge_over_cost=_parse_bool(os.getenv("CRYPTO_OFIM_REQUIRE_EDGE_OVER_COST"), default=True),
        edge_bps_per_score=max(0.0, float(os.getenv("CRYPTO_OFIM_EDGE_BPS_PER_SCORE", "150"))),
        cost_buffer_bps=max(0.0, float(os.getenv("CRYPTO_OFIM_COST_BUFFER_BPS", "6"))),
        min_edge_cost_ratio=max(1.0, float(os.getenv("CRYPTO_OFIM_MIN_EDGE_COST_RATIO", "1.25"))),
        benchmark_soft_risk_score=float(os.getenv("CRYPTO_OFIM_BENCHMARK_SOFT_RISK_SCORE", "-0.15")),
        benchmark_hard_risk_score=float(os.getenv("CRYPTO_OFIM_BENCHMARK_HARD_RISK_SCORE", "-0.45")),
        benchmark_soft_sma_band_bps=float(os.getenv("CRYPTO_OFIM_BENCHMARK_SOFT_SMA_BAND_BPS", "50")),
        benchmark_soft_exposure_multiplier=max(
            0.0,
            min(1.0, float(os.getenv("CRYPTO_OFIM_BENCHMARK_SOFT_EXPOSURE_MULTIPLIER", "0.50"))),
        ),
        liquidate_all_testnet_assets=_parse_bool(os.getenv("CRYPTO_OFIM_LIQUIDATE_ALL_TESTNET_ASSETS"), default=False),
        signal_confirm_cycles=max(1, int(os.getenv("CRYPTO_OFIM_SIGNAL_CONFIRM_CYCLES", "2"))),
    )
    if settings.benchmark not in settings.symbols:
        object.__setattr__(settings, "symbols", tuple(dict.fromkeys([settings.benchmark, *settings.symbols])))
    if settings.mode == "testnet" and (not settings.api_key or not settings.api_secret):
        raise ValueError("CRYPTO_OFIM_MODE=testnet requires CRYPTO_OFIM_API_KEY and CRYPTO_OFIM_API_SECRET in local .env.")
    return settings


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _append_event(event_type: str, payload: dict[str, Any], *, cycle_id: str | None = None) -> None:
    """Best-effort event journal; accounting/order flow must never depend on it."""
    try:
        _append_jsonl(
            EVENTS_FILE,
            {
                "ts": _utc_now(),
                "event_type": event_type,
                "cycle_id": cycle_id,
                **payload,
            },
        )
    except Exception:
        return


def _append_order_memory_safe(
    orders: list["CryptoOfimOrder"],
    *,
    cycle_id: str,
    stage: str,
    settings: "CryptoOfimSettings",
    plan: "CryptoOfimPlan | None" = None,
    account: dict[str, Any] | None = None,
) -> None:
    try:
        append_crypto_order_memory(
            orders,
            cycle_id=cycle_id,
            stage=stage,
            settings=settings,
            plan=plan,
            account=account,
            order_memory_path=RUNTIME_DIR / "crypto_order_memory.jsonl",
        )
    except Exception as exc:
        _append_event("order_memory_error", {"error": f"{type(exc).__name__}: {exc}"}, cycle_id=cycle_id)


def _iso_from_millis(value: Any) -> str:
    millis = _safe_float(value)
    if millis <= 0:
        return _utc_now()
    return datetime.fromtimestamp(millis / 1000, tz=UTC).isoformat()


def _user_stream_fill_record(event: dict[str, Any], *, mode: str, quote_asset: str) -> dict[str, Any] | None:
    if str(event.get("e") or "") != "executionReport":
        return None
    if str(event.get("x") or "").upper() != "TRADE":
        return None
    symbol = str(event.get("s") or "").upper()
    side = str(event.get("S") or "").upper()
    qty = _safe_float(event.get("l"))
    price = _safe_float(event.get("L"))
    if not symbol or side not in {"BUY", "SELL"} or qty <= 0 or price <= 0:
        return None

    base_asset = _base_asset(symbol, quote_asset)
    commission = _safe_float(event.get("n"))
    commission_asset = str(event.get("N") or "").upper()
    fee_quote = 0.0
    adjusted_qty = qty
    adjusted_notional = qty * price
    if commission > 0:
        if commission_asset == quote_asset:
            fee_quote = commission
        elif commission_asset == base_asset:
            if side == "BUY":
                adjusted_qty = max(0.0, qty - commission)
                adjusted_notional = adjusted_qty * price
            elif side == "SELL":
                fee_quote = commission * price

    order_id = str(event.get("i") or "")
    trade_id = str(event.get("t") or "")
    event_id = f"binance_user_fill:{symbol}:{trade_id or order_id}:{side}:{event.get('T') or event.get('E')}"
    return {
        "ts": _iso_from_millis(event.get("T") or event.get("E")),
        "mode": mode,
        "symbol": symbol,
        "side": side,
        "quantity": adjusted_qty,
        "price": price,
        "notional": adjusted_notional,
        "fee": fee_quote,
        "commission": commission,
        "commission_asset": commission_asset,
        "order_id": order_id,
        "trade_id": trade_id,
        "client_order_id": str(event.get("c") or ""),
        "order_status": str(event.get("X") or ""),
        "execution_type": str(event.get("x") or ""),
        "event_id": event_id,
        "source": "binance_user_stream",
    }


def record_crypto_ofim_user_stream_event(event: dict[str, Any], *, mode: str, quote_asset: str) -> None:
    """Persist Binance User Data Stream events; fills become ledger inputs."""
    try:
        event_type = str(event.get("e") or "unknown")
        _append_jsonl(
            USER_STREAM_EVENTS_FILE,
            {
                "ts": _iso_from_millis(event.get("E")),
                "mode": mode,
                "event_type": event_type,
                "raw": event,
            },
        )
        fill = _user_stream_fill_record(event, mode=mode, quote_asset=quote_asset)
        if fill:
            _append_jsonl(USER_FILLS_FILE, fill)
            _append_event("exchange_fill", fill)
    except Exception:
        return


def _write_status(payload: dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": _utc_now(), **payload}
    STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def read_crypto_ofim_status() -> dict[str, Any]:
    if not STATUS_FILE.exists():
        return {"status": "not_started", "message": "crypto OFIM has not run yet"}
    return json.loads(STATUS_FILE.read_text(encoding="utf-8"))


def load_crypto_ofim_ledger_epoch() -> dict[str, Any]:
    if not LEDGER_EPOCH_FILE.exists():
        return {}
    try:
        return json.loads(LEDGER_EPOCH_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _crypto_epoch_id(payload: dict[str, Any]) -> str:
    material = {key: value for key, value in payload.items() if key != "epoch_id"}
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _current_ledger_epoch_id() -> str:
    epoch = load_crypto_ofim_ledger_epoch()
    if not epoch:
        return ""
    return str(epoch.get("epoch_id") or _crypto_epoch_id(epoch))


def set_crypto_ofim_ledger_epoch(
    settings: CryptoOfimSettings,
    *,
    reason: str,
    balances: dict[str, float] | None = None,
) -> dict[str, Any]:
    payload = {
        "ts": _utc_now(),
        "mode": settings.mode,
        "quote_asset": settings.quote_asset,
        "reason": reason,
        "balances": {asset: round(_safe_float(qty), 12) for asset, qty in sorted((balances or {}).items())},
    }
    payload["epoch_id"] = _crypto_epoch_id(payload)
    LEDGER_EPOCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_EPOCH_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _append_event("ledger_epoch_set", payload)
    return payload


def _safe_reset_slug(raw: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(raw or "").strip()).strip("._")
    return slug[:64] or "manual_reset"


def _backup_crypto_ofim_reset_files(settings: CryptoOfimSettings, reason: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # Derive from RUNTIME_DIR at call time so anything that redirects the
    # runtime directory (e.g. unit tests via monkeypatch) also redirects the
    # backups. A module-level constant frozen at import time previously let
    # test runs write backup folders into the real runtime directory.
    backup_root = RUNTIME_DIR / "ledger_reset_backups"
    base_dir = backup_root / f"{stamp}_{_safe_reset_slug(reason)}"
    backup_dir = base_dir
    suffix = 1
    while backup_dir.exists():
        suffix += 1
        backup_dir = Path(f"{base_dir}_{suffix}")
    backup_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "created_at": _utc_now(),
        "reason": reason,
        "files": [],
    }
    for path in (LEDGER_EPOCH_FILE, _state_file_for(settings), STATUS_FILE):
        row: dict[str, Any] = {
            "path": str(path),
            "exists": path.exists(),
        }
        if path.exists() and path.is_file():
            target = backup_dir / path.name
            shutil.copy2(path, target)
            row["backup_path"] = str(target)
            row["bytes"] = path.stat().st_size
        manifest["files"].append(row)
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return backup_dir


def reset_crypto_ofim_testnet_ledger_epoch(
    settings: CryptoOfimSettings | None = None,
    *,
    reason: str = "manual_testnet_ledger_reset",
    backup: bool = True,
    engine: "CryptoOfimEngine | None" = None,
) -> dict[str, Any]:
    """Start a fresh Binance Spot Testnet accounting epoch without submitting orders."""
    settings = settings or load_crypto_ofim_settings()
    if settings.mode != "testnet":
        raise CryptoOfimError("reset_crypto_ofim_testnet_ledger_epoch requires CRYPTO_OFIM_MODE=testnet.")

    engine = engine or CryptoOfimEngine(settings)
    account = engine.client.account()
    balances, free, locked = engine._account_balance_maps(account)
    backup_dir = _backup_crypto_ofim_reset_files(settings, reason) if backup else None
    epoch = set_crypto_ofim_ledger_epoch(settings, reason=reason, balances=balances)

    state = CryptoPaperState.fresh(settings)
    state.cash = _safe_float(balances.get(settings.quote_asset))
    state.positions = {}
    state.avg_cost = {}
    state.realized_pnl = 0.0
    state.fees_paid = 0.0
    state.last_order_books = {}
    state.empty_target_streak = 0
    state.last_target_weights = {}
    state.signal_confirm_streak = {}
    state.ledger_epoch_id = str(epoch.get("epoch_id") or "")
    state.save(settings)

    positive_non_quote = {
        asset: qty
        for asset, qty in balances.items()
        if asset != settings.quote_asset and _safe_float(qty) > 0
    }
    payload = {
        "status": "testnet_ledger_reset",
        "mode": settings.mode,
        "quote_asset": settings.quote_asset,
        "reason": reason,
        "orders_submitted": False,
        "execution_base_url": settings.base_url,
        "market_data_base_url": settings.market_data_base_url,
        "backup_dir": str(backup_dir) if backup_dir else "",
        "epoch": epoch,
        "state_file": str(_state_file_for(settings)),
        "state_cash": round(state.cash, 8),
        "balance_count": len(balances),
        "positive_non_quote_balance_count": len(positive_non_quote),
        "positive_non_quote_balance_sample": dict(list(sorted(positive_non_quote.items()))[:20]),
        "locked_balance_count": sum(1 for qty in locked.values() if _safe_float(qty) > 0),
        "free_quote_cash": round(_safe_float(free.get(settings.quote_asset)), 8),
        "target_weights": {},
        "planned_orders": [],
        "submitted_orders": [],
        "updated_at": _utc_now(),
    }
    _append_event("testnet_ledger_reset", payload)
    _write_status(payload)
    return payload


def _ledger_epoch_seconds(mode: str, quote_asset: str) -> float | None:
    epoch = load_crypto_ofim_ledger_epoch()
    if not epoch:
        return None
    if epoch.get("mode") and epoch.get("mode") != mode:
        return None
    if epoch.get("quote_asset") and epoch.get("quote_asset") != quote_asset:
        return None
    return _parse_order_ts_seconds(epoch.get("ts"))


def _after_ledger_epoch(ts: Any, cutoff_seconds: float | None) -> bool:
    if cutoff_seconds is None:
        return True
    ts_seconds = _parse_order_ts_seconds(ts)
    if ts_seconds is None:
        return False
    return ts_seconds >= cutoff_seconds


def _actual_testnet_fill(order: dict[str, Any], quote_asset: str) -> tuple[float, float, float]:
    symbol = str(order.get("symbol") or "").upper()
    side = str(order.get("side") or "").upper()
    response = order.get("response") if isinstance(order.get("response"), dict) else {}
    qty = _safe_float(response.get("executedQty"))
    notional = _safe_float(response.get("cummulativeQuoteQty"))
    fee_quote = 0.0
    base_asset = _base_asset(symbol, quote_asset)

    if qty > 0 and notional > 0:
        for fill in response.get("fills") or []:
            commission = _safe_float(fill.get("commission"))
            commission_asset = str(fill.get("commissionAsset") or "").upper()
            fill_price = _safe_float(fill.get("price"))
            if commission <= 0:
                continue
            if commission_asset == quote_asset:
                fee_quote += commission
            elif commission_asset == base_asset:
                if side == "BUY":
                    qty = max(0.0, qty - commission)
                elif side == "SELL" and fill_price > 0:
                    notional = max(0.0, notional - commission * fill_price)
        return qty, notional, fee_quote

    fallback_qty = _safe_float(order.get("quantity"))
    fallback_price = _safe_float(order.get("price"))
    fallback_fee = _safe_float(order.get("fee"))
    return fallback_qty, fallback_qty * fallback_price, fallback_fee


def _executed_order_records(
    mode: str,
    quote_asset: str = "USDT",
    order_log_path: Path | None = None,
) -> list[dict[str, Any]]:
    executed_status = "submitted_testnet" if mode == "testnet" else "filled_paper"
    path = order_log_path or ORDERS_FILE
    records: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    user_stream_order_ids: set[str] = set()
    epoch_seconds = _ledger_epoch_seconds(mode, quote_asset)

    if mode == "testnet" and USER_FILLS_FILE.exists():
        for line_no, line in enumerate(USER_FILLS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            try:
                fill = json.loads(line)
            except json.JSONDecodeError:
                continue
            if fill.get("mode") != mode:
                continue
            if not _after_ledger_epoch(fill.get("ts"), epoch_seconds):
                continue
            symbol = str(fill.get("symbol") or "").upper()
            side = str(fill.get("side") or "").upper()
            qty = _safe_float(fill.get("quantity"))
            price = _safe_float(fill.get("price"))
            notional = _safe_float(fill.get("notional"), qty * price)
            event_id = str(fill.get("event_id") or f"user_fill:{line_no}")
            if event_id in seen_event_ids:
                continue
            if not symbol or side not in {"BUY", "SELL"} or qty <= 0 or notional <= 0 or price <= 0:
                continue
            seen_event_ids.add(event_id)
            order_id = str(fill.get("order_id") or "")
            if order_id:
                user_stream_order_ids.add(order_id)
            records.append(
                {
                    "line_no": line_no,
                    "ts": str(fill.get("ts") or ""),
                    "symbol": symbol,
                    "side": side,
                    "quantity": qty,
                    "notional": notional,
                    "price": price,
                    "fee": _safe_float(fill.get("fee")),
                    "event_id": event_id,
                    "order_id": order_id,
                    "source": "user_stream",
                }
            )

    if not path.exists():
        return records

    for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
        try:
            order = json.loads(line)
        except json.JSONDecodeError:
            continue
        if order.get("mode") != mode or order.get("status") != executed_status:
            continue
        ts = str(order.get("ts") or "")
        if not _after_ledger_epoch(ts, epoch_seconds):
            continue
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").upper()
        response = order.get("response") if isinstance(order.get("response"), dict) else {}
        order_id = str(response.get("orderId") or order.get("orderId") or order.get("clientOrderId") or "")
        if order_id and order_id in user_stream_order_ids:
            # User Data Stream gives per-fill truth for this order; skip the
            # aggregate order response to avoid double-counting realized PnL.
            continue
        qty, notional, fee = _actual_testnet_fill(order, quote_asset) if mode == "testnet" else (
            _safe_float(order.get("quantity")),
            _safe_float(order.get("quantity")) * _safe_float(order.get("price")),
            _safe_float(order.get("fee")),
        )
        price = notional / qty if qty > 0 else 0.0
        event_id = str(order.get("orderId") or order.get("clientOrderId") or order_id or line_no)
        if event_id in seen_event_ids:
            continue
        if not symbol or side not in {"BUY", "SELL"} or qty <= 0 or notional <= 0 or price <= 0:
            continue
        seen_event_ids.add(event_id)
        records.append(
            {
                "line_no": line_no,
                "ts": ts,
                "symbol": symbol,
                "side": side,
                "quantity": qty,
                "notional": notional,
                "price": price,
                "fee": fee,
                "event_id": event_id,
                "order_id": order_id,
                "source": "order_log",
            }
        )
    return records


def _estimate_order_log_ledger(
    mode: str,
    quote_asset: str = "USDT",
    estimated_fee_rate: float = BINANCE_OFFICIAL_SPOT_PUBLIC_STANDARD_TAKER_RATE,
    commission_reports: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    estimated_fees_paid = 0.0
    estimated_fee_sources: set[str] = set()
    estimated_fee_rates: dict[str, dict[str, Any]] = {}
    events: list[FillEvent] = []
    records = _executed_order_records(mode, quote_asset)
    if not records:
        return {
            "realized_pnl": 0.0,
            "fees_paid": 0.0,
            "estimated_fees_paid": 0.0,
            "realized_pnl_after_estimated_fees": 0.0,
            "estimated_fee_source": BINANCE_OFFICIAL_SPOT_PUBLIC_FEE_SOURCE,
            "estimated_fee_rates": {},
            "trade_count": 0,
            "positions": {},
            "avg_cost": {},
            "cash_delta": 0.0,
            "unmatched_sells": {},
            "warnings": [],
            "audit_hash": "",
        }

    for record in records:
        symbol = record["symbol"]
        side = record["side"]
        report = (commission_reports or {}).get(symbol) or _official_public_spot_commission(symbol)
        rate = _effective_commission_rate(report, side=side, liquidity="taker")
        if rate <= 0:
            rate = max(0.0, estimated_fee_rate)
        estimated_fees_paid += record["notional"] * rate
        source = str(report.get("source") or BINANCE_OFFICIAL_SPOT_PUBLIC_FEE_SOURCE)
        estimated_fee_sources.add(source)
        estimated_fee_rates.setdefault(
            symbol,
            {
                "source": source,
                "buy_taker": round(_effective_commission_rate(report, side="BUY", liquidity="taker"), 10),
                "sell_taker": round(_effective_commission_rate(report, side="SELL", liquidity="taker"), 10),
            },
        )
        events.append(
            FillEvent(
                ts=record["ts"],
                symbol=symbol,
                side=side,
                quantity=record["quantity"],
                price=record["price"],
                fee=record["fee"],
                event_id=record["event_id"],
                source=f"crypto_ofim_{mode}",
            )
        )

    projection = project_fills(events)
    realized_after_estimated_fees = projection.realized_pnl + projection.fees_paid - estimated_fees_paid
    return {
        "realized_pnl": round(projection.realized_pnl, 8),
        "fees_paid": round(projection.fees_paid, 8),
        "estimated_fees_paid": round(estimated_fees_paid, 8),
        "realized_pnl_after_estimated_fees": round(realized_after_estimated_fees, 8),
        "estimated_fee_source": ",".join(sorted(estimated_fee_sources)) or BINANCE_OFFICIAL_SPOT_PUBLIC_FEE_SOURCE,
        "estimated_fee_rates": estimated_fee_rates,
        "trade_count": projection.trade_count,
        "positions": {symbol: round(qty, 8) for symbol, qty in projection.positions.items() if qty > 1e-10},
        "avg_cost": {symbol: round(cost, 8) for symbol, cost in projection.avg_cost.items() if projection.positions.get(symbol, 0.0) > 1e-10},
        "cash_delta": round(projection.cash_delta, 8),
        "unmatched_sells": projection.unmatched_sells,
        "warnings": list(projection.warnings),
        "audit_hash": projection.audit_hash,
    }


def build_crypto_ofim_balance_audit(
    settings: CryptoOfimSettings,
    balances: dict[str, float],
    *,
    active_symbols: list[str] | tuple[str, ...] | None = None,
    order_log_path: Path | None = None,
) -> dict[str, Any]:
    """Reconcile Binance Testnet faucet balances with strategy-owned balances.

    Binance Spot Testnet gives many virtual assets to every account. This audit
    separates those faucet assets from balances that can be explained by our own
    submitted order log.
    """
    quote_asset = settings.quote_asset
    active = set(active_symbols or settings.symbols)
    records = _executed_order_records(settings.mode, quote_asset, order_log_path)
    deltas: dict[str, float] = {}
    traded_symbols: set[str] = set()

    for record in records:
        symbol = record["symbol"]
        side = record["side"]
        qty = _safe_float(record["quantity"])
        notional = _safe_float(record["notional"])
        fee = _safe_float(record["fee"])
        base = _base_asset(symbol, quote_asset)
        traded_symbols.add(symbol)
        if side == "BUY":
            deltas[base] = deltas.get(base, 0.0) + qty
            deltas[quote_asset] = deltas.get(quote_asset, 0.0) - notional - fee
        elif side == "SELL":
            deltas[base] = deltas.get(base, 0.0) - qty
            deltas[quote_asset] = deltas.get(quote_asset, 0.0) + notional - fee

    ledger = _estimate_order_log_ledger(settings.mode, quote_asset)
    ledger_positions = ledger.get("positions") or {}
    active_bases = {_base_asset(symbol, quote_asset) for symbol in active}
    traded_bases = {_base_asset(symbol, quote_asset) for symbol in traded_symbols}
    all_assets = set(balances) | set(deltas) | {quote_asset} | active_bases | traded_bases
    rows: list[dict[str, Any]] = []

    for asset in sorted(all_assets):
        current_qty = _safe_float(balances.get(asset))
        net_delta = _safe_float(deltas.get(asset))
        inferred_start = current_qty - net_delta
        symbol = f"{asset}{quote_asset}" if asset != quote_asset else quote_asset
        if asset == quote_asset:
            role = "QUOTE_CASH"
        elif symbol in active:
            role = "ACTIVE_UNIVERSE"
        elif symbol in traded_symbols:
            role = "HISTORICALLY_TRADED"
        else:
            role = "TESTNET_UNUSED"

        ledger_qty = _safe_float(ledger_positions.get(symbol))
        strategy_counted_qty = current_qty if asset == quote_asset else min(current_qty, ledger_qty) if ledger_qty > 0 else 0.0
        ignored_testnet_qty = 0.0 if asset == quote_asset else max(0.0, current_qty - strategy_counted_qty)
        if abs(current_qty) <= 1e-12 and abs(inferred_start) <= 1e-12 and asset != quote_asset and asset not in active_bases:
            continue
        rows.append(
            {
                "role": role,
                "asset": asset,
                "symbol": symbol if asset != quote_asset else "",
                "active_universe": symbol in active,
                "historically_traded": symbol in traded_symbols,
                "inferred_start_qty": round(inferred_start, 12),
                "current_qty": round(current_qty, 12),
                "change_from_ofim_orders": round(net_delta, 12),
                "strategy_counted_qty": round(strategy_counted_qty, 12),
                "ignored_testnet_qty": round(ignored_testnet_qty, 12),
            }
        )

    role_order = {"QUOTE_CASH": 0, "ACTIVE_UNIVERSE": 1, "HISTORICALLY_TRADED": 2, "TESTNET_UNUSED": 3}
    rows.sort(key=lambda row: (role_order.get(str(row["role"]), 9), str(row["asset"])))
    return {
        "mode": settings.mode,
        "quote_asset": quote_asset,
        "active_symbols": sorted(active),
        "traded_symbols": sorted(traded_symbols),
        "rows": rows,
        "summary": {
            "asset_count": len(rows),
            "current_nonzero_assets": sum(1 for row in rows if abs(_safe_float(row["current_qty"])) > 1e-12),
            "active_universe_count": sum(1 for row in rows if row["role"] == "ACTIVE_UNIVERSE"),
            "historically_traded_count": sum(1 for row in rows if row["role"] == "HISTORICALLY_TRADED"),
            "testnet_unused_count": sum(1 for row in rows if row["role"] == "TESTNET_UNUSED"),
            "trade_count": len(records),
            "ledger_audit_hash": ledger.get("audit_hash", ""),
        },
    }


def _parse_order_ts_seconds(raw: Any) -> float | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.timestamp()


def _recent_symbol_trade_age_seconds(symbol: str, mode: str) -> float | None:
    recent = _recent_symbol_trade(symbol, mode)
    if recent is None:
        return None
    return recent["age_seconds"]


def _recent_benchmark_risk_off_age_seconds(mode: str) -> float | None:
    if not EVENTS_FILE.exists():
        return None
    latest_ts: float | None = None
    for line in EVENTS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("mode") != mode:
            continue
        if event.get("event_type") != "plan_generated":
            continue
        if event.get("reason") != "benchmark_risk_off":
            continue
        ts_seconds = _parse_order_ts_seconds(event.get("ts"))
        if ts_seconds is None:
            continue
        if latest_ts is None or ts_seconds > latest_ts:
            latest_ts = ts_seconds
    if latest_ts is None:
        return None
    return max(0.0, datetime.now(UTC).timestamp() - latest_ts)


def _recent_risk_off_exit_age_seconds(mode: str) -> float | None:
    if not ORDERS_FILE.exists():
        return None
    executed_status = "submitted_testnet" if mode == "testnet" else "filled_paper"
    latest_ts: float | None = None
    for line in ORDERS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            order = json.loads(line)
        except json.JSONDecodeError:
            continue
        if order.get("mode") != mode or order.get("status") != executed_status:
            continue
        if str(order.get("side") or "").upper() != "SELL":
            continue
        if RISK_OFF_EXIT_REASON not in str(order.get("reason") or ""):
            continue
        ts_seconds = _parse_order_ts_seconds(order.get("ts"))
        if ts_seconds is None:
            continue
        if latest_ts is None or ts_seconds > latest_ts:
            latest_ts = ts_seconds
    if latest_ts is None:
        return None
    return max(0.0, datetime.now(UTC).timestamp() - latest_ts)


def _recent_symbol_trade(symbol: str, mode: str) -> dict[str, Any] | None:
    if not ORDERS_FILE.exists():
        return None
    symbol = symbol.upper()
    executed_status = "submitted_testnet" if mode == "testnet" else "filled_paper"
    latest_ts: float | None = None
    latest_side = ""
    latest_status = ""
    latest_reason = ""
    for line in ORDERS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            order = json.loads(line)
        except json.JSONDecodeError:
            continue
        if order.get("mode") != mode or order.get("status") != executed_status:
            continue
        if str(order.get("symbol") or "").upper() != symbol:
            continue
        ts_seconds = _parse_order_ts_seconds(order.get("ts"))
        if ts_seconds is None:
            continue
        if latest_ts is None or ts_seconds > latest_ts:
            latest_ts = ts_seconds
            latest_side = str(order.get("side") or "").upper()
            latest_status = str(order.get("status") or "")
            latest_reason = str(order.get("reason") or "")
    if latest_ts is None:
        return None
    return {
        "age_seconds": max(0.0, datetime.now(UTC).timestamp() - latest_ts),
        "side": latest_side,
        "status": latest_status,
        "reason": latest_reason,
        "ts_seconds": latest_ts,
    }


def _open_position_age_seconds(symbol: str, mode: str, quote_asset: str = "USDT") -> float | None:
    symbol = symbol.upper()
    open_ts: float | None = None
    net_qty = 0.0
    for record in _executed_order_records(mode, quote_asset):
        if str(record.get("symbol") or "").upper() != symbol:
            continue
        ts_seconds = _parse_order_ts_seconds(record.get("ts"))
        side = str(record.get("side") or "").upper()
        qty = _safe_float(record.get("quantity"))
        if side == "BUY":
            if net_qty <= 1e-10:
                open_ts = ts_seconds
            net_qty += qty
        elif side == "SELL":
            net_qty = max(0.0, net_qty - qty)
            if net_qty <= 1e-10:
                open_ts = None
                net_qty = 0.0
    if net_qty <= 1e-10 or open_ts is None:
        return None
    return max(0.0, datetime.now(UTC).timestamp() - open_ts)


def _book_summary(order_book: dict[str, list[list[float]]] | None) -> dict[str, Any]:
    bids = list((order_book or {}).get("Bid") or [])
    asks = list((order_book or {}).get("Ask") or [])

    def _sum_qty(rows: list[list[float]], levels: int) -> float:
        return round(sum(_safe_float(row[1]) for row in rows[:levels]), 8)

    best_bid = _safe_float(bids[0][0]) if bids else 0.0
    best_ask = _safe_float(asks[0][0]) if asks else 0.0
    mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else 0.0
    return {
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "best_bid": round(best_bid, 8),
        "best_ask": round(best_ask, 8),
        "mid": round(mid, 8),
        "spread_bps": round((best_ask - best_bid) / mid * 10_000, 6) if mid > 0 else None,
        "bid_qty_5": _sum_qty(bids, 5),
        "ask_qty_5": _sum_qty(asks, 5),
        "bid_qty_20": _sum_qty(bids, 20),
        "ask_qty_20": _sum_qty(asks, 20),
    }


def _strategy_sizing_equity(settings: CryptoOfimSettings, equity: float) -> float:
    equity = max(0.0, float(equity or 0.0))
    pct = max(0.0, min(1.0, float(getattr(settings, "active_capital_pct", 0.0) or 0.0)))
    pct_cap = equity * pct if pct > 0 else equity
    abs_cap = max(0.0, float(settings.active_capital or 0.0))
    if abs_cap > 0 and pct > 0:
        return max(0.0, min(equity, abs_cap, pct_cap))
    if abs_cap > 0:
        return max(0.0, min(equity, abs_cap))
    return max(0.0, min(equity, pct_cap))


def _ledger_starting_equity(settings: CryptoOfimSettings) -> float:
    if settings.mode == "testnet":
        epoch = load_crypto_ofim_ledger_epoch()
        balances = epoch.get("balances") if isinstance(epoch.get("balances"), dict) else {}
        quote_start = _safe_float(balances.get(settings.quote_asset))
        if quote_start > 0:
            return quote_start
    return max(0.0, float(settings.initial_cash or 0.0))


def _book_executable_notional(
    order_book: dict[str, list[list[float]]] | None,
    side: str,
    reference_price: float,
    max_impact_bps: float,
) -> float:
    if not order_book or reference_price <= 0:
        return 0.0
    side = side.upper()
    rows = list((order_book or {}).get("Ask" if side == "BUY" else "Bid") or [])
    if not rows:
        return 0.0
    impact = max(0.0, max_impact_bps) / 10_000
    limit_price = reference_price * (1 + impact) if side == "BUY" else reference_price * (1 - impact)
    notional = 0.0
    for row in rows:
        price = _safe_float(row[0])
        qty = _safe_float(row[1])
        if price <= 0 or qty <= 0:
            continue
        if side == "BUY" and price > limit_price:
            break
        if side == "SELL" and price < limit_price:
            break
        notional += price * qty
    return max(0.0, notional)


def _bars_summary(bars: pd.DataFrame) -> dict[str, Any]:
    if bars.empty:
        return {"bar_count": 0}
    close = pd.to_numeric(bars.get("close"), errors="coerce")
    volume = pd.to_numeric(bars.get("volume"), errors="coerce")
    return {
        "bar_count": int(len(bars)),
        "first_time": str(bars.iloc[0].get("time_key", "")),
        "last_time": str(bars.iloc[-1].get("time_key", "")),
        "last_close": round(_safe_float(close.iloc[-1] if len(close) else 0.0), 8),
        "last_volume": round(_safe_float(volume.iloc[-1] if len(volume) else 0.0), 8),
    }


def _benchmark_sma_trend(
    bars_1m: pd.DataFrame,
    snapshot: pd.Series,
    *,
    window: int = 60,
) -> dict[str, Any]:
    closes = pd.to_numeric(bars_1m.get("close"), errors="coerce").dropna() if not bars_1m.empty else pd.Series(dtype=float)
    last_price = _safe_float(snapshot.get("last_price") if snapshot is not None else 0.0)
    if last_price <= 0 and not closes.empty:
        last_price = float(closes.iloc[-1])
    if closes.empty or last_price <= 0:
        return {
            "ok": True,
            "reason": "trend_unavailable",
            "last_price": round(last_price, 8),
            "sma": 0.0,
            "window": 0,
        }
    actual_window = max(1, min(int(window), len(closes)))
    sma = float(closes.tail(actual_window).mean())
    return {
        "ok": bool(last_price >= sma),
        "reason": "benchmark_above_sma" if last_price >= sma else "benchmark_below_sma",
        "last_price": round(last_price, 8),
        "sma": round(sma, 8),
        "window": actual_window,
    }


def _benchmark_risk_budget(
    settings: CryptoOfimSettings,
    benchmark_score: float,
    benchmark_trend: dict[str, Any],
) -> tuple[float, str, dict[str, Any]]:
    base_scale = min(1.0, max(0.0, 0.5 + benchmark_score))
    hard_score = float(getattr(settings, "benchmark_hard_risk_score", -0.45))
    soft_score = float(getattr(settings, "benchmark_soft_risk_score", -0.15))
    soft_band_bps = max(0.0, float(getattr(settings, "benchmark_soft_sma_band_bps", 50.0)))
    soft_multiplier = max(0.0, min(1.0, float(getattr(settings, "benchmark_soft_exposure_multiplier", 0.50))))
    last_price = _safe_float(benchmark_trend.get("last_price"))
    sma = _safe_float(benchmark_trend.get("sma"))
    sma_gap_bps = ((last_price - sma) / sma * 10_000) if last_price > 0 and sma > 0 else None
    trend_below_sma = not bool(benchmark_trend.get("ok", True))
    hard_risk = benchmark_score <= hard_score
    if trend_below_sma and sma_gap_bps is not None and sma_gap_bps < -soft_band_bps:
        hard_risk = True
    reason = "ok"
    scale = base_scale
    if hard_risk:
        reason = "benchmark_risk_off"
        scale = 0.0
    elif benchmark_score <= soft_score or trend_below_sma:
        reason = "benchmark_soft_risk"
        scale *= soft_multiplier
    return (
        max(0.0, min(1.0, scale)),
        reason,
        {
            "sma_gap_bps": round(sma_gap_bps, 6) if sma_gap_bps is not None else None,
            "risk_mode": reason,
            "risk_base_exposure_scale": round(base_scale, 6),
            "risk_exposure_scale": round(max(0.0, min(1.0, scale)), 6),
            "soft_risk_score": soft_score,
            "hard_risk_score": hard_score,
            "soft_sma_band_bps": soft_band_bps,
            "soft_exposure_multiplier": soft_multiplier,
        },
    )


def _recent_order_churn_stats(mode: str, window_seconds: int) -> dict[str, Any]:
    stats = {
        "window_seconds": max(0, int(window_seconds)),
        "trade_count": 0,
        "risk_off_exit_count": 0,
        "flip_count": 0,
        "symbols": [],
    }
    if stats["window_seconds"] <= 0 or not ORDERS_FILE.exists():
        return stats
    now = datetime.now(UTC).timestamp()
    executed_status = "submitted_testnet" if mode == "testnet" else "filled_paper"
    latest_side_by_symbol: dict[str, str] = {}
    symbols: set[str] = set()
    for line in ORDERS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            order = json.loads(line)
        except json.JSONDecodeError:
            continue
        if order.get("mode") != mode or order.get("status") != executed_status:
            continue
        ts_seconds = _parse_order_ts_seconds(order.get("ts"))
        if ts_seconds is None or now - ts_seconds > stats["window_seconds"]:
            continue
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").upper()
        reason = str(order.get("reason") or "")
        stats["trade_count"] += 1
        if symbol:
            symbols.add(symbol)
        if side == "SELL" and RISK_OFF_EXIT_REASON in reason:
            stats["risk_off_exit_count"] += 1
        if symbol and side in {"BUY", "SELL"}:
            previous_side = latest_side_by_symbol.get(symbol)
            if previous_side and previous_side != side:
                stats["flip_count"] += 1
            latest_side_by_symbol[symbol] = side
    stats["symbols"] = sorted(symbols)
    return stats


def _material_position_reconciliation_rows(
    settings: CryptoOfimSettings,
    account: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = account.get("position_reconciliation")
    if not isinstance(rows, list):
        return []
    prices = account.get("prices") if isinstance(account.get("prices"), dict) else {}
    min_notional = max(1.0, _safe_float(getattr(settings, "min_order_notional", 0.0)))
    material: list[dict[str, Any]] = []
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        symbol = str(raw_row.get("symbol") or "").upper()
        missing_qty = max(0.0, _safe_float(raw_row.get("missing_qty")))
        extra_qty = max(0.0, _safe_float(raw_row.get("extra_qty")))
        qty_gap = max(missing_qty, extra_qty)
        if qty_gap <= 0:
            continue
        price = _safe_float(prices.get(symbol))
        estimated_notional = qty_gap * price if price > 0 else None
        if estimated_notional is not None and estimated_notional < min_notional:
            continue
        row = dict(raw_row)
        row["threshold_notional"] = round(min_notional, 8)
        if estimated_notional is not None:
            row["estimated_notional"] = round(estimated_notional, 8)
        material.append(row)
    return material


def _loss_guard_breach(settings: CryptoOfimSettings, account: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    breaches: list[str] = []
    primary_net_pnl = _safe_float(account.get("primary_net_pnl"), _safe_float(account.get("net_pnl")))
    estimated_fees_paid = _safe_float(account.get("estimated_fees_paid"), _safe_float(account.get("fees_paid")))
    trade_count = int(_safe_float(account.get("trade_count")))
    max_loss = max(0.0, float(getattr(settings, "loss_guard_max_loss", 0.0)))
    max_estimated_fees = max(0.0, float(getattr(settings, "loss_guard_max_estimated_fees", 0.0)))
    max_trades = max(0, int(getattr(settings, "loss_guard_max_trades", 0)))
    recent_window_seconds = max(0, int(getattr(settings, "loss_guard_recent_window_seconds", 0)))
    max_recent_trades = max(0, int(getattr(settings, "loss_guard_max_recent_trades", 0)))
    max_recent_risk_off_exits = max(0, int(getattr(settings, "loss_guard_max_recent_risk_off_exits", 0)))
    max_recent_flips = max(0, int(getattr(settings, "loss_guard_max_recent_flips", 0)))
    recent_churn = _recent_order_churn_stats(settings.mode, recent_window_seconds)
    cash_reconciliation = account.get("cash_reconciliation") if isinstance(account.get("cash_reconciliation"), dict) else {}
    unexplained_quote_delta = _safe_float(cash_reconciliation.get("unexplained_quote_delta"))
    position_reconciliation = _material_position_reconciliation_rows(settings, account)

    if max_loss > 0 and primary_net_pnl <= -max_loss:
        breaches.append("loss")
    if max_loss > 0 and cash_reconciliation.get("ok") is False and unexplained_quote_delta <= -max_loss:
        breaches.append("cash_reconciliation")
    if position_reconciliation:
        breaches.append("position_reconciliation")
    if max_estimated_fees > 0 and estimated_fees_paid >= max_estimated_fees:
        breaches.append("estimated_fees")
    if max_trades > 0 and trade_count >= max_trades:
        breaches.append("trade_count")
    if max_recent_trades > 0 and int(recent_churn.get("trade_count", 0) or 0) >= max_recent_trades:
        breaches.append("recent_trades")
    if (
        max_recent_risk_off_exits > 0
        and int(recent_churn.get("risk_off_exit_count", 0) or 0) >= max_recent_risk_off_exits
    ):
        breaches.append("recent_risk_off_exits")
    if max_recent_flips > 0 and int(recent_churn.get("flip_count", 0) or 0) >= max_recent_flips:
        breaches.append("recent_flips")
    if not breaches:
        return "", {}
    reason = "loss_guard_" + "_".join(breaches)
    detail = {
        "reason": reason,
        "breaches": breaches,
        "primary_net_pnl": round(primary_net_pnl, 8),
        "cash_reconciliation": cash_reconciliation,
        "position_reconciliation": position_reconciliation,
        "estimated_fees_paid": round(estimated_fees_paid, 8),
        "trade_count": trade_count,
        "max_loss": max_loss,
        "max_estimated_fees": max_estimated_fees,
        "max_trades": max_trades,
        "recent_window_seconds": recent_window_seconds,
        "recent_trade_count": int(recent_churn.get("trade_count", 0) or 0),
        "max_recent_trades": max_recent_trades,
        "recent_risk_off_exit_count": int(recent_churn.get("risk_off_exit_count", 0) or 0),
        "max_recent_risk_off_exits": max_recent_risk_off_exits,
        "recent_flip_count": int(recent_churn.get("flip_count", 0) or 0),
        "max_recent_flips": max_recent_flips,
        "recent_symbols": recent_churn.get("symbols", []),
        "action": "reduce_only_no_new_entries",
    }
    if (
        "cash_reconciliation" in breaches
        and trade_count == 0
        and abs(_safe_float(cash_reconciliation.get("ledger_cash_delta"))) < 1e-9
    ):
        # Zero trades since the epoch yet the actual quote balance no longer
        # matches it: almost certainly an external balance change (Binance
        # Spot Testnet wipes balances periodically), not a trading loss.
        # Without this hint the guard blocks new entries indefinitely and the
        # operator has no pointer to the fix.
        detail["likely_cause"] = "external_balance_change"
        detail["hint"] = (
            "Quote balance changed with zero trades since the ledger epoch "
            "(Binance Testnet resets balances periodically). If expected, "
            "re-anchor with: taa-futu crypto-ofim-ledger-reset "
            "--reason testnet_balance_reset"
        )
    return reason, detail


def _learning_loss_guard_breach(settings: CryptoOfimSettings) -> tuple[str, dict[str, Any]]:
    max_loss = max(0.0, float(getattr(settings, "loss_guard_max_loss", 0.0)))
    max_estimated_fees = max(0.0, float(getattr(settings, "loss_guard_max_estimated_fees", 0.0)))
    max_trades = max(0, int(getattr(settings, "loss_guard_max_trades", 0)))
    if max_loss <= 0 and max_estimated_fees <= 0 and max_trades <= 0:
        return "", {}
    if not ATTRIBUTION_FILE.exists():
        return "", {}
    try:
        payload = json.loads(ATTRIBUTION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", {}
    total = payload.get("total") if isinstance(payload, dict) else {}
    if not isinstance(total, dict):
        return "", {}

    net_pnl = _safe_float(total.get("net_pnl"))
    estimated_fees = _safe_float(total.get("estimated_fees"), _safe_float(total.get("fees")))
    trades = int(_safe_float(total.get("trades")))
    source = "crypto_attribution"
    if trades <= 0 and estimated_fees <= 0:
        order_quality = payload.get("order_quality") if isinstance(payload.get("order_quality"), dict) else {}
        submitted_estimated_fees = _safe_float(order_quality.get("submitted_estimated_fees"))
        submitted_records = int(_safe_float(order_quality.get("submitted_records")))
        if submitted_records > 0 and submitted_estimated_fees > 0:
            estimated_fees = submitted_estimated_fees
            trades = submitted_records
            source = "crypto_attribution_order_memory"
    breaches: list[str] = []
    if max_loss > 0 and net_pnl <= -max_loss:
        breaches.append("loss")
    if max_estimated_fees > 0 and estimated_fees >= max_estimated_fees:
        breaches.append("estimated_fees")
    if max_trades > 0 and trades >= max_trades:
        breaches.append("trade_count")
    if not breaches:
        return "", {}
    reason = "loss_guard_learning_" + "_".join(breaches)
    return reason, {
        "reason": reason,
        "breaches": breaches,
        "source": source,
        "generated_at": payload.get("generated_at"),
        "net_pnl": round(net_pnl, 8),
        "estimated_fees": round(estimated_fees, 8),
        "trades": trades,
        "max_loss": max_loss,
        "max_estimated_fees": max_estimated_fees,
        "max_trades": max_trades,
        "action": "reduce_only_no_new_entries",
    }


def _symbol_loss_guard_breaches(settings: CryptoOfimSettings, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
    max_loss = max(0.0, float(getattr(settings, "loss_guard_symbol_max_loss", 0.0)))
    max_estimated_fees = max(0.0, float(getattr(settings, "loss_guard_symbol_max_estimated_fees", 0.0)))
    max_trades = max(0, int(getattr(settings, "loss_guard_symbol_max_trades", 0)))
    if max_loss <= 0 and max_estimated_fees <= 0 and max_trades <= 0:
        return {}
    if not ATTRIBUTION_FILE.exists():
        return {}
    try:
        payload = json.loads(ATTRIBUTION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    by_symbol = payload.get("by_symbol") if isinstance(payload, dict) else {}
    if not isinstance(by_symbol, dict):
        by_symbol = {}
    order_quality = payload.get("order_quality") if isinstance(payload.get("order_quality"), dict) else {}
    submitted_cost_by_symbol = (
        order_quality.get("submitted_cost_by_symbol")
        if isinstance(order_quality.get("submitted_cost_by_symbol"), dict)
        else {}
    )

    blocked: dict[str, dict[str, Any]] = {}
    for symbol in {str(item or "").upper() for item in symbols}:
        stats = by_symbol.get(symbol)
        source = "crypto_attribution"
        if not isinstance(stats, dict):
            stats = {}
        net_pnl = _safe_float(stats.get("net_pnl"))
        estimated_fees = _safe_float(stats.get("estimated_fees"), _safe_float(stats.get("fees")))
        trades = int(_safe_float(stats.get("trades")))
        if estimated_fees <= 0 and trades <= 0:
            cost_stats = submitted_cost_by_symbol.get(symbol)
            if not isinstance(cost_stats, dict):
                continue
            estimated_fees = _safe_float(cost_stats.get("estimated_fees"))
            trades = int(_safe_float(cost_stats.get("records")))
            source = "crypto_attribution_order_memory"
        if estimated_fees <= 0 and trades <= 0:
            continue
        breaches: list[str] = []
        if max_loss > 0 and net_pnl <= -max_loss:
            breaches.append("loss")
        if max_estimated_fees > 0 and estimated_fees >= max_estimated_fees:
            breaches.append("estimated_fees")
        if max_trades > 0 and trades >= max_trades:
            breaches.append("trade_count")
        if not breaches:
            continue
        reason = "symbol_loss_guard_" + "_".join(breaches)
        blocked[symbol] = {
            "reason": reason,
            "breaches": breaches,
            "symbol": symbol,
            "net_pnl": round(net_pnl, 8),
            "estimated_fees": round(estimated_fees, 8),
            "trades": trades,
            "source": source,
            "generated_at": payload.get("generated_at"),
            "max_loss": max_loss,
            "max_estimated_fees": max_estimated_fees,
            "max_trades": max_trades,
            "action": "block_new_entry_reduce_only_allowed",
        }
    return blocked


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


def reset_crypto_ofim_paper(settings: CryptoOfimSettings | None = None) -> CryptoPaperState:
    settings = settings or load_crypto_ofim_settings()
    state = CryptoPaperState.fresh(settings)
    state.save(settings)
    _write_status({"status": "reset", "mode": settings.mode, "paper_cash": state.cash})
    return state


def _weight_with_cap(score_map: dict[str, float], total_exposure: float, max_weight: float) -> dict[str, float]:
    if total_exposure <= 0 or not score_map:
        return {}
    remaining = score_map.copy()
    weights: dict[str, float] = {}
    budget = total_exposure
    while remaining and budget > 1e-12:
        total_score = sum(v for v in remaining.values() if v > 0)
        if total_score <= 0:
            break
        capped: list[str] = []
        for symbol, score in remaining.items():
            proposed = budget * score / total_score
            if proposed > max_weight + 1e-9:
                weights[symbol] = max_weight
                budget -= max_weight
                capped.append(symbol)
        if not capped:
            for symbol, score in remaining.items():
                weights[symbol] = budget * score / total_score
            break
        for symbol in capped:
            remaining.pop(symbol, None)
    return {symbol: round(weight, 6) for symbol, weight in weights.items() if weight > 0}


def _entry_edge_cost_context(settings: CryptoOfimSettings, feature: CryptoOfimFeature) -> dict[str, Any]:
    edge_bps = max(0.0, float(feature.score)) * max(0.0, float(settings.edge_bps_per_score))
    round_trip_cost_bps = (
        2.0 * max(0.0, float(settings.fee_rate)) * 10_000.0
        + 2.0 * max(0.0, float(settings.slippage_bps))
        + max(0.0, float(feature.spread_bps))
        + max(0.0, float(settings.cost_buffer_bps))
    )
    min_edge_cost_ratio = max(1.0, float(getattr(settings, "min_edge_cost_ratio", 1.0) or 1.0))
    required_edge_bps = round_trip_cost_bps * min_edge_cost_ratio
    return {
        "estimated_edge_bps": round(edge_bps, 6),
        "estimated_round_trip_cost_bps": round(round_trip_cost_bps, 6),
        "required_edge_bps": round(required_edge_bps, 6),
        "min_edge_cost_ratio": round(min_edge_cost_ratio, 6),
        "edge_bps_per_score": max(0.0, float(settings.edge_bps_per_score)),
        "fee_rate": max(0.0, float(settings.fee_rate)),
        "slippage_bps": max(0.0, float(settings.slippage_bps)),
        "spread_bps": round(max(0.0, float(feature.spread_bps)), 6),
        "cost_buffer_bps": max(0.0, float(settings.cost_buffer_bps)),
    }


def _passes_entry_edge_cost_gate(settings: CryptoOfimSettings, feature: CryptoOfimFeature) -> tuple[bool, dict[str, Any]]:
    context = _entry_edge_cost_context(settings, feature)
    if not settings.require_edge_over_cost:
        return True, context
    return context["estimated_edge_bps"] >= context["required_edge_bps"], context


def _plan_allows_urgent_reduce_only_exit(plan: CryptoOfimPlan, symbol: str) -> bool:
    reason = str(plan.reason or "")
    if reason.startswith("loss_guard") or reason in URGENT_REDUCE_ONLY_PLAN_REASONS:
        return True
    benchmark_trend = plan.benchmark_trend if isinstance(plan.benchmark_trend, dict) else {}
    symbol_guard = benchmark_trend.get("symbol_loss_guard")
    if isinstance(symbol_guard, dict):
        blocked_symbols = {str(item).upper() for item in symbol_guard.get("blocked_symbols") or []}
        if symbol.upper() in blocked_symbols:
            return True
    stale_exit_positions = {str(item).upper() for item in benchmark_trend.get("stale_exit_positions") or []}
    return symbol.upper() in stale_exit_positions


def _strategy_settings_status(settings: CryptoOfimSettings) -> dict[str, Any]:
    return {
        "entry_threshold": settings.entry_threshold,
        "exit_threshold": settings.exit_threshold,
        "exit_confirm_cycles": settings.exit_confirm_cycles,
        "signal_confirm_cycles": settings.signal_confirm_cycles,
        "require_edge_over_cost": settings.require_edge_over_cost,
        "edge_bps_per_score": settings.edge_bps_per_score,
        "cost_buffer_bps": settings.cost_buffer_bps,
        "min_edge_cost_ratio": settings.min_edge_cost_ratio,
        "active_capital_pct": settings.active_capital_pct,
        "max_position_weight": settings.max_position_weight,
        "max_gross_exposure": settings.max_gross_exposure,
        "max_positions": settings.max_positions,
        "min_order_notional": settings.min_order_notional,
        "max_order_notional": settings.max_order_notional,
        "max_spread_bps": settings.max_spread_bps,
        "max_order_book_impact_bps": settings.max_order_book_impact_bps,
        "max_order_book_take_ratio": settings.max_order_book_take_ratio,
        "rebalance_threshold": settings.rebalance_threshold,
        "min_trade_interval_seconds": settings.min_trade_interval_seconds,
        "min_flip_interval_seconds": settings.min_flip_interval_seconds,
        "min_reentry_after_risk_off_seconds": settings.min_reentry_after_risk_off_seconds,
        "min_holding_seconds": settings.min_holding_seconds,
        "max_holding_seconds": settings.max_holding_seconds,
        "loss_guard_max_loss": settings.loss_guard_max_loss,
        "loss_guard_max_estimated_fees": settings.loss_guard_max_estimated_fees,
        "loss_guard_max_trades": settings.loss_guard_max_trades,
        "loss_guard_recent_window_seconds": settings.loss_guard_recent_window_seconds,
        "loss_guard_max_recent_trades": settings.loss_guard_max_recent_trades,
        "loss_guard_max_recent_risk_off_exits": settings.loss_guard_max_recent_risk_off_exits,
        "loss_guard_max_recent_flips": settings.loss_guard_max_recent_flips,
        "loss_guard_symbol_max_loss": settings.loss_guard_symbol_max_loss,
        "loss_guard_symbol_max_estimated_fees": settings.loss_guard_symbol_max_estimated_fees,
        "loss_guard_symbol_max_trades": settings.loss_guard_symbol_max_trades,
        "fee_rate": settings.fee_rate,
        "slippage_bps": settings.slippage_bps,
        "use_ws_cache": settings.use_ws_cache,
        "use_user_stream": settings.use_user_stream,
        "liquidate_all_testnet_assets": settings.liquidate_all_testnet_assets,
    }


class CryptoOfimEngine:
    def __init__(
        self,
        settings: CryptoOfimSettings,
        client: BinanceSpotClient | None = None,
        market_client: BinanceSpotClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or BinanceSpotClient(
            base_url=settings.base_url,
            api_key=settings.api_key,
            api_secret=settings.api_secret,
            recv_window_ms=settings.recv_window_ms,
        )
        self.market_client = market_client or (
            client
            if client is not None
            else BinanceSpotClient(
                base_url=settings.market_data_base_url,
                recv_window_ms=settings.recv_window_ms,
            )
        )
        self._last_testnet_balances: dict[str, float] = {}
        self._active_symbols_cache: list[str] | None = None
        self._commission_cache: dict[str, dict[str, Any]] = {}

    def _commission_reports_for_symbols(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        reports: dict[str, dict[str, Any]] = {}
        can_query_account_commission = (
            getattr(self.client, "base_url", "").rstrip("/") == MAINNET_BASE_URL
            and bool(getattr(self.client, "api_key", None))
            and bool(getattr(self.client, "api_secret", None))
        )
        for raw_symbol in symbols:
            symbol = str(raw_symbol or "").upper()
            if not symbol:
                continue
            if symbol in self._commission_cache:
                reports[symbol] = self._commission_cache[symbol]
                continue
            report = _official_public_spot_commission(symbol)
            if can_query_account_commission and hasattr(self.client, "account_commission"):
                try:
                    report = dict(self.client.account_commission(symbol))
                    report["source"] = "binance_account_commission_endpoint"
                except Exception as exc:
                    _append_event(
                        "commission_rate_fallback",
                        {
                            "symbol": symbol,
                            "source": BINANCE_OFFICIAL_SPOT_PUBLIC_FEE_SOURCE,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
            self._commission_cache[symbol] = report
            reports[symbol] = report
        return reports

    def check(self) -> dict[str, Any]:
        self._active_symbols_cache = None
        self.client.ping()
        server_time = self.client.server_time()
        self.market_client.ping()
        active_symbols = self.active_symbols()
        api_budget = estimate_crypto_ofim_request_weight(self.settings, len(active_symbols))
        result = {
            "status": "ok",
            "mode": self.settings.mode,
            "execution_base_url": self.settings.base_url,
            "market_data": self.settings.market_data,
            "market_data_base_url": self.settings.market_data_base_url,
            "server_time": server_time,
            "symbol_mode": (
                "hot_usdt"
                if self.settings.hot_universe
                else "core_usdt" if self.settings.core_universe else ("tight_usdt" if tuple(self.settings.symbols) == DEFAULT_TIGHT_USDT_SYMBOLS else "custom")
            ),
            "hot_count": self.settings.hot_count if self.settings.hot_universe else None,
            "symbols": active_symbols,
            "submit_label": self.settings.submit_label,
            "market_data_label": self.settings.market_data_label,
            "api_budget": api_budget,
        }
        if self.settings.mode == "testnet":
            account = self.client.account()
            result["can_read_testnet_account"] = True
            result["account_type"] = account.get("accountType")
        _write_status(result)
        return result

    def active_symbols(self) -> list[str]:
        if self._active_symbols_cache is not None:
            return list(self._active_symbols_cache)
        symbols = list(dict.fromkeys(self.settings.symbols))
        if not self.settings.hot_universe:
            self._active_symbols_cache = symbols
            return list(symbols)

        excluded = set(self.settings.excluded_symbols)
        quote = self.settings.quote_asset
        rows = self.market_client.tickers_24h()
        candidates: list[tuple[str, float]] = []
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol.endswith(quote):
                continue
            base = _base_asset(symbol, quote)
            if base in DEFAULT_EXCLUDED_BASES or symbol in excluded or base in excluded:
                continue
            if base.endswith(("UP", "DOWN", "BULL", "BEAR")):
                continue
            quote_volume = _safe_float(row.get("quoteVolume"))
            last_price = _safe_float(row.get("lastPrice"))
            if quote_volume <= 0 or last_price <= 0:
                continue
            candidates.append((symbol, quote_volume))

        top = [symbol for symbol, _ in sorted(candidates, key=lambda item: item[1], reverse=True)]
        count = max(1, min(100, self.settings.hot_count))
        merged = [self.settings.benchmark, *top[:count], *symbols]
        self._active_symbols_cache = list(dict.fromkeys(merged))[: count + 1]
        return list(self._active_symbols_cache)

    def _load_state_for_mode(self) -> CryptoPaperState:
        state_file_existed = _state_file_for(self.settings).exists()
        state = CryptoPaperState.load(self.settings)
        current_epoch_id = _current_ledger_epoch_id()
        if current_epoch_id and state.ledger_epoch_id and state.ledger_epoch_id != current_epoch_id:
            _append_event(
                "state_epoch_resync",
                {
                    "mode": self.settings.mode,
                    "previous_epoch_id": state.ledger_epoch_id,
                    "current_epoch_id": current_epoch_id,
                    "reason": "ledger_epoch_changed",
                },
            )
            state.last_order_books = {}
            state.empty_target_streak = 0
            state.last_target_weights = {}
        if current_epoch_id:
            state.ledger_epoch_id = current_epoch_id
        if self.settings.mode != "testnet":
            return state
        learning_guard_reason, learning_guard_context = _learning_loss_guard_breach(self.settings)
        if learning_guard_reason and current_epoch_id and state_file_existed:
            ledger = _estimate_order_log_ledger("testnet", self.settings.quote_asset)
            ledger_positions = {
                symbol: _safe_float(qty)
                for symbol, qty in (ledger.get("positions") or {}).items()
                if _safe_float(qty) > 1e-10
            }
            local_positions = {
                symbol: _safe_float(qty)
                for symbol, qty in state.positions.items()
                if _safe_float(qty) > 1e-10
            }
            if not ledger_positions and not local_positions:
                self._last_testnet_balances = {self.settings.quote_asset: state.cash}
                _append_event(
                    "testnet_account_sync_skipped",
                    {
                        "mode": self.settings.mode,
                        "reason": learning_guard_reason,
                        "source": learning_guard_context.get("source", "learning_loss_guard"),
                        "epoch_id": current_epoch_id,
                        "local_position_count": 0,
                        "ledger_position_count": 0,
                        "action": "guarded_flat_no_signed_account_poll",
                    },
                )
                state.save(self.settings)
                return state
        account = self.client.account()
        balances = {
            str(row.get("asset")): _safe_float(row.get("free")) + _safe_float(row.get("locked"))
            for row in account.get("balances", [])
        }
        self._last_testnet_balances = balances
        state.cash = balances.get(self.settings.quote_asset, 0.0)
        ledger = _estimate_order_log_ledger("testnet", self.settings.quote_asset)
        ledger_positions = ledger.get("positions") or {}
        positions: dict[str, float] = {}
        for symbol in set(self.active_symbols()) | set(ledger_positions):
            base = _base_asset(symbol, self.settings.quote_asset)
            account_qty = balances.get(base, 0.0)
            ledger_qty = _safe_float(ledger_positions.get(symbol))
            # Binance Testnet accounts often contain free faucet assets. Only
            # count balances that our own submitted order log can explain.
            qty = min(account_qty, ledger_qty) if ledger_qty > 0 else 0.0
            if qty > 0:
                positions[symbol] = qty
        state.positions = positions
        state.avg_cost = {
            symbol: _safe_float(cost)
            for symbol, cost in (ledger.get("avg_cost") or {}).items()
            if positions.get(symbol, 0.0) > 0
        }
        state.save(self.settings)
        return state

    def _load_ws_market_cache(self) -> dict[str, Any]:
        if not self.settings.use_ws_cache:
            return {}
        try:
            from .crypto_ofim_stream import load_crypto_ofim_ws_cache

            cache = load_crypto_ofim_ws_cache(max_age_seconds=max(5, min(30, self.settings.min_trade_interval_seconds or 5)))
            if cache and str(cache.get("market_data") or "") != self.settings.market_data:
                return {}
            return cache
        except Exception:
            return {}

    def _market_data_from_ws_cache(
        self,
        symbol: str,
        cache: dict[str, Any],
    ) -> tuple[pd.Series, dict[str, list[list[float]]], pd.DataFrame, str] | None:
        books = cache.get("books") if isinstance(cache.get("books"), dict) else {}
        book_payload = books.get(symbol) if isinstance(books, dict) else None
        if not isinstance(book_payload, dict):
            return None

        bids = list(book_payload.get("Bid") or [])
        asks = list(book_payload.get("Ask") or [])
        if not bids or not asks:
            return None

        best_bid = _safe_float(book_payload.get("best_bid")) or _safe_float(bids[0][0])
        best_ask = _safe_float(book_payload.get("best_ask")) or _safe_float(asks[0][0])
        last = _safe_float(book_payload.get("mid"))
        if last <= 0 and best_bid > 0 and best_ask > 0:
            last = (best_bid + best_ask) / 2
        snapshot = pd.Series({"last_price": last, "bid_price": best_bid, "ask_price": best_ask})
        book = {"Bid": bids[: self.settings.depth_limit], "Ask": asks[: self.settings.depth_limit]}

        trades_by_symbol = cache.get("trades") if isinstance(cache.get("trades"), dict) else {}
        trade_rows = list((trades_by_symbol or {}).get(symbol) or [])[-self.settings.trade_limit :]
        if trade_rows:
            ticks = pd.DataFrame(trade_rows)
            ticks = ticks[[col for col in ["price", "volume", "ticker_direction"] if col in ticks.columns]]
            for col in ["price", "volume"]:
                if col in ticks.columns:
                    ticks[col] = pd.to_numeric(ticks[col], errors="coerce")
            if "ticker_direction" not in ticks.columns:
                ticks["ticker_direction"] = "NEUTRAL"
            source = "ws_cache"
        else:
            # Newly started streams may have a valid book before the first trade
            # event arrives. Use REST trades once rather than weakening the first
            # cycle with an empty tick tape.
            ticks = self.market_client.recent_trades(symbol, limit=self.settings.trade_limit)
            source = "ws_cache+rest_ticks"
        return snapshot, book, ticks, source

    def _apply_signal_confirmation(
        self,
        candidates: dict[str, float],
        state: CryptoPaperState,
        *,
        cycle_id: str | None = None,
    ) -> dict[str, float]:
        required = max(1, int(getattr(self.settings, "signal_confirm_cycles", 1) or 1))
        if required <= 1:
            state.signal_confirm_streak = {
                symbol: count
                for symbol, count in state.signal_confirm_streak.items()
                if symbol in candidates
            }
            return candidates

        confirmed: dict[str, float] = {}
        next_streak: dict[str, int] = {}
        for symbol, score in candidates.items():
            count = max(0, int(state.signal_confirm_streak.get(symbol, 0))) + 1
            next_streak[symbol] = count
            has_position = _safe_float(state.positions.get(symbol)) > 0
            if has_position or count >= required:
                confirmed[symbol] = score
                continue
            if cycle_id:
                _append_event(
                    "signal_skipped",
                    {
                        "mode": self.settings.mode,
                        "symbol": symbol,
                        "reason": "signal_confirmation_pending",
                        "score": round(float(score), 6),
                        "signal_confirm_count": count,
                        "signal_confirm_cycles": required,
                    },
                    cycle_id=cycle_id,
                )
        state.signal_confirm_streak = next_streak
        return confirmed

    def generate_plan(self, state: CryptoPaperState | None = None, *, cycle_id: str | None = None) -> CryptoOfimPlan:
        state = state or CryptoPaperState.load(self.settings)
        guard_account = self.account_snapshot(state)
        guard_reason, guard_context = _loss_guard_breach(self.settings, guard_account)
        learning_guard_reason, learning_guard_context = _learning_loss_guard_breach(self.settings)
        if guard_reason and learning_guard_reason:
            guard_context = {**guard_context, "learning_loss_guard": learning_guard_context}
        elif learning_guard_reason:
            guard_reason, guard_context = learning_guard_reason, learning_guard_context
        if guard_reason:
            state.empty_target_streak = 0
            state.last_target_weights = {}
            state.signal_confirm_streak = {}
            state.save(self.settings)
            if cycle_id:
                _append_event(
                    "loss_guard_triggered",
                    {
                        "mode": self.settings.mode,
                        **guard_context,
                    },
                    cycle_id=cycle_id,
                )
                _append_event(
                    "plan_generated",
                    {
                        "mode": self.settings.mode,
                        "benchmark": self.settings.benchmark,
                        "benchmark_score": 0.0,
                        "exposure": 0.0,
                        "target_weights": {},
                        "market_sources": {},
                        "reason": guard_reason,
                        "benchmark_trend": guard_context,
                    },
                    cycle_id=cycle_id,
                )
            return CryptoOfimPlan(
                mode=self.settings.mode,
                benchmark=self.settings.benchmark,
                benchmark_score=0.0,
                exposure=0.0,
                target_weights={},
                features=[],
                market_sources={},
                reason=guard_reason,
                benchmark_trend=guard_context,
            )
        active_symbols = self.active_symbols()
        snapshots: dict[str, pd.Series] = {}
        bars_by_symbol: dict[str, pd.DataFrame] = {}
        books: dict[str, dict[str, list[list[float]]]] = {}
        ticks_by_symbol: dict[str, pd.DataFrame] = {}
        market_sources: dict[str, str] = {}
        ws_cache = self._load_ws_market_cache()

        for symbol in active_symbols:
            bars_by_symbol[symbol] = self.market_client.klines(symbol, interval="1m", limit=self.settings.lookback_bars)
            cached = self._market_data_from_ws_cache(symbol, ws_cache)
            if cached:
                snapshots[symbol], books[symbol], ticks_by_symbol[symbol], market_sources[symbol] = cached
            else:
                snapshots[symbol] = self.market_client.book_ticker(symbol)
                books[symbol] = self.market_client.depth(symbol, limit=self.settings.depth_limit)
                ticks_by_symbol[symbol] = self.market_client.recent_trades(symbol, limit=self.settings.trade_limit)
                market_sources[symbol] = "rest"
            if cycle_id:
                _append_event(
                    "market_snapshot",
                    {
                        "mode": self.settings.mode,
                        "market_data": self.settings.market_data,
                        "symbol": symbol,
                        "market_source": market_sources[symbol],
                        "book": _book_summary(books[symbol]),
                        "bars": _bars_summary(bars_by_symbol[symbol]),
                        "ticks": _ticks_summary(ticks_by_symbol[symbol]),
                    },
                    cycle_id=cycle_id,
                )

        benchmark_score = 0.0
        benchmark = self.settings.benchmark
        benchmark_trend: dict[str, Any] = {
            "ok": True,
            "reason": "benchmark_missing",
            "last_price": 0.0,
            "sma": 0.0,
            "window": 0,
        }
        if benchmark in snapshots:
            benchmark_score = _compute_benchmark_score(bars_by_symbol[benchmark], snapshots[benchmark], books[benchmark])
            benchmark_trend = _benchmark_sma_trend(
                bars_by_symbol.get(benchmark, pd.DataFrame()),
                snapshots.get(benchmark, pd.Series(dtype=object)),
                window=min(60, max(1, self.settings.lookback_bars)),
            )

        features: list[CryptoOfimFeature] = []
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
            _append_jsonl(FEATURES_FILE, {"ts": _utc_now(), **asdict(feature)})
            if cycle_id:
                _append_event("feature_scored", asdict(feature), cycle_id=cycle_id)

        state.last_order_books = books

        exposure_scale, plan_reason, risk_context = _benchmark_risk_budget(
            self.settings,
            benchmark_score,
            benchmark_trend,
        )
        benchmark_trend = {**benchmark_trend, **risk_context}
        risk_off_cooldown_seconds = max(0, int(getattr(self.settings, "min_reentry_after_risk_off_seconds", 0)))
        if exposure_scale > 0 and risk_off_cooldown_seconds > 0:
            recent_risk_off_age = _recent_benchmark_risk_off_age_seconds(self.settings.mode)
            cooldown_reason = "benchmark_risk_off_cooldown"
            cooldown_source = "benchmark_risk_off"
            recent_exit_age = _recent_risk_off_exit_age_seconds(self.settings.mode)
            if recent_exit_age is not None and (recent_risk_off_age is None or recent_exit_age < recent_risk_off_age):
                recent_risk_off_age = recent_exit_age
                cooldown_reason = "risk_off_exit_cooldown"
                cooldown_source = "risk_off_exit"
            if recent_risk_off_age is not None and recent_risk_off_age < risk_off_cooldown_seconds:
                plan_reason = cooldown_reason
                exposure_scale = 0.0
                benchmark_trend = {
                    **benchmark_trend,
                    "risk_off_cooldown_active": True,
                    "risk_off_cooldown_source": cooldown_source,
                    "recent_risk_off_age_seconds": round(recent_risk_off_age, 3),
                    "risk_off_cooldown_seconds": risk_off_cooldown_seconds,
                }

        if exposure_scale <= 0:
            state.empty_target_streak = 0
            state.last_target_weights = {}
            state.signal_confirm_streak = {}
            state.save(self.settings)
            if cycle_id:
                for feature in features:
                    if feature.eligible and feature.score >= self.settings.entry_threshold:
                        _append_event(
                            "signal_skipped",
                            {
                                "mode": self.settings.mode,
                                "symbol": feature.symbol,
                                "reason": plan_reason,
                                "score": feature.score,
                                "benchmark_trend": benchmark_trend,
                            },
                            cycle_id=cycle_id,
                        )
                _append_event(
                    "plan_generated",
                    {
                        "mode": self.settings.mode,
                        "benchmark": benchmark,
                        "benchmark_score": round(benchmark_score, 6),
                        "exposure": 0.0,
                        "target_weights": {},
                        "market_sources": market_sources,
                        "reason": plan_reason,
                        "benchmark_trend": benchmark_trend,
                    },
                    cycle_id=cycle_id,
                )
            return CryptoOfimPlan(
                mode=self.settings.mode,
                benchmark=benchmark,
                benchmark_score=round(benchmark_score, 6),
                exposure=0.0,
                target_weights={},
                features=sorted(features, key=lambda item: item.score, reverse=True),
                market_sources=market_sources,
                reason=plan_reason,
                benchmark_trend=benchmark_trend,
            )

        symbol_loss_guards = _symbol_loss_guard_breaches(self.settings, active_symbols)
        if symbol_loss_guards:
            benchmark_trend = {
                **benchmark_trend,
                "symbol_loss_guard": {
                    "blocked_symbols": sorted(symbol_loss_guards),
                    "symbols": symbol_loss_guards,
                },
            }

        exposure = self.settings.max_gross_exposure * exposure_scale
        candidates: dict[str, float] = {}
        symbol_guard_blocked_candidates = 0
        for feature in features:
            if not feature.eligible or feature.score < self.settings.entry_threshold:
                continue
            symbol_guard = symbol_loss_guards.get(feature.symbol)
            if symbol_guard:
                symbol_guard_blocked_candidates += 1
                if cycle_id:
                    _append_event(
                        "signal_skipped",
                        {
                            "mode": self.settings.mode,
                            "symbol": feature.symbol,
                            "reason": symbol_guard["reason"],
                            "score": feature.score,
                            "symbol_loss_guard": symbol_guard,
                        },
                        cycle_id=cycle_id,
                    )
                continue
            passes_cost_gate, cost_context = _passes_entry_edge_cost_gate(self.settings, feature)
            if not passes_cost_gate:
                if cycle_id:
                    _append_event(
                        "signal_skipped",
                        {
                            "mode": self.settings.mode,
                            "symbol": feature.symbol,
                            "reason": "edge_below_cost",
                            "score": feature.score,
                            **cost_context,
                        },
                        cycle_id=cycle_id,
                    )
                continue
            candidates[feature.symbol] = max(feature.score, self.settings.exit_threshold)
        candidates = self._apply_signal_confirmation(candidates, state, cycle_id=cycle_id)
        if cycle_id and plan_reason == "benchmark_soft_risk":
            for feature in features:
                if feature.symbol in candidates:
                    _append_event(
                        "signal_scaled",
                        {
                            "mode": self.settings.mode,
                            "symbol": feature.symbol,
                            "reason": "benchmark_soft_risk",
                            "score": feature.score,
                            "exposure_scale": round(exposure_scale, 6),
                            "benchmark_trend": benchmark_trend,
                        },
                        cycle_id=cycle_id,
                    )
        ordered = dict(sorted(candidates.items(), key=lambda item: item[1], reverse=True)[: self.settings.max_positions])
        target_weights = _weight_with_cap(ordered, exposure if ordered else 0.0, self.settings.max_position_weight)
        stale_positions: set[str] = set()
        stale_exit_positions: set[str] = {symbol for symbol in symbol_loss_guards if state.positions.get(symbol, 0.0) > 0}
        if self.settings.max_holding_seconds > 0 and state.positions:
            for symbol, qty in state.positions.items():
                if qty <= 0:
                    continue
                age_seconds = _open_position_age_seconds(symbol, self.settings.mode, self.settings.quote_asset)
                if age_seconds is not None and age_seconds >= self.settings.max_holding_seconds:
                    stale_positions.add(symbol)
            for symbol in stale_positions:
                if symbol in target_weights:
                    continue
                snapshot = snapshots.get(symbol)
                price = _safe_float(snapshot.get("last_price") if snapshot is not None else 0.0)
                if price <= 0:
                    try:
                        price = _safe_float(self.market_client.book_ticker(symbol).get("last_price"))
                    except Exception:
                        price = 0.0
                if state.positions.get(symbol, 0.0) * price >= self.settings.min_order_notional:
                    stale_exit_positions.add(symbol)
        if stale_exit_positions:
            benchmark_trend = {**benchmark_trend, "stale_exit_positions": sorted(stale_exit_positions)}
        if target_weights:
            state.empty_target_streak = 0
            state.last_target_weights = dict(target_weights)
        elif state.positions and self.settings.exit_confirm_cycles > 1:
            state.empty_target_streak += 1
            if state.empty_target_streak < self.settings.exit_confirm_cycles:
                values: dict[str, float] = {}
                for symbol, qty in state.positions.items():
                    snapshot = snapshots.get(symbol)
                    price = _safe_float(snapshot.get("last_price") if snapshot is not None else 0.0)
                    if price <= 0:
                        try:
                            price = _safe_float(self.market_client.book_ticker(symbol).get("last_price"))
                        except Exception:
                            price = 0.0
                    if qty > 0 and price > 0:
                        values[symbol] = qty * price
                equity = state.cash + sum(values.values())
                if equity > 0:
                    weight_base = _strategy_sizing_equity(self.settings, equity)
                    if weight_base <= 0:
                        weight_base = equity
                    current_weights = {
                        symbol: min(value / weight_base, self.settings.max_position_weight)
                        for symbol, value in values.items()
                        if value >= self.settings.min_order_notional and symbol not in stale_exit_positions
                    }
                    total_weight = sum(current_weights.values())
                    if total_weight > self.settings.max_gross_exposure > 0:
                        scale = self.settings.max_gross_exposure / total_weight
                        current_weights = {symbol: weight * scale for symbol, weight in current_weights.items()}
                    target_weights = {symbol: round(weight, 6) for symbol, weight in current_weights.items() if weight > 0}
                    exposure = sum(target_weights.values())
            if target_weights:
                state.last_target_weights = dict(target_weights)
            else:
                state.last_target_weights = {}
        else:
            state.empty_target_streak = 0
            state.last_target_weights = {}
        if not target_weights and symbol_guard_blocked_candidates > 0 and plan_reason == "ok":
            plan_reason = "symbol_loss_guard"
        state.save(self.settings)
        if cycle_id:
            _append_event(
                "plan_generated",
                {
                    "mode": self.settings.mode,
                    "benchmark": benchmark,
                    "benchmark_score": round(benchmark_score, 6),
                    "exposure": round(sum(target_weights.values()), 6),
                    "target_weights": target_weights,
                    "market_sources": market_sources,
                    "empty_target_streak": state.empty_target_streak,
                    "stale_positions": sorted(stale_positions),
                    "stale_exit_positions": sorted(stale_exit_positions),
                    "reason": plan_reason,
                    "benchmark_trend": benchmark_trend,
                },
                cycle_id=cycle_id,
            )
        return CryptoOfimPlan(
            mode=self.settings.mode,
            benchmark=benchmark,
            benchmark_score=round(benchmark_score, 6),
            exposure=round(sum(target_weights.values()), 6),
            target_weights=target_weights,
            features=sorted(features, key=lambda item: item.score, reverse=True),
            market_sources=market_sources,
            reason=plan_reason,
            benchmark_trend=benchmark_trend,
        )

    def _score_symbol(
        self,
        symbol: str,
        order_book: dict | None,
        prev_order_book: dict | None,
        bars_1m: pd.DataFrame,
        ticks: pd.DataFrame,
        snapshot: pd.Series,
    ) -> CryptoOfimFeature:
        if bars_1m.empty:
            return CryptoOfimFeature(symbol, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.5, 9999.0, 0.0, 0.0, False, "no_bars")
        # Tier 3 upper bound scales with depth_limit so deeper subscriptions
        # are fully utilized rather than being capped at level 60.
        deep_end = max(60, self.settings.depth_limit)
        ofi = compute_multi_level_ofi(order_book, prev_order_book, ((1, 5), (6, 20), (21, deep_end)))
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
        soft_reasons: list[str] = []
        min_vol = max(0.0, self.settings.min_vol_acceleration)
        if min_vol > 0 and vol_accel < min_vol:
            volume_gap = min(1.0, (min_vol - vol_accel) / max(min_vol, 1e-9))
            long_score -= min(0.08, volume_gap * 0.08)
            soft_reasons.append("low_volume_soft_penalty")
        hard_reasons: list[str] = []
        if spread > self.settings.max_spread_bps:
            hard_reasons.append("spread_too_wide")
        if long_score < self.settings.entry_threshold:
            hard_reasons.append("score_below_entry")
        eligible = not hard_reasons
        reasons = hard_reasons + soft_reasons
        conviction = min(1.0, max(0.0, long_score / max(self.settings.max_score, 1e-9)))
        return CryptoOfimFeature(
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
            score=round(long_score, 6),
            conviction=round(conviction, 6),
            eligible=eligible,
            reason="ok" if not reasons else ",".join(reasons),
        )

    def account_snapshot(self, state: CryptoPaperState | None = None) -> dict[str, Any]:
        state = state or self._load_state_for_mode()
        prices: dict[str, float] = {}
        active_symbol_list = self.active_symbols()
        active_symbol_set = set(active_symbol_list)
        for symbol in list(dict.fromkeys([*active_symbol_list, *state.positions])):
            try:
                prices[symbol] = float(self.market_client.book_ticker(symbol).get("last_price", 0.0) or 0.0)
            except Exception:
                prices[symbol] = 0.0
        holdings_value = {
            symbol: qty * prices.get(symbol, 0.0)
            for symbol, qty in state.positions.items()
            if qty > 0
        }
        market_value = sum(holdings_value.values())
        equity = state.cash + market_value
        starting_equity = _ledger_starting_equity(self.settings)
        net_pnl = equity - starting_equity if starting_equity > 0 else 0.0
        net_return_pct = net_pnl / starting_equity if starting_equity > 0 else 0.0
        sizing_equity = _strategy_sizing_equity(self.settings, equity)
        strategy_available_cash = min(state.cash, max(0.0, sizing_equity - market_value))
        unrealized = sum(
            (prices.get(symbol, 0.0) - state.avg_cost.get(symbol, prices.get(symbol, 0.0))) * qty
            for symbol, qty in state.positions.items()
            if qty > 0 and prices.get(symbol, 0.0) > 0
        )
        realized_pnl = state.realized_pnl
        fees_paid = state.fees_paid
        estimated_fees_paid = state.fees_paid
        realized_pnl_after_estimated_fees = state.realized_pnl
        estimated_fee_source = "actual_fills"
        estimated_fee_rates: dict[str, dict[str, Any]] = {}
        trade_count = 0
        ledger_positions: dict[str, float] = {}
        ledger_warnings: list[str] = []
        ledger_audit_hash = ""
        ledger_avg_cost: dict[str, float] = {}
        cash_reconciliation: dict[str, Any] = {}
        position_reconciliation: list[dict[str, Any]] = []
        if self.settings.mode == "testnet":
            commission_reports = self._commission_reports_for_symbols([*active_symbol_list, *state.positions])
            ledger = _estimate_order_log_ledger(
                "testnet",
                self.settings.quote_asset,
                self.settings.fee_rate,
                commission_reports=commission_reports,
            )
            realized_pnl = _safe_float(ledger.get("realized_pnl"))
            fees_paid = _safe_float(ledger.get("fees_paid"))
            estimated_fees_paid = _safe_float(ledger.get("estimated_fees_paid"))
            realized_pnl_after_estimated_fees = _safe_float(ledger.get("realized_pnl_after_estimated_fees"), realized_pnl)
            estimated_fee_source = str(ledger.get("estimated_fee_source") or BINANCE_OFFICIAL_SPOT_PUBLIC_FEE_SOURCE)
            estimated_fee_rates = dict(ledger.get("estimated_fee_rates") or {})
            trade_count = int(ledger.get("trade_count") or 0)
            ledger_positions = ledger.get("positions") or {}
            ledger_avg_cost = ledger.get("avg_cost") or {}
            ledger_warnings = list(ledger.get("warnings") or [])
            ledger_audit_hash = str(ledger.get("audit_hash") or "")
            epoch_balances = load_crypto_ofim_ledger_epoch().get("balances")
            epoch_quote_cash = _safe_float(epoch_balances.get(self.settings.quote_asset)) if isinstance(epoch_balances, dict) else 0.0
            if epoch_quote_cash > 0:
                expected_quote_cash = epoch_quote_cash + _safe_float(ledger.get("cash_delta"))
                actual_quote_cash = state.cash
                unexplained_quote_delta = actual_quote_cash - expected_quote_cash
                cash_tolerance = max(1.0, self.settings.min_order_notional * 0.5, abs(expected_quote_cash) * 1e-6)
                cash_reconciliation = {
                    "quote_asset": self.settings.quote_asset,
                    "epoch_quote_cash": round(epoch_quote_cash, 8),
                    "ledger_cash_delta": round(_safe_float(ledger.get("cash_delta")), 8),
                    "expected_quote_cash": round(expected_quote_cash, 8),
                    "actual_quote_cash": round(actual_quote_cash, 8),
                    "unexplained_quote_delta": round(unexplained_quote_delta, 8),
                    "tolerance": round(cash_tolerance, 8),
                    "ok": abs(unexplained_quote_delta) <= cash_tolerance,
                }
                if not cash_reconciliation["ok"]:
                    ledger_warnings.append(
                        f"{self.settings.quote_asset} cash reconciliation drift "
                        f"{unexplained_quote_delta:.8f}; expected {expected_quote_cash:.8f}, "
                        f"actual {actual_quote_cash:.8f}"
                    )
            for symbol in sorted(set(ledger_positions) | set(state.positions)):
                ledger_qty = _safe_float(ledger_positions.get(symbol))
                counted_qty = _safe_float(state.positions.get(symbol))
                missing_qty = max(0.0, ledger_qty - counted_qty)
                extra_qty = max(0.0, counted_qty - ledger_qty)
                tolerance = max(1e-8, ledger_qty * 1e-6)
                if missing_qty <= tolerance and extra_qty <= tolerance:
                    continue
                row = {
                    "symbol": symbol,
                    "ledger_qty": round(ledger_qty, 12),
                    "strategy_counted_qty": round(counted_qty, 12),
                    "missing_qty": round(missing_qty, 12),
                    "extra_qty": round(extra_qty, 12),
                }
                position_reconciliation.append(row)
                if missing_qty > tolerance:
                    ledger_warnings.append(
                        f"{symbol} ledger/account position shortfall {missing_qty:.12f}; "
                        f"ledger {ledger_qty:.12f}, counted {counted_qty:.12f}"
                    )
            unrealized = sum(
                (prices.get(symbol, 0.0) - _safe_float(ledger_avg_cost.get(symbol), prices.get(symbol, 0.0))) * qty
                for symbol, qty in state.positions.items()
                if qty > 0 and prices.get(symbol, 0.0) > 0
            )
        equity_after_estimated_fees = equity + fees_paid - estimated_fees_paid
        net_pnl_after_estimated_fees = net_pnl + fees_paid - estimated_fees_paid
        primary_equity = equity_after_estimated_fees
        primary_net_pnl = net_pnl_after_estimated_fees
        primary_net_return_pct = primary_net_pnl / starting_equity if starting_equity > 0 else 0.0
        primary_pnl_source = "binance_official_fee_adjusted" if self.settings.mode == "testnet" else "paper_ledger"

        position_details: list[dict[str, Any]] = []
        basis_by_symbol = ledger_avg_cost if self.settings.mode == "testnet" else state.avg_cost
        for symbol, qty in sorted(state.positions.items()):
            if qty <= 0:
                continue
            price = prices.get(symbol, 0.0)
            value = qty * price if price > 0 else 0.0
            avg_cost = _safe_float(basis_by_symbol.get(symbol), price)
            age_seconds = _open_position_age_seconds(symbol, self.settings.mode, self.settings.quote_asset)
            stale = bool(
                self.settings.max_holding_seconds > 0
                and age_seconds is not None
                and age_seconds >= self.settings.max_holding_seconds
            )
            position_details.append(
                {
                    "symbol": symbol,
                    "quantity": round(qty, 12),
                    "last_price": round(price, 8),
                    "market_value": round(value, 8),
                    "avg_cost": round(avg_cost, 8),
                    "unrealized_pnl": round((price - avg_cost) * qty, 8) if price > 0 and avg_cost > 0 else 0.0,
                    "age_seconds": round(age_seconds, 2) if age_seconds is not None else None,
                    "stale": stale,
                    "active_universe": symbol in active_symbol_set,
                }
            )

        tracked_bases = {_base_asset(symbol, self.settings.quote_asset) for symbol in state.positions}
        testnet_balances = self._last_testnet_balances if self.settings.mode == "testnet" else {}
        extra_balances = {
            asset: qty
            for asset, qty in testnet_balances.items()
            if qty > 0 and asset != self.settings.quote_asset and asset not in tracked_bases
        }
        return {
            "cash": round(state.cash, 8),
            "market_value": round(market_value, 8),
            "equity": round(equity, 8),
            "equity_after_estimated_fees": round(equity_after_estimated_fees, 8),
            "primary_equity": round(primary_equity, 8),
            "starting_equity": round(starting_equity, 8),
            "net_pnl": round(net_pnl, 8),
            "net_pnl_after_estimated_fees": round(net_pnl_after_estimated_fees, 8),
            "primary_net_pnl": round(primary_net_pnl, 8),
            "net_return_pct": round(net_return_pct, 8),
            "net_return_after_estimated_fees_pct": round(net_pnl_after_estimated_fees / starting_equity, 8) if starting_equity > 0 else 0.0,
            "primary_net_return_pct": round(primary_net_return_pct, 8),
            "primary_pnl_source": primary_pnl_source,
            "active_capital": round(sizing_equity, 8),
            "strategy_available_cash": round(strategy_available_cash, 8),
            "realized_pnl": round(realized_pnl, 8),
            "realized_pnl_after_estimated_fees": round(realized_pnl_after_estimated_fees, 8),
            "unrealized_pnl": round(unrealized, 8),
            "fees_paid": round(fees_paid, 8),
            "estimated_fees_paid": round(estimated_fees_paid, 8),
            "estimated_fee_source": estimated_fee_source,
            "estimated_fee_rates": estimated_fee_rates,
            "trade_count": trade_count,
            "positions": dict(sorted(state.positions.items())),
            "prices": prices,
            "holdings_value": holdings_value,
            "position_details": position_details,
            "stale_position_count": sum(1 for row in position_details if row["stale"]),
            "ledger_positions": ledger_positions,
            "ledger_warnings": ledger_warnings,
            "ledger_audit_hash": ledger_audit_hash,
            "cash_reconciliation": cash_reconciliation,
            "position_reconciliation": position_reconciliation,
            "ledger_epoch": load_crypto_ofim_ledger_epoch(),
            "extra_balance_count": len(extra_balances),
            "extra_balance_sample": dict(list(sorted(extra_balances.items()))[:20]),
        }

    def balance_audit(self) -> dict[str, Any]:
        if self.settings.mode != "testnet":
            state = CryptoPaperState.load(self.settings)
            balances = {self.settings.quote_asset: state.cash}
            for symbol, qty in state.positions.items():
                balances[_base_asset(symbol, self.settings.quote_asset)] = qty
            return build_crypto_ofim_balance_audit(self.settings, balances, active_symbols=self.active_symbols())
        account = self.client.account()
        balances = {
            str(row.get("asset")): _safe_float(row.get("free")) + _safe_float(row.get("locked"))
            for row in account.get("balances", [])
        }
        self._last_testnet_balances = balances
        return build_crypto_ofim_balance_audit(self.settings, balances, active_symbols=self.active_symbols())

    def _account_balance_maps(self, account: dict[str, Any] | None = None) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
        account = account or self.client.account()
        free: dict[str, float] = {}
        locked: dict[str, float] = {}
        totals: dict[str, float] = {}
        for row in account.get("balances", []):
            asset = str(row.get("asset") or "").upper()
            if not asset:
                continue
            free_qty = _safe_float(row.get("free"))
            locked_qty = _safe_float(row.get("locked"))
            total_qty = free_qty + locked_qty
            if abs(total_qty) <= 1e-12:
                continue
            free[asset] = free_qty
            locked[asset] = locked_qty
            totals[asset] = total_qty
        return totals, free, locked

    def liquidate_testnet_to_quote(self, *, submit: bool = False, reset_epoch: bool = False) -> dict[str, Any]:
        """Sell free non-quote testnet assets with a quote pair.

        This is intentionally outside the strategy planner. It is a maintenance
        operation used to start a fresh testnet experiment from quote cash. By
        default it only touches the configured strategy universe, because
        Binance Testnet faucet accounts often contain unrelated balances.
        """
        if self.settings.mode != "testnet":
            raise CryptoOfimError("liquidate_testnet_to_quote only works in Binance Spot Testnet mode.")
        account = self.client.account()
        totals, free, locked = self._account_balance_maps(account)
        quote = self.settings.quote_asset
        planned: list[dict[str, Any]] = []
        submitted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        ts = _utc_now()
        tradable_symbols = self.client.exchange_symbols()
        book_tickers = self.client.book_tickers()
        liquidation_universe = set(tradable_symbols) if self.settings.liquidate_all_testnet_assets else set(self.active_symbols())

        for asset in sorted(totals):
            if asset == quote:
                continue
            free_qty = _safe_float(free.get(asset))
            locked_qty = _safe_float(locked.get(asset))
            if free_qty <= 1e-12:
                if locked_qty > 0:
                    skipped.append({"asset": asset, "reason": "locked_balance", "locked": locked_qty})
                continue
            symbol = f"{asset}{quote}"
            if symbol not in liquidation_universe:
                skipped.append(
                    {
                        "asset": asset,
                        "symbol": symbol,
                        "quantity": free_qty,
                        "reason": "outside_liquidation_universe",
                    }
                )
                continue
            if symbol not in tradable_symbols:
                skipped.append({"asset": asset, "symbol": symbol, "quantity": free_qty, "reason": "no_quote_market"})
                continue
            try:
                ticker = book_tickers.get(symbol) or {}
                price = _safe_float(ticker.get("last_price"))
                if price <= 0:
                    price = _safe_float(self.client.book_ticker(symbol).get("last_price"))
                if price <= 0:
                    skipped.append({"asset": asset, "symbol": symbol, "quantity": free_qty, "reason": "no_price"})
                    continue
                qty_dec, qty_text, reject_reason = self.client.normalize_market_quantity(symbol, free_qty, price)
            except Exception as exc:
                skipped.append({"asset": asset, "symbol": symbol, "quantity": free_qty, "reason": f"no_quote_market_or_rules:{exc}"})
                continue
            qty = float(qty_dec)
            notional = qty * price
            if reject_reason:
                skipped.append({"asset": asset, "symbol": symbol, "quantity": free_qty, "price": price, "reason": reject_reason})
                continue
            plan_row = {
                "asset": asset,
                "symbol": symbol,
                "side": "SELL",
                "quantity": qty,
                "price": price,
                "notional": notional,
                "locked": locked_qty,
                "_quantity_text": qty_text,
            }
            planned.append(plan_row)
        if submit:
            # Prioritize the balances that matter economically. Testnet faucet
            # accounts can contain hundreds of dust assets; selling large value
            # balances first makes partial reset attempts safer under rate limits.
            for plan_row in sorted(planned, key=lambda row: _safe_float(row.get("notional")), reverse=True):
                symbol = str(plan_row["symbol"])
                price = _safe_float(plan_row.get("price"))
                qty = _safe_float(plan_row.get("quantity"))
                notional = _safe_float(plan_row.get("notional"))
                qty_text = str(plan_row.get("_quantity_text") or qty)
                try:
                    response = self.client.market_order(
                        symbol,
                        "SELL",
                        quantity=qty_text,
                        validate_only=self.settings.testnet_validate_only,
                    )
                except CryptoOfimError as exc:
                    failed = {**plan_row, "status": "rejected_testnet", "reason": f"liquidate_to_quote; binance_error={exc}"}
                    submitted.append(failed)
                    _append_jsonl(
                        ORDERS_FILE,
                        {
                            "ts": ts,
                            "mode": self.settings.mode,
                            "symbol": symbol,
                            "side": "SELL",
                            "quantity": qty,
                            "price": price,
                            "notional": notional,
                            "fee": 0.0,
                            "status": "rejected_testnet",
                            "reason": failed["reason"],
                            "target_weight": 0.0,
                            "current_value": notional,
                            "target_value": 0.0,
                        },
                    )
                    time.sleep(0.12)
                    continue
                order_status = "validated_testnet" if self.settings.testnet_validate_only else "submitted_testnet"
                actual_qty, actual_notional, actual_fee = _actual_testnet_fill(
                    {"symbol": symbol, "side": "SELL", "quantity": qty, "price": price, "fee": 0.0, "response": response or {}},
                    quote,
                )
                actual_price = actual_notional / actual_qty if actual_qty > 0 else price
                order = CryptoOfimOrder(
                    ts=ts,
                    mode=self.settings.mode,
                    symbol=symbol,
                    side="SELL",
                    quantity=actual_qty,
                    price=round(actual_price, 8),
                    notional=round(actual_notional, 8),
                    fee=round(actual_fee, 8),
                    status=order_status,
                    reason="liquidate_to_quote",
                    target_weight=0.0,
                    current_value=round(notional, 8),
                    target_value=0.0,
                    response=response or {},
                )
                submitted.append({**plan_row, "status": order_status, "order_id": (response or {}).get("orderId")})
                _append_jsonl(ORDERS_FILE, asdict(order))
                # Avoid bursting hundreds of Testnet market orders into Binance at
                # once when resetting a faucet account with many assets.
                time.sleep(0.12)

        epoch: dict[str, Any] = {}
        balances_after: dict[str, float] = {}
        if submit and reset_epoch and not self.settings.testnet_validate_only:
            try:
                time.sleep(0.5)
                account_after = self.client.account()
                balances_after, _, _ = self._account_balance_maps(account_after)
            except Exception:
                balances_after = {}
            epoch = set_crypto_ofim_ledger_epoch(
                self.settings,
                reason="liquidate_to_quote",
                balances=balances_after,
            )

        result = {
            "status": "submitted" if submit else "planned",
            "mode": self.settings.mode,
            "quote_asset": quote,
            "planned_count": len(planned),
            "submitted_count": len([row for row in submitted if str(row.get("status", "")).startswith(("submitted", "validated"))]),
            "skipped_count": len(skipped),
            "liquidation_scope": "all_testnet_assets" if self.settings.liquidate_all_testnet_assets else "strategy_universe",
            "liquidation_universe": sorted(liquidation_universe),
            "planned": [{k: v for k, v in row.items() if not str(k).startswith("_")} for row in planned],
            "submitted": [{k: v for k, v in row.items() if not str(k).startswith("_")} for row in submitted],
            "skipped": skipped[:100],
            "epoch": epoch,
            "balances_after": balances_after,
        }
        _append_event("liquidate_to_quote", result)
        _write_status({**result, "status": "liquidated" if submit else "liquidation_planned"})
        return result

    def plan_orders(
        self,
        plan: CryptoOfimPlan,
        state: CryptoPaperState | None = None,
        *,
        cycle_id: str | None = None,
    ) -> list[CryptoOfimOrder]:
        state = state or CryptoPaperState.load(self.settings)
        snapshot = self.account_snapshot(state)
        equity = float(snapshot["equity"])
        sizing_equity = _strategy_sizing_equity(self.settings, equity)
        if sizing_equity <= 0:
            if cycle_id:
                _append_event(
                    "order_skipped",
                    {
                        "mode": self.settings.mode,
                        "reason": "sizing_equity_zero",
                        "equity": equity,
                        "active_capital": self.settings.active_capital,
                    },
                    cycle_id=cycle_id,
                )
            return []
        prices = snapshot["prices"]
        current_values = snapshot["holdings_value"]
        orders: list[CryptoOfimOrder] = []
        raw_orders: list[tuple[str, float, float, float, float, bool]] = []
        symbols = set(plan.target_weights) | set(state.positions)
        ts = _utc_now()
        market_value = sum(float(value or 0.0) for value in current_values.values())
        cash_available = min(state.cash, max(0.0, sizing_equity - market_value))
        projected_guard_trade_count = int(_safe_float(snapshot.get("trade_count")))
        projected_guard_estimated_fees = _safe_float(
            snapshot.get("estimated_fees_paid"),
            _safe_float(snapshot.get("fees_paid")),
        )
        max_guard_estimated_fees = max(0.0, float(getattr(self.settings, "loss_guard_max_estimated_fees", 0.0)))
        max_guard_trades = max(0, int(getattr(self.settings, "loss_guard_max_trades", 0)))
        recent_window_seconds = max(0, int(getattr(self.settings, "loss_guard_recent_window_seconds", 0)))
        recent_guard_stats = _recent_order_churn_stats(self.settings.mode, recent_window_seconds)
        projected_guard_recent_trades = int(_safe_float(recent_guard_stats.get("trade_count")))
        projected_guard_recent_flips = int(_safe_float(recent_guard_stats.get("flip_count")))
        max_guard_recent_trades = max(0, int(getattr(self.settings, "loss_guard_max_recent_trades", 0)))
        max_guard_recent_flips = max(0, int(getattr(self.settings, "loss_guard_max_recent_flips", 0)))
        projected_recent_sides: dict[str, str] = {}

        def _skip(symbol: str, reason: str, **extra: Any) -> None:
            if not cycle_id:
                return
            _append_event(
                "order_skipped",
                {
                    "mode": self.settings.mode,
                    "symbol": symbol,
                    "reason": reason,
                    **extra,
                },
                cycle_id=cycle_id,
            )

        def _projected_flip_increment(symbol: str, side: str, recent_side: str, recent_age: float | None) -> int:
            previous_side = projected_recent_sides.get(symbol)
            if previous_side is None and recent_window_seconds > 0 and recent_age is not None and recent_age <= recent_window_seconds:
                previous_side = recent_side
            if previous_side in {"BUY", "SELL"} and side in {"BUY", "SELL"} and side != previous_side:
                return 1
            return 0

        def _record_projected_recent_order(symbol: str, side: str, recent_side: str, recent_age: float | None) -> None:
            nonlocal projected_guard_recent_trades, projected_guard_recent_flips
            if recent_window_seconds <= 0:
                return
            projected_guard_recent_trades += 1
            projected_guard_recent_flips += _projected_flip_increment(symbol, side, recent_side, recent_age)
            projected_recent_sides[symbol] = side

        def _projected_entry_loss_guard(
            estimated_fees: float,
            *,
            symbol: str,
            side: str,
            recent_side: str,
            recent_age: float | None,
        ) -> dict[str, Any]:
            projected_trade_count = projected_guard_trade_count + 1
            projected_estimated_fees = projected_guard_estimated_fees + max(0.0, float(estimated_fees or 0.0))
            projected_recent_trades = projected_guard_recent_trades + (1 if recent_window_seconds > 0 else 0)
            projected_recent_flips = projected_guard_recent_flips + _projected_flip_increment(
                symbol,
                side,
                recent_side,
                recent_age,
            )
            breaches: list[str] = []
            if max_guard_estimated_fees > 0 and projected_estimated_fees >= max_guard_estimated_fees:
                breaches.append("estimated_fees")
            if max_guard_trades > 0 and projected_trade_count >= max_guard_trades:
                breaches.append("trade_count")
            if max_guard_recent_trades > 0 and projected_recent_trades >= max_guard_recent_trades:
                breaches.append("recent_trades")
            if max_guard_recent_flips > 0 and projected_recent_flips >= max_guard_recent_flips:
                breaches.append("recent_flips")
            if not breaches:
                return {}
            return {
                "breaches": breaches,
                "current_estimated_fees": round(projected_guard_estimated_fees, 8),
                "projected_estimated_fees": round(projected_estimated_fees, 8),
                "max_estimated_fees": max_guard_estimated_fees,
                "current_trade_count": projected_guard_trade_count,
                "projected_trade_count": projected_trade_count,
                "max_trades": max_guard_trades,
                "recent_window_seconds": recent_window_seconds,
                "current_recent_trade_count": projected_guard_recent_trades,
                "projected_recent_trade_count": projected_recent_trades,
                "max_recent_trades": max_guard_recent_trades,
                "current_recent_flip_count": projected_guard_recent_flips,
                "projected_recent_flip_count": projected_recent_flips,
                "max_recent_flips": max_guard_recent_flips,
            }

        for symbol in sorted(symbols):
            price = prices.get(symbol) or 0.0
            if price <= 0:
                _skip(symbol, "price_unavailable")
                continue
            target_weight = float(plan.target_weights.get(symbol, 0.0))
            target_value = sizing_equity * target_weight
            current_value = float(current_values.get(symbol, 0.0))
            diff = target_value - current_value
            min_rebalance = max(self.settings.min_order_notional, sizing_equity * self.settings.rebalance_threshold)
            is_target_zero_exit = diff < 0 and current_value > 0 and target_value <= 0
            urgent_reduce_only_exit = is_target_zero_exit and _plan_allows_urgent_reduce_only_exit(plan, symbol)
            bypass_rebalance_for_exit = urgent_reduce_only_exit and not plan.target_weights
            if abs(diff) < min_rebalance and not bypass_rebalance_for_exit:
                _skip(
                    symbol,
                    "below_rebalance_threshold",
                    diff=round(diff, 8),
                    min_rebalance=round(min_rebalance, 8),
                    target_weight=round(target_weight, 6),
                    current_value=round(current_value, 8),
                    target_value=round(target_value, 8),
                )
                continue
            raw_orders.append((symbol, price, target_weight, target_value, current_value, is_target_zero_exit))

        # Plan sells before buys so one crypto sleeve can rotate without being
        # blocked by temporarily low cash.
        raw_orders.sort(key=lambda item: item[3] - item[4])
        for symbol, price, target_weight, target_value, current_value, is_target_zero_exit in raw_orders:
            diff = target_value - current_value
            side = "BUY" if diff > 0 else "SELL"
            urgent_reduce_only_exit = is_target_zero_exit and _plan_allows_urgent_reduce_only_exit(plan, symbol)
            bypass_rebalance_for_exit = urgent_reduce_only_exit and not plan.target_weights
            recent_trade = _recent_symbol_trade(symbol, self.settings.mode)
            recent_age = recent_trade["age_seconds"] if recent_trade is not None else None
            recent_side = str((recent_trade or {}).get("side") or "").upper()
            recent_reason = str((recent_trade or {}).get("reason") or "")
            symbol_guard_context = (plan.benchmark_trend or {}).get("symbol_loss_guard") or {}
            blocked_symbols = (
                symbol_guard_context.get("blocked_symbols")
                if isinstance(symbol_guard_context, dict)
                else []
            )
            blocked_by_symbol_guard = symbol in set(blocked_symbols or [])
            force_reduce_only_exit = urgent_reduce_only_exit or (is_target_zero_exit and blocked_by_symbol_guard)
            if (
                self.settings.min_trade_interval_seconds > 0
                and recent_age is not None
                and not force_reduce_only_exit
            ):
                if recent_age < self.settings.min_trade_interval_seconds:
                    _skip(
                        symbol,
                        "cooldown_active",
                        recent_age_seconds=round(recent_age, 3),
                        cooldown_seconds=self.settings.min_trade_interval_seconds,
                    )
                    continue
            if (
                not force_reduce_only_exit
                and self.settings.min_flip_interval_seconds > 0
                and recent_age is not None
                and recent_side in {"BUY", "SELL"}
                and side != recent_side
                and recent_age < self.settings.min_flip_interval_seconds
            ):
                _skip(
                    symbol,
                    "flip_cooldown_active",
                    recent_side=recent_side,
                    desired_side=side,
                    recent_age_seconds=round(recent_age, 3),
                    cooldown_seconds=self.settings.min_flip_interval_seconds,
                )
                continue
            if (
                not force_reduce_only_exit
                and side == "BUY"
                and self.settings.min_reentry_after_risk_off_seconds > 0
                and recent_age is not None
                and recent_side == "SELL"
                and RISK_OFF_EXIT_REASON in recent_reason
                and recent_age < self.settings.min_reentry_after_risk_off_seconds
            ):
                _skip(
                    symbol,
                    "risk_off_reentry_cooldown_active",
                    recent_side=recent_side,
                    desired_side=side,
                    recent_age_seconds=round(recent_age, 3),
                    cooldown_seconds=self.settings.min_reentry_after_risk_off_seconds,
                )
                continue
            if (
                side == "SELL"
                and not force_reduce_only_exit
                and self.settings.min_holding_seconds > 0
                and float(state.positions.get(symbol, 0.0)) > 0
            ):
                open_age = _open_position_age_seconds(symbol, self.settings.mode, self.settings.quote_asset)
                if open_age is not None and open_age < self.settings.min_holding_seconds:
                    _skip(
                        symbol,
                        "min_holding_period_active",
                        open_age_seconds=round(open_age, 3),
                        min_holding_seconds=self.settings.min_holding_seconds,
                    )
                    continue
            desired_notional = abs(diff)
            notional_caps = [desired_notional]
            if self.settings.max_order_notional > 0:
                notional_caps.append(self.settings.max_order_notional)
            if self.settings.max_order_book_impact_bps > 0 and self.settings.max_order_book_take_ratio > 0:
                book_notional = _book_executable_notional(
                    state.last_order_books.get(symbol),
                    side,
                    price,
                    self.settings.max_order_book_impact_bps,
                )
                if book_notional > 0:
                    notional_caps.append(book_notional * min(1.0, max(0.01, self.settings.max_order_book_take_ratio)))
            capped_notional = max(0.0, min(notional_caps))
            if capped_notional < self.settings.min_order_notional:
                _skip(
                    symbol,
                    "capped_notional_below_min_order",
                    side=side,
                    desired_notional=round(desired_notional, 8),
                    capped_notional=round(capped_notional, 8),
                    min_order_notional=self.settings.min_order_notional,
                )
                continue
            reason = "rebalance_to_ofim_target"
            if bypass_rebalance_for_exit:
                reason = f"{reason}; {RISK_OFF_EXIT_REASON}"
            if capped_notional < desired_notional * 0.999:
                reason = (
                    f"{reason}; capped_notional={capped_notional:.2f}; "
                    f"desired_notional={desired_notional:.2f}"
                )
            execution_price = price * (1 + self.settings.slippage_bps / 10_000) if side == "BUY" else price * (1 - self.settings.slippage_bps / 10_000)
            if side == "BUY":
                notional = min(capped_notional, max(0.0, cash_available / (1 + self.settings.fee_rate)))
                if notional < self.settings.min_order_notional:
                    _skip(
                        symbol,
                        "insufficient_cash_after_caps",
                        side=side,
                        capped_notional=round(capped_notional, 8),
                        cash_available=round(cash_available, 8),
                        min_order_notional=self.settings.min_order_notional,
                    )
                    continue
                quantity = _round_qty(notional / execution_price)
                fee = quantity * execution_price * self.settings.fee_rate
                reserved_exit_fee = quantity * execution_price * self.settings.fee_rate
                projected_guard = _projected_entry_loss_guard(
                    fee + reserved_exit_fee,
                    symbol=symbol,
                    side=side,
                    recent_side=recent_side,
                    recent_age=recent_age,
                )
                if projected_guard:
                    _skip(
                        symbol,
                        "projected_loss_guard_budget",
                        side=side,
                        fee=round(fee, 8),
                        reserved_exit_fee=round(reserved_exit_fee, 8),
                        **projected_guard,
                    )
                    continue
                cash_available -= quantity * execution_price + fee
            else:
                held_qty = float(state.positions.get(symbol, 0.0))
                quantity = min(held_qty, _round_qty(capped_notional / execution_price))
                if quantity <= 0:
                    _skip(
                        symbol,
                        "sell_quantity_zero",
                        side=side,
                        held_qty=round(held_qty, 12),
                        capped_notional=round(capped_notional, 8),
                    )
                    continue
                notional = quantity * execution_price
                if notional < self.settings.min_order_notional:
                    _skip(
                        symbol,
                        "sell_notional_below_min_order",
                        side=side,
                        quantity=round(quantity, 12),
                        notional=round(notional, 8),
                        min_order_notional=self.settings.min_order_notional,
                    )
                    continue
                fee = notional * self.settings.fee_rate
                cash_available += notional - fee
            orders.append(
                CryptoOfimOrder(
                    ts=ts,
                    mode=self.settings.mode,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=round(execution_price, 8),
                    notional=round(quantity * execution_price, 8),
                    fee=round(fee, 8),
                    status="planned",
                    reason=reason,
                    target_weight=round(target_weight, 6),
                    current_value=round(current_value, 8),
                    target_value=round(target_value, 8),
                )
            )
            projected_guard_trade_count += 1
            projected_guard_estimated_fees += max(0.0, float(fee or 0.0))
            _record_projected_recent_order(symbol, side, recent_side, recent_age)
        return orders

    def submit_paper_orders(self, orders: list[CryptoOfimOrder], state: CryptoPaperState | None = None) -> list[CryptoOfimOrder]:
        state = state or CryptoPaperState.load(self.settings)
        submitted: list[CryptoOfimOrder] = []
        for order in orders:
            if order.side == "BUY":
                gross = order.quantity * order.price
                total = gross + order.fee
                if total > state.cash + 1e-8:
                    submitted.append(
                        CryptoOfimOrder(**{**asdict(order), "status": "rejected", "reason": "insufficient_cash"})
                    )
                    continue
                prev_qty = state.positions.get(order.symbol, 0.0)
                prev_cost = state.avg_cost.get(order.symbol, order.price)
                new_qty = prev_qty + order.quantity
                state.avg_cost[order.symbol] = ((prev_qty * prev_cost) + gross) / new_qty if new_qty > 0 else 0.0
                state.positions[order.symbol] = new_qty
                state.cash -= total
            else:
                held_qty = state.positions.get(order.symbol, 0.0)
                sell_qty = min(held_qty, order.quantity)
                gross = sell_qty * order.price
                fee = gross * self.settings.fee_rate
                avg_cost = state.avg_cost.get(order.symbol, order.price)
                state.realized_pnl += (order.price - avg_cost) * sell_qty - fee
                state.fees_paid += fee
                state.cash += gross - fee
                state.positions[order.symbol] = max(0.0, held_qty - sell_qty)
                if state.positions[order.symbol] <= 1e-10:
                    state.positions.pop(order.symbol, None)
                    state.avg_cost.pop(order.symbol, None)
                order = CryptoOfimOrder(**{**asdict(order), "quantity": sell_qty, "notional": gross, "fee": fee})
            if order.side == "BUY":
                state.fees_paid += order.fee
            executed = CryptoOfimOrder(**{**asdict(order), "status": "filled_paper"})
            submitted.append(executed)
            _append_jsonl(ORDERS_FILE, asdict(executed))
        state.save(self.settings)
        return submitted

    def submit_testnet_orders(self, orders: list[CryptoOfimOrder]) -> list[CryptoOfimOrder]:
        submitted: list[CryptoOfimOrder] = []
        for order in orders:
            quantity_dec, quantity_text, reject_reason = self.client.normalize_market_quantity(
                order.symbol,
                order.quantity,
                order.price,
            )
            adjusted_qty = float(quantity_dec)
            adjusted_notional = round(adjusted_qty * order.price, 8)
            adjusted_fee = round(adjusted_notional * self.settings.fee_rate, 8)
            adjusted_order = CryptoOfimOrder(
                **{
                    **asdict(order),
                    "quantity": adjusted_qty,
                    "notional": adjusted_notional,
                    "fee": adjusted_fee,
                }
            )
            if reject_reason:
                rejected = CryptoOfimOrder(
                    **{
                        **asdict(adjusted_order),
                        "status": "rejected_testnet_filter",
                        "reason": f"{order.reason}; {reject_reason}",
                    }
                )
                submitted.append(rejected)
                _append_jsonl(ORDERS_FILE, asdict(rejected))
                continue
            try:
                response = self.client.market_order(
                    order.symbol,
                    order.side,
                    quantity=quantity_text,
                    validate_only=self.settings.testnet_validate_only,
                )
            except CryptoOfimError as exc:
                rejected = CryptoOfimOrder(
                    **{
                        **asdict(adjusted_order),
                        "status": "rejected_testnet",
                        "reason": f"{order.reason}; binance_error={exc}",
                    }
                )
                submitted.append(rejected)
                _append_jsonl(ORDERS_FILE, asdict(rejected))
                continue
            status = "validated_testnet" if self.settings.testnet_validate_only else "submitted_testnet"
            actual_qty, actual_notional, actual_fee = _actual_testnet_fill(
                {**asdict(adjusted_order), "response": response or {}},
                self.settings.quote_asset,
            )
            actual_price = actual_notional / actual_qty if actual_qty > 0 else adjusted_order.price
            submitted_order = CryptoOfimOrder(
                **{
                    **asdict(adjusted_order),
                    "quantity": actual_qty,
                    "price": round(actual_price, 8),
                    "notional": round(actual_notional, 8),
                    "fee": round(actual_fee, 8),
                    "status": status,
                    "response": response or {},
                }
            )
            submitted.append(submitted_order)
            _append_jsonl(ORDERS_FILE, asdict(submitted_order))
        return submitted

    def run_once(self, *, submit: bool = False) -> dict[str, Any]:
        cycle_id = f"{int(time.time() * 1000)}-{os.getpid()}"
        try:
            _append_event(
                "cycle_started",
                {
                    "mode": self.settings.mode,
                    "submit": bool(submit),
                    "symbols": list(self.settings.symbols),
                    "hot_universe": self.settings.hot_universe,
                    "core_universe": self.settings.core_universe,
                    "depth_limit": self.settings.depth_limit,
                    "trade_limit": self.settings.trade_limit,
                    "active_capital": self.settings.active_capital,
                    "active_capital_pct": self.settings.active_capital_pct,
                    "max_position_weight": self.settings.max_position_weight,
                    "max_gross_exposure": self.settings.max_gross_exposure,
                    "market_data": self.settings.market_data,
                    "market_data_base_url": self.settings.market_data_base_url,
                    "execution_base_url": self.settings.base_url,
                    "poll_style": "ws_cache+rest_fallback" if self.settings.use_ws_cache else "rest",
                },
                cycle_id=cycle_id,
            )
            self._active_symbols_cache = None
            state = self._load_state_for_mode()
            plan = self.generate_plan(state, cycle_id=cycle_id)
            orders = self.plan_orders(plan, state, cycle_id=cycle_id)
            pre_submit_account = self.account_snapshot(state)
            for order in orders:
                _append_event("order_planned", asdict(order), cycle_id=cycle_id)
            if orders:
                _append_order_memory_safe(
                    orders,
                    cycle_id=cycle_id,
                    stage="planned",
                    settings=self.settings,
                    plan=plan,
                    account=pre_submit_account,
                )
            submitted: list[CryptoOfimOrder] = []
            if submit and orders:
                if self.settings.mode == "paper":
                    submitted = self.submit_paper_orders(orders, state)
                else:
                    submitted = self.submit_testnet_orders(orders)
                for order in submitted:
                    _append_event("order_submitted", asdict(order), cycle_id=cycle_id)
            account = self.account_snapshot(self._load_state_for_mode())
            if submitted:
                _append_order_memory_safe(
                    submitted,
                    cycle_id=cycle_id,
                    stage="submitted",
                    settings=self.settings,
                    plan=plan,
                    account=account,
                )
            payload = {
                "status": "submitted" if submitted else "planned",
                "mode": self.settings.mode,
                "submit_label": self.settings.submit_label,
                "market_data": self.settings.market_data,
                "market_data_label": self.settings.market_data_label,
                "market_data_base_url": self.settings.market_data_base_url,
                "execution_base_url": self.settings.base_url,
                "benchmark": plan.benchmark,
                "benchmark_score": plan.benchmark_score,
                "plan_reason": plan.reason,
                "benchmark_trend": plan.benchmark_trend or {},
                "exposure": plan.exposure,
                "target_weights": plan.target_weights,
                "market_sources": plan.market_sources or {},
                "features": [asdict(feature) for feature in plan.features],
                "planned_orders": [asdict(order) for order in orders],
                "submitted_orders": [asdict(order) for order in submitted],
                "account": account,
                "strategy_settings": _strategy_settings_status(self.settings),
                "api_budget": estimate_crypto_ofim_request_weight(self.settings, len(self.active_symbols())),
                "cycle_id": cycle_id,
            }
            _append_event(
                "cycle_completed",
                {
                    "status": payload["status"],
                    "mode": self.settings.mode,
                    "target_count": len(plan.target_weights),
                    "planned_order_count": len(orders),
                    "submitted_order_count": len(submitted),
                    "equity": account.get("equity"),
                    "cash": account.get("cash"),
                    "market_value": account.get("market_value"),
                    "stale_position_count": account.get("stale_position_count", 0),
                },
                cycle_id=cycle_id,
            )
            _write_status(payload)
            return payload
        except Exception as exc:
            raw_error = str(exc)
            if _is_transient_network_message(raw_error):
                _write_status(
                    {
                        "updated_at": datetime.now(UTC).isoformat(),
                        "status": "transient_error",
                        "mode": self.settings.mode,
                        "submit_label": self.settings.submit_label,
                        "market_data": self.settings.market_data,
                        "market_data_label": self.settings.market_data_label,
                        "market_data_base_url": self.settings.market_data_base_url,
                        "execution_base_url": self.settings.base_url,
                        "error": _friendly_transient_network_error(),
                        "raw_error": raw_error[:800],
                        "strategy_settings": _strategy_settings_status(self.settings),
                        "cycle_id": cycle_id,
                    }
                )
            else:
                _write_status({"status": "error", "mode": self.settings.mode, "error": raw_error})
            _append_event(
                "cycle_error",
                {
                    "mode": self.settings.mode,
                    "error": raw_error[:1000],
                    "transient": _is_transient_network_message(raw_error),
                },
                cycle_id=cycle_id,
            )
            raise
