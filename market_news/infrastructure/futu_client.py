"""Thin, defensive Futu OpenAPI client used only by additive enrichment paths.

This module is **opt-in**. Nothing in the core news pipeline imports it unless
``MARKET_NEWS_FUTU_ENRICHMENT`` or the ah-scan command is invoked.

Design constraints:

* Every call is wrapped in a broad ``try / except`` and returns ``None`` on
  failure. The caller MUST treat ``None`` as "no data, skip this enrichment"
  and never crash the pipeline.
* No global state. Each call opens and closes its own context so a long-running
  notifier process does not accumulate OpenD sockets.
* Host/port are read from environment variables but default to the
  loopback OpenD address that the trade project also uses.
* All public functions return plain ``dict`` / ``list`` so the rest of the
  pipeline never needs to know about pandas or futu-api types.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
from typing import Any, Iterator


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Symbol normalization
# ---------------------------------------------------------------------------

# The news collector uses Yahoo-style tickers like "0700.HK", "688981.SH",
# "NVDA". Futu OpenAPI uses prefix-style codes like "HK.00700", "SH.688981",
# "US.NVDA". This converter is intentionally permissive — if it cannot map,
# it returns ``None`` so the caller skips that symbol.

_MARKET_PREFIXES = ("US.", "HK.", "SH.", "SZ.", "SG.", "CC.")


def normalize_to_futu_code(raw: str) -> str | None:
    """Convert a Yahoo/news-style ticker to Futu's ``MARKET.CODE`` form."""

    if not raw:
        return None
    token = str(raw).strip()
    if not token:
        return None
    upper = token.upper()
    # Already a Futu code.
    if any(upper.startswith(p) for p in _MARKET_PREFIXES):
        return upper
    # Yahoo HK / A-share suffix forms: "0700.HK", "688981.SH", "000001.SZ".
    if "." in token:
        body, _, suffix = token.partition(".")
        suffix_upper = suffix.upper()
        if suffix_upper in ("HK", "SH", "SZ"):
            # Pad HK codes to 5 digits if they look numeric.
            if suffix_upper == "HK" and body.isdigit():
                body = body.zfill(5)
            return f"{suffix_upper}.{body}"
    # Plain English ticker → assume US.
    if token.replace(".", "").isalnum() and token.replace(".", "").isalpha():
        return f"US.{upper}"
    return None


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FutuConfig:
    host: str = "127.0.0.1"
    port: int = 11111

    @classmethod
    def from_env(cls) -> "FutuConfig":
        return cls(
            host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
            port=int(os.getenv("FUTU_OPEND_PORT", "11111")),
        )


@contextmanager
def _quote_context(config: FutuConfig | None = None) -> Iterator[Any]:
    """Yield an OpenQuoteContext, closing it on exit. Yields ``None`` on import
    failure or any connection issue so callers can no-op gracefully.
    """

    try:
        import futu  # type: ignore
    except Exception as exc:  # pragma: no cover - skipped when SDK absent
        logger.debug("futu SDK not importable: %s", exc)
        yield None
        return

    cfg = config or FutuConfig.from_env()
    ctx = None
    try:
        ctx = futu.OpenQuoteContext(host=cfg.host, port=cfg.port)
    except Exception as exc:
        logger.warning("futu OpenQuoteContext init failed: %s", exc)
        yield None
        return
    try:
        yield ctx
    finally:
        try:
            ctx.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_capital_flow(code: str, *, period_type: str = "INTRADAY") -> dict[str, Any] | None:
    """Return capital flow summary for a single symbol or ``None`` on failure.

    ``period_type`` is one of futu's ``PeriodType`` enum names — INTRADAY /
    DAY / WEEK / MONTH. We default to INTRADAY so the news enrichment can show
    "money flowing in today" even when the alert fires mid-session.
    """

    futu_code = normalize_to_futu_code(code)
    if futu_code is None:
        return None
    with _quote_context() as ctx:
        if ctx is None:
            return None
        try:
            import futu as _f  # type: ignore

            period_enum = getattr(_f.PeriodType, period_type, _f.PeriodType.INTRADAY)
            ret, data = ctx.get_capital_flow(futu_code, period_type=period_enum)
            if ret != 0 or data is None or len(data) == 0:
                return None
            last = data.iloc[-1].to_dict()
            return {
                "code": futu_code,
                "period_type": period_type,
                "in_flow": float(last.get("in_flow", 0.0) or 0.0),
                "main_in_flow": float(last.get("main_in_flow", 0.0) or 0.0),
                "super_in_flow": float(last.get("super_in_flow", 0.0) or 0.0),
                "big_in_flow": float(last.get("big_in_flow", 0.0) or 0.0),
                "mid_in_flow": float(last.get("mid_in_flow", 0.0) or 0.0),
                "sml_in_flow": float(last.get("sml_in_flow", 0.0) or 0.0),
                "capital_flow_item_time": str(last.get("capital_flow_item_time", "")),
            }
        except Exception as exc:
            logger.debug("get_capital_flow failed for %s: %s", futu_code, exc)
            return None


def get_capital_distribution(code: str) -> dict[str, Any] | None:
    """Return last-tick capital distribution (super / big / mid / sml in & out)."""

    futu_code = normalize_to_futu_code(code)
    if futu_code is None:
        return None
    with _quote_context() as ctx:
        if ctx is None:
            return None
        try:
            ret, data = ctx.get_capital_distribution(futu_code)
            if ret != 0 or data is None or len(data) == 0:
                return None
            row = data.iloc[0].to_dict()
            return {
                "code": futu_code,
                "capital_in_super": float(row.get("capital_in_super", 0.0) or 0.0),
                "capital_in_big": float(row.get("capital_in_big", 0.0) or 0.0),
                "capital_in_mid": float(row.get("capital_in_mid", 0.0) or 0.0),
                "capital_in_small": float(row.get("capital_in_small", 0.0) or 0.0),
                "capital_out_super": float(row.get("capital_out_super", 0.0) or 0.0),
                "capital_out_big": float(row.get("capital_out_big", 0.0) or 0.0),
                "capital_out_mid": float(row.get("capital_out_mid", 0.0) or 0.0),
                "capital_out_small": float(row.get("capital_out_small", 0.0) or 0.0),
                "update_time": str(row.get("update_time", "")),
            }
        except Exception as exc:
            logger.debug("get_capital_distribution failed for %s: %s", futu_code, exc)
            return None


def get_snapshot(codes: list[str]) -> list[dict[str, Any]]:
    """Batch snapshot. Returns a list of normalized dicts; missing or invalid
    symbols are silently dropped.
    """

    futu_codes = [c for c in (normalize_to_futu_code(x) for x in codes) if c]
    if not futu_codes:
        return []
    with _quote_context() as ctx:
        if ctx is None:
            return []
        try:
            # Futu allows up to 400 codes per call.
            results: list[dict[str, Any]] = []
            for chunk_start in range(0, len(futu_codes), 400):
                chunk = futu_codes[chunk_start:chunk_start + 400]
                ret, data = ctx.get_market_snapshot(chunk)
                if ret != 0 or data is None:
                    continue
                for _, row in data.iterrows():
                    item = row.to_dict()
                    results.append(
                        {
                            "code": str(item.get("code", "")),
                            "name": str(item.get("name", "")),
                            "last_price": float(item.get("last_price", 0.0) or 0.0),
                            "open_price": float(item.get("open_price", 0.0) or 0.0),
                            "high_price": float(item.get("high_price", 0.0) or 0.0),
                            "low_price": float(item.get("low_price", 0.0) or 0.0),
                            "prev_close_price": float(item.get("prev_close_price", 0.0) or 0.0),
                            "volume": int(item.get("volume", 0) or 0),
                            "turnover": float(item.get("turnover", 0.0) or 0.0),
                            "turnover_rate": float(item.get("turnover_rate", 0.0) or 0.0),
                            "change_rate": float(item.get("change_rate", 0.0) or 0.0),
                            "amplitude": float(item.get("amplitude", 0.0) or 0.0),
                            "market_val": float(item.get("total_market_val", 0.0) or 0.0),
                            "pe_ratio": float(item.get("pe_ratio", 0.0) or 0.0),
                            "pb_ratio": float(item.get("pb_ratio", 0.0) or 0.0),
                            # Price-limit fields are A-share specific.
                            "price_spread": float(item.get("price_spread", 0.0) or 0.0),
                            "lot_size": int(item.get("lot_size", 0) or 0),
                            "update_time": str(item.get("update_time", "")),
                        }
                    )
            return results
        except Exception as exc:
            logger.debug("get_snapshot failed: %s", exc)
            return []


def get_kline(
    code: str,
    *,
    num: int = 30,
    ktype: str = "K_DAY",
    autype: str = "QFQ",
) -> list[dict[str, Any]] | None:
    """Pull recent K-line bars. ``ktype`` is the futu KLType enum name."""

    futu_code = normalize_to_futu_code(code)
    if futu_code is None:
        return None
    with _quote_context() as ctx:
        if ctx is None:
            return None
        try:
            import futu as _f  # type: ignore

            kl_type = getattr(_f.KLType, ktype, _f.KLType.K_DAY)
            au_type = getattr(_f.AuType, autype, _f.AuType.QFQ)
            ret, data = ctx.get_cur_kline(futu_code, num=num, ktype=kl_type, autype=au_type)
            if ret != 0 or data is None or len(data) == 0:
                return None
            rows = []
            for _, row in data.iterrows():
                rows.append(
                    {
                        "time_key": str(row.get("time_key", "")),
                        "open": float(row.get("open", 0.0) or 0.0),
                        "close": float(row.get("close", 0.0) or 0.0),
                        "high": float(row.get("high", 0.0) or 0.0),
                        "low": float(row.get("low", 0.0) or 0.0),
                        "volume": int(row.get("volume", 0) or 0),
                        "turnover": float(row.get("turnover", 0.0) or 0.0),
                        "change_rate": float(row.get("change_rate", 0.0) or 0.0),
                    }
                )
            return rows
        except Exception as exc:
            logger.debug("get_kline failed for %s: %s", futu_code, exc)
            return None


def get_history_kline(
    code: str,
    *,
    start: str,
    end: str,
    ktype: str = "K_DAY",
    autype: str = "QFQ",
    max_count: int = 1000,
) -> list[dict[str, Any]] | None:
    """Pull historical K-line for an explicit date window. Honours OpenD
    quota; returns ``None`` on quota exhaustion.
    """

    futu_code = normalize_to_futu_code(code)
    if futu_code is None:
        return None
    with _quote_context() as ctx:
        if ctx is None:
            return None
        try:
            import futu as _f  # type: ignore

            kl_type = getattr(_f.KLType, ktype, _f.KLType.K_DAY)
            au_type = getattr(_f.AuType, autype, _f.AuType.QFQ)
            ret, data, _page_key = ctx.request_history_kline(
                futu_code,
                start=start,
                end=end,
                ktype=kl_type,
                autype=au_type,
                max_count=max_count,
            )
            if ret != 0 or data is None or len(data) == 0:
                return None
            return [
                {
                    "time_key": str(row.get("time_key", "")),
                    "open": float(row.get("open", 0.0) or 0.0),
                    "close": float(row.get("close", 0.0) or 0.0),
                    "high": float(row.get("high", 0.0) or 0.0),
                    "low": float(row.get("low", 0.0) or 0.0),
                    "volume": int(row.get("volume", 0) or 0),
                    "turnover": float(row.get("turnover", 0.0) or 0.0),
                    "change_rate": float(row.get("change_rate", 0.0) or 0.0),
                }
                for _, row in data.iterrows()
            ]
        except Exception as exc:
            logger.debug("get_history_kline failed for %s: %s", futu_code, exc)
            return None


def stock_filter(
    *,
    market: str,
    min_market_cap: float | None = None,
    max_market_cap: float | None = None,
    min_pe: float | None = None,
    max_pe: float | None = None,
    min_turnover_rate: float | None = None,
    max_turnover_rate: float | None = None,
    min_change_rate: float | None = None,
    max_change_rate: float | None = None,
    min_volume: int | None = None,
    sort_field: str = "market_val",
    ascend: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]] | None:
    """Wrap futu's ``get_stock_filter``. Returns ``None`` on any failure."""

    try:
        import futu as _f  # type: ignore
    except Exception:
        return None

    market_map = {
        "HK": _f.Market.HK,
        "US": _f.Market.US,
        "SH": _f.Market.SH,
        "SZ": _f.Market.SZ,
    }
    if market not in market_map:
        return None

    filters: list[Any] = []
    try:
        if min_market_cap is not None or max_market_cap is not None:
            filt = _f.SimpleFilter()
            filt.filter_min = (min_market_cap or 0) * 1_0000_0000  # 亿 → 元
            filt.filter_max = (max_market_cap or 1e18) * 1_0000_0000
            filt.stock_field = _f.StockField.MARKET_VAL
            filt.is_no_filter = False
            filters.append(filt)
        if min_pe is not None or max_pe is not None:
            filt = _f.SimpleFilter()
            filt.filter_min = min_pe if min_pe is not None else -1e9
            filt.filter_max = max_pe if max_pe is not None else 1e9
            filt.stock_field = _f.StockField.PE_ANNUAL
            filt.is_no_filter = False
            filters.append(filt)
        if min_turnover_rate is not None or max_turnover_rate is not None:
            filt = _f.SimpleFilter()
            filt.filter_min = min_turnover_rate if min_turnover_rate is not None else 0
            filt.filter_max = max_turnover_rate if max_turnover_rate is not None else 100
            filt.stock_field = _f.StockField.TURNOVER_RATE
            filt.is_no_filter = False
            filters.append(filt)
        if min_change_rate is not None or max_change_rate is not None:
            filt = _f.SimpleFilter()
            filt.filter_min = min_change_rate if min_change_rate is not None else -100
            filt.filter_max = max_change_rate if max_change_rate is not None else 100
            filt.stock_field = _f.StockField.CHANGE_RATE
            filt.is_no_filter = False
            filters.append(filt)
        if min_volume is not None:
            filt = _f.SimpleFilter()
            filt.filter_min = min_volume
            filt.filter_max = 1e18
            filt.stock_field = _f.StockField.VOLUME
            filt.is_no_filter = False
            filters.append(filt)

        sort_field_map = {
            "market_val": _f.StockField.MARKET_VAL,
            "change_rate": _f.StockField.CHANGE_RATE,
            "turnover_rate": _f.StockField.TURNOVER_RATE,
            "volume": _f.StockField.VOLUME,
            "pe": _f.StockField.PE_ANNUAL,
        }
        sort_enum = sort_field_map.get(sort_field, _f.StockField.MARKET_VAL)
    except Exception as exc:
        logger.debug("stock_filter setup failed: %s", exc)
        return None

    with _quote_context() as ctx:
        if ctx is None:
            return None
        try:
            results: list[dict[str, Any]] = []
            begin = 0
            page_size = 200
            while len(results) < limit:
                ret, data = ctx.get_stock_filter(
                    market=market_map[market],
                    filter_list=filters,
                    begin=begin,
                    num=page_size,
                )
                if ret != 0:
                    break
                _last_page, _all_count, page_rows = data
                if not page_rows:
                    break
                for stock in page_rows:
                    results.append(
                        {
                            "code": str(getattr(stock, "stock_code", "")),
                            "name": str(getattr(stock, "stock_name", "")),
                        }
                    )
                if len(page_rows) < page_size:
                    break
                begin += page_size
            # Apply sort orientation by re-querying snapshot to enrich.
            results = results[:limit]
            # If we have results, attach a snapshot pass to provide sort key.
            snaps = {s["code"]: s for s in get_snapshot([r["code"] for r in results])}
            for row in results:
                snap = snaps.get(row["code"], {})
                row.update(
                    {
                        "last_price": snap.get("last_price"),
                        "market_val": snap.get("market_val"),
                        "change_rate": snap.get("change_rate"),
                        "turnover_rate": snap.get("turnover_rate"),
                        "volume": snap.get("volume"),
                        "pe_ratio": snap.get("pe_ratio"),
                    }
                )
            results.sort(
                key=lambda r: (r.get(sort_field) or 0),
                reverse=not ascend,
            )
            return results
        except Exception as exc:
            logger.debug("stock_filter call failed: %s", exc)
            return None


def get_plate_stock(plate_code: str) -> list[dict[str, Any]] | None:
    """Return constituent stocks for a plate/index. ``plate_code`` should be a
    full futu code like ``HK.BK1910`` or ``HK.800700`` (恒生科技).
    """

    with _quote_context() as ctx:
        if ctx is None:
            return None
        try:
            ret, data = ctx.get_plate_stock(plate_code)
            if ret != 0 or data is None or len(data) == 0:
                return None
            return [
                {
                    "code": str(row.get("code", "")),
                    "name": str(row.get("stock_name", "") or row.get("name", "")),
                }
                for _, row in data.iterrows()
            ]
        except Exception as exc:
            logger.debug("get_plate_stock failed for %s: %s", plate_code, exc)
            return None


# ---------------------------------------------------------------------------
# News-search HTTP shims (no OpenD dependency)
# ---------------------------------------------------------------------------


def futunn_community_sentiment(symbol: str, *, size: int = 30, timeout: float = 6.0) -> dict[str, Any] | None:
    """Quick HTTP call to futunn's stock_feed-style endpoint. The exact endpoint
    is the same one the ``futu-comment-sentiment`` skill targets:
    ``https://ai-news-search.futunn.com``. Returns a compact summary dict.

    This function is intentionally best-effort and conservative. It will
    NOT classify sentiment with an LLM — that costs and is the news collector's
    AI layer's job. We just return raw counts and the top recent post headlines
    so the AI layer (if enabled) can decide.
    """

    try:
        import urllib.parse
        import urllib.request
        import json
    except Exception:
        return None
    try:
        params = urllib.parse.urlencode({"keyword": symbol, "size": size})
        url = f"https://ai-news-search.futunn.com/stock_feed?{params}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "futu-enrichment/0.1 (news-collector)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return None
        items = payload.get("data") or []
        headlines = []
        for item in items[:10]:
            title = str(item.get("title", "")).strip()
            if title:
                headlines.append(
                    {
                        "title": title,
                        "publish_time": item.get("publish_time"),
                        "url": item.get("url"),
                    }
                )
        return {
            "symbol": symbol,
            "post_count": len(items),
            "recent_headlines": headlines,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.debug("futunn_community_sentiment failed for %s: %s", symbol, exc)
        return None
