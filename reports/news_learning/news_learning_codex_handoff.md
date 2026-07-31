# Codex Handoff: News Evidence-to-Review

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Must Respect

- Do not auto-modify code.
- Do not auto-modify live/news production config.
- Do not modify stock system.
- Do not modify crypto system.
- Treat every candidate as research/review only until human approval.

## Packet

- Generated: `2026-07-31T10:50:57.367711+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Review packet JSON: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.json`
- Review packet Markdown: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.md`

## Evidence Artifacts

- `news_memory`: `/Users/jiao/All here/news collector/reports/news_learning/news_memory.jsonl`
  - sha256: `7a20b180805f40fec581a6bff195c99a48702efa49fbc02f6378d33ab51e4996`
- `news_claims`: `/Users/jiao/All here/news collector/reports/news_learning/news_claims.jsonl`
  - sha256: `2096d27c5d61bdf6306ea4df62ffba20f2b535939b0bb352f1d1788d809840bb`
- `news_outcomes`: `/Users/jiao/All here/news collector/reports/news_learning/news_outcomes.jsonl`
  - sha256: `4290f38b8c46d934fb6e399e83ba77f02dec2245d594271919405b12ca561a22`
- `news_attribution`: `/Users/jiao/All here/news collector/reports/news_learning/news_attribution.json`
  - sha256: `af768ae42821e0e30927d79956826bc51392555cab9ddfc5aa389738ca49ec09`
- `news_upgrade_candidates`: `/Users/jiao/All here/news collector/reports/news_learning/news_upgrade_candidates.jsonl`
  - sha256: `7ad0c16516f25ae0efae9901b8e5c2b6f801a39acd513edf3f5990dcf0e9cef5`
- `news_promotion_report`: `/Users/jiao/All here/news collector/reports/news_learning/news_promotion_report.json`
  - sha256: `a80f3b978de515782a5330842f15cd51934c2b94035871b032dc1a49f65efb2b`
- `news_learning_codex_handoff`: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_codex_handoff.md`
  - sha256: `6a3c964f4f354b3486c6bfe4d367c746b0519e8b4a71f8d2b687f6f16c370e47`

## Quick Triage

- Source diversity: `{"source_count": 10, "top_source": "eastmoney-ann", "top_source_share": 0.4032, "herfindahl": 0.2144, "over_reliance": false}`
- Candidate count in packet: `30`
- Best sources: eastmoney-topic, gov-nhsa, xinhua-tech, cninfo_latest, eastmoney-ann
- Worst sources: csrc_home, sec_xbrl_usgaap, eastmoney-724, sse_announcements, gov-mot
- Best topics: 供需紧张, 服务器链, 主线题材, 算力扩张, execution, order-growth, 小消息可炒, 订单催化

## Candidates To Review First

- `c02164e2-b76b-53a1-ab47-5d2a983ace56` `add_market_impact_label` target=`system:price_reaction_join` confidence=0.8
  - reason: 新闻已能映射到候选资产，但尚未接入 5m/30m/1d 价格反应，建议作为下一阶段研究标签。
- `5a196e45-5254-5f60-80f6-529427de65cf` `uprank_source` target=`source:cninfo_latest` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `27882c75-6b3f-5ea3-bbb7-3a8dba0659e6` `uprank_source` target=`source:eastmoney-ann` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `9bee32f1-e36c-510a-b508-dcb4c531b53c` `uprank_source` target=`source:eastmoney-topic` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `a7b45353-119f-5651-b9f1-41d45198ba72` `uprank_source` target=`source:gov-mot` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `b071e71c-008e-5952-b886-7645c405056f` `uprank_source` target=`source:gov-nhsa` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `c5c86905-090c-549c-aec3-e85520b071bf` `uprank_source` target=`source:sse_announcements` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `9b08486d-0d59-5491-a790-b489d482346f` `add_cross_source_verification` target=`source:eastmoney-724` confidence=0.66
  - reason: 单来源未验证比例偏高，应先要求交叉验证或补强实体/主题解析，不宜直接按过期新闻降权。
- `d2695b98-3191-531a-80c8-f74658ac3faa` `add_entity_or_topic_filter` target=`topic:capital-return` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `697615b7-14ce-5c97-8edf-6d3443d607f1` `add_entity_or_topic_filter` target=`topic:company` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `02f82d0a-c4eb-58fa-9e36-6c7764480148` `add_entity_or_topic_filter` target=`topic:compliance-risk` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `322d80d9-9894-5253-bc23-34f44cf6510a` `add_entity_or_topic_filter` target=`topic:corporate-action` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `1f447eb4-5d39-527f-b864-c5fb16e4ebf5` `add_entity_or_topic_filter` target=`topic:event-risk` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `df2ce7b8-9acb-55cc-a336-4662a544438d` `add_entity_or_topic_filter` target=`topic:execution` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `d8e6eb96-29f1-50bc-b3f0-f56437ba3c49` `add_entity_or_topic_filter` target=`topic:healthcare` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Ask Codex

请读取上面的 artifact path，先做代码无关的评估：

- 哪些来源应该继续观察、升权、降权或要求交叉验证？
- 哪些主题真的有预测价值，哪些像噪声？
- 哪些 candidate_id 值得下一步让 Codex 改代码或调整采集策略？
- 如果要动代码，请先列计划并等待确认。
