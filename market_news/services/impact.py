from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from market_news.common import clamp, tokenize, unique_preserve
from market_news.domain.models import Direction, EventCluster, EventType, ImpactAssessment, Market


ROUTINE_MARKET_INFRASTRUCTURE_PATTERNS = [
    "主做市服务",
    "质押式回购质押券折算率",
    "质押券折算率",
    "基金流动性服务商",
]

ORDER_LOSS_CONTEXT_PATTERNS = [
    "失去订单",
    "失去客户",
    "采购订单取消",
    "订单取消",
    "采购取消",
    "终止采购",
    "终止合同",
    "客户流失",
    "客户暂停采购",
    "lost order",
    "lost customer",
    "order cancellation",
    "purchase order cancelled",
    "purchase order canceled",
    "contract termination",
]


@dataclass(slots=True)
class ImpactRule:
    name: str
    event_type: EventType
    direction: Direction
    keywords: list[str]
    min_matches: int
    min_source_trust: float
    themes: list[str]
    sectors: list[str]
    markets: list[Market]
    severity: float
    rationale: str


class ConfigDrivenImpactAnalyzer:
    def __init__(self, rules: list[ImpactRule]) -> None:
        self.rules = rules

    @classmethod
    def from_file(cls, path: Path) -> "ConfigDrivenImpactAnalyzer":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rules = [
            ImpactRule(
                name=item["name"],
                event_type=EventType(item["event_type"]),
                direction=Direction(item["direction"]),
                keywords=[keyword.lower() for keyword in item["keywords"]],
                min_matches=int(item.get("min_matches", 1)),
                min_source_trust=float(item.get("min_source_trust", 0.0)),
                themes=item.get("themes", []),
                sectors=item.get("sectors", []),
                markets=[Market(market) for market in item.get("markets", [])],
                severity=float(item.get("severity", 0.5)),
                rationale=item.get("rationale", item["name"]),
            )
            for item in payload
        ]
        return cls(rules)

    def assess(self, cluster: EventCluster) -> ImpactAssessment:
        text = cluster.combined_text.lower()
        if self._is_routine_market_infrastructure(cluster):
            return ImpactAssessment(
                event_type=EventType.UNKNOWN,
                direction=Direction.NEUTRAL,
                affected_markets=[Market.CN_A],
                affected_sectors=cluster.sectors,
                affected_themes=cluster.themes,
                severity=0.12,
                confidence=clamp(0.45 + 0.25 * cluster.avg_source_trust),
                matched_rules=["Routine Market Infrastructure"],
                rationale=[
                    "Routine Market Infrastructure: matched exchange plumbing notice, "
                    "not a company catalyst or tradeable corporate event."
                ],
            )
        token_set = set(tokenize(text))
        matched_rules: list[tuple[ImpactRule, float, list[str]]] = []
        for rule in self.rules:
            if cluster.avg_source_trust < rule.min_source_trust:
                continue
            if rule.name == "Major Contract Win" and any(
                pattern.lower() in text for pattern in ORDER_LOSS_CONTEXT_PATTERNS
            ):
                continue
            matched_terms = self._match_terms(rule.keywords, text, token_set)
            if len(matched_terms) >= rule.min_matches:
                # Large synonym lists should not dilute a strong semantic hit.
                coverage_denominator = min(len(rule.keywords), max(3, rule.min_matches * 3))
                coverage = min(1.0, len(matched_terms) / coverage_denominator)
                matched_rules.append((rule, coverage, matched_terms))

        if not matched_rules:
            return ImpactAssessment(
                event_type=EventType.UNKNOWN,
                direction=Direction.NEUTRAL,
                affected_markets=[Market.CN_A, Market.HK, Market.US],
                affected_sectors=cluster.sectors,
                affected_themes=cluster.themes,
                severity=0.4,
                confidence=clamp(0.25 + 0.25 * cluster.avg_source_trust),
                matched_rules=[],
                rationale=["No configured rule matched, falling back to neutral market observation."],
            )

        positive_score = sum(
            rule.severity * coverage
            for rule, coverage, _ in matched_rules
            if rule.direction == Direction.POSITIVE
        )
        negative_score = sum(
            rule.severity * coverage
            for rule, coverage, _ in matched_rules
            if rule.direction == Direction.NEGATIVE
        )
        if positive_score > negative_score + 0.05:
            direction = Direction.POSITIVE
        elif negative_score > positive_score + 0.05:
            direction = Direction.NEGATIVE
        else:
            direction = Direction.NEUTRAL

        dominant_rule = max(matched_rules, key=lambda item: item[0].severity * item[1])[0]
        severity = clamp(
            sum(rule.severity * coverage for rule, coverage, _ in matched_rules)
            / len(matched_rules)
        )
        confidence = clamp(
            0.35
            + 0.15 * min(1.0, len(matched_rules) / 3)
            + 0.15 * min(1.0, cluster.doc_count / 3)
            + 0.25 * cluster.avg_source_trust
        )
        affected_markets = unique_preserve(
            market.value for rule, _, _ in matched_rules for market in rule.markets
        )
        if not affected_markets:
            affected_markets = [Market.CN_A.value, Market.HK.value, Market.US.value]

        affected_sectors = unique_preserve(
            sector for rule, _, _ in matched_rules for sector in rule.sectors
        ) or cluster.sectors
        affected_themes = unique_preserve(
            theme for rule, _, _ in matched_rules for theme in rule.themes
        ) or cluster.themes

        rationale = [
            f"{rule.name}: matched {', '.join(matched_terms)}. {rule.rationale}"
            for rule, _, matched_terms in matched_rules
        ]
        return ImpactAssessment(
            event_type=dominant_rule.event_type,
            direction=direction,
            affected_markets=[Market(item) for item in affected_markets],
            affected_sectors=affected_sectors,
            affected_themes=affected_themes,
            severity=severity,
            confidence=confidence,
            matched_rules=[rule.name for rule, _, _ in matched_rules],
            rationale=rationale,
        )

    def _match_terms(self, keywords: list[str], text: str, token_set: set[str]) -> list[str]:
        matched: list[str] = []
        for keyword in keywords:
            normalized = keyword.lower()
            if any("\u4e00" <= char <= "\u9fff" for char in normalized):
                if normalized in text:
                    matched.append(normalized)
                continue
            if " " in normalized:
                if normalized in text:
                    matched.append(normalized)
                continue
            if normalized in token_set:
                matched.append(normalized)
                continue
            if re.search(rf"\b{re.escape(normalized)}\b", text):
                matched.append(normalized)
        return matched

    def _is_routine_market_infrastructure(self, cluster: EventCluster) -> bool:
        headline = cluster.headline.lower()
        if any(pattern.lower() in headline for pattern in ROUTINE_MARKET_INFRASTRUCTURE_PATTERNS):
            return True
        if not cluster.documents:
            return False
        titles = [document.title.lower() for document in cluster.documents if document.title]
        return bool(titles) and all(
            any(pattern.lower() in title for pattern in ROUTINE_MARKET_INFRASTRUCTURE_PATTERNS)
            for title in titles
        )
