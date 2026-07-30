"""AH multi-factor scanner.

Emits four sidecar artifacts under ``reports/live/``:

* ``scan_universe.json`` — dynamic universe candidates (liquidity + theme tags).
* ``scan_limit_up_streak.json`` — A-share consecutive limit-up board.
* ``scan_volume_shrink_up.json`` — narrow-volume rise candidates (cross-market).
* ``scan_near_ath.json`` — near all-time-high candidates (cross-market).

Plus a top-level ``scan_summary.json`` that captures generation metadata.

Design tenets:

* Read-only with respect to the existing pipeline. The scanner never writes
  back into the live ``config/tech_universe_cn_hk.json`` file. It writes a
  sibling ``config/tech_universe_cn_hk.dynamic.json``; the CLI loader picks
  whichever the user has flagged on.
* Every market call is best-effort. If Futu is unreachable the scanner
  writes an empty report with a ``skipped_reason`` and exits 0 — it does NOT
  raise, because we don't want a scan failure to break a launchd cycle.
* Caps and pagination so an over-eager scan can't lock OpenD for too long.

The actual screening rules are intentionally conservative:

* 连板: today's close ≥ previous close × (1 + price_limit - epsilon), for N
  consecutive days. Price-limit thresholds are picked per market segment.
* 缩量上涨: today's close > yesterday's close AND today's volume <
  20-day average volume × ``volume_shrink_ratio`` (default 0.85).
* 接近历史新高: latest close ≥ ``ath_pct`` (default 0.95) × max(close, high)
  over the past ``ath_lookback_days`` (default ~3 trading years).

If you want different thresholds, override them via env vars
``MARKET_NEWS_AH_*``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScannerConfig:
    markets: tuple[str, ...] = ("HK", "SH", "SZ")
    top_n: int = 30
    volume_shrink_ratio: float = 0.85
    volume_lookback_days: int = 20
    limit_up_lookback_days: int = 7
    ath_pct: float = 0.95
    ath_lookback_days: int = 750  # ~ 3 trading years
    history_per_call_cap: int = 100  # cap on history_kline pulls per scan run
    universe_size_cap: int = 400  # cap candidates passed downstream

    @classmethod
    def from_env(cls) -> "ScannerConfig":
        def _f(name: str, default: float) -> float:
            raw = os.getenv(name)
            if raw is None or raw == "":
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        def _i(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        markets_raw = os.getenv("MARKET_NEWS_AH_SCANNER_MARKETS", "HK,SH,SZ")
        markets = tuple(
            part.strip().upper() for part in markets_raw.split(",") if part.strip()
        ) or ("HK", "SH", "SZ")
        return cls(
            markets=markets,
            top_n=_i("MARKET_NEWS_AH_SCANNER_TOP_N", 30),
            volume_shrink_ratio=_f("MARKET_NEWS_AH_VOLUME_SHRINK_RATIO", 0.85),
            volume_lookback_days=_i("MARKET_NEWS_AH_VOLUME_LOOKBACK_DAYS", 20),
            limit_up_lookback_days=_i("MARKET_NEWS_AH_LIMIT_UP_LOOKBACK_DAYS", 7),
            ath_pct=_f("MARKET_NEWS_AH_ATH_PCT", 0.95),
            ath_lookback_days=_i("MARKET_NEWS_AH_ATH_LOOKBACK_DAYS", 750),
            history_per_call_cap=_i("MARKET_NEWS_AH_HISTORY_CALL_CAP", 100),
            universe_size_cap=_i("MARKET_NEWS_AH_UNIVERSE_CAP", 400),
        )


# ---------------------------------------------------------------------------
# Candidate sourcing
# ---------------------------------------------------------------------------

# Plate codes per market. The skill exposes aliases like hk_chip/us_chip etc;
# the underlying plate codes are stable. We list them explicitly so the
# scanner does not depend on the alias table.

DEFAULT_PLATES: dict[str, list[str]] = {
    "HK": [
        # 恒生科技 + 半导体 / 电动车 / AI 主题
        "HK.800700",  # Hang Seng Tech
        "HK.BK1052",  # AI
        "HK.BK1910",  # Semiconductor
    ],
    "SH": [
        "SH.BK0490",  # 半导体
        "SH.BK1037",  # AI
    ],
    "SZ": [
        "SZ.BK0729",  # 半导体
        "SZ.BK1037",  # AI
    ],
    "US": [
        "US.BK1004",  # AI
        "US.BK1023",  # Semiconductor
    ],
}


def collect_candidates(markets: Iterable[str]) -> list[dict[str, Any]]:
    """Pull plate-constituent stocks for each market.

    Returns a list of ``{code, name, source_plate}`` dicts. Duplicates are
    merged by ``code``. Best-effort: silently skips plates we can't fetch.
    """

    from market_news.infrastructure import futu_client

    seen: dict[str, dict[str, Any]] = {}
    for market in markets:
        plates = DEFAULT_PLATES.get(market.upper(), [])
        for plate_code in plates:
            members = futu_client.get_plate_stock(plate_code) or []
            for entry in members:
                code = entry.get("code") or ""
                if not code:
                    continue
                if code not in seen:
                    seen[code] = {
                        "code": code,
                        "name": entry.get("name", ""),
                        "source_plates": [plate_code],
                    }
                else:
                    seen[code]["source_plates"].append(plate_code)
    return list(seen.values())


# ---------------------------------------------------------------------------
# Scan rules
# ---------------------------------------------------------------------------


def _price_limit_pct(code: str) -> float:
    """Per-market daily price limit. Used to detect 涨停 / 连板."""

    if code.startswith("SZ."):
        sub = code.split(".", 1)[1]
        # 创业板 30* / 300* and ChiNext: 20%
        if sub.startswith("30"):
            return 0.20
    if code.startswith("SH."):
        sub = code.split(".", 1)[1]
        # 科创板 688*: 20%
        if sub.startswith("688"):
            return 0.20
    if code.startswith(("SH.", "SZ.")):
        return 0.10
    if code.startswith("BJ."):
        return 0.30
    # HK / US: no daily limit
    return float("inf")


def detect_limit_up_streak(klines: list[dict[str, Any]], code: str) -> dict[str, Any] | None:
    """Returns ``{streak: int, last_close: float, limit_pct: float}`` if there
    is at least one limit-up day at the end of the series; otherwise ``None``.
    """

    if not klines or len(klines) < 2:
        return None
    limit_pct = _price_limit_pct(code)
    if limit_pct == float("inf"):
        return None
    # Klines are oldest → newest by convention from request_history_kline.
    streak = 0
    for i in range(len(klines) - 1, 0, -1):
        today = klines[i]
        yest = klines[i - 1]
        try:
            change = (today["close"] - yest["close"]) / yest["close"]
        except (KeyError, TypeError, ZeroDivisionError):
            break
        # Allow a small tolerance (0.5% under the nominal limit) to absorb
        # tick rounding.
        if change >= (limit_pct - 0.005):
            streak += 1
        else:
            break
    if streak == 0:
        return None
    return {
        "streak": streak,
        "last_close": klines[-1].get("close"),
        "last_time": klines[-1].get("time_key"),
        "limit_pct": limit_pct,
    }


def detect_volume_shrink_rise(
    klines: list[dict[str, Any]],
    *,
    lookback: int,
    shrink_ratio: float,
) -> dict[str, Any] | None:
    """Today close > yesterday close AND today vol < shrink_ratio × avg(vol over lookback)."""

    if not klines or len(klines) < lookback + 1:
        return None
    today = klines[-1]
    yest = klines[-2]
    history = klines[-(lookback + 1):-1]
    try:
        avg_vol = sum(bar.get("volume", 0) for bar in history) / max(len(history), 1)
    except (TypeError, ZeroDivisionError):
        return None
    if avg_vol <= 0:
        return None
    if today.get("close", 0) <= yest.get("close", 0):
        return None
    today_vol = today.get("volume", 0)
    if today_vol >= avg_vol * shrink_ratio:
        return None
    try:
        rise_pct = (today["close"] - yest["close"]) / yest["close"]
    except (KeyError, TypeError, ZeroDivisionError):
        return None
    return {
        "today_close": today.get("close"),
        "today_volume": today_vol,
        "avg_volume_lookback": avg_vol,
        "volume_ratio": today_vol / avg_vol if avg_vol else None,
        "rise_pct": rise_pct,
        "time": today.get("time_key"),
    }


def detect_near_ath(
    klines: list[dict[str, Any]],
    *,
    ath_pct: float,
) -> dict[str, Any] | None:
    if not klines or len(klines) < 30:
        return None
    last_close = klines[-1].get("close", 0)
    peak = max((bar.get("high", 0) or 0) for bar in klines)
    if peak <= 0 or last_close <= 0:
        return None
    distance_pct = (peak - last_close) / peak
    if last_close >= ath_pct * peak:
        return {
            "last_close": last_close,
            "lookback_peak": peak,
            "distance_pct": distance_pct,
            "last_time": klines[-1].get("time_key"),
        }
    return None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScanReport:
    generated_at: str
    config: dict[str, Any]
    candidate_count: int
    universe: list[dict[str, Any]] = field(default_factory=list)
    limit_up_streak: list[dict[str, Any]] = field(default_factory=list)
    volume_shrink_up: list[dict[str, Any]] = field(default_factory=list)
    near_ath: list[dict[str, Any]] = field(default_factory=list)
    skipped_reason: str | None = None
    errors: list[str] = field(default_factory=list)


def run_scan(config: ScannerConfig | None = None) -> ScanReport:
    cfg = config or ScannerConfig.from_env()
    now = datetime.now(timezone.utc).isoformat()
    report = ScanReport(
        generated_at=now,
        config={
            "markets": list(cfg.markets),
            "top_n": cfg.top_n,
            "volume_shrink_ratio": cfg.volume_shrink_ratio,
            "volume_lookback_days": cfg.volume_lookback_days,
            "limit_up_lookback_days": cfg.limit_up_lookback_days,
            "ath_pct": cfg.ath_pct,
            "ath_lookback_days": cfg.ath_lookback_days,
        },
        candidate_count=0,
    )

    from market_news.infrastructure import futu_client

    candidates = collect_candidates(cfg.markets)
    if not candidates:
        report.skipped_reason = "no-candidates"
        return report

    candidates = candidates[: cfg.universe_size_cap]
    report.candidate_count = len(candidates)

    # Snapshot pass to enrich universe with price / volume / market_val.
    snaps = {s["code"]: s for s in futu_client.get_snapshot([c["code"] for c in candidates])}
    enriched_universe: list[dict[str, Any]] = []
    for cand in candidates:
        snap = snaps.get(cand["code"], {})
        enriched_universe.append(
            {
                "code": cand["code"],
                "name": cand.get("name") or snap.get("name", ""),
                "source_plates": cand.get("source_plates", []),
                "last_price": snap.get("last_price"),
                "market_val": snap.get("market_val"),
                "turnover": snap.get("turnover"),
                "turnover_rate": snap.get("turnover_rate"),
                "change_rate": snap.get("change_rate"),
                "pe_ratio": snap.get("pe_ratio"),
            }
        )
    enriched_universe.sort(key=lambda r: (r.get("market_val") or 0), reverse=True)
    report.universe = enriched_universe

    # K-line pulls — capped to history_per_call_cap to protect OpenD quota.
    today = datetime.now(timezone.utc).date()
    short_start = (today - timedelta(days=cfg.limit_up_lookback_days + 10)).isoformat()
    short_end = today.isoformat()
    long_start = (today - timedelta(days=cfg.ath_lookback_days)).isoformat()
    long_end = today.isoformat()

    streak_hits: list[dict[str, Any]] = []
    volume_hits: list[dict[str, Any]] = []
    ath_hits: list[dict[str, Any]] = []

    for idx, cand in enumerate(enriched_universe[: cfg.history_per_call_cap]):
        code = cand["code"]
        try:
            short_klines = futu_client.get_history_kline(
                code,
                start=short_start,
                end=short_end,
                ktype="K_DAY",
            ) or []
        except Exception as exc:
            report.errors.append(f"short_klines:{code}:{exc}")
            short_klines = []

        # 连板 detection (A-share only)
        if code.startswith(("SH.", "SZ.", "BJ.")) and short_klines:
            streak = detect_limit_up_streak(short_klines, code)
            if streak:
                streak_hits.append({**cand, **streak})

        # 缩量上涨 — works for any market
        if short_klines and len(short_klines) >= cfg.volume_lookback_days + 1:
            vol_hit = detect_volume_shrink_rise(
                short_klines,
                lookback=cfg.volume_lookback_days,
                shrink_ratio=cfg.volume_shrink_ratio,
            )
            if vol_hit:
                volume_hits.append({**cand, **vol_hit})

        # 近历史新高 needs longer history; do a separate, less frequent pull.
        try:
            long_klines = futu_client.get_history_kline(
                code,
                start=long_start,
                end=long_end,
                ktype="K_DAY",
                max_count=1000,
            ) or []
        except Exception as exc:
            report.errors.append(f"long_klines:{code}:{exc}")
            long_klines = []
        if long_klines:
            ath_hit = detect_near_ath(long_klines, ath_pct=cfg.ath_pct)
            if ath_hit:
                ath_hits.append({**cand, **ath_hit})

    # Sort + cap the boards by their respective merit metric.
    streak_hits.sort(key=lambda r: (r.get("streak") or 0), reverse=True)
    volume_hits.sort(key=lambda r: (r.get("rise_pct") or 0), reverse=True)
    ath_hits.sort(key=lambda r: -1 * (r.get("distance_pct") or 1.0))

    report.limit_up_streak = streak_hits[: cfg.top_n]
    report.volume_shrink_up = volume_hits[: cfg.top_n]
    report.near_ath = ath_hits[: cfg.top_n]
    return report


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------


def write_reports(report: ScanReport, *, reports_dir: Path) -> dict[str, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "universe": reports_dir / "scan_universe.json",
        "limit_up_streak": reports_dir / "scan_limit_up_streak.json",
        "volume_shrink_up": reports_dir / "scan_volume_shrink_up.json",
        "near_ath": reports_dir / "scan_near_ath.json",
        "summary": reports_dir / "scan_summary.json",
    }
    paths["universe"].write_text(
        json.dumps(
            {
                "generated_at": report.generated_at,
                "config": report.config,
                "skipped_reason": report.skipped_reason,
                "universe": report.universe,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    paths["limit_up_streak"].write_text(
        json.dumps(
            {
                "generated_at": report.generated_at,
                "items": report.limit_up_streak,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    paths["volume_shrink_up"].write_text(
        json.dumps(
            {
                "generated_at": report.generated_at,
                "items": report.volume_shrink_up,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    paths["near_ath"].write_text(
        json.dumps(
            {
                "generated_at": report.generated_at,
                "items": report.near_ath,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    paths["summary"].write_text(
        json.dumps(
            {
                "generated_at": report.generated_at,
                "config": report.config,
                "candidate_count": report.candidate_count,
                "universe_count": len(report.universe),
                "limit_up_streak_count": len(report.limit_up_streak),
                "volume_shrink_up_count": len(report.volume_shrink_up),
                "near_ath_count": len(report.near_ath),
                "errors": report.errors,
                "skipped_reason": report.skipped_reason,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return paths


def write_dynamic_universe(report: ScanReport, *, config_dir: Path) -> Path | None:
    """Persist a synthetic ``tech_universe_cn_hk.dynamic.json``.

    Shape mirrors the static file so ``AHShareTechFeatureBlock.from_files``
    can ingest it without changes. Themes are derived from the source plate
    aliases; missing/unknown plates fall back to a generic ``tech-ah`` tag.
    """

    if not report.universe:
        return None
    config_dir.mkdir(parents=True, exist_ok=True)
    dyn = []
    for item in report.universe:
        code = item.get("code", "")
        # Translate Futu code → Yahoo-style symbol for the pipeline.
        symbol = _futu_to_yahoo(code)
        if not symbol:
            continue
        market = _market_label(code)
        themes = _themes_from_plates(item.get("source_plates", []))
        dyn.append(
            {
                "symbol": symbol,
                "market": market,
                "name": item.get("name", ""),
                "aliases": [symbol.lower(), code.lower()],
                "sectors": ["tech-ah-dynamic"],
                "themes": themes,
                "theme_weights": {t: 1.0 for t in themes},
                "liquidity_score": _liquidity_score(item),
                "tier": "dynamic",
            }
        )
    out_path = config_dir / "tech_universe_cn_hk.dynamic.json"
    out_path.write_text(json.dumps(dyn, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def _futu_to_yahoo(code: str) -> str | None:
    if "." not in code:
        return None
    market, body = code.split(".", 1)
    if market == "HK":
        # Futu pads to 5 digits; Yahoo HK uses 4-digit ticker without leading zeros stripped.
        return f"{body.lstrip('0').zfill(4)}.HK"
    if market in {"SH", "SZ"}:
        return f"{body}.{market}"
    if market == "US":
        return body
    return None


def _market_label(code: str) -> str:
    if code.startswith("HK."):
        return "HK"
    if code.startswith("SH."):
        return "CN-A"
    if code.startswith("SZ."):
        return "CN-A"
    if code.startswith("US."):
        return "US"
    return "OTHER"


def _themes_from_plates(plates: list[str]) -> list[str]:
    mapping = {
        "HK.800700": "hk-tech",
        "HK.BK1052": "ai",
        "HK.BK1910": "semiconductor",
        "SH.BK0490": "semiconductor",
        "SH.BK1037": "ai",
        "SZ.BK0729": "semiconductor",
        "SZ.BK1037": "ai",
        "US.BK1004": "ai",
        "US.BK1023": "semiconductor",
    }
    themes = []
    for p in plates:
        themes.append(mapping.get(p, "tech-ah"))
    if not themes:
        themes = ["tech-ah"]
    # dedupe preserving order
    seen: set[str] = set()
    unique = []
    for t in themes:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def _liquidity_score(item: dict[str, Any]) -> float:
    turnover = item.get("turnover") or 0.0
    market_val = item.get("market_val") or 0.0
    if market_val <= 0:
        return 0.0
    # Crude proxy: today's turnover relative to market cap, clamped to [0,1].
    ratio = min(1.0, turnover / market_val)
    return round(ratio, 4)
