from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
import re

from market_news.common import stable_id, unique_preserve
from market_news.domain.models import RawNewsRecord
from market_news.infrastructure.http import UrllibHttpClient


@dataclass(slots=True)
class SFCOfferPeriodsSpec:
    source_id: str = "sfc-offer-periods"
    name: str = "sfc-offer-periods"
    url: str = "https://www.sfc.hk/en/Regulatory-functions/Corporates/Takeovers-and-mergers/offer-periods"
    source_trust: float = 0.99
    language: str = "en"
    item_limit: int = 10
    metadata: dict[str, object] = field(default_factory=dict)


class SFCOfferPeriodsCollector:
    name = "sfc-offer-periods"

    def __init__(self, http_client: UrllibHttpClient, spec: SFCOfferPeriodsSpec) -> None:
        self.http_client = http_client
        self.spec = spec
        self.name = spec.name

    def collect(self) -> list[RawNewsRecord]:
        response = self.http_client.get_text(self.spec.url, headers=self._headers())
        rows = self._extract_rows(response.text)
        records: list[RawNewsRecord] = []
        for row_html in rows[: self.spec.item_limit]:
            record = self._parse_row(row_html)
            if record is not None:
                records.append(record)
        return records

    def _extract_rows(self, html: str) -> list[str]:
        match = re.search(
            r'<tbody class="append-here"[^>]*>(?P<body>.*?)</tbody>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return []
        body = match.group("body")
        return re.findall(r"<tr>(.*?)</tr>", body, flags=re.IGNORECASE | re.DOTALL)

    def _parse_row(self, row_html: str) -> RawNewsRecord | None:
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, flags=re.IGNORECASE | re.DOTALL)
        if len(cells) < 6:
            return None

        offeree_company = self._clean_cell(cells[0])
        stock_code = self._clean_cell(cells[1]).replace(" ", "")
        offeror = self._clean_cell(cells[2])
        relevant_securities = self._clean_cell(cells[3])
        commencement_date = self._clean_cell(cells[4])
        announcement_date = self._clean_cell(cells[5])
        if not offeree_company or not stock_code:
            return None

        relevant_list = unique_preserve(
            part
            for part in re.split(r"[\n;/]+", relevant_securities)
            if part and part.strip()
        )
        title = f"Offer period: {offeree_company}"
        summary_bits = [
            f"Offeror: {offeror}" if offeror else "",
            f"Stock code: {stock_code}",
            f"Commenced: {commencement_date}" if commencement_date else "",
            f"Published: {announcement_date}" if announcement_date else "",
        ]
        summary = " | ".join(bit for bit in summary_bits if bit)
        body_lines = [
            f"Offeree company: {offeree_company}",
            f"Stock code: {stock_code}",
            f"Offeror: {offeror}" if offeror else "Offeror: n/a",
            f"Relevant securities: {relevant_securities}" if relevant_securities else "Relevant securities: n/a",
            f"Date of commencement of offer period: {commencement_date}" if commencement_date else "Date of commencement of offer period: n/a",
            f"Date of publication of announcement: {announcement_date}" if announcement_date else "Date of publication of announcement: n/a",
        ]
        published_at = self._parse_date(announcement_date) or self._parse_date(commencement_date)
        metadata = {
            "offer_period_kind": "takeovers-and-mergers",
            "offeree_company": offeree_company,
            "stock_code": stock_code,
            "offeror": offeror,
            "relevant_securities": relevant_list,
            "date_of_commencement": commencement_date,
            "date_of_publication": announcement_date,
            "instrument_market": "HK",
            "direct_codes": [stock_code],
        }
        metadata.update(self.spec.metadata)
        return RawNewsRecord(
            source_id=self.spec.source_id,
            external_id=stable_id(
                self.spec.source_id,
                stock_code,
                offeree_company,
                offeror,
                commencement_date,
                announcement_date,
            ),
            title=title,
            summary=summary or title,
            body="\n".join(body_lines),
            url=self.spec.url,
            published_at=published_at,
            language=self.spec.language,
            source_trust=self.spec.source_trust,
            entities=unique_preserve([offeree_company, stock_code, offeror, *relevant_list]),
            themes=["takeover", "m&a", "corporate-action"],
            regions=["HK"],
            metadata=metadata,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Referer": self.spec.url,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    def _clean_cell(self, html_fragment: str) -> str:
        text = unescape(html_fragment or "")
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        lines = [line for line in lines if line]
        return "\n".join(unique_preserve(lines)).strip()

    def _parse_date(self, value: str) -> datetime | None:
        text = value.strip()
        if not text:
            return None
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        return None
