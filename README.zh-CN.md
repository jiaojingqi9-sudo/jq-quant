# JQ Quant

Jiao 的量化交易与市场新闻系统。

## 组成

| 目录 | 作用 |
|------|------|
| `trade/` | 交易主系统：股票（TAA/Fusion/OFIM/Cascade 四 sleeve）、加密（Binance 现货+永续）、选股器、Streamlit 控制终端 |
| `news-collector/` | 市场新闻采集与分析：采集 → 去重 → 聚类 → 规则打分 → AI 筛选 → 标的映射 → 手机推送 |
| `watcher/` | 后台文件队列服务（"邮差"）：读取任务文件、在本机执行只读诊断与运维脚本 |
| `skills/` | 富途行情/异动分析脚本 |

## 模型后端

AI 判定走本机 Claude Code CLI，链路为 OpenAI HTTP → Claude CLI → OpenClaw，
第一个可用的接管。筛选用 haiku（快），选股与审阅用默认模型。

注意：launchd 任务的 PATH 只有 `/usr/bin:/bin:/usr/sbin:/sbin`，不含 Homebrew，
所以配置里的 `claude_bin` 必须写绝对路径。

## 运行时数据不进版本库

行情数据、新闻数据库、日志、缓存都由 `.gitignore` 排除——它们跑一次就重新生成，
且体积以 GB 计。仓库只存源码与配置。
