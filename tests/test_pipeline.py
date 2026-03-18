from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from market_news.application.pipeline import MarketNewsPipeline
from market_news.infrastructure.collectors.local_json import LocalJSONCollector
from market_news.infrastructure.persistence.sqlite_store import SQLiteRunStore
from market_news.services.alerts import RuleBasedAlertEngine
from market_news.services.clustering import KeywordEventClusterer
from market_news.services.deduplication import FingerprintDeduplicator
from market_news.services.impact import ConfigDrivenImpactAnalyzer
from market_news.services.mapping import ConfigDrivenInstrumentMapper
from market_news.services.normalization import DefaultNormalizer
from market_news.services.ranking import WeightedEventRanker, WeightedInstrumentRanker
from market_news.services.reporting import MarkdownJsonReporter
from market_news.services.tech_block import AHShareTechFeatureBlock


class PipelineIntegrationTest(unittest.TestCase):
    def test_pipeline_produces_ranked_events_and_instruments(self) -> None:
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            discovery_path = temp_path / "lexicon_discovery.jsonl"
            discovery_path.write_text(
                json.dumps(
                    {
                        "text": "钙钛矿",
                        "raw_freq": 6,
                        "cooccurrence": {"机器人": 4},
                        "inferred_impact": {"robotics": 0.62, "new-materials": 0.55},
                        "discovery_score": 4.8,
                        "example_snippets": ["钙钛矿材料进入机器人电源链"],
                        "detected_at": "2026-03-15T00:01:00Z",
                        "status": "pending",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
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
                reporter=MarkdownJsonReporter(
                    temp_path / "reports",
                    lexicon_discovery_path=discovery_path,
                    lexicon_path=root / "config" / "tech_lexicon.json",
                    tech_block_config_path=root / "config" / "tech_block.json",
                ),
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

            self.assertGreaterEqual(len(snapshot.documents), 5)
            self.assertGreaterEqual(len(snapshot.clusters), 4)
            self.assertGreaterEqual(len(snapshot.ranked_events), 4)
            self.assertGreaterEqual(len(snapshot.ranked_instruments), 6)
            self.assertGreaterEqual(len(snapshot.alerts), 3)
            self.assertTrue((temp_path / "market_news.db").exists())
            self.assertTrue((temp_path / "reports" / "latest_report.json").exists())
            self.assertTrue((temp_path / "reports" / "latest_dashboard.html").exists())
            report_payload = json.loads((temp_path / "reports" / "latest_report.json").read_text())
            self.assertIn("runtime_status", report_payload)
            self.assertIn("lines", report_payload["runtime_status"])
            self.assertIn("tech_block", report_payload)
            self.assertIn("lexicon_discovery", report_payload)
            self.assertEqual(report_payload["lexicon_discovery"]["summary"]["pending_count"], 1)
            self.assertGreater(report_payload["lexicon_discovery"]["summary"]["accepted_count"], 0)
            self.assertGreater(len(report_payload["lexicon_discovery"]["accepted_terms"]), 0)
            self.assertGreaterEqual(report_payload["tech_block"]["summary"]["signal_count"], 1)
            self.assertEqual(report_payload["tech_block"]["summary"]["lexicon_version"], "2026.03-p2")
            dashboard_html = (temp_path / "reports" / "latest_dashboard.html").read_text()
            self.assertIn("Runtime Status", dashboard_html)
            self.assertIn("AH Tech Catalyst Block", dashboard_html)
            self.assertIn("Lexicon Discovery", dashboard_html)
            self.assertIn("Lexicon Catalog", dashboard_html)
            self.assertIn('id="viewSwitch"', dashboard_html)
            self.assertIn('id="coreWorkspace"', dashboard_html)
            self.assertIn('id="techWorkspace"', dashboard_html)
            self.assertIn('id="frontierWorkspace"', dashboard_html)
            self.assertIn('id="techSignalList"', dashboard_html)
            self.assertIn('id="techDetailView"', dashboard_html)
            self.assertIn('id="frontierTrackerList"', dashboard_html)
            self.assertIn('id="frontierSignalList"', dashboard_html)
            self.assertIn('id="frontierDetailView"', dashboard_html)
            self.assertIn('id="lexiconDiscoveryList"', dashboard_html)
            self.assertIn('id="lexiconCatalogList"', dashboard_html)
            self.assertIn('id="lexiconCatalogQuery"', dashboard_html)
            self.assertIn('data-lexicon-remove', dashboard_html)
            self.assertIn('id="runtimeStatusGrid"', dashboard_html)
            self.assertIn("class=\"column column-scroll left-column\"", dashboard_html)
            self.assertIn("class=\"column column-scroll middle-column\"", dashboard_html)
            self.assertIn("class=\"column column-scroll right-column\"", dashboard_html)
            self.assertIn("grid-template-columns: minmax(280px, 1fr) minmax(520px, 1.45fr) minmax(360px, 1.08fr);", dashboard_html)
            self.assertIn("#detailView {", dashboard_html)
            self.assertIn("height: clamp(760px, calc(100vh - 220px), 1200px);", dashboard_html)
            self.assertIn("function rightColumn()", dashboard_html)
            self.assertIn("function renderTechBlock()", dashboard_html)
            self.assertIn("function renderFrontierWorkspace()", dashboard_html)
            self.assertIn("function renderViewSwitch()", dashboard_html)
            self.assertIn("科技前沿", dashboard_html)
            self.assertIn("window.location.replace(url.toString())", dashboard_html)
            self.assertEqual(snapshot.ranked_events[0].impact.direction.value, "positive")
            self.assertIn("tech_block", snapshot.feature_blocks)
            self.assertGreaterEqual(len(snapshot.feature_blocks["tech_block"]["asset_ladder"]), 1)
            symbols = {instrument.symbol for instrument in snapshot.ranked_instruments}
            self.assertIn("NVDA", symbols)
            self.assertIn("TLT", symbols)


if __name__ == "__main__":
    unittest.main()
