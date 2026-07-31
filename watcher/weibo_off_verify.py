#!/usr/bin/env python3
"""weibo_off_verify - 确认微博确实被关掉，且看板不再把它报成错误。

要看三件事：
  1. 采集器工厂不再构造 WeiboCollector
  2. 雪球没被误伤，仍在采集器列表里
  3. cookies 区块把微博报成 disabled 而不是 error，整体状态不再是 error
"""
import json
import sys
from pathlib import Path

NEWS = Path.home() / "All here" / "news collector"
sys.path.insert(0, str(NEWS))


def main() -> int:
    out = {"kind": "weibo_off_verify"}

    cfg = json.loads((NEWS / "config" / "live_sources.json").read_text(encoding="utf-8"))
    out["config"] = {"weibo_enabled": cfg.get("weibo", {}).get("enabled"),
                     "xueqiu_enabled": cfg.get("xueqiu", {}).get("enabled")}

    try:
        from market_news.infrastructure.collectors.factory import build_live_collector
        composite = build_live_collector(NEWS / "config" / "live_sources.json",
                                         "MarketNewsCollector/0.1 (verify)")
        inner = getattr(composite, "collectors", None) or getattr(composite, "_collectors", [])
        names = sorted({type(c).__name__ for c in inner})
        out["collector_classes"] = names
        out["collector_count"] = len(inner)
        out["weibo_built"] = any("Weibo" in n for n in names)
        out["xueqiu_built"] = any("Xueqiu" in n for n in names)
    except Exception as exc:
        out["build_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    try:
        from market_news.services.reporting import MarkdownJsonReporter
        rep = MarkdownJsonReporter(NEWS / "reports" / "live")
        block = rep._runtime_cookie_line()          # type: ignore[attr-defined]
        out["cookie_block"] = {"status": block.get("status"), "detail": block.get("detail"),
                               "modules": block.get("modules")}
    except Exception as exc:
        out["cookie_block_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
