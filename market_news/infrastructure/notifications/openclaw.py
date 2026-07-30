from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import socket
import time


class OpenClawNotifier:
    def __init__(
        self,
        *,
        binary_path: Path | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.home_dir = self._resolve_home_dir(config_path=config_path)
        self.binary_path = (
            binary_path or self.home_dir / ".openclaw" / "bin" / "openclaw"
        ).expanduser()
        self.config_path = (
            config_path or self.home_dir / ".openclaw" / "openclaw.json"
        ).expanduser()

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

        completed = self._run_send(channel=channel, target=target, message=message)
        if completed.returncode != 0:
            force_restart = self._looks_stale_workspace_error(completed) or self._looks_listener_error(completed)
            if force_restart or self._looks_gateway_related(completed):
                self._ensure_gateway_running(force=force_restart)
                completed = self._run_send(channel=channel, target=target, message=message)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            if "gateway" in detail.lower() and "unreachable" in detail.lower():
                detail += " | Start OpenClaw first with `~/.openclaw/bin/openclaw gateway`."
            raise RuntimeError(detail)
        return completed.stdout.strip() or "OpenClaw accepted the message."

    def _run_send(self, *, channel: str, target: str, message: str) -> subprocess.CompletedProcess[str]:
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
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=self._process_env(),
        )

    def _looks_gateway_related(self, completed: subprocess.CompletedProcess[str]) -> bool:
        detail = f"{completed.stdout}\n{completed.stderr}".lower()
        return any(
            token in detail
            for token in (
                "gateway closed",
                "gateway unreachable",
                "connection refused",
                "econnrefused",
                "1006 abnormal closure",
                "websocket",
                "no close frame",
            )
        )

    def _looks_stale_workspace_error(self, completed: subprocess.CompletedProcess[str]) -> bool:
        detail = f"{completed.stdout}\n{completed.stderr}".lower()
        return any(
            token in detail
            for token in (
                "mkdir '/home/node'",
                "mkdir \"/home/node\"",
                "/home/node/.openclaw",
                "/home/node",
            )
        )

    def _looks_listener_error(self, completed: subprocess.CompletedProcess[str]) -> bool:
        detail = f"{completed.stdout}\n{completed.stderr}".lower()
        return any(
            token in detail
            for token in (
                "no active whatsapp web listener",
                "whatsapp web listener",
                "link whatsapp",
                "listener is not active",
            )
        )

    def _ensure_gateway_running(self, *, force: bool = False) -> None:
        host, port = self._gateway_host_port()
        if not force and self._gateway_reachable(host, port):
            return

        if force:
            self._stop_gateway_processes()
            time.sleep(0.5)

        command = [
            str(self.binary_path),
            "gateway",
            "--force",
            "--bind",
            "loopback",
            "--port",
            str(port),
        ]
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=self._process_env(),
        )
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if self._gateway_reachable(host, port):
                return
            time.sleep(0.4)

    def _stop_gateway_processes(self) -> None:
        for proc_name in ("openclaw-gateway", "openclaw"):
            subprocess.run(
                ["pkill", "-x", proc_name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _gateway_host_port(self) -> tuple[str, int]:
        host = "127.0.0.1"
        port = 18789
        if not self.config_path.exists():
            return host, port
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            return host, port
        gateway = config.get("gateway", {}) if isinstance(config, dict) else {}
        if isinstance(gateway, dict):
            port = int(gateway.get("port", port) or port)
            bind = str(gateway.get("bind", "loopback") or "loopback").lower()
            if bind not in {"loopback", "localhost", "127.0.0.1"}:
                host = "0.0.0.0"
        return host, port

    def _gateway_reachable(self, host: str, port: int) -> bool:
        sock = socket.socket()
        sock.settimeout(0.25)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def _process_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(self.home_dir)
        env["USERPROFILE"] = str(self.home_dir)
        env["OPENCLAW_STATE_DIR"] = str(self.config_path.expanduser().parent)
        env["OPENCLAW_CONFIG_PATH"] = str(self.config_path.expanduser())
        return env

    def _resolve_home_dir(self, *, config_path: Path | None) -> Path:
        explicit_config_path = os.environ.get("OPENCLAW_CONFIG_PATH")
        if explicit_config_path:
            candidate = Path(explicit_config_path).expanduser()
            if candidate.parent.parent.exists():
                return candidate.parent.parent

        explicit_state_dir = os.environ.get("OPENCLAW_STATE_DIR")
        if explicit_state_dir:
            candidate = Path(explicit_state_dir).expanduser()
            if candidate.parent.exists():
                return candidate.parent

        if config_path is not None:
            candidate = Path(config_path).expanduser()
            if candidate.parent.parent.exists():
                return candidate.parent.parent

        stable_user_home = Path("/Users/jiao")
        if stable_user_home.exists():
            return stable_user_home

        return Path.home()

    def _load_config(self) -> dict[str, object]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"OpenClaw config not found: {self.config_path}")
        return json.loads(self.config_path.read_text(encoding="utf-8"))
