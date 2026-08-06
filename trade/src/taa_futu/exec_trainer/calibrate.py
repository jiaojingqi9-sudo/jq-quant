"""从真实盘口数据里量出一只股票的「市场性格」，供合成市场引擎标定用。

对齐的验收标准是 JP Morgan AI Research 的 *Get Real: Realism Metrics for
Robust Limit Order Book Market Simulations*（arXiv 1912.04941）。那篇提出
6 大类 26 项「真实性指标」，一个合成订单簿市场要逐项对得上才算数。
这个脚本负责量出其中**这份数据支持得起**的那些，量不了的在输出里标明白，
不拿猜的数冒充实测。

—— 两条踩过的坑，写在最前面 ——

1. **碎股不算一档。** 实盘摆盘长这样：
       买 218.00 × 11股   ← 碎股
          217.99 × 507股  ← 真正的流动性从这才开始
   把 11 股当「买一档」，量出来的买一深度是 125 股、价差 1 tick 占 54%；
   只算整手档则是 346 股、1 tick 占 25%——差一倍多。
   OFR 工作论文 25-01（The Reliability of Odd-Lot Liquidity）实测：大单成交前
   5–10 毫秒碎股撤单量显著上升，碎股流动性「在不到一毫秒内消失」。
   也就是说那些量你根本吃不到，NBBO 按定义也只收整手。
   所以：**低于 ROUND_LOT 的档位直接跳过，不占档位序号。**

2. **买卖两边分开量，不要混着取中位数。** 混着取会把两边的不对称抹平，
   而且第一版就是这么把买一量到 125 的（买边实际 214）。

用法：
    python3 calibrate.py 2026-08-04 US.NVDA
"""
from __future__ import annotations

import gzip
import json
import math
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MARKET_DATA = REPO / "runtime" / "market_data"
OUT_DIR = REPO / "runtime" / "exec_trainer" / "calibration"

# 一手 = 100 股。低于这个数的档位是碎股，不计入盘口形状（理由见模块文档）。
ROUND_LOT = 100

# 只统计到第 20 档。再深的档位挂单极少被碰到，对撮合和盘口形状没有影响。
MAX_LEVEL = 20

# 中间价重采样间隔（秒）。盘口快照最密的时候 0.03 秒一张，全用上没必要。
RESAMPLE_SEC = 1.0
# **只认间隔落在这个窗口里的相邻样本**，其余整对丢弃。
#
# 这一条是被一个很隐蔽的错误逼出来的。这份数据只覆盖盘中约 20% 的秒，
# 中间全是洞。原来的写法是「间隔 0–30 秒都算一步」，再除以平均间隔来年化——
# 等于把 1 秒的收益和 30 秒的收益混进同一个样本里。不同方差的样本混在一起，
# **光是混合本身就会造出肥尾**，跟市场没关系。实测同一天：
#     间隔 0–30 秒混着算 → 峰度 238，5 分钟波动 0.027%
#     只取 0.8–1.5 秒    → 峰度 42.9，5 分钟波动 0.168%
#     只取 4–6 秒        → 峰度 16.8，5 分钟波动 0.160%
# 后两个互相吻合（波动率在不同抽样尺度上应当一致），第一个是纯假象。
# 峰度随尺度变小是真实现象（JP Morgan《Get Real》里的「聚合正态性」），
# 但必须在同一尺度上量才有意义。
DT_MIN, DT_MAX = 0.8, 1.5

# 只统计常规交易时段。数据里的 ts 是 UTC，美东夏令时 ET = UTC-4，
# 所以 09:30–16:00 ET 对应 13:30–20:00 UTC。
RTH_START_SEC = 13.5 * 3600
RTH_END_SEC = 20.0 * 3600

# 单步（约 1 秒）中间价变动超过这个比例的当坏数据丢掉。
MAX_STEP_RETURN = 0.05

# 日内分格：半小时一格，13 格
BUCKET_SEC = 1800.0
N_BUCKETS = int((RTH_END_SEC - RTH_START_SEC) / BUCKET_SEC)


def _open_any(day_dir: Path, name: str):
    plain, gz = day_dir / name, day_dir / (name + ".gz")
    if plain.exists() and plain.stat().st_size > 1024:
        return plain.open("rt", encoding="utf-8", errors="replace")
    if gz.exists() and gz.stat().st_size > 1024:
        return gzip.open(gz, "rt", encoding="utf-8", errors="replace")
    return None


def _pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    k = (len(values) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return values[int(k)]
    return values[lo] * (hi - k) + values[hi] * (k - lo)


def _acf(x: list[float], lag: int) -> float:
    """自相关系数。JP Morgan 那套里收益的线性自相关应当接近 0，
    而绝对收益的自相关要显著为正且缓慢衰减（波动率聚集）。"""
    n = len(x)
    if n <= lag + 2:
        return float("nan")
    m = statistics.mean(x)
    num = sum((x[i] - m) * (x[i + lag] - m) for i in range(n - lag))
    den = sum((v - m) ** 2 for v in x)
    return num / den if den > 0 else float("nan")


def _hill(values: list[float], tail_frac: float = 0.05) -> float:
    """Hill 估计量，估幂律尾部指数 α（分布 P(X>x) ~ x^-α）。

    JP Morgan 那篇里订单大小的分布是幂律，指数 1+μ 约 2–2.7。
    我第一版用的对数正态，形状就不对——尾巴太薄，玩家永远碰不到真正的大单。
    """
    v = sorted(v for v in values if v > 0)
    if len(v) < 200:
        return float("nan")
    k = max(10, int(len(v) * tail_frac))
    tail = v[-k:]
    xmin = tail[0]
    if xmin <= 0:
        return float("nan")
    s = sum(math.log(x / xmin) for x in tail[1:])
    return (len(tail) - 1) / s if s > 0 else float("nan")


def _round_levels(side: list, cap: int) -> list[tuple[float, float]]:
    """只保留整手档，按原顺序取前 cap 档。碎股跳过、不占档位序号。"""
    out = []
    for row in side:
        p, q = row[0], row[1]
        if q >= ROUND_LOT:
            out.append((p, q))
            if len(out) >= cap:
                break
    return out


def collect(day: str, symbol: str) -> dict:
    day_dir = MARKET_DATA / day
    fh = _open_any(day_dir, "lob.jsonl")
    if fh is None:
        raise SystemExit(f"{day} 没有可读的 lob 数据")

    spreads_tick: list[int] = []
    spreads_tick_oddlot: list[int] = []       # 含碎股的版本，只为记录差多少
    depth_bid: list[list[float]] = [[] for _ in range(MAX_LEVEL)]
    depth_ask: list[list[float]] = [[] for _ in range(MAX_LEVEL)]
    mids: list[tuple[float, float]] = []
    deltas: list[float] = []
    delta_span_sec = 0.0
    n_snap = 0
    n_crossed = 0
    n_oddlot_touch = 0
    prev_book: dict[float, float] | None = None
    prev_t: float | None = None
    last_sample_t = -1e9
    bucket_mid: list[list[float]] = [[] for _ in range(N_BUCKETS)]

    with fh:
        for line in fh:
            if symbol not in line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("code") != symbol:
                continue
            bid, ask = r.get("bid") or [], r.get("ask") or []
            if len(bid) < 2 or len(ask) < 2:
                continue

            ts = r.get("ts", "")
            try:
                t = int(ts[11:13]) * 3600 + int(ts[14:16]) * 60 + float(ts[17:26])
            except Exception:
                continue
            if not (RTH_START_SEC <= t <= RTH_END_SEC):
                continue

            # 含碎股的最优价——只用来记录「碎股把价差压窄了多少」
            if ask[0][0] > bid[0][0]:
                spreads_tick_oddlot.append(int(round((ask[0][0] - bid[0][0]) * 100)))
            if bid[0][1] < ROUND_LOT or ask[0][1] < ROUND_LOT:
                n_oddlot_touch += 1

            rb = _round_levels(bid, MAX_LEVEL)
            ra = _round_levels(ask, MAX_LEVEL)
            if not rb or not ra:
                continue
            bb, ba = rb[0][0], ra[0][0]
            spread = ba - bb
            if spread <= 0 or bb <= 0:
                n_crossed += 1
                continue
            n_snap += 1
            spreads_tick.append(int(round(spread * 100)))

            for i, (_, q) in enumerate(rb):
                depth_bid[i].append(q)
            for i, (_, q) in enumerate(ra):
                depth_ask[i].append(q)

            mid = (bb + ba) / 2
            if t - last_sample_t >= RESAMPLE_SEC:
                mids.append((t, mid))
                last_sample_t = t
            bi = min(max(int((t - RTH_START_SEC) // BUCKET_SEC), 0), N_BUCKETS - 1)
            bucket_mid[bi].append(mid)

            # 同价位量变化。买边价格取负数以免和卖边撞车。只看整手档。
            book = {}
            for p, q in rb:
                book[-p] = q
            for p, q in ra:
                book[p] = q
            if prev_book is not None and prev_t is not None and 0 < t - prev_t < 5:
                for p, q in book.items():
                    d = q - prev_book.get(p, 0.0)
                    if d:
                        deltas.append(abs(d))
                delta_span_sec += t - prev_t
            prev_book, prev_t = book, t

    if n_snap == 0:
        raise SystemExit(f"{day} 里没有 {symbol} 的可用快照")

    # ── 价差 ──────────────────────────────────────────────────────────
    def _hist(vals):
        h = {}
        for sp in vals:
            key = str(sp) if sp <= 5 else ">5"
            h[key] = h.get(key, 0) + 1
        return h

    spreads_tick.sort()
    spreads_tick_oddlot.sort()

    # ── 深度剖面（买卖分开） ──────────────────────────────────────────
    def _profile(acc):
        out = []
        for i, vals in enumerate(acc):
            if len(vals) < 50:
                continue
            vals.sort()
            mean = statistics.mean(vals)
            var = statistics.pvariance(vals)
            out.append({
                "level": i + 1,
                "median": _pct(vals, 0.5),
                "mean": mean,
                "p25": _pct(vals, 0.25),
                "p75": _pct(vals, 0.75),
                # Gamma 分布的矩估计：JP Morgan 那篇说各档深度服从 Gamma
                "gamma_shape": (mean * mean / var) if var > 0 else float("nan"),
                "gamma_scale": (var / mean) if mean > 0 else float("nan"),
                "n": len(vals),
            })
        return out

    # ── 中间价：波动率、跳跃、以及 JP Morgan 要的收益分布指标 ─────────
    mids.sort()
    rets, dts = [], []
    for i in range(1, len(mids)):
        dt = mids[i][0] - mids[i - 1][0]
        if DT_MIN <= dt <= DT_MAX and mids[i - 1][1] > 0:
            step = mids[i][1] / mids[i - 1][1] - 1
            if abs(step) > MAX_STEP_RETURN:
                continue
            rets.append(math.log(1 + step))
            dts.append(dt)
    sigma_1s = statistics.pstdev(rets) / math.sqrt(statistics.mean(dts)) if rets else 0.0

    jumps = []
    if rets:
        sd = statistics.pstdev(rets)
        thr = 5 * sd if sd > 0 else float("inf")
        jumps = [r for r in rets if abs(r) > thr]

    # 收益分布的形状（JP Morgan 第 1 类，7 项里能量的几项）
    stylized = {}
    if len(rets) > 500:
        m = statistics.mean(rets)
        sd = statistics.pstdev(rets)
        if sd > 0:
            stylized["kurtosis"] = sum(((r - m) / sd) ** 4 for r in rets) / len(rets)
            stylized["skew"] = sum(((r - m) / sd) ** 3 for r in rets) / len(rets)
        # 收益的线性自相关应接近 0
        stylized["acf_ret"] = {str(k): _acf(rets, k) for k in (1, 5, 20)}
        # 绝对收益的自相关应显著为正、缓慢衰减 —— 波动率聚集
        absr = [abs(r) for r in rets]
        stylized["acf_absret"] = {str(k): _acf(absr, k) for k in (1, 5, 20, 50)}
        stylized["n_returns"] = len(rets)
        stylized["dt_window"] = [DT_MIN, DT_MAX]
        # 聚合正态性：把相邻 5 个 1 秒收益加起来，峰度应当明显下降
        agg = [sum(rets[i:i + 5]) for i in range(0, len(rets) - 5, 5)]
        if len(agg) > 200:
            am, asd = statistics.mean(agg), statistics.pstdev(agg)
            if asd > 0:
                stylized["kurtosis_5s"] = sum(((r - am) / asd) ** 4 for r in agg) / len(agg)

    kappa = 0.0
    if len(mids) > 100:
        m = [x[1] for x in mids]
        mbar = statistics.mean(m)
        num = sum((m[i] - mbar) * (m[i + 1] - m[i]) for i in range(len(m) - 1))
        den = sum((m[i] - mbar) ** 2 for i in range(len(m) - 1))
        if den > 0:
            kappa = -num / den

    # ── 挂撤单事件与单量分布 ─────────────────────────────────────────
    deltas.sort()
    events_per_sec = len(deltas) / delta_span_sec if delta_span_sec > 0 else 0.0

    # 各时段的波动率，用来对「量-波动率正相关」（JP Morgan 第 1 类最后一项）
    bucket_vol = []
    for i in range(N_BUCKETS):
        v = bucket_mid[i]
        if len(v) > 30:
            rr = [math.log(v[j] / v[j - 1]) for j in range(1, len(v))
                  if v[j - 1] > 0 and abs(v[j] / v[j - 1] - 1) < MAX_STEP_RETURN]
            bucket_vol.append(statistics.pstdev(rr) if rr else 0.0)
        else:
            bucket_vol.append(0.0)

    return {
        "day": day,
        "symbol": symbol,
        "round_lot": ROUND_LOT,
        "snapshots": n_snap,
        "crossed_dropped": n_crossed,
        "crossed_rate": n_crossed / max(1, n_snap + n_crossed),
        # 碎股占了多少便宜——记下来，免得以后又有人拿含碎股的数去标定
        "oddlot_touch_rate": n_oddlot_touch / max(1, n_snap + n_crossed),
        "spread_ticks": {
            "median": _pct([float(s) for s in spreads_tick], 0.5),
            "mean": statistics.mean(spreads_tick),
            "hist": _hist(spreads_tick),
        },
        "spread_ticks_with_oddlot": {
            "median": _pct([float(s) for s in spreads_tick_oddlot], 0.5)
            if spreads_tick_oddlot else float("nan"),
            "hist": _hist(spreads_tick_oddlot),
        },
        "depth_bid": _profile(depth_bid),
        "depth_ask": _profile(depth_ask),
        "midprice": {
            "samples": len(mids),
            "sigma_per_sec": sigma_1s,
            "sigma_5min_pct": sigma_1s * math.sqrt(300) * 100,
            "kappa_per_step": kappa,
            "jump_count": len(jumps),
            "jump_abs_median_bp": (statistics.median([abs(j) for j in jumps]) * 1e4)
            if jumps else 0.0,
        },
        "stylized_facts": stylized,
        "bucket_volatility": bucket_vol,
        "order_events": {
            "events_per_sec": events_per_sec,
            "size_median": _pct(deltas, 0.5),
            "size_mean": statistics.mean(deltas) if deltas else 0.0,
            "size_p90": _pct(deltas, 0.9),
            "size_p99": _pct(deltas, 0.99),
            # 幂律尾指数。JP Morgan 那篇给的参考区间是 2–2.7。
            "size_tail_alpha": _hill(deltas),
            "n": len(deltas),
        },
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    day = sys.argv[1]
    symbol = sys.argv[2] if len(sys.argv) > 2 else "US.NVDA"
    out = collect(day, symbol)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{symbol.replace('.', '_')}_{day}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    d = out["depth_bid"]
    print(f"{day}  快照 {out['snapshots']:,}  碎股占最优价 {out['oddlot_touch_rate']*100:.1f}%  "
          f"价差中位 {out['spread_ticks']['median']:.0f} tick（含碎股 "
          f"{out['spread_ticks_with_oddlot']['median']:.0f}）  "
          f"买一 {d[0]['median']:.0f} 股  单量尾指数 {out['order_events']['size_tail_alpha']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
