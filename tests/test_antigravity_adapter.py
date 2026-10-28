from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pac_neon.adapters.antigravity_adapter import AntigravityAdapter
from pac_neon.models import AgentGameState


class TestAntigravityAdapter(unittest.TestCase):
    """
    Objective: Validate that AntigravityAdapter accurately discovers ConnectRPC endpoints,
    parses real brain transcripts + live RPC trajectories, extracts real repositories/branches,
    calculates real context & hallucination metrics, and executes native RPC interactions
    (StartCascade, SendUserCascadeMessage, HandleCascadeUserInteraction, CancelCascadeInvocation,
    UpdateConversationAnnotations) without side effects.
    """

    def setUp(self):
        AntigravityAdapter._RPC_CACHE = (0.0, [])
        AntigravityAdapter._CASCADE_ENDPOINT_MAP.clear()
        AntigravityAdapter._LIVE_TRAJ_CACHE = (0.0, {})

    def test_discover_rpc_endpoints_parses_ps_lsof_and_verifies_endpoint(self):
        fake_ps = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "dev 12345 0.0 0.1 100 100 ?? S 10:00AM 0:01.00 "
                "/Applications/Antigravity.app/Contents/Resources/app/extensions/antigravity/bin/"
                "language_server_macos_arm --csrf_token a1b2c3d4-9999\n"
            ),
            stderr="",
        )
        fake_lsof = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\nlanguage_ 12345 dev 10u IPv4 0x1 0t0 TCP 127.0.0.1:54321 (LISTEN)\n",
            stderr="",
        )
        fake_http_resp = MagicMock()
        fake_http_resp.status = 200
        fake_http_resp.__enter__.return_value = fake_http_resp

        with patch("subprocess.run", side_effect=[fake_ps, fake_lsof]), patch(
            "urllib.request.urlopen", return_value=fake_http_resp
        ):
            eps = AntigravityAdapter._discover_rpc_endpoints(force=True)
            self.assertEqual(eps, [(54321, "a1b2c3d4-9999")])
            # Alias _discover_servers must return the same cached list
            self.assertEqual(AntigravityAdapter._discover_servers(force=False), [(54321, "a1b2c3d4-9999")])

    def test_call_rpc_and_get_all_live_trajectories(self):
        AntigravityAdapter._RPC_CACHE = (time.time(), [(54321, "a1b2c3d4-9999")])
        sample_rpc_resp = {
            "trajectorySummaries": {
                "cascade-1111": {
                    "summary": "Orquestrador Visual De Agentes",
                    "status": "CASCADE_RUN_STATUS_RUNNING",
                    "stepCount": 42,
                    "workspaces": [{"workspaceFolderAbsoluteUri": "file:///Users/dev/Projects/pac-neon"}],
                }
            }
        }
        with patch.object(AntigravityAdapter, "_post_rpc_endpoint", return_value=sample_rpc_resp):
            res = AntigravityAdapter.call_rpc("GetAllCascadeTrajectories", {})
            self.assertIsNotNone(res)
            trajs = AntigravityAdapter.get_all_live_trajectories()
            self.assertIn("cascade-1111", trajs)
            self.assertEqual(trajs["cascade-1111"]["summary"], "Orquestrador Visual De Agentes")
            self.assertEqual(AntigravityAdapter._CASCADE_ENDPOINT_MAP.get("cascade-1111"), (54321, "a1b2c3d4-9999"))

    def test_send_and_cancel_cascade_invocation(self):
        AntigravityAdapter._RPC_CACHE = (time.time(), [(54321, "a1b2c3d4-9999")])
        AntigravityAdapter._CASCADE_ENDPOINT_MAP["cascade-1111"] = (54321, "a1b2c3d4-9999")

        with patch.object(
            AntigravityAdapter,
            "_post_rpc_endpoint",
            return_value={"ok": True},
        ) as rpc_mock:
            sent = AntigravityAdapter.send_cascade_message("cascade-1111", "Execute os testes unitários")
            self.assertTrue(sent)

            cancelled = AntigravityAdapter.cancel_cascade_invocation("cascade-1111")
            self.assertTrue(cancelled)
            methods_called = [call.args[2] for call in rpc_mock.call_args_list]
            self.assertIn("SendUserCascadeMessage", methods_called)
            self.assertIn("CancelCascadeInvocation", methods_called)

    def test_create_cascade_and_resolve_cascade_interaction(self):
        AntigravityAdapter._RPC_CACHE = (time.time(), [(54321, "a1b2c3d4-9999")])

        def fake_post(port, token, method, payload, timeout=4.0):
            if method == "GetAllCascadeTrajectories":
                return {"trajectorySummaries": {}}
            if method == "AddTrackedWorkspace":
                return {"ok": True}
            if method == "StartCascade":
                return {"cascadeId": "new-cascade-uuid-9876"}
            if method == "UpdateConversationAnnotations":
                return {"ok": True}
            if method == "SendUserCascadeMessage":
                return {"ok": True}
            if method == "HandleCascadeUserInteraction":
                return {"ok": True}
            return {}

        with patch.object(AntigravityAdapter, "_post_rpc_endpoint", side_effect=fake_post):
            created = AntigravityAdapter.create_cascade(
                repo_path=str(PROJECT_ROOT),
                title="Missão Teste",
                initial_prompt="Inicie auditoria",
            )
            self.assertTrue(created["ok"])
            self.assertEqual(created["cascade_id"], "new-cascade-uuid-9876")
            self.assertEqual(created["title"], "Missão Teste")
            self.assertTrue(created["prompt_sent"])

            # Resolve waiting command interaction (ALLOW and ALWAYS)
            with patch.object(
                AntigravityAdapter,
                "get_live_trajectory",
                return_value={
                    "trajectory": {
                        "steps": [
                            {
                                "status": "CORTEX_STEP_STATUS_WAITING",
                                "runCommand": {"commandLine": "pytest -v"},
                            }
                        ]
                    }
                },
            ):
                res_allow = AntigravityAdapter.resolve_cascade_interaction("new-cascade-uuid-9876", "allow")
                self.assertTrue(res_allow["ok"])
                self.assertIn("HandleCascadeUserInteraction", res_allow["method"])

                res_always = AntigravityAdapter.resolve_cascade_interaction("new-cascade-uuid-9876", "always")
                self.assertTrue(res_always["ok"])

    def test_parse_conversation_from_brain_transcript_and_waiting_feedback(self):
        adapter = AntigravityAdapter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            conv_dir = Path(tmp_dir) / "11112222-3333-4444-5555-666677778888"
            logs_dir = conv_dir / ".system_generated" / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            (conv_dir / "implementation_plan.md").write_text("# Plano de Ação Refatoração\n", encoding="utf-8")

            transcript_file = logs_dir / "transcript.jsonl"
            events = [
                {
                    "type": "USER_INPUT",
                    "content": f"Crie um plano de ação para o projeto {PROJECT_ROOT}",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "thinking": "Analisando os arquivos principais do repositório pac-neon...",
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "write_to_file",
                            "arguments": {
                                "TargetFile": str(conv_dir / "implementation_plan.md"),
                                "ArtifactMetadata": {"RequestFeedback": True, "UserFacing": True},
                            },
                        }
                    ],
                },
                {
                    "type": "TOOL_RESPONSE",
                    "content": "Requested user feedback on artifact implementation_plan.md",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "thinking": "Plano pronto.",
                    "content": "Aqui está o plano proposto para sua revisão.",
                },
            ]
            transcript_file.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

            unit = adapter._parse_conversation(
                conv_dir,
                transcript_file,
                time.time(),
                {"status": "CASCADE_RUN_STATUS_IDLE", "stepCount": 4},
            )
            self.assertIsNotNone(unit)
            self.assertEqual(unit.provider, "antigravity")
            self.assertEqual(unit.game_state, AgentGameState.POWER_PELLET_WAIT)
            self.assertIsNotNone(unit.pending_permission)
            self.assertEqual(unit.pending_permission.channel, "sdk")
            self.assertGreaterEqual(unit.context_health_pct, 75)
            self.assertLess(unit.hallucination_risk_pct, 25)


if __name__ == "__main__":
    unittest.main()
