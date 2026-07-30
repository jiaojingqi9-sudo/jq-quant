from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from market_news.common import clamp, parse_datetime, stable_id, tokenize, unique_preserve, utcnow


SOURCE_TRUST_HINTS = {
    "cninfo_latest": 0.95,
    "eastmoney-ann": 0.88,
    "eastmoney-focus": 0.84,
    "eastmoney-724": 0.82,
    "eastmoney-topic": 0.78,
    "cls": 0.86,
    "sse_announcements": 0.94,
    "szse_announcements": 0.94,
    "hkex_news": 0.94,
    "sfc-offer-periods": 0.92,
    "xinhua-tech": 0.9,
    "reuters-tech": 0.88,
    "weibo": 0.42,
    "xueqiu": 0.48,
}

OFFICIAL_SOURCE_PREFIXES = (
    "gov-",
    "csrc",
    "cninfo",
    "sse",
    "szse",
    "hkex",
    "sfc",
)

SOCIAL_SOURCE_IDS = {"weibo", "xueqiu"}
REFUTED_PATTERNS = ("辟谣", "不实", "澄清", "误判", "否认", "refute", "false")
STALE_PATTERNS = ("盘前跌", "盘前涨", "盘中跌", "盘中涨", "大跌后", "大涨后", "已完成", "此前")
CLAIM_EVENT_PATTERNS = {
    "policy": ("政策", "规划", "通知", "发布", "标准体系", "意见", "办法"),
    "regulation": ("处罚", "立案", "监管", "问询", "警示", "违规"),
    "company": ("公告", "年度报告", "一季度报告", "净利润", "营收", "订单", "合同", "中标", "收购", "回购"),
    "industry": ("行业", "产业", "价格", "涨价", "需求", "产能"),
    "macro": ("央行", "利率", "汇率", "通胀", "就业"),
}


@dataclass(slots=True)
class NewsLearningResult:
    output_dir: Path
    artifact_paths: dict[str, Path]
    attribution: dict[str, Any]
    candidates: list[dict[str, Any]]
    review_packet: dict[str, Any]


def build_news_learning_artifacts(
    *,
    report_path: Path,
    output_dir: Path,
    min_source_sample: int = 3,
    min_topic_sample: int = 3,
    stale_seconds: int = 24 * 60 * 60,
) -> NewsLearningResult:
    """Build research-only Evidence-to-Review artifacts from a news report.

    This learning loop is deliberately non-mutating: it reads report artifacts,
    writes review files, and never changes production collection/ranking config.
    """
    report = _load_json(report_path)
    generated_at = utcnow()
    fetched_at = parse_datetime(str(report.get("created_at") or generated_at.isoformat()))

    memory = _extract_news_memory(report, fetched_at=fetched_at)
    claims = _extract_claims(memory)
    outcomes = _label_outcomes(
        memory=memory,
        claims=claims,
        generated_at=generated_at,
        stale_seconds=stale_seconds,
    )
    attribution = _build_attribution(
        memory=memory,
        outcomes=outcomes,
        generated_at=generated_at,
        report_path=report_path,
    )
    candidates = _generate_upgrade_candidates(
        attribution=attribution,
        memory=memory,
        min_source_sample=min_source_sample,
        min_topic_sample=min_topic_sample,
    )
    promotion_report = _build_promotion_report(candidates, generated_at=generated_at)

    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_paths = {
        "news_memory": output_dir / "news_memory.jsonl",
        "news_claims": output_dir / "news_claims.jsonl",
        "news_outcomes": output_dir / "news_outcomes.jsonl",
        "news_attribution": output_dir / "news_attribution.json",
        "news_upgrade_candidates": output_dir / "news_upgrade_candidates.jsonl",
        "news_promotion_report": output_dir / "news_promotion_report.json",
        "news_learning_review_packet_json": output_dir / "news_learning_review_packet.json",
        "news_learning_review_packet_md": output_dir / "news_learning_review_packet.md",
        "news_learning_codex_handoff": output_dir / "news_learning_codex_handoff.md",
    }

    _write_jsonl(artifact_paths["news_memory"], memory)
    _write_jsonl(artifact_paths["news_claims"], claims)
    _write_jsonl(artifact_paths["news_outcomes"], outcomes)
    _write_json(artifact_paths["news_attribution"], attribution)
    _write_jsonl(artifact_paths["news_upgrade_candidates"], candidates)
    _write_json(artifact_paths["news_promotion_report"], promotion_report)

    review_packet = _build_review_packet(
        generated_at=generated_at,
        report_path=report_path,
        output_dir=output_dir,
        artifact_paths=artifact_paths,
        attribution=attribution,
        candidates=candidates,
        promotion_report=promotion_report,
    )
    _write_json(artifact_paths["news_learning_review_packet_json"], review_packet)
    _write_text(artifact_paths["news_learning_review_packet_md"], _render_review_packet_markdown(review_packet))
    _write_text(artifact_paths["news_learning_codex_handoff"], _render_codex_handoff_markdown(review_packet))

    return NewsLearningResult(
        output_dir=output_dir,
        artifact_paths=artifact_paths,
        attribution=attribution,
        candidates=candidates,
        review_packet=review_packet,
    )


def _extract_news_memory(report: dict[str, Any], *, fetched_at: datetime) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}

    def upsert(doc: dict[str, Any], context: dict[str, Any]) -> None:
        if not isinstance(doc, dict):
            return
        source_id = str(doc.get("source_id") or doc.get("source") or "unknown").strip() or "unknown"
        title = str(doc.get("title") or context.get("headline") or "").strip()
        if not title:
            return
        url = str(doc.get("url") or "").strip()
        published_at = str(doc.get("published_at") or context.get("first_seen_at") or fetched_at.isoformat())
        record_id = stable_id(source_id, url, title, published_at)
        raw_summary = str(doc.get("summary") or context.get("summary") or "").strip()
        event_symbols = [
            str(item.get("symbol", "")).strip()
            for item in context.get("top_instruments", [])
            if isinstance(item, dict) and str(item.get("symbol", "")).strip()
        ]
        entities = unique_preserve(
            [str(item) for item in doc.get("entities", [])]
            + [str(item) for item in context.get("entities", [])]
        )
        topics = unique_preserve(
            [str(item) for item in doc.get("themes", [])]
            + [str(item) for item in context.get("themes", [])]
            + [str(item) for item in context.get("trigger_tags", [])]
        )
        latency = _latency_seconds(published_at, fetched_at)
        row = records.get(record_id)
        if row is None:
            records[record_id] = {
                "record_id": record_id,
                "source": source_id,
                "source_id": source_id,
                "source_trust": _source_trust(source_id),
                "url": url,
                "title": title,
                "published_at": published_at,
                "fetched_at": fetched_at.isoformat(),
                "latency_seconds": latency,
                "symbols": unique_preserve(event_symbols + _symbolish_entities(entities)),
                "entities": entities,
                "topics": topics,
                "raw_summary": raw_summary,
                "language": str(doc.get("language") or _guess_language(title + raw_summary)),
                "cluster_ids": unique_preserve([str(context.get("cluster_id", ""))]),
                "event_type": str(context.get("event_type") or _infer_event_type(title + " " + raw_summary)),
                "direction": str(context.get("direction") or "neutral"),
                "final_score": _safe_float(context.get("final_score")),
                "source_ids_in_cluster": unique_preserve([str(item) for item in context.get("source_ids", [])] or [source_id]),
                "doc_count": int(context.get("doc_count") or 1),
                "is_social": source_id in SOCIAL_SOURCE_IDS,
            }
            return
        row["symbols"] = unique_preserve(row.get("symbols", []) + event_symbols + _symbolish_entities(entities))
        row["entities"] = unique_preserve(row.get("entities", []) + entities)
        row["topics"] = unique_preserve(row.get("topics", []) + topics)
        row["cluster_ids"] = unique_preserve(row.get("cluster_ids", []) + [str(context.get("cluster_id", ""))])
        row["source_ids_in_cluster"] = unique_preserve(
            row.get("source_ids_in_cluster", []) + [str(item) for item in context.get("source_ids", [])]
        )
        row["doc_count"] = max(int(row.get("doc_count", 1)), int(context.get("doc_count") or 1))
        row["final_score"] = max(_safe_float(row.get("final_score")), _safe_float(context.get("final_score")))

    for event in _iter_event_payloads(report):
        context = dict(event)
        for doc in event.get("related_documents", []) if isinstance(event.get("related_documents"), list) else []:
            upsert(doc, context)

    for signal in _iter_tech_signals(report):
        context = {
            **signal,
            "final_score": signal.get("trading_attention_score") or signal.get("attention_score"),
            "themes": signal.get("trigger_tags", []) + signal.get("themes", []),
        }
        for doc in signal.get("related_documents", []) if isinstance(signal.get("related_documents"), list) else []:
            upsert(doc, context)

    for doc in report.get("latest_feed", []) if isinstance(report.get("latest_feed"), list) else []:
        upsert(doc, {"event_type": "unknown", "direction": "neutral", "source_ids": [doc.get("source_id", "unknown")]})

    duplicate_groups = Counter(_duplicate_key(row) for row in records.values())
    for row in records.values():
        row["is_duplicate"] = duplicate_groups[_duplicate_key(row)] > 1
    return sorted(records.values(), key=lambda item: (str(item.get("published_at", "")), item["record_id"]), reverse=True)


def _extract_claims(memory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claims = []
    for row in memory:
        claim_text = _claim_text(row)
        confidence = _claim_confidence(row)
        claims.append(
            {
                "claim_id": stable_id(str(row["record_id"]), "claim", claim_text),
                "record_id": row["record_id"],
                "claim_text": claim_text,
                "entities": row.get("entities", []),
                "topics": row.get("topics", []),
                "event_type": row.get("event_type") or _infer_event_type(claim_text),
                "confidence": round(confidence, 3),
                "source_ids": row.get("source_ids_in_cluster") or [row.get("source_id")],
                "source": row.get("source_id"),
                "url": row.get("url"),
            }
        )
    return claims


def _label_outcomes(
    *,
    memory: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    generated_at: datetime,
    stale_seconds: int,
) -> list[dict[str, Any]]:
    rows_by_id = {str(row["record_id"]): row for row in memory}
    outcomes = []
    for claim in claims:
        row = rows_by_id.get(str(claim["record_id"]), {})
        text = f"{row.get('title', '')} {row.get('raw_summary', '')} {claim.get('claim_text', '')}".lower()
        validation_sources = [
            source_id
            for source_id in row.get("source_ids_in_cluster", [])
            if source_id and source_id != row.get("source_id")
        ]
        if row.get("is_duplicate"):
            status = "duplicate"
            reason = "同一标题/URL 在当前证据集中重复出现。"
        elif any(pattern.lower() in text for pattern in REFUTED_PATTERNS):
            status = "refuted"
            reason = "文本包含辟谣、误判、澄清或否认类表述，需要人工复核。"
        elif _is_noise(row, claim):
            status = "noise"
            reason = "缺少实体/主题/事件类型，预测价值弱。"
        elif _safe_float(row.get("latency_seconds")) > stale_seconds or any(pattern.lower() in text for pattern in STALE_PATTERNS):
            status = "stale"
            reason = "发布时间到抓取时间延迟过长，或文本呈现事后价格反应。"
        elif validation_sources or _is_official_source(str(row.get("source_id", ""))):
            status = "confirmed"
            reason = "来自官方/高可信来源，或已有跨来源印证。"
        elif _safe_float(claim.get("confidence")) >= 0.66 and _safe_float(row.get("source_trust")) >= 0.78:
            status = "confirmed"
            reason = "单来源但可信度和结构化信息较强，暂记为已验证证据。"
        else:
            status = "unverified"
            reason = "缺少跨来源验证，先作为待复核/未验证证据。"

        outcomes.append(
            {
                "outcome_id": stable_id(str(claim["claim_id"]), status),
                "claim_id": claim["claim_id"],
                "record_id": claim["record_id"],
                "status": status,
                "validation_sources": validation_sources or [str(row.get("source_id", ""))],
                "validated_at": generated_at.isoformat(),
                "validation_reason": reason,
                "market_reaction": {
                    "symbols": row.get("symbols", []),
                    "market_impact_after_5m": None,
                    "market_impact_after_30m": None,
                    "market_impact_after_1d": None,
                    "data_status": "price_series_not_connected",
                    "proxy_event_final_score": row.get("final_score"),
                },
            }
        )
    return outcomes


def _build_attribution(
    *,
    memory: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    generated_at: datetime,
    report_path: Path,
) -> dict[str, Any]:
    outcome_by_record = {str(row["record_id"]): row for row in outcomes}
    source_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    topic_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    entity_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    event_type_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in memory:
        enriched = {**row, "outcome_status": outcome_by_record.get(str(row["record_id"]), {}).get("status", "noise")}
        source_groups[str(row.get("source_id", "unknown"))].append(enriched)
        event_type_groups[str(row.get("event_type", "unknown"))].append(enriched)
        for topic in row.get("topics", []) or ["unknown"]:
            topic_groups[str(topic)].append(enriched)
        for entity in row.get("entities", []) or ["unknown"]:
            entity_groups[str(entity)].append(enriched)

    source_quality = {name: _quality_metrics(rows) for name, rows in sorted(source_groups.items())}
    topic_quality = {name: _quality_metrics(rows) for name, rows in sorted(topic_groups.items())}
    entity_quality = {name: _quality_metrics(rows) for name, rows in sorted(entity_groups.items())}
    event_type_quality = {name: _quality_metrics(rows) for name, rows in sorted(event_type_groups.items())}
    source_counts = Counter(str(row.get("source_id", "unknown")) for row in memory)
    total = sum(source_counts.values()) or 1
    top_source, top_count = source_counts.most_common(1)[0] if source_counts else ("unknown", 0)

    return {
        "generated_at": generated_at.isoformat(),
        "input_report_path": str(report_path),
            "sample_size": len(memory),
        "source_diversity": {
            "source_count": len(source_counts),
            "top_source": top_source,
            "top_source_share": round(top_count / total, 4),
            "herfindahl": round(sum((count / total) ** 2 for count in source_counts.values()), 4),
            "over_reliance": (top_count / total) >= 0.6,
        },
        "source_quality": source_quality,
        "topic_quality": topic_quality,
        "entity_quality": entity_quality,
        "event_type_quality": event_type_quality,
        "best_sources": _rank_quality(source_quality, reverse=True)[:5],
        "worst_sources": _rank_quality(source_quality, reverse=False)[:5],
        "best_topics": _rank_quality(topic_quality, reverse=True)[:8],
        "worst_topics": _rank_quality(topic_quality, reverse=False)[:8],
        "metric_notes": {
            "market_impact_after_5m_30m_1d": "字段已在 news_outcomes.jsonl 保留；当前未接价格序列，因此为 null。",
            "confirmed": "当前版本表示官方/高可信或跨来源印证，不等同于未来绝对真实。",
            "unverified": "当前证据不足或缺少跨来源验证；不等同于发布时间滞后。",
            "scope_guard": "research-only; no code/config mutation.",
        },
    }


def _generate_upgrade_candidates(
    *,
    attribution: dict[str, Any],
    memory: list[dict[str, Any]],
    min_source_sample: int,
    min_topic_sample: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    for source, metrics in attribution.get("source_quality", {}).items():
        total = int(metrics.get("total", 0))
        if total < min_source_sample:
            candidates.append(
                _candidate(
                    action="collect_more_data",
                    target_type="source",
                    target=source,
                    reason=f"来源样本只有 {total} 条，不足以直接调权。",
                    metrics=metrics,
                    confidence=0.45,
                )
            )
            continue
        precision = _safe_float(metrics.get("source_precision"))
        duplicate_rate = _safe_float(metrics.get("duplicate_rate"))
        stale_rate = _safe_float(metrics.get("stale_rate"))
        unverified_rate = _safe_float(metrics.get("unverified_rate"))
        false_rate = _safe_float(metrics.get("false_or_refuted_rate"))
        if precision >= 0.72 and stale_rate <= 0.22 and false_rate <= 0.05:
            candidates.append(
                _candidate(
                    action="uprank_source",
                    target_type="source",
                    target=source,
                    reason="来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。",
                    metrics=metrics,
                    confidence=0.72,
                )
            )
        if duplicate_rate >= 0.35:
            candidates.append(
                _candidate(
                    action="improve_deduplication",
                    target_type="source",
                    target=source,
                    reason="重复新闻比例偏高，应先优化去重而不是扩大抓取。",
                    metrics=metrics,
                    confidence=0.7,
                )
            )
        if stale_rate >= 0.35:
            candidates.append(
                _candidate(
                    action="downrank_source",
                    target_type="source",
                    target=source,
                    reason="滞后/过期新闻比例偏高，建议人工复核是否降权。",
                    metrics=metrics,
                    confidence=0.68,
                )
            )
        if unverified_rate >= 0.35:
            candidates.append(
                _candidate(
                    action="add_cross_source_verification",
                    target_type="source",
                    target=source,
                    reason="单来源未验证比例偏高，应先要求交叉验证或补强实体/主题解析，不宜直接按过期新闻降权。",
                    metrics=metrics,
                    confidence=0.66,
                )
            )
        if false_rate >= 0.08:
            candidates.append(
                _candidate(
                    action="add_cross_source_verification",
                    target_type="source",
                    target=source,
                    reason="出现被反驳/误判信号，进入提醒前应要求二次验证。",
                    metrics=metrics,
                    confidence=0.74,
                )
            )
        if _safe_float(metrics.get("entity_coverage")) < 0.35:
            candidates.append(
                _candidate(
                    action="improve_claim_extraction",
                    target_type="source",
                    target=source,
                    reason="实体覆盖不足，可能需要更好的正文解析或实体抽取。",
                    metrics=metrics,
                    confidence=0.62,
                )
            )

    for topic, metrics in attribution.get("topic_quality", {}).items():
        total = int(metrics.get("total", 0))
        if topic == "unknown" or total < min_topic_sample:
            continue
        quality = _safe_float(metrics.get("topic_signal_quality"))
        if quality >= 0.68:
            candidates.append(
                _candidate(
                    action="add_entity_or_topic_filter",
                    target_type="topic",
                    target=topic,
                    reason="主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。",
                    metrics=metrics,
                    confidence=0.66,
                )
            )
        elif quality <= 0.28:
            candidates.append(
                _candidate(
                    action="downrank_source",
                    target_type="topic",
                    target=topic,
                    reason="主题信号质量偏低，可能属于噪声题材或误匹配。",
                    metrics=metrics,
                    confidence=0.55,
                )
            )

    if attribution.get("source_diversity", {}).get("over_reliance"):
        candidates.append(
            _candidate(
                action="add_cross_source_verification",
                target_type="system",
                target=str(attribution["source_diversity"].get("top_source", "unknown")),
                reason="样本过度依赖单一来源，建议关键新闻进入提醒前至少有官方/二级来源交叉验证。",
                metrics=attribution["source_diversity"],
                confidence=0.78,
            )
        )

    if any(row.get("symbols") for row in memory):
        candidates.append(
            _candidate(
                action="add_market_impact_label",
                target_type="system",
                target="price_reaction_join",
                reason="新闻已能映射到候选资产，但尚未接入 5m/30m/1d 价格反应，建议作为下一阶段研究标签。",
                metrics={"records_with_symbols": sum(1 for row in memory if row.get("symbols")), "total": len(memory)},
                confidence=0.8,
            )
        )

    deduped: dict[str, dict[str, Any]] = {}
    for item in candidates:
        deduped[item["candidate_id"]] = item
    return sorted(deduped.values(), key=lambda item: float(item.get("confidence", 0)), reverse=True)


def _build_promotion_report(candidates: list[dict[str, Any]], *, generated_at: datetime) -> dict[str, Any]:
    ready = [item for item in candidates if _safe_float(item.get("confidence")) >= 0.65]
    return {
        "generated_at": generated_at.isoformat(),
        "scope": "news evidence-to-review",
        "hard_guards": {
            "auto_code_changes_allowed": False,
            "auto_live_config_changes_allowed": False,
            "stock_system_changes_allowed": False,
            "crypto_system_changes_allowed": False,
            "candidate_status": "research_review_only",
        },
        "candidate_count": len(candidates),
        "ready_for_codex_review_count": len(ready),
        "blocked_from_auto_apply_count": len(candidates),
        "candidates_by_action": dict(Counter(str(item.get("action")) for item in candidates)),
        "promoted_candidates": [
            {
                "candidate_id": item["candidate_id"],
                "action": item["action"],
                "target_type": item["target_type"],
                "target": item["target"],
                "confidence": item["confidence"],
                "gate": "manual_codex_review_required",
            }
            for item in ready
        ],
        "promotion_gate": [
            "检查 artifact sha256 是否一致",
            "确认候选不是由单一异常样本触发",
            "人工决定是否让 Codex 修改代码或采集策略",
            "禁止自动修改 live/news production 配置",
        ],
    }


def _build_review_packet(
    *,
    generated_at: datetime,
    report_path: Path,
    output_dir: Path,
    artifact_paths: dict[str, Path],
    attribution: dict[str, Any],
    candidates: list[dict[str, Any]],
    promotion_report: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": "news_learning_review_packet",
        "generated_at": generated_at.isoformat(),
        "purpose": "Evidence-to-Review learning loop for the news collector.",
        "codex_review_prompt": (
            "请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。"
            "请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。"
            "如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，"
            "也不要修改股票系统或 crypto 系统。"
        ),
        "input_report_path": str(report_path),
        "output_dir": str(output_dir),
        "artifacts": [
            {
                "name": key,
                "path": str(path),
                "sha256": _sha256(path) if path.exists() else "",
            }
            for key, path in artifact_paths.items()
            if path.exists() and not key.startswith("news_learning_review_packet")
        ],
        "review_packet_files": {
            "json": str(artifact_paths["news_learning_review_packet_json"]),
            "markdown": str(artifact_paths["news_learning_review_packet_md"]),
        },
        "guards": promotion_report["hard_guards"],
        "source_quality": attribution.get("source_quality", {}),
        "topic_quality": attribution.get("topic_quality", {}),
        "source_diversity": attribution.get("source_diversity", {}),
        "best_sources": attribution.get("best_sources", []),
        "worst_sources": attribution.get("worst_sources", []),
        "best_topics": attribution.get("best_topics", []),
        "worst_topics": attribution.get("worst_topics", []),
        "candidates": candidates[:30],
        "review_checklist": [
            "确认本包只生成 research artifacts，没有改 live/news production 配置。",
            "优先检查 worst_sources 中是否存在应降权或需要二次验证的来源。",
            "区分 stale 与 unverified；只有真实时效性问题才适合作为降权证据。",
            "检查 best_sources 是否样本量足够，避免因小样本误升权。",
            "逐条查看 ready_for_codex_review 的候选是否符合投资逻辑和来源洁净度要求。",
            "如要改代码或采集策略，另开 Codex 任务并引用 candidate_id，不允许本闭环自动执行。",
            "市场影响字段目前为 null；若候选依赖市场反应，先接入价格序列再评估。",
        ],
    }


def _render_review_packet_markdown(packet: dict[str, Any]) -> str:
    lines = [
        "# News Learning Review Packet",
        "",
        f"- Generated: `{packet.get('generated_at')}`",
        f"- Input report: `{packet.get('input_report_path')}`",
        f"- Output dir: `{packet.get('output_dir')}`",
        "- Scope: `research/review only`",
        "- Guard: `no auto code changes; no live config changes; no stock/crypto changes`",
        "",
        "## Codex Review Prompt",
        "",
        str(packet.get("codex_review_prompt", "")),
        "",
        "## Artifacts",
        "",
    ]
    for artifact in packet.get("artifacts", []):
        lines.append(f"- `{artifact['name']}`: `{artifact['path']}`")
        lines.append(f"  - sha256: `{artifact['sha256']}`")
    lines.extend(["", "## Source Quality", ""])
    lines.append(f"- Source diversity: `{json.dumps(packet.get('source_diversity', {}), ensure_ascii=False)}`")
    lines.append("- Best sources:")
    for item in packet.get("best_sources", [])[:5]:
        lines.append(
            f"  - `{item['name']}` precision={item['source_precision']} duplicate={item['duplicate_rate']} "
            f"stale={item['stale_rate']} unverified={item.get('unverified_rate', 0.0)} n={item['total']}"
        )
    lines.append("- Worst sources:")
    for item in packet.get("worst_sources", [])[:5]:
        lines.append(
            f"  - `{item['name']}` precision={item['source_precision']} duplicate={item['duplicate_rate']} "
            f"stale={item['stale_rate']} unverified={item.get('unverified_rate', 0.0)} n={item['total']}"
        )
    lines.extend(["", "## Topic Quality", ""])
    lines.append("- Best topics:")
    for item in packet.get("best_topics", [])[:8]:
        lines.append(f"  - `{item['name']}` quality={item['topic_signal_quality']} precision={item['source_precision']} n={item['total']}")
    lines.append("- Worst topics:")
    for item in packet.get("worst_topics", [])[:8]:
        lines.append(f"  - `{item['name']}` quality={item['topic_signal_quality']} precision={item['source_precision']} n={item['total']}")
    lines.extend(["", "## Candidates", ""])
    for item in packet.get("candidates", [])[:20]:
        lines.append(
            f"- `{item['candidate_id']}` `{item['action']}` target=`{item['target_type']}:{item['target']}` confidence={item['confidence']}"
        )
        lines.append(f"  - reason: {item['reason']}")
    lines.extend(["", "## Review Checklist", ""])
    for item in packet.get("review_checklist", []):
        lines.append(f"- [ ] {item}")
    lines.append("")
    return "\n".join(lines)


def _render_codex_handoff_markdown(packet: dict[str, Any]) -> str:
    prompt = str(packet.get("codex_review_prompt") or "").strip()
    lines = [
        "# Codex Handoff: News Evidence-to-Review",
        "",
        prompt,
        "",
        "## Must Respect",
        "",
        "- Do not auto-modify code.",
        "- Do not auto-modify live/news production config.",
        "- Do not modify stock system.",
        "- Do not modify crypto system.",
        "- Treat every candidate as research/review only until human approval.",
        "",
        "## Packet",
        "",
        f"- Generated: `{packet.get('generated_at')}`",
        f"- Input report: `{packet.get('input_report_path')}`",
        f"- Output dir: `{packet.get('output_dir')}`",
        f"- Review packet JSON: `{packet.get('review_packet_files', {}).get('json', '')}`",
        f"- Review packet Markdown: `{packet.get('review_packet_files', {}).get('markdown', '')}`",
        "",
        "## Evidence Artifacts",
        "",
    ]
    for artifact in packet.get("artifacts", []):
        lines.append(f"- `{artifact['name']}`: `{artifact['path']}`")
        lines.append(f"  - sha256: `{artifact['sha256']}`")
    lines.extend(["", "## Quick Triage", ""])
    lines.append(f"- Source diversity: `{json.dumps(packet.get('source_diversity', {}), ensure_ascii=False)}`")
    lines.append(f"- Candidate count in packet: `{len(packet.get('candidates', []))}`")
    lines.append("- Best sources: " + ", ".join(str(item.get("name")) for item in packet.get("best_sources", [])[:5]))
    lines.append("- Worst sources: " + ", ".join(str(item.get("name")) for item in packet.get("worst_sources", [])[:5]))
    lines.append("- Best topics: " + ", ".join(str(item.get("name")) for item in packet.get("best_topics", [])[:8]))
    lines.extend(["", "## Candidates To Review First", ""])
    for item in packet.get("candidates", [])[:15]:
        lines.append(
            f"- `{item.get('candidate_id')}` `{item.get('action')}` "
            f"target=`{item.get('target_type')}:{item.get('target')}` confidence={item.get('confidence')}"
        )
        lines.append(f"  - reason: {item.get('reason')}")
    lines.extend(
        [
            "",
            "## Ask Codex",
            "",
            "请读取上面的 artifact path，先做代码无关的评估：",
            "",
            "- 哪些来源应该继续观察、升权、降权或要求交叉验证？",
            "- 哪些主题真的有预测价值，哪些像噪声？",
            "- 哪些 candidate_id 值得下一步让 Codex 改代码或调整采集策略？",
            "- 如果要动代码，请先列计划并等待确认。",
            "",
        ]
    )
    return "\n".join(lines)


def _quality_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    if total <= 0:
        return {}
    statuses = Counter(str(row.get("outcome_status", "noise")) for row in rows)
    confirmed = statuses.get("confirmed", 0)
    duplicate = statuses.get("duplicate", 0)
    stale = statuses.get("stale", 0)
    unverified = statuses.get("unverified", 0)
    refuted = statuses.get("refuted", 0)
    noise = statuses.get("noise", 0)
    score_values = [_safe_float(row.get("final_score")) for row in rows if _safe_float(row.get("final_score")) > 0]
    avg_score = sum(score_values) / len(score_values) if score_values else 0.0
    entity_covered = sum(1 for row in rows if row.get("entities") or row.get("symbols"))
    topic_covered = sum(1 for row in rows if row.get("topics"))
    latency_values = [_safe_float(row.get("latency_seconds")) for row in rows]
    precision_denominator = max(1, total - duplicate)
    precision = confirmed / precision_denominator
    return {
        "total": total,
        "confirmed": confirmed,
        "refuted": refuted,
        "stale": stale,
        "unverified": unverified,
        "duplicate": duplicate,
        "noise": noise,
        "source_precision": round(precision, 4),
        "duplicate_rate": round(duplicate / total, 4),
        "stale_rate": round(stale / total, 4),
        "unverified_rate": round(unverified / total, 4),
        "false_or_refuted_rate": round(refuted / total, 4),
        "noise_rate": round(noise / total, 4),
        "avg_latency_seconds": round(sum(latency_values) / len(latency_values), 2) if latency_values else None,
        "entity_coverage": round(entity_covered / total, 4),
        "topic_coverage": round(topic_covered / total, 4),
        "topic_signal_quality": round(clamp(0.65 * precision + 0.35 * clamp(avg_score / 100.0)), 4),
        "market_impact_after_5m": None,
        "market_impact_after_30m": None,
        "market_impact_after_1d": None,
    }


def _rank_quality(metrics_by_name: dict[str, dict[str, Any]], *, reverse: bool) -> list[dict[str, Any]]:
    rows = [
        {"name": name, **metrics}
        for name, metrics in metrics_by_name.items()
        if int(metrics.get("total", 0)) > 0
    ]
    return sorted(
        rows,
        key=lambda item: (
            _safe_float(item.get("topic_signal_quality")),
            _safe_float(item.get("source_precision")),
            int(item.get("total", 0)),
        ),
        reverse=reverse,
    )


def _candidate(
    *,
    action: str,
    target_type: str,
    target: str,
    reason: str,
    metrics: dict[str, Any],
    confidence: float,
) -> dict[str, Any]:
    candidate_id = stable_id("news-learning", action, target_type, target, json.dumps(metrics, sort_keys=True))
    return {
        "candidate_id": candidate_id,
        "action": action,
        "target_type": target_type,
        "target": target,
        "reason": reason,
        "metrics": metrics,
        "confidence": round(clamp(confidence), 3),
        "status": "research_only",
        "promotion_gate": "manual_codex_review_required",
        "allowed_to_auto_apply": False,
        "blocked_scopes": ["live/news production config", "stock system", "crypto system"],
    }


def _iter_event_payloads(report: dict[str, Any]) -> Iterable[dict[str, Any]]:
    seen = set()
    for section in ("top_events", "positive_catalysts", "negative_risks", "watchlist"):
        value = report.get(section, [])
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            key = str(item.get("cluster_id") or item.get("headline") or id(item))
            section_key = f"{section}:{key}"
            if section_key in seen:
                continue
            seen.add(section_key)
            yield item


def _iter_tech_signals(report: dict[str, Any]) -> Iterable[dict[str, Any]]:
    feature_blocks = report.get("feature_blocks", {})
    if not isinstance(feature_blocks, dict):
        feature_blocks = {}
    tech_block = report.get("tech_block") or feature_blocks.get("tech_block", {})
    if not isinstance(tech_block, dict):
        return []
    signals = tech_block.get("signals", [])
    if not isinstance(signals, list):
        return []
    return (item for item in signals if isinstance(item, dict))


def _claim_text(row: dict[str, Any]) -> str:
    title = str(row.get("title") or "").strip()
    summary = str(row.get("raw_summary") or "").strip()
    if summary and summary != title:
        return f"{title}：{summary}"[:600]
    return title[:600]


def _claim_confidence(row: dict[str, Any]) -> float:
    confidence = 0.35 + _safe_float(row.get("source_trust")) * 0.35
    if row.get("entities") or row.get("symbols"):
        confidence += 0.12
    if row.get("topics"):
        confidence += 0.08
    if str(row.get("event_type")) not in {"", "unknown"}:
        confidence += 0.08
    if len(row.get("source_ids_in_cluster", [])) >= 2:
        confidence += 0.1
    return clamp(confidence, 0.05, 0.98)


def _is_noise(row: dict[str, Any], claim: dict[str, Any]) -> bool:
    if row.get("entities") or row.get("symbols"):
        return False
    if str(row.get("event_type")) not in {"", "unknown"} and row.get("topics"):
        return False
    return _safe_float(claim.get("confidence")) < 0.58


def _infer_event_type(text: str) -> str:
    lowered = text.lower()
    for event_type, patterns in CLAIM_EVENT_PATTERNS.items():
        if any(pattern.lower() in lowered for pattern in patterns):
            return event_type
    return "unknown"


def _source_trust(source_id: str) -> float:
    normalized = source_id.strip().lower()
    if normalized in SOURCE_TRUST_HINTS:
        return SOURCE_TRUST_HINTS[normalized]
    if _is_official_source(normalized):
        return 0.9
    if normalized in SOCIAL_SOURCE_IDS:
        return 0.45
    return 0.65


def _is_official_source(source_id: str) -> bool:
    lowered = source_id.strip().lower()
    return (lowered in SOURCE_TRUST_HINTS and SOURCE_TRUST_HINTS[lowered] >= 0.88) or lowered.startswith(
        OFFICIAL_SOURCE_PREFIXES
    )


def _latency_seconds(published_at: str, fetched_at: datetime) -> int:
    try:
        published = parse_datetime(published_at)
    except Exception:
        return 0
    return max(0, int((fetched_at - published).total_seconds()))


def _duplicate_key(row: dict[str, Any]) -> str:
    url = str(row.get("url") or "").strip().lower()
    if url:
        return url
    tokens = tokenize(str(row.get("title") or ""))
    return " ".join(tokens[:12])


def _symbolish_entities(entities: list[str]) -> list[str]:
    symbols = []
    for entity in entities:
        match = re.fullmatch(r"\d{5,6}(?:\.(?:SH|SZ|HK))?", entity.strip(), flags=re.IGNORECASE)
        if match:
            symbols.append(entity.strip())
    return symbols


def _guess_language(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"


def _safe_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected object JSON: {path}")
    return payload


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
