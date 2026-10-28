from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional

from pac_neon.adapters.base import BaseAgentAdapter
from pac_neon.adapters.claude_adapter import ClaudeAdapter
from pac_neon.models import (
    AgentGameState,
    AgentUnit,
    PermissionRequestItem,
    ProviderMetadata,
    compute_context_and_hallucination_metrics,
)


def _scan_live_cli_processes(cli_name: str) -> List[dict]:
    """Scan live OS processes for interactive `gemini` or `codex` CLI instances across all terminals and IDEs."""
    import subprocess
    out: List[dict] = []
    try:
        res = subprocess.run(
            ["ps", "-axo", "pid=,tty=,command="],
            capture_output=True,
            text=True,
            timeout=1.2,
        )
        for raw_ln in res.stdout.splitlines():
            parts = raw_ln.strip().split(None, 2)
            if len(parts) < 3:
                continue
            try:
                pid_val = int(parts[0])
            except ValueError:
                continue
            tty_s, cmd_s = parts[1], parts[2].strip()
            if not tty_s or tty_s == "??":
                continue
            tokens = cmd_s.split()
            if not tokens:
                continue
            bin_name = Path(tokens[0]).name.lower()
            matched = bin_name == cli_name or (
                bin_name in ("node", "bun", "python", "python3")
                and any(
                    f"/{cli_name}" in tok.lower() or f"{cli_name}-cli" in tok.lower()
                    for tok in tokens[1:3]
                )
            )
            if not matched:
                continue
            tty_tree, host_app = ClaudeAdapter._inspect_pid_tree(pid_val)
            cwd = ClaudeAdapter._resolve_pid_cwd(pid_val) or str(Path.home())
            out.append(
                {
                    "pid": pid_val,
                    "tty": tty_tree or tty_s,
                    "host_app": host_app or "",
                    "cwd": cwd.rstrip("/"),
                }
            )
    except Exception:
        pass
    return out


class GeminiAdapter(BaseAgentAdapter):
    """Adapter for Google Gemini CLI reading ~/.gemini/tmp/ and live CLI processes across all terminals/IDEs."""

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
        live_procs = _scan_live_cli_processes("gemini")
        used_pids: set[int] = set()
        units: List[AgentUnit] = []
        now = time.time()
        if gemini_tmp.exists():
            for session_file in sorted(
                gemini_tmp.rglob("*.json*"),
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )[:8]:
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
                    session_cwd = ""
                    last_thought_str = "Sessão Gemini CLI descoberta em ~/.gemini/tmp"
                    active_tool_str = "gemini"
                    pending_perm: Optional[PermissionRequestItem] = None
                    try:
                        raw_text = session_file.read_text(encoding="utf-8", errors="replace")
                        if session_file.suffix == ".jsonl":
                            all_lines = raw_text.splitlines()
                            steps_cnt = max(1, len(all_lines))
                            for jln in all_lines[-80:]:
                                if not jln.strip():
                                    continue
                                obj = json.loads(jln)
                                if isinstance(obj.get("cwd"), str) and obj["cwd"].strip():
                                    session_cwd = obj["cwd"].strip()
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
                            if isinstance(data, dict) and isinstance(data.get("cwd"), str):
                                session_cwd = data["cwd"].strip()
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
                                    thoughts = m.get("thoughts")
                                    if isinstance(thoughts, list):
                                        for th in thoughts:
                                            if isinstance(th, dict) and th.get("description"):
                                                last_thought_str = str(th["description"])
                                                term_tail.append(f"✻ {last_thought_str}")
                                    if txt.strip():
                                        prefix = "❯ " if "user" in role.lower() else "● "
                                        lines_split = [x.rstrip() for x in txt.splitlines()]
                                        if lines_split:
                                            term_tail.append(f"{prefix}{lines_split[0]}")
                                            for sln in lines_split[1:]:
                                                term_tail.append(f"  {sln}")
                                    tcalls = m.get("toolCalls")
                                    if isinstance(tcalls, list):
                                        for tc in tcalls:
                                            if not isinstance(tc, dict):
                                                continue
                                            tname = str(tc.get("name") or "gemini_tool")
                                            active_tool_str = tname
                                            tstatus = str(tc.get("status") or "").lower()
                                            targs = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                                            cmd_prev = str(targs.get("command") or targs.get("path") or targs or tname)
                                            term_tail.append(f"● {tname}({cmd_prev[:120]})")
                                            if "wait" in tstatus or "confirm" in tstatus or "pending" in tstatus:
                                                state = AgentGameState.POWER_PELLET_WAIT
                                                pending_perm = PermissionRequestItem(
                                                    agent_id=f"gemini-{sid}",
                                                    agent_name=f"Gemini CLI ({Path(session_cwd or str(session_file.parent)).name})",
                                                    provider="gemini",
                                                    repo_name=Path(session_cwd or str(session_file.parent)).name,
                                                    tool_name=tname,
                                                    command_preview=cmd_prev[:160],
                                                    reason="Gemini CLI aguardando confirmação de ferramenta.",
                                                    channel="hook",
                                                )
                    except Exception:
                        pass
                    if not term_tail:
                        term_tail = [f"[GEMINI] {session_file.name}"]
                    matched_proc = next((p for p in live_procs if p["pid"] not in used_pids), None)
                    pid_val = matched_proc["pid"] if matched_proc else None
                    tty_val = matched_proc["tty"] if matched_proc else None
                    host_val = matched_proc["host_app"] if matched_proc else ""
                    repo_path = (matched_proc["cwd"] if matched_proc else "") or session_cwd or str(session_file.parent)
                    if pid_val:
                        used_pids.add(pid_val)
                    repo_name = Path(repo_path).name or session_file.parent.name
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
                            name=f"Gemini CLI ({repo_name})",
                            workspace_title=f"Gemini CLI ({repo_name})",
                            session_id=session_file.stem,
                            provider="gemini",
                            repo_name=repo_name,
                            repo_path=repo_path,
                            branch=ClaudeAdapter._resolve_git_branch(repo_path),
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
                            current_action="Aguardando aprovação de ferramenta" if state == AgentGameState.POWER_PELLET_WAIT else ("Gemini CLI ativo" if state == AgentGameState.CHOMPING else "Parado — aguardando próximo comando"),
                            last_thought=last_thought_str,
                            active_tool=active_tool_str,
                            pending_permission=pending_perm,
                            terminal_tail=term_tail[-800:],
                            pid=pid_val,
                            tty=tty_val,
                            host_app=host_val,
                            controllable=True,
                            control_mode="hook",
                            neon_color="#00E5FF",
                            sprite_type="ghost_inky",
                            is_live_discovered=True,
                            archived=False,
                            last_active_ts=max(mtime, now if pid_val else 0.0),
                        )
                    )
                except Exception:
                    continue

        for proc in live_procs:
            if proc["pid"] in used_pids:
                continue
            used_pids.add(proc["pid"])
            repo_path = proc["cwd"]
            repo_name = Path(repo_path).name or "gemini-workspace"
            env_lbl = f"{proc['host_app']} ({proc['tty']})" if proc["host_app"] else (proc["tty"] or "Terminal")
            units.append(
                AgentUnit(
                    id=f"gemini-pid-{proc['pid']}",
                    name=f"Gemini CLI • {repo_name}",
                    workspace_title=f"Gemini CLI • {repo_name}",
                    session_id=f"pid-{proc['pid']}",
                    provider="gemini",
                    repo_name=repo_name,
                    repo_path=repo_path,
                    branch=ClaudeAdapter._resolve_git_branch(repo_path),
                    game_state=AgentGameState.IDLE,
                    current_action=f"Sessão Gemini CLI aberta em {env_lbl} • Pronto para comando",
                    last_thought=f"Processo Gemini CLI ativo em {env_lbl} ({repo_path})",
                    terminal_tail=[
                        f"🟢 Sessão Gemini CLI aberta em {env_lbl}",
                        f"📂 Workspace: {repo_path}",
                    ],
                    pid=proc["pid"],
                    tty=proc["tty"],
                    host_app=proc["host_app"],
                    controllable=True,
                    control_mode="interactive_cli",
                    neon_color="#00E5FF",
                    sprite_type="ghost_inky",
                    is_live_discovered=True,
                    archived=False,
                    last_active_ts=now,
                )
            )
        return units


class CodexAdapter(BaseAgentAdapter):
    """Adapter for OpenAI Codex CLI reading ~/.codex/sessions/ and live CLI processes across all terminals/IDEs."""

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
        live_procs = _scan_live_cli_processes("codex")
        used_pids: set[int] = set()
        units: List[AgentUnit] = []
        now = time.time()
        if codex_dir.exists():
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
                            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                            if isinstance(obj.get("cwd"), str):
                                cwd = obj["cwd"]
                            elif isinstance(payload.get("cwd"), str):
                                cwd = payload["cwd"]
                            ev_type = str(payload.get("type") or obj.get("type") or "")
                            role = str(payload.get("role") or obj.get("role") or "")
                            content = (
                                payload.get("content")
                                or payload.get("message")
                                or payload.get("text")
                                or obj.get("content")
                                or obj.get("message")
                                or obj.get("text")
                            )
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
                                    if isinstance(blk, dict):
                                        b_text = blk.get("text") or blk.get("input_text") or blk.get("output_text")
                                        if isinstance(b_text, str) and b_text.strip():
                                            prefix = "❯ " if role == "user" else "● "
                                            c_lines = [x.rstrip() for x in b_text.splitlines()]
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
                    matched_proc = next(
                        (
                            p
                            for p in live_procs
                            if p["pid"] not in used_pids
                            and (p["cwd"] == cwd.rstrip("/") or not cwd)
                        ),
                        None,
                    )
                    pid_val = matched_proc["pid"] if matched_proc else None
                    tty_val = matched_proc["tty"] if matched_proc else None
                    host_val = matched_proc["host_app"] if matched_proc else ""
                    if pid_val:
                        used_pids.add(pid_val)
                    repo_name = Path(cwd).name or "codex-workspace"
                    pending_perm: Optional[PermissionRequestItem] = None
                    if state == AgentGameState.POWER_PELLET_WAIT:
                        pending_perm = PermissionRequestItem(
                            id=f"perm-codex-{sid}-{int(mtime)}",
                            agent_id=f"codex-{sid}",
                            agent_name=f"Codex // {repo_name}",
                            provider="codex",
                            repo_name=repo_name,
                            tool_name="Codex Approval",
                            command_preview=last_msg,
                            reason="Codex CLI aguardando aprovação ou input do usuário.",
                            channel="hook",
                            full_details={"cwd": cwd},
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
                            branch=ClaudeAdapter._resolve_git_branch(cwd),
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
                            pid=pid_val,
                            tty=tty_val,
                            host_app=host_val,
                            controllable=True,
                            control_mode="hook",
                            neon_color="#00F59B",
                            sprite_type="ghost_funky",
                            is_live_discovered=True,
                            archived=False,
                            last_active_ts=max(mtime, now if pid_val else 0.0),
                        )
                    )
                except Exception:
                    continue

        for proc in live_procs:
            if proc["pid"] in used_pids:
                continue
            used_pids.add(proc["pid"])
            repo_path = proc["cwd"]
            repo_name = Path(repo_path).name or "codex-workspace"
            env_lbl = f"{proc['host_app']} ({proc['tty']})" if proc["host_app"] else (proc["tty"] or "Terminal")
            units.append(
                AgentUnit(
                    id=f"codex-pid-{proc['pid']}",
                    name=f"Codex // {repo_name}",
                    workspace_title=f"Codex // {repo_name}",
                    session_id=f"pid-{proc['pid']}",
                    provider="codex",
                    repo_name=repo_name,
                    repo_path=repo_path,
                    branch=ClaudeAdapter._resolve_git_branch(repo_path),
                    game_state=AgentGameState.IDLE,
                    current_action=f"Sessão Codex CLI aberta em {env_lbl} • Pronto para comando",
                    last_thought=f"Processo Codex CLI ativo em {env_lbl} ({repo_path})",
                    terminal_tail=[
                        f"🟢 Sessão Codex CLI aberta em {env_lbl}",
                        f"📂 Workspace: {repo_path}",
                    ],
                    pid=proc["pid"],
                    tty=proc["tty"],
                    host_app=proc["host_app"],
                    controllable=True,
                    control_mode="interactive_cli",
                    neon_color="#00F59B",
                    sprite_type="ghost_funky",
                    is_live_discovered=True,
                    archived=False,
                    last_active_ts=now,
                )
            )
        return units

