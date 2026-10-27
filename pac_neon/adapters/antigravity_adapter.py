from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pac_neon.adapters.base import BaseAgentAdapter
from pac_neon.models import (
    AgentGameState,
    AgentUnit,
    PermissionRequestItem,
    ProviderMetadata,
    compute_context_and_hallucination_metrics,
)



class AntigravityAdapter(BaseAgentAdapter):
    """Adapter for Google Antigravity (AGY) reading ~/.gemini/antigravity/brain/<id>/ + live LanguageServerService ConnectRPC."""

    _RPC_CACHE: tuple[float, List[tuple[int, str]]] = (0.0, [])

    def get_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            id="antigravity",
            display_name="Antigravity",
            arcade_title="PAC-GRAVITON // AGY",
            neon_color="#0078D9",        # Pac-Neon Vivid Electric Blue (#0078D9)
            secondary_color="#00E5FF",
            sprite_type="pac_neon",
            log_paths=["~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl"],
            cli_command="agy",
            supports_pty_wrap=True,
            supports_hooks=True,
        )

    _RPC_CACHE: tuple[float, List[tuple[int, str]]] = (0.0, [])
    _CASCADE_ENDPOINT_MAP: Dict[str, tuple[int, str]] = {}

    @classmethod
    def _discover_rpc_endpoints(cls, force: bool = False) -> List[tuple[int, str]]:
        """Discover active Antigravity language_server ConnectRPC (port, csrf_token) pairs on 127.0.0.1."""
        now = time.time()
        cached_ts, cached_eps = cls._RPC_CACHE
        if not force and cached_eps and (now - cached_ts) < 5.0:
            return cached_eps

        ide_pid_tokens: Dict[str, str] = {}
        other_pid_tokens: Dict[str, str] = {}
        hub_candidates: List[tuple[int, str]] = []
        try:
            ps_res = subprocess.run(
                ["ps", "aux"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            for ln in (ps_res.stdout or "").splitlines():
                if "language_server" not in ln and "agy --hub" not in ln:
                    continue
                m_tok = re.search(r"--csrf_token[=\s]+([a-fA-F0-9\-]+)", ln)
                if not m_tok:
                    continue
                tok = m_tok.group(1)
                parts = ln.split()
                pid_s = parts[1] if len(parts) > 1 and parts[1].isdigit() else ""
                is_ide_ls = "Antigravity.app" in ln or "--override_ide_name antigravity" in ln
                if pid_s:
                    if is_ide_ls:
                        ide_pid_tokens[pid_s] = tok
                    elif "language_server" in ln:
                        other_pid_tokens[pid_s] = tok
                m_hub = re.search(r"--hub-port[=\s]+(\d+)", ln)
                if m_hub:
                    hub_candidates.append((int(m_hub.group(1)), tok))

            all_pids = list(ide_pid_tokens.keys()) + list(other_pid_tokens.keys())
            ide_candidates: List[tuple[int, str]] = []
            other_candidates: List[tuple[int, str]] = []
            if all_pids:
                lsof_res = subprocess.run(
                    ["lsof", "-Pan", "-p", ",".join(all_pids), "-iTCP", "-sTCP:LISTEN"],
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                )
                for ln in (lsof_res.stdout or "").splitlines():
                    parts = ln.split()
                    if len(parts) < 2:
                        continue
                    pid_s = parts[1]
                    m_port = re.search(r"127\.0\.0\.1:(\d+)", ln)
                    if not m_port:
                        continue
                    port = int(m_port.group(1))
                    if pid_s in ide_pid_tokens:
                        pair = (port, ide_pid_tokens[pid_s])
                        if pair not in ide_candidates:
                            ide_candidates.append(pair)
                    elif pid_s in other_pid_tokens:
                        pair = (port, other_pid_tokens[pid_s])
                        if pair not in other_candidates:
                            other_candidates.append(pair)
        except Exception:
            ide_candidates = []
            other_candidates = []

        # Always prioritize Antigravity.app IDE language_server endpoints first!
        ordered_candidates: List[tuple[int, str]] = []
        for pair in ide_candidates + other_candidates + hub_candidates:
            if pair not in ordered_candidates:
                ordered_candidates.append(pair)

        verified: List[tuple[int, str]] = []
        for port, tok in ordered_candidates:
            url = f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories"
            req = urllib.request.Request(
                url,
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Connect-Protocol-Version": "1",
                    "x-codeium-csrf-token": tok,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=0.6) as resp:
                    if resp.status == 200:
                        verified.append((port, tok))
            except Exception:
                continue

        cls._RPC_CACHE = (now, verified)
        return verified

    @classmethod
    def _post_rpc_endpoint(cls, port: int, tok: str, method: str, body: bytes, timeout: float) -> Optional[dict]:
        url = f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/{method}"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "x-codeium-csrf-token": tok,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw.strip() else {}
        except Exception:
            return None

    @classmethod
    def call_rpc(
        cls,
        method: str,
        payload: dict,
        timeout: float = 4.0,
        cascade_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Invoke a method on Antigravity's local LanguageServerService via ConnectRPC JSON."""
        cid = cascade_id or (payload.get("cascadeId") if isinstance(payload, dict) else None)
        body = json.dumps(payload).encode("utf-8")

        if cid and cid not in cls._CASCADE_ENDPOINT_MAP:
            cls.get_all_live_trajectories()

        for attempt in range(2):
            endpoints = list(cls._discover_rpc_endpoints(force=(attempt > 0)))
            if cid and cid in cls._CASCADE_ENDPOINT_MAP:
                preferred = cls._CASCADE_ENDPOINT_MAP[cid]
                endpoints = [preferred] + [ep for ep in endpoints if ep != preferred]
            for port, tok in endpoints:
                res = cls._post_rpc_endpoint(port, tok, method, body, timeout)
                if res is not None:
                    return res
        return None

    @classmethod
    def get_all_live_trajectories(cls) -> Dict[str, dict]:
        """Query all live LanguageServerService endpoints and merge trajectorySummaries by freshest stepCount/status."""
        endpoints = cls._discover_rpc_endpoints()
        merged: Dict[str, dict] = {}
        best_rank: Dict[str, tuple[int, int, str]] = {}

        for port, tok in endpoints:
            resp = cls._post_rpc_endpoint(port, tok, "GetAllCascadeTrajectories", b"{}", timeout=1.0)
            if not isinstance(resp, dict):
                continue
            tsums = resp.get("trajectorySummaries")
            if not isinstance(tsums, dict):
                continue
            for cid, summary in tsums.items():
                if not isinstance(summary, dict):
                    continue
                step_cnt = int(summary.get("stepCount") or 0)
                status_s = str(summary.get("status") or "")
                is_active = 1 if status_s in ("CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_WAITING_FOR_USER") else 0
                mod_s = str(summary.get("lastModifiedTime") or "")
                rank = (step_cnt, is_active, mod_s)
                if cid not in best_rank or rank > best_rank[cid]:
                    best_rank[cid] = rank
                    merged[cid] = summary
                    cls._CASCADE_ENDPOINT_MAP[cid] = (port, tok)

        return merged

    @classmethod
    def get_live_trajectory(cls, cascade_id: str) -> Optional[dict]:
        """Return full GetCascadeTrajectory response for cascade_id."""
        if not cascade_id:
            return None
        return cls.call_rpc("GetCascadeTrajectory", {"cascadeId": cascade_id}, timeout=2.5, cascade_id=cascade_id)

    _CASCADE_CONFIG_CACHE: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def _get_cascade_config_for_send(cls, cascade_id: str) -> Dict[str, Any]:
        """Extract the conversation's active CascadeConfig (with requestedModel) so SendUserCascadeMessage preserves model selection."""
        traj_resp = cls.get_live_trajectory(cascade_id) or {}
        traj = traj_resp.get("trajectory") or {}
        steps = traj.get("steps") or []

        found_cfg: Optional[Dict[str, Any]] = None
        detected_model: str = "MODEL_PLACEHOLDER_M417"

        # 1. Detect active model from latest step metadata or generatorMetadata as fallback
        for st in reversed(steps):
            if not isinstance(st, dict):
                continue
            gm = (st.get("metadata") or {}).get("generatorModel")
            if isinstance(gm, str) and gm.strip():
                detected_model = gm.strip()
                break
        if detected_model == "MODEL_PLACEHOLDER_M417":
            for gm_entry in reversed(traj.get("generatorMetadata") or []):
                if not isinstance(gm_entry, dict):
                    continue
                cm = (gm_entry.get("chatModel") or {}).get("model")
                if isinstance(cm, str) and cm.strip():
                    detected_model = cm.strip()
                    break

        # 2. Extract full userConfig / lastUserConfig from the most recent USER_INPUT step
        for st in reversed(steps):
            if not isinstance(st, dict) or st.get("type") != "CORTEX_STEP_TYPE_USER_INPUT":
                continue
            ui = st.get("userInput") or {}
            for key in ("userConfig", "lastUserConfig"):
                cand = ui.get(key)
                if not isinstance(cand, dict):
                    continue
                pc = cand.get("plannerConfig")
                if isinstance(pc, dict) and (pc.get("requestedModel") or pc.get("planModel")):
                    found_cfg = json.loads(json.dumps(cand))
                    break
            if found_cfg is not None:
                break

        # 3. Fallback to executorMetadatas.requestOverrideConfig
        if found_cfg is None:
            for em in reversed(traj.get("executorMetadatas") or []):
                if not isinstance(em, dict):
                    continue
                for key in ("requestOverrideConfig", "cascadeConfig"):
                    cand = em.get(key)
                    if not isinstance(cand, dict):
                        continue
                    pc = cand.get("plannerConfig")
                    if isinstance(pc, dict) and (pc.get("requestedModel") or pc.get("planModel")):
                        found_cfg = json.loads(json.dumps(cand))
                        break
                if found_cfg is not None:
                    break

        # 4. Fallback to cached config or template from an existing active conversation
        if found_cfg is None and cascade_id in cls._CASCADE_CONFIG_CACHE:
            found_cfg = json.loads(json.dumps(cls._CASCADE_CONFIG_CACHE[cascade_id]))

        if found_cfg is None and cls._CASCADE_CONFIG_CACHE:
            any_cached = next(iter(cls._CASCADE_CONFIG_CACHE.values()))
            found_cfg = json.loads(json.dumps(any_cached))

        if found_cfg is None:
            try:
                live_all = cls.get_all_live_trajectories()
                for other_cid, sinfo in live_all.items():
                    if other_cid != cascade_id and int(sinfo.get("stepCount") or 0) > 0:
                        other_traj = (cls.get_live_trajectory(other_cid) or {}).get("trajectory") or {}
                        for st in reversed(other_traj.get("steps") or []):
                            if not isinstance(st, dict) or st.get("type") != "CORTEX_STEP_TYPE_USER_INPUT":
                                continue
                            ui = st.get("userInput") or {}
                            cand = ui.get("userConfig") or ui.get("lastUserConfig")
                            if isinstance(cand, dict) and isinstance(cand.get("plannerConfig"), dict):
                                found_cfg = json.loads(json.dumps(cand))
                                break
                        if found_cfg is not None:
                            break
            except Exception:
                pass

        if found_cfg is None:
            found_cfg = {
                "plannerConfig": {
                    "declarativeMixinConfig": {
                        "promptSections": [
                            {"builtinName": "identity"},
                            {"builtinName": "user_information"},
                            {"builtinName": "mcp_servers"},
                            {"builtinName": "user_rules"},
                            {"builtinName": "skills"},
                            {"builtinName": "subagents"},
                            {"builtinName": "subagent_reminder"},
                            {"builtinName": "messaging"},
                            {"builtinName": "conversation_transcript"},
                            {"builtinName": "artifacts"},
                            {"builtinName": "slash_commands"},
                            {"builtinName": "terminal_sandbox"},
                            {"builtinName": "planning_mode"},
                            {"builtinName": "planning_mode_artifacts"},
                            {"builtinName": "guidelines"},
                            {"builtinName": "communication_style"},
                        ],
                        "tools": [
                            {"name": "view_file", "builtin": True},
                            {"name": "run_command", "builtin": True},
                            {"name": "manage_task", "builtin": True},
                            {"name": "send_message", "builtin": True},
                            {"name": "schedule", "builtin": True},
                            {"name": "invoke_subagent", "builtin": True},
                            {"name": "define_subagent", "builtin": True},
                            {"name": "manage_subagents", "builtin": True},
                            {"name": "write_to_file", "builtin": True},
                            {"name": "replace_file_content", "builtin": True},
                            {"name": "generate_image", "builtin": True},
                            {"name": "read_url_content", "builtin": True},
                            {"name": "search_web", "builtin": True},
                            {"name": "find_by_name", "builtin": True},
                            {"name": "grep_search", "builtin": True},
                            {"name": "list_dir", "builtin": True},
                            {"name": "ask_question", "builtin": True},
                        ],
                        "forceDisableFundamentalComponents": True,
                    },
                    "customizationDiscoveryConfig": {
                        "skills": {
                            "builtinSkillNames": [
                                "agy-customizations",
                                "antigravity_guide",
                                "generative_ui",
                                "migrate-workflows",
                            ]
                        },
                        "agents": {
                            "inheritUser": True,
                            "builtinAgentNames": ["self", "research", "browser"],
                        },
                    },
                    "requestedModel": {"model": detected_model},
                    "toolConfig": {
                        "runCommand": {
                            "autoCommandConfig": {
                                "autoExecutionPolicy": "CASCADE_COMMANDS_AUTO_EXECUTION_PROCEED_IN_SANDBOX"
                            }
                        },
                        "notifyUser": {},
                        "wait": {"enabled": False},
                    },
                    "knowledgeConfig": {},
                    "useAiCredits": False,
                    "supportsLatexRendering": True,
                },
                "executorConfig": {"useCoreDirect": True},
                "conversationHistoryConfig": {},
            }
        else:
            pc = found_cfg.setdefault("plannerConfig", {})
            if not pc.get("requestedModel") and not pc.get("planModel"):
                pc["requestedModel"] = {"model": detected_model}
            found_cfg.setdefault("executorConfig", {"useCoreDirect": True})
            found_cfg.setdefault("conversationHistoryConfig", {})

        cls._CASCADE_CONFIG_CACHE[cascade_id] = found_cfg
        return found_cfg

    @classmethod
    def send_cascade_message(cls, cascade_id: str, text: str) -> dict:
        """Send a user prompt directly into a live Antigravity conversation via SendUserCascadeMessage."""
        clean = text.strip()
        if not cascade_id or not clean:
            return {"ok": False, "error": "cascade_id ou texto vazio"}
        cascade_config = cls._get_cascade_config_for_send(cascade_id)
        payload: Dict[str, Any] = {
            "cascadeId": cascade_id,
            "items": [{"text": clean}],
            "cascadeConfig": cascade_config,
        }
        resp = cls.call_rpc("SendUserCascadeMessage", payload, timeout=6.0, cascade_id=cascade_id)
        if resp is not None:
            ep = cls._CASCADE_ENDPOINT_MAP.get(cascade_id)
            port_lbl = f":{ep[0]}" if ep else ""
            req_model = (
                ((cascade_config.get("plannerConfig") or {}).get("requestedModel") or {}).get("model")
                or "default"
            )
            return {
                "ok": True,
                "method": f"Antigravity ConnectRPC{port_lbl} (SendUserCascadeMessage -> {cascade_id[:8]} [{req_model}])",
                "response": resp,
            }
        return {"ok": False, "error": "LanguageServerService indisponível ou conversa inativa"}

    @classmethod
    def cancel_cascade_invocation(cls, cascade_id: str) -> dict:
        """Cancel/stop a running Antigravity turn and any active steps via CancelCascadeInvocation / CancelCascadeSteps ConnectRPC."""
        if not cascade_id:
            return {"ok": False, "error": "cascade_id ausente"}

        running_indices: List[int] = []
        try:
            traj_resp = cls.get_live_trajectory(cascade_id) or {}
            traj = traj_resp.get("trajectory") or {}
            steps = traj.get("steps") or []
            for idx, st in enumerate(steps):
                if not isinstance(st, dict):
                    continue
                st_status = str(st.get("status") or "")
                if st_status in (
                    "CORTEX_STEP_STATUS_RUNNING",
                    "CORTEX_STEP_STATUS_PENDING",
                    "CORTEX_STEP_STATUS_WAITING",
                ):
                    meta = st.get("metadata") or {}
                    src_info = meta.get("sourceTrajectoryStepInfo") or {}
                    step_idx = src_info.get("stepIndex")
                    running_indices.append(int(step_idx) if step_idx is not None else idx)
        except Exception:
            pass

        methods_called: List[str] = []
        ok_any = False

        resp_inv = cls.call_rpc(
            "CancelCascadeInvocation",
            {"cascadeId": cascade_id, "killBackgroundTasks": True},
            timeout=4.0,
            cascade_id=cascade_id,
        )
        if resp_inv is not None:
            ok_any = True
            methods_called.append("CancelCascadeInvocation")

        if running_indices:
            resp_steps = cls.call_rpc(
                "CancelCascadeSteps",
                {"cascadeId": cascade_id, "stepIndices": running_indices},
                timeout=3.0,
                cascade_id=cascade_id,
            )
            if resp_steps is not None:
                ok_any = True
                methods_called.append(f"CancelCascadeSteps({len(running_indices)})")

        if not ok_any:
            resp_tree = cls.call_rpc(
                "ForceStopCascadeTree",
                {"conversationId": cascade_id},
                timeout=3.0,
                cascade_id=cascade_id,
            )
            if resp_tree is not None:
                ok_any = True
                methods_called.append("ForceStopCascadeTree")

        if ok_any:
            ep = cls._CASCADE_ENDPOINT_MAP.get(cascade_id)
            port_lbl = f":{ep[0]}" if ep else ""
            return {
                "ok": True,
                "method": f"Antigravity ConnectRPC{port_lbl} ({' + '.join(methods_called)} -> {cascade_id[:8]})",
                "cancelled_steps": running_indices,
            }
        return {"ok": False, "error": "Não foi possível cancelar a execução via LanguageServerService ConnectRPC"}

    @classmethod
    def _resolve_or_create_project_id(cls, resolved_repo: Path, workspace_uri: str) -> Optional[str]:
        """Resolve the Antigravity Project ID for a workspace URI from ~/.gemini/config/projects/*.json (or create one) so new conversations appear under their Project in the Antigravity sidebar."""
        projects_dir = Path.home() / ".gemini" / "config" / "projects"
        norm_uri = workspace_uri.rstrip("/")
        repo_name_lower = (resolved_repo.name or "").lower()

        if projects_dir.exists():
            name_match_id: Optional[str] = None
            for jf in sorted(projects_dir.glob("*.json")):
                if jf.name == "default-cli-project.json":
                    continue
                try:
                    pdata = json.loads(jf.read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    continue
                pid = str(pdata.get("id") or "").strip()
                if not pid:
                    continue
                resources = ((pdata.get("projectResources") or {}).get("resources")) or []
                for res in resources:
                    if not isinstance(res, dict):
                        continue
                    f_uri = str(res.get("folderUri") or "").rstrip("/")
                    gf_uri = str(((res.get("gitFolder") or {}).get("folderUri")) or "").rstrip("/")
                    if norm_uri in (f_uri, gf_uri):
                        return pid
                if str(pdata.get("name") or "").strip().lower() == repo_name_lower and not name_match_id:
                    name_match_id = pid
            if name_match_id:
                return name_match_id

        # Also check live trajectories for an existing projectId mapped to this workspace
        try:
            for _, tsum in cls.get_all_live_trajectories().items():
                tmeta = (tsum.get("trajectoryMetadata") if isinstance(tsum, dict) else None) or {}
                pid = str(tmeta.get("projectId") or "").strip()
                if not pid:
                    continue
                for w_uri in tmeta.get("workspaceUris") or []:
                    if str(w_uri).rstrip("/") == norm_uri:
                        return pid
        except Exception:
            pass

        # Create a new Antigravity Project for this repository so it shows up in the sidebar
        try:
            import uuid as _uuid
            new_pid = str(_uuid.uuid4())
            has_git = (resolved_repo / ".git").exists()
            res_entry = (
                {"gitFolder": {"folderUri": workspace_uri}}
                if has_git
                else {"folderUri": workspace_uri}
            )
            proj_obj = {
                "id": new_pid,
                "name": resolved_repo.name or "workspace",
                "projectResources": {"resources": [res_entry]},
                "settings": {},
                "isWorkspaceOnly": False,
            }
            cls.call_rpc("CreateProject", {"project": proj_obj}, timeout=2.5)
            if projects_dir.exists():
                target_jf = projects_dir / f"{new_pid}.json"
                if not target_jf.exists():
                    target_jf.write_text(json.dumps(proj_obj, indent=2), encoding="utf-8")
            return new_pid
        except Exception:
            return None

    @classmethod
    def create_cascade(
        cls,
        repo_path: str,
        title: Optional[str] = None,
        initial_prompt: Optional[str] = None,
    ) -> dict:
        """Create a brand-new conversation (Cascade) natively inside Antigravity via StartCascade + SendUserCascadeMessage."""
        raw_repo = (repo_path or "").strip()
        resolved = Path(raw_repo).expanduser() if raw_repo else (Path.home() / "Projects")
        if not resolved.is_absolute() or not resolved.exists():
            candidate = Path.home() / "Projects" / (resolved.name or raw_repo.split("/")[-1])
            if candidate.exists():
                resolved = candidate
            elif not resolved.is_absolute():
                resolved = Path.home() / "Projects" / raw_repo

        resolved_str = str(resolved)
        workspace_uri = resolved.as_uri() if resolved.is_absolute() else f"file://{resolved_str}"

        # Ensure Antigravity is tracking this workspace directory
        try:
            cls.call_rpc("AddTrackedWorkspace", {"workspace": resolved_str}, timeout=2.5)
        except Exception:
            pass

        # Resolve the Antigravity Project ID so the conversation appears under the Project in the Antigravity sidebar
        project_id = cls._resolve_or_create_project_id(resolved, workspace_uri)

        start_resp = None
        if project_id:
            start_resp = cls.call_rpc(
                "StartCascade",
                {
                    "source": "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT",
                    "projectEnvConfig": {
                        "projectId": project_id,
                        "defaultProjectEnvironment": {},
                    },
                },
                timeout=5.0,
            )

        if not isinstance(start_resp, dict) or not start_resp.get("cascadeId"):
            start_resp = cls.call_rpc(
                "StartCascade",
                {
                    "source": "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT",
                    "workspaceUris": [workspace_uri],
                    "overrideWorkspaceUris": [workspace_uri],
                },
                timeout=5.0,
            )

        if not isinstance(start_resp, dict) or not start_resp.get("cascadeId"):
            return {
                "ok": False,
                "error": "Não foi possível criar a conversa via StartCascade no Antigravity LanguageServerService.",
            }

        cascade_id = str(start_resp["cascadeId"]).strip()

        clean_title = (title or "").strip()
        if clean_title:
            try:
                cls.call_rpc(
                    "UpdateConversationAnnotations",
                    {
                        "cascadeId": cascade_id,
                        "annotations": {"title": clean_title},
                        "mergeAnnotations": True,
                    },
                    timeout=2.5,
                    cascade_id=cascade_id,
                )
            except Exception:
                pass

        prompt_res = None
        clean_prompt = (initial_prompt or "").strip()
        if clean_prompt:
            prompt_res = cls.send_cascade_message(cascade_id, clean_prompt)

        return {
            "ok": True,
            "cascade_id": cascade_id,
            "short_id": cascade_id[:8],
            "project_id": project_id,
            "repo_path": resolved_str,
            "repo_name": resolved.name or "workspace",
            "title": clean_title or clean_prompt[:65] or f"Sessão Antigravity #{cascade_id[:4]}",
            "prompt_sent": bool(prompt_res and prompt_res.get("ok")),
            "prompt_result": prompt_res,
        }

    @classmethod
    def resolve_cascade_interaction(
        cls,
        cascade_id: str,
        action: str,
        custom_text: Optional[str] = None,
    ) -> dict:
        """Approve/deny a pending Antigravity tool interaction via HandleCascadeUserInteraction ConnectRPC."""
        if not cascade_id:
            return {"ok": False, "error": "cascade_id ausente"}

        confirm = action.lower() not in ("deny", "3", "n", "no", "nao", "não", "esc")
        traj_resp = cls.get_live_trajectory(cascade_id) or {}
        traj = traj_resp.get("trajectory") or {}
        trajectory_id = traj.get("trajectoryId") or cascade_id
        steps = traj.get("steps") or []

        target_step: Optional[dict] = None
        target_step_index: Optional[int] = None

        # Pass 1: look for a step explicitly in WAITING state or with requestedInteraction
        for idx in range(len(steps) - 1, -1, -1):
            st = steps[idx]
            if not isinstance(st, dict):
                continue
            st_status = str(st.get("status") or "")
            if st_status in (
                "CORTEX_STEP_STATUS_DONE",
                "CORTEX_STEP_STATUS_ERROR",
                "CORTEX_STEP_STATUS_CANCELED",
                "CORTEX_STEP_STATUS_CANCELLED",
            ):
                continue
            if st.get("requestedInteraction") or st_status == "CORTEX_STEP_STATUS_WAITING":
                target_step = st
                meta = st.get("metadata") or {}
                src_info = meta.get("sourceTrajectoryStepInfo") or {}
                target_step_index = src_info.get("stepIndex")
                if target_step_index is None:
                    target_step_index = idx
                break

        # Pass 2: fallback to most recent PENDING/RUNNING tool step if none explicitly marked WAITING yet
        if target_step is None:
            for idx in range(len(steps) - 1, -1, -1):
                st = steps[idx]
                if not isinstance(st, dict):
                    continue
                st_status = str(st.get("status") or "")
                st_type = str(st.get("type") or "")
                if st_status in ("CORTEX_STEP_STATUS_PENDING", "CORTEX_STEP_STATUS_RUNNING") and st_type not in (
                    "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
                    "CORTEX_STEP_TYPE_USER_INPUT",
                ):
                    target_step = st
                    meta = st.get("metadata") or {}
                    src_info = meta.get("sourceTrajectoryStepInfo") or {}
                    target_step_index = src_info.get("stepIndex")
                    if target_step_index is None:
                        target_step_index = idx
                    break

        if target_step is not None and target_step_index is not None:
            req_inter = target_step.get("requestedInteraction") or {}
            scope_str = (
                "PERMISSION_SCOPE_CONVERSATION"
                if action.lower() in ("always", "2")
                else "PERMISSION_SCOPE_ONCE"
            )
            interaction: Dict[str, Any] = {
                "trajectoryId": trajectory_id,
                "stepIndex": int(target_step_index),
            }
            # Inspect req_inter keys first (even run_command steps use "permission" in requestedInteraction)
            if "permission" in req_inter:
                perm_payload: Dict[str, Any] = {
                    "allow": confirm,
                    "scope": scope_str,
                }
                if custom_text and custom_text.strip() and not confirm:
                    perm_payload["userDenyInstruction"] = custom_text.strip()
                interaction["permission"] = perm_payload
            elif "filePermission" in req_inter:
                fp = req_inter.get("filePermission") or {}
                fp_payload: Dict[str, Any] = {
                    "allow": confirm,
                    "scope": scope_str,
                }
                if fp.get("absolutePathUri"):
                    fp_payload["absolutePathUri"] = fp["absolutePathUri"]
                interaction["filePermission"] = fp_payload
            elif "runCommand" in req_inter:
                rc = req_inter.get("runCommand") or target_step.get("runCommand") or {}
                proposed = (
                    rc.get("proposedCommandLine")
                    or rc.get("commandLine")
                    or rc.get("command")
                    or ""
                )
                submitted = custom_text.strip() if custom_text and custom_text.strip() else proposed
                interaction["runCommand"] = {
                    "confirm": confirm,
                    "proposedCommandLine": proposed,
                    "submittedCommandLine": submitted,
                }
            elif "approvalInteraction" in req_inter:
                interaction["approvalInteraction"] = {"confirm": confirm}
            elif "mcp" in req_inter:
                interaction["mcp"] = {"confirm": confirm}
            elif "readUrlContent" in req_inter:
                interaction["readUrlContent"] = {"confirm": confirm}
            elif "openBrowserUrl" in req_inter:
                interaction["openBrowserUrl"] = {"confirm": confirm}
            elif "browserAction" in req_inter:
                interaction["browserAction"] = {"confirm": confirm}
            elif "executeBrowserJavascript" in req_inter:
                interaction["executeBrowserJavascript"] = {"confirm": confirm}
            elif "captureBrowserScreenshot" in req_inter:
                interaction["captureBrowserScreenshot"] = {"confirm": confirm}
            elif "clickBrowserPixel" in req_inter:
                interaction["clickBrowserPixel"] = {"confirm": confirm}
            elif "sendCommandInput" in req_inter:
                interaction["sendCommandInput"] = {"confirm": confirm}
            else:
                interaction["permission"] = {
                    "allow": confirm,
                    "scope": scope_str,
                }

            rpc_res = cls.call_rpc(
                "HandleCascadeUserInteraction",
                {"cascadeId": cascade_id, "interaction": interaction},
                timeout=4.0,
                cascade_id=cascade_id,
            )
            if rpc_res is not None:
                return {
                    "ok": True,
                    "method": f"Antigravity ConnectRPC (HandleCascadeUserInteraction step #{target_step_index})",
                    "response": rpc_res,
                }
            return {
                "ok": False,
                "error": f"HandleCascadeUserInteraction falhou para o step #{target_step_index}",
            }

        # Fallback only when no tool step is waiting (e.g., plan approval after turn ended)
        reply_text = custom_text.strip() if custom_text and custom_text.strip() else ("Proceed" if confirm else "Stop")
        return cls.send_cascade_message(cascade_id, reply_text)

    @staticmethod
    def _read_annotation(conv_id: str) -> tuple[Optional[str], float]:
        """Read title and last_user_view_time from ~/.gemini/antigravity/annotations/<conv_id>.pbtxt."""
        ann_path = Path.home() / ".gemini" / "antigravity" / "annotations" / f"{conv_id}.pbtxt"
        if not ann_path.exists():
            return None, 0.0
        try:
            text = ann_path.read_text(encoding="utf-8", errors="replace")
            title_match = re.search(r'title:\s*"((?:[^"\\]|\\.)*)"', text)
            sec_match = re.search(r"seconds:\s*(\d+)", text)
            title = title_match.group(1).replace('\\"', '"') if title_match else None
            ts = float(sec_match.group(1)) if sec_match else 0.0
            return title, ts
        except Exception:
            return None, 0.0

    @classmethod
    def update_conversation_title(cls, conv_id: str, new_title: str) -> bool:
        """Synchronize workspace title back into Antigravity ConnectRPC + ~/.gemini/antigravity/annotations/<conv_id>.pbtxt."""
        rpc_ok = False
        try:
            res = cls.call_rpc(
                "UpdateConversationAnnotations",
                {
                    "cascadeId": conv_id,
                    "annotations": {"title": new_title},
                    "mergeAnnotations": True,
                },
                timeout=2.0,
            )
            rpc_ok = res is not None
        except Exception:
            pass

        ann_dir = Path.home() / ".gemini" / "antigravity" / "annotations"
        ann_path = ann_dir / f"{conv_id}.pbtxt"
        safe_title = new_title.replace("\\", "\\\\").replace('"', '\\"')
        try:
            ann_dir.mkdir(parents=True, exist_ok=True)
            if ann_path.exists():
                text = ann_path.read_text(encoding="utf-8", errors="replace")
                if re.search(r'title:\s*"(?:[^"\\]|\\.)*"', text):
                    updated = re.sub(
                        r'title:\s*"(?:[^"\\]|\\.)*"',
                        f'title:"{safe_title}"',
                        text,
                        count=1,
                    )
                else:
                    updated = f'title:"{safe_title}" ' + text
                ann_path.write_text(updated, encoding="utf-8")
            else:
                now_sec = int(time.time())
                ann_path.write_text(
                    f'title:"{safe_title}" last_user_view_time:{{seconds:{now_sec} nanos:0}}\n',
                    encoding="utf-8",
                )
            return True
        except Exception:
            return rpc_ok

    @staticmethod
    def _extract_repo_from_path(val: str) -> Optional[tuple[str, str]]:
        """Given an absolute file/directory path inside ~/Projects, return (repo_name, repo_path)."""
        projects_root = Path.home() / "Projects"
        if "/Projects/" not in val:
            return None
        rel = val.split("/Projects/", 1)[1].strip("/")
        if not rel:
            return None
        parts = rel.split("/")
        # Check if a 2-level or 3-level nested repo exists (e.g., and_neon/mobile-android/apps)
        for depth in (3, 2, 1):
            if len(parts) >= depth:
                candidate = projects_root.joinpath(*parts[:depth])
                if candidate.exists() and candidate.is_dir():
                    if (candidate / ".git").exists() or depth == 1:
                        return "/".join(parts[:depth]), str(candidate)
        first = parts[0]
        return first, str(projects_root / first)

    def discover_live_agents(self, max_age_minutes: int = 43200) -> List[AgentUnit]:
        brain_dir = Path.home() / ".gemini" / "antigravity" / "brain"
        if not brain_dir.exists():
            return []

        live_summaries = self.get_all_live_trajectories()

        units: List[AgentUnit] = []
        conv_dirs = []
        seen_conv_ids = set()
        try:
            entries = list(brain_dir.iterdir())
        except OSError:
            entries = []
        for conv_dir in entries:
            try:
                if not conv_dir.is_dir():
                    continue
                transcript = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
                if not transcript.exists():
                    if conv_dir.name in live_summaries:
                        conv_dirs.append((time.time(), conv_dir, transcript))
                        seen_conv_ids.add(conv_dir.name)
                    continue
                mtime = transcript.stat().st_mtime
                conv_dirs.append((mtime, conv_dir, transcript))
                seen_conv_ids.add(conv_dir.name)
            except OSError:
                continue

        # Include any brand-new live cascades from ConnectRPC whose brain/<id> dir isn't flushed yet
        for cid, lsum in live_summaries.items():
            if cid not in seen_conv_ids:
                cdir = brain_dir / cid
                tr = cdir / ".system_generated" / "logs" / "transcript.jsonl"
                conv_dirs.append((time.time(), cdir, tr))
                seen_conv_ids.add(cid)

        conv_dirs.sort(key=lambda item: item[0], reverse=True)

        for mtime, conv_dir, transcript in conv_dirs[:18]:
            unit = self._parse_conversation(
                conv_dir,
                transcript,
                mtime,
                live_summary=live_summaries.get(conv_dir.name),
            )
            if unit:
                units.append(unit)

        return units

    def _parse_conversation(
        self,
        conv_dir: Path,
        transcript: Path,
        mtime: float,
        live_summary: Optional[dict] = None,
    ) -> Optional[AgentUnit]:
        conv_id = conv_dir.name
        short_id = conv_id[:8]
        ann_title, ann_ts = self._read_annotation(conv_id)
        branch_name = "main"

        if isinstance(live_summary, dict):
            live_ann = live_summary.get("annotations") or {}
            live_title = live_ann.get("title") or live_summary.get("summary")
            if isinstance(live_title, str) and live_title.strip():
                ann_title = live_title.strip()
            ws_list = live_summary.get("workspaces") or []
            if isinstance(ws_list, list) and ws_list:
                ws0 = ws_list[0] if isinstance(ws_list[0], dict) else {}
                if isinstance(ws0.get("branchName"), str) and ws0["branchName"].strip():
                    branch_name = ws0["branchName"].strip()

        steps_count = 0
        last_thought = ""
        current_action = "Sessão Antigravity pronta."
        active_tool: Optional[str] = None
        repo_path = str(Path.home() / "Projects")
        repo_name = "antigravity-workspace"
        if isinstance(live_summary, dict):
            ws_list = live_summary.get("workspaces") or []
            if isinstance(ws_list, list) and ws_list and isinstance(ws_list[0], dict):
                uri = str(ws_list[0].get("workspaceFolderAbsoluteUri") or "")
                if uri.startswith("file://"):
                    fs_path = urllib.parse.unquote(uri.replace("file://", "", 1))
                    extracted = self._extract_repo_from_path(fs_path)
                    if extracted:
                        repo_name, repo_path = extracted
        game_state = AgentGameState.CHOMPING
        pending_perm: Optional[PermissionRequestItem] = None
        first_user_prompt = ""
        terminal_tail: List[str] = []
        pending_tool_info: Optional[dict] = None
        awaiting_artifact_feedback = False
        turn_completed = False
        last_step_type = ""
        last_model_text = ""

        transcript_size_bytes = 0
        bytes_since_compaction = 0
        truncated_steps_count = 0
        tool_events_history: List[Dict[str, Any]] = []
        recent_interruptions = 0
        last_user_input_epoch = 0.0
        last_step_epoch = 0.0

        def _parse_iso_epoch(ts_val: Any) -> float:
            if not isinstance(ts_val, str) or not ts_val:
                return 0.0
            try:
                return datetime.fromisoformat(ts_val.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0.0

        try:
            transcript_size_bytes = transcript.stat().st_size
            bytes_since_compaction = transcript_size_bytes
            with transcript.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            steps_count = len(lines)

            # Scan a sample from start + tail to find the workspace repository and first user prompt
            scan_lines = lines[:12] + (lines[-30:] if len(lines) > 12 else [])
            for raw in scan_lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    step = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if not first_user_prompt and step.get("type") == "USER_INPUT":
                    content = str(step.get("content") or "")
                    if "<USER_REQUEST>" in content:
                        content = content.split("<USER_REQUEST>", 1)[1].split("</USER_REQUEST>", 1)[0]
                    first_user_prompt = " ".join(content.strip().split())[:65]

                for tc in step.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    args = tc.get("arguments") or tc.get("args") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    if not isinstance(args, dict):
                        continue
                    for path_key in ("Cwd", "SearchDirectory", "SearchPath", "DirectoryPath", "TargetFile", "AbsolutePath"):
                        val = args.get(path_key)
                        if isinstance(val, str):
                             clean_val = val.strip('"')
                             extracted = self._extract_repo_from_path(clean_val)
                             if extracted:
                                 repo_name, repo_path = extracted

            # Always use the last 80 lines of transcript.jsonl as the authoritative step sequence so huge lines in
            # transcript_full.jsonl never cause the final steps to be missed, and overlay untruncated fields by step_index.
            full_transcript = transcript.parent / "transcript_full.jsonl"
            full_steps_by_idx: Dict[Any, dict] = {}
            if full_transcript.exists():
                try:
                    st_full = full_transcript.stat()
                    mtime = max(mtime, st_full.st_mtime)
                    fsize = st_full.st_size
                    with full_transcript.open("rb") as bf:
                        if fsize > 1048576:
                            bf.seek(fsize - 1048576)
                            raw_chunk = bf.read().decode("utf-8", errors="replace")
                            chunk_lines = raw_chunk.splitlines()[1:]
                        else:
                            raw_chunk = bf.read().decode("utf-8", errors="replace")
                            chunk_lines = raw_chunk.splitlines()
                    for fln in chunk_lines[-80:]:
                        fln = fln.strip()
                        if not fln:
                            continue
                        try:
                            fobj = json.loads(fln)
                            sidx = fobj.get("step_index")
                            if sidx is not None:
                                full_steps_by_idx[sidx] = fobj
                        except Exception:
                            continue
                except Exception:
                    pass

            tail = lines[-80:] if len(lines) > 80 else lines
            accum_tail_bytes = 0
            saw_compaction_in_tail = False

            for idx_tail, raw in enumerate(tail):
                raw_stripped = raw.strip()
                if not raw_stripped:
                    continue
                accum_tail_bytes += len(raw.encode("utf-8", errors="replace"))
                try:
                    raw_step = json.loads(raw_stripped)
                except json.JSONDecodeError:
                    continue
                sidx = raw_step.get("step_index")
                step = full_steps_by_idx[sidx] if (sidx is not None and sidx in full_steps_by_idx) else raw_step

                ev_ts = _parse_iso_epoch(step.get("created_at"))
                if ev_ts > 0:
                    last_step_epoch = ev_ts

                stype = step.get("type") or ""
                sstatus = str(step.get("status") or "").upper()
                last_step_type = stype
                content_str = step.get("content") if isinstance(step.get("content"), str) else ""

                if "CANCEL" in sstatus or "INTERRUPT" in sstatus:
                    if idx_tail >= max(0, len(tail) - 30):
                        recent_interruptions += 1

                if stype == "USER_INPUT":
                    if ev_ts > 0:
                        last_user_input_epoch = ev_ts
                    if "<CONTEXT_SUMMARY>" in content_str:
                        saw_compaction_in_tail = True
                        accum_tail_bytes = len(raw.encode("utf-8", errors="replace"))
                        truncated_steps_count = 0
                        if "<USER_REQUEST>" not in content_str:
                            continue
                    pending_tool_info = None
                    pending_perm = None
                    awaiting_artifact_feedback = False
                    turn_completed = False
                    if "<USER_REQUEST>" in content_str:
                        req_txt = content_str.split("<USER_REQUEST>", 1)[1].split("</USER_REQUEST>", 1)[0].strip()
                    else:
                        req_txt = content_str.split("<ADDITIONAL_METADATA>", 1)[0].strip()
                    if req_txt:
                        req_lines = [ln.rstrip() for ln in req_txt.splitlines()]
                        if req_lines:
                            terminal_tail.append(f"❯ {req_lines[0]}")
                            for rln in req_lines[1:]:
                                terminal_tail.append(f"  {rln}")
                    continue

                if stype != "PLANNER_RESPONSE":
                    pending_tool_info = None
                    pending_perm = None
                    turn_completed = False
                    if (
                        isinstance(raw_step.get("truncated_fields"), list)
                        and "content" in raw_step.get("truncated_fields", [])
                    ):
                        truncated_steps_count += 1
                    lower_content = content_str.lower()
                    header_slice = lower_content[:280]
                    is_step_err = (
                        (sstatus == "ERROR")
                        or (
                            "the command exited with code " in header_slice
                            and "the command exited with code 0" not in header_slice
                        )
                        or ("targetcontent not found" in header_slice)
                        or ("cannot find targetcontent" in header_slice)
                    )
                    if is_step_err and tool_events_history:
                        tool_events_history[-1]["is_err"] = True
                    if "requested user feedback" in lower_content:
                        awaiting_artifact_feedback = True
                        terminal_tail.append("  ⎿  ⚡ Artefato criado — aguardando sua revisão/aprovação")
                    elif content_str:
                        out_lines = [
                            ln.rstrip()
                            for ln in content_str.splitlines()
                            if ln.strip() and not ln.startswith("Created At:") and not ln.startswith("Completed At:")
                        ]
                        for oln in out_lines[:60]:
                            terminal_tail.append(f"  ⎿  {oln}")
                        if len(out_lines) > 60:
                            terminal_tail.append(f"  ⎿  … (+{len(out_lines) - 60} linhas de output)")
                    continue

                thinking = step.get("thinking")
                if isinstance(thinking, str) and thinking.strip():
                    last_thought = thinking.strip()[:300]
                    th_lines = [ln.rstrip() for ln in thinking.strip().splitlines() if ln.strip()]
                    if th_lines:
                        terminal_tail.append(f"✻ Thinking: {th_lines[0]}")
                        for tln in th_lines[1:]:
                            terminal_tail.append(f"  ✻ {tln}")

                # Always log full assistant response text even if the same step also invokes tools
                if content_str.strip():
                    last_model_text = content_str.strip()
                    m_lines = [ln.rstrip() for ln in last_model_text.splitlines()]
                    if m_lines:
                        non_empty = [ln.strip() for ln in m_lines if ln.strip()]
                        if non_empty:
                            current_action = non_empty[-1][:140]
                        terminal_tail.append(f"● {m_lines[0]}")
                        for mln in m_lines[1:]:
                            terminal_tail.append(f"  {mln}")

                tool_calls = step.get("tool_calls") or []
                if isinstance(tool_calls, list) and tool_calls:
                    turn_completed = False
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        t_name = tc.get("name") or tc.get("tool_name") or "tool"
                        args = tc.get("arguments") or tc.get("args") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                args = {}
                        if not isinstance(args, dict):
                            args = {}
                        active_tool = t_name
                        summary = (
                            args.get("CommandLine")
                            or args.get("TargetFile")
                            or args.get("AbsolutePath")
                            or args.get("SearchPath")
                            or args.get("DirectoryPath")
                            or args.get("Url")
                            or args.get("toolAction")
                            or args.get("toolSummary")
                            or t_name
                        )
                        if isinstance(summary, str):
                            summary = summary.strip('"')
                        target_short = ""
                        if args.get("TargetFile") or args.get("AbsolutePath"):
                            target_short = Path(str(args.get("TargetFile") or args.get("AbsolutePath")).strip('"')).name
                        elif args.get("CommandLine"):
                            target_short = str(args.get("CommandLine")).strip().splitlines()[0][:36]
                        elif args.get("Query"):
                            target_short = str(args.get("Query")).strip()[:30]
                        else:
                            target_short = str(summary)[:32]
                        is_read_only = t_name in (
                            "view_file",
                            "grep_search",
                            "find_by_name",
                            "list_dir",
                            "search_web",
                            "read_url_content",
                            "list_resources",
                            "read_resource",
                        )
                        tool_events_history.append(
                            {
                                "tool": str(t_name),
                                "target": target_short,
                                "is_err": ("ERROR" in sstatus),
                                "is_read_only": is_read_only,
                            }
                        )
                        extra_detail = ""
                        if args.get("Query"):
                            extra_detail = f" · Query={args.get('Query')}"
                        elif args.get("Instruction"):
                            extra_detail = f" · {args.get('Instruction')}"
                        current_action = f"[{t_name}] {summary}"
                        terminal_tail.append(f"● {t_name}({summary}{extra_detail})")

                        bypass = str(args.get("BypassSandbox", "")).lower() == "true"
                        art_meta = args.get("ArtifactMetadata")
                        if isinstance(art_meta, str):
                            try:
                                art_meta = json.loads(art_meta)
                            except Exception:
                                art_meta = {}
                        req_fb = isinstance(art_meta, dict) and str(art_meta.get("RequestFeedback", "")).lower() == "true"
                        awaiting_artifact_feedback = req_fb

                        pending_tool_info = {
                            "name": t_name,
                            "summary": str(summary),
                            "bypass": bypass,
                            "req_fb": req_fb,
                            "args": args,
                            "agent_context": (last_model_text or last_thought or "")[-600:],
                        }

                        if t_name == "ask_question":
                            game_state = AgentGameState.POWER_PELLET_WAIT
                            q_list = args.get("questions") if isinstance(args.get("questions"), list) else []
                            q_texts = []
                            q_opts: List[str] = []
                            for q_item in q_list:
                                if isinstance(q_item, dict):
                                    if q_item.get("question"):
                                        q_texts.append(str(q_item["question"]))
                                    if isinstance(q_item.get("options"), list):
                                        q_opts.extend([str(o) for o in q_item["options"] if o])
                            full_q = "\n".join(q_texts) if q_texts else str(summary)
                            pending_perm = PermissionRequestItem(
                                id=f"perm-agy-q-{short_id}",
                                agent_id=f"agy-{short_id}",
                                agent_name=ann_title or f"AGY #{short_id}",
                                provider="antigravity",
                                repo_name=repo_name,
                                tool_name="ask_question (Pergunta do Agente)",
                                command_preview=full_q,
                                reason=str(args.get("toolAction") or args.get("toolSummary") or "O agente Antigravity fez uma pergunta e aguarda sua decisão."),
                                full_details={
                                    "tool_action": str(args.get("toolAction") or args.get("toolSummary") or ""),
                                    "agent_context": (last_model_text or last_thought or "")[-600:],
                                    "questions": q_list,
                                },
                                options=q_opts,
                                channel="sdk",
                            )
                        elif t_name in ("run_command", "call_mcp_tool"):
                            game_state = AgentGameState.RUNNING_TOOL
                        else:
                            game_state = AgentGameState.CHOMPING
                elif content_str.strip():
                    pending_tool_info = None
                    pending_perm = None
                    turn_completed = True
            if saw_compaction_in_tail and accum_tail_bytes > 0:
                bytes_since_compaction = accum_tail_bytes
            elif accum_tail_bytes > 0 and len(lines) > 80:
                # Check if an earlier <CONTEXT_SUMMARY> exists in lines
                last_comp_idx = -1
                for idx_ln in range(len(lines) - 1, -1, -1):
                    if "<CONTEXT_SUMMARY>" in lines[idx_ln]:
                        last_comp_idx = idx_ln
                        break
                if last_comp_idx >= 0:
                    bytes_since_compaction = sum(
                        len(ln.encode("utf-8", errors="replace")) for ln in lines[last_comp_idx:]
                    )
                else:
                    bytes_since_compaction = min(
                        transcript_size_bytes,
                        max(accum_tail_bytes, int(transcript_size_bytes * 0.55)),
                    )
            elif accum_tail_bytes > 0:
                bytes_since_compaction = accum_tail_bytes
        except Exception:
            pass


        workspace_title = ann_title or first_user_prompt or f"Sessão Antigravity #{short_id[:4]}"
        age_seconds = max(0.0, time.time() - mtime)
        rpc_status = str((live_summary or {}).get("status") or "")
        rpc_step_count = int((live_summary or {}).get("stepCount") or 0)
        rpc_is_current = rpc_step_count >= max(0, steps_count - 1)

        def _build_agy_perm_details(t_name_val: str, t_sum_val: str, tc_args: dict, req_inter_obj: dict) -> tuple[str, str, str, Dict[str, Any], List[str]]:
            """Builds (display_tool_name, full_command_preview, detailed_reason, full_details_dict, options_list)."""
            rc = req_inter_obj.get("runCommand") or {}
            fp = req_inter_obj.get("filePermission") or {}
            ru = req_inter_obj.get("readUrlContent") or {}
            mcp_obj = req_inter_obj.get("mcp") or {}

            tool_action = str(tc_args.get("toolAction") or tc_args.get("toolSummary") or "").strip()
            desc = str(tc_args.get("Description") or tc_args.get("Instruction") or "").strip()
            cwd_val = str(rc.get("cwd") or tc_args.get("Cwd") or repo_path or "").strip()
            raw_path = str(
                fp.get("absolutePathUri")
                or tc_args.get("AbsolutePath")
                or tc_args.get("TargetFile")
                or tc_args.get("SearchPath")
                or tc_args.get("DirectoryPath")
                or ""
            ).replace("file://", "").strip()
            block_reason = str(fp.get("blockReason") or "").strip()
            bypass_flag = str(tc_args.get("BypassSandbox", "")).lower() == "true"
            if not block_reason and bypass_flag:
                block_reason = "BypassSandbox=true (Execução fora do sandbox)"

            line_range = ""
            if tc_args.get("StartLine") or tc_args.get("EndLine"):
                line_range = f"Linhas {tc_args.get('StartLine', 1)} a {tc_args.get('EndLine', 'fim')}"

            cmd_full = str(
                rc.get("proposedCommandLine")
                or rc.get("commandLine")
                or tc_args.get("CommandLine")
                or raw_path
                or ru.get("url")
                or tc_args.get("Url")
                or t_sum_val
            ).strip()

            if line_range and raw_path and line_range not in cmd_full:
                cmd_full = f"{raw_path} ({line_range})"
            if tc_args.get("Query") and f"Query=" not in cmd_full:
                cmd_full = f"{cmd_full} · Query: {tc_args.get('Query')}"
            if mcp_obj or t_name_val == "call_mcp_tool":
                srv = mcp_obj.get("serverName") or tc_args.get("ServerName") or ""
                tn = mcp_obj.get("toolName") or tc_args.get("ToolName") or ""
                m_args = mcp_obj.get("arguments") or tc_args.get("Arguments") or {}
                cmd_full = f"MCP {srv} :: {tn}\nArgumentos: {json.dumps(m_args, ensure_ascii=False)}"

            opts: List[str] = []
            q_list = tc_args.get("questions") if isinstance(tc_args.get("questions"), list) else []
            if q_list:
                q_parts = []
                for qi in q_list:
                    if isinstance(qi, dict):
                        if qi.get("question"):
                            q_parts.append(str(qi["question"]))
                        if isinstance(qi.get("options"), list):
                            opts.extend([str(o) for o in qi["options"] if o])
                if q_parts:
                    cmd_full = "\n".join(q_parts)

            display_tool = t_name_val
            if "filePermission" in req_inter_obj and t_name_val not in ("file_permission",):
                display_tool = f"{t_name_val} (Permissão de Acesso a Arquivo)"
            elif block_reason == "OUTSIDE_WORKSPACE":
                display_tool = f"{t_name_val} (Fora do Workspace)"

            reason_parts = []
            if tool_action:
                reason_parts.append(f"Ação: {tool_action}")
            if desc:
                reason_parts.append(f"Detalhe: {desc}")
            if block_reason:
                if block_reason == "OUTSIDE_WORKSPACE":
                    reason_parts.append("Motivo: Acesso a arquivo/pasta fora do workspace atual")
                else:
                    reason_parts.append(f"Motivo: {block_reason}")
            if cwd_val and t_name_val == "run_command":
                reason_parts.append(f"Diretório (Cwd): {cwd_val}")
            reason_str = " • ".join(reason_parts) if reason_parts else "O agente Antigravity aguarda sua confirmação para executar esta ação."

            diff_old = str(tc_args.get("TargetContent") or "").strip()
            diff_new = str(tc_args.get("ReplacementContent") or tc_args.get("CodeContent") or "").strip()

            filtered_params = {
                k: v
                for k, v in tc_args.items()
                if k not in ("TargetContent", "ReplacementContent", "CodeContent") and v not in (None, "", [], {})
            }

            details: Dict[str, Any] = {
                "tool_action": tool_action,
                "description": desc,
                "cwd": cwd_val,
                "target_path": raw_path,
                "line_range": line_range,
                "block_reason": block_reason,
                "diff_old": diff_old[:1500] if diff_old else "",
                "diff_new": diff_new[:1500] if diff_new else "",
                "agent_context": (last_model_text or last_thought or "")[-800:],
                "parameters": filtered_params,
            }
            return display_tool, cmd_full, reason_str, details, opts

        # Check if a live trajectory step is actively waiting for user interaction even if status is RUNNING
        live_waiting_info: Optional[Dict[str, Any]] = None
        if rpc_status in ("CASCADE_RUN_STATUS_WAITING_FOR_USER", "CASCADE_RUN_STATUS_RUNNING"):
            if rpc_status == "CASCADE_RUN_STATUS_WAITING_FOR_USER" or age_seconds >= 0.3 or (pending_tool_info and pending_tool_info.get("bypass")):
                traj_resp = self.get_live_trajectory(conv_id) or {}
                steps_list = ((traj_resp.get("trajectory") or {}).get("steps") or [])
                for st in reversed(steps_list[-8:]):
                    if not isinstance(st, dict):
                        continue
                    st_status = str(st.get("status") or "")
                    if st_status in (
                        "CORTEX_STEP_STATUS_DONE",
                        "CORTEX_STEP_STATUS_ERROR",
                        "CORTEX_STEP_STATUS_CANCELED",
                        "CORTEX_STEP_STATUS_CANCELLED",
                    ):
                        continue
                    req_inter = st.get("requestedInteraction") or {}
                    if req_inter or st_status == "CORTEX_STEP_STATUS_WAITING":
                        meta_tc = (st.get("metadata") or {}).get("toolCall") or {}
                        tc_name = str(meta_tc.get("name") or "").strip()
                        tc_args_raw = meta_tc.get("argumentsJson") or "{}"
                        tc_args: dict = {}
                        if isinstance(tc_args_raw, str):
                            try:
                                tc_args = json.loads(tc_args_raw)
                            except Exception:
                                tc_args = {}
                        elif isinstance(tc_args_raw, dict):
                            tc_args = tc_args_raw
                        if not tc_args and pending_tool_info and isinstance(pending_tool_info.get("args"), dict):
                            tc_args = pending_tool_info["args"]

                        w_name = tc_name or (pending_tool_info or {}).get("name") or active_tool or "run_command"
                        w_sum = (pending_tool_info or {}).get("summary") or current_action
                        if "runCommand" in req_inter or "runCommand" in st:
                            rc = req_inter.get("runCommand") or st.get("runCommand") or {}
                            w_name = tc_name or "run_command"
                            w_sum = rc.get("proposedCommandLine") or rc.get("commandLine") or w_sum
                        elif "filePermission" in req_inter:
                            fp = req_inter.get("filePermission") or {}
                            w_name = tc_name or "file_permission"
                            w_sum = fp.get("absolutePathUri") or w_sum
                        elif "readUrlContent" in req_inter:
                            w_name = tc_name or "read_url_content"
                        elif "mcp" in req_inter:
                            w_name = tc_name or "call_mcp_tool"

                        disp_tool, cmd_full, reason_str, details, opts = _build_agy_perm_details(
                            str(w_name), str(w_sum), tc_args, req_inter
                        )
                        live_waiting_info = {
                            "tool_name": disp_tool,
                            "command_preview": cmd_full,
                            "reason": reason_str,
                            "full_details": details,
                            "options": opts,
                        }
                        break

        # Authoritative live ConnectRPC status from Antigravity LanguageServerService when available:
        if rpc_status == "CASCADE_RUN_STATUS_WAITING_FOR_USER" or live_waiting_info is not None:
            game_state = AgentGameState.POWER_PELLET_WAIT
            mtime = max(mtime, time.time())
            if live_waiting_info is not None:
                t_name = live_waiting_info["tool_name"]
                t_sum = live_waiting_info["command_preview"]
                reason_str = live_waiting_info["reason"]
                details_dict = live_waiting_info["full_details"]
                opts_list = live_waiting_info["options"]
            else:
                raw_name = (pending_tool_info or {}).get("name") or active_tool or "Antigravity Action"
                raw_sum = (pending_tool_info or {}).get("summary") or current_action
                raw_args = (pending_tool_info or {}).get("args") or {}
                t_name, t_sum, reason_str, details_dict, opts_list = _build_agy_perm_details(
                    str(raw_name), str(raw_sum), raw_args, {}
                )
            active_tool = str(t_name)
            current_action = f"⚡ Aguardando aprovação: {t_name} — {t_sum}"
            terminal_tail.append(f"⚡ Aguardando sua aprovação no Antigravity: [{t_name}] {t_sum}")
            if reason_str:
                terminal_tail.append(f"  ⎿  📋 {reason_str}")
            pending_perm = PermissionRequestItem(
                id=f"perm-agy-cmd-{short_id}-{steps_count}",
                agent_id=f"agy-{short_id}",
                agent_name=workspace_title,
                provider="antigravity",
                repo_name=repo_name,
                tool_name=str(t_name),
                command_preview=str(t_sum),
                reason=reason_str,
                full_details=details_dict,
                options=opts_list,
                channel="sdk",
            )
        elif rpc_status == "CASCADE_RUN_STATUS_RUNNING":
            if pending_tool_info is not None:
                t_name = pending_tool_info["name"]
                t_sum = pending_tool_info["summary"]
                raw_args = pending_tool_info.get("args") or {}
                active_tool = t_name
                if t_name == "ask_question" or pending_tool_info["bypass"]:
                    game_state = AgentGameState.POWER_PELLET_WAIT
                    mtime = max(mtime, time.time())
                    disp_tool, cmd_full, reason_str, details_dict, opts_list = _build_agy_perm_details(
                        str(t_name), str(t_sum), raw_args, {}
                    )
                    current_action = f"⚡ Aguardando aprovação: {disp_tool} — {cmd_full}"
                    terminal_tail.append(f"⚡ Aguardando sua aprovação no Antigravity: [{disp_tool}] {cmd_full}")
                    if reason_str:
                        terminal_tail.append(f"  ⎿  📋 {reason_str}")
                    pending_perm = PermissionRequestItem(
                        id=f"perm-agy-cmd-{short_id}-{steps_count}",
                        agent_id=f"agy-{short_id}",
                        agent_name=workspace_title,
                        provider="antigravity",
                        repo_name=repo_name,
                        tool_name=disp_tool,
                        command_preview=cmd_full,
                        reason=reason_str,
                        full_details=details_dict,
                        options=opts_list,
                        channel="sdk",
                    )
                elif t_name in ("run_command", "call_mcp_tool"):
                    game_state = AgentGameState.RUNNING_TOOL
                    current_action = f"Executando [{t_name}] {t_sum}"
                else:
                    game_state = AgentGameState.CHOMPING
                    current_action = f"Trabalhando [{t_name}] {t_sum}"
            elif last_step_type == "USER_INPUT":
                game_state = AgentGameState.THINKING
                active_tool = "thinking"
                current_action = f"Pensando sobre o prompt • {current_action}"
            else:
                game_state = AgentGameState.CHOMPING if age_seconds < 12.0 else AgentGameState.THINKING
                active_tool = active_tool or "thinking"
                current_action = f"Trabalhando ativamente • {current_action}"
        elif rpc_status in (
            "CASCADE_RUN_STATUS_IDLE",
            "CASCADE_RUN_STATUS_DONE",
            "CASCADE_RUN_STATUS_CANCELED",
            "CASCADE_RUN_STATUS_CANCELLED",
        ):
            if turn_completed and awaiting_artifact_feedback and age_seconds < 120:
                game_state = AgentGameState.POWER_PELLET_WAIT
                preview = last_model_text if last_model_text else current_action
                current_action = f"📋 Plano/Artefato aguardando sua revisão: {preview[:120]}"
                terminal_tail.append("❯ [Aguardando sua revisão do plano/artefato (Proceed) no Antigravity]")
                pending_perm = PermissionRequestItem(
                    id=f"perm-agy-plan-{short_id}",
                    agent_id=f"agy-{short_id}",
                    agent_name=workspace_title,
                    provider="antigravity",
                    repo_name=repo_name,
                    tool_name="Revisão de Plano / Feedback",
                    command_preview=preview,
                    reason="O agente concluiu o plano de ação e aguarda sua aprovação (Proceed) ou resposta.",
                    full_details={"agent_context": preview},
                    channel="sdk",
                )
            elif last_step_type == "USER_INPUT" and age_seconds < 3.5:
                game_state = AgentGameState.THINKING
                active_tool = "thinking"
                current_action = f"Pensando sobre o prompt • {current_action}"
            else:
                game_state = AgentGameState.IDLE
                pending_perm = None
                active_tool = None
                current_action = f"Parado / Pronto • {current_action[:110]}"
                terminal_tail.append("❯ [Turno concluído — aguardando próxima instrução]")
        elif not turn_completed and pending_tool_info is not None and age_seconds < 15.0:
            t_name = pending_tool_info["name"]
            t_sum = pending_tool_info["summary"]
            raw_args = pending_tool_info.get("args") or {}
            active_tool = t_name
            if t_name == "ask_question" or (pending_tool_info["bypass"] and age_seconds >= 1.5):
                game_state = AgentGameState.POWER_PELLET_WAIT
                disp_tool, cmd_full, reason_str, details_dict, opts_list = _build_agy_perm_details(
                    str(t_name), str(t_sum), raw_args, {}
                )
                current_action = f"⚡ Aguardando aprovação: {disp_tool} — {cmd_full}"
                terminal_tail.append(f"⚡ Aguardando sua aprovação no Antigravity: [{disp_tool}] {cmd_full}")
                if pending_perm is None:
                    pending_perm = PermissionRequestItem(
                        id=f"perm-agy-cmd-{short_id}",
                        agent_id=f"agy-{short_id}",
                        agent_name=workspace_title,
                        provider="antigravity",
                        repo_name=repo_name,
                        tool_name=disp_tool,
                        command_preview=cmd_full,
                        reason=reason_str,
                        full_details=details_dict,
                        options=opts_list,
                        channel="sdk",
                    )
            else:
                game_state = (
                    AgentGameState.RUNNING_TOOL
                    if t_name in ("run_command", "call_mcp_tool")
                    else AgentGameState.CHOMPING
                )
        elif not turn_completed and last_step_type in ("USER_INPUT", "GENERIC", "CHECKPOINT") and age_seconds < 120.0:
            game_state = AgentGameState.CHOMPING if age_seconds < 8.0 else AgentGameState.THINKING
            active_tool = active_tool or "thinking"
            current_action = f"Trabalhando ativamente • {current_action[:105]}"
        elif turn_completed and awaiting_artifact_feedback and age_seconds < 120:
            game_state = AgentGameState.POWER_PELLET_WAIT
            preview = last_model_text if last_model_text else current_action
            current_action = f"📋 Plano/Artefato aguardando sua revisão: {preview[:120]}"
            terminal_tail.append("❯ [Aguardando sua revisão do plano/artefato (Proceed) no Antigravity]")
            pending_perm = PermissionRequestItem(
                id=f"perm-agy-plan-{short_id}",
                agent_id=f"agy-{short_id}",
                agent_name=workspace_title,
                provider="antigravity",
                repo_name=repo_name,
                tool_name="Revisão de Plano / Feedback",
                command_preview=preview,
                reason="O agente concluiu o plano de ação e aguarda sua aprovação (Proceed) ou resposta.",
                full_details={"agent_context": preview},
                channel="sdk",
            )
        else:
            game_state = AgentGameState.IDLE
            pending_perm = None
            active_tool = None
            current_action = f"Parado / Pronto • {current_action[:110]}"
            terminal_tail.append("❯ [Turno concluído — aguardando próxima instrução]")

        # Conversations older than 6 hours default to archived (History) so the active workspace stays clean
        default_archived = (game_state == AgentGameState.IDLE) and (age_seconds > (6 * 3600))

        artifacts = [
            p.name
            for p in conv_dir.glob("*")
            if p.is_file() and p.suffix in (".md", ".html", ".json") and not p.name.endswith(".metadata.json")
        ]

        # Estimate active context tokens from transcript size + truncated_fields count
        tokens_used = min(
            200000,
            max(1000, int(bytes_since_compaction / 3.8) + (truncated_steps_count * 1100)),
        )

        recent_15 = tool_events_history[-15:]
        recent_tool_errors = sum(1 for ev in recent_15 if ev.get("is_err"))
        last_error_target = next(
            (str(ev.get("target") or "") for ev in reversed(recent_15) if ev.get("is_err") and ev.get("target")),
            "",
        )

        # Loop / Thrashing detection in last 10 tool calls
        recent_10 = tool_events_history[-10:]
        thrashing_count = 0
        thrashing_target = ""
        if recent_10:
            edit_or_err_counts = Counter(
                (ev["tool"], ev["target"])
                for ev in recent_10
                if ev.get("target")
                and (
                    ev.get("is_err")
                    or ev["tool"] in ("run_command", "grep_search", "find_by_name")
                    or (
                        ev["tool"] in ("replace_file_content", "multi_replace_file_content", "write_to_file")
                        and sum(1 for x in recent_10 if x.get("target") == ev.get("target") and x.get("is_err")) >= 1
                    )
                )
            )
            if edit_or_err_counts:
                (top_tool, top_target), top_cnt = edit_or_err_counts.most_common(1)[0]
                if top_cnt >= 3:
                    thrashing_count = top_cnt
                    thrashing_target = f"{top_target}" if top_target != top_tool else top_tool

        # Overthinking / Search spiral: >=12 read-only tools in last 15 steps without an edit/write
        overthinking_read_only_count = 0
        if len(recent_15) >= 12:
            read_cnt = sum(1 for ev in recent_15 if ev.get("is_read_only"))
            write_cnt = sum(
                1
                for ev in recent_15
                if ev.get("tool") in ("replace_file_content", "multi_replace_file_content", "write_to_file")
            )
            if read_cnt >= 12 and write_cnt == 0 and not turn_completed:
                overthinking_read_only_count = read_cnt

        # Turn elapsed time calculation
        active_elapsed_sec = 0
        if game_state != AgentGameState.IDLE and last_user_input_epoch > 0:
            active_elapsed_sec = int(round(max(0.0, time.time() - last_user_input_epoch)))
        elif last_user_input_epoch > 0 and last_step_epoch >= last_user_input_epoch and age_seconds < 120:
            active_elapsed_sec = int(round(last_step_epoch - last_user_input_epoch))

        health_metrics = compute_context_and_hallucination_metrics(
            tokens_used=tokens_used,
            tokens_limit=200000,
            steps_count=steps_count,
            recent_tool_errors=recent_tool_errors,
            last_error_target=last_error_target,
            thrashing_target=thrashing_target,
            thrashing_count=thrashing_count,
            recent_interruptions=recent_interruptions,
            truncated_steps_count=truncated_steps_count,
            overthinking_read_only_count=overthinking_read_only_count,
            game_state=game_state,
            age_seconds=age_seconds,
            active_elapsed_sec=active_elapsed_sec,
        )

        return AgentUnit(
            id=f"agy-{short_id}",
            name=workspace_title,
            workspace_title=workspace_title,
            session_id=conv_id,
            provider="antigravity",
            repo_name=repo_name,
            repo_path=repo_path,
            branch=branch_name,
            game_state=game_state,
            score_xp=steps_count * 150,
            pellets_eaten=min(24, max(2, steps_count // 3)),
            pellets_total=24,
            context_health_pct=health_metrics["context_health_pct"],
            hallucination_risk_pct=health_metrics["hallucination_risk_pct"],
            health_status=health_metrics["health_status"],
            context_tokens_used=health_metrics["context_tokens_used"],
            context_tokens_limit=health_metrics["context_tokens_limit"],
            active_elapsed_sec=health_metrics["active_elapsed_sec"],
            is_stalled=health_metrics["is_stalled"],
            health_signals=health_metrics["health_signals"],
            current_action=current_action[:150],
            last_thought=last_thought or "Orquestrando tarefas e artefatos na sessão Antigravity.",
            active_tool=active_tool,
            terminal_tail=terminal_tail[-800:],
            artifacts=artifacts[:8],
            pending_permission=pending_perm,
            controllable=True,
            control_mode="sdk",
            neon_color="#0078D9",
            sprite_type="pac_neon",
            is_live_discovered=True,
            archived=default_archived,
            host_app="Antigravity",
            last_active_ts=mtime,
        )

