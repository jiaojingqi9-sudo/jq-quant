#!/usr/bin/env python3
"""stop_codex_review - 停用「每小时 Codex 运维审阅推送」这一个 launchd 任务。

范围严格限定：只处理 label = ai.codex.marketnews.codexreview。
不碰采集(collect)、投递(notify)、健康(health)、学习(news-learning) 等任何其他任务。
plist 文件保留（改名备份），随时可以恢复。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

LABEL = "ai.codex.marketnews.codexreview"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def main():
    out = {"kind": "stop_codex_review", "label": LABEL, "steps": []}
    uid = os.getuid()

    # 1. 停止并卸载
    rc, so, se = _run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
    out["steps"].append({"action": "bootout", "rc": rc, "out": so[:200], "err": se[:200]})

    # 2. 兜底：老接口
    rc2, so2, se2 = _run(["launchctl", "remove", LABEL])
    out["steps"].append({"action": "remove", "rc": rc2, "err": se2[:200]})

    # 3. 杀掉可能仍在跑的进程
    rc3, _, _ = _run(["pkill", "-f", "market_news news-learning-codex-review"])
    out["steps"].append({"action": "pkill", "rc": rc3})

    # 4. plist 改名备份，防止重启后自动加载
    if PLIST.exists():
        backup = PLIST.with_suffix(".plist.disabled")
        try:
            PLIST.rename(backup)
            out["plist"] = {"moved_to": str(backup), "ok": True}
        except OSError as exc:
            out["plist"] = {"error": str(exc), "ok": False}
    else:
        out["plist"] = {"exists": False}

    # 5. 确认结果
    rc4, so4, _ = _run(["launchctl", "list"])
    still = [ln for ln in so4.splitlines() if LABEL in ln]
    out["still_loaded"] = still
    out["stopped"] = not still and not PLIST.exists()

    # 6. 顺带列出仍在运行的 market news 相关任务，供确认没误伤
    out["other_marketnews_jobs"] = [
        ln.split("\t")[-1] for ln in so4.splitlines()
        if "marketnews" in ln.lower() or "market_news" in ln.lower()
    ]

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
