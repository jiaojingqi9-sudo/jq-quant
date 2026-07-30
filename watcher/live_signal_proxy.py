#!/usr/bin/env python3
"""Bridge the Futu watcher queue to ``taa-futu live-signal``.

The watcher itself runs under whichever Python launchd starts it with
(currently ``/opt/anaconda3/bin/python3``). The ``trade`` project has its own
virtualenv with a pinned ``futu-api==10.0.6008`` and the rest of the
``taa_futu`` package. This proxy is intentionally minimal so it works even when
the watcher's Python is missing ``futu``: it parses the task JSON, locates the
trade venv, then ``subprocess`` calls ``taa-futu live-signal`` and returns the
JSON payload upstream.

The watcher invokes this with a single ``--task-json <json>`` argument so we do
not have to extend ``build_cmd`` for variadic symbol lists.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


HOME = Path.home()
TRADE_DIR = HOME / "All here" / "trade"
DEFAULT_VENV_PYTHON = TRADE_DIR / ".venv" / "bin" / "python"
DEFAULT_TIMEOUT_SECONDS = 120


def _resolve_venv_python() -> Path:
    override = os.environ.get("TRADE_VENV_PYTHON")
    if override:
        path = Path(override).expanduser()
        if path.exists():
            return path
    return DEFAULT_VENV_PYTHON


def main() -> int:
    p = argparse.ArgumentParser(
        description="Run `taa-futu live-signal` from inside the futu_watcher queue.",
    )
    p.add_argument("--task-json", required=False, default="",
                   help="The watcher's task dict serialised as JSON.")
    p.add_argument("--symbol", action="append", default=[],
                   help="Legacy fallback when --task-json is not provided.")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args, _ = p.parse_known_args()

    task: dict = {}
    if args.task_json:
        try:
            task = json.loads(args.task_json)
        except json.JSONDecodeError as exc:
            print(json.dumps({
                "ok": False,
                "error": f"invalid --task-json: {exc}",
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }, ensure_ascii=False))
            return 0

    # Collect symbols from either the task or --symbol fallback
    symbols: list[str] = []
    raw = task.get("symbols")
    if isinstance(raw, list):
        symbols.extend(str(s).strip() for s in raw if str(s).strip())
    one = task.get("symbol")
    if one:
        symbols.append(str(one).strip())
    symbols.extend(args.symbol or [])
    # Dedupe while preserving order
    seen = set()
    deduped: list[str] = []
    for s in symbols:
        if s and s not in seen:
            deduped.append(s)
            seen.add(s)
    symbols = deduped

    no_universe = bool(task.get("no_universe") or task.get("compact") or False)

    venv_py = _resolve_venv_python()
    if not venv_py.exists():
        print(json.dumps({
            "ok": False,
            "error": (
                f"trade venv python not found at {venv_py}. "
                "Set TRADE_VENV_PYTHON or run `uv venv --python 3.11 .venv` "
                "in ~/All here/trade first."
            ),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False))
        return 0

    cmd = [str(venv_py), "-m", "taa_futu.cli", "live-signal", "--json"]
    if no_universe:
        cmd.append("--no-universe")
    for sym in symbols:
        cmd.extend(["--symbol", sym])

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            cwd=str(TRADE_DIR),
        )
    except subprocess.TimeoutExpired as exc:
        print(json.dumps({
            "ok": False,
            "error": f"live-signal timed out after {args.timeout}s",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False))
        return 0
    except FileNotFoundError as exc:
        print(json.dumps({
            "ok": False,
            "error": f"failed to launch live-signal: {exc}",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False))
        return 0

    if completed.returncode != 0:
        # surface stderr to the queue result so the user can see the failure
        print(json.dumps({
            "ok": False,
            "error": (completed.stderr or "live-signal exited non-zero").strip(),
            "stdout": completed.stdout.strip(),
            "returncode": completed.returncode,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False))
        return 0

    # The CLI already emits a JSON document; pipe it straight through so the
    # watcher's result writer can parse it. Wrap any non-JSON output safely.
    stdout = completed.stdout.strip()
    try:
        json.loads(stdout)  # validate
        sys.stdout.write(stdout)
        sys.stdout.write("\n")
    except json.JSONDecodeError:
        print(json.dumps({
            "ok": True,
            "data": {"raw": stdout},
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
