from __future__ import annotations

from market_news.common import ensure_utc, significant_tokens, stable_id, unique_preserve, utcnow
from market_news.domain.models import NewsDocument, RawNewsRecord


class DefaultNormalizer:
    def normalize(self, records: list[RawNewsRecord]) -> list[NewsDocument]:
        fetched_at = utcnow()
        documents: list[NewsDocument] = []
        for record in records:
            published_at = record.published_at or fetched_at
            canonical_key = record.url.strip().lower()
            if not canonical_key:
                canonical_key = " ".join(significant_tokens(record.title))

            doc_id = stable_id(
                record.source_id,
                record.external_id or "",
                record.url,
                record.title,
                published_at.isoformat(),
            )
            documents.append(
                NewsDocument(
                    doc_id=doc_id,
                    source_id=record.source_id,
                    title=record.title.strip(),
                    summary=record.summary.strip(),
                    body=record.body.strip(),
                    url=record.url.strip(),
                    published_at=ensure_utc(published_at),
                    fetched_at=fetched_at,
                    language=record.language,
                    source_trust=record.source_trust,
                    canonical_key=canonical_key,
                    entities=unique_preserve(record.entities),
                    themes=unique_preserve(record.themes),
                    regions=unique_preserve(record.regions),
                    metadata=dict(record.metadata),
                )
            )
        return documents

