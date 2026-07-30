#!/usr/bin/env python3
"""
综合实时快照脚本 - 用公开 futu-api 一次拿全所需数据

用法:
  python snapshot.py US.TSLA --json
  python snapshot.py US.TSLA --json --kline-days 30

输出 JSON 包含:
  snapshot:              实时行情快照(价格/成交量/市值等)
  kline:                 历史日 K(用来算指标)
  capital_flow_intraday: 当日资金流(分钟级)
  capital_flow_daily:    最近多日资金流
  capital_distribution:  大中小单分布
  option_expirations:    期权到期日列表
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
    p.add_argument("stock_symbol")
    p.add_argument("--time-range", type=int, default=7,
                   help="参数兼容：等同 --kline-days")
    p.add_argument("--kline-days", type=int, default=None,
                   help="历史 K 线天数，默认 30")
    p.add_argument("--host", default=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int,
                   default=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    p.add_argument("--language-id", type=int, default=0)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    kline_days = args.kline_days if args.kline_days is not None else max(args.time_range, 30)

    from futu import (
        OpenQuoteContext, RET_OK, KLType, AuType, PeriodType,
    )
    import futu

    symbol = args.stock_symbol
    out = {
        "symbol": symbol,
        "ts": datetime.now().isoformat(),
        "futu_api_version": getattr(futu, "__version__", "unknown"),
    }

    ctx = OpenQuoteContext(host=args.host, port=args.port)
    try:
        # 1. 实时快照
        try:
            ret, data = ctx.get_market_snapshot([symbol])
            if ret == RET_OK:
                out["snapshot"] = _to_records(data)
            else:
                out["snapshot_error"] = str(data)
        except Exception as e:
            out["snapshot_error"] = f"{type(e).__name__}: {e}"

        # 2. 历史 K 线
        try:
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=kline_days + 10)).strftime("%Y-%m-%d")
            ret, data, _ = ctx.request_history_kline(
                symbol, start=start, end=end,
                ktype=KLType.K_DAY, autype=AuType.QFQ,
                max_count=kline_days + 10,
            )
            if ret == RET_OK:
                recs = _to_records(data)
                # 只保留近 kline_days 根
                if isinstance(recs, list):
                    recs = recs[-kline_days:]
                out["kline"] = recs
            else:
                out["kline_error"] = str(data)
        except Exception as e:
            out["kline_error"] = f"{type(e).__name__}: {e}"

        # 3. 资金流(日线，近 N 天)
        try:
            ret, data = ctx.get_capital_flow(
                symbol, period_type=PeriodType.DAY,
                start=(datetime.now() - timedelta(days=kline_days)).strftime("%Y-%m-%d"),
                end=datetime.now().strftime("%Y-%m-%d"),
            )
            if ret == RET_OK:
                out["capital_flow_daily"] = _to_records(data)
            else:
                out["capital_flow_daily_error"] = str(data)
        except Exception as e:
            out["capital_flow_daily_error"] = f"{type(e).__name__}: {e}"

        # 4. 当日分钟级资金流
        try:
            ret, data = ctx.get_capital_flow(symbol, period_type=PeriodType.INTRADAY)
            if ret == RET_OK:
                recs = _to_records(data)
                # intraday 可能很长，只取最后 30 个点（约 30 分钟）
                if isinstance(recs, list):
                    recs = recs[-30:]
                out["capital_flow_intraday"] = recs
            else:
                out["capital_flow_intraday_error"] = str(data)
        except Exception as e:
            out["capital_flow_intraday_error"] = f"{type(e).__name__}: {e}"

        # 5. 大中小单分布
        try:
            ret, data = ctx.get_capital_distribution(symbol)
            if ret == RET_OK:
                out["capital_distribution"] = _to_records(data)
            else:
                out["capital_distribution_error"] = str(data)
        except Exception as e:
            out["capital_distribution_error"] = f"{type(e).__name__}: {e}"

        # 6. 期权到期日
        try:
            ret, data = ctx.get_option_expiration_date(symbol)
            if ret == RET_OK:
                out["option_expirations"] = _to_records(data)
            else:
                out["option_expirations_error"] = str(data)
        except Exception as e:
            out["option_expirations_error"] = f"{type(e).__name__}: {e}"

    finally:
        try:
            ctx.close()
        except Exception:
            pass

    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception as e:
        err = {"error": f"{type(e).__name__}: {e}",
               "traceback": traceback.format_exc()}
        print(json.dumps(err, ensure_ascii=False))
        sys.exit(1)
