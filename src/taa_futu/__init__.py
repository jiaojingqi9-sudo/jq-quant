from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import subprocess

__all__ = ["__version__", "describe_build"]

__version__ = "3.0.0"


@lru_cache(maxsize=1)
def describe_build() -> tuple[str, str, str]:
    """Return the package version plus best-effort git tag/commit metadata."""
    repo_root = Path(__file__).resolve().parents[2]
    version = __version__
    tag = f"v{version}"
    commit = "unknown"
    try:
        tag_result = subprocess.run(
            ["git", "tag", "--points-at", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        tags = [line.strip() for line in tag_result.stdout.splitlines() if line.strip()]
        if tags:
            tag = tags[0]

        commit_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if commit_result.stdout.strip():
            commit = commit_result.stdout.strip()
    except Exception:
        pass
    return version, tag, commit
