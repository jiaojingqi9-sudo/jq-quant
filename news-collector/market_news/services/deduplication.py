from __future__ import annotations

from market_news.common import jaccard_similarity, significant_tokens
from market_news.domain.models import NewsDocument


class FingerprintDeduplicator:
    def __init__(self, similarity_threshold: float = 0.82) -> None:
        self.similarity_threshold = similarity_threshold

    def deduplicate(self, documents: list[NewsDocument]) -> list[NewsDocument]:
        by_canonical_key: dict[str, NewsDocument] = {}
        for document in sorted(
            documents,
            key=lambda item: (item.source_trust, item.published_at.timestamp()),
            reverse=True,
        ):
            existing = by_canonical_key.get(document.canonical_key)
            if existing is None or document.source_trust > existing.source_trust:
                by_canonical_key[document.canonical_key] = document

        deduplicated: list[NewsDocument] = []
        for document in sorted(
            by_canonical_key.values(),
            key=lambda item: item.published_at.timestamp(),
            reverse=True,
        ):
            tokens = significant_tokens(document.title)
            if any(
                jaccard_similarity(tokens, significant_tokens(existing.title))
                >= self.similarity_threshold
                for existing in deduplicated
            ):
                continue
            deduplicated.append(document)
        return deduplicated

