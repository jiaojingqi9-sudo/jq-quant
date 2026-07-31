#!/usr/bin/env python3
"""cookie_check - 跑采集器自带的 `cookies check`，实测两个 cookie 还灵不灵。

比看 .expired 标记可靠：标记只在重装 cookie 时才清，可能是五月挂到现在的旧
状态；check 是真的拿 cookie 去请求一次。
"""
import json
import subprocess
import sys
from pathlib import Path

NEWS = Path.home() / "All here" / "news collector"
PY = "/opt/anaconda3/bin/python3"


def main() -> int:
    out = {"kind": "cookie_check"}
    p = subprocess.run(
        [PY, "-m", "market_news", "cookies", "check"],
        cwd=str(NEWS), capture_output=True, text=True, timeout=180,
        env={**__import__("os").environ,
             "MARKET_NEWS_USER_AGENT": "MarketNewsCollector/0.1 (cookie check)"},
    )
    out["rc"] = p.returncode
    out["stdout"] = (p.stdout or "")[-2500:]
    out["stderr"] = (p.stderr or "")[-800:]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
