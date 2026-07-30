"""Session-wide pytest fixtures.

CRITICAL: keep test runs from writing into the user's real ``runtime/``
folder. On 2026-06-28 a pytest run added 44 spurious events to
``runtime/stock_events.jsonl`` because the stock_events module read its
path at import time and tests had to monkeypatch ``STOCK_EVENTS_FILE``
case-by-case (some did, some didn't).

This fixture sets ``STOCK_EVENTS_FILE_OVERRIDE`` to a tmp file for the
whole pytest session, and re-imports the module so the new path takes
effect. Production daemons leave the env var unset.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def _redirect_stock_events_to_tmp():
    """Point STOCK_EVENTS_FILE at a tmp file for the entire pytest session."""
    tmpdir = Path(tempfile.mkdtemp(prefix="taa_futu_pytest_events_"))
    tmpfile = tmpdir / "stock_events.jsonl"
    os.environ["STOCK_EVENTS_FILE_OVERRIDE"] = str(tmpfile)

    # If stock_events is already imported, patch its module-level constant
    # so existing references inside other modules also redirect.
    try:
        import taa_futu.stock_events as se
        se.STOCK_EVENTS_FILE = tmpfile
        # Also patch any module that imported the constant by-name at top:
        import sys
        for modname, mod in list(sys.modules.items()):
            if mod is None:
                continue
            if hasattr(mod, "STOCK_EVENTS_FILE") and getattr(mod, "STOCK_EVENTS_FILE", None) != tmpfile:
                # Only patch if this module pulled it from taa_futu.stock_events
                # (don't blindly clobber unrelated modules).
                try:
                    if str(getattr(mod, "STOCK_EVENTS_FILE")).endswith("stock_events.jsonl"):
                        setattr(mod, "STOCK_EVENTS_FILE", tmpfile)
                except Exception:
                    pass
    except ImportError:
        pass

    yield tmpfile

    # Best-effort cleanup; ignore failures because the tmpdir may already
    # be gone on Windows / parallel test runners.
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass
