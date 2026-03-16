from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Iterable
from uuid import NAMESPACE_URL, uuid5


STOP_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
    "after",
    "amid",
    "over",
    "more",
    "new",
    "latest",
    "market",
    "markets",
    "shares",
    "stock",
    "stocks",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: str | None) -> datetime:
    if not value:
        return utcnow()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9\.\-\+]{2,}", text.lower())


def significant_tokens(text: str, extra_stop_words: Iterable[str] | None = None) -> list[str]:
    blocked = STOP_WORDS | set(extra_stop_words or [])
    return [token for token in tokenize(text) if token not in blocked]


def stable_id(*parts: str) -> str:
    seed = "::".join(part.strip() for part in parts if part and part.strip())
    return str(uuid5(NAMESPACE_URL, seed or "market-news"))


def unique_preserve(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(normalized)
    return output


def jaccard_similarity(left: Iterable[str], right: Iterable[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return 0.0
    intersection = len(left_set & right_set)
    union = len(left_set | right_set)
    return intersection / union

