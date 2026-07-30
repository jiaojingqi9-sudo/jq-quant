from __future__ import annotations

from datetime import UTC, datetime
from html import unescape
import json
import re

from market_news.common import utcnow
from market_news.domain.models import RawNewsRecord
from market_news.infrastructure.http import UrllibHttpClient


class GelonghuiLiveCollector:
    name = "gelonghui-hk"

    def __init__(
        self,
        http_client: UrllibHttpClient,
        *,
        item_limit: int = 10,
        source_trust: float = 0.76,
    ) -> None:
        self.http_client = http_client
        self.item_limit = item_limit
        self.source_trust = source_trust

    def collect(self) -> list[RawNewsRecord]:
        response = self.http_client.get_text("https://www.gelonghui.com/live", headers=self._headers())
        items = self._extract_items(response.text)
        records: list[RawNewsRecord] = []
        for item in items[: self.item_limit]:
            title = item["title"][:120]
            body = item["content"]
            records.append(
                RawNewsRecord(
                    source_id="gelonghui",
                    external_id=item["external_id"],
                    title=title,
                    summary=body[:160],
                    body=body,
                    url=item["url"],
                    published_at=item["published_at"],
                    language="zh",
                    source_trust=self.source_trust,
                    entities=[],
                    themes=[],
                    regions=["HK", "CN"],
                    metadata={"direct_codes": item["direct_codes"], "feed": "gelonghui-live"},
                )
            )
        return records

    def _extract_items(self, html: str) -> list[dict[str, object]]:
        matches = re.finditer(
            r'\{id:\d+,title:"(?:\\.|[^"]*)".*?route:"(?:\\.|[^"]*)".*?closeComment:[^}]+\}',
            html,
            flags=re.S,
        )
        items: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for match in matches:
            block = match.group(0)
            id_match = re.search(r'id:(\d+)', block)
            title_match = re.search(r'title:"((?:\\.|[^"])*)"', block)
            ts_match = re.search(r'createTimestamp:(\d+)', block)
            content_match = re.search(r'content:"((?:\\.|[^"])*)"', block)
            route_match = re.search(r'route:"((?:\\.|[^"])*)"', block)
            if not all([id_match, title_match, content_match, route_match]):
                continue
            external_id = id_match.group(1)
            if external_id in seen_ids:
                continue
            seen_ids.add(external_id)
            related_match = re.search(r'relatedStocks:(\[.*?\]|[a-z])', block, flags=re.S)
            related_stocks_raw = related_match.group(1) if related_match else "[]"
            items.append(
                {
                    "external_id": external_id,
                    "title": self._decode_js_string(title_match.group(1)),
                    "content": self._decode_js_string(content_match.group(1)),
                    "published_at": datetime.fromtimestamp(int(ts_match.group(1)) / 1000, tz=UTC)
                    if ts_match
                    else utcnow(),
                    "url": self._decode_js_string(route_match.group(1)),
                    "direct_codes": self._extract_codes(related_stocks_raw),
                }
            )
        return items

    def _headers(self) -> dict[str, str]:
        return {
            "Referer": "https://www.gelonghui.com/live",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    @staticmethod
    def _decode_js_string(value: str) -> str:
        decoded = json.loads(f'"{value}"')
        decoded = unescape(decoded).replace("\xa0", " ")
        decoded = re.sub(r"\s+", " ", decoded)
        return decoded.strip()

    @staticmethod
    def _extract_codes(raw: str) -> list[str]:
        return [code for code in re.findall(r'code:"(\d{4,6})"', raw) if code]
