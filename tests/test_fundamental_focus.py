from __future__ import annotations

from market_news.domain.models import Direction, EventType, ImpactAssessment, Market, RankedEvent
from market_news.services.alerts import RuleBasedAlertEngine
from market_news.services.fundamental_focus import (
    evaluate_model_call_gate,
    evaluate_notification_gate,
    is_fundamental_impact,
    is_low_predictability_risk,
    is_policy_access_opening_impact,
)
from market_news.services.ranking import WeightedEventRanker


class StaticCluster:
    cluster_id = "cluster"
    headline = "公司因涉嫌信披违规被证监会立案"
    doc_count = 1
    source_ids = ["csrc_home"]
    avg_source_trust = 0.98

    @property
    def last_seen_at(self):
        from market_news.common import utcnow

        return utcnow()


class GateCluster:
    source_ids = ["cls"]
    avg_source_trust = 0.96
    doc_count = 1
    entities = ["工信部"]
    combined_text = "工信部发布政策，推动算力基础设施、国产芯片和数据中心协同建设。"


def _impact(*, direction: Direction, rules: list[str], themes: list[str] | None = None) -> ImpactAssessment:
    return ImpactAssessment(
        event_type=EventType.COMPANY,
        direction=direction,
        affected_markets=[Market.CN_A],
        affected_sectors=["broad-market"],
        affected_themes=themes or [],
        severity=0.82,
        confidence=0.82,
        matched_rules=rules,
        rationale=rules,
    )


def test_earnings_impact_is_fundamental_focus() -> None:
    impact = _impact(
        direction=Direction.POSITIVE,
        rules=["Earnings Upgrade: matched 净利润同比增长"],
        themes=["fundamental-improvement"],
    )

    assert is_fundamental_impact(impact)


def test_regulatory_risk_is_not_promoted_to_high_priority_alert() -> None:
    event = RankedEvent(
        cluster_id="risk",
        headline="公司因涉嫌信披违规被证监会立案",
        impact=_impact(direction=Direction.NEGATIVE, rules=["Regulatory Pressure"]),
        heat_score=90,
        importance_score=90,
        confidence_score=90,
        market_relevance_score=90,
        final_score=88,
    )
    alerts = RuleBasedAlertEngine().generate([event], [], seen_cluster_ids=set())

    assert is_low_predictability_risk(event)
    assert len(alerts) == 1
    assert alerts[0].level.value == "medium"


def test_ranking_boosts_fundamentals_and_damps_low_predictability_risk() -> None:
    ranker = WeightedEventRanker()
    cluster = StaticCluster()
    fundamental = ranker.rank(
        cluster,
        _impact(direction=Direction.POSITIVE, rules=["Earnings Upgrade"], themes=["earnings"]),
    )
    risk = ranker.rank(
        cluster,
        _impact(direction=Direction.NEGATIVE, rules=["Regulatory Pressure"]),
    )

    assert fundamental.final_score > risk.final_score


def test_policy_demand_gate_can_call_model_without_spamming_notification() -> None:
    gate = evaluate_model_call_gate(
        GateCluster(),
        _impact(direction=Direction.POSITIVE, rules=["Valuation Demand Re-rating"], themes=["policy-demand"]),
    )

    assert gate.should_call_model
    assert not gate.should_notify
    assert "长期需求/政策链条" in gate.reasons


def test_low_predictability_risk_gate_does_not_call_model() -> None:
    class RiskCluster(GateCluster):
        combined_text = "公司因涉嫌信披违规被证监会立案调查。"

    gate = evaluate_model_call_gate(
        RiskCluster(),
        _impact(direction=Direction.NEGATIVE, rules=["Regulatory Pressure"]),
    )

    assert not gate.should_call_model
    assert "低可预测风险事件" in gate.reasons


def test_earnings_notification_gate_requires_fundamental_chain() -> None:
    event = RankedEvent(
        cluster_id="earnings",
        headline="公司一季度归母净利润同比增长230.43%，经营性现金流改善",
        impact=_impact(direction=Direction.POSITIVE, rules=["Earnings Upgrade"], themes=["earnings"]),
        heat_score=82,
        importance_score=82,
        confidence_score=82,
        market_relevance_score=82,
        final_score=82,
    )

    gate = evaluate_notification_gate(event, is_new=True)

    assert gate.should_notify
    assert gate.fundamental
    assert gate.quantified


def test_lagging_price_reaction_without_order_detail_does_not_call_model() -> None:
    class PriceReactionCluster(GateCluster):
        combined_text = "POET Technologies美股盘前跌超40%。"

    gate = evaluate_model_call_gate(
        PriceReactionCluster(),
        _impact(direction=Direction.NEGATIVE, rules=["No configured rule matched"]),
    )

    assert not gate.should_call_model
    assert "事后价格反应" in gate.reasons


def test_customer_order_loss_is_leading_fundamental_signal() -> None:
    class OrderLossCluster(GateCluster):
        combined_text = "公司失去Celestial AI采购订单，相关客户暂停采购。"

    gate = evaluate_model_call_gate(
        OrderLossCluster(),
        _impact(direction=Direction.NEGATIVE, rules=["Customer Order Loss"]),
    )

    assert gate.should_call_model
    assert gate.fundamental
    assert "领先基本面恶化/改善信号" in gate.reasons


def test_guidance_cut_is_model_worthy_fundamental_signal() -> None:
    class GuidanceCluster(GateCluster):
        combined_text = "公司下调全年业绩指引，主要客户去库存导致订单减少。"

    gate = evaluate_model_call_gate(
        GuidanceCluster(),
        _impact(direction=Direction.NEGATIVE, rules=["Guidance Cut"]),
    )

    assert gate.should_call_model
    assert gate.fundamental
    assert "业绩/经营指引变化" in gate.reasons
    assert "客户/供应链验证" in gate.reasons


def test_accounting_noise_is_not_promoted_without_core_business_signal() -> None:
    class AccountingCluster(GateCluster):
        combined_text = "公司因会计政策变更确认一次性收益，公允价值变动增加当期利润。"

    gate = evaluate_model_call_gate(
        AccountingCluster(),
        _impact(direction=Direction.POSITIVE, rules=["No configured rule matched"]),
    )

    assert not gate.should_notify
    assert "一次性/会计噪声" in gate.reasons


def test_price_margin_and_capacity_can_trigger_model_call() -> None:
    class MarginCluster(GateCluster):
        combined_text = "公司产品提价，产能利用率提升，毛利率改善明显。"

    gate = evaluate_model_call_gate(
        MarginCluster(),
        _impact(direction=Direction.POSITIVE, rules=["Margin Improvement"]),
    )

    assert gate.should_call_model
    assert "价格/毛利率变量" in gate.reasons
    assert "产能/稼动率变量" in gate.reasons


def test_commercial_space_access_opening_is_high_priority_policy_alert() -> None:
    event = RankedEvent(
        cluster_id="space",
        headline="国家航天局召开商业航天高质量发展企业圆桌会议",
        impact=ImpactAssessment(
            event_type=EventType.POLICY,
            direction=Direction.POSITIVE,
            affected_markets=[Market.CN_A, Market.HK],
            affected_sectors=["aerospace-defense", "satellite-internet"],
            affected_themes=["commercial-space", "satellite-internet", "policy-demand"],
            severity=0.84,
            confidence=0.76,
            matched_rules=["Commercial Space Access Opening"],
            rationale=[
                "Commercial Space Access Opening: matched 国家航天局, 商业航天, "
                "许可准入, 发射申请, 频率协调, 一站式, 公共服务平台."
            ],
        ),
        heat_score=70,
        importance_score=88,
        confidence_score=78,
        market_relevance_score=90,
        final_score=92,
    )

    alerts = RuleBasedAlertEngine().generate([event], [], seen_cluster_ids=set())

    assert is_policy_access_opening_impact(event.impact)
    assert alerts
    assert alerts[0].level.value == "high"
