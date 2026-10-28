from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pac_neon.cli as cli_mod
import pac_neon.hooks.pac_neon_hook as hook_mod


class TestCliAndHooks(unittest.TestCase):
    """
    Objective: Validate that the `pac-neon` CLI lifecycle commands (start, stop, status,
    install, doctor, uninstall, wrap) and the non-blocking hook forwarder operate safely
    and deterministically without hardcoded paths or side effects.
    """

    def test_pac_neon_hook_forwards_json_and_never_blocks(self):
        payload = {"session_id": "s-123", "hook_event_name": "PreToolUse", "tool_name": "Bash"}
        fake_stdin = io.StringIO(json.dumps(payload))
        fake_stdin.isatty = lambda: False  # type: ignore[attr-defined]
        with patch.object(sys, "stdin", fake_stdin), patch("subprocess.Popen") as popen_mock:
            code = hook_mod.main()
            self.assertEqual(code, 0)
            popen_mock.assert_called_once()
            curl_args = popen_mock.call_args[0][0]
            self.assertEqual(curl_args[0], "curl")
            self.assertIn("http://127.0.0.1:8765/api/hooks/agent-event", curl_args)
            sent_data = json.loads(curl_args[curl_args.index("-d") + 1])
            self.assertEqual(sent_data["session_id"], "s-123")
            self.assertEqual(sent_data["tool_name"], "Bash")

    def test_cli_is_server_alive_and_daemon_lifecycle(self):
        # 1. ensure_daemon_and_open when already running
        with patch.object(cli_mod, "_is_server_alive", return_value=True), patch(
            "webbrowser.open"
        ) as wb_mock:
            ret = cli_mod.ensure_daemon_and_open(open_browser=True)
            self.assertEqual(ret, 0)
            wb_mock.assert_called_once_with(cli_mod.PAC_NEON_URL, new=0, autoraise=True)

        # 2. stop_daemon terminates PIDs on port 8765
        fake_lsof = subprocess.CompletedProcess(args=[], returncode=0, stdout="999991\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            Path, "home", return_value=Path(tmp_dir)
        ), patch("subprocess.run", return_value=fake_lsof), patch("os.kill") as kill_mock:
            code = cli_mod.stop_daemon()
            self.assertEqual(code, 0)
            kill_mock.assert_called_once_with(999991, 15)

    def test_cli_zero_touch_installer_and_uninstaller_in_isolated_temp_home(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            with patch.object(Path, "home", return_value=tmp_home), patch("subprocess.run"), patch.object(
                cli_mod, "_is_server_alive", return_value=True
            ):
                code = cli_mod.install_auto_triggers(install_launch_agent=True, open_browser=False)
                self.assertEqual(code, 0)

                plist_path = cli_mod._get_plist_path()
                app_bundle = tmp_home / "Applications" / "Pac-Neon.app"
                local_bin = tmp_home / ".local" / "bin" / "pac-neon"
                claude_settings = tmp_home / ".claude" / "settings.json"

                self.assertTrue(plist_path.exists())
                self.assertTrue(app_bundle.exists())
                self.assertTrue(local_bin.exists())
                self.assertTrue(claude_settings.exists())

                plist_text = plist_path.read_text(encoding="utf-8")
                self.assertIn("com.neon.pac-neon", plist_text)
                self.assertIn("--no-open", plist_text)

                # Test clean uninstall removes LaunchAgent, app bundle, and local symlink
                with patch.object(cli_mod, "stop_daemon", return_value=0):
                    un_code = cli_mod.uninstall_pac_neon()
                    self.assertEqual(un_code, 0)
                    self.assertFalse(plist_path.exists())
                    self.assertFalse(app_bundle.exists())
                    self.assertFalse(local_bin.exists())

    def test_cli_doctor_and_main_subcommands(self):
        fake_report = {
            "ok": True,
            "ready_score_pct": 100,
            "passed_checks": 7,
            "total_checks": 7,
            "user": "dev",
            "home": "/Users/dev",
            "default_repo_path": str(PROJECT_ROOT),
            "discovered_repos_count": 5,
            "checks": [
                {"ok": True, "title": "Servidor Pac-Neon", "detail": "Ativo", "fix_cmd": None},
            ],
        }
        with patch("pac_neon.server.run_environment_doctor", return_value=fake_report):
            code = cli_mod.run_cli_doctor()
            self.assertEqual(code, 0)
            code_json = cli_mod.run_cli_doctor(as_json=True)
            self.assertEqual(code_json, 0)

        # Test CLI main subcommand routing ('status', 'stop', 'show-hook-config')
        with patch.object(sys, "argv", ["pac-neon", "status"]), patch.object(
            cli_mod, "_is_server_alive", return_value=True
        ):
            self.assertEqual(cli_mod.main(), 0)

        with patch.object(sys, "argv", ["pac-neon", "stop"]), patch.object(
            cli_mod, "stop_daemon", return_value=0
        ):
            self.assertEqual(cli_mod.main(), 0)

        with patch.object(sys, "argv", ["pac-neon", "show-hook-config"]):
            self.assertEqual(cli_mod.main(), 0)


if __name__ == "__main__":
    unittest.main()
