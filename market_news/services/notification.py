from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from market_news.domain.models import AlertLevel


LEVEL_ORDER = {
    AlertLevel.MEDIUM: 1,
    AlertLevel.HIGH: 2,
    AlertLevel.CRITICAL: 3,
}

LEVEL_LABELS = {
    "critical": "紧急",
    "high": "高优先级",
    "medium": "中优先级",
}

DIRECTION_LABELS = {
    "positive": "利好",
    "negative": "利空",
    "neutral": "中性",
}

EVENT_TYPE_LABELS = {
    "company": "公司",
    "industry": "行业",
    "policy": "政策",
    "macro": "宏观",
    "commodity": "商品",
    "regulation": "监管",
    "unknown": "其他",
}


@dataclass(slots=True)
class NotificationPlan:
    channel: str
    target: str
    message: str
    cluster_ids: list[str]
    alert_count: int
    preview_path: Path


class AlertDigestBuilder:
    def __init__(
        self,
        *,
        min_level: AlertLevel = AlertLevel.HIGH,
        max_alerts: int = 3,
        include_existing: bool = False,
    ) -> None:
        self.min_level = min_level
        self.max_alerts = max_alerts
        self.include_existing = include_existing

    def compose(
        self,
        payload: dict[str, object],
        *,
        channel: str,
        target: str,
        sent_cluster_ids: set[str] | None = None,
        preview_path: Path,
    ) -> NotificationPlan | None:
        sent_cluster_ids = sent_cluster_ids or set()
        event_lookup = self._build_event_lookup(payload)
        selected_alerts: list[dict[str, object]] = []
        for alert in payload.get("alerts", []):
            if not isinstance(alert, dict):
                continue
            cluster_id = str(alert.get("cluster_id", "")).strip()
            if not cluster_id:
                continue
            if not self._passes_level(str(alert.get("level", "medium"))):
                continue
            if not self.include_existing and not bool(alert.get("is_new", False)):
                continue
            if cluster_id in sent_cluster_ids:
                continue
            selected_alerts.append(alert)
            if len(selected_alerts) >= self.max_alerts:
                break

        if not selected_alerts:
            return None

        created_at = str(payload.get("created_at", ""))
        source = str(payload.get("source", "market-news"))
        lines = [
            "市场新闻提醒",
            f"时间: {created_at}",
            f"来源: {source}",
            f"新增高优先级提醒: {len(selected_alerts)} 条",
            "",
        ]

        cluster_ids: list[str] = []
        for index, alert in enumerate(selected_alerts, start=1):
            cluster_id = str(alert.get("cluster_id", ""))
            cluster_ids.append(cluster_id)
            event = event_lookup.get(cluster_id, {})
            level = LEVEL_LABELS.get(str(alert.get("level", "")), str(alert.get("level", "")))
            direction = DIRECTION_LABELS.get(
                str(alert.get("direction", "")),
                str(alert.get("direction", "")),
            )
            event_type = EVENT_TYPE_LABELS.get(
                str(alert.get("event_type", "")),
                str(alert.get("event_type", "")),
            )
            symbols = self._resolve_symbols(alert, event)
            rationale = self._resolve_rationale(alert, event)
            score = int(round(float(alert.get("final_score", 0))))

            lines.append(
                f"{index}. [{level}][{direction}][{event_type}] {self._truncate(str(alert.get('headline', '')), 110)}"
            )
            lines.append(f"   标的: {', '.join(symbols) if symbols else 'n/a'}")
            lines.append(f"   要点: {rationale} | 分数: {score}")

        message = "\n".join(lines).strip()
        return NotificationPlan(
            channel=channel,
            target=target,
            message=message,
            cluster_ids=cluster_ids,
            alert_count=len(cluster_ids),
            preview_path=preview_path,
        )

    def _passes_level(self, level_value: str) -> bool:
        try:
            level = AlertLevel(level_value)
        except ValueError:
            return False
        return LEVEL_ORDER[level] >= LEVEL_ORDER[self.min_level]

    def _build_event_lookup(self, payload: dict[str, object]) -> dict[str, dict[str, object]]:
        lookup: dict[str, dict[str, object]] = {}
        for key in [
            "top_events",
            "negative_risks",
            "positive_catalysts",
            "watchlist",
        ]:
            for event in payload.get(key, []):
                if not isinstance(event, dict):
                    continue
                cluster_id = str(event.get("cluster_id", "")).strip()
                if cluster_id and cluster_id not in lookup:
                    lookup[cluster_id] = event
        return lookup

    def _resolve_symbols(
        self,
        alert: dict[str, object],
        event: dict[str, object],
    ) -> list[str]:
        symbols = [
            str(symbol).strip()
            for symbol in alert.get("symbols", [])
            if str(symbol).strip()
        ]
        if symbols:
            return symbols[:3]

        event_symbols: list[str] = []
        for instrument in event.get("top_instruments", []):
            if not isinstance(instrument, dict):
                continue
            symbol = str(instrument.get("symbol", "")).strip()
            if symbol and symbol not in event_symbols:
                event_symbols.append(symbol)
        return event_symbols[:3]

    def _resolve_rationale(
        self,
        alert: dict[str, object],
        event: dict[str, object],
    ) -> str:
        rationale = [
            self._truncate(str(item).strip(), 48)
            for item in event.get("rationale", [])
            if str(item).strip()
        ]
        if rationale:
            return "；".join(rationale[:2])
        reason = str(alert.get("reason", "")).strip()
        if reason:
            return self._truncate(reason, 96)
        return "等待补充解释"

    def _truncate(self, value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 3].rstrip() + "..."
