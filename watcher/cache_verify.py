#!/usr/bin/env python3
"""cache_verify - 验证日线缓存：结果必须一致，且第二次要快。

正确性优先：先用同一批参数分别在「禁用缓存」和「启用缓存」下取数，
逐个数值比对。只有结果完全一致，加速才有意义。
"""
import json
import subprocess
import sys
from pathlib import Path

TRADE = Path.home() / "All here" / "trade"
VENV = TRADE / ".venv" / "bin" / "python"

CODE = r'''
import os, sys, time, json, shutil
sys.path.insert(0, "src")
from pathlib import Path

CACHE = Path("runtime/cache/daily_closes")
if CACHE.exists():
    shutil.rmtree(CACHE)          # 从干净状态开始

from taa_futu.config import load_settings
from taa_futu.futu_gateway import FutuPaperTrader
from taa_futu import strategy_stack as ss

settings = load_settings()
symbols = list(settings.symbols)[:4]      # 取前 4 个标的即可说明问题
start = "2024-01-01"
res = {"symbols": symbols, "start": start}

def fetch():
    with FutuPaperTrader(settings) as trader:
        trader.resolve_trade_account()
        t0 = time.time()
        df = ss.fetch_futu_daily_closes(trader, symbols, start=start)
        return df, round(time.time() - t0, 1)

# 1. 禁用缓存（基准真值）
os.environ["TAA_DAILY_CLOSE_CACHE"] = "0"
base_df, base_sec = fetch()
res["no_cache_sec"] = base_sec
res["no_cache_shape"] = list(base_df.shape)

# 2. 启用缓存，冷启动（要建缓存，应与基准同量级）
os.environ["TAA_DAILY_CLOSE_CACHE"] = "1"
cold_df, cold_sec = fetch()
res["cold_sec"] = cold_sec
res["cold_shape"] = list(cold_df.shape)

# 3. 启用缓存，热启动（应显著变快）
warm_df, warm_sec = fetch()
res["warm_sec"] = warm_sec
res["warm_shape"] = list(warm_df.shape)

# 4. 正确性：三者必须一致
def cmp(a, b):
    if list(a.shape) != list(b.shape):
        return f"形状不同 {a.shape} vs {b.shape}"
    a2, b2 = a.sort_index(), b.sort_index()
    if not a2.index.equals(b2.index):
        return "索引不同"
    diff = (a2 - b2).abs().max().max()
    return f"最大差异 {float(diff):.10f}" if diff == diff else "存在NaN差异"

res["cold_vs_nocache"] = cmp(base_df, cold_df)
res["warm_vs_nocache"] = cmp(base_df, warm_df)
res["cache_files"] = len(list(CACHE.glob("*.pkl"))) if CACHE.exists() else 0
res["speedup"] = round(base_sec / warm_sec, 1) if warm_sec > 0 else None
print("RES" + json.dumps(res))
'''


def main():
    p = subprocess.run([str(VENV), "-c", CODE], cwd=str(TRADE),
                       capture_output=True, text=True, timeout=900)
    out = (p.stdout or "") + (p.stderr or "")
    line = [l for l in out.splitlines() if l.startswith("RES")]
    print(json.dumps({
        "kind": "cache_verify",
        "result": json.loads(line[0][3:]) if line else {"raw": out[-1200:]},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
