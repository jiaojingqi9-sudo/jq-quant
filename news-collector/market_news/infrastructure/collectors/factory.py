from __future__ import annotations

import json
from pathlib import Path

from market_news.infrastructure.collectors.cninfo import (
    CNInfoLatestAnnouncementsCollector,
    CNInfoSpec,
)
from market_news.infrastructure.collectors.cls import ClsTelegraphCollector
from market_news.infrastructure.collectors.composite import CompositeCollector
from market_news.infrastructure.collectors.eastmoney import EastmoneyCollector
from market_news.infrastructure.collectors.eastmoney_topic import (
    EastmoneyTopicCollector,
    EastmoneyTopicSpec,
)
from market_news.infrastructure.collectors.full_text import FullTextEnrichingCollector
from market_news.infrastructure.collectors.gelonghui import GelonghuiLiveCollector
from market_news.infrastructure.collectors.html_source import (
    HtmlListDetailCollector,
    HtmlSourceSpec,
)
from market_news.infrastructure.collectors.rss import FeedSpec, RSSCollector
from market_news.infrastructure.collectors.sfc_offer_periods import (
    SFCOfferPeriodsCollector,
    SFCOfferPeriodsSpec,
)
from market_news.infrastructure.collectors.weibo import WeiboCollector
from market_news.infrastructure.collectors.xueqiu import XueqiuCollector
from market_news.infrastructure.cookie_store import resolve_cookie_path
from market_news.infrastructure.http import UrllibHttpClient


def build_live_collector(config_path: Path, user_agent: str) -> CompositeCollector:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    default_http_client = UrllibHttpClient(user_agent=user_agent, timeout=8)
    official_http_client = UrllibHttpClient(user_agent=user_agent, timeout=12)
    collectors = []
    if isinstance(payload, list):
        payload = {"legacy_sources": payload}

    cninfo_config = payload.get("cninfo", {})
    if isinstance(cninfo_config, dict) and bool(cninfo_config.get("enabled", True)):
        collectors.append(
            CNInfoLatestAnnouncementsCollector(
                default_http_client,
                CNInfoSpec(
                    source_id=cninfo_config.get("source_id", "cninfo_latest"),
                    name=cninfo_config.get("name", "cninfo-latest-announcements"),
                    url=cninfo_config.get("url", "https://www.cninfo.com.cn/"),
                    source_trust=float(cninfo_config.get("source_trust", 0.99)),
                    item_limit=int(cninfo_config.get("item_limit", 12)),
                    language=cninfo_config.get("language", "zh"),
                ),
            )
        )

    rss_config = payload.get("rss", {})
    for item in rss_config.get("feeds", []) if isinstance(rss_config, dict) else []:
        # RSS feeds honour `enabled` the same way html_sources do. Without this
        # check the flag was silently ignored, so a feed marked disabled (e.g. a
        # dead upstream returning 404 every cycle) kept being fetched forever.
        if not bool(item.get("enabled", True)):
            continue
        client = _select_rss_client(item=item, default_http_client=default_http_client, official_http_client=official_http_client)
        collectors.append(
            RSSCollector(
                client,
                FeedSpec(
                    source_id=item["source_id"],
                    name=item.get("name", item["source_id"]),
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

    eastmoney_config = payload.get("eastmoney", {})
    if isinstance(eastmoney_config, dict) and bool(eastmoney_config.get("enabled", False)):
        collectors.append(
            EastmoneyCollector(
                default_http_client,
                endpoints=eastmoney_config.get("endpoints", ["ann-a", "ann-h", "news"]),
                page_size=int(eastmoney_config.get("page_size", 50)),
                global_page_size=int(eastmoney_config.get("global_page_size", 30)),
                max_records_per_endpoint=int(eastmoney_config.get("max_records_per_endpoint", 20)),
                tech_focus_include_patterns=eastmoney_config.get("tech_focus_include_patterns", []),
                tech_focus_boost_patterns=eastmoney_config.get("tech_focus_boost_patterns", []),
                tech_focus_exclude_patterns=eastmoney_config.get("tech_focus_exclude_patterns", []),
            )
        )

    eastmoney_topic_config = payload.get("eastmoney_topic", {})
    if isinstance(eastmoney_topic_config, dict) and bool(eastmoney_topic_config.get("enabled", False)):
        collectors.append(
            EastmoneyTopicCollector(
                default_http_client,
                EastmoneyTopicSpec(
                    source_id=eastmoney_topic_config.get("source_id", "eastmoney-topic"),
                    name=eastmoney_topic_config.get("name", "eastmoney-topic"),
                    source_trust=float(eastmoney_topic_config.get("source_trust", 0.83)),
                    language=eastmoney_topic_config.get("language", "zh"),
                    homepage_limit=int(eastmoney_topic_config.get("homepage_limit", 10)),
                    history_limit=int(eastmoney_topic_config.get("history_limit", 10)),
                    metadata=eastmoney_topic_config.get("metadata", {}),
                ),
            )
        )

    sfc_offer_periods_config = payload.get("sfc_offer_periods", {})
    if isinstance(sfc_offer_periods_config, dict) and bool(sfc_offer_periods_config.get("enabled", False)):
        collectors.append(
            SFCOfferPeriodsCollector(
                official_http_client,
                SFCOfferPeriodsSpec(
                    source_id=sfc_offer_periods_config.get("source_id", "sfc-offer-periods"),
                    name=sfc_offer_periods_config.get("name", "sfc-offer-periods"),
                    url=sfc_offer_periods_config.get(
                        "url",
                        "https://www.sfc.hk/en/Regulatory-functions/Corporates/Takeovers-and-mergers/offer-periods",
                    ),
                    source_trust=float(sfc_offer_periods_config.get("source_trust", 0.99)),
                    language=sfc_offer_periods_config.get("language", "en"),
                    item_limit=int(sfc_offer_periods_config.get("item_limit", 10)),
                    metadata=sfc_offer_periods_config.get("metadata", {}),
                ),
            )
        )

    cls_config = payload.get("cls", {})
    if isinstance(cls_config, dict) and bool(cls_config.get("enabled", False)):
        last_time_path = Path(str(cls_config.get("last_time_file", "cls_last_time.txt")))
        if not last_time_path.is_absolute():
            last_time_path = config_path.parent.parent / "data" / last_time_path
        collectors.append(
            ClsTelegraphCollector(
                http_client=default_http_client,
                page_size=int(cls_config.get("page_size", 20)),
                min_level=int(cls_config.get("min_level", 1)),
                last_time_file=last_time_path,
            )
        )

    gelonghui_config = payload.get("gelonghui", {})
    if isinstance(gelonghui_config, dict) and bool(gelonghui_config.get("enabled", False)):
        collectors.append(
            GelonghuiLiveCollector(
                default_http_client,
                item_limit=int(gelonghui_config.get("item_limit", 10)),
                source_trust=float(gelonghui_config.get("source_trust", 0.76)),
            )
        )

    for item in payload.get("html_sources", []):
        if not bool(item.get("enabled", True)):
            continue
        html_client = _select_html_client(
            item=item,
            default_http_client=default_http_client,
            official_http_client=official_http_client,
            user_agent=user_agent,
        )
        collectors.append(
            HtmlListDetailCollector(
                html_client,
                HtmlSourceSpec(
                    source_id=item["source_id"],
                    name=item["name"],
                    url=item["url"],
                    source_trust=float(item.get("source_trust", 0.8)),
                    language=item.get("language", "zh"),
                    regions=item.get("regions", []),
                    themes=item.get("themes", []),
                    item_limit=int(item.get("item_limit", 12)),
                    detail_fetch_limit=int(item.get("detail_fetch_limit", 6)),
                    include_link_patterns=item.get("include_link_patterns", []),
                    exclude_link_patterns=item.get("exclude_link_patterns", []),
                    include_title_patterns=item.get("include_title_patterns", []),
                    exclude_title_patterns=item.get("exclude_title_patterns", []),
                    body_container_patterns=item.get("body_container_patterns", []),
                    metadata=item.get("metadata", {}),
                ),
            )
        )

    weibo_config = payload.get("weibo", {})
    if isinstance(weibo_config, dict) and bool(weibo_config.get("enabled", False)):
        sleep_range = weibo_config.get("sleep_range_seconds", [0.4, 1.0])
        if not isinstance(sleep_range, (list, tuple)) or len(sleep_range) != 2:
            sleep_range = [0.4, 1.0]
        collectors.append(
            WeiboCollector(
                queries=weibo_config.get("queries", []),
                cookie_path=resolve_cookie_path(weibo_config.get("cookie_path", "~/.market_news/weibo_cookies.json")),
                http_client=default_http_client,
                max_results_per_query=int(weibo_config.get("max_results_per_query", 20)),
                max_queries_per_cycle=(
                    int(weibo_config.get("max_queries_per_cycle", 0))
                    if int(weibo_config.get("max_queries_per_cycle", 0) or 0) > 0
                    else None
                ),
                sleep_range=(float(sleep_range[0]), float(sleep_range[1])),
                browser_timeout_ms=int(weibo_config.get("browser_timeout_ms", 15000)),
                browser_warmup_ms=int(weibo_config.get("browser_warmup_ms", 1200)),
                browser_fallback_enabled=bool(weibo_config.get("browser_fallback_enabled", False)),
            )
        )

    xueqiu_config = payload.get("xueqiu", {})
    if isinstance(xueqiu_config, dict) and bool(xueqiu_config.get("enabled", False)):
        collectors.append(
            XueqiuCollector(
                queries=xueqiu_config.get("queries", []),
                cookie_path=resolve_cookie_path(xueqiu_config.get("cookie_path", "~/.market_news/xueqiu_cookies.json")),
                http_client=default_http_client,
                max_results_per_query=int(xueqiu_config.get("max_results_per_query", 20)),
                browser_timeout_ms=int(xueqiu_config.get("browser_timeout_ms", 15000)),
                browser_warmup_ms=int(xueqiu_config.get("browser_warmup_ms", 8000)),
            )
        )

    for item in payload.get("legacy_sources", []):
        if not bool(item.get("enabled", True)):
            continue
        if item.get("type") == "html_list_detail":
            collectors.append(
                HtmlListDetailCollector(
                    default_http_client,
                    HtmlSourceSpec(
                        source_id=item["source_id"],
                        name=item["name"],
                        url=item["url"],
                        source_trust=float(item.get("source_trust", 0.8)),
                        language=item.get("language", "zh"),
                        regions=item.get("regions", []),
                        themes=item.get("themes", []),
                        item_limit=int(item.get("item_limit", 12)),
                        detail_fetch_limit=int(item.get("detail_fetch_limit", 6)),
                        include_link_patterns=item.get("include_link_patterns", []),
                        exclude_link_patterns=item.get("exclude_link_patterns", []),
                        include_title_patterns=item.get("include_title_patterns", []),
                        exclude_title_patterns=item.get("exclude_title_patterns", []),
                        body_container_patterns=item.get("body_container_patterns", []),
                        metadata=item.get("metadata", {}),
                    ),
                )
            )

    composite = CompositeCollector("live-authoritative", collectors)
    full_text_config = payload.get("full_text", {})
    if isinstance(full_text_config, dict) and bool(full_text_config.get("enabled", True)):
        detail_http_client = UrllibHttpClient(
            user_agent=user_agent,
            timeout=int(full_text_config.get("timeout", 18)),
        )
        return FullTextEnrichingCollector(
            composite,
            detail_http_client,
            enabled_source_ids=full_text_config.get("source_ids", []),
            min_body_length=int(full_text_config.get("min_body_length", 220)),
            max_records=int(full_text_config.get("max_records", 36)),
            max_body_length=int(full_text_config.get("max_body_length", 24000)),
        )
    return composite


def _select_rss_client(
    *,
    item: dict[str, object],
    default_http_client: UrllibHttpClient,
    official_http_client: UrllibHttpClient,
) -> UrllibHttpClient:
    url = str(item.get("url", "")).lower()
    source_id = str(item.get("source_id", "")).lower()
    if "xinhuanet" in url or source_id.startswith("xinhua"):
        return official_http_client
    return default_http_client


def _select_html_client(
    *,
    item: dict[str, object],
    default_http_client: UrllibHttpClient,
    official_http_client: UrllibHttpClient,
    user_agent: str,
) -> UrllibHttpClient:
    timeout = item.get("timeout")
    if timeout is not None:
        return UrllibHttpClient(user_agent=user_agent, timeout=int(timeout))
    if float(item.get("source_trust", 0.0)) >= 0.9:
        return official_http_client
    return default_http_client
