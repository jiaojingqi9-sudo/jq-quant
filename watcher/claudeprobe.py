#!/usr/bin/env python3
"""claudeprobe - 真实调用一次 Claude CLI 后端，确认端到端可用。

不写任何生产文件、不发手机消息。只验证：
  claude -p <prompt>  能否返回可被 _parse_json_object 解析的 JSON。
"""
import json
import sys
import time
from pathlib import Path

REPO = Path.home() / "All here" / "news collector"
sys.path.insert(0, str(REPO))


def main():
    out = {"kind": "claudeprobe"}
    try:
        from market_news.services import model_judgement as M
    except Exception as exc:
        out["import_error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    cfg = M.ModelJudgementConfig.from_file(
        REPO / "config" / "model_judgement.json", project_root=REPO
    )
    binary = M._resolve_claude_binary(cfg.claude_bin)
    out["binary"] = binary
    out["model_configured"] = cfg.claude_model or "(default)"
    if not binary:
        out["error"] = "claude binary not found"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    cache = M.ModelJudgementCache(cfg.cache_path, ttl_hours=cfg.cache_ttl_hours)
    budget = M.ModelCallBudget(cfg.budget_path, daily_limit=cfg.model_daily_call_limit)
    client = M.ClaudeCliJsonClient(cfg, cache, budget)
    out["available"] = client.available

    # 直接打后端，绕过缓存/预算，纯粹测通路
    instructions = (
        "你是一个只输出 JSON 的接口。根据 INPUT_JSON 判断这条新闻是否值得投资者注意。"
        "只输出一个 JSON object，字段：worth_attention(bool)、confidence(0-1 float)、reason(中文一句话)。"
        "不要输出任何解释、markdown 代码块或多余文字。"
    )
    payload = {
        "task": "screen_event",
        "headline": "某半导体公司公告：第二季度净利润同比增长 120%，并上调全年营收指引",
        "source": "公司公告",
    }

    t0 = time.time()
    try:
        result = client._run_json_agent(instructions=instructions, payload=payload)
        out["elapsed_seconds"] = round(time.time() - t0, 1)
        out["parsed_result"] = result
        out["end_to_end_ok"] = isinstance(result, dict) and "worth_attention" in (result or {})
    except Exception as exc:
        out["elapsed_seconds"] = round(time.time() - t0, 1)
        out["call_error"] = f"{type(exc).__name__}: {exc}"
        out["end_to_end_ok"] = False

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
