#!/usr/bin/env python3
"""apply_source_fixes - 让采集源修复生效。

1. 把运行中的 collect plist 里的 User-Agent 换成含邮箱的版本
   （SEC 强制要求 UA 含真实联系方式，否则 403）
2. 重启 collect，让新的 live_sources.json 与新 UA 生效
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LABEL = "ai.codex.marketnews.collect"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
NEW_UA = "MarketNewsCollector/0.1 (Jiao Jingqi jiaojingqi9@gmail.com)"


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def main():
    out = {"kind": "apply_source_fixes"}

    # 1. patch plist
    if PLIST.exists():
        txt = PLIST.read_text(encoding="utf-8")
        before = txt
        txt = re.sub(
            r"MARKET_NEWS_USER_AGENT='[^']*'",
            f"MARKET_NEWS_USER_AGENT='{NEW_UA}'",
            txt,
        )
        if txt != before:
            PLIST.write_text(txt, encoding="utf-8")
            out["plist_patched"] = True
        else:
            out["plist_patched"] = False
            out["plist_note"] = "未找到 UA 声明或已是新值"
        m = re.search(r"MARKET_NEWS_USER_AGENT='([^']*)'", txt)
        out["user_agent_now"] = m.group(1) if m else None
    else:
        out["plist_exists"] = False

    # 2. 重启 collect（先卸载再加载，确保读取新 plist）
    uid = os.getuid()
    rc, so, _ = _run(["ps", "-Ao", "pid,command"])
    pids = [
        int(l.strip().split()[0]) for l in so.splitlines()
        if "market_news" in l and "collect" in l and "grep" not in l
    ]
    for pid in pids:
        _run(["kill", "-9", str(pid)])
    out["killed_pids"] = pids

    _run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
    time.sleep(1)
    rc, _, se = _run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)])
    out["bootstrap_rc"] = rc
    if se:
        out["bootstrap_err"] = se[:200]
    _run(["launchctl", "kickstart", "-k", f"gui/{uid}/{LABEL}"])

    time.sleep(6)
    rc, so, _ = _run(["ps", "-Ao", "pid,etime,command"])
    out["collect_after"] = [
        l.strip()[:100] for l in so.splitlines()
        if "market_news" in l and "collect" in l and "grep" not in l
    ]
    out["restarted"] = bool(out["collect_after"])

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
