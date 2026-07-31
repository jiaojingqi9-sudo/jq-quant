# News Learning Review Packet

- Generated: `2026-07-31T09:10:52.146395+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Scope: `research/review only`
- Guard: `no auto code changes; no live config changes; no stock/crypto changes`

## Codex Review Prompt

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Artifacts

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

## Source Quality

- Source diversity: `{"source_count": 11, "top_source": "eastmoney-ann", "top_source_share": 0.3684, "herfindahl": 0.1936, "over_reliance": false}`
- Best sources:
  - `eastmoney-topic` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=8
  - `gov-nhsa` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `xinhua-tech` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=2
  - `eastmoney-ann` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=21
  - `gov-mot` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
- Worst sources:
  - `spacechina-news` precision=0.0 duplicate=0.0 stale=0.0 unverified=1.0 n=5
  - `sec_xbrl_usgaap` precision=0.0 duplicate=0.0 stale=0.0 unverified=1.0 n=2
  - `eastmoney-724` precision=0.75 duplicate=0.0 stale=0.0 unverified=0.25 n=4
  - `gov-mofcom` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=1
  - `gov-cnsa` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=1

## Topic Quality

- Best topics:
  - `execution` quality=1.0 precision=1.0 n=7
  - `order-growth` quality=1.0 precision=1.0 n=7
  - `供需紧张` quality=1.0 precision=1.0 n=4
  - `服务器链` quality=1.0 precision=1.0 n=4
  - `小消息可炒` quality=0.9987 precision=1.0 n=5
  - `订单催化` quality=0.9987 precision=1.0 n=5
  - `主线题材` quality=0.9973 precision=1.0 n=4
  - `算力扩张` quality=0.9973 precision=1.0 n=4
- Worst topics:
  - `state-owned-enterprise` quality=0.1875 precision=0.0 n=5
  - `corporate-action` quality=0.2478 precision=0.0 n=2
  - `m&a` quality=0.2478 precision=0.0 n=2
  - `takeover` quality=0.2478 precision=0.0 n=2
  - `technology` quality=0.4532 precision=0.375 n=8
  - `trade` quality=0.8382 precision=1.0 n=1
  - `company` quality=0.8396 precision=1.0 n=1
  - `market` quality=0.8396 precision=1.0 n=1

## Candidates

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
- `07940dc1-3ea3-5c03-8bad-848183587f7a` `add_entity_or_topic_filter` target=`topic:valuation-rerating` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `c743dca3-9ddc-5adb-93a7-3b90a9179d91` `add_entity_or_topic_filter` target=`topic:主线题材` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `20df23ad-9ee2-5a7f-855c-8e38cf4e9a71` `add_entity_or_topic_filter` target=`topic:供需紧张` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `683b92bf-89b8-5dba-b04b-4844ae91a44b` `add_entity_or_topic_filter` target=`topic:小消息可炒` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `bab7bb6e-6eca-58d8-b2dc-05b3f390d5ae` `add_entity_or_topic_filter` target=`topic:待补充` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Review Checklist

- [ ] 确认本包只生成 research artifacts，没有改 live/news production 配置。
- [ ] 优先检查 worst_sources 中是否存在应降权或需要二次验证的来源。
- [ ] 区分 stale 与 unverified；只有真实时效性问题才适合作为降权证据。
- [ ] 检查 best_sources 是否样本量足够，避免因小样本误升权。
- [ ] 逐条查看 ready_for_codex_review 的候选是否符合投资逻辑和来源洁净度要求。
- [ ] 如要改代码或采集策略，另开 Codex 任务并引用 candidate_id，不允许本闭环自动执行。
- [ ] 市场影响字段目前为 null；若候选依赖市场反应，先接入价格序列再评估。
