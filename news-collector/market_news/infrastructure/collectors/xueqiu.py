from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from html import unescape
import logging
from pathlib import Path
import re

from market_news.common import utcnow
from market_news.domain.models import RawNewsRecord
from market_news.exceptions import CookieExpiredError
from market_news.infrastructure.cookie_store import load_cookies, mark_cookie_expired
from market_news.infrastructure.http import UrllibHttpClient


logger = logging.getLogger(__name__)


class XueqiuCollector:
    name = "xueqiu"

    def __init__(
        self,
        *,
        queries: list[str],
        cookie_path: Path,
        http_client: UrllibHttpClient,
        max_results_per_query: int = 20,
        browser_executable_path: str | None = None,
        browser_timeout_ms: int = 15_000,
        browser_warmup_ms: int = 8_000,
    ) -> None:
        self.queries = list(queries)
        self.cookie_path = Path(cookie_path).expanduser()
        self.http_client = http_client
        self.max_results_per_query = max_results_per_query
        self.browser_executable_path = browser_executable_path
        self.browser_timeout_ms = browser_timeout_ms
        self.browser_warmup_ms = browser_warmup_ms

    def collect(self) -> list[RawNewsRecord]:
        try:
            cookies = load_cookies(self.cookie_path)
        except FileNotFoundError:
            logger.warning("xueqiu cookie file missing: %s", self.cookie_path)
            return []
        except Exception as exc:
            logger.warning("xueqiu cookie file is invalid: %s", exc)
            return []

        # Retry browser launch once on transient failures (network blip, browser crash).
        # CookieExpiredError is an auth failure — no point retrying.
        html: str = ""
        for attempt in range(2):
            try:
                html = self._fetch_homepage_html_browser(cookies)
                break
            except CookieExpiredError as exc:
                logger.warning("xueqiu cookie expired")
                mark_cookie_expired(self.cookie_path, reason=str(exc))
                return []
            except Exception as exc:
                if attempt == 0:
                    logger.debug("xueqiu browser attempt 1 failed (%s), retrying", exc)
                    continue
                logger.warning("xueqiu homepage scrape failed after 2 attempts: %s", exc)
                return []

        try:
            records = self._records_from_homepage_html(html)
        except Exception as exc:
            logger.warning("xueqiu HTML parsing failed: %s", exc)
            return []

        if not records and html:
            logger.warning(
                "xueqiu: %d bytes HTML but 0 records parsed — "
                "possible HTML structure change, check _records_from_homepage_html",
                len(html),
            )
        return self._filter_records(records)

    def check_session(self) -> tuple[bool, str]:
        try:
            cookies = load_cookies(self.cookie_path)
            html = self._fetch_homepage_html_browser(cookies)
            records = self._records_from_homepage_html(html, limit=1)
            if not records:
                return False, "Xueqiu homepage rendered without timeline items"
        except CookieExpiredError as exc:
            return False, str(exc)
        except FileNotFoundError:
            return False, f"cookie file missing: {self.cookie_path}"
        except Exception as exc:
            return False, str(exc)
        return True, "cookie accepted"

    def _fetch_homepage_html_browser(self, cookies: dict[str, str]) -> str:
        from playwright.sync_api import sync_playwright

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
                page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=self.browser_timeout_ms)
                page.wait_for_timeout(self.browser_warmup_ms)
                html = page.content()
            finally:
                browser.close()

        # Primary check: login wall means cookie is expired or missing
        # These strings appear in the redirect / login page, not the logged-in homepage
        login_signals = (
            'action="/login"',
            'href="/login"',
            '"isLogin":false',
            '"logged_in":false',
            "请先登录才能",
            "登录雪球",
        )
        if any(sig in html for sig in login_signals):
            raise CookieExpiredError("Xueqiu session requires login")

        # Secondary check: did the feed section actually render?
        # xueqiu injects timeline content server-side, so absence = cookie/render failure
        # Accept multiple possible class name patterns (CSS Modules hashing may vary)
        feed_signals = (
            "timeline__item",   # covers style_timeline__item and any variant
            "xq-article",       # alternative card class used in some page variants
            '"feed_type"',      # embedded JSON feed marker
        )
        if not any(sig in html for sig in feed_signals):
            logger.warning("xueqiu: timeline content not found in HTML — treating as auth failure")
            raise CookieExpiredError("Xueqiu homepage did not expose timeline content")
        return html

    def _filter_records(self, records: list[RawNewsRecord]) -> list[RawNewsRecord]:
        if not records:
            return []
        terms = self._query_terms()
        if not terms:
            return records[: self.max_results_per_query]

        matched: list[tuple[int, RawNewsRecord]] = []
        for record in records:
            haystack = "\n".join(
                part.lower() for part in (record.title, record.summary, record.body) if part
            )
            hits = [term for term in terms if term.lower() in haystack]
            if not hits:
                continue
            record.metadata["query_hits"] = hits
            record.source_trust = min(record.source_trust + 0.02 * len(hits), 0.78)
            matched.append((len(hits), record))

        matched.sort(key=lambda item: (-item[0], -item[1].published_at.timestamp()))
        if matched:
            return [record for _, record in matched[: self.max_results_per_query]]
        # No query terms matched any record — return nothing rather than injecting noise
        logger.debug("xueqiu: homepage feed contained %d records but none matched query terms", len(records))
        return []

    def _query_terms(self) -> list[str]:
        terms: list[str] = []
        for query in self.queries:
            query = query.strip()
            if not query:
                continue
            terms.append(query)
            for token in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{2,}", query):
                if token not in terms:
                    terms.append(token)
        return terms

    def _records_from_homepage_html(self, html: str, *, limit: int | None = None) -> list[RawNewsRecord]:
        records: list[RawNewsRecord] = []
        max_items = limit if limit is not None else self.max_results_per_query
        for block in re.findall(r"<article\b.*?</article>", html, flags=re.S):
            title_match = re.search(r"<h3[^>]*>(.*?)</h3>", block, flags=re.S)
            summary_match = re.search(
                r"<div[^>]+class=\"[^\"]*content--description[^\"]*\"[^>]*>(.*?)</div>",
                block,
                flags=re.S,
            )
            if not title_match and not summary_match:
                continue

            href_match = re.search(r"href=\"(/[^\"]+/[^\"]+)\"", block)
            author_match = re.search(r"data-screenname=\"([^\"]+)\"", block)
            meta_match = re.search(
                r"<a[^>]+class=\"[^\"]*date-and-source[^\"]*\"[^>]*>(.*?)</a>",
                block,
                flags=re.S,
            )

            title = self.clean_html(title_match.group(1) if title_match else "")
            summary = self.clean_html(summary_match.group(1) if summary_match else "")
            if not title and not summary:
                continue

            href = href_match.group(1) if href_match else ""
            external_id = href.rsplit("/", 1)[-1] if href else (title or summary)[:40]
            author = unescape(author_match.group(1)).strip() if author_match else ""
            meta_text = self.clean_html(meta_match.group(1) if meta_match else "")
            published_label, origin = self._split_meta(meta_text)
            body_parts = [part for part in (title, summary) if part]
            records.append(
                RawNewsRecord(
                    source_id="xueqiu",
                    external_id=external_id,
                    title=title or summary[:100],
                    summary=summary[:160] if summary else title[:160],
                    body="\n".join(body_parts)[:4000],
                    url=f"https://xueqiu.com{href}" if href else "https://xueqiu.com/",
                    published_at=self._parse_relative_timestamp(published_label),
                    language="zh",
                    source_trust=0.58,
                    entities=[],
                    themes=[],
                    regions=["CN", "HK"],
                    metadata={
                        "author": author,
                        "published_label": published_label,
                        "origin": origin,
                        "feed": "homepage-hot",
                        # Best-effort: xueqiu embeds interaction counts in data-* attributes
                        # or inline JSON within the article block.  Falls back to 0 gracefully
                        # so tech_block heat scoring degrades cleanly when not present.
                        "discussion_count": self._extract_discussion_count(block),
                    },
                )
            )
            if len(records) >= max_items:
                break
        return records

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

    @staticmethod
    def _browser_cookies(cookies: dict[str, str]) -> list[dict[str, object]]:
        cookie_specs: list[dict[str, object]] = []
        for key, value in cookies.items():
            cookie_specs.append(
                {
                    "name": key,
                    "value": value,
                    "domain": ".xueqiu.com",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                }
            )
        return cookie_specs

    @staticmethod
    def clean_html(text: str) -> str:
        cleaned = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        cleaned = unescape(cleaned)
        cleaned = cleaned.replace("\xa0", " ")
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        return cleaned.strip()

    @staticmethod
    def _split_meta(text: str) -> tuple[str, str]:
        if "·" not in text:
            return text.strip(), ""
        left, right = text.split("·", 1)
        return left.strip(), right.strip()

    @staticmethod
    def _extract_discussion_count(block: str) -> int:
        """Return like + comment + repost counts from a single article HTML block.

        Xueqiu may change its HTML structure at any time.  We try several known
        patterns and return 0 (not raise) if none match — the caller treats 0 as
        "no social signal available" and continues normally.
        """
        total = 0

        # Pattern A: data-* attributes on the article element or child nodes
        # e.g. data-like-count="128" data-comment-count="34" data-repost-count="12"
        for attr in ("data-like-count", "data-comment-count", "data-repost-count",
                     "data-likes", "data-comments", "data-reposts"):
            m = re.search(rf'{attr}="(\d+)"', block)
            if m:
                total += int(m.group(1))

        if total:
            return total

        # Pattern B: embedded JSON snippet — {"like_count":123,"reply_count":45,...}
        # Xueqiu sometimes server-side-renders a small JSON blob per card
        for key in ("like_count", "reply_count", "retweet_count",
                    "comment_count", "liked_count"):
            m = re.search(rf'"{key}"\s*:\s*(\d+)', block)
            if m:
                total += int(m.group(1))

        return total

    @staticmethod
    def _parse_relative_timestamp(value: str) -> datetime:
        _CST = timezone(timedelta(hours=8))  # xueqiu displays times in China Standard Time

        text = value.strip()
        if not text:
            return utcnow()
        for prefix in ("修改于", "发表于"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()

        now_utc = datetime.now(tz=UTC)

        # Relative expressions: "X分钟/小时/天前" — relative to wall clock, timezone-independent
        minute_match = re.fullmatch(r"(\d+)\s*分钟前", text)
        if minute_match:
            return now_utc - timedelta(minutes=int(minute_match.group(1)))

        hour_match = re.fullmatch(r"(\d+)\s*小时前", text)
        if hour_match:
            return now_utc - timedelta(hours=int(hour_match.group(1)))

        day_match = re.fullmatch(r"(\d+)\s*天前", text)
        if day_match:
            return now_utc - timedelta(days=int(day_match.group(1)))

        # Absolute expressions below are displayed in CST — parse in CST then convert to UTC
        now_cst = datetime.now(tz=_CST)

        yesterday_match = re.fullmatch(r"昨天\s*(\d{1,2}:\d{2})", text)
        if yesterday_match:
            hour, minute = yesterday_match.group(1).split(":")
            candidate = (now_cst - timedelta(days=1)).replace(
                hour=int(hour),
                minute=int(minute),
                second=0,
                microsecond=0,
            )
            return candidate.astimezone(UTC)

        month_day_match = re.fullmatch(r"(\d{2})-(\d{2})\s*(\d{1,2}:\d{2})", text)
        if month_day_match:
            month = int(month_day_match.group(1))
            day = int(month_day_match.group(2))
            hour, minute = month_day_match.group(3).split(":")
            try:
                candidate = now_cst.replace(
                    month=month,
                    day=day,
                    hour=int(hour),
                    minute=int(minute),
                    second=0,
                    microsecond=0,
                )
            except ValueError:
                return now_utc
            # If the resulting CST time is more than 1 day in the future, it's last year
            if candidate > now_cst + timedelta(days=1):
                candidate = candidate.replace(year=candidate.year - 1)
            return candidate.astimezone(UTC)

        return now_utc
