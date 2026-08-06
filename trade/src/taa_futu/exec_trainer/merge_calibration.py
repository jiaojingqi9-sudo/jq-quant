"""把多天的标定结果合成一份「市场性格档案」，供合成市场引擎读取。

单日结果会被当天的行情带偏（有的日子有消息、有的日子很闷），所以引擎要用的是
多天的中位数。这里同时把标定结果翻译成引擎直接能用的参数名。

档案里同时带一份 `realism_targets`：那是 JP Morgan《Get Real》那 26 项里
**这份数据量得出来**的部分，验收脚本拿它跟合成市场跑出来的数逐项对。
量不出来的项（订单寿命、到达间隔——L2 快照没有订单号，推不出来）标成 null，
用文献值并注明，不拿猜的数冒充实测。

用法：
    python3 merge_calibration.py US.NVDA
"""
from __future__ import annotations

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
    # 不能用 f"{tag}_*.json"：那会把成交量标定的 US_NVDA_vol_*.json 也收进来。
    files = sorted(CAL_DIR.glob(f"{tag}_20*.json"))
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    rows = [r for r in rows if "depth_bid" in r]
    if not rows:
        raise SystemExit(f"{CAL_DIR} 里没有 {symbol} 的盘口标定结果，先跑 calibrate.py")

    def med(fn) -> float:
        vals = []
        for r in rows:
            try:
                v = fn(r)
            except Exception:
                continue
            if v is not None and v == v:
                vals.append(v)
        return statistics.median(vals) if vals else 0.0

    n_levels = min(min(len(r["depth_bid"]), len(r["depth_ask"])) for r in rows)

    # 买卖两边分开量、分开合并。做市商挂梯子时两边用各自的形状。
    def side_profile(key):
        return [med(lambda r, i=i: r[key][i]["median"]) for i in range(n_levels)]

    depth_bid = side_profile("depth_bid")
    depth_ask = side_profile("depth_ask")
    # 引擎的梯子用两边的平均——做市商是对称报价的，
    # 但两边的数各自留在档案里，方便以后做不对称做市商。
    depth = [(b + a) / 2 for b, a in zip(depth_bid, depth_ask)]

    # 各档深度的 Gamma 参数。JP Morgan 那篇说盘口各档深度服从 Gamma 分布，
    # 第一版用的对数正态是拍的。shape/scale 用矩估计，逐档取多天中位数。
    gamma = []
    for i in range(n_levels):
        sh = med(lambda r, i=i: (r["depth_bid"][i]["gamma_shape"] + r["depth_ask"][i]["gamma_shape"]) / 2)
        sc = med(lambda r, i=i: (r["depth_bid"][i]["gamma_scale"] + r["depth_ask"][i]["gamma_scale"]) / 2)
        gamma.append({"shape": round(sh, 4), "scale": round(sc, 2)})

    # 价差分布（整手口径）
    hist: dict[str, int] = {}
    total = 0
    for r in rows:
        for k, v in r["spread_ticks"]["hist"].items():
            hist[k] = hist.get(k, 0) + v
            total += v
    spread_dist = {k: hist[k] / total for k in sorted(hist, key=lambda x: (x == ">5", x))}

    # 含碎股的价差分布——只作记录，提醒后来人这两个口径差多少
    hist_o: dict[str, int] = {}
    tot_o = 0
    for r in rows:
        for k, v in (r.get("spread_ticks_with_oddlot", {}).get("hist") or {}).items():
            hist_o[k] = hist_o.get(k, 0) + v
            tot_o += v
    spread_dist_odd = ({k: hist_o[k] / tot_o for k in sorted(hist_o, key=lambda x: (x == ">5", x))}
                       if tot_o else {})

    # ── JP Morgan《Get Real》里这份数据量得出来的那些 ──────────────────
    def med_nested(path, key):
        return med(lambda r: (r.get("stylized_facts") or {}).get(path, {}).get(key))

    realism = {
        "_source": "JP Morgan AI Research, Get Real (arXiv 1912.04941), 6 类 26 项",
        "kurtosis": med(lambda r: (r.get("stylized_facts") or {}).get("kurtosis")),
        "skew": med(lambda r: (r.get("stylized_facts") or {}).get("skew")),
        # 收益的线性自相关：应当接近 0
        "acf_ret_1": med_nested("acf_ret", "1"),
        "acf_ret_5": med_nested("acf_ret", "5"),
        "acf_ret_20": med_nested("acf_ret", "20"),
        # 绝对收益的自相关：应显著为正且缓慢衰减（波动率聚集）
        "acf_absret_1": med_nested("acf_absret", "1"),
        "acf_absret_5": med_nested("acf_absret", "5"),
        "acf_absret_20": med_nested("acf_absret", "20"),
        "acf_absret_50": med_nested("acf_absret", "50"),
        "spread_ticks_median": med(lambda r: r["spread_ticks"]["median"]),
        "sigma_5min_pct": med(lambda r: r["midprice"]["sigma_5min_pct"]),
        # 量不出来的：L2 快照没有订单号，订单寿命和到达间隔推不出来
        "order_lifetime_alpha": None,
        "order_interarrival_dist": None,
        "_unmeasurable_note": (
            "订单寿命与到达间隔需要逐笔订单流（含订单号）才能量；L2 快照做不到。"
            "引擎里这两项用文献值：寿命幂律指数 1.3–1.6，到达间隔取指数分布。"),
    }

    return {
        "symbol": symbol,
        "days": [r["day"] for r in rows],
        "n_days": len(rows),
        "round_lot": rows[0].get("round_lot", 100),
        "data_quality": {
            "crossed_rate": med(lambda r: r["crossed_rate"]),
            # 有多大比例的快照里，最优价那一档是碎股。这个数很高（实测五成到七成），
            # 是「必须按整手口径标定」的直接证据。
            "oddlot_touch_rate": med(lambda r: r.get("oddlot_touch_rate", 0.0)),
        },

        # ── 引擎直接使用的参数 ──────────────────────────────────────
        "tick_size": 0.01,
        "spread_ticks_dist": spread_dist,
        "spread_ticks_median": med(lambda r: r["spread_ticks"]["median"]),
        "spread_ticks_dist_with_oddlot": spread_dist_odd,
        "depth_profile_shares": depth,
        "depth_profile_bid": depth_bid,
        "depth_profile_ask": depth_ask,
        "depth_gamma": gamma,
        "depth_top5_shares": sum(depth[:5]),
        "depth_top10_shares": sum(depth[:10]),
        "sigma_per_sec": med(lambda r: r["midprice"]["sigma_per_sec"]),
        "sigma_5min_pct": med(lambda r: r["midprice"]["sigma_5min_pct"]),
        "kappa_per_step": med(lambda r: r["midprice"]["kappa_per_step"]),
        "jumps_per_day": med(lambda r: r["midprice"]["jump_count"]),
        "jump_abs_median_bp": med(lambda r: r["midprice"]["jump_abs_median_bp"]),
        "order_events_per_sec": med(lambda r: r["order_events"]["events_per_sec"]),
        "order_size_median": med(lambda r: r["order_events"]["size_median"]),
        "order_size_mean": med(lambda r: r["order_events"]["size_mean"]),
        "order_size_p90": med(lambda r: r["order_events"]["size_p90"]),
        "order_size_p99": med(lambda r: r["order_events"]["size_p99"]),
        # 幂律尾指数。注意这是拿「相邻快照的档位量变化」当订单大小的替身量的，
        # 不是真正的逐笔订单大小，所以跟文献的 2–2.7 不能直接比。
        "order_size_tail_alpha": med(lambda r: r["order_events"]["size_tail_alpha"]),
        "realism_targets": realism,
    }


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "US.NVDA"
    prof = merge(symbol)
    out = CAL_DIR.parent / f"{symbol.replace('.', '_')}_market_profile.json"
    out.write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
    q = prof["data_quality"]
    print(f"{prof['n_days']} 天  整手口径")
    print(f"  碎股占最优价 {q['oddlot_touch_rate']*100:.0f}%   交叉盘口 {q['crossed_rate']*100:.1f}%")
    print(f"  价差分布 " + "  ".join(f"{k}:{v*100:.0f}%" for k, v in list(prof['spread_ticks_dist'].items())[:5]))
    print(f"  含碎股则是 " + "  ".join(f"{k}:{v*100:.0f}%" for k, v in list(prof['spread_ticks_dist_with_oddlot'].items())[:5]))
    print(f"  深度前10 " + " ".join(f"{x:.0f}" for x in prof['depth_profile_shares'][:10]))
    print(f"  Gamma shape 前5 " + " ".join(f"{g['shape']:.2f}" for g in prof['depth_gamma'][:5]))
    r = prof["realism_targets"]
    print(f"  峰度 {r['kurtosis']:.1f}  收益自相关(1/5/20) "
          f"{r['acf_ret_1']:+.3f}/{r['acf_ret_5']:+.3f}/{r['acf_ret_20']:+.3f}")
    print(f"  绝对收益自相关(1/5/20/50) {r['acf_absret_1']:.3f}/{r['acf_absret_5']:.3f}/"
          f"{r['acf_absret_20']:.3f}/{r['acf_absret_50']:.3f}")
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
