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
    FeatureModule,
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
        feature_modules: list[FeatureModule] | None = None,
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
        self.feature_modules = list(feature_modules or [])

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
        ranked_events = self._dedupe_ranked_events(ranked_events)
        ranked_instruments = self._dedupe_ranked_instruments(ranked_instruments)
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
        if self.feature_modules:
            feature_blocks = {
                module.name: module.evaluate(snapshot)
                for module in self.feature_modules
            }
            snapshot = replace(snapshot, feature_blocks=feature_blocks)
        self.store.persist(snapshot)
        artifacts = {key: str(path) for key, path in self.reporter.write(snapshot).items()}
        snapshot = replace(snapshot, artifacts=artifacts)
        return snapshot

    def _dedupe_ranked_events(self, events: list[object]) -> list[object]:
        deduped: list[object] = []
        seen_cluster_ids: set[str] = set()
        for event in events:
            if event.cluster_id in seen_cluster_ids:
                continue
            seen_cluster_ids.add(event.cluster_id)
            deduped.append(event)
        return deduped

    def _dedupe_ranked_instruments(self, instruments: list[object]) -> list[object]:
        deduped: list[object] = []
        seen_keys: set[tuple[str, str, str]] = set()
        for instrument in instruments:
            key = (instrument.cluster_id, instrument.symbol, instrument.market.value)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(instrument)
        return deduped
