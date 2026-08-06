#!/usr/bin/env python3
"""把 trade 仓库推到 GitHub。

放在本机跑而不是沙箱：沙箱和挂载的本地 VM 都没有网络，只有邮差这个原生
macOS 进程能访问 GitHub，也只有它拿得到你 keychain / SSH agent 里的凭证。

做的事，按顺序：
  1. git ls-remote 先看一眼远程有什么（只读，不改任何东西）
  2. 把 main 快进到目标分支（默认 codex/stocks_chose）
  3. 配 origin，推 main

**绝不 force push。** 远程如果有本地没有的提交，push 会被拒绝，
脚本把 git 的原话原样带回来，不自作主张。

用法（邮差）：
    {"skill": "gitpush"}
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path.home() / "All here" / "trade"
REMOTE_URL = "https://github.com/jiaojingqi9-sudo/jq-quant.git"
SOURCE_BRANCH = "codex/stocks_chose"     # 最新的东西在这条线上
TARGET_BRANCH = "main"                   # GitHub 打开默认显示的分支


def git(*args, timeout=180):
    p = subprocess.run(["git", "-C", str(REPO), *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def main():
    out = {"kind": "gitpush", "repo": str(REPO), "remote_url": REMOTE_URL}

    lock = REPO / ".git" / "index.lock"
    if lock.exists():
        try:
            lock.unlink()
            out["removed_stale_lock"] = True
        except OSError as exc:
            out["lock_error"] = str(exc)

    # ── 1) 先只读地看一眼远程 ────────────────────────────────────────
    rc, so, se = git("ls-remote", "--heads", REMOTE_URL, timeout=90)
    out["ls_remote_rc"] = rc
    if rc != 0:
        out["ls_remote_error"] = (se or so)[-500:]
        out["hint"] = ("读不到远程。可能是：仓库不存在、是私有的但没登录、"
                       "或者 git 凭证过期。先在终端跑一次 "
                       "`git ls-remote https://github.com/jiaojingqi9-sudo/jq-quant.git` "
                       "看提示。")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1
    out["remote_branches"] = [l.split("\t")[-1] for l in so.splitlines()] if so else []
    out["remote_is_empty"] = not out["remote_branches"]

    # 远程 main 的位置，用来判断会不会被拒
    remote_main = None
    for line in so.splitlines():
        sha, ref = line.split("\t")
        if ref == f"refs/heads/{TARGET_BRANCH}":
            remote_main = sha
    out["remote_main_sha"] = remote_main
    if remote_main:
        rc2, _, _ = git("cat-file", "-e", f"{remote_main}^{{commit}}")
        out["remote_main_in_local_history"] = (rc2 == 0)
        if rc2 != 0:
            out["warning"] = ("远程 main 上有本地没有的提交，直接推会被拒。"
                              "脚本不会 force，先把结果带回去让人决定。")

    # ── 2) 把 main 快进到最新那条线 ─────────────────────────────────
    rc, cur, _ = git("rev-parse", "--abbrev-ref", "HEAD")
    out["branch_before"] = cur
    rc, so_st, _ = git("status", "--porcelain")
    if so_st.strip():
        out["error"] = f"工作区不干净，先提交再推：\n{so_st[:400]}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    rc, ahead, _ = git("rev-list", "--count", f"{TARGET_BRANCH}..{SOURCE_BRANCH}")
    rc, behind, _ = git("rev-list", "--count", f"{SOURCE_BRANCH}..{TARGET_BRANCH}")
    out["main_behind_source"] = ahead
    out["main_has_extra"] = behind
    if behind != "0":
        out["error"] = (f"{TARGET_BRANCH} 上有 {SOURCE_BRANCH} 没有的提交，"
                        f"不是简单快进，需要人工决定怎么合。脚本不动。")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    # --ff-only：只允许快进，有任何需要造 merge commit 的情况就失败退出
    rc, so, se = git("checkout", TARGET_BRANCH)
    out["checkout_rc"] = rc
    if rc != 0:
        out["checkout_error"] = (se or so)[-300:]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1
    rc, so, se = git("merge", "--ff-only", SOURCE_BRANCH)
    out["ff_rc"] = rc
    out["ff_out"] = (so or se)[-300:]
    if rc != 0:
        git("checkout", cur)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1
    rc, so, _ = git("log", "--oneline", "-1")
    out["main_head"] = so

    # ── 3) 配 origin 并推 ───────────────────────────────────────────
    rc, so, _ = git("remote")
    if "origin" in so.split():
        git("remote", "set-url", "origin", REMOTE_URL)
        out["remote_action"] = "已有 origin，改成目标地址"
    else:
        git("remote", "add", "origin", REMOTE_URL)
        out["remote_action"] = "新建 origin"

    rc, so, se = git("push", "-u", "origin", TARGET_BRANCH, timeout=600)
    out["push_rc"] = rc
    out["push_out"] = (so or se)[-800:]
    out["pushed"] = (rc == 0)
    if rc != 0:
        out["hint"] = ("推失败。常见原因：没登录（HTTPS 需要 GitHub token）、"
                       "仓库不存在、或远程有本地没有的提交。上面 push_out 是 git 的原话。")

    rc, so, _ = git("log", "--oneline", "-1")
    out["final_head"] = so
    rc, so, _ = git("remote", "-v")
    out["remote_after"] = so

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["pushed"] else 1


if __name__ == "__main__":
    sys.exit(main())
