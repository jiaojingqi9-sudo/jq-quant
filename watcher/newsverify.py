#!/usr/bin/env python3
"""newsverify - 在本机验证 news collector 的改动（只读，不发消息）

做三件事：
1. 跑单元测试
2. 用真实的 latest_report.json 跑一遍推送选择逻辑（dry-run，不发送）
3. 探测 Claude CLI 后端是否可用

不修改任何文件、不触发任何手机推送。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path.home() / "All here" / "news collector"


def _run(cmd, timeout=300, cwd=None):
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def main():
    out = {"kind": "newsverify", "repo": str(REPO), "python": sys.executable}

    if not REPO.exists():
        out["error"] = "repo not found"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    # ── 1. 单元测试 ────────────────────────────────────────────────────────
    rc, so, se = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=REPO)
    tail = (se or so).strip().splitlines()
    out["unit_tests"] = {
        "returncode": rc,
        "passed": rc == 0,
        "tail": tail[-12:] if tail else [],
    }

    # ── 2. 推送选择逻辑（真实报告，dry-run）────────────────────────────────
    probe = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / "All here" / "news collector"))
from market_news.services.notification import AlertDigestBuilder
from market_news.domain.models import AlertLevel

report = Path.home() / "All here" / "news collector" / "reports" / "live" / "latest_report.json"
payload = json.loads(report.read_text(encoding="utf-8"))

res = {}
res["report_created_at"] = payload.get("created_at")
res["alerts_in_report"] = len(payload.get("alerts", []))
levels = {}
for a in payload.get("alerts", []):
    if isinstance(a, dict):
        levels[a.get("level")] = levels.get(a.get("level"), 0) + 1
res["alert_levels"] = levels

b = AlertDigestBuilder(min_level=AlertLevel.HIGH, max_alerts=3, include_existing=True)
lookup = b._build_event_lookup(payload)
res["events_in_lookup"] = len(lookup)
res["model_layer_down"] = b._model_judgement_unavailable(lookup)

plan = b.compose(payload, channel="whatsapp", target="probe",
                 sent_cluster_ids=set(), preview_path=Path("/tmp/_nv_preview.txt"))
res["plan_is_none"] = plan is None
if plan is not None:
    res["would_send_alert_count"] = plan.alert_count
    res["message_preview"] = plan.message[:700]
print(json.dumps(res, ensure_ascii=False))
'''
    rc, so, se = _run([sys.executable, "-c", probe], cwd=REPO)
    if rc == 0 and so.strip():
        try:
            out["delivery_probe"] = json.loads(so.strip().splitlines()[-1])
        except Exception:
            out["delivery_probe"] = {"raw": so[-1500:]}
    else:
        out["delivery_probe"] = {"returncode": rc, "stderr": se[-1200:]}

    # ── 3. Claude 后端可用性 ───────────────────────────────────────────────
    backend = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / "All here" / "news collector"))
from market_news.services import model_judgement as M
repo = Path.home() / "All here" / "news collector"
cfg = M.ModelJudgementConfig.from_file(repo / "config" / "model_judgement.json", project_root=repo)
cache = M.ModelJudgementCache(cfg.cache_path, ttl_hours=cfg.cache_ttl_hours)
budget = M.ModelCallBudget(cfg.budget_path, daily_limit=cfg.model_daily_call_limit)
c = M.ClaudeCliJsonClient(cfg, cache, budget)
o = M.OpenClawAgentJsonClient(cfg, cache, budget)
p = M.OpenAIResponsesJsonClient(cfg, cache, budget)
chain = M.CascadingModelJudgementClient(primary=p, fallback=o, extras=[c])
print(json.dumps({
    "claude_bin_resolved": M._resolve_claude_binary(cfg.claude_bin),
    "claude_available": c.available,
    "openclaw_available": o.available,
    "openai_available": p.available,
    "chain_available": chain.available,
    "claude_max_screen": c._max_screening_calls,
    "claude_cache_tag": c.backend_tag,
}, ensure_ascii=False))
'''
    rc, so, se = _run([sys.executable, "-c", backend], cwd=REPO)
    if rc == 0 and so.strip():
        try:
            out["backends"] = json.loads(so.strip().splitlines()[-1])
        except Exception:
            out["backends"] = {"raw": so[-1000:]}
    else:
        out["backends"] = {"returncode": rc, "stderr": se[-1200:]}

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
