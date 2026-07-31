# stock/launchers

日常操作在**寻宝猫**里。这里是开关与应急。

| 脚本 | 什么时候用 |
| --- | --- |
| Launch_Trading_Control_Panel.command | **不要移动**：app 首页「启动桌面控制台」按钮直接调用它 |
| Cancel_All_Orders.command | 应急全撤。刻意保持成一个双击就跑的独立文件，不依赖 app 能不能打开 |
| Pregate_Active / LogOnly / Off | 下单前置闸门三档：真拦 / 只记录 / 关闭 |
| Start_All_Day_Auto_Run / Stop_ | 全天自动运行的开与关 |
| Install_Login_Auto_Start / Uninstall_ | 开机自启的装与卸 |
| 重启Dashboard.command | streamlit 卡住时重启，比关了再开快 |
| 跑OFIM研究.command | 跑一整轮 OFIM 研究批处理，结果写 runtime/ofim_research_run.log |
| Open_Stock_Screener.command | 开独立的 Tkinter 选股窗口。它和 app 里的「选股器」页**不是**同一个东西，后者也嵌不进前者；app 选股器页底部那个按钮调的就是这个文件 |

2026-07-31 移走了 3 个：`Open_TAA_Quant_Trading_App`、`Open_Trading_Dashboard`
（两者都只是起交易终端，寻宝猫已取代）、`修复启动脚本`（要修的桌面文件已不存在）。
调用它们的按钮也一并从桌面控制台和 app 里去掉了。理由见
`_回收站_20260730/冗余启动器_20260731/移走清单.md`。
