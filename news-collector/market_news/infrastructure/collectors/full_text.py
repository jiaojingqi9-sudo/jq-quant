from __future__ import annotations

from dataclasses import dataclass, replace
from html import unescape
import io
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from market_news.domain.models import RawNewsRecord
from market_news.domain.ports import Collector
from market_news.infrastructure.http import UrllibHttpClient


logger = logging.getLogger(__name__)


DEFAULT_FULL_TEXT_SOURCES = {
    "cninfo_latest",
    "eastmoney-724",
    "eastmoney-focus",
    "eastmoney-news",
    "hkex_news",
    "sec_press",
    "sec_xbrl_usgaap",
    "reuters-tech",
}

GENERIC_CONTAINER_PATTERNS = [
    r"<article\b[^>]*>.*?</article>",
    r"<div[^>]+id=\"ContentBody\"[^>]*>.*?</div>",
    r"<div[^>]+class=\"[^\"]*(?:article|detail|content|正文|news-content|article-content|main-content|post-content)[^\"]*\"[^>]*>.*?</div>",
]

GENERIC_PARAGRAPH_TAGS = ["p", "li", "div"]


@dataclass(slots=True)
class _ExtractionResult:
    body: str
    summary: str
    metadata: dict[str, Any]


class FullTextEnrichingCollector:
    def __init__(
        self,
        base_collector: Collector,
        http_client: UrllibHttpClient,
        *,
        enabled_source_ids: list[str] | None = None,
        min_body_length: int = 220,
        max_records: int = 36,
        max_body_length: int = 24000,
    ) -> None:
        self.base_collector = base_collector
        self.http_client = http_client
        self.enabled_source_ids = set(enabled_source_ids or DEFAULT_FULL_TEXT_SOURCES)
        self.min_body_length = min_body_length
        self.max_records = max_records
        self.max_body_length = max_body_length
        self.name = base_collector.name

    @property
    def collectors(self) -> list[Collector]:
        return getattr(self.base_collector, "collectors", [])

    def collect(self) -> list[RawNewsRecord]:
        records = self.base_collector.collect()
        enriched: list[RawNewsRecord] = []
        remaining_budget = self.max_records
        for record in records:
            if remaining_budget <= 0 or not self._should_enrich(record):
                enriched.append(record)
                continue
            try:
                updated = self._enrich_record(record)
            except Exception as exc:
                logger.warning("full-text enrichment failed: %s (%s)", record.url or record.title, exc)
                updated = record
            if updated is not record:
                remaining_budget -= 1
            enriched.append(updated)
        return enriched

    def _should_enrich(self, record: RawNewsRecord) -> bool:
        if not record.url:
            return False
        if record.metadata.get("full_text_status") == "full":
            return False
        if record.source_id in {"weibo", "xueqiu", "cls", "gelonghui"}:
            return False
        if record.source_id in self.enabled_source_ids:
            return len(record.body.strip()) < self._target_body_length(record)
        return False

    def _target_body_length(self, record: RawNewsRecord) -> int:
        if record.source_id == "cninfo_latest":
            return max(self.min_body_length, 1800)
        if record.source_id == "eastmoney-ann":
            return max(self.min_body_length, 800)
        if record.source_id.startswith("eastmoney-"):
            return max(self.min_body_length, 400)
        if record.source_id in {"hkex_news", "sec_press", "sec_xbrl_usgaap", "reuters-tech"}:
            return max(self.min_body_length, 320)
        return self.min_body_length

    def _enrich_record(self, record: RawNewsRecord) -> RawNewsRecord:
        if record.source_id == "cninfo_latest":
            extraction = self._extract_cninfo_pdf(record)
        else:
            response = self.http_client.get_text(
                record.url,
                headers={"Referer": self._detail_referer(record.url)},
            )
            extraction = self._extract_from_html(record, response.text)
        if extraction is None:
            return record
        new_body = self._clip_text(extraction.body)
        if not new_body:
            return record
        old_body = record.body.strip()
        if old_body and len(new_body) + 40 < len(old_body):
            return record
        new_summary = extraction.summary.strip() or record.summary.strip() or record.title
        metadata = dict(record.metadata)
        metadata.update(extraction.metadata)
        metadata["full_text_status"] = "full"
        metadata["full_text_body_length"] = len(new_body)
        return replace(
            record,
            summary=new_summary,
            body=new_body,
            metadata=metadata,
        )

    def _extract_from_html(self, record: RawNewsRecord, html: str) -> _ExtractionResult | None:
        if record.source_id.startswith("eastmoney"):
            result = self._extract_eastmoney_html(html)
            if result is not None:
                return result
        result = self._extract_generic_html(html)
        if result is not None:
            return result
        return None

    def _extract_eastmoney_html(self, html: str) -> _ExtractionResult | None:
        container = self._first_match(
            html,
            [
                r"<div[^>]+id=\"ContentBody\"[^>]*>.*?</div>",
                r"<div[^>]+class=\"txtinfos\"[^>]*>.*?</div>",
            ],
        )
        body = self._extract_text_block(container or html)
        summary = self._extract_meta_content(html, "description")
        if not body and not summary:
            return None
        if not summary:
            summary = self._build_summary(body)
        return _ExtractionResult(
            body=body or summary,
            summary=summary,
            metadata={"full_text_method": "eastmoney-html"},
        )

    def _extract_generic_html(self, html: str) -> _ExtractionResult | None:
        body = ""
        for pattern in GENERIC_CONTAINER_PATTERNS:
            match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            candidate = self._extract_text_block(match.group(0))
            if len(candidate) > len(body):
                body = candidate
        if not body:
            body = self._extract_text_block(html)
        summary = self._extract_meta_content(html, "description") or self._build_summary(body)
        if len(body) < 80 and not summary:
            return None
        if not body:
            body = summary
        return _ExtractionResult(
            body=body,
            summary=summary,
            metadata={"full_text_method": "generic-html"},
        )

    def _extract_cninfo_pdf(self, record: RawNewsRecord) -> _ExtractionResult | None:
        parsed = urlparse(record.url)
        query = parse_qs(parsed.query)
        announce_id = (query.get("announcementId") or [""])[0]
        announce_time = (query.get("announcementTime") or [""])[0]
        stock_code = (query.get("stockCode") or [""])[0]
        if not announce_id or not announce_time:
            return None
        flag = "true" if stock_code.startswith(("0", "2", "3")) else "false"
        endpoint = "https://www.cninfo.com.cn/new/announcement/bulletin_detail"
        response = self.http_client.post_text(
            f"{endpoint}?{urlencode({'announceId': announce_id, 'flag': flag, 'announceTime': announce_time})}",
            headers={
                "Referer": record.url,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
            },
            data=b"",
        )
        payload = json.loads(response.text)
        pdf_url = str(payload.get("fileUrl") or "").strip()
        announcement = payload.get("announcement", {}) if isinstance(payload, dict) else {}
        if not pdf_url and isinstance(announcement, dict):
            adjunct = str(announcement.get("adjunctUrl") or "").strip()
            if adjunct:
                pdf_url = f"https://static.cninfo.com.cn/{adjunct.lstrip('/')}"
        if not pdf_url:
            return None
        pdf_response = self.http_client.get_text(
            pdf_url,
            headers={"Referer": record.url, "Accept": "application/pdf, */*"},
            encoding="latin-1",
        )
        body = self._extract_pdf_text(pdf_response.body)
        if not body:
            return None
        title = str(announcement.get("announcementTitle") or record.title).strip()
        summary = self._build_summary(body) or title
        return _ExtractionResult(
            body=body,
            summary=summary,
            metadata={
                "full_text_method": "cninfo-pdf",
                "pdf_url": pdf_url,
            },
        )

    def _extract_pdf_text(self, pdf_bytes: bytes) -> str:
        try:
            from pypdf import PdfReader
        except ImportError:
            logger.warning("pypdf is not installed; CNInfo PDF full-text extraction is disabled.")
            return ""
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages: list[str] = []
        for page in reader.pages[:80]:
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(text)
        normalized = self._normalize_plain_text("\n".join(pages))
        return self._clip_text(normalized)

    def _extract_text_block(self, html: str) -> str:
        cleaned = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
        cleaned = re.sub(r"<script\b.*?</script>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r"<style\b.*?</style>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
        chunks: list[str] = []
        for tag in GENERIC_PARAGRAPH_TAGS:
            pattern = rf"<{tag}\b[^>]*>(.*?)</{tag}>"
            for fragment in re.findall(pattern, cleaned, flags=re.IGNORECASE | re.DOTALL):
                text = self._normalize_html_text(fragment)
                if len(text) >= 20:
                    chunks.append(text)
        if not chunks:
            text = self._normalize_html_text(cleaned)
            if len(text) >= 80:
                chunks.append(text)
        unique_chunks: list[str] = []
        seen: set[str] = set()
        for chunk in chunks:
            key = chunk.lower()
            if key in seen:
                continue
            seen.add(key)
            unique_chunks.append(chunk)
        return self._clip_text("\n".join(unique_chunks))

    def _extract_meta_content(self, html: str, name: str) -> str:
        patterns = [
            rf"<meta[^>]+name=[\"']{name}[\"'][^>]+content=[\"'](.*?)[\"']",
            rf"<meta[^>]+property=[\"']og:{name}[\"'][^>]+content=[\"'](.*?)[\"']",
        ]
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            return self._normalize_html_text(match.group(1))
        return ""

    def _build_summary(self, body: str) -> str:
        for line in body.splitlines():
            stripped = line.strip()
            if len(stripped) >= 16:
                if len(stripped) <= 160:
                    return stripped
                return stripped[:157].rstrip() + "..."
        return ""

    def _normalize_html_text(self, fragment: str) -> str:
        text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.IGNORECASE)
        text = re.sub(r"</?(?:p|div|li|ul|ol|article|section|span|strong|em)\b[^>]*>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = unescape(text).replace("\xa0", " ")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        lines = [re.sub(r"\s+", " ", part).strip() for part in text.splitlines()]
        lines = [line for line in lines if line]
        return "\n".join(lines)

    def _normalize_plain_text(self, value: str) -> str:
        lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
        lines = [line for line in lines if line]
        return "\n".join(lines)

    def _clip_text(self, value: str) -> str:
        text = value.strip()
        if len(text) <= self.max_body_length:
            return text
        return text[: self.max_body_length].rstrip() + "..."

    def _first_match(self, text: str, patterns: list[str]) -> str:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(0)
        return ""

    def _detail_referer(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else ""
