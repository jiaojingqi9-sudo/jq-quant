"""All Here 交易总控 — native desktop wrapper around the Streamlit dashboard.

User asked for a real desktop app, not "open it in Chrome at localhost:8501".
We get the best of both worlds by:

1. Spawning a hidden Streamlit server in the background (the same
   ``dashboard_app.py`` the browser launcher uses — one source of truth for
   the UI).
2. Wrapping the resulting ``http://localhost:<port>`` in a native macOS
   window via ``pywebview`` (which uses WebKit/Cocoa under the hood).
3. Tearing down the Streamlit child process when the window closes.

To the user this looks like a normal macOS application — Cmd-Q to quit, no
URL bar, no tab strip, no Chrome window at all. Internally it's still the
same Streamlit code base so all our changes to dashboard_app /
dashboard_extras / crypto_ofim_app render exactly the same in the app and
in a browser (useful if the WebKit window ever fails: ``--browser`` fallback
opens the URL in the user's default browser instead).
"""

from __future__ import annotations

import argparse
import atexit
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from typing import Optional


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
DASHBOARD_PATH = HERE / "dashboard_app.py"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

# Use a dedicated port (8505) so the desktop-app session does not collide
# with a browser-based ``Open_Trading_Dashboard.command`` that already owns
# 8501. Both can run side-by-side.
DEFAULT_PORT = 8505
WINDOW_TITLE = "All Here 交易总控 / Trading Hub"
WINDOW_WIDTH = 1400
WINDOW_HEIGHT = 900

# Global handle to the streamlit subprocess so atexit can clean up.
_streamlit_proc: Optional[subprocess.Popen] = None


def _socket_ready(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _start_streamlit(port: int) -> subprocess.Popen:
    """Launch ``streamlit run dashboard_app.py`` as a background process.

    Streamlit's own logs go to a file under runtime/ so the desktop window
    stays uncluttered. We pass ``--server.headless true`` to suppress the
    auto-browser-open Streamlit normally does (we open our own native window).
    """
    runtime_dir = PROJECT_ROOT / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = runtime_dir / "unified_app_streamlit.log"

    python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    cmd = [
        python,
        "-m", "streamlit", "run",
        str(DASHBOARD_PATH),
        "--server.port", str(port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    env = os.environ.copy()
    pythonpath_parts = [str(PROJECT_ROOT / "src")]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

    log_fh = log_path.open("a", encoding="utf-8")
    log_fh.write(f"\n=== unified_app spawn streamlit @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    log_fh.flush()

    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=str(PROJECT_ROOT),
        # Detach into its own process group so Ctrl-C in our terminal does
        # not double-kill it before we get a chance to clean up.
        start_new_session=True,
    )
    return proc


def _wait_for_streamlit(host: str, port: int, timeout: float = 30.0) -> bool:
    """Poll the streamlit port until it accepts a connection or ``timeout``
    expires. Returns True iff streamlit came up in time.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _socket_ready(host, port):
            return True
        # Streamlit could have died — short-circuit instead of spinning.
        if _streamlit_proc is not None and _streamlit_proc.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def _cleanup_streamlit() -> None:
    """SIGTERM then SIGKILL the streamlit child. Idempotent."""
    global _streamlit_proc
    if _streamlit_proc is None:
        return
    if _streamlit_proc.poll() is not None:
        _streamlit_proc = None
        return
    try:
        # Kill the whole process group so streamlit's worker threads die too.
        os.killpg(os.getpgid(_streamlit_proc.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        _streamlit_proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(_streamlit_proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    _streamlit_proc = None


def _open_in_default_browser(url: str) -> None:
    """Fallback when pywebview is unavailable or the WebKit window won't
    start (rare; usually only on bare Linux without GTK)."""
    print(f"⚠ pywebview unavailable — falling back to system browser at {url}")
    webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Launch the All Here trading hub as a native desktop window."
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--width", type=int, default=WINDOW_WIDTH)
    parser.add_argument("--height", type=int, default=WINDOW_HEIGHT)
    parser.add_argument(
        "--browser", action="store_true",
        help="Skip the native window and open the URL in the default browser.",
    )
    parser.add_argument(
        "--no-spawn", action="store_true",
        help="Assume Streamlit is already running on --port; just open the window.",
    )
    args = parser.parse_args(argv)

    host = "127.0.0.1"
    url = f"http://localhost:{args.port}"

    global _streamlit_proc

    if not args.no_spawn:
        if _socket_ready(host, args.port):
            print(f"✓ Streamlit already running on {args.port} — reusing it.")
        else:
            print(f"→ Spawning Streamlit on {host}:{args.port} …")
            _streamlit_proc = _start_streamlit(args.port)
            atexit.register(_cleanup_streamlit)
            if not _wait_for_streamlit(host, args.port, timeout=45):
                print(
                    f"❌ Streamlit failed to come up on {host}:{args.port} within 45s. "
                    f"See {PROJECT_ROOT / 'runtime' / 'unified_app_streamlit.log'} for details."
                )
                _cleanup_streamlit()
                return 1
            print(f"✓ Streamlit ready on {url}")
    else:
        if not _socket_ready(host, args.port):
            print(f"❌ --no-spawn requested but nothing is listening on {host}:{args.port}.")
            return 1

    if args.browser:
        _open_in_default_browser(url)
        return 0

    try:
        import webview  # type: ignore
    except ImportError:
        print("⚠ pywebview not installed — pip install pywebview, or use --browser.")
        _open_in_default_browser(url)
        return 0

    # Build the native window. The text_select=True allows copying values
    # from tables (e.g. Live-Signal results) without disabling all input.
    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=args.width,
        height=args.height,
        resizable=True,
        text_select=True,
        confirm_close=False,
    )

    # When the window closes, tear down Streamlit. atexit also covers
    # Cmd-Q / kill cases; this explicit handler is a belt-and-suspenders.
    def _on_closing() -> None:
        _cleanup_streamlit()

    try:
        window.events.closing += _on_closing
    except Exception:
        # Older pywebview versions may not expose events.closing the same way.
        pass

    try:
        # ``http_server=False`` because Streamlit is the http server; we just
        # render its pages inside the WebKit view.
        webview.start(http_server=False)
    except Exception as exc:  # pragma: no cover - GUI launch failure
        print(f"⚠ pywebview start failed: {exc}")
        print("→ Falling back to the system browser.")
        _open_in_default_browser(url)
        # Keep streamlit alive long enough for the user's browser to pick it
        # up; the atexit handler will clean it later if this process exits.
        try:
            while _streamlit_proc and _streamlit_proc.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
