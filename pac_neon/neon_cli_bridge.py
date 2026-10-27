"""Bridge between Pac-Neon and local Neon & Developer CLI tools:
- `neon-cli` (`~/.local/bin/neon` and `~/Projects/neon.cli`)
- `agy` (`~/.local/bin/agy` — Antigravity IDE CLI)
- `glowworm` (`~/.local/bin/glowworm` — NeonLumen Design System CLI)
- `oncall-*` (`~/.local/bin/oncall-*` — Neon oncall scripts)
- Native Agent Lifecycle Hooks (`~/.pac-neon/hooks/notify.sh` + `~/.claude/settings.json`)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_GITHUB_REPO_RE = re.compile(r"github\.com[:/][^/]+/([^/]+?)(?:\.git)?$")
_JIRA_KEY_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,9}-\d+)(?![A-Za-z0-9])")
_GITHUB_PR_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE,
)
_GITHUB_API_PR_URL_RE = re.compile(
    r"https?://api\.github\.com/repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pulls/(\d+)",
    re.IGNORECASE,
)
_HTML_URL_PR_RE = re.compile(
    r'"html_url"\s*:\s*"(https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+))"',
    re.IGNORECASE,
)
_GH_PR_CMD_RE = re.compile(
    r"\bgh\s+pr\s+(create|view|checks|diff|comment|edit|checkout|review|merge|ready)\b"
    r"(?:\s+#?(\d{1,7}))?(?:[^\n\"'`]*?--repo\s+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+))?",
    re.IGNORECASE,
)
_GH_API_PULL_CMD_RE = re.compile(
    r"\bgh\s+api\s+[^\n\"'`]*?repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pulls/(\d+)",
    re.IGNORECASE,
)
_PR_EXPLICIT_HASH_RE = re.compile(
    r"(?:(?<!Merge\s)(?<!merge\s)\b(?:PR|pull\s+request|MR)(?:\s+anterior|\s+novo)?(?:\s*\([^)\n]{1,60}\))?\s*:?\s*#(\d{1,7})\b|/review-pr\s+#?(\d{1,7})\b)",
    re.IGNORECASE,
)
_USER_REQUEST_BLOCK_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)
_REPO_HINT_RE = re.compile(
    r"(?:--repo\s+neon/([A-Za-z0-9_.-]+)|repos/neon/([A-Za-z0-9_.-]+)/pulls|/Projects/(?:and_neon/)?([A-Za-z0-9_.-]+?)(?=/|\s|\"|'|$))",
    re.IGNORECASE,
)
_STEP_OUTPUT_FILE_RE = re.compile(
    r"file://(/[^ \n\"'`]+/\.system_generated/steps/\d+/output\.txt)"
)
_NOISE_MARKERS = (
    "workspace_state.json",
    "neon_cli_bridge.py",
    "verify_pr_resolver",
    "audit_all_agents_prs",
    "prs_completos.md",
)
_DEFAULT_BRANCHES = {"main", "master", "develop", "dev", "head", "unknown", ""}


def _clean_json_str(val: Any) -> str:
    """Strips surrounding whitespace and extra literal JSON quotes from Antigravity transcript args."""
    s = str(val or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].strip()
    return s


def _extract_pr_payload_snippet(raw_text: str) -> str:
    """Extracts the top-level PR URL from a GitHub REST API PR JSON response without leaking PRs mentioned inside 'body'."""
    if not raw_text:
        return ""
    if '"diff_url"' in raw_text or '"patch_url"' in raw_text or '"issue_url"' in raw_text:
        header_part = raw_text.split('"body"', 1)[0]
        urls = [m.group(1) for m in _HTML_URL_PR_RE.finditer(header_part)]
        if urls:
            return " ".join(urls)
    return raw_text[:4000]


class NeonCliBridge:
    """Integrates `neon-cli` (`neon`), `agy`, `glowworm`, and native agent hooks into Pac-Neon."""

    def __init__(self) -> None:
        self.neon_bin = self._find_bin("neon", [Path.home() / ".local" / "bin" / "neon"])
        self.agy_bin = self._find_bin(
            "agy",
            [
                Path.home() / ".local" / "bin" / "agy",
                Path.home() / ".antigravity-ide" / "antigravity-ide" / "bin" / "agy",
            ],
        )
        self.glowworm_bin = self._find_bin(
            "glowworm", [Path.home() / ".local" / "bin" / "glowworm"]
        )
        self.claude_bin = self._find_bin(
            "claude", [Path.home() / ".local" / "bin" / "claude"]
        )
        self.gh_bin = self._find_bin(
            "gh",
            [
                Path("/opt/homebrew/bin/gh"),
                Path("/usr/local/bin/gh"),
                Path.home() / ".local" / "bin" / "gh",
            ],
        )
        self.neon_repo_dir = Path.home() / "Projects" / "neon.cli"
        self.superset_repo_dir = Path.home() / "Projects" / "superset"
        self.flow_db_path = Path.home() / ".neon" / "flow.sqlite3"
        self._git_remote_cache: Dict[str, Optional[str]] = {}
        self._transcript_pr_cache: Dict[str, Tuple[float, List[Tuple[str, str]]]] = {}
        self._branch_pr_cache: Dict[Tuple[str, str], Tuple[float, Optional[Dict[str, Any]]]] = {}
        self._claude_jsonl_path_cache: Dict[str, Optional[Path]] = {}

    @staticmethod
    def _find_bin(name: str, extra_paths: List[Path]) -> Optional[str]:
        for candidate in extra_paths:
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        found = shutil.which(name)
        return found

    def get_toolchain_status(self) -> Dict[str, Any]:
        """Returns discovery status of all local tools available to Pac-Neon."""
        local_bin = Path.home() / ".local" / "bin"
        oncall_scripts = (
            sorted(p.name for p in local_bin.glob("oncall-*"))
            if local_bin.exists()
            else []
        )
        hooks_installed = (Path.home() / ".pac-neon" / "hooks" / "notify.sh").exists()
        claude_sessions_dir = Path.home() / ".claude" / "sessions"
        active_session_files = (
            len(list(claude_sessions_dir.glob("*.json")))
            if claude_sessions_dir.exists()
            else 0
        )
        claude_ide_dir = Path.home() / ".claude" / "ide"
        active_ide_locks = (
            len(list(claude_ide_dir.glob("*.lock")))
            if claude_ide_dir.exists()
            else 0
        )
        return {
            "neon_cli": {
                "installed": bool(self.neon_bin),
                "bin_path": self.neon_bin,
                "source_repo": str(self.neon_repo_dir)
                if self.neon_repo_dir.exists()
                else None,
                "namespaces": [
                    "aws",
                    "bootstrap",
                    "dd",
                    "flow",
                    "gh",
                    "golden-path",
                    "iac",
                    "k8s",
                    "pam-pam",
                    "self",
                    "teleport",
                ],
            },
            "agy_cli": {
                "installed": bool(self.agy_bin),
                "bin_path": self.agy_bin,
            },
            "glowworm_cli": {
                "installed": bool(self.glowworm_bin),
                "bin_path": self.glowworm_bin,
            },
            "superset_local": {
                "repo_cloned": self.superset_repo_dir.exists(),
                "repo_path": str(self.superset_repo_dir)
                if self.superset_repo_dir.exists()
                else None,
            },
            "claude_native": {
                "installed": bool(self.claude_bin),
                "bin_path": self.claude_bin,
                "active_session_files": active_session_files,
                "active_ide_locks": active_ide_locks,
                "hooks_registered": hooks_installed,
            },
            "oncall_scripts": {
                "count": len(oncall_scripts),
                "scripts": oncall_scripts,
            },
        }

    def get_remote_repo_name(self, repo_path: str) -> Optional[str]:
        """Extracts the GitHub repository name (e.g. `payments.invoice-statement`) using neon-cli's convention."""
        if not repo_path or repo_path.startswith("superset://"):
            return None
        if repo_path in self._git_remote_cache:
            return self._git_remote_cache[repo_path]
        p = Path(repo_path)
        if not p.exists():
            self._git_remote_cache[repo_path] = None
            return None
        try:
            res = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(p),
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            url = res.stdout.strip()
            if url:
                m = _GITHUB_REPO_RE.search(url)
                if m:
                    repo_name = m.group(1)
                    self._git_remote_cache[repo_path] = repo_name
                    return repo_name
        except Exception:
            pass
        # Fallback to directory name if it matches Neon's `namespace.appname` convention
        if "." in p.name and not p.name.startswith("."):
            self._git_remote_cache[repo_path] = p.name
            return p.name
        self._git_remote_cache[repo_path] = None
        return None

    def _find_claude_jsonl(self, session_id: str) -> Optional[Path]:
        if not session_id:
            return None
        if session_id in self._claude_jsonl_path_cache:
            cached = self._claude_jsonl_path_cache[session_id]
            if cached and cached.exists():
                return cached
        projects_dir = Path.home() / ".claude" / "projects"
        if not projects_dir.exists():
            return None
        try:
            for pdir in projects_dir.iterdir():
                if not pdir.is_dir():
                    continue
                candidate = pdir / f"{session_id}.jsonl"
                if candidate.exists():
                    self._claude_jsonl_path_cache[session_id] = candidate
                    return candidate
        except Exception:
            pass
        return None

    def _collect_transcript_pr_snippets(
        self, provider: Optional[str], session_id: Optional[str]
    ) -> List[Tuple[str, str]]:
        """Reads user prompts, assistant responses, and PR tool calls/outputs from disk transcript (cached by mtime)."""
        if not session_id:
            return []
        prov = (provider or "").lower()
        t_file: Optional[Path] = None
        if prov == "antigravity":
            cand = (
                Path.home()
                / ".gemini"
                / "antigravity"
                / "brain"
                / session_id
                / ".system_generated"
                / "logs"
                / "transcript.jsonl"
            )
            if cand.exists():
                t_file = cand
        elif prov == "claude":
            t_file = self._find_claude_jsonl(session_id)

        if t_file is None or not t_file.exists():
            return []

        try:
            mtime = t_file.stat().st_mtime
        except OSError:
            return []

        cache_key = f"{prov}:{session_id}"
        cached = self._transcript_pr_cache.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]

        snippets: List[Tuple[str, str]] = []
        try:
            lines = t_file.read_text(encoding="utf-8", errors="replace").splitlines()
            if prov == "antigravity":
                parsed_steps: List[Tuple[int, int, Dict[str, Any]]] = []
                for line_idx, raw in enumerate(lines):
                    raw_s = raw.strip()
                    if not raw_s:
                        continue
                    try:
                        step = json.loads(raw_s)
                    except Exception:
                        continue
                    if isinstance(step, dict):
                        s_idx = int(step.get("step_index") or (line_idx + 1))
                        parsed_steps.append((s_idx, line_idx, step))

                # Sort by step_index so parallel tool output steps (GENERIC) always follow their PLANNER_RESPONSE
                parsed_steps.sort(key=lambda item: (item[0], item[1]))
                pending_pr_kinds: List[str] = []

                for _, _, step in parsed_steps:
                    stype = str(step.get("type") or "")
                    content = step.get("content") if isinstance(step.get("content"), str) else ""

                    if stype == "USER_INPUT":
                        pending_pr_kinds.clear()
                        if content:
                            if "<USER_REQUEST>" in content:
                                req_blocks = _USER_REQUEST_BLOCK_RE.findall(content)
                                clean_u = "\n".join(b.strip() for b in req_blocks if b.strip())
                            elif "<CONTEXT_SUMMARY>" in content:
                                clean_u = ""
                            else:
                                clean_u = content.split("# Conversation Logs", 1)[0].strip()
                            if clean_u and not any(nm in clean_u for nm in _NOISE_MARKERS):
                                snippets.append(("user", clean_u))
                        continue

                    if stype == "PLANNER_RESPONSE":
                        pending_pr_kinds.clear()
                        if content.strip() and not any(nm in content for nm in _NOISE_MARKERS):
                            snippets.append(("assistant", content.strip()))
                        tool_calls = step.get("tool_calls") or []
                        if isinstance(tool_calls, list):
                            for tc in tool_calls:
                                if not isinstance(tc, dict):
                                    continue
                                tname = _clean_json_str(tc.get("name") or tc.get("tool_name") or "")
                                args = tc.get("arguments") or tc.get("args") or {}
                                if isinstance(args, str):
                                    try:
                                        args = json.loads(args)
                                    except Exception:
                                        args = {}
                                if not isinstance(args, dict):
                                    args = {}

                                if tname == "run_command":
                                    cmd_line = _clean_json_str(args.get("CommandLine") or "")
                                    cwd_val = _clean_json_str(args.get("Cwd") or "")
                                    if cwd_val:
                                        snippets.append(("repo_hint", f"/Projects/{Path(cwd_val).name}/"))
                                    if any(nm in cmd_line for nm in _NOISE_MARKERS):
                                        continue
                                    low_cmd = cmd_line.lower()
                                    if _GH_PR_CMD_RE.search(cmd_line) or _GH_API_PULL_CMD_RE.search(cmd_line):
                                        kind = "pr_created" if "gh pr create" in low_cmd else "pr_cmd"
                                        snippets.append((kind, cmd_line))
                                        pending_pr_kinds.append(kind)
                                elif tname == "call_mcp_tool" or "pull_request" in tname.lower():
                                    sub_tool = _clean_json_str(args.get("ToolName") or tname).lower()
                                    if "pull_request" in sub_tool and "list_pull_requests" not in sub_tool:
                                        kind = (
                                            "pr_created"
                                            if "create_pull_request" == sub_tool
                                            else "pr_cmd"
                                        )
                                        raw_mcp_args = args.get("Arguments")
                                        mcp_args: Dict[str, Any] = {}
                                        if isinstance(raw_mcp_args, str):
                                            try:
                                                mcp_args = json.loads(raw_mcp_args)
                                            except Exception:
                                                mcp_args = {}
                                        elif isinstance(raw_mcp_args, dict):
                                            mcp_args = raw_mcp_args
                                        owner_arg = _clean_json_str(mcp_args.get("owner") or "neon")
                                        repo_arg = _clean_json_str(mcp_args.get("repo") or "")
                                        pr_num_arg = mcp_args.get("pullNumber") or mcp_args.get("pull_number")
                                        if repo_arg:
                                            snippets.append(("repo_hint", f"--repo {owner_arg}/{repo_arg}"))
                                        if repo_arg and pr_num_arg and str(pr_num_arg).isdigit():
                                            snippets.append(
                                                (
                                                    kind,
                                                    f"https://github.com/{owner_arg}/{repo_arg}/pull/{int(pr_num_arg)}",
                                                )
                                            )
                                        pending_pr_kinds.append(kind)
                                elif tname in ("view_file", "replace_file_content", "write_to_file", "grep_search"):
                                    path_arg = _clean_json_str(
                                        args.get("AbsolutePath")
                                        or args.get("TargetFile")
                                        or args.get("SearchPath")
                                        or ""
                                    )
                                    if "/Projects/" in path_arg:
                                        snippets.append(("repo_hint", path_arg))
                        continue

                    if content and not any(nm in content for nm in _NOISE_MARKERS):
                        if pending_pr_kinds:
                            cur_kind = pending_pr_kinds.pop(0)
                            snippets.append((cur_kind, _extract_pr_payload_snippet(content)))
                            m_out = _STEP_OUTPUT_FILE_RE.search(content)
                            if m_out:
                                out_path = Path(m_out.group(1))
                                if out_path.exists():
                                    try:
                                        out_raw = out_path.read_text(encoding="utf-8", errors="replace")[:10000]
                                        snippets.append((cur_kind, _extract_pr_payload_snippet(out_raw)))
                                    except Exception:
                                        pass

            elif prov == "claude":
                expect_pr_tool_ids: Dict[str, str] = {}
                for raw in lines:
                    raw_s = raw.strip()
                    if not raw_s:
                        continue
                    try:
                        entry = json.loads(raw_s)
                    except Exception:
                        continue
                    etype = str(entry.get("type") or "")
                    msg = entry.get("message") or {}
                    content = msg.get("content")
                    if etype == "user":
                        if isinstance(content, str) and content.strip():
                            if not any(nm in content for nm in _NOISE_MARKERS):
                                snippets.append(("user", content))
                        elif isinstance(content, list):
                            for blk in content:
                                if not isinstance(blk, dict):
                                    continue
                                btype = blk.get("type")
                                if btype == "text" and blk.get("text"):
                                    txt = str(blk["text"])
                                    if not any(nm in txt for nm in _NOISE_MARKERS):
                                        snippets.append(("user", txt))
                                elif btype == "tool_result":
                                    t_uid = str(blk.get("tool_use_id") or "")
                                    if t_uid in expect_pr_tool_ids:
                                        kind = expect_pr_tool_ids[t_uid]
                                        blk_s = json.dumps(blk, ensure_ascii=False)
                                        if not any(nm in blk_s for nm in _NOISE_MARKERS):
                                            snippets.append((kind, _extract_pr_payload_snippet(blk_s)))
                    elif etype == "assistant" and isinstance(content, list):
                        for blk in content:
                            if not isinstance(blk, dict):
                                continue
                            btype = blk.get("type")
                            if btype == "text" and blk.get("text"):
                                txt = str(blk["text"])
                                if not any(nm in txt for nm in _NOISE_MARKERS):
                                    snippets.append(("assistant", txt))
                            elif btype == "tool_use":
                                tname = str(blk.get("name") or "").lower()
                                t_uid = str(blk.get("id") or "")
                                inp = blk.get("input") or {}
                                inp_s = json.dumps(inp, ensure_ascii=False)
                                if any(nm in inp_s for nm in _NOISE_MARKERS):
                                    continue
                                if "/Projects/" in inp_s:
                                    snippets.append(("repo_hint", inp_s))
                                low_inp = inp_s.lower()
                                is_pr_call = (
                                    bool(_GH_PR_CMD_RE.search(inp_s))
                                    or bool(_GH_API_PULL_CMD_RE.search(inp_s))
                                    or ("pull_request" in tname and "list_pull_requests" not in tname)
                                )
                                if is_pr_call:
                                    kind = (
                                        "pr_created"
                                        if ("gh pr create" in low_inp or "create_pull_request" in tname)
                                        else "pr_cmd"
                                    )
                                    snippets.append((kind, inp_s))
                                    if t_uid:
                                        expect_pr_tool_ids[t_uid] = kind
        except Exception:
            pass

        self._transcript_pr_cache[cache_key] = (mtime, snippets)
        return snippets

    def _lookup_branch_pr(
        self,
        gh_repo: Optional[str],
        branch: Optional[str],
        repo_path: Optional[str] = None,
        allow_network: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Checks if an open/merged GitHub PR exists for the agent's current feature branch (cached)."""
        clean_branch = (branch or "").strip()
        if not clean_branch or clean_branch.lower() in _DEFAULT_BRANCHES:
            return None
        clean_repo = (gh_repo or "").strip()
        if not clean_repo:
            return None

        cache_key = (clean_repo.lower(), clean_branch)
        now = time.time()
        cached = self._branch_pr_cache.get(cache_key)
        if cached and (now - cached[0]) < 180.0:
            return cached[1]

        if not allow_network or not self.gh_bin:
            return cached[1] if cached else None

        full_repo = clean_repo if "/" in clean_repo else f"neon/{clean_repo}"
        cwd = (
            repo_path
            if (repo_path and Path(repo_path).exists() and not repo_path.startswith("superset://"))
            else str(Path.home())
        )
        try:
            proc = subprocess.run(
                [
                    self.gh_bin,
                    "pr",
                    "list",
                    "--repo",
                    full_repo,
                    "--head",
                    clean_branch,
                    "--state",
                    "all",
                    "--limit",
                    "1",
                    "--json",
                    "number,url,state",
                ],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=2.5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                arr = json.loads(proc.stdout.strip())
                if isinstance(arr, list) and arr:
                    top = arr[0]
                    pr_num = int(top.get("number") or 0)
                    pr_url = str(top.get("url") or "").strip()
                    if pr_num > 0 and pr_url:
                        item = {
                            "pr_url": pr_url,
                            "pr_number": pr_num,
                            "pr_repo": full_repo,
                            "pr_source": "branch",
                        }
                        res = {
                            **item,
                            "prs": [item],
                        }
                        self._branch_pr_cache[cache_key] = (now, res)
                        return res
            self._branch_pr_cache[cache_key] = (now, None)
        except Exception:
            pass
        return None

    def resolve_agent_pr(
        self,
        repo_path: Optional[str] = None,
        repo_name: Optional[str] = None,
        gh_repo: Optional[str] = None,
        branch: Optional[str] = None,
        title: Optional[str] = None,
        current_action: Optional[str] = None,
        last_thought: Optional[str] = None,
        terminal_tail: Optional[List[str]] = None,
        provider: Optional[str] = None,
        session_id: Optional[str] = None,
        check_gh_branch: bool = False,
    ) -> Dict[str, Any]:
        """Resolves all GitHub PRs associated with an agent conversation (created by the agent or given as context)."""
        default_repo = gh_repo or self.get_remote_repo_name(repo_path or "")
        if not default_repo and repo_name and repo_name.lower() not in ("workspace", "unknown", "projects", ""):
            default_repo = repo_name
        if default_repo and "/" in default_repo:
            default_repo = default_repo.split("/")[-1]

        snippets: List[Tuple[str, str]] = []
        if title and title.strip():
            snippets.append(("title", title.strip()))

        # 1. Disk transcript snippets (user inputs, assistant responses, PR tool calls & outputs)
        transcript_snippets = self._collect_transcript_pr_snippets(provider, session_id)
        if transcript_snippets:
            snippets.extend(transcript_snippets)
        elif terminal_tail:
            # Fallback for archived snapshots or PTY sessions where only terminal_tail is in memory
            active_block_kind: Optional[str] = None
            for line in terminal_tail:
                if not isinstance(line, str) or any(nm in line for nm in _NOISE_MARKERS):
                    continue
                if "/Projects/" in line or "--repo neon/" in line or "repos/neon/" in line:
                    snippets.append(("repo_hint", line))
                sline = line.strip()
                if sline.startswith("❯ "):
                    active_block_kind = "user"
                    snippets.append(("user", sline[2:]))
                elif sline.startswith("● "):
                    body = sline[2:].strip()
                    low_body = body.lower()
                    if any(
                        low_body.startswith(ro)
                        for ro in (
                            "view_file(",
                            "grep_search(",
                            "find_by_name(",
                            "list_dir(",
                            "replace_file_content(",
                            "write_to_file(",
                        )
                    ):
                        active_block_kind = None
                    elif (
                        _GH_PR_CMD_RE.search(body)
                        or _GH_API_PULL_CMD_RE.search(body)
                        or "creating pull request" in low_body
                        or "create_pull_request" in low_body
                    ):
                        active_block_kind = (
                            "pr_created"
                            if (
                                "gh pr create" in low_body
                                or "creating pull request" in low_body
                                or "create_pull_request" in low_body
                            )
                            else "pr_cmd"
                        )
                        snippets.append((active_block_kind, body))
                    elif "git log" in low_body or "gh pr list" in low_body or "gh run" in low_body:
                        active_block_kind = None
                    else:
                        active_block_kind = "assistant"
                        snippets.append(("assistant", body))
                elif sline.startswith("✻ "):
                    active_block_kind = None
                elif active_block_kind in ("user", "assistant", "pr_created", "pr_cmd"):
                    clean_cont = sline[3:].strip() if sline.startswith("⎿") else sline
                    snippets.append((active_block_kind, clean_cont))
                elif '"html_url":' in sline and "/pull/" in sline:
                    snippets.append(("pr_created", sline))

        if current_action and current_action.strip() and not any(nm in current_action for nm in _NOISE_MARKERS):
            snippets.append(("assistant", current_action.strip()))

        # Infer the effective repository from the conversation itself (handles cases where an IDE window
        # was opened on repo A, e.g. mobile-android, while the conversation actually worked on repo B, e.g. payments.invoice-statement)
        repo_votes: Dict[str, int] = {}
        for kind, text in snippets:
            if not text:
                continue
            for m_rh in _REPO_HINT_RE.finditer(text):
                r_cand = (m_rh.group(1) or m_rh.group(2) or m_rh.group(3) or "").strip().strip("/")
                if not r_cand or r_cand.lower() in ("pac-neon", "neon.cli", "superset", "projects", "workspace"):
                    continue
                if r_cand == "and_neon":
                    r_cand = "mobile-android"
                # Explicit `--repo neon/X` or `repos/neon/X/pulls` gets stronger weight
                weight = 3 if (m_rh.group(1) or m_rh.group(2)) else 1
                repo_votes[r_cand] = repo_votes.get(r_cand, 0) + weight

        effective_repo = default_repo
        if repo_votes:
            top_voted_repo, top_votes = max(repo_votes.items(), key=lambda kv: kv[1])
            default_votes = repo_votes.get(default_repo or "", 0)
            if not effective_repo or (top_voted_repo != effective_repo and top_votes > default_votes * 2 and top_votes >= 2):
                effective_repo = top_voted_repo

        candidates: Dict[Tuple[str, str, int], Dict[str, Any]] = {}

        def _record(
            owner: str,
            repo: str,
            number: int,
            kind: str,
            seq: int,
            is_created_hint: bool = False,
            is_url: bool = False,
        ) -> None:
            if number <= 0:
                return
            clean_owner = owner.strip().strip("/") or "neon"
            clean_repo = repo.strip().strip("/")
            if clean_repo.endswith(".git"):
                clean_repo = clean_repo[:-4]
            if clean_repo == "and_neon":
                clean_repo = "mobile-android"
            if not clean_repo:
                return
            key = (clean_owner.lower(), clean_repo.lower(), number)
            rec = candidates.get(key)
            if rec is None:
                rec = {
                    "owner": clean_owner,
                    "repo": clean_repo,
                    "number": number,
                    "created": False,
                    "in_title": False,
                    "in_pr_cmd": False,
                    "in_user": False,
                    "has_url": False,
                    "count": 0,
                    "first_seq": seq,
                    "last_seq": seq,
                }
                candidates[key] = rec
            rec["count"] += 1
            rec["last_seq"] = seq
            if is_url:
                rec["has_url"] = True
            if kind == "pr_created" or is_created_hint:
                rec["created"] = True
            if kind == "title":
                rec["in_title"] = True
            if kind == "pr_cmd":
                rec["in_pr_cmd"] = True
            if kind == "user":
                rec["in_user"] = True

        for seq, (kind, text) in enumerate(snippets):
            if kind == "repo_hint" or not text or any(nm in text for nm in _NOISE_MARKERS):
                continue
            found_url_in_snippet = False

            for m in _GITHUB_PR_URL_RE.finditer(text):
                owner_m, repo_m, num_s = m.group(1), m.group(2), m.group(3)
                start_pos = max(0, m.start() - 100)
                end_pos = min(len(text), m.end() + 100)
                ctx_window = text[start_pos:end_pos].lower()
                created_hint = (
                    kind == "pr_created"
                    or "create_pull_request" in ctx_window
                    or "gh pr create" in ctx_window
                    or "pr criado" in ctx_window
                    or "prs criados" in ctx_window
                    or "criei o pr" in ctx_window
                    or "novo pr" in ctx_window
                    or "abrimos o novo pr" in ctx_window
                    or "foram abertos no github" in ctx_window
                    or "aberto com sucesso" in ctx_window
                    or "pull request created" in ctx_window
                )
                _record(
                    owner_m,
                    repo_m,
                    int(num_s),
                    kind,
                    seq,
                    is_created_hint=created_hint,
                    is_url=True,
                )
                found_url_in_snippet = True

            for m in _GITHUB_API_PR_URL_RE.finditer(text):
                owner_m, repo_m, num_s = m.group(1), m.group(2), m.group(3)
                _record(
                    owner_m,
                    repo_m,
                    int(num_s),
                    kind,
                    seq,
                    is_created_hint=(kind == "pr_created"),
                    is_url=True,
                )
                found_url_in_snippet = True

            for m in _GH_API_PULL_CMD_RE.finditer(text):
                owner_m, repo_m, num_s = m.group(1), m.group(2), m.group(3)
                _record(owner_m, repo_m, int(num_s), "pr_cmd", seq, is_created_hint=False)
                found_url_in_snippet = True

            for m in _GH_PR_CMD_RE.finditer(text):
                subcmd, num_s, owner_m, repo_m = m.group(1), m.group(2), m.group(3), m.group(4)
                if num_s:
                    target_owner = owner_m or "neon"
                    target_repo = repo_m or effective_repo or ""
                    if target_repo:
                        is_cr = subcmd.lower() == "create" or kind == "pr_created"
                        _record(target_owner, target_repo, int(num_s), "pr_cmd", seq, is_created_hint=is_cr)
                        found_url_in_snippet = True

            if not found_url_in_snippet and effective_repo and kind in ("title", "user", "pr_cmd", "pr_created", "assistant"):
                for m in _PR_EXPLICIT_HASH_RE.finditer(text):
                    num_s = m.group(1) or m.group(2)
                    if not num_s:
                        continue
                    start_pos = max(0, m.start() - 80)
                    end_pos = min(len(text), m.end() + 80)
                    ctx_window = text[start_pos:end_pos].lower()
                    assistant_created_hint = (
                        kind == "pr_created"
                        or "pr #" + num_s + " aberto" in ctx_window
                        or "pr #" + num_s + " criado" in ctx_window
                        or "pr anterior" in ctx_window
                        or "novo pr" in ctx_window
                        or "criei o pr #" + num_s in ctx_window
                    )
                    if kind == "assistant" and not assistant_created_hint:
                        continue
                    _record("neon", effective_repo, int(num_s), kind, seq, is_created_hint=assistant_created_hint)

        if candidates:
            eff_repo_low = (effective_repo or "").lower()

            # Filter out cross-repo noise if there are candidates matching the effective repo
            matching_repo_cands = [
                c for c in candidates.values() if eff_repo_low and c["repo"].lower() == eff_repo_low
            ]
            pool = matching_repo_cands if matching_repo_cands else list(candidates.values())

            # Select coherent PRs belonging to this conversation:
            # - If the agent created 1+ PRs in the conversation, include all created PRs + any PR explicitly in the title.
            # - Otherwise, prefer PRs explicitly referenced by title or user prompt (plus repeated full PR URLs), falling back to PR commands.
            has_created = any(c["created"] for c in pool)
            if has_created:
                selected = [c for c in pool if c["created"] or c["in_title"]]
            else:
                has_user_or_title = any(c["in_title"] or c["in_user"] for c in pool)
                if has_user_or_title:
                    selected = [
                        c
                        for c in pool
                        if c["in_title"] or c["in_user"] or (c["has_url"] and c["count"] >= 2)
                    ]
                else:
                    selected = [c for c in pool if c["in_pr_cmd"]] or pool

            # Order buttons coherently by first appearance / PR number for multi-PR display
            selected.sort(key=lambda c: (int(c["first_seq"]), int(c["number"])))

            prs_list: List[Dict[str, Any]] = []
            for c in selected:
                o_str = c["owner"]
                r_str = c["repo"]
                n_int = int(c["number"])
                prs_list.append(
                    {
                        "pr_url": f"https://github.com/{o_str}/{r_str}/pull/{n_int}",
                        "pr_number": n_int,
                        "pr_repo": f"{o_str}/{r_str}",
                        "pr_source": "created" if c["created"] else "context",
                    }
                )

            def _rank(rec: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
                matches_repo = 1 if (eff_repo_low and rec["repo"].lower() == eff_repo_low) else 0
                actively_worked = 1 if (rec["in_pr_cmd"] or rec["count"] >= 2) else 0
                return (
                    1 if rec["created"] else 0,
                    1 if rec["in_title"] else 0,
                    actively_worked,
                    matches_repo,
                    int(rec["last_seq"]),
                )

            best = max(selected, key=_rank)
            owner_out = best["owner"]
            repo_out = best["repo"]
            num_out = int(best["number"])
            pr_url = f"https://github.com/{owner_out}/{repo_out}/pull/{num_out}"
            pr_source = "created" if best["created"] else "context"
            return {
                "pr_url": pr_url,
                "pr_number": num_out,
                "pr_repo": f"{owner_out}/{repo_out}",
                "pr_source": pr_source,
                "prs": prs_list,
            }

        # Fallback to Git branch PR lookup if available in cache or requested via check_gh_branch
        branch_pr = self._lookup_branch_pr(
            effective_repo, branch, repo_path=repo_path, allow_network=check_gh_branch
        )
        if branch_pr:
            return branch_pr

        return {
            "pr_url": None,
            "pr_number": None,
            "pr_repo": None,
            "pr_source": None,
            "prs": [],
        }

    def enrich_agent_metadata(
        self,
        repo_path: str,
        repo_name: str,
        branch: str,
        title: str,
        current_action: Optional[str] = None,
        last_thought: Optional[str] = None,
        terminal_tail: Optional[List[str]] = None,
        provider: Optional[str] = None,
        session_id: Optional[str] = None,
        check_gh_branch: bool = False,
    ) -> Dict[str, Any]:
        """Infers Neon K8s namespace, service name, Jira issue key, and associated GitHub PR(s)."""
        gh_repo = self.get_remote_repo_name(repo_path) or (
            repo_name if "." in (repo_name or "") else None
        )
        k8s_namespace: Optional[str] = None
        app_name: Optional[str] = None
        if gh_repo and "." in gh_repo:
            parts = gh_repo.split(".", 1)
            k8s_namespace = parts[0]
            app_name = parts[1]

        # Detect Jira issue key in branch or title (e.g. CCB-1234, PAY-567)
        jira_key: Optional[str] = None
        for candidate_text in (branch or "", title or ""):
            m = _JIRA_KEY_RE.search(candidate_text.upper())
            if m:
                jira_key = m.group(1)
                break

        jira_summary: Optional[str] = None
        if jira_key and self.flow_db_path.exists():
            try:
                conn = sqlite3.connect(self.flow_db_path)
                row = conn.execute(
                    "SELECT payload FROM issues WHERE key = ?", (jira_key,)
                ).fetchone()
                conn.close()
                if row and row[0]:
                    payload = json.loads(row[0])
                    fields = payload.get("fields") or {}
                    jira_summary = fields.get("summary")
            except Exception:
                pass

        pr_info = self.resolve_agent_pr(
            repo_path=repo_path,
            repo_name=repo_name,
            gh_repo=gh_repo,
            branch=branch,
            title=title,
            current_action=current_action,
            last_thought=last_thought,
            terminal_tail=terminal_tail,
            provider=provider,
            session_id=session_id,
            check_gh_branch=check_gh_branch,
        )

        # If gh_repo wasn't detected from the folder, inherit from the resolved PR repository if present
        if not gh_repo and pr_info.get("pr_repo"):
            gh_repo = str(pr_info["pr_repo"]).split("/")[-1]

        is_mobile_repo = any(
            marker in (repo_path or "").lower()
            for marker in ("and_neon", "mobile-android", "ios", "neonlumen")
        )

        return {
            "neon_cli_available": bool(self.neon_bin),
            "gh_repo": gh_repo,
            "k8s_namespace": k8s_namespace,
            "app_name": app_name,
            "service_name": gh_repo or repo_name,
            "jira_key": jira_key,
            "jira_summary": jira_summary,
            "glowworm_applicable": bool(self.glowworm_bin and is_mobile_repo),
            "pr_url": pr_info.get("pr_url"),
            "pr_number": pr_info.get("pr_number"),
            "pr_repo": pr_info.get("pr_repo"),
            "pr_source": pr_info.get("pr_source"),
            "prs": pr_info.get("prs") or [],
        }

    @staticmethod
    def _open_url_in_browser(url: str) -> bool:
        try:
            proc = subprocess.run(
                ["open", url],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if proc.returncode == 0:
                return True
        except Exception:
            pass
        try:
            return bool(webbrowser.open(url))
        except Exception:
            return False

    def execute_neon_action(
        self,
        action: str,
        repo_path: Optional[str] = None,
        repo_name: Optional[str] = None,
        jira_key: Optional[str] = None,
        env: str = "prod",
        pr_url: Optional[str] = None,
        pr_number: Optional[int] = None,
        pr_repo: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Executes a `neon` CLI command (`gh open`, `dd logs`, `dd service`, `flow open`) or opens the agent's PR."""
        cwd = (
            repo_path
            if (repo_path and Path(repo_path).exists() and not repo_path.startswith("superset://"))
            else str(Path.home())
        )
        gh_repo = (
            self.get_remote_repo_name(repo_path or "")
            or (repo_name if repo_name and repo_name.lower() not in ("workspace", "unknown", "projects", "") else None)
        )
        service_target = gh_repo or repo_name or ""

        # Direct handling for `gh_prs`: open the agent's specific PR if associated, otherwise fall back to PR list
        if action == "gh_prs":
            resolved_pr_url = (pr_url or "").strip() or None
            resolved_pr_num = pr_number
            resolved_pr_repo = pr_repo

            if not resolved_pr_url and gh_repo and branch:
                branch_pr = self._lookup_branch_pr(
                    gh_repo, branch, repo_path=repo_path, allow_network=True
                )
                if branch_pr and branch_pr.get("pr_url"):
                    resolved_pr_url = str(branch_pr["pr_url"])
                    resolved_pr_num = branch_pr.get("pr_number")
                    resolved_pr_repo = branch_pr.get("pr_repo")

            if resolved_pr_url:
                opened = self._open_url_in_browser(resolved_pr_url)
                pr_label = f"PR #{resolved_pr_num}" if resolved_pr_num else "PR"
                repo_suffix = f" ({resolved_pr_repo})" if resolved_pr_repo else ""
                return {
                    "ok": opened,
                    "command": f"open {resolved_pr_url}",
                    "url": resolved_pr_url,
                    "pr_number": resolved_pr_num,
                    "opened_specific_pr": True,
                    "message": f"Abrindo {pr_label}{repo_suffix} associado ao agente no GitHub",
                    "output": resolved_pr_url,
                }

            # Fallback: no specific PR associated with this agent -> open repository's PR list
            if not self.neon_bin:
                fallback_repo = gh_repo or "neon"
                prs_list_url = (
                    f"https://github.com/{fallback_repo}/pulls"
                    if "/" in fallback_repo
                    else f"https://github.com/neon/{fallback_repo}/pulls"
                )
                opened = self._open_url_in_browser(prs_list_url)
                return {
                    "ok": opened,
                    "command": f"open {prs_list_url}",
                    "url": prs_list_url,
                    "pr_number": None,
                    "opened_specific_pr": False,
                    "message": f"Sem PR específico na conversa — abrindo lista de PRs de {gh_repo or 'repositório'}",
                    "output": prs_list_url,
                }

        if not self.neon_bin:
            return {
                "ok": False,
                "message": "neon-cli não encontrado em ~/.local/bin/neon.",
            }

        cmd: List[str] = []
        description = ""

        if action == "gh_open":
            cmd = [self.neon_bin, "gh", "open"]
            if gh_repo:
                cmd.append(gh_repo)
            description = f"Abrindo repositório {gh_repo or 'atual'} no GitHub via neon-cli"
        elif action == "gh_prs":
            cmd = [self.neon_bin, "gh", "open"]
            if gh_repo:
                cmd.append(gh_repo)
            cmd.append("--prs")
            description = f"Sem PR específico na conversa — abrindo lista de PRs de {gh_repo or 'repositório'} via neon-cli"
        elif action == "dd_service":
            if not service_target:
                return {"ok": False, "message": "Nome do serviço não identificado para o Datadog."}
            cmd = [self.neon_bin, "dd", "service", service_target]
            description = f"Abrindo APM Service ({service_target}) no Datadog via neon-cli"
        elif action == "dd_logs":
            if not service_target:
                return {"ok": False, "message": "Nome do serviço não identificado para o Datadog."}
            cmd = [self.neon_bin, "dd", "logs", service_target, "--since", "15m"]
            if env:
                cmd.extend(["--env", env])
            description = f"Abrindo Logs ({service_target}) no Datadog via neon-cli"
        elif action == "flow_open":
            if not jira_key:
                return {"ok": False, "message": "Nenhum ticket Jira detectado na branch/título."}
            cmd = [self.neon_bin, "flow", "open", jira_key]
            description = f"Abrindo issue {jira_key} no Jira via neon flow"
        elif action == "pam_pam_token":
            cmd = [self.neon_bin, "pam-pam", "token"]
            description = "Copiando token pam-pam via neon-cli"
        else:
            return {"ok": False, "message": f"Ação neon-cli desconhecida: {action}"}

        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=4.0,
                check=False,
            )
            out = (proc.stdout or proc.stderr or "").strip()
            return {
                "ok": proc.returncode == 0,
                "command": " ".join(cmd),
                "opened_specific_pr": False,
                "message": description,
                "output": out[:300],
            }
        except Exception as exc:
            return {
                "ok": False,
                "command": " ".join(cmd),
                "message": f"Erro ao executar neon-cli: {exc}",
            }


def install_pac_neon_agent_hooks(port: int = 8765) -> bool:
    """Installs `~/.pac-neon/hooks/notify.sh` and merges lifecycle hooks into `~/.claude/settings.json`.

    Inspired directly by Superset (`packages/agent-setup/templates/notify-hook.template.sh`)
    and Herdr (`https://herdr.dev/`):
    - Fires on `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest`,
      `Notification`, `Stop`, `SessionStart`, `SessionEnd`.
    - Sends a fast, non-blocking background POST to `http://127.0.0.1:<port>/api/hooks/agent-event`
      so Pac-Neon updates agent statuses (`CHOMPING`, `POWER_PELLET_WAIT`, `IDLE`) in real-time (<10ms).
    """
    try:
        hooks_dir = Path.home() / ".pac-neon" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        notify_script = hooks_dir / "notify.sh"

        script_content = f"""#!/bin/bash
# Pac-Neon Native Agent Lifecycle & Permission Gate Hook (Herdr / Superset compatible)
# - For PermissionRequest: blocks synchronously (up to 600s) waiting for user click in Pac-Neon
#   and outputs Claude Code's native `hookSpecificOutput` JSON decision to stdout.
# - For all other lifecycle events: sends a fast non-blocking background POST (<10ms).

if [ -n "$1" ]; then
  INPUT="$1"
else
  INPUT=$(cat)
fi

[ -z "$INPUT" ] && exit 0

if printf '%s' "$INPUT" | grep -q '"hook_event_name"[[:space:]]*:[[:space:]]*"PermissionRequest"'; then
  RESPONSE=$(curl -sS -X POST "http://127.0.0.1:{port}/api/hooks/agent-event" \\
    --connect-timeout 0.6 --max-time 600 \\
    -H "Content-Type: application/json" \\
    -d "$INPUT" 2>/dev/null)
  if [ -n "$RESPONSE" ] && printf '%s' "$RESPONSE" | grep -q '"hookSpecificOutput"'; then
    printf '%s\\n' "$RESPONSE"
  fi
  exit 0
fi

curl -sX POST "http://127.0.0.1:{port}/api/hooks/agent-event" \\
  --connect-timeout 0.4 --max-time 0.8 \\
  -H "Content-Type: application/json" \\
  -d "$INPUT" >/dev/null 2>&1 &

exit 0
"""
        notify_script.write_text(script_content, encoding="utf-8")
        notify_script.chmod(
            notify_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )

        # Safely merge into ~/.claude/settings.json without clobbering user settings
        claude_settings_path = Path.home() / ".claude" / "settings.json"
        settings: Dict[str, Any] = {}
        if claude_settings_path.exists():
            try:
                settings = json.loads(
                    claude_settings_path.read_text(encoding="utf-8")
                )
            except Exception:
                return True

        if not isinstance(settings, dict):
            return True

        hooks_obj = settings.setdefault("hooks", {})
        if not isinstance(hooks_obj, dict):
            return True

        hook_cmd = f"[ -x \"{notify_script}\" ] && \"{notify_script}\" || true"
        managed_events = {
            "SessionStart": None,
            "SessionEnd": None,
            "UserPromptSubmit": None,
            "Stop": None,
            "PermissionRequest": "*",
            "Notification": "*",
            "PreToolUse": "*",
            "PostToolUse": "*",
        }

        changed = False
        for event_name, matcher in managed_events.items():
            event_list = hooks_obj.setdefault(event_name, [])
            if not isinstance(event_list, list):
                continue
            already_present = False
            for entry in event_list:
                if not isinstance(entry, dict):
                    continue
                for h in entry.get("hooks") or []:
                    if isinstance(h, dict) and ".pac-neon/hooks/notify.sh" in str(
                        h.get("command", "")
                    ):
                        already_present = True
                        break
            if not already_present:
                new_entry: Dict[str, Any] = {
                    "hooks": [{"type": "command", "command": hook_cmd}]
                }
                if matcher:
                    new_entry["matcher"] = matcher
                event_list.append(new_entry)
                changed = True

        if changed:
            claude_settings_path.parent.mkdir(parents=True, exist_ok=True)
            claude_settings_path.write_text(
                json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        return True
    except Exception:
        return False
