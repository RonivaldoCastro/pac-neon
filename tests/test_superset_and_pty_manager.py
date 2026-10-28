from __future__ import annotations

import asyncio
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pac_neon.models import AgentGameState, AgentUnit, PermissionRequestItem, PermissionResolutionAction
from pac_neon.pty_manager import ManagedPTYSession, PTYSessionManager, strip_ansi
from pac_neon.superset_bridge import SupersetBridge


class TestSupersetBridgeAndPTYManager(unittest.TestCase):
    """
    Objective: Validate that SupersetBridge accurately discovers Superset worktrees/terminals,
    sends terminal keystrokes (including distinct ALLOW 'y', ALWAYS '2', DENY 'n'), opens deep links,
    and that PTYSessionManager intercepts terminal output to detect permission prompts and inject commands.
    """

    def test_superset_host_status_installed_and_not_installed(self):
        bridge = SupersetBridge()
        with patch.object(bridge, "is_installed", return_value=False):
            status = bridge.get_host_status()
            self.assertFalse(status["installed"])
            self.assertFalse(status["running"])
            self.assertEqual(status["mode"], "native_hooks_and_antigravity")

        with patch.object(bridge, "is_installed", return_value=True), patch.object(
            bridge, "_run_cli_json", return_value={"running": True, "healthy": True, "pid": 4321, "port": 51234}
        ):
            bridge.cli_bin = "/usr/local/bin/superset"
            live_status = bridge.get_host_status()
            self.assertTrue(live_status["installed"])
            self.assertTrue(live_status["running"])
            self.assertTrue(live_status["healthy"])
            self.assertEqual(live_status["mode"], "superset_live")
            self.assertEqual(live_status["pid"], 4321)

    def test_superset_terminal_read_send_and_permission_resolution(self):
        bridge = SupersetBridge()
        bridge.cli_bin = "/usr/local/bin/superset"

        # 1. Read terminal screen strips ANSI and caches output
        with patch.object(
            bridge,
            "_run_cli_json",
            return_value={"text": "\x1b[32mclaude >\x1b[0m Allow Bash tool? [y/n]:"},
        ):
            screen = bridge.read_terminal_screen("ws_1", "term_1")
            self.assertEqual(screen, "claude > Allow Bash tool? [y/n]:")

        # Fallback to cache when CLI returns None
        with patch.object(bridge, "_run_cli_json", return_value=None):
            cached = bridge.read_terminal_screen("ws_1", "term_1")
            self.assertEqual(cached, "claude > Allow Bash tool? [y/n]:")

        # 2. Send to terminal invokes superset CLI
        mock_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with patch.object(bridge, "is_installed", return_value=True), patch(
            "subprocess.run", return_value=mock_proc
        ) as run_mock:
            ok = bridge.send_to_terminal("ws_1", "term_1", text="y")
            self.assertTrue(ok)
            called_cmd = run_mock.call_args[0][0]
            self.assertIn("--text", called_cmd)
            self.assertIn("y", called_cmd)
            self.assertIn("ws_1:term_1", bridge._resolved_perm_keys)

            ok_keys = bridge.send_to_terminal("ws_1", "term_1", keys="Enter")
            self.assertTrue(ok_keys)
            self.assertIn("--keys", run_mock.call_args[0][0])

        # 3. Resolve permission maps ALLOW -> 'y', ALWAYS -> '2', DENY -> 'n', custom_text -> custom
        with patch.object(bridge, "send_to_terminal", return_value=True) as mock_send:
            bridge.resolve_permission_in_superset("ws_1", "term_1", PermissionResolutionAction.ALLOW)
            mock_send.assert_called_with("ws_1", "term_1", text="y")

            bridge.resolve_permission_in_superset("ws_1", "term_1", PermissionResolutionAction.ALWAYS)
            mock_send.assert_called_with("ws_1", "term_1", text="2")

            bridge.resolve_permission_in_superset("ws_1", "term_1", PermissionResolutionAction.DENY)
            mock_send.assert_called_with("ws_1", "term_1", text="n")

            bridge.resolve_permission_in_superset(
                "ws_1", "term_1", PermissionResolutionAction.CUSTOM, custom_text="custom instruction"
            )
            mock_send.assert_called_with("ws_1", "term_1", text="custom instruction")

    def test_superset_spawn_and_deep_link_ui_open(self):
        bridge = SupersetBridge()
        with patch.object(
            bridge, "_run_cli_json", return_value={"sessionId": "term_99", "workspaceId": "ws_99"}
        ) as cli_mock:
            res = bridge.spawn_agent_in_workspace(
                "ws_99", agent_preset="claude", prompt="Fix tests", model="sonnet", effort="high"
            )
            self.assertEqual(res["sessionId"], "term_99")
            args_passed = cli_mock.call_args[0][0]
            self.assertIn("--model", args_passed)
            self.assertIn("--effort", args_passed)

        with patch("subprocess.Popen") as popen_mock:
            link1 = bridge.open_in_superset_desktop("ws_99", "term_99")
            self.assertEqual(link1, "superset://v2-workspace/ws_99?terminalId=term_99")
            popen_mock.assert_called_with(["open", link1])

            link2 = bridge.open_workspace_in_superset_ui("ws_99")
            self.assertEqual(link2, "superset://v2-workspace/ws_99")

    def test_pty_manager_ansi_stripping_and_permission_interception(self):
        raw = "\x1b[1;33m⚡ Claude wants to run:\x1b[0m rm -rf build/\nDo you want to proceed? [y/n]"
        clean = strip_ansi(raw)
        self.assertNotIn("\x1b", clean)
        self.assertTrue(ManagedPTYSession._looks_like_permission_prompt(clean))

        agent = AgentUnit(
            id="claude-pty01",
            name="Claude PTY",
            provider="claude",
            repo_name="pac-neon",
            repo_path=str(PROJECT_ROOT),
        )
        updates: list[AgentUnit] = []

        session = ManagedPTYSession(
            unit=agent,
            command=["echo", "hello"],
            cwd=str(PROJECT_ROOT),
            on_state_change=lambda ag: updates.append(ag),
        )
        session.master_fd = 999
        agent.pending_permission = PermissionRequestItem(
            agent_id=agent.id,
            agent_name=agent.name,
            provider=agent.provider,
            repo_name=agent.repo_name,
            command_preview="rm -rf build/",
        )
        agent.game_state = AgentGameState.POWER_PELLET_WAIT

        with patch("os.write", return_value=2) as write_mock, patch("os.close"):
            resolved = session.resolve_permission(PermissionResolutionAction.ALLOW)
            self.assertTrue(resolved)
            self.assertEqual(agent.game_state, AgentGameState.CHOMPING)
            self.assertIsNone(agent.pending_permission)
            write_mock.assert_called_once_with(999, b"y\r")
            session.terminate()

    def test_pty_session_manager_spawn_and_outbox_queue(self):
        async def _run():
            mgr = PTYSessionManager()
            agent = AgentUnit(
                id="gemini-pty02",
                name="Gemini PTY",
                provider="gemini",
                repo_name="pac-neon",
                repo_path=str(PROJECT_ROOT),
            )
            with patch.object(ManagedPTYSession, "start", return_value=None):
                sess = await mgr.spawn_pty_agent(
                    unit=agent,
                    command_str="gemini --model pro",
                )
                self.assertIs(mgr.sessions.get(agent.id), sess)

            # Fallback outbox queue when master_fd is not open
            ok = mgr.send_command(agent, "pytest -q", auto_enter=True)
            self.assertTrue(ok)
            queued = mgr.pop_queued_commands(agent.id)
            self.assertEqual(queued, ["pytest -q"])
            self.assertEqual(mgr.pop_queued_commands(agent.id), [])

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
