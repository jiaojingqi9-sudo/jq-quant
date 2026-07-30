#!/usr/bin/env python3
"""archive_marketdata - 压缩历史行情数据（无损，代码可直接读 .gz）。

背景：runtime/market_data 每天 2-4GB 的 jsonl，累计 131GB。实测 gzip 压缩率 96%。
taa_futu 的 _load_jsonl 会自动优先读 <name>.jsonl，找不到再读 <name>.jsonl.gz，
所以压缩后代码无需改动。

安全措施：
  - 保留最近 KEEP_DAYS 天不压缩（这些还在被写入）
  - 压缩后立刻校验：能否 gzip 读回、行数是否一致；不一致就保留原文件
  - 原文件确认无误后才删除
  - 支持断点续跑：已经是 .gz 的直接跳过

用法：
  {"skill":"archive_marketdata"}        预览（不动文件）
  {"skill":"archive_marketdata_run"}    后台执行，进度写入 runtime/archive_progress.json
"""
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path.home() / "All here" / "trade" / "runtime" / "market_data"
PROGRESS = Path.home() / "All here" / "trade" / "runtime" / "archive_progress.json"
KEEP_DAYS = 3          # 最近 3 天不动（仍在写入）
TARGETS = ("lob.jsonl", "ticks.jsonl", "klines.jsonl", "snapshots.jsonl")


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def day_dirs():
    cutoff = date.today() - timedelta(days=KEEP_DAYS)
    out = []
    for p in sorted(ROOT.glob("20*-*-*")):
        if not p.is_dir():
            continue
        try:
            d = datetime.strptime(p.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d <= cutoff:
            out.append(p)
    return out


def compress_one(path: Path):
    """压缩单个文件并校验。返回 (ok, 原大小, 新大小, 说明)。"""
    orig = path.stat().st_size
    gz = path.with_suffix(path.suffix + ".gz")
    if gz.exists():
        return False, orig, gz.stat().st_size, "已存在 .gz，跳过"

    # 数原始行数
    lines_in = 0
    with open(path, "rb") as f:
        for _ in f:
            lines_in += 1

    tmp = gz.with_suffix(".gz.tmp")
    try:
        with open(path, "rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout, length=8 * 1024 * 1024)
        # 校验：读回并数行
        lines_out = 0
        with gzip.open(tmp, "rb") as f:
            for _ in f:
                lines_out += 1
        if lines_out != lines_in:
            tmp.unlink(missing_ok=True)
            return False, orig, 0, f"校验失败(行数 {lines_in}->{lines_out})，已保留原文件"
        os.replace(tmp, gz)
        new = gz.stat().st_size
        path.unlink()          # 校验通过才删原文件
        return True, orig, new, "ok"
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return False, orig, 0, f"{type(exc).__name__}: {str(exc)[:80]}"


def main():
    run = "--run" in sys.argv
    dirs = day_dirs()

    if not run:
        total = n = 0
        for d in dirs:
            for name in TARGETS:
                p = d / name
                if p.exists():
                    total += p.stat().st_size
                    n += 1
        print(json.dumps({
            "kind": "archive_marketdata", "mode": "preview",
            "day_dirs_eligible": len(dirs),
            "files_to_compress": n,
            "bytes_to_compress": human(total),
            "keep_recent_days": KEEP_DAYS,
            "estimated_after": human(total * 0.04),
            "note": "预览模式。用 archive_marketdata_run 后台执行；进度见 runtime/archive_progress.json",
        }, ensure_ascii=False, indent=2))
        return 0

    started = time.time()
    done = saved = failed = 0
    freed = 0
    errors = []
    files = [(d, d / n) for d in dirs for n in TARGETS if (d / n).exists()]
    total_files = len(files)

    for i, (d, p) in enumerate(files, 1):
        ok, before, after, msg = compress_one(p)
        if ok:
            saved += 1
            freed += before - after
        elif "跳过" not in msg:
            failed += 1
            if len(errors) < 10:
                errors.append(f"{d.name}/{p.name}: {msg}")
        done = i
        if i % 5 == 0 or i == total_files:
            PROGRESS.write_text(json.dumps({
                "running": i < total_files,
                "done": done, "total": total_files,
                "compressed": saved, "failed": failed,
                "freed": human(freed),
                "elapsed_sec": int(time.time() - started),
                "errors": errors,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

    PROGRESS.write_text(json.dumps({
        "running": False, "done": done, "total": total_files,
        "compressed": saved, "failed": failed, "freed": human(freed),
        "elapsed_sec": int(time.time() - started), "errors": errors,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"kind": "archive_marketdata", "finished": True,
                      "compressed": saved, "freed": human(freed),
                      "failed": failed}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
