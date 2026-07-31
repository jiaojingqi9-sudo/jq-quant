#!/usr/bin/env python3
"""cookie_probe - 分清「cookie 真的失效」还是「过期标记从五月挂到现在」。

过期标记 (.expired) 只在重新安装 cookie 时才清除。所以看板上那条红色可能是
真失效，也可能是旧标记一直没人清而源其实还能抓。两者处理方式完全不同，
不实测分不出来。

做法：读现有 cookie 文件与标记，然后真的用它们各抓一次，看拿回多少条。只读。
"""
import json
import sys
from pathlib import Path

NEWS = Path.home() / "All here" / "news collector"
sys.path.insert(0, str(NEWS))
COOKIE_DIR = Path.home() / ".market_news"


def main() -> int:
    out = {"kind": "cookie_probe", "cookie_dir": str(COOKIE_DIR)}

    files = []
    if COOKIE_DIR.exists():
        for p in sorted(COOKIE_DIR.iterdir()):
            if p.is_dir():
                continue
            entry = {"name": p.name, "bytes": p.stat().st_size,
                     "mtime": __import__("time").strftime(
                         "%Y-%m-%d %H:%M", __import__("time").localtime(p.stat().st_mtime))}
            if p.suffix == ".expired":
                try:
                    entry["flag"] = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    entry["flag"] = "(读不出)"
            elif p.suffix == ".json":
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    entry["cookie_keys"] = sorted(data.keys())[:12] if isinstance(data, dict) else "非字典"
                except Exception as exc:
                    entry["parse_error"] = str(exc)[:80]
            files.append(entry)
    out["files"] = files

    # 配置里这两个源怎么定义的
    try:
        from market_news.config import load_settings  # type: ignore
        settings = load_settings()
        out["settings_loaded"] = True
    except Exception as exc:
        out["settings_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"

    # 真抓一次
    results = {}
    for source_id in ("weibo", "xueqiu"):
        try:
            from market_news.infrastructure.collectors.factory import build_collectors  # type: ignore
            cols = build_collectors()
            target = None
            for c in cols:
                if getattr(c, "source_id", "") == source_id or source_id in str(getattr(c, "source_id", "")):
                    target = c
                    break
            if target is None:
                results[source_id] = {"status": "未在采集器列表里找到"}
                continue
            records = list(target.collect())
            results[source_id] = {"status": "ok", "records": len(records),
                                  "sample_title": (str(records[0].title)[:60] if records else None)}
        except Exception as exc:
            results[source_id] = {"status": "error",
                                  "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    out["live_fetch"] = results

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
