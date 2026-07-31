# Codex Handoff: News Evidence-to-Review

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Must Respect

- Do not auto-modify code.
- Do not auto-modify live/news production config.
- Do not modify stock system.
- Do not modify crypto system.
- Treat every candidate as research/review only until human approval.

## Packet

- Generated: `2026-07-31T09:10:52.146395+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Review packet JSON: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.json`
- Review packet Markdown: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.md`

## Evidence Artifacts

- `news_memory`: `/Users/jiao/All here/news collector/reports/news_learning/news_memory.jsonl`
  - sha256: `a6511de228489fa1f784e1095ef95d74bf06c373f46df04a3b63e603d7556b77`
- `news_claims`: `/Users/jiao/All here/news collector/reports/news_learning/news_claims.jsonl`
  - sha256: `784b52fd68069029be3fd2315b027cfacefd3e6b2c86dac078a227e69f95e235`
- `news_outcomes`: `/Users/jiao/All here/news collector/reports/news_learning/news_outcomes.jsonl`
  - sha256: `e8bb61b50b535eab093d4772172d2601bed99353b98c89896ce51659445d73a3`
- `news_attribution`: `/Users/jiao/All here/news collector/reports/news_learning/news_attribution.json`
  - sha256: `7d75f70a26734257f7a4bba65a0db49646123ac2993e9b1e626be784838ffde2`
- `news_upgrade_candidates`: `/Users/jiao/All here/news collector/reports/news_learning/news_upgrade_candidates.jsonl`
  - sha256: `30a98632f59b02a79c90dfaa8fbcd6f705faf46149d54398aee008dd77d227d7`
- `news_promotion_report`: `/Users/jiao/All here/news collector/reports/news_learning/news_promotion_report.json`
  - sha256: `98654eeacb7be7ddf679e88e7dad7f2e3cb0a459e3960c079a2a96d9d9476331`
- `news_learning_codex_handoff`: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_codex_handoff.md`
  - sha256: `b032337f8bac3d491c0fc83f7ec3baa9a93a086d5856bf9dbb1c46c8295ba46c`

## Quick Triage

- Source diversity: `{"source_count": 11, "top_source": "eastmoney-ann", "top_source_share": 0.3684, "herfindahl": 0.1936, "over_reliance": false}`
- Candidate count in packet: `30`
- Best sources: eastmoney-topic, gov-nhsa, xinhua-tech, eastmoney-ann, gov-mot
- Worst sources: spacechina-news, sec_xbrl_usgaap, eastmoney-724, gov-mofcom, gov-cnsa
- Best topics: execution, order-growth, 供需紧张, 服务器链, 小消息可炒, 订单催化, 主线题材, 算力扩张

## Candidates To Review First

- `2038c5b9-4c3b-5606-846b-cd260f37d7c3` `add_market_impact_label` target=`system:price_reaction_join` confidence=0.8
  - reason: 新闻已能映射到候选资产，但尚未接入 5m/30m/1d 价格反应，建议作为下一阶段研究标签。
- `494135cc-d8a2-568e-bac8-4853992c1a15` `uprank_source` target=`source:eastmoney-724` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `53cdd19f-d374-5190-8050-0f04574aa90b` `uprank_source` target=`source:eastmoney-ann` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `63086c36-7399-5305-be21-71fb251978a7` `uprank_source` target=`source:eastmoney-topic` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `e7b75f4b-0432-591b-b758-22c32abcc918` `uprank_source` target=`source:gov-mot` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `994db36a-48b0-5b7b-bbd5-3a44adb045a0` `uprank_source` target=`source:gov-nhsa` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `7020cc74-9dcc-5cf2-be66-28ff3d72348c` `add_cross_source_verification` target=`source:spacechina-news` confidence=0.66
  - reason: 单来源未验证比例偏高，应先要求交叉验证或补强实体/主题解析，不宜直接按过期新闻降权。
- `cf375522-78d5-554d-a944-33b2784a3282` `add_entity_or_topic_filter` target=`topic:execution` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `9ae29d8f-46c1-5b0e-b7d3-62bec9787ce9` `add_entity_or_topic_filter` target=`topic:healthcare` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `b762db75-2cd1-5d15-abe5-1744dd5a39b6` `add_entity_or_topic_filter` target=`topic:infrastructure` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `26c7b7b2-7d2d-5249-870e-18804985fd84` `add_entity_or_topic_filter` target=`topic:logistics` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `fe9404d1-4c10-5204-8f64-8294da644fd3` `add_entity_or_topic_filter` target=`topic:long-term-demand` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `a29259af-ec41-5605-8665-070ce1d5b33d` `add_entity_or_topic_filter` target=`topic:order-growth` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `56a38fbb-81d7-5eaf-809e-f4b9df7d6784` `add_entity_or_topic_filter` target=`topic:policy` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `ee1d5b30-e75d-5f9f-b14b-68a822640239` `add_entity_or_topic_filter` target=`topic:policy-demand` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Ask Codex

请读取上面的 artifact path，先做代码无关的评估：

- 哪些来源应该继续观察、升权、降权或要求交叉验证？
- 哪些主题真的有预测价值，哪些像噪声？
- 哪些 candidate_id 值得下一步让 Codex 改代码或调整采集策略？
- 如果要动代码，请先列计划并等待确认。
