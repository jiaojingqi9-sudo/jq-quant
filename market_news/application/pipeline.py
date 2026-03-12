from __future__ import annotations

from dataclasses import replace

from market_news.common import stable_id, utcnow
from market_news.domain.models import PipelineSnapshot
from market_news.domain.ports import (
    AlertEngine,
    Clusterer,
    Collector,
    Deduplicator,
    EventRanker,
    ImpactAnalyzer,
    InstrumentMapper,
    InstrumentRanker,
    Normalizer,
    Reporter,
    RunStore,
)


class MarketNewsPipeline:
    def __init__(
        self,
        collector: Collector,
        normalizer: Normalizer,
        deduplicator: Deduplicator,
        clusterer: Clusterer,
        impact_analyzer: ImpactAnalyzer,
        event_ranker: EventRanker,
        instrument_mapper: InstrumentMapper,
        instrument_ranker: InstrumentRanker,
        alert_engine: AlertEngine,
        store: RunStore,
        reporter: Reporter,
    ) -> None:
        self.collector = collector
        self.normalizer = normalizer
        self.deduplicator = deduplicator
        self.clusterer = clusterer
        self.impact_analyzer = impact_analyzer
        self.event_ranker = event_ranker
        self.instrument_mapper = instrument_mapper
        self.instrument_ranker = instrument_ranker
        self.alert_engine = alert_engine
        self.store = store
        self.reporter = reporter

    def run(self) -> PipelineSnapshot:
        created_at = utcnow()
        seen_cluster_ids = self.store.load_recent_event_ids()
        raw_records = self.collector.collect()
        documents = self.normalizer.normalize(raw_records)
        deduplicated_documents = self.deduplicator.deduplicate(documents)
        clusters = self.clusterer.cluster(deduplicated_documents)

        ranked_events = []
        ranked_instruments = []
        for cluster in clusters:
            impact = self.impact_analyzer.assess(cluster)
            event = self.event_ranker.rank(cluster, impact)
            ranked_events.append(event)

            matches = self.instrument_mapper.map(cluster, impact)
            ranked_instruments.extend(
                self.instrument_ranker.rank(cluster, impact, event, matches)
            )

        ranked_events.sort(key=lambda item: item.final_score, reverse=True)
        ranked_instruments.sort(key=lambda item: item.final_score, reverse=True)
        alerts = self.alert_engine.generate(
            ranked_events,
            ranked_instruments,
            seen_cluster_ids,
        )

        snapshot = PipelineSnapshot(
            run_id=stable_id(self.collector.name, created_at.isoformat()),
            created_at=created_at,
            source_name=self.collector.name,
            raw_records=raw_records,
            documents=deduplicated_documents,
            clusters=clusters,
            ranked_events=ranked_events,
            ranked_instruments=ranked_instruments,
            alerts=alerts,
        )
        self.store.persist(snapshot)
        artifacts = {key: str(path) for key, path in self.reporter.write(snapshot).items()}
        snapshot = replace(snapshot, artifacts=artifacts)
        return snapshot
