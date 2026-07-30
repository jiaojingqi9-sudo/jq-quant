#!/usr/bin/env python3
"""newsdoctor - 新闻收集器全盘体检：一次看清整套系统哪里在报错。

只读。检查六个方面：
  1. 五条后台任务是否在跑、上次退出码
  2. 各条线的心跳是否新鲜（UTC/本地时区已对齐）
  3. 模型层是否活着（今日调用量、用的哪个后端）
  4. 最近一轮采集的产出与推送结果
  5. 各采集源的失败排行（哪些源一直抓不到）
  6. 磁盘占用大户
"""
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path.home() / "All here" / "news collector"
LIVE = REPO / "reports" / "live"
LOGS = REPO / "runtime" / "logs"

JOBS = [
    "ai.codex.marketnews.collect",
    "ai.codex.marketnews.notify",
    "ai.codex.marketnews.health",
    "ai.codex.marketnews.newslearning",
    "ai.codex.marketnews.reviewapi",
]


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _age(iso):
    try:
        dt = datetime.fromisoformat(str(iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return None


def _tail(path, nbytes=300_000):
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, sz - nbytes))
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def main():
    out = {"kind": "newsdoctor", "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    problems = []

    # ── 1. launchd 任务 ────────────────────────────────────────────────────
    rc, so, _ = _run(["launchctl", "list"])
    loaded = {}
    for line in so.splitlines():
        if "marketnews" in line.lower():
            parts = line.split("\t")
            loaded[parts[-1]] = {"pid": parts[0], "last_exit": parts[1] if len(parts) > 1 else "?"}
    jobs = {}
    for label in JOBS:
        info = loaded.get(label)
        if not info:
            jobs[label] = "未加载"
            problems.append(f"任务未加载: {label}")
        else:
            jobs[label] = info
            le = info.get("last_exit", "0")
            if le not in ("0", "-", "-9", "-15"):
                problems.append(f"任务上次异常退出: {label} (exit={le})")
    out["jobs"] = jobs
    out["disabled_jobs"] = [
        p.name for p in (Path.home() / "Library" / "LaunchAgents").glob("*marketnews*.disabled")
    ]

    # ── 2. 各条线心跳 ──────────────────────────────────────────────────────
    lines_status = {}
    for name, fn in [
        ("collect", "collect_status.json"),
        ("delivery", "delivery_status.json"),
        ("health", "health_status.json"),
        ("news_learning", "news_learning_status.json"),
        ("review_api", "review_api_status.json"),
    ]:
        d = _load(LIVE / fn)
        if not d:
            lines_status[name] = "状态文件缺失"
            problems.append(f"状态文件缺失: {fn}")
            continue
        age = _age(d.get("timestamp"))
        entry = {"status": d.get("overall_status"), "age_seconds": age}
        errs = d.get("errors") or []
        if errs:
            entry["errors"] = [str(e)[:160] for e in errs[:2]]
            problems.append(f"{name} 报错: {str(errs[0])[:100]}")
        if age is not None and age > 1800:
            problems.append(f"{name} 心跳过期 {age//60} 分钟")
        lines_status[name] = entry
    out["lines"] = lines_status

    # ── 3. 模型层 ──────────────────────────────────────────────────────────
    budget = _load(REPO / "data" / "model_judgement_budget.json") or {}
    cfg = _load(REPO / "config" / "model_judgement.json") or {}
    model = {
        "today_calls": budget.get("used"),
        "by_backend": budget.get("by_kind"),
        "daily_limit": cfg.get("model_daily_call_limit"),
        "claude_enabled": cfg.get("claude_enabled"),
        "openclaw_enabled": cfg.get("openclaw_enabled"),
    }
    rep = _load(LIVE / "latest_report.json") or {}
    st = Counter()
    backends = Counter()
    for key in ("top_events", "negative_risks", "positive_catalysts", "watchlist"):
        for e in rep.get(key, []):
            if isinstance(e, dict):
                mj = e.get("model_judgement") or {}
                st[str(mj.get("screening_status"))] += 1
                b = mj.get("_model_backend")
                if b:
                    backends[str(b)] += 1
    model["screening_status"] = dict(st)
    model["backends_used"] = dict(backends)
    if not st.get("used"):
        problems.append("模型层无有效判定（screening_status 没有 used）——推送会退到规则降级模式")
    out["model_layer"] = model

    # ── 4. 最近产出与推送 ──────────────────────────────────────────────────
    out["latest_report"] = {
        "created_at": rep.get("created_at"),
        "age_seconds": _age(rep.get("created_at")),
        "alerts": len(rep.get("alerts", [])),
        "alert_levels": dict(Counter(a.get("level") for a in rep.get("alerts", []) if isinstance(a, dict))),
    }
    deliv = _load(LIVE / "delivery_status.json") or {}
    n = deliv.get("notification") or {}
    out["last_delivery"] = {
        "status": n.get("status"),
        "alert_count": n.get("alert_count"),
        "detail": str(n.get("detail", ""))[:120],
    }

    # 最近一次真正发送成功是什么时候
    last_sent = None
    hist = LIVE / "delivery_history.jsonl"
    for line in reversed(_tail(hist, 2_000_000).splitlines()):
        try:
            d = json.loads(line)
        except Exception:
            continue
        nn = d.get("notification") or {}
        if nn.get("status") == "sent" and (nn.get("alert_count") or 0) > 0:
            last_sent = d.get("timestamp")
            break
    out["last_successful_push"] = last_sent
    if last_sent:
        age = _age(last_sent)
        if age and age > 86400 * 2:
            problems.append(f"已 {age//86400} 天没有成功推送过新闻")

    # ── 5. 采集源失败排行 ──────────────────────────────────────────────────
    # err 日志是追加的、且行内没有时间戳，按"读最后 N 字节"统计会把几小时前
    # 早已修好的错误算进来，看上去像"修了还在报错"。改为记录上次检查的读取位置，
    # 只统计两次检查之间的新增部分。
    errlog = LOGS / "collect.launchd.err.log"
    marker = REPO / "runtime" / ".newsdoctor_errlog_offset"
    try:
        size_now = errlog.stat().st_size
    except OSError:
        size_now = 0
    last_off = 0
    try:
        last_off = int(marker.read_text().strip())
    except Exception:
        last_off = 0
    if last_off > size_now:      # 日志被轮转/清空过
        last_off = 0
    first_run = last_off == 0

    chunk = ""
    if size_now > last_off:
        try:
            with open(errlog, "rb") as f:
                f.seek(last_off)
                chunk = f.read(2_000_000).decode("utf-8", errors="replace")
        except OSError:
            chunk = ""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(size_now))
    except OSError:
        pass

    fails = Counter(re.findall(r"collector ([\w\-]+) failed", chunk))
    empties = Counter(re.findall(r"^([\w\-]+): body extraction returned empty", chunk, re.M))
    out["source_failures_since_last_check"] = {
        "new_log_bytes": len(chunk),
        "first_run_baseline_only": first_run,
        "top": fails.most_common(10),
    }
    out["body_extraction_empty_since_last_check"] = empties.most_common(5)
    if fails and not first_run:
        problems.append(f"上次检查以来有 {len(fails)} 个采集源报错，最频繁: {fails.most_common(1)[0][0]}")
    if empties and not first_run:
        problems.append(f"{len(empties)} 个源抓到页面但提取不到正文（解析规则过时）")

    # ── 6. 磁盘 ────────────────────────────────────────────────────────────
    big = []
    for p in list(LIVE.glob("*.jsonl")) + list(LOGS.glob("*.log")):
        try:
            mb = p.stat().st_size / 1024 / 1024
            if mb > 50:
                big.append({"file": str(p.relative_to(REPO)), "MB": round(mb)})
        except OSError:
            pass
    big.sort(key=lambda x: -x["MB"])
    out["large_files"] = big[:8]
    total = sum(b["MB"] for b in big)
    if total > 1000:
        problems.append(f"日志/历史文件已占用约 {total/1024:.1f} GB")

    out["problems"] = problems
    out["problem_count"] = len(problems)
    out["verdict"] = "全部正常" if not problems else f"发现 {len(problems)} 个问题"

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
