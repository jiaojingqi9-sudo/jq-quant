#!/usr/bin/env python3
"""
Futu Watcher - 后台静默服务（v3，带自动热重载 + 账户查询）

每秒扫一次 ~/All here/futu_queue/，看到 *.task.json 就调对应的脚本，
执行完把结果写到 *.result.json，删掉原 task 文件。

自动热重载：watcher.py 改了 → 自动退出 → launchd 重启。

支持的 skill：
  行情类（需要 symbol 参数）:
    - snapshot       公开API综合快照（价格/K线/资金分布/期权到期日）
    - technical      技术面异动
    - capital        资金面异动
    - derivatives    衍生品/期权异动
  账户类（不需要 symbol）:
    - accounts       账户列表
    - positions      当前持仓
    - cash           现金/购买力
    - orders         当前挂单
    - history        历史委托 + 历史成交
    - account_all    一把抓
  诊断:
    - _ping          watcher 状态、futu-api 版本

任务文件示例:
  { "skill": "snapshot", "symbol": "US.TSLA", "time_range": 30 }
  { "skill": "positions" }
  { "skill": "history", "days": 90 }
"""
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

HOME = Path.home()
ALLHERE = HOME / "All here"
SKILLS_DIR = ALLHERE / "skills"
WATCHER_DIR = ALLHERE / "futu_watcher"
QUEUE_DIR = ALLHERE / "futu_queue"
LOG_FILE = QUEUE_DIR / "_watcher.log"

# skill -> (script_path, use_symbol, extra_positional_args)
SKILL_REG = {
    # 行情类
    "snapshot":    (WATCHER_DIR / "snapshot.py",   True,  []),
    "technical":   (SKILLS_DIR / "futu-technical-anomaly"   / "scripts" / "handle_technical_anomaly.py",   True, []),
    "capital":     (SKILLS_DIR / "futu-capital-anomaly"     / "scripts" / "handle_capital_anomaly.py",     True, []),
    "derivatives": (SKILLS_DIR / "futu-derivatives-anomaly" / "scripts" / "handle_derivatives_anomaly.py", True, []),
    # 账户类
    "accounts":    (WATCHER_DIR / "account.py", False, ["accounts"]),
    "positions":   (WATCHER_DIR / "account.py", False, ["positions"]),
    "cash":        (WATCHER_DIR / "account.py", False, ["cash"]),
    "orders":      (WATCHER_DIR / "account.py", False, ["orders"]),
    "history":     (WATCHER_DIR / "account.py", False, ["history"]),
    "account_all": (WATCHER_DIR / "account.py", False, ["all"]),
    # 临时诊断
    "intraday_check": (WATCHER_DIR / "intraday_check.py", False, []),
    "envcheck":       (WATCHER_DIR / "envcheck.py",       False, []),
    "desktop_scan":   (WATCHER_DIR / "desktop_scan.py",   False, []),
    "make_jq_app":    (WATCHER_DIR / "make_jq_app.py",    False, []),
    "make_icon":      (WATCHER_DIR / "make_icon.py",      False, []),
    "finish_desktop": (WATCHER_DIR / "finish_desktop.py", False, []),
    "test_jq_app":    (WATCHER_DIR / "test_jq_app.py",    False, []),
    "verify_jq_app":  (WATCHER_DIR / "verify_jq_app.py",  False, []),
    "newsverify":     (WATCHER_DIR / "newsverify.py",     False, []),
    "newsdoctor":     (WATCHER_DIR / "newsdoctor.py",     False, []),
    "sourcecheck":    (WATCHER_DIR / "sourcecheck.py",    False, []),
    "sourcefix_probe": (WATCHER_DIR / "sourcefix_probe.py", False, []),
    "apply_source_fixes": (WATCHER_DIR / "apply_source_fixes.py", False, []),
    "bodyprobe":      (WATCHER_DIR / "bodyprobe.py",      False, []),
    "newspanel_verify": (WATCHER_DIR / "newspanel_verify.py", False, []),
    "plugin_verify":  (WATCHER_DIR / "plugin_verify.py",  False, []),
    "tradetests":     (WATCHER_DIR / "tradetests.py",     False, []),
    "tradetests_bg":  (WATCHER_DIR / "tradetests_bg.py",  False, []),
    "home_tests":     (WATCHER_DIR / "home_tests.py",     False, []),
    "tradetest_one":  (WATCHER_DIR / "tradetest_one.py",  False, []),
    "baseline_test":  (WATCHER_DIR / "baseline_test.py",  False, []),
    "timing_probe":   (WATCHER_DIR / "timing_probe.py",   False, []),
    "apptest_timing": (WATCHER_DIR / "apptest_timing.py", False, []),
    "profile_stock":  (WATCHER_DIR / "profile_stock.py",  False, []),
    "profile_direct": (WATCHER_DIR / "profile_direct.py", False, []),
    "profile_live":   (WATCHER_DIR / "profile_live.py",   False, []),
    "order_shape":    (WATCHER_DIR / "order_shape.py",    False, []),
    "order_range_probe": (WATCHER_DIR / "order_range_probe.py", False, []),
    "order_chunk_verify": (WATCHER_DIR / "order_chunk_verify.py", False, []),
    "cache_verify":   (WATCHER_DIR / "cache_verify.py",   False, []),
    "baseline_realdata": (WATCHER_DIR / "baseline_realdata.py", False, []),
    "restart_trade_app": (WATCHER_DIR / "restart_trade_app.py", False, []),
    "logclean":       (WATCHER_DIR / "logclean.py",       False, []),
    "logclean_apply": (WATCHER_DIR / "logclean.py",       False, ["--apply"]),
    "install_maintenance": (WATCHER_DIR / "install_maintenance.py", False, []),
    "gitclean":       (WATCHER_DIR / "gitclean.py",       False, []),
    "gitproc":        (WATCHER_DIR / "gitproc.py",        False, []),
    "gitcommit":      (WATCHER_DIR / "gitcommit.py",      False, []),
    "githubcheck":    (WATCHER_DIR / "githubcheck.py",    False, []),
    "gitidentity":    (WATCHER_DIR / "gitidentity.py",    False, []),
    "ghrepos":        (WATCHER_DIR / "ghrepos.py",        False, []),
    "ghinspect":      (WATCHER_DIR / "ghinspect.py",      False, []),
    "gitdiverge":     (WATCHER_DIR / "gitdiverge.py",     False, []),
    "build_monorepo": (WATCHER_DIR / "build_monorepo.py", False, []),
    "sync_monorepo":  (WATCHER_DIR / "sync_monorepo.py",  False, []),
    "deepclean":      (WATCHER_DIR / "deepclean.py",      False, []),
    "deepclean_apply": (WATCHER_DIR / "deepclean.py",     False, ["--apply"]),
    "gitclean_run":   (WATCHER_DIR / "gitclean_launch.py", False, []),
    "archive_marketdata":     (WATCHER_DIR / "archive_marketdata.py", False, []),
    "archive_marketdata_run": (WATCHER_DIR / "archive_launch.py",     False, []),
    "claudeprobe":    (WATCHER_DIR / "claudeprobe.py",    False, []),
    "claudemodel":    (WATCHER_DIR / "claudemodel.py",    False, []),
    "stop_codex_review": (WATCHER_DIR / "stop_codex_review.py", False, []),
    "stop_codex_all": (WATCHER_DIR / "stop_codex_all.py", False, []),
    "collectdoctor":  (WATCHER_DIR / "collectdoctor.py",  False, []),
    "collectrestart": (WATCHER_DIR / "collectdoctor.py",  False, ["--fix"]),
    "restart_news_stack": (WATCHER_DIR / "restart_news_stack.py", False, []),
    "option_quote":   (WATCHER_DIR / "option_quote.py",   True,  []),
    "multi_snap":     (WATCHER_DIR / "multi_snap.py",     True,  []),
    "qot":            (WATCHER_DIR / "qot_call.py",   False, []),
    "svh":            (WATCHER_DIR / "shrink_vol_high.py", False, []),
    # 综合多策略实时建议 (read-only, 走 trade venv)
    "live_signal":    (WATCHER_DIR / "live_signal_proxy.py", False, []),
    "filter":         (WATCHER_DIR / "stock_filter.py", False, []),
    # 文件整理（只读盘点在前，实际移动要显式 _apply）
    "launchd_audit":  (WATCHER_DIR / "launchd_audit.py", False, []),
    "plist_probe":    (WATCHER_DIR / "plist_probe.py",   False, []),
    "fix_notify_plist": (WATCHER_DIR / "fix_notify_plist.py", False, []),
    "tidy_plan":      (WATCHER_DIR / "tidy_launchers.py", False, []),
    "tidy_apply":     (WATCHER_DIR / "tidy_launchers.py", False, ["--apply"]),
    "tidy_restore":   (WATCHER_DIR / "restore_from_trash.py", False, []),
    "tidy_tests":     (WATCHER_DIR / "tidy_tests.py",     False, []),
    "tidy_verify":    (WATCHER_DIR / "tidy_verify.py",    False, []),
    "capture_schema": (WATCHER_DIR / "capture_schema.py", False, []),
    "ledger_verify":   (WATCHER_DIR / "ledger_verify.py", False, []),
    "break_probe":     (WATCHER_DIR / "break_probe.py",   False, []),
    "fills_diff":      (WATCHER_DIR / "fills_diff.py",    False, []),
    "cookie_probe":    (WATCHER_DIR / "cookie_probe.py",  False, []),
    "cookie_check":    (WATCHER_DIR / "cookie_check.py",  False, []),
    "newstests":       (WATCHER_DIR / "newstests.py",     False, []),
    "weibo_off":       (WATCHER_DIR / "weibo_off_verify.py", False, []),
    "doctor_test":     (WATCHER_DIR / "doctor_test.py",   False, []),
    "shoot_real_news": (WATCHER_DIR / "shoot_real_news.py", False, []),
    "backfill_plan":   (WATCHER_DIR / "backfill_fills.py", False, []),
    "backfill_apply":  (WATCHER_DIR / "backfill_fills.py", False, ["--apply"]),
    "fix_epoch_plan":  (WATCHER_DIR / "fix_epoch.py", False, []),
    "fix_epoch_apply": (WATCHER_DIR / "fix_epoch.py", False, ["--apply"]),
    "demo_news":      (WATCHER_DIR / "make_demo_news.py", False, []),
    "screenshots":    (WATCHER_DIR / "make_screenshots.py", False, []),
    "deadcode_plan":  (WATCHER_DIR / "deadcode_cut.py",   False, []),
    "deadcode_apply": (WATCHER_DIR / "deadcode_cut.py",   False, ["--apply"]),
}

# 这些 skill 走 multi 模式：watcher 把整个 task dict 透传给 script，
# script 自己解析 symbols 列表等多值字段。其他 skill 仍按传统 single-symbol 套路。
SKILL_KIND_MULTI = {"live_signal", "filter", "qot", "svh"}

_SELF_PATH = Path(__file__).resolve()
_START_MTIME = _SELF_PATH.stat().st_mtime


def _runner_python() -> str:
    """Pick the Python interpreter that runs each skill subprocess.

    Defaults to ``sys.executable`` (whatever launchd started the watcher with —
    historically ``/opt/anaconda3/bin/python3``). Setting the env var
    ``FUTU_WATCHER_PYTHON`` to an absolute path overrides this, which is the
    recommended way to align skills with the ``trade`` project venv:

        export FUTU_WATCHER_PYTHON="$HOME/All here/trade/.venv/bin/python"

    The override path is only used if the file exists; otherwise we silently
    fall back so a misconfigured plist does not break the watcher.
    """
    override = (os.environ.get("FUTU_WATCHER_PYTHON") or "").strip()
    if override and Path(override).exists():
        return override
    return sys.executable


def log(msg: str) -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line.rstrip(), flush=True)


def handle_ping() -> dict:
    info = {
        "watcher_pid": os.getpid(),
        "watcher_py": str(_SELF_PATH),
        "watcher_start_mtime": _START_MTIME,
        "python": sys.executable,
        "subprocess_python": _runner_python(),
        "futu_watcher_python_env": os.environ.get("FUTU_WATCHER_PYTHON", ""),
        "skills_registered": sorted(SKILL_REG.keys()),
    }
    try:
        import futu
        info["futu_version"] = getattr(futu, "__version__", "unknown")
        info["has_get_technical_unusual"] = hasattr(futu.OpenQuoteContext, "get_technical_unusual")
        info["has_position_list_query"] = hasattr(futu.OpenSecTradeContext, "position_list_query")
    except Exception as e:
        info["futu_error"] = str(e)
    return {"ok": True, "data": info, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}


def build_cmd(skill: str, task: dict) -> list:
    script, use_symbol, extra = SKILL_REG[skill]
    python = _runner_python()
    # multi 模式：把整个 task 透传给 script，script 自己 parse。
    if skill in SKILL_KIND_MULTI:
        return [python, str(script), "--task-json", json.dumps(task, ensure_ascii=False)]
    cmd = [python, str(script)]
    cmd.extend(extra)
    if use_symbol:
        sym = task.get("symbol")
        if not sym:
            raise ValueError(f"skill '{skill}' requires 'symbol' field")
        cmd.append(sym)
    # 通用参数
    cmd.extend(["--time-range", str(task.get("time_range", task.get("days", 30)))])
    cmd.extend(["--language-id", str(task.get("language_id", 0))])
    cmd.append("--json")
    ind = task.get("indicator_filters")
    if ind and use_symbol:
        cmd.extend(["--indicator-filters"] + [str(x) for x in ind])
    return cmd


def process_task(task_path: Path) -> None:
    task_id = task_path.name.replace(".task.json", "")
    result_tmp = QUEUE_DIR / f"{task_id}.result.tmp"
    result_final = QUEUE_DIR / f"{task_id}.result.json"
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")

    try:
        with open(task_path, encoding="utf-8") as f:
            task = json.load(f)
        log(f"task {task_id}: {task}")

        skill = task.get("skill", "")

        if skill == "_ping":
            result = handle_ping()
        elif skill not in SKILL_REG:
            result = {
                "ok": False,
                "error": f"Unknown skill: {skill}; supported: {sorted(SKILL_REG.keys()) + ['_ping']}",
                "ts": ts,
            }
        else:
            script, _, _ = SKILL_REG[skill]
            if not script.exists():
                raise FileNotFoundError(f"Script not found: {script}")

            cmd = build_cmd(skill, task)
            log(f"  exec: {' '.join(cmd)}")
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=180,
            )

            if completed.returncode == 0 and completed.stdout.strip():
                try:
                    payload = json.loads(completed.stdout)
                    result = {"ok": True, "data": payload, "ts": ts}
                except json.JSONDecodeError:
                    result = {"ok": True, "data": {"raw": completed.stdout}, "ts": ts}
            else:
                result = {
                    "ok": False,
                    "error": (completed.stderr or completed.stdout or "unknown error").strip(),
                    "returncode": completed.returncode,
                    "ts": ts,
                }
    except Exception as e:
        result = {
            "ok": False, "error": str(e),
            "traceback": traceback.format_exc(), "ts": ts,
        }
        log(f"task {task_id} FAILED: {e}")

    with open(result_tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    result_tmp.replace(result_final)
    try:
        task_path.unlink()
    except FileNotFoundError:
        pass

    status = "ok" if result.get("ok") else "fail"
    log(f"task {task_id} -> result written ({status})")


def check_self_updated() -> bool:
    try:
        cur = _SELF_PATH.stat().st_mtime
        return cur > _START_MTIME + 0.5
    except Exception:
        return False


def main() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"watcher started v3 (python={sys.executable}, mtime={_START_MTIME})")

    health = QUEUE_DIR / "_watcher_alive.txt"

    while True:
        try:
            health.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
            for task_path in sorted(QUEUE_DIR.glob("*.task.json")):
                process_task(task_path)

            if check_self_updated():
                log("watcher.py changed on disk, exiting for launchd respawn")
                sys.exit(0)
        except SystemExit:
            raise
        except Exception as e:
            log(f"loop error: {e}")
            log(traceback.format_exc())
        time.sleep(1)


if __name__ == "__main__":
    main()
