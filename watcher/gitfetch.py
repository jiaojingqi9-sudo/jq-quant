#!/usr/bin/env python3
"""只做一件事：git fetch origin。不改任何分支、不合并、不推。

为什么单独一个 skill：沙箱和本地 VM 都没网，只有邮差能连 GitHub。
抓下来之后远程的提交对象就躺在本地 .git 里了，后面用不联网的工具
也能随便查看比对——这样「看清楚再决定」不用反复走网络。

用法（邮差）：
    {"skill": "gitfetch"}
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path.home() / "All here" / "trade"


def git(*args, timeout=300):
    p = subprocess.run(["git", "-C", str(REPO), *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def main():
    out = {"kind": "gitfetch"}
    lock = REPO / ".git" / "index.lock"
    if lock.exists():
        try:
            lock.unlink()
        except OSError:
            pass

    rc, so, se = git("fetch", "origin", "--prune")
    out["fetch_rc"] = rc
    out["fetch_out"] = (so or se)[-600:]
    if rc != 0:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    rc, so, _ = git("rev-list", "--count", "origin/main")
    out["remote_main_commits"] = so
    rc, so, _ = git("rev-list", "--count", "main")
    out["local_main_commits"] = so
    rc, so, _ = git("rev-list", "--count", "origin/main..main")
    out["local_only"] = so
    rc, so, _ = git("rev-list", "--count", "main..origin/main")
    out["remote_only"] = so
    rc, so, _ = git("merge-base", "main", "origin/main")
    out["common_ancestor"] = so or "(没有共同祖先——两边是完全无关的历史)"
    rc, so, _ = git("log", "origin/main", "--oneline", "-15")
    out["remote_recent"] = so.splitlines()
    rc, so, _ = git("log", "origin/main", "-1", "--format=%ci  %an  %s")
    out["remote_head_detail"] = so
    rc, so, _ = git("diff", "--stat", "main", "origin/main")
    out["diff_stat_tail"] = so.splitlines()[-25:] if so else []
    # 远程有而本地完全没有的文件
    rc, so, _ = git("diff", "--diff-filter=D", "--name-only", "main", "origin/main")
    out["files_only_on_remote"] = so.splitlines()[:40]
    out["n_files_only_on_remote"] = len(so.splitlines()) if so else 0

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
