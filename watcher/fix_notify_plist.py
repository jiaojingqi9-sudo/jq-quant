#!/usr/bin/env python3
"""fix_notify_plist - 修好 notify 后台任务的配置文件。

问题：plist 是 XML，命令行里的 & 必须写成 &amp;。notify 的命令行含
    [ -f "$HOME/.market_news/futu_env" ] && . "$HOME/.market_news/futu_env"
那个裸 && 让文件成为非法 XML。plutil -lint 报 "unknown ampersand-escape
sequence at line 11"。

为什么现在看着没事：launchd 内存里加载的是更早那份合法版本，任务照常跑。
一旦重启或重新加载，读的是磁盘上这份坏的，加载失败，「把新闻发到手机」这
一步就此消失——和 2026 年断推 58 天是同一类静默失效。

做法：只把 <string> 里未转义的 & 补成 &amp;，其余一个字节不动。
改前备份，改后用 plutil 校验，再重新加载这一个任务。
"""
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

P = Path.home() / "Library" / "LaunchAgents" / "ai.codex.marketnews.notify.plist"
LABEL = "ai.codex.marketnews.notify"


def sh(*a, timeout=30):
    p = subprocess.run(a, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def main():
    out = {"kind": "fix_notify_plist", "path": str(P)}
    if not P.exists():
        out["error"] = "文件不存在"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    text = P.read_text(encoding="utf-8")

    rc, so, _ = sh("plutil", "-lint", str(P))
    out["before_lint_ok"] = rc == 0
    out["before_lint"] = so[:160]
    if rc == 0:
        out["result"] = "文件本来就是合法的，无需修改"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # 备份
    bak = P.with_suffix(f".plist.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(P, bak)
    out["backup"] = bak.name

    # 只补 & ——把不属于合法实体的 & 换成 &amp;。
    # 合法实体形如 &amp; &lt; &gt; &quot; &apos; &#123; &#x1F;
    entity = re.compile(r"&(?:amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);")

    fixed_count = 0

    def repl(m):
        nonlocal fixed_count
        fixed_count += 1
        return "&amp;"

    # 逐段处理：跳过已经是合法实体的部分
    pieces, last = [], 0
    for m in entity.finditer(text):
        seg = text[last:m.start()]
        pieces.append(re.sub(r"&", repl, seg))
        pieces.append(m.group(0))
        last = m.end()
    pieces.append(re.sub(r"&", repl, text[last:]))
    new_text = "".join(pieces)

    out["ampersands_escaped"] = fixed_count
    if new_text == text:
        out["error"] = "没有找到需要转义的 &，问题可能在别处"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    P.write_text(new_text, encoding="utf-8")

    rc, so, _ = sh("plutil", "-lint", str(P))
    out["after_lint_ok"] = rc == 0
    out["after_lint"] = so[:160]
    if rc != 0:
        shutil.copy2(bak, P)
        out["error"] = "改完仍不合法，已还原备份"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    # 确认解析出来的命令行没被改坏
    import plistlib
    data = plistlib.loads(P.read_bytes())
    args = data.get("ProgramArguments", [])
    out["parsed_label"] = data.get("Label")
    out["parsed_command"] = args[-1][:200] if args else None
    out["command_intact"] = bool(args) and "&&" in args[-1] and "market_news notify" in args[-1]

    # 重新加载这一个任务
    uid = str(__import__("os").getuid())
    sh("launchctl", "bootout", f"gui/{uid}", str(P))
    rc, so, se = sh("launchctl", "bootstrap", f"gui/{uid}", str(P))
    out["bootstrap_rc"] = rc
    out["bootstrap_msg"] = (so or se)[:160]

    rc, so, _ = sh("launchctl", "list", LABEL)
    out["loaded_after"] = rc == 0
    m = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', so)
    out["last_exit"] = m.group(1) if m else None

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
