from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime" / "crypto_ofim"
CRYPTO_PERP_RUNTIME_DIR = REPO_ROOT / "runtime" / "crypto_perp"
CRYPTO_ORDER_MEMORY_FILE = RUNTIME_DIR / "crypto_order_memory.jsonl"
CRYPTO_TRADE_OUTCOMES_FILE = RUNTIME_DIR / "crypto_trade_outcomes.jsonl"
CRYPTO_ATTRIBUTION_FILE = RUNTIME_DIR / "crypto_attribution.json"
CRYPTO_UPGRADE_CANDIDATES_FILE = RUNTIME_DIR / "crypto_upgrade_candidates.jsonl"
CRYPTO_PROMOTION_REPORT_FILE = RUNTIME_DIR / "crypto_promotion_report.json"
CRYPTO_LEARNING_REVIEW_PACKET_JSON_FILE = RUNTIME_DIR / "crypto_learning_review_packet.json"
CRYPTO_LEARNING_REVIEW_PACKET_FILE = RUNTIME_DIR / "crypto_learning_review_packet.md"
CRYPTO_ORDERS_FILE = RUNTIME_DIR / "orders.jsonl"
CRYPTO_USER_FILLS_FILE = RUNTIME_DIR / "user_fills.jsonl"
CRYPTO_LEDGER_EPOCH_FILE = RUNTIME_DIR / "ledger_epoch.json"
CRYPTO_PERP_ORDERS_FILE = CRYPTO_PERP_RUNTIME_DIR / "orders.jsonl"
CRYPTO_PERP_STATUS_FILE = CRYPTO_PERP_RUNTIME_DIR / "status.json"
CRYPTO_PERP_STATE_FILE = CRYPTO_PERP_RUNTIME_DIR / "paper_state.json"
DEFAULT_TESTNET_SPOT_TAKER_FEE_RATE = 0.001
DEFAULT_TESTNET_SPOT_FEE_SOURCE = "binance_official_public_spot_vip0_standard_estimate"


@dataclass(frozen=True)
class CryptoLearningPipelineResult:
    outcome_count: int
    candidate_count: int
    attribution_path: Path
    outcomes_path: Path
    candidates_path: Path
    promotion_path: Path
    review_packet_path: Path
    review_packet_json_path: Path


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, pd.Series):
        return value.to_dict()
    if isinstance(value, pd.DataFrame):
        return value.to_dict("records")
    return str(value)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default), encoding="utf-8")
    tmp.replace(path)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n")
    tmp.replace(path)


def _ensure_jsonl_artifact(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def _append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n")
            count += 1
    return count


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _tail_lines(path: Path, *, tail: int, block_size: int = 65536, max_scan_bytes: int = 16 * 1024 * 1024) -> list[str]:
    if tail <= 0 or not path.exists():
        return []
    try:
        file_size = path.stat().st_size
    except OSError:
        return []
    if file_size <= 0:
        return []

    chunks: list[bytes] = []
    lines_found = 0
    bytes_read = 0
    with path.open("rb") as handle:
        position = file_size
        while position > 0 and lines_found <= tail and bytes_read < max_scan_bytes:
            read_size = min(block_size, position, max_scan_bytes - bytes_read)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            bytes_read += len(chunk)
            lines_found += chunk.count(b"\n")

    data = b"".join(reversed(chunks))
    return data.decode("utf-8", errors="ignore").splitlines()[-tail:]


def _load_jsonl(path: Path, *, tail: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = _tail_lines(path, tail=tail) if tail is not None and tail > 0 else path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _hash_payload(payload: dict[str, Any], *, length: int = 16) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _file_sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _artifact_meta(path: Path) -> dict[str, Any]:
    exists = path.exists()
    return {
        "path": str(path),
        "exists": exists,
        "bytes": path.stat().st_size if exists and path.is_file() else 0,
        "sha256": _file_sha256(path) if exists and path.is_file() else "",
    }


def _asdict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _settings_snapshot(settings: Any | None) -> dict[str, Any]:
    if settings is None:
        return {}
    keys = (
        "mode",
        "base_url",
        "symbols",
        "benchmark",
        "quote_asset",
        "entry_threshold",
        "exit_threshold",
        "max_spread_bps",
        "max_position_weight",
        "max_gross_exposure",
        "max_positions",
        "min_order_notional",
        "max_order_notional",
        "max_order_book_impact_bps",
        "max_order_book_take_ratio",
        "rebalance_threshold",
        "exit_confirm_cycles",
        "min_trade_interval_seconds",
        "min_flip_interval_seconds",
        "min_reentry_after_risk_off_seconds",
        "min_holding_seconds",
        "max_holding_seconds",
        "fee_rate",
        "slippage_bps",
        "use_ws_cache",
        "use_user_stream",
    )
    return {key: getattr(settings, key, None) for key in keys if hasattr(settings, key)}


def _venue_label(settings: Any | None, mode: str = "") -> str:
    mode = mode or str(getattr(settings, "mode", "") or "")
    base_url = str(getattr(settings, "base_url", "") or "").lower()
    if "testnet" in base_url or mode == "testnet":
        return "binance_spot_testnet"
    return "binance_spot_global"


def _market_regime(plan: Any | None) -> str:
    score = _safe_float(getattr(plan, "benchmark_score", 0.0))
    if score >= 0.15:
        return "risk_on"
    if score <= -0.15:
        return "risk_off"
    return "neutral"


def _order_id(order: dict[str, Any]) -> str:
    response = order.get("response") if isinstance(order.get("response"), dict) else {}
    return str(response.get("orderId") or response.get("clientOrderId") or order.get("order_id") or order.get("client_order_id") or "")


def _valid_latency_ms(value: object) -> float | None:
    latency = _safe_float(value, -1.0)
    if 0.0 <= latency <= 600_000.0:
        return latency
    return None


def _response_latency_ms(response: dict[str, Any]) -> float | None:
    for key in ("_request_latency_ms", "request_latency_ms", "latency_ms", "exchange_latency_ms"):
        if key in response:
            latency = _valid_latency_ms(response.get(key))
            if latency is not None:
                return latency
    return None


def _memory_stage_from_status(status: str, default: str) -> str:
    text = str(status or "").lower()
    if "reject" in text:
        return "rejected"
    if "cancel" in text:
        return "cancelled"
    if text in {"filled_paper", "filled", "filled_all"}:
        return "filled"
    if text in {"submitted_testnet", "validated_testnet"}:
        return "submitted"
    return default


def append_order_memory(
    orders: Iterable[Any],
    *,
    cycle_id: str,
    stage: str,
    settings: Any | None = None,
    plan: Any | None = None,
    account: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
    order_memory_path: Path = CRYPTO_ORDER_MEMORY_FILE,
    now_utc: datetime | None = None,
) -> int:
    stamp = (now_utc or datetime.now(UTC)).isoformat()
    settings_ctx = _settings_snapshot(settings)
    mode = str(settings_ctx.get("mode") or getattr(settings, "mode", "") or "")
    venue = _venue_label(settings, mode)
    plan_sources = getattr(plan, "market_sources", None) or {}
    benchmark_trend = getattr(plan, "benchmark_trend", None) or {}
    account = account or {}
    rows: list[dict[str, Any]] = []
    for idx, order_obj in enumerate(orders):
        order = _asdict(order_obj)
        symbol = str(order.get("symbol") or "").upper()
        status = str(order.get("status") or "")
        actual_stage = _memory_stage_from_status(status, stage)
        notional = _safe_float(order.get("notional"), _safe_float(order.get("quantity")) * _safe_float(order.get("price")))
        fee = _safe_float(order.get("fee"))
        response = order.get("response") if isinstance(order.get("response"), dict) else {}
        exchange_event_time_ms = response.get("transactTime") or response.get("workingTime")
        payload = {
            "schema_version": 1,
            "record_type": "crypto_order_memory",
            "ts": stamp,
            "cycle_id": cycle_id,
            "stage": actual_stage,
            "sequence": idx,
            "exchange": "binance",
            "venue": venue,
            "instrument_type": "spot",
            "product_type": "spot",
            "mode": mode or str(order.get("mode") or ""),
            "symbol": symbol,
            "side": str(order.get("side") or "").upper(),
            "quantity": _safe_float(order.get("quantity")),
            "price": _safe_float(order.get("price")),
            "notional": notional,
            "fee": fee,
            "maker_taker": "taker",
            "status": status,
            "reason": str(order.get("reason") or ""),
            "target_weight": _safe_float(order.get("target_weight")),
            "current_value": _safe_float(order.get("current_value")),
            "target_value": _safe_float(order.get("target_value")),
            "order_id": _order_id(order),
            "response_status": str(response.get("status") or ""),
            "response_type": str(response.get("type") or ""),
            "exchange_latency_ms": _response_latency_ms(response),
            "exchange_event_time_ms": _safe_float(exchange_event_time_ms, 0.0) if exchange_event_time_ms else None,
            "partial_fill": bool(0 < _safe_float(response.get("executedQty")) < _safe_float(order.get("quantity"))),
            "rejected": "reject" in status.lower(),
            "stale_book": str(plan_sources.get(symbol, "")).startswith("stale"),
            "market_source": str(plan_sources.get(symbol, "")),
            "benchmark_score": _safe_float(getattr(plan, "benchmark_score", 0.0)),
            "benchmark_trend": benchmark_trend,
            "market_regime_24h": _market_regime(plan),
            "exchange_or_book_slippage_bps": _safe_float(getattr(settings, "slippage_bps", 0.0)),
            "inventory_exposure": _safe_float(account.get("market_value")),
            "equity": _safe_float(account.get("equity")),
            "cash": _safe_float(account.get("cash")),
            "max_drawdown": account.get("max_drawdown"),
            "leverage": 1.0,
            "margin_mode": "none",
            "liquidation_distance_pct": None,
            "margin_risk": None,
            "funding_rate": 0.0,
            "funding_paid": 0.0,
            "settings": settings_ctx,
            "diagnostics": diagnostics or {},
        }
        payload["decision_id"] = _hash_payload(
            {
                "cycle_id": cycle_id,
                "stage": actual_stage,
                "sequence": idx,
                "symbol": symbol,
                "side": payload["side"],
                "quantity": payload["quantity"],
                "price": payload["price"],
                "order_id": payload["order_id"],
            },
            length=20,
        )
        rows.append(payload)
    return _append_jsonl(order_memory_path, rows)


def load_order_memory(path: Path = CRYPTO_ORDER_MEMORY_FILE, *, tail: int | None = None) -> list[dict[str, Any]]:
    return _load_jsonl(path, tail=tail)


def _parse_ts(value: object) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _epoch_seconds(path: Path, *, mode: str, quote_asset: str) -> float | None:
    epoch = _load_json(path)
    if not epoch:
        return None
    if epoch.get("mode") and epoch.get("mode") != mode:
        return None
    if epoch.get("quote_asset") and epoch.get("quote_asset") != quote_asset:
        return None
    ts = _parse_ts(epoch.get("ts"))
    return ts.timestamp() if ts is not None else None


def _after_epoch(value: object, cutoff: float | None) -> bool:
    if cutoff is None:
        return True
    ts = _parse_ts(value)
    return ts is not None and ts.timestamp() >= cutoff


def _ts_seconds(value: object) -> float | None:
    ts = _parse_ts(value)
    return ts.timestamp() if ts is not None else None


def _base_asset(symbol: str, quote_asset: str) -> str:
    return symbol[: -len(quote_asset)] if symbol.endswith(quote_asset) else symbol


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
    return fallback_qty, fallback_qty * fallback_price, _safe_float(order.get("fee"))


def _fill_records_from_logs(
    *,
    mode: str,
    quote_asset: str,
    orders_path: Path,
    user_fills_path: Path,
    epoch_path: Path,
) -> list[dict[str, Any]]:
    cutoff = _epoch_seconds(epoch_path, mode=mode, quote_asset=quote_asset)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    user_order_keys: set[tuple[str, str, str]] = set()

    if mode == "testnet":
        for line_no, fill in enumerate(_load_jsonl(user_fills_path), start=1):
            if fill.get("mode") != mode or not _after_epoch(fill.get("ts"), cutoff):
                continue
            symbol = str(fill.get("symbol") or "").upper()
            side = str(fill.get("side") or "").upper()
            qty = _safe_float(fill.get("quantity"))
            price = _safe_float(fill.get("price"))
            notional = _safe_float(fill.get("notional"), qty * price)
            event_id = str(fill.get("event_id") or f"user_fill:{line_no}")
            if event_id in seen or not symbol or side not in {"BUY", "SELL"} or qty <= 0 or price <= 0 or notional <= 0:
                continue
            seen.add(event_id)
            order_id = str(fill.get("order_id") or "")
            if order_id:
                user_order_keys.add((symbol, side, order_id))
            records.append(
                {
                    "ts": str(fill.get("ts") or ""),
                    "mode": mode,
                    "symbol": symbol,
                    "side": side,
                    "quantity": qty,
                    "price": price,
                    "notional": notional,
                    "fee": _safe_float(fill.get("fee")),
                    "event_id": event_id,
                    "order_id": order_id,
                    "venue": "binance_spot_testnet",
                    "source": "binance_user_stream",
                }
            )

    executed_status = "submitted_testnet" if mode == "testnet" else "filled_paper"
    for line_no, order in enumerate(_load_jsonl(orders_path), start=1):
        if order.get("mode") != mode or order.get("status") != executed_status or not _after_epoch(order.get("ts"), cutoff):
            continue
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").upper()
        response = order.get("response") if isinstance(order.get("response"), dict) else {}
        order_id = str(response.get("orderId") or response.get("clientOrderId") or "")
        if order_id and (symbol, side, order_id) in user_order_keys:
            continue
        qty, notional, fee = _actual_testnet_fill(order, quote_asset) if mode == "testnet" else (
            _safe_float(order.get("quantity")),
            _safe_float(order.get("notional"), _safe_float(order.get("quantity")) * _safe_float(order.get("price"))),
            _safe_float(order.get("fee")),
        )
        price = notional / qty if qty > 0 else 0.0
        raw_event_id = str(order.get("event_id") or order.get("orderId") or order_id or line_no)
        event_id = f"order_log:{symbol}:{side}:{raw_event_id}:{line_no}"
        if event_id in seen or not symbol or side not in {"BUY", "SELL"} or qty <= 0 or price <= 0 or notional <= 0:
            continue
        seen.add(event_id)
        records.append(
            {
                "ts": str(order.get("ts") or ""),
                "mode": mode,
                "symbol": symbol,
                "side": side,
                "quantity": qty,
                "price": price,
                "notional": notional,
                "fee": fee,
                "event_id": event_id,
                "order_id": order_id,
                "venue": _venue_label(None, mode),
                "source": "order_log",
            }
        )
    return sorted(records, key=lambda row: (str(row.get("ts", "")), str(row.get("event_id", ""))))


def _memory_indexes(memory: list[dict[str, Any]]) -> tuple[dict[tuple[str, str, str], list[dict[str, Any]]], dict[tuple[str, str], list[dict[str, Any]]]]:
    by_order_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    by_symbol_side: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in memory:
        order_id = str(row.get("order_id") or "").strip()
        symbol = str(row.get("symbol") or "").upper()
        side = str(row.get("side") or "").upper()
        if order_id:
            by_order_key.setdefault((symbol, side, order_id), []).append(row)
        key = (symbol, side)
        by_symbol_side.setdefault(key, []).append(row)
    for rows in list(by_order_key.values()) + list(by_symbol_side.values()):
        rows.sort(key=lambda row: (_ts_seconds(row.get("ts")) is None, _ts_seconds(row.get("ts")) or 0.0, str(row.get("stage") or "")))
    return by_order_key, by_symbol_side


def _closest_memory_row(rows: list[dict[str, Any]], fill_ts: object) -> dict[str, Any]:
    if not rows:
        return {}
    fill_seconds = _ts_seconds(fill_ts)
    if fill_seconds is None:
        return rows[-1]
    return min(rows, key=lambda row: abs((_ts_seconds(row.get("ts")) or fill_seconds) - fill_seconds))


def _context_for_fill(fill: dict[str, Any], by_order_key: dict[tuple[str, str, str], list[dict[str, Any]]], by_symbol_side: dict[tuple[str, str], list[dict[str, Any]]]) -> dict[str, Any]:
    symbol = str(fill.get("symbol") or "").upper()
    side = str(fill.get("side") or "").upper()
    order_id = str(fill.get("order_id") or "").strip()
    if order_id:
        rows = by_order_key.get((symbol, side, order_id), [])
        if rows:
            return _closest_memory_row(rows, fill.get("ts"))
    rows = by_symbol_side.get((symbol, side), [])
    return _closest_memory_row(rows, fill.get("ts"))


def _fill_slippage_bps(fill: dict[str, Any], context: dict[str, Any]) -> float:
    expected = _safe_float(context.get("price"))
    actual = _safe_float(fill.get("price"))
    side = str(fill.get("side") or "").upper()
    if expected <= 0 or actual <= 0:
        return 0.0
    if side == "BUY":
        return (actual - expected) / expected * 10_000
    if side == "SELL":
        return (expected - actual) / expected * 10_000
    return 0.0


def _context_fee_rate(context: dict[str, Any]) -> tuple[float, str]:
    settings = context.get("settings") if isinstance(context.get("settings"), dict) else {}
    raw_rate = _safe_float(settings.get("fee_rate"), DEFAULT_TESTNET_SPOT_TAKER_FEE_RATE) if isinstance(settings, dict) else DEFAULT_TESTNET_SPOT_TAKER_FEE_RATE
    rate = max(DEFAULT_TESTNET_SPOT_TAKER_FEE_RATE, raw_rate)
    source = "order_context_fee_rate" if isinstance(settings, dict) and "fee_rate" in settings else DEFAULT_TESTNET_SPOT_FEE_SOURCE
    return rate, source


def _fee_quote(fill: dict[str, Any], context: dict[str, Any]) -> tuple[float, str, bool, float]:
    fee = _safe_float(fill.get("fee"))
    if fee > 0:
        return fee, "actual_commission_or_log_fee", False, 0.0

    mode = str(fill.get("mode") or context.get("mode") or "").lower()
    venue = str(fill.get("venue") or context.get("venue") or "").lower()
    if mode == "testnet" or "testnet" in venue:
        qty = _safe_float(fill.get("quantity"))
        price = _safe_float(fill.get("price"))
        notional = _safe_float(fill.get("notional"), qty * price)
        rate, source = _context_fee_rate(context)
        if notional > 0 and rate > 0:
            return notional * rate, source, True, rate

    return fee, "zero_fee_recorded", False, 0.0


def _hold_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 5 * 60:
        return "under_5m"
    if seconds < 60 * 60:
        return "under_1h"
    if seconds < 24 * 60 * 60:
        return "under_24h"
    return "multi_day"


def _classify_outcome(outcome: dict[str, Any]) -> tuple[str, list[str]]:
    tags: list[str] = []
    net = _safe_float(outcome.get("net_pnl"))
    gross = _safe_float(outcome.get("gross_pnl"))
    fees = _safe_float(outcome.get("fees"))
    slippage = _safe_float(outcome.get("slippage_bps"))
    hold_seconds = outcome.get("hold_seconds")
    funding_paid = _safe_float(outcome.get("funding_paid"))
    if net > 0:
        tags.append("profitable")
    if fees > 0 and fees >= max(abs(gross) * 0.5, 0.01) and net <= 0:
        tags.append("fees_dominated")
    if gross < 0:
        tags.append("signal_error")
    if hold_seconds is not None and _safe_float(hold_seconds) < 5 * 60 and net < 0:
        tags.append("fast_noise_loss")
    if abs(slippage) > 10 and net < 0:
        tags.append("slippage_or_spread")
    if funding_paid < 0 and net < 0:
        tags.append("funding_drag")
    if not tags:
        tags.append("loss_unclassified" if net < 0 else "flat")
    primary = next((tag for tag in tags if tag != "profitable"), tags[0])
    return primary, tags


def build_trade_outcomes(
    *,
    mode: str = "paper",
    quote_asset: str = "USDT",
    orders_path: Path = CRYPTO_ORDERS_FILE,
    user_fills_path: Path = CRYPTO_USER_FILLS_FILE,
    order_memory_path: Path = CRYPTO_ORDER_MEMORY_FILE,
    epoch_path: Path = CRYPTO_LEDGER_EPOCH_FILE,
    outcome_path: Path | None = CRYPTO_TRADE_OUTCOMES_FILE,
) -> list[dict[str, Any]]:
    memory = load_order_memory(order_memory_path)
    by_order_id, by_symbol_side = _memory_indexes(memory)
    fills = _fill_records_from_logs(
        mode=mode,
        quote_asset=quote_asset,
        orders_path=orders_path,
        user_fills_path=user_fills_path,
        epoch_path=epoch_path,
    )
    lots: dict[str, list[dict[str, Any]]] = {}
    outcomes: list[dict[str, Any]] = []
    for fill in fills:
        context = _context_for_fill(fill, by_order_id, by_symbol_side)
        symbol = str(fill.get("symbol") or "").upper()
        side = str(fill.get("side") or "").upper()
        qty = _safe_float(fill.get("quantity"))
        price = _safe_float(fill.get("price"))
        fee, fee_source, fee_estimated, fee_rate = _fee_quote(fill, context)
        slippage_bps = _fill_slippage_bps(fill, context)
        if side == "BUY":
            lots.setdefault(symbol, []).append(
                {
                    "remaining_qty": qty,
                    "entry_price": price,
                    "entry_fee_per_unit": fee / qty if qty > 0 else 0.0,
                    "entry_fee_source": fee_source,
                    "entry_fee_estimated": fee_estimated,
                    "entry_fee_rate": fee_rate,
                    "entry_ts": fill.get("ts", ""),
                    "entry_event_id": fill.get("event_id", ""),
                    "entry_order_id": fill.get("order_id", ""),
                    "entry_slippage_bps": slippage_bps,
                    "context": context,
                }
            )
            continue
        remaining = qty
        sell_fee_per_unit = fee / qty if qty > 0 else 0.0
        symbol_lots = lots.setdefault(symbol, [])
        while remaining > 1e-12 and symbol_lots:
            lot = symbol_lots[0]
            matched = min(remaining, _safe_float(lot.get("remaining_qty")))
            if matched <= 0:
                symbol_lots.pop(0)
                continue
            entry_price = _safe_float(lot.get("entry_price"))
            buy_fee = _safe_float(lot.get("entry_fee_per_unit")) * matched
            sell_fee = sell_fee_per_unit * matched
            gross = (price - entry_price) * matched
            net = gross - buy_fee - sell_fee
            fee_sources = sorted(
                source
                for source in {str(lot.get("entry_fee_source") or ""), fee_source}
                if source
            )
            fee_rates = [_safe_float(lot.get("entry_fee_rate")), fee_rate]
            entry_ts = _parse_ts(lot.get("entry_ts"))
            exit_ts = _parse_ts(fill.get("ts"))
            hold_seconds = (exit_ts - entry_ts).total_seconds() if entry_ts is not None and exit_ts is not None else None
            entry_context = dict(lot.get("context") or {})
            exit_context = context
            strategy = str(entry_context.get("reason") or "OFIM")
            avg_slippage = (_safe_float(lot.get("entry_slippage_bps")) + slippage_bps) / 2
            outcome = {
                "schema_version": 1,
                "record_type": "crypto_trade_outcome",
                "outcome_id": _hash_payload(
                    {
                        "entry": lot.get("entry_event_id"),
                        "exit": fill.get("event_id"),
                        "quantity": matched,
                    },
                    length=20,
                ),
                "mode": mode,
                "exchange": "binance",
                "venue": fill.get("venue") or entry_context.get("venue") or _venue_label(None, mode),
                "instrument_type": "spot",
                "symbol": symbol,
                "strategy": "OFIM",
                "reason": strategy,
                "entry_ts": str(lot.get("entry_ts") or ""),
                "exit_ts": str(fill.get("ts") or ""),
                "timeframe": _hold_bucket(hold_seconds),
                "hold_seconds": hold_seconds,
                "quantity": matched,
                "entry_price": entry_price,
                "exit_price": price,
                "gross_pnl": gross,
                "fees": buy_fee + sell_fee,
                "fees_estimated": bool(lot.get("entry_fee_estimated") or fee_estimated),
                "fee_sources": fee_sources,
                "estimated_fee_rate": max(fee_rates) if any(rate > 0 for rate in fee_rates) else 0.0,
                "net_pnl": net,
                "return_pct": net / (entry_price * matched + buy_fee) if entry_price > 0 and matched > 0 else 0.0,
                "slippage_bps": avg_slippage,
                "entry_slippage_bps": _safe_float(lot.get("entry_slippage_bps")),
                "exit_slippage_bps": slippage_bps,
                "maker_taker": "taker",
                "leverage": _safe_float(entry_context.get("leverage"), 1.0),
                "margin_mode": entry_context.get("margin_mode", "none"),
                "liquidation_distance_pct": entry_context.get("liquidation_distance_pct"),
                "margin_risk": entry_context.get("margin_risk"),
                "funding_rate": _safe_float(entry_context.get("funding_rate")),
                "funding_paid": _safe_float(entry_context.get("funding_paid")),
                "market_regime_24h": entry_context.get("market_regime_24h", "unknown"),
                "market_source": entry_context.get("market_source", ""),
                "exchange_latency_ms": entry_context.get("exchange_latency_ms"),
                "rejected_order": False,
                "partial_fill": bool(entry_context.get("partial_fill") or exit_context.get("partial_fill")),
                "stale_book": bool(entry_context.get("stale_book") or exit_context.get("stale_book")),
                "inventory_exposure": _safe_float(entry_context.get("inventory_exposure")),
                "max_drawdown": entry_context.get("max_drawdown"),
                "entry_event_id": lot.get("entry_event_id", ""),
                "exit_event_id": fill.get("event_id", ""),
                "entry_order_id": lot.get("entry_order_id", ""),
                "exit_order_id": fill.get("order_id", ""),
            }
            primary, tags = _classify_outcome(outcome)
            outcome["primary_reason"] = primary
            outcome["reason_tags"] = tags
            outcomes.append(outcome)
            remaining -= matched
            lot["remaining_qty"] = _safe_float(lot.get("remaining_qty")) - matched
            if _safe_float(lot.get("remaining_qty")) <= 1e-12:
                symbol_lots.pop(0)
        if remaining > 1e-12:
            outcome = {
                "schema_version": 1,
                "record_type": "crypto_trade_outcome",
                "outcome_id": _hash_payload({"unmatched": fill.get("event_id"), "quantity": remaining}, length=20),
                "mode": mode,
                "exchange": "binance",
                "venue": fill.get("venue") or _venue_label(None, mode),
                "instrument_type": "spot",
                "symbol": symbol,
                "strategy": "OFIM",
                "reason": "unmatched_sell",
                "entry_ts": "",
                "exit_ts": str(fill.get("ts") or ""),
                "timeframe": "unknown",
                "hold_seconds": None,
                "quantity": remaining,
                "entry_price": 0.0,
                "exit_price": price,
                "gross_pnl": 0.0,
                "fees": sell_fee_per_unit * remaining,
                "fees_estimated": fee_estimated,
                "fee_sources": [fee_source] if fee_source else [],
                "estimated_fee_rate": fee_rate,
                "net_pnl": -sell_fee_per_unit * remaining,
                "return_pct": 0.0,
                "slippage_bps": slippage_bps,
                "primary_reason": "unmatched_sell",
                "reason_tags": ["unmatched_sell", "attribution_ambiguous"],
            }
            outcomes.append(outcome)
    if outcome_path is not None:
        _write_jsonl_atomic(outcome_path, outcomes)
    return outcomes


def load_trade_outcomes(path: Path = CRYPTO_TRADE_OUTCOMES_FILE, *, tail: int | None = None) -> list[dict[str, Any]]:
    return _load_jsonl(path, tail=tail)


def _metric_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    net_values = [_safe_float(row.get("net_pnl")) for row in rows]
    gross_values = [_safe_float(row.get("gross_pnl")) for row in rows]
    fee_values = [_safe_float(row.get("fees")) for row in rows]
    estimated_fee_values = [_safe_float(row.get("fees")) for row in rows if row.get("fees_estimated")]
    slippage_values = [_safe_float(row.get("slippage_bps")) for row in rows]
    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value < 0]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in net_values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return {
        "trades": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / count if count else 0.0,
        "gross_pnl": sum(gross_values),
        "fees": sum(fee_values),
        "estimated_fees": sum(estimated_fee_values),
        "estimated_fee_count": len(estimated_fee_values),
        "net_pnl": sum(net_values),
        "avg_net_pnl": sum(net_values) / count if count else 0.0,
        "avg_return_pct": sum(_safe_float(row.get("return_pct")) for row in rows) / count if count else 0.0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses else None,
        "max_drawdown": max_drawdown,
        "avg_slippage_bps": sum(slippage_values) / count if count else 0.0,
        "funding_paid": sum(_safe_float(row.get("funding_paid")) for row in rows),
        "partial_fill_count": sum(1 for row in rows if row.get("partial_fill")),
        "stale_book_count": sum(1 for row in rows if row.get("stale_book")),
    }


def _group_summary(outcomes: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in outcomes:
        grouped.setdefault(str(row.get(key) or "unknown"), []).append(row)
    return {name: _metric_summary(rows) for name, rows in sorted(grouped.items())}


def _reason_tag_summary(outcomes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in outcomes:
        tags = row.get("reason_tags")
        if not isinstance(tags, list) or not tags:
            tags = [row.get("primary_reason") or "unknown"]
        for tag in tags:
            grouped.setdefault(str(tag or "unknown"), []).append(row)
    return {name: _metric_summary(rows) for name, rows in sorted(grouped.items())}


def _order_memory_fee_rate(row: dict[str, Any]) -> float:
    settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
    rate = _safe_float(settings.get("fee_rate"), 0.0) if isinstance(settings, dict) else 0.0
    mode = str(row.get("mode") or "").lower()
    venue = str(row.get("venue") or "").lower()
    if rate <= 0 and (mode == "testnet" or "testnet" in venue):
        rate = DEFAULT_TESTNET_SPOT_TAKER_FEE_RATE
    return max(0.0, rate)


def _order_memory_estimated_fee(row: dict[str, Any]) -> float:
    logged_fee = _safe_float(row.get("fee"))
    if logged_fee > 0:
        return logged_fee
    notional = abs(_safe_float(row.get("notional")))
    rate = _order_memory_fee_rate(row)
    return notional * rate if notional > 0 and rate > 0 else 0.0


def _order_memory_cost_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    count = 0
    notional = 0.0
    logged_fees = 0.0
    estimated_fees = 0.0
    taker_count = 0
    for row in rows:
        count += 1
        row_notional = abs(_safe_float(row.get("notional")))
        notional += row_notional
        logged_fees += _safe_float(row.get("fee"))
        estimated_fees += _order_memory_estimated_fee(row)
        if str(row.get("maker_taker") or "").lower() == "taker":
            taker_count += 1
    return {
        "records": count,
        "notional": round(notional, 8),
        "logged_fees": round(logged_fees, 8),
        "estimated_fees": round(estimated_fees, 8),
        "taker_records": taker_count,
    }


def _order_quality(memory: list[dict[str, Any]]) -> dict[str, Any]:
    stages: dict[str, int] = {}
    by_venue: dict[str, list[dict[str, Any]]] = {}
    by_submitted_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in memory:
        stage = str(row.get("stage") or "unknown")
        stages[stage] = stages.get(stage, 0) + 1
        by_venue.setdefault(str(row.get("venue") or "unknown"), []).append(row)
        if stage in {"submitted", "filled"} and not row.get("rejected"):
            symbol = str(row.get("symbol") or "unknown").upper()
            by_submitted_symbol.setdefault(symbol, []).append(row)
    valid_latencies = [
        latency
        for latency in (_valid_latency_ms(row.get("exchange_latency_ms")) for row in memory if row.get("exchange_latency_ms") is not None)
        if latency is not None
    ]
    invalid_latency_count = sum(1 for row in memory if row.get("exchange_latency_ms") is not None and _valid_latency_ms(row.get("exchange_latency_ms")) is None)
    planned_cost = _order_memory_cost_summary(row for row in memory if str(row.get("stage") or "") == "planned")
    submitted_cost = _order_memory_cost_summary(
        row for row in memory if str(row.get("stage") or "") in {"submitted", "filled"} and not row.get("rejected")
    )
    return {
        "records": len(memory),
        "stages": stages,
        "rejected": sum(1 for row in memory if row.get("rejected")),
        "partial_fill": sum(1 for row in memory if row.get("partial_fill")),
        "stale_book": sum(1 for row in memory if row.get("stale_book")),
        "planned_records": planned_cost["records"],
        "planned_notional": planned_cost["notional"],
        "planned_estimated_fees": planned_cost["estimated_fees"],
        "submitted_records": submitted_cost["records"],
        "submitted_notional": submitted_cost["notional"],
        "submitted_logged_fees": submitted_cost["logged_fees"],
        "submitted_estimated_fees": submitted_cost["estimated_fees"],
        "submitted_cost_by_symbol": {
            symbol: _order_memory_cost_summary(rows)
            for symbol, rows in sorted(by_submitted_symbol.items())
            if symbol != "UNKNOWN"
        },
        "avg_exchange_latency_ms": sum(valid_latencies) / len(valid_latencies) if valid_latencies else None,
        "valid_exchange_latency_count": len(valid_latencies),
        "invalid_exchange_latency_count": invalid_latency_count,
        "by_venue": {
            venue: {
                "records": len(rows),
                "rejected": sum(1 for row in rows if row.get("rejected")),
                "partial_fill": sum(1 for row in rows if row.get("partial_fill")),
                "stale_book": sum(1 for row in rows if row.get("stale_book")),
                "invalid_exchange_latency_count": sum(
                    1 for row in rows if row.get("exchange_latency_ms") is not None and _valid_latency_ms(row.get("exchange_latency_ms")) is None
                ),
            }
            for venue, rows in sorted(by_venue.items())
        },
    }


def _perp_status_account(status_path: Path, state_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    status = _load_json(status_path)
    account = status.get("account") if isinstance(status.get("account"), dict) else {}
    if account:
        return status, dict(account)

    state = _load_json(state_path)
    if not state:
        return status, {}
    realized = _safe_float(state.get("realized_pnl"))
    fees = _safe_float(state.get("fees_paid"))
    funding = _safe_float(state.get("funding_paid"))
    cash = _safe_float(state.get("cash"))
    return status, {
        "cash": cash,
        "wallet_balance": cash,
        "equity": cash,
        "realized_pnl": realized,
        "unrealized_pnl": 0.0,
        "fees_paid": fees,
        "funding_paid": funding,
        "net_pnl": realized - fees - funding,
        "pnl_source": "local_perp_state",
    }


def _perp_order_evidence(
    *,
    orders_path: Path = CRYPTO_PERP_ORDERS_FILE,
    status_path: Path = CRYPTO_PERP_STATUS_FILE,
    state_path: Path = CRYPTO_PERP_STATE_FILE,
) -> dict[str, Any]:
    rows = _load_jsonl(orders_path)
    status, account = _perp_status_account(status_path, state_path)
    by_symbol: dict[str, dict[str, Any]] = {}
    status_counts: dict[str, int] = {}
    total_notional = 0.0
    gross_pnl = 0.0
    fees = 0.0
    first_ts = ""
    last_ts = ""

    for row in rows:
        symbol = str(row.get("symbol") or "unknown").upper()
        status_text = str(row.get("status") or "unknown").lower()
        status_counts[status_text] = status_counts.get(status_text, 0) + 1
        row_notional = abs(_safe_float(row.get("notional")))
        row_fee = _safe_float(row.get("fee"))
        response = row.get("response") if isinstance(row.get("response"), dict) else {}
        realized = _safe_float(response.get("paper_realized_pnl"))
        total_notional += row_notional
        fees += row_fee
        gross_pnl += realized
        row_ts = str(row.get("ts") or "")
        if row_ts and (not first_ts or row_ts < first_ts):
            first_ts = row_ts
        if row_ts and row_ts > last_ts:
            last_ts = row_ts

        summary = by_symbol.setdefault(
            symbol,
            {
                "records": 0,
                "filled": 0,
                "posted": 0,
                "expired": 0,
                "notional": 0.0,
                "gross_pnl": 0.0,
                "fees": 0.0,
                "net_after_fees": 0.0,
            },
        )
        summary["records"] += 1
        if status_text in {"filled", "posted", "expired"}:
            summary[status_text] += 1
        summary["notional"] += row_notional
        summary["gross_pnl"] += realized
        summary["fees"] += row_fee
        summary["net_after_fees"] = summary["gross_pnl"] - summary["fees"]

    account_gross = _safe_float(account.get("realized_pnl"), gross_pnl)
    account_fees = _safe_float(account.get("fees_paid"), fees)
    funding_paid = _safe_float(account.get("funding_paid"))
    computed_net = account_gross - account_fees - funding_paid
    net_pnl = _safe_float(account.get("net_pnl"), computed_net)
    fees_to_gross_ratio = account_fees / abs(account_gross) if account_gross else None
    summary_by_symbol = {
        symbol: {
            key: round(value, 8) if isinstance(value, float) else value
            for key, value in summary.items()
        }
        for symbol, summary in sorted(by_symbol.items())
    }
    top_loss_symbols = sorted(
        (
            {"symbol": symbol, **summary}
            for symbol, summary in summary_by_symbol.items()
        ),
        key=lambda row: _safe_float(row.get("net_after_fees")),
    )[:5]
    drag_reason = ""
    if net_pnl < 0 and account_fees >= max(abs(account_gross), 0.01):
        drag_reason = "fees_exceed_gross_pnl"
    elif net_pnl < 0 and funding_paid > 0 and funding_paid >= max(abs(account_gross), 0.01):
        drag_reason = "funding_exceeds_gross_pnl"
    elif net_pnl < 0:
        drag_reason = "negative_perp_pnl"

    return {
        "record_type": "crypto_perp_order_evidence",
        "orders_path": str(orders_path),
        "status_path": str(status_path),
        "state_path": str(state_path),
        "status_updated_at": status.get("updated_at") or status.get("ts") or "",
        "status": status.get("status", ""),
        "mode": status.get("mode", ""),
        "reason": status.get("reason", ""),
        "pnl_source": account.get("pnl_source", ""),
        "records": len(rows),
        "status_counts": status_counts,
        "filled_records": int(status_counts.get("filled", 0)),
        "posted_records": int(status_counts.get("posted", 0)),
        "expired_records": int(status_counts.get("expired", 0)),
        "first_order_ts": first_ts,
        "last_order_ts": last_ts,
        "notional": round(total_notional, 8),
        "gross_pnl": round(account_gross, 8),
        "fees": round(account_fees, 8),
        "funding_paid": round(funding_paid, 8),
        "net_pnl": round(net_pnl, 8),
        "fees_to_abs_gross_pnl": round(fees_to_gross_ratio, 8) if fees_to_gross_ratio is not None else None,
        "drag_reason": drag_reason,
        "account_trade_count": int(_safe_float(account.get("trade_count"), status_counts.get("filled", 0))),
        "by_symbol": summary_by_symbol,
        "top_loss_symbols": top_loss_symbols,
    }


def _order_memory_after_epoch(
    memory: list[dict[str, Any]],
    *,
    mode: str,
    quote_asset: str,
    epoch_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    epoch = _load_json(epoch_path)
    cutoff = _epoch_seconds(epoch_path, mode=mode, quote_asset=quote_asset)
    if cutoff is None:
        return memory, {
            "scope": "all_order_memory",
            "mode": mode,
            "quote_asset": quote_asset,
            "records": len(memory),
            "excluded_records": 0,
            "epoch_id": epoch.get("epoch_id", ""),
            "epoch_ts": epoch.get("ts", ""),
            "reason": "no_matching_ledger_epoch",
        }

    scoped: list[dict[str, Any]] = []
    excluded = 0
    for row in memory:
        row_mode = str(row.get("mode") or "")
        if row_mode and row_mode != mode:
            excluded += 1
            continue
        if _after_epoch(row.get("ts"), cutoff):
            scoped.append(row)
        else:
            excluded += 1
    return scoped, {
        "scope": "ledger_epoch",
        "mode": mode,
        "quote_asset": quote_asset,
        "records": len(scoped),
        "excluded_records": excluded,
        "epoch_id": epoch.get("epoch_id", ""),
        "epoch_ts": epoch.get("ts", ""),
        "reason": epoch.get("reason", ""),
    }


def build_attribution_report(
    outcomes: list[dict[str, Any]],
    *,
    order_memory: list[dict[str, Any]] | None = None,
    perp_orders_path: Path = CRYPTO_PERP_ORDERS_FILE,
    perp_status_path: Path = CRYPTO_PERP_STATUS_FILE,
    perp_state_path: Path = CRYPTO_PERP_STATE_FILE,
    attribution_path: Path | None = CRYPTO_ATTRIBUTION_FILE,
) -> dict[str, Any]:
    memory = order_memory if order_memory is not None else load_order_memory()
    perp_evidence = _perp_order_evidence(
        orders_path=perp_orders_path,
        status_path=perp_status_path,
        state_path=perp_state_path,
    )
    report = {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "total": _metric_summary(outcomes),
        "by_strategy": _group_summary(outcomes, "strategy"),
        "by_symbol": _group_summary(outcomes, "symbol"),
        "by_reason": _group_summary(outcomes, "primary_reason"),
        "by_reason_tag": _reason_tag_summary(outcomes),
        "by_venue": _group_summary(outcomes, "venue"),
        "by_timeframe": _group_summary(outcomes, "timeframe"),
        "by_market_regime_24h": _group_summary(outcomes, "market_regime_24h"),
        "order_quality": _order_quality(memory),
        "perp_order_evidence": perp_evidence,
        "safety": {
            "live_auto_promotion": False,
            "code_auto_modification": False,
            "scope": "crypto_evidence_to_review_only",
        },
        "notes": [
            "Outcomes are FIFO realized round trips from crypto order/user-fill logs.",
            "Candidates are research/paper review ideas only; no live auto-promotion is allowed.",
            "Spot mode has leverage=1, margin_mode=none, funding=0 unless future venues add those fields.",
        ],
    }
    if attribution_path is not None:
        _write_json_atomic(attribution_path, report)
    return report


def _candidate(
    *,
    action_type: str,
    rationale: str,
    evidence: dict[str, Any],
    param: str = "",
    current_value: Any = None,
    proposed_value: Any = None,
    confidence: float = 0.0,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "status": "research_only",
        "action_type": action_type,
        "param": param,
        "current_value": current_value,
        "proposed_value": proposed_value,
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "rationale": rationale,
        "evidence": evidence,
        "forbidden_actions": ["auto_modify_code", "auto_modify_live_params", "auto_promote_live_strategy"],
        "safety_gate": "requires_codex_human_review_replay_paper_and_manual_approval_before_any_code_change",
    }
    payload["candidate_id"] = _hash_payload(
        {"action_type": action_type, "param": param, "proposed_value": proposed_value, "evidence": evidence},
        length=20,
    )
    return payload


def generate_upgrade_candidates(
    report: dict[str, Any],
    *,
    settings: Any | None = None,
    candidates_path: Path | None = CRYPTO_UPGRADE_CANDIDATES_FILE,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    total = dict(report.get("total") or {})
    trades = int(total.get("trades", 0) or 0)
    if trades < 10:
        candidates.append(
            _candidate(
                action_type="collect_more_data",
                rationale="Crypto realized outcomes sample is still small; generate review evidence before changing anything.",
                evidence={"trades": trades, "minimum_recommended": 10},
                confidence=0.25,
            )
        )

    by_reason = dict(report.get("by_reason") or {})
    by_reason_tag = dict(report.get("by_reason_tag") or {})
    fees = dict(by_reason_tag.get("fees_dominated") or by_reason.get("fees_dominated") or {})
    low_edge = dict(by_reason_tag.get("flat") or by_reason.get("flat") or {})
    if int(fees.get("trades", 0) or 0) >= 3 or (int(low_edge.get("trades", 0) or 0) >= 3 and _safe_float(low_edge.get("net_pnl")) <= 0):
        current = getattr(settings, "min_order_notional", None)
        proposed = round(max(_safe_float(current, 20.0) * 1.5, _safe_float(current, 20.0) + 10.0), 2)
        candidates.append(
            _candidate(
                action_type="raise_min_order_value",
                param="CRYPTO_OFIM_MIN_ORDER_NOTIONAL",
                current_value=current,
                proposed_value=proposed,
                rationale="Fees dominate too many low-edge crypto round trips; test a higher minimum order value in paper/replay.",
                evidence={"fees_dominated": fees, "flat": low_edge},
                confidence=0.55,
            )
        )

    signal = dict(by_reason_tag.get("signal_error") or by_reason.get("signal_error") or {})
    if int(signal.get("trades", 0) or 0) >= 3 and _safe_float(signal.get("net_pnl")) < 0:
        current = getattr(settings, "entry_threshold", None)
        candidates.append(
            _candidate(
                action_type="tighten_entry_threshold",
                param="CRYPTO_OFIM_ENTRY_THRESHOLD",
                current_value=current,
                proposed_value=round(_safe_float(current, 0.2) + 0.05, 4),
                rationale="Losing trades are attributed to signal error; test a stricter OFIM entry threshold.",
                evidence=signal,
                confidence=0.6,
            )
        )

    fast_noise = dict(by_reason_tag.get("fast_noise_loss") or by_reason.get("fast_noise_loss") or {})
    if int(fast_noise.get("trades", 0) or 0) >= 10 and _safe_float(fast_noise.get("net_pnl")) < 0:
        current = getattr(settings, "min_reentry_after_risk_off_seconds", None)
        current_seconds = _safe_float(current, 900.0)
        proposed = int(max(current_seconds * 2.0, current_seconds + 900.0, 1800.0))
        candidates.append(
            _candidate(
                action_type="extend_risk_off_reentry_cooldown",
                param="CRYPTO_OFIM_MIN_REENTRY_AFTER_RISK_OFF_SECONDS",
                current_value=current,
                proposed_value=proposed,
                rationale="Fast losing round trips show churn after risk-off flips; test a longer re-entry cooldown in replay/paper before changing execution.",
                evidence=fast_noise,
                confidence=0.62,
            )
        )

    spread = dict(by_reason_tag.get("slippage_or_spread") or by_reason.get("slippage_or_spread") or {})
    if int(spread.get("trades", 0) or 0) >= 2 or abs(_safe_float(total.get("avg_slippage_bps"))) > 10:
        current = getattr(settings, "max_spread_bps", None)
        candidates.append(
            _candidate(
                action_type="widen_or_tighten_spread_guard",
                param="CRYPTO_OFIM_MAX_SPREAD_BPS",
                current_value=current,
                proposed_value=round(max(1.0, _safe_float(current, 20.0) * 0.8), 4),
                rationale="Slippage/spread is visible in losing outcomes; test a tighter spread guard before live changes.",
                evidence={"slippage_reason": spread, "total_avg_slippage_bps": total.get("avg_slippage_bps")},
                confidence=0.5,
            )
        )

    funding = dict(by_reason.get("funding_drag") or {})
    if int(funding.get("trades", 0) or 0) > 0 or _safe_float(total.get("funding_paid")) < 0:
        candidates.append(
            _candidate(
                action_type="avoid_high_funding",
                rationale="Funding drag is present. Spot currently has funding=0; if perp venues are enabled, add a funding guard.",
                evidence={"funding_drag": funding, "funding_paid": total.get("funding_paid")},
                confidence=0.35,
            )
        )

    by_symbol = dict(report.get("by_symbol") or {})
    for symbol, summary in by_symbol.items():
        summary = dict(summary or {})
        if int(summary.get("trades", 0) or 0) >= 3 and _safe_float(summary.get("net_pnl")) < 0:
            candidates.append(
                _candidate(
                    action_type="reduce_symbol_weight",
                    rationale=f"{symbol} has negative realized contribution; test reducing max symbol weight or excluding it in paper.",
                    evidence={"symbol": symbol, **summary},
                    confidence=0.45,
                )
            )
            if _safe_float(summary.get("max_drawdown")) < -abs(_safe_float(summary.get("net_pnl"))) * 0.8:
                candidates.append(
                    _candidate(
                        action_type="pause_symbol_or_venue",
                        rationale=f"{symbol} drawdown is concentrated; generate a review packet before trading it again.",
                        evidence={"symbol": symbol, **summary},
                        confidence=0.4,
                    )
                )

    order_quality = dict(report.get("order_quality") or {})
    submitted_records = int(order_quality.get("submitted_records", 0) or 0)
    submitted_estimated_fees = _safe_float(order_quality.get("submitted_estimated_fees"))
    submitted_cost_by_symbol = {
        symbol: dict(summary or {})
        for symbol, summary in dict(order_quality.get("submitted_cost_by_symbol") or {}).items()
    }
    if trades == 0 and submitted_records >= 20 and submitted_estimated_fees > 0:
        top_symbols = sorted(
            (
                {
                    "symbol": symbol,
                    "records": int(summary.get("records", 0) or 0),
                    "notional": _safe_float(summary.get("notional")),
                    "estimated_fees": _safe_float(summary.get("estimated_fees")),
                    "taker_records": int(summary.get("taker_records", 0) or 0),
                }
                for symbol, summary in submitted_cost_by_symbol.items()
            ),
            key=lambda row: _safe_float(row.get("estimated_fees")),
            reverse=True,
        )[:5]
        candidates.append(
            _candidate(
                action_type="investigate_order_memory_fee_drag",
                rationale="Round-trip outcomes are empty, but submitted order memory shows material fee drag; rebuild attribution or keep entries paused before collecting more execution data.",
                evidence={
                    "outcome_trades": trades,
                    "submitted_records": submitted_records,
                    "submitted_estimated_fees": round(submitted_estimated_fees, 8),
                    "top_symbols": top_symbols,
                },
                confidence=0.58,
            )
        )
    historical_order_quality = dict(report.get("historical_order_quality") or {})
    historical_submitted_records = int(historical_order_quality.get("submitted_records", 0) or 0)
    historical_submitted_estimated_fees = _safe_float(historical_order_quality.get("submitted_estimated_fees"))
    if trades == 0 and submitted_records == 0 and historical_submitted_records >= 20 and historical_submitted_estimated_fees > 0:
        historical_cost_by_symbol = {
            symbol: dict(summary or {})
            for symbol, summary in dict(historical_order_quality.get("submitted_cost_by_symbol") or {}).items()
        }
        top_symbols = sorted(
            (
                {
                    "symbol": symbol,
                    "records": int(summary.get("records", 0) or 0),
                    "notional": _safe_float(summary.get("notional")),
                    "estimated_fees": _safe_float(summary.get("estimated_fees")),
                    "taker_records": int(summary.get("taker_records", 0) or 0),
                }
                for symbol, summary in historical_cost_by_symbol.items()
            ),
            key=lambda row: _safe_float(row.get("estimated_fees")),
            reverse=True,
        )[:5]
        scope = dict(report.get("order_quality_scope") or {})
        candidates.append(
            _candidate(
                action_type="keep_spot_guarded_after_historical_fee_drag",
                rationale="Current epoch has no submitted spot orders, but historical testnet order memory shows material fee drag; keep spot entries conservative until a fresh epoch proves lower churn.",
                evidence={
                    "outcome_trades": trades,
                    "current_epoch_submitted_records": submitted_records,
                    "historical_submitted_records": historical_submitted_records,
                    "historical_submitted_estimated_fees": round(historical_submitted_estimated_fees, 8),
                    "excluded_records": int(scope.get("excluded_records", 0) or 0),
                    "epoch_id": scope.get("epoch_id", ""),
                    "epoch_ts": scope.get("epoch_ts", ""),
                    "top_symbols": top_symbols,
                },
                confidence=0.6,
            )
        )
    if int(order_quality.get("rejected", 0) or 0) or int(order_quality.get("partial_fill", 0) or 0) or int(order_quality.get("stale_book", 0) or 0):
        candidates.append(
            _candidate(
                action_type="improve_execution_scheduler",
                rationale="Rejected, partial-fill, or stale-book evidence exists; consider execution scheduling improvements in research.",
                evidence=order_quality,
                confidence=0.5,
            )
        )
    perp = dict(report.get("perp_order_evidence") or {})
    if int(perp.get("filled_records", 0) or 0) and _safe_float(perp.get("net_pnl")) < 0:
        candidates.append(
            _candidate(
                action_type="keep_perp_guarded_until_fee_drag_retest",
                rationale="Perp paper evidence is net negative and fee drag is visible; keep new perp entries blocked or paper-only until maker/confirm/cooldown settings pass a fresh replay or paper retest.",
                evidence={
                    "mode": perp.get("mode"),
                    "status": perp.get("status"),
                    "reason": perp.get("reason"),
                    "filled_records": perp.get("filled_records"),
                    "gross_pnl": perp.get("gross_pnl"),
                    "fees": perp.get("fees"),
                    "funding_paid": perp.get("funding_paid"),
                    "net_pnl": perp.get("net_pnl"),
                    "fees_to_abs_gross_pnl": perp.get("fees_to_abs_gross_pnl"),
                    "top_loss_symbols": perp.get("top_loss_symbols", []),
                },
                confidence=0.68 if perp.get("drag_reason") == "fees_exceed_gross_pnl" else 0.5,
            )
        )
    deduped = {str(candidate["candidate_id"]): candidate for candidate in candidates}
    out = list(deduped.values())
    if candidates_path is not None:
        _write_jsonl_atomic(candidates_path, out)
    return out


def build_promotion_report(
    candidates: list[dict[str, Any]],
    report: dict[str, Any],
    *,
    promotion_path: Path | None = CRYPTO_PROMOTION_REPORT_FILE,
    min_trades_for_replay: int = 30,
) -> dict[str, Any]:
    total_trades = int(dict(report.get("total") or {}).get("trades", 0) or 0)
    decisions: list[dict[str, Any]] = []
    for candidate in candidates:
        evidence = dict(candidate.get("evidence") or {})
        evidence_trades = int(evidence.get("trades", total_trades) or 0)
        blockers: list[str] = []
        if total_trades < min_trades_for_replay:
            blockers.append(f"total_trades<{min_trades_for_replay}")
        if evidence_trades and evidence_trades < max(5, min_trades_for_replay // 3):
            blockers.append("candidate_evidence_sample_too_small")
        if candidate.get("action_type") == "collect_more_data":
            blockers.append("data_collection_only")
        decision = "eligible_for_replay_review" if not blockers else "needs_more_data"
        decisions.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "action_type": candidate.get("action_type"),
                "decision": decision,
                "blockers": blockers,
                "research_allowed": True,
                "paper_allowed": decision == "eligible_for_replay_review",
                "live_allowed": False,
                "code_auto_change_allowed": False,
            }
        )
    promotion = {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "policy": {
            "live_auto_promotion": False,
            "code_auto_modification": False,
            "required_before_live": [
                "human_codex_review",
                "walk_forward_or_purged_validation",
                "cost_slippage_latency_stress",
                "paper_or_testnet_observation",
                "manual_parameter_or_code_approval",
            ],
        },
        "total_trades": total_trades,
        "decisions": decisions,
    }
    if promotion_path is not None:
        _write_json_atomic(promotion_path, promotion)
    return promotion


def _top_outcomes(outcomes: list[dict[str, Any]], *, reverse: bool, limit: int = 10) -> list[dict[str, Any]]:
    rows = sorted(outcomes, key=lambda row: _safe_float(row.get("net_pnl")), reverse=reverse)[:limit]
    return [
        {
            "outcome_id": row.get("outcome_id", ""),
            "symbol": row.get("symbol", ""),
            "venue": row.get("venue", ""),
            "timeframe": row.get("timeframe", ""),
            "net_pnl": row.get("net_pnl", 0.0),
            "gross_pnl": row.get("gross_pnl", 0.0),
            "fees": row.get("fees", 0.0),
            "slippage_bps": row.get("slippage_bps", 0.0),
            "primary_reason": row.get("primary_reason", ""),
            "reason_tags": row.get("reason_tags", []),
        }
        for row in rows
    ]


def _markdown_metric_table(rows: dict[str, Any]) -> str:
    keys = [
        "trades",
        "wins",
        "losses",
        "win_rate",
        "gross_pnl",
        "fees",
        "estimated_fees",
        "estimated_fee_count",
        "net_pnl",
        "avg_return_pct",
        "profit_factor",
        "max_drawdown",
        "avg_slippage_bps",
    ]
    lines = ["| metric | value |", "| --- | --- |"]
    for key in keys:
        if key in rows:
            lines.append(f"| {key} | {rows.get(key)} |")
    return "\n".join(lines)


def _markdown_group_table(grouped: dict[str, Any], *, name_column: str, limit: int = 12) -> str:
    lines = [f"| {name_column} | trades | win_rate | net_pnl | fees | avg_slippage_bps |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for name, summary in list(grouped.items())[:limit]:
        summary = dict(summary or {})
        lines.append(
            "| "
            + " | ".join(
                [
                    str(name),
                    str(summary.get("trades", 0)),
                    str(round(_safe_float(summary.get("win_rate")), 4)),
                    str(round(_safe_float(summary.get("net_pnl")), 6)),
                    str(round(_safe_float(summary.get("fees")), 6)),
                    str(round(_safe_float(summary.get("avg_slippage_bps")), 6)),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _markdown_order_quality_table(order_quality: dict[str, Any]) -> str:
    keys = [
        "records",
        "rejected",
        "partial_fill",
        "stale_book",
        "planned_records",
        "planned_notional",
        "planned_estimated_fees",
        "submitted_records",
        "submitted_notional",
        "submitted_logged_fees",
        "submitted_estimated_fees",
        "avg_exchange_latency_ms",
        "valid_exchange_latency_count",
        "invalid_exchange_latency_count",
    ]
    lines = ["| metric | value |", "| --- | --- |"]
    for key in keys:
        if key in order_quality:
            value = order_quality.get(key)
            lines.append(f"| {key} | {value if value is not None else 'null'} |")
    return "\n".join(lines)


def _markdown_perp_evidence_table(perp: dict[str, Any]) -> str:
    keys = [
        "mode",
        "status",
        "reason",
        "pnl_source",
        "filled_records",
        "posted_records",
        "expired_records",
        "gross_pnl",
        "fees",
        "funding_paid",
        "net_pnl",
        "fees_to_abs_gross_pnl",
        "drag_reason",
        "status_updated_at",
    ]
    lines = ["| metric | value |", "| --- | --- |"]
    for key in keys:
        if key in perp:
            value = perp.get(key)
            lines.append(f"| {key} | {value if value is not None else 'null'} |")
    return "\n".join(lines)


def _data_quality_warnings(report: dict[str, Any]) -> list[str]:
    total = dict(report.get("total") or {})
    order_quality = dict(report.get("order_quality") or {})
    order_quality_scope = dict(report.get("order_quality_scope") or {})
    perp = dict(report.get("perp_order_evidence") or {})
    warnings: list[str] = []
    excluded_records = int(order_quality_scope.get("excluded_records", 0) or 0)
    if order_quality_scope.get("scope") == "ledger_epoch" and excluded_records:
        warnings.append(
            f"Order-memory quality is scoped to ledger epoch `{order_quality_scope.get('epoch_id', '')}`; "
            f"{excluded_records} pre-epoch rows are retained only in historical_order_quality."
        )
    invalid_latency = int(order_quality.get("invalid_exchange_latency_count", 0) or 0)
    valid_latency = int(order_quality.get("valid_exchange_latency_count", 0) or 0)
    records = int(order_quality.get("records", 0) or 0)
    if invalid_latency:
        warnings.append(
            f"`exchange_latency_ms` has {invalid_latency} invalid historical rows; do not use latency attribution until fresh measured samples arrive."
        )
    if records and valid_latency == 0:
        warnings.append("No valid exchange latency samples are available in this packet.")
    avg_slippage = _safe_float(total.get("avg_slippage_bps"))
    if abs(avg_slippage) > 10:
        warnings.append(f"Average slippage is {round(avg_slippage, 6)} bps; review fill/context matching and outliers before live changes.")
    estimated_fee_count = int(total.get("estimated_fee_count", 0) or 0)
    if estimated_fee_count:
        warnings.append(
            f"{estimated_fee_count} outcomes use conservative estimated spot fees where exchange/testnet logs reported zero commission."
        )
    submitted_records = int(order_quality.get("submitted_records", 0) or 0)
    submitted_estimated_fees = _safe_float(order_quality.get("submitted_estimated_fees"))
    if submitted_records and submitted_estimated_fees > 0:
        warnings.append(
            f"Submitted order memory implies {round(submitted_estimated_fees, 6)} estimated spot fees across {submitted_records} submitted/filled rows; use this to avoid treating empty outcomes as zero cost."
        )
    if int(perp.get("filled_records", 0) or 0) and _safe_float(perp.get("net_pnl")) < 0:
        warnings.append(
            "Perp paper evidence is negative: "
            f"net {round(_safe_float(perp.get('net_pnl')), 6)}, "
            f"gross {round(_safe_float(perp.get('gross_pnl')), 6)}, "
            f"fees {round(_safe_float(perp.get('fees')), 6)}, "
            f"funding_paid {round(_safe_float(perp.get('funding_paid')), 6)}."
        )
    if records and not any(int(order_quality.get(key, 0) or 0) for key in ("rejected", "partial_fill", "stale_book")):
        warnings.append("Rejected, partial-fill and stale-book counts are all zero; treat this as observed-log evidence, not proof of absence.")
    return warnings


def _markdown_data_quality_warnings(report: dict[str, Any]) -> str:
    warnings = _data_quality_warnings(report)
    if not warnings:
        return "No obvious data-quality warnings in this packet."
    return "\n".join(f"- {warning}" for warning in warnings)


def _candidate_markdown(candidates: list[dict[str, Any]], promotion: dict[str, Any]) -> str:
    decisions = {str(row.get("candidate_id")): row for row in promotion.get("decisions", []) if isinstance(row, dict)}
    lines = ["| candidate_id | action | param | proposed | confidence | gate | rationale |", "| --- | --- | --- | --- | ---: | --- | --- |"]
    for item in candidates[:20]:
        decision = decisions.get(str(item.get("candidate_id")), {})
        rationale = str(item.get("rationale", "")).replace("\n", " ")[:180]
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item.get("candidate_id", "")),
                    str(item.get("action_type", "")),
                    str(item.get("param", "")),
                    str(item.get("proposed_value", "")),
                    str(item.get("confidence", "")),
                    str(decision.get("decision", "unreviewed")),
                    rationale,
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _render_review_packet_markdown(packet: dict[str, Any]) -> str:
    report = dict(packet.get("attribution_report") or {})
    artifacts = dict(packet.get("artifacts") or {})
    candidates = list(packet.get("upgrade_candidates") or [])
    promotion = dict(packet.get("promotion_report") or {})
    lines = [
        "# Crypto Evidence-to-Review Packet",
        "",
        f"- generated_at: `{packet.get('generated_at', '')}`",
        f"- packet_id: `{packet.get('packet_id', '')}`",
        "- live_auto_promotion: `False`",
        "- code_auto_modification: `False`",
        "- 结论：本文件只记录证据和候选研究方向，不允许自动改代码或 live 参数。",
        "",
        "## How to Use With Codex",
        "",
        str(packet.get("codex_review_prompt", "")),
        "",
        "## Summary",
        "",
        _markdown_metric_table(dict(report.get("total") or {})),
        "",
        "## Strategy Attribution",
        "",
        _markdown_group_table(dict(report.get("by_strategy") or {}), name_column="strategy"),
        "",
        "## Symbol Attribution",
        "",
        _markdown_group_table(dict(report.get("by_symbol") or {}), name_column="symbol"),
        "",
        "## Reason Attribution",
        "",
        _markdown_group_table(dict(report.get("by_reason") or {}), name_column="reason"),
        "",
        "## Venue Attribution",
        "",
        _markdown_group_table(dict(report.get("by_venue") or {}), name_column="venue"),
        "",
        "## Order Quality",
        "",
        _markdown_order_quality_table(dict(report.get("order_quality") or {})),
        "",
        "## Perp PnL Evidence",
        "",
        _markdown_perp_evidence_table(dict(report.get("perp_order_evidence") or {})),
        "",
        "## Data Quality Risks",
        "",
        _markdown_data_quality_warnings(report),
        "",
        "## Upgrade Candidates",
        "",
        _candidate_markdown(candidates, promotion) if candidates else "No candidates.",
        "",
        "## Top Winners",
        "",
        "```json",
        json.dumps(packet.get("top_winners", []), ensure_ascii=False, indent=2, default=_json_default),
        "```",
        "",
        "## Top Losers",
        "",
        "```json",
        json.dumps(packet.get("top_losers", []), ensure_ascii=False, indent=2, default=_json_default),
        "```",
        "",
        "## Artifacts",
        "",
        "| artifact | path | bytes | sha256 |",
        "| --- | --- | ---: | --- |",
    ]
    for name, meta in artifacts.items():
        meta = dict(meta or {})
        lines.append(f"| {name} | `{meta.get('path', '')}` | {meta.get('bytes', 0)} | `{meta.get('sha256', '')}` |")
    lines.extend(
        [
            "",
            "## Review Checklist",
            "",
            "- 样本量是否足够，是否存在单 symbol、单 regime、单 venue 偶然性。",
            "- 是否已经把 maker/taker fees、slippage、partial fill、rejection、stale book 纳入判断。",
            "- 如果未来接入 perpetual，是否检查 leverage、margin mode、liquidation distance 和 funding drag。",
            "- 候选是否只能进入 replay/paper/testnet，不得直接 live promotion。",
            "- 如需改代码，请让 Codex 先写独立计划和测试，不要让学习系统自改代码。",
            "",
        ]
    )
    return "\n".join(lines)


def build_learning_review_packet(
    report: dict[str, Any],
    candidates: list[dict[str, Any]],
    promotion: dict[str, Any],
    outcomes: list[dict[str, Any]],
    *,
    order_memory_path: Path = CRYPTO_ORDER_MEMORY_FILE,
    outcomes_path: Path = CRYPTO_TRADE_OUTCOMES_FILE,
    attribution_path: Path = CRYPTO_ATTRIBUTION_FILE,
    candidates_path: Path = CRYPTO_UPGRADE_CANDIDATES_FILE,
    promotion_path: Path = CRYPTO_PROMOTION_REPORT_FILE,
    review_packet_path: Path | None = CRYPTO_LEARNING_REVIEW_PACKET_FILE,
    review_packet_json_path: Path | None = CRYPTO_LEARNING_REVIEW_PACKET_JSON_FILE,
) -> dict[str, Any]:
    order_memory = load_order_memory(order_memory_path)
    generated_at = _utc_now()
    seed = {
        "generated_at": generated_at,
        "summary": report.get("total", {}),
        "candidates": [candidate.get("candidate_id") for candidate in candidates],
        "artifact_hashes": {
            "order_memory": _file_sha256(order_memory_path),
            "outcomes": _file_sha256(outcomes_path),
            "attribution": _file_sha256(attribution_path),
            "candidates": _file_sha256(candidates_path),
            "promotion": _file_sha256(promotion_path),
        },
    }
    packet = {
        "schema_version": 1,
        "record_type": "crypto_learning_review_packet",
        "generated_at": generated_at,
        "packet_id": _hash_payload(seed, length=20),
        "approval_policy": {
            "code_auto_modification": False,
            "live_auto_promotion": False,
            "review_required_before_code_change": True,
            "manual_reviewer": "human+Codex",
            "allowed_next_stage": "research_replay_or_paper_only",
        },
        "codex_review_prompt": (
            "请基于这份 crypto Evidence-to-Review packet 评估候选改进。"
            "先检查 artifact sha256、样本量、PnL 归因、费用/滑点、partial fill/rejection/stale book、"
            "promotion gate 和过拟合风险；禁止自动改 live 参数。"
        ),
        "attribution_report": report,
        "upgrade_candidates": candidates,
        "promotion_report": promotion,
        "top_winners": _top_outcomes(outcomes, reverse=True),
        "top_losers": _top_outcomes(outcomes, reverse=False),
        "recent_order_memory": order_memory[-20:],
        "artifacts": {
            "order_memory": _artifact_meta(order_memory_path),
            "outcomes": _artifact_meta(outcomes_path),
            "attribution": _artifact_meta(attribution_path),
            "candidates": _artifact_meta(candidates_path),
            "promotion": _artifact_meta(promotion_path),
        },
        "limitations": [
            "This packet records evidence, not causality.",
            "Small samples and repeated strategy searches can overfit quickly in crypto.",
            "Live changes require replay, paper/testnet observation and manual approval.",
        ],
    }
    if review_packet_json_path is not None:
        _write_json_atomic(review_packet_json_path, packet)
    if review_packet_path is not None:
        _write_text_atomic(review_packet_path, _render_review_packet_markdown(packet))
    return packet


def run_learning_pipeline(
    *,
    mode: str = "paper",
    quote_asset: str = "USDT",
    settings: Any | None = None,
    orders_path: Path = CRYPTO_ORDERS_FILE,
    user_fills_path: Path = CRYPTO_USER_FILLS_FILE,
    order_memory_path: Path = CRYPTO_ORDER_MEMORY_FILE,
    epoch_path: Path = CRYPTO_LEDGER_EPOCH_FILE,
    perp_orders_path: Path = CRYPTO_PERP_ORDERS_FILE,
    perp_status_path: Path = CRYPTO_PERP_STATUS_FILE,
    perp_state_path: Path = CRYPTO_PERP_STATE_FILE,
    outcomes_path: Path = CRYPTO_TRADE_OUTCOMES_FILE,
    attribution_path: Path = CRYPTO_ATTRIBUTION_FILE,
    candidates_path: Path = CRYPTO_UPGRADE_CANDIDATES_FILE,
    promotion_path: Path = CRYPTO_PROMOTION_REPORT_FILE,
    review_packet_path: Path = CRYPTO_LEARNING_REVIEW_PACKET_FILE,
    review_packet_json_path: Path = CRYPTO_LEARNING_REVIEW_PACKET_JSON_FILE,
) -> CryptoLearningPipelineResult:
    _ensure_jsonl_artifact(order_memory_path)
    outcomes = build_trade_outcomes(
        mode=mode,
        quote_asset=quote_asset,
        orders_path=orders_path,
        user_fills_path=user_fills_path,
        order_memory_path=order_memory_path,
        epoch_path=epoch_path,
        outcome_path=outcomes_path,
    )
    all_order_memory = load_order_memory(order_memory_path)
    order_memory, order_memory_scope = _order_memory_after_epoch(
        all_order_memory,
        mode=mode,
        quote_asset=quote_asset,
        epoch_path=epoch_path,
    )
    report = build_attribution_report(
        outcomes,
        order_memory=order_memory,
        perp_orders_path=perp_orders_path,
        perp_status_path=perp_status_path,
        perp_state_path=perp_state_path,
        attribution_path=None,
    )
    report["order_quality_scope"] = order_memory_scope
    if len(order_memory) != len(all_order_memory):
        report["historical_order_quality"] = _order_quality(all_order_memory)
    if attribution_path is not None:
        _write_json_atomic(attribution_path, report)
    candidates = generate_upgrade_candidates(report, settings=settings, candidates_path=candidates_path)
    promotion = build_promotion_report(candidates, report, promotion_path=promotion_path)
    build_learning_review_packet(
        report,
        candidates,
        promotion,
        outcomes,
        order_memory_path=order_memory_path,
        outcomes_path=outcomes_path,
        attribution_path=attribution_path,
        candidates_path=candidates_path,
        promotion_path=promotion_path,
        review_packet_path=review_packet_path,
        review_packet_json_path=review_packet_json_path,
    )
    return CryptoLearningPipelineResult(
        outcome_count=len(outcomes),
        candidate_count=len(candidates),
        attribution_path=attribution_path,
        outcomes_path=outcomes_path,
        candidates_path=candidates_path,
        promotion_path=promotion_path,
        review_packet_path=review_packet_path,
        review_packet_json_path=review_packet_json_path,
    )


def load_learning_report(path: Path = CRYPTO_ATTRIBUTION_FILE) -> dict[str, Any]:
    return _load_json(path)


def load_upgrade_candidates(path: Path = CRYPTO_UPGRADE_CANDIDATES_FILE, *, tail: int | None = None) -> list[dict[str, Any]]:
    return _load_jsonl(path, tail=tail)


def load_promotion_report(path: Path = CRYPTO_PROMOTION_REPORT_FILE) -> dict[str, Any]:
    return _load_json(path)


def load_learning_review_packet(path: Path = CRYPTO_LEARNING_REVIEW_PACKET_JSON_FILE) -> dict[str, Any]:
    return _load_json(path)
