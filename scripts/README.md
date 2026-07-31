# news collector/scripts

采集、推送、健康检查、学习**已经由后台任务常驻**，不需要手动启动。
这里剩下的是安装器和功能开关。

| 脚本 | 什么时候用 |
| --- | --- |
| market_news_stack.command | 安装/重装整套后台任务（采集·推送·健康·学习·看板接口）。改过配置后跑一次 |
| market_news_stack_stop.command | 全部停掉 |
| market_news_learning_auto.command / _stop | 单独装/卸学习任务 |
| Enable_Dynamic_Universe / Disable_ | AH 催化板块用扫描器动态生成成分股，还是用静态文件 |
| Enable_Futu_Enrichment / Disable_ | 推送里是否附带富途行情 |
| AH_Multi_Factor_Scanner.command | 手动跑一次 AH 多因子扫描（Enable_Dynamic_Universe 会自动调用） |

想看采集器实时输出，用邮差的 `collectdoctor`，不要双击脚本前台跑——
那会先 pkill 掉常驻任务，然后和 launchd 拉起的新实例撞在一起。

2026-07-31 移走了 8 个（4 个 Codex 遗留 + 看板 + 三个与常驻任务冲突的），理由见
`_回收站_20260730/冗余启动器_20260731/移走清单.md`。
