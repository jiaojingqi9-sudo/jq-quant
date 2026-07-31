#!/usr/bin/env python3
"""gitcommit - 在本机提交 news collector 与 trade 的当前状态。

放在本机跑而不是沙箱：沙箱对挂载目录的 unlink 权限受限，git 的索引与对象
写入会失败（unable to unlink / index.lock）。

只做本地提交，不推远程（远程仓库还没建）。
"""
import json
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
REPOS = {
    "news collector": HOME / "All here" / "news collector",
    "trade": HOME / "All here" / "trade",
}
MESSAGES = {
    "news collector": """修复 cookie 状态不会自愈，并关闭微博源

看板上那条红色告警是反的：雪球标红、微博显示正常，而实测结果正相反。

    weibo:  error | Expecting value: line 1 column 1 (char 0)
    xueqiu: ok    | cookie accepted

过期标记（.expired）只在重新安装 cookie 时才清除，从不因为成功而清。于是
雪球在 2026-05-12 一次临时失败后被永久标红，实际上它一直是好的——标记挂了
两个半月。反过来 check 失败时不写标记，所以真坏掉的微博在看板上是绿的：
它拿回的不是 JSON，多半是登录页或拦截页。

看错方向比看不见更糟：盯着红的那个是好的，绿的那个是坏的。

改动：新增 record_cookie_check(path, ok, detail)，成功清标记、失败落标记；
`cookies check` 两个源都调它；采集成功也顺带清标记，不必等人手动跑 check。
改完重跑一次，雪球标记自动消失、微博标记自动建立。

微博源关闭（enabled=false）：信噪比太低，聚类里大量无关内容；cookie 也已
过期（存的是 2026-03-15 的登录态）。配置里写了 disabled_reason 与重新启用
的步骤。雪球不受影响，仍在 25 个采集器里。

配套：区分「未启动」与「已关闭」。以前两者都画成红色，于是一个刻意关掉的源
会在看板上永远挂红牌，时间一长这块就没人看了。cookies 汇总也不再把停用的源
算进错误，两个源都停用时不会误报「Cookie 未配置」。

测试：test_factory_skips_disabled_sources 原来直接断言 weibo 必须在采集器
列表里——它测的其实是「当前配置恰好是什么」，而不是它名字说的「被禁用的源
会不会被跳过」，所以一关微博就红。改成读配置再断言：启用的必须建出来、
禁用的必须建不出来。这条规则有前科——RSS 源的 enabled 开关曾长期没被读取，
直到 2026-07-30 才发现，正是因为没有测试盯住它。

test_news_learning_automation_scripts 里两个测试断言 Codex 审阅启动器的内容，
而那四个脚本已随启动器整理移走。反转为断言它们确实不在了，并新增一条
「剩下的启动器里不能再有人调 Codex」，防止有人从回收站捞回来。

---
修复生成 launchd 配置时不转义 XML，并清理冗余启动器

推送任务的配置文件在磁盘上是坏的，而且一直没人发现。

plist 是 XML，命令行里的 & 必须写成 &amp;。write_agent 只转义了自己拼接的
那个 &&（`cd '$workdir' &amp;&amp; $command_line`），把 $command_line 原样
插进 <string>。五个任务里只有 notify 的命令行含 &：

    [ -f "$HOME/.market_news/futu_env" ] && . "$HOME/.market_news/futu_env"

于是只有它生成出非法 XML。plutil -lint 报 "unknown ampersand-escape sequence
at line 11"。

看着没事是因为 launchd 内存里加载的是更早那份合法版本，任务照常每 5 分钟跑。
一旦重启或重新加载就会读磁盘上这份，加载失败，「把新闻发到手机」这一步静默
消失——和之前断推 58 天是同一类故障，且同样不会报错。

修 write_agent 与 write_keepalive_agent，统一走 xml_escape 处理 & < >。
已装的那份用 futu_watcher/fix_notify_plist.py 就地补了两个字符，改前备份、
改后 plutil 校验通过、重新 bootstrap 成功、命令行内容逐字核对未变。

同时移走 8 个启动器（进回收站，未删除）：
  · 4 个 Codex 遗留（codex_review_auto 与 thread_review_auto 及其停止脚本），
    thread_review 直接调 /Applications/Codex.app 的二进制，订阅已停
  · market_news_board——看板已是 app 里的一个页面
  · console / delivery / health——这三个会先 pkill 掉 launchd 常驻的同名任务
    再前台重跑，而 launchd 随后又拉起自己那份，结果两个采集器同时写库

剩下的 9 个写进 scripts/README.md，逐个说明什么时候用。

另修 reporting.py 里一处 \\s 未转义，每次 notify 运行都往错误日志写
SyntaxWarning。改成 \\\\s 后生成的 JS 一字未变，噪音消除。""",

    "_old_news": """修复新闻推送中断，模型层切换到 Claude，并清理仓库

新闻推送已断 58 天（最后一条 2026-06-02）。三个原因叠加：

1. 投递闸门要求每条告警都有模型判定（screening_status=="used"）。模型层
   不可用时，这道闸门静默拦下 100% 的告警——采集和排序一直正常，只是什么
   都发不出去。现改为失败开放：全报告没有任何有效判定时降级到规则筛选
   （仅 critical/high）并在消息里标注；模型只要活着，它的否决仍然算数。
   科技催化信号在降级模式下静音，因为区分真催化和例行公告正是规则做不到的。

2. 模型层自 2026-06-16 起挂掉：未配 OpenAI key，全靠 Codex 订阅兜底而其
   额度耗尽，且失败的调用照样扣预算，把每日 120 次额度吃光。现接入本机
   Claude Code CLI（筛选走 haiku、选股走默认模型），停用已死的 OpenClaw
   通道。claude 必须写绝对路径：launchd 的 PATH 不含 Homebrew。

3. Codex 审阅失败时会去读上一次成功遗留的分析文件，判定「含建议动作=值得
   推送」，把同一份两个月前的旧报告每小时重发，共 299 次。现改为失败不发、
   内容去重。

采集源：修复交通运输部地址、SEC 的 User-Agent（缺邮箱被 403）、新华网正文
容器（在 id 而非 class，长期抓空）；停用境外不可达的住建部/国资委/海关与
已下线的 Reuters feed；修复 RSS 源 enabled 开关从未被读取的 bug；过滤 SEC
批量备案避免刷屏。

仓库：.gitignore 原为空，3.5GB 数据库长期待提交，反复中断的 git 操作留下
18 个 tmp_pack 残骸，.git 膨胀到 11GB（真正引用的对象仅 313 个）。清理后
1.7MB，跟踪内容从 3530MB 降到 0.92MB。

新增回归测试覆盖「模型挂掉不得静默吞掉所有新闻」。""",

    "trade": """新闻看板融进 app，并修复列高被卡死导致的卡片截断

看板是用 iframe 整个嵌进来的，看着像「页面里套一张页面」。配色令牌两边本来
就是同一套（ink / muted / 阴影 / 圆角 / 字体），所以问题全在接缝：自带的渐变
页面背景在 app 浅底上显出一块矩形、body 外边距让内容离容器边缘忽宽忽窄、
顶部那张 hero 大标题成了第二个页头、内容限宽居中与 app 栅格对不齐。

在嵌入时注入样式解决，不动生成器——生成器产出的那张页面还要独立打开、还要
给推送用，它有自己的门面是对的。

**中途改错一次，记下来免得再犯。** 我一度加了
`.column-scroll { overflow: visible; max-height: none }`，理由是「iframe 自己
会滚，内部再套一层就是双滚动条」。结果卡片被拦腰切断：`.column-scroll` 上有
明确的 `height: clamp(760px, calc(100vh - 220px), 1200px)`，只改 overflow 而
高度限制还在，超出的内容溢出后被祖先裁掉，既不滚动也看不全。三栏各自滚动
就是这个看板的布局模型，而且并不存在双滚动条。已删除该规则。

**顺带修好「加高」开关**：它一直没有实际效果。列高被 clamp 的上限 1200px
卡死，把 iframe 从 1500 撑到 2400 只是在下方多出一千多像素空白，而开关的说明
写着「2400px，适合大屏」。现在按 iframe 高度重算列高并去掉上限
（1500 → 1280px，2400 → 2180px），下限保留 760px。

---
修复账本 Epoch 判定分歧，以及收盘前成交漏记

界面上 Epoch 卡片显示着日期，旁边却写「还没有设置账本 Epoch」，起点资产
「未设置」、期间盈亏「待初始化」、券商对账整块禁用。查下来是三个叠起来的问题。

**一、schema 不一致导致三处判定打架**

stock/tools/repair_ledger.py 手工拼 epoch dict、绕过 write_stock_ledger_epoch()，
只写了 cash 没写 total_assets。而判定「Epoch 设了没有」的口径有三套：
Epoch 卡片只看 ts、Doctor 只看 ts、主界面要 ts 且 total_assets>0。同一份文件
被判出相反结论。

更糟的是券商对账其实一直在算——reconcile 只需要 cash，而文件恰好有 cash——
结果就在内存里，只是 UI 卡在一个不相关的字段上不给看。

改：stock_runtime 新增 epoch_start_value() / epoch_is_set() 作为唯一判定入口，
界面与 Doctor 都走它；positions 为空时用 cash 兜底（无持仓则总资产恒等于现金，
是恒等式不是估算）；Doctor 增加「有 ts 但缺起点资产」的 warn 分支，两边说同
一句话；repair_ledger 补齐 schema，且校准现金时同步 total_assets。

**二、对账一打开就暴露 4 处差异**

账本以为还持有 MSTR 631 / IBIT 416 / META 40，券商早已清零，现金差 77,012.82。
按 order_id 对差集，本地流水比券商少 5 笔，时间戳是：
15:54:30 · 15:54:36 · 15:54:12 · 15:55:09 · 15:54:54
全部卡在 AUTO_TRADER_END_TIME=15:55 上。

**三、根因：记账被交易时段的闸门挡住了**

run_cycle() 里窗口一关就 return，而 _record_new_fills 排在 return 之后。成交
发生在 15:54，下一个轮询周期已过 15:55，那笔成交再没人看一眼。而补记只查
[昨天, 今天]：周五 15:54 成交，下个交易日周一查 [周日, 周一]，永久追不回来
——06-05 与 07-17 就是这样丢的。

_record_new_fills 对券商只读（只调 get_order_history），不下单不撤单，本就不
该受交易闸门管。改：窗口外也补记一次再返回 waiting；回溯从 1 天放宽到 7 天
（AUTO_TRADER_FILL_LOOKBACK_DAYS，去重按 order_id 做，多扫不会重复入账）。

连带修一个隐藏很深的问题：账本按文件顺序回放做 FIFO，而补记的记录只能追加到
末尾。补回 07-24 的 AAPL 买入后，它排在自己的卖出之后，账本于是认为还持有
116 股。load_stock_fill_records 改为按 ts 稳定排序——对本来有序的文件是空操作，
只在乱序时生效。

**结果**（用 futu_watcher/backfill_fills.py 补回那 5 笔，改前备份）：
持仓差异 3 处 → 0 处，现金差 77,012.82 → 8,999.77（剩余是费用估算与分红的
累积残差，与 repair_ledger 当初吸收的 14,307 同性质）。净已实现
3,660.48 → 9,531.97：那 5 笔本就带着约 5,871 的已实现盈亏，一直没进账。
成交数 5389 → 5394，链校验仍通过。

**四、给危险按钮加真实警告**

「把当前账户设为统一股票系统起点」会把 fills_count_at_reset 写成当前成交总数，
两个账本投影都按这个偏移量切片，已入账的成交被整体排除出视图（净已实现、
费用、分录数归零）。原说明只写「不会修改券商账户」，读起来像无害操作。现在
会显式列出将被排除的笔数，并说明按钮不提供撤销。

**测试**：test_stock_doctor 的三个 fixture 原本写 account_snapshot={}，
却用于名为「接受自洽的运行时契约」的断言——一份缺起点资产的 Epoch 并不自洽，
这等于让线上这个真实故障被测试放行。补齐 fixture。

---
新增演示模式与 URL 路由，删除 1169 行死代码，重写 README

**演示模式**：设 JQ_DEMO=1 就不连富途，改用合成数据，六个页面全部可用。
没有富途账号、没有行情数据的人 clone 下来也能把界面点一遍——这是仓库能给别人
看的前提，此前不具备。

注入点只有一处：futu_gateway.__enter__ 返回 DemoTrader 而不是 self。
全仓 26 个调用点都写 `with FutuPaperTrader(settings) as trader:`，而 with...as
绑定的是 __enter__ 的返回值不是构造出的对象，所以改这一处即可覆盖全部，
包括子进程里跑的 CLI（环境变量默认继承）。__init__ 本来就不连接任何东西，
测试里 __new__ 绕过构造的用法也不受影响。

合成数据的列名不是手写的，来自 demo_data/futu_schema.json——用
futu_watcher/capture_schema.py 从真实接口抓取，只抓列名与 dtype、不抓任何数值。
手写列名的话漏一个下游就是 KeyError，且要点进那个页面才炸。

配套修了三处「演示时自相矛盾」：
  · 状态栏原会探测真实端口，本机开着 OpenD 时显示「已连接」，与横幅的
    「未连接富途」直接打架
  · 系统体检查的是本机运行时文件，干净安装时必然全红，像程序坏了
  · request_history_klines 原固定返回 250 根日线（约 8 个完整月），
    而 TAA baseline 要 10 个月均线，股票页报「月线数据不足」

所有下单路径在演示模式下直接抛异常——不是下到假账户，是根本不让调。

**URL 路由**：?view=xxx 直接定位页面，地址栏跟随导航变化。页面可收藏、可分享，
截图脚本也不必模拟点击。只在会话首次运行时读一次 URL，否则用户点侧边栏切走后
下一次 rerun 又被拽回去，表现为「点了没反应」。

**删死代码 1169 行**（先全仓 grep 确认零引用）：
  · unified_app.py 246 行——依赖从未装进 dependencies 的 pywebview，实际执行
    只会退化成打开浏览器，与 taa-futu dashboard 等价，且无任何调用方
  · audit_log.py 69 行——模块与两个公开函数均 0 引用；真正在做审计账本的是
    stock_ledger.py + stock_events.py
  · _removed_features_backup/ 51 行——插件化尝试的残留，内容与 dashboard_app
    里的现役登记逐字重复
  · dashboard_app 六个函数 651 行——_render_symbol_detail / _candlestick_chart /
    两个 _render_terminal_* 等，是一整套没有入口的「终端风格行情页」
  · dashboard_extras 的 render_home / render_view / SIDEBAR_OPTIONS 等 137 行——
    插件架构之前的手写分发，注册表上线后就没有调用方了。留着会让人以为
    导航有两套

覆盖这批代码的三个测试没有跟着删，而是改盯注册表：它们要防的事情没变
（页面漏注册、render 签名不对、未知页面把外壳搞崩），只是清单的真实来源
换了地方。测试数不变。

**修两个功能失效**：
  · shell.py 的 _extra_quick_actions() 在 return 之后还有一段展示
    registry.errors 的代码，永远执行不到——插件导入失败会被完全静默吞掉，
    功能从侧边栏消失而界面上没有任何提示。拆成 render_discovery_errors()
    并在首页末尾调用。
  · plugin.register 用 `is` 比对 render 判断重复登记。dashboard_app 把
    stock/stock_history 的 render 写成 main() 内的闭包，而该文件是 Streamlit
    直接执行的脚本，每次交互都重跑 main()，每次都是新函数对象——于是每次
    交互都往日志刷两行「重复登记」，真正的重名冲突反被淹没。改成比对
    (__module__, __qualname__)。

**README 重写**：六张截图（演示模式下用 Chrome 调试协议截的整页图）、
安装与演示步骤、六个页面各自做什么、四条策略、常用命令、插件架构怎么加功能、
安全边界、常见问题。旧版是一篇 TAA 策略说明，看不出这是个能跑的东西。

截图脚本 futu_watcher/make_screenshots.py 记了一个坑：
chrome --headless --screenshot 配 --virtual-time-budget 会跳过 websocket 的
真实往返，而 Streamlit 内容全靠 websocket 推——截出来是灰色骨架不是页面。
改用调试协议，真的等到内容出现再截。

**验证**：把工作区打成干净副本（去掉 .venv/runtime/.env/.git），在只有
Python 3.12 的环境里照 README 的命令原样走一遍——pip install -e . 一次装上，
演示模式起来，七个页面 AppTest 渲染零异常零错误、演示横幅每页都在。

**pyproject**：删掉指向已删模块的 taa-futu-app 入口点。

---
清理冗余启动器，并去掉指向它们的按钮

桌面入口收成一个「寻宝猫」之后，各处还散着 43 个 .command，其中不少只是
「打开某个页面」——而那些页面现在都是 app 里的功能页。

移走 5 个（进回收站，未删除）：
  · Open_TAA_Quant_Trading_App——全文只有一行 open 旧的 .app
  · Open_Trading_Dashboard——跑 taa_futu.cli dashboard，就是寻宝猫做的事
  · 启动量化交易控制台——与 Launch_Trading_Control_Panel 重复
  · 修复启动脚本——它修的是桌面上的「启动量化交易控制台.command」，
    那个文件早已不在桌面，点了也没有对象可修
  · Open_Crypto_OFIM_App（crypto/launchers）

同步去掉调用它们的按钮：unified_panel 的「打开 监控Dashboard」「打开 TAA App」
「修复启动脚本」，dashboard_extras 的「打开 TAA Quant Trading App」。
不去掉的话按钮还在，点了显示「找不到 .../xxx.command」。

其中两个判断错了，已放回：
  · Open_Stock_Screener 起的是 futu_stock_screener_desktop.py，一个独立的
    Tkinter 窗口，和 Streamlit 的选股器页不是同一个东西，后者也嵌不进前者；
    app 选股器页底部那个按钮调的正是它
  · Open_Crypto_OFIM_App 是加密页嵌入渲染失败时的兜底路径，兜底本来就是给
    「主路径坏了」准备的，不能因为主路径现在好用就删

之所以能确定移走不会断掉后台，是先盘点了本机已加载的 launchd 任务：九个
全部直接调 python 模块，没有一个指向 .command（futu_watcher/launchd_audit.py）。
而 app 首页「启动桌面控制台」按钮确实依赖 Launch_Trading_Control_Panel.command，
这一个保留。

同时移走 runtime/desktop_launcher_archive_20260310（文件名已叠成
.app.bak-20260310-v2.app.bak-20260310-v2）与根目录无人引用的 .venv（140MB）。

剩下的启动器写进各目录的 README.md，逐个说明什么时候用。""",

    "_old_trade_orderhistory": """修复历史委托查询失败，并加入计时探针

历史委托一直查不到数据，而且没人发现。

富途的 history_order_list_query 在记录数约 5000 条时直接断开连接而不是返回错误。
控制终端默认查 2026-04-01 至今（121 天）正好越过这个临界点：重试 6 次、耗时 35 秒、
最终失败，随后被 _safe_live_fetch 吞掉异常返回空表——页面看起来正常，历史委托却
一直是空的。

实测各跨度：7天 0.7s/251条 · 30天 2.9s/1214条 · 60天 3.8s/1658条 · 90天 10.4s/4174条
· 120天 失败。据此把查询按 45 天分段后再合并去重。

结果：121 天从「35 秒失败、0 条」变为「12.9 秒成功、5613 条」，并用手动逐段查询作
独立对照，两者行数完全一致。股票页首屏 54.8 秒降到 47.3 秒。

同时加入分阶段计时探针（设 JQ_PROFILE_LIVE=1 启用），把各步耗时写到
runtime/live_payload_timing.json——正是它定位到瓶颈在历史委托而非我起初猜测的
日线拉取。""",

    "_old_trade": """新增插件架构：功能可插拔，支持统一版与独立版

把交易终端从「手工维护的功能清单 + if/elif 分发」改成插件架构。

核心 (plugin.py) 定义 Feature 契约与注册表，且不 import 任何具体功能；功能放在
features/ 里自行登记；外壳 (shell.py) 从注册表生成导航与首页。新增一个功能只需
往 features/ 丢一个文件——不必改核心、导航或分发。以前要改三处，漏一处就出现
「侧边栏有按钮但点了没反应」。

同一份代码给出两种运行形态：
  统一版  streamlit run src/taa_futu/dashboard_app.py
  独立版  JQ_FEATURE=news streamlit run src/taa_futu/standalone.py
两者调用同一个 Feature.render，不存在两套实现互相偏离。

契约要点：功能自带可用性检查（依赖缺失时自己报告而非拖垮外壳）；渲染异常就地
捕获显示，一个功能坏掉不会白屏；placement 显式声明在首页是大卡片、快捷链接还是
内容区块。

股票与历史模拟由 dashboard_app 自行登记而不放进 features/：本文件是 Streamlit
直接执行的脚本，若从 features/ 反向 import 它，这 5700 行模块会被再完整导入一次，
实测导致端到端测试超时。

同时修正 test_dashboard_e2e 的超时上限（30→150 秒）。股票页渲染实测约 55 秒，
用改动前后的代码各测一次分别为 54.7s 与 54.8s，说明这是页面固有耗时而非本次引入；
原上限在数据量变大后已不够，表现为「运行超时」而非断言失败，容易被误读成路由损坏。

新增市场新闻工作台：嵌入采集器已生成的交互看板而非用 Streamlit 重做，从而保留
点击跳转、卡片联动与拖拽问 AI（这些依赖浏览器 DOM，Streamlit 做不了）。""",
}


def git(repo, *args, timeout=300):
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def main():
    out = {"kind": "gitcommit", "repos": {}}
    for name, repo in REPOS.items():
        entry = {}
        if not repo.exists():
            entry["error"] = "仓库不存在"
            out["repos"][name] = entry
            continue

        # 清掉可能残留的锁（确认没有 git 进程时才安全，调用方已核实）
        lock = repo / ".git" / "index.lock"
        if lock.exists():
            try:
                lock.unlink()
                entry["removed_stale_lock"] = True
            except OSError as exc:
                entry["lock_error"] = str(exc)

        rc, so, se = git(repo, "add", "-A")
        entry["add_rc"] = rc
        if se:
            entry["add_warnings"] = se.splitlines()[:3]

        rc, so, _ = git(repo, "status", "--porcelain")
        staged = len(so.splitlines())
        entry["files_staged"] = staged
        if staged == 0:
            entry["result"] = "没有需要提交的改动"
            out["repos"][name] = entry
            continue

        rc, so, se = git(repo, "-c", "user.name=Jiao", "-c", "user.email=jiaojingqi9@gmail.com",
                         "commit", "-m", MESSAGES[name])
        entry["commit_rc"] = rc
        entry["commit_out"] = (so or se)[-300:]

        rc, so, _ = git(repo, "log", "--oneline", "-1")
        entry["head"] = so
        rc, so, _ = git(repo, "status", "--porcelain")
        entry["remaining_uncommitted"] = len(so.splitlines())
        p = subprocess.run(["du", "-sh", str(repo / ".git")], capture_output=True, text=True)
        entry["git_size"] = (p.stdout or "").split()[0] if p.stdout else "?"
        rc, so, _ = git(repo, "remote", "-v")
        entry["remote"] = so or "(无远程仓库)"
        out["repos"][name] = entry

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
