# Codex Handoff: News Evidence-to-Review

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Must Respect

- Do not auto-modify code.
- Do not auto-modify live/news production config.
- Do not modify stock system.
- Do not modify crypto system.
- Treat every candidate as research/review only until human approval.

## Packet

- Generated: `2026-08-06T23:44:36.448530+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Review packet JSON: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.json`
- Review packet Markdown: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.md`

## Evidence Artifacts

- `news_memory`: `/Users/jiao/All here/news collector/reports/news_learning/news_memory.jsonl`
  - sha256: `956dc2668a327fb66a999ed6d318a6f420c955c10e623154b00f3d725f1a9b96`
- `news_claims`: `/Users/jiao/All here/news collector/reports/news_learning/news_claims.jsonl`
  - sha256: `53cbf4796b03e047a9b967f33fac898006ce0b73f4316dd59c12733cc60e8834`
- `news_outcomes`: `/Users/jiao/All here/news collector/reports/news_learning/news_outcomes.jsonl`
  - sha256: `d255e53158e7d5b17eac4fdc092d23ac613614b7c5968703a9b54e2c2f5dad56`
- `news_attribution`: `/Users/jiao/All here/news collector/reports/news_learning/news_attribution.json`
  - sha256: `557649b6e43d8e8596459de62aaf49ecdb2cc3a2a8278cb1ffb24cb2235caef7`
- `news_upgrade_candidates`: `/Users/jiao/All here/news collector/reports/news_learning/news_upgrade_candidates.jsonl`
  - sha256: `4277e5570c8794ad5951b123dae6b968595b27eea0a28246e7518c47c8201f98`
- `news_promotion_report`: `/Users/jiao/All here/news collector/reports/news_learning/news_promotion_report.json`
  - sha256: `f303bb2aad90a5e17d55c9da9bfde35d6afa30d3f6b8d57660522751bb11e718`
- `news_learning_codex_handoff`: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_codex_handoff.md`
  - sha256: `0d812132c1d41df2c630bd22305bc03dbec008cc51a4eb3ea77141f372b930d8`

## Quick Triage

- Source diversity: `{"source_count": 13, "top_source": "eastmoney-724", "top_source_share": 0.3175, "herfindahl": 0.161, "over_reliance": false}`
- Candidate count in packet: `30`
- Best sources: eastmoney-ann, eastmoney-topic, sse_announcements, cninfo_latest, gov-nhsa
- Worst sources: csrc_home, spacechina-news, sec_xbrl_usgaap, eastmoney-724, gov-mofcom
- Best topics: execution, order-growth, AI落地, 制造升级, 合作催化, 待补充, 政策催化, company

## Candidates To Review First

- `b19f4163-bef4-5168-8220-4012100c3d30` `add_market_impact_label` target=`system:price_reaction_join` confidence=0.8
  - reason: 新闻已能映射到候选资产，但尚未接入 5m/30m/1d 价格反应，建议作为下一阶段研究标签。
- `eee92fd8-b437-57bc-9683-e4e182ee8af7` `uprank_source` target=`source:cninfo_latest` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `0ac42f9c-e0be-54f3-b981-d1cf69358be9` `uprank_source` target=`source:eastmoney-ann` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `f823aee8-b5a9-5e22-8ed8-aacdbe1dd64c` `uprank_source` target=`source:gov-mot` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `573602a6-8ba0-52c4-addc-a73d1f46605a` `uprank_source` target=`source:gov-nhsa` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `2c58d2f6-f5bc-56a7-aa41-3ba8955d34e6` `add_cross_source_verification` target=`source:eastmoney-724` confidence=0.66
  - reason: 单来源未验证比例偏高，应先要求交叉验证或补强实体/主题解析，不宜直接按过期新闻降权。
- `a2d5dca1-fb23-5226-80b6-a98610c5a3fc` `add_cross_source_verification` target=`source:spacechina-news` confidence=0.66
  - reason: 单来源未验证比例偏高，应先要求交叉验证或补强实体/主题解析，不宜直接按过期新闻降权。
- `22e53cb7-2d53-50e4-bd18-ec1cda77c2b0` `add_entity_or_topic_filter` target=`topic:AI落地` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `d471d46e-4a7f-5d1c-b6cf-3d8a119b868d` `add_entity_or_topic_filter` target=`topic:capital-return` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `c746d514-5e30-5fbb-8035-85fee3ccb455` `add_entity_or_topic_filter` target=`topic:corporate-action` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `114b6a15-25f6-5d37-9324-4dfad89a82be` `add_entity_or_topic_filter` target=`topic:execution` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `f8230b4d-6461-5ef4-94fa-06148e3f0ac9` `add_entity_or_topic_filter` target=`topic:healthcare` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `dd85e6bb-2a7a-5f48-82cd-a992f45a315f` `add_entity_or_topic_filter` target=`topic:infrastructure` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `5891da23-0c5d-5748-9652-8c2f19cbe942` `add_entity_or_topic_filter` target=`topic:logistics` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `08ca20ea-e9d1-5a26-9f18-e1d1b5e7d311` `add_entity_or_topic_filter` target=`topic:long-term-demand` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Ask Codex

请读取上面的 artifact path，先做代码无关的评估：

- 哪些来源应该继续观察、升权、降权或要求交叉验证？
- 哪些主题真的有预测价值，哪些像噪声？
- 哪些 candidate_id 值得下一步让 Codex 改代码或调整采集策略？
- 如果要动代码，请先列计划并等待确认。
