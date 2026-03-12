from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from market_news.common import clamp, tokenize, unique_preserve
from market_news.domain.models import Direction, EventCluster, EventType, ImpactAssessment, Market


@dataclass(slots=True)
class ImpactRule:
    name: str
    event_type: EventType
    direction: Direction
    keywords: list[str]
    min_matches: int
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
        token_set = set(tokenize(text))
        matched_rules: list[tuple[ImpactRule, float, list[str]]] = []
        for rule in self.rules:
            matched_terms = self._match_terms(rule.keywords, text, token_set)
            if len(matched_terms) >= rule.min_matches:
                coverage = len(matched_terms) / len(rule.keywords)
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
