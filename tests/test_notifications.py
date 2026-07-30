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
from market_news.services.notification import AlertDigestBuilder
from market_news.services.ranking import WeightedEventRanker, WeightedInstrumentRanker
from market_news.services.reporting import MarkdownJsonReporter
from market_news.services.tech_block import AHShareTechFeatureBlock
from market_news.domain.models import AlertLevel


class FakeOpenClawNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def resolve_target(self, channel: str, explicit_target: str | None = None) -> str:
        return explicit_target or "+10000000000"

    def send(self, *, channel: str, target: str, message: str) -> str:
        self.messages.append((channel, target, message))
        return "fake-send-ok"


class FakeBrokenOpenClawNotifier(FakeOpenClawNotifier):
    def send(self, *, channel: str, target: str, message: str) -> str:
        raise RuntimeError("No active WhatsApp Web listener")


def _mark_first_alert_as_model_screened(report_path: Path) -> None:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    alerts = payload.get("alerts", [])
    if not alerts:
        return
    target_cluster_id = str(alerts[0].get("cluster_id", ""))
    alerts[0]["level"] = "critical"
    alerts[0]["is_new"] = True
    for key in ["top_events", "negative_risks", "positive_catalysts", "watchlist"]:
        for event in payload.get(key, []):
            if isinstance(event, dict) and event.get("cluster_id") == target_cluster_id:
                event["model_judgement"] = {
                    "screening_status": "used",
                    "screening": {
                        "worth_attention": True,
                        "confidence": 0.92,
                        "reason": "测试用AI完整判断。",
                    },
                }
                event.setdefault("rationale", []).insert(0, "AI筛选: 测试用AI完整判断。")
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class NotificationIntegrationTest(unittest.TestCase):
    def test_digest_requires_full_gpt_judgement_by_default(self) -> None:
        payload = {
            "created_at": "2026-04-27T00:00:00+00:00",
            "alerts": [
                {
                    "cluster_id": "c1",
                    "headline": "公司净利润同比增长100%",
                    "level": "critical",
                    "direction": "positive",
                    "event_type": "company",
                    "is_new": True,
                    "final_score": 90,
                }
            ],
            "top_events": [
                {
                    "cluster_id": "c1",
                    "headline": "公司净利润同比增长100%",
                    "rationale": ["基本面改善"],
                }
            ],
        }
        builder = AlertDigestBuilder(min_level=AlertLevel.HIGH)

        # The model layer answering "no" must still suppress the alert. Here the
        # layer ran (screening_status="used") and declined it, which is a real
        # verdict — distinct from the layer never having run at all.
        payload["top_events"][0]["model_judgement"] = {
            "screening_status": "used",
            "screening": {"worth_attention": False, "confidence": 0.88},
        }
        plan_rejected_by_gpt = builder.compose(
            payload,
            channel="whatsapp",
            target="+10000000000",
            preview_path=Path("preview.txt"),
        )

        payload["top_events"][0]["model_judgement"] = {
            "screening_status": "used",
            "screening": {
                "worth_attention": True,
                "confidence": 0.91,
                "reason": "利润增速明确且会影响估值。",
            },
        }
        plan_with_gpt = builder.compose(
            payload,
            channel="whatsapp",
            target="+10000000000",
            preview_path=Path("preview.txt"),
        )

        self.assertIsNone(plan_rejected_by_gpt)
        self.assertIsNotNone(plan_with_gpt)
        assert plan_with_gpt is not None
        self.assertIn("AI完整判断", plan_with_gpt.message)

    def test_digest_fails_open_when_model_layer_never_ran(self) -> None:
        """A dead model layer must not silently mute every alert.

        Regression guard for the outage where no event carried a model verdict
        (no API key / quota exhausted), so the model gate dropped 100% of alerts
        and no market news reached the phone for weeks.
        """

        payload = {
            "created_at": "2026-04-27T08:00:00+00:00",
            "alerts": [
                {
                    "cluster_id": "c1",
                    "headline": "公司净利润同比增长100%",
                    "level": "critical",
                    "direction": "positive",
                    "event_type": "company",
                    "final_score": 90.0,
                    "is_new": True,
                }
            ],
            "top_events": [
                {
                    "cluster_id": "c1",
                    "headline": "公司净利润同比增长100%",
                    "rationale": ["基本面改善"],
                    "model_judgement": {"screening_status": "unavailable"},
                }
            ],
        }
        builder = AlertDigestBuilder(min_level=AlertLevel.HIGH)
        plan = builder.compose(
            payload,
            channel="whatsapp",
            target="+10000000000",
            preview_path=Path("preview.txt"),
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.alert_count, 1)
        self.assertIn("AI 筛选层不可用", plan.message)

    def test_degraded_mode_still_respects_level_bar(self) -> None:
        """Failing open must not mean sending everything: medium stays muted."""

        payload = {
            "created_at": "2026-04-27T08:00:00+00:00",
            "alerts": [
                {
                    "cluster_id": "c9",
                    "headline": "某公司发布例行公告",
                    "level": "medium",
                    "direction": "neutral",
                    "event_type": "company",
                    "final_score": 40.0,
                    "is_new": True,
                }
            ],
            "top_events": [
                {
                    "cluster_id": "c9",
                    "headline": "某公司发布例行公告",
                    "model_judgement": {"screening_status": "unavailable"},
                }
            ],
        }
        builder = AlertDigestBuilder(min_level=AlertLevel.HIGH)
        plan = builder.compose(
            payload,
            channel="whatsapp",
            target="+10000000000",
            preview_path=Path("preview.txt"),
        )

        self.assertIsNone(plan)

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
                feature_modules=[
                    AHShareTechFeatureBlock.from_files(
                        universe_path=root / "config" / "tech_universe_cn_hk.json",
                        lexicon_path=root / "config" / "tech_lexicon.json",
                        graph_path=root / "config" / "tech_impact_graph.json",
                    )
                ],
            )
            snapshot = pipeline.run()
            _mark_first_alert_as_model_screened(Path(snapshot.artifacts["json_report"]))

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
            self.assertIn("市场提醒", preview_path.read_text(encoding="utf-8"))
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
            self.assertTrue(any(module["name"] == "core_alerts" for module in sent.modules))

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

    def test_notification_falls_back_to_preview_when_phone_channel_is_unavailable(self) -> None:
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
                        graph_path=root / "config" / "tech_impact_graph.json",
                    )
                ],
            )
            snapshot = pipeline.run()
            _mark_first_alert_as_model_screened(Path(snapshot.artifacts["json_report"]))
            runner = NotificationRunner(
                store=SQLiteRunStore(temp_path / "market_news.db"),
                notifier=FakeBrokenOpenClawNotifier(),
            )
            preview_path = temp_path / "reports" / "latest_phone_alert.txt"

            result = runner.deliver_from_report(
                report_path=Path(snapshot.artifacts["json_report"]),
                preview_path=preview_path,
                channel="whatsapp",
                max_alerts=20,
            )

            self.assertEqual(result.status, "preview")
            self.assertIn("temporarily unavailable", result.detail)
            self.assertTrue(preview_path.exists())


if __name__ == "__main__":
    unittest.main()
