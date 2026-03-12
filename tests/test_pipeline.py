from __future__ import annotations

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


class PipelineIntegrationTest(unittest.TestCase):
    def test_pipeline_produces_ranked_events_and_instruments(self) -> None:
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
            dashboard_html = (temp_path / "reports" / "latest_dashboard.html").read_text()
            self.assertIn('id="detailView" tabindex="0"', dashboard_html)
            self.assertIn("grid-template-rows: auto minmax(0, 1fr);", dashboard_html)
            self.assertIn("installDetailScrollBridge()", dashboard_html)
            self.assertIn("window.location.replace(url.toString())", dashboard_html)
            self.assertEqual(snapshot.ranked_events[0].impact.direction.value, "positive")
            symbols = {instrument.symbol for instrument in snapshot.ranked_instruments}
            self.assertIn("NVDA", symbols)
            self.assertIn("TLT", symbols)


if __name__ == "__main__":
    unittest.main()
