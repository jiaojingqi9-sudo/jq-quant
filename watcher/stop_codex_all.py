#!/usr/bin/env python3
"""stop_codex_all - 停用所有依赖 Codex 的 launchd 任务（Codex 订阅已停）。

只处理明确依赖 Codex CLI 的任务；采集/投递/健康/学习包生成等一律不动。
plist 改名为 .disabled 备份，随时可恢复。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# 仅这两个任务真正调用 Codex CLI
TARGETS = [
    "ai.codex.marketnews.codexreview",   # 每小时运维审阅推送（已在上一步停用）
    "ai.codex.marketnews.threadreview",  # 往 Codex 对话回写的审阅
]
AGENTS = Path.home() / "Library" / "LaunchAgents"


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def main():
    uid = os.getuid()
    out = {"kind": "stop_codex_all", "targets": TARGETS, "results": []}

    for label in TARGETS:
        entry = {"label": label}
        rc, _, se = _run(["launchctl", "bootout", f"gui/{uid}/{label}"])
        entry["bootout_rc"] = rc
        _run(["launchctl", "remove", label])

        plist = AGENTS / f"{label}.plist"
        if plist.exists():
            try:
                plist.rename(plist.with_suffix(".plist.disabled"))
                entry["plist"] = "disabled"
            except OSError as exc:
                entry["plist"] = f"error: {exc}"
        elif (AGENTS / f"{label}.plist.disabled").exists():
            entry["plist"] = "already disabled"
        else:
            entry["plist"] = "not found"
        out["results"].append(entry)

    # 杀掉可能残留的进程
    for pat in ("news-learning-codex-review", "news_learning_thread_review", "thread-review"):
        _run(["pkill", "-f", pat])

    # 最终确认：列出仍加载的 market news 任务
    rc, so, _ = _run(["launchctl", "list"])
    remaining = []
    for line in so.splitlines():
        if "marketnews" in line.lower():
            parts = line.split("\t")
            remaining.append({"label": parts[-1], "pid": parts[0], "last_exit": parts[1] if len(parts) > 1 else ""})
    out["remaining_marketnews_jobs"] = remaining
    out["codex_still_loaded"] = [r["label"] for r in remaining if r["label"] in TARGETS]
    out["all_stopped"] = not out["codex_still_loaded"]

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
