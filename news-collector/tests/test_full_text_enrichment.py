from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import unittest
from unittest.mock import patch

from market_news.domain.models import RawNewsRecord
from market_news.infrastructure.collectors.full_text import FullTextEnrichingCollector


@dataclass
class _FakeResponse:
    url: str
    text: str
    body: bytes = b""
    status: int = 200
    headers: dict[str, str] | None = None


class _FakeHttpClient:
    def __init__(self, pages: dict[tuple[str, str], _FakeResponse]) -> None:
        self.pages = pages

    def get_text(self, url: str, **_: object) -> _FakeResponse:
        key = ("GET", url)
        if key not in self.pages:
            raise FileNotFoundError(url)
        return self.pages[key]

    def post_text(self, url: str, **_: object) -> _FakeResponse:
        key = ("POST", url)
        if key not in self.pages:
            raise FileNotFoundError(url)
        return self.pages[key]


class _FakeCollector:
    name = "fake-live"

    def __init__(self, records: list[RawNewsRecord]) -> None:
        self._records = records

    def collect(self) -> list[RawNewsRecord]:
        return list(self._records)


class FullTextEnrichmentTest(unittest.TestCase):
    def test_eastmoney_records_are_upgraded_to_full_article_text(self) -> None:
        record = RawNewsRecord(
            source_id="eastmoney-724",
            title="AI服务器景气度持续上行",
            summary="多家厂商扩产。",
            body="多家厂商扩产。",
            url="https://finance.eastmoney.com/a/202603150001.html",
            published_at=datetime(2026, 3, 15, 11, 0, tzinfo=UTC),
            language="zh",
            source_trust=0.82,
        )
        html = """
        <html>
          <head>
            <meta name="description" content="AI服务器订单持续放量，液冷与高速连接环节同步受益。">
          </head>
          <body>
            <div class="txtinfos" id="ContentBody">
              <p>AI服务器订单持续放量，液冷与高速连接环节同步受益。</p>
              <p>多家厂商表示客户拉货节奏正在加快，算力基础设施投资继续提升。</p>
            </div>
          </body>
        </html>
        """
        collector = FullTextEnrichingCollector(
            _FakeCollector([record]),
            _FakeHttpClient(
                {
                    ("GET", record.url): _FakeResponse(url=record.url, text=html),
                }
            ),
            enabled_source_ids=["eastmoney-724"],
            min_body_length=40,
        )

        [enriched] = collector.collect()

        self.assertIn("液冷与高速连接环节同步受益", enriched.body)
        self.assertGreater(len(enriched.body), len(record.body))
        self.assertEqual(enriched.metadata["full_text_method"], "eastmoney-html")
        self.assertEqual(enriched.metadata["full_text_status"], "full")

    def test_generic_rss_detail_page_is_used_when_feed_only_has_summary(self) -> None:
        record = RawNewsRecord(
            source_id="hkex_news",
            title="HKEX launches new AI market data service",
            summary="Launch notice.",
            body="",
            url="https://example.com/hkex/ai-service",
            published_at=datetime(2026, 3, 15, 9, 0, tzinfo=UTC),
            language="en",
            source_trust=0.97,
        )
        html = """
        <html>
          <head>
            <meta name="description" content="HKEX says the new service improves structured access to AI-linked market data.">
          </head>
          <body>
            <article>
              <p>HKEX says the new service improves structured access to AI-linked market data.</p>
              <p>The rollout starts with institutional clients and may expand to derivatives analytics later this year.</p>
            </article>
          </body>
        </html>
        """
        collector = FullTextEnrichingCollector(
            _FakeCollector([record]),
            _FakeHttpClient(
                {
                    ("GET", record.url): _FakeResponse(url=record.url, text=html),
                }
            ),
            enabled_source_ids=["hkex_news"],
            min_body_length=40,
        )

        [enriched] = collector.collect()

        self.assertIn("institutional clients", enriched.body)
        self.assertEqual(enriched.metadata["full_text_method"], "generic-html")
        self.assertEqual(enriched.metadata["full_text_status"], "full")

    def test_cninfo_records_can_be_upgraded_from_pdf_notice(self) -> None:
        record = RawNewsRecord(
            source_id="cninfo_latest",
            title="麦迪科技关于2025年度利润分配预案的公告",
            summary="麦迪科技 (603990) official disclosure",
            body="",
            url="https://www.cninfo.com.cn/new/disclosure/detail?stockCode=603990&orgId=9900029594&announcementId=1225015442&announcementTime=2026-03-18",
            published_at=datetime(2026, 3, 18, 0, 0, tzinfo=UTC),
            language="zh",
            source_trust=0.99,
            entities=["麦迪科技", "603990"],
        )
        api_url = "https://www.cninfo.com.cn/new/announcement/bulletin_detail?announceId=1225015442&flag=false&announceTime=2026-03-18"
        pdf_url = "http://static.cninfo.com.cn/finalpage/2026-03-18/1225015442.PDF"
        api_payload = """
        {
          "announcement": {
            "announcementTitle": "麦迪科技关于2025年度利润分配预案的公告",
            "adjunctUrl": "finalpage/2026-03-18/1225015442.PDF"
          },
          "fileUrl": "http://static.cninfo.com.cn/finalpage/2026-03-18/1225015442.PDF"
        }
        """
        collector = FullTextEnrichingCollector(
            _FakeCollector([record]),
            _FakeHttpClient(
                {
                    ("POST", api_url): _FakeResponse(url=api_url, text=api_payload, body=api_payload.encode("utf-8")),
                    ("GET", pdf_url): _FakeResponse(url=pdf_url, text="", body=b"%PDF-demo"),
                }
            ),
            enabled_source_ids=["cninfo_latest"],
            min_body_length=40,
        )

        with patch.object(
            FullTextEnrichingCollector,
            "_extract_pdf_text",
            return_value="公司拟以现金方式实施利润分配，每10股派发现金红利1.2元。",
        ):
            [enriched] = collector.collect()

        self.assertIn("每10股派发现金红利1.2元", enriched.body)
        self.assertEqual(enriched.metadata["full_text_method"], "cninfo-pdf")
        self.assertEqual(enriched.metadata["pdf_url"], pdf_url)


if __name__ == "__main__":
    unittest.main()
