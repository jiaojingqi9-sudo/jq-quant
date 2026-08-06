#!/usr/bin/env python3
"""gitcommit - 在本机提交指定仓库的当前状态。

放在本机跑而不是沙箱：沙箱对挂载目录的 unlink 权限受限，git 的索引与对象
写入会失败（unable to unlink / index.lock）。

只做本地提交，不推远程（远程仓库还没建）。

提交信息与目标仓库从队列目录读，不再写死在这个文件里：

    futu_queue/commit_message.txt   提交信息全文（必需，空则不提交）
    futu_queue/commit_repo.txt      仓库名，`trade` 或 `news collector`，
                                    一行一个；文件不存在时默认只提交 trade

这样每次提交只要写那两个文件，不必改脚本本身——以前每提交一次就要把上一条
提交信息从源码里替换掉，既容易误伤，也让 diff 里全是无关内容。
"""
import json
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
QUEUE = HOME / "All here" / "futu_queue"
REPOS = {
    "news collector": HOME / "All here" / "news collector",
    "trade": HOME / "All here" / "trade",
    # monorepo 暂存仓库自己也有文件（顶层 README），改了要能单独提交，
    # 否则只能被 sync_monorepo 那句「同步邮差与技能脚本」顺手带走，
    # 提交历史里看不出到底改了什么。
    "monorepo": HOME / "All here" / ".jq_quant_repo",
}
MESSAGE_FILE = QUEUE / "commit_message.txt"
REPO_FILE = QUEUE / "commit_repo.txt"


def read_message():
    try:
        return MESSAGE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_targets():
    try:
        names = [n.strip() for n in REPO_FILE.read_text(encoding="utf-8").splitlines()]
    except OSError:
        names = []
    names = [n for n in names if n in REPOS]
    return names or ["trade"]


def git(repo, *args, timeout=300):
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def main():
    out = {"kind": "gitcommit", "repos": {}}
    message = read_message()
    targets = read_targets()
    out["message_file"] = str(MESSAGE_FILE)
    out["message_lines"] = len(message.splitlines())
    out["targets"] = targets
    if not message:
        out["error"] = f"没有提交信息：先写 {MESSAGE_FILE}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    for name in targets:
        repo = REPOS[name]
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
        entry["files"] = so.splitlines()[:20]
        if staged == 0:
            entry["result"] = "没有需要提交的改动"
            out["repos"][name] = entry
            continue

        rc, so, se = git(repo, "-c", "user.name=Jiao", "-c", "user.email=jiaojingqi9@gmail.com",
                         "commit", "-m", message)
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
