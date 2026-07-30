from __future__ import annotations

from dataclasses import dataclass
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlparse

from market_news.common import utcnow
from market_news.services.model_judgement import (
    ModelCallBudget,
    ModelJudgementCache,
    ModelJudgementConfig,
    ClaudeCliJsonClient,
    OpenClawAgentJsonClient,
)
from market_news.services.reporting import refresh_runtime_status_views
from market_news.services.unknown_term_detector import UnknownTermDetector


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


LEXICON_AI_REVIEW_INSTRUCTIONS = """
你是港A科技新闻系统的词库审核助手。你的任务不是把所有新词都收进去，而是帮用户减少人工审核负担。

判断原则：
1. 只有能稳定帮助识别科技主题、产业链、催化类型、政策链条、风险类型、公司实体的词，才建议 add。
2. 泛词、口水词、短期噪声、媒体标题残片、普通动词、没有独立投资含义的词，建议 reject。
3. 暂时看不清但可能有价值的词，也先 reject。系统坚持“宁可少收、准收”，不把不确定性留给用户点。
4. term_type 只能从 theme、tech、catalyst、policy、risk、company 中选择。
5. 交易表现泛词（涨幅、跌幅、开盘、收盘等）、普通职位/人物/媒体来源/外交表述，一般 reject；系统已有价格异动字段，不需要把它们放入词库。
6. 不要为了扩大词库而收录；宁可少收、准收。

只输出 JSON object，格式为：
{"reviews":[{"term":"原词","action":"add|reject","term_type":"theme|tech|catalyst|policy|risk|company","confidence":0.0,"reason":"一句中文理由"}]}
"""


AI_AUTO_REVIEW_FORCE_REJECT_TERMS = {
    "涨幅",
    "跌幅",
    "上涨",
    "下跌",
    "开盘",
    "收盘",
    "早盘",
    "午盘",
    "尾盘",
    "盘中",
    "盘前",
    "盘后",
    "走势",
    "取得",
    "人民币",
    "地区",
    "当前",
    "空袭",
    "回应",
    "声明",
    "讨论",
    "承诺",
    "达成",
    "希望",
    "暂无",
}

AI_AUTO_REVIEW_FORCE_REJECT_FRAGMENTS = {
    "外交部",
    "外交部长",
    "总理",
    "央视",
    "记者",
    "报道称",
}


MANUAL_NEWS_ANALYSIS_INSTRUCTIONS = """
你是一个偏基本面和估值框架的二级市场分析助手。用户会贴一段新闻、公告或网页文字，你要判断这条信息是否值得继续研究，以及可能影响哪些港股、A股、美股或金融产品。

分析原则：
1. 优先看基本面变量：收入、利润、毛利率、现金流、订单、客户、产能、价格、政策需求、竞争格局、资本开支、资产负债表。
2. 区分领先信号和事后信号：已经大涨大跌后的报道价值低；订单流失、价格变化、政策需求、业绩指引变化等更有预测价值。
3. 对估值有帮助才重要：能改变长期收入、利润率、现金流、ROE、成长空间或风险折现率的消息优先。
4. 不要把弱相关消息硬贴热门题材；如果证据不足，要明确写“证据不足”。
5. 微博/雪球/论坛观点只能作为热度参考，不能当主证据。

只输出 JSON object，格式为：
{
  "worth_attention": true,
  "attention_score": 0,
  "summary": "一句话概括",
  "impact_logic": ["为什么影响基本面或估值"],
  "affected_assets": [{"symbol":"代码或未知","name":"名称","market":"CN-A|HK|US|unknown","direction":"positive|negative|neutral","reason":"影响链"}],
  "watch_points": ["后续要验证什么"],
  "missing_evidence": ["还缺什么关键证据"],
  "conclusion": "给用户的一句话结论",
  "confidence": 0.0
}
"""


class OpenClawReviewAssistant:
    """Backend for the manual "ask the AI about this news" panel.

    Name kept for import compatibility; it is no longer OpenClaw-only. Tries the
    Claude CLI first and falls back to OpenClaw, so the panel keeps working
    whichever backend is alive.
    """

    def __init__(self, *, config_path: Path, project_root: Path) -> None:
        config = ModelJudgementConfig.from_file(config_path, project_root=project_root)
        cache = ModelJudgementCache(config.cache_path, ttl_hours=config.cache_ttl_hours)
        # Keep manual/lexicon review on a separate, explicit budget so it cannot
        # crowd out the main news judgement lane when we raise news capacity.
        manual_limit = max(1, int(os.environ.get("MARKET_NEWS_MANUAL_AI_DAILY_LIMIT", "40") or "40"))
        review_budget_path = Path(
            os.environ.get("MARKET_NEWS_REVIEW_AI_BUDGET_PATH", "").strip()
            or str((config.budget_path or project_root / "data" / "model_judgement_budget.json").with_name("review_api_model_budget.json"))
        ).expanduser()
        if not review_budget_path.is_absolute():
            review_budget_path = project_root / review_budget_path
        budget = ModelCallBudget(review_budget_path, daily_limit=manual_limit)
        self.clients = [
            ClaudeCliJsonClient(config, cache, budget),
            OpenClawAgentJsonClient(config, cache, budget),
        ]

    @property
    def client(self):  # backwards compatible accessor
        for candidate in self.clients:
            if candidate.available:
                return candidate
        return self.clients[0]

    def run_json(self, *, kind: str, instructions: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        for candidate in self.clients:
            if not candidate.available:
                continue
            result = candidate.run_task_json(kind=kind, instructions=instructions, payload=payload)
            if result is not None:
                return result
        return None


@dataclass(slots=True)
class ReviewApiStateWriter:
    status_path: Path
    history_path: Path

    def write(
        self,
        *,
        host: str,
        port: int,
        overall_status: str,
        detail: str,
        lexicon_path: Path,
        discovery_path: Path,
        last_action: str | None = None,
        errors: list[str] | None = None,
    ) -> dict[str, object]:
        payload = {
            "timestamp": utcnow().isoformat(),
            "overall_status": overall_status,
            "artifacts": {
                "lexicon": str(lexicon_path),
                "discovery_file": str(discovery_path),
            },
            "modules": [
                {
                    "name": "lexicon_review_api",
                    "status": overall_status,
                    "detail": detail,
                    "host": host,
                    "port": port,
                    "last_action": last_action or "",
                }
            ],
            "errors": list(errors or []),
        }
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return payload


class LexiconReviewService:
    def __init__(
        self,
        *,
        lexicon_path: Path,
        discovery_path: Path,
        report_path: Path,
        tech_block_config: dict[str, Any] | None = None,
        status_writer: ReviewApiStateWriter | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        min_score: float = 2.0,
        list_limit: int = 100,
        ai_client: Any | None = None,
        auto_review_enabled: bool = False,
        auto_review_interval_seconds: int = 900,
        auto_review_batch_limit: int = 40,
        auto_review_max_batches_per_cycle: int = 3,
        auto_review_min_add_confidence: float = 0.65,
    ) -> None:
        self.lexicon_path = lexicon_path
        self.discovery_path = discovery_path
        self.report_path = report_path
        self.detector_config = dict((tech_block_config or {}).get("unknown_term_detector", {}) or {})
        self.status_writer = status_writer
        self.host = host
        self.port = port
        self.min_score = min_score
        self.list_limit = list_limit
        self.ai_client = ai_client
        self.auto_review_enabled = auto_review_enabled
        self.auto_review_interval_seconds = max(60, int(auto_review_interval_seconds or 900))
        self.auto_review_batch_limit = max(1, min(int(auto_review_batch_limit or 40), 40))
        self.auto_review_max_batches_per_cycle = max(1, min(int(auto_review_max_batches_per_cycle or 3), 5))
        self.auto_review_min_add_confidence = max(0.0, min(1.0, float(auto_review_min_add_confidence)))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._auto_review_thread: threading.Thread | None = None
        self._last_action = "ready"

    def pending_payload(self, *, message: str = "") -> dict[str, object]:
        detector = self._build_detector()
        detector.prune_noise(self.discovery_path)
        self._prune_force_reject_terms(detector)
        candidates = detector.list_pending(
            self.discovery_path,
            min_score=self.min_score,
            limit=self.list_limit,
        )
        accepted_terms = self._accepted_terms_payload()
        payload = {
            "ok": True,
            "summary": {
                "pending_count": len(candidates),
                "accepted_count": len(accepted_terms),
                "discovery_path": str(self.discovery_path),
                "lexicon_path": str(self.lexicon_path),
            },
            "candidates": candidates,
            "accepted_terms": accepted_terms,
            "message": message or "待审核队列已刷新。",
        }
        return payload

    def add_term(self, term: str, *, term_type: str = "theme") -> dict[str, object]:
        with self._lock:
            lexicon_payload = self._load_lexicon()
            detector = self._build_detector(lexicon_payload)
            candidates = detector.load(self.discovery_path)
            candidate = next(
                (
                    item
                    for item in candidates
                    if str(item.get("text", "")).strip().lower() == term.strip().lower()
                ),
                None,
            )
            if candidate is None:
                raise ValueError(f"Candidate not found: {term}")

            known_terms = {
                str(item.get("canonical_text", "")).strip().lower()
                for item in lexicon_payload
                if str(item.get("canonical_text", "")).strip()
            }
            known_terms.update(
                str(synonym).strip().lower()
                for item in lexicon_payload
                for synonym in item.get("synonyms", [])
                if str(synonym).strip()
            )

            if term.strip().lower() not in known_terms:
                lexicon_payload.append(detector.build_lexicon_entry(candidate, term_type=term_type))
                _write_json(self.lexicon_path, lexicon_payload)

            detector.set_status(self.discovery_path, term, "accepted")
            self._last_action = f"accepted {term} as {term_type}"
            self._touch_status("ok", self._last_action)
            return self.pending_payload(message=f"已收录：{term}（{term_type}）")

    def reject_term(self, term: str) -> dict[str, object]:
        with self._lock:
            detector = self._build_detector()
            ok = detector.set_status(self.discovery_path, term, "rejected")
            if not ok:
                raise ValueError(f"Candidate not found: {term}")
            self._last_action = f"rejected {term}"
            self._touch_status("ok", self._last_action)
            return self.pending_payload(message=f"已忽略：{term}")

    def remove_term(self, term: str) -> dict[str, object]:
        with self._lock:
            lexicon_payload = self._load_lexicon()
            lowered = term.strip().lower()
            filtered = [
                item
                for item in lexicon_payload
                if not self._term_matches_entry(item, lowered)
            ]
            if len(filtered) == len(lexicon_payload):
                raise ValueError(f"Lexicon term not found: {term}")
            _write_json(self.lexicon_path, filtered)
            detector = self._build_detector(filtered)
            detector.set_status(self.discovery_path, term, "rejected")
            self._last_action = f"removed {term}"
            self._touch_status("ok", self._last_action)
            return self.pending_payload(message=f"已从正式词库删除：{term}")

    def ai_review_pending_terms(self, *, limit: int = 20) -> dict[str, object]:
        return self.ai_autoreview_pending_terms(limit=limit)

    def ai_autoreview_pending_terms(self, *, limit: int = 40) -> dict[str, object]:
        base_payload = self.pending_payload(message="待审核队列已刷新。")
        candidates = list(base_payload.get("candidates", []))
        selected = [
            {
                "term": str(item.get("text", "")).strip(),
                "raw_freq": item.get("raw_freq", 0),
                "discovery_score": item.get("discovery_score", 0),
                "inferred_impact": item.get("inferred_impact", {}),
                "cooccurrence": item.get("cooccurrence", {}),
                "example_snippets": item.get("example_snippets", [])[:3],
            }
            for item in candidates[: max(1, min(int(limit or self.auto_review_batch_limit), 40))]
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        ]
        if not selected:
            return {**base_payload, "ai_review": [], "message": "当前没有待审核新词。"}
        if self.ai_client is None:
            return {
                **base_payload,
                "ai_review": [],
                "message": "AI自动审核未启用：审核服务没有接入模型客户端。",
            }

        result = self.ai_client.run_json(
            kind="lexicon-review",
            instructions=LEXICON_AI_REVIEW_INSTRUCTIONS,
            payload={
                "task": "lexicon_review",
                "candidates": selected,
                "output_contract": {
                    "reviews": [
                        {
                            "term": "原词",
                            "action": "add|reject",
                            "term_type": "theme|tech|catalyst|policy|risk|company",
                            "confidence": "0-1",
                            "reason": "一句中文理由",
                        }
                    ]
                },
            },
        )
        if not result:
            return {
                **base_payload,
                "ai_review": [],
                "message": "AI自动审核暂时没跑起来：可能是模型预算用完、OpenClaw未响应或网络刚恢复。",
            }

        reviews = self._normalized_ai_reviews(result.get("reviews", []))
        applied = self._apply_ai_lexicon_reviews(reviews, candidates)
        refreshed_payload = self.pending_payload(
            message=(
                "AI自动审核完成："
                f"收录 {applied['accepted']} 个，删除 {applied['rejected']} 个，"
                f"跳过 {applied['skipped']} 个。以后新增词会由后台继续自动处理。"
            )
        )
        self._last_action = (
            "ai auto-reviewed "
            f"{len(reviews)} lexicon candidates; accepted={applied['accepted']}; rejected={applied['rejected']}"
        )
        self._touch_status("ok", self._last_action)
        return {
            **refreshed_payload,
            "ai_review": reviews,
            "auto_review": applied,
        }

    def analyze_news_text(self, *, text: str, question: str = "") -> dict[str, object]:
        cleaned_text = text.strip()
        if len(cleaned_text) < 10:
            raise ValueError("请先粘贴或拖入一段新闻/公告文字。")
        if self.ai_client is None:
            raise ValueError("AI分析未启用：审核服务没有接入模型客户端。")
        result = self.ai_client.run_json(
            kind="manual-news-analysis",
            instructions=MANUAL_NEWS_ANALYSIS_INSTRUCTIONS,
            payload={
                "task": "manual_news_analysis",
                "question": question.strip()[:800],
                "text": cleaned_text[:16000],
                "output_contract": {
                    "worth_attention": "boolean",
                    "attention_score": "0-100",
                    "summary": "一句话概括",
                    "impact_logic": ["为什么影响基本面或估值"],
                    "affected_assets": [
                        {
                            "symbol": "代码或未知",
                            "name": "名称",
                            "market": "CN-A|HK|US|unknown",
                            "direction": "positive|negative|neutral",
                            "reason": "影响链",
                        }
                    ],
                    "watch_points": ["后续要验证什么"],
                    "missing_evidence": ["还缺什么关键证据"],
                    "conclusion": "给用户的一句话结论",
                    "confidence": "0-1",
                },
            },
        )
        if not result:
            raise ValueError("AI分析暂时没跑起来：可能是模型预算用完、OpenClaw未响应或网络刚恢复。")
        self._last_action = "manual news analysis"
        self._touch_status("ok", self._last_action)
        return {"ok": True, "analysis": result}

    def health_payload(self) -> dict[str, object]:
        return {
            "ok": True,
            "status": "ok",
            "host": self.host,
            "port": self.port,
            "last_action": self._last_action,
        }

    @staticmethod
    def _normalized_ai_reviews(value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, object]] = []
        valid_actions = {"add", "reject", "hold"}
        valid_types = {"theme", "tech", "catalyst", "policy", "risk", "company"}
        for item in value:
            if not isinstance(item, dict):
                continue
            term = str(item.get("term", "")).strip()
            if not term:
                continue
            action = str(item.get("action", "hold")).strip().lower()
            term_type = str(item.get("term_type", "theme")).strip().lower()
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0) or 0)))
            except (TypeError, ValueError):
                confidence = 0.0
            normalized.append(
                {
                    "term": term,
                    "action": action if action in valid_actions else "hold",
                    "term_type": term_type if term_type in valid_types else "theme",
                    "confidence": round(confidence, 3),
                    "reason": str(item.get("reason", "")).strip()[:240],
                }
            )
        return normalized

    def _apply_ai_lexicon_reviews(
        self,
        reviews: list[dict[str, object]],
        candidates: list[object],
    ) -> dict[str, object]:
        candidate_by_term = {
            str(item.get("text", "")).strip().lower(): item
            for item in candidates
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        }
        stats: dict[str, object] = {
            "accepted": 0,
            "rejected": 0,
            "skipped": 0,
            "results": [],
        }
        with self._lock:
            lexicon_payload = self._load_lexicon()
            detector = self._build_detector(lexicon_payload)
            known_terms = self._known_terms(lexicon_payload)
            lexicon_changed = False
            for review in reviews:
                term = str(review.get("term", "")).strip()
                term_key = term.lower()
                candidate = candidate_by_term.get(term_key)
                if not term or candidate is None:
                    stats["skipped"] = int(stats["skipped"]) + 1
                    continue

                action = str(review.get("action", "reject")).strip().lower()
                confidence = float(review.get("confidence", 0.0) or 0.0)
                term_type = str(review.get("term_type", "theme") or "theme").strip().lower()
                reason = str(review.get("reason", "")).strip()
                force_reject_reason = self._force_reject_ai_term_reason(term)
                if force_reject_reason:
                    final_status = "rejected"
                    final_reason = force_reject_reason
                elif action != "add":
                    final_status = "rejected"
                    final_reason = reason or "AI判断不适合进入正式词库。"
                elif confidence < self.auto_review_min_add_confidence:
                    final_status = "rejected"
                    final_reason = (
                        f"AI建议收录但置信度 {confidence:.2f} 低于 "
                        f"{self.auto_review_min_add_confidence:.2f}，按准入规则拒绝。"
                    )
                else:
                    final_status = "accepted"
                    final_reason = reason or "AI判断可稳定帮助识别科技主题或投资链条。"

                if final_status == "accepted" and term_key not in known_terms:
                    entry = detector.build_lexicon_entry(candidate, term_type=term_type)
                    entry["review_meta"] = {
                        "source": "ai-auto",
                        "confidence": round(confidence, 3),
                        "reason": final_reason[:240],
                        "reviewed_at": utcnow().isoformat(),
                    }
                    lexicon_payload.append(entry)
                    known_terms.update(self._entry_terms(entry))
                    lexicon_changed = True

                detector.set_status(
                    self.discovery_path,
                    term,
                    final_status,
                    metadata={
                        "ai_review_action": action if action in {"add", "reject", "hold"} else "reject",
                        "ai_review_term_type": term_type,
                        "ai_review_confidence": round(confidence, 3),
                        "ai_review_reason": final_reason[:240],
                        "ai_reviewed_at": utcnow().isoformat(),
                    },
                )
                if final_status == "accepted":
                    stats["accepted"] = int(stats["accepted"]) + 1
                else:
                    stats["rejected"] = int(stats["rejected"]) + 1
                cast_results = stats["results"]
                if isinstance(cast_results, list):
                    cast_results.append(
                        {
                            "term": term,
                            "status": final_status,
                            "action": action,
                            "term_type": term_type,
                            "confidence": round(confidence, 3),
                            "reason": final_reason[:240],
                        }
                    )
            if lexicon_changed:
                _write_json(self.lexicon_path, lexicon_payload)
        return stats

    def start_heartbeat(self, *, interval_seconds: int = 60) -> None:
        if self.status_writer is None or self._heartbeat_thread is not None:
            return

        def loop() -> None:
            while not self._stop_event.wait(interval_seconds):
                self._touch_status("ok", f"listening on {self.host}:{self.port}")

        self._touch_status("ok", f"listening on {self.host}:{self.port}")
        self._heartbeat_thread = threading.Thread(target=loop, daemon=True, name="lexicon-review-heartbeat")
        self._heartbeat_thread.start()
        self._start_auto_review_worker()

    def stop_heartbeat(self) -> None:
        self._stop_event.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)
            self._heartbeat_thread = None
        if self._auto_review_thread is not None:
            self._auto_review_thread.join(timeout=1.0)
            self._auto_review_thread = None

    def _start_auto_review_worker(self) -> None:
        if not self.auto_review_enabled or self.ai_client is None or self._auto_review_thread is not None:
            return

        def loop() -> None:
            while not self._stop_event.wait(15):
                try:
                    for _ in range(self.auto_review_max_batches_per_cycle):
                        payload = self.ai_autoreview_pending_terms(limit=self.auto_review_batch_limit)
                        summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
                        auto_review = payload.get("auto_review", {}) if isinstance(payload, dict) else {}
                        pending_count = int(summary.get("pending_count", 0) or 0) if isinstance(summary, dict) else 0
                        changed = (
                            int(auto_review.get("accepted", 0) or 0)
                            + int(auto_review.get("rejected", 0) or 0)
                            if isinstance(auto_review, dict)
                            else 0
                        )
                        if pending_count <= 0 or changed <= 0:
                            break
                except Exception as exc:  # pragma: no cover - defensive runtime guard
                    self._last_action = "ai auto-review failed"
                    self._touch_status("degraded", "AI自动审核失败", errors=[str(exc)])
                if self._stop_event.wait(self.auto_review_interval_seconds):
                    break

        self._auto_review_thread = threading.Thread(target=loop, daemon=True, name="lexicon-auto-review")
        self._auto_review_thread.start()

    def _touch_status(self, overall_status: str, detail: str, *, errors: list[str] | None = None) -> None:
        if self.status_writer is None:
            return
        self.status_writer.write(
            host=self.host,
            port=self.port,
            overall_status=overall_status,
            detail=detail,
            lexicon_path=self.lexicon_path,
            discovery_path=self.discovery_path,
            last_action=self._last_action,
            errors=errors,
        )
        refresh_runtime_status_views(self.report_path)

    def _load_lexicon(self) -> list[dict[str, Any]]:
        payload = _load_json(self.lexicon_path)
        if not isinstance(payload, list):
            raise ValueError(f"Lexicon file must be a JSON array: {self.lexicon_path}")
        return payload

    def _build_detector(self, lexicon_payload: list[dict[str, Any]] | None = None) -> UnknownTermDetector:
        payload = lexicon_payload if lexicon_payload is not None else self._load_lexicon()
        return UnknownTermDetector(lexicon=payload, config=self.detector_config)

    @staticmethod
    def _term_matches_entry(entry: dict[str, Any], lowered: str) -> bool:
        canonical = str(entry.get("canonical_text", "")).strip().lower()
        if canonical and canonical == lowered:
            return True
        for synonym in entry.get("synonyms", []):
            if str(synonym).strip().lower() == lowered:
                return True
        return False

    @staticmethod
    def _entry_terms(entry: dict[str, Any]) -> set[str]:
        terms = {str(entry.get("canonical_text", "")).strip().lower()}
        terms.update(str(synonym).strip().lower() for synonym in entry.get("synonyms", []) if str(synonym).strip())
        return {term for term in terms if term}

    def _known_terms(self, lexicon_payload: list[dict[str, Any]]) -> set[str]:
        known_terms: set[str] = set()
        for entry in lexicon_payload:
            known_terms.update(self._entry_terms(entry))
        return known_terms

    def _prune_force_reject_terms(self, detector: UnknownTermDetector) -> int:
        updated = 0
        for row in detector.load(self.discovery_path):
            if str(row.get("status", "pending")) != "pending":
                continue
            term = str(row.get("text", "")).strip()
            reason = self._force_reject_ai_term_reason(term)
            if not reason:
                continue
            if detector.set_status(
                self.discovery_path,
                term,
                "rejected",
                metadata={
                    "ai_review_action": "reject",
                    "ai_review_confidence": 1.0,
                    "ai_review_reason": reason,
                    "ai_reviewed_at": utcnow().isoformat(),
                },
            ):
                updated += 1
        return updated

    @staticmethod
    def _force_reject_ai_term_reason(term: str) -> str:
        normalized = term.strip()
        if normalized in AI_AUTO_REVIEW_FORCE_REJECT_TERMS:
            return "硬规则拒绝：这是交易表现或标题泛词，系统已有结构化字段处理，不进入词库。"
        if any(fragment in normalized for fragment in AI_AUTO_REVIEW_FORCE_REJECT_FRAGMENTS):
            return "硬规则拒绝：这是普通媒体/职位/外交表述，容易扩大噪声，不进入词库。"
        return ""

    def _accepted_terms_payload(self) -> list[dict[str, Any]]:
        lexicon_payload = self._load_lexicon()
        return sorted(
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


def make_review_api_handler(service: LexiconReviewService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def end_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            super().end_headers()

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.end_headers()

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                self._send_json(service.health_payload())
                return
            if path == "/api/lexicon/pending":
                self._send_json(service.pending_payload())
                return
            self._send_json({"ok": False, "error": "not-found"}, status_code=404)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            payload = self._read_json_body()
            try:
                if path == "/api/lexicon/add":
                    term = str(payload.get("term", "")).strip()
                    term_type = str(payload.get("term_type", "theme") or "theme").strip()
                    if not term:
                        raise ValueError("Missing term")
                    self._send_json(service.add_term(term, term_type=term_type))
                    return
                if path == "/api/lexicon/reject":
                    term = str(payload.get("term", "")).strip()
                    if not term:
                        raise ValueError("Missing term")
                    self._send_json(service.reject_term(term))
                    return
                if path == "/api/lexicon/remove":
                    term = str(payload.get("term", "")).strip()
                    if not term:
                        raise ValueError("Missing term")
                    self._send_json(service.remove_term(term))
                    return
                if path == "/api/lexicon/ai-review":
                    limit = int(payload.get("limit", 20) or 20)
                    self._send_json(service.ai_review_pending_terms(limit=limit))
                    return
                if path == "/api/ai/analyze-news":
                    text = str(payload.get("text", "")).strip()
                    question = str(payload.get("question", "")).strip()
                    self._send_json(service.analyze_news_text(text=text, question=question))
                    return
                self._send_json({"ok": False, "error": "not-found"}, status_code=404)
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status_code=400)
            except Exception as exc:  # pragma: no cover - defensive
                service._touch_status("error", "review api request failed", errors=[str(exc)])
                self._send_json({"ok": False, "error": str(exc)}, status_code=500)

        def _read_json_body(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "0").strip()
            length = int(raw_length) if raw_length.isdigit() else 0
            if length <= 0:
                return {}
            body = self.rfile.read(length)
            if not body:
                return {}
            payload = json.loads(body.decode("utf-8"))
            return payload if isinstance(payload, dict) else {}

        def _send_json(self, payload: dict[str, Any], *, status_code: int = 200) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def serve_review_api(
    service: LexiconReviewService,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    heartbeat_interval: int = 60,
) -> None:
    server = ThreadingHTTPServer((host, port), make_review_api_handler(service))
    service.host = host
    service.port = port
    service.start_heartbeat(interval_seconds=heartbeat_interval)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        service.stop_heartbeat()
        server.server_close()
