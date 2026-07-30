from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Literal

import pandas as pd
import requests

from .crypto_ofim import (
    BINANCE_OFFICIAL_SPOT_PUBLIC_STANDARD_TAKER_RATE,
    FEATURES_FILE as SPOT_FEATURES_FILE,
    MAINNET_BASE_URL,
    _safe_float as _spot_safe_float,
)
from .crypto_perp import (
    DEFAULT_USDM_MAKER_FEE_RATE,
    DEFAULT_USDM_TAKER_FEE_RATE,
    FEATURES_FILE as PERP_FEATURES_FILE,
    FUTURES_MAINNET_BASE_URL,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime" / "crypto_research"
DATA_DIR = RUNTIME_DIR / "data"
DATA_FILE = DATA_DIR / "crypto_replay_rows.jsonl"
MANIFEST_FILE = DATA_DIR / "manifest.json"

Sleeve = Literal["spot", "perp", "both"]
SplitName = Literal["train", "validation", "locked_test", "all"]


@dataclass(frozen=True)
class CryptoBacktestProfile:
    name: str = "default"
    entry_threshold: float = 0.24
    exit_threshold: float = 0.10
    max_holding_bars: int = 20
    confirm_cycles: int = 1
    max_position_weight: float = 0.25
    max_gross_exposure: float = 0.50
    max_positions: int = 1
    min_order_notional: float = 20.0
    min_trade_interval_bars: int = 1
    spot_fee_rate: float = BINANCE_OFFICIAL_SPOT_PUBLIC_STANDARD_TAKER_RATE
    perp_taker_fee_rate: float = DEFAULT_USDM_TAKER_FEE_RATE
    perp_maker_fee_rate: float = DEFAULT_USDM_MAKER_FEE_RATE
    slippage_bps: float = 5.0
    order_style: str = "maker_limit"
    edge_bps_per_score: float = 60.0
    cost_buffer_bps: float = 6.0
    funding_interval_seconds: int = 28_800
    max_drawdown_limit: float = 0.08
    max_cost_drag_ratio: float = 0.60
    min_trades: int = 20


DEFAULT_PROFILES: dict[str, CryptoBacktestProfile] = {
    "default": CryptoBacktestProfile(),
    "spot_conservative": CryptoBacktestProfile(
        name="spot_conservative",
        entry_threshold=0.32,
        exit_threshold=0.12,
        max_position_weight=0.20,
        max_gross_exposure=0.40,
        slippage_bps=5.0,
    ),
    "spot_active": CryptoBacktestProfile(
        name="spot_active",
        entry_threshold=0.20,
        exit_threshold=0.06,
        max_holding_bars=12,
        max_position_weight=0.35,
        max_gross_exposure=0.60,
        slippage_bps=5.0,
    ),
    "perp_conservative": CryptoBacktestProfile(
        name="perp_conservative",
        entry_threshold=0.36,
        exit_threshold=0.14,
        max_position_weight=0.10,
        max_gross_exposure=0.12,
        order_style="maker_limit",
        slippage_bps=2.0,
    ),
    "perp_active": CryptoBacktestProfile(
        name="perp_active",
        entry_threshold=0.24,
        exit_threshold=0.10,
        max_holding_bars=16,
        max_position_weight=0.18,
        max_gross_exposure=0.24,
        order_style="maker_limit",
        slippage_bps=2.0,
    ),
}


@dataclass(frozen=True)
class CryptoBacktestResult:
    sleeve: str
    split: str
    profile: dict[str, Any]
    start_ts: str | None
    end_ts: str | None
    initial_equity: float
    final_equity: float
    net_pnl: float
    gross_pnl: float
    total_return: float
    max_drawdown: float
    trade_count: int
    fees_paid: float
    slippage_paid: float
    funding_paid: float
    passed_gate: bool
    failure_reasons: list[str]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n")
            count += 1
    return count


def _write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n")
            count += 1
    tmp.replace(path)
    return count


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def profile_from_name(name: str | None) -> CryptoBacktestProfile:
    if not name:
        return DEFAULT_PROFILES["default"]
    if name in DEFAULT_PROFILES:
        return DEFAULT_PROFILES[name]
    path = RUNTIME_DIR / "profiles" / f"{name}.json"
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        base = asdict(DEFAULT_PROFILES["default"])
        base.update({key: value for key, value in payload.items() if key in base})
        base["name"] = name
        return CryptoBacktestProfile(**base)
    raise ValueError(f"Unknown crypto backtest profile: {name}")


def _coerce_feature_rows(path: Path, *, sleeve: str, source: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in _load_jsonl(path):
        ts = raw.get("ts")
        symbol = str(raw.get("symbol") or "").upper()
        price = _safe_float(raw.get("last_price"))
        score = _safe_float(raw.get("score"))
        if not ts or not symbol or price <= 0:
            continue
        rows.append(
            {
                "ts": ts,
                "sleeve": sleeve,
                "symbol": symbol,
                "price": price,
                "score": score,
                "eligible": bool(raw.get("eligible", True)),
                "reason": raw.get("reason", ""),
                "spread_bps": _safe_float(raw.get("spread_bps")),
                "funding_rate": _safe_float(raw.get("funding_rate")),
                "source": source,
            }
        )
    return rows


def _fetch_klines(base_url: str, path: str, symbol: str, *, start_ms: int, end_ms: int, interval: str) -> list[list[Any]]:
    rows: list[list[Any]] = []
    cursor = int(start_ms)
    while cursor < end_ms:
        response = requests.get(
            f"{base_url}{path}",
            params={"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": 1000},
            timeout=15,
        )
        response.raise_for_status()
        batch = response.json()
        if not batch:
            break
        rows.extend(batch)
        last_open = int(batch[-1][0])
        next_cursor = last_open + 60_000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
    return rows


def _score_public_klines(rows: list[list[Any]], *, symbol: str, sleeve: str) -> list[dict[str, Any]]:
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    frame["ts"] = pd.to_datetime(frame[0], unit="ms", utc=True)
    frame["price"] = pd.to_numeric(frame[4], errors="coerce")
    frame["volume"] = pd.to_numeric(frame[5], errors="coerce").fillna(0.0)
    frame = frame.dropna(subset=["price"]).sort_values("ts")
    if frame.empty:
        return []
    ret_3 = frame["price"].pct_change(3).fillna(0.0)
    ret_10 = frame["price"].pct_change(10).fillna(0.0)
    ret_30 = frame["price"].pct_change(30).fillna(0.0)
    vol = frame["volume"]
    vol_base = vol.rolling(30, min_periods=3).mean().replace(0, pd.NA)
    vol_accel = (vol / vol_base).fillna(1.0).clip(0, 3)
    raw_score = (ret_3 * 35.0 + ret_10 * 25.0 + ret_30 * 15.0 + (vol_accel - 1.0) * 0.04).clip(-1.0, 1.0)
    if sleeve == "spot":
        raw_score = raw_score.clip(lower=0.0)
    out: list[dict[str, Any]] = []
    for idx, row in frame.iterrows():
        out.append(
            {
                "ts": row["ts"].isoformat(),
                "sleeve": sleeve,
                "symbol": symbol,
                "price": round(float(row["price"]), 12),
                "score": round(float(raw_score.loc[idx]), 6),
                "eligible": True,
                "reason": "public_kline_proxy",
                "spread_bps": 0.0,
                "funding_rate": 0.0,
                "source": "binance_public_1m_proxy",
            }
        )
    return out


def _gap_stats(frame: pd.DataFrame) -> list[dict[str, Any]]:
    stats: list[dict[str, Any]] = []
    if frame.empty:
        return stats
    for (sleeve, symbol), group in frame.sort_values("ts").groupby(["sleeve", "symbol"]):
        diffs = group["ts"].diff().dropna().dt.total_seconds()
        stats.append(
            {
                "sleeve": sleeve,
                "symbol": symbol,
                "rows": int(len(group)),
                "start_ts": group["ts"].min().isoformat(),
                "end_ts": group["ts"].max().isoformat(),
                "max_gap_seconds": round(float(diffs.max()), 3) if not diffs.empty else 0.0,
                "gap_count_gt_120s": int((diffs > 120).sum()) if not diffs.empty else 0,
            }
        )
    return stats


def build_crypto_backtest_dataset(
    *,
    symbols: Iterable[str] | None = None,
    days: int = 14,
    interval: str = "1m",
    include_public: bool = True,
    include_local: bool = True,
    data_file: Path = DATA_FILE,
    manifest_file: Path = MANIFEST_FILE,
) -> dict[str, Any]:
    symbols = tuple(dict.fromkeys(symbol.upper().replace("/", "") for symbol in (symbols or ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"))))
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    sources: list[str] = []
    if include_local:
        spot_rows = _coerce_feature_rows(SPOT_FEATURES_FILE, sleeve="spot", source="local_spot_features")
        perp_rows = _coerce_feature_rows(PERP_FEATURES_FILE, sleeve="perp", source="local_perp_features")
        rows.extend(row for row in [*spot_rows, *perp_rows] if row["symbol"] in symbols)
        sources.extend(["local_spot_features", "local_perp_features"])
    if include_public:
        end = datetime.now(UTC)
        start = end - timedelta(days=max(1, int(days)))
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        for symbol in symbols:
            try:
                spot_klines = _fetch_klines(MAINNET_BASE_URL, "/api/v3/klines", symbol, start_ms=start_ms, end_ms=end_ms, interval=interval)
                rows.extend(_score_public_klines(spot_klines, symbol=symbol, sleeve="spot"))
            except Exception as exc:
                errors.append(f"spot:{symbol}:{type(exc).__name__}:{exc}")
            try:
                perp_klines = _fetch_klines(FUTURES_MAINNET_BASE_URL, "/fapi/v1/klines", symbol, start_ms=start_ms, end_ms=end_ms, interval=interval)
                rows.extend(_score_public_klines(perp_klines, symbol=symbol, sleeve="perp"))
            except Exception as exc:
                errors.append(f"perp:{symbol}:{type(exc).__name__}:{exc}")
        sources.append("binance_public_1m_proxy")

    frame = _rows_to_frame(rows)
    normalized = frame.sort_values(["ts", "sleeve", "symbol"]).to_dict("records") if not frame.empty else []
    _write_jsonl_atomic(data_file, normalized)
    manifest = {
        "generated_at": _utc_now(),
        "data_file": str(data_file),
        "sha256": _file_sha256(data_file),
        "symbols": list(symbols),
        "interval": interval,
        "include_public": include_public,
        "include_local": include_local,
        "sources": sorted(set(sources)),
        "row_count": len(normalized),
        "start_ts": frame["ts"].min().isoformat() if not frame.empty else None,
        "end_ts": frame["ts"].max().isoformat() if not frame.empty else None,
        "gap_stats": _gap_stats(frame),
        "errors": errors[:50],
    }
    _write_json_atomic(manifest_file, manifest)
    return manifest


def _rows_to_frame(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(list(rows))
    if frame.empty:
        return pd.DataFrame(columns=["ts", "sleeve", "symbol", "price", "score", "eligible", "spread_bps", "funding_rate", "source", "reason"])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True, errors="coerce")
    frame["sleeve"] = frame["sleeve"].astype(str).str.lower()
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    for column in ("price", "score", "spread_bps", "funding_rate"):
        frame[column] = pd.to_numeric(frame.get(column, 0.0), errors="coerce").fillna(0.0)
    if "eligible" not in frame.columns:
        frame["eligible"] = True
    frame["eligible"] = frame["eligible"].astype(bool)
    if "source" not in frame.columns:
        frame["source"] = ""
    if "reason" not in frame.columns:
        frame["reason"] = ""
    frame["source"] = frame["source"].astype(str)
    frame["reason"] = frame["reason"].astype(str)
    frame = frame.dropna(subset=["ts"])
    frame = frame[frame["price"] > 0]
    frame["ts"] = frame["ts"].dt.floor("60s")
    frame = frame.sort_values(["ts", "sleeve", "symbol"]).drop_duplicates(["ts", "sleeve", "symbol"], keep="last")
    return frame.sort_values(["ts", "sleeve", "symbol"]).reset_index(drop=True)


def load_crypto_replay_frame(data_file: Path = DATA_FILE) -> pd.DataFrame:
    return _rows_to_frame(_load_jsonl(data_file))


def split_time_series(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    frame = _rows_to_frame(frame.to_dict("records"))
    if frame.empty:
        return {"train": frame.copy(), "validation": frame.copy(), "locked_test": frame.copy()}
    unique_ts = sorted(frame["ts"].dropna().unique())
    count = len(unique_ts)
    train_end = max(1, int(count * 0.60))
    validation_end = max(train_end + 1, int(count * 0.80)) if count >= 3 else count
    train_ts = set(unique_ts[:train_end])
    validation_ts = set(unique_ts[train_end:validation_end])
    test_ts = set(unique_ts[validation_end:])
    return {
        "train": frame[frame["ts"].isin(train_ts)].copy(),
        "validation": frame[frame["ts"].isin(validation_ts)].copy(),
        "locked_test": frame[frame["ts"].isin(test_ts)].copy(),
    }


def _mark_to_market(cash: float, positions: dict[str, float], prices: dict[str, float]) -> float:
    return cash + sum(qty * prices.get(symbol, 0.0) for symbol, qty in positions.items())


def _max_drawdown(equity_curve: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, equity / peak - 1.0)
    return abs(worst)


def _targets_for_group(
    group: pd.DataFrame,
    *,
    sleeve: str,
    profile: CryptoBacktestProfile,
    positions: dict[str, float],
    holding_bars: dict[str, int],
) -> dict[str, float]:
    rows = group.sort_values("score", key=lambda item: item.abs() if sleeve == "perp" else item, ascending=False)
    targets: dict[str, float] = {}
    candidates: list[tuple[str, float]] = []
    for row in rows.itertuples(index=False):
        symbol = str(row.symbol)
        score = float(row.score)
        eligible = bool(row.eligible)
        if sleeve == "spot":
            if score >= profile.entry_threshold and eligible:
                candidates.append((symbol, score))
        else:
            if abs(score) >= profile.entry_threshold and eligible:
                candidates.append((symbol, score))
    for symbol, score in candidates[: max(1, profile.max_positions)]:
        if sleeve == "spot":
            targets[symbol] = min(profile.max_position_weight, profile.max_gross_exposure)
        else:
            targets[symbol] = math.copysign(min(profile.max_position_weight, profile.max_gross_exposure), score)

    for symbol, qty in positions.items():
        if abs(qty) <= 0 or symbol in targets:
            continue
        row = group[group["symbol"] == symbol]
        score = float(row["score"].iloc[-1]) if not row.empty else 0.0
        if holding_bars.get(symbol, 0) >= profile.max_holding_bars:
            continue
        if sleeve == "spot" and score >= profile.exit_threshold:
            targets[symbol] = min(profile.max_position_weight, profile.max_gross_exposure)
        elif sleeve == "perp" and abs(score) >= profile.exit_threshold and math.copysign(1.0, score or qty) == math.copysign(1.0, qty):
            targets[symbol] = math.copysign(min(profile.max_position_weight, profile.max_gross_exposure), qty)
    return targets


def _paid_costs(fees_paid: float, slippage_paid: float, funding_paid: float) -> float:
    return max(0.0, fees_paid) + max(0.0, slippage_paid) + max(0.0, funding_paid)


def _cost_drag_ratio(gross_pnl: float, fees_paid: float, slippage_paid: float, funding_paid: float) -> float:
    paid_costs = _paid_costs(fees_paid, slippage_paid, funding_paid)
    if gross_pnl <= 0:
        return float("inf") if paid_costs > 0 else 0.0
    return paid_costs / gross_pnl


def _gate_failure_reasons(
    *,
    profile: CryptoBacktestProfile,
    net_pnl: float,
    gross_pnl: float,
    max_drawdown: float,
    trade_count: int,
    fees_paid: float,
    slippage_paid: float,
    funding_paid: float,
) -> list[str]:
    failures: list[str] = []
    if net_pnl <= 0:
        failures.append("net_pnl_not_positive")
    if max_drawdown > profile.max_drawdown_limit:
        failures.append("max_drawdown_over_limit")
    if trade_count < profile.min_trades:
        failures.append("trade_count_below_minimum")
    max_cost_drag_ratio = max(0.0, float(profile.max_cost_drag_ratio))
    if net_pnl > 0 and _cost_drag_ratio(gross_pnl, fees_paid, slippage_paid, funding_paid) > max_cost_drag_ratio:
        failures.append("cost_drag_over_limit")
    return failures


def _passes_cost_gate(row: Any, *, sleeve: str, profile: CryptoBacktestProfile) -> bool:
    if sleeve == "spot":
        fee_rate = profile.spot_fee_rate
        slippage_bps = profile.slippage_bps * 2.0
    else:
        fee_rate = profile.perp_maker_fee_rate if profile.order_style == "maker_limit" else profile.perp_taker_fee_rate
        slippage_bps = 0.0 if profile.order_style == "maker_limit" else profile.slippage_bps * 2.0
    expected_edge_bps = abs(float(row.score)) * profile.edge_bps_per_score
    required_edge_bps = fee_rate * 2.0 * 10_000.0 + slippage_bps + max(0.0, float(row.spread_bps)) + profile.cost_buffer_bps
    return expected_edge_bps >= required_edge_bps


def run_crypto_backtest(
    frame: pd.DataFrame | None = None,
    *,
    sleeve: Sleeve = "both",
    profile: CryptoBacktestProfile | str | None = None,
    split: SplitName = "all",
    initial_equity: float = 10_000.0,
    data_file: Path = DATA_FILE,
) -> CryptoBacktestResult | dict[str, CryptoBacktestResult]:
    profile_obj = profile_from_name(profile) if isinstance(profile, str) or profile is None else profile
    full_frame = load_crypto_replay_frame(data_file) if frame is None else _rows_to_frame(frame.to_dict("records"))
    if split != "all":
        full_frame = split_time_series(full_frame)[split]
    if sleeve == "both":
        per_sleeve: dict[str, CryptoBacktestResult] = {}
        for one in ("spot", "perp"):
            per_sleeve[one] = _run_single_sleeve(
                full_frame[full_frame["sleeve"] == one].copy(),
                sleeve=one,
                profile=profile_obj,
                split=split,
                initial_equity=initial_equity / 2.0,
            )
        combined = _combine_results(per_sleeve, split=split, profile=profile_obj, initial_equity=initial_equity)
        per_sleeve["combined"] = combined
        return per_sleeve
    return _run_single_sleeve(full_frame[full_frame["sleeve"] == sleeve].copy(), sleeve=sleeve, profile=profile_obj, split=split, initial_equity=initial_equity)


def _run_single_sleeve(
    frame: pd.DataFrame,
    *,
    sleeve: str,
    profile: CryptoBacktestProfile,
    split: str,
    initial_equity: float,
) -> CryptoBacktestResult:
    if frame.empty:
        return _result(
            sleeve=sleeve,
            split=split,
            profile=profile,
            start_ts=None,
            end_ts=None,
            initial_equity=initial_equity,
            final_equity=initial_equity,
            gross_pnl=0.0,
            fees_paid=0.0,
            slippage_paid=0.0,
            funding_paid=0.0,
            trade_count=0,
            equity_curve=[initial_equity],
        )
    cash = float(initial_equity)
    positions: dict[str, float] = {}
    holding_bars: dict[str, int] = {}
    last_trade_bar: dict[str, int] = {}
    fees_paid = 0.0
    slippage_paid = 0.0
    funding_paid = 0.0
    trade_count = 0
    equity_curve: list[float] = []
    prev_ts: pd.Timestamp | None = None
    start_ts = frame["ts"].min().isoformat()
    end_ts = frame["ts"].max().isoformat()

    grouped = list(frame.sort_values("ts").groupby("ts"))
    for bar_index, (ts, group) in enumerate(grouped):
        prices = {str(row.symbol): float(row.price) for row in group.itertuples(index=False)}
        if sleeve == "perp" and prev_ts is not None:
            elapsed = max(0.0, (pd.Timestamp(ts) - prev_ts).total_seconds())
            for row in group.itertuples(index=False):
                qty = positions.get(str(row.symbol), 0.0)
                if abs(qty) <= 0:
                    continue
                notional = qty * float(row.price)
                funding = notional * float(row.funding_rate) * elapsed / max(1, profile.funding_interval_seconds)
                cash -= funding
                funding_paid += funding
        prev_ts = pd.Timestamp(ts)

        equity = _mark_to_market(cash, positions, prices)
        targets = _targets_for_group(group, sleeve=sleeve, profile=profile, positions=positions, holding_bars=holding_bars)
        for row in group.itertuples(index=False):
            symbol = str(row.symbol)
            price = float(row.price)
            target_weight = targets.get(symbol, 0.0)
            if sleeve == "spot":
                target_weight = max(0.0, target_weight)
            target_qty = target_weight * equity / price if price > 0 else 0.0
            current_qty = positions.get(symbol, 0.0)
            delta_qty = target_qty - current_qty
            notional = abs(delta_qty) * price
            if notional < profile.min_order_notional:
                continue
            if bar_index - last_trade_bar.get(symbol, -10**9) < profile.min_trade_interval_bars:
                continue
            if abs(delta_qty) > abs(current_qty) and not _passes_cost_gate(row, sleeve=sleeve, profile=profile):
                continue
            fee_rate = profile.spot_fee_rate if sleeve == "spot" else (profile.perp_maker_fee_rate if profile.order_style == "maker_limit" else profile.perp_taker_fee_rate)
            fee = notional * fee_rate
            slip_bps = 0.0 if sleeve == "perp" and profile.order_style == "maker_limit" else profile.slippage_bps
            slippage = notional * slip_bps / 10_000.0
            execution_price = price * (1.0 + slip_bps / 10_000.0 if delta_qty > 0 else 1.0 - slip_bps / 10_000.0)
            cash -= delta_qty * execution_price
            cash -= fee
            positions[symbol] = current_qty + delta_qty
            if abs(positions[symbol]) < 1e-12:
                positions.pop(symbol, None)
                holding_bars.pop(symbol, None)
            else:
                holding_bars[symbol] = 0 if abs(current_qty) < 1e-12 or math.copysign(1.0, current_qty) != math.copysign(1.0, positions[symbol]) else holding_bars.get(symbol, 0)
            fees_paid += fee
            slippage_paid += slippage
            trade_count += 1
            last_trade_bar[symbol] = bar_index
        for symbol in list(positions):
            holding_bars[symbol] = holding_bars.get(symbol, 0) + 1
        equity_curve.append(_mark_to_market(cash, positions, prices))

    last_prices = {str(row.symbol): float(row.price) for row in grouped[-1][1].itertuples(index=False)}
    final_equity = _mark_to_market(cash, positions, last_prices)
    equity_curve.append(final_equity)
    gross_pnl = final_equity - initial_equity + fees_paid + slippage_paid + funding_paid
    return _result(
        sleeve=sleeve,
        split=split,
        profile=profile,
        start_ts=start_ts,
        end_ts=end_ts,
        initial_equity=initial_equity,
        final_equity=final_equity,
        gross_pnl=gross_pnl,
        fees_paid=fees_paid,
        slippage_paid=slippage_paid,
        funding_paid=funding_paid,
        trade_count=trade_count,
        equity_curve=equity_curve,
    )


def _result(
    *,
    sleeve: str,
    split: str,
    profile: CryptoBacktestProfile,
    start_ts: str | None,
    end_ts: str | None,
    initial_equity: float,
    final_equity: float,
    gross_pnl: float,
    fees_paid: float,
    slippage_paid: float,
    funding_paid: float,
    trade_count: int,
    equity_curve: list[float],
) -> CryptoBacktestResult:
    net_pnl = final_equity - initial_equity
    max_dd = _max_drawdown(equity_curve)
    failures = _gate_failure_reasons(
        profile=profile,
        net_pnl=net_pnl,
        gross_pnl=gross_pnl,
        max_drawdown=max_dd,
        trade_count=trade_count,
        fees_paid=fees_paid,
        slippage_paid=slippage_paid,
        funding_paid=funding_paid,
    )
    return CryptoBacktestResult(
        sleeve=sleeve,
        split=split,
        profile=asdict(profile),
        start_ts=start_ts,
        end_ts=end_ts,
        initial_equity=round(initial_equity, 8),
        final_equity=round(final_equity, 8),
        net_pnl=round(net_pnl, 8),
        gross_pnl=round(gross_pnl, 8),
        total_return=round(net_pnl / initial_equity if initial_equity > 0 else 0.0, 8),
        max_drawdown=round(max_dd, 8),
        trade_count=int(trade_count),
        fees_paid=round(fees_paid, 8),
        slippage_paid=round(slippage_paid, 8),
        funding_paid=round(funding_paid, 8),
        passed_gate=not failures,
        failure_reasons=failures,
    )


def _combine_results(
    per_sleeve: dict[str, CryptoBacktestResult],
    *,
    split: str,
    profile: CryptoBacktestProfile,
    initial_equity: float,
) -> CryptoBacktestResult:
    final_equity = sum(item.final_equity for item in per_sleeve.values())
    fees = sum(item.fees_paid for item in per_sleeve.values())
    slippage = sum(item.slippage_paid for item in per_sleeve.values())
    funding = sum(item.funding_paid for item in per_sleeve.values())
    gross = sum(item.gross_pnl for item in per_sleeve.values())
    trade_count = sum(item.trade_count for item in per_sleeve.values())
    max_dd = max((item.max_drawdown for item in per_sleeve.values()), default=0.0)
    start_ts = min((item.start_ts for item in per_sleeve.values() if item.start_ts), default=None)
    end_ts = max((item.end_ts for item in per_sleeve.values() if item.end_ts), default=None)
    result = _result(
        sleeve="combined",
        split=split,
        profile=profile,
        start_ts=start_ts,
        end_ts=end_ts,
        initial_equity=initial_equity,
        final_equity=final_equity,
        gross_pnl=gross,
        fees_paid=fees,
        slippage_paid=slippage,
        funding_paid=funding,
        trade_count=trade_count,
        equity_curve=[initial_equity, final_equity],
    )
    failures = _gate_failure_reasons(
        profile=profile,
        net_pnl=result.net_pnl,
        gross_pnl=gross,
        max_drawdown=max_dd,
        trade_count=trade_count,
        fees_paid=fees,
        slippage_paid=slippage,
        funding_paid=funding,
    )
    return replace(result, max_drawdown=round(max_dd, 8), passed_gate=not failures, failure_reasons=failures)


def result_to_dict(result: CryptoBacktestResult | dict[str, CryptoBacktestResult]) -> dict[str, Any]:
    if isinstance(result, dict):
        return {key: asdict(value) for key, value in result.items()}
    return asdict(result)
