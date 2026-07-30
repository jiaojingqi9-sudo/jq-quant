from __future__ import annotations

from collections import defaultdict
import re

from market_news.domain.models import (
    AlertItem,
    AlertLevel,
    Direction,
    EventType,
    RankedEvent,
    RankedInstrument,
)
from market_news.services.fundamental_focus import (
    evaluate_notification_gate,
    is_fundamental_impact,
    is_low_predictability_risk,
    is_policy_access_opening_impact,
    is_policy_demand_impact,
)


# Bulk regulatory filings carry no event and no readable headline: a US XBRL
# financial-statement submission arrives as "COMPANY NAME (0001234567) (Filer)".
# These feeds are already excluded from model judgement in
# config/model_judgement.json, and the alert engine has to honour the same call —
# otherwise a single working XBRL feed floods the phone with filer names.
#
# RankedEvent does not carry its source documents, so the filter keys off the
# filing headline shape, which is unmistakable and stable.
BULK_FILING_HEADLINE_RE = re.compile(
    r"\(\s*\d{7,10}\s*\)\s*\((?:Filer|Subject|Reporting)\)\s*$",
    re.IGNORECASE,
)


class RuleBasedAlertEngine:
    def __init__(self, max_alerts: int = 12, drop_bulk_filings: bool = True) -> None:
        self.max_alerts = max_alerts
        self.drop_bulk_filings = drop_bulk_filings
        self.level_priority = {
            AlertLevel.CRITICAL: 3,
            AlertLevel.HIGH: 2,
            AlertLevel.MEDIUM: 1,
        }

    def _is_bulk_filing(self, event: RankedEvent) -> bool:
        if not self.drop_bulk_filings:
            return False
        return bool(BULK_FILING_HEADLINE_RE.search(str(event.headline or "").strip()))

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
            if self._is_bulk_filing(event):
                continue
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
        gate = evaluate_notification_gate(event, is_new=is_new)
        if is_low_predictability_risk(event):
            if is_new and score >= 72:
                return AlertLevel.MEDIUM
            return None
        if is_new and gate.should_notify and is_fundamental_impact(event.impact):
            return AlertLevel.CRITICAL
        if is_new and gate.should_notify and is_policy_demand_impact(event.impact):
            return AlertLevel.HIGH
        if (
            is_new
            and is_policy_access_opening_impact(event.impact)
            and event.impact.direction != Direction.NEUTRAL
            and score >= 82
            and event.impact.confidence >= 0.70
        ):
            return AlertLevel.HIGH
        if is_new and gate.tier == "model" and is_fundamental_impact(event.impact) and score >= 68:
            return AlertLevel.HIGH
        if is_new and event_type != EventType.UNKNOWN and gate.score >= 50 and score >= 68:
            return AlertLevel.MEDIUM
        if direction == Direction.NEGATIVE and score >= 62:
            return AlertLevel.MEDIUM
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
        gate = evaluate_notification_gate(event, is_new=is_new)
        if gate.reasons:
            parts.append("gate=" + "/".join(gate.reasons[:3]))
        parts.append(f"score={event.final_score}")
        return ", ".join(parts)
