# News Learning Review Packet

- Generated: `2026-08-06T23:44:36.448530+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Scope: `research/review only`
- Guard: `no auto code changes; no live config changes; no stock/crypto changes`

## Codex Review Prompt

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Artifacts

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

## Source Quality

- Source diversity: `{"source_count": 13, "top_source": "eastmoney-724", "top_source_share": 0.3175, "herfindahl": 0.161, "over_reliance": false}`
- Best sources:
  - `eastmoney-ann` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=10
  - `eastmoney-topic` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=2
  - `sse_announcements` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=2
  - `cninfo_latest` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `gov-nhsa` precision=0.8333 duplicate=0.0 stale=0.1667 unverified=0.0 n=6
- Worst sources:
  - `csrc_home` precision=0.0 duplicate=0.0 stale=1.0 unverified=0.0 n=1
  - `spacechina-news` precision=0.0 duplicate=0.0 stale=0.0 unverified=1.0 n=3
  - `sec_xbrl_usgaap` precision=0.0 duplicate=0.0 stale=0.0 unverified=1.0 n=2
  - `eastmoney-724` precision=0.25 duplicate=0.0 stale=0.0 unverified=0.75 n=20
  - `gov-mofcom` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=2

## Topic Quality

- Best topics:
  - `execution` quality=0.9734 precision=1.0 n=3
  - `order-growth` quality=0.9734 precision=1.0 n=3
  - `AI落地` quality=0.9588 precision=1.0 n=8
  - `制造升级` quality=0.9588 precision=1.0 n=8
  - `合作催化` quality=0.9559 precision=1.0 n=9
  - `待补充` quality=0.9232 precision=1.0 n=7
  - `政策催化` quality=0.923 precision=1.0 n=2
  - `company` quality=0.9213 precision=1.0 n=2
- Worst topics:
  - `unknown` quality=0.0 precision=0.0 n=15
  - `compliance-risk` quality=0.1457 precision=0.0 n=1
  - `event-risk` quality=0.1457 precision=0.0 n=1
  - `state-owned-enterprise` quality=0.1876 precision=0.0 n=3
  - `technology` quality=0.5144 precision=0.5 n=6
  - `corporate-action` quality=0.7603 precision=0.7 n=10
  - `m&a` quality=0.7603 precision=0.7 n=10
  - `takeover` quality=0.7603 precision=0.7 n=10

## Candidates

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
- `cd822200-8380-5b52-9b9e-b9a24d534455` `add_entity_or_topic_filter` target=`topic:m&a` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `c83cfdd3-4173-5d74-8300-e9a42783578e` `add_entity_or_topic_filter` target=`topic:order-growth` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `e7ccffb5-c621-578f-9a86-f539b572fa21` `add_entity_or_topic_filter` target=`topic:policy` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `c3840244-0ffe-50d6-992d-c8a1bd710ab1` `add_entity_or_topic_filter` target=`topic:policy-demand` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `1cabaa8f-72b1-5606-afe2-e27e1df0e1a3` `add_entity_or_topic_filter` target=`topic:shareholder-exit` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Review Checklist

- [ ] 确认本包只生成 research artifacts，没有改 live/news production 配置。
- [ ] 优先检查 worst_sources 中是否存在应降权或需要二次验证的来源。
- [ ] 区分 stale 与 unverified；只有真实时效性问题才适合作为降权证据。
- [ ] 检查 best_sources 是否样本量足够，避免因小样本误升权。
- [ ] 逐条查看 ready_for_codex_review 的候选是否符合投资逻辑和来源洁净度要求。
- [ ] 如要改代码或采集策略，另开 Codex 任务并引用 candidate_id，不允许本闭环自动执行。
- [ ] 市场影响字段目前为 null；若候选依赖市场反应，先接入价格序列再评估。
