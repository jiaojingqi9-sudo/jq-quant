from __future__ import annotations

import json
from pathlib import Path
import subprocess


class OpenClawNotifier:
    def __init__(
        self,
        *,
        binary_path: Path | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.binary_path = (binary_path or Path.home() / ".openclaw" / "bin" / "openclaw").expanduser()
        self.config_path = (config_path or Path.home() / ".openclaw" / "openclaw.json").expanduser()

    def resolve_target(self, channel: str, explicit_target: str | None = None) -> str:
        if explicit_target:
            return explicit_target
        config = self._load_config()
        channel_config = config.get("channels", {}).get(channel, {})
        allow_from = channel_config.get("allowFrom", [])
        if allow_from:
            return str(allow_from[0])
        raise ValueError(
            f"No target provided and no `{channel}` allowlist target found in {self.config_path}."
        )

    def send(self, *, channel: str, target: str, message: str) -> str:
        if not self.binary_path.exists():
            raise FileNotFoundError(f"OpenClaw binary not found: {self.binary_path}")

        command = [
            str(self.binary_path),
            "message",
            "send",
            "--channel",
            channel,
            "--target",
            target,
            "--message",
            message,
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            if "gateway" in detail.lower() and "unreachable" in detail.lower():
                detail += " | Start OpenClaw first with `~/.openclaw/bin/openclaw gateway`."
            raise RuntimeError(detail)
        return completed.stdout.strip() or "OpenClaw accepted the message."

    def _load_config(self) -> dict[str, object]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"OpenClaw config not found: {self.config_path}")
        return json.loads(self.config_path.read_text(encoding="utf-8"))
