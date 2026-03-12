from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html import unescape
import re
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

from market_news.common import ensure_utc, utcnow
from market_news.domain.models import RawNewsRecord
from market_news.infrastructure.http import UrllibHttpClient


@dataclass(slots=True)
class FeedSpec:
    source_id: str
    name: str
    url: str
    source_trust: float
    language: str = "en"
    regions: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    item_limit: int = 20
    include_title_patterns: list[str] = field(default_factory=list)
    exclude_title_patterns: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


class RSSCollector:
    def __init__(self, http_client: UrllibHttpClient, spec: FeedSpec) -> None:
        self.http_client = http_client
        self.spec = spec
        self.name = spec.name

    def collect(self) -> list[RawNewsRecord]:
        response = self.http_client.get_text(self.spec.url)
        root = ET.fromstring(response.text)
        records: list[RawNewsRecord] = []
        for item in self._iter_items(root):
            title = self._get_text(item, ["title"]).strip()
            if not title or not self._allowed(title):
                continue
            link = self._get_link(item, response.url)
            summary = self._get_text(item, ["description", "summary", "content"])
            published = self._parse_date(
                self._get_text(item, ["pubDate", "published", "updated"])
            )
            guid = self._get_text(item, ["guid", "id"])
            records.append(
                RawNewsRecord(
                    source_id=self.spec.source_id,
                    external_id=guid or link or title,
                    title=unescape(title),
                    summary=unescape(summary.strip()),
                    url=link,
                    published_at=published,
                    language=self.spec.language,
                    source_trust=self.spec.source_trust,
                    entities=[],
                    themes=list(self.spec.themes),
                    regions=list(self.spec.regions),
                    metadata=dict(self.spec.metadata),
                )
            )
            if len(records) >= self.spec.item_limit:
                break
        return records

    def _iter_items(self, root: ET.Element) -> list[ET.Element]:
        items = root.findall(".//item")
        if items:
            return items
        return root.findall(".//{http://www.w3.org/2005/Atom}entry")

    def _allowed(self, title: str) -> bool:
        if self.spec.include_title_patterns and not any(
            re.search(pattern, title, flags=re.IGNORECASE)
            for pattern in self.spec.include_title_patterns
        ):
            return False
        if any(
            re.search(pattern, title, flags=re.IGNORECASE)
            for pattern in self.spec.exclude_title_patterns
        ):
            return False
        return True

    def _get_text(self, item: ET.Element, tags: list[str]) -> str:
        for tag in tags:
            node = item.find(tag)
            if node is not None and node.text:
                return node.text
            for child in item:
                if child.tag.rsplit("}", 1)[-1] == tag and child.text:
                    return child.text
        return ""

    def _get_link(self, item: ET.Element, base_url: str) -> str:
        direct = self._get_text(item, ["link"])
        if direct:
            return urljoin(base_url, direct.strip())
        for child in item:
            if child.tag.rsplit("}", 1)[-1] != "link":
                continue
            href = child.attrib.get("href")
            if href:
                return urljoin(base_url, href.strip())
        return ""

    def _parse_date(self, value: str) -> object:
        if not value:
            return utcnow()
        try:
            return ensure_utc(parsedate_to_datetime(value))
        except (TypeError, ValueError):
            return utcnow()

