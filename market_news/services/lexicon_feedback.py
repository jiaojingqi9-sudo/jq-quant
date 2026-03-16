from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path

from market_news.common import utcnow


class LexiconFeedbackStore:
    def __init__(self, feedback_path: Path) -> None:
        self.feedback_path = feedback_path

    def record(
        self,
        *,
        signal_id: str,
        result: str,
        matched_terms: list[str],
        note: str = "",
    ) -> None:
        payload = {
            "ts": utcnow().isoformat(),
            "signal_id": signal_id,
            "result": result,
            "matched_terms": list(matched_terms),
            "note": note,
        }
        self.feedback_path.parent.mkdir(parents=True, exist_ok=True)
        with self.feedback_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def aggregate(self) -> dict[str, dict[str, float | int]]:
        stats: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"good": 0, "bad": 0, "net_score": 0.0}
        )
        if not self.feedback_path.exists():
            return {}
        with self.feedback_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                payload = json.loads(stripped)
                result = str(payload.get("result", "")).strip().lower()
                if result not in {"good", "bad"}:
                    continue
                terms = [str(item).strip() for item in payload.get("matched_terms", []) if str(item).strip()]
                for term in terms:
                    stats[term][result] = int(stats[term][result]) + 1

        for term, values in stats.items():
            total = int(values["good"]) + int(values["bad"])
            values["net_score"] = 0.0 if total == 0 else (int(values["good"]) - int(values["bad"])) / total
        return dict(stats)
