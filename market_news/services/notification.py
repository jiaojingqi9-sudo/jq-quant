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
    modules: list[dict[str, object]]


class AlertDigestBuilder:
    def __init__(
        self,
        *,
        min_level: AlertLevel = AlertLevel.HIGH,
        max_alerts: int = 3,
        include_existing: bool = False,
        max_tech_signals: int = 2,
        min_tech_attention: float = 55.0,
    ) -> None:
        self.min_level = min_level
        self.max_alerts = max_alerts
        self.include_existing = include_existing
        self.max_tech_signals = max_tech_signals
        self.min_tech_attention = min_tech_attention

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
        selected_core_alerts: list[dict[str, object]] = []
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
            selected_core_alerts.append(alert)
            if len(selected_core_alerts) >= self.max_alerts:
                break

        selected_cluster_ids = {
            str(alert.get("cluster_id", "")).strip()
            for alert in selected_core_alerts
            if str(alert.get("cluster_id", "")).strip()
        }
        selected_tech_signals = self._select_tech_signals(
            payload,
            selected_cluster_ids=selected_cluster_ids,
            sent_cluster_ids=sent_cluster_ids,
        )

        if not selected_core_alerts and not selected_tech_signals:
            return None

        created_at = str(payload.get("created_at", ""))
        source = str(payload.get("source", "market-news"))
        unique_cluster_ids = []
        for cluster_id in list(selected_cluster_ids) + [
            str(signal.get("cluster_id", "")).strip()
            for signal in selected_tech_signals
        ]:
            if cluster_id and cluster_id not in unique_cluster_ids:
                unique_cluster_ids.append(cluster_id)

        lines = [
            "市场新闻提醒",
            f"时间: {created_at}",
            f"来源: {source}",
            f"主线提醒: {len(selected_core_alerts)} 条 | 港A科技催化: {len(selected_tech_signals)} 条",
            "",
        ]

        if selected_core_alerts:
            lines.append("全市场主线")
        for index, alert in enumerate(selected_core_alerts, start=1):
            cluster_id = str(alert.get("cluster_id", ""))
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

        if selected_tech_signals:
            if selected_core_alerts:
                lines.append("")
            lines.append("港A科技催化")
            for index, signal in enumerate(selected_tech_signals, start=1):
                assets = [
                    str(item.get("symbol", "")).strip()
                    for item in signal.get("candidate_assets", [])
                    if isinstance(item, dict) and str(item.get("symbol", "")).strip()
                ][:3]
                triggers = [
                    str(item).strip()
                    for item in signal.get("trigger_tags", [])
                    if str(item).strip()
                ][:3]
                rationale = [
                    self._truncate(str(item).strip(), 44)
                    for item in signal.get("rationale", [])
                    if str(item).strip()
                ][:2]
                lines.append(
                    f"{index}. [{self._tech_tier_label(str(signal.get('attention_tier', 'watch')))}]"
                    f"[{DIRECTION_LABELS.get(str(signal.get('direction', 'neutral')), str(signal.get('direction', 'neutral')))}] "
                    f"{self._truncate(str(signal.get('headline', '')), 110)}"
                )
                lines.append(
                    f"   港A候选: {', '.join(assets) if assets else 'n/a'}"
                )
                lines.append(
                    f"   触发: {', '.join(triggers) if triggers else 'n/a'} | "
                    f"关注分: {int(round(float(signal.get('trading_attention_score', 0))))}"
                )
                if rationale:
                    lines.append(f"   逻辑: {'；'.join(rationale)}")

        message = "\n".join(lines).strip()
        modules = [
            {
                "name": "core_alerts",
                "status": "active" if selected_core_alerts else "idle",
                "count": len(selected_core_alerts),
                "detail": "high/critical alert stream",
            },
            {
                "name": "tech_block",
                "status": "active" if selected_tech_signals else "idle",
                "count": len(selected_tech_signals),
                "detail": "A/H tech catalyst stream",
            },
        ]
        return NotificationPlan(
            channel=channel,
            target=target,
            message=message,
            cluster_ids=unique_cluster_ids,
            alert_count=len(unique_cluster_ids),
            preview_path=preview_path,
            modules=modules,
        )

    def _select_tech_signals(
        self,
        payload: dict[str, object],
        *,
        selected_cluster_ids: set[str],
        sent_cluster_ids: set[str],
    ) -> list[dict[str, object]]:
        tech_block = payload.get("tech_block", {})
        if not isinstance(tech_block, dict):
            return []

        selected: list[dict[str, object]] = []
        for signal in tech_block.get("signals", []):
            if not isinstance(signal, dict):
                continue
            cluster_id = str(signal.get("cluster_id", "")).strip()
            if not cluster_id:
                continue
            attention_score = float(signal.get("trading_attention_score", 0.0) or 0.0)
            tier = str(signal.get("attention_tier", "watch")).strip().lower()
            if attention_score < self.min_tech_attention and tier not in {"hot", "warm"}:
                continue
            if cluster_id in sent_cluster_ids and cluster_id not in selected_cluster_ids:
                continue
            selected.append(signal)
            if len(selected) >= self.max_tech_signals:
                break
        return selected

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

    def _tech_tier_label(self, tier: str) -> str:
        labels = {
            "hot": "热点",
            "warm": "观察",
            "watch": "跟踪",
        }
        return labels.get(tier, tier or "跟踪")

    def _truncate(self, value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 3].rstrip() + "..."
