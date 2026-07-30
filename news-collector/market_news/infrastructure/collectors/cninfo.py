from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import re
from urllib.parse import parse_qs, urljoin, urlparse

from market_news.common import parse_datetime
from market_news.domain.models import Market, RawNewsRecord
from market_news.infrastructure.http import UrllibHttpClient


@dataclass(slots=True)
class CNInfoSpec:
    source_id: str
    name: str
    url: str = "https://www.cninfo.com.cn/"
    source_trust: float = 0.99
    item_limit: int = 12
    language: str = "zh"


class CNInfoLatestAnnouncementsCollector:
    def __init__(self, http_client: UrllibHttpClient, spec: CNInfoSpec) -> None:
        self.http_client = http_client
        self.spec = spec
        self.name = spec.name

    def collect(self) -> list[RawNewsRecord]:
        response = self.http_client.get_text(self.spec.url)
        table_body = self._extract_latest_table(response.text)
        rows = re.findall(r"<tr>(.*?)</tr>", table_body, flags=re.DOTALL)
        records: list[RawNewsRecord] = []
        for row in rows[: self.spec.item_limit]:
            item = self._parse_row(row, response.url)
            if item is not None:
                records.append(item)
        return records

    def _extract_latest_table(self, html: str) -> str:
        match = re.search(
            r"最新公告.*?<table class=\"table jc-table jc-table3\">.*?<tbody>(?P<body>.*?)</tbody>",
            html,
            flags=re.DOTALL,
        )
        if not match:
            raise ValueError("Could not find the CNINFO latest announcements table.")
        return match.group("body")

    def _parse_row(self, row_html: str, base_url: str) -> RawNewsRecord | None:
        hrefs = re.findall(r'href="([^"]+)"', row_html)
        if len(hrefs) < 3:
            return None
        stock_code_match = re.search(r"stockCode=(\d+)", hrefs[0])
        stock_code = stock_code_match.group(1) if stock_code_match else ""
        name_match = re.search(r'<span title="([^"]+)">', row_html)
        stock_name = unescape(name_match.group(1).strip()) if name_match else ""
        title_match = re.search(r'<span class="ell" title="([^"]+)">', row_html)
        title = unescape(title_match.group(1).strip()) if title_match else ""
        detail_href = urljoin(base_url, hrefs[2])
        query = parse_qs(urlparse(detail_href).query)
        announcement_date = query.get("announcementTime", [""])[0]
        day_match = re.search(r">\s*(\d{2}-\d{2})\s*<", row_html)
        market = self._infer_market(stock_code)
        if not title:
            return None
        return RawNewsRecord(
            source_id=self.spec.source_id,
            external_id=query.get("announcementId", [detail_href])[0],
            title=title,
            summary=f"{stock_name} ({stock_code}) official disclosure" if stock_name else "CNINFO official disclosure",
            url=detail_href,
            published_at=parse_datetime(f"{announcement_date}T00:00:00+08:00" if announcement_date else None),
            language=self.spec.language,
            source_trust=self.spec.source_trust,
            entities=[value for value in [stock_name, stock_code] if value],
            themes=[],
            regions=["CN"],
            metadata={
                "stock_code": stock_code,
                "stock_name": stock_name,
                "instrument_market": market.value if market else Market.CN_A.value,
                "display_day": day_match.group(1) if day_match else "",
                "sectors": [],
            },
        )

    def _infer_market(self, stock_code: str) -> Market | None:
        if stock_code.startswith(("0", "3", "6")):
            return Market.CN_A
        return None

