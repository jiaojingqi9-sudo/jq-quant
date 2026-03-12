from __future__ import annotations

import json
from pathlib import Path

from market_news.infrastructure.collectors.cninfo import (
    CNInfoLatestAnnouncementsCollector,
    CNInfoSpec,
)
from market_news.infrastructure.collectors.composite import CompositeCollector
from market_news.infrastructure.collectors.rss import FeedSpec, RSSCollector
from market_news.infrastructure.http import UrllibHttpClient


def build_live_collector(config_path: Path, user_agent: str) -> CompositeCollector:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    http_client = UrllibHttpClient(user_agent=user_agent)
    collectors = []
    for item in payload:
        source_type = item["type"]
        if source_type == "rss":
            collectors.append(
                RSSCollector(
                    http_client,
                    FeedSpec(
                        source_id=item["source_id"],
                        name=item["name"],
                        url=item["url"],
                        source_trust=float(item.get("source_trust", 0.8)),
                        language=item.get("language", "en"),
                        regions=item.get("regions", []),
                        themes=item.get("themes", []),
                        item_limit=int(item.get("item_limit", 20)),
                        include_title_patterns=item.get("include_title_patterns", []),
                        exclude_title_patterns=item.get("exclude_title_patterns", []),
                        metadata=item.get("metadata", {}),
                    ),
                )
            )
            continue
        if source_type == "cninfo_home":
            collectors.append(
                CNInfoLatestAnnouncementsCollector(
                    http_client,
                    CNInfoSpec(
                        source_id=item["source_id"],
                        name=item["name"],
                        url=item["url"],
                        source_trust=float(item.get("source_trust", 0.99)),
                        item_limit=int(item.get("item_limit", 12)),
                        language=item.get("language", "zh"),
                    ),
                )
            )
            continue
        raise ValueError(f"Unsupported live source type: {source_type}")

    return CompositeCollector("live-authoritative", collectors)

