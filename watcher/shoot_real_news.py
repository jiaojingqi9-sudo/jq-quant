#!/usr/bin/env python3
"""shoot_real_news - 截真实运行中的 app（8501）的新闻页，验证嵌入样式已生效。

和 make_screenshots 的区别：那个是在演示模式下另起一个实例截图，这个直接打
用户正在用的 8501，用来确认改动在真实环境里确实生效了。
"""
import base64
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

TRADE = Path.home() / "All here" / "trade"
OUT = TRADE / "runtime" / "real_news.png"
APP_PORT = 8501
CDP_PORT = 9335
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

try:
    from websockets.sync.client import connect as ws_connect
except Exception:
    venv = TRADE / ".venv" / "bin" / "python"
    if venv.exists() and Path(sys.executable).resolve() != venv.resolve():
        os.execv(str(venv), [str(venv), str(Path(__file__).resolve())])
    raise


def port_open(p):
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=1):
            return True
    except OSError:
        return False


class Tab:
    def __init__(self, url):
        self.ws = ws_connect(url, max_size=200 * 1024 * 1024, open_timeout=20)
        self._id = 0

    def call(self, method, params=None, timeout=90):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv(timeout=max(1, end - time.time())))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"])[:150])
                return msg.get("result", {})
        raise TimeoutError(method)


PROBE = """
(() => {
  let text = (document.body ? document.body.innerText : "") || "";
  let frames = 0, hero = 0, embed = 0;
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const d = f.contentDocument;
      if (d && d.body) {
        frames++;
        text += d.body.innerText || "";
        hero += d.querySelectorAll('section.hero').length;
        embed += d.querySelectorAll('style[data-jq-embed]').length;
      }
    } catch (e) {}
  }
  const busy = document.querySelectorAll(
    '[data-testid="stSkeleton"],[data-testid="stSpinner"]').length;
  return {len: text.length, frames, hero_count: hero,
          embed_style_present: embed > 0, busy};
})()
"""


def main():
    out = {"kind": "shoot_real_news", "app_running": port_open(APP_PORT)}
    if not out["app_running"]:
        out["error"] = "8501 没在跑"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    profile = TRADE / "runtime" / "shot_profile_real"
    shutil.rmtree(profile, ignore_errors=True)
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--no-first-run", "--no-default-browser-check",
         f"--user-data-dir={profile}", f"--remote-debugging-port={CDP_PORT}",
         "--window-size=1680,1050", "--force-device-scale-factor=2", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(40):
            if port_open(CDP_PORT):
                break
            time.sleep(0.5)
        pages = [t for t in json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json", timeout=20).read())
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        tab = Tab(pages[0]["webSocketDebuggerUrl"])
        tab.call("Page.enable")
        tab.call("Runtime.enable")
        tab.call("Page.navigate", {"url": f"http://127.0.0.1:{APP_PORT}/?view=news"})

        probe, stable, last = {}, 0, -1
        end = time.time() + 90
        while time.time() < end:
            r = tab.call("Runtime.evaluate",
                         {"expression": PROBE, "returnByValue": True})
            probe = r.get("result", {}).get("value") or {}
            n = int(probe.get("len") or 0)
            if n > 400 and not probe.get("busy"):
                stable = stable + 1 if n == last else 0
                if stable >= 3:
                    break
            last = n
            time.sleep(1)
        time.sleep(1.5)
        out["probe"] = probe

        shot = tab.call("Page.captureScreenshot",
                        {"format": "png", "captureBeyondViewport": True})
        OUT.write_bytes(base64.b64decode(shot["data"]))
        out["file"] = str(OUT)
        out["kb"] = round(OUT.stat().st_size / 1024)
    finally:
        try:
            os.killpg(os.getpgid(chrome.pid), signal.SIGTERM)
        except Exception:
            chrome.terminate()
        shutil.rmtree(profile, ignore_errors=True)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
