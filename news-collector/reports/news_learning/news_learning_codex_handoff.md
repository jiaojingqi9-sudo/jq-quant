# Codex Handoff: News Evidence-to-Review

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Must Respect

- Do not auto-modify code.
- Do not auto-modify live/news production config.
- Do not modify stock system.
- Do not modify crypto system.
- Treat every candidate as research/review only until human approval.

## Packet

- Generated: `2026-07-31T08:10:49.510721+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Review packet JSON: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.json`
- Review packet Markdown: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.md`

## Evidence Artifacts

- `news_memory`: `/Users/jiao/All here/news collector/reports/news_learning/news_memory.jsonl`
  - sha256: `9004ee81979862eb26aa3945ff91ea40d310fcd4bcf425c286662f9b4e027813`
- `news_claims`: `/Users/jiao/All here/news collector/reports/news_learning/news_claims.jsonl`
  - sha256: `f66d7e0ddff31081f1df2c5f8bee0eb19f89134ec1744ed3116887a0c7987370`
- `news_outcomes`: `/Users/jiao/All here/news collector/reports/news_learning/news_outcomes.jsonl`
  - sha256: `e537adb6049171054399e8a082714eac84096754b78ffcbb7ffac1a239468f20`
- `news_attribution`: `/Users/jiao/All here/news collector/reports/news_learning/news_attribution.json`
  - sha256: `c5754d4303a6c7dc2292a7ff5fea44dd3b677ab0de491e8cde760816d4c3950b`
- `news_upgrade_candidates`: `/Users/jiao/All here/news collector/reports/news_learning/news_upgrade_candidates.jsonl`
  - sha256: `3338310f0098186c104e9efda93521705219ba8776530039ce62324a48e7d683`
- `news_promotion_report`: `/Users/jiao/All here/news collector/reports/news_learning/news_promotion_report.json`
  - sha256: `1ef1a7b2014f3b17799c446ee2c60689714b0dd0e7bcdad5995c16d14607b454`
- `news_learning_codex_handoff`: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_codex_handoff.md`
  - sha256: `3a5666444a91a9024f37ae779fed4a82fa55ffcd68356ad1222948c470d0cc07`

## Quick Triage

- Source diversity: `{"source_count": 13, "top_source": "eastmoney-ann", "top_source_share": 0.3443, "herfindahl": 0.176, "over_reliance": false}`
- Candidate count in packet: `30`
- Best sources: eastmoney-topic, gov-nhsa, xinhua-tech, gov-mot, sse_announcements
- Worst sources: csrc_home, spacechina-news, hkex_news, sec_xbrl_usgaap, eastmoney-724
- Best topics: execution, order-growth, 小消息可炒, 订单催化, 供需紧张, 服务器链, 具身智能, 机器人

## Candidates To Review First

- `dac6c8d0-fe84-5803-bd0e-73fe5000d8ba` `add_market_impact_label` target=`system:price_reaction_join` confidence=0.8
  - reason: 新闻已能映射到候选资产，但尚未接入 5m/30m/1d 价格反应，建议作为下一阶段研究标签。
- `7d1604da-8600-5748-8722-9d024ea9f40f` `uprank_source` target=`source:eastmoney-ann` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `34f41e87-9f52-52fe-adc5-a11c7037817c` `uprank_source` target=`source:eastmoney-topic` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `9663d6c8-12b7-591c-8e99-ac482f08709f` `uprank_source` target=`source:gov-mot` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `7713f27c-8e1c-5c2a-9c8c-bb1f8f3a10e5` `uprank_source` target=`source:gov-nhsa` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `23e75f23-4eaa-5785-8f2f-dfe0da12f434` `downrank_source` target=`source:hkex_news` confidence=0.68
  - reason: 滞后/过期新闻比例偏高，建议人工复核是否降权。
- `d3f804d3-568c-5cb7-94cd-9d154f882863` `add_cross_source_verification` target=`source:spacechina-news` confidence=0.66
  - reason: 单来源未验证比例偏高，应先要求交叉验证或补强实体/主题解析，不宜直接按过期新闻降权。
- `ec9ed917-b639-577e-ae3f-58aa13973243` `add_entity_or_topic_filter` target=`topic:execution` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `7afaf36f-9d66-50d8-b6ec-aaa6a02698d0` `add_entity_or_topic_filter` target=`topic:healthcare` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `9264b325-fa8c-5ce1-96e7-f9b07b4bbec9` `add_entity_or_topic_filter` target=`topic:infrastructure` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `235c134f-bacd-555b-a7ae-0d96542546b2` `add_entity_or_topic_filter` target=`topic:logistics` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `ddb9dcdc-c0e7-51d7-9e2e-b3c96232b2cb` `add_entity_or_topic_filter` target=`topic:long-term-demand` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `58f3c3f3-771c-5c77-bad7-65515d35244d` `add_entity_or_topic_filter` target=`topic:order-growth` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `91093ec9-59fb-59da-8735-72ce09c80db2` `add_entity_or_topic_filter` target=`topic:policy` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `e3cf62b9-2b96-5853-9ada-a7a9dac979b8` `add_entity_or_topic_filter` target=`topic:policy-demand` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Ask Codex

请读取上面的 artifact path，先做代码无关的评估：

- 哪些来源应该继续观察、升权、降权或要求交叉验证？
- 哪些主题真的有预测价值，哪些像噪声？
- 哪些 candidate_id 值得下一步让 Codex 改代码或调整采集策略？
- 如果要动代码，请先列计划并等待确认。
