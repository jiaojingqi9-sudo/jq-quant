#!/usr/bin/env python3
"""deepclean - 第二轮清理：日志、安装介质、浏览器缓存、漏压的行情文件。

默认预览，--apply 才执行。原则和第一轮一致：
  - 交易记录/研究数据一律不碰（events.jsonl、mininder 的 csv、数据库）
  - 日志只留最近 5MB
  - 能重新下载或重新生成的才删，且移到回收站而非直接删
"""
import gzip
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
ALL = HOME / "All here"
TRADE = ALL / "trade"
TRASH = ALL / "_回收站_20260730"

KEEP_LOG_BYTES = 5 * 1024 * 1024


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def trim_log(path, apply):
    if not path.exists():
        return None
    before = path.stat().st_size
    if before <= KEEP_LOG_BYTES:
        return None
    if apply:
        with open(path, "rb") as f:
            f.seek(before - KEEP_LOG_BYTES)
            tail = f.read()
        nl = tail.find(b"\n")
        if nl > 0:
            tail = tail[nl + 1:]
        with open(path, "wb") as f:
            f.write(tail)
    return {"action": "截断日志", "file": str(path.relative_to(ALL)),
            "before": human(before), "after": human(min(before, KEEP_LOG_BYTES)),
            "freed": before - KEEP_LOG_BYTES}


def move_to_trash(path, label, apply):
    if not path.exists():
        return None
    size = 0
    if path.is_dir():
        for p in path.rglob("*"):
            try:
                size += p.stat().st_size
            except OSError:
                pass
    else:
        size = path.stat().st_size
    if apply:
        dest = TRASH / label
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True) if dest.is_dir() else dest.unlink()
        shutil.move(str(path), str(dest))
    return {"action": "移入回收站", "file": str(path.relative_to(ALL)),
            "size": human(size), "freed": size}


def compress_leftover(apply):
    """第一轮漏压的文件（当时报 FileNotFoundError 的那个）。"""
    out = []
    md = TRADE / "runtime" / "market_data"
    if not md.exists():
        return out
    import datetime
    cutoff = datetime.date.today() - datetime.timedelta(days=3)
    for day in sorted(md.glob("20*-*-*")):
        try:
            d = datetime.datetime.strptime(day.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d > cutoff:
            continue
        for name in ("lob.jsonl", "ticks.jsonl", "klines.jsonl", "snapshots.jsonl"):
            p = day / name
            if not p.exists():
                continue
            gz = p.with_suffix(p.suffix + ".gz")
            if gz.exists():
                continue
            before = p.stat().st_size
            if apply:
                lines_in = sum(1 for _ in open(p, "rb"))
                tmp = gz.with_suffix(".gz.tmp")
                with open(p, "rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fo:
                    shutil.copyfileobj(fin, fo, length=8 * 1024 * 1024)
                lines_out = sum(1 for _ in gzip.open(tmp, "rb"))
                if lines_out != lines_in:
                    tmp.unlink(missing_ok=True)
                    out.append({"action": "压缩失败(行数不符,已保留原文件)",
                                "file": f"{day.name}/{name}", "freed": 0})
                    continue
                os.replace(tmp, gz)
                after = gz.stat().st_size
                p.unlink()
            else:
                after = int(before * 0.04)
            out.append({"action": "压缩", "file": f"{day.name}/{name}",
                        "before": human(before), "after": human(after),
                        "freed": before - after})
    return out


def main():
    apply = "--apply" in sys.argv
    actions = []

    for log in (TRADE / "runtime" / "stock_app.log",
                TRADE / "runtime" / "auto_trader.log",
                TRADE / "runtime" / "archive_marketdata.log"):
        r = trim_log(log, apply)
        if r:
            actions.append(r)

    # OpenD 的 dmg：安装介质，OpenD 本身已装好在跑，需要时可重新下载
    dmg = TRADE / "Futu_OpenD_10.0.6018_Mac" / "Futu_OpenD-GUI_10.0.6018_Mac" / "Futu_OpenD-GUI_10.0.6018_Mac.dmg"
    r = move_to_trash(dmg, "FutuOpenD_installer.dmg", apply)
    if r:
        actions.append(r)

    # Chrome app профиль：纯缓存，app 下次启动会重建
    prof = TRADE / "runtime" / "chrome_app_profile"
    r = move_to_trash(prof, "chrome_app_profile", apply)
    if r:
        actions.append(r)

    actions.extend(compress_leftover(apply))

    freed = sum(a.get("freed", 0) for a in actions)
    print(json.dumps({
        "kind": "deepclean",
        "mode": "APPLY" if apply else "preview",
        "actions": actions,
        "total_freed": human(freed),
        "untouched": [
            "runtime/crypto_ofim/events.jsonl（91万行交易事件，5月至今）",
            "news collector/data/*.db（新闻历史数据库）",
            "05_论文与研究/mininder/*.csv（论文原始数据）",
            "最近 3 天的行情数据（仍在写入）",
        ],
        "note": "预览模式，加 --apply 执行" if not apply else "已执行",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
