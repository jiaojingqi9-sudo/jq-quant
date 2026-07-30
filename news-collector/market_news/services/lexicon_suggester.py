from __future__ import annotations

from typing import Any

from market_news.common import clamp


class LexiconSuggester:
    def suggest(
        self,
        *,
        feedback: dict[str, dict[str, Any]],
        current_lexicon: list[dict[str, Any]],
        min_feedback_count: int = 5,
    ) -> list[dict[str, Any]]:
        suggestions = []
        for term, stats in feedback.items():
            total = int(stats.get("good", 0) or 0) + int(stats.get("bad", 0) or 0)
            if total < min_feedback_count:
                continue
            net_score = (int(stats.get("good", 0) or 0) - int(stats.get("bad", 0) or 0)) / total
            entry = self._find_entry(term, current_lexicon)
            if entry is None:
                continue
            current_conf = float(entry.get("base_confidence", 0.5))
            suggested_conf = clamp(current_conf + 0.08 * net_score, 0.30, 0.98)
            if abs(suggested_conf - current_conf) < 0.04:
                continue
            suggestions.append(
                {
                    "canonical_text": entry["canonical_text"],
                    "current_base_confidence": current_conf,
                    "suggested_base_confidence": round(suggested_conf, 3),
                    "net_score": round(net_score, 3),
                    "feedback_count": total,
                    "reason": f"good={stats.get('good', 0)}, bad={stats.get('bad', 0)}",
                }
            )
        return sorted(suggestions, key=lambda item: abs(float(item["net_score"])), reverse=True)

    def _find_entry(
        self,
        term: str,
        current_lexicon: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        lowered = term.lower()
        for entry in current_lexicon:
            synonyms = [str(item).lower() for item in entry.get("synonyms", [])]
            if str(entry.get("canonical_text", "")).lower() == lowered or lowered in synonyms:
                return entry
        return None
