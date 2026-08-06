from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

from .stock_learning import STOCK_LEARNING_REVIEW_PACKET_JSON_FILE, load_learning_review_packet
from .stock_runtime import STOCK_LEDGER_EPOCH_FILE, epoch_is_set, load_stock_ledger_epoch
from .strategy_experiment import SPLIT_STATE_FILE, load_strategy_split_state, split_state_matches_current, split_state_weight_map


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "runtime"
AUTO_TRADER_STATUS_FILE = RUNTIME_DIR / "auto_trader_status.json"
WATCHDOG_STATUS_FILE = RUNTIME_DIR / "watchdog_status.json"

STATUS_RANK = {"ok": 0, "info": 1, "warn": 2, "fail": 3}

# `taa-futu stock-system-reset` 写 Epoch 与分账起点时用的 reason。
# 两份文件都带这个 reason，才说明它们本该是同一次设置的。
RESET_REASON = "manual_stock_system_epoch"


def _fix(subcommand: str, *, exe: str | None = None, on_windows: bool | None = None) -> str:
    """把修复命令写成当前这台机器能直接粘贴运行的样子。

    以前写死 ``.venv/bin/taa-futu``。Windows 上 venv 的可执行文件在
    ``.venv\\Scripts\\`` 下，照着界面上的命令敲会报「系统找不到指定的路径」。
    优先用正在跑这段代码的解释器所在目录（venv 不叫 .venv 也对），
    找不到再按平台给默认值。

    ``exe`` / ``on_windows`` 只给测试用——直接改全局 ``os.name`` 会把 pathlib
    一起带偏（``Path()`` 按 ``os.name`` 选 WindowsPath/PosixPath）。
    """
    if on_windows is None:
        on_windows = os.name == "nt"
    name = "taa-futu.exe" if on_windows else "taa-futu"
    exe_dir = Path(exe or sys.executable).parent
    # venv 里 python 和 taa-futu 同目录；系统级安装时脚本在 Scripts/ 或 bin/ 下
    base = ".venv\\Scripts\\taa-futu" if on_windows else ".venv/bin/taa-futu"
    for folder in (exe_dir, exe_dir / ("Scripts" if on_windows else "bin")):
        script = folder / name
        if not script.exists():
            continue
        try:
            base = str(script.relative_to(Path.cwd()))
        except ValueError:
            base = str(script)
        if on_windows and base.lower().endswith(".exe"):
            base = base[:-4]
        break
    return f"{base} {subcommand}"


@dataclass(frozen=True)
class StockDoctorFinding:
    area: str
    status: str
    summary: str
    detail: str = ""
    fix_command: str = ""


@dataclass(frozen=True)
class StockDoctorReport:
    status: str
    checked_at: str
    findings: tuple[StockDoctorFinding, ...]

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "checked_at": self.checked_at,
            "findings": [asdict(item) for item in self.findings],
        }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_ts(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _age_seconds(value: object, now_utc: datetime) -> float | None:
    ts = _parse_ts(value)
    if ts is None:
        return None
    return max(0.0, (now_utc - ts).total_seconds())


def _finite_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _format_weights(weights: dict[str, float]) -> str:
    return " / ".join(f"{name} {float(value):.0%}" for name, value in weights.items())


def _status_from_findings(findings: list[StockDoctorFinding]) -> str:
    worst = max((STATUS_RANK.get(item.status, 0) for item in findings), default=0)
    if worst >= STATUS_RANK["fail"]:
        return "fail"
    if worst >= STATUS_RANK["warn"]:
        return "warn"
    return "ok"


def run_stock_system_doctor(
    settings,
    *,
    now_utc: datetime | None = None,
    epoch_path: Path = STOCK_LEDGER_EPOCH_FILE,
    split_state_path: Path = SPLIT_STATE_FILE,
    review_packet_path: Path = STOCK_LEARNING_REVIEW_PACKET_JSON_FILE,
    auto_status_path: Path = AUTO_TRADER_STATUS_FILE,
    watchdog_status_path: Path = WATCHDOG_STATUS_FILE,
    reconciliation: Any | None = None,
) -> StockDoctorReport:
    """Check whether the stock system's glue contracts are coherent.

    This is intentionally read-only. It reports what is disconnected and points
    to the one preferred repair command instead of silently mutating runtime
    state.
    """

    now = now_utc or datetime.now(UTC)
    findings: list[StockDoctorFinding] = []

    epoch = load_stock_ledger_epoch(epoch_path)
    split_state = load_strategy_split_state() if split_state_path == SPLIT_STATE_FILE else _read_json(split_state_path)

    epoch_ts = epoch.get("ts") if isinstance(epoch, dict) else None
    split_ts = split_state.get("reset_at") if isinstance(split_state, dict) else None
    if not epoch_ts:
        findings.append(
            StockDoctorFinding(
                "system_epoch",
                "fail",
                "股票事件账本还没有统一起点。",
                "没有 Epoch 时，审计账本会把现有券商持仓看成初始化差异。",
                _fix("stock-system-reset"),
            )
        )
    elif not epoch_is_set(epoch):
        # 有时间戳但起点资产缺失。Doctor 以前只看 ts，所以会说「已设置」，
        # 而主界面要求 ts + 起点资产，会说「未设置」——同一份文件两种结论，
        # 用户看到的是自相矛盾的界面。这一支就是为了让两边说同一句话。
        findings.append(
            StockDoctorFinding(
                "system_epoch",
                "warn",
                "Epoch 有时间戳但缺起点资产，期间归因与券商对账都用不了。",
                "account_snapshot 里没有 total_assets（也无法从空持仓的 cash 推出）。"
                "多半是旧版 stock/tools/repair_ledger.py 手工拼 epoch 时漏写的。",
                _fix("stock-system-reset"),
            )
        )
    if not split_ts:
        findings.append(
            StockDoctorFinding(
                "strategy_split",
                "fail",
                "四策略分账还没有统一起点。",
                "没有 split_state 时，单策略净表现没有可靠起始资金。",
                _fix("stock-system-reset"),
            )
        )
    if epoch_ts and split_ts:
        epoch_dt = _parse_ts(epoch_ts)
        split_dt = _parse_ts(split_ts)
        if epoch_dt and split_dt:
            drift_seconds = abs((epoch_dt - split_dt).total_seconds())
            epoch_reason = str((epoch or {}).get("reason") or "")
            split_reason = str((split_state or {}).get("reason") or "")
            # 只有两份都出自 stock-system-reset 时，时间不一致才说明出了问题。
            # 账本起点被有意回溯（reason=broker_history_rebuild 之类）是常见做法：
            # 让账本覆盖账户真实开始以来的全部历史，而分账实验从另一天起算。
            # 以前这里只比时间差，把这种刻意设置报成 warn，等于逼人跑
            # stock-system-reset 去迎合检查——而那会把账本起点改成今天，
            # 之前的期间归因起点就没了。拿真数据迁就规则，方向反了。
            both_from_reset = (epoch_reason.startswith(RESET_REASON)
                               and split_reason.startswith(RESET_REASON))
            if drift_seconds <= 120:
                findings.append(StockDoctorFinding("system_epoch", "ok", "股票事件账本和四策略分账起点已同步。"))
            elif both_from_reset:
                findings.append(
                    StockDoctorFinding(
                        "system_epoch",
                        "warn",
                        "股票事件账本和四策略分账起点不是同一次设置。",
                        f"两份都是 stock-system-reset 写的，却相差约 {int(drift_seconds)} 秒，"
                        "说明其中一份后来被单独改过，容易出现账本各算各的。",
                        _fix("stock-system-reset"),
                    )
                )
            else:
                findings.append(
                    StockDoctorFinding(
                        "system_epoch",
                        "info",
                        "账本起点与分账起点是分别设置的，时间不同属正常。",
                        f"账本起点 {epoch_dt.date()}（reason={epoch_reason or '未写'}），"
                        f"分账起点 {split_dt.date()}（reason={split_reason or '未写'}）。"
                        "账本起点回溯到账户真实开始那天，账本才覆盖全部历史；"
                        "分账实验从另一天起算。两者不必相同。"
                        "真要统一，跑 stock-system-reset——代价是账本起点被改成今天，"
                        "之前的期间归因起点丢失。",
                    )
                )

    if split_state:
        try:
            if split_state_matches_current(split_state, settings):
                findings.append(StockDoctorFinding("strategy_split", "ok", "四策略分账权重和当前控制台配置一致。"))
            else:
                reset_weights = split_state_weight_map(split_state)
                findings.append(
                    StockDoctorFinding(
                        "strategy_split",
                        "warn",
                        "四策略分账权重和当前控制台配置不一致。",
                        f"分账起点权重：{_format_weights(reset_weights)}。当前权重已经变化，单策略净表现只能作历史参考。",
                        _fix("stock-system-reset"),
                    )
                )
        except Exception as exc:
            findings.append(StockDoctorFinding("strategy_split", "warn", "无法检查四策略权重一致性。", str(exc)))

    packet = load_learning_review_packet(review_packet_path)
    if not packet:
        findings.append(
            StockDoctorFinding(
                "learning_lab",
                "warn",
                "股票学习审阅包不存在或不可读取。",
                "自动分析和人工复核缺少证据包。",
                _fix("stock-learning-export"),
            )
        )
    else:
        age = _age_seconds(packet.get("generated_at"), now)
        evidence = dict(packet.get("evidence") or {})
        if age is not None and age > 36 * 3600:
            findings.append(
                StockDoctorFinding(
                    "learning_lab",
                    "warn",
                    "股票学习审阅包已经过期。",
                    f"packet_id={packet.get('packet_id', 'unknown')}，约 {age / 3600:.1f} 小时前生成。",
                    _fix("stock-learning-export"),
                )
            )
        else:
            findings.append(
                StockDoctorFinding(
                    "learning_lab",
                    "ok",
                    "股票学习审阅包可读取。",
                    f"packet_id={packet.get('packet_id', 'unknown')}，outcomes={evidence.get('realized_outcomes', 0)}，candidates={evidence.get('candidate_count', 0)}。",
                )
            )

    auto_status = _read_json(auto_status_path)
    if not auto_status:
        findings.append(StockDoctorFinding("auto_trader", "info", "自动交易状态文件不存在。", "如果当前没有启动自动交易，这是正常的。"))
    else:
        action = str(auto_status.get("action", ""))
        age = _age_seconds(auto_status.get("updated_at"), now)
        if action == "error":
            findings.append(StockDoctorFinding("auto_trader", "fail", "自动交易最近状态是 error。", str(auto_status.get("detail", ""))))
        elif age is not None and age > max(600, int(getattr(settings, "watchdog_stale_status_seconds", 240)) * 2):
            findings.append(StockDoctorFinding("auto_trader", "warn", "自动交易状态偏旧。", f"最近更新时间约 {age:.0f} 秒前。"))
        else:
            findings.append(StockDoctorFinding("auto_trader", "ok", "自动交易状态文件可读取。", f"action={action or '-'}"))

    watchdog_status = _read_json(watchdog_status_path)
    if not watchdog_status:
        findings.append(StockDoctorFinding("watchdog", "info", "守护监控状态文件不存在。", "如果当前没有启动全天自动运行，这是正常的。"))
    else:
        action = str(watchdog_status.get("action", ""))
        health = str(watchdog_status.get("health", ""))
        if health == "error" or action == "error":
            findings.append(StockDoctorFinding("watchdog", "fail", "守护监控最近状态是 error。", str(watchdog_status.get("detail", ""))))
        else:
            findings.append(StockDoctorFinding("watchdog", "ok", "守护监控状态文件可读取。", f"health={health or '-'}, action={action or '-'}"))

    if reconciliation is not None:
        if getattr(reconciliation, "ok", False):
            findings.append(StockDoctorFinding("broker_reconciliation", "ok", "审计账本和券商持仓对账通过。"))
        else:
            breaks = getattr(reconciliation, "breaks", ()) or ()
            status = "warn" if epoch_ts else "info"
            break_detail = "; ".join(
                f"{getattr(item, 'kind', '?')} {getattr(item, 'symbol', '')}: "
                f"expected={getattr(item, 'expected', 0):.4f}, actual={getattr(item, 'actual', 0):.4f}, diff={getattr(item, 'difference', 0):+.4f}"
                for item in list(breaks)[:3]
            )
            if len(breaks) > 3:
                break_detail += f"; 另有 {len(breaks) - 3} 个差异"
            detail = (
                "未设置统一起点时这通常只是初始化差异；设置起点后仍存在才需要排查。"
                if not epoch_ts
                else f"已设置统一起点，差异来自成交同步、券商费用/现金四舍五入或持仓快照延迟。{break_detail}"
            )
            findings.append(
                StockDoctorFinding(
                    "broker_reconciliation",
                    status,
                    f"审计账本和券商持仓有 {len(breaks)} 个差异。",
                    detail,
                    _fix("stock-system-reset") if not epoch_ts else "",
                )
            )

    if not findings:
        findings.append(StockDoctorFinding("stock_system", "ok", "没有发现需要处理的股票系统胶水问题。"))

    return StockDoctorReport(status=_status_from_findings(findings), checked_at=now.isoformat(), findings=tuple(findings))
