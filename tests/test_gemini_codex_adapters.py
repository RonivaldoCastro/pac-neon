from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pac_neon.adapters.gemini_codex_adapters import CodexAdapter, GeminiAdapter
from pac_neon.models import AgentGameState


class TestGeminiAndCodexAdapters(unittest.TestCase):
    """
    Objective: Validate that GeminiAdapter and CodexAdapter accurately discover real sessions,
    parse nested/flat events, detect pending approvals (`POWER_PELLET_WAIT`), and populate
    `PermissionRequestItem` and anti-hallucination telemetry.
    """

    def test_gemini_adapter_discovers_session_and_approval_wait(self):
        adapter = GeminiAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            chats_dir = tmp_home / ".gemini" / "tmp" / "proj-hash" / "chats"
            chats_dir.mkdir(parents=True, exist_ok=True)
            session_file = chats_dir / "session-2026-10-28-abcd1234.json"

            session_data = {
                "sessionId": "abcd1234-9999",
                "cwd": str(PROJECT_ROOT),
                "messages": [
                    {"type": "user", "content": "Verifique os recursos do Terraform"},
                    {
                        "type": "gemini",
                        "thoughts": [{"description": "Analisando plano Terraform"}],
                        "content": "Posso aplicar o plano?",
                        "toolCalls": [
                            {
                                "name": "run_shell_command",
                                "status": "waiting_confirmation",
                                "args": {"command": "terraform apply"},
                            }
                        ],
                    },
                ],
            }
            session_file.write_text(json.dumps(session_data), encoding="utf-8")

            with patch.object(Path, "home", return_value=tmp_home), patch(
                "pac_neon.adapters.gemini_codex_adapters._scan_live_cli_processes",
                return_value=[{"pid": 8881, "tty": "ttys004", "cwd": str(PROJECT_ROOT), "host_app": "Terminal"}],
            ):
                units = adapter.discover_live_agents()
                self.assertEqual(len(units), 1)
                u = units[0]
                self.assertEqual(u.provider, "gemini")
                self.assertEqual(u.game_state, AgentGameState.POWER_PELLET_WAIT)
                self.assertIsNotNone(u.pending_permission)
                self.assertEqual(u.pending_permission.tool_name, "run_shell_command")
                self.assertEqual(u.pid, 8881)

    def test_codex_adapter_discovers_nested_payload_rollout_and_permission_details(self):
        adapter = CodexAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            sessions_dir = tmp_home / ".codex" / "sessions" / "2026" / "10" / "28"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            rollout_file = sessions_dir / "rollout-2026-10-28-codex999.jsonl"

            events = [
                {"type": "session_meta", "payload": {"id": "codex999", "cwd": str(PROJECT_ROOT)}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Atualize o Dockerfile"}],
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "exec_approval_request",
                        "message": "docker build -t pac-neon .",
                    },
                },
            ]
            rollout_file.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

            with patch.object(Path, "home", return_value=tmp_home), patch(
                "pac_neon.adapters.gemini_codex_adapters._scan_live_cli_processes",
                return_value=[{"pid": 9992, "tty": "ttys005", "cwd": str(PROJECT_ROOT), "host_app": "iTerm"}],
            ):
                units = adapter.discover_live_agents()
                self.assertEqual(len(units), 1)
                u = units[0]
                self.assertEqual(u.provider, "codex")
                self.assertEqual(u.game_state, AgentGameState.POWER_PELLET_WAIT)
                self.assertIsNotNone(u.pending_permission)
                self.assertEqual(u.pending_permission.full_details.get("cwd"), str(PROJECT_ROOT))
                self.assertEqual(u.pid, 9992)
                self.assertEqual(u.host_app, "iTerm")


if __name__ == "__main__":
    unittest.main()
