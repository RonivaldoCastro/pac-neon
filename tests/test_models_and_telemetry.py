from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pac_neon.models import (
    AgentGameState,
    AgentUnit,
    ArchiveAgentRequest,
    ContextFruit,
    DirectCommandRequest,
    HookPermissionPayload,
    NeonCliActionRequest,
    PermissionRequestItem,
    PermissionResolutionAction,
    ProviderMetadata,
    RenameWorkspaceRequest,
    ResolvePermissionRequest,
    SelfHandoffRequest,
    SendWarpFruitRequest,
    SpawnAgentRequest,
    compute_context_and_hallucination_metrics,
    utc_now_iso,
)


class TestModelsAndAntiHallucinationTelemetry(unittest.TestCase):
    """
    Objective: Validate that all domain models and the deterministic anti-hallucination
    & context health engine accurately reflect real agent telemetry without guessing.
    """

    def test_utc_now_iso_and_domain_models_defaults(self):
        iso = utc_now_iso()
        self.assertTrue(iso.endswith("+00:00") or iso.endswith("Z"))

        perm = PermissionRequestItem(
            id="perm-01",
            agent_id="claude-1234",
            agent_name="Claude // pac-neon",
            provider="claude",
            repo_name="pac-neon",
            tool_name="Bash",
            command_preview="pytest -v",
            reason="Executar suíte de testes",
        )
        self.assertEqual(perm.channel, "pty")
        self.assertEqual(perm.tool_name, "Bash")

        fruit = ContextFruit(
            id="fruit-01",
            fruit_type="cherry_contract",
            title="API Contract",
            content="POST /v1/payments",
            source_agent_id="agy-1",
            source_agent_name="AGY",
            source_repo="pac-neon",
            target_agent_id="claude-2",
            target_repo="payments-api",
        )
        self.assertFalse(fruit.consumed)
        self.assertEqual(fruit.fruit_type, "cherry_contract")

        meta = ProviderMetadata(
            id="antigravity",
            display_name="Antigravity",
            arcade_title="PAC-GRAVITON // AGY",
            neon_color="#0078D9",
            secondary_color="#00E5FF",
            sprite_type="pac_neon",
        )
        self.assertTrue(meta.supports_pty_wrap)
        self.assertTrue(meta.supports_hooks)

        agent = AgentUnit(
            id="agy-abcd1234",
            name="Antigravity • pac-neon",
            provider="antigravity",
            repo_name="pac-neon",
            repo_path="/Users/dev/Projects/pac-neon",
        )
        self.assertEqual(agent.game_state, AgentGameState.CHOMPING)
        self.assertEqual(agent.context_tokens_limit, 200000)
        self.assertEqual(agent.health_status, "HEALTHY")

    def test_request_payload_models_validation(self):
        r_perm = ResolvePermissionRequest(
            action=PermissionResolutionAction.CUSTOM,
            custom_text="Use staging environment first",
        )
        self.assertEqual(r_perm.action, PermissionResolutionAction.CUSTOM)

        cmd_req = DirectCommandRequest(prompt="git status", auto_enter=True)
        self.assertTrue(cmd_req.auto_enter)

        warp_req = SendWarpFruitRequest(
            source_agent_id="a1",
            target_agent_id="a2",
            title="Schema",
            content="SELECT 1;",
        )
        self.assertEqual(warp_req.fruit_type, "cherry_contract")

        handoff_req = SelfHandoffRequest()
        self.assertEqual(handoff_req.mode, "clear_and_continue")
        self.assertIsNone(handoff_req.custom_handoff)

        spawn_req = SpawnAgentRequest(provider="claude", name="Backend Agent", repo_path="~/Projects/pac-neon")
        self.assertFalse(spawn_req.use_superset)
        self.assertFalse(spawn_req.use_real_pty)

        neon_req = NeonCliActionRequest(action="dd_logs", env="stg", jira_key="PAY-123")
        self.assertEqual(neon_req.action, "dd_logs")
        self.assertEqual(neon_req.env, "stg")

        hook_req = HookPermissionPayload(agent_id="claude-1", tool_name="Edit", command_preview="server.py")
        self.assertEqual(hook_req.provider, "claude")

        arch_req = ArchiveAgentRequest(archived=True)
        self.assertTrue(arch_req.archived)

        ren_req = RenameWorkspaceRequest(title="Novo Título")
        self.assertEqual(ren_req.title, "Novo Título")
        self.assertTrue(ren_req.sync_external)

    def test_healthy_agent_telemetry_metrics(self):
        """Objective: Verify that an active agent with low token usage and zero errors is rated HEALTHY."""
        m = compute_context_and_hallucination_metrics(
            tokens_used=18_000,
            tokens_limit=200_000,
            steps_count=12,
            recent_tool_errors=0,
            thrashing_count=0,
            overthinking_read_only_count=3,
            game_state=AgentGameState.CHOMPING,
            age_seconds=3.0,
            active_elapsed_sec=15,
        )
        self.assertEqual(m["health_status"], "HEALTHY")
        self.assertGreaterEqual(m["context_health_pct"], 85)
        self.assertLess(m["hallucination_risk_pct"], 15)
        self.assertFalse(m["is_stalled"])
        self.assertEqual(m["health_signals"], [])

    def test_context_saturation_degrades_health_and_raises_risk(self):
        """Objective: Verify that exceeding 75% and 85% of the 200k token window triggers WARNING/CRITICAL alerts."""
        warn_m = compute_context_and_hallucination_metrics(
            tokens_used=152_000,  # 76%
            tokens_limit=200_000,
            steps_count=65,
            recent_tool_errors=0,
            game_state=AgentGameState.CHOMPING,
        )
        self.assertEqual(warn_m["health_status"], "WARNING")
        self.assertTrue(any("76%" in s for s in warn_m["health_signals"]))

        crit_m = compute_context_and_hallucination_metrics(
            tokens_used=180_000,  # 90%
            tokens_limit=200_000,
            steps_count=130,
            recent_tool_errors=0,
            game_state=AgentGameState.CHOMPING,
        )
        self.assertEqual(crit_m["health_status"], "CRITICAL")
        self.assertLessEqual(crit_m["context_health_pct"], 46)
        self.assertTrue(any("90%" in s for s in crit_m["health_signals"]))

    def test_loop_thrashing_distinguishes_errors_vs_repetitions(self):
        """Objective: Verify that repeated identical tool calls distinguish between repeated failures and repeated calls."""
        # Case A: Thrashing with errors
        err_loop = compute_context_and_hallucination_metrics(
            tokens_used=50_000,
            tokens_limit=200_000,
            steps_count=30,
            recent_tool_errors=3,
            last_error_target="server.py",
            thrashing_target="server.py",
            thrashing_count=4,
            game_state=AgentGameState.CHOMPING,
        )
        self.assertEqual(err_loop["health_status"], "CRITICAL")
        self.assertGreaterEqual(err_loop["hallucination_risk_pct"], 60)
        self.assertTrue(any("4 falhas seguidas em server.py" in s for s in err_loop["health_signals"]))

        # Case B: Thrashing without tool errors (repeated read/grep on same file)
        rep_loop = compute_context_and_hallucination_metrics(
            tokens_used=50_000,
            tokens_limit=200_000,
            steps_count=30,
            recent_tool_errors=0,
            thrashing_target="config.yaml",
            thrashing_count=3,
            game_state=AgentGameState.CHOMPING,
        )
        self.assertIn(rep_loop["health_status"], ("WARNING", "CRITICAL"))
        self.assertTrue(any("3 repetições seguidas em config.yaml" in s for s in rep_loop["health_signals"]))

        # Case C: Thrashing without specific target
        generic_loop = compute_context_and_hallucination_metrics(
            tokens_used=40_000,
            tokens_limit=200_000,
            steps_count=25,
            recent_tool_errors=0,
            thrashing_target="",
            thrashing_count=3,
            game_state=AgentGameState.CHOMPING,
        )
        self.assertTrue(any("3 chamadas repetidas seguidas na mesma ferramenta" in s for s in generic_loop["health_signals"]))

    def test_search_spiral_and_stalled_agent_detection(self):
        """
        Objective: Detect search spiral (>=12 read-only steps without writes) and stalled execution
        (>60s without transcript progress while CHOMPING/THINKING), while never flagging IDLE or POWER_PELLET_WAIT as stalled.
        """
        stalled = compute_context_and_hallucination_metrics(
            tokens_used=80_000,
            tokens_limit=200_000,
            steps_count=45,
            recent_tool_errors=1,
            last_error_target="db.py",
            overthinking_read_only_count=15,
            game_state=AgentGameState.THINKING,
            age_seconds=125.0,
            active_elapsed_sec=140,
            recent_interruptions=1,
        )
        self.assertTrue(stalled["is_stalled"])
        self.assertEqual(stalled["health_status"], "STALLED")
        self.assertTrue(any("Espiral de busca" in s or "IA sem resposta" in s for s in stalled["health_signals"]))

        # Waiting for user approval or IDLE must NEVER be marked as stalled even after hours
        waiting = compute_context_and_hallucination_metrics(
            tokens_used=80_000,
            tokens_limit=200_000,
            steps_count=45,
            game_state=AgentGameState.POWER_PELLET_WAIT,
            age_seconds=3600.0,
        )
        self.assertFalse(waiting["is_stalled"])
        self.assertNotEqual(waiting["health_status"], "STALLED")

        idle = compute_context_and_hallucination_metrics(
            tokens_used=20_000,
            tokens_limit=200_000,
            steps_count=10,
            game_state=AgentGameState.IDLE,
            age_seconds=7200.0,
        )
        self.assertFalse(idle["is_stalled"])
        self.assertEqual(idle["health_status"], "HEALTHY")


if __name__ == "__main__":
    unittest.main()
