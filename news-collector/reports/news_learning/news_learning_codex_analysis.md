新闻学习审阅：建议用户确认是否变更。

最值得看的问题：
1. 市场反应标签缺失是当前最大瓶颈：42 个 source/topic 指标的 5m/30m/1d market_impact 全为空；60 条记录中 32 条已能映射资产，已经足够先做 research 级价格反应 join。
2. `spacechina-news` 连续 5 条全部 unverified，实体覆盖 0、topic_signal_quality 0.1875；不应拉黑或降权，但值得要求交叉验证或改进 claim/entity 抽取。

建议动作：
1. `693708ec-525c-5ca5-8638-33dcddfc5d89` add_market_impact_label `system:price_reaction_join`：先接入研究用 5m/30m/1d 市场反应标签，否则无法判断主题是否真有预测价值。
2. `7020cc74-9dcc-5cf2-be66-28ff3d72348c` add_cross_source_verification `source:spacechina-news`：5/5 未验证，适合先设为需交叉验证，不建议直接降权或拉黑。

不建议现在做的事：
1. 暂不升权 `cninfo_latest`、`eastmoney-724`、`eastmoney-topic`：样本只有 4-5 条；`eastmoney-ann` 虽有 28/28 confirmed，但已占 46.67%，再升权可能增加单源依赖。
2. 暂不按 `healthcare`、`policy`、`long-term-demand` 等主题改策略：存在滞后或实体覆盖不足，且缺少市场反应标签。
3. 暂不拉黑 `csrc_home`、`hkex_news`、`sec_xbrl_usgaap`：各只有 1 条 stale，样本不足。

如果用户同意，建议下一条指令：
请只在 research/news_learning 流程中评估并实现 `693708ec-525c-5ca5-8638-33dcddfc5d89` 和 `7020cc74-9dcc-5cf2-be66-28ff3d72348c`，不得修改 live/news production 配置、股票系统或 crypto 系统。