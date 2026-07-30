from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from market_news.domain.models import Direction, EventCluster, ImpactAssessment, RankedEvent


FUNDAMENTAL_TEXT_PATTERNS = [
    "净利润",
    "扣非净利润",
    "归母净利润",
    "营收",
    "营业收入",
    "毛利率",
    "现金流",
    "经营性现金流",
    "自由现金流",
    "roe",
    "roic",
    "eps",
    "每股收益",
    "业绩预告",
    "业绩快报",
    "一季报",
    "半年报",
    "三季报",
    "年报",
    "扭亏",
    "同比增长",
    "同比下降",
    "record profit",
    "earnings",
    "revenue",
    "gross margin",
    "free cash flow",
]

FUNDAMENTAL_RULE_NAMES = {
    "Earnings Upgrade",
    "Earnings Downshift",
    "Fundamental Earnings Growth",
    "Fundamental Earnings Deterioration",
    "Cash Flow and Margin Improvement",
}

POLICY_ACCESS_OPENING_RULE_NAMES = {
    "Commercial Space Access Opening",
}

POLICY_DEMAND_PATTERNS = [
    "进校园",
    "以旧换新",
    "消费补贴",
    "设备更新",
    "产业政策",
    "医保支付",
    "集采",
    "订单",
    "合同",
    "中标",
    "算力基础设施",
    "国产芯片",
    "数据中心",
    "人工智能基础设施",
    "商业航天",
    "商业航天高质量发展",
    "许可准入",
    "发射申请",
    "频率协调",
    "一站式",
    "公共服务平台",
    "放得活",
    "管得住",
    "箭星场频网",
    "全链条协同",
    "太空算力",
    "太空制造",
    "产能利用率",
    "渗透率",
    "需求增长",
]

LEADING_FUNDAMENTAL_PATTERNS = [
    "失去订单",
    "失去客户",
    "订单取消",
    "采购取消",
    "采购订单取消",
    "终止采购",
    "终止合同",
    "合同终止",
    "客户流失",
    "大客户流失",
    "客户暂停采购",
    "采购延后",
    "订单延后",
    "订单下滑",
    "订单减少",
    "订单放缓",
    "在手订单下降",
    "backlog decline",
    "order cancellation",
    "lost order",
    "lost customer",
    "purchase order cancelled",
    "purchase order canceled",
    "customer loss",
    "contract termination",
    "terminated contract",
]

GUIDANCE_REVISION_PATTERNS = [
    "上调指引",
    "下调指引",
    "业绩指引",
    "盈利预测上调",
    "盈利预测下调",
    "预期上调",
    "预期下调",
    "guidance raised",
    "guidance cut",
    "raises outlook",
    "cuts outlook",
]

CUSTOMER_SUPPLIER_PATTERNS = [
    "主要客户",
    "大客户",
    "客户集中度",
    "核心客户",
    "供应商",
    "渠道库存",
    "库存周期",
    "去库存",
    "补库存",
    "backlog",
    "major customer",
    "channel inventory",
]

PRICE_MARGIN_PATTERNS = [
    "涨价",
    "降价",
    "提价",
    "平均售价",
    "asp",
    "毛利率改善",
    "毛利率下降",
    "成本下降",
    "成本上升",
    "原材料价格",
    "price increase",
    "price cut",
    "margin expansion",
    "margin pressure",
]

CAPACITY_UTILIZATION_PATTERNS = [
    "产能利用率",
    "稼动率",
    "满产",
    "扩产",
    "投产",
    "产能爬坡",
    "产线",
    "capacity utilization",
    "capacity expansion",
    "production ramp",
]

BALANCE_SHEET_PATTERNS = [
    "资产负债率",
    "应收账款",
    "存货",
    "减值",
    "商誉",
    "经营性现金流",
    "自由现金流",
    "working capital",
    "inventory write-down",
    "impairment",
]

COMPETITIVE_POSITION_PATTERNS = [
    "份额提升",
    "市占率",
    "市场份额",
    "国产替代",
    "进口替代",
    "技术路线切换",
    "竞争格局",
    "share gain",
    "market share",
]

CAPITAL_ALLOCATION_PATTERNS = [
    "回购",
    "分红",
    "派息",
    "特别股息",
    "提高派息",
    "share repurchase",
    "buyback",
    "special dividend",
]

LAGGING_PRICE_REACTION_PATTERNS = [
    "盘前跌",
    "盘前涨",
    "盘中跌",
    "盘中涨",
    "跌超",
    "涨超",
    "大跌",
    "大涨",
    "股价异动",
    "股价大跌",
    "股价大涨",
    "premarket",
    "pre-market",
    "shares fall",
    "shares plunge",
    "shares surge",
    "stock falls",
    "stock jumps",
]

LOW_PREDICTABILITY_RISK_PATTERNS = [
    "立案",
    "处罚",
    "问询函",
    "监管函",
    "异常波动",
    "退市风险警示",
    "*st",
    "减持",
    "质押",
]

LOW_PREDICTABILITY_RULE_NAMES = {
    "Regulatory Pressure",
    "Share Reduction Pressure",
}

ROUTINE_OR_LOW_SIGNAL_PATTERNS = [
    "独立董事述职",
    "董事会决议",
    "监事会决议",
    "年度股东大会",
    "会议资料",
    "会议通知",
    "内部控制",
    "社会责任报告",
    "esg报告",
    "主做市服务",
    "质押券折算率",
    "募集资金三方监管协议",
    "现金管理",
    "闲置募集资金",
    "股本变动公告",
]

ONE_OFF_OR_ACCOUNTING_NOISE_PATTERNS = [
    "会计政策变更",
    "非经常性损益",
    "政府补助",
    "公允价值变动",
    "营业收入扣除",
    "资产处置收益",
    "一次性收益",
    "one-off gain",
    "fair value change",
]

VAGUE_OPINION_PATTERNS = [
    "几个方向",
    "还是要重视",
    "未来会变量很大",
    "可能会",
    "有比较大预期差",
    "个人观点",
    "仅供参考",
]

DIRECT_VALUATION_PATTERNS = [
    "pe",
    "pb",
    "市盈率",
    "市净率",
    "估值",
    "分红率",
    "股息率",
    "回报率",
    "roe",
    "roic",
]

NUMERIC_EVIDENCE_RE = re.compile(r"(\d+(?:\.\d+)?%|\d+(?:\.\d+)?[亿万]元|同比|环比|bps|pct)")


@dataclass(frozen=True, slots=True)
class AttentionGateDecision:
    score: float
    tier: str
    reasons: list[str]
    fundamental: bool
    policy_demand: bool
    low_predictability_risk: bool
    quantified: bool

    @property
    def should_call_model(self) -> bool:
        return self.tier in {"model", "notify"}

    @property
    def should_notify(self) -> bool:
        return self.tier == "notify"

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 2),
            "tier": self.tier,
            "reasons": self.reasons,
            "fundamental": self.fundamental,
            "policy_demand": self.policy_demand,
            "low_predictability_risk": self.low_predictability_risk,
            "quantified": self.quantified,
        }


def _haystack_from_event(event: RankedEvent) -> str:
    return " ".join(
        [
            event.headline,
            " ".join(event.impact.matched_rules),
            " ".join(event.impact.affected_themes),
            " ".join(event.impact.rationale),
        ]
    ).lower()


def _impact_text(impact: ImpactAssessment) -> str:
    return " ".join(
        impact.matched_rules + impact.affected_themes + impact.affected_sectors + impact.rationale
    ).lower()


def _cluster_text(cluster: EventCluster) -> str:
    return str(getattr(cluster, "combined_text", "") or "").lower()


def is_fundamental_impact(impact: ImpactAssessment) -> bool:
    rule_names = set(impact.matched_rules)
    if rule_names & FUNDAMENTAL_RULE_NAMES:
        return True
    text = _impact_text(impact)
    return any(pattern.lower() in text for pattern in FUNDAMENTAL_TEXT_PATTERNS)


def is_policy_demand_impact(impact: ImpactAssessment) -> bool:
    text = _impact_text(impact)
    return any(pattern.lower() in text for pattern in POLICY_DEMAND_PATTERNS)


def is_policy_access_opening_impact(impact: ImpactAssessment) -> bool:
    """High-trust access-opening policies can be tradeable even before earnings data shows up."""
    if set(impact.matched_rules) & POLICY_ACCESS_OPENING_RULE_NAMES:
        return True
    text = _impact_text(impact)
    return (
        "商业航天" in text
        and any(
            pattern in text
            for pattern in [
                "许可准入",
                "发射申请",
                "频率协调",
                "一站式",
                "公共服务平台",
                "放得活",
                "打破产业各环节壁垒",
            ]
        )
    )


def is_low_predictability_risk(event: RankedEvent) -> bool:
    if event.impact.direction != Direction.NEGATIVE:
        return False
    if set(event.impact.matched_rules) & LOW_PREDICTABILITY_RULE_NAMES:
        return True
    text = _haystack_from_event(event)
    return any(pattern.lower() in text for pattern in LOW_PREDICTABILITY_RISK_PATTERNS)


def evaluate_model_call_gate(cluster: EventCluster, impact: ImpactAssessment) -> AttentionGateDecision:
    text = " ".join([_cluster_text(cluster), _impact_text(impact)]).lower()
    score, reasons = _base_attention_score(text, impact)
    avg_trust = float(getattr(cluster, "avg_source_trust", 0.0) or 0.0)
    doc_count = int(getattr(cluster, "doc_count", 0) or 0)
    source_ids = {str(item).lower() for item in getattr(cluster, "source_ids", [])}

    if avg_trust >= 0.9:
        score += 12
        reasons.append("高可信来源")
    if doc_count >= 2 or len(source_ids) >= 2:
        score += 5
        reasons.append("多文档/多来源印证")
    if getattr(cluster, "entities", None):
        score += 6
        reasons.append("有明确公司或实体")

    tier = _resolve_tier(score, reasons)
    return _decision(score, tier, reasons, text, impact)


def evaluate_notification_gate(event: RankedEvent, *, is_new: bool) -> AttentionGateDecision:
    text = _haystack_from_event(event)
    score, reasons = _base_attention_score(text, event.impact)
    score += min(12.0, max(0.0, event.final_score - 55.0) * 0.35)
    if is_new:
        score += 6
        reasons.append("新出现")
    if event.final_score >= 70:
        score += 5
        reasons.append("排序分较高")
    tier = _resolve_tier(score, reasons)
    return _decision(score, tier, reasons, text, event.impact)


def _base_attention_score(text: str, impact: ImpactAssessment) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    fundamental = is_fundamental_impact(impact) or any(
        pattern.lower() in text for pattern in FUNDAMENTAL_TEXT_PATTERNS
    )
    policy_demand = is_policy_demand_impact(impact) or any(
        pattern.lower() in text for pattern in POLICY_DEMAND_PATTERNS
    )
    quantified = bool(NUMERIC_EVIDENCE_RE.search(text))
    leading_fundamental = any(pattern.lower() in text for pattern in LEADING_FUNDAMENTAL_PATTERNS)
    guidance_revision = any(pattern.lower() in text for pattern in GUIDANCE_REVISION_PATTERNS)
    customer_supplier = any(pattern.lower() in text for pattern in CUSTOMER_SUPPLIER_PATTERNS)
    price_margin = any(pattern.lower() in text for pattern in PRICE_MARGIN_PATTERNS)
    capacity_utilization = any(pattern.lower() in text for pattern in CAPACITY_UTILIZATION_PATTERNS)
    balance_sheet = any(pattern.lower() in text for pattern in BALANCE_SHEET_PATTERNS)
    competitive_position = any(pattern.lower() in text for pattern in COMPETITIVE_POSITION_PATTERNS)
    capital_allocation = any(pattern.lower() in text for pattern in CAPITAL_ALLOCATION_PATTERNS)
    lagging_price_reaction = any(
        pattern.lower() in text for pattern in LAGGING_PRICE_REACTION_PATTERNS
    )
    low_risk = (
        impact.direction == Direction.NEGATIVE
        and (
            set(impact.matched_rules) & LOW_PREDICTABILITY_RULE_NAMES
            or any(pattern.lower() in text for pattern in LOW_PREDICTABILITY_RISK_PATTERNS)
        )
    )

    if fundamental:
        score += 38
        reasons.append("基本面信号")
    if leading_fundamental:
        score += 34
        reasons.append("领先基本面恶化/改善信号")
    if guidance_revision:
        score += 24
        reasons.append("业绩/经营指引变化")
    if customer_supplier:
        score += 18
        reasons.append("客户/供应链验证")
    if price_margin:
        score += 18
        reasons.append("价格/毛利率变量")
    if capacity_utilization:
        score += 14
        reasons.append("产能/稼动率变量")
    if balance_sheet:
        score += 14
        reasons.append("资产负债表/现金流变量")
    if competitive_position:
        score += 12
        reasons.append("竞争格局变量")
    if capital_allocation:
        score += 8
        reasons.append("资本配置变量")
    if quantified:
        score += 16
        reasons.append("有可量化数据")
    if policy_demand:
        score += 28
        reasons.append("长期需求/政策链条")
    if any(pattern.lower() in text for pattern in DIRECT_VALUATION_PATTERNS):
        score += 12
        reasons.append("可进入估值分析")
    if impact.direction == Direction.POSITIVE:
        score += 6
        reasons.append("正向变化")
    if low_risk:
        score -= 34
        reasons.append("低可预测风险事件")
    if lagging_price_reaction:
        score -= 28
        reasons.append("事后价格反应")
    if any(pattern.lower() in text for pattern in ROUTINE_OR_LOW_SIGNAL_PATTERNS):
        score -= 32
        reasons.append("日常/低信号公告")
    if any(pattern.lower() in text for pattern in ONE_OFF_OR_ACCOUNTING_NOISE_PATTERNS):
        score -= 22
        reasons.append("一次性/会计噪声")
    if any(pattern.lower() in text for pattern in VAGUE_OPINION_PATTERNS):
        score -= 24
        reasons.append("泛观点/缺少增量事实")
    if not (
        fundamental
        or policy_demand
        or leading_fundamental
        or guidance_revision
        or customer_supplier
        or price_margin
        or capacity_utilization
        or balance_sheet
        or competitive_position
    ):
        score -= 12
        reasons.append("缺少基本面或需求链")
    return max(0.0, score), reasons


def _resolve_tier(score: float, reasons: list[str]) -> str:
    reason_text = " ".join(reasons)
    if "低可预测风险事件" in reason_text and "基本面信号" not in reason_text:
        return "observe"
    if score >= 76:
        return "notify"
    if score >= 45:
        return "model"
    if score >= 35:
        return "observe"
    return "reject"


def _decision(
    score: float,
    tier: str,
    reasons: list[str],
    text: str,
    impact: ImpactAssessment,
) -> AttentionGateDecision:
    return AttentionGateDecision(
        score=score,
        tier=tier,
        reasons=reasons,
        fundamental=is_fundamental_impact(impact)
        or any(pattern.lower() in text for pattern in FUNDAMENTAL_TEXT_PATTERNS)
        or any(pattern.lower() in text for pattern in LEADING_FUNDAMENTAL_PATTERNS)
        or any(pattern.lower() in text for pattern in GUIDANCE_REVISION_PATTERNS)
        or any(pattern.lower() in text for pattern in CUSTOMER_SUPPLIER_PATTERNS)
        or any(pattern.lower() in text for pattern in PRICE_MARGIN_PATTERNS)
        or any(pattern.lower() in text for pattern in CAPACITY_UTILIZATION_PATTERNS)
        or any(pattern.lower() in text for pattern in BALANCE_SHEET_PATTERNS)
        or any(pattern.lower() in text for pattern in COMPETITIVE_POSITION_PATTERNS),
        policy_demand=is_policy_demand_impact(impact)
        or any(pattern.lower() in text for pattern in POLICY_DEMAND_PATTERNS),
        low_predictability_risk=(
            impact.direction == Direction.NEGATIVE
            and (
                bool(set(impact.matched_rules) & LOW_PREDICTABILITY_RULE_NAMES)
                or any(pattern.lower() in text for pattern in LOW_PREDICTABILITY_RISK_PATTERNS)
            )
        ),
        quantified=bool(NUMERIC_EVIDENCE_RE.search(text)),
    )
