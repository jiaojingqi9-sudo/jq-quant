from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path
import re
from typing import Any

from market_news.common import clamp, tokenize, unique_preserve, utcnow
from market_news.domain.models import Direction, EventCluster, EventType, PipelineSnapshot, RankedEvent, RankedInstrument
from market_news.services.tech_lexicon_store import VersionedTechLexiconStore


SOURCE_CREDIBILITY_MULTIPLIER = {
    "cls": 1.20,
    "eastmoney-724": 1.12,
    "eastmoney-focus": 1.18,
    "eastmoney-ann": 1.15,
    "eastmoney-news": 1.05,
    "xinhua-finance": 1.20,
    "xinhua-tech": 1.20,
    "csrc_home": 1.10,
    "gov-miit": 1.15,
    "gov-most": 1.12,
    "gov-ndrc": 1.10,
    "36kr": 0.92,
    "huxiu": 0.90,
    "tmtpost": 0.88,
    "gelonghui": 1.00,
    "ifeng-tech": 0.90,
    "reuters-tech": 1.05,
    "sec_press": 1.00,
    "hkex_news": 1.00,
    "weibo": 0.72,
    "xueqiu": 0.78,
}


class AHShareTechFeatureBlock:
    name = "tech_block"

    def __init__(
        self,
        *,
        universe: list[dict[str, Any]],
        lexicon: list[dict[str, Any]],
        theme_labels: dict[str, str],
        theme_aliases: dict[str, list[str]],
        priority_themes: dict[str, float],
        graph_edges: list[dict[str, Any]],
        frontier_map: list[dict[str, Any]] | None = None,
        lexicon_release: dict[str, Any] | None = None,
        source_policy: dict[str, Any] | None = None,
        top_n: int = 8,
    ) -> None:
        self.universe = universe
        self.lexicon = lexicon
        self.theme_labels = theme_labels
        self.theme_aliases = theme_aliases
        self.priority_themes = priority_themes
        self.graph_edges = graph_edges
        self.frontier_map = list(frontier_map or [])
        self.lexicon_release = dict(lexicon_release or {})
        self.source_policy = dict(source_policy or {})
        self.top_n = top_n
        self._token_regex_cache: dict[str, re.Pattern[str]] = {}
        self.evidence_source_ids = {
            str(item).strip().lower()
            for item in self.source_policy.get("evidence_source_ids", [])
            if str(item).strip()
        }
        self.official_source_ids = {
            str(item).strip().lower()
            for item in self.source_policy.get("official_source_ids", [])
            if str(item).strip()
        }
        self.social_source_ids = {
            str(item).strip().lower()
            for item in self.source_policy.get("social_source_ids", [])
            if str(item).strip()
        }
        self.vetted_wire_source_ids = {
            str(item).strip().lower()
            for item in self.source_policy.get("vetted_wire_source_ids", [])
            if str(item).strip()
        }
        self.excluded_source_ids = {
            str(item).strip().lower()
            for item in self.source_policy.get("excluded_source_ids", [])
            if str(item).strip()
        }
        self.min_evidence_docs = int(self.source_policy.get("min_evidence_docs", 0))
        self.min_evidence_source_trust = float(self.source_policy.get("min_evidence_source_trust", 0.0))
        self.min_attention_if_single_evidence_doc = float(
            self.source_policy.get("min_attention_if_single_evidence_doc", 0.0)
        )
        self.allowed_event_types = {
            str(item).strip().lower()
            for item in self.source_policy.get("allowed_event_types", [])
            if str(item).strip()
        }
        self.require_candidate_assets = bool(self.source_policy.get("require_candidate_assets", False))

    @classmethod
    def from_files(
        cls,
        *,
        universe_path: Path,
        lexicon_path: Path,
        lexicon_release_path: Path | None = None,
        graph_path: Path,
        frontier_map_path: Path | None = None,
        config_path: Path | None = None,
        top_n: int = 8,
    ) -> "AHShareTechFeatureBlock":
        universe = json.loads(universe_path.read_text(encoding="utf-8"))
        if lexicon_release_path is not None and lexicon_release_path.exists():
            lexicon_release = VersionedTechLexiconStore.from_files(
                release_path=lexicon_release_path,
                terms_path=lexicon_path,
            )
            lexicon = lexicon_release.terms
            lexicon_release_payload = {
                "version": lexicon_release.version,
                "published_at": lexicon_release.published_at,
                "reviewer": lexicon_release.reviewer,
                "change_note": lexicon_release.change_note,
                "source_trace": lexicon_release.source_trace,
            }
        else:
            lexicon = json.loads(lexicon_path.read_text(encoding="utf-8"))
            lexicon_release_payload = {}
        graph_payload = json.loads(graph_path.read_text(encoding="utf-8"))
        frontier_map: list[dict[str, Any]] = []
        if frontier_map_path is not None and frontier_map_path.exists():
            loaded_frontier_map = json.loads(frontier_map_path.read_text(encoding="utf-8"))
            if isinstance(loaded_frontier_map, list):
                frontier_map = loaded_frontier_map
        source_policy = {}
        if config_path is not None and config_path.exists():
            config_payload = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(config_payload, dict):
                source_policy = config_payload.get("source_policy", {})
        return cls(
            universe=universe,
            lexicon=lexicon,
            theme_labels=graph_payload.get("theme_labels", {}),
            theme_aliases=graph_payload.get("theme_aliases", {}),
            priority_themes=graph_payload.get("priority_themes", {}),
            graph_edges=graph_payload.get("edges", []),
            frontier_map=frontier_map,
            lexicon_release=lexicon_release_payload,
            source_policy=source_policy if isinstance(source_policy, dict) else {},
            top_n=top_n,
        )

    def evaluate(self, snapshot: PipelineSnapshot) -> dict[str, Any]:
        cluster_by_id = {cluster.cluster_id: cluster for cluster in snapshot.clusters}
        instruments_by_cluster: dict[str, list[RankedInstrument]] = defaultdict(list)
        for instrument in snapshot.ranked_instruments:
            instruments_by_cluster[instrument.cluster_id].append(instrument)

        signals: list[dict[str, Any]] = []
        theme_rollup: dict[str, dict[str, Any]] = {}
        asset_rollup: dict[str, dict[str, Any]] = {}

        for event in snapshot.ranked_events:
            cluster = cluster_by_id.get(event.cluster_id)
            if cluster is None:
                continue
            signal = self._build_signal(
                cluster=cluster,
                event=event,
                mapped_instruments=instruments_by_cluster.get(event.cluster_id, []),
            )
            if signal is None:
                continue
            signals.append(signal)
            self._accumulate_themes(theme_rollup, signal)
            self._accumulate_assets(asset_rollup, signal)

        signals.sort(key=lambda item: item["trading_attention_score"], reverse=True)
        themes = sorted(
            theme_rollup.values(),
            key=lambda item: item["score"],
            reverse=True,
        )
        assets = sorted(
            asset_rollup.values(),
            key=lambda item: item["score"],
            reverse=True,
        )

        top_signals = signals[: self.top_n]
        return {
            "summary": {
                "signal_count": len(signals),
                "tracked_assets": len(self.universe),
                "hot_theme_count": len(themes),
                "top_attention_score": top_signals[0]["trading_attention_score"] if top_signals else 0.0,
                "lexicon_version": self.lexicon_release.get("version", "unversioned"),
            },
            "lexicon_release": self.lexicon_release,
            "signals": top_signals,
            "themes": themes[:10],
            "asset_ladder": assets[:12],
        }

    def _build_signal(
        self,
        *,
        cluster: EventCluster,
        event: RankedEvent,
        mapped_instruments: list[RankedInstrument],
    ) -> dict[str, Any] | None:
        # T4 = social-only sources; a cluster that consists *entirely* of T4 sources
        # cannot produce an independent signal — it can only add heat weight to a
        # signal that already has at least one evidence document.
        # Use self.social_source_ids (from source_policy config) so that adding a new
        # social source to config automatically extends the T4 guard without code changes.
        # Fall back to a minimal hardcoded set when source_policy is not configured.
        t4_sources = self.social_source_ids or {"weibo", "xueqiu", "guba"}
        if cluster.source_ids and all(str(source_id).lower() in t4_sources for source_id in cluster.source_ids):
            return None

        is_unknown_event = event.impact.event_type is EventType.UNKNOWN
        if self.allowed_event_types and event.impact.event_type.value not in self.allowed_event_types:
            if not is_unknown_event:
                return None

        evidence_docs, social_docs = self._partition_documents(cluster)
        if self.min_evidence_docs and len(evidence_docs) < self.min_evidence_docs:
            return None

        evidence_source_ids = unique_preserve([doc.source_id for doc in evidence_docs])
        social_source_ids = unique_preserve([doc.source_id for doc in social_docs])
        if self.evidence_source_ids and not evidence_source_ids:
            return None

        text_parts = [cluster.headline, cluster.summary]
        text_parts.extend(document.combined_text for document in evidence_docs)
        text = "\n".join(part for part in text_parts if part).lower()
        cluster_entities = {
            item.lower()
            for item in (
                list(cluster.entities)
                + [entity for document in evidence_docs for entity in document.entities]
            )
        }
        cluster_sectors = {item.lower() for item in cluster.sectors}
        theme_scores: dict[str, float] = defaultdict(float)
        theme_drivers: dict[str, list[str]] = defaultdict(list)
        matched_terms: list[dict[str, Any]] = []
        frontier_hits: list[dict[str, Any]] = []
        trigger_tags: list[str] = []
        positive_bias = 0.0
        negative_bias = 0.0
        spec_raw = 0.0
        heat_raw = 0.0
        importance_raw = 0.0

        for entry in self.lexicon:
            hit_terms = self._match_entry(entry, text)
            if not hit_terms:
                continue
            strength = clamp(
                float(entry.get("base_confidence", 0.5))
                * (0.62 + 0.12 * min(3, len(hit_terms)))
            )
            matched_terms.append(
                {
                    "term": entry["canonical_text"],
                    "matched_terms": hit_terms,
                    "term_type": entry.get("term_type", "unknown"),
                    "trigger_tags": entry.get("trigger_tags", []),
                    "match_strength": round(strength, 3),
                }
            )
            trigger_tags.extend(entry.get("trigger_tags", []))
            direction_hint = str(entry.get("direction_hint", "neutral"))
            if direction_hint == "positive":
                positive_bias += strength
            elif direction_hint == "negative":
                negative_bias += strength
            spec_raw += float(entry.get("spec_weight", 0.0)) * strength
            heat_raw += float(entry.get("heat_weight", 0.0)) * strength
            importance_raw += float(entry.get("importance_weight", 0.0)) * strength
            for theme, weight in entry.get("impact_vector", {}).items():
                contribution = float(weight) * strength
                if contribution <= 0:
                    continue
                theme_scores[theme] += contribution
                theme_drivers[theme].append(entry["canonical_text"])

        for entry in self.frontier_map:
            hit_terms = [
                keyword
                for keyword in entry.get("cn_breakthrough_keywords", [])
                if self._contains(text, keyword)
            ]
            if not hit_terms:
                continue
            bonus = float(entry.get("breakthrough_bonus", 0.2))
            for theme in entry.get("impact_themes", []):
                theme_scores[theme] += bonus
                theme_drivers[theme].append(f"frontier:{entry['frontier_id']}")
            frontier_hits.append(
                {
                    "frontier_id": entry["frontier_id"],
                    "cn_label": entry["cn_label"],
                    "gap_level": entry.get("gap_level", "unknown"),
                    "matched_keywords": hit_terms[:4],
                    "bonus": round(bonus, 3),
                }
            )
            positive_bias += bonus * 0.8
            spec_raw += bonus * 1.2
            importance_raw += bonus * 1.0

        for theme, alias_strength, driver in self._match_theme_aliases(cluster, text):
            theme_scores[theme] += alias_strength
            theme_drivers[theme].append(driver)

        if not theme_scores and not matched_terms:
            return None

        propagated_scores = dict(theme_scores)
        propagated_paths: dict[str, list[str]] = defaultdict(list)
        for edge in self.graph_edges:
            source = edge.get("source")
            target = edge.get("target")
            if source not in propagated_scores:
                continue
            contribution = propagated_scores[source] * float(edge.get("weight", 0.0))
            if contribution < 0.08:
                continue
            propagated_scores[target] = propagated_scores.get(target, 0.0) + contribution
            note = edge.get("rationale") or edge.get("relation") or "theme propagation"
            propagated_paths[target].append(
                f"{self._theme_label(source)} -> {self._theme_label(target)}: {note}"
            )

        active_themes = self._top_active_themes(propagated_scores, theme_drivers, propagated_paths)
        candidate_assets = self._rank_assets(
            propagated_scores=propagated_scores,
            cluster=cluster,
            cluster_entities=cluster_entities,
            cluster_sectors=cluster_sectors,
            mapped_instruments=mapped_instruments,
            trading_direction=self._resolve_direction(event.impact.direction, positive_bias, negative_bias),
            base_event_score=event.final_score,
        )

        if self.require_candidate_assets and not candidate_assets:
            return None
        if not candidate_assets and len(active_themes) < 2:
            return None

        age_hours = max(0.0, (utcnow() - cluster.last_seen_at).total_seconds() / 3600)
        freshness = clamp(1.0 - (age_hours / 24.0))

        # --- Short-line heat signals (contained entirely within this feature block) ---

        # Burst window: steep 6-hour decay.  News published ≤6 h ago gets a strong bonus
        # that collapses to zero beyond that, creating a clear recency cliff for day-trading.
        burst_freshness = clamp(1.0 - age_hours / 6.0) if age_hours <= 6.0 else 0.0

        # Social volume: sum of discussion_count written by weibo/xueqiu collectors into
        # document metadata.  Other sources that don't populate the field contribute 0,
        # so the signal degrades gracefully.  log1p normalised at 500 interactions
        # (≈half-fill) so modest viral posts score well without needing tens of thousands.
        raw_discussion = sum(
            int(doc.metadata.get("discussion_count", 0))
            for doc in social_docs
        )
        social_signal = clamp(math.log1p(raw_discussion) / math.log1p(500))

        # -------------------------------------------------------------------------

        event_heat = clamp(event.heat_score / 100.0)
        event_importance = clamp(event.importance_score / 100.0)
        catalyst_density = clamp(spec_raw / 2.8)
        theme_focus = clamp(sum(min(score, 1.0) for score in propagated_scores.values()) / 5.0)
        propagation_breadth = clamp(len([score for score in propagated_scores.values() if score >= 0.2]) / 5.0)
        evidence_doc_count = len(evidence_docs)
        official_doc_count = sum(
            1 for doc in evidence_docs if doc.source_id.lower() in self.official_source_ids
        )
        small_signal_bonus = 1.0 if evidence_doc_count <= 2 and catalyst_density >= 0.35 else 0.45

        spec_score = 100 * (
            0.34 * catalyst_density
            + 0.20 * theme_focus
            + 0.16 * propagation_breadth
            + 0.16 * freshness
            + 0.14 * small_signal_bonus
        )
        # heat_score weights revised to reward short-line recency and social discussion.
        # burst_freshness (15 %) fires only within 6 h, creating a sharp "just-broke" cliff.
        # social_signal (10 %) reflects weibo reposts+comments and xueqiu interactions.
        # event_heat weight trimmed slightly to make room; doc_count reduced proportionally.
        heat_score = 100 * (
            0.35 * event_heat
            + 0.17 * freshness
            + 0.13 * clamp(heat_raw / 2.4)
            + 0.10 * clamp(evidence_doc_count / 3)
            + 0.15 * burst_freshness
            + 0.10 * social_signal
        )
        importance_score = 100 * (
            0.55 * event_importance
            + 0.25 * clamp(importance_raw / 2.6)
            + 0.20 * theme_focus
        )
        trading_attention_score = (
            0.40 * spec_score
            + 0.35 * heat_score
            + 0.25 * importance_score
        )
        source_multiplier = self._source_multiplier(evidence_source_ids)
        confirmation_bonus = 1.0
        if len(evidence_source_ids) >= 2:
            confirmation_bonus += 0.06 * min(2, len(evidence_source_ids) - 1)
        if official_doc_count:
            confirmation_bonus += 0.06
        trading_attention_score *= clamp(source_multiplier * confirmation_bonus, 0.7, 1.30)

        if trading_attention_score < 45 and not any(item["score"] >= 58 for item in candidate_assets):
            return None
        if (
            len(evidence_source_ids) <= 1
            and official_doc_count == 0
            and trading_attention_score < self.min_attention_if_single_evidence_doc
        ):
            return None
        if is_unknown_event and official_doc_count == 0:
            evidence_set = {source_id.lower() for source_id in evidence_source_ids}
            vetted_only = bool(evidence_set) and evidence_set.issubset(self.vetted_wire_source_ids)
            if not vetted_only or not candidate_assets:
                return None

        rationale = unique_preserve(
            [
                f"hit {item['term']} via {', '.join(item['matched_terms'])}"
                for item in matched_terms[:4]
            ]
            + [item["path"] for item in active_themes if item.get("path")]
            + [
                "primary evidence: " + ", ".join(evidence_source_ids)
                if evidence_source_ids
                else "primary evidence unavailable"
            ]
            + [
                "social heat only: " + ", ".join(social_source_ids)
                if social_source_ids
                else ""
            ]
            + [
                f"top candidate assets: {', '.join(asset['symbol'] for asset in candidate_assets[:3])}"
                if candidate_assets
                else "no A/H tech asset crossed the relevance threshold yet"
            ]
        )

        return {
            "cluster_id": cluster.cluster_id,
            "headline": cluster.headline,
            "direction": self._resolve_direction(event.impact.direction, positive_bias, negative_bias).value,
            "event_type": event.impact.event_type.value,
            "doc_count": cluster.doc_count,
            "source_ids": cluster.source_ids,
            "evidence_source_ids": evidence_source_ids,
            "social_source_ids": social_source_ids,
            "evidence_doc_count": evidence_doc_count,
            "social_doc_count": len(social_docs),
            "source_quality": self._source_quality(
                official_doc_count=official_doc_count,
                evidence_source_ids=evidence_source_ids,
            ),
            "trading_attention_score": round(trading_attention_score, 2),
            "spec_score": round(spec_score, 2),
            "heat_score": round(heat_score, 2),
            "importance_score": round(importance_score, 2),
            "burst_freshness": round(burst_freshness * 100, 1),
            "social_signal": round(social_signal * 100, 1),
            "discussion_count": raw_discussion,
            "attention_tier": self._signal_tier(trading_attention_score),
            "trigger_tags": unique_preserve(trigger_tags)[:6],
            "matched_terms": matched_terms[:8],
            "frontier_hits": frontier_hits,
            "activated_themes": active_themes,
            "candidate_assets": candidate_assets[:6],
            "rationale": rationale,
        }

    def _rank_assets(
        self,
        *,
        propagated_scores: dict[str, float],
        cluster: EventCluster,
        cluster_entities: set[str],
        cluster_sectors: set[str],
        mapped_instruments: list[RankedInstrument],
        trading_direction: Direction,
        base_event_score: float,
    ) -> list[dict[str, Any]]:
        text = cluster.combined_text.lower()
        mapped_symbols = {item.symbol for item in mapped_instruments}
        assets: list[dict[str, Any]] = []
        for asset in self.universe:
            theme_weights = asset.get("theme_weights", {})
            total_theme_weight = max(1.0, sum(float(value) for value in theme_weights.values()))
            theme_score_raw = sum(
                propagated_scores.get(theme, 0.0) * float(weight)
                for theme, weight in theme_weights.items()
            )
            theme_score = clamp(theme_score_raw / total_theme_weight)
            alias_hit = 0.0
            hit_aliases: list[str] = []
            for alias in asset.get("aliases", []):
                if self._contains(text, alias):
                    alias_hit = 1.0
                    hit_aliases.append(alias)
            sector_overlap = 0.0
            if asset.get("sectors"):
                sector_overlap = len(cluster_sectors & {item.lower() for item in asset["sectors"]}) / len(asset["sectors"])
            entity_overlap = 1.0 if cluster_entities & {item.lower() for item in asset.get("aliases", [])} else 0.0
            mapper_bonus = 1.0 if asset["symbol"] in mapped_symbols else 0.0
            tier_bonus = 1.0 if asset.get("tier", "watch") == "core" else 0.72
            relevance = clamp(
                0.52 * theme_score
                + 0.18 * alias_hit
                + 0.10 * sector_overlap
                + 0.10 * entity_overlap
                + 0.10 * mapper_bonus
            )
            relevance *= tier_bonus
            if relevance < 0.32:
                continue
            score = base_event_score * (
                0.58 * relevance
                + 0.24 * float(asset.get("liquidity_score", 0.6))
                + 0.18 * theme_score
            )
            reasons: list[str] = []
            if theme_score > 0:
                reasons.append(
                    "theme exposure: "
                    + ", ".join(
                        self._theme_label(theme)
                        for theme in theme_weights
                        if propagated_scores.get(theme, 0.0) > 0
                    )
                )
            if hit_aliases:
                reasons.append("alias match: " + ", ".join(unique_preserve(hit_aliases[:3])))
            if mapper_bonus:
                reasons.append("already surfaced by the core instrument mapper")
            if sector_overlap:
                reasons.append("sector overlap with event")
            assets.append(
                {
                    "symbol": asset["symbol"],
                    "market": asset["market"],
                    "name": asset["name"],
                    "direction": trading_direction.value,
                    "score": round(score, 2),
                    "relevance": round(relevance * 100, 2),
                    "tier": asset.get("tier", "watch"),
                    "reasons": reasons or ["theme linkage only"],
                }
            )
        assets.sort(key=lambda item: item["score"], reverse=True)
        return assets

    def _match_entry(self, entry: dict[str, Any], text: str) -> list[str]:
        matches = []
        for phrase in entry.get("synonyms", []):
            if self._contains(text, phrase):
                matches.append(phrase)
        for pattern in entry.get("regexes", []):
            if re.search(pattern, text, flags=re.IGNORECASE):
                matches.append(pattern)
        return unique_preserve(matches)

    def _match_theme_aliases(self, cluster: EventCluster, text: str) -> list[tuple[str, float, str]]:
        combined_inputs = unique_preserve(
            list(cluster.themes)
            + list(cluster.entities)
            + list(cluster.sectors)
        )
        matches: list[tuple[str, float, str]] = []
        for theme, aliases in self.theme_aliases.items():
            for alias in aliases:
                if self._contains(text, alias) or any(alias.lower() == item.lower() for item in combined_inputs):
                    priority = float(self.priority_themes.get(theme, 0.55))
                    matches.append((theme, clamp(0.18 + 0.42 * priority), alias))
                    break
        return matches

    def _top_active_themes(
        self,
        scores: dict[str, float],
        drivers: dict[str, list[str]],
        paths: dict[str, list[str]],
    ) -> list[dict[str, Any]]:
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        output = []
        for theme, score_value in ranked[:8]:
            output.append(
                {
                    "theme": theme,
                    "label": self._theme_label(theme),
                    "score": round(score_value * 100, 2),
                    "drivers": unique_preserve(drivers.get(theme, []))[:4],
                    "path": unique_preserve(paths.get(theme, []))[0] if paths.get(theme) else "",
                }
            )
        return output

    def _accumulate_themes(self, rollup: dict[str, dict[str, Any]], signal: dict[str, Any]) -> None:
        for theme in signal.get("activated_themes", []):
            current = rollup.setdefault(
                theme["theme"],
                {
                    "theme": theme["theme"],
                    "label": theme["label"],
                    "score": 0.0,
                    "drivers": [],
                    "cluster_ids": [],
                },
            )
            current["score"] = round(current["score"] + float(theme["score"]), 2)
            current["drivers"] = unique_preserve(current["drivers"] + theme.get("drivers", []))[:6]
            current["cluster_ids"] = unique_preserve(current["cluster_ids"] + [signal["cluster_id"]])[:6]

    def _accumulate_assets(self, rollup: dict[str, dict[str, Any]], signal: dict[str, Any]) -> None:
        for asset in signal.get("candidate_assets", []):
            current = rollup.setdefault(
                asset["symbol"],
                {
                    "symbol": asset["symbol"],
                    "market": asset["market"],
                    "name": asset["name"],
                    "direction": asset["direction"],
                    "score": 0.0,
                    "drivers": [],
                    "cluster_ids": [],
                },
            )
            current["score"] = round(current["score"] + float(asset["score"]), 2)
            current["drivers"] = unique_preserve(current["drivers"] + asset.get("reasons", []))[:6]
            current["cluster_ids"] = unique_preserve(current["cluster_ids"] + [signal["cluster_id"]])[:6]
            if current["direction"] == "neutral" and asset["direction"] != "neutral":
                current["direction"] = asset["direction"]

    def _resolve_direction(
        self,
        event_direction: Direction,
        positive_bias: float,
        negative_bias: float,
    ) -> Direction:
        if positive_bias > negative_bias + 0.12:
            return Direction.POSITIVE
        if negative_bias > positive_bias + 0.12:
            return Direction.NEGATIVE
        return event_direction

    def _signal_tier(self, score: float) -> str:
        if score >= 72:
            return "hot"
        if score >= 58:
            return "warm"
        return "watch"

    def _partition_documents(self, cluster: EventCluster) -> tuple[list[Any], list[Any]]:
        evidence_docs: list[Any] = []
        social_docs: list[Any] = []
        for document in cluster.documents:
            source_id = document.source_id.lower()
            if source_id in self.excluded_source_ids:
                continue
            if source_id in self.social_source_ids:
                social_docs.append(document)
                continue
            if self.evidence_source_ids:
                if (
                    source_id in self.evidence_source_ids
                    and float(document.source_trust) >= self.min_evidence_source_trust
                ):
                    evidence_docs.append(document)
                continue
            evidence_docs.append(document)
        return evidence_docs, social_docs

    def _source_quality(self, *, official_doc_count: int, evidence_source_ids: list[str]) -> str:
        evidence_source_count = len(evidence_source_ids)
        if official_doc_count and evidence_source_count >= 2:
            return "official-confirmed"
        if official_doc_count:
            return "official-led"
        if evidence_source_count and all(
            source_id.lower() in self.vetted_wire_source_ids
            for source_id in evidence_source_ids
        ):
            return "vetted-wire"
        if evidence_source_count >= 2:
            return "multi-source"
        return "single-wire"

    def _theme_label(self, theme: str) -> str:
        return self.theme_labels.get(theme, theme.replace("-", " ").title())

    def _source_multiplier(self, source_ids: list[str]) -> float:
        multipliers = [
            SOURCE_CREDIBILITY_MULTIPLIER.get(source_id, 1.0)
            for source_id in source_ids
        ]
        return max(multipliers) if multipliers else 1.0

    def _contains(self, text: str, pattern: str) -> bool:
        normalized = str(pattern).strip().lower()
        if not normalized:
            return False
        if any("\u4e00" <= char <= "\u9fff" for char in normalized):
            return normalized in text
        if " " in normalized or "." in normalized or "-" in normalized:
            return normalized in text
        compiled = self._token_regex_cache.get(normalized)
        if compiled is None:
            compiled = re.compile(rf"\b{re.escape(normalized)}\b", flags=re.IGNORECASE)
            self._token_regex_cache[normalized] = compiled
        if compiled.search(text):
            return True
        return normalized in tokenize(text)
