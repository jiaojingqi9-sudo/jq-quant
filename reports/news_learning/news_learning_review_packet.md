# News Learning Review Packet

- Generated: `2026-07-31T12:46:02.787122+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Scope: `research/review only`
- Guard: `no auto code changes; no live config changes; no stock/crypto changes`

## Codex Review Prompt

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Artifacts

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

## Source Quality

- Source diversity: `{"source_count": 11, "top_source": "eastmoney-ann", "top_source_share": 0.3929, "herfindahl": 0.2079, "over_reliance": false}`
- Best sources:
  - `eastmoney-topic` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `gov-nhsa` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `xinhua-tech` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=1
  - `cninfo_latest` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=4
  - `hkex_news` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=1
- Worst sources:
  - `csrc_home` precision=0.0 duplicate=0.0 stale=1.0 unverified=0.0 n=1
  - `gov-cnsa` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=1
  - `eastmoney-ann` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=22
  - `gov-mot` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `sse_announcements` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6

## Topic Quality

- Best topics:
  - `execution` quality=1.0 precision=1.0 n=6
  - `order-growth` quality=1.0 precision=1.0 n=6
  - `小消息可炒` quality=1.0 precision=1.0 n=5
  - `订单催化` quality=1.0 precision=1.0 n=5
  - `供需紧张` quality=1.0 precision=1.0 n=4
  - `服务器链` quality=1.0 precision=1.0 n=4
  - `主线题材` quality=1.0 precision=1.0 n=3
  - `算力扩张` quality=1.0 precision=1.0 n=3
- Worst topics:
  - `compliance-risk` quality=0.6534 precision=0.6667 n=3
  - `event-risk` quality=0.6534 precision=0.6667 n=3
  - `unknown` quality=0.843 precision=1.0 n=23
  - `infrastructure` quality=0.8594 precision=1.0 n=6
  - `logistics` quality=0.8594 precision=1.0 n=6
  - `company` quality=0.8823 precision=1.0 n=6
  - `market` quality=0.8823 precision=1.0 n=6
  - `technology` quality=0.8841 precision=1.0 n=2

## Candidates

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
- `37b991dd-9ed0-53d3-87a2-257d387e9444` `add_entity_or_topic_filter` target=`topic:m&a` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `8e2c4fcf-cb0f-5712-9a3e-26657483d5c0` `add_entity_or_topic_filter` target=`topic:market` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `10ea5842-7934-5af4-b081-2bb4807601a5` `add_entity_or_topic_filter` target=`topic:order-growth` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `4db2c9d8-9d54-5610-a25c-2c2aa5b25b9d` `add_entity_or_topic_filter` target=`topic:policy` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `ee1d5b30-e75d-5f9f-b14b-68a822640239` `add_entity_or_topic_filter` target=`topic:policy-demand` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Review Checklist

- [ ] 确认本包只生成 research artifacts，没有改 live/news production 配置。
- [ ] 优先检查 worst_sources 中是否存在应降权或需要二次验证的来源。
- [ ] 区分 stale 与 unverified；只有真实时效性问题才适合作为降权证据。
- [ ] 检查 best_sources 是否样本量足够，避免因小样本误升权。
- [ ] 逐条查看 ready_for_codex_review 的候选是否符合投资逻辑和来源洁净度要求。
- [ ] 如要改代码或采集策略，另开 Codex 任务并引用 candidate_id，不允许本闭环自动执行。
- [ ] 市场影响字段目前为 null；若候选依赖市场反应，先接入价格序列再评估。
