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

    def test_source_multiplier_rewards_official_sources_over_social_sources(self) -> None:
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

        social_document = replace(document, source_id="weibo", source_trust=0.4)
        social_cluster = replace(cluster, documents=[social_document], source_ids=["weibo"])
        social_snapshot = replace(base_snapshot, documents=[social_document], clusters=[social_cluster])
        social_score = block.evaluate(social_snapshot)["signals"][0]["trading_attention_score"]

        self.assertGreater(official_score, social_score)


if __name__ == "__main__":
    unittest.main()
