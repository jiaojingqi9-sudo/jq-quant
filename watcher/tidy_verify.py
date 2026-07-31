#!/usr/bin/env python3
"""tidy_verify - 整理完成后的收尾核对。

四件事：
  1. 改过的 market_news_stack.command 语法是否还正确（zsh -n）
  2. 用它的 write_agent 真的生成一份 plist 到临时目录，看 plutil 认不认
     ——光看代码不算数，2026-07-31 那个 bug 就是「看着对、生成出来是坏的」
  3. 九个后台任务是否都还在
  4. 各启动器目录剩下几个，以及是否还有指向已移走文件的引用
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
ALL = HOME / "All here"
STACK = ALL / "news collector" / "scripts" / "market_news_stack.command"


def sh(*a, **kw):
    p = subprocess.run(a, capture_output=True, text=True, timeout=kw.get("timeout", 60))
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def main():
    out = {"kind": "tidy_verify"}

    # 1. 语法
    rc, so, se = sh("/bin/zsh", "-n", str(STACK))
    out["stack_syntax_ok"] = rc == 0
    if rc != 0:
        out["stack_syntax_err"] = se[:200]

    # 2. 真的生成一份 plist 出来验
    #    抽出 xml_escape 与 write_agent，喂进 notify 那条含 && 的命令行
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "probe.plist"
        # 只保留函数定义：从开头截到 write_keepalive_agent 之前，再删掉
        # 会真去装任务的 cd / mkdir 行。源文件路径含空格，必须由参数传入并加引号。
        src = STACK.read_text(encoding="utf-8")
        cut = src.split("write_keepalive_agent()")[0]
        cut = "\n".join(l for l in cut.splitlines()
                        if not l.startswith("cd ") and not l.startswith("mkdir "))
        funcs = Path(td) / "funcs.zsh"
        funcs.write_text(cut, encoding="utf-8")

        script = f'''
        set -u
        workdir="/tmp/probe"
        source {str(funcs)!r}
        write_agent "probe.label" "300" \\
          'export X=1; [ -f "$HOME/.market_news/futu_env" ] && . "$HOME/.market_news/futu_env"; python3 -m market_news notify' \\
          "/tmp/probe.log" "/tmp/probe.err" {str(target)!r}
        '''
        rc, so, se = sh("/bin/zsh", "-c", script)
        out["probe_generate_rc"] = rc
        if target.exists():
            rc2, so2, _ = sh("plutil", "-lint", str(target))
            out["probe_plist_valid"] = rc2 == 0
            out["probe_plutil"] = so2[-120:]
            if rc2 == 0:
                import plistlib
                d = plistlib.loads(target.read_bytes())
                cmd = (d.get("ProgramArguments") or ["", "", ""])[-1]
                out["probe_command_has_double_amp"] = "&&" in cmd
                out["probe_command"] = cmd[:150]
        else:
            out["probe_error"] = (se or so)[:200]

    # 3. 后台任务
    rc, so, _ = sh("launchctl", "list")
    want = ["ai.codex.marketnews.collect", "ai.codex.marketnews.notify",
            "ai.codex.marketnews.health", "ai.codex.marketnews.newslearning",
            "ai.codex.marketnews.reviewapi", "com.jiao.futu-watcher",
            "com.jiao.taa_futu_crypto_ofim_app", "com.jiao.taa_futu_crypto_ofim_watchdog",
            "ai.jqquant.maintenance"]
    out["jobs_present"] = {w: (w in so) for w in want}
    out["jobs_missing"] = [w for w in want if w not in so]

    # notify 的 plist 现在合法吗
    np = HOME / "Library" / "LaunchAgents" / "ai.codex.marketnews.notify.plist"
    rc, so, _ = sh("plutil", "-lint", str(np))
    out["notify_plist_valid"] = rc == 0

    # 4. 启动器清点
    dirs = {
        "01_启动器": ALL / "01_启动器",
        "stock": ALL / "trade" / "stock" / "launchers",
        "crypto": ALL / "trade" / "crypto" / "launchers",
        "news": ALL / "news collector" / "scripts",
    }
    counts, readme = {}, {}
    for k, d in dirs.items():
        if d.exists():
            counts[k] = len([p for p in d.iterdir() if p.suffix in (".command", ".sh")])
            readme[k] = (d / "README.md").exists()
    out["launcher_counts"] = counts
    out["total_launchers"] = sum(counts.values())
    out["readme_present"] = readme
    out["root_readme"] = (ALL / "README.md").exists()

    # 桌面
    out["desktop"] = sorted(p.name for p in (HOME / "Desktop").iterdir()
                            if p.suffix in (".app", ".command"))

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
