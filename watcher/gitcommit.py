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
    "news collector": """修复新闻推送中断，模型层切换到 Claude，并清理仓库

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

    "trade": """新增市场新闻工作台，接入新闻收集器

交易终端新增「市场新闻」页与首页新闻块，数据来自新闻收集器写出的报告文件。

工作台直接嵌入收集器已生成的交互看板，而不是用 Streamlit 重做一遍：看板的
点击跳转、卡片联动、拖拽问 AI 依赖浏览器 DOM，Streamlit 的重跑模型做不了，
重写必然丢功能。嵌入后原有交互全部保留，问 AI 仍通过 127.0.0.1:8765 工作。

耦合是单向且基于文件的：只读收集器写出的 JSON/HTML，不导入也不调用它的
代码，缺失时降级为说明文字——新闻侧出问题不会拖垮交易终端。

同时修复 intraday_replay 判断 plan.jsonl 是否存在时未考虑 .gz 的问题：历史
行情已压缩归档（131GB→5GB，96% 压缩率，_load_jsonl 原生支持 .gz），未修复
时归档日会静默改用 lob 的时间轴。""",
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
