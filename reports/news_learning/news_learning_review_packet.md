# News Learning Review Packet

- Generated: `2026-07-31T06:38:48.817299+00:00`
- Input report: `/Users/jiao/All here/news collector/reports/live/latest_report.json`
- Output dir: `/Users/jiao/All here/news collector/reports/news_learning`
- Scope: `research/review only`
- Guard: `no auto code changes; no live config changes; no stock/crypto changes`

## Codex Review Prompt

请基于这份新闻 Evidence-to-Review 审阅包，评估新闻收集系统的候选改进是否值得进一步修改。请先检查 source quality、topic quality、候选建议、样本量、重复/滞后/反驳比例、来源多样性和市场影响字段。如果建议改代码或采集策略，请明确引用 candidate_id；不要自动修改 live/news production 配置，也不要修改股票系统或 crypto 系统。

## Artifacts

- `news_memory`: `/Users/jiao/All here/news collector/reports/news_learning/news_memory.jsonl`
  - sha256: `c500d6ae511cc51dd5aa1c065cb187d545b41022d8039d8ea833edc7179e3562`
- `news_claims`: `/Users/jiao/All here/news collector/reports/news_learning/news_claims.jsonl`
  - sha256: `4dd99c5e95547e50cc968629aecd89ac09cd0417ac018cc94ce1733a2a4ea9dc`
- `news_outcomes`: `/Users/jiao/All here/news collector/reports/news_learning/news_outcomes.jsonl`
  - sha256: `91e1638f100a7c887b0112ba739a7f90f7f8aff41a22f9210d4075d32f239933`
- `news_attribution`: `/Users/jiao/All here/news collector/reports/news_learning/news_attribution.json`
  - sha256: `455295f59e0646afb3bce460632a4087409b9091a01e526b2ebad1d9bf733f77`
- `news_upgrade_candidates`: `/Users/jiao/All here/news collector/reports/news_learning/news_upgrade_candidates.jsonl`
  - sha256: `faa99b12516da53c437a5a2db01a7eab1e10fdc27e3a12f6298ba98adb686134`
- `news_promotion_report`: `/Users/jiao/All here/news collector/reports/news_learning/news_promotion_report.json`
  - sha256: `a6c78d9e5bca8929184cce35c191c894480de30a1afe58a88238d3fe747111b7`
- `news_learning_codex_handoff`: `/Users/jiao/All here/news collector/reports/news_learning/news_learning_codex_handoff.md`
  - sha256: `6224a0b7fd02f0a2670204198cf8cf7ffe4c805e0b3269542c521364481de22d`

## Source Quality

- Source diversity: `{"source_count": 13, "top_source": "eastmoney-724", "top_source_share": 0.375, "herfindahl": 0.1907, "over_reliance": false}`
- Best sources:
  - `eastmoney-topic` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=8
  - `xinhua-tech` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=2
  - `gov-mot` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=6
  - `sse_announcements` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=1
  - `gov-cnsa` precision=1.0 duplicate=0.0 stale=0.0 unverified=0.0 n=1
- Worst sources:
  - `csrc_home` precision=0.0 duplicate=0.0 stale=1.0 unverified=0.0 n=1
  - `spacechina-news` precision=0.0 duplicate=0.0 stale=0.0 unverified=1.0 n=3
  - `hkex_news` precision=0.0 duplicate=0.0 stale=1.0 unverified=0.0 n=4
  - `sec_xbrl_usgaap` precision=0.0 duplicate=0.0 stale=0.0 unverified=1.0 n=2
  - `eastmoney-724` precision=0.0476 duplicate=0.0 stale=0.0952 unverified=0.8571 n=21

## Topic Quality

- Best topics:
  - `execution` quality=1.0 precision=1.0 n=6
  - `order-growth` quality=1.0 precision=1.0 n=6
  - `小消息可炒` quality=1.0 precision=1.0 n=5
  - `订单催化` quality=1.0 precision=1.0 n=5
  - `long-term-demand` quality=1.0 precision=1.0 n=4
  - `policy-demand` quality=1.0 precision=1.0 n=4
  - `valuation-rerating` quality=1.0 precision=1.0 n=4
  - `供需紧张` quality=1.0 precision=1.0 n=4
- Worst topics:
  - `state-owned-enterprise` quality=0.1875 precision=0.0 n=3
  - `unknown` quality=0.2017 precision=0.0 n=22
  - `brokerage` quality=0.2205 precision=0.0 n=3
  - `china-reopening` quality=0.2205 precision=0.0 n=3
  - `consumption` quality=0.2205 precision=0.0 n=3
  - `property` quality=0.2205 precision=0.0 n=3
  - `corporate-action` quality=0.4321 precision=0.25 n=4
  - `m&a` quality=0.4321 precision=0.25 n=4

## Candidates

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
- `e938fef6-b9fc-5815-86de-688bd380663e` `add_entity_or_topic_filter` target=`topic:long-term-demand` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `1339f6ab-72b7-5cdd-9303-7d2e9555e567` `add_entity_or_topic_filter` target=`topic:order-growth` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `a0605c5d-d1f0-5dae-a0c5-8b445d71aad6` `add_entity_or_topic_filter` target=`topic:policy` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `dcba5ac2-8a15-5142-8d01-5568cf57f6ec` `add_entity_or_topic_filter` target=`topic:policy-demand` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。
- `a88c6374-ae09-5cd2-9b97-23f5dc3f255a` `add_entity_or_topic_filter` target=`topic:profitability` confidence=0.66
  - reason: 主题确认率和事件分数较好，适合进入人工复核的正向主题过滤候选。

## Review Checklist

- [ ] 确认本包只生成 research artifacts，没有改 live/news production 配置。
- [ ] 优先检查 worst_sources 中是否存在应降权或需要二次验证的来源。
- [ ] 区分 stale 与 unverified；只有真实时效性问题才适合作为降权证据。
- [ ] 检查 best_sources 是否样本量足够，避免因小样本误升权。
- [ ] 逐条查看 ready_for_codex_review 的候选是否符合投资逻辑和来源洁净度要求。
- [ ] 如要改代码或采集策略，另开 Codex 任务并引用 candidate_id，不允许本闭环自动执行。
- [ ] 市场影响字段目前为 null；若候选依赖市场反应，先接入价格序列再评估。
