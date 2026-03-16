from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import json
from math import log2
from pathlib import Path
import re
from typing import Any

from market_news.common import STOP_WORDS, utcnow
from market_news.domain.models import RawNewsRecord

try:  # pragma: no cover - optional dependency
    import jieba  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    jieba = None


DEFAULT_EXTRA_STOP_WORDS = {
    "author",
    "authors",
    "edit",
    "edited",
    "editor",
    "name",
    "names",
    "source",
    "sources",
    "title",
    "titles",
    "updated",
    "全文",
    "交易",
    "资金",
    "智能",
    "中国",
    "模型",
    "赛道",
    "概念",
    "布局",
    "生态",
    "用户",
    "客户",
    "方案",
    "平台",
    "系统",
    "服务",
    "数据",
    "能力",
    "应用",
    "行业链",
    "产业",
    "产业链",
    "企业",
    "资产",
    "板块",
    "科技",
    "股份",
    "证券",
    "申请",
    "现金",
    "股东",
    "董事",
    "监事",
    "审核",
    "问询",
    "问询函",
    "回复",
    "回函",
    "议案",
    "会议",
    "披露",
    "募集",
    "上市",
    "有限公司",
    "当地时间",
    "微博视频",
    "的微博视频",
    "公司",
    "行业",
    "市场",
    "相关",
    "表示",
    "消息",
    "记者",
    "技术",
    "产品",
    "业务",
    "进行",
    "已经",
    "一个",
    "我们",
    "他们",
    "你们",
    "可以",
    "实现",
    "通过",
    "发布",
    "公告",
    "显示",
    "来自",
    "雪球",
    "微博",
}

TECH_DISCOVERY_SOURCE_HINTS = (
    "36kr",
    "tmtpost",
    "huxiu",
    "ifeng",
    "xinhua",
    "weibo",
    "xueqiu",
    "cls",
    "gelonghui",
    "eastmoney",
)

BOILERPLATE_NOISE_PHRASES = (
    "股份有限公司",
    "发行股份",
    "审核问询函",
    "回复问询函",
    "限制性股票",
    "董事会",
    "股东大会",
    "专项核查意见",
    "支付现金",
    "配套资金",
    "关联交易",
    "交易异常波动",
)


@dataclass(slots=True)
class TermCandidate:
    text: str
    raw_freq: int
    cooccurrence: dict[str, float]
    inferred_impact: dict[str, float]
    discovery_score: float
    example_snippets: list[str]
    detected_at: str
    status: str = "pending"


class UnknownTermDetector:
    def __init__(
        self,
        *,
        lexicon: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> None:
        self.lexicon = list(lexicon)
        self.config = dict(config or {})
        self.min_freq = int(self.config.get("min_freq", 3))
        self.min_discovery_score = float(self.config.get("min_discovery_score", 2.0))
        self.max_candidates_per_run = int(self.config.get("max_candidates_per_run", 50))
        self.token_min_length = int(self.config.get("token_min_length", 2))
        self.token_max_length = int(self.config.get("token_max_length", 10))
        self.min_top_impact_score = float(self.config.get("min_top_impact_score", 0.35))
        self._known_terms = self._build_known_term_index(self.lexicon)
        self._known_variants = sorted(self._known_terms, key=len, reverse=True)
        self._entry_lookup = self._build_entry_lookup(self.lexicon)
        self._blocked_terms = {word.lower() for word in STOP_WORDS | DEFAULT_EXTRA_STOP_WORDS}

    def run(self, records: list[RawNewsRecord]) -> list[TermCandidate]:
        if not records:
            return []

        raw_freq: Counter[str] = Counter()
        display_terms: dict[str, str] = {}
        snippets: dict[str, list[str]] = defaultdict(list)
        cooccurrence: dict[str, Counter[str]] = defaultdict(Counter)

        for record in records:
            text = "\n".join(part for part in [record.title, record.summary, record.body] if part).strip()
            if not text:
                continue
            tokens = self.tokenize(text)
            if not tokens:
                continue

            unique_unknowns: dict[str, str] = {}
            for token in tokens:
                lowered = token.lower()
                if lowered in self._known_terms:
                    continue
                if self._is_substring_of_known_term(lowered):
                    continue
                unique_unknowns.setdefault(lowered, token)

            if not unique_unknowns:
                continue

            known_terms = self._known_terms_in_text(text)
            for lowered, token in unique_unknowns.items():
                raw_freq[lowered] += 1
                display_terms.setdefault(lowered, token)
                snippet = self._extract_snippet(text, token)
                if snippet and snippet not in snippets[lowered] and len(snippets[lowered]) < 3:
                    snippets[lowered].append(snippet)
                for known in known_terms:
                    cooccurrence[lowered][known] += 1

        candidates: list[TermCandidate] = []
        for lowered, doc_freq in raw_freq.items():
            raw_freq_score = log2(1 + doc_freq)
            co_map = {term: float(count) for term, count in cooccurrence.get(lowered, {}).items()}
            cooc_score = max(co_map.values(), default=0.0)
            discovery_score = raw_freq_score * (1.0 + cooc_score)
            if doc_freq < self.min_freq or discovery_score < self.min_discovery_score:
                continue
            candidates.append(
                TermCandidate(
                    text=display_terms.get(lowered, lowered),
                    raw_freq=doc_freq,
                    cooccurrence=co_map,
                    inferred_impact=self.infer_impact(co_map),
                    discovery_score=round(discovery_score, 3),
                    example_snippets=snippets.get(lowered, [])[:3],
                    detected_at=utcnow().isoformat(),
                )
            )

        candidates.sort(key=lambda item: (item.discovery_score, item.raw_freq, len(item.text)), reverse=True)
        return self._suppress_substrings(candidates)[: self.max_candidates_per_run]

    def select_relevant_records(self, records: list[RawNewsRecord]) -> list[RawNewsRecord]:
        relevant: list[RawNewsRecord] = []
        for record in records:
            text = "\n".join(part for part in [record.title, record.summary, record.body] if part).strip()
            if not text:
                continue
            source_id = str(record.source_id).lower()
            theme_text = " ".join(str(theme).lower() for theme in record.themes)
            if any(hint in source_id for hint in TECH_DISCOVERY_SOURCE_HINTS):
                relevant.append(record)
                continue
            if "tech" in theme_text or "technology" in theme_text or "科技" in theme_text:
                relevant.append(record)
                continue
            if self._known_terms_in_text(text):
                relevant.append(record)
        return relevant

    def save(self, candidates: list[TermCandidate], path: Path) -> list[TermCandidate]:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self._load_index(path)
        saved: list[TermCandidate] = []
        with path.open("a", encoding="utf-8") as handle:
            for candidate in candidates:
                lowered = candidate.text.lower()
                if lowered in existing:
                    continue
                handle.write(json.dumps(asdict(candidate), ensure_ascii=False) + "\n")
                existing[lowered] = candidate.status
                saved.append(candidate)
        return saved

    def load(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        return rows

    def list_pending(
        self,
        path: Path,
        *,
        min_score: float = 2.0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.load(path)
            if str(row.get("status", "pending")) == "pending"
            and float(row.get("discovery_score", 0.0) or 0.0) >= min_score
            and self._is_actionable_row(row)
        ]
        rows.sort(key=lambda row: float(row.get("discovery_score", 0.0) or 0.0), reverse=True)
        return rows[:limit]

    def set_status(self, path: Path, text: str, status: str) -> bool:
        rows = self.load(path)
        updated = False
        for row in rows:
            if str(row.get("text", "")).lower() != text.lower():
                continue
            row["status"] = status
            updated = True
        if updated:
            self._rewrite(path, rows)
        return updated

    def prune_noise(self, path: Path) -> int:
        rows = self.load(path)
        updated = 0
        for row in rows:
            if str(row.get("status", "pending")) != "pending":
                continue
            if self._is_actionable_row(row):
                continue
            row["status"] = "rejected"
            row["rejection_reason"] = "auto-pruned-noise"
            updated += 1
        if updated:
            self._rewrite(path, rows)
        return updated

    def build_lexicon_entry(self, candidate: dict[str, Any], *, term_type: str = "theme") -> dict[str, Any]:
        inferred_impact = candidate.get("inferred_impact", {})
        if not isinstance(inferred_impact, dict):
            inferred_impact = {}
        return {
            "canonical_text": candidate["text"],
            "term_type": term_type,
            "synonyms": [candidate["text"]],
            "regexes": [],
            "impact_vector": inferred_impact,
            "trigger_tags": ["待补充"],
            "base_confidence": 0.65,
            "spec_weight": 0.60,
            "heat_weight": 0.60,
            "importance_weight": 0.65,
            "direction_hint": "positive",
        }

    def infer_impact(self, cooccurrence: dict[str, float]) -> dict[str, float]:
        accumulator: dict[str, float] = defaultdict(float)
        total_weight = 0.0
        for known_term, cooc_count in cooccurrence.items():
            entry = self._entry_lookup.get(known_term.lower())
            if entry is None:
                continue
            for theme, score in entry.get("impact_vector", {}).items():
                accumulator[theme] += float(score) * float(cooc_count)
            total_weight += float(cooc_count)
        if total_weight <= 0:
            return {}
        normalized = {
            theme: round(score / total_weight, 3)
            for theme, score in accumulator.items()
            if score > 0
        }
        ordered = sorted(normalized.items(), key=lambda item: item[1], reverse=True)[:5]
        return {theme: score for theme, score in ordered}

    def tokenize(self, text: str) -> list[str]:
        if jieba is not None:
            tokens = [token.strip() for token in jieba.cut(text) if token.strip()]
        else:
            tokens = []
            for chunk in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z][A-Za-z0-9\-\+]{2,}", text):
                if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
                    max_ngram = min(self.token_max_length, 6, len(chunk))
                    for size in range(self.token_min_length, max_ngram + 1):
                        for start in range(0, len(chunk) - size + 1):
                            tokens.append(chunk[start : start + size])
                    continue
                tokens.append(chunk)
        output: list[str] = []
        for token in tokens:
            if not self._is_candidate_text(token):
                continue
            output.append(token)
        return output

    def _is_actionable_row(self, row: dict[str, Any]) -> bool:
        text = str(row.get("text", "")).strip()
        if not self._is_candidate_text(text):
            return False
        lowered = text.lower()
        if lowered in self._known_terms or self._is_substring_of_known_term(lowered):
            return False
        inferred_impact = row.get("inferred_impact", {})
        strongest_theme = ""
        strongest = 0.0
        if isinstance(inferred_impact, dict):
            strongest_theme, strongest = max(
                (
                    (str(theme), float(score))
                    for theme, score in inferred_impact.items()
                ),
                key=lambda item: item[1],
                default=("", 0.0),
            )
            if strongest < self.min_top_impact_score:
                return False
        if re.fullmatch(r"[\u4e00-\u9fff]+", text):
            if self._is_boilerplate_fragment(text):
                return False
            if len(text) == 2 and strongest < 0.55:
                return False
            if strongest_theme == "compliance-risk":
                return False
            joined_snippets = " ".join(str(item) for item in row.get("example_snippets", []))
            if strongest_theme in {"compliance-risk", "order-momentum"} and re.search(
                r"official disclosure|问询函|董事会|股东大会|专项核查意见",
                joined_snippets,
                flags=re.I,
            ):
                return False
        return True

    def _is_candidate_text(self, token: str) -> bool:
        lowered = token.lower()
        if lowered in self._blocked_terms:
            return False
        if len(token) < self.token_min_length or len(token) > self.token_max_length:
            return False
        if token.isdigit():
            return False
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            if token in DEFAULT_EXTRA_STOP_WORDS:
                return False
            if token.startswith(("的", "了", "在")):
                return False
            if len(token) <= 2 and token.endswith(("的", "了", "在", "将", "用")):
                return False
        return True

    def _known_terms_in_text(self, text: str) -> list[str]:
        lowered = text.lower()
        matches: list[str] = []
        for entry in self.lexicon:
            canonical = str(entry.get("canonical_text", "")).strip()
            if not canonical:
                continue
            variants = [canonical]
            variants.extend(str(item).strip() for item in entry.get("synonyms", []))
            variants.extend(self._regex_variants(entry))
            for variant in variants:
                if not variant:
                    continue
                if variant.startswith("re:"):
                    pattern = variant[3:]
                    if re.search(pattern, text, flags=re.I):
                        matches.append(canonical)
                        break
                    continue
                if variant.lower() in lowered:
                    matches.append(canonical)
                    break
        return list(dict.fromkeys(matches))

    def _regex_variants(self, entry: dict[str, Any]) -> list[str]:
        return [f"re:{item}" for item in entry.get("regexes", []) if str(item).strip()]

    def _build_known_term_index(self, lexicon: list[dict[str, Any]]) -> set[str]:
        known: set[str] = set()
        for entry in lexicon:
            canonical = str(entry.get("canonical_text", "")).strip()
            if canonical:
                known.add(canonical.lower())
            for synonym in entry.get("synonyms", []):
                token = str(synonym).strip()
                if token:
                    known.add(token.lower())
        return known

    def _build_entry_lookup(self, lexicon: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        lookup: dict[str, dict[str, Any]] = {}
        for entry in lexicon:
            canonical = str(entry.get("canonical_text", "")).strip()
            if canonical:
                lookup[canonical.lower()] = entry
            for synonym in entry.get("synonyms", []):
                token = str(synonym).strip()
                if token:
                    lookup[token.lower()] = entry
        return lookup

    def _extract_snippet(self, text: str, token: str) -> str:
        index = text.lower().find(token.lower())
        if index < 0:
            return text[:120]
        start = max(0, index - 36)
        end = min(len(text), index + len(token) + 48)
        snippet = text[start:end].replace("\n", " ")
        return snippet.strip()

    def _is_substring_of_known_term(self, token: str) -> bool:
        return any(token != known and token in known for known in self._known_variants)

    def _is_boilerplate_fragment(self, token: str) -> bool:
        return any(token in phrase for phrase in BOILERPLATE_NOISE_PHRASES)

    def _suppress_substrings(self, candidates: list[TermCandidate]) -> list[TermCandidate]:
        kept: list[TermCandidate] = []
        for candidate in candidates:
            lowered = candidate.text.lower()
            if any(
                lowered != existing.text.lower()
                and lowered in existing.text.lower()
                and candidate.raw_freq <= existing.raw_freq
                and candidate.discovery_score <= existing.discovery_score
                for existing in kept
            ):
                continue
            kept.append(candidate)
        return kept

    def _load_index(self, path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        rows = self.load(path)
        return {
            str(row.get("text", "")).lower(): str(row.get("status", "pending"))
            for row in rows
            if str(row.get("text", "")).strip()
        }

    def _rewrite(self, path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
