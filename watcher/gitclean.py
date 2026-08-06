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

REPO = Path.home() / "All here" / "trade"
LOCKS = [
    ".git/index.lock",
    ".git/packed-refs.lock",
    ".git/refs/remotes/origin/HEAD.lock",
    ".git/refs/remotes/origin/main.lock",
]


def git(*args, timeout=180):
    p = subprocess.run(["git", "-C", str(REPO), *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def main():
    out = {"kind": "gitclean", "removed": [], "kept": []}

    # 1) 清锁。也扫一遍 .git 下所有 .lock，免得漏
    for rel in LOCKS:
        p = REPO / rel
        if p.exists():
            try:
                p.unlink()
                out["removed"].append(rel)
            except OSError as exc:
                out["kept"].append(f"{rel}: {exc}")
    for p in (REPO / ".git").rglob("*.lock"):
        try:
            rel = str(p.relative_to(REPO))
            p.unlink()
            out["removed"].append(rel)
        except OSError as exc:
            out["kept"].append(f"{p}: {exc}")

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
    rc, so, se = git("fsck", "--no-progress", "--connectivity-only", timeout=300)
    out["fsck_rc"] = rc
    out["fsck_out"] = ((so or "") + (se or ""))[-300:] or "(干净)"

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
