from __future__ import annotations

from datetime import timedelta

from market_news.common import clamp, utcnow
from market_news.domain.models import (
    Direction,
    EventCluster,
    ImpactAssessment,
    InstrumentMatch,
    RankedEvent,
    RankedInstrument,
)


class WeightedEventRanker:
    def __init__(self) -> None:
        self.recency_window = timedelta(hours=72)

    def rank(self, cluster: EventCluster, impact: ImpactAssessment) -> RankedEvent:
        age = utcnow() - cluster.last_seen_at
        recency_score = clamp(1.0 - (age / self.recency_window))
        doc_count_score = clamp(cluster.doc_count / 4)
        source_diversity_score = clamp(len(cluster.source_ids) / 4)
        trust_score = clamp(cluster.avg_source_trust)
        theme_density_score = clamp(len(set(impact.affected_themes)) / 4)
        market_score = clamp(len(set(impact.affected_markets)) / 3)
        event_type_factor = 1.0 if impact.event_type.value != "unknown" else 0.45

        heat_score = 100 * (
            0.35 * recency_score
            + 0.20 * doc_count_score
            + 0.20 * source_diversity_score
            + 0.25 * trust_score
        )
        importance_score = 100 * (
            0.40 * impact.severity
            + 0.20 * theme_density_score
            + 0.15 * market_score
            + 0.15 * trust_score
            + 0.10 * event_type_factor
        )
        confidence_score = 100 * (0.70 * impact.confidence + 0.30 * trust_score)
        market_relevance_score = 100 * (
            0.50 * market_score
            + 0.25 * theme_density_score
            + 0.25 * (1.0 if impact.direction != Direction.NEUTRAL else 0.55)
        )
        final_score = (
            0.35 * heat_score
            + 0.35 * importance_score
            + 0.20 * confidence_score
            + 0.10 * market_relevance_score
        )
        if impact.event_type.value == "unknown" and impact.direction == Direction.NEUTRAL:
            final_score *= 0.82
        return RankedEvent(
            cluster_id=cluster.cluster_id,
            headline=cluster.headline,
            impact=impact,
            heat_score=round(heat_score, 2),
            importance_score=round(importance_score, 2),
            confidence_score=round(confidence_score, 2),
            market_relevance_score=round(market_relevance_score, 2),
            final_score=round(final_score, 2),
        )


class WeightedInstrumentRanker:
    def rank(
        self,
        cluster: EventCluster,
        impact: ImpactAssessment,
        event: RankedEvent,
        matches: list[InstrumentMatch],
    ) -> list[RankedInstrument]:
        ranked: list[RankedInstrument] = []
        for match in matches:
            confidence_score = 100 * clamp(0.65 * impact.confidence + 0.35 * match.exposure_score)
            impact_score = 100 * clamp(0.60 * impact.severity + 0.40 * match.exposure_score)
            final_score = (
                0.45 * event.importance_score
                + 0.30 * impact_score
                + 0.15 * (match.instrument.liquidity_score * 100)
                + 0.10 * confidence_score
            )
            ranked.append(
                RankedInstrument(
                    cluster_id=cluster.cluster_id,
                    cluster_headline=cluster.headline,
                    symbol=match.instrument.symbol,
                    market=match.instrument.market,
                    asset_type=match.instrument.asset_type,
                    name=match.instrument.name,
                    direction=match.direction,
                    exposure_score=round(match.exposure_score * 100, 2),
                    liquidity_score=round(match.instrument.liquidity_score * 100, 2),
                    impact_score=round(impact_score, 2),
                    confidence_score=round(confidence_score, 2),
                    final_score=round(final_score, 2),
                    reasons=match.reasons,
                )
            )
        ranked.sort(key=lambda item: item.final_score, reverse=True)
        return ranked
