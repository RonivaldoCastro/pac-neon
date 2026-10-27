from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional

from pac_neon.adapters.base import BaseAgentAdapter
from pac_neon.models import (
    AgentGameState,
    AgentUnit,
    PermissionRequestItem,
    ProviderMetadata,
    compute_context_and_hallucination_metrics,
)


class GeminiAdapter(BaseAgentAdapter):
    """Adapter for Google Gemini CLI reading ~/.gemini/tmp/ and PTY/Hook streams."""

    def get_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            id="gemini",
            display_name="Gemini CLI",
            arcade_title="INKY-GEMINI // FLASH-PRO",
            neon_color="#00E5FF",        # Neon Brand Electric Cyan
            secondary_color="#0066FF",
            sprite_type="ghost_inky",
            log_paths=["~/.gemini/tmp/*"],
            cli_command="gemini",
            supports_pty_wrap=True,
            supports_hooks=True,
        )

    def discover_live_agents(self, max_age_minutes: int = 1440) -> List[AgentUnit]:
        gemini_tmp = Path.home() / ".gemini" / "tmp"
        if not gemini_tmp.exists():
            return []
        units: List[AgentUnit] = []
        now = time.time()
        for session_file in sorted(
            gemini_tmp.rglob("*.json*"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )[:6]:
            try:
                st = session_file.stat()
                mtime = st.st_mtime
                if (now - mtime) > max_age_minutes * 60:
                    continue
                sid = session_file.stem[:8]
                age = now - mtime
                state = AgentGameState.CHOMPING if age < 15.0 else AgentGameState.IDLE
                term_tail: List[str] = []
                recent_errors = 0
                steps_cnt = 1
                try:
                    raw_text = session_file.read_text(encoding="utf-8", errors="replace")
                    if session_file.suffix == ".jsonl":
                        all_lines = raw_text.splitlines()
                        steps_cnt = max(1, len(all_lines))
                        for jln in all_lines[-80:]:
                            if not jln.strip():
                                continue
                            obj = json.loads(jln)
                            role = str(obj.get("role") or obj.get("type") or "")
                            txt = str(obj.get("content") or obj.get("text") or "")
                            if "error:" in txt.lower() or obj.get("is_error"):
                                recent_errors += 1
                            if txt.strip():
                                prefix = "❯ " if "user" in role.lower() else "● "
                                lines_split = [x.rstrip() for x in txt.splitlines()]
                                if lines_split:
                                    term_tail.append(f"{prefix}{lines_split[0]}")
                                    for sln in lines_split[1:]:
                                        term_tail.append(f"  {sln}")
                    else:
                        data = json.loads(raw_text)
                        msgs = data.get("messages") if isinstance(data, dict) else (data if isinstance(data, list) else [])
                        if isinstance(msgs, list):
                            steps_cnt = max(1, len(msgs))
                            for m in msgs[-50:]:
                                if not isinstance(m, dict):
                                    continue
                                role = str(m.get("role") or m.get("type") or "")
                                txt = str(m.get("content") or m.get("text") or "")
                                if "error:" in txt.lower() or m.get("is_error"):
                                    recent_errors += 1
                                if txt.strip():
                                    prefix = "❯ " if "user" in role.lower() else "● "
                                    lines_split = [x.rstrip() for x in txt.splitlines()]
                                    if lines_split:
                                        term_tail.append(f"{prefix}{lines_split[0]}")
                                        for sln in lines_split[1:]:
                                            term_tail.append(f"  {sln}")
                except Exception:
                    pass
                if not term_tail:
                    term_tail = [f"[GEMINI] {session_file.name}"]
                health_metrics = compute_context_and_hallucination_metrics(
                    tokens_used=min(200000, max(1000, int(st.st_size / 4.0))),
                    tokens_limit=200000,
                    steps_count=steps_cnt,
                    recent_tool_errors=min(4, recent_errors),
                    game_state=state,
                    age_seconds=age,
                )
                units.append(
                    AgentUnit(
                        id=f"gemini-{sid}",
                        name=f"Gemini CLI ({sid})",
                        workspace_title=f"Gemini CLI ({sid})",
                        session_id=session_file.stem,
                        provider="gemini",
                        repo_name=session_file.parent.name,
                        repo_path=str(session_file.parent),
                        branch="main",
                        game_state=state,
                        score_xp=400,
                        pellets_eaten=6,
                        pellets_total=24,
                        context_health_pct=health_metrics["context_health_pct"],
                        hallucination_risk_pct=health_metrics["hallucination_risk_pct"],
                        health_status=health_metrics["health_status"],
                        context_tokens_used=health_metrics["context_tokens_used"],
                        context_tokens_limit=health_metrics["context_tokens_limit"],
                        active_elapsed_sec=health_metrics["active_elapsed_sec"],
                        is_stalled=health_metrics["is_stalled"],
                        health_signals=health_metrics["health_signals"],
                        current_action="Gemini CLI ativo" if state == AgentGameState.CHOMPING else "Parado — aguardando próximo comando",
                        last_thought="Sessão Gemini CLI descoberta em ~/.gemini/tmp",
                        active_tool="gemini",
                        terminal_tail=term_tail[-800:],
                        controllable=True,
                        control_mode="hook",
                        neon_color="#00E5FF",
                        sprite_type="ghost_inky",
                        is_live_discovered=True,
                        archived=age > 8 * 3600,
                        last_active_ts=mtime,
                    )
                )
            except Exception:
                continue
        return units


class CodexAdapter(BaseAgentAdapter):
    """Adapter for OpenAI Codex CLI reading ~/.codex/sessions/ and PTY/Hook streams."""

    def get_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            id="codex",
            display_name="OpenAI Codex",
            arcade_title="FUNKY-CODEX // FORGE",
            neon_color="#00F59B",        # Neon Emerald Green
            secondary_color="#10B981",
            sprite_type="ghost_funky",
            log_paths=["~/.codex/sessions/*"],
            cli_command="codex",
            supports_pty_wrap=True,
            supports_hooks=True,
        )

    def discover_live_agents(self, max_age_minutes: int = 1440) -> List[AgentUnit]:
        codex_dir = Path.home() / ".codex" / "sessions"
        if not codex_dir.exists():
            return []
        units: List[AgentUnit] = []
        now = time.time()
        for rollout_file in sorted(
            codex_dir.rglob("*.jsonl"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )[:8]:
            try:
                st = rollout_file.stat()
                mtime = st.st_mtime
                age = now - mtime
                if age > max_age_minutes * 60:
                    continue
                sid = rollout_file.stem[:8]
                lines = rollout_file.read_text(encoding="utf-8", errors="replace").splitlines()
                cwd = str(Path.home())
                last_msg = "Sessão Codex CLI"
                waiting_approval = False
                term_tail: List[str] = []
                recent_errors = 0
                for raw in lines[-100:]:
                    if not raw.strip():
                        continue
                    try:
                        obj = json.loads(raw)
                        if isinstance(obj.get("cwd"), str):
                            cwd = obj["cwd"]
                        ev_type = str(obj.get("type") or "")
                        role = str(obj.get("role") or "")
                        content = obj.get("content") or obj.get("message") or obj.get("text")
                        if isinstance(content, str) and content.strip():
                            if "error:" in content.lower() or "failed" in ev_type.lower():
                                recent_errors += 1
                            prefix = "❯ " if role == "user" else "● "
                            c_lines = [x.rstrip() for x in content.splitlines()]
                            if c_lines:
                                term_tail.append(f"{prefix}{c_lines[0]}")
                                for cln in c_lines[1:]:
                                    term_tail.append(f"  {cln}")
                                last_msg = c_lines[-1][:140]
                        elif isinstance(content, list):
                            for blk in content:
                                if isinstance(blk, dict) and isinstance(blk.get("text"), str) and blk["text"].strip():
                                    prefix = "❯ " if role == "user" else "● "
                                    c_lines = [x.rstrip() for x in blk["text"].splitlines()]
                                    if c_lines:
                                        term_tail.append(f"{prefix}{c_lines[0]}")
                                        for cln in c_lines[1:]:
                                            term_tail.append(f"  {cln}")
                                        last_msg = c_lines[-1][:140]
                        if ev_type in ("exec_approval_request", "apply_patch_approval_request", "request_user_input"):
                            waiting_approval = True
                            last_msg = f"Aguardando aprovação ({ev_type})"
                            term_tail.append(f"⚡ {last_msg}")
                        elif ev_type in ("agent-turn-complete", "task_complete"):
                            waiting_approval = False
                            last_msg = "Parado — turno finalizado"
                    except Exception:
                        continue
                if not term_tail:
                    term_tail = [f"[CODEX] {last_msg}"]
                if waiting_approval and age < 14400:
                    state = AgentGameState.POWER_PELLET_WAIT
                elif age < 15.0:
                    state = AgentGameState.CHOMPING
                else:
                    state = AgentGameState.IDLE
                repo_name = Path(cwd).name or "codex-workspace"
                pending_perm: Optional[PermissionRequestItem] = None
                if state == AgentGameState.POWER_PELLET_WAIT:
                    pending_perm = PermissionRequestItem(
                        id=f"perm-codex-{sid}-{int(mtime)}",
                        agent_id=f"codex-{sid}",
                        agent_name=f"Codex // {repo_name}",
                        provider="codex",
                        repo_name=repo_name,
                        repo_path=cwd,
                        tool_name="Codex Approval",
                        command_preview=last_msg,
                        reason="Codex CLI aguardando aprovação ou input do usuário.",
                        channel="hook",
                    )
                health_metrics = compute_context_and_hallucination_metrics(
                    tokens_used=min(200000, max(1000, int(st.st_size / 4.0))),
                    tokens_limit=200000,
                    steps_count=len(lines),
                    recent_tool_errors=min(4, recent_errors),
                    game_state=state,
                    age_seconds=age,
                )
                units.append(
                    AgentUnit(
                        id=f"codex-{sid}",
                        name=f"Codex // {repo_name}",
                        workspace_title=f"Codex // {repo_name}",
                        session_id=rollout_file.stem,
                        provider="codex",
                        repo_name=repo_name,
                        repo_path=cwd,
                        branch="main",
                        game_state=state,
                        score_xp=len(lines) * 80,
                        pellets_eaten=min(24, max(2, len(lines) // 4)),
                        pellets_total=24,
                        context_health_pct=health_metrics["context_health_pct"],
                        hallucination_risk_pct=health_metrics["hallucination_risk_pct"],
                        health_status=health_metrics["health_status"],
                        context_tokens_used=health_metrics["context_tokens_used"],
                        context_tokens_limit=health_metrics["context_tokens_limit"],
                        active_elapsed_sec=health_metrics["active_elapsed_sec"],
                        is_stalled=health_metrics["is_stalled"],
                        health_signals=health_metrics["health_signals"],
                        current_action=last_msg[:140],
                        last_thought=f"Sessão Codex em {repo_name}",
                        active_tool="codex",
                        terminal_tail=term_tail[-800:],
                        pending_permission=pending_perm,
                        controllable=True,
                        control_mode="hook",
                        neon_color="#00F59B",
                        sprite_type="ghost_funky",
                        is_live_discovered=True,
                        archived=age > 8 * 3600,
                        last_active_ts=mtime,
                    )
                )
            except Exception:
                continue
        return units

