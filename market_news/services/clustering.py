from __future__ import annotations

from dataclasses import dataclass, field

from market_news.common import jaccard_similarity, significant_tokens, stable_id, unique_preserve
from market_news.domain.models import EventCluster, NewsDocument


GENERIC_CLUSTER_TOKENS = {
    "company",
    "financial",
    "industrial-chain",
    "market",
    "official-policy",
    "policy",
    "state-owned-enterprise",
    "technology",
    "trade",
}


@dataclass
class _WorkingCluster:
    anchor_tokens: set[str]
    key_tokens: set[str]
    documents: list[NewsDocument] = field(default_factory=list)


class KeywordEventClusterer:
    def __init__(self, similarity_threshold: float = 0.35) -> None:
        self.similarity_threshold = similarity_threshold

    def cluster(self, documents: list[NewsDocument]) -> list[EventCluster]:
        working_clusters: list[_WorkingCluster] = []
        for document in sorted(documents, key=lambda item: item.published_at.timestamp()):
            anchor_tokens = set(self._anchor_tokens(document))
            document_tokens = set(self._cluster_tokens(document))
            best_match: _WorkingCluster | None = None
            best_score = 0.0
            for cluster in working_clusters:
                anchor_score = jaccard_similarity(anchor_tokens, cluster.anchor_tokens)
                keyword_score = jaccard_similarity(document_tokens, cluster.key_tokens)
                score = max(anchor_score, (0.65 * anchor_score) + (0.35 * keyword_score))
                if score > best_score:
                    best_score = score
                    best_match = cluster
            if best_match is not None and (
                best_score >= self.similarity_threshold
                or jaccard_similarity(anchor_tokens, best_match.anchor_tokens) >= 0.2
            ):
                best_match.documents.append(document)
                best_match.anchor_tokens.update(anchor_tokens)
                best_match.key_tokens.update(document_tokens)
            else:
                working_clusters.append(
                    _WorkingCluster(
                        anchor_tokens=anchor_tokens,
                        key_tokens=document_tokens,
                        documents=[document],
                    )
                )

        clusters = [self._to_cluster(cluster) for cluster in working_clusters]
        clusters.sort(key=lambda item: item.last_seen_at.timestamp(), reverse=True)
        return clusters

    def _cluster_tokens(self, document: NewsDocument) -> list[str]:
        title_tokens = significant_tokens(document.title, extra_stop_words=document.regions)[:4]
        return unique_preserve(
            self._specific_tokens(document.themes)
            + list(document.entities)
            + title_tokens
        )

    def _anchor_tokens(self, document: NewsDocument) -> list[str]:
        return unique_preserve(self._specific_tokens(document.themes) + list(document.entities))

    def _specific_tokens(self, values: list[str]) -> list[str]:
        return [
            value
            for value in values
            if value.strip().lower() not in GENERIC_CLUSTER_TOKENS
        ]

    def _to_cluster(self, working_cluster: _WorkingCluster) -> EventCluster:
        representative = max(
            working_cluster.documents,
            key=lambda item: (item.source_trust, item.published_at.timestamp()),
        )
        documents = sorted(
            working_cluster.documents,
            key=lambda item: item.published_at.timestamp(),
            reverse=True,
        )
        themes = unique_preserve(theme for doc in documents for theme in doc.themes)
        entities = unique_preserve(entity for doc in documents for entity in doc.entities)
        regions = unique_preserve(region for doc in documents for region in doc.regions)
        sectors = unique_preserve(
            sector
            for doc in documents
            for sector in doc.metadata.get("sectors", [])
            if isinstance(sector, str)
        )
        source_ids = unique_preserve(doc.source_id for doc in documents)
        story_key = " ".join(sorted(working_cluster.key_tokens)[:6]) or representative.title.lower()
        return EventCluster(
            cluster_id=stable_id(story_key, representative.title),
            story_key=story_key,
            headline=representative.title,
            summary=representative.summary,
            documents=documents,
            entities=entities,
            themes=themes,
            sectors=sectors,
            regions=regions,
            source_ids=source_ids,
            first_seen_at=min(doc.published_at for doc in documents),
            last_seen_at=max(doc.published_at for doc in documents),
        )
