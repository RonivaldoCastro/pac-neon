from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pac_neon.adapters import AdapterRegistry, AntigravityAdapter
from pac_neon.models import (
    AgentGameState,
    ArchiveAgentRequest,
    DirectCommandRequest,
    PermissionResolutionAction,
    RenameWorkspaceRequest,
    ResolvePermissionRequest,
    SendWarpFruitRequest,
    SpawnAgentRequest,
)
from pac_neon.server import (
    api_state,
    archive_agent_endpoint,
    rename_agent_endpoint,
    resolve_permission,
    send_agent_command,
    send_warp_fruit,
    spawn_new_agent,
    trigger_test_permission,
)
from pac_neon.superset_bridge import SupersetBridge


def test_superset_bridge_discovers_worktrees_and_blocked_permissions():
    bridge = SupersetBridge()

    mock_snapshot = {
        "workspaces": [
            {
                "id": "ws_neon_01",
                "name": "feat-pix-automatico",
                "project": str(Path.home() / "Projects" / "payments.invoice-statement"),
                "branch": "feat/pix-auto",
                "terminals": [
                    {
                        "id": "term_claude_1",
                        "title": "claude code",
                        "agent": {"id": "ag_1", "state": "working"},
                    },
                    {
                        "id": "term_gemini_2",
                        "title": "gemini cli",
                        "agent": {"id": "ag_2", "state": "blocked"},
                    },
                ],
            }
        ]
    }

    with patch.object(
        bridge, "_run_cli_json", return_value=mock_snapshot
    ), patch.object(
        bridge,
        "read_terminal_screen",
        return_value="gemini > Plan: 1 to add\nAllow execution of 'terraform apply'? [y/n]:",
    ):
        agents = bridge.discover_from_snapshot()

    assert len(agents) == 2

    claude_ag = next(a for a in agents if a.provider == "claude")
    assert claude_ag.game_state == AgentGameState.CHOMPING
    assert claude_ag.superset_workspace_id == "ws_neon_01"
    assert claude_ag.superset_terminal_id == "term_claude_1"

    gemini_ag = next(a for a in agents if a.provider == "gemini")
    assert gemini_ag.game_state == AgentGameState.POWER_PELLET_WAIT
    assert gemini_ag.pending_permission is not None
    assert gemini_ag.pending_permission.channel == "superset"
    assert "terraform apply" in gemini_ag.pending_permission.command_preview


def test_superset_bridge_sends_permission_keystroke():
    bridge = SupersetBridge()
    with patch.object(bridge, "send_to_terminal", return_value=True) as mock_send:
        ok = bridge.resolve_permission_in_superset("ws_neon_01", "term_gemini_2", PermissionResolutionAction.ALLOW)
        assert ok is True
        mock_send.assert_called_once_with("ws_neon_01", "term_gemini_2", text="y")


def test_antigravity_adapter_discovers_live_transcript():
    adapter = AntigravityAdapter()
    agents = adapter.discover_live_agents()
    assert isinstance(agents, list)
    if agents:
        assert agents[0].provider == "antigravity"
        assert agents[0].neon_color == "#FFE600"
        assert bool(agents[0].workspace_title)


def test_pluggable_adapter_registry_for_future_agents():
    spec = AdapterRegistry.ensure_provider("cursor-agent")
    assert spec.id == "cursor-agent"
    assert any(p["id"] == "cursor-agent" for p in AdapterRegistry.list_providers())


def test_anti_hallucination_and_context_telemetry_metrics():
    from pac_neon.models import compute_context_and_hallucination_metrics

    # 1. Healthy agent with balanced reads & edits
    healthy = compute_context_and_hallucination_metrics(
        tokens_used=24_000,
        tokens_limit=200_000,
        steps_count=18,
        recent_tool_errors=0,
        thrashing_count=0,
        overthinking_read_only_count=2,
        game_state=AgentGameState.CHOMPING,
        age_seconds=2.0,
        active_elapsed_sec=8,
    )
    assert healthy["health_status"] == "HEALTHY"
    assert healthy["context_health_pct"] >= 80
    assert healthy["hallucination_risk_pct"] < 25

    # 2. Loop / Thrashing detection (same tool + target repeated >= 3 times)
    loop_metrics = compute_context_and_hallucination_metrics(
        tokens_used=125_000,
        tokens_limit=200_000,
        steps_count=95,
        recent_tool_errors=3,
        last_error_target="broken.py",
        thrashing_target="broken.py",
        thrashing_count=4,
        game_state=AgentGameState.CHOMPING,
        age_seconds=5.0,
    )
    assert loop_metrics["health_status"] == "CRITICAL"
    assert loop_metrics["hallucination_risk_pct"] >= 60
    assert any("Loop detectado" in s for s in loop_metrics["health_signals"])

    # 3. Search Spiral / Overthinking detection (>= 12 reads without writes) + Stall
    spiral_metrics = compute_context_and_hallucination_metrics(
        tokens_used=165_000,
        tokens_limit=200_000,
        steps_count=160,
        recent_tool_errors=2,
        overthinking_read_only_count=14,
        game_state=AgentGameState.THINKING,
        age_seconds=95.0,
        active_elapsed_sec=130,
    )
    assert spiral_metrics["is_stalled"] is True
    assert spiral_metrics["health_status"] == "STALLED"
    assert any("Espiral de busca" in s for s in spiral_metrics["health_signals"])


def test_portable_repository_discovery_and_environment_doctor():
    from pac_neon.server import (
        _discover_user_repositories,
        _is_trusted_local_origin,
        run_environment_doctor,
    )

    repos, default_repo = _discover_user_repositories()
    assert isinstance(repos, list)
    assert len(repos) >= 1
    assert isinstance(default_repo, str) and len(default_repo) > 0

    doc = run_environment_doctor()
    assert "ready_score_pct" in doc
    assert "checks" in doc
    assert len(doc["checks"]) >= 6

    # Verify localhost CSRF origin check
    assert _is_trusted_local_origin("http://127.0.0.1:8765") is True
    assert _is_trusted_local_origin("http://localhost:8765") is True
    assert _is_trusted_local_origin("https://evil-site.example.com") is False


def test_hub_endpoints_and_permission_approval():
    from pac_neon.server import get_doctor_status_endpoint, handoff_preview_agent

    async def _run():
        data = await api_state()
        assert "agents" in data
        assert "superset" in data
        assert "user_repositories" in data
        assert "default_repo_path" in data

        doc = await get_doctor_status_endpoint()
        assert "checks" in doc

        # Ensure ZERO fake/demo agents exist
        demo_ids = {"agy-invoice", "gemini-iac", "claude-arch", "codex-bff"}
        actual_ids = {a["id"] for a in data["agents"]}
        assert demo_ids.isdisjoint(actual_ids), f"Found demo agents: {demo_ids & actual_ids}"

        # Spawn 2 test agents if fewer than 2 real agents exist in test env
        import pac_neon.server as srv_mod
        if len(data["agents"]) < 2:
            with patch.object(
                AntigravityAdapter,
                "create_cascade",
                return_value={
                    "ok": True,
                    "cascade_id": "test-cascade-0001-uuid",
                    "short_id": "test0001",
                    "repo_path": str(PROJECT_ROOT),
                    "repo_name": PROJECT_ROOT.name,
                    "title": "Orquestrador Visual De Agentes",
                },
            ), patch.object(
                srv_mod, "_focus_antigravity_app", return_value=True
            ), patch.object(
                srv_mod,
                "_spawn_in_macos_terminal_app",
                return_value={"ok": True, "tty": "/dev/ttys999", "pid": 99999},
            ):
                await spawn_new_agent(
                    SpawnAgentRequest(
                        provider="antigravity",
                        name="Orquestrador Visual De Agentes",
                        repo_path=str(PROJECT_ROOT),
                        initial_prompt="Test",
                        use_superset=False,
                    )
                )
                await spawn_new_agent(
                    SpawnAgentRequest(
                        provider="claude",
                        name="Claude Session",
                        repo_path=str(PROJECT_ROOT),
                        initial_prompt="Test 2",
                        use_superset=False,
                    )
                )
            data = await api_state()

        target_id = data["agents"][0]["id"]
        original_title = data["agents"][0].get("workspace_title") or data["agents"][0]["name"]

        # Test Handoff Preview endpoint
        preview = await handoff_preview_agent(target_id)
        assert preview["ok"] is True
        assert "handoff_prompt" in preview
        assert len(preview["handoff_prompt"]) > 20

        # Test Archive to History and Restore to Active
        arch_res = await archive_agent_endpoint(target_id, {"archived": True})
        assert arch_res["ok"] is True
        assert arch_res["agent"]["archived"] is True

        unarch_res = await archive_agent_endpoint(target_id, {"archived": False})
        assert unarch_res["ok"] is True
        assert unarch_res["agent"]["archived"] is False

        # Test Workspace Title Rename & Sync
        ren_res = await rename_agent_endpoint(
            target_id,
            {"title": original_title},
        )
        assert ren_res["ok"] is True
        assert ren_res["title"] == original_title

        trig = await trigger_test_permission(target_id)
        assert trig["ok"] is True
        perm_id = trig["permission"]["id"]

        import pac_neon.server as srv_mod
        with patch.object(
            srv_mod,
            "inject_input_into_live_terminal",
            return_value={"injected": True, "method": "mock_terminal", "detail": "Injetado em teste"},
        ):
            res = await resolve_permission(
                perm_id,
                ResolvePermissionRequest(action=PermissionResolutionAction.ALLOW),
            )
            assert res["ok"] is True

            cmd_res = await send_agent_command(
                target_id,
                DirectCommandRequest(prompt="Rode os testes de contrato REST", auto_enter=True),
            )
            assert cmd_res["ok"] is True

        second_id = data["agents"][1]["id"]
        warp_res = await send_warp_fruit(
            SendWarpFruitRequest(
                source_agent_id=target_id,
                target_agent_id=second_id,
                fruit_type="cherry_contract",
                title="pix-contract.json",
                content="POST /v1/pix/installments com header X-Idempotency-Key",
            )
        )
        assert warp_res["ok"] is True

    asyncio.run(_run())


def test_claude_adapter_discovers_waiting_for_attention_and_terminal_mirror():
    from pac_neon.adapters.claude_adapter import ClaudeAdapter
    adapter = ClaudeAdapter()
    agents = adapter.discover_live_agents()
    assert isinstance(agents, list)
    for ag in agents:
        assert isinstance(ag.terminal_tail, list)
        assert len(ag.terminal_tail) > 0


if __name__ == "__main__":
    test_superset_bridge_discovers_worktrees_and_blocked_permissions()
    print("✅ [PASS] test_superset_bridge_discovers_worktrees_and_blocked_permissions")
    test_superset_bridge_sends_permission_keystroke()
    print("✅ [PASS] test_superset_bridge_sends_permission_keystroke")
    test_antigravity_adapter_discovers_live_transcript()
    print("✅ [PASS] test_antigravity_adapter_discovers_live_transcript")
    test_pluggable_adapter_registry_for_future_agents()
    print("✅ [PASS] test_pluggable_adapter_registry_for_future_agents")
    test_anti_hallucination_and_context_telemetry_metrics()
    print("✅ [PASS] test_anti_hallucination_and_context_telemetry_metrics")
    test_portable_repository_discovery_and_environment_doctor()
    print("✅ [PASS] test_portable_repository_discovery_and_environment_doctor")
    test_claude_adapter_discovers_waiting_for_attention_and_terminal_mirror()
    print("✅ [PASS] test_claude_adapter_discovers_waiting_for_attention_and_terminal_mirror")
    test_hub_endpoints_and_permission_approval()
    print("✅ [PASS] test_hub_endpoints_and_permission_approval")
    print("🎉 ALL 8 PAC-NEON MATURITY + ANTI-HALLUCINATION + ORCHESTRATOR TESTS PASSED!")
