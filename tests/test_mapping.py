from __future__ import annotations

from datetime import UTC, datetime
import unittest

from market_news.domain.models import (
    Direction,
    EventCluster,
    ImpactAssessment,
    InstrumentDescriptor,
    Market,
    NewsDocument,
)
from market_news.domain.models import EventType
from market_news.services.mapping import ConfigDrivenInstrumentMapper


class MappingTest(unittest.TestCase):
    def test_direct_codes_short_circuit_to_known_universe_symbols(self) -> None:
        mapper = ConfigDrivenInstrumentMapper(
            [
                InstrumentDescriptor(
                    symbol="300059.SZ",
                    market=Market.CN_A,
                    asset_type="stock",
                    name="Eastmoney",
                    sectors=["brokerage"],
                    themes=["cloud-software"],
                    aliases=["东方财富", "300059"],
                    liquidity_score=0.9,
                )
            ]
        )
        now = datetime(2026, 3, 16, 8, 0, tzinfo=UTC)
        document = NewsDocument(
            doc_id="doc-cls",
            source_id="cls",
            title="财联社快讯",
            summary="东方财富回购",
            body="",
            url="https://www.cls.cn/detail/1",
            published_at=now,
            fetched_at=now,
            language="zh",
            source_trust=0.9,
            canonical_key="cls-1",
            metadata={"direct_codes": ["300059"]},
        )
        cluster = EventCluster(
            cluster_id="cluster-cls",
            story_key="cls-1",
            headline=document.title,
            summary=document.summary,
            documents=[document],
            entities=[],
            themes=[],
            sectors=[],
            regions=["CN"],
            source_ids=["cls"],
            first_seen_at=now,
            last_seen_at=now,
        )
        impact = ImpactAssessment(
            event_type=EventType.COMPANY,
            direction=Direction.POSITIVE,
            affected_markets=[Market.CN_A],
            affected_sectors=[],
            affected_themes=[],
            severity=0.7,
            confidence=0.8,
            matched_rules=["company"],
            rationale=["direct code"],
        )

        matches = mapper.map(cluster, impact)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].instrument.symbol, "300059.SZ")
        self.assertIn("direct code from source", matches[0].reasons)


if __name__ == "__main__":
    unittest.main()
