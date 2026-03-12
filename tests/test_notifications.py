from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from market_news.application.notify import NotificationRunner
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


class FakeOpenClawNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def resolve_target(self, channel: str, explicit_target: str | None = None) -> str:
        return explicit_target or "+10000000000"

    def send(self, *, channel: str, target: str, message: str) -> str:
        self.messages.append((channel, target, message))
        return "fake-send-ok"


class NotificationIntegrationTest(unittest.TestCase):
    def test_notification_preview_and_delivery_history(self) -> None:
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

            notifier = FakeOpenClawNotifier()
            store = SQLiteRunStore(temp_path / "market_news.db")
            runner = NotificationRunner(store=store, notifier=notifier)
            preview_path = temp_path / "reports" / "latest_phone_alert.txt"

            preview = runner.deliver_from_report(
                report_path=Path(snapshot.artifacts["json_report"]),
                preview_path=preview_path,
                channel="whatsapp",
                max_alerts=20,
                dry_run=True,
            )
            self.assertEqual(preview.status, "preview")
            self.assertTrue(preview_path.exists())
            self.assertIn("市场新闻提醒", preview_path.read_text(encoding="utf-8"))
            self.assertEqual(len(notifier.messages), 0)

            sent = runner.deliver_from_report(
                report_path=Path(snapshot.artifacts["json_report"]),
                preview_path=preview_path,
                channel="whatsapp",
                max_alerts=20,
            )
            self.assertEqual(sent.status, "sent")
            self.assertGreaterEqual(sent.alert_count, 1)
            self.assertEqual(len(notifier.messages), 1)

            skipped = runner.deliver_from_report(
                report_path=Path(snapshot.artifacts["json_report"]),
                preview_path=preview_path,
                channel="whatsapp",
                max_alerts=20,
            )
            self.assertEqual(skipped.status, "skipped")
            self.assertEqual(len(notifier.messages), 1)

    def test_probe_message_can_be_previewed_and_sent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            notifier = FakeOpenClawNotifier()
            store = SQLiteRunStore(temp_path / "market_news.db")
            runner = NotificationRunner(store=store, notifier=notifier)
            preview_path = temp_path / "reports" / "latest_probe_message.txt"
            status_path = temp_path / "reports" / "monitor_status.json"
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(
                json.dumps(
                    {
                        "overall_status": "ok",
                        "counts": {"ranked_events": 12, "ranked_instruments": 5},
                        "alert_counts": {"critical": 1, "high": 2, "medium": 3},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            preview = runner.send_probe(
                channel="whatsapp",
                preview_path=preview_path,
                dry_run=True,
                status_path=status_path,
            )
            self.assertEqual(preview.status, "preview")
            self.assertIn("市场新闻系统测试消息", preview_path.read_text(encoding="utf-8"))
            self.assertEqual(len(notifier.messages), 0)

            sent = runner.send_probe(
                channel="whatsapp",
                preview_path=preview_path,
                status_path=status_path,
            )
            self.assertEqual(sent.status, "sent")
            self.assertEqual(len(notifier.messages), 1)


if __name__ == "__main__":
    unittest.main()
