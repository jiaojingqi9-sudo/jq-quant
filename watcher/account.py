#!/usr/bin/env python3
"""
富途账户只读查询脚本（不下单，不解锁交易密码）

用法:
  python account.py accounts  --json     # 列账户
  python account.py positions --json     # 查持仓
  python account.py cash      --json     # 查现金/购买力
  python account.py orders    --json     # 查当前挂单
  python account.py history   --json --days 180   # 查历史成交
  python account.py all       --json     # 一把抓
"""
import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timedelta


def _to_records(obj):
    if obj is None:
        return None
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict(orient="records")
        except TypeError:
            return obj.to_dict()
    return obj


def main():
    p = argparse.ArgumentParser()
    p.add_argument("query_type", choices=["accounts","positions","cash","orders","history","all"])
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--trd-env", default="REAL", choices=["REAL","SIMULATE"])
    p.add_argument("--market", default="US", choices=["US","HK","CN"])
    p.add_argument("--host", default=os.getenv("FUTU_OPEND_HOST","127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("FUTU_OPEND_PORT","11111")))
    p.add_argument("--language-id", type=int, default=0)
    p.add_argument("--time-range", type=int, default=180)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    # 如果用户传 time-range 又没传 days, 用 time-range
    if args.days == 180 and args.time_range != 180:
        args.days = args.time_range

    from futu import (
        OpenSecTradeContext, RET_OK, TrdMarket, SecurityFirm, TrdEnv,
    )
    import futu

    trd_env = TrdEnv.REAL if args.trd_env == "REAL" else TrdEnv.SIMULATE
    market_map = {"US": TrdMarket.US, "HK": TrdMarket.HK, "CN": TrdMarket.CN}
    market = market_map[args.market]

    # 扫所有 (firm × market) 组合，把所有账户合并起来
    sf_candidates = ["FUTUINC", "FUTUSECURITIES", "FUTUSG", "FUTUAU", "FUTUJP"]
    if args.market == "ALL":
        market_candidates = [TrdMarket.US, TrdMarket.HK, TrdMarket.CN_SH, TrdMarket.CN_SZ] if hasattr(TrdMarket, "CN_SH") else [TrdMarket.US, TrdMarket.HK]
        for m in ["JP", "SG", "AU"]:
            mv = getattr(TrdMarket, m, None)
            if mv:
                market_candidates.append(mv)
    else:
        market_candidates = [market]

    out = {
        "ts": datetime.now().isoformat(),
        "query_type": args.query_type,
        "market": args.market,
        "trd_env": args.trd_env,
        "futu_api_version": getattr(futu, "__version__", "unknown"),
    }

    all_accounts = []
    scan_log = []
    ctx = None
    last_err = None

    for sf_name in sf_candidates:
        sf_val = getattr(SecurityFirm, sf_name, None)
        if sf_val is None:
            continue
        for m_val in market_candidates:
            try:
                test_ctx = OpenSecTradeContext(
                    filter_trdmarket=m_val, host=args.host, port=args.port,
                    security_firm=sf_val, is_encrypt=None,
                )
                ret, data = test_ctx.get_acc_list()
                if ret == RET_OK and data is not None:
                    recs = _to_records(data)
                    if recs:
                        for r in recs:
                            r["_security_firm"] = sf_name
                            r["_filter_market"] = str(m_val)
                            # 去重：相同 acc_id 只保留一次
                            if not any(x.get("acc_id") == r.get("acc_id") for x in all_accounts):
                                all_accounts.append(r)
                        scan_log.append(f"{sf_name}/{m_val}: {len(recs)} acc")
                        if ctx is None:
                            ctx = test_ctx
                            continue  # 留着用
                    else:
                        scan_log.append(f"{sf_name}/{m_val}: 0 acc")
                else:
                    last_err = f"{sf_name}/{m_val}: {data}"
                    scan_log.append(f"{sf_name}/{m_val}: ERR {data}")
                test_ctx.close()
            except Exception as e:
                last_err = f"{sf_name}/{m_val}: {type(e).__name__}: {e}"
                scan_log.append(f"{sf_name}/{m_val}: EXC {e}")
                try:
                    test_ctx.close()
                except Exception:
                    pass
                continue

    out["scan_log"] = scan_log
    accounts = all_accounts
    sf_used = "MULTI"
    # 关闭旧的 ctx
    if ctx is not None:
        try: ctx.close()
        except: pass
        ctx = None

    if not accounts:
        out["error"] = f"扫描完毕未找到任何账户。最后错误: {last_err}"
        out["hint"] = "确认 OpenD 已登录交易账户"
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 1

    out["security_firm_used"] = sf_used
    out["accounts"] = accounts

    if args.query_type == "accounts":
        try: ctx.close()
        except: pass
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0

    # 挑账户：优先匹配请求的 trd_env，没匹配上就用第一个可用账户
    target = None
    for a in accounts:
        if a.get("trd_env") == args.trd_env:
            target = a
            break
    if target is None and accounts:
        target = accounts[0]
        # 强制用 target 实际的 trd_env，避免错配
        from futu import TrdEnv
        actual_env = target.get("trd_env")
        if actual_env == "SIMULATE":
            trd_env = TrdEnv.SIMULATE
        elif actual_env == "REAL":
            trd_env = TrdEnv.REAL

    if target is None:
        out["error"] = "没找到合适的账户"
        try: ctx.close()
        except: pass
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 1

    acc_id = target["acc_id"]
    out["selected_account"] = {
        "acc_id": acc_id,
        "acc_type": target.get("acc_type"),
        "trd_env": target.get("trd_env"),
        "trdmarket_auth": target.get("trdmarket_auth"),
        "security_firm": target.get("_security_firm"),
    }

    # 按 target 的 firm 重建 ctx
    target_sf_name = target.get("_security_firm", "FUTUINC")
    target_sf = getattr(SecurityFirm, target_sf_name, None)
    if target_sf is None:
        out["error"] = f"未知 SecurityFirm: {target_sf_name}"
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 1
    # 选个 market：用账户授权里的第一个
    auth = target.get("trdmarket_auth") or ["US"]
    first_market = auth[0] if isinstance(auth, list) else str(auth).split(",")[0]
    market_for_ctx = getattr(TrdMarket, first_market, TrdMarket.US)
    try:
        ctx = OpenSecTradeContext(
            filter_trdmarket=market_for_ctx, host=args.host, port=args.port,
            security_firm=target_sf, is_encrypt=None,
        )
    except Exception as e:
        out["error"] = f"重建 ctx 失败: {e}"
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 1

    def safe_call(label, fn):
        try:
            ret, data = fn()
            if ret == RET_OK:
                out[label] = _to_records(data)
            else:
                out[label + "_error"] = str(data)
        except Exception as e:
            out[label + "_error"] = f"{type(e).__name__}: {e}"

    if args.query_type in ("positions", "all"):
        safe_call("positions",
                  lambda: ctx.position_list_query(trd_env=trd_env, acc_id=acc_id))

    if args.query_type in ("cash", "all"):
        safe_call("account_info",
                  lambda: ctx.accinfo_query(trd_env=trd_env, acc_id=acc_id))

    if args.query_type in ("orders", "all"):
        safe_call("open_orders",
                  lambda: ctx.order_list_query(trd_env=trd_env, acc_id=acc_id))

    if args.query_type in ("history", "all"):
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
        out["history_window"] = f"{start} ~ {end}"
        safe_call("history_orders",
                  lambda: ctx.history_order_list_query(
                      start=start, end=end, trd_env=trd_env, acc_id=acc_id))
        safe_call("history_deals",
                  lambda: ctx.history_deal_list_query(
                      start=start, end=end, trd_env=trd_env, acc_id=acc_id))

    try: ctx.close()
    except: pass

    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception as e:
        err = {"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
        print(json.dumps(err, ensure_ascii=False))
        sys.exit(1)
