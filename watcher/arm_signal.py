#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARM 信号检测：读入一个 snapshot result.json，判定信号并打印中文结论。
  A) 缩量最后一跌：回调中 + 下跌 + 缩量 + 跌不动企稳  -> 可能见底/低吸观察
  B) 放量见顶/派发 + 放量异动走弱：
       - 放量异动走弱（用户自定义）：反常大量(当日量≥2×昨日 或 ≥1.8×20日均量) 且 走弱(当日收跌 或 冲高回落)
       - 高位放量滞涨/冲高回落 / 放量下跌弱收 / 放量跌破MA10
     -> 可能派发/异动，减仓观察
用法： python3 arm_signal.py <result.json 路径>
输出： 末行以 VERDICT= 开头，值为 A / B / AB / NONE / ERROR
注：放量异动走弱抓的是“放量+走弱”的日子（如 6/18 天量冲高回落）；
    缩量/中量的阴跌（如 6/22、6/23）成交量不够，不会触发本类信号——这是设计如此。
"""
import json, re, sys

def load_kline(path):
    d = json.load(open(path, encoding="utf-8"))
    if not d.get("ok", True) and "data" not in d:
        raise RuntimeError(d.get("error", "result not ok"))
    data = d.get("data", d)
    raw = data["raw"] if isinstance(data, dict) and "raw" in data else json.dumps(data)
    obj = json.loads(re.search(r'(\{.*\})', raw, re.S).group(1))
    if obj.get("kline_error"):
        raise RuntimeError("kline_error: " + str(obj["kline_error"]))
    kl = obj.get("kline", [])
    snap = (obj.get("snapshot") or [{}])[0]
    return kl, snap

def evaluate(kl, snap):
    """对一段日K（最后一根视作“当日”）做信号判定，返回 (表头三行, 信号明细notes, verdict)。"""
    closes = [k["close"] for k in kl]
    highs  = [k["high"] for k in kl]
    lows   = [k["low"] for k in kl]
    vols   = [k["volume"] for k in kl]
    opens  = [k.get("open", k["close"]) for k in kl]

    last, prevc = closes[-1], closes[-2]
    hi, lo, op = highs[-1], lows[-1], opens[-1]
    vol = vols[-1]
    prev_vol = vols[-2]
    rng = max(hi - lo, 1e-9)
    loc = (last - lo) / rng                       # 收盘在当日振幅的位置 0~1
    body = abs(last - op) / rng                   # 实体占比
    chg = (last / prevc - 1) * 100                # 当日涨跌%
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else sum(closes) / len(closes)
    avg20v = sum(vols[-20:]) / 20
    high20 = max(highs[-20:])
    low20 = min(lows[-20:])
    week52_high = snap.get("highest52weeks_price") or max(highs)
    dist_high = (1 - last / week52_high) * 100     # 距52周高 %（负=低于）
    drop_from_hi20 = (1 - last / high20) * 100      # 距近20日高 %
    vs_prev = vol / prev_vol if prev_vol else 0     # 当日量 ÷ 昨日量
    # 最近这波下跌的参考量：近 3~6 日里下跌日的均量
    recent_down_vols = [vols[-i] for i in range(2, 7) if len(closes) > i and closes[-i] < closes[-i-1]]
    down_ref = sum(recent_down_vols) / len(recent_down_vols) if recent_down_vols else avg20v

    notes = []

    # ---------- 信号 A：缩量最后一跌 ----------
    in_pullback = (last < ma10) and (drop_from_hi20 >= 8)
    is_down = (chg < 0) or (closes[-1] < closes[-3])
    shrink = (vol < 0.7 * avg20v) and (vol <= down_ref)
    stabilize = (loc >= 0.5) or (body < 0.4) or (last <= ma50 * 1.03) or (last <= low20 * 1.04)
    A_core = in_pullback and is_down and shrink
    A = A_core and stabilize
    if A_core:
        notes.append(f"[A核心] 回调中(现价<MA10且距20日高{drop_from_hi20:.1f}%)+下跌+缩量"
                     f"(量{vol:,.0f}={vol/avg20v:.2f}×20日均量, 跌势参考量{down_ref:,.0f})")
        notes.append(f"[A企稳] 收在振幅{loc*100:.0f}%处, 实体{body*100:.0f}%, "
                     f"{'近MA50/前低支撑' if (last<=ma50*1.03 or last<=low20*1.04) else '未到支撑'} -> {'企稳成立' if stabilize else '尚未企稳'}")

    # ---------- 信号 B：放量见顶/派发 + 放量异动走弱 ----------
    upper_wick = (hi - max(op, last)) / rng        # 上影占比
    red_body = last < op                            # 阴线（收<开）
    # 放量异动走弱（用户自定义）：反常大量 且 走弱（收跌 或 冲高回落）
    abnormal_vol = (vol >= 2.0 * prev_vol) or (vol >= 1.8 * avg20v)
    upthrust_reversal = (upper_wick >= 0.30) and (last <= op)   # 冲高回落：长上影且收于开盘下方
    vol_anomaly_weak = abnormal_vol and ((chg < 0) or upthrust_reversal)
    # 高位派发的几种形态（原有）
    near_high = (dist_high <= 5) or (drop_from_hi20 <= 3)
    tag_high = hi >= high20 * 0.99                   # 当日摸到/逼近近20日高
    upthrust = (vol >= 1.8 * avg20v) and (loc < 0.5) and tag_high               # 高位放量冲高回落（看收盘位置）
    wick_rejection = (vol >= 1.8 * avg20v) and tag_high and red_body and (upper_wick >= 0.35)  # 高位放量长上影阴线（看实体+上影）
    distribution = (chg < 0) and (vol >= 1.5 * avg20v) and (loc < 0.34)          # 放量下跌弱收
    breakdown = (last < ma10) and (chg < 0) and (vol >= 1.3 * avg20v) and (closes[-2] >= ma10)
    B = vol_anomaly_weak or (near_high and (upthrust or wick_rejection or distribution)) or breakdown
    if vol_anomaly_weak:
        trig = []
        if vol >= 2.0 * prev_vol:
            trig.append(f"量{vs_prev:.2f}×昨日(≥2×)")
        if vol >= 1.8 * avg20v:
            trig.append(f"量{vol/avg20v:.2f}×20日均量(≥1.8×)")
        weak_desc = f"收跌{chg:.2f}%" if chg < 0 else f"冲高回落(上影{upper_wick*100:.0f}%、收于开盘下方)"
        pos_desc = "高位" if near_high else f"距52周高{dist_high:.1f}%、回调途中"
        notes.append(f"[B-放量异动走弱] {' + '.join(trig)}，且{weak_desc}；{pos_desc}，收在振幅{loc*100:.0f}%处")
    if near_high and upthrust:
        notes.append(f"[B] 高位放量冲高回落: 量{vol/avg20v:.2f}×均量, 摸到{hi}(近20日高{high20}), 收在振幅{loc*100:.0f}%处")
    if near_high and wick_rejection:
        notes.append(f"[B] 高位放量长上影阴线: 量{vol/avg20v:.2f}×均量, 开{op:.0f}冲{hi:.0f}收{last:.0f}(阴), 上影占{upper_wick*100:.0f}%")
    if near_high and distribution:
        notes.append(f"[B] 高位放量下跌弱收: 跌{chg:.2f}%, 量{vol/avg20v:.2f}×均量, 收在振幅{loc*100:.0f}%处(<34%)")
    if breakdown:
        notes.append(f"[B] 放量跌破MA10: 跌{chg:.2f}%, 量{vol/avg20v:.2f}×均量, 昨日还在MA10上方")

    header = [
        f"ARM {kl[-1]['time_key'][:10]} 收 {last}  ({chg:+.2f}%)",
        f"距52周高 {dist_high:+.2f}% | 距20日高 {drop_from_hi20:+.2f}% | MA10 {ma10:.1f} MA20 {ma20:.1f} MA50 {ma50:.1f}",
        f"量 {vol:,.0f} = {vol/avg20v:.2f}× 20日均量 | 当日量 {vs_prev:.2f}× 昨日 | 收盘位置 {loc*100:.0f}% | 实体 {body*100:.0f}%",
    ]
    verdict = ("A" if A else "") + ("B" if B else "")
    return header, notes, (verdict if verdict else "NONE")

def main():
    path = sys.argv[1]
    try:
        kl, snap = load_kline(path)
    except Exception as e:
        print("读取失败：", e)
        print("VERDICT=ERROR")
        return
    if len(kl) < 25:
        print(f"K线不足（{len(kl)} 根，需≥25），无法判定。")
        print("VERDICT=ERROR")
        return

    header, notes, verdict = evaluate(kl, snap)
    for h in header:
        print(h)
    if notes:
        print("信号明细：")
        for n in notes:
            print("  " + n)
    else:
        print("无信号：既不满足缩量企稳，也不满足放量见顶/放量异动走弱。")
    print("VERDICT=" + verdict)

if __name__ == "__main__":
    main()
