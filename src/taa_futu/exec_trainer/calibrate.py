"""从真实盘口数据里量出一只股票的「市场性格」，供合成市场引擎标定用。

为什么要这一步：合成市场的参数如果照抄教材默认值，跑出来的是一只泛泛的教学玩具股，
跟真正要练的标的对不上。NVDA 的价差只有 1 tick、最优档才两百多股、深度全靠后面十几档
堆起来——这个形状必须先量出来，引擎才有目标可对。

一次处理一个交易日，结果写成一份 JSON。多天的结果由 merge_calibration.py 合并。
分天跑是因为邮差单次调用有时间上限，一天一跑最稳。

用法：
    python3 calibrate.py 2026-08-04 US.NVDA
"""
from __future__ import annotations

import gzip
import json
import math
import statistics
import sys
from bisect import insort
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MARKET_DATA = REPO / "runtime" / "market_data"
OUT_DIR = REPO / "runtime" / "exec_trainer" / "calibration"

# 只统计到第 20 档。再深的档位挂单极少被碰到，对撮合和盘口形状没有影响，
# 但会显著拖慢统计。
MAX_LEVEL = 20
# 中间价重采样间隔（秒）。盘口快照最密的时候 0.03 秒一张，全用上没必要，
# 而且会把「同一时刻的多张快照」当成价格变化。
RESAMPLE_SEC = 1.0

# 只统计常规交易时段。数据里的 ts 是 UTC，美东夏令时 ET = UTC-4，
# 所以 09:30–16:00 ET 对应 13:30–20:00 UTC。2026 年的夏令时是 3/8–11/1，
# 我们所有可用交易日（3/11–8/4）都在这个区间内。
# 为什么必须过滤：盘前盘后价差能到几十个 tick，混进来会把中位数整个拉偏——
# 第一版算出来的「价差中位 2–3 tick」就是这么来的，盘中其实只有 1 tick。
RTH_START_SEC = 13.5 * 3600
RTH_END_SEC = 20.0 * 3600

# 单步（约 1 秒）中间价变动超过这个比例的，当坏数据丢掉。真实的 1 秒 5% 波动
# 在 NVDA 这种标的上不存在；不设这道闸，一个坏点能把当日波动率抬高一百倍
# （实测 7/31 被一个坏点抬到 19%，而正常是 0.2%）。
MAX_STEP_RETURN = 0.05

# 日内活跃度按半小时一格统计。美股 9:30–16:00 共 6.5 小时 = 13 格。
# 为什么要这一格一格地量：真实市场开盘和收盘最忙、中午最闲，而
# 「在量大的时候多做」是做大单最核心的一课。引擎里如果一整天流量都一样，
# 这一课既练不到也测不出来。
BUCKET_SEC = 1800.0
N_BUCKETS = int((RTH_END_SEC - RTH_START_SEC) / BUCKET_SEC)


def _open_any(day_dir: Path, name: str):
    """同一份数据可能是明文也可能是 .gz（老日期会被压缩）。"""
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


def collect(day: str, symbol: str) -> dict:
    day_dir = MARKET_DATA / day
    fh = _open_any(day_dir, "lob.jsonl")
    if fh is None:
        raise SystemExit(f"{day} 没有可读的 lob 数据")

    spreads_tick: list[int] = []
    depth_by_level: list[list[float]] = [[] for _ in range(MAX_LEVEL)]
    mids: list[tuple[float, float]] = []       # (秒, 中间价)，已按 RESAMPLE_SEC 抽稀
    # 相邻快照之间，同一价位的挂单量变化。正数＝有人挂进来，负数＝撤单或成交。
    # 这是在只有 L2 快照的情况下推断「订单到达率」和「单量分布」的唯一途径——
    # 逐笔数据只有约 17% 覆盖率，指望不上。
    deltas: list[float] = []
    delta_span_sec = 0.0
    n_snap = 0
    n_crossed = 0
    prev_book: dict[float, float] | None = None
    prev_t: float | None = None
    last_sample_t = -1e9
    # 每格：观测到的秒数、挂撤单事件数、变动股数、快照数
    buckets = [{"span": 0.0, "events": 0, "shares": 0.0, "snaps": 0} for _ in range(N_BUCKETS)]

    with fh:
        for line in fh:
            if symbol not in line:          # 先做字符串粗筛，比 json.loads 快一个数量级
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("code") != symbol:
                continue
            bid, ask = r.get("bid") or [], r.get("ask") or []
            if len(bid) < 2 or len(ask) < 2:
                continue                     # 只有 1 档的是降级快照，约占 1%，丢掉

            # 时间用当天的秒数，避免反复构造 datetime（这里要跑几万次）
            ts = r.get("ts", "")
            try:
                hh, mm, ss = ts[11:13], ts[14:16], ts[17:26]
                t = int(hh) * 3600 + int(mm) * 60 + float(ss)
            except Exception:
                continue
            if not (RTH_START_SEC <= t <= RTH_END_SEC):
                continue

            bb, ba = bid[0][0], ask[0][0]
            spread = ba - bb
            # 交叉盘口（买一高于卖一）在真实市场不该存在，是买卖两边快照
            # 不同步造成的。实测约占 3–4%，全部丢掉并单独计数。
            if spread <= 0 or bb <= 0:
                n_crossed += 1
                continue
            n_snap += 1
            bi = min(max(int((t - RTH_START_SEC) // BUCKET_SEC), 0), N_BUCKETS - 1)
            buckets[bi]["snaps"] += 1
            # 换算成「几个最小变动价位」。美股 1 tick = 0.01，浮点直接比会有误差。
            spreads_tick.append(int(round(spread * 100)))

            for i in range(min(MAX_LEVEL, len(bid))):
                depth_by_level[i].append(bid[i][1])
            for i in range(min(MAX_LEVEL, len(ask))):
                depth_by_level[i].append(ask[i][1])

            if t - last_sample_t >= RESAMPLE_SEC:
                mids.append((t, (bb + ba) / 2))
                last_sample_t = t

            # 同价位量变化。买卖两边合到一个字典里，买边价格取负数以免撞车。
            book = {}
            for p, q, *_ in bid[:MAX_LEVEL]:
                book[-p] = q
            for p, q, *_ in ask[:MAX_LEVEL]:
                book[p] = q
            if prev_book is not None and prev_t is not None and 0 < t - prev_t < 5:
                for p, q in book.items():
                    d = q - prev_book.get(p, 0.0)
                    if d:
                        deltas.append(abs(d))
                        buckets[bi]["events"] += 1
                        buckets[bi]["shares"] += abs(d)
                delta_span_sec += t - prev_t
                buckets[bi]["span"] += t - prev_t
            prev_book, prev_t = book, t

    if n_snap == 0:
        raise SystemExit(f"{day} 里没有 {symbol} 的可用快照")

    # ── 价差 ──────────────────────────────────────────────────────────
    spreads_tick.sort()
    spread_hist: dict[str, int] = {}
    for sp in spreads_tick:
        key = str(sp) if sp <= 5 else ">5"
        spread_hist[key] = spread_hist.get(key, 0) + 1

    # ── 深度剖面 ──────────────────────────────────────────────────────
    depth = []
    for i, vals in enumerate(depth_by_level):
        if not vals:
            continue
        vals.sort()
        depth.append({
            "level": i + 1,
            "median": _pct(vals, 0.5),
            "p25": _pct(vals, 0.25),
            "p75": _pct(vals, 0.75),
            "n": len(vals),
        })

    # ── 中间价：波动率、均值回复、跳跃 ───────────────────────────────
    mids.sort()
    rets, dts = [], []
    for i in range(1, len(mids)):
        dt = mids[i][0] - mids[i - 1][0]
        if 0 < dt < 30 and mids[i - 1][1] > 0:
            step = mids[i][1] / mids[i - 1][1] - 1
            if abs(step) > MAX_STEP_RETURN:
                continue
            rets.append(math.log(1 + step))
            dts.append(dt)
    sigma_1s = statistics.pstdev(rets) / math.sqrt(statistics.mean(dts)) if rets else 0.0

    # 跳跃＝单步收益超过 5 倍局部标准差。阈值取 5 是为了只抓真正的台阶，
    # 不把正常波动当成消息。
    jumps = []
    if rets:
        sd = statistics.pstdev(rets)
        thr = 5 * sd if sd > 0 else float("inf")
        jumps = [r for r in rets if abs(r) > thr]

    # 均值回复：对 Δm 关于 (m - 当日均值) 做一元回归，斜率的相反数即 kappa。
    kappa = 0.0
    if len(mids) > 100:
        m = [x[1] for x in mids]
        mbar = statistics.mean(m)
        num = sum((m[i] - mbar) * (m[i + 1] - m[i]) for i in range(len(m) - 1))
        den = sum((m[i] - mbar) ** 2 for i in range(len(m) - 1))
        if den > 0:
            kappa = -num / den

    # ── 挂撤单事件 ───────────────────────────────────────────────────
    deltas.sort()
    events_per_sec = len(deltas) / delta_span_sec if delta_span_sec > 0 else 0.0

    # ── 日内活跃度曲线 ───────────────────────────────────────────────
    # 关键：除以「这一格实际观测到多少秒」，不是除以 1800 秒。
    # 数据本身有大量空洞（逐笔覆盖率只有约 17%），直接数事件数量出来的是
    # 「采集器那半小时勤不勤快」，不是「市场那半小时忙不忙」。
    intraday = []
    for i, bk in enumerate(buckets):
        rate = bk["events"] / bk["span"] if bk["span"] > 0 else 0.0
        intraday.append({
            "bucket": i,
            "start_et": f"{9 + (30 + i * 30) // 60}:{(30 + i * 30) % 60:02d}",
            "events_per_sec": rate,
            "shares_per_sec": (bk["shares"] / bk["span"]) if bk["span"] > 0 else 0.0,
            "observed_sec": round(bk["span"], 1),
            "coverage": round(bk["span"] / BUCKET_SEC, 3),
            "snapshots": bk["snaps"],
        })

    return {
        "day": day,
        "symbol": symbol,
        "snapshots": n_snap,
        "crossed_dropped": n_crossed,
        "crossed_rate": n_crossed / max(1, n_snap + n_crossed),
        "spread_ticks": {
            "median": _pct([float(s) for s in spreads_tick], 0.5),
            "mean": statistics.mean(spreads_tick),
            "hist": spread_hist,
        },
        "depth_profile": depth,
        "midprice": {
            "samples": len(mids),
            "sigma_per_sec": sigma_1s,
            "sigma_5min_pct": sigma_1s * math.sqrt(300) * 100,
            "kappa_per_step": kappa,
            "jump_count": len(jumps),
            "jump_abs_median_bp": (statistics.median([abs(j) for j in jumps]) * 1e4) if jumps else 0.0,
        },
        "intraday": intraday,
        "order_events": {
            "events_per_sec": events_per_sec,
            "size_median": _pct(deltas, 0.5),
            "size_p90": _pct(deltas, 0.9),
            "size_p99": _pct(deltas, 0.99),
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
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
