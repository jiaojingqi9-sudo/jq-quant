from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import unittest

from market_news.domain.models import Direction, EventCluster, EventType, NewsDocument
from market_news.services.impact import ConfigDrivenImpactAnalyzer


class ImpactRulesTest(unittest.TestCase):
    def test_takeover_offer_period_matches_company_event(self) -> None:
        root = Path(__file__).resolve().parent.parent
        analyzer = ConfigDrivenImpactAnalyzer.from_file(root / "config" / "impact_rules.json")
        now = datetime(2026, 3, 18, 8, 0, tzinfo=UTC)
        document = NewsDocument(
            doc_id="doc-sfc",
            source_id="sfc-offer-periods",
            title="Offer period: Bright Smart Securities & Commodities Group Limited",
            summary="Offeror: Wealthiness and Prosperity Holding Limited | Stock code: 01428",
            body="Offeree company: Bright Smart Securities & Commodities Group Limited\nStock code: 01428\nOfferor: Wealthiness and Prosperity Holding Limited\nRelevant securities: Bright Smart Securities & Commodities Group Limited\nDate of commencement of offer period: 25 Apr 2025\nDate of publication of announcement: 25 Apr 2025",
            url="https://www.sfc.hk/en/Regulatory-functions/Corporates/Takeovers-and-mergers/offer-periods",
            published_at=now,
            fetched_at=now,
            language="en",
            source_trust=0.99,
            canonical_key="sfc-bright-smart",
            entities=["Bright Smart Securities & Commodities Group Limited", "01428", "Wealthiness and Prosperity Holding Limited"],
            themes=["takeover", "m&a", "corporate-action"],
            regions=["HK"],
            metadata={"stock_code": "01428", "instrument_market": "HK", "direct_codes": ["01428"]},
        )
        cluster = EventCluster(
            cluster_id="cluster-sfc",
            story_key="sfc-bright-smart",
            headline=document.title,
            summary=document.summary,
            documents=[document],
            entities=document.entities,
            themes=document.themes,
            sectors=[],
            regions=document.regions,
            source_ids=[document.source_id],
            first_seen_at=now,
            last_seen_at=now,
        )

        assessment = analyzer.assess(cluster)

        self.assertEqual(assessment.event_type, EventType.COMPANY)
        self.assertEqual(assessment.direction, Direction.POSITIVE)
        self.assertIn("Takeover and Offer Period", assessment.matched_rules)

    def test_social_commentary_does_not_trigger_takeover_rule(self) -> None:
        root = Path(__file__).resolve().parent.parent
        analyzer = ConfigDrivenImpactAnalyzer.from_file(root / "config" / "impact_rules.json")
        now = datetime(2026, 3, 18, 8, 0, tzinfo=UTC)
        document = NewsDocument(
            doc_id="doc-weibo",
            source_id="weibo",
            title="现在几个方向，还是要重视。一个就是长期价值投资的，跌多了的，市盈率比较低，行业成长性稳定的。",
            summary="一个是国产替代，行业有增量弹性比较高的，华虹这类。一个是远端超跌，一个是并购重组，中小票。",
            body="这类未来会变量很大，有比较大预期差。",
            url="https://weibo.com/example",
            published_at=now,
            fetched_at=now,
            language="zh",
            source_trust=0.58,
            canonical_key="weibo-commentary",
            entities=[],
            themes=[],
            regions=["CN"],
            metadata={},
        )
        cluster = EventCluster(
            cluster_id="cluster-weibo",
            story_key="weibo-commentary",
            headline=document.title,
            summary=document.summary,
            documents=[document],
            entities=document.entities,
            themes=document.themes,
            sectors=[],
            regions=document.regions,
            source_ids=[document.source_id],
            first_seen_at=now,
            last_seen_at=now,
        )

        assessment = analyzer.assess(cluster)

        self.assertEqual(assessment.event_type, EventType.UNKNOWN)
        self.assertEqual(assessment.direction, Direction.NEUTRAL)
        self.assertNotIn("Takeover and Offer Period", assessment.matched_rules)

    def test_routine_market_maker_notice_is_not_positive_catalyst(self) -> None:
        root = Path(__file__).resolve().parent.parent
        analyzer = ConfigDrivenImpactAnalyzer.from_file(root / "config" / "impact_rules.json")
        now = datetime(2026, 4, 27, 17, 36, tzinfo=UTC)
        document = NewsDocument(
            doc_id="doc-market-maker",
            source_id="sse_announcements",
            title="关于东方财富证券股份有限公司为国联安中证A500增强策略交易型开放式指数证券投资基金提供主做市服务的公告",
            summary="",
            body="交易所公告：证券公司为交易型开放式指数证券投资基金提供主做市服务。",
            url="https://www.sse.com.cn/example.shtml",
            published_at=now,
            fetched_at=now,
            language="zh",
            source_trust=0.98,
            canonical_key="market-maker-notice",
            entities=[],
            themes=["company", "market"],
            regions=["CN-A"],
            metadata={},
        )
        cluster = EventCluster(
            cluster_id="cluster-market-maker",
            story_key="market-maker-notice",
            headline=document.title,
            summary=document.summary,
            documents=[document],
            entities=document.entities,
            themes=document.themes,
            sectors=[],
            regions=document.regions,
            source_ids=[document.source_id],
            first_seen_at=now,
            last_seen_at=now,
        )

        assessment = analyzer.assess(cluster)

        self.assertEqual(assessment.event_type, EventType.UNKNOWN)
        self.assertEqual(assessment.direction, Direction.NEUTRAL)
        self.assertLess(assessment.severity, 0.2)
        self.assertIn("Routine Market Infrastructure", assessment.matched_rules)

    def test_customer_order_loss_matches_leading_fundamental_risk(self) -> None:
        root = Path(__file__).resolve().parent.parent
        analyzer = ConfigDrivenImpactAnalyzer.from_file(root / "config" / "impact_rules.json")
        now = datetime(2026, 4, 27, 21, 20, tzinfo=UTC)
        document = NewsDocument(
            doc_id="doc-order-loss",
            source_id="eastmoney-724",
            title="POET Technologies失去Celestial AI采购订单",
            summary="公司失去大客户采购订单，相关收入预期下修。",
            body="Celestial AI终止采购订单，公司未来订单和收入存在下滑风险。",
            url="https://example.com/order-loss",
            published_at=now,
            fetched_at=now,
            language="zh",
            source_trust=0.9,
            canonical_key="poet-order-loss",
            entities=["POET Technologies", "Celestial AI"],
            themes=["order-loss"],
            regions=["US"],
            metadata={},
        )
        cluster = EventCluster(
            cluster_id="cluster-order-loss",
            story_key="poet-order-loss",
            headline=document.title,
            summary=document.summary,
            documents=[document],
            entities=document.entities,
            themes=document.themes,
            sectors=[],
            regions=document.regions,
            source_ids=[document.source_id],
            first_seen_at=now,
            last_seen_at=now,
        )

        assessment = analyzer.assess(cluster)

        self.assertEqual(assessment.event_type, EventType.COMPANY)
        self.assertEqual(assessment.direction, Direction.NEGATIVE)
        self.assertIn("Customer Order Loss", assessment.matched_rules)

    def test_commercial_space_policy_matches_access_opening(self) -> None:
        root = Path(__file__).resolve().parent.parent
        analyzer = ConfigDrivenImpactAnalyzer.from_file(root / "config" / "impact_rules.json")
        now = datetime(2026, 4, 27, 15, 28, tzinfo=UTC)
        document = NewsDocument(
            doc_id="doc-cnsa-commercial-space",
            source_id="gov-cnsa",
            title="国家航天局召开商业航天高质量发展企业圆桌会议",
            summary="聚焦形成箭星场频网一体化发展合力，加快推进商业航天产业化发展。",
            body=(
                "围绕科研生产、许可准入、发射申请、频率协调、在轨运行、应用推广等方面研讨。"
                "建设商业航天公共服务平台，创新采用一站式审批模式，做到既放得活又管得住。"
            ),
            url="https://www.cnsa.gov.cn/n6758823/n6758838/c10744822/content.html",
            published_at=now,
            fetched_at=now,
            language="zh",
            source_trust=0.98,
            canonical_key="cnsa-commercial-space",
            entities=["国家航天局", "商业航天"],
            themes=["commercial-space"],
            regions=["CN"],
            metadata={"source_category": "official-policy"},
        )
        cluster = EventCluster(
            cluster_id="cluster-cnsa-commercial-space",
            story_key="cnsa-commercial-space",
            headline=document.title,
            summary=document.summary,
            documents=[document],
            entities=document.entities,
            themes=document.themes,
            sectors=[],
            regions=document.regions,
            source_ids=[document.source_id],
            first_seen_at=now,
            last_seen_at=now,
        )

        assessment = analyzer.assess(cluster)

        self.assertEqual(assessment.event_type, EventType.POLICY)
        self.assertEqual(assessment.direction, Direction.POSITIVE)
        self.assertIn("Commercial Space Access Opening", assessment.matched_rules)
        self.assertIn("satellite-internet", assessment.affected_themes)


if __name__ == "__main__":
    unittest.main()
