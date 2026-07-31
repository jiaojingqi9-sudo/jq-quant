#!/usr/bin/env python3
"""make_screenshots - 演示模式起 app，用 Chrome 调试协议截各页面整页图。

为什么不用 `chrome --headless --screenshot`：那条路配合 --virtual-time-budget
时，虚拟时间会跳过 websocket 的真实往返，而 Streamlit 的内容全靠 websocket
推过来——截出来的是灰色骨架占位，不是页面。实测过，确实如此。

改用调试协议（CDP）：导航之后不断问页面"正文有多少字、还有没有骨架元素"，
真的等到内容出现再截，并且用 captureBeyondViewport 截整页而不是一屏。

只依赖 websockets（已在 pyproject 的 dependencies 里）。若当前解释器没有它，
自动换成 trade/.venv 的 python 重跑一次。

页面用 ?view=xxx 直接定位，不模拟点击。演示数据是固定种子生成的，所以重复跑
截出来的图一致。
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
TRADE = HOME / "All here" / "trade"
OUT = TRADE / "docs" / "screenshots"
APP_PORT = 8597
CDP_PORT = 9333
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# (view, 文件名, 最多等几秒)
SHOTS = [
    ("home",          "01-首页.png",      45),
    ("stock",         "02-股票交易.png", 150),
    ("news",          "03-市场新闻.png",  60),
    ("crypto",        "04-加密交易.png",  60),
    ("screener",      "05-选股器.png",    45),
    ("stock_history", "06-历史模拟.png",  60),
]


def _reexec_with_venv():
    """当前解释器没有 websockets 就换 trade 的 venv 重跑。"""
    venv = TRADE / ".venv" / "bin" / "python"
    if not venv.exists() or Path(sys.executable).resolve() == venv.resolve():
        return False
    os.execv(str(venv), [str(venv), str(Path(__file__).resolve())])
    return True


try:
    import websockets  # noqa: F401
    from websockets.sync.client import connect as ws_connect
except Exception:
    if not _reexec_with_venv():
        print(json.dumps({"kind": "make_screenshots",
                          "error": "缺少 websockets，且找不到 trade/.venv/bin/python"},
                         ensure_ascii=False, indent=2))
        raise SystemExit(1)


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


class Tab:
    """一条 CDP 连接。只用到导航、执行 JS、截图三件事。"""

    def __init__(self, ws_url: str):
        self.ws = ws_connect(ws_url, max_size=200 * 1024 * 1024, open_timeout=20)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 60):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv(timeout=max(1, deadline - time.time()))
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(method)

    def js(self, expr: str, timeout: float = 30):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True,
                       "awaitPromise": False}, timeout=timeout)
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# 判断页面渲染完没有：正文字数够多、没有骨架占位、且连续几次字数不再变化。
#
# 新闻页的看板是用 components.html 嵌进来的 iframe，document.body.innerText
# 看不到里面的内容——只按主文档算字数，新闻页会永远判定为"没画完"。所以把
# 同源 iframe 的正文也加进来（跨域会抛异常，忽略即可）。
PROBE = """
(() => {
  const skeleton = document.querySelectorAll(
    '[data-testid="stSkeleton"], .stSkeleton, [class*="skeleton"]').length;
  const spinner = document.querySelectorAll('[data-testid="stSpinner"]').length;
  let text = (document.body ? document.body.innerText : "") || "";
  let frames = 0;
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const d = f.contentDocument;
      if (d && d.body) { text += d.body.innerText || ""; frames++; }
    } catch (e) { /* 跨域，看不了就算了 */ }
  }
  return {len: text.length, skeleton, spinner, frames,
          h: document.body ? document.body.scrollHeight : 0};
})()
"""


def wait_ready(tab: Tab, budget_s: int) -> dict:
    """等到内容出现且稳定。返回最后一次探测结果，供排查用。"""
    deadline = time.time() + budget_s
    stable = 0
    last_len = -1
    probe = {}
    while time.time() < deadline:
        try:
            probe = tab.js(PROBE) or {}
        except Exception:
            time.sleep(1)
            continue
        length = int(probe.get("len") or 0)
        busy = int(probe.get("skeleton") or 0) + int(probe.get("spinner") or 0)
        if length > 400 and busy == 0:
            stable = stable + 1 if length == last_len else 0
            if stable >= 3:              # 连续三次字数不变，认为画完了
                break
        last_len = length
        time.sleep(1)
    time.sleep(1.5)                       # 给图表最后一点绘制时间
    return probe


def main() -> int:
    out = {"kind": "make_screenshots", "shots": []}
    if not Path(CHROME).exists():
        out["error"] = "找不到 Google Chrome"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    py = TRADE / ".venv" / "bin" / "python"

    env = dict(os.environ)
    env["JQ_DEMO"] = "1"
    env["JQ_NEWS_ROOT"] = str(TRADE / "demo_data" / "news")
    env["PYTHONPATH"] = str(TRADE / "src")

    app_proc = None
    if port_open(APP_PORT):
        out["reused_app"] = True
    else:
        log = TRADE / "runtime" / "screenshot_server.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        app_proc = subprocess.Popen(
            [str(py), "-m", "streamlit", "run",
             str(TRADE / "src" / "taa_futu" / "dashboard_app.py"),
             "--server.port", str(APP_PORT), "--server.headless", "true",
             "--browser.gatherUsageStats", "false"],
            env=env, stdout=open(log, "ab"), stderr=subprocess.STDOUT,
            start_new_session=True)
        for _ in range(60):
            if port_open(APP_PORT):
                break
            time.sleep(1)
        else:
            out["error"] = f"app 启动超时，日志见 {log}"
            app_proc.terminate()
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 1

    profile = TRADE / "runtime" / "shot_profile"
    shutil.rmtree(profile, ignore_errors=True)
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--no-first-run", "--no-default-browser-check",
         f"--user-data-dir={profile}",
         f"--remote-debugging-port={CDP_PORT}",
         "--window-size=1680,1050",
         "--force-device-scale-factor=2",          # 2 倍图，README 里清晰
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        for _ in range(40):
            if port_open(CDP_PORT):
                break
            time.sleep(0.5)
        else:
            out["error"] = "Chrome 调试端口没起来"
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 1

        # 复用启动时那个 about:blank 标签页，每次 Page.navigate 过去。
        # 不走 /json/new：新版 Chrome 要求那个接口用 PUT，而且没必要开一堆标签。
        targets = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json", timeout=20).read())
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not pages:
            out["error"] = "Chrome 里没有可用的标签页"
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 1
        ws_url = pages[0]["webSocketDebuggerUrl"]

        for view, filename, budget in SHOTS:
            info = {"view": view, "file": filename}
            tab = None
            try:
                url = f"http://127.0.0.1:{APP_PORT}/?view={view}"
                tab = Tab(ws_url)
                tab.call("Page.enable")
                tab.call("Runtime.enable")
                # 每次都换一个 session id，避免上一页的 Streamlit 会话把
                # view 状态带过来（session_state 里 _view_from_url_applied
                # 一旦置位，URL 就不再生效）。
                tab.call("Network.enable")
                tab.call("Network.clearBrowserCookies")
                tab.call("Page.navigate", {"url": "about:blank"})
                time.sleep(0.6)
                tab.call("Page.navigate", {"url": url})
                probe = wait_ready(tab, budget)
                info["text_len"] = probe.get("len")
                info["page_height"] = probe.get("h")

                shot = tab.call("Page.captureScreenshot",
                                {"format": "png", "captureBeyondViewport": True},
                                timeout=90)
                import base64
                target = OUT / filename
                target.write_bytes(base64.b64decode(shot["data"]))
                info["kb"] = round(target.stat().st_size / 1024)
                info["ok"] = target.stat().st_size > 20_000 and (probe.get("len") or 0) > 400
            except Exception as exc:
                info["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                info["ok"] = False
            finally:
                if tab:
                    tab.close()
            out["shots"].append(info)
    finally:
        for proc in (chrome, app_proc):
            if proc is None:
                continue
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        shutil.rmtree(profile, ignore_errors=True)

    out["output_dir"] = str(OUT)
    out["all_ok"] = all(s.get("ok") for s in out["shots"])
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
