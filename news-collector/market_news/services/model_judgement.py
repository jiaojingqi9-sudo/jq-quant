from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
from urllib import error, request

from market_news.common import clamp, unique_preserve, utcnow
from market_news.domain.models import (
    Direction,
    EventCluster,
    EventType,
    ImpactAssessment,
    InstrumentDescriptor,
    InstrumentMatch,
    Market,
)
from market_news.services.fundamental_focus import (
    FUNDAMENTAL_TEXT_PATTERNS,
    BALANCE_SHEET_PATTERNS,
    CAPACITY_UTILIZATION_PATTERNS,
    COMPETITIVE_POSITION_PATTERNS,
    CUSTOMER_SUPPLIER_PATTERNS,
    GUIDANCE_REVISION_PATTERNS,
    LEADING_FUNDAMENTAL_PATTERNS,
    LAGGING_PRICE_REACTION_PATTERNS,
    LOW_PREDICTABILITY_RISK_PATTERNS,
    PRICE_MARGIN_PATTERNS,
    POLICY_DEMAND_PATTERNS,
    AttentionGateDecision,
    evaluate_model_call_gate,
)


MODEL_SCREENING_TRIGGER_PATTERNS = [
    "增长",
    "下降",
    "扭亏",
    "预增",
    "预减",
    "亏损",
    "净利润",
    "营收",
    "订单",
    "合同",
    "中标",
    "并购",
    "收购",
    "重组",
    "资产注入",
    "回购",
    "增持",
    "减持",
    "停牌",
    "复牌",
    "制裁",
    "出口管制",
    "关税",
    "涨价",
    "降价",
    "产能",
    "投产",
    "扩产",
    "政策",
    "规划",
    "通知",
    "人工智能",
    "半导体",
    "芯片",
    "算力",
    "机器人",
    "新材料",
    "固态电池",
    "低空经济",
    "量子",
    "医保",
    "集采",
    "电力负荷",
    "breakthrough",
    "sanction",
    "tariff",
    "acquisition",
    "merger",
    "earnings",
    "guidance",
    "指引",
    "客户",
    "供应链",
    "毛利率",
    "价格",
    "库存",
]


ROUTINE_DISCLOSURE_PATTERNS = [
    "年度股东大会",
    "股东周年大会",
    "会议通知",
    "会议资料",
    "董事会决议",
    "监事会决议",
    "独立董事",
    "续聘会计师",
    "会计师事务所履职",
    "内部控制",
    "内控",
    "公司章程",
    "管理制度",
    "累积投票",
    "信息披露",
    "ESG报告",
    "社会责任报告",
    "环境、社会及公司治理",
    "分红派息实施",
    "权益分派实施",
    "主做市服务",
    "做市服务",
    "一般授权",
    "重选董事",
    "unclaimed dividend",
    "annual general meeting",
    "general mandate",
    "re-election",
    "reappoint",
    "charity",
]


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _load_secret_from_env_file(name: str) -> str:
    path = Path.home() / ".market_news" / "openai_env"
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != name:
            continue
        return value.strip().strip("\"'")
    return ""


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class ModelJudgementConfig:
    enabled: bool = True
    screening_enabled: bool = True
    asset_mapping_enabled: bool = True
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str = "https://api.openai.com/v1"
    screening_model: str = "gpt-4.1-mini"
    asset_model: str = "gpt-4.1"
    request_timeout: int = 45
    max_text_chars: int = 9000
    max_documents: int = 5
    min_screening_source_trust: float = 0.82
    min_asset_confidence: float = 0.52
    min_asset_exposure: float = 0.45
    max_asset_candidates: int = 8
    cache_ttl_hours: int = 72
    cache_path: Path | None = None
    model_daily_call_limit: int = 10
    budget_path: Path | None = None
    openclaw_enabled: bool = True
    openclaw_bin: Path = Path.home() / ".openclaw" / "bin" / "openclaw"
    openclaw_session_id: str = "market-news-judge"
    openclaw_timeout: int = 180
    openclaw_max_screening_calls_per_run: int = 1
    openclaw_max_asset_calls_per_run: int = 0
    # ── Claude Code CLI backend ───────────────────────────────────────────────
    # Absolute path matters: launchd jobs run with PATH=/usr/bin:/bin:/usr/sbin:/sbin,
    # which does NOT include /opt/homebrew/bin, so a bare "claude" would not resolve.
    claude_enabled: bool = True
    claude_bin: Path = Path("/opt/homebrew/bin/claude")
    claude_model: str = ""
    # Split by task, mirroring the OpenAI lane: a fast model decides "is this
    # worth attention?", a stronger one reasons about which instruments move.
    claude_screening_model: str = "claude-haiku-4-5-20251001"
    claude_asset_model: str = ""
    claude_timeout: int = 180
    claude_max_screening_calls_per_run: int = 8
    claude_max_asset_calls_per_run: int = 3
    evidence_source_ids: set[str] = field(default_factory=set)
    social_source_ids: set[str] = field(default_factory=lambda: {"weibo", "xueqiu"})
    excluded_source_ids: set[str] = field(default_factory=set)

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip() or _load_secret_from_env_file(self.api_key_env)

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_key)

    @classmethod
    def from_file(cls, path: Path, *, project_root: Path) -> "ModelJudgementConfig":
        payload: dict[str, Any] = {}
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded

        cache_path = payload.get("cache_path")
        resolved_cache_path = None
        if isinstance(cache_path, str) and cache_path.strip():
            resolved_cache_path = Path(cache_path).expanduser()
            if not resolved_cache_path.is_absolute():
                resolved_cache_path = project_root / resolved_cache_path
        budget_path = payload.get("budget_path")
        resolved_budget_path = None
        if isinstance(budget_path, str) and budget_path.strip():
            resolved_budget_path = Path(budget_path).expanduser()
            if not resolved_budget_path.is_absolute():
                resolved_budget_path = project_root / resolved_budget_path

        config = cls(
            enabled=bool(payload.get("enabled", True)),
            screening_enabled=bool(payload.get("screening_enabled", True)),
            asset_mapping_enabled=bool(payload.get("asset_mapping_enabled", True)),
            api_key_env=str(payload.get("api_key_env", "OPENAI_API_KEY")),
            base_url=str(payload.get("base_url", "https://api.openai.com/v1")).rstrip("/"),
            screening_model=str(payload.get("screening_model", "gpt-4.1-mini")),
            asset_model=str(payload.get("asset_model", "gpt-4.1")),
            request_timeout=int(payload.get("request_timeout", 45)),
            max_text_chars=int(payload.get("max_text_chars", 9000)),
            max_documents=int(payload.get("max_documents", 5)),
            min_screening_source_trust=float(payload.get("min_screening_source_trust", 0.82)),
            min_asset_confidence=float(payload.get("min_asset_confidence", 0.52)),
            min_asset_exposure=float(payload.get("min_asset_exposure", 0.45)),
            max_asset_candidates=int(payload.get("max_asset_candidates", 8)),
            cache_ttl_hours=int(payload.get("cache_ttl_hours", 72)),
            cache_path=resolved_cache_path or project_root / "data" / "model_judgement_cache.json",
            model_daily_call_limit=int(payload.get("model_daily_call_limit", 10)),
            budget_path=resolved_budget_path or project_root / "data" / "model_judgement_budget.json",
            openclaw_enabled=bool(payload.get("openclaw_enabled", True)),
            openclaw_bin=Path(
                str(payload.get("openclaw_bin") or Path.home() / ".openclaw" / "bin" / "openclaw")
            ).expanduser(),
            openclaw_session_id=str(payload.get("openclaw_session_id", "market-news-judge")),
            openclaw_timeout=int(payload.get("openclaw_timeout", 180)),
            openclaw_max_screening_calls_per_run=int(payload.get("openclaw_max_screening_calls_per_run", 1)),
            openclaw_max_asset_calls_per_run=int(payload.get("openclaw_max_asset_calls_per_run", 0)),
            claude_enabled=bool(payload.get("claude_enabled", True)),
            claude_bin=Path(
                str(payload.get("claude_bin") or "/opt/homebrew/bin/claude")
            ).expanduser(),
            claude_model=str(payload.get("claude_model", "")).strip(),
            claude_screening_model=str(
                payload.get("claude_screening_model", "claude-haiku-4-5-20251001")
            ).strip(),
            claude_asset_model=str(payload.get("claude_asset_model", "")).strip(),
            claude_timeout=int(payload.get("claude_timeout", 180)),
            claude_max_screening_calls_per_run=int(payload.get("claude_max_screening_calls_per_run", 8)),
            claude_max_asset_calls_per_run=int(payload.get("claude_max_asset_calls_per_run", 3)),
            evidence_source_ids={
                str(item).strip().lower()
                for item in payload.get("evidence_source_ids", [])
                if str(item).strip()
            },
            social_source_ids={
                str(item).strip().lower()
                for item in payload.get("social_source_ids", ["weibo", "xueqiu"])
                if str(item).strip()
            },
            excluded_source_ids={
                str(item).strip().lower()
                for item in payload.get("excluded_source_ids", [])
                if str(item).strip()
            },
        )
        config.enabled = _env_flag("MARKET_NEWS_MODEL_JUDGE", config.enabled)
        config.enabled = not _env_flag("MARKET_NEWS_MODEL_JUDGE_DISABLED", not config.enabled)
        config.screening_model = os.environ.get("MARKET_NEWS_SCREENING_MODEL", config.screening_model)
        config.asset_model = os.environ.get("MARKET_NEWS_ASSET_MODEL", config.asset_model)
        daily_limit = os.environ.get("MARKET_NEWS_MODEL_DAILY_LIMIT", "").strip()
        if daily_limit:
            try:
                config.model_daily_call_limit = int(daily_limit)
            except ValueError:
                pass
        return config


class ModelJudgementCache:
    def __init__(self, path: Path | None, *, ttl_hours: int) -> None:
        self.path = path
        self.ttl = timedelta(hours=max(1, ttl_hours))
        self._payload: dict[str, dict[str, Any]] | None = None

    def get(self, key: str) -> dict[str, Any] | None:
        payload = self._load()
        row = payload.get(key)
        if not isinstance(row, dict):
            return None
        try:
            created_at = datetime.fromisoformat(str(row.get("created_at")))
        except Exception:
            return None
        if utcnow() - created_at > self.ttl:
            return None
        value = row.get("value")
        return value if isinstance(value, dict) else None

    def set(self, key: str, value: dict[str, Any]) -> None:
        if self.path is None:
            return
        payload = self._load()
        payload[key] = {
            "created_at": utcnow().isoformat(),
            "value": value,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._payload is not None:
            return self._payload
        if self.path is None or not self.path.exists():
            self._payload = {}
            return self._payload
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            loaded = {}
        self._payload = loaded if isinstance(loaded, dict) else {}
        return self._payload


class ModelCallBudget:
    def __init__(self, path: Path | None, *, daily_limit: int) -> None:
        self.path = path
        self.daily_limit = max(0, int(daily_limit))
        self._payload: dict[str, Any] | None = None

    def reserve(self, kind: str) -> bool:
        if self.daily_limit <= 0:
            return False
        payload = self._load_today()
        used = int(payload.get("used", 0))
        if used >= self.daily_limit:
            return False
        payload["used"] = used + 1
        by_kind = payload.setdefault("by_kind", {})
        if isinstance(by_kind, dict):
            by_kind[kind] = int(by_kind.get(kind, 0)) + 1
        self._save(payload)
        return True

    def _load_today(self) -> dict[str, Any]:
        today = utcnow().date().isoformat()
        payload = self._load()
        if payload.get("date") != today:
            payload = {"date": today, "used": 0, "by_kind": {}}
            self._payload = payload
        return payload

    def _load(self) -> dict[str, Any]:
        if self._payload is not None:
            return self._payload
        if self.path is None or not self.path.exists():
            self._payload = {"date": utcnow().date().isoformat(), "used": 0, "by_kind": {}}
            return self._payload
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            loaded = {}
        self._payload = loaded if isinstance(loaded, dict) else {}
        return self._payload

    def _save(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class OpenAIResponsesJsonClient:
    def __init__(
        self,
        config: ModelJudgementConfig,
        cache: ModelJudgementCache | None = None,
        budget: ModelCallBudget | None = None,
    ) -> None:
        self.config = config
        self.cache = cache or ModelJudgementCache(config.cache_path, ttl_hours=config.cache_ttl_hours)
        self.budget = budget or ModelCallBudget(config.budget_path, daily_limit=config.model_daily_call_limit)

    @property
    def available(self) -> bool:
        return self.config.available

    def screen_event(self, cluster: EventCluster, base_impact: ImpactAssessment) -> dict[str, Any] | None:
        payload = {
            "cluster": _cluster_payload(cluster, self.config),
            "base_rule_assessment": {
                "event_type": base_impact.event_type.value,
                "direction": base_impact.direction.value,
                "severity": base_impact.severity,
                "confidence": base_impact.confidence,
                "matched_rules": base_impact.matched_rules,
                "rationale": base_impact.rationale[:6],
            },
            "output_contract": {
                "worth_attention": "boolean",
                "attention_score": "0-100",
                "event_type": "company|industry|policy|macro|commodity|regulation|unknown",
                "direction": "positive|negative|neutral",
                "severity": "0-1",
                "confidence": "0-1",
                "affected_markets": ["CN-A", "HK", "US"],
                "affected_sectors": ["short sector labels"],
                "affected_themes": ["short theme labels"],
                "catalyst_labels": ["order|policy|earnings|m&a|price|sanction|breakthrough|capacity|regulation|other"],
                "reason": "one concise Chinese sentence",
                "evidence": ["specific facts from the source text"],
                "reject_reason": "if not worth attention",
            },
        }
        key = self._cache_key("screen", self.config.screening_model, payload)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if not self.budget.reserve("openai-screen"):
            return None
        result = self._request_json(
            model=self.config.screening_model,
            instructions=SCREENING_INSTRUCTIONS,
            payload=payload,
        )
        if result:
            self.cache.set(key, result)
        return result

    def map_assets(
        self,
        cluster: EventCluster,
        impact: ImpactAssessment,
        instruments: list[InstrumentDescriptor],
    ) -> dict[str, Any] | None:
        universe = [
            {
                "symbol": item.symbol,
                "market": item.market.value,
                "name": item.name,
                "asset_type": item.asset_type,
                "sectors": item.sectors[:4],
                "themes": item.themes[:6],
                "aliases": item.aliases[:5],
            }
            for item in instruments[:120]
        ]
        payload = {
            "cluster": _cluster_payload(cluster, self.config),
            "event_assessment": {
                "event_type": impact.event_type.value,
                "direction": impact.direction.value,
                "severity": impact.severity,
                "confidence": impact.confidence,
                "themes": impact.affected_themes,
                "sectors": impact.affected_sectors,
                "markets": [market.value for market in impact.affected_markets],
                "model_screening": impact.model_judgement.get("screening", {}),
            },
            "candidate_universe": universe,
            "output_contract": {
                "candidates": [
                    {
                        "symbol": "ticker from candidate_universe or direct source code",
                        "market": "CN-A|HK|US",
                        "name": "company/product name",
                        "direction": "positive|negative|neutral",
                        "exposure_score": "0-1",
                        "confidence": "0-1",
                        "relation": "direct_company|peer|supplier|customer|sector_beta|policy_beneficiary|avoid",
                        "reason": "one concise Chinese sentence with evidence chain",
                    }
                ],
                "notes": "brief warning if evidence is weak",
            },
        }
        key = self._cache_key("assets", self.config.asset_model, payload)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if not self.budget.reserve("openai-assets"):
            return None
        result = self._request_json(
            model=self.config.asset_model,
            instructions=ASSET_MAPPING_INSTRUCTIONS,
            payload=payload,
        )
        if result:
            self.cache.set(key, result)
        return result

    def _request_json(self, *, model: str, instructions: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.available:
            return None
        request_payload = {
            "model": model,
            "instructions": instructions,
            "input": json.dumps(payload, ensure_ascii=False),
        }
        req = request.Request(
            f"{self.config.base_url}/responses",
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.config.request_timeout) as response:
                raw_payload = json.loads(response.read().decode("utf-8"))
        except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError):
            return None
        text = _extract_response_text(raw_payload)
        return _parse_json_object(text)

    def _cache_key(self, kind: str, model: str, payload: dict[str, Any]) -> str:
        compact = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(compact.encode("utf-8")).hexdigest()
        return f"{kind}:{model}:{digest}"


class OpenClawAgentJsonClient:
    def __init__(
        self,
        config: ModelJudgementConfig,
        cache: ModelJudgementCache | None = None,
        budget: ModelCallBudget | None = None,
    ) -> None:
        self.config = config
        self.cache = cache or ModelJudgementCache(config.cache_path, ttl_hours=config.cache_ttl_hours)
        self.budget = budget or ModelCallBudget(config.budget_path, daily_limit=config.model_daily_call_limit)
        self.screening_calls = 0
        self.asset_calls = 0

    # ── overridable knobs (so alternative CLI backends can subclass cleanly) ──
    backend_tag: str = "openclaw"
    backend_name: str = "openclaw-agent"

    @property
    def _max_screening_calls(self) -> int:
        return self.config.openclaw_max_screening_calls_per_run

    @property
    def _max_asset_calls(self) -> int:
        return self.config.openclaw_max_asset_calls_per_run

    @property
    def _session_tag(self) -> str:
        return self.config.openclaw_session_id

    @property
    def available(self) -> bool:
        return (
            self.config.enabled
            and self.config.openclaw_enabled
            and bool(self.config.openclaw_session_id.strip())
            and (
                self.config.openclaw_bin.exists()
                or shutil.which(str(self.config.openclaw_bin)) is not None
                or shutil.which("openclaw") is not None
            )
        )

    def screen_event(self, cluster: EventCluster, base_impact: ImpactAssessment) -> dict[str, Any] | None:
        payload = {
            "task": "screen_event",
            "cluster": _cluster_payload(cluster, self.config),
            "base_rule_assessment": {
                "event_type": base_impact.event_type.value,
                "direction": base_impact.direction.value,
                "severity": base_impact.severity,
                "confidence": base_impact.confidence,
                "matched_rules": base_impact.matched_rules,
                "rationale": base_impact.rationale[:6],
            },
        }
        key = self._cache_key(f"{self.backend_tag}-screen", self._session_tag, payload)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if self.screening_calls >= self._max_screening_calls:
            return None
        if not self.budget.reserve(f"{self.backend_tag}-screen"):
            return None
        self.screening_calls += 1
        result = self._run_json_agent(
            instructions=SCREENING_INSTRUCTIONS + "\n严格只输出 JSON object。",
            payload=payload,
        )
        if result:
            self.cache.set(key, result)
        return result

    def map_assets(
        self,
        cluster: EventCluster,
        impact: ImpactAssessment,
        instruments: list[InstrumentDescriptor],
    ) -> dict[str, Any] | None:
        universe = [
            {
                "symbol": item.symbol,
                "market": item.market.value,
                "name": item.name,
                "asset_type": item.asset_type,
                "sectors": item.sectors[:4],
                "themes": item.themes[:6],
                "aliases": item.aliases[:5],
            }
            for item in instruments[:120]
        ]
        payload = {
            "task": "map_assets",
            "cluster": _cluster_payload(cluster, self.config),
            "event_assessment": {
                "event_type": impact.event_type.value,
                "direction": impact.direction.value,
                "severity": impact.severity,
                "confidence": impact.confidence,
                "themes": impact.affected_themes,
                "sectors": impact.affected_sectors,
                "markets": [market.value for market in impact.affected_markets],
                "model_screening": impact.model_judgement.get("screening", {}),
            },
            "candidate_universe": universe,
        }
        key = self._cache_key(f"{self.backend_tag}-assets", self._session_tag, payload)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if self.asset_calls >= self._max_asset_calls:
            return None
        if not self.budget.reserve(f"{self.backend_tag}-assets"):
            return None
        self.asset_calls += 1
        result = self._run_json_agent(
            instructions=ASSET_MAPPING_INSTRUCTIONS + "\n严格只输出 JSON object。",
            payload=payload,
        )
        if result:
            self.cache.set(key, result)
        return result

    def run_task_json(
        self,
        *,
        kind: str,
        instructions: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Run a manual JSON task through the same cached/budgeted OpenClaw lane."""
        cache_kind = f"{self.backend_tag}-{kind}"
        key = self._cache_key(cache_kind, self._session_tag, payload)
        cached = self.cache.get(key)
        if cached is not None:
            return {**cached, "_model_backend": self.backend_name, "_cached": True}
        if not self.budget.reserve(cache_kind):
            return None
        result = self._run_json_agent(
            instructions=instructions.strip() + "\n严格只输出 JSON object。",
            payload=payload,
        )
        if result:
            self.cache.set(key, result)
            return {**result, "_model_backend": self.backend_name, "_cached": False}
        return None

    def _run_json_agent(self, *, instructions: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.available:
            return None
        openclaw_bin = str(self.config.openclaw_bin)
        if not Path(openclaw_bin).exists():
            openclaw_bin = shutil.which(openclaw_bin) or shutil.which("openclaw") or openclaw_bin
        message = (
            instructions.strip()
            + "\n\nINPUT_JSON:\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        try:
            completed = subprocess.run(
                [
                    openclaw_bin,
                    "agent",
                    "--session-id",
                    self.config.openclaw_session_id,
                    "--message",
                    message,
                    "--json",
                    "--timeout",
                    str(self.config.openclaw_timeout),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.config.openclaw_timeout + 20,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        envelope = _parse_json_object(completed.stdout)
        if not envelope:
            return None
        status = str(envelope.get("status", "ok")).strip().lower()
        if status and status not in {"ok", "success"}:
            return None
        text = _extract_openclaw_text(envelope)
        parsed_text = _parse_json_object(text) if text else None
        if parsed_text:
            return parsed_text
        # Some OpenClaw modes can return the agent JSON directly without the
        # payload envelope. Treat that as a valid result, but do not leak wrapper
        # metadata as a task answer.
        if not any(key in envelope for key in ("payloads", "result", "meta", "status")):
            return envelope
        return None

    def _cache_key(self, kind: str, model: str, payload: dict[str, Any]) -> str:
        compact = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(compact.encode("utf-8")).hexdigest()
        return f"{kind}:{model}:{digest}"


def _resolve_claude_binary(candidate: Path) -> str:
    """Find the claude CLI.

    launchd jobs run with PATH=/usr/bin:/bin:/usr/sbin:/sbin, which does not
    include Homebrew, so ``shutil.which("claude")`` alone is not enough. Try the
    configured absolute path first, then PATH, then the usual install locations.
    """

    try:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    except OSError:
        pass
    found = shutil.which(str(candidate)) or shutil.which("claude")
    if found:
        return found
    for fallback in (
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
        Path.home() / ".local/bin/claude",
        Path.home() / ".claude/local/claude",
    ):
        try:
            if fallback.exists() and os.access(fallback, os.X_OK):
                return str(fallback)
        except OSError:
            continue
    return ""


class ClaudeCliJsonClient(OpenClawAgentJsonClient):
    """Model-judgement backend that shells out to the local Claude Code CLI.

    Reuses the OpenClaw lane wholesale — same prompts, same cache, same daily
    budget, same per-run caps — and only swaps the transport. Cache keys and
    budget entries are tagged ``claude-*`` so judgements from different models
    never share a cache slot.
    """

    backend_tag = "claude"
    backend_name = "claude-cli"

    @property
    def _max_screening_calls(self) -> int:
        return self.config.claude_max_screening_calls_per_run

    @property
    def _max_asset_calls(self) -> int:
        return self.config.claude_max_asset_calls_per_run

    @property
    def _session_tag(self) -> str:
        return self.config.claude_model or "claude-default"

    @property
    def available(self) -> bool:
        return (
            self.config.enabled
            and self.config.claude_enabled
            and bool(_resolve_claude_binary(self.config.claude_bin))
        )

    def _model_for(self, payload: dict[str, Any]) -> str:
        """Pick the model from the task already named in the payload.

        ``claude_model`` overrides everything when set; otherwise screening uses
        the fast model and anything else (asset mapping, manual tasks) uses the
        CLI default configured in ~/.claude/settings.json.
        """

        if self.config.claude_model:
            return self.config.claude_model
        if str(payload.get("task", "")) == "screen_event":
            return self.config.claude_screening_model
        return self.config.claude_asset_model

    def _run_json_agent(self, *, instructions: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        binary = _resolve_claude_binary(self.config.claude_bin)
        if not binary:
            return None
        prompt = (
            instructions.strip()
            + "\n\nINPUT_JSON:\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        command = [binary, "-p", prompt]
        model = self._model_for(payload)
        if model:
            command.extend(["--model", model])
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.config.claude_timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return _parse_json_object(completed.stdout)


class CascadingModelJudgementClient:
    """Try each backend in order until one returns a usable judgement.

    Order is primary (OpenAI HTTP) -> extras (Claude CLI) -> fallback
    (OpenClaw/Codex). Any backend that reports itself unavailable, or that
    returns nothing, is skipped so one dead backend cannot stall the chain.
    """

    def __init__(
        self,
        primary: OpenAIResponsesJsonClient,
        fallback: OpenClawAgentJsonClient,
        extras: list[Any] | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.extras = list(extras or [])

    @property
    def _chain(self) -> list[Any]:
        return [self.primary, *self.extras, self.fallback]

    @property
    def available(self) -> bool:
        return any(getattr(client, "available", False) for client in self._chain)

    def _backend_name(self, client: Any) -> str:
        if client is self.primary:
            return "openai-responses"
        return getattr(client, "backend_name", "unknown")

    def screen_event(self, cluster: EventCluster, base_impact: ImpactAssessment) -> dict[str, Any] | None:
        for client in self._chain:
            if not getattr(client, "available", False):
                continue
            result = client.screen_event(cluster, base_impact)
            if result is not None:
                return {**result, "_model_backend": self._backend_name(client)}
        return None

    def map_assets(
        self,
        cluster: EventCluster,
        impact: ImpactAssessment,
        instruments: list[InstrumentDescriptor],
    ) -> dict[str, Any] | None:
        for client in self._chain:
            if not getattr(client, "available", False):
                continue
            result = client.map_assets(cluster, impact, instruments)
            if result is not None:
                return {**result, "_model_backend": self._backend_name(client)}
        return None


class ModelEnhancedImpactAnalyzer:
    def __init__(
        self,
        base_analyzer: Any,
        client: Any,
        config: ModelJudgementConfig,
    ) -> None:
        self.base_analyzer = base_analyzer
        self.client = client
        self.config = config

    def assess(self, cluster: EventCluster) -> ImpactAssessment:
        base_impact = self.base_analyzer.assess(cluster)
        should_screen, gate = self._should_screen(cluster, base_impact)
        gate_payload = gate.as_dict()
        if not should_screen:
            return replace(
                base_impact,
                model_judgement={
                    **base_impact.model_judgement,
                    "screening_status": "skipped",
                    "screening_reason": "; ".join(gate.reasons[:4])
                    or "model disabled, no API key, social-only, low-trust source, or below attention gate",
                    "attention_gate": gate_payload,
                },
            )
        decision = self.client.screen_event(cluster, base_impact)
        if not decision:
            return replace(
                base_impact,
                model_judgement={
                    **base_impact.model_judgement,
                    "screening_status": "unavailable",
                    "attention_gate": gate_payload,
                },
            )
        merged = self._merge_screening(base_impact, decision)
        return replace(
            merged,
            model_judgement={
                **merged.model_judgement,
                "attention_gate": gate_payload,
            },
        )

    def _should_screen(
        self,
        cluster: EventCluster,
        base_impact: ImpactAssessment,
    ) -> tuple[bool, AttentionGateDecision]:
        if not self.config.enabled or not self.config.screening_enabled or not self.client.available:
            return False, AttentionGateDecision(0, "reject", ["模型未启用或不可用"], False, False, False, False)
        source_ids = {source_id.lower() for source_id in cluster.source_ids}
        if source_ids and source_ids.issubset(self.config.social_source_ids):
            return False, AttentionGateDecision(0, "reject", ["仅社媒来源"], False, False, False, False)
        if source_ids and source_ids & self.config.excluded_source_ids:
            source_ids = source_ids - self.config.excluded_source_ids
            if not source_ids:
                return False, AttentionGateDecision(0, "reject", ["来源被排除"], False, False, False, False)
        if self.config.evidence_source_ids and not source_ids.intersection(self.config.evidence_source_ids):
            return False, AttentionGateDecision(0, "reject", ["缺少可信证据源"], False, False, False, False)
        gate = evaluate_model_call_gate(cluster, base_impact)
        if not gate.should_call_model:
            return False, gate
        if not self._passes_model_focus_prefilter(cluster):
            return False, AttentionGateDecision(
                min(gate.score, 34),
                "reject",
                gate.reasons + ["未通过基本面预筛"],
                gate.fundamental,
                gate.policy_demand,
                gate.low_predictability_risk,
                gate.quantified,
            )
        if cluster.avg_source_trust < self.config.min_screening_source_trust:
            return False, AttentionGateDecision(
                min(gate.score, 34),
                "reject",
                gate.reasons + ["来源可信度不足"],
                gate.fundamental,
                gate.policy_demand,
                gate.low_predictability_risk,
                gate.quantified,
            )
        return True, gate

    def _is_openclaw_only(self) -> bool:
        primary = getattr(self.client, "primary", None)
        return primary is not None and not bool(getattr(primary, "available", False))

    def _passes_openclaw_prefilter(self, cluster: EventCluster) -> bool:
        return self._passes_model_focus_prefilter(cluster)

    def _passes_model_focus_prefilter(self, cluster: EventCluster) -> bool:
        text = str(getattr(cluster, "combined_text", "") or "").lower()
        headline = str(getattr(cluster, "headline", "") or "").lower()
        if not text:
            fallback_parts = [
                str(getattr(cluster, "title", "") or ""),
                str(getattr(cluster, "summary", "") or ""),
                headline,
            ]
            text = " ".join(part for part in fallback_parts if part).lower()
        if not headline:
            headline = text
        has_trigger = any(pattern.lower() in text for pattern in MODEL_SCREENING_TRIGGER_PATTERNS)
        has_fundamental = any(pattern.lower() in text for pattern in FUNDAMENTAL_TEXT_PATTERNS)
        has_leading_fundamental = any(
            pattern.lower() in text for pattern in LEADING_FUNDAMENTAL_PATTERNS
        )
        has_extended_fundamental = any(
            pattern.lower() in text
            for pattern in (
                GUIDANCE_REVISION_PATTERNS
                + CUSTOMER_SUPPLIER_PATTERNS
                + PRICE_MARGIN_PATTERNS
                + CAPACITY_UTILIZATION_PATTERNS
                + BALANCE_SHEET_PATTERNS
                + COMPETITIVE_POSITION_PATTERNS
            )
        )
        has_lagging_price_reaction = any(
            pattern.lower() in text for pattern in LAGGING_PRICE_REACTION_PATTERNS
        )
        has_policy_demand = any(pattern.lower() in text for pattern in POLICY_DEMAND_PATTERNS)
        low_predictability_risk = any(
            pattern.lower() in text for pattern in LOW_PREDICTABILITY_RISK_PATTERNS
        )
        if low_predictability_risk and not (
            has_fundamental or has_policy_demand or has_leading_fundamental or has_extended_fundamental
        ):
            return False
        if has_lagging_price_reaction and not has_leading_fundamental:
            return False
        is_routine = any(pattern.lower() in headline for pattern in ROUTINE_DISCLOSURE_PATTERNS)
        if is_routine and not has_trigger:
            return False
        if is_routine and not any(
            pattern.lower() in text
            for pattern in [
                "净利润",
                "营收",
                "增长",
                "下降",
                "亏损",
                "处罚",
                "立案",
                "收购",
                "重组",
                "中标",
                "合同",
                "earnings",
                "guidance",
            ]
        ):
            return False
        return (
            has_fundamental
            or has_leading_fundamental
            or has_extended_fundamental
            or has_policy_demand
            or has_trigger
        )

    def _merge_screening(self, base: ImpactAssessment, decision: dict[str, Any]) -> ImpactAssessment:
        worth_attention = bool(decision.get("worth_attention", False))
        attention = clamp(_as_float(decision.get("attention_score"), 0.0) / 100.0)
        model_confidence = clamp(_as_float(decision.get("confidence"), 0.0))
        severity_hint = clamp(_as_float(decision.get("severity"), attention))

        if not worth_attention and model_confidence >= 0.45:
            direction = Direction.NEUTRAL
            event_type = EventType.UNKNOWN if base.event_type == EventType.UNKNOWN else base.event_type
            severity = min(base.severity, max(0.16, attention * 0.45))
            confidence = min(base.confidence, max(0.25, model_confidence * 0.65))
        else:
            direction = _parse_direction(decision.get("direction"), base.direction)
            event_type = _parse_event_type(decision.get("event_type"), base.event_type)
            severity = max(base.severity, severity_hint)
            confidence = max(base.confidence, model_confidence)

        markets = _parse_markets(decision.get("affected_markets")) or base.affected_markets
        sectors = unique_preserve(base.affected_sectors + _string_list(decision.get("affected_sectors")))
        themes = unique_preserve(base.affected_themes + _string_list(decision.get("affected_themes")))
        reason = str(decision.get("reason") or decision.get("reject_reason") or "").strip()
        evidence = _string_list(decision.get("evidence"))
        model_rationale = []
        if reason:
            model_rationale.append(f"AI筛选: {reason}")
        if evidence:
            model_rationale.append("AI证据: " + "；".join(evidence[:3]))

        return replace(
            base,
            event_type=event_type,
            direction=direction,
            affected_markets=markets,
            affected_sectors=sectors or base.affected_sectors,
            affected_themes=themes or base.affected_themes,
            severity=clamp(severity),
            confidence=clamp(confidence),
            matched_rules=unique_preserve(base.matched_rules + ["gpt-screening"]),
            rationale=unique_preserve(model_rationale + base.rationale),
            model_judgement={
                **base.model_judgement,
                "screening_status": "used",
                "screening": decision,
            },
        )


class ModelEnhancedInstrumentMapper:
    def __init__(
        self,
        base_mapper: Any,
        client: Any,
        config: ModelJudgementConfig,
        candidate_instruments: list[InstrumentDescriptor],
    ) -> None:
        self.base_mapper = base_mapper
        self.client = client
        self.config = config
        self.candidate_instruments = candidate_instruments
        self.instruments_by_symbol = {
            instrument.symbol.upper(): instrument for instrument in candidate_instruments
        }

    def map(self, cluster: EventCluster, impact: ImpactAssessment) -> list[InstrumentMatch]:
        base_matches = self.base_mapper.map(cluster, impact)
        if not self._should_map(impact):
            return base_matches
        result = self.client.map_assets(cluster, impact, self.candidate_instruments)
        if not result:
            return base_matches
        existing_symbols = {match.instrument.symbol.upper() for match in base_matches}
        model_matches: list[InstrumentMatch] = []
        for item in result.get("candidates", []):
            if not isinstance(item, dict):
                continue
            match = self._candidate_to_match(cluster, impact, item)
            if match is None:
                continue
            symbol_key = match.instrument.symbol.upper()
            if symbol_key in existing_symbols:
                continue
            existing_symbols.add(symbol_key)
            model_matches.append(match)
        combined = base_matches + model_matches
        combined.sort(key=lambda item: item.exposure_score, reverse=True)
        return combined[:10]

    def _should_map(self, impact: ImpactAssessment) -> bool:
        if not self.config.enabled or not self.config.asset_mapping_enabled or not self.client.available:
            return False
        screening = impact.model_judgement.get("screening", {})
        gate = impact.model_judgement.get("attention_gate", {})
        if isinstance(screening, dict) and screening.get("worth_attention") is False:
            return False
        if impact.confidence < self.config.min_asset_confidence and impact.event_type == EventType.UNKNOWN:
            return False
        if isinstance(screening, dict) and screening.get("worth_attention") is True:
            return _as_float(screening.get("attention_score"), 0.0) >= 65.0
        if isinstance(gate, dict):
            gate_tier = str(gate.get("tier", "")).strip().lower()
            gate_score = _as_float(gate.get("score"), 0.0)
            if gate_tier == "notify" and gate_score >= 90.0:
                return True
        # Do not spend the expensive asset-mapping call on events that the model
        # never screened. The rule mapper already covers obvious direct matches.
        return False

    def _candidate_to_match(
        self,
        cluster: EventCluster,
        impact: ImpactAssessment,
        item: dict[str, Any],
    ) -> InstrumentMatch | None:
        relation = str(item.get("relation") or "").strip().lower()
        if relation in {"avoid", "unsupported", "none"}:
            return None
        exposure = clamp(_as_float(item.get("exposure_score"), 0.0))
        confidence = clamp(_as_float(item.get("confidence"), 0.0))
        if exposure < self.config.min_asset_exposure or confidence < self.config.min_asset_confidence:
            return None
        symbol = _normalize_symbol(str(item.get("symbol") or ""), str(item.get("market") or ""))
        if not symbol:
            return None
        instrument = self.instruments_by_symbol.get(symbol.upper())
        if instrument is None:
            market = _parse_market(str(item.get("market") or ""))
            if market is None:
                return None
            instrument = InstrumentDescriptor(
                symbol=symbol,
                market=market,
                asset_type="stock",
                name=str(item.get("name") or symbol),
                sectors=impact.affected_sectors[:4],
                themes=impact.affected_themes[:6],
                aliases=unique_preserve([str(item.get("name") or ""), symbol]),
                liquidity_score=0.55,
                metadata={"generated_from": "gpt-asset-mapper"},
            )
        direction = _parse_direction(item.get("direction"), impact.direction)
        reason = str(item.get("reason") or "AI asset mapping").strip()
        blended_exposure = clamp(0.70 * exposure + 0.30 * confidence)
        return InstrumentMatch(
            cluster_id=cluster.cluster_id,
            instrument=instrument,
            direction=direction,
            exposure_score=blended_exposure,
            reasons=[
                f"AI asset map: {reason}",
                f"relation: {relation or 'not specified'}",
            ],
        )


def build_model_judgement_stack(
    *,
    config_path: Path,
    project_root: Path,
    base_impact_analyzer: Any,
    base_instrument_mapper: Any,
    candidate_instruments: list[InstrumentDescriptor],
) -> tuple[Any, Any, ModelJudgementConfig]:
    config = ModelJudgementConfig.from_file(config_path, project_root=project_root)
    cache = ModelJudgementCache(config.cache_path, ttl_hours=config.cache_ttl_hours)
    budget = ModelCallBudget(config.budget_path, daily_limit=config.model_daily_call_limit)
    client = CascadingModelJudgementClient(
        primary=OpenAIResponsesJsonClient(config, cache, budget),
        fallback=OpenClawAgentJsonClient(config, cache, budget),
        extras=[ClaudeCliJsonClient(config, cache, budget)],
    )
    if not config.enabled:
        return base_impact_analyzer, base_instrument_mapper, config
    return (
        ModelEnhancedImpactAnalyzer(base_impact_analyzer, client, config),
        ModelEnhancedInstrumentMapper(base_instrument_mapper, client, config, candidate_instruments),
        config,
    )


def _cluster_payload(cluster: EventCluster, config: ModelJudgementConfig) -> dict[str, Any]:
    documents = []
    for document in cluster.documents[: config.max_documents]:
        documents.append(
            {
                "source_id": document.source_id,
                "title": document.title,
                "summary": document.summary,
                "body": _truncate(document.body, config.max_text_chars // max(1, config.max_documents)),
                "url": document.url,
                "published_at": document.published_at.isoformat(),
                "source_trust": document.source_trust,
                "entities": document.entities[:8],
                "themes": document.themes[:8],
                "metadata": {
                    key: value
                    for key, value in document.metadata.items()
                    if key in {"stock_code", "stock_name", "instrument_market", "direct_codes", "discussion_count"}
                },
            }
        )
    return {
        "cluster_id": cluster.cluster_id,
        "headline": cluster.headline,
        "summary": cluster.summary,
        "source_ids": cluster.source_ids,
        "avg_source_trust": cluster.avg_source_trust,
        "entities": cluster.entities[:10],
        "themes": cluster.themes[:10],
        "sectors": cluster.sectors[:8],
        "regions": cluster.regions[:8],
        "first_seen_at": cluster.first_seen_at.isoformat(),
        "last_seen_at": cluster.last_seen_at.isoformat(),
        "documents": documents,
    }


def _extract_response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return str(payload["output_text"])
    chunks: list[str] = []
    for output in payload.get("output", []):
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def _extract_openclaw_text(payload: dict[str, Any]) -> str:
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        return ""
    chunks: list[str] = []
    for item in result.get("payloads", []):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            chunks.append(str(item["text"]))
    return "\n".join(chunks).strip()


def _parse_json_object(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return unique_preserve(str(item).strip() for item in value if str(item).strip())


def _parse_direction(value: object, default: Direction) -> Direction:
    try:
        return Direction(str(value).strip().lower())
    except ValueError:
        return default


def _parse_event_type(value: object, default: EventType) -> EventType:
    try:
        return EventType(str(value).strip().lower())
    except ValueError:
        return default


def _parse_market(value: object) -> Market | None:
    normalized = str(value).strip().upper()
    aliases = {
        "A": Market.CN_A,
        "CN": Market.CN_A,
        "CN-A": Market.CN_A,
        "A-SHARE": Market.CN_A,
        "HK": Market.HK,
        "HONG KONG": Market.HK,
        "US": Market.US,
        "USA": Market.US,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return Market(normalized)
    except ValueError:
        return None


def _parse_markets(value: object) -> list[Market]:
    if not isinstance(value, list):
        return []
    markets = []
    for item in value:
        market = _parse_market(item)
        if market is not None and market not in markets:
            markets.append(market)
    return markets


def _normalize_symbol(symbol: str, market: str) -> str:
    raw_symbol = symbol.strip().upper()
    parsed_market = _parse_market(market)
    if not raw_symbol:
        return ""
    if parsed_market == Market.CN_A:
        digits = "".join(char for char in raw_symbol if char.isdigit())
        if len(digits) == 6:
            if raw_symbol.endswith((".SH", ".SZ")):
                return raw_symbol
            if digits.startswith("6"):
                return f"{digits}.SH"
            if digits.startswith(("0", "3")):
                return f"{digits}.SZ"
    if parsed_market == Market.HK:
        digits = "".join(char for char in raw_symbol if char.isdigit())
        if digits:
            return f"{digits.zfill(4)}.HK"
        return raw_symbol
    return raw_symbol


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit].rsplit(" ", 1)[0].strip() or value[:limit]


SCREENING_INSTRUCTIONS = """
你是一个二级市场新闻分拣员。任务不是“解释新闻”，而是判断这条新闻是否值得交易员继续看。
只基于输入里的标题、摘要、正文、来源和规则判断，不要补充外部事实。

严格原则：
1. 官方公告、监管政策、交易所、财联社/东财快讯等高可信来源优先。
2. 微博/雪球/论坛观点不能独立构成事件，只能作为热度佐证。
3. 如果只是泛泛观点、复读、无明确主体、无明确增量事实、营销软文、404/视频壳、过旧消息，worth_attention=false。
4. 如果是订单、并购、重组、财报、监管、政策、价格变化、技术突破、产能变化、制裁/出口管制、医保/集采等清晰催化，worth_attention=true。
5. 基本面和估值优先：收入、利润、毛利率、现金流、订单/客户、产能/稼动率、价格、政策需求、竞争格局、资产负债表、业绩指引变化，比单纯股价异动更重要。
6. 区分领先信号和事后信号：如果只是“已经涨跌后再报道”，且没有订单/客户/业绩/政策等新增事实，worth_attention=false。
7. 不要为了凑题材把无关消息贴到量子计算、卫星互联网、机器人等热门主题。

只输出一个 JSON object，不要输出解释性段落。
"""


ASSET_MAPPING_INSTRUCTIONS = """
你是一个港股/A股/美股影响链分析员。任务是判断“这条已筛选新闻可能影响哪些股票或金融产品”。
只基于输入新闻和 candidate_universe。不要编造不存在的关系。

严格原则：
1. 直接公司、公告股票代码、政策直接指向行业 > 供应链/同行 > 泛题材。
2. 如果只是弱主题相似，不要给 candidate，或把 confidence/exposure 压低。
3. 每个 candidate 必须说明影响链：直接公司、同业、供应商、客户、政策受益或板块 beta。
4. 不要把“增发股份/支付现金/股本变动”硬连到科技主题，除非正文明确提到相关业务或资产。
5. 微博/雪球只能辅助热度，不可作为资产映射的主证据。

只输出一个 JSON object，不要输出解释性段落。
"""
