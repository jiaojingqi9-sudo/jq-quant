# News Learning Review Packet

- Generated: `2026-07-31T08:10:49.510721+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Scope: `research/review only`
- Guard: `no auto code changes; no live config changes; no stock/crypto changes`

## Codex Review Prompt

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Artifacts

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

## Source Quality

- Source diversity: `{"source_count": 13, "top_source": "eastmoney-ann", "top_source_share": 0.3443, "herfindahl": 0.176, "over_reliance": false}`
- Best sources:
  - `eastmoney-topic` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=8
  - `gov-nhsa` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=8
  - `xinhua-tech` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=2
  - `gov-mot` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `sse_announcements` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=1
- Worst sources:
  - `csrc_home` precision=0.0 duplicate=0.0 stale=1.0 unverified=0.0 n=1
  - `spacechina-news` precision=0.0 duplicate=0.0 stale=0.0 unverified=1.0 n=5
  - `hkex_news` precision=0.0 duplicate=0.0 stale=1.0 unverified=0.0 n=3
  - `sec_xbrl_usgaap` precision=0.0 duplicate=0.0 stale=0.0 unverified=1.0 n=2
  - `eastmoney-724` precision=0.5 duplicate=0.0 stale=0.0 unverified=0.5 n=2

## Topic Quality

- Best topics:
  - `execution` quality=1.0 precision=1.0 n=5
  - `order-growth` quality=1.0 precision=1.0 n=5
  - `小消息可炒` quality=1.0 precision=1.0 n=5
  - `订单催化` quality=1.0 precision=1.0 n=5
  - `供需紧张` quality=1.0 precision=1.0 n=4
  - `服务器链` quality=1.0 precision=1.0 n=4
  - `具身智能` quality=1.0 precision=1.0 n=1
  - `机器人` quality=1.0 precision=1.0 n=1
- Worst topics:
  - `state-owned-enterprise` quality=0.1876 precision=0.0 n=5
  - `brokerage` quality=0.2206 precision=0.0 n=2
  - `china-reopening` quality=0.2206 precision=0.0 n=2
  - `consumption` quality=0.2206 precision=0.0 n=2
  - `property` quality=0.2206 precision=0.0 n=2
  - `corporate-action` quality=0.2404 precision=0.0 n=3
  - `m&a` quality=0.2404 precision=0.0 n=3
  - `takeover` quality=0.2404 precision=0.0 n=3

## Candidates

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
- `de5c8153-5197-5df7-876b-d835cee4c731` `add_entity_or_topic_filter` target=`topic:valuation-rerating` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `b40db629-a87a-5f5a-9437-d670cdcb5d09` `add_entity_or_topic_filter` target=`topic:主线题材` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `20df23ad-9ee2-5a7f-855c-8e38cf4e9a71` `add_entity_or_topic_filter` target=`topic:供需紧张` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `6e37211e-10ae-5aee-8c2c-3043f18b9ace` `add_entity_or_topic_filter` target=`topic:小消息可炒` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `4d9861ab-f83e-5299-b81a-28155512b2b0` `add_entity_or_topic_filter` target=`topic:待补充` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Review Checklist

- [ ] 确认本包只生成 research artifacts，没有改 live/news production 配置。
- [ ] 优先检查 worst_sources 中是否存在应降权或需要二次验证的来源。
- [ ] 区分 stale 与 unverified；只有真实时效性问题才适合作为降权证据。
- [ ] 检查 best_sources 是否样本量足够，避免因小样本误升权。
- [ ] 逐条查看 ready_for_codex_review 的候选是否符合投资逻辑和来源洁净度要求。
- [ ] 如要改代码或采集策略，另开 Codex 任务并引用 candidate_id，不允许本闭环自动执行。
- [ ] 市场影响字段目前为 null；若候选依赖市场反应，先接入价格序列再评估。
