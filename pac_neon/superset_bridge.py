"""Bridge between Pac-Neon Arcade Cockpit and Superset CLI (`superset-sh/superset`).

Uses the official Superset CLI commands:
- `superset status --json`
- `superset snapshot --json`
- `superset terminals read --local --workspace <ws> --terminal <term> --max-lines 20 --json`
- `superset terminals send --local --workspace <ws> --terminal <term> --text <text>`
- `superset agents create --local --workspace <ws> --agent <agent> --prompt <prompt> --json`
- `superset workspaces open <ws>` / `superset://v2-workspace/<ws>?terminalId=<term>`
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from pac_neon.adapters.base import AdapterRegistry
from pac_neon.models import (
    AgentGameState,
    AgentUnit,
    PermissionRequestItem,
    PermissionResolutionAction,
    utc_now_iso,
)

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def _infer_provider_id(agent_id_or_title: str) -> str:
    lowered = (agent_id_or_title or "").lower()
    if "claude" in lowered:
        return "claude"
    if "codex" in lowered or "openai" in lowered:
        return "codex"
    if "gemini" in lowered:
        return "gemini"
    if "antigravity" in lowered or "agy" in lowered:
        return "antigravity"
    return "claude"


def _map_superset_state(superset_state: Optional[str]) -> AgentGameState:
    """Maps Superset's `starting | working | blocked | idle` states to Pac-Neon states."""
    if superset_state == "blocked":
        # In Superset, `blocked` means the agent paused for user input or tool approval!
        return AgentGameState.POWER_PELLET_WAIT
    if superset_state == "working":
        return AgentGameState.CHOMPING
    if superset_state == "starting":
        return AgentGameState.THINKING
    return AgentGameState.IDLE


class SupersetBridge:
    """Connects Pac-Neon to the local Superset host server and CLI."""

    def __init__(self, cli_bin: Optional[str] = None) -> None:
        self.cli_bin = cli_bin or self._find_superset_cli()
        self._terminal_screen_cache: Dict[str, str] = {}
        self._resolved_perm_keys: set[str] = set()

    @staticmethod
    def _find_superset_cli() -> Optional[str]:
        found = shutil.which("superset")
        if found:
            return found
        candidates = [
            Path.home() / ".superset" / "bin" / "superset",
            Path.home() / ".local" / "bin" / "superset",
            Path.home() / ".local" / "share" / "superset" / "bin" / "superset",
            Path("/Applications/Superset.app/Contents/Resources/bin/superset"),
            Path("/opt/homebrew/bin/superset"),
            Path.home() / "Projects" / "superset" / "packages" / "cli" / "bin" / "superset",
        ]
        for candidate in candidates:
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    def is_installed(self) -> bool:
        if not self.cli_bin:
            self.cli_bin = self._find_superset_cli()
        return self.cli_bin is not None

    def _run_cli_json(self, args: List[str], timeout: float = 3.5) -> Optional[Any]:
        if not self.is_installed() or not self.cli_bin:
            return None
        cmd = [self.cli_bin, *args, "--json"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return None
            return json.loads(proc.stdout)
        except Exception:
            return None

    def get_host_status(self) -> Dict[str, Any]:
        """Checks `superset status --json` and local ~/Projects/superset repository."""
        installed = self.is_installed()
        local_repo = Path.home() / "Projects" / "superset"
        superset_dir_exists = (
            (Path.home() / ".superset").exists()
            or Path("/Applications/Superset.app").exists()
            or local_repo.exists()
        )
        if not installed:
            return {
                "installed": False,
                "superset_app_detected": superset_dir_exists,
                "local_repo_path": str(local_repo) if local_repo.exists() else None,
                "running": False,
                "healthy": False,
                "cli_path": None,
                "mode": "native_hooks_and_antigravity",
                "install_hint": "Repo local ~/Projects/superset detectado (hooks nativos ativos em ~/.pac-neon/hooks/notify.sh)",
            }
        data = self._run_cli_json(["status"], timeout=2.5)
        if not isinstance(data, dict):
            return {
                "installed": True,
                "superset_app_detected": True,
                "running": False,
                "healthy": False,
                "cli_path": self.cli_bin,
                "mode": "superset_cli_idle",
                "install_hint": "Execute `superset start --daemon` ou abra o Superset Desktop.",
            }
        running = bool(data.get("running", False))
        return {
            "installed": True,
            "superset_app_detected": True,
            "running": running,
            "healthy": bool(data.get("healthy", running)),
            "pid": data.get("pid"),
            "port": data.get("port"),
            "endpoint": data.get("endpoint"),
            "host_name": data.get("hostName"),
            "cli_path": self.cli_bin,
            "mode": "superset_live" if running else "superset_cli_idle",
        }

    def read_terminal_screen(
        self, workspace_id: str, terminal_id: str, max_lines: int = 18
    ) -> str:
        """Runs `superset terminals read --local --workspace <ws> --terminal <term> --max-lines <n> --json`."""
        data = self._run_cli_json(
            [
                "terminals",
                "read",
                "--local",
                "--workspace",
                workspace_id,
                "--terminal",
                terminal_id,
                "--max-lines",
                str(max_lines),
            ],
            timeout=2.5,
        )
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            cleaned = strip_ansi(data["text"]).strip()
            self._terminal_screen_cache[f"{workspace_id}:{terminal_id}"] = cleaned
            return cleaned
        return self._terminal_screen_cache.get(f"{workspace_id}:{terminal_id}", "")

    def send_to_terminal(
        self,
        workspace_id: str,
        terminal_id: str,
        text: Optional[str] = None,
        keys: Optional[str] = None,
    ) -> bool:
        """Runs `superset terminals send --local --workspace <ws> --terminal <term> ...`."""
        if not self.is_installed() or not self.cli_bin:
            return False
        cmd = [
            self.cli_bin,
            "terminals",
            "send",
            "--local",
            "--workspace",
            workspace_id,
            "--terminal",
            terminal_id,
        ]
        if keys:
            cmd.extend(["--keys", keys])
        elif text is not None:
            cmd.extend(["--text", text])
        else:
            return False

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=4.0,
                check=False,
            )
            if proc.returncode == 0:
                self._resolved_perm_keys.add(f"{workspace_id}:{terminal_id}")
                return True
        except Exception:
            pass
        return False

    def resolve_permission_in_superset(
        self,
        workspace_id: str,
        terminal_id: str,
        action: PermissionResolutionAction,
        custom_text: Optional[str] = None,
    ) -> bool:
        """Sends approval/denial (`y`, `2`, `n`, or custom text) directly to a blocked Superset terminal."""
        if custom_text:
            return self.send_to_terminal(workspace_id, terminal_id, text=custom_text)
        if action == PermissionResolutionAction.DENY:
            return self.send_to_terminal(workspace_id, terminal_id, text="n")
        if action == PermissionResolutionAction.ALWAYS:
            # In Claude Code / Gemini CLI, '2' or 'a' often approves for the session
            return self.send_to_terminal(workspace_id, terminal_id, text="y")
        return self.send_to_terminal(workspace_id, terminal_id, text="y")

    def spawn_agent_in_workspace(
        self,
        workspace_id: str,
        agent_preset: str = "claude",
        prompt: str = "",
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Runs `superset agents create --local --workspace <ws> --agent <preset> --prompt <prompt> --json`."""
        args = [
            "agents",
            "create",
            "--local",
            "--workspace",
            workspace_id,
            "--agent",
            agent_preset,
            "--prompt",
            prompt or "Analyze workspace status",
        ]
        if model:
            args.extend(["--model", model])
        if effort:
            args.extend(["--effort", effort])
        res = self._run_cli_json(args, timeout=6.0)
        if isinstance(res, dict):
            return res
        return None

    def open_in_superset_desktop(
        self, workspace_id: str, terminal_id: Optional[str] = None
    ) -> str:
        """Builds the `superset://` deep link and opens it on macOS."""
        if terminal_id:
            deep_link = f"superset://v2-workspace/{workspace_id}?terminalId={terminal_id}"
        else:
            deep_link = f"superset://v2-workspace/{workspace_id}"
        try:
            subprocess.Popen(["open", deep_link])
        except Exception:
            pass
        return deep_link

    def discover_from_snapshot(self) -> List[AgentUnit]:
        """Calls `superset snapshot --json` to discover all workspaces, terminals, and blocked agents."""
        data = self._run_cli_json(["snapshot"], timeout=3.5)
        if not isinstance(data, dict):
            return []

        workspaces = data.get("workspaces")
        if not isinstance(workspaces, list):
            return []

        agents: List[AgentUnit] = []

        for ws_idx, ws in enumerate(workspaces):
            if not isinstance(ws, dict):
                continue
            ws_id = str(ws.get("id") or "")
            ws_name = str(ws.get("name") or "workspace")
            project_name = str(ws.get("project") or ws_name)
            branch_name = str(ws.get("branch") or "main")
            terminals = ws.get("terminals") or []

            if not isinstance(terminals, list):
                continue

            for t_idx, term in enumerate(terminals):
                if not isinstance(term, dict):
                    continue
                term_id = str(term.get("id") or "")
                term_title = str(term.get("title") or "Terminal")
                agent_info = term.get("agent")

                if isinstance(agent_info, dict):
                    raw_agent_id = str(agent_info.get("id") or term_title)
                    raw_state = str(agent_info.get("state") or "idle")
                else:
                    if not any(
                        k in term_title.lower()
                        for k in ("claude", "codex", "gemini", "agy", "agent")
                    ):
                        continue
                    raw_agent_id = term_title
                    raw_state = "idle"

                provider_id = _infer_provider_id(f"{raw_agent_id} {term_title}")
                adapter = AdapterRegistry.get(provider_id)
                meta = adapter.get_metadata() if adapter else None
                neon_color = meta.neon_color if meta else "#00E5FF"
                sprite_type = meta.sprite_type if meta else "ghost_clyde"
                display_name = meta.display_name if meta else provider_id.upper()
                game_state = _map_superset_state(raw_state)

                screen_snippet = ""
                if raw_state in ("blocked", "working"):
                    screen_snippet = self.read_terminal_screen(
                        ws_id, term_id, max_lines=14
                    )

                last_lines = [
                    ln.strip() for ln in screen_snippet.splitlines() if ln.strip()
                ]
                last_line_summary = (
                    last_lines[-1][:140]
                    if last_lines
                    else f"[Superset {raw_state.upper()}] {term_title} ({branch_name})"
                )

                agent_uid = f"superset-{ws_id[:6]}-{term_id[:6]}"
                deep_link = f"superset://v2-workspace/{ws_id}?terminalId={term_id}"

                pending_perm: Optional[PermissionRequestItem] = None
                perm_key = f"{ws_id}:{term_id}"
                if raw_state == "blocked" and perm_key not in self._resolved_perm_keys:
                    prompt_preview = (
                        " | ".join(last_lines[-3:])
                        if last_lines
                        else f"Agente {raw_agent_id} aguardando aprovação ou input no Superset ({ws_name})."
                    )
                    pending_perm = PermissionRequestItem(
                        id=f"perm-ss-{ws_id[:6]}-{term_id[:6]}",
                        agent_id=agent_uid,
                        agent_name=f"{display_name} // {ws_name.upper()}",
                        provider=provider_id,
                        repo_name=project_name,
                        tool_name="Superset Terminal (blocked)",
                        command_preview=prompt_preview[:220],
                        reason=f"O Superset reportou estado 'blocked' (aguardando permissão/input) na worktree {ws_name} ({branch_name}).",
                        channel="superset",
                    )
                elif raw_state != "blocked":
                    self._resolved_perm_keys.discard(perm_key)

                agents.append(
                    AgentUnit(
                        id=agent_uid,
                        name=f"{display_name.upper()} // {ws_name.upper()}",
                        provider=provider_id,
                        repo_name=project_name,
                        repo_path=f"superset://{ws_id}",
                        branch=branch_name,
                        game_state=game_state,
                        score_xp=1200 + (ws_idx * 450) + (t_idx * 250),
                        pellets_eaten=min(24, 8 + (t_idx * 4)),
                        pellets_total=24,
                        context_health_pct=88,
                        current_action=last_line_summary,
                        last_thought=f"Worktree gerenciada pelo Superset ({ws_name} @ {branch_name}) — estado: {raw_state}.",
                        active_tool=f"superset:{raw_state}",
                        terminal_tail=last_lines[-10:]
                        if last_lines
                        else [f"[SUPERSET] Workspace: {ws_name} ({branch_name})", f"[SUPERSET] Agent: {raw_agent_id} · {raw_state}"],
                        pending_permission=pending_perm,
                        controllable=True,
                        control_mode="superset",
                        neon_color=neon_color,
                        sprite_type=sprite_type,
                        is_live_discovered=True,
                        superset_workspace_id=ws_id,
                        superset_terminal_id=term_id,
                        superset_deep_link=deep_link,
                        updated_at=utc_now_iso(),
                    )
                )

        return agents
