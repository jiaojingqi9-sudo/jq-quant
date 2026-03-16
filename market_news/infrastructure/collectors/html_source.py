from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
import re
from urllib.parse import urljoin, urlparse

from market_news.common import utcnow, unique_preserve
from market_news.domain.models import RawNewsRecord
from market_news.infrastructure.http import UrllibHttpClient


@dataclass(slots=True)
class HtmlSourceSpec:
    source_id: str
    name: str
    url: str
    source_trust: float
    language: str = "zh"
    regions: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    item_limit: int = 12
    detail_fetch_limit: int = 6
    include_link_patterns: list[str] = field(default_factory=list)
    exclude_link_patterns: list[str] = field(default_factory=list)
    include_title_patterns: list[str] = field(default_factory=list)
    exclude_title_patterns: list[str] = field(default_factory=list)
    body_container_patterns: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class _AnchorCandidate:
    title: str
    url: str


class HtmlListDetailCollector:
    def __init__(self, http_client: UrllibHttpClient, spec: HtmlSourceSpec) -> None:
        self.http_client = http_client
        self.spec = spec
        self.name = spec.name

    def collect(self) -> list[RawNewsRecord]:
        response = self._get_text_with_retry(self.spec.url)
        candidates = self._extract_candidates(response.text, response.url)
        records: list[RawNewsRecord] = []
        for index, candidate in enumerate(candidates[: self.spec.item_limit]):
            body = ""
            summary = ""
            published_at = utcnow()
            if index < self.spec.detail_fetch_limit:
                try:
                    detail = self._get_text_with_retry(candidate.url)
                except Exception:
                    detail = None
                if detail is not None:
                    body = self._extract_body(detail.text)
                    summary = self._build_summary(body)
                    published_at = self._extract_published_at(detail.text) or published_at

            record = RawNewsRecord(
                source_id=self.spec.source_id,
                external_id=candidate.url,
                title=candidate.title,
                summary=summary,
                body=body,
                url=candidate.url,
                published_at=published_at,
                language=self.spec.language,
                source_trust=self.spec.source_trust,
                entities=[],
                themes=list(self.spec.themes),
                regions=list(self.spec.regions),
                metadata=dict(self.spec.metadata),
            )
            records.append(record)
        return records

    def _get_text_with_retry(self, url: str):
        last_error: Exception | None = None
        for _ in range(2):
            try:
                return self.http_client.get_text(url)
            except Exception as exc:
                last_error = exc
        if last_error is None:
            raise RuntimeError(f"failed to fetch html source: {url}")
        raise last_error

    def _extract_candidates(self, html: str, base_url: str) -> list[_AnchorCandidate]:
        anchors = re.findall(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, flags=re.IGNORECASE | re.DOTALL)
        candidates: list[_AnchorCandidate] = []
        seen_urls: set[str] = set()
        for href, inner_html in anchors:
            absolute_url = urljoin(base_url, unescape(href.strip()))
            if absolute_url in seen_urls or not self._allowed_url(absolute_url):
                continue
            title = self._normalize_text(inner_html)
            if not title:
                continue
            if not self._allowed_title(title):
                continue
            seen_urls.add(absolute_url)
            candidates.append(_AnchorCandidate(title=title, url=absolute_url))
        return candidates

    def _allowed_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if any(re.search(pattern, url, flags=re.IGNORECASE) for pattern in self.spec.exclude_link_patterns):
            return False
        if self.spec.include_link_patterns and not any(
            re.search(pattern, url, flags=re.IGNORECASE)
            for pattern in self.spec.include_link_patterns
        ):
            return False
        return True

    def _allowed_title(self, title: str) -> bool:
        if len(title) < 8 or len(title) > 120:
            return False
        if any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in self.spec.exclude_title_patterns):
            return False
        if self.spec.include_title_patterns and not any(
            re.search(pattern, title, flags=re.IGNORECASE)
            for pattern in self.spec.include_title_patterns
        ):
            return False
        return True

    def _extract_body(self, html: str) -> str:
        cleaned = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
        cleaned = re.sub(r"<script\b.*?</script>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r"<style\b.*?</style>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)

        containers = []
        for pattern in self.spec.body_container_patterns:
            match = re.search(pattern, cleaned, flags=re.IGNORECASE | re.DOTALL)
            if match:
                containers.append(match.group(0))
        target_html = max(containers, key=len) if containers else cleaned

        paragraphs = [
            self._normalize_text(chunk)
            for chunk in re.findall(r"<p\b[^>]*>(.*?)</p>", target_html, flags=re.IGNORECASE | re.DOTALL)
        ]
        paragraphs = [
            paragraph
            for paragraph in paragraphs
            if len(paragraph) >= 16
        ]
        if not paragraphs:
            paragraphs = [
                self._normalize_text(chunk)
                for chunk in re.findall(r"<div\b[^>]*>(.*?)</div>", target_html, flags=re.IGNORECASE | re.DOTALL)
            ]
            paragraphs = [
                paragraph
                for paragraph in paragraphs
                if len(paragraph) >= 24
            ]
        return "\n".join(unique_preserve(paragraphs[:10]))

    def _build_summary(self, body: str) -> str:
        if not body:
            return ""
        parts = [
            line.strip()
            for line in body.splitlines()
            if line.strip()
            and not re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?", line.strip())
            and not re.fullmatch(r"\d{4}年\d{1,2}月\d{1,2}日(?:\s*\d{1,2}:\d{2}(?::\d{2})?)?", line.strip())
        ]
        summary = (parts[0] if parts else body.splitlines()[0]).strip()
        if len(summary) <= 160:
            return summary
        return summary[:157].rstrip() + "..."

    def _extract_published_at(self, html: str) -> datetime | None:
        patterns = [
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})",
            r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})",
            r"(\d{4}年\d{1,2}月\d{1,2}日(?:\s*\d{1,2}:\d{2}(?::\d{2})?)?)",
            r"published at (\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2} [AP]M)",
        ]
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE)
            if not match:
                continue
            parsed = self._parse_datetime_text(match.group(1))
            if parsed is not None:
                return parsed
        return None

    def _parse_datetime_text(self, value: str) -> datetime | None:
        text = value.strip()
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%m/%d/%Y %I:%M:%S %p",
        ]
        for pattern in formats:
            try:
                return datetime.strptime(text, pattern).replace(tzinfo=UTC)
            except ValueError:
                continue
        cn_match = re.match(
            r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日(?:\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?",
            text,
        )
        if cn_match:
            second = int(cn_match.group("second") or 0)
            return datetime(
                int(cn_match.group("year")),
                int(cn_match.group("month")),
                int(cn_match.group("day")),
                int(cn_match.group("hour") or 0),
                int(cn_match.group("minute") or 0),
                second,
                tzinfo=UTC,
            )
        return None

    def _normalize_text(self, html_fragment: str) -> str:
        text = re.sub(r"<[^>]+>", " ", html_fragment)
        text = unescape(text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
