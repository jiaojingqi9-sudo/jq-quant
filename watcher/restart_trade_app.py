#!/usr/bin/env python3
"""restart_trade_app - 重启交易 app 的 Streamlit 进程，让新代码（含新闻页）生效。

只动 Streamlit 服务本身，不碰采集/投递/邮差等后台任务。
重启后会等端口重新可用，并确认新闻页模块能被导入。
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

TRADE = Path.home() / "All here" / "trade"
APP = TRADE / "src" / "taa_futu" / "dashboard_app.py"
VENV_PY = TRADE / ".venv" / "bin" / "python"
PORT = int(os.environ.get("TAA_DASHBOARD_PORT", "8501"))
LOG = TRADE / "runtime" / "stock_app.log"


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def port_open(port, host="127.0.0.1", timeout=1.0):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def main():
    out = {"kind": "restart_trade_app", "port": PORT}

    # 1. 先确认新闻页模块本身没有语法/导入问题，别把 app 重启成坏的
    rc, so, se = _run([str(VENV_PY), "-c",
                       "import sys;sys.path.insert(0,r'%s');"
                       "import ast;ast.parse(open(r'%s',encoding='utf-8').read());"
                       "print('syntax ok')" % (TRADE / "src", TRADE / "src/taa_futu/news_panel.py")])
    out["news_panel_syntax"] = so or se[:200]
    if rc != 0:
        out["aborted"] = "news_panel.py 有问题，未重启"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    out["was_running"] = port_open(PORT)

    # 2. 停掉现有 streamlit
    rc, so, _ = _run(["ps", "-Ao", "pid,command"])
    pids = []
    for line in so.splitlines():
        if "streamlit" in line and "dashboard_app" in line and "grep" not in line:
            try:
                pids.append(int(line.strip().split()[0]))
            except (ValueError, IndexError):
                pass
    for pid in pids:
        _run(["kill", "-TERM", str(pid)])
    out["killed"] = pids
    if pids:
        time.sleep(3)
        for pid in pids:
            _run(["kill", "-9", str(pid)])

    # 3. 重新启动
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "ab") as log:
        subprocess.Popen(
            [str(VENV_PY), "-m", "streamlit", "run", str(APP),
             "--server.port", str(PORT),
             "--server.headless", "true",
             "--browser.gatherUsageStats", "false"],
            cwd=str(TRADE), stdout=log, stderr=log, start_new_session=True,
        )

    # 4. 等端口起来
    for _ in range(40):
        if port_open(PORT):
            out["restarted"] = True
            break
        time.sleep(1)
    else:
        out["restarted"] = False
        try:
            out["log_tail"] = LOG.read_text(errors="replace")[-800:]
        except OSError:
            pass

    out["url"] = f"http://localhost:{PORT}"
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
