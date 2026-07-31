#!/usr/bin/env python3
"""fix_epoch - 给股票账本 Epoch 补上缺失的 total_assets 字段。

背景：2026-06-02 跑 stock/tools/repair_ledger.py 重建账本时，那个脚本手工拼
epoch dict、绕过了 write_stock_ledger_epoch()，只写了 cash 没写 total_assets。
后果是界面判定「Epoch 未设置」，起点资产显示未设置、期间盈亏待初始化、券商
对账被整块禁用——而对账算法其实只需要 cash，一直算得出结果，只是不给看。

补哪个值：epoch 的 account_snapshot 里 positions 是空的，没有持仓时总资产
恒等于现金，所以 total_assets = cash = 校准过的那个开仓现金。这不是估计。

只加字段，不改任何已有值，不碰成交流水。改前备份。
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

EPOCH = Path.home() / "All here" / "trade" / "runtime" / "stock_ledger_epoch.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args, _ = ap.parse_known_args()

    out = {"kind": "fix_epoch", "applied": args.apply, "path": str(EPOCH)}
    if not EPOCH.exists():
        out["error"] = "epoch 文件不存在"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    epoch = json.loads(EPOCH.read_text(encoding="utf-8"))
    snap = epoch.get("account_snapshot") or {}
    out["before"] = {"ts": epoch.get("ts"),
                     "snapshot_keys": sorted(snap.keys()),
                     "fills_count_at_reset": epoch.get("fills_count_at_reset")}

    if snap.get("total_assets"):
        out["result"] = "已经有 total_assets，无需修改"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    positions = snap.get("positions")
    if not isinstance(positions, list) or positions:
        out["error"] = ("positions 不是空列表，不能用 cash 推总资产。"
                        "需要人工确认那一刻的持仓市值。")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    cash = snap.get("cash")
    try:
        cash = float(cash)
    except (TypeError, ValueError):
        out["error"] = f"cash 不是数字：{cash!r}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1
    if cash <= 0:
        out["error"] = f"cash 不是正数：{cash}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    out["will_set"] = {"total_assets": cash, "market_val": 0.0, "position_count": 0}
    if not args.apply:
        out["note"] = "这是计划，没有改文件。加 --apply 才写入。"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    backup = EPOCH.with_name(f"stock_ledger_epoch.backup_{time.strftime('%Y%m%d_%H%M%S')}.json")
    shutil.copy2(EPOCH, backup)
    out["backup"] = backup.name

    snap["total_assets"] = cash
    snap.setdefault("market_val", 0.0)
    snap.setdefault("position_count", 0)
    epoch["account_snapshot"] = snap
    epoch["schema_fix_note"] = (
        "2026-07-31 补写 total_assets。原文件由旧版 repair_ledger.py 生成，"
        "该脚本绕过 write_stock_ledger_epoch() 漏写了此字段，导致界面判定 Epoch 未设置、"
        "券商对账被禁用。positions 为空故 total_assets = cash，非估算值。"
    )

    tmp = EPOCH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(epoch, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(EPOCH)

    check = json.loads(EPOCH.read_text(encoding="utf-8"))
    csnap = check.get("account_snapshot") or {}
    out["after"] = {"ts": check.get("ts"),
                    "total_assets": csnap.get("total_assets"),
                    "cash": csnap.get("cash"),
                    "fills_count_at_reset": check.get("fills_count_at_reset")}
    # 这两个值绝不能被动到
    out["fills_offset_unchanged"] = (check.get("fills_count_at_reset")
                                     == out["before"]["fills_count_at_reset"])
    out["cash_unchanged"] = csnap.get("cash") == cash

    sys.path.insert(0, str(Path.home() / "All here" / "trade" / "src"))
    try:
        from taa_futu.stock_runtime import epoch_is_set, epoch_start_value
        out["epoch_is_set_now"] = epoch_is_set(check)
        out["epoch_start_value_now"] = epoch_start_value(check)
    except Exception as exc:
        out["verify_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
