from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
from string import Template

from market_news.domain.models import Direction, EventCluster, NewsDocument, PipelineSnapshot
from market_news.services.unknown_term_detector import UnknownTermDetector


class MarkdownJsonReporter:
    def __init__(
        self,
        output_dir: Path,
        top_n: int = 8,
        *,
        lexicon_discovery_path: Path | None = None,
        lexicon_path: Path | None = None,
        tech_block_config_path: Path | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.top_n = top_n
        self.lexicon_discovery_path = lexicon_discovery_path
        self.lexicon_path = lexicon_path
        self.tech_block_config_path = tech_block_config_path
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write(self, snapshot: PipelineSnapshot) -> dict[str, Path]:
        json_path = self.output_dir / "latest_report.json"
        markdown_path = self.output_dir / "latest_report.md"
        html_path = self.output_dir / "latest_dashboard.html"

        cluster_by_id = {cluster.cluster_id: cluster for cluster in snapshot.clusters}

        grouped_instruments: dict[str, list[dict[str, object]]] = defaultdict(list)
        for instrument in snapshot.ranked_instruments:
            grouped_instruments[instrument.cluster_id].append(
                {
                    "symbol": instrument.symbol,
                    "market": instrument.market.value,
                    "name": instrument.name,
                    "direction": instrument.direction.value,
                    "final_score": instrument.final_score,
                    "reasons": instrument.reasons,
                }
            )

        positive_events = [
            event for event in snapshot.ranked_events if event.impact.direction == Direction.POSITIVE
        ][: self.top_n]
        negative_events = [
            event for event in snapshot.ranked_events if event.impact.direction == Direction.NEGATIVE
        ][: self.top_n]
        neutral_events = [
            event for event in snapshot.ranked_events if event.impact.direction == Direction.NEUTRAL
        ][: self.top_n]
        latest_feed = snapshot.documents[: self.top_n * 2]

        alert_counts = {
            "critical": sum(1 for item in snapshot.alerts if item.level.value == "critical"),
            "high": sum(1 for item in snapshot.alerts if item.level.value == "high"),
            "medium": sum(1 for item in snapshot.alerts if item.level.value == "medium"),
            "new": sum(1 for item in snapshot.alerts if item.is_new),
        }
        runtime_status = self._load_runtime_status()
        tech_block = self._enrich_tech_block(
            snapshot.feature_blocks.get("tech_block", {}),
            cluster_by_id,
        )
        lexicon_discovery = self._load_lexicon_discovery()
        ai_judgement = self._build_ai_judgement(
            snapshot.ranked_events,
            cluster_by_id,
            grouped_instruments,
        )

        json_payload = {
            "run_id": snapshot.run_id,
            "created_at": snapshot.created_at.isoformat(),
            "source": snapshot.source_name,
            "counts": {
                "raw_records": len(snapshot.raw_records),
                "documents": len(snapshot.documents),
                "clusters": len(snapshot.clusters),
                "ranked_events": len(snapshot.ranked_events),
                "ranked_instruments": len(snapshot.ranked_instruments),
            },
            "alerts": [
                {
                    "cluster_id": item.cluster_id,
                    "headline": item.headline,
                    "level": item.level.value,
                    "direction": item.direction.value,
                    "event_type": item.event_type.value,
                    "is_new": item.is_new,
                    "final_score": item.final_score,
                    "symbols": item.symbols,
                    "reason": item.reason,
                }
                for item in snapshot.alerts
            ],
            "alert_counts": alert_counts,
            "top_events": [
                self._event_payload(
                    event,
                    cluster_by_id.get(event.cluster_id),
                    grouped_instruments.get(event.cluster_id, [])[:5],
                )
                for event in snapshot.ranked_events[: self.top_n]
            ],
            "positive_catalysts": [
                self._event_payload(
                    event,
                    cluster_by_id.get(event.cluster_id),
                    grouped_instruments.get(event.cluster_id, [])[:5],
                )
                for event in positive_events
            ],
            "negative_risks": [
                self._event_payload(
                    event,
                    cluster_by_id.get(event.cluster_id),
                    grouped_instruments.get(event.cluster_id, [])[:5],
                )
                for event in negative_events
            ],
            "watchlist": [
                self._event_payload(
                    event,
                    cluster_by_id.get(event.cluster_id),
                    grouped_instruments.get(event.cluster_id, [])[:5],
                )
                for event in neutral_events
            ],
            "top_instruments": [
                {
                    "cluster_id": instrument.cluster_id,
                    "headline": instrument.cluster_headline,
                    "symbol": instrument.symbol,
                    "market": instrument.market.value,
                    "name": instrument.name,
                    "direction": instrument.direction.value,
                    "final_score": instrument.final_score,
                    "reasons": instrument.reasons,
                }
                for instrument in snapshot.ranked_instruments[: self.top_n]
            ],
            "latest_feed": [
                self._document_payload(document)
                for document in latest_feed
            ],
            "feature_blocks": snapshot.feature_blocks,
            "tech_block": tech_block,
            "lexicon_discovery": lexicon_discovery,
            "ai_judgement": ai_judgement,
            "runtime_status": runtime_status,
        }
        json_path.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [
            "# Market News Collector Report",
            "",
            f"- Run ID: `{snapshot.run_id}`",
            f"- Source: `{snapshot.source_name}`",
            f"- Raw records: `{len(snapshot.raw_records)}`",
            f"- Deduplicated documents: `{len(snapshot.documents)}`",
            f"- Event clusters: `{len(snapshot.clusters)}`",
            f"- Alerts: `critical={alert_counts['critical']}` `high={alert_counts['high']}` `medium={alert_counts['medium']}` `new={alert_counts['new']}`",
            "",
            "## Alert Summary",
            "",
        ]

        if snapshot.alerts:
            for index, alert in enumerate(snapshot.alerts[: self.top_n], start=1):
                prefix = "NEW " if alert.is_new else ""
                symbols = ", ".join(alert.symbols) if alert.symbols else "n/a"
                lines.extend(
                    [
                        f"{index}. `{alert.level.value.upper()}` {prefix}`{alert.direction.value}` {alert.headline}",
                        f"   symbols: {symbols}",
                        f"   why: {alert.reason}",
                    ]
                )
            lines.append("")
        else:
            lines.extend(["No active alerts.", ""])

        self._append_event_section(
            lines,
            title="## Negative Risks",
            events=negative_events,
            grouped_instruments=grouped_instruments,
            cluster_by_id=cluster_by_id,
        )
        self._append_event_section(
            lines,
            title="## Positive Catalysts",
            events=positive_events,
            grouped_instruments=grouped_instruments,
            cluster_by_id=cluster_by_id,
        )
        self._append_event_section(
            lines,
            title="## Watchlist",
            events=neutral_events,
            grouped_instruments=grouped_instruments,
            cluster_by_id=cluster_by_id,
        )
        self._append_tech_block_section(lines, tech_block)
        self._append_lexicon_discovery_section(lines, lexicon_discovery)

        lines.extend(["## Latest Feed", ""])
        for index, document in enumerate(latest_feed, start=1):
            themes = ", ".join(document.themes[:3]) or "n/a"
            entities = ", ".join(document.entities[:3]) or "n/a"
            lines.extend(
                [
                    f"{index}. `{document.published_at.isoformat()}` `{document.source_id}` {document.title}",
                    f"   themes: {themes}",
                    f"   entities: {entities}",
                ]
            )
        lines.append("")

        markdown_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        html_path.write_text(self._build_html(json_payload), encoding="utf-8")
        return {
            "json_report": json_path,
            "markdown_report": markdown_path,
            "html_report": html_path,
        }

    def _load_lexicon_discovery(self) -> dict[str, object]:
        if (
            self.lexicon_discovery_path is None
            or self.lexicon_path is None
            or not self.lexicon_discovery_path.exists()
            or not self.lexicon_path.exists()
        ):
            return {
                "summary": {
                    "pending_count": 0,
                    "accepted_count": 0,
                    "discovery_path": str(self.lexicon_discovery_path or ""),
                    "lexicon_path": str(self.lexicon_path or ""),
                },
                "candidates": [],
                "accepted_terms": [],
            }

        try:
            lexicon_payload = json.loads(self.lexicon_path.read_text(encoding="utf-8"))
            if not isinstance(lexicon_payload, list):
                raise ValueError("lexicon payload must be a JSON array")
            detector_config: dict[str, object] = {}
            if self.tech_block_config_path is not None and self.tech_block_config_path.exists():
                config_payload = json.loads(self.tech_block_config_path.read_text(encoding="utf-8"))
                if isinstance(config_payload, dict):
                    detector_config = config_payload.get("unknown_term_detector", config_payload)
                    if not isinstance(detector_config, dict):
                        detector_config = {}
            detector = UnknownTermDetector(lexicon=lexicon_payload, config=detector_config)
            min_score = float(detector_config.get("min_discovery_score", 2.0))
            candidates = detector.list_pending(
                self.lexicon_discovery_path,
                min_score=min_score,
                limit=max(self.top_n, 10),
            )
            accepted_terms = sorted(
                [
                    {
                        "text": str(item.get("canonical_text", "")).strip(),
                        "term_type": str(item.get("term_type", "theme") or "theme").strip(),
                        "synonyms": [
                            str(synonym).strip()
                            for synonym in item.get("synonyms", [])
                            if str(synonym).strip()
                        ][:5],
                        "synonym_count": len(
                            [
                                str(synonym).strip()
                                for synonym in item.get("synonyms", [])
                                if str(synonym).strip()
                            ]
                        ),
                        "trigger_tags": [
                            str(tag).strip()
                            for tag in item.get("trigger_tags", [])
                            if str(tag).strip()
                        ][:4],
                    }
                    for item in lexicon_payload
                    if str(item.get("canonical_text", "")).strip()
                ],
                key=lambda item: (
                    str(item.get("term_type", "theme")),
                    str(item.get("text", "")).lower(),
                ),
            )
        except Exception:
            candidates = []
            accepted_terms = []

        return {
            "summary": {
                "pending_count": len(candidates),
                "accepted_count": len(accepted_terms),
                "discovery_path": str(self.lexicon_discovery_path),
                "lexicon_path": str(self.lexicon_path),
            },
            "candidates": candidates,
            "accepted_terms": accepted_terms,
        }

    def _append_event_section(
        self,
        lines: list[str],
        *,
        title: str,
        events: list[object],
        grouped_instruments: dict[str, list[dict[str, object]]],
        cluster_by_id: dict[str, EventCluster],
    ) -> None:
        lines.extend([title, ""])
        if not events:
            lines.extend(["No items.", ""])
            return
        for index, event in enumerate(events[: self.top_n], start=1):
            cluster = cluster_by_id.get(event.cluster_id)
            summary = cluster.summary if cluster is not None else ""
            lines.extend(
                [
                    f"{index}. `{event.final_score}` {event.headline}",
                    f"   direction: {event.impact.direction.value} | type: {event.impact.event_type.value}",
                    f"   themes: {', '.join(event.impact.affected_themes) or 'n/a'}",
                    f"   why: {'; '.join(event.impact.rationale)}",
                ]
            )
            if summary:
                lines.append(f"   summary: {self._truncate(summary, 160)}")
            related = grouped_instruments.get(event.cluster_id, [])[:4]
            if related:
                lines.append(
                    "   instruments: "
                    + ", ".join(
                        f"{instrument['symbol']}({instrument['market']})"
                        for instrument in related
                    )
                )
        lines.append("")

    def _append_tech_block_section(
        self,
        lines: list[str],
        tech_block: dict[str, object],
    ) -> None:
        lines.extend(["## AH Tech Catalyst Block", ""])
        signals = tech_block.get("signals", []) if isinstance(tech_block, dict) else []
        assets = tech_block.get("asset_ladder", []) if isinstance(tech_block, dict) else []
        themes = tech_block.get("themes", []) if isinstance(tech_block, dict) else []
        summary = tech_block.get("summary", {}) if isinstance(tech_block, dict) else {}
        lexicon_version = summary.get("lexicon_version") if isinstance(summary, dict) else None
        if lexicon_version:
            lines.append(f"词库版本: `{lexicon_version}`")
            lines.append("")
        if not signals:
            lines.extend(["No tech speculative signals in this run.", ""])
            return

        for index, signal in enumerate(signals[: self.top_n], start=1):
            candidate_assets = ", ".join(
                f"{asset['symbol']}({asset['market']})"
                for asset in signal.get("candidate_assets", [])[:4]
            ) or "n/a"
            trigger_tags = ", ".join(signal.get("trigger_tags", [])[:4]) or "n/a"
            lines.extend(
                [
                    f"{index}. `{signal['trading_attention_score']}` {signal['headline']}",
                    f"   direction: {signal['direction']} | tier: {signal['attention_tier']}",
                    f"   triggers: {trigger_tags}",
                    f"   assets: {candidate_assets}",
                    f"   why: {'; '.join(signal.get('rationale', [])[:3])}",
                ]
            )
        lines.append("")

        lines.extend(["## AH Tech Theme Ladder", ""])
        if themes:
            for index, theme in enumerate(themes[:8], start=1):
                lines.append(
                    f"{index}. `{theme['score']}` {theme['label']} | drivers: {', '.join(theme.get('drivers', [])[:4]) or 'n/a'}"
                )
        else:
            lines.append("No active tech themes.")
        lines.append("")

        lines.extend(["## AH Tech Asset Ladder", ""])
        if assets:
            for index, asset in enumerate(assets[:10], start=1):
                lines.append(
                    f"{index}. `{asset['score']}` {asset['symbol']} {asset['name']} | {asset['direction']} | {', '.join(asset.get('drivers', [])[:3]) or 'n/a'}"
                )
        else:
            lines.append("No ranked tech assets.")
        lines.append("")

    def _append_lexicon_discovery_section(
        self,
        lines: list[str],
        lexicon_discovery: dict[str, object],
    ) -> None:
        lines.extend(["## Lexicon Discovery Queue", ""])
        summary = lexicon_discovery.get("summary", {}) if isinstance(lexicon_discovery, dict) else {}
        candidates = lexicon_discovery.get("candidates", []) if isinstance(lexicon_discovery, dict) else []
        pending_count = summary.get("pending_count", 0) if isinstance(summary, dict) else 0
        lines.append(f"待审核候选词: `{pending_count}`")
        lines.append("")
        if not candidates:
            lines.extend(["No pending discovery candidates.", ""])
            return
        for index, candidate in enumerate(candidates[:8], start=1):
            impacts = candidate.get("inferred_impact", {}) if isinstance(candidate, dict) else {}
            snippets = candidate.get("example_snippets", []) if isinstance(candidate, dict) else []
            impact_text = ", ".join(f"{key}:{value}" for key, value in list(impacts.items())[:4]) or "n/a"
            lines.extend(
                [
                    f"{index}. `{candidate.get('discovery_score', 0)}` {candidate.get('text', 'term')}",
                    f"   freq: {candidate.get('raw_freq', 0)} | impact: {impact_text}",
                    f"   snippet: {self._truncate(' | '.join(snippets), 160) if snippets else 'n/a'}",
                ]
            )
        lines.append("")

    def _event_payload(
        self,
        event: object,
        cluster: EventCluster | None,
        top_instruments: list[dict[str, object]],
    ) -> dict[str, object]:
        related_documents = []
        if cluster is not None:
            related_documents = [
                self._document_payload(document)
                for document in cluster.documents[:6]
            ]

        return {
            "cluster_id": event.cluster_id,
            "headline": event.headline,
            "summary": cluster.summary if cluster is not None else "",
            "direction": event.impact.direction.value,
            "event_type": event.impact.event_type.value,
            "final_score": event.final_score,
            "heat_score": event.heat_score,
            "importance_score": event.importance_score,
            "confidence_score": event.confidence_score,
            "markets": [market.value for market in event.impact.affected_markets],
            "themes": event.impact.affected_themes,
            "entities": cluster.entities[:8] if cluster is not None else [],
            "sectors": cluster.sectors[:6] if cluster is not None else [],
            "regions": cluster.regions[:6] if cluster is not None else [],
            "rationale": event.impact.rationale,
            "model_judgement": event.impact.model_judgement,
            "source_ids": cluster.source_ids if cluster is not None else [],
            "doc_count": cluster.doc_count if cluster is not None else len(related_documents),
            "first_seen_at": cluster.first_seen_at.isoformat() if cluster is not None else "",
            "last_seen_at": cluster.last_seen_at.isoformat() if cluster is not None else "",
            "top_instruments": top_instruments,
            "related_documents": related_documents,
        }

    def _build_ai_judgement(
        self,
        ranked_events: list[object],
        cluster_by_id: dict[str, EventCluster],
        grouped_instruments: dict[str, list[dict[str, object]]],
    ) -> dict[str, object]:
        ai_events: list[dict[str, object]] = []
        instrument_by_key: dict[str, dict[str, object]] = {}
        limit = self.top_n * 2

        for event in ranked_events:
            if not self._is_ai_selected_event(event):
                continue
            payload = self._event_payload(
                event,
                cluster_by_id.get(event.cluster_id),
                grouped_instruments.get(event.cluster_id, [])[:5],
            )
            selection = self._ai_selection_payload(event)
            payload["ai_selection"] = selection
            ai_events.append(payload)

            for instrument in payload.get("top_instruments", []):
                if not isinstance(instrument, dict):
                    continue
                self._add_ai_instrument(instrument_by_key, instrument, payload, selection)
            for instrument in self._fallback_ai_instruments(payload):
                self._add_ai_instrument(instrument_by_key, instrument, payload, selection)

            if len(ai_events) >= limit:
                break

        instruments = list(instrument_by_key.values())[:limit]
        return {
            "summary": {
                "event_count": len(ai_events),
                "instrument_count": len(instruments),
                "description": "通过AI判断或注意力门槛的新闻与候选标的。",
            },
            "events": ai_events,
            "instruments": instruments,
        }

    def _add_ai_instrument(
        self,
        instrument_by_key: dict[str, dict[str, object]],
        instrument: dict[str, object],
        event_payload: dict[str, object],
        selection: dict[str, object],
    ) -> None:
        symbol = str(instrument.get("symbol") or "").strip()
        market = str(instrument.get("market") or "").strip()
        if not symbol:
            return
        key = f"{market}:{symbol}"
        if key in instrument_by_key:
            return
        instrument_by_key[key] = {
            **instrument,
            "cluster_id": event_payload["cluster_id"],
            "headline": event_payload["headline"],
            "ai_score": selection.get("score", 0),
            "ai_tier": selection.get("tier", ""),
            "ai_reason": selection.get("summary_reason", ""),
        }

    def _fallback_ai_instruments(self, event_payload: dict[str, object]) -> list[dict[str, object]]:
        """Extract explicit stock codes from clean source text for display only.

        This keeps the AI panel useful even when the main universe has not yet
        learned a newly mentioned A/H stock. It does not feed back into ranking.
        """
        text_parts = [
            str(event_payload.get("headline") or ""),
            str(event_payload.get("summary") or ""),
        ]
        for document in event_payload.get("related_documents", []):
            if not isinstance(document, dict):
                continue
            text_parts.extend([
                str(document.get("title") or ""),
                str(document.get("summary") or ""),
            ])
        text = "\n".join(text_parts)
        instruments: list[dict[str, object]] = []
        seen: set[str] = set()

        for match in re.finditer(r"([\u4e00-\u9fffA-Za-z0-9·]{2,24})[（(](\d{5,6})\.(SH|SZ|BJ|HK)[)）]", text, re.IGNORECASE):
            name, code, suffix = match.groups()
            suffix = suffix.upper()
            symbol = f"{code}.{suffix}"
            if symbol in seen:
                continue
            seen.add(symbol)
            instruments.append(self._fallback_instrument_payload(event_payload, symbol, suffix, name))

        shorthand_name = self._extract_security_name(text)
        for code in re.findall(r"证券代码[：: ]+(\d{5,6})", text):
            suffix = self._infer_cn_suffix(code)
            symbol = f"{code}.{suffix}"
            if symbol in seen:
                continue
            seen.add(symbol)
            instruments.append(self._fallback_instrument_payload(event_payload, symbol, suffix, shorthand_name or code))

        return instruments

    def _fallback_instrument_payload(
        self,
        event_payload: dict[str, object],
        symbol: str,
        suffix: str,
        name: str,
    ) -> dict[str, object]:
        return {
            "symbol": symbol,
            "market": "HK" if suffix == "HK" else "CN-A",
            "name": name.strip(" ：:，,") or symbol,
            "direction": event_payload.get("direction") or "neutral",
            "final_score": event_payload.get("final_score") or 0,
            "reasons": ["source text code match"],
        }

    def _extract_security_name(self, text: str) -> str:
        match = re.search(r"证券简称[：: ]+([\u4e00-\u9fffA-Za-z0-9·]+)", text)
        return match.group(1) if match else ""

    def _infer_cn_suffix(self, code: str) -> str:
        if code.startswith(("6", "9")):
            return "SH"
        if code.startswith(("8", "4")) or code.startswith("920"):
            return "BJ"
        return "SZ"

    def _is_ai_selected_event(self, event: object) -> bool:
        judgement = self._model_judgement(event)
        if not judgement:
            return False
        screening = judgement.get("screening")
        status = str(judgement.get("screening_status") or "").lower()
        if status == "used":
            return isinstance(screening, dict) and screening.get("worth_attention") is True

        gate = judgement.get("attention_gate")
        if not isinstance(gate, dict):
            return False
        tier = str(gate.get("tier") or "").lower()
        score = self._safe_float(gate.get("score"))
        return tier in {"model", "notify"} and score >= 50

    def _ai_selection_payload(self, event: object) -> dict[str, object]:
        judgement = self._model_judgement(event)
        screening = judgement.get("screening")
        gate = judgement.get("attention_gate")
        screening_dict = screening if isinstance(screening, dict) else {}
        gate_dict = gate if isinstance(gate, dict) else {}
        gate_reasons = gate_dict.get("reasons")
        reasons = [str(item) for item in gate_reasons] if isinstance(gate_reasons, list) else []
        model_reason = str(
            screening_dict.get("reason")
            or screening_dict.get("reject_reason")
            or judgement.get("screening_reason")
            or ""
        ).strip()
        summary_parts = []
        if model_reason:
            summary_parts.append(model_reason)
        if reasons:
            summary_parts.append("；".join(reasons[:4]))
        selected_by = "gpt" if str(judgement.get("screening_status") or "").lower() == "used" else "attention_gate"
        return {
            "selected_by": selected_by,
            "status": judgement.get("screening_status") or "attention_gate",
            "tier": gate_dict.get("tier") or "",
            "score": self._safe_float(gate_dict.get("score")),
            "reasons": reasons,
            "model_reason": model_reason,
            "summary_reason": "；".join(summary_parts) or "通过AI注意力门槛。",
            "worth_attention": screening_dict.get("worth_attention"),
        }

    def _model_judgement(self, event: object) -> dict[str, object]:
        impact = getattr(event, "impact", None)
        judgement = getattr(impact, "model_judgement", {}) if impact is not None else {}
        return judgement if isinstance(judgement, dict) else {}

    def _safe_float(self, value: object) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _document_payload(self, document: NewsDocument) -> dict[str, object]:
        summary = document.summary.strip() or self._truncate(document.body.strip(), 220)
        return {
            "doc_id": document.doc_id,
            "published_at": document.published_at.isoformat(),
            "source_id": document.source_id,
            "title": document.title,
            "summary": summary,
            "themes": document.themes,
            "entities": document.entities[:4],
            "url": document.url,
        }

    def _enrich_tech_block(
        self,
        tech_block: object,
        cluster_by_id: dict[str, EventCluster],
    ) -> dict[str, object]:
        """Inject related_documents into each tech signal so the dashboard can
        render 主证据原文 without needing a separate ranked-event lookup."""
        if not isinstance(tech_block, dict):
            return {}
        raw_signals = tech_block.get("signals", [])
        if not isinstance(raw_signals, list):
            return dict(tech_block)
        enriched: list[dict[str, object]] = []
        for signal in raw_signals:
            if not isinstance(signal, dict):
                enriched.append(signal)
                continue
            cid = signal.get("cluster_id")
            cluster = cluster_by_id.get(cid) if cid else None
            if cluster is None:
                enriched.append(signal)
                continue
            evidence_ids: set[str] = set(signal.get("evidence_source_ids") or [])
            docs = [
                self._document_payload(doc)
                for doc in cluster.documents[:8]
                if not evidence_ids or doc.source_id in evidence_ids
            ]
            enriched.append({**signal, "related_documents": docs})
        return {**tech_block, "signals": enriched}

    def _truncate(self, value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 3].rstrip() + "..."

    def _load_runtime_status(self) -> dict[str, object]:
        health_payload = self._read_json(self.output_dir / "health_status.json")
        collect_payload = self._read_json(self.output_dir / "collect_status.json")
        delivery_payload = self._read_json(self.output_dir / "delivery_status.json")
        monitor_payload = self._read_json(self.output_dir / "monitor_status.json")

        health_checks = {}
        if health_payload:
            for check in health_payload.get("checks", []):
                name = str(check.get("name") or "").strip().lower()
                if name:
                    health_checks[name] = {
                        "name": name,
                        "status": str(check.get("status") or "unknown"),
                        "detail": str(check.get("detail") or ""),
                        "last_update": check.get("last_update"),
                        "age_seconds": check.get("age_seconds"),
                        "source_status": check.get("source_status"),
                        "status_path": check.get("status_path"),
                        "report_path": check.get("report_path"),
                        "modules": check.get("modules", []),
                    }

        lines = [
            self._runtime_line(
                name="collect",
                status_payload=collect_payload,
                health_check=health_checks.get("collect"),
            ),
            self._runtime_line(
                name="delivery",
                status_payload=delivery_payload,
                health_check=health_checks.get("delivery"),
            ),
            self._runtime_line(
                name="review_api",
                status_payload=self._read_json(self.output_dir / "review_api_status.json"),
                health_check=health_checks.get("review_api"),
            ),
            self._runtime_health_line(health_payload),
            self._runtime_cookie_line(),
            self._runtime_futu_enrichment_line(),
            self._runtime_ah_scanner_line(),
        ]

        if not collect_payload and not delivery_payload and monitor_payload:
            lines = [
                self._runtime_line(
                    name="monitor",
                    status_payload=monitor_payload,
                    health_check=health_checks.get("monitor"),
                ),
                self._runtime_health_line(health_payload),
            ]

        checked_at = None
        if health_payload:
            checked_at = health_payload.get("timestamp")
        else:
            available_timestamps = [
                line["last_update"] for line in lines if line.get("last_update")
            ]
            checked_at = available_timestamps[0] if available_timestamps else None

        return {
            "overall_status": health_payload.get("overall_status")
            if health_payload
            else self._derive_runtime_overall(lines),
            "checked_at": checked_at,
            "lines": lines,
        }

    def _runtime_line(
        self,
        *,
        name: str,
        status_payload: dict[str, object] | None,
        health_check: dict[str, object] | None,
    ) -> dict[str, object]:
        if health_check is not None:
            return health_check
        if status_payload is None:
            return {
                "name": name,
                "status": "missing",
                "detail": "还没有产出状态文件",
                "last_update": None,
                "age_seconds": None,
                "source_status": None,
                "status_path": str(self.output_dir / f"{name}_status.json"),
                "report_path": None,
                "modules": [],
            }
        artifacts = status_payload.get("artifacts", {})
        return {
            "name": name,
            "status": str(status_payload.get("overall_status") or "unknown"),
            "detail": "状态文件已更新",
            "last_update": status_payload.get("timestamp"),
            "age_seconds": None,
            "source_status": status_payload.get("overall_status"),
            "status_path": str(self.output_dir / f"{name}_status.json"),
            "report_path": artifacts.get("json_report"),
            "modules": status_payload.get("modules", []),
        }

    def _runtime_health_line(self, health_payload: dict[str, object] | None) -> dict[str, object]:
        if health_payload is None:
            return {
                "name": "health",
                "status": "missing",
                "detail": "健康监控还没有运行",
                "last_update": None,
                "age_seconds": None,
            "source_status": None,
            "status_path": str(self.output_dir / "health_status.json"),
            "report_path": None,
            "modules": [],
        }
        return {
            "name": "health",
            "status": str(health_payload.get("overall_status") or "unknown"),
            "detail": "健康监控快照",
            "last_update": health_payload.get("timestamp"),
            "age_seconds": None,
            "source_status": health_payload.get("overall_status"),
            "status_path": str(self.output_dir / "health_status.json"),
            "report_path": None,
            "modules": [],
        }

    def _runtime_cookie_line(self) -> dict[str, object]:
        """Build a runtime status card showing whether weibo/xueqiu cookies are valid."""
        import json as _json
        from market_news.infrastructure.cookie_store import (
            is_cookie_expired,
            market_news_cookie_dir,
        )

        cookie_dir = market_news_cookie_dir()
        known = [
            ("weibo",  cookie_dir / "weibo_cookies.json"),
            ("xueqiu", cookie_dir / "xueqiu_cookies.json"),
        ]
        modules: list[dict[str, object]] = []
        any_error = False
        all_missing = True

        for name, path in known:
            if not path.exists():
                modules.append({"name": name, "status": "missing"})
            else:
                all_missing = False
                expired, _reason = is_cookie_expired(path)
                if expired:
                    any_error = True
                    expired_at = ""
                    try:
                        data = _json.loads(path.with_suffix(".expired").read_text(encoding="utf-8"))
                        raw_ts = str(data.get("expired_at", ""))
                        if raw_ts:
                            from datetime import datetime, UTC as _UTC
                            dt = datetime.fromisoformat(raw_ts)
                            expired_at = dt.strftime("%m-%d %H:%M")
                    except Exception:
                        pass
                    modules.append({
                        "name": name,
                        "status": "error",
                        "reason": f"过期 {expired_at}".strip() if expired_at else "已过期",
                    })
                else:
                    modules.append({"name": name, "status": "ok"})

        if any_error:
            overall, detail = "error", "Cookie 已过期 — 运行 market-news cookies set-<source>"
        elif all_missing:
            overall, detail = "missing", "Cookie 未配置 — 运行 market-news cookies set-weibo/set-xueqiu"
        elif any(m["status"] == "missing" for m in modules):
            overall, detail = "degraded", "部分 Cookie 未配置"
        else:
            overall, detail = "ok", "所有 Cookie 有效"

        return {
            "name": "cookies",
            "status": overall,
            "detail": detail,
            "last_update": None,
            "age_seconds": None,
            "source_status": overall,
            "status_path": str(cookie_dir),
            "report_path": None,
            "modules": modules,
        }

    def _runtime_futu_enrichment_line(self) -> dict[str, object]:
        """Status card for the Futu news-alert enrichment sidecar.

        Reads two pieces of evidence (no Futu calls — purely file-based):
        1. ~/.market_news/futu_env: presence + content tells us if user has
           clicked Enable_Futu_Enrichment.command.
        2. reports/live/latest_phone_alert_enriched.json: the sidecar artifact
           the notify line produces when enrichment is on.
        """

        import os as _os
        from datetime import datetime, timezone as _tz

        toggle_path = Path(_os.path.expanduser("~/.market_news/futu_env"))
        enabled = False
        if toggle_path.exists():
            try:
                content = toggle_path.read_text(encoding="utf-8")
                # Treat the var as enabled when set to a truthy value.
                for line in content.splitlines():
                    if "MARKET_NEWS_FUTU_ENRICHMENT" in line and "=1" in line:
                        enabled = True
                        break
            except Exception:
                pass

        sidecar = self.output_dir / "latest_phone_alert_enriched.json"
        modules: list[dict[str, object]] = []
        last_update: str | None = None
        age_seconds: float | None = None
        sidecar_alerts = 0
        if sidecar.exists():
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
                last_update = str(payload.get("generated_at", "")) or None
                alerts = payload.get("alerts") or []
                sidecar_alerts = len(alerts) if isinstance(alerts, list) else 0
                if last_update:
                    dt = datetime.fromisoformat(last_update.replace("Z", "+00:00"))
                    age_seconds = max(0.0, (datetime.now(_tz.utc) - dt).total_seconds())
            except Exception:
                pass

        if not enabled and not sidecar.exists():
            overall = "missing"
            detail = "未启用 — 双击 Enable_Futu_Enrichment.command 开启"
        elif enabled and not sidecar.exists():
            overall = "idle"
            detail = "已启用，等待下一轮 notify 写入增强数据"
        elif enabled and sidecar.exists():
            overall = "ok"
            detail = f"最近增强了 {sidecar_alerts} 条告警"
            modules.append({"name": "capital_flow", "status": "ok"})
            modules.append({"name": "capital_distribution", "status": "ok"})
            modules.append({"name": "community_sentiment", "status": "ok"})
        else:
            overall = "idle"
            detail = "未启用 — 历史 enriched 文件仍保留"

        return {
            "name": "futu_enrichment",
            "status": overall,
            "detail": detail,
            "last_update": last_update,
            "age_seconds": age_seconds,
            "source_status": overall,
            "status_path": str(toggle_path),
            "report_path": str(sidecar) if sidecar.exists() else None,
            "modules": modules,
        }

    def _runtime_ah_scanner_line(self) -> dict[str, object]:
        """Status card for the AH multi-factor scanner.

        Reads `reports/live/scan_summary.json` if present. No Futu calls.
        """

        from datetime import datetime, timezone as _tz

        summary_path = self.output_dir / "scan_summary.json"
        if not summary_path.exists():
            return {
                "name": "ah_scanner",
                "status": "missing",
                "detail": "还没跑过 — 双击 AH_Multi_Factor_Scanner.command",
                "last_update": None,
                "age_seconds": None,
                "source_status": "missing",
                "status_path": str(summary_path),
                "report_path": None,
                "modules": [],
            }

        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "name": "ah_scanner",
                "status": "error",
                "detail": "scan_summary.json 读取失败",
                "last_update": None,
                "age_seconds": None,
                "source_status": "error",
                "status_path": str(summary_path),
                "report_path": None,
                "modules": [],
            }

        last_update = str(payload.get("generated_at", "")) or None
        age_seconds: float | None = None
        if last_update:
            try:
                dt = datetime.fromisoformat(last_update.replace("Z", "+00:00"))
                age_seconds = max(0.0, (datetime.now(_tz.utc) - dt).total_seconds())
            except Exception:
                pass

        u = int(payload.get("universe_count", 0) or 0)
        s = int(payload.get("limit_up_streak_count", 0) or 0)
        v = int(payload.get("volume_shrink_up_count", 0) or 0)
        a = int(payload.get("near_ath_count", 0) or 0)
        errors = payload.get("errors") or []

        modules = [
            {"name": f"universe {u}", "status": "ok" if u else "idle"},
            {"name": f"连板 {s}", "status": "ok" if s else "idle"},
            {"name": f"缩量上涨 {v}", "status": "ok" if v else "idle"},
            {"name": f"近新高 {a}", "status": "ok" if a else "idle"},
        ]

        if payload.get("skipped_reason"):
            status = "degraded"
            detail = f"跳过：{payload.get('skipped_reason')}"
        elif errors:
            status = "degraded"
            detail = f"{u} 候选；{len(errors)} 个非致命错误"
        elif u == 0:
            status = "idle"
            detail = "扫描完成但 universe 为空（OpenD 是否在线？）"
        else:
            status = "ok"
            detail = f"扫描 {u} 候选 → {s} 连板 / {v} 缩量上涨 / {a} 近新高"

        return {
            "name": "ah_scanner",
            "status": status,
            "detail": detail,
            "last_update": last_update,
            "age_seconds": age_seconds,
            "source_status": status,
            "status_path": str(summary_path),
            "report_path": str(self.output_dir),
            "modules": modules,
        }

    def _derive_runtime_overall(self, lines: list[dict[str, object]]) -> str:
        statuses = {str(line.get("status") or "unknown") for line in lines}
        if statuses <= {"ok", "idle"}:
            return "ok"
        if statuses & {"error", "missing"}:
            return "error"
        return "degraded"

    def _read_json(self, path: Path) -> dict[str, object] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _build_html(self, payload: dict[str, object]) -> str:
        payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        template = Template(
            """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store, max-age=0">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <title>Market News Board</title>
  <style>
    /* ── JQ Quant 统一设计令牌 ──────────────────────────────────────────
       本看板嵌在交易终端里显示。原先它是暖米色 + 衬线标题 + 低饱和大地色，
       而终端是冷蓝灰 + 无衬线 + 高饱和涨跌色——两套语言在同一个窗口里对撞，
       接缝非常明显。现统一到终端那套（冷色技术风），并保留高饱和涨跌色，
       因为看盘时红绿必须一眼可辨。

       变量名一个没改，只换值：改配色只动这一处，不必翻遍样式表。 */
    :root {
      --bg: #eef3f8;
      --bg-soft: #f7fafd;
      --ink: #182534;
      --muted: #6b7d90;
      --line: rgba(24, 37, 52, 0.12);
      --card: rgba(255, 255, 255, 0.96);
      --accent: #185fa5;
      --accent-soft: rgba(55, 138, 221, 0.14);
      /* 涨跌用高饱和：低饱和的砖红/深绿在数据密集处不够醒目 */
      --danger: #c02a44;
      --danger-soft: rgba(255, 77, 103, 0.16);
      --success: #0f7a5c;
      --success-soft: rgba(32, 201, 151, 0.16);
      --warning: #a36a0d;
      --warning-soft: rgba(190, 123, 29, 0.16);
      /* 阴影收敛：原来 40px 的大扩散在终端的浅底上显得脏 */
      --shadow: 0 10px 24px rgba(24, 37, 52, 0.07);
      /* 圆角收敛：26px 太圆，与终端的方正卡片不搭 */
      --radius-lg: 16px;
      --radius-md: 12px;
      --radius-sm: 8px;
      /* 标题不再用衬线，与终端一致 */
      --headline: -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", "Segoe UI", sans-serif;
      --ui: -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", "Segoe UI", sans-serif;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      font-family: var(--ui);
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(13, 111, 111, 0.12), transparent 28%),
        radial-gradient(circle at top right, rgba(190, 123, 29, 0.16), transparent 22%),
        linear-gradient(180deg, #f5f9fc 0%, #e7eef6 100%);
      min-height: 100vh;
      overflow-x: hidden;
      overflow-y: auto;
    }

    a {
      color: var(--accent);
      text-decoration: none;
    }

    a:hover {
      text-decoration: underline;
    }

    .shell {
      width: min(1440px, calc(100vw - 28px));
      margin: 18px auto 32px;
      display: grid;
      gap: 18px;
      grid-template-rows: auto auto auto;
    }

    .hero,
    .panel {
      border: 1px solid var(--line);
      background: var(--card);
      backdrop-filter: blur(14px);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
    }

    .hero {
      padding: 24px 26px;
      display: grid;
      gap: 20px;
      grid-template-columns: 1.5fr 1fr;
      overflow: hidden;
      position: relative;
    }

    .hero::after {
      content: "";
      position: absolute;
      inset: auto -120px -120px auto;
      width: 280px;
      height: 280px;
      background: radial-gradient(circle, rgba(13, 111, 111, 0.18), transparent 65%);
      pointer-events: none;
    }

    .eyebrow {
      margin: 0 0 8px;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      font-size: 12px;
      color: var(--muted);
    }

    h1 {
      margin: 0;
      font-family: var(--headline);
      font-weight: 700;
      font-size: clamp(32px, 5vw, 58px);
      line-height: 0.96;
      max-width: 12ch;
    }

    .hero-copy p {
      margin: 16px 0 0;
      max-width: 68ch;
      color: var(--muted);
      font-size: 15px;
      line-height: 1.65;
    }

    .ticker {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border-radius: 999px;
      background: var(--warning-soft);
      border: 1px solid rgba(190, 123, 29, 0.18);
      color: var(--warning);
      font-size: 13px;
      margin-top: 18px;
    }

    .metric-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      align-self: start;
    }

    .metric-card {
      padding: 16px;
      border-radius: var(--radius-md);
      background: var(--bg-soft);
      border: 1px solid var(--line);
    }

    .metric-card .label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      margin-bottom: 8px;
    }

    .metric-card .value {
      font-size: 30px;
      font-weight: 700;
      line-height: 1;
      margin-bottom: 6px;
    }

    .metric-card .meta {
      color: var(--muted);
      font-size: 13px;
    }

    .toolbar {
      display: grid;
      gap: 12px;
      grid-template-columns: auto 1fr auto;
      align-items: center;
    }

    .view-switch {
      display: inline-flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }

    .view-button {
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.84);
      color: var(--muted);
      border-radius: 999px;
      padding: 11px 16px;
      font-size: 13px;
      cursor: pointer;
      transition: transform 120ms ease, background 120ms ease, color 120ms ease, border-color 120ms ease;
    }

    .view-button:hover {
      transform: translateY(-1px);
    }

    .view-button.active {
      color: #fff;
      background: var(--accent);
      border-color: var(--accent);
    }

    .filters {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }

    .chip {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--muted);
      border-radius: 999px;
      padding: 10px 14px;
      font-size: 13px;
      cursor: pointer;
      transition: transform 120ms ease, background 120ms ease, color 120ms ease;
    }

    .chip:hover {
      transform: translateY(-1px);
    }

    .chip.active {
      color: #fff;
      background: var(--ink);
      border-color: var(--ink);
    }

    .tools {
      display: flex;
      gap: 10px;
      align-items: center;
    }

    .status-panel {
      padding: 18px 20px;
    }

    .runtime-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 12px;
    }

    .status-card {
      border-radius: var(--radius-md);
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.88);
      padding: 14px 16px;
    }

    .status-card.ok,
    .status-card.idle {
      border-color: rgba(30, 124, 87, 0.22);
      background: rgba(30, 124, 87, 0.07);
    }

    .status-card.degraded,
    .status-card.stale {
      border-color: rgba(190, 123, 29, 0.26);
      background: rgba(190, 123, 29, 0.08);
    }

    .status-card.error,
    .status-card.missing {
      border-color: rgba(181, 72, 61, 0.24);
      background: rgba(181, 72, 61, 0.08);
    }

    .status-topline {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      margin-bottom: 10px;
    }

    .status-name {
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
    }

    .status-state {
      font-size: 12px;
      font-weight: 700;
      padding: 5px 10px;
      border-radius: 999px;
      background: rgba(17, 32, 45, 0.08);
      color: var(--ink);
    }

    .status-card.ok .status-state,
    .status-card.idle .status-state {
      background: var(--success-soft);
      color: var(--success);
    }

    .status-card.degraded .status-state,
    .status-card.stale .status-state {
      background: var(--warning-soft);
      color: var(--warning);
    }

    .status-card.error .status-state,
    .status-card.missing .status-state {
      background: var(--danger-soft);
      color: var(--danger);
    }

    .status-note {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
    }

    .status-meta {
      display: grid;
      gap: 4px;
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
    }

    .status-module-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }

    .status-module {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 12px;
      background: rgba(17, 32, 45, 0.06);
      color: var(--muted);
    }

    .status-module.ok,
    .status-module.idle,
    .status-module.active {
      background: var(--success-soft);
      color: var(--success);
    }

    .status-module.degraded,
    .status-module.stale {
      background: var(--warning-soft);
      color: var(--warning);
    }

    .status-module.error,
    .status-module.missing {
      background: var(--danger-soft);
      color: var(--danger);
    }

    .workspace {
      display: none;
    }

    .workspace.active {
      display: block;
    }

    .tech-stack {
      display: grid;
      gap: 10px;
      align-content: start;
    }

    .tech-detail-grid {
      display: grid;
      gap: 14px;
    }

    .tech-card,
    .theme-card,
    .asset-card {
      border-radius: var(--radius-md);
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.88);
      padding: 14px 16px;
      transition: transform 140ms ease, border-color 140ms ease, box-shadow 140ms ease;
    }

    .tech-card:hover,
    .asset-card:hover {
      transform: translateY(-2px);
      border-color: rgba(13, 111, 111, 0.25);
      box-shadow: 0 12px 28px rgba(17, 32, 45, 0.08);
    }

    .tech-card button,
    .asset-card button {
      width: 100%;
      border: none;
      padding: 0;
      background: transparent;
      color: inherit;
      font: inherit;
      text-align: left;
      cursor: pointer;
    }

    .tech-score-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
    }

    .mini-score {
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(17, 32, 45, 0.06);
      color: var(--muted);
      font-size: 12px;
    }

    .review-note {
      margin: -2px 0 14px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }

    .review-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-top: 12px;
    }

    .review-select {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.96);
      color: var(--ink);
      padding: 7px 10px;
      font-size: 13px;
    }

    .review-action {
      border: none;
      border-radius: 999px;
      padding: 8px 12px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: transform 120ms ease, box-shadow 120ms ease, opacity 120ms ease;
    }

    .review-action:hover {
      transform: translateY(-1px);
      box-shadow: 0 10px 20px rgba(17, 32, 45, 0.08);
    }

    .review-action:disabled {
      opacity: 0.55;
      cursor: wait;
      transform: none;
      box-shadow: none;
    }

    .review-action.approve {
      background: var(--success-soft);
      color: var(--success);
    }

    .review-action.reject {
      background: rgba(17, 32, 45, 0.08);
      color: var(--ink);
    }

    .ask-panel {
      display: grid;
      gap: 14px;
    }

    .ask-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(280px, 0.9fr);
      gap: 14px;
    }

    .ask-input,
    .ask-question {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.92);
      color: var(--ink);
      font: inherit;
      outline: none;
      resize: vertical;
    }

    .ask-input {
      min-height: 160px;
      padding: 14px 16px;
      line-height: 1.6;
    }

    .ask-question {
      min-height: 72px;
      padding: 12px 14px;
      line-height: 1.5;
    }

    .drop-zone {
      border: 1px dashed rgba(17, 32, 45, 0.24);
      border-radius: 18px;
      padding: 14px;
      background: rgba(17, 32, 45, 0.03);
      color: var(--muted);
      line-height: 1.55;
    }

    .drop-zone.dragover {
      border-color: rgba(13, 111, 111, 0.45);
      background: rgba(13, 111, 111, 0.08);
      color: var(--ink);
    }

    .drag-news-card {
      cursor: grab;
      user-select: none;
    }

    .drag-news-card:active {
      cursor: grabbing;
    }

    .drag-news-card.dragging {
      opacity: 0.62;
      transform: translateY(-2px);
    }

    .ai-answer {
      border-radius: var(--radius-md);
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.88);
      padding: 14px 16px;
      color: var(--muted);
      line-height: 1.6;
    }

    .ai-answer strong {
      color: var(--ink);
    }

    .ai-review-box {
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(13, 111, 111, 0.06);
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }

    .ai-review-box .ai-review-action {
      color: var(--ink);
      font-weight: 700;
    }

    .search {
      width: min(340px, 46vw);
      padding: 11px 14px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.9);
      color: var(--ink);
      font-size: 14px;
      outline: none;
    }

    .search:focus {
      border-color: rgba(13, 111, 111, 0.4);
      box-shadow: 0 0 0 4px rgba(13, 111, 111, 0.08);
    }

    .review-search {
      width: 100%;
      margin-bottom: 12px;
    }

    .layout {
      display: grid;
      grid-template-columns: minmax(280px, 1fr) minmax(520px, 1.45fr) minmax(360px, 1.08fr);
      gap: 18px;
      align-items: start;
    }

    .column {
      display: grid;
      gap: 18px;
      align-content: start;
      min-height: 0;
    }

    .column-scroll {
      height: clamp(760px, calc(100vh - 220px), 1200px);
      overflow-y: scroll;
      overflow-x: hidden;
      overscroll-behavior: contain;
      -webkit-overflow-scrolling: touch;
      padding-right: 8px;
      margin-right: -6px;
      scrollbar-gutter: stable;
      scrollbar-width: thin;
      scrollbar-color: rgba(17, 32, 45, 0.22) transparent;
    }

    .column-scroll::-webkit-scrollbar {
      width: 10px;
    }

    .column-scroll::-webkit-scrollbar-thumb {
      background: rgba(17, 32, 45, 0.18);
      border-radius: 999px;
      border: 2px solid rgba(255, 255, 255, 0.3);
    }

    .panel {
      padding: 20px;
      animation: rise 280ms ease;
    }

    @keyframes rise {
      from {
        opacity: 0;
        transform: translateY(8px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    .panel h2 {
      margin: 0 0 14px;
      font-family: var(--headline);
      font-size: 26px;
    }

    .panel-subtitle {
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 14px;
    }

    .left-column .panel,
    .middle-column .panel,
    .right-column .panel {
      width: 100%;
    }

    .stack {
      display: grid;
      gap: 12px;
      align-content: start;
    }

    .ai-group-title {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-top: 2px;
    }

    .ai-judgement-panel {
      border-color: rgba(13, 111, 111, 0.18);
      background:
        linear-gradient(135deg, rgba(219, 236, 252, 0.88), rgba(255, 255, 255, 0.86)),
        var(--card);
    }

    .ai-workspace-list {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      align-items: start;
    }

    .ai-judgement-panel .event-card,
    .ai-judgement-panel .instrument-card {
      background: rgba(255, 255, 255, 0.72);
    }

    .ai-reason {
      margin-top: 8px;
      padding: 8px 10px;
      border-radius: var(--radius-sm);
      background: rgba(13, 111, 111, 0.08);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.55;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .alert-card,
    .event-card,
    .instrument-card,
    .feed-card,
    .doc-card {
      border-radius: var(--radius-md);
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.88);
      padding: 14px 16px;
      transition: transform 140ms ease, border-color 140ms ease, box-shadow 140ms ease;
    }

    button.card-button {
      width: 100%;
      text-align: left;
      background: transparent;
      border: none;
      padding: 0;
      min-width: 0;
      cursor: pointer;
      color: inherit;
      font: inherit;
    }

    .alert-card:hover,
    .event-card:hover,
    .instrument-card:hover,
    .feed-card:hover {
      transform: translateY(-2px);
      border-color: rgba(13, 111, 111, 0.25);
      box-shadow: 0 12px 28px rgba(17, 32, 45, 0.08);
    }

    .event-card.active,
    .alert-card.active,
    .instrument-card.active,
    .tech-card.active {
      border-color: rgba(13, 111, 111, 0.42);
      box-shadow: 0 0 0 4px rgba(13, 111, 111, 0.08);
    }

    .card-topline,
    .card-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.01em;
    }

    .badge.level-critical,
    .badge.dir-negative {
      background: var(--danger-soft);
      color: var(--danger);
    }

    .badge.level-high,
    .badge.type-regulation {
      background: var(--warning-soft);
      color: var(--warning);
    }

    .badge.level-medium,
    .badge.dir-neutral {
      background: rgba(17, 32, 45, 0.08);
      color: var(--ink);
    }

    .badge.dir-positive,
    .badge.type-company {
      background: var(--success-soft);
      color: var(--success);
    }

    .headline {
      margin: 10px 0 8px;
      font-size: 18px;
      line-height: 1.35;
      font-weight: 700;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .headline-serif {
      font-family: var(--headline);
      font-size: 30px;
      line-height: 1.12;
      margin: 0 0 12px;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .summary {
      color: var(--muted);
      font-size: 14px;
      line-height: 1.6;
      margin: 0;
      overflow-wrap: anywhere;
      word-break: break-word;
      white-space: pre-wrap;
    }

    .chip-row,
    .tag-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }

    .mini-tag {
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(17, 32, 45, 0.06);
      color: var(--muted);
      font-size: 12px;
    }

    .score-strip {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin: 16px 0;
    }

    .score-box {
      border-radius: var(--radius-sm);
      background: var(--bg-soft);
      border: 1px solid var(--line);
      padding: 12px;
    }

    .score-box .label {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      margin-bottom: 6px;
    }

    .score-box .value {
      font-size: 24px;
      font-weight: 700;
    }

    .detail-grid {
      display: grid;
      gap: 14px;
    }

    .detail-block {
      padding: 14px 16px;
      border-radius: var(--radius-md);
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.78);
    }

    #detailView {
      min-height: 0;
    }

    .detail-section-title {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      margin-bottom: 10px;
      font-size: 12px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
    }

    .detail-times {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
      margin-top: 12px;
    }

    .detail-section-title strong {
      color: var(--ink);
      letter-spacing: 0.02em;
      text-transform: none;
      font-size: 15px;
    }

    .doc-list,
    .rationale-list {
      display: grid;
      gap: 10px;
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .doc-card p,
    .instrument-note,
    .feed-card p {
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
      overflow-wrap: anywhere;
      word-break: break-word;
      white-space: pre-wrap;
    }

    .event-card .summary,
    .alert-card .summary,
    .instrument-card .summary,
    .feed-card p {
      display: -webkit-box;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 5;
      overflow: hidden;
    }

    .detail-block .summary,
    .detail-block p,
    .detail-block li,
    .detail-block a {
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .rationale-list li {
      padding: 10px 12px;
      border-radius: var(--radius-sm);
      background: rgba(17, 32, 45, 0.05);
      line-height: 1.55;
    }

    .empty {
      padding: 22px;
      border-radius: var(--radius-md);
      background: var(--bg-soft);
      border: 1px dashed var(--line);
      color: var(--muted);
      text-align: center;
    }

    .section-header {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: end;
      margin-bottom: 14px;
    }

    .tiny {
      color: var(--muted);
      font-size: 12px;
    }

    @media (max-width: 1180px) {
      .hero {
        grid-template-columns: 1fr;
      }

      .metric-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .layout {
        grid-template-columns: 1fr;
      }

      .ai-workspace-list {
        grid-template-columns: 1fr;
      }

      .column-scroll {
        height: auto;
        overflow: visible;
        padding-right: 0;
        margin-right: 0;
        scrollbar-gutter: auto;
      }
    }

    @media (max-width: 720px) {
      .shell {
        width: min(100vw - 18px, 100%);
      }

      .hero,
      .panel {
        border-radius: 22px;
        padding: 18px;
      }

      .toolbar {
        grid-template-columns: 1fr;
      }

      .ask-grid {
        grid-template-columns: 1fr;
      }

      .runtime-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .tools {
        flex-wrap: wrap;
      }

      .search {
        width: 100%;
      }

      .metric-grid,
      .runtime-grid,
      .score-strip {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">Market News Board</p>
        <h1>可点击市场消息控制台</h1>
        <p id="heroMeta"></p>
        <div class="ticker">
          <span id="refreshText">页面会自动刷新</span>
        </div>
      </div>
      <div class="metric-grid" id="metricGrid"></div>
    </section>

    <section class="toolbar">
      <div class="view-switch" id="viewSwitch"></div>
      <div class="filters" id="filterChips"></div>
      <div class="tools">
        <input id="searchInput" class="search" type="search" placeholder="搜索新闻、主题、股票代码、公司名">
        <button id="resetButton" class="chip" type="button">重置</button>
      </div>
    </section>

    <section class="panel status-panel">
      <div class="section-header">
        <div>
          <h2>Runtime Status</h2>
          <div class="panel-subtitle">把收集线、推送线和健康监控集合到同一张网页里看。</div>
        </div>
        <div class="tiny" id="runtimeOverallLabel"></div>
      </div>
      <div class="runtime-grid" id="runtimeStatusGrid"></div>
    </section>

    <section class="panel ask-panel">
      <div class="section-header">
        <div>
          <h2>主动问AI</h2>
          <div class="panel-subtitle">把新闻、公告或网页文字拖进来，主动问“这条消息值不值得看、影响哪些票”。点击才会调用模型。</div>
        </div>
        <button id="aiAskButton" class="review-action approve" type="button">分析这条新闻</button>
      </div>
      <div class="ask-grid">
        <div>
          <textarea id="aiAskInput" class="ask-input" placeholder="把新闻卡片拖到右侧框，或把新闻正文、公告内容、网页文字粘贴到这里。"></textarea>
        </div>
        <div class="tech-stack">
          <textarea id="aiQuestionInput" class="ask-question" placeholder="可选：你想重点问什么？比如：这是不是领先信号？影响哪些港A股？"></textarea>
          <div id="aiDropZone" class="drop-zone">把你看中的新闻卡片拖到这里；也支持 .txt/.md 文件或网页选中文字。</div>
        </div>
      </div>
      <div id="aiAskResult" class="ai-answer">还没有主动分析。这个入口不会自动消耗模型额度，只有点击“分析这条新闻”才会请求AI。</div>
    </section>

    <section class="workspace active" id="coreWorkspace" data-view="core">
      <section class="layout">
        <section class="column column-scroll left-column" id="coreLeftColumn" data-scroll-key="core-left">
          <section class="panel alerts-panel">
            <div class="section-header">
              <div>
                <h2>Alert Radar</h2>
                <div class="panel-subtitle">先看提醒，再点进去看事件链和原文。</div>
              </div>
              <div class="tiny" id="alertCountLabel"></div>
            </div>
            <div class="stack" id="alertsList"></div>
          </section>

          <section class="panel instruments-panel">
            <div class="section-header">
              <div>
                <h2>Instrument Ladder</h2>
                <div class="panel-subtitle">按候选标的强度排序，点击会联动右侧详情。</div>
              </div>
              <div class="tiny" id="instrumentCountLabel"></div>
            </div>
            <div class="stack" id="instrumentList"></div>
          </section>

          <section class="panel feed-panel">
            <div class="section-header">
              <div>
                <h2>Latest Feed</h2>
                <div class="panel-subtitle">原始消息流，保留发布时间和原文跳转。</div>
              </div>
              <div class="tiny" id="feedCountLabel"></div>
            </div>
            <div class="stack" id="feedList"></div>
          </section>
        </section>

        <section class="column column-scroll middle-column" id="coreMiddleColumn" data-scroll-key="core-middle">
          <section class="panel events-panel">
            <div class="section-header">
              <div>
                <h2>Event Deck</h2>
                <div class="panel-subtitle">事件卡片支持筛选、搜索和点击展开。</div>
              </div>
              <div class="tiny" id="eventCountLabel"></div>
            </div>
            <div class="stack" id="eventsGrid"></div>
          </section>
        </section>

        <section class="column column-scroll right-column" id="coreRightColumn" data-scroll-key="core-right">
          <section class="panel detail-panel">
            <div class="section-header">
              <div>
                <h2>Event Detail</h2>
                <div class="panel-subtitle">这里会聚合理由、标的、相关新闻原文链接。</div>
              </div>
            </div>
            <div id="detailView"></div>
          </section>
        </section>
      </section>
    </section>

    <section class="workspace" id="aiWorkspace" data-view="ai">
      <section class="panel ai-judgement-panel">
        <div class="section-header">
          <div>
            <h2>AI判断</h2>
            <div class="panel-subtitle">把通过AI判断或注意力门槛的新闻和候选股票集中放这里。</div>
          </div>
          <div class="tiny" id="aiJudgementCountLabel"></div>
        </div>
        <div class="stack ai-workspace-list" id="aiJudgementList"></div>
      </section>
    </section>

    <section class="workspace" id="techWorkspace" data-view="tech">
      <section class="layout">
        <section class="column column-scroll left-column" id="techLeftColumn" data-scroll-key="tech-left">
          <section class="panel">
            <div class="section-header">
              <div>
                <h2>AH Tech Catalyst Block</h2>
                <div class="panel-subtitle">从全市场新闻里筛出港A科技里更像会被交易的小催化。</div>
              </div>
              <div class="tiny" id="techSummaryLabel"></div>
            </div>
            <div class="stack" id="techSignalList"></div>
          </section>

          <section class="panel">
            <div class="section-header">
              <div>
                <h2>Theme Ladder</h2>
                <div class="panel-subtitle">先看哪些科技主题被触发，再看链条怎么扩散。</div>
              </div>
              <div class="tiny" id="techThemeCountLabel"></div>
            </div>
            <div class="tech-stack" id="techThemeList"></div>
          </section>

          <section class="panel">
            <div class="section-header">
              <div>
                <h2>Frontier Tracker</h2>
                <div class="panel-subtitle">把“美强中追”的前沿突破单独拎出来看，优先看真正有追赶意义的科技点。</div>
              </div>
              <div class="tiny" id="techFrontierCountLabel"></div>
            </div>
            <div class="tech-stack" id="techFrontierList"></div>
          </section>
        </section>

        <section class="column column-scroll middle-column" id="techMiddleColumn" data-scroll-key="tech-middle">
          <section class="panel">
            <div class="section-header">
              <div>
                <h2>Asset Ladder</h2>
                <div class="panel-subtitle">港A科技候选标的按专题关注度排序。</div>
              </div>
              <div class="tiny" id="techAssetCountLabel"></div>
            </div>
            <div class="tech-stack" id="techAssetList"></div>
          </section>

          <section class="panel">
            <div class="section-header">
              <div>
                <h2>Lexicon Discovery</h2>
                <div class="panel-subtitle">新冒出来的词会先进入队列，再由 AI 自动收录或删除。</div>
              </div>
              <div class="review-row">
                <button id="lexiconAiReviewButton" class="review-action approve" type="button">立即AI自动处理</button>
                <div class="tiny" id="lexiconDiscoveryCountLabel"></div>
              </div>
            </div>
            <div class="review-note" id="lexiconReviewNote"></div>
            <div class="tech-stack" id="lexiconDiscoveryList"></div>
          </section>

          <section class="panel">
            <div class="section-header">
              <div>
                <h2>Lexicon Catalog</h2>
                <div class="panel-subtitle">正式词库里已经生效的词。加错了可以直接从这里删掉。</div>
              </div>
              <div class="tiny" id="lexiconCatalogCountLabel"></div>
            </div>
            <input id="lexiconCatalogQuery" class="search review-search" type="search" placeholder="搜索正式词库词条、类型、同义词">
            <div class="tech-stack" id="lexiconCatalogList"></div>
          </section>
        </section>

        <section class="column column-scroll right-column" id="techRightColumn" data-scroll-key="tech-right">
          <section class="panel detail-panel">
            <div class="section-header">
              <div>
                <h2>Tech Event Detail</h2>
                <div class="panel-subtitle">把触发词、影响链、候选标的和原始新闻放在一起看。</div>
              </div>
              <div class="tiny" id="techSignalCountLabel"></div>
            </div>
            <div id="techDetailView"></div>
          </section>
        </section>
      </section>
    </section>

    <section class="workspace" id="frontierWorkspace" data-view="frontier">
      <section class="layout">
        <section class="column column-scroll left-column" id="frontierLeftColumn" data-scroll-key="frontier-left">
          <section class="panel">
            <div class="section-header">
              <div>
                <h2>Frontier Tracker</h2>
                <div class="panel-subtitle">只看“美强中追”的关键突破点，按追赶价值聚合。</div>
              </div>
              <div class="tiny" id="frontierTrackerCountLabel"></div>
            </div>
            <div class="tech-stack" id="frontierTrackerList"></div>
          </section>
        </section>

        <section class="column column-scroll middle-column" id="frontierMiddleColumn" data-scroll-key="frontier-middle">
          <section class="panel">
            <div class="section-header">
              <div>
                <h2>Frontier Signal Deck</h2>
                <div class="panel-subtitle">只展示命中过前沿差距图谱的科技信号。</div>
              </div>
              <div class="tiny" id="frontierSignalCountLabel"></div>
            </div>
            <div class="stack" id="frontierSignalList"></div>
          </section>
        </section>

        <section class="column column-scroll right-column" id="frontierRightColumn" data-scroll-key="frontier-right">
          <section class="panel detail-panel">
            <div class="section-header">
              <div>
                <h2>Frontier Detail</h2>
                <div class="panel-subtitle">把前沿命中、影响链、候选标的和原始证据放在一起看。</div>
              </div>
              <div class="tiny" id="frontierDetailCountLabel"></div>
            </div>
            <div id="frontierDetailView"></div>
          </section>
        </section>
      </section>
    </section>
  </div>

  <script id="report-data" type="application/json">$payload_json</script>
  <script>
    (function ensureFreshUrl() {
      const url = new URL(window.location.href);
      if (!url.searchParams.get("ts")) {
        url.searchParams.set("ts", String(Date.now()));
        window.location.replace(url.toString());
      }
    })();

    const report = JSON.parse(document.getElementById("report-data").textContent);
    const aiJudgement = report.ai_judgement || { summary: {}, events: [], instruments: [] };
    const aiJudgementEvents = Array.isArray(aiJudgement.events) ? aiJudgement.events : [];
    const aiJudgementInstruments = Array.isArray(aiJudgement.instruments) ? aiJudgement.instruments : [];
    const unionEvents = [];
    const seenEventIds = new Set();
    ["top_events", "negative_risks", "positive_catalysts", "watchlist"].forEach(function (key) {
      (report[key] || []).forEach(function (event) {
        if (!seenEventIds.has(event.cluster_id)) {
          seenEventIds.add(event.cluster_id);
          unionEvents.push(event);
        }
      });
    });
    aiJudgementEvents.forEach(function (event) {
      if (!seenEventIds.has(event.cluster_id)) {
        seenEventIds.add(event.cluster_id);
        unionEvents.push(event);
      }
    });

    const eventByCluster = new Map(unionEvents.map(function (event) {
      return [event.cluster_id, event];
    }));

    const techBlock = report.tech_block || { summary: {}, signals: [], themes: [], asset_ladder: [] };
    const lexiconDiscovery = report.lexicon_discovery || { summary: {}, candidates: [], accepted_terms: [] };
    const techSignals = Array.isArray(techBlock.signals) ? techBlock.signals : [];
    const reviewApiBase = "http://127.0.0.1:8765";
    const dashboardStateKey = "marketNewsDashboardState.v4";
    const interactionGraceMs = 3 * 60 * 1000;
    const lexiconTypeOptions = [
      { value: "theme", label: "主题" },
      { value: "tech", label: "技术词" },
      { value: "catalyst", label: "催化词" },
      { value: "policy", label: "政策词" },
      { value: "risk", label: "风险词" },
      { value: "company", label: "公司词" }
    ];
    let reviewApiOnline = false;
    let lexiconReviewMessage = "";
    const lexiconAiReviewByTerm = new Map();
    let pendingReviewRequest = false;
    let lastInteractionAt = Date.now();
    let restoreScrollPending = true;
    let persistStateTimer = null;

    const state = {
      view: "core",
      direction: "all",
      query: "",
      lexiconCatalogQuery: "",
      selectedFrontierId: null,
      selectedFrontierClusterId: null,
      selectedClusterId: (report.alerts && report.alerts[0] && report.alerts[0].cluster_id)
        || (unionEvents[0] && unionEvents[0].cluster_id)
        || null,
      selectedTechClusterId: (techSignals[0] && techSignals[0].cluster_id) || null
    };

    const directionLabels = {
      all: "全部",
      negative: "利空",
      positive: "利好",
      neutral: "中性"
    };

    const runtimeStatusLabels = {
      ok: "正常",
      idle: "空闲",
      degraded: "降级",
      stale: "过期",
      error: "错误",
      missing: "未启动",
      unknown: "未知"
    };

    const runtimeLineLabels = {
      collect: "COLLECT",
      delivery: "DELIVERY",
      review_api: "REVIEW API",
      health: "HEALTH",
      cookies: "COOKIES",
      futu_enrichment: "富途增强",
      ah_scanner: "AH 扫描器"
    };

    const viewLabels = {
      core: "全市场消息",
      ai: "AI判断",
      tech: "港A股消息",
      frontier: "科技前沿"
    };

    function escapeHtml(value) {
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function score(value) {
      const number = Number(value || 0);
      return String(Math.round(number * 100) / 100);
    }

    function schedulePersistState() {
      if (persistStateTimer) {
        clearTimeout(persistStateTimer);
      }
      persistStateTimer = setTimeout(persistDashboardState, 120);
    }

    function collectScrollPositions() {
      const positions = {};
      document.querySelectorAll("[data-scroll-key]").forEach(function (node) {
        const key = node.getAttribute("data-scroll-key");
        if (!key) {
          return;
        }
        positions[key] = node.scrollTop || 0;
      });
      return positions;
    }

    function persistDashboardState() {
      try {
        const payload = {
          view: state.view,
          direction: state.direction,
          query: state.query,
          lexiconCatalogQuery: state.lexiconCatalogQuery,
          selectedFrontierId: state.selectedFrontierId,
          selectedFrontierClusterId: state.selectedFrontierClusterId,
          selectedClusterId: state.selectedClusterId,
          selectedTechClusterId: state.selectedTechClusterId,
          lastInteractionAt: lastInteractionAt,
          scrollPositions: collectScrollPositions()
        };
        window.localStorage.setItem(dashboardStateKey, JSON.stringify(payload));
      } catch (error) {
        return;
      }
    }

    function restoreDashboardState() {
      try {
        const raw = window.localStorage.getItem(dashboardStateKey);
        if (!raw) {
          return;
        }
        const payload = JSON.parse(raw);
        if (payload && typeof payload === "object") {
          if (payload.view === "core" || payload.view === "ai" || payload.view === "tech" || payload.view === "frontier") {
            state.view = payload.view;
          }
          if (payload.direction === "all" || payload.direction === "negative" || payload.direction === "positive" || payload.direction === "neutral") {
            state.direction = payload.direction;
          }
          if (typeof payload.query === "string") {
            state.query = payload.query;
          }
          if (typeof payload.lexiconCatalogQuery === "string") {
            state.lexiconCatalogQuery = payload.lexiconCatalogQuery;
          }
          if (typeof payload.selectedFrontierId === "string") {
            state.selectedFrontierId = payload.selectedFrontierId;
          }
          if (typeof payload.selectedFrontierClusterId === "string") {
            state.selectedFrontierClusterId = payload.selectedFrontierClusterId;
          }
          if (typeof payload.selectedClusterId === "string") {
            state.selectedClusterId = payload.selectedClusterId;
          }
          if (typeof payload.selectedTechClusterId === "string") {
            state.selectedTechClusterId = payload.selectedTechClusterId;
          }
          if (typeof payload.lastInteractionAt === "number") {
            lastInteractionAt = payload.lastInteractionAt;
          }
        }
      } catch (error) {
        return;
      }
    }

    function restoreScrollPositions() {
      try {
        const raw = window.localStorage.getItem(dashboardStateKey);
        if (!raw) {
          return;
        }
        const payload = JSON.parse(raw);
        const positions = payload && typeof payload === "object" ? payload.scrollPositions : null;
        if (!positions || typeof positions !== "object") {
          return;
        }
        document.querySelectorAll("[data-scroll-key]").forEach(function (node) {
          const key = node.getAttribute("data-scroll-key");
          if (!key || typeof positions[key] !== "number") {
            return;
          }
          node.scrollTop = positions[key];
        });
      } catch (error) {
        return;
      }
    }

    function markInteraction() {
      lastInteractionAt = Date.now();
      schedulePersistState();
    }

    function hasRecentInteraction() {
      return pendingReviewRequest || (Date.now() - lastInteractionAt < interactionGraceMs);
    }

    function reviewStatusText() {
      if (reviewApiOnline) {
        return lexiconReviewMessage || "词库候选词会由 AI 自动收录或删除；你只需要在正式词库里删除误收录词。";
      }
      return "审核服务还没连上，所以这里只显示队列。启动总控后刷新页面即可。";
    }

    async function fetchReviewApi(path, options) {
      const response = await fetch(reviewApiBase + path, Object.assign({
        headers: { "Content-Type": "application/json" }
      }, options || {}));
      let payload = {};
      try {
        payload = await response.json();
      } catch (error) {
        payload = {};
      }
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error || ("request failed: " + response.status));
      }
      return payload;
    }

    function applyDiscoveryPayload(payload) {
      lexiconDiscovery.summary = payload.summary || {};
      lexiconDiscovery.candidates = payload.candidates || [];
      lexiconDiscovery.accepted_terms = payload.accepted_terms || [];
      if (Array.isArray(payload.ai_review)) {
        payload.ai_review.forEach(function (item) {
          const term = String(item.term || "").trim().toLowerCase();
          if (term) {
            lexiconAiReviewByTerm.set(term, item);
          }
        });
      }
      lexiconDiscovery.candidates.forEach(function (candidate) {
        const term = String(candidate.text || "").trim().toLowerCase();
        if (term && candidate.ai_review) {
          lexiconAiReviewByTerm.set(term, candidate.ai_review);
        }
      });
      if (payload.message) {
        lexiconReviewMessage = payload.message;
      }
    }

    async function refreshDiscoveryFromApi() {
      try {
        const payload = await fetchReviewApi("/api/lexicon/pending");
        reviewApiOnline = true;
        applyDiscoveryPayload(payload);
      } catch (error) {
        reviewApiOnline = false;
      }
      renderTechBlock();
    }

    async function submitDiscoveryAction(action, term, termType, triggerButton) {
      markInteraction();
      pendingReviewRequest = true;
      const payload = action === "add"
        ? { term: term, term_type: termType }
        : { term: term };
      const buttons = triggerButton && triggerButton.closest(".theme-card")
        ? Array.from(triggerButton.closest(".theme-card").querySelectorAll(".review-action"))
        : [];
      buttons.forEach(function (button) { button.disabled = true; });
      try {
        const result = await fetchReviewApi(
          action === "add" ? "/api/lexicon/add" : "/api/lexicon/reject",
          {
            method: "POST",
            body: JSON.stringify(payload)
          }
        );
        reviewApiOnline = true;
        applyDiscoveryPayload(result);
      } catch (error) {
        reviewApiOnline = false;
        lexiconReviewMessage = "审核动作失败：" + String(error.message || error);
      } finally {
        pendingReviewRequest = false;
        markInteraction();
      }
      renderTechBlock();
    }

    async function submitLexiconRemove(term, triggerButton) {
      markInteraction();
      pendingReviewRequest = true;
      const buttons = triggerButton && triggerButton.closest(".theme-card")
        ? Array.from(triggerButton.closest(".theme-card").querySelectorAll(".review-action"))
        : [];
      buttons.forEach(function (button) { button.disabled = true; });
      try {
        const result = await fetchReviewApi(
          "/api/lexicon/remove",
          {
            method: "POST",
            body: JSON.stringify({ term: term })
          }
        );
        reviewApiOnline = true;
        applyDiscoveryPayload(result);
      } catch (error) {
        reviewApiOnline = false;
        lexiconReviewMessage = "删除词条失败：" + String(error.message || error);
      } finally {
        pendingReviewRequest = false;
        markInteraction();
      }
      renderTechBlock();
    }

    async function requestLexiconAiReview(triggerButton) {
      markInteraction();
      pendingReviewRequest = true;
      if (triggerButton) {
        triggerButton.disabled = true;
      }
      lexiconReviewMessage = "AI正在自动审核待处理词：会直接收录高置信词，删除噪声词。";
      renderTechBlock();
      try {
        const result = await fetchReviewApi(
          "/api/lexicon/ai-review",
          {
            method: "POST",
            body: JSON.stringify({ limit: 24 })
          }
        );
        reviewApiOnline = true;
        applyDiscoveryPayload(result);
      } catch (error) {
        reviewApiOnline = false;
        lexiconReviewMessage = "AI自动审核失败：" + String(error.message || error);
      } finally {
        pendingReviewRequest = false;
        if (triggerButton) {
          triggerButton.disabled = false;
        }
        markInteraction();
      }
      renderTechBlock();
    }

    async function submitManualNewsAnalysis() {
      markInteraction();
      const input = document.getElementById("aiAskInput");
      const questionInput = document.getElementById("aiQuestionInput");
      const button = document.getElementById("aiAskButton");
      const resultHost = document.getElementById("aiAskResult");
      const text = input ? String(input.value || "").trim() : "";
      const question = questionInput ? String(questionInput.value || "").trim() : "";
      if (!text) {
        resultHost.innerHTML = "先把新闻、公告或网页文字粘贴进来，我再帮你拆影响链。";
        return;
      }
      if (button) {
        button.disabled = true;
      }
      resultHost.innerHTML = "AI正在判断：先看这是不是领先基本面信号，再看可能影响哪些港A美标的。";
      try {
        const payload = await fetchReviewApi(
          "/api/ai/analyze-news",
          {
            method: "POST",
            body: JSON.stringify({ text: text, question: question })
          }
        );
        renderManualAnalysisResult(payload.analysis || {});
      } catch (error) {
        resultHost.innerHTML = "主动分析失败：" + escapeHtml(String(error.message || error));
      } finally {
        if (button) {
          button.disabled = false;
        }
        markInteraction();
      }
    }

    function renderManualAnalysisResult(analysis) {
      const host = document.getElementById("aiAskResult");
      if (!host) {
        return;
      }
      const assets = Array.isArray(analysis.affected_assets) ? analysis.affected_assets : [];
      const impactLogic = Array.isArray(analysis.impact_logic) ? analysis.impact_logic : [];
      const watchPoints = Array.isArray(analysis.watch_points) ? analysis.watch_points : [];
      const missingEvidence = Array.isArray(analysis.missing_evidence) ? analysis.missing_evidence : [];
      const attention = score(analysis.attention_score || 0);
      const confidence = score((Number(analysis.confidence || 0) || 0) * 100);
      host.innerHTML =
        '<div class="card-topline">'
        + '<span class="badge ' + (analysis.worth_attention ? 'dir-positive' : 'dir-neutral') + '">'
        + (analysis.worth_attention ? "值得继续看" : "暂不优先") + "</span>"
        + '<span>关注分 ' + escapeHtml(attention) + "</span>"
        + '<span>置信度 ' + escapeHtml(confidence) + "%</span>"
        + "</div>"
        + '<p><strong>一句话：</strong>' + escapeHtml(analysis.summary || analysis.conclusion || "暂无摘要") + "</p>"
        + (impactLogic.length
            ? '<p><strong>影响逻辑：</strong></p><ul>'
              + impactLogic.slice(0, 5).map(function (item) {
                  return "<li>" + escapeHtml(item) + "</li>";
                }).join("")
              + "</ul>"
            : "")
        + (assets.length
            ? '<p><strong>可能相关标的：</strong></p><ul>'
              + assets.slice(0, 8).map(function (item) {
                  return "<li>" + escapeHtml([item.symbol, item.name, item.market, item.direction].filter(Boolean).join(" · "))
                    + (item.reason ? "：" + escapeHtml(item.reason) : "") + "</li>";
                }).join("")
              + "</ul>"
            : '<p><strong>可能相关标的：</strong>证据不足，暂不硬配票。</p>')
        + (watchPoints.length
            ? '<p><strong>后续验证：</strong>' + escapeHtml(watchPoints.slice(0, 4).join("；")) + "</p>"
            : "")
        + (missingEvidence.length
            ? '<p><strong>缺失证据：</strong>' + escapeHtml(missingEvidence.slice(0, 4).join("；")) + "</p>"
            : "")
        + '<p><strong>结论：</strong>' + escapeHtml(analysis.conclusion || "暂无结论") + "</p>";
    }

    function tokensForEvent(event) {
      return [
        event.headline,
        event.summary,
        (event.themes || []).join(" "),
        (event.entities || []).join(" "),
        (event.sectors || []).join(" "),
        (event.regions || []).join(" "),
        (event.top_instruments || []).map(function (item) { return item.symbol + " " + item.name; }).join(" ")
      ].join(" ").toLowerCase();
    }

    function matchesDirection(event) {
      return state.direction === "all" || event.direction === state.direction;
    }

    function matchesQuery(text) {
      if (!state.query) {
        return true;
      }
      return String(text || "").toLowerCase().indexOf(state.query) !== -1;
    }

    const dragNewsPayloadById = new Map();

    function compactText(value, fallback) {
      const text = String(value || fallback || "").replace(/\\s+/g, " ").trim();
      return text || "n/a";
    }

    function listText(values, limit) {
      if (!Array.isArray(values)) {
        return "n/a";
      }
      const items = values.map(function (value) {
        return compactText(value, "");
      }).filter(function (value) {
        return value && value !== "n/a";
      }).slice(0, limit || 6);
      return items.join("，") || "n/a";
    }

    function instrumentLine(item) {
      if (!item) {
        return "";
      }
      return [
        item.symbol,
        item.name,
        item.market,
        item.direction,
        item.final_score !== undefined ? ("score " + score(item.final_score)) : "",
        item.ai_score !== undefined ? ("AI " + score(item.ai_score)) : ""
      ].filter(Boolean).join(" · ");
    }

    function documentLine(doc) {
      if (!doc) {
        return "";
      }
      return [
        compactText(doc.published_at, ""),
        compactText(doc.source_id, ""),
        compactText(doc.title, ""),
        compactText(doc.url, "")
      ].filter(function (value) {
        return value && value !== "n/a";
      }).join(" · ");
    }

    function relatedDocumentText(docs) {
      const items = Array.isArray(docs) ? docs.slice(0, 5).map(documentLine).filter(Boolean) : [];
      return items.length ? items.map(function (line) { return "- " + line; }).join("\\n") : "- n/a";
    }

    function eventForClusterId(clusterId) {
      return eventByCluster.get(clusterId) || null;
    }

    function registerNewsDragPayload(prefix, key, payload) {
      const id = String(prefix || "news") + ":" + String(key || dragNewsPayloadById.size);
      dragNewsPayloadById.set(id, String(payload || "").slice(0, 20000));
      return id;
    }

    function formatDraggedEvent(event, label) {
      const selection = event.ai_selection || {};
      const judgement = event.model_judgement || {};
      const instruments = (event.top_instruments || []).slice(0, 8).map(instrumentLine).filter(Boolean);
      const reasons = []
        .concat(Array.isArray(event.rationale) ? event.rationale : [])
        .concat(selection.summary_reason ? [selection.summary_reason] : [])
        .concat(selection.model_reason ? [selection.model_reason] : [])
        .concat(judgement.screening_reason ? [judgement.screening_reason] : []);
      return [
        "【来自看板拖入：" + compactText(label, "新闻事件") + "】",
        "标题：" + compactText(event.headline, ""),
        "摘要：" + compactText(event.summary, ""),
        "方向/类型/分数：" + [event.direction, event.event_type, score(event.final_score)].filter(Boolean).join(" · "),
        "主题：" + listText(event.themes, 8),
        "实体：" + listText(event.entities, 8),
        "来源：" + listText(event.source_ids, 8),
        "候选标的：" + (instruments.join("；") || "n/a"),
        "系统理由：" + listText(reasons, 8),
        "原文证据：",
        relatedDocumentText(event.related_documents),
        "我想问：这条消息是否值得继续看？它是不是领先的基本面/需求/估值信号？可能影响哪些港A股、美股或A股？"
      ].join("\\n");
    }

    function formatDraggedAlert(alert) {
      const event = eventForClusterId(alert.cluster_id);
      if (event) {
        return formatDraggedEvent(event, "提醒卡片");
      }
      return [
        "【来自看板拖入：提醒卡片】",
        "标题：" + compactText(alert.headline, ""),
        "方向/级别/分数：" + [alert.direction, alert.level, score(alert.final_score)].filter(Boolean).join(" · "),
        "关联标的：" + listText(alert.symbols, 8),
        "提醒理由：" + compactText(alert.reason, ""),
        "我想问：这条提醒是否值得继续跟踪？影响哪些股票？"
      ].join("\\n");
    }

    function formatDraggedInstrument(item, label) {
      const event = eventForClusterId(item.cluster_id);
      const base = event
        ? formatDraggedEvent(event, label || "候选标的卡片")
        : [
            "【来自看板拖入：" + compactText(label, "候选标的卡片") + "】",
            "标题：" + compactText(item.headline, ""),
            "方向/分数：" + [item.direction, score(item.final_score)].filter(Boolean).join(" · ")
          ].join("\\n");
      return base + "\\n\\n【当前拖入的关联标的】\\n" + instrumentLine(item);
    }

    function formatDraggedFeed(item) {
      return [
        "【来自看板拖入：原始消息流】",
        "标题：" + compactText(item.title, ""),
        "摘要：" + compactText(item.summary, ""),
        "来源/时间：" + [item.source_id, item.published_at].filter(Boolean).join(" · "),
        "主题：" + listText(item.themes, 8),
        "实体：" + listText(item.entities, 8),
        "原文：" + compactText(item.url, ""),
        "我想问：这条原始消息能不能形成清晰投资逻辑？是否值得进入重点看板？"
      ].join("\\n");
    }

    function formatDraggedTechSignal(signal, label) {
      const linkedEvent = eventForClusterId(signal.cluster_id);
      const docs = Array.isArray(signal.related_documents) && signal.related_documents.length
        ? signal.related_documents
        : (linkedEvent && linkedEvent.related_documents ? linkedEvent.related_documents : []);
      const assets = (signal.candidate_assets || []).slice(0, 8).map(instrumentLine).filter(Boolean);
      const matchedTerms = (signal.matched_terms || []).slice(0, 8).map(function (item) {
        return [item.term, item.term_type, listText(item.matched_terms, 4)].filter(Boolean).join(" · ");
      });
      return [
        "【来自看板拖入：" + compactText(label, "专题信号") + "】",
        "标题：" + compactText(signal.headline, ""),
        "方向/关注分：" + [signal.direction, score(signal.trading_attention_score)].filter(Boolean).join(" · "),
        "触发标签：" + listText(signal.trigger_tags, 10),
        "命中词库：" + (matchedTerms.join("；") || "n/a"),
        "专题逻辑：" + listText(signal.rationale, 8),
        "候选标的：" + (assets.join("；") || "n/a"),
        "主证据：",
        relatedDocumentText(docs),
        "我想问：这个专题是否真的有基本面或需求变化？候选股票里谁更直接、谁只是蹭概念？"
      ].join("\\n");
    }

    function attachNewsDragHandlers(host) {
      if (!host) {
        return;
      }
      host.querySelectorAll("[data-drag-news-id]").forEach(function (node) {
        if (node.getAttribute("data-drag-ready") === "1") {
          return;
        }
        node.setAttribute("data-drag-ready", "1");
        node.setAttribute("title", "拖到“主动问AI”里分析");
        node.addEventListener("dragstart", function (event) {
          const key = node.getAttribute("data-drag-news-id") || "";
          const payload = dragNewsPayloadById.get(key) || "";
          if (!payload || !event.dataTransfer) {
            return;
          }
          event.dataTransfer.effectAllowed = "copy";
          event.dataTransfer.setData("text/plain", payload);
          event.dataTransfer.setData("application/x-market-news-card", key);
          node.classList.add("dragging");
        });
        node.addEventListener("dragend", function () {
          node.classList.remove("dragging");
        });
      });
    }

    function loadAskDraftFromText(text, label) {
      if (!aiAskInput) {
        return;
      }
      aiAskInput.value = String(text || "").slice(0, 20000);
      aiAskInput.focus();
      const resultHost = document.getElementById("aiAskResult");
      if (resultHost) {
        resultHost.innerHTML = '<strong>' + escapeHtml(label || "已放入这条新闻") + '</strong>。你可以补一句问题，然后点“分析这条新闻”。';
      }
    }

    function renderViewSwitch() {
      const host = document.getElementById("viewSwitch");
      const views = ["core", "ai", "tech", "frontier"];
      host.innerHTML = views.map(function (view) {
        const active = state.view === view ? " active" : "";
        return '<button class="view-button' + active + '" type="button" data-view="' + view + '">'
          + escapeHtml(viewLabels[view] || view)
          + "</button>";
      }).join("");
      host.querySelectorAll("[data-view]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.view = button.getAttribute("data-view") || "core";
          render();
        });
      });
    }

    function renderWorkspaces() {
      document.querySelectorAll(".workspace").forEach(function (node) {
        node.classList.toggle("active", node.getAttribute("data-view") === state.view);
      });
    }

    function filteredEvents() {
      return unionEvents.filter(function (event) {
        return matchesDirection(event) && matchesQuery(tokensForEvent(event));
      });
    }

    function filteredAiJudgementEvents() {
      return aiJudgementEvents.filter(function (event) {
        return matchesDirection(event) && matchesQuery(tokensForEvent(event));
      });
    }

    function filteredAiJudgementInstruments() {
      return aiJudgementInstruments.filter(function (instrument) {
        const event = eventByCluster.get(instrument.cluster_id);
        const text = [
          instrument.symbol,
          instrument.name,
          instrument.headline,
          instrument.ai_tier,
          instrument.ai_reason,
          (instrument.reasons || []).join(" ")
        ].join(" ").toLowerCase();
        return matchesQuery(text) && (!event || matchesDirection(event));
      });
    }

    function filteredInstruments() {
      return (report.top_instruments || []).filter(function (instrument) {
        const text = [
          instrument.symbol,
          instrument.name,
          instrument.headline,
          (instrument.reasons || []).join(" ")
        ].join(" ").toLowerCase();
        const event = eventByCluster.get(instrument.cluster_id);
        return matchesQuery(text) && (!event || matchesDirection(event));
      });
    }

    function filteredFeed() {
      return (report.latest_feed || []).filter(function (item) {
        const text = [
          item.title,
          item.summary,
          item.source_id,
          (item.themes || []).join(" "),
          (item.entities || []).join(" ")
        ].join(" ").toLowerCase();
        return matchesQuery(text);
      });
    }

    function tokensForTechSignal(signal) {
      return [
        signal.headline,
        (signal.trigger_tags || []).join(" "),
        (signal.rationale || []).join(" "),
        (signal.matched_terms || []).map(function (item) {
          return item.term + " " + (item.matched_terms || []).join(" ");
        }).join(" "),
        (signal.candidate_assets || []).map(function (item) {
          return item.symbol + " " + item.name;
        }).join(" "),
        (signal.activated_themes || []).map(function (item) {
          return item.label + " " + (item.drivers || []).join(" ");
        }).join(" ")
      ].join(" ").toLowerCase();
    }

    function filteredTechSignals() {
      return techSignals.filter(function (signal) {
        const directionOk = state.direction === "all" || signal.direction === state.direction;
        return directionOk && matchesQuery(tokensForTechSignal(signal));
      });
    }

    function filteredTechAssets() {
      return (techBlock.asset_ladder || []).filter(function (asset) {
        const text = [
          asset.symbol,
          asset.name,
          (asset.drivers || []).join(" ")
        ].join(" ").toLowerCase();
        const directionOk = state.direction === "all" || asset.direction === state.direction;
        return directionOk && matchesQuery(text);
      });
    }

    function filteredAcceptedTerms() {
      const items = Array.isArray(lexiconDiscovery.accepted_terms) ? lexiconDiscovery.accepted_terms : [];
      const query = state.lexiconCatalogQuery.trim().toLowerCase();
      if (!query) {
        return items;
      }
      return items.filter(function (item) {
        const text = String(item.text || "").toLowerCase();
        const termType = String(item.term_type || "").toLowerCase();
        const synonyms = Array.isArray(item.synonyms) ? item.synonyms.join(" ").toLowerCase() : "";
        const tags = Array.isArray(item.trigger_tags) ? item.trigger_tags.join(" ").toLowerCase() : "";
        return text.indexOf(query) >= 0
          || termType.indexOf(query) >= 0
          || synonyms.indexOf(query) >= 0
          || tags.indexOf(query) >= 0;
      });
    }

    function frontierTrackerItems(baseSignals) {
      const tracker = new Map();
      (Array.isArray(baseSignals) ? baseSignals : techSignals).forEach(function (signal) {
        (signal.frontier_hits || []).forEach(function (hit) {
          const frontierId = String(hit.frontier_id || "").trim();
          if (!frontierId) {
            return;
          }
          const current = tracker.get(frontierId) || {
            frontier_id: frontierId,
            cn_label: hit.cn_label || frontierId,
            gap_level: hit.gap_level || "unknown",
            score: 0,
            hit_count: 0,
            bonuses: [],
            matched_keywords: [],
            cluster_ids: [],
            headlines: [],
          };
          current.score += Number(signal.trading_attention_score || 0) * Math.max(Number(hit.bonus || 0), 0.1);
          current.hit_count += 1;
          current.bonuses.push(Number(hit.bonus || 0));
          current.matched_keywords = Array.from(new Set(current.matched_keywords.concat(hit.matched_keywords || []))).slice(0, 6);
          current.cluster_ids = Array.from(new Set(current.cluster_ids.concat([signal.cluster_id]))).slice(0, 6);
          current.headlines = Array.from(new Set(current.headlines.concat([signal.headline]))).slice(0, 3);
          tracker.set(frontierId, current);
        });
      });
      return Array.from(tracker.values())
        .map(function (item) {
          item.score = Math.round(item.score * 100) / 100;
          item.top_bonus = Math.max.apply(null, item.bonuses.length ? item.bonuses : [0]);
          return item;
        })
        .sort(function (left, right) {
          return right.score - left.score;
        });
    }

    function frontierSignalsBase() {
      return techSignals.filter(function (signal) {
        if (!Array.isArray(signal.frontier_hits) || !signal.frontier_hits.length) {
          return false;
        }
        const directionOk = state.direction === "all" || signal.direction === state.direction;
        const frontierText = (signal.frontier_hits || []).map(function (item) {
          return [
            item.frontier_id,
            item.cn_label,
            item.gap_level,
            (item.matched_keywords || []).join(" ")
          ].join(" ");
        }).join(" ");
        return directionOk && matchesQuery(tokensForTechSignal(signal) + " " + frontierText);
      });
    }

    function filteredFrontierSignals() {
      const signals = frontierSignalsBase();
      if (!state.selectedFrontierId) {
        return signals;
      }
      const narrowed = signals.filter(function (signal) {
        return (signal.frontier_hits || []).some(function (hit) {
          return hit.frontier_id === state.selectedFrontierId;
        });
      });
      return narrowed.length ? narrowed : signals;
    }

    function selectedFrontierSignal() {
      const signals = filteredFrontierSignals();
      const current = signals.find(function (signal) {
        return signal.cluster_id === state.selectedFrontierClusterId;
      });
      if (current) {
        return current;
      }
      const first = signals[0] || frontierSignalsBase()[0] || null;
      if (first) {
        state.selectedFrontierClusterId = first.cluster_id;
      }
      return first;
    }

    function renderTechSignalDetail(detailHost, currentSignal, summaryLabel, countLabelId) {
      if (!currentSignal) {
        detailHost.innerHTML = '<div class="empty">当前没有科技专题详情可看。</div>';
        if (countLabelId) {
          document.getElementById(countLabelId).textContent = "暂无信号";
        }
        return;
      }

      if (countLabelId) {
        document.getElementById(countLabelId).textContent = "关注分 " + score(currentSignal.trading_attention_score);
      }

      const linkedEvent = eventByCluster.get(currentSignal.cluster_id);
      const matchedTerms = (currentSignal.matched_terms || []).map(function (item) {
        const matched = Array.isArray(item.matched_terms) ? item.matched_terms.slice(0, 4).join(", ") : "n/a";
        return '<li>' + escapeHtml(item.term || "term")
          + ' · ' + escapeHtml(item.term_type || "unknown")
          + ' · ' + escapeHtml(matched)
          + "</li>";
      }).join("") || "<li>暂无触发词。</li>";
      const frontierCards = (currentSignal.frontier_hits || []).map(function (item) {
        return '<div class="instrument-card">'
          + '<div class="card-topline">'
          + '<span class="badge type-company">frontier</span>'
          + '<span>' + escapeHtml(item.gap_level || "unknown") + "</span>"
          + '<span>bonus ' + escapeHtml(score(item.bonus)) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(item.cn_label || item.frontier_id || "frontier") + "</div>"
          + '<p class="summary">' + escapeHtml((item.matched_keywords || []).join("，") || "暂无命中词") + "</p>"
          + "</div>";
      }).join("") || '<div class="empty">当前没有命中的前沿突破。</div>';
      const themeCards = (currentSignal.activated_themes || []).map(function (theme) {
        return '<div class="instrument-card">'
          + '<div class="card-topline">'
          + '<span class="badge type-company">theme</span>'
          + '<span>score ' + escapeHtml(score(theme.score)) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(theme.label || theme.theme || "theme") + "</div>"
          + '<p class="summary">' + escapeHtml(theme.path || (theme.drivers || []).join("，") || "暂无链路说明") + "</p>"
          + "</div>";
      }).join("") || '<div class="empty">当前没有主题扩散链。</div>';
      const candidateCards = (currentSignal.candidate_assets || []).map(function (asset) {
        return '<div class="instrument-card">'
          + '<div class="card-topline">'
          + '<span class="badge dir-' + escapeHtml(asset.direction) + '">' + escapeHtml(asset.direction) + '</span>'
          + '<span>' + escapeHtml(asset.market) + "</span>"
          + '<span>score ' + escapeHtml(score(asset.score)) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(asset.symbol + " · " + asset.name) + "</div>"
          + '<p class="instrument-note">' + escapeHtml((asset.reasons || []).join("；") || "暂无候选说明") + "</p>"
          + "</div>";
      }).join("") || '<div class="empty">当前没有候选标的。</div>';
      // Prefer docs embedded directly in the signal by reporting.py enrichment;
      // fall back to linked event docs for backward compatibility.
      const signalDocs = Array.isArray(currentSignal.related_documents) ? currentSignal.related_documents : [];
      const fallbackDocs = (linkedEvent && Array.isArray(linkedEvent.related_documents))
        ? linkedEvent.related_documents : [];
      const evidenceSources = Array.isArray(currentSignal.evidence_source_ids) ? currentSignal.evidence_source_ids : [];
      const allCandidateDocs = signalDocs.length ? signalDocs : fallbackDocs;
      const linkedDocs = allCandidateDocs
        .filter(function (doc) {
          return !evidenceSources.length || evidenceSources.indexOf(doc.source_id) >= 0;
        })
        .map(function (doc) {
          const link = doc.url
            ? '<a href="' + escapeHtml(doc.url) + '" target="_blank" rel="noreferrer">打开原文</a>'
            : '<span class="tiny">暂无原文链接</span>';
          return '<li class="doc-card">'
            + '<div class="card-meta">' + escapeHtml(doc.published_at) + " · " + escapeHtml(doc.source_id) + "</div>"
            + '<div class="headline">' + escapeHtml(doc.title) + "</div>"
            + '<p>' + escapeHtml(doc.summary || "无摘要") + "</p>"
            + '<div class="tag-row">' + link + "</div>"
            + "</li>";
        })
        .join("");
      const rationale = (currentSignal.rationale || []).map(function (item) {
        return "<li>" + escapeHtml(item) + "</li>";
      }).join("") || "<li>暂无科技专题逻辑。</li>";

      detailHost.innerHTML = `
        <div class="tech-detail-grid">
          <div class="detail-block">
            <div class="detail-section-title"><strong>专题摘要</strong><span>$${escapeHtml(summaryLabel || "港A科技催化")}</span></div>
            <div class="card-topline">
              <span class="badge dir-$${escapeHtml(currentSignal.direction)}">$${escapeHtml(currentSignal.direction)}</span>
              <span class="badge level-medium">$${escapeHtml(String(currentSignal.attention_tier || "watch").toUpperCase())}</span>
              <span>docs $${escapeHtml(currentSignal.doc_count)}</span>
              <span>$${escapeHtml(currentSignal.source_quality || "n/a")}</span>
            </div>
            <h3 class="headline-serif">$${escapeHtml(currentSignal.headline)}</h3>
            <p class="summary">$${escapeHtml((currentSignal.trigger_tags || []).join("，") || "暂无触发词标签")}</p>
            <div class="chip-row">
              <span class="mini-tag">主证据：$${escapeHtml((currentSignal.evidence_source_ids || []).join(", ") || "n/a")}</span>
              <span class="mini-tag">热度：$${escapeHtml((currentSignal.social_source_ids || []).join(", ") || "无")}</span>
            </div>
            <div class="score-strip">
              <div class="score-box"><div class="label">Attention</div><div class="value">$${escapeHtml(score(currentSignal.trading_attention_score))}</div></div>
              <div class="score-box"><div class="label">Spec</div><div class="value">$${escapeHtml(score(currentSignal.spec_score))}</div></div>
              <div class="score-box"><div class="label">Heat</div><div class="value">$${escapeHtml(score(currentSignal.heat_score))}</div></div>
            </div>
          </div>
          <div class="detail-block"><div class="detail-section-title"><strong>触发词</strong><span>命中的炒作因子</span></div><ul class="rationale-list">$${matchedTerms}</ul></div>
          <div class="detail-block"><div class="detail-section-title"><strong>前沿命中</strong><span>美强中追追踪</span></div><div class="stack">$${frontierCards}</div></div>
          <div class="detail-block"><div class="detail-section-title"><strong>影响链</strong><span>主题扩散路径</span></div><div class="stack">$${themeCards}</div></div>
          <div class="detail-block"><div class="detail-section-title"><strong>候选标的</strong><span>港A科技映射</span></div><div class="stack">$${candidateCards}</div></div>
          <div class="detail-block"><div class="detail-section-title"><strong>专题逻辑</strong><span>为什么值得看</span></div><ul class="rationale-list">$${rationale}</ul></div>
          <div class="detail-block"><div class="detail-section-title"><strong>主证据原文</strong><span>只展示干净来源；微博/雪球只做热度加权</span></div><ul class="doc-list">$${linkedDocs || '<div class="empty">当前没有联动原文。</div>'}</ul></div>
        </div>`;
      detailHost.scrollTop = 0;
      const column = detailHost.closest(".right-column");
      if (column) {
        column.scrollTop = 0;
      }
    }

    function selectedTechSignal() {
      const current = techSignals.find(function (signal) {
        return signal.cluster_id === state.selectedTechClusterId;
      });
      if (current) {
        return current;
      }
      const first = filteredTechSignals()[0] || techSignals[0] || null;
      if (first) {
        state.selectedTechClusterId = first.cluster_id;
      }
      return first;
    }

    function selectedEvent() {
      const current = eventByCluster.get(state.selectedClusterId);
      if (current) {
        return current;
      }
      const first = filteredEvents()[0] || unionEvents[0] || null;
      if (first) {
        state.selectedClusterId = first.cluster_id;
      }
      return first;
    }

    function rightColumn() {
      return document.querySelector(".right-column");
    }

    function detailView() {
      return document.getElementById("detailView");
    }

    function renderHero() {
      const counts = report.counts || {};
      const runtime = report.runtime_status || {};
      const runtimeLabel = runtimeStatusLabels[runtime.overall_status] || runtime.overall_status || "未知";
      document.getElementById("heroMeta").textContent =
        "更新于 " + report.created_at + "，来源 " + report.source + "。全局新闻映射到 A 股、港股、美股候选标的，当前运行状态 " + runtimeLabel + "。";

      const metrics = [
        { label: "事件", value: counts.ranked_events || 0, meta: "当前排序后的事件数" },
        { label: "候选标的", value: counts.ranked_instruments || 0, meta: "港美 A 股相关标的" },
        { label: "高优先级提醒", value: (report.alert_counts || {}).high || 0, meta: "当前 high 级提醒" },
        { label: "紧急提醒", value: (report.alert_counts || {}).critical || 0, meta: "当前 critical 级提醒" }
      ];
      document.getElementById("metricGrid").innerHTML = metrics.map(function (metric) {
        return '<div class="metric-card">'
          + '<div class="label">' + escapeHtml(metric.label) + '</div>'
          + '<div class="value">' + escapeHtml(metric.value) + '</div>'
          + '<div class="meta">' + escapeHtml(metric.meta) + '</div>'
          + '</div>';
      }).join("");
    }

    function renderRuntimeStatus() {
      const runtime = report.runtime_status || { overall_status: "unknown", lines: [] };
      const lines = runtime.lines || [];
      document.getElementById("runtimeOverallLabel").textContent =
        "总览 " + (runtimeStatusLabels[runtime.overall_status] || runtime.overall_status || "未知");

      const host = document.getElementById("runtimeStatusGrid");
      host.innerHTML = lines.map(function (line) {
        const status = String(line.status || "unknown");
        const displayStatus = line.name === "cookies" && status === "missing"
          ? "未配置"
          : (runtimeStatusLabels[status] || status);
        const ageText = line.age_seconds === null || line.age_seconds === undefined
          ? "age n/a"
          : "age " + line.age_seconds + "s";
        const updateText = line.last_update ? String(line.last_update) : "n/a";
        const modules = Array.isArray(line.modules) ? line.modules : [];
        return '<div class="status-card ' + escapeHtml(status) + '">'
          + '<div class="status-topline">'
          + '<div class="status-name">' + escapeHtml(runtimeLineLabels[line.name] || line.name || "line") + "</div>"
          + '<div class="status-state">' + escapeHtml(displayStatus) + "</div>"
          + "</div>"
          + '<p class="status-note">' + escapeHtml(line.detail || "暂无说明") + "</p>"
          + '<div class="status-meta">'
          + '<span>last update: ' + escapeHtml(updateText) + "</span>"
          + '<span>' + escapeHtml(ageText) + "</span>"
          + '<span>source: ' + escapeHtml(line.source_status || "n/a") + "</span>"
          + "</div>"
          + (modules.length
              ? '<div class="status-module-row">'
                + modules.map(function (module) {
                    const moduleStatus = String(module.status || "unknown");
                    const counter = module.count || module.signal_count || module.event_count || module.alert_count || "";
                    const extra = module.reason ? String(module.reason) : (counter !== "" ? String(counter) : "");
                    return '<span class="status-module ' + escapeHtml(moduleStatus) + '">'
                      + escapeHtml(module.name || "module")
                      + (extra !== "" ? ' · ' + escapeHtml(extra) : "")
                      + "</span>";
                  }).join("")
                + "</div>"
              : "")
          + "</div>";
      }).join("");

      if (!lines.length) {
        host.innerHTML = '<div class="empty">还没有可展示的运行状态。</div>';
      }
    }

    function renderTechBlock() {
      const tech = techBlock;
      const summary = tech.summary || {};
      const signals = filteredTechSignals();
      const themes = Array.isArray(tech.themes) ? tech.themes : [];
      const assets = filteredTechAssets();
      const discoveryCandidates = Array.isArray(lexiconDiscovery.candidates) ? lexiconDiscovery.candidates : [];
      const acceptedTerms = filteredAcceptedTerms();
      const frontierItems = frontierTrackerItems(signals);
      const currentSignal = selectedTechSignal();

      document.getElementById("techSummaryLabel").textContent =
        "信号 " + String(summary.signal_count || 0) + " · 主题 " + String(summary.hot_theme_count || 0)
        + " · 词库 " + String(summary.lexicon_version || "unversioned");
      document.getElementById("techSignalCountLabel").textContent =
        currentSignal ? "关注分 " + score(currentSignal.trading_attention_score) : "暂无信号";
      document.getElementById("techThemeCountLabel").textContent = themes.length + " 个";
      document.getElementById("techFrontierCountLabel").textContent = frontierItems.length + " 个";
      document.getElementById("techAssetCountLabel").textContent = assets.length + " 个";
      document.getElementById("lexiconDiscoveryCountLabel").textContent = discoveryCandidates.length + " 个";
      document.getElementById("lexiconCatalogCountLabel").textContent =
        acceptedTerms.length + " / " + String((lexiconDiscovery.summary || {}).accepted_count || 0) + " 个";

      const signalHost = document.getElementById("techSignalList");
      const themeHost = document.getElementById("techThemeList");
      const frontierHost = document.getElementById("techFrontierList");
      const assetHost = document.getElementById("techAssetList");
      const discoveryHost = document.getElementById("lexiconDiscoveryList");
      const catalogHost = document.getElementById("lexiconCatalogList");
      const reviewNoteHost = document.getElementById("lexiconReviewNote");
      const detailHost = document.getElementById("techDetailView");

      reviewNoteHost.textContent = reviewStatusText();

      if (!signals.length) {
        signalHost.innerHTML = '<div class="empty">当前没有科技专题信号。</div>';
      } else {
        signalHost.innerHTML = signals.map(function (signal) {
          const active = signal.cluster_id === state.selectedTechClusterId ? " active" : "";
          const assetsText = (signal.candidate_assets || []).slice(0, 3).map(function (item) {
            return item.symbol;
          }).join(", ") || "n/a";
          const dragId = registerNewsDragPayload("tech-signal", signal.cluster_id, formatDraggedTechSignal(signal, "港A股消息专题"));
          return '<div class="tech-card' + active + '">'
            + '<button class="drag-news-card" type="button" draggable="true" data-drag-news-id="' + escapeHtml(dragId) + '" data-tech-cluster="' + escapeHtml(signal.cluster_id) + '">'
            + '<div class="card-topline">'
            + '<span class="badge dir-' + escapeHtml(signal.direction) + '">' + escapeHtml(signal.direction) + '</span>'
            + '<span class="badge level-medium">' + escapeHtml(String(signal.attention_tier || "watch").toUpperCase()) + '</span>'
            + '<span>attention ' + escapeHtml(score(signal.trading_attention_score)) + "</span>"
            + "</div>"
            + '<div class="headline">' + escapeHtml(signal.headline) + "</div>"
            + '<p class="summary">' + escapeHtml((signal.rationale || []).slice(0, 2).join("；") || "暂无专题解释") + "</p>"
            + '<div class="chip-row">'
            + (signal.trigger_tags || []).slice(0, 4).map(function (tag) {
                return '<span class="mini-tag">' + escapeHtml(tag) + "</span>";
              }).join("")
            + "</div>"
            + '<div class="tech-score-row">'
            + '<span class="mini-score">炒作度 ' + escapeHtml(score(signal.spec_score)) + "</span>"
            + '<span class="mini-score">热度 ' + escapeHtml(score(signal.heat_score)) + "</span>"
            + '<span class="mini-score">证据 ' + escapeHtml((signal.evidence_source_ids || []).join(", ") || "n/a") + "</span>"
            + '<span class="mini-score">标的 ' + escapeHtml(assetsText) + "</span>"
            + "</div>"
            + "</button></div>";
        }).join("");
        signalHost.querySelectorAll("[data-tech-cluster]").forEach(function (button) {
          button.addEventListener("click", function () {
            state.selectedClusterId = button.getAttribute("data-tech-cluster");
            state.selectedTechClusterId = state.selectedClusterId;
            renderTechBlock();
            renderDetail();
          });
        });
        attachNewsDragHandlers(signalHost);
      }

      if (!themes.length) {
        themeHost.innerHTML = '<div class="empty">当前没有科技热主题。</div>';
      } else {
        themeHost.innerHTML = themes.map(function (theme) {
          return '<div class="theme-card">'
            + '<div class="card-topline"><span class="badge type-company">theme</span><span>score ' + escapeHtml(score(theme.score)) + "</span></div>"
            + '<div class="headline">' + escapeHtml(theme.label) + "</div>"
            + '<p class="summary">' + escapeHtml((theme.drivers || []).join("，") || "暂无驱动词") + "</p>"
            + "</div>";
        }).join("");
      }

      if (!frontierItems.length) {
        frontierHost.innerHTML = '<div class="empty">当前没有命中的前沿突破信号。</div>';
      } else {
        frontierHost.innerHTML = frontierItems.map(function (item) {
          const linkedClusterId = item.cluster_ids[0] || "";
          const keywords = item.matched_keywords.join("，") || "暂无命中词";
          const leadingHeadline = item.headlines[0] || "暂无联动信号";
          return '<div class="theme-card">'
            + '<button type="button" data-tech-frontier-cluster="' + escapeHtml(linkedClusterId) + '">'
            + '<div class="card-topline">'
            + '<span class="badge type-company">frontier</span>'
            + '<span>' + escapeHtml(item.gap_level || "unknown") + "</span>"
            + '<span>score ' + escapeHtml(score(item.score)) + "</span>"
            + "</div>"
            + '<div class="headline">' + escapeHtml(item.cn_label || item.frontier_id) + "</div>"
            + '<p class="summary">' + escapeHtml(keywords) + "</p>"
            + '<p class="instrument-note">' + escapeHtml(leadingHeadline) + "</p>"
            + "</button></div>";
        }).join("");
        frontierHost.querySelectorAll("[data-tech-frontier-cluster]").forEach(function (button) {
          button.addEventListener("click", function () {
            const clusterId = button.getAttribute("data-tech-frontier-cluster");
            if (!clusterId) {
              return;
            }
            state.selectedClusterId = clusterId;
            state.selectedTechClusterId = clusterId;
            renderTechBlock();
            renderDetail();
          });
        });
      }

      if (!assets.length) {
        assetHost.innerHTML = '<div class="empty">当前没有专题候选标的。</div>';
      } else {
        assetHost.innerHTML = assets.map(function (asset) {
          return '<div class="asset-card">'
            + '<button type="button" data-tech-symbol="' + escapeHtml(asset.symbol) + '">'
            + '<div class="card-topline">'
            + '<span class="badge dir-' + escapeHtml(asset.direction) + '">' + escapeHtml(asset.direction) + '</span>'
            + '<span>' + escapeHtml(asset.market) + "</span>"
            + '<span>score ' + escapeHtml(score(asset.score)) + "</span>"
            + "</div>"
            + '<div class="headline">' + escapeHtml(asset.symbol + " · " + asset.name) + "</div>"
            + '<p class="summary">' + escapeHtml((asset.drivers || []).slice(0, 2).join("；") || "暂无专题解释") + "</p>"
            + "</button></div>";
        }).join("");
        assetHost.querySelectorAll("[data-tech-symbol]").forEach(function (button) {
          button.addEventListener("click", function () {
            const symbol = button.getAttribute("data-tech-symbol");
            const candidate = techSignals.find(function (signal) {
              return (signal.candidate_assets || []).some(function (item) {
                return item.symbol === symbol;
              });
            });
            if (candidate) {
              state.selectedClusterId = candidate.cluster_id;
              state.selectedTechClusterId = candidate.cluster_id;
              renderTechBlock();
              renderDetail();
            }
          });
        });
      }

      if (!discoveryCandidates.length) {
        discoveryHost.innerHTML = '<div class="empty">当前没有待审核新词。</div>';
      } else {
        discoveryHost.innerHTML = discoveryCandidates.map(function (candidate) {
          const impacts = Object.entries(candidate.inferred_impact || {}).slice(0, 4).map(function (entry) {
            return entry[0] + ":" + score(entry[1]);
          }).join(", ") || "n/a";
          const snippets = Array.isArray(candidate.example_snippets) ? candidate.example_snippets.slice(0, 2) : [];
          const termKey = String(candidate.text || "").trim().toLowerCase();
          const aiReview = candidate.ai_review || lexiconAiReviewByTerm.get(termKey);
          const aiReviewHtml = aiReview
            ? '<div class="ai-review-box">'
              + '<div><span class="ai-review-action">AI判断：' + escapeHtml(aiReview.action || "reject")
              + '</span> · ' + escapeHtml(aiReview.term_type || "theme")
              + ' · confidence ' + escapeHtml(score(aiReview.confidence || 0)) + "</div>"
              + '<div>' + escapeHtml(aiReview.reason || "暂无理由") + "</div>"
              + "</div>"
            : "";
          return '<div class="theme-card">'
            + '<div class="card-topline">'
            + '<span class="badge type-company">pending</span>'
            + '<span>freq ' + escapeHtml(candidate.raw_freq || 0) + '</span>'
            + '<span>score ' + escapeHtml(score(candidate.discovery_score)) + "</span>"
            + "</div>"
            + '<div class="headline">' + escapeHtml(candidate.text || "term") + "</div>"
            + '<p class="summary">' + escapeHtml(impacts) + "</p>"
            + (snippets.length
                ? '<div class="stack">'
                  + snippets.map(function (snippet) {
                      return '<p class="instrument-note">' + escapeHtml(snippet) + "</p>";
                    }).join("")
                  + "</div>"
                : "")
            + aiReviewHtml
            + '<p class="instrument-note">等待 AI 自动审核；确定有用会进入正式词库，不确定或噪声会自动删除。</p>'
            + "</div>";
        }).join("");
      }

      if (!acceptedTerms.length) {
        catalogHost.innerHTML = '<div class="empty">当前没有可展示的正式词库词条。</div>';
      } else {
        catalogHost.innerHTML = acceptedTerms.map(function (item) {
          const synonyms = Array.isArray(item.synonyms) ? item.synonyms.slice(0, 5).join("，") : "";
          const tags = Array.isArray(item.trigger_tags) ? item.trigger_tags.slice(0, 4) : [];
          return '<div class="theme-card">'
            + '<div class="card-topline">'
            + '<span class="badge type-company">' + escapeHtml(item.term_type || "theme") + '</span>'
            + '<span>正式词库</span>'
            + '<span>同义词 ' + escapeHtml(item.synonym_count || 0) + '</span>'
            + "</div>"
            + '<div class="headline">' + escapeHtml(item.text || "term") + "</div>"
            + '<p class="summary">' + escapeHtml(synonyms || "暂无同义词") + "</p>"
            + (tags.length
                ? '<div class="chip-row">' + tags.map(function (tag) {
                    return '<span class="mini-tag">' + escapeHtml(tag) + "</span>";
                  }).join("") + "</div>"
                : "")
            + '<div class="review-row">'
            + '<button type="button" class="review-action reject" data-lexicon-remove="' + escapeHtml(item.text || "") + '">删除</button>'
            + "</div>"
            + "</div>";
        }).join("");
        catalogHost.querySelectorAll("[data-lexicon-remove]").forEach(function (button) {
          button.addEventListener("click", function () {
            submitLexiconRemove(button.getAttribute("data-lexicon-remove"), button);
          });
        });
      }

      if (!currentSignal) {
        detailHost.innerHTML = '<div class="empty">当前没有科技专题详情可看。</div>';
        return;
      }
      renderTechSignalDetail(detailHost, currentSignal, "港A科技催化", "techSignalCountLabel");
    }

    function renderFrontierWorkspace() {
      const trackerItems = frontierTrackerItems(frontierSignalsBase());
      const signalHost = document.getElementById("frontierSignalList");
      const trackerHost = document.getElementById("frontierTrackerList");
      const detailHost = document.getElementById("frontierDetailView");

      const hasSelectedFrontier = trackerItems.some(function (item) {
        return item.frontier_id === state.selectedFrontierId;
      });
      if (!trackerItems.length) {
        state.selectedFrontierId = null;
        state.selectedFrontierClusterId = null;
      } else if (!state.selectedFrontierId || !hasSelectedFrontier) {
        state.selectedFrontierId = trackerItems[0].frontier_id;
      }

      const signals = filteredFrontierSignals();
      const currentSignal = selectedFrontierSignal();

      document.getElementById("frontierTrackerCountLabel").textContent = trackerItems.length + " 个";
      document.getElementById("frontierSignalCountLabel").textContent = signals.length + " 条";

      if (!trackerItems.length) {
        trackerHost.innerHTML = '<div class="empty">当前没有命中的前沿突破主题。</div>';
      } else {
        trackerHost.innerHTML = trackerItems.map(function (item) {
          const active = item.frontier_id === state.selectedFrontierId ? " active" : "";
          const keywords = item.matched_keywords.join("，") || "暂无命中词";
          const headline = item.headlines[0] || "暂无联动信号";
          return '<div class="theme-card' + active + '">'
            + '<button type="button" data-frontier-id="' + escapeHtml(item.frontier_id) + '" data-frontier-cluster="' + escapeHtml(item.cluster_ids[0] || "") + '">'
            + '<div class="card-topline">'
            + '<span class="badge type-company">frontier</span>'
            + '<span>' + escapeHtml(item.gap_level || "unknown") + "</span>"
            + '<span>score ' + escapeHtml(score(item.score)) + "</span>"
            + "</div>"
            + '<div class="headline">' + escapeHtml(item.cn_label || item.frontier_id) + "</div>"
            + '<p class="summary">' + escapeHtml(keywords) + "</p>"
            + '<p class="instrument-note">' + escapeHtml(headline) + "</p>"
            + "</button></div>";
        }).join("");
        trackerHost.querySelectorAll("[data-frontier-id]").forEach(function (button) {
          button.addEventListener("click", function () {
            const frontierId = button.getAttribute("data-frontier-id");
            const clusterId = button.getAttribute("data-frontier-cluster");
            state.selectedFrontierId = frontierId || null;
            state.selectedFrontierClusterId = clusterId || null;
            if (clusterId) {
              state.selectedTechClusterId = clusterId;
              state.selectedClusterId = clusterId;
            }
            renderFrontierWorkspace();
          });
        });
      }

      if (!signals.length) {
        signalHost.innerHTML = '<div class="empty">当前没有前沿信号。</div>';
      } else {
        signalHost.innerHTML = signals.map(function (signal) {
          const active = signal.cluster_id === state.selectedFrontierClusterId ? " active" : "";
          const frontierSummary = (signal.frontier_hits || []).map(function (item) {
            return item.cn_label || item.frontier_id;
          }).slice(0, 3).join("，") || "暂无前沿命中";
          const dragId = registerNewsDragPayload("frontier-signal", signal.cluster_id, formatDraggedTechSignal(signal, "科技前沿信号"));
          return '<div class="tech-card' + active + '">'
            + '<button class="drag-news-card" type="button" draggable="true" data-drag-news-id="' + escapeHtml(dragId) + '" data-frontier-signal="' + escapeHtml(signal.cluster_id) + '">'
            + '<div class="card-topline">'
            + '<span class="badge dir-' + escapeHtml(signal.direction) + '">' + escapeHtml(signal.direction) + '</span>'
            + '<span class="badge level-medium">' + escapeHtml(String(signal.attention_tier || "watch").toUpperCase()) + '</span>'
            + '<span>attention ' + escapeHtml(score(signal.trading_attention_score)) + "</span>"
            + "</div>"
            + '<div class="headline">' + escapeHtml(signal.headline) + "</div>"
            + '<p class="summary">' + escapeHtml(frontierSummary) + "</p>"
            + '<div class="tech-score-row">'
            + '<span class="mini-score">前沿 ' + escapeHtml((signal.frontier_hits || []).length) + "</span>"
            + '<span class="mini-score">炒作度 ' + escapeHtml(score(signal.spec_score)) + "</span>"
            + '<span class="mini-score">热度 ' + escapeHtml(score(signal.heat_score)) + "</span>"
            + "</div>"
            + "</button></div>";
        }).join("");
        signalHost.querySelectorAll("[data-frontier-signal]").forEach(function (button) {
          button.addEventListener("click", function () {
            const clusterId = button.getAttribute("data-frontier-signal");
            const signal = signals.find(function (item) {
              return item.cluster_id === clusterId;
            });
            state.selectedFrontierClusterId = clusterId || null;
            if (signal && signal.frontier_hits && signal.frontier_hits.length) {
              state.selectedFrontierId = signal.frontier_hits[0].frontier_id;
            }
            if (clusterId) {
              state.selectedTechClusterId = clusterId;
              state.selectedClusterId = clusterId;
            }
            renderFrontierWorkspace();
          });
        });
        attachNewsDragHandlers(signalHost);
      }

      renderTechSignalDetail(detailHost, currentSignal, "科技前沿突破", "frontierDetailCountLabel");
    }

    function renderFilters() {
      const filterHost = document.getElementById("filterChips");
      const directions = ["all", "negative", "positive", "neutral"];
      filterHost.innerHTML = directions.map(function (key) {
        const active = state.direction === key ? " active" : "";
        return '<button class="chip' + active + '" type="button" data-direction="' + key + '">'
          + escapeHtml(directionLabels[key])
          + "</button>";
      }).join("");
      filterHost.querySelectorAll("[data-direction]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.direction = button.getAttribute("data-direction") || "all";
          render();
        });
      });
    }

    function renderAiJudgement() {
      const events = filteredAiJudgementEvents();
      const instruments = filteredAiJudgementInstruments();
      document.getElementById("aiJudgementCountLabel").textContent =
        events.length + " 条 · " + instruments.length + " 票";

      const host = document.getElementById("aiJudgementList");
      if (!events.length && !instruments.length) {
        host.innerHTML = '<div class="empty">当前还没有通过AI判断的新闻或候选股票。</div>';
        return;
      }

      const eventCards = events.map(function (event) {
        const active = event.cluster_id === state.selectedClusterId ? " active" : "";
        const selection = event.ai_selection || {};
        const topSymbols = (event.top_instruments || []).map(function (item) {
          return item.symbol;
        }).slice(0, 4).join(", ") || "暂无候选";
        const reason = selection.summary_reason
          || selection.model_reason
          || ((selection.reasons || []).join("；"))
          || "通过AI注意力门槛。";
        const selectedBy = selection.selected_by === "gpt" ? "AI" : "门槛";
        const dragId = registerNewsDragPayload("ai-event", event.cluster_id, formatDraggedEvent(event, "AI判断新闻"));
        return '<div class="event-card' + active + '">'
          + '<button class="card-button drag-news-card" type="button" draggable="true" data-drag-news-id="' + escapeHtml(dragId) + '" data-ai-cluster="' + escapeHtml(event.cluster_id) + '">'
          + '<div class="card-topline">'
          + '<span class="badge dir-' + escapeHtml(event.direction) + '">' + escapeHtml(event.direction) + '</span>'
          + '<span class="badge type-company">' + escapeHtml(selectedBy) + '</span>'
          + '<span>AI ' + escapeHtml(score(selection.score || event.final_score)) + "</span>"
          + '<span>标的 ' + escapeHtml(topSymbols) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(event.headline) + "</div>"
          + '<p class="summary">' + escapeHtml(event.summary || "暂无摘要。") + "</p>"
          + '<div class="ai-reason">' + escapeHtml(reason) + "</div>"
          + "</button></div>";
      }).join("");

      const instrumentCards = instruments.map(function (item) {
        const active = item.cluster_id === state.selectedClusterId ? " active" : "";
        const dragId = registerNewsDragPayload("ai-instrument", item.symbol || item.cluster_id, formatDraggedInstrument(item, "AI判断关联股票"));
        return '<div class="instrument-card' + active + '">'
          + '<button class="card-button drag-news-card" type="button" draggable="true" data-drag-news-id="' + escapeHtml(dragId) + '" data-ai-cluster="' + escapeHtml(item.cluster_id) + '">'
          + '<div class="card-topline">'
          + '<span class="badge dir-' + escapeHtml(item.direction) + '">' + escapeHtml(item.direction) + '</span>'
          + '<span>' + escapeHtml(item.market) + "</span>"
          + '<span>AI ' + escapeHtml(score(item.ai_score)) + "</span>"
          + '<span>score ' + escapeHtml(score(item.final_score)) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(item.symbol + " · " + item.name) + "</div>"
          + '<p class="summary">' + escapeHtml(item.headline || "暂无对应新闻标题") + "</p>"
          + "</button></div>";
      }).join("");

      host.innerHTML =
        '<div class="ai-group-title"><span>选中新闻</span><span>' + escapeHtml(events.length) + '</span></div>'
        + (eventCards || '<div class="empty">当前筛选下没有AI选中的新闻。</div>')
        + '<div class="ai-group-title"><span>关联股票</span><span>' + escapeHtml(instruments.length) + '</span></div>'
        + (instrumentCards || '<div class="empty">当前筛选下没有AI关联股票。</div>');

      host.querySelectorAll("[data-ai-cluster]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.selectedClusterId = button.getAttribute("data-ai-cluster");
          render();
        });
      });
      attachNewsDragHandlers(host);
    }

    function renderAlerts() {
      const alerts = (report.alerts || []).filter(function (alert) {
        const text = [alert.headline, alert.reason, (alert.symbols || []).join(" ")].join(" ").toLowerCase();
        return (state.direction === "all" || alert.direction === state.direction) && matchesQuery(text);
      });
      document.getElementById("alertCountLabel").textContent = alerts.length + " 条";

      const host = document.getElementById("alertsList");
      if (!alerts.length) {
        host.innerHTML = '<div class="empty">当前筛选条件下没有提醒。</div>';
        return;
      }

      host.innerHTML = alerts.map(function (alert) {
        const active = alert.cluster_id === state.selectedClusterId ? " active" : "";
        const prefix = alert.is_new ? '<span class="badge level-medium">NEW</span>' : "";
        const dragId = registerNewsDragPayload("alert", alert.cluster_id, formatDraggedAlert(alert));
        return '<div class="alert-card' + active + '">'
          + '<button class="card-button drag-news-card" type="button" draggable="true" data-drag-news-id="' + escapeHtml(dragId) + '" data-cluster="' + escapeHtml(alert.cluster_id) + '">'
          + '<div class="card-topline">'
          + '<span class="badge level-' + escapeHtml(alert.level) + '">' + escapeHtml(alert.level.toUpperCase()) + '</span>'
          + '<span class="badge dir-' + escapeHtml(alert.direction) + '">' + escapeHtml(alert.direction) + '</span>'
          + prefix
          + "</div>"
          + '<div class="headline">' + escapeHtml(alert.headline) + "</div>"
          + '<div class="card-meta">分数 ' + escapeHtml(score(alert.final_score)) + " · "
          + escapeHtml((alert.symbols || []).join(", ") || "n/a") + "</div>"
          + '<p class="summary">' + escapeHtml(alert.reason) + "</p>"
          + "</button></div>";
      }).join("");

      host.querySelectorAll("[data-cluster]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.selectedClusterId = button.getAttribute("data-cluster");
          render();
        });
      });
      attachNewsDragHandlers(host);
    }

    function renderEvents() {
      const events = filteredEvents();
      document.getElementById("eventCountLabel").textContent = events.length + " 个";

      const host = document.getElementById("eventsGrid");
      if (!events.length) {
        host.innerHTML = '<div class="empty">没有匹配的事件，试试更宽松的搜索词。</div>';
        return;
      }

      host.innerHTML = events.map(function (event) {
        const active = event.cluster_id === state.selectedClusterId ? " active" : "";
        const topSymbols = (event.top_instruments || []).map(function (item) { return item.symbol; }).slice(0, 4).join(", ") || "n/a";
        const dragId = registerNewsDragPayload("event", event.cluster_id, formatDraggedEvent(event, "事件卡片"));
        return '<div class="event-card' + active + '">'
          + '<button class="card-button drag-news-card" type="button" draggable="true" data-drag-news-id="' + escapeHtml(dragId) + '" data-cluster="' + escapeHtml(event.cluster_id) + '">'
          + '<div class="card-topline">'
          + '<span class="badge dir-' + escapeHtml(event.direction) + '">' + escapeHtml(event.direction) + '</span>'
          + '<span class="badge type-' + escapeHtml(event.event_type) + '">' + escapeHtml(event.event_type) + '</span>'
          + '<span>score ' + escapeHtml(score(event.final_score)) + "</span>"
          + '<span>docs ' + escapeHtml(event.doc_count) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(event.headline) + "</div>"
          + '<p class="summary">' + escapeHtml(event.summary || "暂无事件摘要，先看右侧原文列表。") + "</p>"
          + '<div class="tag-row">'
          + (event.themes || []).slice(0, 4).map(function (theme) {
              return '<span class="mini-tag">' + escapeHtml(theme) + "</span>";
            }).join("")
          + "</div>"
          + '<div class="card-meta">候选标的: ' + escapeHtml(topSymbols) + "</div>"
          + "</button></div>";
      }).join("");

      host.querySelectorAll("[data-cluster]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.selectedClusterId = button.getAttribute("data-cluster");
          render();
        });
      });
      attachNewsDragHandlers(host);
    }

    function renderAiInsightDetail(event) {
      const judgement = event.model_judgement || {};
      const selection = event.ai_selection || {};
      const hasJudgement = Object.keys(judgement).length > 0 || Object.keys(selection).length > 0;
      if (!hasJudgement) {
        return "";
      }
      const gate = judgement.attention_gate || {};
      const screening = judgement.screening || {};
      const status = judgement.screening_status || selection.status || "n/a";
      const selectedBy = selection.selected_by === "gpt" || status === "used" ? "AI判断" : "注意力门槛";
      const tier = gate.tier || selection.tier || "n/a";
      const gateScore = gate.score !== undefined ? gate.score : selection.score;
      const reasons = Array.isArray(gate.reasons)
        ? gate.reasons
        : (Array.isArray(selection.reasons) ? selection.reasons : []);
      const modelReason = screening.reason
        || screening.reject_reason
        || selection.model_reason
        || judgement.screening_reason
        || "";
      const reasonList = []
        .concat(modelReason ? ["模型判断：" + modelReason] : [])
        .concat(reasons.length ? ["门槛理由：" + reasons.slice(0, 5).join("；")] : []);
      const renderedReasons = reasonList.map(function (item) {
        return "<li>" + escapeHtml(item) + "</li>";
      }).join("") || "<li>暂无AI判断细节。</li>";

      return '<div class="detail-block">'
        + '<div class="detail-section-title"><strong>AI判断</strong><span>是否值得调用模型/继续看</span></div>'
        + '<div class="chip-row">'
        + '<span class="mini-tag">来源：' + escapeHtml(selectedBy) + "</span>"
        + '<span class="mini-tag">状态：' + escapeHtml(status) + "</span>"
        + '<span class="mini-tag">层级：' + escapeHtml(tier) + "</span>"
        + '<span class="mini-tag">门槛分：' + escapeHtml(score(gateScore)) + "</span>"
        + "</div>"
        + '<ul class="rationale-list" style="margin-top:12px;">' + renderedReasons + "</ul>"
        + "</div>";
    }

    function renderDetail() {
      const event = selectedEvent();
      const host = detailView();
      if (!event) {
        host.innerHTML = '<div class="empty">暂无事件可展示。</div>';
        host.scrollTop = 0;
        const column = rightColumn();
        if (column) {
          column.scrollTop = 0;
        }
        return;
      }

      const instruments = (event.top_instruments || []).map(function (item) {
        return '<div class="instrument-card">'
          + '<div class="card-topline">'
          + '<span class="badge dir-' + escapeHtml(item.direction) + '">' + escapeHtml(item.direction) + '</span>'
          + '<span>' + escapeHtml(item.market) + "</span>"
          + '<span>score ' + escapeHtml(score(item.final_score)) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(item.symbol + " · " + item.name) + "</div>"
          + '<p class="instrument-note">' + escapeHtml((item.reasons || []).join("；") || "暂无说明") + "</p>"
          + "</div>";
      }).join("") || '<div class="empty">当前事件还没有映射到候选标的。</div>';

      const docs = (event.related_documents || []).map(function (doc) {
        const summary = doc.summary || "无摘要";
        const link = doc.url
          ? '<a href="' + escapeHtml(doc.url) + '" target="_blank" rel="noreferrer">打开原文</a>'
          : '<span class="tiny">暂无原文链接</span>';
        return '<li class="doc-card">'
          + '<div class="card-meta">' + escapeHtml(doc.published_at) + " · " + escapeHtml(doc.source_id) + "</div>"
          + '<div class="headline">' + escapeHtml(doc.title) + "</div>"
          + '<p>' + escapeHtml(summary) + "</p>"
          + '<div class="tag-row">'
          + (doc.themes || []).slice(0, 4).map(function (theme) {
              return '<span class="mini-tag">' + escapeHtml(theme) + "</span>";
            }).join("")
          + "</div>"
          + '<div class="tag-row">' + link + "</div>"
          + "</li>";
      }).join("") || '<div class="empty">当前事件没有展开到原文。</div>';

      const rationale = (event.rationale || []).map(function (item) {
        return "<li>" + escapeHtml(item) + "</li>";
      }).join("") || "<li>暂无解释。</li>";

      host.innerHTML = '<div class="detail-grid">'
        + '<div class="detail-block">'
        + '<div class="detail-section-title"><strong>事件摘要</strong><span>核心信息</span></div>'
        + '<div class="card-topline">'
        + '<span class="badge dir-' + escapeHtml(event.direction) + '">' + escapeHtml(event.direction) + '</span>'
        + '<span class="badge type-' + escapeHtml(event.event_type) + '">' + escapeHtml(event.event_type) + '</span>'
        + '<span>docs ' + escapeHtml(event.doc_count) + "</span>"
        + '<span>' + escapeHtml((event.source_ids || []).join(", ") || "source n/a") + "</span>"
        + "</div>"
        + '<h3 class="headline-serif">' + escapeHtml(event.headline) + "</h3>"
        + '<p class="summary">' + escapeHtml(event.summary || "暂无摘要。") + "</p>"
        + '<div class="chip-row">'
        + (event.entities || []).slice(0, 6).map(function (entity) {
            return '<span class="mini-tag">' + escapeHtml(entity) + "</span>";
          }).join("")
        + (event.markets || []).map(function (market) {
            return '<span class="mini-tag">' + escapeHtml(market) + "</span>";
          }).join("")
        + "</div>"
        + '<div class="detail-times">'
        + '<span>首次出现: ' + escapeHtml(event.first_seen_at || "n/a") + "</span>"
        + '<span>最近更新: ' + escapeHtml(event.last_seen_at || "n/a") + "</span>"
        + "</div>"
        + "</div>"
        + renderAiInsightDetail(event)
        + '<div class="detail-block">'
        + '<div class="detail-section-title"><strong>评分拆解</strong><span>为什么它排在这里</span></div>'
        + '<div class="score-strip">'
        + '<div class="score-box"><div class="label">Final</div><div class="value">' + escapeHtml(score(event.final_score)) + "</div></div>"
        + '<div class="score-box"><div class="label">Heat</div><div class="value">' + escapeHtml(score(event.heat_score)) + "</div></div>"
        + '<div class="score-box"><div class="label">Importance</div><div class="value">' + escapeHtml(score(event.importance_score)) + "</div></div>"
        + "</div>"
        + "</div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>影响逻辑</strong><span>打分依据</span></div><ul class="rationale-list">' + rationale + "</ul></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>候选标的</strong><span>可能受影响的交易对象</span></div><div class="stack">' + instruments + "</div></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>相关新闻原文</strong><span>点击可跳转</span></div><ul class="doc-list">' + docs + "</ul></div>"
        + "</div>";
      host.scrollTop = 0;
      const column = rightColumn();
      if (column) {
        column.scrollTop = 0;
      }
    }

    function renderInstruments() {
      const instruments = filteredInstruments();
      document.getElementById("instrumentCountLabel").textContent = instruments.length + " 个";
      const host = document.getElementById("instrumentList");
      if (!instruments.length) {
        host.innerHTML = '<div class="empty">当前筛选下没有候选标的。</div>';
        return;
      }

      host.innerHTML = instruments.map(function (item) {
        const active = item.cluster_id === state.selectedClusterId ? " active" : "";
        const dragId = registerNewsDragPayload("instrument", item.symbol || item.cluster_id, formatDraggedInstrument(item, "候选标的卡片"));
        return '<div class="instrument-card' + active + '">'
          + '<button class="card-button drag-news-card" type="button" draggable="true" data-drag-news-id="' + escapeHtml(dragId) + '" data-cluster="' + escapeHtml(item.cluster_id) + '">'
          + '<div class="card-topline">'
          + '<span class="badge dir-' + escapeHtml(item.direction) + '">' + escapeHtml(item.direction) + '</span>'
          + '<span>' + escapeHtml(item.market) + "</span>"
          + '<span>score ' + escapeHtml(score(item.final_score)) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(item.symbol + " · " + item.name) + "</div>"
          + '<p class="summary">' + escapeHtml(item.headline) + "</p>"
          + "</button></div>";
      }).join("");

      host.querySelectorAll("[data-cluster]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.selectedClusterId = button.getAttribute("data-cluster");
          render();
        });
      });
      attachNewsDragHandlers(host);
    }

    function renderFeed() {
      const items = filteredFeed();
      document.getElementById("feedCountLabel").textContent = items.length + " 条";
      const host = document.getElementById("feedList");
      if (!items.length) {
        host.innerHTML = '<div class="empty">当前筛选下没有消息流内容。</div>';
        return;
      }

      host.innerHTML = items.map(function (item) {
        const link = item.url
          ? '<a href="' + escapeHtml(item.url) + '" target="_blank" rel="noreferrer">打开原文</a>'
          : '<span class="tiny">暂无原文链接</span>';
        const dragId = registerNewsDragPayload("feed", item.url || item.title, formatDraggedFeed(item));
        return '<div class="feed-card drag-news-card" draggable="true" data-drag-news-id="' + escapeHtml(dragId) + '">'
          + '<div class="card-meta">' + escapeHtml(item.published_at) + " · " + escapeHtml(item.source_id) + "</div>"
          + '<div class="headline">' + escapeHtml(item.title) + "</div>"
          + '<p>' + escapeHtml(item.summary || "无摘要") + "</p>"
          + '<div class="tag-row">'
          + (item.themes || []).slice(0, 4).map(function (theme) {
              return '<span class="mini-tag">' + escapeHtml(theme) + "</span>";
            }).join("")
          + (item.entities || []).slice(0, 4).map(function (entity) {
              return '<span class="mini-tag">' + escapeHtml(entity) + "</span>";
            }).join("")
          + '</div><div class="tag-row">' + link + "</div></div>";
      }).join("");
      attachNewsDragHandlers(host);
    }

    function renderPanelError(hostId, title, detail) {
      const host = document.getElementById(hostId);
      if (!host) {
        return;
      }
      host.innerHTML = '<div class="empty"><strong>' + escapeHtml(title)
        + '</strong><br>' + escapeHtml(detail || "当前模块渲染失败，请稍后刷新重试。")
        + "</div>";
    }

    function safeRender(label, hostIds, fn) {
      try {
        fn();
      } catch (error) {
        console.error("dashboard render failed:", label, error);
        hostIds.forEach(function (hostId) {
          renderPanelError(hostId, label + " 模块暂时不可用", "这块内容渲染出错了，但其他功能块会继续工作。");
        });
      }
    }

    function render() {
      safeRender("视图切换", ["viewSwitch"], renderViewSwitch);
      safeRender("工作区", ["coreWorkspace", "aiWorkspace", "techWorkspace", "frontierWorkspace"], renderWorkspaces);
      safeRender("头部摘要", ["heroMeta", "metricGrid"], renderHero);
      safeRender("运行状态", ["runtimeStatusGrid"], renderRuntimeStatus);
      safeRender("筛选器", ["filterChips"], renderFilters);

      if (state.view === "tech") {
        safeRender(
          "港A科技专题",
          ["techSignalList", "techThemeList", "techFrontierList", "techAssetList", "lexiconDiscoveryList", "lexiconReviewNote", "techDetailView"],
          renderTechBlock
        );
        schedulePersistState();
        return;
      }

      if (state.view === "ai") {
        safeRender("AI判断", ["aiJudgementList"], renderAiJudgement);
        schedulePersistState();
        return;
      }

      if (state.view === "frontier") {
        safeRender(
          "科技前沿",
          ["frontierTrackerList", "frontierSignalList", "frontierDetailView"],
          renderFrontierWorkspace
        );
        schedulePersistState();
        return;
      }

      safeRender("提醒列表", ["alertsList"], renderAlerts);
      safeRender("事件列表", ["eventsGrid"], renderEvents);
      safeRender("事件详情", ["detailView"], renderDetail);
      safeRender("候选标的", ["instrumentList"], renderInstruments);
      safeRender("原始消息流", ["feedList"], renderFeed);
      schedulePersistState();
    }

    function renderAndRestoreIfNeeded() {
      render();
      if (restoreScrollPending) {
        restoreScrollPositions();
        restoreScrollPending = false;
      }
    }

    const searchInput = document.getElementById("searchInput");
    const lexiconCatalogInput = document.getElementById("lexiconCatalogQuery");
    const resetButton = document.getElementById("resetButton");
    const lexiconAiReviewButton = document.getElementById("lexiconAiReviewButton");
    const aiAskButton = document.getElementById("aiAskButton");
    const aiDropZone = document.getElementById("aiDropZone");
    const aiAskInput = document.getElementById("aiAskInput");

    restoreDashboardState();
    if (searchInput) {
      searchInput.value = state.query;
    }
    if (lexiconCatalogInput) {
      lexiconCatalogInput.value = state.lexiconCatalogQuery;
    }
    renderAndRestoreIfNeeded();
    refreshDiscoveryFromApi();

    if (searchInput) {
      searchInput.addEventListener("input", function (event) {
        markInteraction();
        state.query = String(event.target.value || "").trim().toLowerCase();
        render();
      });
    }

    if (lexiconCatalogInput) {
      lexiconCatalogInput.addEventListener("input", function (event) {
        markInteraction();
        state.lexiconCatalogQuery = String(event.target.value || "").trim().toLowerCase();
        renderTechBlock();
        schedulePersistState();
      });
    }

    if (resetButton) {
      resetButton.addEventListener("click", function () {
        markInteraction();
        state.direction = "all";
        state.query = "";
        if (searchInput) {
          searchInput.value = "";
        }
        render();
      });
    }

    if (lexiconAiReviewButton) {
      lexiconAiReviewButton.addEventListener("click", function () {
        requestLexiconAiReview(lexiconAiReviewButton);
      });
    }

    if (aiAskButton) {
      aiAskButton.addEventListener("click", submitManualNewsAnalysis);
    }

    if (aiDropZone && aiAskInput) {
      ["dragenter", "dragover"].forEach(function (eventName) {
        aiDropZone.addEventListener(eventName, function (event) {
          event.preventDefault();
          markInteraction();
          if (event.dataTransfer) {
            event.dataTransfer.dropEffect = "copy";
          }
          aiDropZone.classList.add("dragover");
        });
      });
      ["dragleave", "drop"].forEach(function (eventName) {
        aiDropZone.addEventListener(eventName, function (event) {
          event.preventDefault();
          aiDropZone.classList.remove("dragover");
        });
      });
      aiDropZone.addEventListener("drop", function (event) {
        markInteraction();
        const transfer = event.dataTransfer;
        if (!transfer) {
          return;
        }
        const droppedText = transfer.getData("text/plain");
        if (droppedText) {
          loadAskDraftFromText(droppedText, transfer.getData("application/x-market-news-card") ? "已接收看板新闻卡片" : "已接收拖入文本");
          return;
        }
        const file = transfer.files && transfer.files[0];
        if (!file) {
          return;
        }
        const reader = new FileReader();
        reader.onload = function () {
          loadAskDraftFromText(String(reader.result || "").slice(0, 20000), "已读取文件内容");
        };
        reader.readAsText(file);
      });
    }

    document.addEventListener("click", function () {
      markInteraction();
    }, true);
    document.addEventListener("keydown", function () {
      markInteraction();
    }, true);
    document.addEventListener("change", function () {
      markInteraction();
    }, true);
    document.querySelectorAll("[data-scroll-key]").forEach(function (node) {
      node.addEventListener("scroll", function () {
        markInteraction();
      }, { passive: true });
    });
    window.addEventListener("beforeunload", function () {
      persistDashboardState();
    });

    let countdown = 60;
    function refreshPage() {
      persistDashboardState();
      const url = new URL(window.location.href);
      url.searchParams.set("ts", String(Date.now()));
      window.location.replace(url.toString());
    }
    function renderRefreshText() {
      const refreshText = document.getElementById("refreshText");
      if (!refreshText) {
        return;
      }
      refreshText.textContent = hasRecentInteraction()
        ? "检测到你最近在操作，自动刷新已延后，停止操作后会再等 60 秒刷新。"
        : ("页面每 60 秒自动刷新一次，距下次刷新 " + countdown + " 秒");
    }
    renderRefreshText();
    setInterval(function () {
      if (hasRecentInteraction()) {
        countdown = 60;
        renderRefreshText();
        return;
      }
      countdown -= 1;
      if (countdown <= 0) {
        refreshPage();
        return;
      }
      renderRefreshText();
    }, 1000);
  </script>
</body>
</html>
"""
        )
        return template.substitute(payload_json=payload_json)


def refresh_runtime_status_views(report_path: Path) -> None:
    if not report_path.exists():
        return
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    reporter = MarkdownJsonReporter(report_path.parent)
    payload["runtime_status"] = reporter._load_runtime_status()
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html_path = report_path.with_name("latest_dashboard.html")
    html_path.write_text(reporter._build_html(payload), encoding="utf-8")
