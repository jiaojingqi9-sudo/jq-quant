from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
from urllib.parse import urlencode

from market_news.common import utcnow, unique_preserve
from market_news.domain.models import RawNewsRecord
from market_news.infrastructure.http import UrllibHttpClient


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class EastmoneyTopicSpec:
    source_id: str = "eastmoney-topic"
    name: str = "eastmoney-topic"
    source_trust: float = 0.83
    language: str = "zh"
    homepage_limit: int = 10
    history_limit: int = 10
    metadata: dict[str, object] = field(default_factory=dict)


class EastmoneyTopicCollector:
    name = "eastmoney-topic"

    HOME_PAGE_PATH = "newtopic/api/Topic/HomePageListRead"
    HISTORY_PATH = "newtopic/api/Topic/HistoryTopicRead"

    def __init__(self, http_client: UrllibHttpClient, spec: EastmoneyTopicSpec) -> None:
        self.http_client = http_client
        self.spec = spec
        self.name = spec.name

    def collect(self) -> list[RawNewsRecord]:
        records: list[RawNewsRecord] = []
        seen_ids: set[str] = set()
        for record in self._collect_homepage_topics():
            if record.external_id in seen_ids:
                continue
            seen_ids.add(record.external_id or record.title)
            records.append(record)
        for record in self._collect_history_topics():
            if record.external_id in seen_ids:
                continue
            seen_ids.add(record.external_id or record.title)
            records.append(record)
        return records

    def _collect_homepage_topics(self) -> list[RawNewsRecord]:
        payload = self._fetch_json(
            self.HOME_PAGE_PATH,
            {
                "ps": self.spec.homepage_limit,
                "p": 1,
                "type": 0,
            },
        )
        items = payload.get("re", [])
        if not isinstance(items, list):
            return []

        records: list[RawNewsRecord] = []
        for index, item in enumerate(items[: self.spec.homepage_limit], start=1):
            if not isinstance(item, dict):
                continue
            record = self._build_record(
                topic_kind="homepage",
                item=item,
                published_at=utcnow(),
                rank=index,
            )
            if record is not None:
                records.append(record)
        return records

    def _collect_history_topics(self) -> list[RawNewsRecord]:
        payload = self._fetch_json(
            self.HISTORY_PATH,
            {
                "ps": self.spec.history_limit,
                "p": 1,
                "type": 0,
            },
        )
        groups = payload.get("re", [])
        if not isinstance(groups, list):
            return []

        records: list[RawNewsRecord] = []
        rank = 0
        for group in groups:
            if not isinstance(group, dict):
                continue
            published_at = self._parse_iso_time(group.get("htime"))
            history_topics = group.get("historyTopic", [])
            if not isinstance(history_topics, list):
                continue
            for topic in history_topics[: self.spec.history_limit]:
                if not isinstance(topic, dict):
                    continue
                rank += 1
                record = self._build_record(
                    topic_kind="history",
                    item=topic,
                    published_at=published_at,
                    rank=rank,
                )
                if record is not None:
                    records.append(record)
        return records

    def _build_record(
        self,
        *,
        topic_kind: str,
        item: dict[str, object],
        published_at: datetime,
        rank: int,
    ) -> RawNewsRecord | None:
        htid = str(item.get("htid", "")).strip()
        title = self._clean_text(
            str(
                item.get("nickname")
                or item.get("name")
                or item.get("title")
                or item.get("desc")
                or htid
            )
        )
        if not title:
            return None

        desc = self._clean_text(str(item.get("desc", "") or ""))
        introduction = self._clean_text(str(item.get("introduction", "") or ""))
        stock_list = self._normalize_stock_list(item.get("stock_list", []))
        stock_names = [stock["name"] for stock in stock_list if stock.get("name")]
        stock_codes = [stock["code"] for stock in stock_list if stock.get("code")]
        summary = introduction or desc or self._stock_summary(stock_names)
        body = desc or self._stock_summary(stock_names)
        entities = unique_preserve(
            [
                *([title] if title else []),
                *stock_names,
                *stock_codes,
            ]
        )
        regions = self._derive_regions(stock_list)
        metadata = {
            "topic_kind": topic_kind,
            "htid": htid,
            "rank": rank,
            "post_number": item.get("postNumber"),
            "click_number": item.get("clickNumber"),
            "collect_number": item.get("collectNumber"),
            "is_recommend": item.get("isRecommend"),
            "has_collect": item.get("has_collect"),
            "fire_style": item.get("fireStyle"),
            "stock_list": stock_list,
        }
        metadata.update(self.spec.metadata)
        return RawNewsRecord(
            source_id=self.spec.source_id,
            external_id=htid or title,
            title=title,
            summary=summary,
            body=body,
            url=self._topic_url(htid),
            published_at=published_at,
            language=self.spec.language,
            source_trust=self.spec.source_trust,
            entities=entities,
            themes=[],
            regions=regions,
            metadata=metadata,
        )

    def _fetch_json(self, path: str, params: dict[str, object]) -> dict[str, object]:
        url = f"https://gubatopic.eastmoney.com/interface/GetData.aspx?path={path}"
        payload = {
            "param": urlencode(params, doseq=True),
            "path": path,
            "env": 2,
        }
        response = self.http_client.post_text(
            url,
            headers=self._headers(path),
            data=urlencode(payload).encode("utf-8"),
        )
        parsed = json.loads(response.text)
        if not isinstance(parsed, dict):
            raise ValueError(f"eastmoney topic endpoint returned non-object payload: {path}")
        return parsed

    def _headers(self, path: str) -> dict[str, str]:
        return {
            "Referer": "https://gubatopic.eastmoney.com/",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://gubatopic.eastmoney.com",
        }

    def _topic_url(self, htid: str) -> str:
        if not htid:
            return "https://gubatopic.eastmoney.com/"
        return f"https://gubatopic.eastmoney.com/topic_v2.html?htid={htid}"

    def _normalize_stock_list(self, stock_list: object) -> list[dict[str, str]]:
        if not isinstance(stock_list, list):
            return []
        normalized: list[dict[str, str]] = []
        for item in stock_list:
            if not isinstance(item, dict):
                continue
            code = self._clean_text(str(item.get("code", "") or ""))
            name = self._clean_text(str(item.get("name", "") or ""))
            if not code and not name:
                continue
            normalized.append(
                {
                    "code": code,
                    "name": name,
                    "market": self._clean_text(str(item.get("market", "") or "")),
                    "qmarket": self._clean_text(str(item.get("qmarket", "") or "")),
                    "qcode": self._clean_text(str(item.get("qcode", "") or "")),
                    "third_app_code": self._clean_text(str(item.get("third_app_code", "") or "")),
                }
            )
        return normalized

    def _derive_regions(self, stock_list: list[dict[str, str]]) -> list[str]:
        regions: list[str] = []
        for item in stock_list:
            code = item.get("code", "").lower()
            if code.startswith("hk") or item.get("market") in {"106", "116"}:
                regions.append("HK")
            elif code:
                regions.append("CN-A")
        if not regions:
            return ["GLOBAL"]
        return unique_preserve(regions)

    def _stock_summary(self, stock_names: list[str]) -> str:
        if not stock_names:
            return ""
        preview = "、".join(stock_names[:6])
        if len(stock_names) > 6:
            preview += " 等"
        return f"相关标的：{preview}"

    def _clean_text(self, value: str) -> str:
        text = value.strip()
        text = text.replace("\u3000", " ")
        text = " ".join(text.split())
        return text

    def _parse_iso_time(self, value: object) -> datetime:
        text = str(value or "").strip()
        if not text:
            return utcnow()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return utcnow()
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
