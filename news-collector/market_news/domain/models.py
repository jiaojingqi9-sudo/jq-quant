from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Market(StrEnum):
    CN_A = "CN-A"
    HK = "HK"
    US = "US"


class Direction(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class EventType(StrEnum):
    COMPANY = "company"
    INDUSTRY = "industry"
    POLICY = "policy"
    MACRO = "macro"
    COMMODITY = "commodity"
    REGULATION = "regulation"
    UNKNOWN = "unknown"


class AlertLevel(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"


@dataclass(slots=True)
class RawNewsRecord:
    source_id: str
    title: str
    summary: str = ""
    body: str = ""
    url: str = ""
    published_at: datetime | None = None
    language: str = "en"
    source_trust: float = 0.5
    external_id: str | None = None
    entities: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NewsDocument:
    doc_id: str
    source_id: str
    title: str
    summary: str
    body: str
    url: str
    published_at: datetime
    fetched_at: datetime
    language: str
    source_trust: float
    canonical_key: str
    entities: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def combined_text(self) -> str:
        return " ".join(part for part in [self.title, self.summary, self.body] if part).strip()


@dataclass(slots=True)
class EventCluster:
    cluster_id: str
    story_key: str
    headline: str
    summary: str
    documents: list[NewsDocument]
    entities: list[str]
    themes: list[str]
    sectors: list[str]
    regions: list[str]
    source_ids: list[str]
    first_seen_at: datetime
    last_seen_at: datetime

    @property
    def doc_count(self) -> int:
        return len(self.documents)

    @property
    def avg_source_trust(self) -> float:
        if not self.documents:
            return 0.0
        return sum(document.source_trust for document in self.documents) / len(self.documents)

    @property
    def combined_text(self) -> str:
        parts = [self.headline, self.summary]
        parts.extend(document.combined_text for document in self.documents)
        return "\n".join(part for part in parts if part).strip()


@dataclass(slots=True)
class ImpactAssessment:
    event_type: EventType
    direction: Direction
    affected_markets: list[Market]
    affected_sectors: list[str]
    affected_themes: list[str]
    severity: float
    confidence: float
    matched_rules: list[str]
    rationale: list[str]
    model_judgement: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RankedEvent:
    cluster_id: str
    headline: str
    impact: ImpactAssessment
    heat_score: float
    importance_score: float
    confidence_score: float
    market_relevance_score: float
    final_score: float


@dataclass(slots=True)
class InstrumentDescriptor:
    symbol: str
    market: Market
    asset_type: str
    name: str
    sectors: list[str]
    themes: list[str]
    aliases: list[str]
    liquidity_score: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InstrumentMatch:
    cluster_id: str
    instrument: InstrumentDescriptor
    direction: Direction
    exposure_score: float
    reasons: list[str]


@dataclass(slots=True)
class RankedInstrument:
    cluster_id: str
    cluster_headline: str
    symbol: str
    market: Market
    asset_type: str
    name: str
    direction: Direction
    exposure_score: float
    liquidity_score: float
    impact_score: float
    confidence_score: float
    final_score: float
    reasons: list[str]


@dataclass(slots=True)
class AlertItem:
    cluster_id: str
    headline: str
    level: AlertLevel
    direction: Direction
    event_type: EventType
    final_score: float
    is_new: bool
    symbols: list[str]
    reason: str


@dataclass(slots=True)
class PipelineSnapshot:
    run_id: str
    created_at: datetime
    source_name: str
    raw_records: list[RawNewsRecord]
    documents: list[NewsDocument]
    clusters: list[EventCluster]
    ranked_events: list[RankedEvent]
    ranked_instruments: list[RankedInstrument]
    alerts: list[AlertItem] = field(default_factory=list)
    feature_blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
