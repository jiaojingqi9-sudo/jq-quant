# Codex Handoff: News Evidence-to-Review

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Must Respect

- Do not auto-modify code.
- Do not auto-modify live/news production config.
- Do not modify stock system.
- Do not modify crypto system.
- Treat every candidate as research/review only until human approval.

## Packet

- Generated: `2026-07-31T12:46:02.787122+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Review packet JSON: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.json`
- Review packet Markdown: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.md`

## Evidence Artifacts

- `news_memory`: `/Users/jiao/All here/news collector/reports/news_learning/news_memory.jsonl`
  - sha256: `de6fb500fbc546610ccd5e9e7c3e341a14b3121c464345ae1d3e75dc36db84b2`
- `news_claims`: `/Users/jiao/All here/news collector/reports/news_learning/news_claims.jsonl`
  - sha256: `20dc727d465d3d8733721e70520548695eae1bddd750a44edc6545a7253d1ad3`
- `news_outcomes`: `/Users/jiao/All here/news collector/reports/news_learning/news_outcomes.jsonl`
  - sha256: `ac677edf414e428b9bfb7af50d557d6cef7896156f57f1416d7354d9b23e99f7`
- `news_attribution`: `/Users/jiao/All here/news collector/reports/news_learning/news_attribution.json`
  - sha256: `d2bae04a984c0a20df58d231296969a2a13261a2c73b482beb28f3b941e1f291`
- `news_upgrade_candidates`: `/Users/jiao/All here/news collector/reports/news_learning/news_upgrade_candidates.jsonl`
  - sha256: `feaeab3e32f2ef8d26782488f1bd8905dd395ab9f47ee684e373b74135d87b90`
- `news_promotion_report`: `/Users/jiao/All here/news collector/reports/news_learning/news_promotion_report.json`
  - sha256: `63328a41f9fafcf2fc98bb9fb4f2a3c8fa24a18e293dc3238279b8f8758a2882`
- `news_learning_codex_handoff`: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_codex_handoff.md`
  - sha256: `8a221966b08e28038b576fa384c19eb45ed552fa935e3ee685c318b45f1ebfe8`

## Quick Triage

- Source diversity: `{"source_count": 11, "top_source": "eastmoney-ann", "top_source_share": 0.3929, "herfindahl": 0.2079, "over_reliance": false}`
- Candidate count in packet: `30`
- Best sources: eastmoney-topic, gov-nhsa, xinhua-tech, cninfo_latest, hkex_news
- Worst sources: csrc_home, gov-cnsa, eastmoney-ann, gov-mot, sse_announcements
- Best topics: execution, order-growth, 小消息可炒, 订单催化, 供需紧张, 服务器链, 主线题材, 算力扩张

## Candidates To Review First

- `23d16d84-65f4-5a07-bc4a-bb8f1d37c1b6` `add_market_impact_label` target=`system:price_reaction_join` confidence=0.8
  - reason: 新闻已能映射到候选资产，但尚未接入 5m/30m/1d 价格反应，建议作为下一阶段研究标签。
- `b1187ab6-c4e5-5de5-bdec-f6cb2358f54b` `uprank_source` target=`source:cninfo_latest` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `ed141e6d-b322-5165-8cec-fff73b82cd07` `uprank_source` target=`source:eastmoney-ann` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `6c3477b1-3b3b-54c3-b773-1f99d351f5c6` `uprank_source` target=`source:eastmoney-topic` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `37f4b0db-5578-50a1-ae0d-6f16e28f0124` `uprank_source` target=`source:gov-mot` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `994db36a-48b0-5b7b-bbd5-3a44adb045a0` `uprank_source` target=`source:gov-nhsa` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `adca2555-d48e-5ded-83de-f470daf1d04a` `uprank_source` target=`source:sse_announcements` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `74f46280-e495-5dfc-b4a3-893f6848c921` `add_entity_or_topic_filter` target=`topic:capital-return` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `ee12e6ea-95bc-5db9-800c-5f6e306c4a84` `add_entity_or_topic_filter` target=`topic:company` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `03dd586b-eb43-5dd5-a58f-a408344144aa` `add_entity_or_topic_filter` target=`topic:corporate-action` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `ab0e16f9-f0ac-5758-870b-c8654d009081` `add_entity_or_topic_filter` target=`topic:execution` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `9ae29d8f-46c1-5b0e-b7d3-62bec9787ce9` `add_entity_or_topic_filter` target=`topic:healthcare` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `14e849ad-464b-5134-8e78-93a809befa82` `add_entity_or_topic_filter` target=`topic:infrastructure` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `0ef4a434-3b5d-5a1b-bad2-21abeaf25a87` `add_entity_or_topic_filter` target=`topic:logistics` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `fe9404d1-4c10-5204-8f64-8294da644fd3` `add_entity_or_topic_filter` target=`topic:long-term-demand` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Ask Codex

请读取上面的 artifact path，先做代码无关的评估：

- 哪些来源应该继续观察、升权、降权或要求交叉验证？
- 哪些主题真的有预测价值，哪些像噪声？
- 哪些 candidate_id 值得下一步让 Codex 改代码或调整采集策略？
- 如果要动代码，请先列计划并等待确认。
