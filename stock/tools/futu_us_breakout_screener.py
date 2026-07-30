#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date
import os
from pathlib import Path
import re
import socket
import sys
import time
from typing import Iterable

import pandas as pd


DEFAULT_HISTORY_START = "2000-01-01"


class ScreenerError(RuntimeError):
    pass


SPECIAL_SECURITY_KEYWORDS = (
    " ADR",
    " ADS",
    "UNIT",
    "UNITS",
    "WARRANT",
    "WARRANTS",
    " WT",
    " WTS",
    " RIGHT",
    " RIGHTS",
    " PFD",
    " PREFERRED",
    " PREF",
    "SPAC",
    "ACQUISITION CORP",
)

SUPPORTED_US_EXCHANGES = {"US_NASDAQ", "US_NYSE", "US_AMEX"}


@dataclass(frozen=True)
class BreakoutMatch:
    code: str
    name: str
    last_date: str
    open: float
    close: float
    prev_close: float
    latest_high: float
    previous_record: float
    record_distance_pct: float
    volume: float
    prev_volume: float
    volume_ratio: float
    streak_days: int


@dataclass(frozen=True)
class QuickCandidate:
    code: str
    name: str
    cur_price: float | None
    change_rate: float | None
    high_to_52w_high_pct: float | None
    cur_to_52w_high_pct: float | None
    volume: float | None
    prev_volume: float | None
    volume_ratio: float | None


@dataclass(frozen=True)
class MonthlyHighCandidate:
    code: str
    name: str
    yahoo_symbol: str
    last_date: str
    latest_value: float
    historical_record: float
    previous_record: float
    record_distance_pct: float
    volume: float | None


@dataclass(frozen=True)
class LiquidityRule:
    lookback_days: int = 60
    min_trading_days: int = 45
    median_dollar_lookback_days: int = 20
    min_median_dollar_volume: float = 1_000_000.0


def normalize_us_code(raw: str) -> str:
    code = raw.strip().upper()
    if not code:
        raise ValueError("empty symbol")
    return code if code.startswith("US.") else f"US.{code}"


def parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return []
    tokens = raw.replace("\n", ",").replace(" ", ",").split(",")
    return list(dict.fromkeys(normalize_us_code(token) for token in tokens if token.strip()))


def is_probably_otc_or_special_us_code(code: str, name: str = "") -> bool:
    ticker = code.split(".", 1)[-1].upper()
    clean_name = re.sub(r"\s+", " ", name.upper()).strip()
    if "." in ticker or "-" in ticker:
        return True
    if len(ticker) >= 5:
        return True
    if ticker.endswith(("U", "W", "R")) and len(ticker) >= 4:
        return True
    return any(keyword in clean_name for keyword in SPECIAL_SECURITY_KEYWORDS)


def ensure_opend_reachable(host: str, port: int) -> None:
    try:
        with socket.create_connection((host, port), timeout=3):
            return
    except OSError as exc:
        raise ScreenerError(f"Cannot connect to Futu OpenD at {host}:{port}. Start OpenD first.") from exc


def _expect_ok(result: tuple, futu, context: str):
    ret, payload, *rest = result
    if ret != futu.RET_OK:
        raise ScreenerError(f"{context} failed: {payload}")
    if rest:
        return (payload, *rest)
    return payload


def fetch_us_universe(quote_ctx, futu, *, include_etf: bool = False) -> pd.DataFrame:
    security_types = [futu.SecurityType.STOCK]
    if include_etf:
        security_types.append(futu.SecurityType.ETF)

    frames: list[pd.DataFrame] = []
    for security_type in security_types:
        data = _expect_ok(
            quote_ctx.get_stock_basicinfo(futu.Market.US, stock_type=security_type),
            futu,
            f"get_stock_basicinfo[{security_type}]",
        )
        if not data.empty:
            frames.append(data.copy())

    if not frames:
        return pd.DataFrame(columns=["code", "name"])

    universe = pd.concat(frames, ignore_index=True)
    universe = universe[universe["code"].astype(str).str.startswith("US.")].copy()
    universe = universe.drop_duplicates(subset=["code"], keep="first")
    return universe.sort_values("code").reset_index(drop=True)


def fetch_main_us_stock_codes(quote_ctx, futu) -> set[str]:
    universe = fetch_us_universe(quote_ctx, futu, include_etf=False)
    if universe.empty or "exchange_type" not in universe.columns:
        return set()
    main = universe[universe["exchange_type"].astype(str).isin(SUPPORTED_US_EXCHANGES)]
    main = main[~main.apply(lambda row: is_probably_otc_or_special_us_code(str(row["code"]), str(row.get("name", ""))), axis=1)]
    return set(main["code"].astype(str))


def fetch_industry_map(quote_ctx, futu, codes: Iterable[str], *, batch_size: int = 100) -> dict[str, str]:
    unique_codes = list(dict.fromkeys(str(code) for code in codes if str(code).strip()))
    if not unique_codes:
        return {}

    industries: dict[str, str] = {}
    batch_size = max(1, int(batch_size))
    for start in range(0, len(unique_codes), batch_size):
        batch = unique_codes[start : start + batch_size]
        data = _expect_ok(quote_ctx.get_owner_plate(batch), futu, "get_owner_plate")
        if data.empty or not {"code", "plate_name", "plate_type"}.issubset(data.columns):
            continue
        industry_rows = data[data["plate_type"].astype(str).str.upper().eq("INDUSTRY")]
        for code, group in industry_rows.groupby("code"):
            names = [str(name).strip() for name in group["plate_name"].dropna().unique() if str(name).strip()]
            if names:
                industries[str(code)] = " / ".join(names)
    return industries


def _simple_filter(futu, stock_field, *, filter_min=None, filter_max=None, sort=None):
    filter_obj = futu.SimpleFilter()
    filter_obj.stock_field = stock_field
    filter_obj.is_no_filter = False
    filter_obj.filter_min = filter_min
    filter_obj.filter_max = filter_max
    filter_obj.sort = sort
    return filter_obj


def _accumulate_filter(futu, stock_field, *, days: int, filter_min=None, filter_max=None, sort=None):
    filter_obj = futu.AccumulateFilter()
    filter_obj.stock_field = stock_field
    filter_obj.days = days
    filter_obj.is_no_filter = False
    filter_obj.filter_min = filter_min
    filter_obj.filter_max = filter_max
    filter_obj.sort = sort
    return filter_obj


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _item_value(item, key, default=None):
    if isinstance(key, tuple):
        return item.__dict__.get(key, default)
    return getattr(item, key, item.__dict__.get(key, default))


def futu_us_code_to_yahoo_symbol(code: str) -> str:
    ticker = str(code).strip().upper()
    if ticker.startswith("US."):
        ticker = ticker[3:]
    return ticker.replace(".", "-")


def _extract_yfinance_symbol_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()

    frame = pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        level_0 = raw.columns.get_level_values(0)
        level_1 = raw.columns.get_level_values(1)
        if symbol in level_0:
            frame = raw[symbol].copy()
        elif symbol in level_1:
            frame = raw.xs(symbol, axis=1, level=1).copy()
    else:
        frame = raw.copy()
    return frame


def _standardize_yfinance_history(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["time_key", "open", "high", "low", "close", "volume"])

    clean = frame.copy().reset_index()
    clean.columns = [str(column).strip() for column in clean.columns]
    date_column = "Date" if "Date" in clean.columns else "Datetime" if "Datetime" in clean.columns else clean.columns[0]
    rename_map = {
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    }
    clean = clean.rename(columns=rename_map)
    clean["time_key"] = pd.to_datetime(clean[date_column], errors="coerce").dt.tz_localize(None)
    for column in ("open", "high", "low", "close", "volume"):
        if column not in clean.columns:
            clean[column] = pd.NA
        clean[column] = pd.to_numeric(clean[column], errors="coerce")
    clean = clean.dropna(subset=["time_key", "open", "high", "low", "close"])
    clean = clean.sort_values("time_key").drop_duplicates(subset=["time_key"], keep="last")
    return clean[["time_key", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


_YF_TZ_CACHE_READY = False


def _ensure_yfinance_tz_cache(yf) -> None:
    """Point yfinance's sqlite timezone cache at a writable per-user dir.

    The default shared cache caused intermittent
    ``OperationalError('unable to open database file')`` during threaded
    batch downloads (see runtime/futu_stock_screener_app.log, 2026-04-30).
    Override the location with SCREENER_YF_CACHE_DIR if needed. Best-effort:
    any failure falls back to yfinance's default behavior.
    """
    global _YF_TZ_CACHE_READY
    if _YF_TZ_CACHE_READY:
        return
    try:
        import tempfile

        cache_dir = os.environ.get("SCREENER_YF_CACHE_DIR") or str(
            Path(tempfile.gettempdir()) / f"yf-tz-cache-{os.getuid()}"
        )
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(cache_dir)
        _YF_TZ_CACHE_READY = True
    except Exception:
        # Never let cache plumbing break a download attempt.
        _YF_TZ_CACHE_READY = True


def download_yfinance_ohlcv(
    targets: Iterable[tuple[str, str]],
    *,
    period: str,
    interval: str,
    start: str | None = None,
    end: str | None = None,
    batch_size: int = 80,
    batch_sleep_seconds: float = 0.0,
    progress=None,
) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    _ensure_yfinance_tz_cache(yf)

    target_list = [(str(code), str(name)) for code, name in targets if str(code).strip()]
    if not target_list:
        return {}

    symbol_to_code: dict[str, str] = {}
    symbol_names: list[str] = []
    for code, _name in target_list:
        symbol = futu_us_code_to_yahoo_symbol(code)
        if symbol and symbol not in symbol_to_code:
            symbol_to_code[symbol] = code
            symbol_names.append(symbol)

    histories: dict[str, pd.DataFrame] = {}
    batch_size = max(1, int(batch_size))
    total = len(symbol_names)
    for offset in range(0, total, batch_size):
        batch = symbol_names[offset : offset + batch_size]
        kwargs = _yfinance_download_kwargs(batch, period=period, interval=interval, start=start, end=end, threads=True)
        try:
            raw = yf.download(**kwargs)
        except Exception:
            raw = pd.DataFrame()
        empty_symbols: list[str] = []
        for symbol in batch:
            code = symbol_to_code[symbol]
            frame = _extract_yfinance_symbol_frame(raw, symbol)
            history = _standardize_yfinance_history(frame)
            histories[code] = history
            if history.empty:
                empty_symbols.append(symbol)

        if empty_symbols:
            if batch_sleep_seconds > 0:
                time.sleep(min(batch_sleep_seconds, 1.0))
            for symbol in empty_symbols:
                code = symbol_to_code[symbol]
                try:
                    retry_kwargs = _yfinance_download_kwargs(
                        [symbol],
                        period=period,
                        interval=interval,
                        start=start,
                        end=end,
                        threads=False,
                    )
                    retry_raw = yf.download(**retry_kwargs)
                except Exception:
                    retry_raw = pd.DataFrame()
                retry_frame = _extract_yfinance_symbol_frame(retry_raw, symbol)
                histories[code] = _standardize_yfinance_history(retry_frame)
        if progress is not None:
            progress(min(offset + len(batch), total), total)
        if batch_sleep_seconds > 0 and offset + len(batch) < total:
            time.sleep(batch_sleep_seconds)
    return histories


def _yfinance_download_kwargs(
    symbols: list[str],
    *,
    period: str,
    interval: str,
    start: str | None,
    end: str | None,
    threads: bool,
) -> dict:
    kwargs = {
        "tickers": symbols if len(symbols) > 1 else symbols[0],
        "interval": interval,
        "auto_adjust": False,
        "actions": False,
        "progress": False,
        "threads": threads,
        "group_by": "ticker",
    }
    if start:
        kwargs["start"] = start
        if end:
            kwargs["end"] = end
    else:
        kwargs["period"] = period
    return kwargs


def evaluate_monthly_high_candidate(
    code: str,
    name: str,
    history: pd.DataFrame,
    *,
    near_high_pct: float,
    new_high_on: str = "high",
) -> MonthlyHighCandidate | None:
    clean = _standardize_yfinance_history(history) if "time_key" not in history.columns else history.copy()
    if clean.empty:
        return None

    field = "close" if new_high_on == "close" else "high"
    clean[field] = pd.to_numeric(clean[field], errors="coerce")
    clean["volume"] = pd.to_numeric(clean.get("volume"), errors="coerce")
    clean = clean.dropna(subset=["time_key", field])
    if clean.empty:
        return None

    latest = clean.iloc[-1]
    historical_record = float(clean[field].max())
    latest_value = float(latest[field])
    if historical_record <= 0:
        return None
    record_distance_pct = (latest_value / historical_record - 1.0) * 100.0
    if record_distance_pct < -max(0.0, float(near_high_pct)):
        return None

    previous_series = clean[field].iloc[:-1]
    previous_record = float(previous_series.max()) if len(previous_series) else historical_record
    if pd.isna(previous_record) or previous_record <= 0:
        previous_record = historical_record

    volume = _as_float(latest.get("volume"))
    return MonthlyHighCandidate(
        code=code,
        name=name,
        yahoo_symbol=futu_us_code_to_yahoo_symbol(code),
        last_date=pd.to_datetime(latest["time_key"]).strftime("%Y-%m-%d"),
        latest_value=latest_value,
        historical_record=historical_record,
        previous_record=previous_record,
        record_distance_pct=record_distance_pct,
        volume=volume,
    )


def evaluate_recent_weekly_high_candidate(
    code: str,
    name: str,
    long_history: pd.DataFrame,
    weekly_history: pd.DataFrame,
    *,
    near_high_pct: float,
    new_high_on: str = "high",
    recent_weeks: int = 3,
) -> MonthlyHighCandidate | None:
    long_clean = _standardize_yfinance_history(long_history) if "time_key" not in long_history.columns else long_history.copy()
    weekly_clean = _standardize_yfinance_history(weekly_history) if "time_key" not in weekly_history.columns else weekly_history.copy()
    if long_clean.empty or weekly_clean.empty:
        return None

    field = "close" if new_high_on == "close" else "high"
    for frame in (long_clean, weekly_clean):
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
        frame["volume"] = pd.to_numeric(frame.get("volume"), errors="coerce")
        frame.dropna(subset=["time_key", field], inplace=True)
        frame.sort_values("time_key", inplace=True)
        frame.reset_index(drop=True, inplace=True)
    if long_clean.empty or weekly_clean.empty:
        return None

    historical_record = float(long_clean[field].max())
    if historical_record <= 0:
        return None

    previous_series = long_clean[field].iloc[:-1]
    previous_record = float(previous_series.max()) if len(previous_series) else historical_record
    if pd.isna(previous_record) or previous_record <= 0:
        previous_record = historical_record

    recent = weekly_clean.tail(max(1, int(recent_weeks))).copy()
    value_index = recent[field].idxmax()
    latest_value = float(recent.loc[value_index, field])
    if latest_value <= 0:
        return None

    record_distance_pct = (latest_value / historical_record - 1.0) * 100.0
    if record_distance_pct < -max(0.0, float(near_high_pct)):
        return None

    volume = _as_float(recent["volume"].sum())
    return MonthlyHighCandidate(
        code=code,
        name=name,
        yahoo_symbol=futu_us_code_to_yahoo_symbol(code),
        last_date=pd.to_datetime(recent.loc[value_index, "time_key"]).strftime("%Y-%m-%d"),
        latest_value=latest_value,
        historical_record=historical_record,
        previous_record=previous_record,
        record_distance_pct=record_distance_pct,
        volume=volume,
    )


def liquidity_reject_reason(history: pd.DataFrame, rule: LiquidityRule | None = None) -> str | None:
    rule = rule or LiquidityRule()
    clean = _standardize_yfinance_history(history) if "time_key" not in history.columns else history.copy()
    if clean.empty:
        return "日K无数据"
    for column in ("close", "volume"):
        clean[column] = pd.to_numeric(clean[column], errors="coerce")
    clean = clean.dropna(subset=["time_key", "close", "volume"]).sort_values("time_key")
    if clean.empty:
        return "日K无数据"

    recent = clean.tail(max(1, int(rule.lookback_days)))
    trading_days = int((recent["volume"] > 0).sum())
    if trading_days < int(rule.min_trading_days):
        return "成交天数不足"

    dollar_recent = clean.tail(max(1, int(rule.median_dollar_lookback_days))).copy()
    dollar_volume = dollar_recent["close"] * dollar_recent["volume"]
    median_dollar_volume = float(dollar_volume.median()) if len(dollar_volume) else 0.0
    if median_dollar_volume < float(rule.min_median_dollar_volume):
        return "成交额过低"
    return None


def build_quick_filter_list(
    futu,
    *,
    near_high_pct: float = 15.0,
    new_high_on: str = "high",
    max_volume_ratio: float | None = None,
    require_up_day: bool = True,
    require_lower_volume: bool = True,
):
    high_field = (
        futu.StockField.CUR_PRICE_TO_HIGHEST52_WEEKS_RATIO
        if new_high_on == "close"
        else futu.StockField.HIGH_PRICE_TO_HIGHEST52_WEEKS_RATIO
    )
    filters = [
        _simple_filter(
            futu,
            high_field,
            filter_min=-max(0.0, float(near_high_pct)),
            sort=futu.SortDir.DESCEND,
        ),
        _simple_filter(futu, futu.StockField.CUR_PRICE, filter_min=0.01),
    ]
    if require_lower_volume:
        filters.extend(
            [
                _simple_filter(futu, futu.StockField.VOLUME_RATIO, filter_min=0.0, filter_max=max_volume_ratio),
                _accumulate_filter(futu, futu.StockField.VOLUME, days=1, filter_min=1.0),
                _accumulate_filter(futu, futu.StockField.VOLUME, days=2, filter_min=1.0),
            ]
        )
    if require_up_day:
        filters.append(_accumulate_filter(futu, futu.StockField.CHANGE_RATE, days=1, filter_min=0.0001))
    return filters


def _quick_candidate_from_item(
    item,
    *,
    require_lower_volume: bool = True,
    skip_special_filter: bool = True,
) -> QuickCandidate | None:
    code = str(item.stock_code)
    name = str(item.stock_name)
    if skip_special_filter and is_probably_otc_or_special_us_code(code, name):
        return None

    volume = _as_float(_item_value(item, ("volume", 1)))
    volume_2d = _as_float(_item_value(item, ("volume", 2)))
    prev_volume = volume_2d - volume if volume is not None and volume_2d is not None else None
    if prev_volume is not None and prev_volume < 0:
        prev_volume = None
    if require_lower_volume:
        if volume is None or prev_volume is None or prev_volume <= 0:
            return None
        if volume >= prev_volume:
            return None

    return QuickCandidate(
        code=code,
        name=name,
        cur_price=_as_float(_item_value(item, "cur_price")),
        change_rate=_as_float(_item_value(item, ("change_rate", 1))),
        high_to_52w_high_pct=_as_float(_item_value(item, "high_price_to_highest52_weeks_ratio")),
        cur_to_52w_high_pct=_as_float(_item_value(item, "cur_price_to_highest52_weeks_ratio")),
        volume=volume,
        prev_volume=prev_volume,
        volume_ratio=_as_float(_item_value(item, "volume_ratio")),
    )


def fetch_quick_candidates(
    quote_ctx,
    futu,
    *,
    near_high_pct: float = 15.0,
    new_high_on: str = "high",
    limit: int = 200,
    page_size: int = 200,
    page_sleep_seconds: float = 3.1,
    max_volume_ratio: float | None = None,
    require_up_day: bool = True,
    require_lower_volume: bool = True,
    skip_special_filter: bool = True,
    progress=None,
) -> tuple[list[QuickCandidate], int]:
    filters = build_quick_filter_list(
        futu,
        near_high_pct=near_high_pct,
        new_high_on=new_high_on,
        max_volume_ratio=max_volume_ratio,
        require_up_day=require_up_day,
        require_lower_volume=require_lower_volume,
    )
    max_results = max(0, int(limit))
    page_size = max(1, min(200, int(page_size)))
    begin = 0
    all_count = 0
    candidates: list[QuickCandidate] = []

    while True:
        request_num = page_size if max_results <= 0 else min(page_size, max_results - len(candidates))
        if request_num <= 0:
            break
        ret, data = quote_ctx.get_stock_filter(futu.Market.US, filters, begin=begin, num=request_num)
        if ret != futu.RET_OK:
            raise ScreenerError(f"get_stock_filter failed: {data}")
        last_page, all_count, stock_list = data
        if progress is not None:
            progress(begin, all_count, len(stock_list))
        for item in stock_list:
            candidate = _quick_candidate_from_item(
                item,
                require_lower_volume=require_lower_volume,
                skip_special_filter=skip_special_filter,
            )
            if candidate is not None:
                candidates.append(candidate)
                if max_results > 0 and len(candidates) >= max_results:
                    break
        begin += len(stock_list)
        if last_page or not stock_list or (max_results > 0 and len(candidates) >= max_results):
            break
        if page_sleep_seconds > 0:
            time.sleep(page_sleep_seconds)

    return candidates, int(all_count)


def request_daily_history(
    quote_ctx,
    futu,
    code: str,
    *,
    start: str,
    end: str | None,
    max_count: int,
    retry_on_rate_limit: bool = True,
) -> pd.DataFrame:
    effective_end = end or date.today().isoformat()
    page_req_key = None
    frames: list[pd.DataFrame] = []
    page_guard = 0

    while True:
        ret, data, page_req_key = quote_ctx.request_history_kline(
            code=code,
            start=start,
            end=effective_end,
            ktype=futu.KLType.K_DAY,
            autype=futu.AuType.QFQ,
            fields=[
                futu.KL_FIELD.DATE_TIME,
                futu.KL_FIELD.OPEN,
                futu.KL_FIELD.CLOSE,
                futu.KL_FIELD.HIGH,
                futu.KL_FIELD.TRADE_VOL,
            ],
            max_count=max_count,
            page_req_key=page_req_key,
            session=futu.Session.RTH,
        )
        if ret != futu.RET_OK:
            if retry_on_rate_limit and "每30秒最多60次" in str(data):
                time.sleep(31.0)
                return request_daily_history(
                    quote_ctx,
                    futu,
                    code,
                    start=start,
                    end=end,
                    max_count=max_count,
                    retry_on_rate_limit=False,
                )
            raise ScreenerError(f"request_history_kline[{code}] failed: {data}")
        if not data.empty:
            frames.append(data)
        page_guard += 1
        if page_req_key is None or page_guard >= 30:
            break

    if not frames:
        return pd.DataFrame(columns=["time_key", "open", "close", "high", "volume"])

    history = pd.concat(frames, ignore_index=True)
    history = history.drop_duplicates(subset=["time_key"], keep="last")
    return history.sort_values("time_key").reset_index(drop=True)


def _clean_history(history: pd.DataFrame) -> pd.DataFrame:
    required = {"time_key", "open", "close", "high", "volume"}
    missing = required - set(history.columns)
    if missing:
        raise ScreenerError(f"history is missing columns: {sorted(missing)}")

    clean = history[["time_key", "open", "close", "high", "volume"]].copy()
    clean["time_key"] = pd.to_datetime(clean["time_key"], errors="coerce")
    for column in ("open", "close", "high", "volume"):
        clean[column] = pd.to_numeric(clean[column], errors="coerce")
    clean = clean.dropna(subset=["time_key", "open", "close", "high", "volume"])
    clean = clean.sort_values("time_key").drop_duplicates(subset=["time_key"], keep="last")
    return clean.reset_index(drop=True)


def evaluate_history(
    code: str,
    name: str,
    history: pd.DataFrame,
    *,
    up_days: int = 14,
    new_high_on: str = "high",
    near_high_pct: float = 0.0,
    volume_mode: str = "lower",
) -> BreakoutMatch | None:
    match, _reason = evaluate_history_detailed(
        code,
        name,
        history,
        up_days=up_days,
        new_high_on=new_high_on,
        near_high_pct=near_high_pct,
        volume_mode=volume_mode,
    )
    return match


def evaluate_history_detailed(
    code: str,
    name: str,
    history: pd.DataFrame,
    *,
    up_days: int = 14,
    new_high_on: str = "high",
    near_high_pct: float = 0.0,
    volume_mode: str = "lower",
    previous_record_override: float | None = None,
) -> tuple[BreakoutMatch | None, str | None]:
    up_days = max(1, int(up_days))
    clean = _clean_history(history)
    required_rows = max(up_days, 2)
    if len(clean) < required_rows:
        return None, "日K不足"

    bullish_flags = clean["close"] > clean["open"]
    actual_streak_days = 0
    for is_bullish in reversed(bullish_flags.tolist()):
        if not bool(is_bullish):
            break
        actual_streak_days += 1
    if actual_streak_days < up_days:
        return None, "连续阳线不足"

    latest = clean.iloc[-1]
    previous = clean.iloc[-2]
    latest_volume = float(latest["volume"])
    previous_volume = float(previous["volume"])
    if volume_mode == "lower" and not latest_volume < previous_volume:
        return None, "不满足缩量"
    if volume_mode == "higher" and not latest_volume > previous_volume:
        return None, "不满足放量"

    high_field = "close" if new_high_on == "close" else "high"
    previous_record = clean[high_field].iloc[:-1].max()
    if previous_record_override is not None and not pd.isna(previous_record_override):
        previous_record = max(float(previous_record), float(previous_record_override))
    if pd.isna(previous_record):
        return None, "缺少前高"
    record_value = float(latest[high_field])
    previous_record = float(previous_record)
    if previous_record <= 0:
        return None, "前高无效"
    record_distance_pct = (record_value / previous_record - 1.0) * 100.0
    threshold = max(0.0, float(near_high_pct))
    if threshold <= 0:
        if record_value <= previous_record:
            return None, "未创历史新高"
    elif record_distance_pct < -threshold:
        return None, "距离历史高点过远"

    prev_volume = previous_volume
    volume_ratio = float(latest["volume"]) / prev_volume if prev_volume else 0.0
    return BreakoutMatch(
        code=code,
        name=name,
        last_date=latest["time_key"].strftime("%Y-%m-%d"),
        open=float(latest["open"]),
        close=float(latest["close"]),
        prev_close=float(previous["close"]),
        latest_high=float(latest["high"]),
        previous_record=previous_record,
        record_distance_pct=record_distance_pct,
        volume=float(latest["volume"]),
        prev_volume=prev_volume,
        volume_ratio=volume_ratio,
        streak_days=actual_streak_days,
    ), None


def _iter_targets(universe: pd.DataFrame, symbols: Iterable[str], limit: int | None) -> list[tuple[str, str]]:
    symbol_list = list(symbols)
    if symbol_list:
        name_map = dict(zip(universe.get("code", []), universe.get("name", []), strict=False))
        return [(code, str(name_map.get(code, ""))) for code in symbol_list]

    targets = [(str(row["code"]), str(row.get("name", ""))) for _, row in universe.iterrows()]
    if limit is not None:
        return targets[:limit]
    return targets


def scan_targets(
    quote_ctx,
    futu,
    targets: list[tuple[str, str]],
    *,
    start: str,
    end: str | None,
    up_days: int,
    new_high_on: str,
    near_high_pct: float,
    volume_mode: str,
    sleep_seconds: float,
    max_count: int,
    progress: bool,
) -> list[BreakoutMatch]:
    matches: list[BreakoutMatch] = []
    total = len(targets)
    for index, (code, name) in enumerate(targets, start=1):
        if progress:
            print(f"[{index}/{total}] {code} {name}".rstrip(), flush=True)
        try:
            history = request_daily_history(
                quote_ctx,
                futu,
                code,
                start=start,
                end=end,
                max_count=max_count,
            )
            match = evaluate_history(
                code,
                name,
                history,
                up_days=up_days,
                new_high_on=new_high_on,
                near_high_pct=near_high_pct,
                volume_mode=volume_mode,
            )
        except Exception as exc:
            if progress:
                print(f"  skip: {exc}", flush=True)
            match = None
        if match is not None:
            matches.append(match)
            print(f"  MATCH: {match.code} close={match.close:.2f} volume_ratio={match.volume_ratio:.2f}", flush=True)
        if sleep_seconds > 0 and index < total:
            time.sleep(sleep_seconds)
    return matches


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Standalone Futu OpenD US screener: consecutive bullish daily candles, "
            "optional latest volume condition, and proximity to the prior record high."
        )
    )
    parser.add_argument("--host", default=os.getenv("FUTU_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("FUTU_PORT", "11111")))
    parser.add_argument("--symbols", help="Comma/space separated tickers, e.g. AAPL,NVDA,US.MSFT")
    parser.add_argument("--limit", type=int, help="Only scan the first N symbols from the US universe.")
    parser.add_argument("--include-etf", action="store_true", help="Include US ETFs in addition to stocks.")
    parser.add_argument("--confirm-full-market", action="store_true", help="Allow scanning the entire US universe.")
    parser.add_argument("--start", default=DEFAULT_HISTORY_START, help="History start date, default: 2000-01-01.")
    parser.add_argument("--end", default=None, help="History end date, default: latest available.")
    parser.add_argument("--up-days", type=int, default=14, help="Number of consecutive bullish daily candles required.")
    parser.add_argument(
        "--new-high-on",
        choices=("high", "close"),
        default="high",
        help="Use latest intraday high or latest close to define the historical high.",
    )
    parser.add_argument(
        "--near-high-pct",
        type=float,
        default=0.0,
        help="Allow the latest high/close to be within this percent below the prior record. 0 means strict new high.",
    )
    parser.add_argument(
        "--volume-mode",
        choices=("lower", "higher", "none"),
        default="lower",
        help="Latest volume condition for exact K-line validation.",
    )
    parser.add_argument("--sleep", type=float, default=0.6, help="Pause between symbols to respect Futu limits.")
    parser.add_argument("--max-count", type=int, default=1000, help="K-line rows per page.")
    parser.add_argument("--output", type=Path, help="Optional CSV path for matches.")
    parser.add_argument("--quiet", action="store_true", help="Hide per-symbol progress.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = parse_symbols(args.symbols)

    ensure_opend_reachable(args.host, args.port)

    import futu

    quote_ctx = futu.OpenQuoteContext(host=args.host, port=args.port)
    try:
        universe = fetch_us_universe(quote_ctx, futu, include_etf=args.include_etf)
        if universe.empty and not symbols:
            raise ScreenerError("US universe is empty. Check market-data permission in OpenD.")

        if not symbols and args.limit is None and not args.confirm_full_market:
            print(
                f"Loaded {len(universe)} US symbols. Full scan consumes one history-K quota per symbol.\n"
                "For a test run, add --limit 50 or --symbols AAPL,NVDA.\n"
                "For the whole market, add --confirm-full-market.",
                file=sys.stderr,
            )
            return 2

        targets = _iter_targets(universe, symbols, args.limit)
        matches = scan_targets(
            quote_ctx,
            futu,
            targets,
            start=args.start,
            end=args.end,
            up_days=args.up_days,
            new_high_on=args.new_high_on,
            near_high_pct=args.near_high_pct,
            volume_mode=args.volume_mode,
            sleep_seconds=args.sleep,
            max_count=args.max_count,
            progress=not args.quiet,
        )
    finally:
        quote_ctx.close()

    rows = [asdict(match) for match in matches]
    result = pd.DataFrame(rows)
    if result.empty:
        print("No matches.")
        return 0

    result = result.sort_values(["volume_ratio", "code"]).reset_index(drop=True)
    print()
    print(result.to_string(index=False))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(args.output, index=False)
        print(f"\nSaved {len(result)} match(es) to {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ScreenerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
