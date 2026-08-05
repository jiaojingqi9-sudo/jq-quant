"""把多天的标定结果合成一份「市场性格档案」，供合成市场引擎读取。

单日结果会被当天的行情带偏（有的日子有消息、有的日子很闷），所以引擎要用的是
多天的中位数。这里同时把标定结果翻译成引擎直接能用的参数名。

用法：
    python3 merge_calibration.py US.NVDA
"""
from __future__ import annotations

import glob
import json
import math
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CAL_DIR = REPO / "runtime" / "exec_trainer" / "calibration"


def merge(symbol: str) -> dict:
    tag = symbol.replace(".", "_")
    # 只收「US_NVDA_2026-08-04.json」这种盘口标定结果。
    # 不能用 f"{tag}_*.json"：那会把成交量标定的 US_NVDA_vol_*.json 也收进来，
    # 两种文件字段完全不同，合并时会直接 KeyError。
    files = sorted(CAL_DIR.glob(f"{tag}_20*.json"))
    if not files:
        raise SystemExit(f"{CAL_DIR} 里没有 {symbol} 的标定结果，先跑 calibrate.py")
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    rows = [r for r in rows if "depth_profile" in r]
    if not rows:
        raise SystemExit(f"{CAL_DIR} 里没有 {symbol} 的盘口标定结果，先跑 calibrate.py")

    def med(fn) -> float:
        vals = [fn(r) for r in rows]
        vals = [v for v in vals if v is not None and v == v]
        return statistics.median(vals) if vals else 0.0

    n_levels = min(len(r["depth_profile"]) for r in rows)
    depth = [med(lambda r, i=i: r["depth_profile"][i]["median"]) for i in range(n_levels)]
    # 各档挂单量的离散度。引擎里做市商每次重挂都要按这个抖一下，
    # 否则每一档永远是同一个数，盘口看起来像画上去的。
    # 对数正态下 p75/p25 = exp(2 × 0.6745 × sigma)，反解出 sigma。
    disp = []
    for i in range(n_levels):
        p25 = med(lambda r, i=i: r["depth_profile"][i]["p25"])
        p75 = med(lambda r, i=i: r["depth_profile"][i]["p75"])
        if p25 > 0 and p75 > p25:
            disp.append(math.log(p75 / p25) / 1.349)
    dispersion = statistics.median(disp) if disp else 0.35

    # 价差分布合并成概率，引擎按这个分布决定做市商挂多宽
    hist: dict[str, int] = {}
    total = 0
    for r in rows:
        for k, v in r["spread_ticks"]["hist"].items():
            hist[k] = hist.get(k, 0) + v
            total += v
    spread_dist = {k: hist[k] / total for k in sorted(hist, key=lambda x: (x == ">5", x))}

    return {
        "symbol": symbol,
        "days": [r["day"] for r in rows],
        "n_days": len(rows),
        # 数据质量：买卖两边快照不同步造成的交叉盘口比例。记在档案里，
        # 提醒后面的人这份数据本身有这么多要丢。
        "data_quality": {"crossed_rate": med(lambda r: r["crossed_rate"])},

        # ── 引擎直接使用的参数 ──────────────────────────────────────
        "tick_size": 0.01,
        "spread_ticks_dist": spread_dist,
        "spread_ticks_median": med(lambda r: r["spread_ticks"]["median"]),
        # 单边各档的目标挂单量。做市商的梯子要挂成这个形状——注意是驼峰形，
        # 最优档最薄，第 3–6 档最厚：做市商不会把大量堆在最前面，怕被一扫而空。
        "depth_profile_shares": depth,
        "depth_dispersion": round(dispersion, 4),
        "depth_top5_shares": sum(depth[:5]),
        "depth_top10_shares": sum(depth[:10]),
        # 中间价的随机游走强度与均值回复速度
        "sigma_per_sec": med(lambda r: r["midprice"]["sigma_per_sec"]),
        "sigma_5min_pct": med(lambda r: r["midprice"]["sigma_5min_pct"]),
        "kappa_per_step": med(lambda r: r["midprice"]["kappa_per_step"]),
        # 跳跃（消息）：一天几次、每次多大
        "jumps_per_day": med(lambda r: r["midprice"]["jump_count"]),
        "jump_abs_median_bp": med(lambda r: r["midprice"]["jump_abs_median_bp"]),
        # 挂撤单事件：每秒几次、每次多少股
        "order_events_per_sec": med(lambda r: r["order_events"]["events_per_sec"]),
        "order_size_median": med(lambda r: r["order_events"]["size_median"]),
        "order_size_p90": med(lambda r: r["order_events"]["size_p90"]),
        "order_size_p99": med(lambda r: r["order_events"]["size_p99"]),
    }


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "US.NVDA"
    prof = merge(symbol)
    out = CAL_DIR.parent / f"{symbol.replace('.', '_')}_market_profile.json"
    out.write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(prof, ensure_ascii=False, indent=2))
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
