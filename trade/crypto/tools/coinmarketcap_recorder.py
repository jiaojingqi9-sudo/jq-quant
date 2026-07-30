#!/usr/bin/env python3
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


COINMARKETCAP_QUOTES_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
COINMARKETCAP_KEY_INFO_URL = "https://pro-api.coinmarketcap.com/v1/key/info"


class CoinMarketCapError(RuntimeError):
    pass


@dataclass(frozen=True)
class CoinMarketCapSnapshotConfig:
    api_key: str
    symbols: tuple[str, ...]
    convert: str = "USD"
    output_path: Path = Path("runtime/coinmarketcap_market_records.csv")
    timeout_seconds: float = 15.0


def parse_crypto_symbols(raw: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(raw, str):
        tokens = raw.replace("\n", ",").replace(" ", ",").split(",")
    else:
        tokens = list(raw)
    symbols: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        symbol = str(token).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return tuple(symbols)


def fetch_coinmarketcap_quotes(
    api_key: str,
    symbols: Iterable[str],
    *,
    convert: str = "USD",
    timeout_seconds: float = 15.0,
) -> dict:
    clean_key = api_key.strip()
    clean_symbols = parse_crypto_symbols(symbols)
    clean_convert = convert.strip().upper() or "USD"
    if not clean_key:
        raise CoinMarketCapError("CoinMarketCap API Key 为空")
    if not clean_symbols:
        raise CoinMarketCapError("加密货币代码为空")

    params = urlencode({"symbol": ",".join(clean_symbols), "convert": clean_convert})
    request = Request(
        f"{COINMARKETCAP_QUOTES_URL}?{params}",
        headers={
            "Accepts": "application/json",
            "X-CMC_PRO_API_KEY": clean_key,
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=float(timeout_seconds)) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CoinMarketCapError(f"CoinMarketCap HTTP {exc.code}: {body[:300]}") from exc
    except (URLError, TimeoutError) as exc:
        raise CoinMarketCapError(f"CoinMarketCap 请求失败: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CoinMarketCapError("CoinMarketCap 返回不是有效 JSON") from exc

    status = payload.get("status") or {}
    error_code = status.get("error_code")
    if error_code not in (None, 0):
        message = status.get("error_message") or f"error_code={error_code}"
        raise CoinMarketCapError(f"CoinMarketCap 返回错误: {message}")
    return payload


def fetch_coinmarketcap_key_info(api_key: str, *, timeout_seconds: float = 15.0) -> dict:
    clean_key = api_key.strip()
    if not clean_key:
        raise CoinMarketCapError("CoinMarketCap API Key 为空")

    request = Request(
        COINMARKETCAP_KEY_INFO_URL,
        headers={
            "Accepts": "application/json",
            "X-CMC_PRO_API_KEY": clean_key,
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=float(timeout_seconds)) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CoinMarketCapError(f"CoinMarketCap HTTP {exc.code}: {body[:300]}") from exc
    except (URLError, TimeoutError) as exc:
        raise CoinMarketCapError(f"CoinMarketCap 请求失败: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CoinMarketCapError("CoinMarketCap 返回不是有效 JSON") from exc

    status = payload.get("status") or {}
    error_code = status.get("error_code")
    if error_code not in (None, 0):
        message = status.get("error_message") or f"error_code={error_code}"
        raise CoinMarketCapError(f"CoinMarketCap 返回错误: {message}")
    return payload


def flatten_coinmarketcap_quotes(payload: dict, *, event: str, convert: str) -> list[dict]:
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    quote_currency = convert.strip().upper() or "USD"
    data = payload.get("data") or {}
    rows: list[dict] = []

    for symbol, value in data.items():
        item = value[0] if isinstance(value, list) and value else value
        if not isinstance(item, dict):
            continue
        quote = (item.get("quote") or {}).get(quote_currency) or {}
        rows.append(
            {
                "captured_at_utc": captured_at,
                "event": event,
                "symbol": str(symbol).upper(),
                "cmc_id": item.get("id", ""),
                "name": item.get("name", ""),
                "slug": item.get("slug", ""),
                "quote_currency": quote_currency,
                "price": quote.get("price", ""),
                "volume_24h": quote.get("volume_24h", ""),
                "volume_change_24h": quote.get("volume_change_24h", ""),
                "percent_change_1h": quote.get("percent_change_1h", ""),
                "percent_change_24h": quote.get("percent_change_24h", ""),
                "percent_change_7d": quote.get("percent_change_7d", ""),
                "market_cap": quote.get("market_cap", ""),
                "market_cap_dominance": quote.get("market_cap_dominance", ""),
                "fully_diluted_market_cap": quote.get("fully_diluted_market_cap", ""),
                "circulating_supply": item.get("circulating_supply", ""),
                "total_supply": item.get("total_supply", ""),
                "max_supply": item.get("max_supply", ""),
                "last_updated": quote.get("last_updated", item.get("last_updated", "")),
                "source": "coinmarketcap",
            }
        )
    return rows


def append_coinmarketcap_snapshot(config: CoinMarketCapSnapshotConfig, *, event: str) -> int:
    payload = fetch_coinmarketcap_quotes(
        config.api_key,
        config.symbols,
        convert=config.convert,
        timeout_seconds=config.timeout_seconds,
    )
    rows = flatten_coinmarketcap_quotes(payload, event=event, convert=config.convert)
    if not rows:
        return 0

    output_path = Path(config.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    write_header = not output_path.exists() or output_path.stat().st_size == 0
    with output_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
    return len(rows)
