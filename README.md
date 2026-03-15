# TAA + Futu 交易控制台

这个项目不是去 GitHub 抄一个“收益截图最好看”的仓库，而是从一篇可验证、可复现、可执行的策略白皮书出发，自己实现一套：

- 历史回测
- 本地监控面板
- 最新月度信号生成
- 仓位目标计算
- 富途 OpenAPI 下单

项目里现在有两套策略：

- `TAA baseline`：复现 Meb Faber 的 **A Quantitative Approach to Tactical Asset Allocation**
- `Fusion Intraday`：为富途环境新设计的专属事件驱动日内策略
- `Strategy Stack`：把 `Baseline + Fusion + Daily Round-Trip fallback` 组合成可分仓运行的统一账户层

## TAA baseline

核心规则很简单：

- 月末收盘价高于 10 个月均线：持有
- 月末收盘价低于或等于 10 个月均线：空仓/现金
- 对所有满足条件的资产等权分配

为了能在富途上实际执行，默认使用一组流动性高、富途美股模拟盘容易交易的 ETF 代理：

- `US.SPY` 美股
- `US.EFA` 海外发达市场股票
- `US.IEF` 美债
- `US.VNQ` REITs
- `US.DBC` 商品

## 研究结论

没有客观意义上的“市面最强策略文章”。如果目标是：

- 有公开原始文章
- 规则明确
- 可以稳定复现
- 可以在富途模拟盘执行

那么这篇多资产趋势跟踪白皮书比很多需要做空、上杠杆、交易期货、依赖分钟级 alpha 的论文更适合落地。

更详细的选型说明见 [docs/strategy-selection.md](/Users/jiao/All%20here/trade/docs/strategy-selection.md)。

## Fusion Intraday

这套是专门为“保留富途，但想更高频、更强一些”设计出来的组合策略。它不是直接抄某一篇论文，而是把几篇文章里真正能落到富途的部分拼成一套可执行结构：

- 大盘 regime 过滤
- Stocks in Play / 开盘区间突破
- 5 分钟动量
- VWAP 偏离
- 盘口失衡
- 逐笔方向失衡
- 点差和仓位约束

说明文档见 [docs/fusion-intraday-strategy.md](/Users/jiao/All%20here/trade/docs/fusion-intraday-strategy.md)。

## 环境准备

1. 安装 Python 3.11。
2. 创建虚拟环境并安装依赖：

```bash
uv venv --python 3.11 .venv
uv pip install -p .venv/bin/python -e .[dev]
```

3. 准备环境变量：

```bash
cp .env.example .env
```

4. 如果要接富途账户，需要额外准备：

- 本机运行 Futu OpenD
- OpenD 已登录你的富途账号
- OpenD 已开启 OpenAPI
- 目标交易账户可用

如果要接真实盘，还需要：

- `.env` 里把 `FUTU_TRD_ENV=REAL`
- 显式打开 `FUTU_ENABLE_REAL_TRADING=true`
- 把 `FUTU_UNLOCK_TRADE_PASSWORD_MD5` 填成你的富途交易密码 MD5，不要明文密码
- 如果你真的要让自动交易跑真实盘，再额外打开 `FUTU_ALLOW_AUTO_REAL=true`

默认行为是保守的：

- 真实盘可以查看账户、持仓和订单
- 真实手动下单默认锁定，除非你显式打开 `FUTU_ENABLE_REAL_TRADING`
- 真实自动下单默认继续锁定，除非你再额外打开 `FUTU_ALLOW_AUTO_REAL`
- 富途接口偶发超时会先自动重试，重试参数由 `FUTU_API_RETRY_ATTEMPTS` 和 `FUTU_API_RETRY_BACKOFF_SECONDS` 控制
- 历史回测、账户复盘和精确执行复盘默认会扣估算交易成本，参数由 `TRADE_COST_*` 控制

## 常用命令

回测：

```bash
.venv/bin/taa-futu backtest
```

按实时落盘日志和 `order_id` 做精确执行复盘：

```bash
.venv/bin/taa-futu backtest --strategy exact --start 2026-03-11 --end 2026-03-11
```

查看当前最新已完成月份的目标仓位：

```bash
.venv/bin/taa-futu signals
```

生成富途调仓计划，不实际下单：

```bash
.venv/bin/taa-futu paper-trade
```

实际提交富途订单：

```bash
.venv/bin/taa-futu paper-trade --submit
```

运行专属日内策略并生成调仓计划：

```bash
.venv/bin/taa-futu fusion-intraday
```

实际提交专属日内策略订单：

```bash
.venv/bin/taa-futu fusion-intraday --submit
```

启动监控面板：

```bash
.venv/bin/taa-futu dashboard
```

启动傻瓜式控制台：

```bash
.venv/bin/taa-futu-panel
```

或者直接在 Finder 里双击：

- [Launch_Trading_Control_Panel.command](/Users/jiao/All%20here/trade/Launch_Trading_Control_Panel.command)
- [Trading Control Panel.applescript source](/Users/jiao/All%20here/trade/macos/Trading_Control_Panel.applescript)

控制台和监控页现在都已经做成了中英文对照：

- `一键启动 / One-Click Start`：同时打开 `FutuOpenD` 和监控页
- `运行回测 / Run Backtest`：按你输入的历史日期跑回测
- `查看月度信号 / Show Monthly Signal`：看当前月度目标仓位
- `预演订单 / Plan Orders`：只生成下单计划，不真正下单
- `提交订单 / Submit Orders`：真正发到当前交易环境
- `试运行 / Run Dry-Run`：运行专属日内策略但不提交
- `启动自动运行 / Start Auto Run`：在美股交易时段持续检查 `Fusion Intraday`，自动向当前交易环境提交订单
- `组合运行 / Stack Controls`：设置 `Baseline sleeve` 是否启用，以及 `Baseline / Fusion` 各自占用多少账户资金
- `停止自动运行 / Stop Auto Run`：停止全天自动运行服务

现在 `启动自动运行` 不是只拉起交易引擎，而是先启动一个本地守护监控。自动盘执行的是当前配置好的 `Strategy Stack`：

- 它会在主要交易时段按随机间隔自检，默认大约每 `4` 到 `9` 分钟检查一次
- 如果发现 `OpenD` 掉线、自动运行进程退出、状态文件卡死或进入 `error`，会自动尝试恢复
- 它会把状态写到：
  - [runtime/watchdog_status.json](/Users/jiao/All%20here/trade/runtime/watchdog_status.json)
  - [runtime/watchdog.log](/Users/jiao/All%20here/trade/runtime/watchdog.log)
- 原交易引擎状态仍然写到：
  - [runtime/auto_trader_status.json](/Users/jiao/All%20here/trade/runtime/auto_trader_status.json)
  - [runtime/auto_trader.log](/Users/jiao/All%20here/trade/runtime/auto_trader.log)

默认组合建议：

- `Fusion Only`：`Baseline=off, Fusion=100%, Fallback=off`
- `Fusion + Fallback`：`Baseline=off, Fusion=90%, Reserve=10%`
- `Full Stack`：`Baseline=55%, Fusion=35%, Reserve/Fallback=10%`

## 运行逻辑

- 回测和默认信号源使用 `yfinance` 拉取日线。
- 富途下单阶段会连接 OpenD，用最新快照做价格和股数换算。
- `Fusion Intraday` 会额外使用富途的 1 分钟线、盘口和逐笔。
- `Strategy Stack` 会把 `Baseline` 和 `Fusion` 先按 sleeve 权重缩放，再汇总成一个账户级目标仓位；`Fallback` 只使用预留现金。
- 实盘执行默认只做 long-only。
- 默认用限价单，买单在卖一基础上上浮，卖单在买一基础上下浮，缓冲由 `FUTU_PRICE_BUFFER_BPS` 控制。
- 费用模型默认按富途香港美股固定费率估算：
  - 佣金：`0.0049 USD/股`，最低 `0.99 USD`
  - 平台费：`0.005 USD/股`，最低 `1.00 USD`
  - 交收费：`0.003 USD/股`，最低 `0.01 USD`
  - SEC / TAF：按卖出时的监管费规则估算
- `baseline / fusion / fallback / stack / account / exact` 现在都会把估算费用记进净结果。
- 监控面板支持：
- 查看账户资产、持仓、订单状态
  - 查看订单历史里的 `dealt_qty / dealt_avg_price`
  - 自选起止时间跑月频历史模拟，查看收益曲线和调仓日志
- 控制台支持：
  - 一键启动 `FutuOpenD + Dashboard`
  - 一键打开浏览器监控页
- 按按钮跑回测、信号、订单预演、订单提交
- 启动/停止全天自动运行的 `Fusion Intraday`
  - 启动后会自动拉起守护监控 `watchdog`
  - 所有命令输出集中显示在一个日志窗格里

## 实盘前必须知道

- 默认目标市场是 `US`，因为这篇策略最适合用美股 ETF 代理。
- `paper-trade` 默认是 dry-run，只有加 `--submit` 才会真的发单。
- `REAL` 环境下，真实手动下单必须同时满足：
  - `FUTU_ENABLE_REAL_TRADING=true`
  - `FUTU_UNLOCK_TRADE_PASSWORD_MD5` 已配置
- `REAL` 环境下，真实自动下单必须额外满足：
  - `FUTU_ALLOW_AUTO_REAL=true`
- 这套实现更适合月频执行，不适合日内反复运行。
- 富途文档不同页面对模拟账户展示关系的描述并不完全一致；实际执行前请以 `get_acc_list()` 返回的 `trd_env=SIMULATE`、`sim_acc_type` 和 `trdmarket_auth` 为准。
- 富途模拟交易接口当前不支持 `deal_list` 成交明细查询，所以监控页里的“成交”是订单维度，不是逐笔撮合流水。
- 富途 `history_order_list_query` 当前不会返回费用字段，所以页面里的“已实现 / 净变化 / 估算费用”在多数情况下是按官方费率估算，不是券商回传原值。
- 全天自动运行默认只在 `America/New_York 09:45-15:55` 工作，交易引擎轮询间隔默认 `60` 秒；可以在 `.env` 里改 `AUTO_TRADER_*` 配置。
- 守护监控默认在主要交易时段每 `240-540` 秒随机检查一次，盘后每 `900-1800` 秒随机检查一次；可以在 `.env` 里改 `WATCHDOG_*` 配置。
