# News Learning Review Packet

- Generated: `2026-07-31T10:50:57.367711+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Scope: `research/review only`
- Guard: `no auto code changes; no live config changes; no stock/crypto changes`

## Codex Review Prompt

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Artifacts

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

## Source Quality

- Source diversity: `{"source_count": 10, "top_source": "eastmoney-ann", "top_source_share": 0.4032, "herfindahl": 0.2144, "over_reliance": false}`
- Best sources:
  - `eastmoney-topic` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `gov-nhsa` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `xinhua-tech` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=1
  - `cninfo_latest` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `eastmoney-ann` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=25
- Worst sources:
  - `csrc_home` precision=0.0 duplicate=0.0 stale=1.0 unverified=0.0 n=1
  - `sec_xbrl_usgaap` precision=0.0 duplicate=0.0 stale=0.0 unverified=1.0 n=1
  - `eastmoney-724` precision=0.5 duplicate=0.0 stale=0.0 unverified=0.5 n=4
  - `sse_announcements` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `gov-mot` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6

## Topic Quality

- Best topics:
  - `供需紧张` quality=1.0 precision=1.0 n=4
  - `服务器链` quality=1.0 precision=1.0 n=4
  - `主线题材` quality=1.0 precision=1.0 n=3
  - `算力扩张` quality=1.0 precision=1.0 n=3
  - `execution` quality=0.9989 precision=1.0 n=5
  - `order-growth` quality=0.9989 precision=1.0 n=5
  - `小消息可炒` quality=0.9945 precision=1.0 n=6
  - `订单催化` quality=0.9945 precision=1.0 n=6
- Worst topics:
  - `compliance-risk` quality=0.7602 precision=0.875 n=8
  - `event-risk` quality=0.7602 precision=0.875 n=8
  - `corporate-action` quality=0.7689 precision=0.75 n=4
  - `m&a` quality=0.7689 precision=0.75 n=4
  - `takeover` quality=0.7689 precision=0.75 n=4
  - `unknown` quality=0.8001 precision=0.9048 n=21
  - `company` quality=0.8538 precision=1.0 n=6
  - `market` quality=0.8538 precision=1.0 n=6

## Candidates

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
- `d6e1e17b-1406-5e34-9d28-46fa0d9d0f44` `add_entity_or_topic_filter` target=`topic:infrastructure` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `daa48cbf-69b1-5253-ba76-3989c02eb71e` `add_entity_or_topic_filter` target=`topic:logistics` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `fa76f91f-33e6-566b-b54a-9597359955dd` `add_entity_or_topic_filter` target=`topic:long-term-demand` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `93941ab7-8cc2-5eb4-9cc5-dc461f3587cc` `add_entity_or_topic_filter` target=`topic:m&a` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `111aa92e-cd47-570c-87b1-1a03c4832226` `add_entity_or_topic_filter` target=`topic:market` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Review Checklist

- [ ] 确认本包只生成 research artifacts，没有改 live/news production 配置。
- [ ] 优先检查 worst_sources 中是否存在应降权或需要二次验证的来源。
- [ ] 区分 stale 与 unverified；只有真实时效性问题才适合作为降权证据。
- [ ] 检查 best_sources 是否样本量足够，避免因小样本误升权。
- [ ] 逐条查看 ready_for_codex_review 的候选是否符合投资逻辑和来源洁净度要求。
- [ ] 如要改代码或采集策略，另开 Codex 任务并引用 candidate_id，不允许本闭环自动执行。
- [ ] 市场影响字段目前为 null；若候选依赖市场反应，先接入价格序列再评估。
