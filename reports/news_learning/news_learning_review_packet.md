# News Learning Review Packet

- Generated: `2026-07-30T20:29:42.828461+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Scope: `research/review only`
- Guard: `no auto code changes; no live config changes; no stock/crypto changes`

## Codex Review Prompt

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Artifacts

- `news_memory`: `/Users/jiao/All here/news collector/reports/news_learning/news_memory.jsonl`
  - sha256: `e99e6921f4ac82525b73542534907c000753fdbf597b8a4dd3a5ce0f7db4f36f`
- `news_claims`: `/Users/jiao/All here/news collector/reports/news_learning/news_claims.jsonl`
  - sha256: `bfa06a72737912625bf88850611ec78c53349689f430de00605a97cd02359f36`
- `news_outcomes`: `/Users/jiao/All here/news collector/reports/news_learning/news_outcomes.jsonl`
  - sha256: `196cc21df9b0705c17d8b4087aee88c9930f4a5ad9549e8adaf6c37b3ca4a7fc`
- `news_attribution`: `/Users/jiao/All here/news collector/reports/news_learning/news_attribution.json`
  - sha256: `5e7ccdc8df67f1c6485a00c0342c24398a585a205c31514b472427c42ee1e8c1`
- `news_upgrade_candidates`: `/Users/jiao/All here/news collector/reports/news_learning/news_upgrade_candidates.jsonl`
  - sha256: `ffcdcc1265f67a796d6391d1ddfc5cb3f27fa930954ea2b7913b1167f9fc7357`
- `news_promotion_report`: `/Users/jiao/All here/news collector/reports/news_learning/news_promotion_report.json`
  - sha256: `e4d0b51697f9b23e0f6d533b57f09e1bf77e4ee27ece8754a4cf0f5236571fb9`
- `news_learning_codex_handoff`: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_codex_handoff.md`
  - sha256: `896cbc44c369f7f91a705f83c41cc3b253af82a458173c5aa3c5f8c11d03c14f`

## Source Quality

- Source diversity: `{"source_count": 13, "top_source": "eastmoney-724", "top_source_share": 0.3333, "herfindahl": 0.163, "over_reliance": false}`
- Best sources:
  - `gov-nhsa` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `eastmoney-topic` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=5
  - `cninfo_latest` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `eastmoney-ann` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `sse_announcements` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=1
- Worst sources:
  - `csrc_home` precision=0.0 duplicate=0.0 stale=1.0 unverified=0.0 n=1
  - `spacechina-news` precision=0.0 duplicate=0.0 stale=0.0 unverified=1.0 n=5
  - `hkex_news` precision=0.0 duplicate=0.0 stale=1.0 unverified=0.0 n=2
  - `sec_xbrl_usgaap` precision=0.0 duplicate=0.0 stale=0.0 unverified=1.0 n=2
  - `eastmoney-724` precision=0.2381 duplicate=0.0 stale=0.0 unverified=0.7619 n=21

## Topic Quality

- Best topics:
  - `execution` quality=1.0 precision=1.0 n=7
  - `order-growth` quality=1.0 precision=1.0 n=7
  - `服务器链` quality=1.0 precision=1.0 n=6
  - `小消息可炒` quality=1.0 precision=1.0 n=5
  - `订单催化` quality=1.0 precision=1.0 n=5
  - `earnings` quality=1.0 precision=1.0 n=4
  - `fundamental-improvement` quality=1.0 precision=1.0 n=4
  - `profitability` quality=1.0 precision=1.0 n=4
- Worst topics:
  - `unknown` quality=0.0 precision=0.0 n=16
  - `state-owned-enterprise` quality=0.1876 precision=0.0 n=5
  - `brokerage` quality=0.221 precision=0.0 n=1
  - `china-reopening` quality=0.221 precision=0.0 n=1
  - `consumption` quality=0.221 precision=0.0 n=1
  - `property` quality=0.221 precision=0.0 n=1
  - `corporate-action` quality=0.2447 precision=0.0 n=3
  - `m&a` quality=0.2447 precision=0.0 n=3

## Candidates

- `05951a68-158c-5e5f-864a-83b1c48d47b1` `add_market_impact_label` target=`system:price_reaction_join` confidence=0.8
  - reason: 新闻已能映射到候选资产，但尚未接入 5m/30m/1d 价格反应，建议作为下一阶段研究标签。
- `2d5f43a9-1d9c-518d-a828-63ddb2d6f277` `uprank_source` target=`source:cninfo_latest` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `1eed32cd-9ad8-50b6-b3ac-5f7f51f632b1` `uprank_source` target=`source:eastmoney-ann` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `704058ae-7c38-5923-be5b-2b8ffbe7f219` `uprank_source` target=`source:eastmoney-topic` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `ec57bbaa-d102-5a8c-b5ad-febf78bc4651` `uprank_source` target=`source:gov-mot` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `9d769163-6c73-5707-aa4a-f4a97aec51ee` `uprank_source` target=`source:gov-nhsa` confidence=0.72
  - reason: 来源确认率较高且滞后/反驳比例低，可作为候选高质量来源。
- `55034a70-b6d5-591b-bca2-a184b2cd9ccb` `add_cross_source_verification` target=`source:eastmoney-724` confidence=0.66
  - reason: 单来源未验证比例偏高，应先要求交叉验证或补强实体/主题解析，不宜直接按过期新闻降权。
- `d3f804d3-568c-5cb7-94cd-9d154f882863` `add_cross_source_verification` target=`source:spacechina-news` confidence=0.66
  - reason: 单来源未验证比例偏高，应先要求交叉验证或补强实体/主题解析，不宜直接按过期新闻降权。
- `19e86243-e234-548e-a329-061f51578e1f` `add_entity_or_topic_filter` target=`topic:capital-return` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `78cbee69-0a91-5337-8c8f-0b06a4b9eced` `add_entity_or_topic_filter` target=`topic:compliance-risk` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `a1ec9dc0-564f-55ff-a39a-bf8ff98bf841` `add_entity_or_topic_filter` target=`topic:earnings` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `0f570d42-8316-57f0-8eed-0ac69be178b0` `add_entity_or_topic_filter` target=`topic:event-risk` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `2af2b128-80ce-59f2-9afe-b087f7546804` `add_entity_or_topic_filter` target=`topic:execution` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `fb1efb80-21dc-56cf-bd10-55512d417187` `add_entity_or_topic_filter` target=`topic:fundamental-improvement` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `e03f6d2c-299c-5c28-8c68-401edd1bf9c1` `add_entity_or_topic_filter` target=`topic:healthcare` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `1f0d4a0c-9124-5890-98c6-562ba69bfa31` `add_entity_or_topic_filter` target=`topic:infrastructure` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `07f508f6-0cfe-51f4-a2c7-501353f80082` `add_entity_or_topic_filter` target=`topic:logistics` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `81a6ce7c-a0b8-578b-9db1-da15f2f91527` `add_entity_or_topic_filter` target=`topic:long-term-demand` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `ae31f7c2-ce96-52bd-8044-87fecf926eba` `add_entity_or_topic_filter` target=`topic:order-growth` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `32534d47-167d-5cd6-aacc-b60a35c328c7` `add_entity_or_topic_filter` target=`topic:policy` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Review Checklist

- [ ] 确认本包只生成 research artifacts，没有改 live/news production 配置。
- [ ] 优先检查 worst_sources 中是否存在应降权或需要二次验证的来源。
- [ ] 区分 stale 与 unverified；只有真实时效性问题才适合作为降权证据。
- [ ] 检查 best_sources 是否样本量足够，避免因小样本误升权。
- [ ] 逐条查看 ready_for_codex_review 的候选是否符合投资逻辑和来源洁净度要求。
- [ ] 如要改代码或采集策略，另开 Codex 任务并引用 candidate_id，不允许本闭环自动执行。
- [ ] 市场影响字段目前为 null；若候选依赖市场反应，先接入价格序列再评估。
