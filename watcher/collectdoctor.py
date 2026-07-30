#!/usr/bin/env python3
"""collectdoctor - 诊断（可选修复）market news 采集线卡死。

失效模式：launchd 每 5 分钟触发一次 collect，但上一轮没退出就不会启动下一轮。
只要有一次网络请求挂住不返回，整条采集线就永久停摆。

默认只诊断。传 {"skill":"collectdoctor","fix":true} 才会：
  - 杀掉卡死的 collect 进程（仅限 market_news collect）
  - 让 launchd 立刻重跑一轮
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path.home() / "All here" / "news collector"
STATUS = REPO / "reports" / "live" / "collect_status.json"
LABEL = "ai.codex.marketnews.collect"
STALE_SECONDS = 900  # 15 分钟没心跳就算卡死


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def main():
    want_fix = "--fix" in sys.argv or os.environ.get("COLLECTDOCTOR_FIX") == "1"
    for arg in sys.argv:
        if arg.startswith("{"):
            try:
                if json.loads(arg).get("fix"):
                    want_fix = True
            except Exception:
                pass

    out = {"kind": "collectdoctor", "fix_requested": want_fix}

    # 1. 心跳新鲜度
    try:
        st = json.loads(STATUS.read_text(encoding="utf-8"))
        ts = st.get("timestamp", "")
        out["status_timestamp"] = ts
        out["status_overall"] = st.get("overall_status")
        try:
            dt = datetime.fromisoformat(ts)
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            out["heartbeat_age_seconds"] = int(age)
            out["stale"] = age > STALE_SECONDS
        except Exception:
            out["heartbeat_age_seconds"] = None
    except Exception as exc:
        out["status_error"] = str(exc)

    # 2. 进程实况
    rc, so, _ = _run(["ps", "-Ao", "pid,etime,command"])
    procs = []
    for line in so.splitlines():
        if "market_news" in line and "collect" in line and "grep" not in line:
            m = re.match(r"\s*(\d+)\s+(\S+)\s+(.*)", line)
            if m:
                procs.append({"pid": int(m.group(1)), "elapsed": m.group(2), "cmd": m.group(3)[:120]})
    out["collect_processes"] = procs
    out["process_count"] = len(procs)

    # 3. launchd 里该任务的状态
    rc, so, _ = _run(["launchctl", "list"])
    for line in so.splitlines():
        if LABEL in line:
            parts = line.split("\t")
            out["launchd"] = {"raw": line.strip(), "pid": parts[0], "last_exit": parts[1] if len(parts) > 1 else ""}
            break

    # 3b. 投递线进程（同样是 --watch 常驻，同样需要重启才会加载新代码）
    rc, so_ps, _ = _run(["ps", "-Ao", "pid,etime,command"])
    notify_procs = []
    for line in so_ps.splitlines():
        if "market_news" in line and " notify" in line and "grep" not in line:
            m = re.match(r"\s*(\d+)\s+(\S+)\s+(.*)", line)
            if m:
                notify_procs.append({"pid": int(m.group(1)), "elapsed": m.group(2), "cmd": m.group(3)[:120]})
    out["notify_processes"] = notify_procs

    # 4. 修复：重启常驻线，让它们加载新代码与新配置
    if want_fix:
        uid = os.getuid()
        actions = []
        # 只重启这两条线；health / review-api / news-learning 不动
        for label, plist_procs in (
            (LABEL, procs),
            ("ai.codex.marketnews.notify", notify_procs),
        ):
            for p in plist_procs:
                rc, _, se = _run(["kill", "-9", str(p["pid"])])
                actions.append({"kill": p["pid"], "label": label, "rc": rc, "err": se[:120]})
            time.sleep(1)
            rc, _, se2 = _run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"])
            actions.append({"kickstart": label, "rc": rc, "err": se2[:200]})
        out["fix_actions"] = actions
        time.sleep(6)
        rc, so3, _ = _run(["ps", "-Ao", "pid,etime,command"])
        out["processes_after_fix"] = [
            l.strip()[:110] for l in so3.splitlines()
            if "market_news" in l and ("collect" in l or " notify" in l) and "grep" not in l
        ]

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
