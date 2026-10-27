#!/usr/bin/env python3
"""
Universal PreToolUse / Permission Hook + Auto-Trigger for Claude Code, Gemini CLI, Codex, and custom agents.

Features:
1. If the Pac-Neon server (http://127.0.0.1:8765) is NOT running yet when an agent needs permission,
   this hook AUTOMATICALLY spawns `python3 -m pac_neon.server` in the background and pops open the
   Pac-Neon Arcade Cockpit in your browser!
2. Sends the pending tool execution details to Pac-Neon and waits for you to click ALLOW [Y] or DENY [N].
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PAC_NEON_URL = os.environ.get("PAC_NEON_URL", "http://127.0.0.1:8765")
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _ensure_server_running_and_open() -> bool:
    """Checks if Pac-Neon server is alive on :8765; if not, auto-starts it in background and opens the browser."""
    try:
        with urllib.request.urlopen(f"{PAC_NEON_URL}/api/superset/status", timeout=0.8) as resp:
            if resp.status == 200:
                return True
    except Exception:
        pass

    # Auto-spawn Pac-Neon server in the background!
    try:
        log_path = Path.home() / ".pac_neon_server.log"
        with open(log_path, "ab") as log_file:
            subprocess.Popen(
                [sys.executable, "-m", "pac_neon.server"],
                cwd=str(PROJECT_ROOT),
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
        # Wait up to 3 seconds for server to bind
        for _ in range(15):
            time.sleep(0.2)
            try:
                with urllib.request.urlopen(f"{PAC_NEON_URL}/api/superset/status", timeout=0.5) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def main() -> int:
    raw_stdin = sys.stdin.read().strip() if not sys.stdin.isatty() else "{}"
    try:
        hook_data = json.loads(raw_stdin) if raw_stdin else {}
    except json.JSONDecodeError:
        hook_data = {"raw": raw_stdin}

    tool_name = (
        hook_data.get("tool_name")
        or hook_data.get("tool")
        or os.environ.get("PAC_NEON_TOOL", "Bash")
    )
    tool_input = hook_data.get("tool_input") or hook_data.get("input") or {}
    if isinstance(tool_input, dict):
        cmd_preview = (
            tool_input.get("command")
            or tool_input.get("file_path")
            or tool_input.get("path")
            or json.dumps(tool_input)[:140]
        )
    else:
        cmd_preview = str(tool_input)[:140]

    cwd = hook_data.get("cwd") or os.environ.get("SUPERSET_WORKSPACE_PATH") or os.getcwd()
    superset_ws = os.environ.get("SUPERSET_WORKSPACE_ID", "")
    superset_term = os.environ.get("SUPERSET_TERMINAL_ID", "")
    session_id = (
        hook_data.get("session_id")
        or superset_term
        or os.environ.get("PAC_NEON_AGENT_ID")
        or f"ext-{os.getppid()}"
    )
    provider = os.environ.get("PAC_NEON_PROVIDER", "claude")

    # Ensure Pac-Neon Server + Browser Cockpit are running automatically!
    _ensure_server_running_and_open()

    # If running inside a Superset terminal, also fire a native Superset notification!
    if superset_ws:
        import shutil

        superset_bin = shutil.which("superset")
        if superset_bin:
            try:
                subprocess.Popen(
                    [
                        superset_bin,
                        "notifications",
                        "show",
                        f"PAC-NEON // Permissão ({tool_name})",
                        "--body",
                        str(cmd_preview)[:100],
                        "--sound",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

    payload = {
        "agent_id": f"{provider}-{str(session_id)[:8]}",
        "agent_name": f"{provider.capitalize()} #{str(session_id)[:4]}",
        "provider": provider,
        "repo_path": cwd,
        "tool_name": str(tool_name),
        "command_preview": str(cmd_preview),
        "reason": f"Aprovação solicitada via Hook ({tool_name}) em {Path(cwd).name}",
        "timeout_seconds": 180,
    }

    req = urllib.request.Request(
        f"{PAC_NEON_URL}/api/hooks/request-permission",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=185) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            decision = result.get("decision", "allow")
            if decision == "deny":
                sys.stderr.write(result.get("message", "Bloqueado pelo usuário no painel Pac-Neon.\n"))
                return 2
            print(json.dumps({"permissionDecision": "allow"}))
            return 0
    except urllib.error.URLError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
