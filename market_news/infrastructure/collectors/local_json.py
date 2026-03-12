from __future__ import annotations

import json
from pathlib import Path

from market_news.common import parse_datetime
from market_news.domain.models import RawNewsRecord


class LocalJSONCollector:
    def __init__(self, path: Path, name: str = "local-json") -> None:
        self.path = path
        self.name = name

    def collect(self) -> list[RawNewsRecord]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        records: list[RawNewsRecord] = []
        for item in payload:
            records.append(
                RawNewsRecord(
                    source_id=item["source_id"],
                    external_id=item.get("external_id"),
                    title=item["title"],
                    summary=item.get("summary", ""),
                    body=item.get("body", ""),
                    url=item.get("url", ""),
                    published_at=parse_datetime(item.get("published_at")),
                    language=item.get("language", "en"),
                    source_trust=float(item.get("source_trust", 0.5)),
                    entities=item.get("entities", []),
                    themes=item.get("themes", []),
                    regions=item.get("regions", []),
                    metadata=item.get("metadata", {}),
                )
            )
        return records

