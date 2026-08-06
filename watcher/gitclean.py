#!/usr/bin/env python3
"""清掉 trade 仓库里的残留 .lock，并去掉误加的 origin。

背景：沙箱挂载的那个 Linux VM 对 .git 目录没有 unlink 权限，
`git remote remove` 做到一半会失败并留下 packed-refs.lock 之类的锁文件，
之后任何 git 写操作都会报「Another git process seems to be running」。
只有邮差这个原生 macOS 进程能真正删掉它们。

顺带把 origin 去掉：本地 trade 不是 jq-quant 那个仓库本身，
jq-quant 是 monorepo，trade 是被 subtree 同步进去的一部分。
在 trade 上挂一个指向 jq-quant 的 origin 会误导下一个人（包括我自己）
往错的地方推。

用法（邮差）：
    {"skill": "gitclean"}
"""
import json
import subprocess
import sys
from pathlib import Path

ALL = Path.home() / "All here"
REPO = ALL / "trade"
# 三个都要扫：沙箱那个 Linux VM 只要跑过 git（哪怕只是 status），
# 就会在 .git 里留一个删不掉的 index.lock，下一次真正的 git 写操作就被卡住。
REPOS = [ALL / "trade", ALL / "news collector", ALL / ".jq_quant_repo"]


def git(*args, timeout=180):
    p = subprocess.run(["git", "-C", str(REPO), *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def main():
    out = {"kind": "gitclean", "removed": [], "kept": []}

    # 1) 三个仓库的 .git 下所有 .lock 全清
    for repo in REPOS:
        g = repo / ".git"
        if not g.exists():
            continue
        for p in list(g.rglob("*.lock")):
            try:
                p.unlink()
                out["removed"].append(f"{repo.name}/{p.relative_to(g)}")
            except OSError as exc:
                out["kept"].append(f"{repo.name}/{p.relative_to(g)}: {exc}")

    # 2) 去掉误加的 origin
    rc, so, _ = git("remote")
    out["remotes_before"] = so.split() if so else []
    if "origin" in out["remotes_before"]:
        rc, so2, se2 = git("remote", "remove", "origin")
        out["remove_origin_rc"] = rc
        if rc != 0:
            out["remove_origin_error"] = (se2 or so2)[-300:]

    # 3) 复核
    rc, so, _ = git("remote")
    out["remotes_after"] = so.split() if so else []
    rc, so, _ = git("rev-parse", "--abbrev-ref", "HEAD")
    out["branch"] = so
    rc, so, _ = git("status", "--porcelain")
    out["dirty_files"] = len(so.splitlines()) if so else 0
    rc, so, _ = git("log", "--oneline", "-1")
    out["head"] = so
    rc, so, _ = git("branch", "-v")
    out["branches"] = so.splitlines()
    # fsck 只看有没有明显损坏，慢就跳过
    # 各仓库工作区是否干净——subtree pull 要求全干净才肯动
    out["repos"] = {}
    for repo in REPOS:
        if not (repo / ".git").exists():
            continue
        r = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=120)
        r2 = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, timeout=60)
        out["repos"][repo.name] = {
            "branch": (r2.stdout or "").strip(),
            "dirty": len([x for x in (r.stdout or "").splitlines() if x.strip()]),
        }

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
