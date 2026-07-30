from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from market_news.infrastructure.notifications.openclaw import OpenClawNotifier


class OpenClawNotifierTest(unittest.TestCase):
    def test_stale_workspace_error_triggers_forced_gateway_restart(self) -> None:
        notifier = OpenClawNotifier(
            binary_path=Path("/usr/bin/true"),
            config_path=Path("/Users/jiao/.openclaw/openclaw.json"),
        )
        stale_error = subprocess.CompletedProcess(
            args=["openclaw", "message", "send"],
            returncode=1,
            stdout="",
            stderr="Error: ENOENT: no such file or directory, mkdir '/home/node'",
        )
        ok = subprocess.CompletedProcess(
            args=["openclaw", "message", "send"],
            returncode=0,
            stdout="OpenClaw accepted the message.",
            stderr="",
        )

        with patch.object(notifier, "_run_send", side_effect=[stale_error, ok]) as run_send:
            with patch.object(notifier, "_ensure_gateway_running") as ensure_gateway:
                result = notifier.send(
                    channel="whatsapp",
                    target="+85259908875",
                    message="hello",
                )

        self.assertEqual(result, "OpenClaw accepted the message.")
        self.assertEqual(run_send.call_count, 2)
        ensure_gateway.assert_called_once_with(force=True)

    def test_stale_workspace_detector_matches_home_node_paths(self) -> None:
        notifier = OpenClawNotifier(
            binary_path=Path("/usr/bin/true"),
            config_path=Path("/Users/jiao/.openclaw/openclaw.json"),
        )
        completed = subprocess.CompletedProcess(
            args=["openclaw", "message", "send"],
            returncode=1,
            stdout="",
            stderr="mkdir '/home/node'",
        )
        self.assertTrue(notifier._looks_stale_workspace_error(completed))

    def test_listener_error_detector_matches_whatsapp_listener_errors(self) -> None:
        notifier = OpenClawNotifier(
            binary_path=Path("/usr/bin/true"),
            config_path=Path("/Users/jiao/.openclaw/openclaw.json"),
        )
        completed = subprocess.CompletedProcess(
            args=["openclaw", "message", "send"],
            returncode=1,
            stdout="",
            stderr="Error: No active WhatsApp Web listener (account: default).",
        )
        self.assertTrue(notifier._looks_listener_error(completed))


if __name__ == "__main__":
    unittest.main()
