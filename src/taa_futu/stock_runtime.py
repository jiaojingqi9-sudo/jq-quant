from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime"
STOCK_FILLS_FILE = RUNTIME_DIR / "stock_fills.jsonl"
STOCK_LEDGER_EPOCH_FILE = RUNTIME_DIR / "stock_ledger_epoch.json"
STOCK_JOURNAL_FILE = RUNTIME_DIR / "stock_journal.jsonl"


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def append_stock_fill(record: dict[str, Any], *, fills_path: Path = STOCK_FILLS_FILE) -> None:
    fills_path.parent.mkdir(parents=True, exist_ok=True)
    with fills_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")


def load_stock_fill_records(fills_path: Path = STOCK_FILLS_FILE) -> list[dict[str, Any]]:
    if not fills_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = fills_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)

    # 按成交时间稳定排序后再返回。
    #
    # 账本是按列表顺序回放做 FIFO 配对的，而这个文件是追加写的，正常情况下
    # 文件顺序就是时间顺序，排序等于空操作（stable sort 保持原有相对次序）。
    # 但补记历史成交时，那条记录只能追加到文件末尾——如果它的时间早于已有
    # 记录，回放就会先卖后买：2026-07-31 补记 07-24 的 AAPL 买入 116 股后，
    # 对应的卖出排在它前面，账本于是认为还持有 116 股，而券商已清零。
    #
    # 只在乱序时才改变行为，所以对既有数据无影响。ts 缺失的排到最前，
    # 保持它们原有的相对次序。
    def _sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, str]:
        index, record = item
        stamp = str(record.get("ts") or "").strip()
        return (0, "") if not stamp else (1, stamp)

    ordered = sorted(enumerate(rows), key=_sort_key)
    return [record for _, record in ordered]


def load_recorded_stock_fill_ids(fills_path: Path = STOCK_FILLS_FILE) -> set[str]:
    ids: set[str] = set()
    for row in load_stock_fill_records(fills_path):
        event_id = str(row.get("event_id") or "").strip()
        if event_id:
            ids.add(event_id)
    return ids


def load_stock_order_fill_cumulatives(
    fills_path: Path = STOCK_FILLS_FILE,
) -> dict[str, dict[str, float]]:
    """Return cumulative recorded quantity/notional/fees by broker order id."""

    cumulative: dict[str, dict[str, float]] = {}
    for row in load_stock_fill_records(fills_path):
        order_id = str(row.get("order_id") or "").strip()
        if not order_id:
            continue
        try:
            qty = float(row.get("quantity", 0.0) or 0.0)
            price = float(row.get("price", 0.0) or 0.0)
            fee = float(row.get("fee", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        bucket = cumulative.setdefault(order_id, {"quantity": 0.0, "notional": 0.0, "fee": 0.0})
        bucket["quantity"] += max(0.0, qty)
        bucket["notional"] += max(0.0, qty * price)
        bucket["fee"] += max(0.0, fee)
    return cumulative


def count_stock_fills(fills_path: Path = STOCK_FILLS_FILE) -> int:
    return len(load_stock_fill_records(fills_path))


def load_stock_ledger_epoch(epoch_path: Path = STOCK_LEDGER_EPOCH_FILE) -> dict[str, Any]:
    if not epoch_path.exists():
        return {}
    try:
        payload = json.loads(epoch_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def epoch_start_value(epoch: dict[str, Any] | None) -> float:
    """Epoch 那一刻的总资产。取不到就返回 0.0。

    这是「Epoch 是否可用」的唯一判定依据，界面、Doctor、券商对账开关都必须走
    这一个函数。以前三处各读各的字段：卡片只看 ``ts``、Doctor 只看 ``ts``、
    而主界面要 ``ts`` 且 ``total_assets > 0``，于是同一个文件被判出两种相反结论
    ——卡片显示着 Epoch 日期，旁边却写「还没有设置账本 Epoch」。

    ``positions`` 为空时用 ``cash`` 兜底：没有持仓，总资产就等于现金。
    这不是猜，是恒等式。真正需要它的原因是历史遗留——
    ``stock/tools/repair_ledger.py`` 曾绕过 :func:`write_stock_ledger_epoch`
    手工拼 epoch，只写了 ``cash``。那个脚本已修，但已经落盘的文件还在。
    """
    snapshot = (epoch or {}).get("account_snapshot") or {}
    if not isinstance(snapshot, dict):
        return 0.0

    def _num(value: Any) -> float:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return 0.0
        return out if out == out else 0.0        # 挡住 NaN

    total = _num(snapshot.get("total_assets"))
    if total > 0:
        return total
    positions = snapshot.get("positions")
    if isinstance(positions, (list, tuple)) and len(positions) == 0:
        return _num(snapshot.get("cash"))
    return 0.0


def epoch_is_set(epoch: dict[str, Any] | None) -> bool:
    """Epoch 是否已经设好、可以用来做期间归因与券商对账。

    时间戳与起点资产缺一不可：只有时间戳算不出「Epoch 后总盈亏」。
    """
    return bool((epoch or {}).get("ts")) and epoch_start_value(epoch) > 0


def write_stock_ledger_epoch(
    *,
    reason: str,
    account_snapshot: dict[str, Any] | None = None,
    fills_path: Path = STOCK_FILLS_FILE,
    epoch_path: Path = STOCK_LEDGER_EPOCH_FILE,
) -> Path:
    epoch_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "reason": reason,
        "account_snapshot": account_snapshot or {},
        "fills_count_at_reset": count_stock_fills(fills_path),
    }
    tmp = epoch_path.with_suffix(epoch_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    tmp.replace(epoch_path)
    return epoch_path
