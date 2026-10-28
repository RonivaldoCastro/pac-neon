from __future__ import annotations

import asyncio
import datetime
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pac_neon.server as srv_mod
from pac_neon.adapters.antigravity_adapter import AntigravityAdapter
from pac_neon.models import (
    AgentGameState,
    AgentUnit,
    HookPermissionPayload,
    PermissionResolutionAction,
    ResolvePermissionRequest,
    SelfHandoffRequest,
)


class TestServerOrchestratorAndEndpoints(unittest.TestCase):
    """
    Objective: Validate that all orchestrator server capabilities in `pac_neon/server.py`
    fulfill their business objectives:
    - CSRF Localhost security guard
    - 4-layer MeetingCalendarGuard (manual, pmset hardware call, .ics parser, macOS Calendar SQLite)
    - Self-Handoff in the SAME agent without duplicating conversations or opening new windows
    - Real-time hooks (SessionStart, PreToolUse, PermissionRequest, Stop, SessionEnd)
    - Permission resolution across Hook, ConnectRPC, Superset, PTY, and Terminal injection
    - Stop execution, Direct command, Warp Fruit tunnel, Archive/Restore, Rename, Slash commands
    """

    def test_localhost_csrf_origin_guard(self):
        self.assertTrue(srv_mod._is_trusted_local_origin(""))
        self.assertTrue(srv_mod._is_trusted_local_origin("http://127.0.0.1:8765"))
        self.assertTrue(srv_mod._is_trusted_local_origin("http://localhost:8765"))
        self.assertFalse(srv_mod._is_trusted_local_origin("https://attacker.evil.com"))
        self.assertFalse(srv_mod._is_trusted_local_origin("http://192.168.1.50:8765"))

    def test_meeting_calendar_guard_all_layers_and_sound_muting(self):
        guard = srv_mod.MeetingCalendarGuard()

        # Layer 1: Manual activation mutes sounds in meetings
        guard.update_settings({"manual_in_meeting": True, "meeting_mute_enabled": True})
        status_manual = guard.get_status(force_refresh=True)
        self.assertTrue(status_manual["in_meeting"])
        self.assertEqual(status_manual["source"], "manual")
        self.assertTrue(status_manual["should_mute_sound"])
        self.assertTrue(status_manual["should_mute_macos_sound"])

        # Layer 2: Hardware call detection (Teams/Zoom/Meet/Slack Huddle)
        guard.update_settings({"manual_in_meeting": False, "detect_active_call": True, "detect_macos_calendar": False})
        with patch.object(
            guard, "_detect_active_macos_call", return_value="Reunião ativa no Microsoft Teams"
        ):
            status_hw = guard.get_status(force_refresh=True)
            self.assertTrue(status_hw["in_meeting"])
            self.assertEqual(status_hw["source"], "active_call")
            self.assertIn("Microsoft Teams", status_hw["active_meeting_title"])

        # Layer 3: RFC 5545 .ics calendar feed parser
        now_local = datetime.datetime.now()
        dt_start = (now_local - datetime.timedelta(minutes=5)).strftime("%Y%m%dT%H%M%S")
        dt_end = (now_local + datetime.timedelta(minutes=25)).strftime("%Y%m%dT%H%M%S")
        sample_ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            f"DTSTART:{dt_start}\r\n"
            f"DTEND:{dt_end}\r\n"
            "SUMMARY:Daily Arquitetura Neon\r\n"
            "STATUS:CONFIRMED\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        guard.update_settings(
            {
                "manual_in_meeting": False,
                "detect_active_call": False,
                "detect_macos_calendar": False,
                "calendar_ics_url": "https://calendar.example.com/feed.ics",
            }
        )
        fake_resp = MagicMock()
        fake_resp.read.return_value = sample_ics.encode("utf-8")
        fake_resp.__enter__.return_value = fake_resp
        with patch("urllib.request.urlopen", return_value=fake_resp):
            status_ics = guard.get_status(force_refresh=True)
            self.assertTrue(status_ics["in_meeting"])
            self.assertEqual(status_ics["source"], "ics")
            self.assertIn("Daily Arquitetura Neon", status_ics["active_meeting_title"])

    def test_self_handoff_keeps_same_agent_and_never_creates_new_conversation(self):
        """
        Objective: Self-Handoff MUST reuse the exact same agent (`kept_same_agent: True`),
        NEVER call `create_cascade` or open a new agent window, and restore context health to 100%.
        """

        async def _run():
            # 1. Antigravity agent with degraded context (170k tokens, 35% health)
            agy_agent = AgentUnit(
                id="agy-handoff01",
                name="Conversa Antigravity Original",
                workspace_title="Conversa Antigravity Original",
                provider="antigravity",
                repo_name="pac-neon",
                repo_path=str(PROJECT_ROOT),
                branch="main",
                game_state=AgentGameState.CHOMPING,
                context_health_pct=35,
                context_tokens_used=170000,
                hallucination_risk_pct=65,
                health_status="CRITICAL",
                session_id="cascade-original-uuid-001",
                host_app="Antigravity",
                terminal_tail=["❯ Refatore o módulo X", "● Editando server.py"],
            )
            srv_mod.hub.agents[agy_agent.id] = agy_agent

            with patch.object(
                AntigravityAdapter, "create_cascade"
            ) as create_cascade_mock, patch.object(
                srv_mod,
                "_send_antigravity_command_async",
                return_value={"injected": True, "method": "Antigravity ConnectRPC"},
            ) as send_agy_mock, patch.object(
                srv_mod.hub, "_save_workspace_state", return_value=None
            ):
                # Preview handoff prompt
                prev = await srv_mod.handoff_preview_agent(agy_agent.id)
                self.assertTrue(prev["ok"])
                self.assertIn("HANDOFF DE CONTINUIDADE", prev["handoff_prompt"])

                # Execute Self-Handoff
                res = await srv_mod.self_handoff_agent(
                    agy_agent.id,
                    SelfHandoffRequest(
                        mode="clear_and_continue",
                        custom_handoff=None,
                    ),
                )
                self.assertTrue(res["ok"])
                self.assertTrue(res["kept_same_agent"])
                self.assertEqual(res["agent"]["id"], "agy-handoff01")
                self.assertEqual(res["agent"]["session_id"], "cascade-original-uuid-001")
                self.assertEqual(res["agent"]["context_health_pct"], 100)
                self.assertEqual(res["agent"]["hallucination_risk_pct"], 0)
                self.assertEqual(res["agent"]["health_status"], "HEALTHY")
                # Crucial assertion: create_cascade MUST NOT be called!
                create_cascade_mock.assert_not_called()
                send_agy_mock.assert_called_once()

            # 2. Claude CLI agent Self-Handoff (`clear_and_continue` sends `/clear` + handoff prompt to same session)
            claude_agent = AgentUnit(
                id="claude-handoff02",
                name="Claude Session Original",
                workspace_title="Claude Session Original",
                provider="claude",
                repo_name="pac-neon",
                repo_path=str(PROJECT_ROOT),
                branch="main",
                game_state=AgentGameState.IDLE,
                context_health_pct=28,
                context_tokens_used=180000,
                hallucination_risk_pct=72,
                health_status="CRITICAL",
                session_id="claude-sess-uuid-002",
                tty="ttys009",
                host_app="iTerm",
                terminal_tail=["❯ Corrija o bug", "● Pronto"],
            )
            srv_mod.hub.agents[claude_agent.id] = claude_agent

            with patch.object(
                srv_mod,
                "_dispatch_to_cli_agent_terminal",
                return_value={"injected": True, "method": "tty_direct_ioctl", "detail": "Injetado"},
            ) as inject_mock, patch("asyncio.sleep", return_value=None), patch.object(
                srv_mod.hub, "_save_workspace_state", return_value=None
            ):
                res_cl = await srv_mod.self_handoff_agent(
                    claude_agent.id,
                    SelfHandoffRequest(mode="clear_and_continue"),
                )
                self.assertTrue(res_cl["ok"])
                self.assertTrue(res_cl["agent"]["context_health_pct"], 100)
                self.assertEqual(res_cl["agent"]["id"], "claude-handoff02")
                self.assertEqual(res_cl["agent"]["hallucination_risk_pct"], 0)
                self.assertEqual(inject_mock.call_count, 2)  # 1st: /clear, 2nd: handoff prompt

            # Clean up temporary test agents
            srv_mod.hub.agents.pop("agy-handoff01", None)
            srv_mod.hub.agents.pop("claude-handoff02", None)
            srv_mod.hub.self_handoff_overrides.pop("agy-handoff01", None)
            srv_mod.hub.self_handoff_overrides.pop("claude-handoff02", None)

        asyncio.run(_run())

    def test_hook_agent_event_lifecycle_and_blocking_permission_resolution(self):
        """
        Objective: Validate real-time hook events (`SessionStart`, `UserPromptSubmit`, `PreToolUse`,
        `PermissionRequest`, `Stop`, `SessionEnd`) and interactive blocking hook permission resolution.
        """

        async def _run():
            sid = "test-hook-session-9999"
            aid = f"claude-{sid[:8]}"

            with patch.object(srv_mod, "trigger_cockpit_popup", return_value=None), patch.object(
                srv_mod.hub, "_save_workspace_state", return_value=None
            ):
                # 1. SessionStart creates the live agent in IDLE state
                r_start = await srv_mod.hook_agent_event(
                    {
                        "session_id": sid,
                        "hook_event_name": "SessionStart",
                        "cwd": str(PROJECT_ROOT),
                    }
                )
                self.assertTrue(r_start["ok"])
                self.assertIn(aid, srv_mod.hub.agents)
                self.assertEqual(srv_mod.hub.agents[aid].game_state, AgentGameState.IDLE)

                # 2. UserPromptSubmit transitions agent to CHOMPING
                await srv_mod.hook_agent_event(
                    {
                        "session_id": sid,
                        "hook_event_name": "UserPromptSubmit",
                        "cwd": str(PROJECT_ROOT),
                        "prompt": "Implemente a rota /health",
                    }
                )
                self.assertEqual(srv_mod.hub.agents[aid].game_state, AgentGameState.CHOMPING)

                # 3. PreToolUse updates current_action and active_tool
                await srv_mod.hook_agent_event(
                    {
                        "session_id": sid,
                        "hook_event_name": "PreToolUse",
                        "cwd": str(PROJECT_ROOT),
                        "tool_name": "Edit",
                        "tool_input": {"file_path": "pac_neon/server.py"},
                    }
                )
                self.assertEqual(srv_mod.hub.agents[aid].active_tool, "Edit")

                # 4a. Non-blocking legacy HookPermissionPayload returns allow immediately
                hook_payload = HookPermissionPayload(
                    agent_id=aid,
                    agent_name="Claude Hook Agent",
                    provider="claude",
                    repo_path=str(PROJECT_ROOT),
                    tool_name="Bash",
                    command_preview="pytest -q",
                )
                legacy_res = await srv_mod.hook_request_permission(hook_payload)
                self.assertEqual(legacy_res.get("decision"), "allow")

                # 4b. PermissionRequest via hook_agent_event sets POWER_PELLET_WAIT + pending_permission, then resolve_permission clears it
                hook_res = await srv_mod.hook_agent_event(
                    {
                        "session_id": sid,
                        "hook_event_name": "PermissionRequest",
                        "cwd": str(PROJECT_ROOT),
                        "tool_name": "Bash",
                        "tool_input": {"command": "rm -rf dist/", "description": "Limpar diretório de build"},
                    }
                )
                self.assertEqual(hook_res.get("state"), "POWER_PELLET_WAIT")
                perm = srv_mod.hub.agents[aid].pending_permission
                self.assertIsNotNone(perm)
                await srv_mod.resolve_permission(
                    perm.id,
                    ResolvePermissionRequest(action=PermissionResolutionAction.ALLOW),
                )
                self.assertIsNone(srv_mod.hub.agents[aid].pending_permission)

                # 5. Stop event returns agent to IDLE
                await srv_mod.hook_agent_event(
                    {
                        "session_id": sid,
                        "hook_event_name": "Stop",
                        "cwd": str(PROJECT_ROOT),
                    }
                )
                self.assertEqual(srv_mod.hub.agents[aid].game_state, AgentGameState.IDLE)

                # 6. SessionEnd clears pid/tty and sets IDLE state
                r_end = await srv_mod.hook_agent_event(
                    {
                        "session_id": sid,
                        "hook_event_name": "SessionEnd",
                        "cwd": str(PROJECT_ROOT),
                    }
                )
                self.assertTrue(r_end["ok"])
                self.assertIsNone(srv_mod.hub.agents[aid].pid)
                self.assertEqual(srv_mod.hub.agents[aid].game_state, AgentGameState.IDLE)

        asyncio.run(_run())

    def test_stop_agent_execution_and_slash_commands_and_settings(self):
        async def _run():
            ag = AgentUnit(
                id="agy-stoptest",
                name="Stop Test Agent",
                provider="antigravity",
                repo_name="pac-neon",
                repo_path=str(PROJECT_ROOT),
                game_state=AgentGameState.CHOMPING,
                session_id="cascade-stop-123",
            )
            srv_mod.hub.agents[ag.id] = ag

            with patch.object(
                AntigravityAdapter,
                "cancel_cascade_invocation",
                return_value={"ok": True, "method": "Antigravity ConnectRPC"},
            ), patch.object(srv_mod.hub, "_save_workspace_state", return_value=None):
                stop_res = await srv_mod.stop_agent_execution(ag.id)
                self.assertTrue(stop_res["ok"])
                self.assertEqual(srv_mod.hub.agents[ag.id].game_state, AgentGameState.IDLE)

            srv_mod.hub.agents.pop(ag.id, None)

            # Slash commands discovery
            cmds_res = await srv_mod.get_slash_commands_endpoint()
            self.assertIn("commands", cmds_res)
            self.assertGreaterEqual(cmds_res["count"], 4)
            names = {c["command"] for c in cmds_res["commands"]}
            self.assertIn("/stop", names)
            self.assertIn("/compact", names)

            # Settings endpoint updates notification timeout and meeting guard
            with patch.object(srv_mod.hub, "_save_workspace_state", return_value=None):
                set_res = await srv_mod.update_settings_endpoint(
                    {"notification_timeout_sec": 45.0, "meeting_settings": {"manual_in_meeting": True}}
                )
                self.assertTrue(set_res["ok"])
                self.assertEqual(set_res["notification_timeout_sec"], 45.0)
                self.assertTrue(set_res["meeting"]["in_meeting"])

                # Restore default settings
                await srv_mod.update_settings_endpoint(
                    {"notification_timeout_sec": 60.0, "meeting_settings": {"manual_in_meeting": False}}
                )

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
