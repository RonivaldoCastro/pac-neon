from __future__ import annotations

import json
import subprocess
import time
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



class ClaudeAdapter(BaseAgentAdapter):
    """Adapter for Anthropic Claude Code reading ~/.claude/projects/*/*.jsonl and PTY/Hook streams."""

    def get_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            id="claude",
            display_name="Claude Code",
            arcade_title="CLYDE-CLAUDE // SONNET-OPUS",
            neon_color="#FF7A00",        # Neon Arcade Orange
            secondary_color="#FFB800",
            sprite_type="ghost_clyde",
            log_paths=["~/.claude/projects/*/*.jsonl"],
            cli_command="claude",
            supports_pty_wrap=True,
            supports_hooks=True,
        )

    def format_approval_sequence(self, action: str, custom_text: Optional[str] = None) -> str:
        # Claude Code interactive permission prompt usually accepts '1' (Yes), '2' (Yes, don't ask again), '3' / 'n' (No) or 'y'/'n'
        if custom_text:
            return custom_text.rstrip("\r\n") + "\r"
        if action == "allow":
            return "y\r"
        if action == "always":
            return "2\r"
        if action == "deny":
            return "n\r"
        return "y\r"

    def format_approval_Sequence(self, action: str, custom_text: Optional[str] = None) -> str:
        return self.format_approval_sequence(action, custom_text)

    @staticmethod
    def _resolve_pid_cwd(pid: int) -> str:
        """Resolve the current working directory of a live process on macOS/Linux via lsof."""
        import subprocess
        try:
            res = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True,
                text=True,
                timeout=1.2,
            )
            for ln in res.stdout.splitlines():
                if ln.startswith("n") and len(ln) > 1:
                    return ln[1:].strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _resolve_git_branch(cwd: str) -> str:
        """Fast lookup of current git branch from .git/HEAD without spawning git."""
        if not cwd:
            return "main"
        try:
            curr = Path(cwd).expanduser().resolve()
            for p in [curr, *list(curr.parents)[:4]]:
                head_file = p / ".git" / "HEAD"
                if head_file.is_file():
                    raw = head_file.read_text(encoding="utf-8", errors="replace").strip()
                    if raw.startswith("ref: refs/heads/"):
                        return raw.replace("ref: refs/heads/", "").strip()
                    return raw[:12] or "main"
        except Exception:
            pass
        return "main"

    @staticmethod
    def _load_active_claude_sessions() -> dict[str, dict]:
        """Read ~/.claude/sessions/*.json, ~/.claude/ide/*.lock, and live `ps` processes mapping sessionId -> {pid, cwd, tty, host_app, alive, started_at}."""
        sessions_dir = Path.home() / ".claude" / "sessions"
        ide_dir = Path.home() / ".claude" / "ide"
        out: dict[str, dict] = {}
        known_live_pids: set[int] = set()
        import os
        import subprocess

        # Map workspace folder -> live IDE name from ~/.claude/ide/*.lock
        ide_by_folder: dict[str, str] = {}
        if ide_dir.exists():
            for lock_file in ide_dir.glob("*.lock"):
                try:
                    lock_data = json.loads(lock_file.read_text(encoding="utf-8", errors="replace"))
                    ide_pid = lock_data.get("pid")
                    ide_name = lock_data.get("ideName")
                    folders = lock_data.get("workspaceFolders") or []
                    if isinstance(ide_pid, int) and ide_pid > 0 and ide_name:
                        try:
                            os.kill(ide_pid, 0)
                            for folder in folders:
                                if isinstance(folder, str):
                                    ide_by_folder[folder.rstrip("/")] = str(ide_name)
                        except OSError:
                            pass
                except Exception:
                    continue

        if sessions_dir.exists():
            for fpath in sessions_dir.glob("*.json"):
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8", errors="replace"))
                    sid = data.get("sessionId")
                    pid = data.get("pid")
                    if not sid:
                        continue
                    alive = False
                    tty = None
                    host_app = None
                    cwd = str(data.get("cwd") or "").rstrip("/")
                    started_at_raw = float(data.get("startedAt") or 0.0)
                    started_at = started_at_raw / 1000.0 if started_at_raw > 1e11 else started_at_raw
                    if isinstance(pid, int) and pid > 0:
                        try:
                            os.kill(pid, 0)
                            alive = True
                            known_live_pids.add(pid)
                        except OSError:
                            alive = False
                        if alive:
                            tty, host_app = ClaudeAdapter._inspect_pid_tree(pid)
                            if not cwd:
                                cwd = ClaudeAdapter._resolve_pid_cwd(pid).rstrip("/")
                    if not host_app and cwd and cwd in ide_by_folder:
                        host_app = ide_by_folder[cwd]
                    out[sid] = {
                        "pid": pid if alive else None,
                        "cwd": cwd or data.get("cwd"),
                        "alive": alive,
                        "tty": tty,
                        "host_app": host_app,
                        "ide_connected": ide_by_folder.get(cwd) if cwd else None,
                        "started_at": started_at,
                    }
                except Exception:
                    continue

        # Also scan live OS processes for any interactive `claude` CLI process not yet in ~/.claude/sessions/*.json
        try:
            ps_res = subprocess.run(
                ["ps", "-axo", "pid=,tty=,command="],
                capture_output=True,
                text=True,
                timeout=1.2,
            )
            for raw_ln in ps_res.stdout.splitlines():
                parts = raw_ln.strip().split(None, 2)
                if len(parts) < 3:
                    continue
                try:
                    pid_val = int(parts[0])
                except ValueError:
                    continue
                if pid_val in known_live_pids:
                    continue
                tty_s, cmd_s = parts[1], parts[2].strip()
                if not tty_s or tty_s == "??":
                    continue
                first_tok = cmd_s.split()[0] if cmd_s.split() else ""
                bin_name = Path(first_tok).name.lower()
                is_claude_cli = (
                    bin_name == "claude"
                    or (
                        bin_name in ("node", "bun")
                        and ("/claude" in cmd_s or "@anthropic-ai/claude-code" in cmd_s)
                    )
                )
                if not is_claude_cli or " -p " in f" {cmd_s} " or "--print" in cmd_s:
                    continue
                tty_tree, host_app = ClaudeAdapter._inspect_pid_tree(pid_val)
                cwd = ClaudeAdapter._resolve_pid_cwd(pid_val).rstrip("/")
                if not host_app and cwd and cwd in ide_by_folder:
                    host_app = ide_by_folder[cwd]
                synth_sid = f"pid-{pid_val}"
                out[synth_sid] = {
                    "pid": pid_val,
                    "cwd": cwd or str(Path.home()),
                    "alive": True,
                    "tty": tty_tree or tty_s,
                    "host_app": host_app,
                    "ide_connected": ide_by_folder.get(cwd) if cwd else None,
                    "started_at": time.time(),
                    "is_proc_fallback": True,
                }
        except Exception:
            pass

        return out

    _PID_TREE_CACHE: dict[int, tuple[Optional[str], Optional[str]]] = {}

    @staticmethod
    def _inspect_pid_tree(pid: int) -> tuple[Optional[str], Optional[str]]:
        """Walk parent PIDs of a live agent process to find its TTY and parent macOS .app (Terminal/IDE)."""
        cached = ClaudeAdapter._PID_TREE_CACHE.get(pid)
        if cached and cached[1]:
            return cached
        import re
        import subprocess
        tty_val: Optional[str] = None
        host_app: Optional[str] = None
        curr = pid
        visited = set()
        try:
            for _ in range(12):
                if curr <= 1 or curr in visited:
                    break
                visited.add(curr)
                res = subprocess.run(
                    ["ps", "-o", "ppid=,tty=,args=", "-p", str(curr)],
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                )
                line = res.stdout.strip()
                if not line:
                    break
                parts = line.split(None, 2)
                if len(parts) < 3:
                    break
                ppid_s, tty_s, comm_s = parts[0], parts[1], parts[2]
                if not tty_val and tty_s and tty_s != "??":
                    tty_val = tty_s
                comm_lower = comm_s.lower()
                if "intellij idea" in comm_lower or "/idea" in comm_lower:
                    host_app = "IntelliJ IDEA"
                    break
                if "android studio" in comm_lower or comm_lower.endswith("/studio"):
                    host_app = "Android Studio"
                    break
                if "antigravity" in comm_lower:
                    host_app = "Antigravity"
                    break
                if "visual studio code" in comm_lower or "code helper" in comm_lower or "/vscode" in comm_lower:
                    host_app = "Visual Studio Code"
                    break
                if "cursor" in comm_lower:
                    host_app = "Cursor"
                    break
                if "windsurf" in comm_lower:
                    host_app = "Windsurf"
                    break
                if "pycharm" in comm_lower:
                    host_app = "PyCharm"
                    break
                if "webstorm" in comm_lower:
                    host_app = "WebStorm"
                    break
                if "goland" in comm_lower:
                    host_app = "GoLand"
                    break
                if "clion" in comm_lower:
                    host_app = "CLion"
                    break
                if "rider" in comm_lower:
                    host_app = "Rider"
                    break
                if "phpstorm" in comm_lower:
                    host_app = "PhpStorm"
                    break
                if "rubymine" in comm_lower:
                    host_app = "RubyMine"
                    break
                if "datagrip" in comm_lower:
                    host_app = "DataGrip"
                    break
                if "fleet" in comm_lower:
                    host_app = "Fleet"
                    break
                if "zed.app" in comm_lower or comm_lower.endswith("/zed"):
                    host_app = "Zed"
                    break
                if "xcode" in comm_lower:
                    host_app = "Xcode"
                    break
                if "superset" in comm_lower:
                    host_app = "Superset"
                    break
                if "iterm" in comm_lower:
                    host_app = "iTerm"
                    break
                if "warp" in comm_lower:
                    host_app = "Warp"
                    break
                if "ghostty" in comm_lower:
                    host_app = "Ghostty"
                    break
                if "alacritty" in comm_lower:
                    host_app = "Alacritty"
                    break
                if "kitty" in comm_lower:
                    host_app = "Kitty"
                    break
                if "wezterm" in comm_lower:
                    host_app = "WezTerm"
                    break
                if "hyper.app" in comm_lower:
                    host_app = "Hyper"
                    break
                if "terminal.app" in comm_lower or comm_lower.endswith("/terminal"):
                    host_app = "Terminal"
                    break
                app_m = re.search(r"/([^/]+)\.app/Contents/MacOS/", comm_s)
                if app_m and "helper" not in app_m.group(1).lower() and "python" not in app_m.group(1).lower():
                    host_app = app_m.group(1)
                    break
                curr = int(ppid_s)
        except Exception:
            pass
        ClaudeAdapter._PID_TREE_CACHE[pid] = (tty_val, host_app)
        return tty_val, host_app

    def discover_live_agents(self, max_age_minutes: int = 43200) -> List[AgentUnit]:
        projects_dir = Path.home() / ".claude" / "projects"
        active_sessions = self._load_active_claude_sessions()
        candidates = []

        if projects_dir.exists():
            for proj_dir in projects_dir.iterdir():
                if not proj_dir.is_dir():
                    continue
                for jsonl_file in proj_dir.glob("*.jsonl"):
                    try:
                        mtime = jsonl_file.stat().st_mtime
                        candidates.append((mtime, proj_dir, jsonl_file))
                    except OSError:
                        continue

        # Prioritize truly alive sessions first, then sort by mtime descending
        candidates.sort(
            key=lambda item: (
                1 if (item[2].stem in active_sessions and active_sessions[item[2].stem].get("alive")) else 0,
                item[0],
            ),
            reverse=True,
        )
        units: List[AgentUnit] = []
        seen_sessions: set[str] = set()
        seen_live_pids: set[int] = set()

        # Always include all alive sessions plus top recent historical sessions
        max_candidates = max(16, sum(1 for v in active_sessions.values() if v.get("alive")) + 12)
        for mtime, proj_dir, jsonl_file in candidates[:max_candidates]:
            sid = jsonl_file.stem
            if sid in seen_sessions:
                continue
            seen_sessions.add(sid)
            s_info = active_sessions.get(sid)
            unit = self._parse_claude_jsonl(proj_dir, jsonl_file, mtime, s_info)
            if unit:
                if unit.pid:
                    seen_live_pids.add(unit.pid)
                units.append(unit)

        # Also emit AgentUnits for any LIVE Claude session (e.g. newly opened in IntelliJ/Terminal/IDE at the ❯ prompt)
        # that does not have a .jsonl transcript file on disk yet!
        for sid, s_info in active_sessions.items():
            if not s_info.get("alive"):
                continue
            pid_val = s_info.get("pid")
            if sid in seen_sessions or (pid_val and pid_val in seen_live_pids):
                continue
            # If this is a process-table fallback, check if another live session in the same cwd+tty already covered it
            s_tty = s_info.get("tty")
            if s_info.get("is_proc_fallback") and s_tty and any(u.tty == s_tty and u.pid for u in units):
                continue

            seen_sessions.add(sid)
            if pid_val:
                seen_live_pids.add(pid_val)
            repo_path = str(s_info.get("cwd") or Path.home())
            if "/Projects/" in repo_path:
                repo_name = repo_path.split("/Projects/", 1)[1].strip("/")
            else:
                repo_name = Path(repo_path).name or "claude-workspace"
            host_app = s_info.get("host_app") or ""
            branch = self._resolve_git_branch(repo_path)
            short_id = sid[:8] if not sid.startswith("pid-") else sid
            started_ts = float(s_info.get("started_at") or time.time())
            env_label = f"{host_app} ({s_tty})" if host_app and s_tty else (host_app or s_tty or "Terminal")
            units.append(
                AgentUnit(
                    id=f"claude-{short_id}",
                    name=f"Claude • {repo_name}",
                    workspace_title=f"Claude • {repo_name}",
                    provider="claude",
                    repo_name=repo_name,
                    repo_path=repo_path,
                    branch=branch,
                    game_state=AgentGameState.IDLE,
                    current_action=f"Sessão conectada em {env_label} • ❯ Aguardando primeiro comando",
                    last_thought=f"Conversa Claude Code aberta em {env_label} no diretório {repo_path}.",
                    pellets_eaten=0,
                    score_xp=100,
                    context_health_pct=100,
                    context_tokens_used=0,
                    hallucination_risk_pct=0,
                    health_status="HEALTHY",
                    active_elapsed_sec=0,
                    neon_color="#FF9100",
                    sprite_type="ghost_clyde",
                    terminal_tail=[
                        f"🟢 Sessão Claude Code iniciada em {env_label}",
                        f"📂 Workspace: {repo_path} ({branch})",
                        "❯ [Pronto no prompt — digite pelo painel ou direto no terminal]",
                    ],
                    session_id=sid,
                    pid=pid_val,
                    tty=s_tty,
                    host_app=host_app,
                    control_mode="interactive_cli",
                    is_live_discovered=True,
                    archived=False,
                    last_active_ts=started_ts,
                )
            )

        return units

    def _parse_claude_jsonl(
        self,
        proj_dir: Path,
        jsonl_file: Path,
        mtime: float,
        session_info: Optional[dict] = None,
    ) -> Optional[AgentUnit]:
        full_session_id = jsonl_file.stem
        short_id = full_session_id[:8]
        slug = proj_dir.name

        repo_path = (session_info or {}).get("cwd") or ""
        branch = "main"
        custom_title = ""
        first_user_text = ""

        steps = 0
        current_action = "Sessão Claude Code ativa"
        last_thought = "Analisando arquitetura e código do repositório."
        active_tool: Optional[str] = None
        game_state = AgentGameState.CHOMPING
        pending_perm: Optional[PermissionRequestItem] = None
        terminal_tail: List[str] = []

        pending_tool_use: Optional[dict] = None
        last_event_kind = ""
        last_stop_reason: Optional[str] = None
        was_interrupted = False
        last_assistant_text = ""

        latest_usage_tokens = 0
        file_size_bytes = 0
        tool_events_history: List[Dict[str, Any]] = []
        recent_interruptions = 0
        last_turn_duration_sec = 0
        last_user_prompt_epoch = 0.0
        last_event_epoch = 0.0

        def _parse_iso_epoch(ts_val: Any) -> float:
            if not isinstance(ts_val, str) or not ts_val:
                return 0.0
            try:
                return datetime.fromisoformat(ts_val.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0.0

        try:
            file_size_bytes = jsonl_file.stat().st_size
            with jsonl_file.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            steps = len(lines)

            # Inspect early lines and tail for cwd, branch, customTitle, and first user request
            head_and_tail = lines[:12] + (lines[-35:] if len(lines) > 12 else [])
            for raw in head_and_tail:
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                if not repo_path and isinstance(entry.get("cwd"), str):
                    repo_path = entry["cwd"]
                if isinstance(entry.get("gitBranch"), str) and entry["gitBranch"]:
                    branch = entry["gitBranch"]
                if isinstance(entry.get("customTitle"), str) and entry["customTitle"]:
                    custom_title = entry["customTitle"]
                elif isinstance(entry.get("summary"), str) and entry["summary"]:
                    custom_title = entry["summary"]

                if not first_user_text and entry.get("type") == "user":
                    msg = entry.get("message") or {}
                    c = msg.get("content")
                    if isinstance(c, str) and not c.startswith("<"):
                        cleaned = c.split("{")[0].strip() if "{" in c and len(c.split("{")[0].strip()) > 12 else c
                        first_user_text = " ".join(cleaned.split())[:68]

            tail = lines[-100:] if len(lines) > 100 else lines
            for idx_tail, raw in enumerate(tail):
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue

                ev_ts = _parse_iso_epoch(entry.get("timestamp"))
                if ev_ts > 0:
                    last_event_epoch = ev_ts

                etype = entry.get("type")
                if etype in ("file-history-snapshot", "queue-operation"):
                    continue

                if etype == "system":
                    subtype = entry.get("subtype") or ""
                    if subtype in ("turn_duration", "stop_hook_summary"):
                        if subtype == "turn_duration":
                            dur_s = round((entry.get("durationMs") or 0) / 1000.0, 1)
                            last_turn_duration_sec = int(round(dur_s))
                            terminal_tail.append(f"✻ Worked for {dur_s}s")
                        last_event_kind = "turn_end"
                        last_stop_reason = "end_turn"
                        pending_tool_use = None
                    continue

                msg = entry.get("message") or {}
                content = msg.get("content")

                if etype == "user":
                    if isinstance(content, str):
                        if (
                            "HANDOFF DE CONTINUIDADE" in content
                            or "<command-name>/clear</command-name>" in content
                            or "<command-name>/compact</command-name>" in content
                            or content.strip() in ("/clear", "/compact")
                        ):
                            tool_events_history.clear()
                            recent_interruptions = 0
                            latest_usage_tokens = 1500
                        if "<command-name>" in content:
                            cmd_name = content.split("<command-name>", 1)[1].split("</command-name>", 1)[0].strip()
                            terminal_tail.append(f"❯ {cmd_name}")
                        elif not content.startswith("<"):
                            user_lines = [ln.rstrip() for ln in content.splitlines() if ln.strip()]
                            if user_lines:
                                terminal_tail.append(f"❯ {user_lines[0]}")
                                for uln in user_lines[1:]:
                                    terminal_tail.append(f"  {uln}")
                                current_action = f"Prompt: {user_lines[0][:120]}"
                        if ev_ts > 0:
                            last_user_prompt_epoch = ev_ts
                        last_turn_duration_sec = 0
                        pending_tool_use = None
                        was_interrupted = False
                        last_stop_reason = None
                        last_event_kind = "user_prompt"
                    elif isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type")
                            if btype == "tool_result":
                                pending_tool_use = None
                                was_interrupted = False
                                last_stop_reason = None
                                last_turn_duration_sec = 0
                                is_err = bool(block.get("is_error"))
                                res_content = block.get("content")
                                tool_use_res = entry.get("toolUseResult")
                                out_text = ""
                                if isinstance(tool_use_res, dict):
                                    out_text = str(tool_use_res.get("stdout") or tool_use_res.get("stderr") or "")
                                if not out_text:
                                    if isinstance(res_content, str):
                                        out_text = res_content
                                    elif isinstance(res_content, list):
                                        out_text = "\n".join(
                                            str(x.get("text", "")) for x in res_content if isinstance(x, dict)
                                        )
                                lower_out = out_text.lower()
                                if not is_err and any(
                                    err_pat in lower_out
                                    for err_pat in (
                                        "file does not exist",
                                        "no such file or directory",
                                        "string to replace not found",
                                        "old_string not found",
                                        "error:",
                                        "traceback (most recent call last)",
                                    )
                                ):
                                    is_err = True
                                if tool_events_history:
                                    t_uid = block.get("tool_use_id")
                                    matched_ev = None
                                    if t_uid:
                                        for tev in reversed(tool_events_history):
                                            if tev.get("id") == t_uid:
                                                matched_ev = tev
                                                break
                                    if matched_ev is None:
                                        matched_ev = tool_events_history[-1]
                                    if is_err:
                                        matched_ev["is_err"] = True
                                out_lines = [ln.rstrip() for ln in out_text.splitlines() if ln.strip()]
                                prefix = "  ⎿  ❌ " if is_err else "  ⎿  "
                                if out_lines:
                                    for oln in out_lines[:60]:
                                        terminal_tail.append(f"{prefix}{oln}")
                                    if len(out_lines) > 60:
                                        terminal_tail.append(f"  ⎿  … (+{len(out_lines) - 60} linhas)")
                                else:
                                    terminal_tail.append(f"{prefix}(concluído)")
                                current_action = f"Analisando retorno de [{active_tool or 'Tool'}]..."
                                last_event_kind = "tool_result"
                            elif btype == "text":
                                txt = str(block.get("text", "")).strip()
                                if "HANDOFF DE CONTINUIDADE" in txt or txt in ("/clear", "/compact"):
                                    tool_events_history.clear()
                                    recent_interruptions = 0
                                    latest_usage_tokens = 1500
                                if "[Request interrupted" in txt:
                                    was_interrupted = True
                                    if idx_tail >= max(0, len(tail) - 30):
                                        recent_interruptions += 1
                                    pending_tool_use = None
                                    last_stop_reason = "interrupted"
                                    last_event_kind = "interrupted"
                                    terminal_tail.append("⚠️ Interrupted · What should Claude do instead?")
                                elif txt and not txt.startswith("<"):
                                    was_interrupted = False
                                    pending_tool_use = None
                                    last_stop_reason = None
                                    last_turn_duration_sec = 0
                                    if txt.startswith("Base directory for this skill:"):
                                        last_event_kind = "tool_result"
                                    else:
                                        last_event_kind = "user_prompt"
                                        if ev_ts > 0:
                                            last_user_prompt_epoch = ev_ts
                                        t_lines = [ln.rstrip() for ln in txt.splitlines() if ln.strip()]
                                        if t_lines:
                                            current_action = f"Prompt: {t_lines[0][:120]}"
                                            terminal_tail.append(f"❯ {t_lines[0]}")
                                            for tln in t_lines[1:]:
                                                terminal_tail.append(f"  {tln}")

                elif etype == "assistant":
                    was_interrupted = False
                    stop_reason = msg.get("stop_reason")
                    last_stop_reason = stop_reason
                    usage_obj = msg.get("usage")
                    if isinstance(usage_obj, dict):
                        u_sum = (
                            int(usage_obj.get("input_tokens") or 0)
                            + int(usage_obj.get("cache_read_input_tokens") or 0)
                            + int(usage_obj.get("cache_creation_input_tokens") or 0)
                            + int(usage_obj.get("output_tokens") or 0)
                        )
                        if u_sum > 0:
                            latest_usage_tokens = u_sum
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type")
                            if btype == "thinking":
                                raw_thought = str(block.get("thinking", "")).strip()
                                if raw_thought:
                                    last_thought = raw_thought[:300]
                                    th_lines = [ln.rstrip() for ln in raw_thought.splitlines() if ln.strip()]
                                    if th_lines:
                                        terminal_tail.append(f"✻ Thinking: {th_lines[0]}")
                                        for tln in th_lines[1:]:
                                            terminal_tail.append(f"  ✻ {tln}")
                                    game_state = AgentGameState.THINKING
                                    last_event_kind = "thinking"
                            elif btype == "tool_use":
                                t_name = block.get("name", "Tool")
                                t_input = block.get("input") or {}
                                active_tool = t_name
                                cmd = (
                                    t_input.get("command")
                                    or t_input.get("file_path")
                                    or t_input.get("pattern")
                                    or t_input.get("description")
                                    or t_input.get("question")
                                    or t_input.get("skill")
                                    or t_name
                                )
                                target_short = ""
                                if t_input.get("file_path"):
                                    target_short = Path(str(t_input["file_path"])).name
                                elif t_input.get("command"):
                                    target_short = str(t_input["command"]).strip().splitlines()[0][:36]
                                elif t_input.get("pattern"):
                                    target_short = str(t_input["pattern"]).strip()[:30]
                                else:
                                    target_short = str(t_name)
                                is_read_only = t_name in ("Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch", "Skill", "ToolSearch")
                                tool_events_history.append(
                                    {
                                        "id": block.get("id") or f"tu-{short_id}-{len(tool_events_history)}",
                                        "tool": str(t_name),
                                        "target": target_short,
                                        "is_err": False,
                                        "is_read_only": is_read_only,
                                    }
                                )
                                current_action = f"[{t_name}] {cmd}"
                                terminal_tail.append(f"● {t_name}({cmd})")
                                if t_name in ("Edit", "Write"):
                                    if isinstance(t_input.get("old_string"), str):
                                        for oln in t_input["old_string"].splitlines()[:25]:
                                            terminal_tail.append(f"    - {oln}")
                                    if isinstance(t_input.get("new_string"), str):
                                        for pln in t_input["new_string"].splitlines()[:35]:
                                            terminal_tail.append(f"    + {pln}")
                                pending_tool_use = {
                                    "id": block.get("id") or f"tu-{short_id}",
                                    "name": t_name,
                                    "input": t_input,
                                    "cmd": str(cmd),
                                }
                                game_state = AgentGameState.RUNNING_TOOL
                                last_event_kind = "tool_use"
                            elif btype == "text":
                                txt = str(block.get("text", "")).strip()
                                if txt:
                                    last_assistant_text = txt
                                    txt_lines = [ln.rstrip() for ln in txt.splitlines()]
                                    if txt_lines:
                                        non_empty = [ln.strip() for ln in txt_lines if ln.strip()]
                                        if non_empty:
                                            current_action = non_empty[-1][:140]
                                        terminal_tail.append(f"● {txt_lines[0]}")
                                        for aln in txt_lines[1:]:
                                            terminal_tail.append(f"  {aln}")
                                    game_state = AgentGameState.CHOMPING
                                    last_event_kind = "assistant_text"
                                    pending_tool_use = None
                    if stop_reason == "end_turn":
                        last_event_kind = "turn_end"
                        pending_tool_use = None
        except Exception:
            pass

        if not repo_path:
            fallback_name = slug.split("-Projects-")[-1] if "-Projects-" in slug else slug.strip("-")[-28:]
            repo_path = str(Path.home() / "Projects" / fallback_name)

        if "/Projects/" in repo_path:
            repo_name = repo_path.split("/Projects/", 1)[1].strip("/")
        else:
            repo_name = Path(repo_path).name or "claude-workspace"

        pid = (session_info or {}).get("pid")
        tty = (session_info or {}).get("tty")
        host_app = (session_info or {}).get("host_app") or ""
        alive = bool((session_info or {}).get("alive"))

        # Check subagent transcripts and OS child processes (including Claude Code's active-turn `caffeinate` lock
        # and active tool subprocesses like `rg`, `bash`, `git`, `python`)
        has_tool_child = False
        has_caffeinate_lock = False
        has_active_subagent = False
        if alive:
            subagents_dir = proj_dir / full_session_id / "subagents"
            if subagents_dir.is_dir():
                try:
                    now_check = time.time()
                    for sub_f in subagents_dir.glob("*.jsonl"):
                        sub_mtime = sub_f.stat().st_mtime
                        if sub_mtime > mtime:
                            mtime = sub_mtime
                        if (now_check - sub_mtime) < 25.0:
                            has_active_subagent = True
                except Exception:
                    pass
            if pid:
                try:
                    cp = subprocess.run(
                        ["ps", "-axo", "ppid=,command="],
                        capture_output=True,
                        text=True,
                        timeout=0.4,
                    )
                    pid_s = str(pid)
                    for cln in cp.stdout.splitlines():
                        cparts = cln.strip().split(None, 1)
                        if len(cparts) == 2 and cparts[0] == pid_s:
                            ccmd = cparts[1].lower()
                            if (
                                "mcp" in ccmd
                                or "notify.sh" in ccmd
                                or "pac_neon_hook" in ccmd
                            ):
                                continue
                            if "caffeinate" in ccmd:
                                has_caffeinate_lock = True
                                continue
                            has_tool_child = True
                except Exception:
                    pass

        age_seconds = max(0.0, time.time() - mtime)
        workspace_title = custom_title or first_user_text or f"Claude • {repo_name}"

        # Herdr-inspired strict 3-state model:
        # 1. Not alive -> always IDLE (never working or blocked)
        # 2. Alive + pending tool_use -> RUNNING_TOOL (if OS child/caffeinate active or read-only/fast tool) or POWER_PELLET_WAIT (if paused for user approval)
        # 3. Alive + turn ended / interrupted (and no active OS lock) -> IDLE (at prompt ❯)
        # 4. Alive + active mid-turn -> CHOMPING / RUNNING_TOOL / THINKING
        auto_tools = (
            "Read",
            "Glob",
            "Grep",
            "LS",
            "TodoWrite",
            "Task",
            "Agent",
            "Skill",
            "ToolSearch",
            "NotebookRead",
        )
        if not alive:
            game_state = AgentGameState.IDLE
            pending_perm = None
            active_tool = None
            current_action = f"Parado (Sessão encerrada) • Última: {current_action}"
        elif pending_tool_use is not None and age_seconds < 300:
            t_name = pending_tool_use["name"]
            t_cmd = pending_tool_use["cmd"]
            active_tool = t_name

            if (
                has_tool_child
                or has_active_subagent
                or has_caffeinate_lock
                or (t_name in auto_tools and t_name not in ("AskUserQuestion", "ExitPlanMode"))
                or (t_name not in ("AskUserQuestion", "ExitPlanMode") and age_seconds < 2.0)
            ):
                game_state = AgentGameState.RUNNING_TOOL
                pending_perm = None
                current_action = f"Executando [{t_name}] {t_cmd[:100]}"
            else:
                game_state = AgentGameState.POWER_PELLET_WAIT
                t_inp = pending_tool_use.get("input") if isinstance(pending_tool_use.get("input"), dict) else {}
                desc = str(t_inp.get("description") or "").strip()
                fpath = str(t_inp.get("file_path") or "").strip()
                diff_old = str(t_inp.get("old_string") or "").strip()
                diff_new = str(t_inp.get("new_string") or t_inp.get("content") or t_inp.get("plan") or "").strip()
                opts_list: List[str] = []
                q_list = t_inp.get("questions") if isinstance(t_inp.get("questions"), list) else []
                if q_list:
                    q_texts = []
                    for qi in q_list:
                        if isinstance(qi, dict):
                            if qi.get("question"):
                                q_texts.append(str(qi["question"]))
                            if isinstance(qi.get("options"), list):
                                for opt_item in qi["options"]:
                                    if isinstance(opt_item, dict) and opt_item.get("label"):
                                        opts_list.append(str(opt_item["label"]))
                                    elif isinstance(opt_item, str):
                                        opts_list.append(opt_item)
                    if q_texts:
                        t_cmd = "\n".join(q_texts)

                reason_parts = []
                if desc:
                    reason_parts.append(f"Descrição: {desc}")
                if fpath and fpath != t_cmd:
                    reason_parts.append(f"Arquivo: {fpath}")
                if repo_path:
                    reason_parts.append(f"Diretório: {repo_path}")
                reason_str = (
                    " • ".join(reason_parts)
                    if reason_parts
                    else "O agente Claude pausou para pedir sua permissão de execução no terminal (1=Yes / 2=Allow All / 3=No)."
                )
                filtered_params = {
                    k: v
                    for k, v in t_inp.items()
                    if k not in ("old_string", "new_string", "content") and v not in (None, "", [], {})
                }
                current_action = f"⚡ Aguardando permissão: {t_name} — {t_cmd}"
                terminal_tail.append(f"⚡ Do you want to proceed with {t_name}? [1] Yes  [2] Allow all  [3] No")
                pending_perm = PermissionRequestItem(
                    id=f"perm-hook-claude-{short_id}",
                    agent_id=f"claude-{short_id}",
                    agent_name=workspace_title,
                    provider="claude",
                    repo_name=repo_name,
                    tool_name=t_name,
                    command_preview=str(t_cmd),
                    reason=reason_str,
                    full_details={
                        "tool_action": desc,
                        "description": desc,
                        "cwd": repo_path,
                        "target_path": fpath,
                        "diff_old": diff_old[:1500] if diff_old else "",
                        "diff_new": diff_new[:1500] if diff_new else "",
                        "agent_context": (last_assistant_text or last_thought or "")[-800:],
                        "parameters": filtered_params,
                    },
                    options=opts_list,
                    channel="terminal_prompt",
                )
        elif not (has_caffeinate_lock or has_tool_child or has_active_subagent) and (
            was_interrupted
            or last_event_kind in ("turn_end", "interrupted")
            or last_stop_reason == "end_turn"
            or (last_event_kind == "assistant_text" and age_seconds >= 45.0)
            or (last_event_kind not in ("user_prompt", "thinking", "tool_result", "assistant_text") and age_seconds >= 15.0)
            or age_seconds >= 180.0
        ):
            game_state = AgentGameState.IDLE
            pending_perm = None
            active_tool = None
            preview_lines = [ln.strip() for ln in last_assistant_text.splitlines() if ln.strip()]
            preview = preview_lines[-1][:110] if preview_lines else current_action[:110]
            if was_interrupted or last_event_kind == "interrupted":
                current_action = f"Parado no prompt (❯ Interrompido) • {preview}"
                terminal_tail.append("❯ [Parado no prompt após interrupção — pronto para comando]")
            else:
                current_action = f"Parado no prompt (❯ Pronto) • {preview}"
                terminal_tail.append("❯ [Parado no prompt — aguardando próxima instrução]")
        else:
            if last_event_kind in ("user_prompt", "thinking"):
                game_state = AgentGameState.THINKING
                active_tool = active_tool or "thinking"
                if last_event_kind == "user_prompt" and current_action.startswith("Prompt:"):
                    current_action = f"Pensando • {current_action}"
            elif has_tool_child and active_tool:
                game_state = AgentGameState.RUNNING_TOOL
            else:
                game_state = AgentGameState.CHOMPING
                active_tool = active_tool or "coding"

        # Never auto-archive agents to History; archiving happens only via explicit user action
        default_archived = False

        # Compute real Context Health, Hallucination Risk, and Stall/Overthinking metrics
        tokens_used = latest_usage_tokens if latest_usage_tokens > 0 else int(file_size_bytes / 4.2)
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
            target_counts = Counter(
                (ev["tool"], ev["target"])
                for ev in recent_10
                if ev.get("target")
                and (
                    ev.get("is_err")
                    or ev["tool"] in ("Bash", "Grep", "Glob")
                    or (
                        ev["tool"] in ("Edit", "Write", "MultiEdit")
                        and sum(1 for x in recent_10 if x.get("target") == ev.get("target") and x.get("is_err")) >= 1
                    )
                )
            )
            if target_counts:
                (top_tool, top_target), top_cnt = target_counts.most_common(1)[0]
                if top_cnt >= 3:
                    thrashing_count = top_cnt
                    thrashing_target = f"{top_target}" if top_target != top_tool else top_tool

        # Overthinking / Search spiral: >=12 read-only tools in last 15 steps without an edit/write
        overthinking_read_only_count = 0
        if len(recent_15) >= 12:
            read_cnt = sum(1 for ev in recent_15 if ev.get("is_read_only"))
            write_cnt = sum(1 for ev in recent_15 if ev.get("tool") in ("Edit", "Write", "MultiEdit", "NotebookEdit"))
            if read_cnt >= 12 and write_cnt == 0:
                overthinking_read_only_count = read_cnt

        # Turn elapsed time calculation
        active_elapsed_sec = last_turn_duration_sec
        if game_state != AgentGameState.IDLE and last_user_prompt_epoch > 0:
            active_elapsed_sec = max(active_elapsed_sec, int(round(max(0.0, time.time() - last_user_prompt_epoch))))
        elif last_turn_duration_sec == 0 and last_user_prompt_epoch > 0 and last_event_epoch > last_user_prompt_epoch:
            active_elapsed_sec = int(round(last_event_epoch - last_user_prompt_epoch))

        health_metrics = compute_context_and_hallucination_metrics(
            tokens_used=tokens_used,
            tokens_limit=200000,
            steps_count=steps,
            recent_tool_errors=recent_tool_errors,
            last_error_target=last_error_target,
            thrashing_target=thrashing_target,
            thrashing_count=thrashing_count,
            recent_interruptions=recent_interruptions,
            truncated_steps_count=0,
            overthinking_read_only_count=overthinking_read_only_count,
            game_state=game_state,
            age_seconds=age_seconds,
            active_elapsed_sec=active_elapsed_sec,
        )

        return AgentUnit(
            id=f"claude-{short_id}",
            name=workspace_title,
            workspace_title=workspace_title,
            session_id=full_session_id,
            provider="claude",
            repo_name=repo_name,
            repo_path=repo_path,
            branch=branch,
            game_state=game_state,
            score_xp=steps * 120,
            pellets_eaten=min(24, max(2, steps // 3)),
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
            last_thought=last_thought,
            active_tool=active_tool,
            terminal_tail=terminal_tail[-800:],
            pending_permission=pending_perm,
            controllable=True,
            control_mode="hook",
            neon_color="#FF7A00",
            sprite_type="ghost_clyde",
            is_live_discovered=True,
            archived=default_archived,
            pid=pid,
            tty=tty,
            host_app=host_app,
            last_active_ts=mtime,
        )

