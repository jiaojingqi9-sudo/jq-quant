#!/usr/bin/env python3
"""ledger_verify - 确认补 Epoch 字段之后账本数字一个没变、对账真的能跑。

要证明两件事：
  1. 5389 笔成交与净已实现 3,660.48 原封不动——补的是一个之前没人读到的字段，
     不该影响任何计算
  2. 券商对账从「被禁用」变成真的出结果——它本来就算得出来，只是界面卡在
     一个不相关的字段上不给看
"""
import json
import sys
from pathlib import Path

TRADE = Path.home() / "All here" / "trade"
sys.path.insert(0, str(TRADE / "src"))


def main() -> int:
    out = {"kind": "ledger_verify"}
    from taa_futu.config import load_settings
    from taa_futu.costs import build_stock_fills_ledger
    from taa_futu.stock_ledger import build_stock_double_entry_ledger, reconcile_stock_ledger
    from taa_futu.stock_runtime import (STOCK_FILLS_FILE, epoch_is_set,
                                        epoch_start_value, load_stock_ledger_epoch)

    epoch = load_stock_ledger_epoch()
    out["epoch"] = {
        "ts": epoch.get("ts"),
        "start_value": epoch_start_value(epoch),
        "is_set": epoch_is_set(epoch),
        "fills_count_at_reset": epoch.get("fills_count_at_reset"),
    }

    # 两个投影都接 epoch_path（不是 epoch 对象），内部自己读文件取切片偏移量
    from taa_futu.stock_runtime import STOCK_LEDGER_EPOCH_FILE
    simple = build_stock_fills_ledger(STOCK_FILLS_FILE, epoch_path=STOCK_LEDGER_EPOCH_FILE)
    journal = build_stock_double_entry_ledger(STOCK_FILLS_FILE,
                                              epoch_path=STOCK_LEDGER_EPOCH_FILE)
    out["ledgers"] = {
        "simple_trade_count": int(getattr(simple, "trade_count", 0)),
        "simple_net_realized": round(float(getattr(simple, "net_realized_pnl", 0.0)), 2),
        "simple_fees": round(float(getattr(simple, "fees_paid", 0.0)), 2),
        "journal_entries": len(getattr(journal, "entries", []) or []),
        "journal_net_realized": round(float(getattr(journal, "net_realized_pnl", 0.0)), 2),
        "journal_chain_valid": bool(getattr(journal, "chain_valid", False)),
    }

    # Doctor 现在怎么说（只读）
    try:
        from taa_futu.stock_doctor import run_stock_system_doctor
        settings = load_settings()
        report = run_stock_system_doctor(settings)
        out["doctor_status"] = report.status
        out["doctor_epoch_findings"] = [
            {"id": f.check_id, "status": f.status, "msg": f.summary[:90]}
            for f in report.findings if "epoch" in f.check_id.lower()
        ]
    except Exception as exc:
        out["doctor_error"] = f"{type(exc).__name__}: {str(exc)[:140]}"

    # 券商对账：需要连 OpenD 拿实时持仓
    try:
        from taa_futu.futu_gateway import FutuPaperTrader
        settings = load_settings()
        with FutuPaperTrader(settings) as trader:
            acc = trader.resolve_trade_account()
            account = trader.get_account_info(acc)
            positions = trader.get_positions(acc)
        recon = reconcile_stock_ledger(journal, positions=positions,
                                       account=account, epoch=epoch)
        out["reconciliation"] = {
            "ok": bool(getattr(recon, "ok", False)),
            "break_count": len(getattr(recon, "breaks", []) or []),
            "breaks": [
                {"kind": b.kind, "symbol": b.symbol,
                 "expected": round(float(b.expected), 2),
                 "actual": round(float(b.actual), 2),
                 "diff": round(float(b.difference), 2)}
                for b in (getattr(recon, "breaks", []) or [])[:8]
            ],
        }
    except Exception as exc:
        out["reconciliation_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
