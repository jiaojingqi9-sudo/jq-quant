#!/usr/bin/env python3
"""capture_schema - 只取富途各接口返回表的「列名与类型」，不取任何数值。

演示模式要造假数据，列名必须和真接口一致，否则页面上会 KeyError。但真实的
持仓、余额、成交价不能进仓库。所以这里只读 schema：列名、dtype、行数。
每一列的实际内容一律不输出。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "All here" / "trade" / "src"))


def describe(obj):
    """只描述结构，绝不返回内容。"""
    import pandas as pd
    if isinstance(obj, pd.DataFrame):
        return {"type": "DataFrame", "rows": int(len(obj)),
                "index_name": obj.index.name,
                "columns": {str(c): str(obj[c].dtype) for c in obj.columns}}
    if isinstance(obj, pd.Series):
        return {"type": "Series", "length": int(len(obj)),
                "index": [str(i) for i in obj.index],
                "dtypes": {str(i): type(obj[i]).__name__ for i in obj.index}}
    if isinstance(obj, dict):
        return {"type": "dict", "keys": sorted(str(k) for k in obj)}
    return {"type": type(obj).__name__}


def main():
    out = {"kind": "capture_schema"}
    try:
        from taa_futu.config import load_settings
        from taa_futu.futu_gateway import FutuPaperTrader
    except Exception as exc:
        out["error"] = f"导入失败: {type(exc).__name__}: {exc}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    settings = load_settings()
    try:
        with FutuPaperTrader(settings) as trader:
            acc = trader.resolve_trade_account()
            out["schemas"] = {}
            probes = [
                ("account_info", lambda: trader.get_account_info(acc)),
                ("positions",    lambda: trader.get_positions(acc)),
                ("open_orders",  lambda: trader.get_open_orders(acc)),
                ("order_history", lambda: trader.get_order_history(acc, "2026-07-25", "2026-07-31")),
                ("snapshots",    lambda: trader.get_snapshots(["US.SPY", "US.IEF"])),
                ("daily_klines", lambda: trader.get_daily_klines("US.SPY", 5)),
                ("recent_klines", lambda: trader.get_recent_klines("US.SPY", 5)),
                ("recent_tickers", lambda: trader.get_recent_tickers("US.SPY", 5)),
                ("order_book",   lambda: trader.get_order_book_safe("US.SPY", 5)),
                ("healthcheck",  lambda: trader.healthcheck()),
            ]
            for name, fn in probes:
                try:
                    out["schemas"][name] = describe(fn())
                except Exception as exc:
                    out["schemas"][name] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
