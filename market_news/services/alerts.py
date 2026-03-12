from __future__ import annotations

from collections import defaultdict

from market_news.domain.models import (
    AlertItem,
    AlertLevel,
    Direction,
    EventType,
    RankedEvent,
    RankedInstrument,
)


class RuleBasedAlertEngine:
    def __init__(self, max_alerts: int = 12) -> None:
        self.max_alerts = max_alerts
        self.level_priority = {
            AlertLevel.CRITICAL: 3,
            AlertLevel.HIGH: 2,
            AlertLevel.MEDIUM: 1,
        }

    def generate(
        self,
        ranked_events: list[RankedEvent],
        ranked_instruments: list[RankedInstrument],
        seen_cluster_ids: set[str],
    ) -> list[AlertItem]:
        symbols_by_cluster: dict[str, list[str]] = defaultdict(list)
        for instrument in ranked_instruments:
            current = symbols_by_cluster[instrument.cluster_id]
            if instrument.symbol not in current and len(current) < 4:
                current.append(instrument.symbol)

        alerts: list[AlertItem] = []
        for event in ranked_events:
            is_new = event.cluster_id not in seen_cluster_ids
            level = self._resolve_level(event, is_new)
            if level is None:
                continue
            alerts.append(
                AlertItem(
                    cluster_id=event.cluster_id,
                    headline=event.headline,
                    level=level,
                    direction=event.impact.direction,
                    event_type=event.impact.event_type,
                    final_score=event.final_score,
                    is_new=is_new,
                    symbols=symbols_by_cluster.get(event.cluster_id, []),
                    reason=self._build_reason(event, is_new),
                )
            )

        alerts.sort(
            key=lambda item: (
                self.level_priority[item.level],
                1 if item.is_new else 0,
                item.final_score,
            ),
            reverse=True,
        )
        return alerts[: self.max_alerts]

    def _resolve_level(
        self,
        event: RankedEvent,
        is_new: bool,
    ) -> AlertLevel | None:
        direction = event.impact.direction
        event_type = event.impact.event_type
        score = event.final_score
        if is_new and direction == Direction.NEGATIVE and score >= 66:
            return AlertLevel.CRITICAL
        if is_new and event_type != EventType.UNKNOWN and score >= 68:
            return AlertLevel.CRITICAL
        if is_new and event_type != EventType.UNKNOWN and score >= 58:
            return AlertLevel.HIGH
        if direction == Direction.NEGATIVE and score >= 62:
            return AlertLevel.HIGH
        if is_new and score >= 50:
            return AlertLevel.MEDIUM
        return None

    def _build_reason(self, event: RankedEvent, is_new: bool) -> str:
        parts = []
        if is_new:
            parts.append("new since last run")
        if event.impact.direction == Direction.NEGATIVE:
            parts.append("downside-sensitive")
        elif event.impact.direction == Direction.POSITIVE:
            parts.append("positive catalyst")
        if event.impact.event_type != EventType.UNKNOWN:
            parts.append(event.impact.event_type.value)
        parts.append(f"score={event.final_score}")
        return ", ".join(parts)
