from __future__ import annotations

from datetime import UTC, datetime
import unittest

from market_news.domain.models import NewsDocument
from market_news.services.clustering import KeywordEventClusterer


def _document(
    title: str,
    *,
    doc_id: str,
    source_id: str = "gov-demo",
    themes: list[str] | None = None,
) -> NewsDocument:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    return NewsDocument(
        doc_id=doc_id,
        source_id=source_id,
        title=title,
        summary="",
        body="",
        url=f"https://example.com/{doc_id}",
        published_at=now,
        fetched_at=now,
        language="zh",
        source_trust=0.95,
        canonical_key=doc_id,
        entities=[],
        themes=themes or ["technology", "policy"],
        regions=["CN"],
        metadata={},
    )


class ClusteringTest(unittest.TestCase):
    def test_exact_chinese_titles_cluster_across_sources(self) -> None:
        clusterer = KeywordEventClusterer()
        documents = [
            _document("国家航天局召开商业航天高质量发展企业圆桌会议", doc_id="cnsa", source_id="gov-cnsa"),
            _document("国家航天局召开商业航天高质量发展企业圆桌会议", doc_id="eastmoney", source_id="eastmoney-724"),
        ]

        clusters = clusterer.cluster(documents)

        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].doc_count, 2)

    def test_generic_policy_themes_do_not_merge_unrelated_chinese_titles(self) -> None:
        clusterer = KeywordEventClusterer()
        documents = [
            _document("国家航天局召开商业航天高质量发展企业圆桌会议", doc_id="space"),
            _document("出口商品技术指南", doc_id="trade", source_id="gov-mofcom", themes=["policy", "trade"]),
        ]

        clusters = clusterer.cluster(documents)

        self.assertEqual(len(clusters), 2)


if __name__ == "__main__":
    unittest.main()
