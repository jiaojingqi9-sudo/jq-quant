# Codex Handoff: News Evidence-to-Review

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Must Respect

- Do not auto-modify code.
- Do not auto-modify live/news production config.
- Do not modify stock system.
- Do not modify crypto system.
- Treat every candidate as research/review only until human approval.

## Packet

- Generated: `2026-07-31T13:41:05.020745+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Review packet JSON: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.json`
- Review packet Markdown: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.md`

## Evidence Artifacts

- `news_memory`: `/Users/jiao/All here/news collector/reports/news_learning/news_memory.jsonl`
  - sha256: `77a4883d9d0301251d974592c3dc56c4ba80412c2614e111ee1f009193394d32`
- `news_claims`: `/Users/jiao/All here/news collector/reports/news_learning/news_claims.jsonl`
  - sha256: `498e04e06579d5ebe4f3c43335ed9be8b31776b860961bd86f3fa8699e79f237`
- `news_outcomes`: `/Users/jiao/All here/news collector/reports/news_learning/news_outcomes.jsonl`
  - sha256: `ec34320f58af77039669c14920bc234d120aad61fd4d4f8fbb7fe5621bca4d76`
- `news_attribution`: `/Users/jiao/All here/news collector/reports/news_learning/news_attribution.json`
  - sha256: `c1aa5ae27aad8d1176d98cabdfb1aeaa09e51cb133f9738980b4c7a3e6d6ab7f`
- `news_upgrade_candidates`: `/Users/jiao/All here/news collector/reports/news_learning/news_upgrade_candidates.jsonl`
  - sha256: `48fc87ee5cb2a63038453d493e849a77b249a8a4705f23b46557808d9fcf4681`
- `news_promotion_report`: `/Users/jiao/All here/news collector/reports/news_learning/news_promotion_report.json`
  - sha256: `8cab73d0eaf469033d7b1b0e387b4d93ba7a6b3f59fa883c6e4a76578193daad`
- `news_learning_codex_handoff`: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_codex_handoff.md`
  - sha256: `970fb735a12d38bd33a55ce4069526041e554a27d49750408de5f68e2b5ebb3d`

## Quick Triage

- Source diversity: `{"source_count": 11, "top_source": "eastmoney-ann", "top_source_share": 0.377, "herfindahl": 0.1965, "over_reliance": false}`
- Candidate count in packet: `30`
- Best sources: eastmoney-topic, gov-nhsa, xinhua-tech, sse_announcements, hkex_news
- Worst sources: csrc_home, sec_xbrl_usgaap, eastmoney-724, eastmoney-ann, gov-mot
- Best topics: execution, order-growth, 小消息可炒, 订单催化, 供需紧张, 服务器链, 主线题材, 算力扩张

## Candidates To Review First

- `086049e1-028e-5853-9de1-4c84755f9eb9` `add_market_impact_label` target=`system:price_reaction_join` confidence=0.8
  - reason: 新闻已能映射到候选资产，但尚未接入 5m/30m/1d 价格反应，建议作为下一阶段研究标签。
- `abfbc8d1-0a63-516a-ba46-089296ff373b` `uprank_source` target=`source:cninfo_latest` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `c5999a42-1086-529b-b9be-cb9c4d90e281` `uprank_source` target=`source:eastmoney-ann` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `788dc926-73ef-56a2-8a72-f259d5bdd830` `uprank_source` target=`source:eastmoney-topic` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `4b2f2682-5b28-52b4-beaf-e19b73190ddb` `uprank_source` target=`source:gov-mot` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `b2f25a05-3c5c-5c69-8764-7506726cb4bc` `uprank_source` target=`source:gov-nhsa` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `0602bc5f-377a-53eb-98a4-5735cde40ca4` `uprank_source` target=`source:sse_announcements` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `149806f4-291b-5933-b5b4-b144a412bf27` `add_cross_source_verification` target=`source:eastmoney-724` confidence=0.66
  - reason: 单来源未验证比例偏高，应先要求交叉验证或补强实体/主题解析，不宜直接按过期新闻降权。
- `166dbdac-1303-5a91-9a25-ed94a08ba055` `add_entity_or_topic_filter` target=`topic:capital-return` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `484628b5-3320-54b3-9082-32ddb40d3cdd` `add_entity_or_topic_filter` target=`topic:company` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `79ce4256-1f2e-5f3f-8640-70506c8ee864` `add_entity_or_topic_filter` target=`topic:compliance-risk` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `72028247-18e5-5119-b816-714128e7b95a` `add_entity_or_topic_filter` target=`topic:corporate-action` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `735e9e53-9d08-5967-b775-30e8bad743f5` `add_entity_or_topic_filter` target=`topic:event-risk` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `ec9ed917-b639-577e-ae3f-58aa13973243` `add_entity_or_topic_filter` target=`topic:execution` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `952959dd-0d27-5824-af82-e019a89b4bd7` `add_entity_or_topic_filter` target=`topic:healthcare` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Ask Codex

请读取上面的 artifact path，先做代码无关的评估：

- 哪些来源应该继续观察、升权、降权或要求交叉验证？
- 哪些主题真的有预测价值，哪些像噪声？
- 哪些 candidate_id 值得下一步让 Codex 改代码或调整采集策略？
- 如果要动代码，请先列计划并等待确认。
