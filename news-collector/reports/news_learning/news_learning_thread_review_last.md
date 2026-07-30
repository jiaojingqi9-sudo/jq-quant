新闻学习审阅：建议用户确认是否变更。
最值得看的问题：
1. source_count=12，eastmoney-ann占47.46%，over_reliance=false但集中度继续升高；全局duplicate/stale/refuted/noise为空，来源层duplicate=0。高质量来源：eastmoney-ann 28/28确认、eastmoney-topic 5/5、cninfo_latest 4/4；eastmoney-724 3/3确认但entity=0，先补抽取。spacechina-news 5/5未验证需交叉验证；csrc/hkex/sec小样本stale，继续观察不拉黑。有效主题：小消息可炒、订单催化、execution、order-growth、监管风险；噪声/弱项：state-owned-enterprise、technology、unknown。market_impact为空，不列价格反应候选。
建议动作：
1. 2459772f-5088-5bfa-8427-336876618ba3 uprank_source eastmoney-ann：28/28确认，但继续监控集中度。
2. a975f802-348c-57fb-8eba-e3119f983950 uprank_source eastmoney-topic：5/5确认且覆盖完整。
3. a40447b8-1ec3-5763-a198-cc380f8adf5f uprank_source cninfo_latest：4/4确认。
4. 87cb4e74-bd16-5575-9519-8e20858a72f0 improve_claim_extraction eastmoney-724：3/3确认但entity=0。
5. 7020cc74-9dcc-5cf2-be66-28ff3d72348c add_cross_source_verification spacechina-news：5/5未验证。
6. 60a6436b-9e3b-5ea1-a4ce-51bc4342e8a2 downrank_source state-owned-enterprise：5/5未验证。
7. a478b85d-e3a7-5c71-8d9d-2260c41aa429 add_entity_or_topic_filter execution：7/7确认。
如果用户同意，建议下一条指令：
请按上述candidate_id生成仅供人工确认的新闻学习策略变更草案，不修改生产配置。