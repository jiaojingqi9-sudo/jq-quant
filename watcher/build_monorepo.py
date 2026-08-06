#!/usr/bin/env python3
"""build_monorepo - 把本地各系统汇聚成 JQ Quant 单一仓库并推送到 GitHub。

重要：本脚本**不移动、不修改**任何正在运行的目录。它在一个独立的暂存目录里
组装仓库，因此 launchd 任务、.app 启动器、写死的路径全都不受影响。
文件系统的整理改名是后续独立的一步。

结构：
    jq-quant/
      trade/           ← 从本地 trade 仓库导入，保留提交历史
      news-collector/  ← 从本地 news collector 仓库导入，保留提交历史
      watcher/         ← 邮差脚本（原本无版本历史）
      skills/          ← 技能脚本（原本无版本历史）
      README.md

7/24 那两个旧仓库（quant-trading-workbench / market-news-collector）不动，
按用户要求以本地现状为准。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
ALL = HOME / "All here"
STAGE = ALL / ".jq_quant_repo"
REMOTE = "https://github.com/jiaojingqi9-sudo/jq-quant.git"

SUBTREES = [
    ("trade", ALL / "trade", "codex/stocks_chose"),
    ("news-collector", ALL / "news collector", "codex/chinatechnicnews"),
]
PLAIN_COPIES = [
    ("watcher", ALL / "futu_watcher"),
    ("skills", ALL / "skills"),
]
COPY_SKIP = {"__pycache__", ".pytest_cache", ".DS_Store", ".git"}

README = """# JQ Quant

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
"""


def run(cmd, cwd=None, timeout=600, check=False):
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}: {(p.stderr or p.stdout)[-300:]}")
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def copy_tree(src: Path, dst: Path):
    def ignore(_dir, names):
        return [n for n in names if n in COPY_SKIP or n.endswith(".pyc")]
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=ignore)


def main():
    out = {"kind": "build_monorepo", "steps": []}
    fresh = "--fresh" in sys.argv

    if fresh and STAGE.exists():
        shutil.rmtree(STAGE)

    if not STAGE.exists():
        STAGE.mkdir(parents=True)
        run(["git", "init", "-b", "main"], cwd=STAGE, check=True)
        out["steps"].append("初始化暂存仓库")
    else:
        out["steps"].append("复用已有暂存仓库")

    # README + .gitignore 先落地，保证有个初始提交（subtree add 需要 HEAD）
    (STAGE / "README.md").write_text(README, encoding="utf-8")
    (STAGE / ".gitignore").write_text(
        "__pycache__/\n*.py[cod]\n.DS_Store\n.venv/\n*.log\n", encoding="utf-8")
    run(["git", "add", "-A"], cwd=STAGE)
    rc, so, se = run(["git", "-c", "user.name=Jiao", "-c", "user.email=jiaojingqi9@gmail.com",
                      "commit", "-m", "JQ Quant: 初始化单一仓库"], cwd=STAGE)
    out["steps"].append(f"初始提交 rc={rc}")

    # 导入两个系统，保留各自历史
    for prefix, repo, branch in SUBTREES:
        if not (repo / ".git").exists():
            out["steps"].append(f"跳过 {prefix}：不是 git 仓库")
            continue
        rc, head, _ = run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"])
        use_branch = head or branch
        rc, so, se = run(["git", "subtree", "add", f"--prefix={prefix}",
                          str(repo), use_branch], cwd=STAGE, timeout=900)
        if rc != 0:
            # 已存在则改用 pull
            rc2, so2, se2 = run(["git", "subtree", "pull", f"--prefix={prefix}",
                                 str(repo), use_branch, "-m", f"更新 {prefix}"],
                                cwd=STAGE, timeout=900)
            out["steps"].append(f"{prefix}: add 失败({se[-90:]}), pull rc={rc2}")
        else:
            out["steps"].append(f"{prefix}: 已导入分支 {use_branch}（保留历史）")

    # 无历史的目录直接复制
    for prefix, src in PLAIN_COPIES:
        if src.exists():
            copy_tree(src, STAGE / prefix)
            out["steps"].append(f"{prefix}: 已复制 {len(list((STAGE/prefix).rglob('*')))} 个条目")

    run(["git", "add", "-A"], cwd=STAGE)
    rc, so, se = run(["git", "-c", "user.name=Jiao", "-c", "user.email=jiaojingqi9@gmail.com",
                      "commit", "-m", "加入邮差与技能脚本"], cwd=STAGE)
    out["steps"].append(f"提交附属目录 rc={rc}")

    # 远程
    run(["git", "remote", "remove", "origin"], cwd=STAGE)
    run(["git", "remote", "add", "origin", REMOTE], cwd=STAGE, check=True)

    rc, so, se = run(["git", "push", "-u", "origin", "main", "--force"],
                     cwd=STAGE, timeout=1800)
    out["push_rc"] = rc
    out["push_output"] = (so + " " + se)[-400:]

    rc, so, _ = run(["git", "log", "--oneline", "-6"], cwd=STAGE)
    out["recent_commits"] = so.splitlines()
    rc, so, _ = run(["git", "rev-list", "--count", "HEAD"], cwd=STAGE)
    out["total_commits"] = so
    rc, so, _ = run(["du", "-sh", str(STAGE / ".git")])
    out["repo_size"] = so.split()[0] if so else "?"
    out["stage_dir"] = str(STAGE)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
