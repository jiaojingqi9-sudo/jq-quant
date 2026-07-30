from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any
from uuid import uuid4

import websockets

from .crypto_ofim import (
    BinanceSpotClient,
    CryptoOfimError,
    MAINNET_BASE_URL,
    RUNTIME_DIR,
    TESTNET_BASE_URL,
    _append_jsonl,
    _base_asset,
    _safe_float,
    load_crypto_ofim_settings,
    record_crypto_ofim_user_stream_event,
)


STREAM_PID_FILE = RUNTIME_DIR / "stream.pid"
STREAM_LOG_FILE = RUNTIME_DIR / "stream.log"
STREAM_STATUS_FILE = RUNTIME_DIR / "stream_status.json"
STREAM_CACHE_FILE = RUNTIME_DIR / "ws_cache.json"
STREAM_EVENTS_FILE = RUNTIME_DIR / "ws_events.jsonl"
HIGH_VOLUME_STREAM_EVENTS = {"trade", "depth_delta"}

PROD_STREAM_BASE_URL = "wss://stream.binance.com:9443/stream"
TESTNET_STREAM_BASE_URL = "wss://stream.testnet.binance.vision/stream"
PROD_USER_STREAM_BASE_URL = "wss://stream.binance.com:9443/ws"
TESTNET_USER_STREAM_BASE_URL = "wss://stream.testnet.binance.vision/ws"
PROD_WS_API_BASE_URL = "wss://ws-api.binance.com/ws-api/v3"
TESTNET_WS_API_BASE_URL = "wss://ws-api.testnet.binance.vision/ws-api/v3"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return True
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _stream_age_seconds(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _env_float(name: str, *, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _stream_debug_events_enabled() -> bool:
    return _env_bool("CRYPTO_OFIM_STREAM_DEBUG_EVENTS", default=False)


def _stream_ws_max_queue() -> int:
    return _env_int("CRYPTO_OFIM_STREAM_WS_MAX_QUEUE", default=64, minimum=32, maximum=4096)


def _stream_user_ws_max_queue() -> int:
    return _env_int("CRYPTO_OFIM_USER_STREAM_WS_MAX_QUEUE", default=32, minimum=16, maximum=1024)


def _stream_cache_interval_seconds() -> float:
    return _env_float("CRYPTO_OFIM_STREAM_CACHE_INTERVAL_SECONDS", default=2.0, minimum=0.5, maximum=10.0)


def _append_stream_event(event_type: str, payload: dict[str, Any]) -> None:
    if event_type in HIGH_VOLUME_STREAM_EVENTS and not _stream_debug_events_enabled():
        return
    try:
        _append_jsonl(STREAM_EVENTS_FILE, {"ts": _iso_now(), "event_type": event_type, **payload})
    except Exception:
        return


def _public_error(exc: BaseException | str) -> str:
    text = str(exc)
    for marker in ("signature=", "apiKey="):
        if marker in text:
            text = text.split(marker, 1)[0] + marker + "***"
    return text[:500]


def _signed_ws_api_params(
    *,
    api_key: str,
    api_secret: str,
    recv_window_ms: int,
    now_ms: int | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "apiKey": api_key,
        "recvWindow": int(recv_window_ms),
        "timestamp": int(now_ms if now_ms is not None else time.time() * 1000),
    }
    payload = "&".join(f"{key}={params[key]}" for key in sorted(params))
    params["signature"] = hmac.new(api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return params


def _ws_api_request(method: str, *, params: dict[str, Any] | None = None, request_id: str | None = None) -> dict[str, Any]:
    return {
        "id": request_id or str(uuid4()),
        "method": method,
        "params": params or {},
    }


def _extract_user_stream_event(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("event"), dict) and payload["event"].get("e"):
        return payload["event"]
    if isinstance(payload.get("data"), dict) and payload["data"].get("e"):
        return payload["data"]
    if payload.get("e"):
        return payload
    return None


def _ws_api_status_ok(payload: dict[str, Any], *, request_id: str) -> bool:
    return payload.get("id") == request_id and int(_safe_float(payload.get("status"))) == 200


def _book_to_rows(side: dict[float, float], *, reverse: bool, limit: int) -> list[list[float]]:
    prices = sorted(side, reverse=reverse)[:limit]
    return [[round(price, 12), round(side[price], 12)] for price in prices if side[price] > 0]


@dataclass
class LocalDepthBook:
    symbol: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    last_update_id: int = 0
    initialized_at: str | None = None
    updated_at: str | None = None
    resync_count: int = 0
    gap_count: int = 0

    def load_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.bids = {
            _safe_float(price): _safe_float(qty)
            for price, qty in snapshot.get("bids", [])
            if _safe_float(price) > 0 and _safe_float(qty) > 0
        }
        self.asks = {
            _safe_float(price): _safe_float(qty)
            for price, qty in snapshot.get("asks", [])
            if _safe_float(price) > 0 and _safe_float(qty) > 0
        }
        self.last_update_id = int(_safe_float(snapshot.get("lastUpdateId")))
        now = _iso_now()
        self.initialized_at = now
        self.updated_at = now

    def apply_depth_update(self, event: dict[str, Any]) -> str:
        first_update_id = int(_safe_float(event.get("U")))
        final_update_id = int(_safe_float(event.get("u")))
        if final_update_id <= 0:
            return "invalid"
        if self.last_update_id and final_update_id <= self.last_update_id:
            return "stale"
        if self.last_update_id and first_update_id > self.last_update_id + 1:
            self.gap_count += 1
            return "gap"
        if self.last_update_id and not (first_update_id <= self.last_update_id + 1 <= final_update_id):
            return "waiting"

        for price_raw, qty_raw in event.get("b", []):
            self._set_level(self.bids, price_raw, qty_raw)
        for price_raw, qty_raw in event.get("a", []):
            self._set_level(self.asks, price_raw, qty_raw)
        self.last_update_id = final_update_id
        self.updated_at = _iso_now()
        return "applied"

    def _set_level(self, side: dict[float, float], price_raw: Any, qty_raw: Any) -> None:
        price = _safe_float(price_raw)
        qty = _safe_float(qty_raw)
        if price <= 0:
            return
        if qty <= 0:
            side.pop(price, None)
        else:
            side[price] = qty

    def snapshot(self, *, limit: int) -> dict[str, Any]:
        bids = _book_to_rows(self.bids, reverse=True, limit=limit)
        asks = _book_to_rows(self.asks, reverse=False, limit=limit)
        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else 0.0
        return {
            "symbol": self.symbol,
            "last_update_id": self.last_update_id,
            "initialized_at": self.initialized_at,
            "updated_at": self.updated_at,
            "resync_count": self.resync_count,
            "gap_count": self.gap_count,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "Bid": bids,
            "Ask": asks,
        }


class BinanceMarketStream:
    def __init__(self, *, depth_limit: int | None = None) -> None:
        self.settings = load_crypto_ofim_settings()
        self.depth_limit = int(depth_limit or self.settings.depth_limit)
        self.market_client = BinanceSpotClient(
            base_url=self.settings.market_data_base_url,
            recv_window_ms=self.settings.recv_window_ms,
        )
        self.user_client = BinanceSpotClient(
            base_url=self.settings.base_url,
            api_key=self.settings.api_key,
            api_secret=self.settings.api_secret,
            recv_window_ms=self.settings.recv_window_ms,
        )
        # Use the same symbol selection as the trading engine so the stream is
        # a replaceable market-data plug rather than a separate strategy.
        from .crypto_ofim import CryptoOfimEngine

        self.symbols = CryptoOfimEngine(
            self.settings,
            client=self.user_client,
            market_client=self.market_client,
        ).active_symbols()
        self.books = {symbol: LocalDepthBook(symbol=symbol) for symbol in self.symbols}
        self.trades: dict[str, deque[dict[str, Any]]] = {
            symbol: deque(maxlen=max(100, int(self.settings.trade_limit))) for symbol in self.symbols
        }
        self.stop_requested = False
        self.message_count = 0
        self.user_event_count = 0
        self.user_stream_status = "disabled"
        self.last_user_event_at: str | None = None
        self.started_at = _iso_now()

    @property
    def stream_base_url(self) -> str:
        return PROD_STREAM_BASE_URL

    def _stream_url(self) -> str:
        streams: list[str] = []
        for symbol in self.symbols:
            lower = symbol.lower()
            streams.append(f"{lower}@depth@100ms")
            streams.append(f"{lower}@trade")
        return f"{self.stream_base_url}?streams={'/'.join(streams)}"

    @property
    def user_stream_base_url(self) -> str:
        if self.settings.mode == "testnet" or self.settings.base_url.rstrip("/") == TESTNET_BASE_URL:
            return TESTNET_USER_STREAM_BASE_URL
        if self.settings.base_url.rstrip("/") == MAINNET_BASE_URL:
            return PROD_USER_STREAM_BASE_URL
        return TESTNET_USER_STREAM_BASE_URL if "testnet" in self.settings.base_url else PROD_USER_STREAM_BASE_URL

    @property
    def ws_api_base_url(self) -> str:
        if self.settings.mode == "testnet" or self.settings.base_url.rstrip("/") == TESTNET_BASE_URL:
            return TESTNET_WS_API_BASE_URL
        if self.settings.base_url.rstrip("/") == MAINNET_BASE_URL:
            return PROD_WS_API_BASE_URL
        return TESTNET_WS_API_BASE_URL if "testnet" in self.settings.base_url else PROD_WS_API_BASE_URL

    async def _resync_symbol(self, symbol: str) -> None:
        snapshot_limit = min(5000, max(100, self.depth_limit))
        snapshot = self.market_client.depth_snapshot(symbol, limit=snapshot_limit)
        book = self.books[symbol]
        book.load_snapshot(snapshot)
        book.resync_count += 1
        _append_stream_event(
            "depth_resynced",
            {
                "symbol": symbol,
                "last_update_id": book.last_update_id,
                "bid_levels": len(book.bids),
                "ask_levels": len(book.asks),
            },
        )

    async def _resync_all(self) -> None:
        for symbol in self.symbols:
            await self._resync_symbol(symbol)

    def _handle_trade(self, data: dict[str, Any]) -> None:
        symbol = str(data.get("s") or "").upper()
        if symbol not in self.trades:
            return
        direction = "SELL" if data.get("m") else "BUY"
        row = {
            "event_time": data.get("E"),
            "trade_time": data.get("T"),
            "price": _safe_float(data.get("p")),
            "volume": _safe_float(data.get("q")),
            "ticker_direction": direction,
        }
        self.trades[symbol].append(row)
        _append_stream_event("trade", {"symbol": symbol, **row})

    async def _handle_depth(self, data: dict[str, Any]) -> None:
        symbol = str(data.get("s") or "").upper()
        book = self.books.get(symbol)
        if book is None:
            return
        result = book.apply_depth_update(data)
        if result == "gap":
            _append_stream_event(
                "depth_gap",
                {
                    "symbol": symbol,
                    "local_update_id": book.last_update_id,
                    "event_first_update_id": data.get("U"),
                    "event_final_update_id": data.get("u"),
                },
            )
            await self._resync_symbol(symbol)
        elif result == "applied":
            _append_stream_event(
                "depth_delta",
                {
                    "symbol": symbol,
                    "first_update_id": data.get("U"),
                    "final_update_id": data.get("u"),
                    "bid_updates": len(data.get("b") or []),
                    "ask_updates": len(data.get("a") or []),
                },
            )

    def _cache_payload(self, *, status: str, detail: str = "") -> dict[str, Any]:
        return {
            "status": status,
            "detail": detail,
            "mode": self.settings.mode,
            "execution_base_url": self.settings.base_url,
            "market_data": self.settings.market_data,
            "market_data_base_url": self.settings.market_data_base_url,
            "stream_url": self.stream_base_url,
            "symbols": self.symbols,
            "depth_limit": self.depth_limit,
            "started_at": self.started_at,
            "updated_at": _iso_now(),
            "message_count": self.message_count,
            "user_event_count": self.user_event_count,
            "user_stream_status": self.user_stream_status,
            "last_user_event_at": self.last_user_event_at,
            "books": {
                symbol: book.snapshot(limit=self.depth_limit)
                for symbol, book in self.books.items()
            },
            "trades": {
                symbol: list(buffer)
                for symbol, buffer in self.trades.items()
            },
        }

    def write_cache(self, *, status: str = "running", detail: str = "") -> None:
        payload = self._cache_payload(status=status, detail=detail)
        _write_json_atomic(STREAM_CACHE_FILE, payload)
        _write_json_atomic(STREAM_STATUS_FILE, self._status_payload(payload))

    def _status_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        books = payload.get("books") if isinstance(payload.get("books"), dict) else {}
        trades = payload.get("trades") if isinstance(payload.get("trades"), dict) else {}
        return {
            key: value
            for key, value in {
                **payload,
                "book_count": len(books),
                "trade_buffer_count": sum(len(rows) for rows in trades.values() if isinstance(rows, list)),
            }.items()
            if key not in {"books", "trades"}
        }

    async def _run_market_stream(self) -> None:
        url = self._stream_url()
        _append_stream_event(
            "stream_started",
            {
                "mode": self.settings.mode,
                "market_data": self.settings.market_data,
                "symbols": self.symbols,
                "url": self.stream_base_url,
            },
        )
        cache_interval = _stream_cache_interval_seconds()
        async with websockets.connect(url, ping_interval=20, ping_timeout=60, close_timeout=5, max_queue=_stream_ws_max_queue()) as ws:
            self.write_cache(status="running")
            last_cache_write = time.time()
            async for raw in ws:
                if self.stop_requested:
                    break
                self.message_count += 1
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                data = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
                if not isinstance(data, dict):
                    continue
                event_type = data.get("e")
                if event_type == "trade":
                    self._handle_trade(data)
                elif event_type == "depthUpdate":
                    await self._handle_depth(data)
                if time.time() - last_cache_write >= cache_interval:
                    self.write_cache(status="running")
                    last_cache_write = time.time()

    async def _keepalive_user_stream(self, listen_key: str) -> None:
        while not self.stop_requested:
            await asyncio.sleep(20 * 60)
            if self.stop_requested:
                return
            self.user_client.keepalive_user_data_stream(listen_key)
            _append_stream_event("user_stream_keepalive", {"listen_key_suffix": listen_key[-6:]})

    async def _run_signed_user_stream_once(self) -> None:
        if not self.settings.api_key or not self.settings.api_secret:
            self.user_stream_status = "disabled"
            self.write_cache(status="running")
            return
        request_id = str(uuid4())
        request = _ws_api_request(
            "userDataStream.subscribe.signature",
            request_id=request_id,
            params=_signed_ws_api_params(
                api_key=self.settings.api_key,
                api_secret=self.settings.api_secret,
                recv_window_ms=self.settings.recv_window_ms,
            ),
        )
        self.user_stream_status = "connecting_ws_api"
        self.write_cache(status="running")
        _append_stream_event("user_stream_ws_api_connecting", {"url": self.ws_api_base_url})
        async with websockets.connect(
            self.ws_api_base_url,
            ping_interval=20,
            ping_timeout=60,
            close_timeout=5,
            max_queue=_stream_user_ws_max_queue(),
        ) as ws:
            await ws.send(json.dumps(request, separators=(",", ":"), ensure_ascii=False))
            subscription_id: int | None = None
            deadline = time.time() + 15
            while time.time() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                event = _extract_user_stream_event(payload)
                if event:
                    self._record_user_stream_event(event, source="ws_api")
                    continue
                if payload.get("id") != request_id:
                    continue
                if _ws_api_status_ok(payload, request_id=request_id):
                    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
                    subscription_id = int(_safe_float(result.get("subscriptionId")))
                    break
                raise CryptoOfimError(f"WS API user stream subscribe failed: {payload.get('error') or payload.get('msg') or payload.get('status')}")
            if subscription_id is None:
                raise CryptoOfimError("WS API user stream subscribe timed out.")

            self.user_stream_status = "running_ws_api"
            self.write_cache(status="running")
            _append_stream_event("user_stream_ws_api_started", {"subscription_id": subscription_id})
            async for raw in ws:
                if self.stop_requested:
                    break
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event = _extract_user_stream_event(payload)
                if not event:
                    continue
                self._record_user_stream_event(event, source="ws_api")

    def _record_user_stream_event(self, event: dict[str, Any], *, source: str) -> None:
        self.user_event_count += 1
        self.last_user_event_at = _iso_now()
        event_type = str(event.get("e") or "unknown")
        record_crypto_ofim_user_stream_event(
            event,
            mode=self.settings.mode,
            quote_asset=self.settings.quote_asset,
        )
        _append_stream_event("user_stream_event", {"event_type": event_type, "source": source})
        self.write_cache(status="running")

    async def _run_signed_user_stream(self) -> None:
        retry_delay = 3.0
        while not self.stop_requested:
            try:
                await self._run_signed_user_stream_once()
            except CryptoOfimError:
                raise
            except Exception as exc:
                self.user_stream_status = "ws_api_reconnecting"
                _append_stream_event("user_stream_ws_api_error", {"detail": _public_error(exc)})
                self.write_cache(status="running", detail="user stream reconnecting")
                await asyncio.sleep(retry_delay)
                retry_delay = min(30.0, retry_delay * 1.5)
            else:
                if not self.stop_requested:
                    self.user_stream_status = "ws_api_reconnecting"
                    self.write_cache(status="running", detail="user stream reconnecting")
                    await asyncio.sleep(retry_delay)

    async def _run_legacy_user_stream(self) -> None:
        if self.settings.mode != "testnet" or not self.settings.api_key or not self.settings.use_user_stream:
            self.user_stream_status = "disabled"
            self.write_cache(status="running")
            return
        try:
            listen_key = self.user_client.start_user_data_stream()
        except CryptoOfimError as exc:
            detail = str(exc)
            if "410" in detail or "Gone" in detail:
                self.user_stream_status = "unavailable_listen_key_deprecated"
                _append_stream_event(
                    "user_stream_unavailable",
                    {
                        "detail": "listenKey endpoint unavailable/deprecated on this Binance environment; using order-log ledger fallback",
                    },
                )
                self.write_cache(status="running", detail="user stream unavailable; order-log ledger fallback")
                return
            raise
        self.user_stream_status = "connecting"
        self.write_cache(status="running")
        _append_stream_event("user_stream_started", {"url": self.user_stream_base_url, "listen_key_suffix": listen_key[-6:]})
        keepalive_task = asyncio.create_task(self._keepalive_user_stream(listen_key))
        try:
            async with websockets.connect(
                f"{self.user_stream_base_url}/{listen_key}",
                ping_interval=20,
                ping_timeout=60,
                close_timeout=5,
                max_queue=_stream_user_ws_max_queue(),
            ) as ws:
                self.user_stream_status = "running"
                self.write_cache(status="running")
                async for raw in ws:
                    if self.stop_requested:
                        break
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    self._record_user_stream_event(event, source="legacy_listen_key")
        finally:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass
            try:
                self.user_client.close_user_data_stream(listen_key)
            except Exception:
                pass
            self.user_stream_status = "stopped"

    async def _run_user_stream(self) -> None:
        if self.settings.mode != "testnet" or not self.settings.api_key or not self.settings.use_user_stream:
            self.user_stream_status = "disabled"
            self.write_cache(status="running")
            return
        try:
            await self._run_signed_user_stream()
        except CryptoOfimError as exc:
            self.user_stream_status = "ws_api_unavailable"
            _append_stream_event(
                "user_stream_ws_api_unavailable",
                {"detail": _public_error(exc), "fallback": "legacy_listen_key"},
            )
            self.write_cache(status="running", detail="WS API user stream unavailable; trying legacy listenKey")
            await self._run_legacy_user_stream()

    async def run(self) -> None:
        await self._resync_all()
        self.write_cache(status="connecting")
        tasks = [asyncio.create_task(self._run_market_stream())]
        if self.settings.mode == "testnet" and self.settings.api_key and self.settings.use_user_stream:
            tasks.append(asyncio.create_task(self._run_user_stream()))
        try:
            await asyncio.gather(*tasks)
        finally:
            self.stop_requested = True
            for task in tasks:
                if not task.done():
                    task.cancel()
            self.write_cache(status="stopped")


def load_crypto_ofim_ws_cache(*, max_age_seconds: int = 5) -> dict[str, Any]:
    payload = _read_json(STREAM_CACHE_FILE)
    if not payload:
        return {}
    age = _stream_age_seconds(STREAM_CACHE_FILE)
    if age is None or age > max_age_seconds:
        return {}
    if payload.get("status") not in {"running", "connecting"}:
        return {}
    return payload


def read_crypto_ofim_stream_status() -> dict[str, Any]:
    payload = _read_json(STREAM_STATUS_FILE)
    pid = _pid_from_file(STREAM_PID_FILE)
    if not payload:
        return {"running": _pid_running(pid), "pid": pid, "status": "not_started", "detail": "stream has not run yet"}
    payload["running"] = _pid_running(pid)
    payload["pid"] = pid
    payload["cache_age_seconds"] = _stream_age_seconds(STREAM_CACHE_FILE)
    return payload


def _pid_from_file(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def run_crypto_ofim_stream(*, depth_limit: int | None = None) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    current = _pid_from_file(STREAM_PID_FILE)
    if current and current != os.getpid() and _pid_running(current):
        raise SystemExit(f"Crypto OFIM market stream is already running with pid {current}.")
    STREAM_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    stream = BinanceMarketStream(depth_limit=depth_limit)

    def _handle_signal(_signum, _frame) -> None:
        stream.stop_requested = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    try:
        asyncio.run(stream.run())
    except Exception as exc:
        _write_json_atomic(
            STREAM_STATUS_FILE,
            {
                "status": "error",
                "running": False,
                "pid": os.getpid(),
                "updated_at": _iso_now(),
                "detail": f"{type(exc).__name__}: {exc}",
            },
        )
        _append_stream_event("stream_error", {"detail": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        if STREAM_PID_FILE.exists():
            try:
                if _pid_from_file(STREAM_PID_FILE) == os.getpid():
                    STREAM_PID_FILE.unlink()
            except OSError:
                pass
