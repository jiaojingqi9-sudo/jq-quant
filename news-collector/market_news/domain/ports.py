from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from market_news.domain.models import (
    AlertItem,
    EventCluster,
    ImpactAssessment,
    InstrumentMatch,
    NewsDocument,
    PipelineSnapshot,
    RankedEvent,
    RankedInstrument,
    RawNewsRecord,
)


class Collector(Protocol):
    name: str

    def collect(self) -> list[RawNewsRecord]:
        ...


class Normalizer(Protocol):
    def normalize(self, records: list[RawNewsRecord]) -> list[NewsDocument]:
        ...


class Deduplicator(Protocol):
    def deduplicate(self, documents: list[NewsDocument]) -> list[NewsDocument]:
        ...


class Clusterer(Protocol):
    def cluster(self, documents: list[NewsDocument]) -> list[EventCluster]:
        ...


class ImpactAnalyzer(Protocol):
    def assess(self, cluster: EventCluster) -> ImpactAssessment:
        ...


class EventRanker(Protocol):
    def rank(self, cluster: EventCluster, impact: ImpactAssessment) -> RankedEvent:
        ...


class InstrumentMapper(Protocol):
    def map(self, cluster: EventCluster, impact: ImpactAssessment) -> list[InstrumentMatch]:
        ...


class InstrumentRanker(Protocol):
    def rank(
        self,
        cluster: EventCluster,
        impact: ImpactAssessment,
        event: RankedEvent,
        matches: list[InstrumentMatch],
    ) -> list[RankedInstrument]:
        ...


class AlertEngine(Protocol):
    def generate(
        self,
        ranked_events: list[RankedEvent],
        ranked_instruments: list[RankedInstrument],
        seen_cluster_ids: set[str],
    ) -> list[AlertItem]:
        ...


class RunStore(Protocol):
    def load_recent_event_ids(self) -> set[str]:
        ...

    def persist(self, snapshot: PipelineSnapshot) -> None:
        ...


class DeliveryStore(Protocol):
    def load_sent_alert_cluster_ids(
        self,
        channel: str,
        target: str,
        *,
        lookback_hours: int = 72,
    ) -> set[str]:
        ...

    def persist_alert_delivery(
        self,
        *,
        run_id: str,
        channel: str,
        target: str,
        cluster_ids: list[str],
        message_text: str,
    ) -> None:
        ...


class Reporter(Protocol):
    def write(self, snapshot: PipelineSnapshot) -> dict[str, Path]:
        ...


class FeatureModule(Protocol):
    name: str

    def evaluate(self, snapshot: PipelineSnapshot) -> dict[str, Any]:
        ...


class Notifier(Protocol):
    def resolve_target(self, channel: str, explicit_target: str | None = None) -> str:
        ...

    def send(self, *, channel: str, target: str, message: str) -> str:
        ...
