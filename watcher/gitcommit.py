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
    "news collector": """无代码改动，同步生成物

本次提交仅为保存工作区状态。看板样式的实际改动在上一个提交。""",

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

    "trade": """修复历史委托查询失败，并加入计时探针

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
