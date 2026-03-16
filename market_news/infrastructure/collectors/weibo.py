from __future__ import annotations

from datetime import datetime
from html import unescape
import json
import logging
from pathlib import Path
import random
import re
import time
from typing import Any
from urllib.parse import quote

from market_news.common import ensure_utc, utcnow
from market_news.domain.models import RawNewsRecord
from market_news.exceptions import CookieExpiredError
from market_news.infrastructure.cookie_store import load_cookies, mark_cookie_expired
from market_news.infrastructure.http import UrllibHttpClient


logger = logging.getLogger(__name__)


class WeiboCollector:
    name = "weibo"

    def __init__(
        self,
        *,
        queries: list[str],
        cookie_path: Path,
        http_client: UrllibHttpClient,
        max_results_per_query: int = 20,
        sleep_range: tuple[float, float] = (0.4, 1.0),
        browser_executable_path: str | None = None,
        browser_timeout_ms: int = 15_000,
        browser_warmup_ms: int = 1_200,
    ) -> None:
        self.queries = list(queries)
        self.cookie_path = Path(cookie_path).expanduser()
        self.http_client = http_client
        self.max_results_per_query = max_results_per_query
        self.sleep_range = sleep_range
        self.browser_executable_path = browser_executable_path
        self.browser_timeout_ms = browser_timeout_ms
        self.browser_warmup_ms = browser_warmup_ms

    def collect(self) -> list[RawNewsRecord]:
        try:
            cookies = load_cookies(self.cookie_path)
        except FileNotFoundError:
            logger.warning("weibo cookie file missing: %s", self.cookie_path)
            return []
        except Exception as exc:
            logger.warning("weibo cookie file is invalid: %s", exc)
            return []

        records: list[RawNewsRecord] = []
        for index, query in enumerate(self.queries):
            if index > 0:
                time.sleep(random.uniform(*self.sleep_range))
            try:
                records.extend(self._fetch_query(query, cookies))
            except CookieExpiredError as exc:
                logger.warning("weibo cookie expired")
                mark_cookie_expired(self.cookie_path, reason=str(exc))
                return []
            except Exception as exc:
                logger.warning("weibo query failed: %s (%s)", query, exc)
        return records

    def check_session(self) -> tuple[bool, str]:
        # HTTP-only: don't spin up a browser just for a health check
        try:
            cookies = load_cookies(self.cookie_path)
            self._fetch_payload_http(self.queries[0] if self.queries else "半导体", cookies)
        except CookieExpiredError as exc:
            return False, str(exc)
        except FileNotFoundError:
            return False, f"cookie file missing: {self.cookie_path}"
        except Exception as exc:
            return False, str(exc)
        return True, "cookie accepted"

    def _fetch_query(self, query: str, cookies: dict[str, str], limit: int | None = None) -> list[RawNewsRecord]:
        # Try the lightweight HTTP path up to 2 times before paying the cost of a browser launch.
        # CookieExpiredError is an auth failure — retrying won't help, propagate immediately.
        last_http_exc: Exception | None = None
        for attempt in range(2):
            try:
                payload = self._fetch_payload_http(query, cookies)
                return self._records_from_payload(payload, limit=limit)
            except CookieExpiredError:
                raise
            except Exception as exc:
                last_http_exc = exc
                if attempt == 0:
                    wait = 1.5 + random.uniform(0, 1.0)
                    logger.debug("weibo HTTP attempt 1 failed (%s), retrying in %.1fs", exc, wait)
                    time.sleep(wait)

        logger.debug("weibo HTTP path failed twice (%s), falling back to browser", last_http_exc)
        payload = self._fetch_payload_browser(query, cookies)
        return self._records_from_payload(payload, limit=limit)

    def _fetch_payload_http(self, query: str, cookies: dict[str, str]) -> dict[str, Any]:
        url = (
            "https://m.weibo.cn/api/container/getIndex"
            f"?containerid=100103type%3D1%26q%3D{quote(query)}&page_type=searchall&page=1"
        )
        response = self.http_client.get_text(url, headers=self._headers(cookies))
        if response.url.startswith("https://passport.weibo.cn/"):
            raise CookieExpiredError("Weibo session redirected to login")
        payload = json.loads(response.text)
        if isinstance(payload, dict) and payload.get("ok") == 0:
            raise CookieExpiredError("Weibo API reported invalid session")
        return payload

    def _records_from_payload(self, payload: dict[str, Any], *, limit: int | None = None) -> list[RawNewsRecord]:
        cards = payload.get("data", {}).get("cards", [])
        records: list[RawNewsRecord] = []
        max_items = limit if limit is not None else self.max_results_per_query
        for card in cards:
            if not isinstance(card, dict) or int(card.get("card_type", 0) or 0) != 9:
                continue
            mblog = card.get("mblog") or {}
            if not isinstance(mblog, dict):
                continue
            body = self.clean_html(str(mblog.get("text") or ""))
            if not body:
                continue
            user = mblog.get("user") or {}
            followers = self._parse_count(user.get("followers_count", 0))
            user_id = str(user.get("id", "")).strip()
            post_id = str(mblog.get("id", "")).strip()
            reposts = self._parse_count(mblog.get("reposts_count", 0))
            comments = self._parse_count(mblog.get("comments_count", 0))
            likes = self._parse_count(mblog.get("attitudes_count", 0))
            records.append(
                RawNewsRecord(
                    source_id="weibo",
                    external_id=post_id or body[:40],
                    title=body[:120],
                    summary=body[:160],
                    body=body,
                    url=f"https://weibo.com/{user_id}/{post_id}" if user_id and post_id else "",
                    published_at=self.parse_weibo_date(str(mblog.get("created_at", ""))),
                    language="zh",
                    source_trust=self._weibo_trust(followers),
                    entities=[],
                    themes=[],
                    regions=["CN", "HK"],
                    metadata={
                        "followers": followers,
                        "reposts": reposts,
                        "comments": comments,
                        "likes": likes,
                        # discussion_count drives the social_signal in tech_block heat scoring
                        "discussion_count": reposts + comments,
                        "author": str(user.get("screen_name", "")).strip(),
                    },
                )
            )
            if len(records) >= max_items:
                break
        return records

    def _fetch_payload_browser(self, query: str, cookies: dict[str, str]) -> dict[str, Any]:
        from playwright.sync_api import sync_playwright

        api_url = (
            "https://m.weibo.cn/api/container/getIndex"
            f"?containerid=100103type%3D1%26q%3D{quote(query)}&page_type=searchall&page=1"
        )
        browser_path = self._resolve_browser_executable()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=browser_path,
            )
            try:
                context = browser.new_context()
                context.set_default_navigation_timeout(self.browser_timeout_ms)
                context.set_default_timeout(self.browser_timeout_ms)
                context.add_cookies(self._browser_cookies(cookies))
                page = context.new_page()
                page.goto("https://m.weibo.cn/", wait_until="domcontentloaded", timeout=self.browser_timeout_ms)
                page.wait_for_timeout(self.browser_warmup_ms)
                response = context.request.get(api_url, timeout=self.browser_timeout_ms)
                status = response.status
                text = self._decode_body(response.body())
            finally:
                browser.close()
        if status >= 400:
            raise CookieExpiredError(f"Weibo browser session failed with status {status}")
        payload = json.loads(text)
        if isinstance(payload, dict) and payload.get("ok") == 0:
            raise CookieExpiredError("Weibo browser session reported invalid login")
        return payload

    def _headers(self, cookies: dict[str, str]) -> dict[str, str]:
        return {
            "Referer": "https://m.weibo.cn/",
            "Accept": "application/json, text/plain, */*",
            "Cookie": "; ".join(f"{key}={value}" for key, value in cookies.items()),
        }

    def _resolve_browser_executable(self) -> str | None:
        if self.browser_executable_path:
            return self.browser_executable_path
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),  # macOS
            Path("/usr/bin/google-chrome"),          # Linux
            Path("/usr/bin/google-chrome-stable"),   # Linux (some distros)
            Path("/usr/bin/chromium-browser"),        # Debian/Ubuntu
            Path("/usr/bin/chromium"),                # Arch/Alpine
        ]
        for path in candidates:
            if path.exists():
                return str(path)
        return None  # fall back to Playwright's own bundled Chromium

    def _browser_cookies(self, cookies: dict[str, str]) -> list[dict[str, object]]:
        cookie_specs: list[dict[str, object]] = []
        for key, value in cookies.items():
            domains = [".weibo.com"]
            if key == "_T_WM":
                domains = [".weibo.cn"]
            for domain in domains:
                cookie_specs.append(
                    {
                        "name": key,
                        "value": value,
                        "domain": domain,
                        "path": "/",
                        "httpOnly": False,
                        "secure": True,
                    }
                )
        return cookie_specs

    @staticmethod
    def clean_html(text: str) -> str:
        cleaned = re.sub(r"<[^>]+>", "", text)
        cleaned = unescape(cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    @staticmethod
    def parse_weibo_date(value: str) -> datetime:
        try:
            return ensure_utc(datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y"))
        except ValueError:
            return utcnow()

    @staticmethod
    def _weibo_trust(followers: int) -> float:
        if followers >= 1_000_000:
            return 0.72
        if followers >= 100_000:
            return 0.62
        if followers >= 10_000:
            return 0.52
        return 0.38

    @staticmethod
    def _parse_count(value: object) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        multiplier = 1
        if text.endswith("万"):
            multiplier = 10_000
            text = text[:-1]
        elif text.endswith("亿"):
            multiplier = 100_000_000
            text = text[:-1]
        try:
            return int(float(text) * multiplier)
        except ValueError:
            digits = re.sub(r"[^\d.]", "", text)
            if not digits:
                return 0
            return int(float(digits) * multiplier)

    @staticmethod
    def _decode_body(body: bytes) -> str:
        for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
            try:
                return body.decode(encoding)
            except UnicodeDecodeError:
                continue
        return body.decode("utf-8", errors="replace")
