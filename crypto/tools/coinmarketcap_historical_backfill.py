#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import json
import math
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from coinmarketcap_recorder import CoinMarketCapError, parse_crypto_symbols


CMC_LISTINGS_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
CMC_HISTORICAL_QUOTES_URL = "https://pro-api.coinmarketcap.com/v3/cryptocurrency/quotes/historical"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "runtime" / "coinmarketcap_history"

INTERVAL_SECONDS = {
    "5m": 5 * 60,
    "10m": 10 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "6h": 6 * 60 * 60,
    "12h": 12 * 60 * 60,
    "24h": 24 * 60 * 60,
    "1d": 24 * 60 * 60,
}

HISTORICAL_FIELDNAMES = [
    "timestamp",
    "id",
    "symbol",
    "name",
    "slug",
    "quote_currency",
    "price",
    "volume_24h",
    "market_cap",
    "circulating_supply",
    "total_supply",
    "max_supply",
    "last_updated",
]


@dataclass(frozen=True)
class Asset:
    id: int
    symbol: str
    name: str
    slug: str = ""
    cmc_rank: int | None = None


@dataclass
class RateLimiter:
    requests_per_minute: float
    next_at: float = 0.0

    def wait(self) -> None:
        if self.requests_per_minute <= 0:
            return
        now = time.monotonic()
        if self.next_at > now:
            time.sleep(self.next_at - now)
        spacing = 60.0 / float(self.requests_per_minute)
        self.next_at = max(time.monotonic(), self.next_at) + spacing


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_api_key(arg_key: str | None) -> str:
    key = (arg_key or os.getenv("COINMARKETCAP_API_KEY") or os.getenv("CMC_API_KEY") or "").strip()
    if not key:
        raise CoinMarketCapError("缺少 CoinMarketCap API Key。可用 --api-key 或 COINMARKETCAP_API_KEY。")
    return key


def cmc_get(
    url: str,
    api_key: str,
    params: dict,
    *,
    limiter: RateLimiter,
    timeout_seconds: float,
    max_retries: int,
) -> dict:
    query = urlencode({key: value for key, value in params.items() if value is not None})
    request_url = f"{url}?{query}"
    last_error: Exception | None = None

    for attempt in range(max(1, max_retries + 1)):
        limiter.wait()
        request = Request(
            request_url,
            headers={"Accepts": "application/json", "X-CMC_PRO_API_KEY": api_key},
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = CoinMarketCapError(f"HTTP {exc.code}: {body[:500]}")
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                sleep_seconds = float(retry_after) if retry_after and retry_after.isdigit() else min(60.0, 2.0 ** attempt)
                time.sleep(sleep_seconds)
                continue
            if 500 <= exc.code < 600:
                time.sleep(min(60.0, 2.0 ** attempt))
                continue
            raise last_error
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(min(60.0, 2.0 ** attempt))
            continue

        status = payload.get("status") or {}
        error_code = status.get("error_code")
        if error_code not in (None, 0):
            message = status.get("error_message") or f"error_code={error_code}"
            if "rate limit" in str(message).lower() and attempt < max_retries:
                time.sleep(min(60.0, 2.0 ** attempt))
                continue
            raise CoinMarketCapError(f"CoinMarketCap 返回错误: {message}")
        return payload

    raise CoinMarketCapError(f"CoinMarketCap 请求重试耗尽: {last_error}")


def fetch_top_assets(
    api_key: str,
    *,
    limit: int,
    convert: str,
    limiter: RateLimiter,
    timeout_seconds: float,
    max_retries: int,
) -> list[Asset]:
    payload = cmc_get(
        CMC_LISTINGS_URL,
        api_key,
        {
            "start": 1,
            "limit": int(limit),
            "convert": convert,
            "sort": "market_cap",
            "sort_dir": "desc",
        },
        limiter=limiter,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )
    assets: list[Asset] = []
    for item in payload.get("data") or []:
        try:
            asset_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        assets.append(
            Asset(
                id=asset_id,
                symbol=str(item.get("symbol") or "").upper(),
                name=str(item.get("name") or ""),
                slug=str(item.get("slug") or ""),
                cmc_rank=int(item["cmc_rank"]) if item.get("cmc_rank") is not None else None,
            )
        )
    return assets


def flatten_historical_quotes(payload: dict, *, quote_currency: str) -> list[dict]:
    data = payload.get("data") or []
    if isinstance(data, dict):
        if "quotes" in data or "quote" in data:
            data = [data]
        else:
            data = [value for value in data.values() if isinstance(value, dict)]

    rows: list[dict] = []
    for asset in data:
        if not isinstance(asset, dict):
            continue
        asset_id = asset.get("id", "")
        symbol = str(asset.get("symbol") or "").upper()
        name = asset.get("name", "")
        slug = asset.get("slug", "")
        quotes = asset.get("quotes") or asset.get("quote") or []
        if isinstance(quotes, dict):
            quotes = [quotes]
        for quote_item in quotes:
            if not isinstance(quote_item, dict):
                continue
            timestamp = quote_item.get("timestamp") or quote_item.get("time_open") or quote_item.get("last_updated")
            quote = (quote_item.get("quote") or {}).get(quote_currency) or {}
            rows.append(
                {
                    "timestamp": timestamp or quote.get("last_updated", ""),
                    "id": asset_id,
                    "symbol": symbol,
                    "name": name,
                    "slug": slug,
                    "quote_currency": quote_currency,
                    "price": quote.get("price", ""),
                    "volume_24h": quote.get("volume_24h", ""),
                    "market_cap": quote.get("market_cap", ""),
                    "circulating_supply": asset.get("circulating_supply", quote.get("circulating_supply", "")),
                    "total_supply": asset.get("total_supply", quote.get("total_supply", "")),
                    "max_supply": asset.get("max_supply", quote.get("max_supply", "")),
                    "last_updated": quote.get("last_updated", timestamp or ""),
                }
            )
    return rows


def append_rows(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with gzip.open(path, "at", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORICAL_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "assets": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": 1, "assets": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def sanitize_filename(value: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.upper())
    return clean.strip("_") or "UNKNOWN"


def asset_output_path(output_dir: Path, asset: Asset, interval: str, convert: str) -> Path:
    filename = f"{asset.id}_{sanitize_filename(asset.symbol)}_{interval}_{convert.upper()}.csv.gz"
    return output_dir / "quotes_historical" / filename


def iter_chunks(start: datetime, end: datetime, *, interval: str, max_points: int) -> Iterable[tuple[datetime, datetime]]:
    seconds = INTERVAL_SECONDS[interval]
    cursor = start
    chunk_span = timedelta(seconds=seconds * max(1, int(max_points) - 1))
    step = timedelta(seconds=seconds)
    while cursor < end:
        chunk_end = min(end, cursor + chunk_span)
        yield cursor, chunk_end
        cursor = chunk_end + step


def estimate_requests(start: datetime, end: datetime, *, asset_count: int, interval: str, max_points: int) -> tuple[int, int, int]:
    seconds = INTERVAL_SECONDS[interval]
    chunks_per_asset = 0
    approx_points_per_asset = 0
    approx_credits_per_asset = 0
    for chunk_start, chunk_end in iter_chunks(start, end, interval=interval, max_points=max_points):
        chunk_points = int((chunk_end - chunk_start).total_seconds() // seconds) + 1
        chunks_per_asset += 1
        approx_points_per_asset += chunk_points
        approx_credits_per_asset += max(1, math.ceil(chunk_points / 100))
    return (
        chunks_per_asset * int(asset_count),
        approx_points_per_asset * int(asset_count),
        approx_credits_per_asset * int(asset_count),
    )


def write_universe(output_dir: Path, assets: list[Asset]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "universe_top_assets.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "symbol", "name", "slug", "cmc_rank"])
        writer.writeheader()
        for asset in assets:
            writer.writerow(
                {
                    "id": asset.id,
                    "symbol": asset.symbol,
                    "name": asset.name,
                    "slug": asset.slug,
                    "cmc_rank": asset.cmc_rank or "",
                }
            )


def fetch_historical_chunk(
    api_key: str,
    asset: Asset,
    *,
    start: datetime,
    end: datetime,
    interval: str,
    convert: str,
    count: int,
    limiter: RateLimiter,
    timeout_seconds: float,
    max_retries: int,
) -> tuple[list[dict], int]:
    payload = cmc_get(
        CMC_HISTORICAL_QUOTES_URL,
        api_key,
        {
            "id": asset.id,
            "time_start": iso_utc(start),
            "time_end": iso_utc(end),
            "interval": interval,
            "convert": convert,
            "count": count,
        },
        limiter=limiter,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )
    status = payload.get("status") or {}
    try:
        credit_count = int(status.get("credit_count") or 0)
    except (TypeError, ValueError):
        credit_count = 0
    return flatten_historical_quotes(payload, quote_currency=convert), credit_count


def run_backfill(args: argparse.Namespace) -> int:
    api_key = load_api_key(args.api_key)
    output_dir = Path(args.output_dir)
    state_path = output_dir / "backfill_state.json"
    state = load_state(state_path)
    state.setdefault("assets", {})
    limiter = RateLimiter(float(args.requests_per_minute))

    end = parse_utc(args.end) if args.end else utc_now()
    start = parse_utc(args.start) if args.start else end - timedelta(days=365 * int(args.years))
    if start >= end:
        raise CoinMarketCapError("start 必须早于 end")
    if args.interval not in INTERVAL_SECONDS:
        raise CoinMarketCapError(f"暂不支持 interval={args.interval}")

    if args.ids:
        id_tokens = [token.strip() for token in args.ids.replace(" ", ",").split(",") if token.strip()]
        assets = [Asset(id=int(token), symbol=f"ID{token}", name=f"CoinMarketCap {token}") for token in id_tokens]
    else:
        assets = fetch_top_assets(
            api_key,
            limit=int(args.limit),
            convert=args.convert,
            limiter=limiter,
            timeout_seconds=args.timeout,
            max_retries=args.max_retries,
        )
        if args.symbols:
            wanted = set(parse_crypto_symbols(args.symbols))
            assets = [asset for asset in assets if asset.symbol in wanted]

    if not assets:
        raise CoinMarketCapError("没有可抓取的币种")

    write_universe(output_dir, assets)
    requests, points, credits = estimate_requests(
        start,
        end,
        asset_count=len(assets),
        interval=args.interval,
        max_points=args.max_points,
    )
    message = (
        f"计划抓取 {len(assets)} 个币种，{iso_utc(start)} -> {iso_utc(end)}，interval={args.interval}，"
        f"预计请求约 {requests:,} 次，数据点约 {points:,} 个，credits 约 {credits:,}。"
    )
    _emit(args, message)
    if args.dry_run:
        return 0

    total_assets = len(assets)
    for asset_index, asset in enumerate(assets, start=1):
        key = str(asset.id)
        asset_state = state["assets"].setdefault(
            key,
            {
                "id": asset.id,
                "symbol": asset.symbol,
                "name": asset.name,
                "start": iso_utc(start),
                "end": iso_utc(end),
                "interval": args.interval,
                "next_start": iso_utc(start),
                "rows": 0,
                "requests": 0,
                "credits": 0,
                "completed": False,
            },
        )
        if asset_state.get("completed") and not args.force:
            _emit(args, f"[{asset_index}/{total_assets}] {asset.symbol} 已完成，跳过")
            continue

        cursor = parse_utc(asset_state.get("next_start") or iso_utc(start))
        if args.force:
            cursor = start
            asset_state.update({"next_start": iso_utc(start), "rows": 0, "requests": 0, "completed": False})

        output_path = asset_output_path(output_dir, asset, args.interval, args.convert)
        for chunk_start, chunk_end in iter_chunks(cursor, end, interval=args.interval, max_points=args.max_points):
            if getattr(args, "cancel_event", None) is not None and args.cancel_event.is_set():
                asset_state["updated_at"] = iso_utc(utc_now())
                save_state(state_path, state)
                _emit(args, f"已停止：{asset.symbol} 下次从 {asset_state.get('next_start')} 继续")
                return 2
            _emit(args, f"[{asset_index}/{total_assets}] {asset.symbol} {iso_utc(chunk_start)} -> {iso_utc(chunk_end)}")
            try:
                rows, credit_count = fetch_historical_chunk(
                    api_key,
                    asset,
                    start=chunk_start,
                    end=chunk_end,
                    interval=args.interval,
                    convert=args.convert,
                    count=args.max_points,
                    limiter=limiter,
                    timeout_seconds=args.timeout,
                    max_retries=args.max_retries,
                )
            except CoinMarketCapError as exc:
                asset_state["last_error"] = str(exc)
                asset_state["updated_at"] = iso_utc(utc_now())
                save_state(state_path, state)
                _emit(args, f"停止：{asset.symbol} 请求失败：{exc}", error=True)
                return 1

            append_rows(output_path, rows)
            asset_state["rows"] = int(asset_state.get("rows") or 0) + len(rows)
            asset_state["requests"] = int(asset_state.get("requests") or 0) + 1
            asset_state["credits"] = int(asset_state.get("credits") or 0) + int(credit_count)
            asset_state["next_start"] = iso_utc(chunk_end + timedelta(seconds=INTERVAL_SECONDS[args.interval]))
            asset_state["updated_at"] = iso_utc(utc_now())
            asset_state.pop("last_error", None)
            save_state(state_path, state)

        asset_state["completed"] = True
        asset_state["next_start"] = iso_utc(end)
        asset_state["updated_at"] = iso_utc(utc_now())
        save_state(state_path, state)
        _emit(
            args,
            f"[{asset_index}/{total_assets}] {asset.symbol} 完成，"
            f"累计 rows={asset_state.get('rows')} credits={asset_state.get('credits')}",
        )

    _emit(args, f"完成。数据目录：{output_dir}")
    return 0


def _emit(args: argparse.Namespace, message: str, *, error: bool = False) -> None:
    callback = getattr(args, "progress_callback", None)
    if callback is not None:
        callback(message)
        return
    print(message, file=sys.stderr if error else sys.stdout, flush=True)


def make_backfill_args(
    *,
    api_key: str | None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    limit: int = 100,
    symbols: str | None = None,
    ids: str | None = None,
    convert: str = "USD",
    interval: str = "5m",
    years: int = 10,
    start: str | None = None,
    end: str | None = None,
    max_points: int = 10000,
    requests_per_minute: float = 25.0,
    timeout: float = 30.0,
    max_retries: int = 5,
    dry_run: bool = False,
    force: bool = False,
    progress_callback: Callable[[str], None] | None = None,
    cancel_event=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        api_key=api_key,
        output_dir=output_dir,
        limit=limit,
        symbols=symbols,
        ids=ids,
        convert=convert,
        interval=interval,
        years=years,
        start=start,
        end=end,
        max_points=max_points,
        requests_per_minute=requests_per_minute,
        timeout=timeout,
        max_retries=max_retries,
        dry_run=dry_run,
        force=force,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CoinMarketCap historical high-frequency backfill with rate limiting and resume.")
    parser.add_argument("--api-key", help="CoinMarketCap API key. Defaults to COINMARKETCAP_API_KEY or CMC_API_KEY.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--limit", type=int, default=100, help="Top N by current CoinMarketCap market cap.")
    parser.add_argument("--symbols", help="Optional comma/space separated symbols within the top-N universe.")
    parser.add_argument("--ids", help="Optional comma/space separated CoinMarketCap IDs. Overrides --limit/--symbols.")
    parser.add_argument("--convert", default="USD")
    parser.add_argument("--interval", default="5m", choices=sorted(INTERVAL_SECONDS))
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--start", help="UTC ISO start, e.g. 2016-01-01T00:00:00Z. Overrides --years.")
    parser.add_argument("--end", help="UTC ISO end. Default: now.")
    parser.add_argument("--max-points", type=int, default=10000, help="Historical points per request chunk.")
    parser.add_argument("--requests-per-minute", type=float, default=25.0, help="Stay just below your official plan limit.")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true", help="Estimate request/point counts without downloading history.")
    parser.add_argument("--force", action="store_true", help="Restart assets even if state says completed.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_backfill(args)
    except CoinMarketCapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
