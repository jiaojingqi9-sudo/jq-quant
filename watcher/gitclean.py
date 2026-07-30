#!/usr/bin/env python3
"""gitclean - 清理 news collector 仓库里中断操作留下的孤儿对象。

背景：.git 有 11GB，但真正被提交引用的只有 313 个对象、最大 blob 仅 3MB。
其余是反复中断的 git add/gc 留下的不可达对象。reflog 与分支已核对完整，
无悬空提交，所以 prune 是安全的。

安全措施：
  - 跑之前记录所有 ref 的 SHA，跑完逐一核对没有丢失
  - 只在核对通过时报告成功
  - 全程不碰工作区文件（179 个未提交改动不受影响）
"""
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path.home() / "All here" / "news collector"
PROGRESS = Path.home() / "All here" / "news collector" / "runtime" / "gitclean_progress.json"


def git(*args, timeout=3600):
    p = subprocess.run(["git", "-C", str(REPO), *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def refs_snapshot():
    _, so, _ = git("show-ref")
    return {line.split()[1]: line.split()[0] for line in so.splitlines() if " " in line}


def size_of_git():
    p = subprocess.run(["du", "-sh", str(REPO / ".git")], capture_output=True, text=True)
    return (p.stdout or "").split()[0] if p.stdout else "?"


def write(payload):
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    run = "--run" in sys.argv
    before_refs = refs_snapshot()
    before_size = size_of_git()

    if not run:
        _, so, _ = git("count-objects", "-vH")
        print(json.dumps({
            "kind": "gitclean", "mode": "preview",
            "git_size": before_size,
            "refs": before_refs,
            "count_objects": so,
            "note": "预览。用 gitclean_run 后台执行；进度见 runtime/gitclean_progress.json",
        }, ensure_ascii=False, indent=2))
        return 0

    started = time.time()
    write({"running": True, "stage": "开始", "before_size": before_size, "refs_before": before_refs})

    # 1. 让 reflog 立即过期，否则不可达对象仍被 reflog 引用而无法回收
    write({"running": True, "stage": "清 reflog", "before_size": before_size})
    git("reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")

    # 2. 重新打包并丢弃不可达对象
    write({"running": True, "stage": "重打包(耗时最长)", "before_size": before_size})
    rc, so, se = git("gc", "--prune=now", "--aggressive", timeout=7200)

    after_refs = refs_snapshot()
    after_size = size_of_git()

    # 3. 核对每个 ref 都还在且 SHA 未变
    lost = [r for r in before_refs if r not in after_refs]
    changed = [r for r in before_refs if r in after_refs and before_refs[r] != after_refs[r]]
    rc2, so2, _ = git("log", "--oneline", "-5")
    rc3, fsck_out, fsck_err = git("fsck", "--no-progress", "--connectivity-only", timeout=1800)

    result = {
        "running": False,
        "gc_returncode": rc,
        "gc_error": se[-400:] if se else "",
        "before_size": before_size,
        "after_size": after_size,
        "refs_lost": lost,
        "refs_changed": changed,
        "refs_intact": not lost and not changed,
        "recent_commits": so2.splitlines(),
        "fsck_clean": rc3 == 0 and "missing" not in (fsck_out + fsck_err).lower(),
        "elapsed_sec": int(time.time() - started),
    }
    write(result)
    print(json.dumps({"kind": "gitclean", **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
