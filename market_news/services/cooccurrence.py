from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
import re
from typing import Any

from market_news.common import utcnow
from market_news.domain.models import PipelineSnapshot

try:  # pragma: no cover - optional dependency
    import jieba  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    jieba = None


class CooccurrenceMiner:
    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path

    def write_from_snapshot(self, snapshot: PipelineSnapshot) -> list[dict[str, Any]]:
        tech_block = snapshot.feature_blocks.get("tech_block", {})
        signals = tech_block.get("signals", []) if isinstance(tech_block, dict) else []
        cluster_lookup = {cluster.cluster_id: cluster for cluster in snapshot.clusters}
        known_terms = {
            str(item.get("term", "")).strip()
            for signal in signals
            for item in signal.get("matched_terms", [])
            if isinstance(item, dict) and str(item.get("term", "")).strip()
        }
        co_map: dict[str, Counter[str]] = defaultdict(Counter)
        doc_counts: Counter[tuple[str, str]] = Counter()
        for signal in signals:
            cluster = cluster_lookup.get(str(signal.get("cluster_id", "")))
            if cluster is None:
                continue
            text = "\n".join(document.combined_text for document in cluster.documents)
            tokens = self._tokenize(text)
            if not tokens:
                continue
            unique_tokens = {token for token in tokens if len(token) >= 2}
            matched_terms = {
                str(item.get("term", "")).strip()
                for item in signal.get("matched_terms", [])
                if isinstance(item, dict) and str(item.get("term", "")).strip()
            }
            for term in matched_terms:
                for token in unique_tokens:
                    if token == term or token in known_terms:
                        continue
                    co_map[term][token] += 1
                    doc_counts[(term, token)] += 1

        candidates: list[dict[str, Any]] = []
        for term, counter in co_map.items():
            term_total = sum(counter.values()) or 1
            for token, count in counter.items():
                pmi = self._approx_pmi(count, term_total, len(signals))
                if count < 10 or pmi < 2.0:
                    continue
                candidates.append(
                    {
                        "ts": utcnow().isoformat(),
                        "term": term,
                        "candidate": token,
                        "cooccurrence_count": count,
                        "pmi": round(pmi, 3),
                    }
                )
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("w", encoding="utf-8") as handle:
            for item in sorted(candidates, key=lambda row: (row["cooccurrence_count"], row["pmi"]), reverse=True):
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return candidates

    def _tokenize(self, text: str) -> list[str]:
        if jieba is not None:
            return [token.strip() for token in jieba.cut(text) if token.strip()]
        return re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9\.\-\+]{2,}", text.lower())

    def _approx_pmi(self, count: int, term_total: int, doc_total: int) -> float:
        joint = count / max(doc_total, 1)
        base = (term_total / max(doc_total, 1)) ** 2
        if base <= 0 or joint <= 0:
            return 0.0
        return max(0.0, joint / base)
