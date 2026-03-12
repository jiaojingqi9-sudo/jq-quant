from __future__ import annotations

import sys

from market_news.domain.models import RawNewsRecord
from market_news.domain.ports import Collector


class CompositeCollector:
    def __init__(self, name: str, collectors: list[Collector]) -> None:
        self.name = name
        self.collectors = collectors

    def collect(self) -> list[RawNewsRecord]:
        records: list[RawNewsRecord] = []
        for collector in self.collectors:
            try:
                records.extend(collector.collect())
            except Exception as exc:  # pragma: no cover - exercised in live mode
                print(
                    f"[warn] collector {collector.name} failed: {exc}",
                    file=sys.stderr,
                )
        return records
