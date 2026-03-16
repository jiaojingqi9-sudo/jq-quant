from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TechLexiconRelease:
    version: str
    published_at: str
    reviewer: str
    change_note: str
    source_trace: dict[str, Any]
    terms: list[dict[str, Any]]


class VersionedTechLexiconStore:
    @classmethod
    def from_files(
        cls,
        *,
        release_path: Path,
        terms_path: Path,
    ) -> TechLexiconRelease:
        release_payload = json.loads(release_path.read_text(encoding="utf-8"))
        terms_payload = json.loads(terms_path.read_text(encoding="utf-8"))
        if not isinstance(terms_payload, list):
            raise ValueError("Tech lexicon terms file must be a JSON array.")
        return TechLexiconRelease(
            version=str(release_payload.get("version", "draft")),
            published_at=str(release_payload.get("published_at", "")),
            reviewer=str(release_payload.get("reviewer", "unreviewed")),
            change_note=str(release_payload.get("change_note", "")),
            source_trace=release_payload.get("source_trace", {}),
            terms=terms_payload,
        )
