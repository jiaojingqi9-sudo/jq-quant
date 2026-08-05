"""从 1 分钟 K 线量出「一天成交多少股」和「日内什么时候忙」。

为什么单独一个脚本、不并进 calibrate.py：
两者的数据源和可信度完全不同，混在一起会互相污染。

  盘口快照（lob.jsonl）——覆盖率只有两成多，一天 23400 秒里只看得见约 5000 秒。
    能量准的是「盘口长什么样」（价差、各档厚度），因为那是横截面性质，
    抽样看几千次就够了。量不准的是「一共成交了多少」，因为那是累计量，
    漏掉的部分就是漏掉了。

  1 分钟 K 线（klines.jsonl）——交易所报的成交量，实测 390 根一根不缺。
    量成交总量和日内曲线就该用它。

第一版拿盘口的「挂撤单事件数」当日内活跃度的替身，结果算出来收盘前那半小时
只有 0.77 倍——美股实际上收盘前是全天第二忙的时段。那个替身量到的其实是
波动率和采集器的勤快程度，不是成交量。改用 K 线之后是标准的 U 形：
开盘 2.2 倍、中午 0.5 倍、收盘前 1.9 倍。

用法：
    python3 calibrate_volume.py 2026-08-04 US.NVDA
"""
from __future__ import annotations

import collections
import gzip
import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MARKET_DATA = REPO / "runtime" / "market_data"
OUT_DIR = REPO / "runtime" / "exec_trainer" / "calibration"

# 美股常规时段 09:30–16:00，按半小时一格共 13 格
RTH_START_MIN = 9 * 60 + 30
RTH_END_MIN = 16 * 60
BUCKET_MIN = 30
N_BUCKETS = (RTH_END_MIN - RTH_START_MIN) // BUCKET_MIN


def _open_any(day_dir: Path, name: str):
    plain, gz = day_dir / name, day_dir / (name + ".gz")
    if plain.exists() and plain.stat().st_size > 1024:
        return plain.open("rt", encoding="utf-8", errors="replace")
    if gz.exists() and gz.stat().st_size > 1024:
        return gzip.open(gz, "rt", encoding="utf-8", errors="replace")
    return None


def collect(day: str, symbol: str) -> dict:
    fh = _open_any(MARKET_DATA / day, "klines.jsonl")
    if fh is None:
        raise SystemExit(f"{day} 没有可读的 klines 数据")

    # 同一分钟会在很多张快照里重复出现（采集器每次都带一段回看窗口），
    # 按 time_key 去重，后写的覆盖先写的。
    vol: dict[str, float] = {}
    turn: dict[str, float] = {}
    closes: list[float] = []
    with fh:
        for line in fh:
            if symbol not in line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("code") != symbol or r.get("type") != "klines":
                continue
            for row in r.get("rows") or []:
                k = row.get("time_key")
                if row.get("code") != symbol or not k:
                    continue
                # 只认整分钟的 1 分钟 K 线。2026-07-31 那天的数据里混进了
                # 秒位不为 00 的记录（09:17:09、:24、:25…），而且收盘价范围
                # 从 99.84 到 201.81——NVDA 那天没跌到 99，说明别的标的的
                # 数据被打上了 NVDA 的 code。不挡住的话那天会算出 2.6 亿股、
                # VWAP 129.72，直接把中位数带偏。
                if k[17:19] != "00":
                    continue
                vol[k] = row.get("volume", 0) or 0
                turn[k] = row.get("turnover", 0.0) or 0.0
                px = row.get("close")
                if px:
                    closes.append(px)

    # 只留当天盘中。K 线的 time_key 是美东当地时间。
    rth = [k for k in vol
           if k.startswith(day) and RTH_START_MIN <= int(k[11:13]) * 60 + int(k[14:16]) < RTH_END_MIN]
    # 三道体检，任何一道不过就整天作废——宁可少几天，不要脏数据进中位数
    expected = RTH_END_MIN - RTH_START_MIN
    if not (expected * 0.95 <= len(rth) <= expected):
        raise SystemExit(
            f"{day} 盘中 K 线 {len(rth)} 根，应为 {expected} 根（允许缺 5%）——这天作废")
    if closes and min(closes) > 0 and max(closes) / min(closes) > 1.3:
        raise SystemExit(
            f"{day} 收盘价范围 {min(closes):.2f}→{max(closes):.2f}，一天涨跌超过 30% "
            f"不可能，说明混进了别的标的——这天作废")

    total = sum(vol[k] for k in rth)
    total_turn = sum(turn[k] for k in rth)
    buckets = collections.defaultdict(float)
    for k in rth:
        i = (int(k[11:13]) * 60 + int(k[14:16]) - RTH_START_MIN) // BUCKET_MIN
        buckets[min(max(i, 0), N_BUCKETS - 1)] += vol[k]

    mean = total / N_BUCKETS if total else 1.0
    profile = []
    for i in range(N_BUCKETS):
        h, m = divmod(RTH_START_MIN + i * BUCKET_MIN, 60)
        profile.append({
            "bucket": i,
            "start_et": f"{h}:{m:02d}",
            "shares": buckets[i],
            "share_of_day": buckets[i] / total if total else 0.0,
            "vs_flat": buckets[i] / mean if mean else 0.0,
        })

    return {
        "day": day,
        "symbol": symbol,
        "minutes": len(rth),
        "minutes_expected": (RTH_END_MIN - RTH_START_MIN),
        "rth_volume": total,
        "rth_turnover": total_turn,
        "vwap": (total_turn / total) if total else 0.0,
        "volume_per_sec": total / ((RTH_END_MIN - RTH_START_MIN) * 60),
        "intraday_volume": profile,
    }


def merge(symbol: str) -> dict:
    tag = symbol.replace(".", "_")
    files = sorted(OUT_DIR.glob(f"{tag}_vol_*.json"))
    if not files:
        raise SystemExit(f"没有 {symbol} 的成交量标定结果")
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    # 合并时再挡一道：目录里可能留着旧版本跑出来的、没过体检的结果文件。
    # 只信盘中 K 线齐全的那些天。
    expected = RTH_END_MIN - RTH_START_MIN
    dropped = [r["day"] for r in rows if r.get("minutes", 0) < expected * 0.95]
    rows = [r for r in rows if r.get("minutes", 0) >= expected * 0.95]
    if not rows:
        raise SystemExit("没有一天的成交量数据是齐全的")
    prof = []
    for i in range(N_BUCKETS):
        vals = [r["intraday_volume"][i]["vs_flat"] for r in rows]
        prof.append(round(statistics.median(vals), 4))
    # 归一化成「均值为 1」，方便直接当流量倍数用
    m = statistics.mean(prof)
    prof = [round(x / m, 4) for x in prof]
    return {
        "symbol": symbol,
        "days": [r["day"] for r in rows],
        "n_days": len(rows),
        "days_dropped": dropped,
        "rth_volume_median": statistics.median([r["rth_volume"] for r in rows]),
        "volume_per_sec_median": statistics.median([r["volume_per_sec"] for r in rows]),
        "vwap_median": statistics.median([r["vwap"] for r in rows]),
        "minutes_covered": [r["minutes"] for r in rows],
        "intraday_volume_profile": prof,
        "bucket_minutes": BUCKET_MIN,
        "rth_start_et": "9:30",
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    symbol = sys.argv[2] if len(sys.argv) > 2 else "US.NVDA"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if sys.argv[1] == "merge":
        out = merge(symbol)
        path = OUT_DIR.parent / f"{symbol.replace('.', '_')}_volume_profile.json"
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"\n已写入 {path}")
        return 0
    day = sys.argv[1]
    out = collect(day, symbol)
    path = OUT_DIR / f"{symbol.replace('.', '_')}_vol_{day}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{day}  盘中 {out['minutes']}/{out['minutes_expected']} 分钟  "
          f"成交 {out['rth_volume']:,.0f} 股  VWAP {out['vwap']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
