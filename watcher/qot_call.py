#!/usr/bin/env python3
"""通用行情接口透传 skill。

任务格式：
  {"skill":"qot", "api":"<OpenQuoteContext 方法名>", "kwargs":{...}, "limit":50}

特殊 api：
  __list__   列出当前 SDK 支持的全部行情方法
  __probe__  批量探测接口是否存在，kwargs.names 传方法名列表
  __ver__    只报版本与 OpenD 状态

安全设计：只绑定 OpenQuoteContext（纯行情，无下单能力），
不挂 OpenSecTradeContext，禁止调用下划线开头的私有方法。
"""
import argparse
import json
import sys
import traceback


def jsonable(x):
    """DataFrame / Series -> 原生结构"""
    if hasattr(x, "to_dict"):
        try:
            return x.to_dict(orient="records")
        except TypeError:
            return x.to_dict()
    return x


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--task-json", required=True)
    args = p.parse_args()

    task = json.loads(args.task_json)
    api = task.get("api", "")
    kwargs = task.get("kwargs") or {}
    limit = int(task.get("limit", 50))

    import futu

    out = {"sdk_version": getattr(futu, "__version__", "unknown"), "api": api}
    QC = futu.OpenQuoteContext

    # ---- 不需要连接 OpenD 的元操作 ----
    if api == "__list__":
        ms = sorted(m for m in dir(QC)
                    if not m.startswith("_") and callable(getattr(QC, m, None)))
        out["methods"] = ms
        out["count"] = len(ms)
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0

    if api == "__probe__":
        names = kwargs.get("names") or []
        out["found"] = [n for n in names if hasattr(QC, n)]
        out["missing"] = [n for n in names if not hasattr(QC, n)]
        out["found_count"] = len(out["found"])
        out["missing_count"] = len(out["missing"])
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0

    if api == "__enums__":
        # 倒出条件选股的因子/枚举表；kwargs.keyword 可过滤成员名
        import futu.quote.stock_screen_const as SC
        kw = (kwargs.get("keyword") or "").upper()
        enums = {}
        for cname in dir(SC):
            if cname.startswith("_"):
                continue
            c = getattr(SC, cname)
            if not isinstance(c, type):
                continue
            members = {}
            for mn in dir(c):
                if mn.startswith("_"):
                    continue
                if kw and kw not in mn.upper():
                    continue
                mv = getattr(c, mn)
                try:
                    members[mn] = int(mv)
                except (TypeError, ValueError):
                    if isinstance(mv, str):
                        members[mn] = mv
            if members:
                enums[cname] = members
        out["enums"] = enums
        out["class_count"] = len(enums)
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0

    # ---- 参数校验 ----
    if api and api not in ("__ver__", "__screen__"):
        if api.startswith("_"):
            out["err"] = "不允许调用私有方法"
            print(json.dumps(out, ensure_ascii=False))
            return 1
        if not hasattr(QC, api):
            out["err"] = f"当前 SDK ({out['sdk_version']}) 没有这个接口: {api}"
            out["hint"] = '用 {"skill":"qot","api":"__list__"} 看支持列表'
            print(json.dumps(out, ensure_ascii=False))
            return 1
    elif not api:
        out["err"] = "缺少 'api' 字段"
        print(json.dumps(out, ensure_ascii=False))
        return 1

    # ---- 连接 OpenD ----
    ctx = QC(host="127.0.0.1", port=11111)
    try:
        # 顺手带上 OpenD 状态，便于排查版本错配
        try:
            r, gs = ctx.get_global_state()
            if r == futu.RET_OK and isinstance(gs, dict):
                out["opend"] = {k: gs.get(k) for k in
                                ("server_ver", "program_status_type",
                                 "qot_logined", "trd_logined") if k in gs}
        except Exception as e:
            out["opend_err"] = f"{type(e).__name__}: {e}"

        if api == "__ver__":
            print(json.dumps(out, ensure_ascii=False, default=str))
            return 0

        if api == "__book__":
            # 同一连接内 订阅摆盘 -> 查询，kwargs: {"code":"US.AAPL","num":40}
            code = kwargs.get("code", "US.AAPL")
            num = int(kwargs.get("num", 40))
            import time as _t
            r0, e0 = ctx.subscribe([code], [futu.SubType.ORDER_BOOK],
                                   subscribe_push=False)
            if r0 != futu.RET_OK:
                out["err"] = f"subscribe: {str(e0)[:300]}"
                print(json.dumps(out, ensure_ascii=False))
                return 1
            _t.sleep(2)
            r1, book = ctx.get_order_book(code, num=num)
            if r1 != futu.RET_OK:
                out["err"] = str(book)[:300]
            else:
                bid = book.get("Bid") or []
                ask = book.get("Ask") or []
                out["ret_ok"] = True
                out["bid_levels"] = len(bid)
                out["ask_levels"] = len(ask)
                out["bid_top5"] = bid[:5]
                out["ask_top5"] = ask[:5]
                out["book_keys"] = sorted(book.keys())
            print(json.dumps(out, ensure_ascii=False, default=str))
            return 0

        if api == "__screen__":
            # 条件选股 V2：kwargs.build = [[builder方法名, {参数}], ...]
            # 枚举用 "类名.成员" 字符串表示，如 "ScrMarket.US"、"SimpleProperty.PRICE"
            import futu.quote.stock_screen_const as SC

            def _resolve(v):
                if isinstance(v, str) and "." in v:
                    cls, _, name = v.partition(".")
                    c = getattr(SC, cls, None)
                    if c is not None and hasattr(c, name):
                        return getattr(c, name)
                if isinstance(v, list):
                    return [_resolve(x) for x in v]
                if isinstance(v, dict):
                    return {k: _resolve(x) for k, x in v.items()}
                return v

            req = futu.StockScreenRequest()
            for call in (kwargs.get("build") or []):
                mname = call[0]
                mkw = _resolve(call[1] if len(call) > 1 else {})
                if mname.startswith("_") or not hasattr(req, mname):
                    out["err"] = f"StockScreenRequest 没有方法: {mname}"
                    print(json.dumps(out, ensure_ascii=False))
                    return 1
                if mname in ("set_sort", "add_sort") and isinstance(mkw.get("property_params"), dict):
                    mkw["property_params"] = {
                        k: (v if isinstance(v, str) else int(v))
                        for k, v in mkw["property_params"].items()}
                getattr(req, mname)(**mkw)
            if "page_count" in kwargs:
                req.page_count = int(kwargs["page_count"])
            if "page_from" in kwargs:
                req.page_from = int(kwargs["page_from"])

            code, data = ctx.get_stock_screen(req)
            if code != futu.RET_OK:
                out["ret_ok"] = False
                out["err"] = str(data)[:800]
            else:
                last_page, all_count, items = data
                out["ret_ok"] = True
                out["all_count"] = all_count
                out["last_page"] = last_page
                out["row_count"] = len(items)
                out["truncated"] = len(items) > limit
                out["data"] = items[:limit]
            print(json.dumps(out, ensure_ascii=False, default=str))
            return 0

        ret = getattr(ctx, api)(**kwargs)

        if isinstance(ret, tuple):
            code = ret[0]
            data = ret[1] if len(ret) > 1 else None
            ok = (code == futu.RET_OK)
            out["ret_ok"] = ok
            if not ok:
                out["err"] = str(data)[:800]
            else:
                d = jsonable(data)
                if isinstance(d, list):
                    out["row_count"] = len(d)
                    out["truncated"] = len(d) > limit
                    out["data"] = d[:limit]
                else:
                    out["data"] = d
            if len(ret) > 2:
                out["extra"] = str(ret[2])[:300]
        else:
            out["data"] = jsonable(ret)

        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0
    finally:
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception as e:
        print(json.dumps({"err": f"{type(e).__name__}: {e}",
                          "tb": traceback.format_exc()}, ensure_ascii=False))
        sys.exit(1)
