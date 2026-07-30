#!/usr/bin/env python3
"""缩量上涨·接近高位 两段式筛选 (skill: svh)

Stage 1  get_stock_screen 粗筛（宽口径 + 流动性双门槛，挡 OTC 死票）
Stage 2  subscribe + get_cur_kline 拉日K逐只验证（不吃历史K线配额）：
         - 接近高位: 现价距 high_window 日最高价 <= near_pct
         - 上涨:     5 日涨幅 >= chg5_min
         - 整段缩量: 近5日中 后2日均量/前2日均量 <= shrink_ratio
                     且 5日均量 <= vol5_vs20_max × 前20日均量

任务格式（全部参数可省略，走默认）：
  {"skill":"svh",
   "mktcap_min":2e9, "near_pct":0.05, "chg5_min":0.02,
   "shrink_ratio":0.9, "vol5_vs20_max":1.0,
   "avg_vol_min":500000, "avg_turnover_min":1e7,
   "cap":60, "high_window":250}
"""
import argparse
import json
import sys
import time
import traceback


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--task-json", required=True)
    args = p.parse_args()
    task = json.loads(args.task_json)

    P = {
        "mktcap_min": float(task.get("mktcap_min", 2e9)),
        "near_pct": float(task.get("near_pct", 0.05)),        # 距高位容差
        "near_pct_s1": float(task.get("near_pct_s1", 0.08)),  # 一段放宽口径
        "chg5_min": float(task.get("chg5_min", 0.02)),
        "vr_max_s1": float(task.get("vr_max_s1", 1.1)),
        "vr_min_s1": float(task.get("vr_min_s1", 0.15)),  # 盘前跑请置 0(量比无值)
        "avg_vol_min": float(task.get("avg_vol_min", 5e5)),
        "avg_turnover_min": float(task.get("avg_turnover_min", 1e7)),
        "cap": int(task.get("cap", 60)),
        "high_window": int(task.get("high_window", 250)),
        "shrink_ratio": float(task.get("shrink_ratio", 0.9)),
        "vol5_vs20_max": float(task.get("vol5_vs20_max", 1.0)),
        "min_bars": int(task.get("min_bars", 60)),
        # 盘中运行时置 true：剔除当日未完成K线（量极小会把"缩量"全判错），
        # 按最近一根完整日K（如上周五）口径判定
        "exclude_today": bool(task.get("exclude_today", False)),
    }

    import futu
    from futu import (OpenQuoteContext, RET_OK, StockScreenRequest,
                      SubType, KLType)
    from futu.quote.stock_screen_const import (
        ScrMarket, ScrSortDir, SimpleField, SimpleProperty,
        CumulativeProperty, BasicProperty)

    out = {"sdk": getattr(futu, "__version__", "?"), "params": P}
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        # ---------- Stage 1 粗筛 ----------
        req = StockScreenRequest()
        req.add_simple_field(field=SimpleField.MARKET, values=[ScrMarket.US])
        req.add_simple_property(name=SimpleProperty.MARKET_CAP,
                                lower=P["mktcap_min"])
        req.add_simple_property(name=SimpleProperty.PRICE_TO_52W_HIGH,
                                lower=-P["near_pct_s1"])
        req.add_simple_property(name=SimpleProperty.VOLUME_RATIO,
                                lower=P["vr_min_s1"], upper=P["vr_max_s1"])
        req.add_cumulative_property(name=CumulativeProperty.PRICE_CHANGE_PCT,
                                    days=5, lower=P["chg5_min"])
        req.add_cumulative_property(name=CumulativeProperty.AVG_VOLUME,
                                    days=5, lower=P["avg_vol_min"])
        req.add_cumulative_property(name=CumulativeProperty.AVG_TURNOVER,
                                    days=5, lower=P["avg_turnover_min"])
        req.add_retrieve_basic(name=BasicProperty.CODE)
        req.add_retrieve_basic(name=BasicProperty.NAME)
        req.add_retrieve_simple(name=SimpleProperty.PRICE)
        req.add_retrieve_simple(name=SimpleProperty.MARKET_CAP)
        req.set_sort(direction=ScrSortDir.DESC, property_type="simple",
                     property_params={"name": int(SimpleProperty.PRICE_TO_52W_HIGH)})
        req.page_count = min(P["cap"], 200)

        ret, data = ctx.get_stock_screen(req)
        if ret != RET_OK:
            out["err"] = f"stage1: {str(data)[:400]}"
            print(json.dumps(out, ensure_ascii=False))
            return 1
        _, all_count, items = data
        out["stage1_total"] = all_count

        CODE, NAME = int(BasicProperty.CODE), int(BasicProperty.NAME)
        PRICE = int(SimpleProperty.PRICE)
        MCAP = int(SimpleProperty.MARKET_CAP)
        cands = []
        for it in items:
            row = {}
            for res in it.get("results", []):
                n = res.get("property", {}).get("name")
                v = res.get("sval", res.get("dval", res.get("ival")))
                if n == CODE:
                    row["code"] = v
                elif n == NAME:
                    row["name"] = v
                elif n == PRICE:
                    row["price"] = v
                elif n == MCAP:
                    row["mktcap"] = v
            if row.get("code"):
                cands.append(row)
        cands = cands[:P["cap"]]
        out["stage1_taken"] = len(cands)

        # ---------- Stage 2 日K验证 ----------
        symbols = ["US." + c["code"] for c in cands]
        by_sym = {("US." + c["code"]): c for c in cands}
        passed, skipped = [], []

        # 订阅是整批失败语义：一只 OTC 无行情会拖死全批。
        # 策略：失败时从错误文本里解析出问题票，剔除后重试。
        # 注意：额度 100，关连接后约 1 分钟释放 -> 两次运行至少间隔 1 分钟。
        if symbols:
            pending = list(symbols)
            for _ in range(30):
                if not pending:
                    break
                ret, err = ctx.subscribe(pending, [SubType.K_DAY],
                                         subscribe_push=False)
                if ret == RET_OK:
                    break
                es = str(err)
                if ("频率" in es) or ("frequency" in es.lower()):
                    time.sleep(11)
                    continue
                bad = None
                for s in pending:
                    if s.split(".", 1)[-1] in es:
                        bad = s
                        break
                if bad is None:
                    out["err"] = f"subscribe: {es[:400]}"
                    print(json.dumps(out, ensure_ascii=False))
                    return 1
                pending.remove(bad)
                skipped.append({"code": bad, "reason": "无行情权限(OTC?)"})
                time.sleep(0.3)
            symbols = pending
            if symbols:
                time.sleep(3)  # 等 OpenD 灌历史bar

        need = max(P["min_bars"], 26)
        for sym in symbols:
            recs = None
            for attempt in (1, 2):
                r, kl = ctx.get_cur_kline(sym, min(P["high_window"] + 10, 1000),
                                          KLType.K_DAY)
                if r == RET_OK and hasattr(kl, "to_dict"):
                    recs = kl.to_dict(orient="records")
                    if len(recs) >= need:
                        break
                time.sleep(1)
            if recs and P["exclude_today"]:
                today = time.strftime("%Y-%m-%d")
                if str(recs[-1].get("time_key", "")).startswith(today):
                    recs = recs[:-1]
            if not recs or len(recs) < need:
                skipped.append({"code": sym, "reason":
                                f"bars={0 if not recs else len(recs)}<{need}"})
                continue

            closes = [x["close"] for x in recs]
            highs = [x["high"] for x in recs]
            vols = [x["volume"] for x in recs]
            last = closes[-1]

            win = min(P["high_window"], len(recs))
            hi = max(highs[-win:])
            dist = last / hi - 1 if hi else -9
            chg5 = last / closes[-6] - 1 if len(closes) >= 6 else -9
            v5 = vols[-5:]
            back = vols[-25:-5] if len(vols) >= 25 else vols[:-5]
            base20 = sum(back) / len(back) if back else 0
            shrink = (sum(v5[-2:]) / 2) / (sum(v5[:2]) / 2) if sum(v5[:2]) else 9
            v5_vs20 = (sum(v5) / 5) / base20 if base20 else 9

            checks = {
                "near": dist >= -P["near_pct"],
                "up": chg5 >= P["chg5_min"],
                "shrink": shrink <= P["shrink_ratio"],
                "quiet": v5_vs20 <= P["vol5_vs20_max"],
            }
            # 加分项（不作硬门槛，沿用 choose stock 策略的打分思路）
            ma5 = sum(closes[-5:]) / 5
            ma10 = sum(closes[-10:]) / 10
            ma20 = sum(closes[-20:]) / 20
            ma_bull = (ma5 > ma10 > ma20) and last > ma20   # 均线多头
            trend20 = len(closes) >= 21 and last > closes[-21]
            w60 = recs[-60:]
            mv = max(w60, key=lambda x: x["volume"])
            lo60 = min(x["low"] for x in w60)
            hi60 = max(x["high"] for x in w60)
            lowpos = ((mv["close"] - lo60) <= 0.4 * (hi60 - lo60)
                      if hi60 > lo60 else False)            # 低位放量(吸筹)
            score = sum([checks["near"], checks["up"],
                         checks["shrink"] and checks["quiet"],
                         ma_bull, lowpos])
            info = by_sym.get(sym, {})
            rec = {"code": sym, "name": info.get("name"),
                   "price": round(last, 2),
                   "mktcap_b": round((info.get("mktcap") or 0) / 1e9, 1),
                   "dist_high": round(dist, 4),
                   "chg5": round(chg5, 4),
                   "shrink": round(shrink, 2),
                   "v5_vs20": round(v5_vs20, 2),
                   "ma_bull": ma_bull, "trend20": trend20,
                   "lowpos_maxvol": lowpos, "score": score,
                   "win": win}
            if all(checks.values()):
                passed.append(rec)
            else:
                rec["fail"] = [k for k, v in checks.items() if not v]
                skipped.append(rec)

        passed.sort(key=lambda x: (-x["score"], -x["dist_high"]))
        out["validated"] = len(symbols) - sum(
            1 for s in skipped if "bars=" in str(s.get("reason", "")))
        out["passed_count"] = len(passed)
        out["passed"] = passed
        out["rejected_sample"] = [s for s in skipped if s.get("fail")][:12]
        out["nodata_sample"] = [s for s in skipped if s.get("reason")][:8]
        out["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0
    finally:
        try:
            ctx.close()   # 连接关闭即释放全部订阅
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception as e:
        print(json.dumps({"err": f"{type(e).__name__}: {e}",
                          "tb": traceback.format_exc()}, ensure_ascii=False))
        sys.exit(1)
