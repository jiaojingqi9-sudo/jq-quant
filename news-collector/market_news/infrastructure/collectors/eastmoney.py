from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import re
from typing import Any

from market_news.common import utcnow
from market_news.domain.models import RawNewsRecord
from market_news.infrastructure.http import UrllibHttpClient


logger = logging.getLogger(__name__)

DEFAULT_TECH_FOCUS_INCLUDE_PATTERNS = [
    "AI|人工智能|算力|大模型|芯片|半导体|光模块|CPO|液冷|服务器|数据中心|智算|GPU|存储",
    "机器人|人形机器人|工业AI|机器视觉|自动驾驶|智驾|低空经济|无人机|卫星",
    "信创|网络安全|工业软件|云计算|量子|AR|VR|新材料|碳纤维|储能|固态电池|核电",
]

DEFAULT_TECH_FOCUS_BOOST_PATTERNS = [
    "板块|异动|焦点|概念|热点|题材|产业链|催化|景气|涨停|领涨|放量|订单|中标|量产|突破"
]

DEFAULT_TECH_FOCUS_EXCLUDE_PATTERNS = [
    "期货|原油|黄金|中东|航母|护航|足球|机场|海峡|农业|大米|咖啡|甲醇|伊朗|乌克兰|导弹|空袭|战事|冲突"
]


class EastmoneyCollector:
    name = "eastmoney"

    ANNOUNCEMENT_ENDPOINTS = {
        "ann-a": "https://np-anotice-stock.eastmoney.com/api/security/ann?page_index=1&page_size={page_size}&ann_type=A&client_source=web",
        "ann-h": "https://np-anotice-stock.eastmoney.com/api/security/ann?page_index=1&page_size={page_size}&ann_type=H&client_source=web",
    }
    NEWS_ENDPOINTS = {
        "news": "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_{page_size}_1_.html",
        "focus": "https://newsapi.eastmoney.com/kuaixun/v1/getlist_350_ajaxResult_{page_size}_1_.html",
        "global": "https://globalnewsapi.eastmoney.com/api/News/GetNewsList?type=102&pageindex=1&pagesize={page_size}",
    }

    def __init__(
        self,
        http_client: UrllibHttpClient,
        *,
        endpoints: list[str] | None = None,
        page_size: int = 50,
        global_page_size: int = 30,
        max_records_per_endpoint: int = 20,
        tech_focus_include_patterns: list[str] | None = None,
        tech_focus_boost_patterns: list[str] | None = None,
        tech_focus_exclude_patterns: list[str] | None = None,
    ) -> None:
        self.http_client = http_client
        self.endpoints = list(endpoints or ["ann-a", "ann-h", "news", "focus", "global"])
        self.page_size = page_size
        self.global_page_size = global_page_size
        self.max_records_per_endpoint = max_records_per_endpoint
        self.tech_focus_include_patterns = [
            re.compile(pattern, flags=re.IGNORECASE)
            for pattern in (tech_focus_include_patterns or DEFAULT_TECH_FOCUS_INCLUDE_PATTERNS)
        ]
        self.tech_focus_boost_patterns = [
            re.compile(pattern, flags=re.IGNORECASE)
            for pattern in (tech_focus_boost_patterns or DEFAULT_TECH_FOCUS_BOOST_PATTERNS)
        ]
        self.tech_focus_exclude_patterns = [
            re.compile(pattern, flags=re.IGNORECASE)
            for pattern in (tech_focus_exclude_patterns or DEFAULT_TECH_FOCUS_EXCLUDE_PATTERNS)
        ]

    def collect(self) -> list[RawNewsRecord]:
        records: list[RawNewsRecord] = []
        for endpoint in self.endpoints:
            try:
                if endpoint in self.ANNOUNCEMENT_ENDPOINTS:
                    records.extend(self._collect_announcements(endpoint))
                elif endpoint in self.NEWS_ENDPOINTS:
                    records.extend(self._collect_news(endpoint))
                else:
                    logger.warning("eastmoney endpoint is not supported: %s", endpoint)
            except Exception as exc:
                logger.warning("eastmoney endpoint failed: %s (%s)", endpoint, exc)
                continue
        return records

    def _collect_announcements(self, endpoint: str) -> list[RawNewsRecord]:
        response = self.http_client.get_text(
            self.ANNOUNCEMENT_ENDPOINTS[endpoint].format(page_size=self.page_size),
            headers=self._headers(),
        )
        payload = json.loads(response.text)
        items = payload.get("data", {}).get("list", [])
        records: list[RawNewsRecord] = []
        for item in items[: self.max_records_per_endpoint]:
            title = str(item.get("title") or item.get("title_ch") or "").strip()
            if not title:
                continue
            code_info = (item.get("codes") or [{}])[0]
            stock_code = str(code_info.get("stock_code") or "").strip()
            short_name = str(code_info.get("short_name") or "").strip()
            art_code = str(item.get("art_code") or "")
            body = str(item.get("content") or item.get("CONTENT") or "").strip()
            summary = body[:160].strip() if body else title
            published_at = self._parse_datetime(
                item.get("notice_date") or item.get("display_time") or item.get("eiTime")
            )
            detail_url = ""
            if stock_code and art_code:
                detail_url = f"https://data.eastmoney.com/notices/detail/{stock_code}/{art_code}.html"
            elif art_code:
                detail_url = f"https://data.eastmoney.com/notices/detail/{art_code}.html"
            metadata = {
                "endpoint": endpoint,
                "stock_code": stock_code,
                "short_name": short_name,
                "ann_type": code_info.get("ann_type", ""),
                "column_names": [
                    str(column.get("column_name", "")).strip()
                    for column in item.get("columns", [])
                    if str(column.get("column_name", "")).strip()
                ],
            }
            entities = [value for value in [short_name, stock_code] if value]
            regions = ["CN"] if endpoint == "ann-a" else ["HK"]
            records.append(
                RawNewsRecord(
                    source_id="eastmoney-ann",
                    external_id=art_code or title,
                    title=title,
                    summary=summary,
                    body=body,
                    url=detail_url,
                    published_at=published_at,
                    language="zh",
                    source_trust=0.82,
                    entities=entities,
                    themes=[],
                    regions=regions,
                    metadata=metadata,
                )
            )
        return records

    def _collect_news(self, endpoint: str) -> list[RawNewsRecord]:
        page_size = self.global_page_size if endpoint == "global" else self.page_size
        response = self.http_client.get_text(
            self.NEWS_ENDPOINTS[endpoint].format(page_size=page_size),
            headers=self._headers(),
        )
        payload = self._parse_news_payload(endpoint, response.text)
        items = []
        if endpoint in ("news", "focus"):
            items = payload.get("LivesList", [])
        else:
            items = payload.get("data", {}).get("list", []) or payload.get("list", [])
        records: list[RawNewsRecord] = []
        source_id_map = {
            "news": ("eastmoney-724", 0.82),
            "focus": ("eastmoney-focus", 0.88),
            "global": ("eastmoney-news", 0.82),
        }
        source_id, source_trust = source_id_map.get(endpoint, ("eastmoney-news", 0.82))
        for item in items[: self.max_records_per_endpoint]:
            title = str(item.get("title") or item.get("TITLE") or "").strip()
            if not title:
                continue
            body = str(
                item.get("digest")
                or item.get("simdigest")
                or item.get("CONTENT")
                or item.get("content")
                or ""
            ).strip()
            article_id = str(item.get("newsid") or item.get("id") or item.get("ArticleID") or "")
            url = str(item.get("url_w") or item.get("url") or item.get("Url") or "").strip()
            published_at = self._parse_datetime(
                item.get("showtime")
                or item.get("ordertime")
                or item.get("NOTICE_DATE")
                or item.get("publishTime")
            )
            records.append(
                RawNewsRecord(
                    source_id=source_id,
                    external_id=article_id or title,
                    title=title,
                    summary=(body[:160].strip() if body else title),
                    body=body,
                    url=url,
                    published_at=published_at,
                    language="zh",
                    source_trust=source_trust,
                    entities=[],
                    themes=[],
                    regions=["CN", "HK"],
                    metadata={
                        "endpoint": endpoint,
                        "column": item.get("column", ""),
                        "newstype": item.get("newstype", ""),
                    },
                )
            )
        return records

    def _is_tech_focus(self, *, title: str, body: str) -> bool:
        text = " ".join(part for part in [title, body] if part).strip()
        if not text:
            return False
        if any(pattern.search(text) for pattern in self.tech_focus_exclude_patterns):
            return False
        include_hit = any(pattern.search(text) for pattern in self.tech_focus_include_patterns)
        return include_hit

    def _is_focus_boosted(self, *, title: str, body: str) -> bool:
        text = " ".join(part for part in [title, body] if part).strip()
        if not text:
            return False
        if any(pattern.search(text) for pattern in self.tech_focus_exclude_patterns):
            return False
        return any(pattern.search(text) for pattern in self.tech_focus_boost_patterns) or len(title) <= 24

    def _parse_news_payload(self, endpoint: str, text: str) -> dict[str, Any]:
        cleaned = text.strip().lstrip("\ufeff").strip()
        if endpoint in {"news", "focus"} and cleaned.startswith("var ajaxResult="):
            cleaned = cleaned[len("var ajaxResult="):]
        if endpoint in {"news", "focus"} and "}var ajaxResult=" in cleaned:
            cleaned = cleaned.split("}var ajaxResult=", 1)[0] + "}"
        if endpoint == "global" and cleaned.lower().startswith("<!doctype html"):
            return {}
        if cleaned and cleaned[0] not in "{[":
            return {}
        cleaned = cleaned.rstrip(";").strip()
        payload = json.loads(cleaned)
        if not isinstance(payload, dict):
            raise ValueError(f"Eastmoney endpoint returned non-object payload: {endpoint}")
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Referer": "https://www.eastmoney.com/",
            "Accept": "application/json, text/plain, */*",
        }

    def _parse_datetime(self, value: object) -> datetime:
        if value is None:
            return utcnow()
        text = str(value).strip()
        for pattern in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H:%M:%S:%f",
        ):
            try:
                return datetime.strptime(text, pattern).replace(tzinfo=UTC)
            except ValueError:
                continue
        return utcnow()
