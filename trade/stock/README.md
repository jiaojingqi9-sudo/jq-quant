# Stock System

这里放股票 / 富途侧的用户入口、桌面 App、文档和辅助工具。

核心 Python 包仍保留在项目根目录的 `src/taa_futu/`，运行数据仍保留在 `runtime/`。这是刻意的：只整理外层入口，不改导入路径，避免因为换文件夹导致交易系统连锁出错。

常用入口：

- `stock/apps/TAA Quant Trading.app`：股票量化交易一体化 App，后台启动监控页并打开一个专用窗口
- `~/Desktop/TAA 量化交易.app`：本机桌面快捷方式，指向上面的 App（`.app` 包是本机生成的，不进仓库）
- `stock/launchers/Open_TAA_Quant_Trading_App.command`：命令式备用入口
- `stock/launchers/Launch_Trading_Control_Panel.command`：打开可点击控制台
- `stock/launchers/Open_Trading_Dashboard.command`：打开股票监控页
- `stock/launchers/Start_All_Day_Auto_Run.command`：启动股票自动运行守护
- `stock/launchers/Stop_All_Day_Auto_Run.command`：停止股票自动运行守护
- `stock/apps/Trading Control Panel.app`：股票控制台 App 包
- `stock/docs/`：股票策略说明文档
- `stock/tools/`：股票筛选器等辅助工具

新增工程化运行文件：

- `runtime/stock_events.jsonl`：股票自动交易 cycle 事件流水
- `runtime/stock_fills.jsonl`：股票成交 append-only 日志
- `runtime/stock_ledger_epoch.json`：股票账本起点，用于清晰计算某次实验之后的 PnL
- `runtime/stock_journal.jsonl`：双分录审计账本，带前后哈希链
- `runtime/stock_order_memory.jsonl`：订单决策快照黑匣子
- `runtime/stock_trade_outcomes.jsonl`：成交配对后的交易结果标签
- `runtime/stock_attribution.json`：策略/标的/亏盈原因归因报告
- `runtime/strategy_upgrade_candidates.jsonl`：策略自动升级候选建议
- `runtime/strategy_promotion_report.json`：候选晋级门禁报告
- `runtime/stock_learning_review_packet.md`：可直接发给 Codex 复核的学习审阅包
- `runtime/stock_learning_review_packet.json`：带证据 hash 的机器可校验审阅包
- `runtime/lob_cache.json`：股票盘口缓存，策略优先读取新鲜缓存再回退到轮询

常用诊断命令：

- `.venv/bin/taa-futu stock-status`
- `.venv/bin/taa-futu stock-system-doctor`
- `.venv/bin/taa-futu stock-system-reset`
- `.venv/bin/taa-futu stock-ledger-status`
- `.venv/bin/taa-futu stock-ledger-reset`
- `.venv/bin/taa-futu stock-ledger-audit`
- `.venv/bin/taa-futu stock-learning-build`
- `.venv/bin/taa-futu stock-learning-export`
- `.venv/bin/taa-futu stock-learning-status`

关键风控开关：

- `AUTO_TRADER_MAX_TARGET_GROSS_EXPOSURE`
- `AUTO_TRADER_MAX_TARGET_WEIGHT`
- `AUTO_TRADER_MAX_ORDER_VALUE_USD`
- `AUTO_TRADER_MAX_CYCLE_TURNOVER_USD`
- `AUTO_TRADER_MAX_EPOCH_LOSS_USD`
- `AUTO_TRADER_MAX_EPOCH_LOSS_PCT`
