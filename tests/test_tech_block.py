from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import tempfile
from pathlib import Path
import unittest

from market_news.common import stable_id
from market_news.application.pipeline import MarketNewsPipeline
from market_news.infrastructure.collectors.local_json import LocalJSONCollector
from market_news.infrastructure.persistence.sqlite_store import SQLiteRunStore
from market_news.domain.models import (
    AlertItem,
    AlertLevel,
    Direction,
    EventCluster,
    EventType,
    ImpactAssessment,
    NewsDocument,
    PipelineSnapshot,
    RankedEvent,
)
from market_news.services.alerts import RuleBasedAlertEngine
from market_news.services.clustering import KeywordEventClusterer
from market_news.services.deduplication import FingerprintDeduplicator
from market_news.services.impact import ConfigDrivenImpactAnalyzer
from market_news.services.mapping import ConfigDrivenInstrumentMapper
from market_news.services.normalization import DefaultNormalizer
from market_news.services.ranking import WeightedEventRanker, WeightedInstrumentRanker
from market_news.services.reporting import MarkdownJsonReporter
from market_news.services.tech_block import AHShareTechFeatureBlock


class TechBlockTest(unittest.TestCase):
    def test_tech_block_surfaces_ah_tech_signals_and_assets(self) -> None:
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pipeline = MarketNewsPipeline(
                collector=LocalJSONCollector(root / "data" / "sample_news.json"),
                normalizer=DefaultNormalizer(),
                deduplicator=FingerprintDeduplicator(),
                clusterer=KeywordEventClusterer(),
                impact_analyzer=ConfigDrivenImpactAnalyzer.from_file(
                    root / "config" / "impact_rules.json"
                ),
                event_ranker=WeightedEventRanker(),
                instrument_mapper=ConfigDrivenInstrumentMapper.from_file(
                    root / "config" / "instrument_universe.json"
                ),
                instrument_ranker=WeightedInstrumentRanker(),
                alert_engine=RuleBasedAlertEngine(),
                store=SQLiteRunStore(temp_path / "market_news.db"),
                reporter=MarkdownJsonReporter(temp_path / "reports"),
                feature_modules=[
                    AHShareTechFeatureBlock.from_files(
                        universe_path=root / "config" / "tech_universe_cn_hk.json",
                        lexicon_path=root / "config" / "tech_lexicon.json",
                        lexicon_release_path=root / "config" / "tech_lexicon_release.json",
                        graph_path=root / "config" / "tech_impact_graph.json",
                    )
                ],
            )

            snapshot = pipeline.run()

            tech_block = snapshot.feature_blocks["tech_block"]
            self.assertGreaterEqual(tech_block["summary"]["signal_count"], 1)
            self.assertEqual(tech_block["summary"]["lexicon_version"], "2026.03-p2")
            self.assertGreaterEqual(len(tech_block["signals"]), 1)
            self.assertGreaterEqual(len(tech_block["themes"]), 1)
            self.assertGreaterEqual(len(tech_block["asset_ladder"]), 1)

            first_signal = tech_block["signals"][0]
            self.assertIn("trading_attention_score", first_signal)
            self.assertGreaterEqual(first_signal["spec_score"], 40)
            self.assertGreaterEqual(len(first_signal["matched_terms"]), 1)
            top_assets = {item["symbol"] for item in first_signal["candidate_assets"]}
            self.assertTrue({"688981.SH", "0981.HK", "300308.SZ"} & top_assets)

    def test_source_multiplier_rewards_official_sources_over_lower_quality_media(self) -> None:
        root = Path(__file__).resolve().parent.parent
        block = AHShareTechFeatureBlock.from_files(
            universe_path=root / "config" / "tech_universe_cn_hk.json",
            lexicon_path=root / "config" / "tech_lexicon.json",
            lexicon_release_path=root / "config" / "tech_lexicon_release.json",
            graph_path=root / "config" / "tech_impact_graph.json",
        )
        timestamp = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
        document = NewsDocument(
            doc_id=stable_id("doc", "deepseek"),
            source_id="xinhua-finance",
            title="DeepSeek 推理调用量爆发带动 AI 算力扩容",
            summary="大模型推理需求提升，服务器与光模块链持续受益。",
            body="DeepSeek、推理算力、光模块、服务器持续走强。",
            url="https://example.com/deepseek",
            published_at=timestamp,
            fetched_at=timestamp,
            language="zh",
            source_trust=0.95,
            canonical_key="deepseek-official",
        )
        cluster = EventCluster(
            cluster_id="cluster-deepseek",
            story_key="deepseek",
            headline=document.title,
            summary=document.summary,
            documents=[document],
            entities=["DeepSeek"],
            themes=["大模型", "AI算力"],
            sectors=["ai", "servers"],
            regions=["CN"],
            source_ids=["xinhua-finance"],
            first_seen_at=timestamp,
            last_seen_at=timestamp,
        )
        impact = ImpactAssessment(
            event_type=EventType.INDUSTRY,
            direction=Direction.POSITIVE,
            affected_markets=[],
            affected_sectors=["ai"],
            affected_themes=["model-applications", "ai-compute"],
            severity=0.82,
            confidence=0.86,
            matched_rules=["ai"],
            rationale=["tech demand"],
        )
        event = RankedEvent(
            cluster_id=cluster.cluster_id,
            headline=cluster.headline,
            impact=impact,
            heat_score=72.0,
            importance_score=70.0,
            confidence_score=80.0,
            market_relevance_score=75.0,
            final_score=74.0,
        )
        base_snapshot = PipelineSnapshot(
            run_id="run-tech",
            created_at=timestamp,
            source_name="test",
            raw_records=[],
            documents=[document],
            clusters=[cluster],
            ranked_events=[event],
            ranked_instruments=[],
            alerts=[
                AlertItem(
                    cluster_id=cluster.cluster_id,
                    headline=cluster.headline,
                    level=AlertLevel.HIGH,
                    direction=Direction.POSITIVE,
                    event_type=EventType.INDUSTRY,
                    final_score=74.0,
                    is_new=True,
                    symbols=[],
                    reason="test",
                )
            ],
        )
        official_score = block.evaluate(base_snapshot)["signals"][0]["trading_attention_score"]

        lower_quality_document = replace(document, source_id="huxiu", source_trust=0.71)
        lower_quality_cluster = replace(cluster, documents=[lower_quality_document], source_ids=["huxiu"])
        lower_quality_snapshot = replace(
            base_snapshot,
            documents=[lower_quality_document],
            clusters=[lower_quality_cluster],
        )
        lower_quality_score = block.evaluate(lower_quality_snapshot)["signals"][0]["trading_attention_score"]

        self.assertGreater(official_score, lower_quality_score)

    def test_clean_source_policy_blocks_social_only_signal_and_keeps_social_as_heat_only(self) -> None:
        root = Path(__file__).resolve().parent.parent
        block = AHShareTechFeatureBlock.from_files(
            universe_path=root / "config" / "tech_universe_cn_hk.json",
            lexicon_path=root / "config" / "tech_lexicon.json",
            lexicon_release_path=root / "config" / "tech_lexicon_release.json",
            graph_path=root / "config" / "tech_impact_graph.json",
            config_path=root / "config" / "tech_block.json",
        )
        timestamp = datetime(2026, 3, 17, 9, 0, tzinfo=UTC)
        weibo_doc = NewsDocument(
            doc_id=stable_id("doc", "weibo-tech"),
            source_id="weibo",
            title="算力链今天又爆了",
            summary="讨论聚焦AI算力与光模块。",
            body="AI算力、光模块、大模型链路被集中讨论。",
            url="https://weibo.com/demo/1",
            published_at=timestamp,
            fetched_at=timestamp,
            language="zh",
            source_trust=0.45,
            canonical_key="weibo-tech",
            metadata={"discussion_count": 320},
        )
        clean_doc = replace(
            weibo_doc,
            doc_id=stable_id("doc", "cls-tech"),
            source_id="cls",
            title="【财联社】算力产业链景气延续",
            summary="服务器、光模块和液冷环节景气延续。",
            body="AI算力、服务器、光模块和液冷链持续受益。",
            url="https://www.cls.cn/detail/123",
            source_trust=0.9,
            canonical_key="cls-tech",
            metadata={"discussion_count": 0},
        )
        impact = ImpactAssessment(
            event_type=EventType.INDUSTRY,
            direction=Direction.POSITIVE,
            affected_markets=[],
            affected_sectors=["ai"],
            affected_themes=["ai-compute"],
            severity=0.82,
            confidence=0.86,
            matched_rules=["ai"],
            rationale=["tech demand"],
        )
        event = RankedEvent(
            cluster_id="cluster-clean-tech",
            headline=clean_doc.title,
            impact=impact,
            heat_score=70.0,
            importance_score=68.0,
            confidence_score=80.0,
            market_relevance_score=76.0,
            final_score=73.0,
        )

        social_only_cluster = EventCluster(
            cluster_id="cluster-clean-tech",
            story_key="clean-tech",
            headline=weibo_doc.title,
            summary=weibo_doc.summary,
            documents=[weibo_doc],
            entities=[],
            themes=["AI算力"],
            sectors=["ai"],
            regions=["CN"],
            source_ids=["weibo"],
            first_seen_at=timestamp,
            last_seen_at=timestamp,
        )
        social_only_snapshot = PipelineSnapshot(
            run_id="run-social-only",
            created_at=timestamp,
            source_name="test",
            raw_records=[],
            documents=[weibo_doc],
            clusters=[social_only_cluster],
            ranked_events=[event],
            ranked_instruments=[],
            alerts=[],
        )
        self.assertEqual(block.evaluate(social_only_snapshot)["signals"], [])

        mixed_cluster = replace(
            social_only_cluster,
            headline=clean_doc.title,
            summary=clean_doc.summary,
            documents=[clean_doc, weibo_doc],
            source_ids=["cls", "weibo"],
        )
        mixed_snapshot = replace(
            social_only_snapshot,
            documents=[clean_doc, weibo_doc],
            clusters=[mixed_cluster],
        )
        signal = block.evaluate(mixed_snapshot)["signals"][0]
        self.assertEqual(signal["evidence_source_ids"], ["cls"])
        self.assertEqual(signal["social_source_ids"], ["weibo"])
        self.assertEqual(signal["source_quality"], "vetted-wire")

    def test_frontier_breakthrough_detection_surfaces_semicap_signal(self) -> None:
        root = Path(__file__).resolve().parent.parent
        block = AHShareTechFeatureBlock.from_files(
            universe_path=root / "config" / "tech_universe_cn_hk.json",
            lexicon_path=root / "config" / "tech_lexicon.json",
            lexicon_release_path=root / "config" / "tech_lexicon_release.json",
            graph_path=root / "config" / "tech_impact_graph.json",
            frontier_map_path=root / "config" / "tech_frontier_map.json",
        )
        timestamp = datetime(2026, 3, 17, 10, 0, tzinfo=UTC)
        document = NewsDocument(
            doc_id=stable_id("doc", "arf-breakthrough"),
            source_id="eastmoney-focus",
            title="国产ArF光刻胶突破量产",
            summary="光刻胶国产化率提升，半导体材料链受关注。",
            body="国产ArF光刻胶突破量产，电子特气国产替代提速，南大光电与晶瑞电材等半导体材料链受关注。",
            url="https://example.com/arf-breakthrough",
            published_at=timestamp,
            fetched_at=timestamp,
            language="zh",
            source_trust=0.88,
            canonical_key="arf-breakthrough",
        )
        cluster = EventCluster(
            cluster_id="cluster-arf-breakthrough",
            story_key="arf-breakthrough",
            headline=document.title,
            summary=document.summary,
            documents=[document],
            entities=["南大光电", "晶瑞电材"],
            themes=["半导体", "国产替代"],
            sectors=["semicap", "materials"],
            regions=["CN"],
            source_ids=["eastmoney-focus"],
            first_seen_at=timestamp,
            last_seen_at=timestamp,
        )
        impact = ImpactAssessment(
            event_type=EventType.INDUSTRY,
            direction=Direction.POSITIVE,
            affected_markets=[],
            affected_sectors=["半导体材料"],
            affected_themes=["semicap-equipment", "domestic-substitution"],
            severity=0.8,
            confidence=0.84,
            matched_rules=["semicap"],
            rationale=["国产替代与半导体材料突破"],
        )
        event = RankedEvent(
            cluster_id=cluster.cluster_id,
            headline=cluster.headline,
            impact=impact,
            heat_score=70.0,
            importance_score=76.0,
            confidence_score=82.0,
            market_relevance_score=78.0,
            final_score=75.0,
        )
        snapshot = PipelineSnapshot(
            run_id="run-frontier",
            created_at=timestamp,
            source_name="test",
            raw_records=[],
            documents=[document],
            clusters=[cluster],
            ranked_events=[event],
            ranked_instruments=[],
            alerts=[],
        )

        signal = block.evaluate(snapshot)["signals"][0]

        self.assertTrue(signal["frontier_hits"])
        self.assertEqual(signal["frontier_hits"][0]["frontier_id"], "photoresist-chemicals")
        self.assertGreaterEqual(signal["spec_score"], 55)
        self.assertIn("semicap-equipment", {item["theme"] for item in signal["activated_themes"]})

    def test_frontier_quantum_does_not_fire_on_generic_company_action_text(self) -> None:
        root = Path(__file__).resolve().parent.parent
        block = AHShareTechFeatureBlock.from_files(
            universe_path=root / "config" / "tech_universe_cn_hk.json",
            lexicon_path=root / "config" / "tech_lexicon.json",
            lexicon_release_path=root / "config" / "tech_lexicon_release.json",
            graph_path=root / "config" / "tech_impact_graph.json",
            frontier_map_path=root / "config" / "tech_frontier_map.json",
        )
        timestamp = datetime(2026, 3, 19, 10, 0, tzinfo=UTC)
        document = NewsDocument(
            doc_id=stable_id("doc", "generic-corp-action"),
            source_id="cninfo_latest",
            title="上海晶丰明源半导体股份有限公司关于发行股份及支付现金购买资产并募集配套资金的发行结果暨股本变动公告",
            summary="证券代码：688368 证券简称：晶丰明源 公告编号：2026-019",
            body="本公告为公司发行股份及支付现金购买资产事项的结果说明，正文不涉及量子计算、量子比特或量子纠错。",
            url="https://example.com/corp-action",
            published_at=timestamp,
            fetched_at=timestamp,
            language="zh",
            source_trust=0.99,
            canonical_key="generic-corp-action",
        )
        cluster = EventCluster(
            cluster_id="cluster-generic-corp-action",
            story_key="generic-corp-action",
            headline=document.title,
            summary=document.summary,
            documents=[document],
            entities=["晶丰明源", "688368"],
            themes=["takeover", "m&a", "corporate-action"],
            sectors=[],
            regions=["CN"],
            source_ids=["cninfo_latest"],
            first_seen_at=timestamp,
            last_seen_at=timestamp,
        )
        impact = ImpactAssessment(
            event_type=EventType.COMPANY,
            direction=Direction.POSITIVE,
            affected_markets=[],
            affected_sectors=[],
            affected_themes=["takeover", "m&a", "corporate-action"],
            severity=0.7,
            confidence=0.9,
            matched_rules=["Takeover and Offer Period"],
            rationale=["official corporate action"],
        )
        event = RankedEvent(
            cluster_id=cluster.cluster_id,
            headline=cluster.headline,
            impact=impact,
            heat_score=66.0,
            importance_score=70.0,
            confidence_score=88.0,
            market_relevance_score=62.0,
            final_score=72.0,
        )
        snapshot = PipelineSnapshot(
            run_id="run-corp-action-no-quantum",
            created_at=timestamp,
            source_name="test",
            raw_records=[],
            documents=[document],
            clusters=[cluster],
            ranked_events=[event],
            ranked_instruments=[],
            alerts=[],
        )

        evaluated = block.evaluate(snapshot)
        signals = evaluated["signals"]
        if not signals:
            self.assertEqual(signals, [])
            return

        signal = signals[0]
        frontier_ids = {hit["frontier_id"] for hit in signal["frontier_hits"]}
        self.assertNotIn("quantum-computing", frontier_ids)
        self.assertFalse(
            signal["frontier_hits"]
            and any(hit["frontier_id"] == "quantum-computing" for hit in signal["frontier_hits"])
        )

    def test_misaligned_official_docs_do_not_pollute_frontier_matching(self) -> None:
        root = Path(__file__).resolve().parent.parent
        block = AHShareTechFeatureBlock.from_files(
            universe_path=root / "config" / "tech_universe_cn_hk.json",
            lexicon_path=root / "config" / "tech_lexicon.json",
            lexicon_release_path=root / "config" / "tech_lexicon_release.json",
            graph_path=root / "config" / "tech_impact_graph.json",
            frontier_map_path=root / "config" / "tech_frontier_map.json",
            config_path=root / "config" / "tech_block.json",
        )
        timestamp = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
        medical_doc = NewsDocument(
            doc_id=stable_id("doc", "medical-policy"),
            source_id="gov-nhsa",
            title="“冠心病PCI术后”门诊慢特病待遇认定“零跑腿”——湖南医保便民有了新进展",
            summary="湖南医保便民服务再升级。",
            body="本文只讨论医保认定、门诊慢特病与便民流程，不涉及卫星互联网或低轨卫星。",
            url="https://example.com/nhsa-medical",
            published_at=timestamp,
            fetched_at=timestamp,
            language="zh",
            source_trust=0.95,
            canonical_key="nhsa-medical",
            themes=["policy", "healthcare"],
        )
        polluted_doc = NewsDocument(
            doc_id=stable_id("doc", "satellite-policy"),
            source_id="xinhua-tech",
            title="我国成功发射卫星互联网低轨20组卫星",
            summary="卫星互联网建设提速。",
            body="卫星互联网、低轨卫星组网与商业航天持续推进。",
            url="https://example.com/satellite",
            published_at=timestamp,
            fetched_at=timestamp,
            language="zh",
            source_trust=0.9,
            canonical_key="satellite-policy",
            themes=["technology", "policy"],
        )
        cluster = EventCluster(
            cluster_id="cluster-medical-policy",
            story_key="medical-policy",
            headline=medical_doc.title,
            summary=medical_doc.summary,
            documents=[medical_doc, polluted_doc],
            entities=[],
            themes=["policy", "healthcare", "technology"],
            sectors=[],
            regions=["CN"],
            source_ids=["gov-nhsa", "xinhua-tech"],
            first_seen_at=timestamp,
            last_seen_at=timestamp,
        )
        impact = ImpactAssessment(
            event_type=EventType.POLICY,
            direction=Direction.POSITIVE,
            affected_markets=[],
            affected_sectors=["healthcare"],
            affected_themes=["healthcare"],
            severity=0.68,
            confidence=0.86,
            matched_rules=["policy"],
            rationale=["official medical policy"],
        )
        event = RankedEvent(
            cluster_id=cluster.cluster_id,
            headline=cluster.headline,
            impact=impact,
            heat_score=70.0,
            importance_score=74.0,
            confidence_score=86.0,
            market_relevance_score=68.0,
            final_score=73.0,
        )
        snapshot = PipelineSnapshot(
            run_id="run-coherent-frontier",
            created_at=timestamp,
            source_name="test",
            raw_records=[],
            documents=[medical_doc, polluted_doc],
            clusters=[cluster],
            ranked_events=[event],
            ranked_instruments=[],
            alerts=[],
        )

        signals = block.evaluate(snapshot)["signals"]
        if not signals:
            self.assertEqual(signals, [])
            return

        signal = signals[0]
        frontier_ids = {hit["frontier_id"] for hit in signal["frontier_hits"]}
        self.assertNotIn("satellite-internet", frontier_ids)
        self.assertEqual(signal["evidence_source_ids"], ["gov-nhsa"])


if __name__ == "__main__":
    unittest.main()
