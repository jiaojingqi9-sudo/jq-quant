#!/usr/bin/env python3
"""envcheck - 只读环境探测（给 watcher 用）

回答一个问题：这台 Mac 上有哪些可用的模型后端 / CLI，
以便决定新闻系统的模型判断层该走谁。

设计约束：
- 只读，不修改任何文件、不联网、不调用付费接口
- 只用标准库
- 容忍 watcher 传进来的额外参数（--time-range/--language-id/--json）
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()


def _run(cmd, timeout=12):
    """跑一条命令，返回 (ok, 输出)。任何异常都吞掉。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "").strip() or (p.stderr or "").strip()
        return p.returncode == 0, out[:400]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:200]


def _which(name):
    """在 PATH 和常见安装位置里找可执行文件（launchd 的 PATH 很窄）。"""
    found = shutil.which(name)
    if found:
        return found
    candidates = [
        HOME / ".local/bin" / name,
        HOME / "bin" / name,
        HOME / ".npm-global/bin" / name,
        HOME / ".nvm/versions" / name,
        HOME / ".claude/local" / name,
        HOME / ".bun/bin" / name,
        Path("/usr/local/bin") / name,
        Path("/opt/homebrew/bin") / name,
        Path("/usr/bin") / name,
    ]
    for c in candidates:
        try:
            if c.exists() and os.access(c, os.X_OK):
                return str(c)
        except Exception:
            pass
    # 兜底：npm 全局目录
    for base in (HOME / ".nvm/versions/node", Path("/opt/homebrew/lib/node_modules")):
        try:
            if base.exists():
                for sub in base.rglob(name):
                    if sub.is_file() and os.access(sub, os.X_OK):
                        return str(sub)
        except Exception:
            pass
    return ""


def probe_cli(name, version_args=("--version",)):
    path = _which(name)
    info = {"name": name, "found": bool(path), "path": path}
    if path:
        ok, out = _run([path, *version_args])
        info["version_ok"] = ok
        info["version"] = out
    return info


def main():
    result = {
        "kind": "envcheck",
        "python": sys.executable,
        "home": str(HOME),
        "path_env": os.environ.get("PATH", "")[:600],
        "clis": {},
        "env_files": {},
        "api_keys_present": {},
        "openclaw": {},
        "notes": [],
    }

    # ---- 候选模型 CLI ----
    for name in ("claude", "codex", "openclaw", "node", "npm", "gemini", "llm"):
        result["clis"][name] = probe_cli(name)

    # claude 额外探测：能否非交互运行
    claude_path = result["clis"].get("claude", {}).get("path")
    if claude_path:
        ok, out = _run([claude_path, "--help"], timeout=20)
        result["clis"]["claude"]["help_ok"] = ok
        result["clis"]["claude"]["help_head"] = out[:300]
        # 关键：确认是否支持 -p / --print 非交互模式
        result["clis"]["claude"]["supports_print_mode"] = ("-p" in out) or ("--print" in out)

    # ---- 私有 env 文件 ----
    for label, p in {
        "market_news_dir": HOME / ".market_news",
        "openai_env": HOME / ".market_news/openai_env",
        "anthropic_env": HOME / ".market_news/anthropic_env",
        "openclaw_config": HOME / ".openclaw/openclaw.json",
        "claude_config_dir": HOME / ".claude",
        "claude_json": HOME / ".claude.json",
    }.items():
        try:
            result["env_files"][label] = {
                "exists": p.exists(),
                "is_dir": p.is_dir() if p.exists() else None,
            }
        except Exception as exc:
            result["env_files"][label] = {"error": str(exc)}

    # ---- 环境变量里是否有 key（只报是否存在，绝不回传内容）----
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        val = os.environ.get(var, "")
        result["api_keys_present"][var] = {"present": bool(val.strip()), "length": len(val.strip())}

    # env 文件里是否写了 key（只看有没有该变量名，不读值）
    for label, p in {
        "openai_env": HOME / ".market_news/openai_env",
        "anthropic_env": HOME / ".market_news/anthropic_env",
    }.items():
        try:
            if p.exists():
                txt = p.read_text(encoding="utf-8", errors="replace")
                result["env_files"][label]["declares"] = [
                    v for v in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
                    if v in txt
                ]
        except Exception as exc:
            result["env_files"][label]["read_error"] = str(exc)

    # ---- openclaw 状态 ----
    oc = result["clis"].get("openclaw", {}).get("path") or str(HOME / ".openclaw/bin/openclaw")
    if Path(oc).exists():
        ok, out = _run([oc, "--version"])
        result["openclaw"] = {"path": oc, "version_ok": ok, "version": out}
    else:
        result["openclaw"] = {"path": oc, "exists": False}

    # ---- 结论建议 ----
    c = result["clis"].get("claude", {})
    if c.get("found") and c.get("supports_print_mode"):
        result["notes"].append("RECOMMEND: claude CLI 可用且支持非交互模式，可作为模型后端")
    elif c.get("found"):
        result["notes"].append("PARTIAL: 找到 claude CLI，但未确认非交互模式")
    else:
        result["notes"].append("MISSING: 未找到 claude CLI")
    if result["api_keys_present"].get("ANTHROPIC_API_KEY", {}).get("present"):
        result["notes"].append("ANTHROPIC_API_KEY 已在环境变量中")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
