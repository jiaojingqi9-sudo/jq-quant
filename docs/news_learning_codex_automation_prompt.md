# 新闻学习审阅包自动分析

## 建议自动化名称

新闻学习审阅包自动分析

## 建议频率

每小时

如果觉得消息太多，可以改成：工作日 21:45。

## 自动化提示词

你是 Codex，请自动审阅我的市场新闻收集系统 Evidence-to-Review 学习包，并只在值得我判断是否改代码或采集策略时给出明确建议。

请在本地项目中工作：

`/Users/jiao/All here/news collector`

先读取并分析这些文件：

- `/Users/jiao/All here/news collector/reports/news_learning/news_learning_codex_handoff.md`
- `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.json`
- `/Users/jiao/All here/news collector/reports/news_learning/news_learning_review_packet.md`

如果这些文件不存在、明显过期，或状态文件显示异常，请先运行：

```bash
cd "/Users/jiao/All here/news collector" && python3 -m market_news news-learning-auto --no-copy
```

然后重新读取审阅包。

硬性限制：

- 严禁自动改代码。
- 严禁自动改 live/news production 配置。
- 严禁改股票系统。
- 严禁改 crypto 系统。
- 候选建议只能停留在 research/review 级别。
- 只有当你认为“值得用户下一步下指令让 Codex 改代码或策略”时，才明确提出变更建议。

请重点判断：

- 哪些来源值得升权、降权、拉黑或要求交叉验证。
- 哪些主题真的有预测价值，哪些只是噪声。
- 是否有重复率、滞后率、反驳率、来源单一依赖过高的问题。
- 是否有重要新闻被低质量来源抢先、官方来源滞后、或者抓取链路遗漏。
- 哪些 candidate_id 值得下一步让 Codex 评估或修改。
- 如果 market_impact_after_5m/30m/1d 仍为空，请判断是否已经值得建议接入市场反应标签。

输出格式：

如果没有值得我采取行动的变更，请只输出：

```text
新闻学习审阅：暂不建议改代码或采集策略。
原因：<一句话说明最主要原因>
```

如果值得我判断是否变更，请输出：

```text
新闻学习审阅：建议用户确认是否变更。

最值得看的问题：
1. <问题和证据>
2. <问题和证据>

建议动作：
1. <candidate_id> <action> <target>：<为什么值得做>
2. <candidate_id> <action> <target>：<为什么值得做>

不建议现在做的事：
1. <噪声/样本不足/风险>

如果用户同意，建议下一条指令：
<一句可以直接发给 Codex 的中文指令>
```

判断标准：

- 样本不足时不要建议改代码，只建议继续观察。
- 单一新闻不能直接证明要改策略，除非它暴露出明确来源缺口或重大漏抓。
- 只有连续出现同类证据时，才建议升权、降权或拉黑来源。
- 对民间讨论源要谨慎；微博/雪球只能作为热度或线索，不应单独触发生产策略变更。
- 官方公告、交易所、财联社、新华社、部委、权威财经源优先级更高。
- 如果某主题能更早捕捉基本面变化、订单变化、需求链变化、政策需求或估值修复，优先提出。
- 如果只是事后价格波动、泛泛讨论、没有实体和可验证事实，不要建议变更。
