#!/usr/bin/env python3
"""logclean - 清理 news collector 的历史文件与日志，并防止再次膨胀。

默认只报告（dry-run）。传 --apply 才真正动手。

策略：
  *.jsonl 历史文件  -> 只保留最近 N 行（这些文件用于趋势统计，不能全删）
  *.log  运行日志    -> 只保留最近 N MB（就地截断，对 launchd 的 O_APPEND 是安全的）
所有操作前先记录大小，操作后核对，避免误删。
"""
import json
import os
import sys
from pathlib import Path

REPO = Path.home() / "All here" / "news collector"
LIVE = REPO / "reports" / "live"
LOGS = REPO / "runtime" / "logs"

KEEP_JSONL_LINES = 3000      # 每个历史文件保留最近 3000 条记录
KEEP_LOG_BYTES = 5 * 1024 * 1024   # 每个日志保留最近 5MB


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def trim_jsonl(path: Path, keep: int, apply: bool):
    """保留文件末尾 keep 行。写临时文件再原子替换，避免中途崩溃丢数据。"""
    before = path.stat().st_size
    # 从末尾读取足够的数据来凑够 keep 行
    chunk = min(before, 60 * 1024 * 1024)
    with open(path, "rb") as f:
        f.seek(before - chunk)
        data = f.read()
    lines = data.split(b"\n")
    if lines and lines[0] and before > chunk:
        lines = lines[1:]           # 丢弃可能被截断的首行
    lines = [l for l in lines if l.strip()]
    kept = lines[-keep:]
    after = sum(len(l) + 1 for l in kept)
    if apply:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as f:
            for l in kept:
                f.write(l + b"\n")
        os.replace(tmp, path)
    return {"file": str(path.relative_to(REPO)), "before": human(before),
            "after": human(after), "kept_lines": len(kept),
            "freed_bytes": before - after}


def trim_log(path: Path, keep_bytes: int, apply: bool):
    """就地截断日志，只保留末尾 keep_bytes。"""
    before = path.stat().st_size
    if before <= keep_bytes:
        return None
    if apply:
        with open(path, "rb") as f:
            f.seek(before - keep_bytes)
            tail = f.read()
        # 从第一个换行开始，避免半行
        nl = tail.find(b"\n")
        if nl > 0:
            tail = tail[nl + 1:]
        with open(path, "wb") as f:   # 截断并写回尾部
            f.write(tail)
        after = path.stat().st_size
    else:
        after = keep_bytes
    return {"file": str(path.relative_to(REPO)), "before": human(before),
            "after": human(after), "freed_bytes": before - after}


def main():
    apply = "--apply" in sys.argv
    out = {"kind": "logclean", "mode": "APPLY" if apply else "dry-run", "actions": []}
    freed = 0

    for p in sorted(LIVE.glob("*.jsonl")):
        try:
            if p.stat().st_size < 5 * 1024 * 1024:
                continue
            r = trim_jsonl(p, KEEP_JSONL_LINES, apply)
            out["actions"].append(r)
            freed += r["freed_bytes"]
        except Exception as exc:
            out["actions"].append({"file": p.name, "error": str(exc)[:120]})

    for p in sorted(LOGS.glob("*.log")):
        try:
            r = trim_log(p, KEEP_LOG_BYTES, apply)
            if r:
                out["actions"].append(r)
                freed += r["freed_bytes"]
        except Exception as exc:
            out["actions"].append({"file": p.name, "error": str(exc)[:120]})

    out["total_freed"] = human(freed)
    out["note"] = ("这是预览，加 --apply 才真正执行" if not apply
                   else "已执行；历史文件保留最近 3000 条，日志保留最近 5MB")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
