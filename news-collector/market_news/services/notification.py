from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from market_news.domain.models import AlertLevel


LEVEL_ORDER = {
    AlertLevel.MEDIUM: 1,
    AlertLevel.HIGH: 2,
    AlertLevel.CRITICAL: 3,
}

# ── emoji maps ────────────────────────────────────────────────────────────────
LEVEL_EMOJI = {
    "critical": "🚨",
    "high": "⚠️",
    "medium": "📌",
}
LEVEL_LABELS = {
    "critical": "紧急",
    "high": "高优先",
    "medium": "关注",
}
DIRECTION_EMOJI = {
    "positive": "📈",
    "negative": "📉",
    "neutral": "➡️",
}
DIRECTION_LABELS = {
    "positive": "利好",
    "negative": "利空",
    "neutral": "中性",
}
EVENT_TYPE_LABELS = {
    "company": "公司",
    "industry": "行业",
    "policy": "政策",
    "macro": "宏观",
    "commodity": "商品",
    "regulation": "监管",
    "unknown": "其他",
}
TIER_EMOJI = {
    "hot": "🔥",
    "warm": "👀",
    "watch": "📌",
}
TIER_LABELS = {
    "hot": "热点",
    "warm": "观察",
    "watch": "跟踪",
}
GAP_LEVEL_ZH = {
    "large": "大差距",
    "catching-up": "追赶中",
    "parallel": "并跑",
    "leading": "领先",
}
SOURCE_QUALITY_ZH = {
    "official-confirmed": "官方确认✅",
    "official-led": "官方主导",
    "vetted-wire": "权威电讯",
    "multi-source": "多源验证",
    "single-wire": "单一来源",
}

_DIVIDER = "─" * 18


@dataclass(slots=True)
class NotificationPlan:
    channel: str
    target: str
    message: str
    cluster_ids: list[str]
    alert_count: int
    preview_path: Path
    modules: list[dict[str, object]]


class AlertDigestBuilder:
    def __init__(
        self,
        *,
        min_level: AlertLevel = AlertLevel.HIGH,
        max_alerts: int = 3,
        include_existing: bool = False,
        max_tech_signals: int = 2,
        min_tech_attention: float = 55.0,
        require_full_model_judgement: bool = True,
        min_model_confidence: float = 0.65,
        degraded_min_level: AlertLevel = AlertLevel.HIGH,
        mute_tech_when_model_down: bool = True,
    ) -> None:
        self.min_level = min_level
        self.max_alerts = max_alerts
        self.include_existing = include_existing
        self.max_tech_signals = max_tech_signals
        self.min_tech_attention = min_tech_attention
        self.require_full_model_judgement = require_full_model_judgement
        self.min_model_confidence = min_model_confidence
        # Level bar applied when the model layer is unavailable and we fall back
        # to rules. Kept at HIGH so a dead model does not open the floodgates.
        self.degraded_min_level = degraded_min_level
        self.mute_tech_when_model_down = mute_tech_when_model_down
        self._model_layer_down = False

    def compose(
        self,
        payload: dict[str, object],
        *,
        channel: str,
        target: str,
        sent_cluster_ids: set[str] | None = None,
        preview_path: Path,
    ) -> NotificationPlan | None:
        sent_cluster_ids = sent_cluster_ids or set()
        event_lookup = self._build_event_lookup(payload)
        # Fail-open: if the model layer produced no usable judgement anywhere in
        # this report (no API key, quota exhausted, backend down), requiring a
        # model verdict would silently mute every alert. In that case fall back
        # to rule-based gating at a stricter level instead of going dark.
        self._model_layer_down = self._model_judgement_unavailable(event_lookup)
        require_model = self.require_full_model_judgement and not self._model_layer_down
        effective_min_level = self.min_level
        if self._model_layer_down:
            effective_min_level = self.degraded_min_level
        selected_core_alerts: list[dict[str, object]] = []
        for alert in payload.get("alerts", []):
            if not isinstance(alert, dict):
                continue
            cluster_id = str(alert.get("cluster_id", "")).strip()
            if not cluster_id:
                continue
            if not self._passes_level(str(alert.get("level", "medium")), effective_min_level):
                continue
            if not self.include_existing and not bool(alert.get("is_new", False)):
                continue
            if cluster_id in sent_cluster_ids:
                continue
            if require_model and not self._has_full_model_judgement(
                event_lookup.get(cluster_id, {})
            ):
                continue
            selected_core_alerts.append(alert)
            if len(selected_core_alerts) >= self.max_alerts:
                break

        selected_cluster_ids = {
            str(alert.get("cluster_id", "")).strip()
            for alert in selected_core_alerts
            if str(alert.get("cluster_id", "")).strip()
        }
        # Tech-catalyst signals lean on the model to separate a real catalyst from
        # boilerplate (an auditor's filing scored "hot" and mapped to Tencent, an
        # index-commentary headline, etc.). With the model down the rules cannot
        # make that call, so the degraded path carries core alerts only.
        if self._model_layer_down and self.mute_tech_when_model_down:
            selected_tech_signals: list[dict[str, object]] = []
        else:
            selected_tech_signals = self._select_tech_signals(
                payload,
                event_lookup=event_lookup,
                selected_cluster_ids=selected_cluster_ids,
                sent_cluster_ids=sent_cluster_ids,
                require_model=require_model,
            )

        if not selected_core_alerts and not selected_tech_signals:
            return None

        created_at = str(payload.get("created_at", ""))
        source = str(payload.get("source", "market-news"))
        unique_cluster_ids = []
        for cluster_id in list(selected_cluster_ids) + [
            str(signal.get("cluster_id", "")).strip()
            for signal in selected_tech_signals
        ]:
            if cluster_id and cluster_id not in unique_cluster_ids:
                unique_cluster_ids.append(cluster_id)

        # ── header ────────────────────────────────────────────────────────────
        lines: list[str] = []
        time_label = self._format_created_at(created_at)
        core_count = len(selected_core_alerts)
        tech_count = len(selected_tech_signals)
        lines.append(f"📊 *市场提醒* {time_label}")
        summary_parts = []
        if core_count:
            summary_parts.append(f"🔔 主线 {core_count} 条")
        if tech_count:
            summary_parts.append(f"🧬 科技 {tech_count} 条")
        lines.append(" | ".join(summary_parts))
        if self._model_layer_down:
            lines.append("⚙️ _AI 筛选层不可用，本条按规则筛选（仅高危级）_")
        lines.append(_DIVIDER)

        # ── core alerts ───────────────────────────────────────────────────────
        if selected_core_alerts:
            lines.append("*全市场主线*")
            lines.append("")
            for index, alert in enumerate(selected_core_alerts, start=1):
                cluster_id = str(alert.get("cluster_id", ""))
                event = event_lookup.get(cluster_id, {})
                level_key = str(alert.get("level", "medium"))
                direction_key = str(alert.get("direction", "neutral"))
                event_type_key = str(alert.get("event_type", "unknown"))

                level_e = LEVEL_EMOJI.get(level_key, "📌")
                level_l = LEVEL_LABELS.get(level_key, level_key)
                dir_e = DIRECTION_EMOJI.get(direction_key, "➡️")
                dir_l = DIRECTION_LABELS.get(direction_key, direction_key)
                type_l = EVENT_TYPE_LABELS.get(event_type_key, event_type_key)

                headline = self._truncate(str(alert.get("headline", "")), 60)
                symbols = self._resolve_symbols(alert, event)
                rationale = self._resolve_rationale(alert, event)
                score = int(round(float(alert.get("final_score", 0))))
                first_seen = self._time_distance(str(event.get("first_seen_at", "")))

                lines.append(
                    f"{index}️⃣  {level_e} [{level_l}·{dir_l}·{type_l}] {dir_e}"
                )
                lines.append(f"*{headline}*")
                if symbols:
                    lines.append(f"标的: {' · '.join(symbols)}")
                if rationale:
                    lines.append(f"逻辑: {rationale}")
                meta_parts = []
                if first_seen:
                    meta_parts.append(f"⏱ {first_seen}")
                meta_parts.append(f"综合分 {score}")
                lines.append(" · ".join(meta_parts))
                if index < len(selected_core_alerts):
                    lines.append("")

        # ── tech signals ──────────────────────────────────────────────────────
        if selected_tech_signals:
            if selected_core_alerts:
                lines.append("")
                lines.append(_DIVIDER)
            lines.append("*港A科技催化*")
            lines.append("")
            for index, signal in enumerate(selected_tech_signals, start=1):
                tier_key = str(signal.get("attention_tier", "watch")).lower()
                direction_key = str(signal.get("direction", "neutral"))
                tier_e = TIER_EMOJI.get(tier_key, "📌")
                tier_l = TIER_LABELS.get(tier_key, tier_key)
                dir_e = DIRECTION_EMOJI.get(direction_key, "➡️")
                dir_l = DIRECTION_LABELS.get(direction_key, direction_key)

                headline = self._truncate(str(signal.get("headline", "")), 60)
                assets = [
                    str(item.get("symbol", "")).strip()
                    for item in signal.get("candidate_assets", [])
                    if isinstance(item, dict) and str(item.get("symbol", "")).strip()
                ][:4]
                triggers = [
                    str(item).strip()
                    for item in signal.get("trigger_tags", [])
                    if str(item).strip()
                ][:4]

                # ── frontier hits (priority info) ──────────────────────────
                frontier_hits = [
                    item for item in signal.get("frontier_hits", [])
                    if isinstance(item, dict)
                ][:3]

                attention = int(round(float(signal.get("trading_attention_score", 0))))
                heat = int(round(float(signal.get("heat_score", 0))))
                spec = int(round(float(signal.get("spec_score", 0))))

                source_quality_key = str(signal.get("source_quality", "")).strip()
                source_quality_label = SOURCE_QUALITY_ZH.get(
                    source_quality_key, source_quality_key or ""
                )

                # first_seen from signal or linked event
                cluster_id = str(signal.get("cluster_id", ""))
                event = event_lookup.get(cluster_id, {})
                first_seen = self._time_distance(str(event.get("first_seen_at", "")))

                lines.append(
                    f"{index}️⃣  {tier_e} [{tier_l}·{dir_l}] {dir_e}"
                )
                lines.append(f"*{headline}*")
                if assets:
                    lines.append(f"候选: {' · '.join(assets)}")
                if triggers:
                    lines.append(f"触发: {' · '.join(triggers)}")

                # frontier hits — most important new block
                if frontier_hits:
                    frontier_parts = []
                    for hit in frontier_hits:
                        name = str(hit.get("cn_label", hit.get("frontier_id", ""))).strip()
                        gap = GAP_LEVEL_ZH.get(
                            str(hit.get("gap_level", "")), str(hit.get("gap_level", ""))
                        )
                        if name:
                            frontier_parts.append(f"{name}({gap})" if gap else name)
                    if frontier_parts:
                        lines.append(f"🌐 前沿: {' · '.join(frontier_parts)}")

                # three scores
                lines.append(f"关注 {attention} · 热度 {heat} · 投机 {spec}")

                # source quality + first seen
                meta_parts = []
                if first_seen:
                    meta_parts.append(f"⏱ {first_seen}")
                if source_quality_label:
                    meta_parts.append(source_quality_label)
                if meta_parts:
                    lines.append(" · ".join(meta_parts))

                if index < len(selected_tech_signals):
                    lines.append("")

        message = "\n".join(lines).strip()
        modules = [
            {
                "name": "core_alerts",
                "status": "active" if selected_core_alerts else "idle",
                "count": len(selected_core_alerts),
                "detail": "AI fully screened high/critical alert stream",
            },
            {
                "name": "tech_block",
                "status": "active" if selected_tech_signals else "idle",
                "count": len(selected_tech_signals),
                "detail": "AI fully screened A/H tech catalyst stream",
            },
        ]
        return NotificationPlan(
            channel=channel,
            target=target,
            message=message,
            cluster_ids=unique_cluster_ids,
            alert_count=len(unique_cluster_ids),
            preview_path=preview_path,
            modules=modules,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _select_tech_signals(
        self,
        payload: dict[str, object],
        *,
        event_lookup: dict[str, dict[str, object]],
        selected_cluster_ids: set[str],
        sent_cluster_ids: set[str],
        require_model: bool = True,
    ) -> list[dict[str, object]]:
        tech_block = payload.get("tech_block", {})
        if not isinstance(tech_block, dict):
            return []

        selected: list[dict[str, object]] = []
        for signal in tech_block.get("signals", []):
            if not isinstance(signal, dict):
                continue
            cluster_id = str(signal.get("cluster_id", "")).strip()
            if not cluster_id:
                continue
            if require_model and not self._has_full_model_judgement(
                event_lookup.get(cluster_id, {})
            ):
                continue
            attention_score = float(signal.get("trading_attention_score", 0.0) or 0.0)
            tier = str(signal.get("attention_tier", "watch")).strip().lower()
            if attention_score < self.min_tech_attention and tier not in {"hot", "warm"}:
                continue
            if cluster_id in sent_cluster_ids and cluster_id not in selected_cluster_ids:
                continue
            selected.append(signal)
            if len(selected) >= self.max_tech_signals:
                break
        return selected

    def _passes_level(self, level_value: str, min_level: AlertLevel | None = None) -> bool:
        try:
            level = AlertLevel(level_value)
        except ValueError:
            return False
        return LEVEL_ORDER[level] >= LEVEL_ORDER[min_level or self.min_level]

    def _model_judgement_unavailable(self, event_lookup: dict[str, dict[str, object]]) -> bool:
        """True when no event in this report carries a usable model verdict.

        Distinguishes "the model looked and said no" from "the model never ran".
        Only the latter should relax the gate: if at least one event was actually
        screened, the layer is alive and its verdicts stay authoritative.
        """

        if not event_lookup:
            return False
        for event in event_lookup.values():
            judgement = event.get("model_judgement", {})
            if not isinstance(judgement, dict):
                continue
            if str(judgement.get("screening_status", "")).lower() == "used":
                return False
        return True

    def _build_event_lookup(self, payload: dict[str, object]) -> dict[str, dict[str, object]]:
        lookup: dict[str, dict[str, object]] = {}
        for key in ["top_events", "negative_risks", "positive_catalysts", "watchlist"]:
            for event in payload.get(key, []):
                if not isinstance(event, dict):
                    continue
                cluster_id = str(event.get("cluster_id", "")).strip()
                if cluster_id and cluster_id not in lookup:
                    lookup[cluster_id] = event
        ai_judgement = payload.get("ai_judgement", {})
        if isinstance(ai_judgement, dict):
            for event in ai_judgement.get("events", []):
                if not isinstance(event, dict):
                    continue
                cluster_id = str(event.get("cluster_id", "")).strip()
                if cluster_id and cluster_id not in lookup:
                    lookup[cluster_id] = event
        return lookup

    def _has_full_model_judgement(self, event: dict[str, object]) -> bool:
        judgement = event.get("model_judgement", {})
        if not isinstance(judgement, dict):
            return False
        screening = judgement.get("screening", {})
        if not isinstance(screening, dict):
            return False
        if str(judgement.get("screening_status", "")).lower() != "used":
            return False
        if screening.get("worth_attention") is not True:
            return False
        try:
            confidence = float(screening.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return confidence >= self.min_model_confidence

    def _resolve_symbols(
        self,
        alert: dict[str, object],
        event: dict[str, object],
    ) -> list[str]:
        symbols = [
            str(symbol).strip()
            for symbol in alert.get("symbols", [])
            if str(symbol).strip()
        ]
        if symbols:
            return symbols[:3]
        event_symbols: list[str] = []
        for instrument in event.get("top_instruments", []):
            if not isinstance(instrument, dict):
                continue
            symbol = str(instrument.get("symbol", "")).strip()
            if symbol and symbol not in event_symbols:
                event_symbols.append(symbol)
        return event_symbols[:3]

    def _resolve_rationale(
        self,
        alert: dict[str, object],
        event: dict[str, object],
    ) -> str:
        rationale = [
            self._truncate(str(item).strip(), 40)
            for item in event.get("rationale", [])
            if str(item).strip()
        ]
        judgement = event.get("model_judgement", {})
        screening = judgement.get("screening", {}) if isinstance(judgement, dict) else {}
        model_reason = str(screening.get("reason", "")).strip() if isinstance(screening, dict) else ""
        if model_reason:
            return "AI完整判断: " + self._truncate(model_reason, 68)
        if rationale:
            return "；".join(rationale[:2])
        reason = str(alert.get("reason", "")).strip()
        if reason:
            return self._truncate(reason, 80)
        return ""

    @staticmethod
    def _format_created_at(iso: str) -> str:
        """Convert ISO timestamp to 'MM-DD HH:MM' for the message header."""
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            # Display in CST (UTC+8) so Chinese users see local time
            from datetime import timedelta
            cst = dt.astimezone(UTC) + timedelta(hours=8)
            return cst.strftime("%m-%d %H:%M")
        except Exception:
            return iso[:16] if len(iso) >= 16 else iso

    @staticmethod
    def _time_distance(iso: str) -> str:
        """Return human-readable time distance like '2h前', '35m前', '昨天'."""
        if not iso:
            return ""
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            delta = datetime.now(UTC) - dt.astimezone(UTC)
            total_seconds = delta.total_seconds()
            if total_seconds < 0:
                return ""
            minutes = int(total_seconds // 60)
            if minutes < 1:
                return "刚刚"
            if minutes < 60:
                return f"{minutes}m前"
            hours = minutes // 60
            if hours < 24:
                return f"{hours}h前"
            days = hours // 24
            if days == 1:
                return "昨天"
            return f"{days}天前"
        except Exception:
            return ""

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 1].rstrip() + "…"
