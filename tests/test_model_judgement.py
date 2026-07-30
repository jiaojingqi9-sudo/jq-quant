from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from market_news.domain.models import (
    Direction,
    EventCluster,
    EventType,
    ImpactAssessment,
    InstrumentDescriptor,
    Market,
    NewsDocument,
)
from market_news.services.impact import ConfigDrivenImpactAnalyzer
from market_news.services.mapping import ConfigDrivenInstrumentMapper
from market_news.services.model_judgement import (
    ModelCallBudget,
    ModelEnhancedImpactAnalyzer,
    ModelEnhancedInstrumentMapper,
    ModelJudgementConfig,
    _extract_openclaw_text,
    _parse_json_object,
)


class FakeModelClient:
    available = True

    def __init__(self, *, screen_result: dict[str, object] | None = None, asset_result: dict[str, object] | None = None) -> None:
        self.screen_result = screen_result
        self.asset_result = asset_result
        self.asset_calls = 0

    def screen_event(self, cluster: EventCluster, base_impact: ImpactAssessment) -> dict[str, object] | None:
        return self.screen_result

    def map_assets(
        self,
        cluster: EventCluster,
        impact: ImpactAssessment,
        instruments: list[InstrumentDescriptor],
    ) -> dict[str, object] | None:
        self.asset_calls += 1
        return self.asset_result


def _cluster(*, source_id: str = "cls") -> EventCluster:
    now = datetime(2026, 3, 20, 8, 0, tzinfo=UTC)
    document = NewsDocument(
        doc_id="doc-1",
        source_id=source_id,
        title="工信部发布人工智能算力基础设施政策",
        summary="政策提出推动数据中心和国产芯片适配。",
        body="官方文件明确推动算力基础设施、国产芯片、数据中心协同建设。",
        url="https://example.com/news/1",
        published_at=now,
        fetched_at=now,
        language="zh",
        source_trust=0.95,
        canonical_key="policy-ai-compute",
        entities=["工信部"],
        themes=["policy", "ai", "chips"],
        regions=["CN"],
    )
    return EventCluster(
        cluster_id="cluster-1",
        story_key="policy-ai-compute",
        headline=document.title,
        summary=document.summary,
        documents=[document],
        entities=document.entities,
        themes=document.themes,
        sectors=["technology"],
        regions=document.regions,
        source_ids=[source_id],
        first_seen_at=now,
        last_seen_at=now,
    )


def _base_impact() -> ImpactAssessment:
    return ImpactAssessment(
        event_type=EventType.UNKNOWN,
        direction=Direction.NEUTRAL,
        affected_markets=[Market.CN_A, Market.HK],
        affected_sectors=[],
        affected_themes=[],
        severity=0.35,
        confidence=0.45,
        matched_rules=[],
        rationale=["base fallback"],
    )


class StaticImpactAnalyzer:
    def __init__(self, impact: ImpactAssessment) -> None:
        self.impact = impact

    def assess(self, cluster: EventCluster) -> ImpactAssessment:
        return self.impact


class ModelJudgementTest(unittest.TestCase):
    def test_screening_can_upgrade_event_assessment(self) -> None:
        config = ModelJudgementConfig(
            evidence_source_ids={"cls"},
            social_source_ids={"weibo", "xueqiu"},
            excluded_source_ids=set(),
        )
        client = FakeModelClient(
            screen_result={
                "worth_attention": True,
                "attention_score": 86,
                "event_type": "policy",
                "direction": "positive",
                "severity": 0.82,
                "confidence": 0.78,
                "affected_markets": ["CN-A", "HK"],
                "affected_sectors": ["technology"],
                "affected_themes": ["ai", "chips"],
                "reason": "官方政策直接推动算力和国产芯片方向。",
                "evidence": ["工信部发布政策", "提到算力基础设施"],
            }
        )
        analyzer = ModelEnhancedImpactAnalyzer(StaticImpactAnalyzer(_base_impact()), client, config)

        impact = analyzer.assess(_cluster())

        self.assertEqual(impact.event_type, EventType.POLICY)
        self.assertEqual(impact.direction, Direction.POSITIVE)
        self.assertGreaterEqual(impact.severity, 0.82)
        self.assertIn("gpt-screening", impact.matched_rules)
        self.assertEqual(impact.model_judgement["screening_status"], "used")
        self.assertIn("AI筛选", impact.rationale[0])

    def test_social_only_cluster_is_not_sent_to_model(self) -> None:
        config = ModelJudgementConfig(
            evidence_source_ids={"cls"},
            social_source_ids={"weibo", "xueqiu"},
            excluded_source_ids=set(),
        )
        client = FakeModelClient(
            screen_result={
                "worth_attention": True,
                "event_type": "policy",
                "direction": "positive",
            }
        )
        analyzer = ModelEnhancedImpactAnalyzer(StaticImpactAnalyzer(_base_impact()), client, config)

        impact = analyzer.assess(_cluster(source_id="weibo"))

        self.assertEqual(impact.model_judgement["screening_status"], "skipped")
        self.assertNotIn("gpt-screening", impact.matched_rules)

    def test_asset_mapper_adds_model_candidate_with_evidence_reason(self) -> None:
        config = ModelJudgementConfig(
            min_asset_confidence=0.5,
            min_asset_exposure=0.45,
        )
        candidate = InstrumentDescriptor(
            symbol="688981.SH",
            market=Market.CN_A,
            asset_type="stock",
            name="中芯国际",
            sectors=["semiconductors"],
            themes=["chips", "domestic-substitution"],
            aliases=["中芯国际", "SMIC"],
            liquidity_score=0.88,
        )
        base_mapper = ConfigDrivenInstrumentMapper([])
        client = FakeModelClient(
            asset_result={
                "candidates": [
                    {
                        "symbol": "688981.SH",
                        "market": "CN-A",
                        "name": "中芯国际",
                        "direction": "positive",
                        "exposure_score": 0.72,
                        "confidence": 0.67,
                        "relation": "policy_beneficiary",
                        "reason": "国产芯片政策受益链条明确。",
                    }
                ]
            }
        )
        mapper = ModelEnhancedInstrumentMapper(base_mapper, client, config, [candidate])
        impact = _base_impact()
        impact = ImpactAssessment(
            event_type=EventType.POLICY,
            direction=Direction.POSITIVE,
            affected_markets=[Market.CN_A],
            affected_sectors=["technology"],
            affected_themes=["chips"],
            severity=0.8,
            confidence=0.7,
            matched_rules=["gpt-screening"],
            rationale=["AI筛选"],
            model_judgement={"screening": {"worth_attention": True, "attention_score": 86}},
        )

        matches = mapper.map(_cluster(), impact)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].instrument.symbol, "688981.SH")
        self.assertIn("AI asset map", matches[0].reasons[0])

    def test_asset_mapper_does_not_spend_model_call_without_screening(self) -> None:
        config = ModelJudgementConfig(
            min_asset_confidence=0.5,
            min_asset_exposure=0.45,
        )
        base_mapper = ConfigDrivenInstrumentMapper([])
        client = FakeModelClient(asset_result={"candidates": []})
        mapper = ModelEnhancedInstrumentMapper(base_mapper, client, config, [])
        impact = ImpactAssessment(
            event_type=EventType.COMPANY,
            direction=Direction.POSITIVE,
            affected_markets=[Market.CN_A],
            affected_sectors=[],
            affected_themes=[],
            severity=0.7,
            confidence=0.8,
            matched_rules=[],
            rationale=[],
            model_judgement={"attention_gate": {"tier": "watch", "score": 72}},
        )

        matches = mapper.map(_cluster(), impact)

        self.assertEqual(matches, [])
        self.assertEqual(client.asset_calls, 0)

    def test_openclaw_json_envelope_can_be_parsed(self) -> None:
        envelope = {
            "status": "ok",
            "result": {
                "payloads": [
                    {"text": '{"worth_attention": true, "source": "openclaw-agent"}'}
                ]
            },
        }

        parsed = _parse_json_object(_extract_openclaw_text(envelope))

        self.assertEqual(parsed["source"], "openclaw-agent")
        self.assertTrue(parsed["worth_attention"])

    def test_openclaw_top_level_payload_envelope_can_be_parsed(self) -> None:
        envelope = {
            "payloads": [
                {"text": '{"worth_attention": true, "source": "openclaw-agent"}'}
            ],
            "meta": {"provider": "openai-codex"},
        }

        parsed = _parse_json_object(_extract_openclaw_text(envelope))

        self.assertEqual(parsed["source"], "openclaw-agent")
        self.assertTrue(parsed["worth_attention"])

    def test_model_call_budget_enforces_daily_limit(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "budget.json"
            budget = ModelCallBudget(path, daily_limit=2)

            self.assertTrue(budget.reserve("openclaw-screen"))
            self.assertTrue(budget.reserve("openclaw-screen"))
            self.assertFalse(budget.reserve("openclaw-assets"))

    def test_zero_model_call_budget_disables_reservations(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "budget.json"
            budget = ModelCallBudget(path, daily_limit=0)

            self.assertFalse(budget.reserve("openclaw-screen"))


if __name__ == "__main__":
    unittest.main()
