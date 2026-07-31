#!/usr/bin/env python3
"""plist_probe - 查看 notify 任务的 plist 为什么解析失败。

launchd_audit 报告 ai.codex.marketnews.notify.plist 在第 11 行第 176 列有非法
字符。这个任务正是「把新闻发到手机」的那一步——文件坏了意味着一旦重启或重新
加载，推送会再次失效。只读，先看清楚坏在哪。
"""
import json
import plistlib
import subprocess
import sys
from pathlib import Path

P = Path.home() / "Library" / "LaunchAgents" / "ai.codex.marketnews.notify.plist"


def main():
    out = {"kind": "plist_probe", "path": str(P), "exists": P.exists()}
    if not P.exists():
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    raw = P.read_bytes()
    out["bytes"] = len(raw)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    out["line_count"] = len(lines)

    # 报错在第 11 行；把 10-12 行完整打出来
    for i in range(max(0, 9), min(len(lines), 13)):
        out.setdefault("lines_10_to_13", {})[str(i + 1)] = lines[i]

    if len(lines) >= 11:
        l = lines[10]
        out["line11_len"] = len(l)
        # 第 176 列附近
        out["line11_around_col176"] = l[150:210]
        # 找出未转义的 XML 特殊字符
        bad = [(idx, ch) for idx, ch in enumerate(l, 1) if ch in "<>&"]
        out["line11_special_chars"] = [{"col": c, "ch": ch} for c, ch in bad][:20]

    # 系统自己的校验器怎么说
    r = subprocess.run(["plutil", "-lint", str(P)], capture_output=True, text=True)
    out["plutil"] = (r.stdout or r.stderr).strip()[:300]

    # 当前 launchd 里这个 label 的状态
    r = subprocess.run(["launchctl", "list", "ai.codex.marketnews.notify"],
                       capture_output=True, text=True)
    out["launchctl_rc"] = r.returncode
    out["launchctl"] = (r.stdout or r.stderr).strip()[:600]

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
