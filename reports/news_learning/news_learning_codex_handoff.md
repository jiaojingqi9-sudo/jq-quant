# Codex Handoff: News Evidence-to-Review

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Must Respect

- Do not auto-modify code.
- Do not auto-modify live/news production config.
- Do not modify stock system.
- Do not modify crypto system.
- Treat every candidate as research/review only until human approval.

## Packet

- Generated: `2026-07-31T06:43:49.034911+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Review packet JSON: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.json`
- Review packet Markdown: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.md`

## Evidence Artifacts

- `news_memory`: `/Users/jiao/All here/news collector/reports/news_learning/news_memory.jsonl`
  - sha256: `c500d6ae511cc51dd5aa1c065cb187d545b41022d8039d8ea833edc7179e3562`
- `news_claims`: `/Users/jiao/All here/news collector/reports/news_learning/news_claims.jsonl`
  - sha256: `4dd99c5e95547e50cc968629aecd89ac09cd0417ac018cc94ce1733a2a4ea9dc`
- `news_outcomes`: `/Users/jiao/All here/news collector/reports/news_learning/news_outcomes.jsonl`
  - sha256: `9a1a0778843b07a6cf657c7c77a337ada0ffa04cf44eb7af46197ba5bb54cf88`
- `news_attribution`: `/Users/jiao/All here/news collector/reports/news_learning/news_attribution.json`
  - sha256: `bca145003e4882c3a84befdfb9b1e95f0e87e92b1e76562e7bf1b69551ad24d7`
- `news_upgrade_candidates`: `/Users/jiao/All here/news collector/reports/news_learning/news_upgrade_candidates.jsonl`
  - sha256: `faa99b12516da53c437a5a2db01a7eab1e10fdc27e3a12f6298ba98adb686134`
- `news_promotion_report`: `/Users/jiao/All here/news collector/reports/news_learning/news_promotion_report.json`
  - sha256: `512b66122b5de1f82b2b54533836761e26ef70e509fa22ba27cd6be55ff7f364`
- `news_learning_codex_handoff`: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_codex_handoff.md`
  - sha256: `d9fe97c7b839661bc1c6f7f107f1eda9344f7492ba417258275feae6a1d69851`

## Quick Triage

- Source diversity: `{"source_count": 13, "top_source": "eastmoney-724", "top_source_share": 0.375, "herfindahl": 0.1907, "over_reliance": false}`
- Candidate count in packet: `30`
- Best sources: eastmoney-topic, xinhua-tech, gov-mot, sse_announcements, gov-cnsa
- Worst sources: csrc_home, spacechina-news, hkex_news, sec_xbrl_usgaap, eastmoney-724
- Best topics: execution, order-growth, 小消息可炒, 订单催化, long-term-demand, policy-demand, valuation-rerating, 供需紧张

## Candidates To Review First

- `f09b6bce-7cfb-5393-83cb-4a43a2b0e62f` `add_market_impact_label` target=`system:price_reaction_join` confidence=0.8
  - reason: 新闻已能映射到候选资产，但尚未接入 5m/30m/1d 价格反应，建议作为下一阶段研究标签。
- `b445d6df-8b96-5a96-bc46-afe646b20f36` `add_cross_source_verification` target=`source:cninfo_latest` confidence=0.74
  - reason: 出现被反驳/误判信号，进入提醒前应要求二次验证。
- `588093ec-ab48-59d8-8923-605734f80dcd` `uprank_source` target=`source:eastmoney-topic` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `9663d6c8-12b7-591c-8e99-ac482f08709f` `uprank_source` target=`source:gov-mot` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `f134d03f-e579-5b8f-8403-7f3d07979653` `downrank_source` target=`source:hkex_news` confidence=0.68
  - reason: 滞后/过期新闻比例偏高，建议人工复核是否降权。
- `c031fc5e-b737-5db9-bd5e-afb17108309d` `add_cross_source_verification` target=`source:eastmoney-724` confidence=0.66
  - reason: 单来源未验证比例偏高，应先要求交叉验证或补强实体/主题解析，不宜直接按过期新闻降权。
- `08da806d-3a60-5857-aebb-90f5cf35a358` `add_cross_source_verification` target=`source:spacechina-news` confidence=0.66
  - reason: 单来源未验证比例偏高，应先要求交叉验证或补强实体/主题解析，不宜直接按过期新闻降权。
- `7c16e954-ad5f-57b0-95cf-b8dc86ffc816` `add_entity_or_topic_filter` target=`topic:capital-return` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `8f04b2f9-5ca4-5194-a1e7-ff8e7178ff4f` `add_entity_or_topic_filter` target=`topic:compliance-risk` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `fce51eca-2d0e-5062-8bea-44e029ccbf11` `add_entity_or_topic_filter` target=`topic:earnings` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `68bc634d-bb11-522d-b1d4-4ff16d6cc70a` `add_entity_or_topic_filter` target=`topic:event-risk` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `d97017ff-ba00-5e89-b361-54c761e5d181` `add_entity_or_topic_filter` target=`topic:execution` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `fedb754a-fc75-5310-8a6e-67ddfe2bac33` `add_entity_or_topic_filter` target=`topic:fundamental-improvement` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `9264b325-fa8c-5ce1-96e7-f9b07b4bbec9` `add_entity_or_topic_filter` target=`topic:infrastructure` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `235c134f-bacd-555b-a7ae-0d96542546b2` `add_entity_or_topic_filter` target=`topic:logistics` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Ask Codex

请读取上面的 artifact path，先做代码无关的评估：

- 哪些来源应该继续观察、升权、降权或要求交叉验证？
- 哪些主题真的有预测价值，哪些像噪声？
- 哪些 candidate_id 值得下一步让 Codex 改代码或调整采集策略？
- 如果要动代码，请先列计划并等待确认。
