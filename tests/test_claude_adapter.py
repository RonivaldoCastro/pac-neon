from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pac_neon.adapters.claude_adapter import ClaudeAdapter
from pac_neon.models import AgentGameState


class TestClaudeAdapter(unittest.TestCase):
    """
    Objective: Validate that ClaudeAdapter accurately maps numbered approval menus (1/2/3),
    resolves process trees (PID/TTY/Host IDE or Terminal), parses real `.jsonl` transcripts
    (tokens, errors, loops, waiting tools, end_turn), and initializes brand-new live processes
    with 100% context health and canonical sprite `ghost_clyde`.
    """

    def setUp(self):
        ClaudeAdapter._PID_TREE_CACHE.clear()

    def test_claude_format_approval_sequence_numbered_menu(self):
        adapter = ClaudeAdapter()
        self.assertEqual(adapter.format_approval_sequence("allow"), "y\r")
        self.assertEqual(adapter.format_approval_sequence("always"), "2\r")
        self.assertEqual(adapter.format_approval_sequence("deny"), "n\r")
        self.assertEqual(adapter.format_approval_sequence("custom", "use pytest"), "use pytest\r")

    def test_inspect_pid_tree_identifies_host_ide_and_tty(self):
        # Simulate process tree: PID 500 (claude on ttys008) -> parent 400 (zsh) -> parent 300 (IntelliJ IDEA.app)
        def fake_ps(cmd, **kwargs):
            pid_arg = cmd[-1]
            if pid_arg == "500":
                out = "400 ttys008 claude --session-id abc\n"
            elif pid_arg == "400":
                out = "300 ttys008 /bin/zsh\n"
            elif pid_arg == "300":
                out = "1 ?? /Applications/IntelliJ IDEA.app/Contents/MacOS/idea\n"
            else:
                out = "0 ?? /sbin/launchd\n"
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=out, stderr="")

        with patch("subprocess.run", side_effect=fake_ps):
            tty_val, host_app = ClaudeAdapter._inspect_pid_tree(500)
            self.assertEqual(tty_val, "ttys008")
            self.assertEqual(host_app, "IntelliJ IDEA")

    def test_parse_claude_jsonl_waiting_permission_and_end_turn(self):
        adapter = ClaudeAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            proj_dir = Path(tmp_dir) / "-Users-dev-Projects-pac-neon"
            proj_dir.mkdir(parents=True, exist_ok=True)
            jsonl_path = proj_dir / "session-11112222.jsonl"

            # Scenario A: Claude emits tool_use (Bash) without tool_result yet -> POWER_PELLET_WAIT
            lines_waiting = [
                {
                    "type": "user",
                    "cwd": str(PROJECT_ROOT),
                    "gitBranch": "feat/unit-tests",
                    "message": {"role": "user", "content": "Rode o deploy de homologação"},
                },
                {
                    "type": "assistant",
                    "cwd": str(PROJECT_ROOT),
                    "gitBranch": "feat/unit-tests",
                    "message": {
                        "role": "assistant",
                        "stop_reason": "tool_use",
                        "usage": {
                            "input_tokens": 14000,
                            "cache_read_input_tokens": 10000,
                            "output_tokens": 500,
                        },
                        "content": [
                            {"type": "thinking", "thinking": "Preciso rodar o script de deploy."},
                            {
                                "type": "tool_use",
                                "id": "toolu_01",
                                "name": "Bash",
                                "input": {"command": "./scripts/deploy_stg.sh", "description": "Deploy staging"},
                            },
                        ],
                    },
                },
            ]
            jsonl_path.write_text("\n".join(json.dumps(x) for x in lines_waiting) + "\n", encoding="utf-8")

            session_info = {
                "alive": True,
                "pid": 500,
                "cwd": str(PROJECT_ROOT),
                "tty": "ttys008",
                "host_app": "iTerm",
            }
            unit_wait = adapter._parse_claude_jsonl(proj_dir, jsonl_path, time.time() - 3.0, session_info)
            self.assertIsNotNone(unit_wait)
            self.assertEqual(unit_wait.game_state, AgentGameState.POWER_PELLET_WAIT)
            self.assertIsNotNone(unit_wait.pending_permission)
            self.assertIn("deploy_stg.sh", unit_wait.pending_permission.command_preview)
            self.assertEqual(unit_wait.context_tokens_used, 24500)
            self.assertEqual(unit_wait.host_app, "iTerm")

            # Scenario B: Assistant finishes turn (`stop_reason == "end_turn"`) -> transitions immediately to IDLE
            lines_done = lines_waiting + [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "Deploy OK"}],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 25000, "output_tokens": 300},
                        "content": [{"type": "text", "text": "Deploy concluído com sucesso!"}],
                    },
                },
            ]
            jsonl_path.write_text("\n".join(json.dumps(x) for x in lines_done) + "\n", encoding="utf-8")
            unit_idle = adapter._parse_claude_jsonl(proj_dir, jsonl_path, time.time(), session_info)
            self.assertIsNotNone(unit_idle)
            self.assertEqual(unit_idle.game_state, AgentGameState.IDLE)
            self.assertIsNone(unit_idle.pending_permission)

    def test_discover_live_agents_fallback_for_brand_new_process_without_jsonl(self):
        adapter = ClaudeAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            fake_sessions = {
                "abcd1234-5678": {
                    "alive": True,
                    "pid": 7777,
                    "cwd": str(PROJECT_ROOT),
                    "tty": "ttys012",
                    "host_app": "Terminal",
                    "started_at": time.time(),
                }
            }
            with patch.object(Path, "home", return_value=tmp_home), patch.object(
                adapter, "_load_active_claude_sessions", return_value=fake_sessions
            ):
                units = adapter.discover_live_agents()
                self.assertEqual(len(units), 1)
                ag = units[0]
                self.assertEqual(ag.id, "claude-abcd1234")
                self.assertEqual(ag.context_health_pct, 100)
                self.assertEqual(ag.context_tokens_used, 0)
                self.assertEqual(ag.hallucination_risk_pct, 0)
                self.assertEqual(ag.health_status, "HEALTHY")
                self.assertEqual(ag.sprite_type, "ghost_clyde")
                self.assertEqual(ag.score_xp, 100)


if __name__ == "__main__":
    unittest.main()
