from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from market_news.application.monitoring import MonitorStateWriter
from market_news.application.notify import NotificationResult
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


class MonitorStateWriterTest(unittest.TestCase):
    def test_writer_persists_cycle_and_failure_payloads(self) -> None:
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
            writer = MonitorStateWriter(
                status_path=temp_path / "reports" / "monitor_status.json",
                history_path=temp_path / "reports" / "monitor_history.jsonl",
            )

            cycle_payload = writer.write_cycle(
                snapshot=snapshot,
                report_path=Path(snapshot.artifacts["json_report"]),
                preview_path=temp_path / "reports" / "latest_phone_alert.txt",
                notification_result=NotificationResult(
                    status="sent",
                    channel="whatsapp",
                    target="+10000000000",
                    alert_count=2,
                    preview_path=temp_path / "reports" / "latest_phone_alert.txt",
                    cluster_ids=["c1", "c2"],
                    detail="sent",
                ),
            )
            self.assertEqual(cycle_payload["overall_status"], "ok")
            saved_status = json.loads(writer.status_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_status["run_id"], snapshot.run_id)
            self.assertEqual(saved_status["notification"]["status"], "sent")

            failure_payload = writer.write_failure(
                error_message="gateway unavailable",
                report_path=Path(snapshot.artifacts["json_report"]),
                preview_path=temp_path / "reports" / "latest_phone_alert.txt",
            )
            self.assertEqual(failure_payload["overall_status"], "error")
            saved_failure = json.loads(writer.status_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_failure["errors"], ["gateway unavailable"])

            history_lines = writer.history_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(history_lines), 2)


if __name__ == "__main__":
    unittest.main()
