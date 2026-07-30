#!/usr/bin/env python3
"""claudemodel - 查 claude CLI 当前实际使用的模型。"""
import json
import subprocess
import sys
from pathlib import Path

BIN = "/opt/homebrew/bin/claude"


def _run(cmd, timeout=90, stdin=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def main():
    out = {"kind": "claudemodel", "binary": BIN}

    rc, so, se = _run([BIN, "--version"])
    out["version"] = so or se

    # 用 json 输出格式跑一次，元数据里带 model
    rc, so, se = _run([BIN, "-p", "reply with the single word: ok", "--output-format", "json"])
    out["json_call_rc"] = rc
    if so:
        try:
            data = json.loads(so)
            out["model_from_metadata"] = (
                data.get("model")
                or (data.get("modelUsage") and list(data["modelUsage"].keys()))
                or data.get("meta", {}).get("model")
            )
            out["result_text"] = str(data.get("result", ""))[:120]
            out["usage_keys"] = list(data.keys())[:15]
        except Exception as exc:
            out["json_parse_error"] = str(exc)
            out["raw_head"] = so[:400]
    else:
        out["stderr"] = se[:300]

    # 配置里是否固定了模型
    for label, p in {
        "settings": Path.home() / ".claude" / "settings.json",
        "claude_json": Path.home() / ".claude.json",
    }.items():
        try:
            if p.exists():
                txt = p.read_text(encoding="utf-8", errors="replace")
                d = json.loads(txt)
                if isinstance(d, dict):
                    out[f"{label}_model"] = d.get("model", "(not set)")
        except Exception as exc:
            out[f"{label}_error"] = str(exc)[:120]

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
