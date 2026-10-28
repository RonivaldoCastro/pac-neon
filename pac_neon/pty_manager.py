from __future__ import annotations

import asyncio
import os
import pty
import re
import shlex
import signal
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pac_neon.adapters import AdapterRegistry
from pac_neon.models import (
    AgentGameState,
    AgentUnit,
    PermissionRequestItem,
    PermissionResolutionAction,
    utc_now_iso,
)

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

PERMISSION_PATTERNS = [
    re.compile(r"Do you want to proceed\?", re.IGNORECASE),
    re.compile(r"Allow\s+.*\s+to\s+(run|execute|write|edit|read)", re.IGNORECASE),
    re.compile(r"\[(y/n|Y/n|y/N|yes/no)\]", re.IGNORECASE),
    re.compile(r"\((y/n|Y/n|yes/no)\)", re.IGNORECASE),
    re.compile(r"1\.\s*Yes.*2\.\s*Yes", re.IGNORECASE | re.DOTALL),
    re.compile(r"Approve\s+(tool|command|execution)", re.IGNORECASE),
    re.compile(r"Apply\s+changes\?", re.IGNORECASE),
]


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


class ManagedPTYSession:
    """Wraps a live CLI agent inside a pseudo-terminal (PTY) for remote command & permission injection."""

    def __init__(
        self,
        unit: AgentUnit,
        command: List[str],
        cwd: str,
        on_state_change: Optional[Callable[[AgentUnit], None]] = None,
    ):
        self.unit = unit
        self.command = command
        self.cwd = cwd
        self.on_state_change = on_state_change
        self.master_fd: Optional[int] = None
        self.pid: Optional[int] = None
        self.tty_name: Optional[str] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._closed = False

    async def start(self) -> None:
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        try:
            self.tty_name = os.ttyname(slave_fd).replace("/dev/", "")
        except Exception:
            self.tty_name = None

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        extra_paths = [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            str(Path.home() / ".local" / "bin"),
            str(Path.home() / ".npm-global" / "bin"),
            str(Path.home() / ".cargo" / "bin"),
        ]
        cur_path = env.get("PATH", "")
        env["PATH"] = ":".join([p for p in extra_paths if p not in cur_path.split(":")] + ([cur_path] if cur_path else []))

        target_cwd = self.cwd if os.path.isdir(self.cwd) else str(Path.home())

        self.pid = os.fork()
        if self.pid == 0:
            # Child process
            os.setsid()
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            if master_fd > 2:
                os.close(master_fd)
            try:
                os.chdir(target_cwd)
                os.execvpe(self.command[0], self.command, env)
            except Exception as exc:
                os.write(2, f"\r\n[PAC-NEON PTY ERROR] Falha ao iniciar '{self.command[0]}': {exc}\r\n".encode("utf-8"))
                os._exit(1)
        else:
            os.close(slave_fd)
            os.set_blocking(master_fd, False)
            self.unit.pid = self.pid
            if self.tty_name:
                self.unit.tty = self.tty_name
            self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self.master_fd is not None
        loop = asyncio.get_running_loop()
        buffer = ""

        while not self._closed:
            try:
                data = await loop.run_in_executor(None, self._blocking_read, self.master_fd)
                if not data:
                    await asyncio.sleep(0.08)
                    continue
                decoded = data.decode("utf-8", errors="replace")
                clean = strip_ansi(decoded)
                buffer = (buffer + clean)[-1200:]

                lines = [ln.rstrip() for ln in clean.splitlines() if ln.strip()]
                if lines:
                    self.unit.terminal_tail = (self.unit.terminal_tail + lines)[-800:]
                    self.unit.current_action = lines[-1][:140]
                    self.unit.pellets_eaten = min(self.unit.pellets_total, self.unit.pellets_eaten + 1)
                    self.unit.score_xp += 50
                    self.unit.updated_at = utc_now_iso()

                # Detect permission prompts automatically
                if self.unit.pending_permission is None and self._looks_like_permission_prompt(buffer):
                    snippet = lines[-1] if lines else buffer[-160:].strip()
                    perm = PermissionRequestItem(
                        agent_id=self.unit.id,
                        agent_name=self.unit.name,
                        provider=self.unit.provider,
                        repo_name=self.unit.repo_name,
                        tool_name="Terminal / PTY Prompt",
                        command_preview=snippet[:160],
                        reason="Prompt de confirmação detectado automaticamente no terminal do agente.",
                        channel="pty",
                    )
                    self.unit.pending_permission = perm
                    self.unit.game_state = AgentGameState.POWER_PELLET_WAIT

                if self.on_state_change:
                    self.on_state_change(self.unit)
            except BlockingIOError:
                await asyncio.sleep(0.08)
            except OSError:
                break

    @staticmethod
    def _blocking_read(fd: int) -> bytes:
        try:
            return os.read(fd, 4096)
        except BlockingIOError:
            return b""

    @staticmethod
    def _looks_like_permission_prompt(buffer: str) -> bool:
        tail = buffer[-400:]
        return any(pattern.search(tail) for pattern in PERMISSION_PATTERNS)

    def write_stdin(self, text: str) -> bool:
        if self.master_fd is None or self._closed:
            return False
        try:
            os.write(self.master_fd, text.encode("utf-8"))
            return True
        except OSError:
            return False

    def resolve_permission(self, action: PermissionResolutionAction, custom_text: Optional[str] = None) -> bool:
        adapter = AdapterRegistry.get(self.unit.provider)
        seq = (
            adapter.format_approval_sequence(action.value, custom_text)
            if adapter
            else (custom_text + "\r" if custom_text else ("y\r" if action != PermissionResolutionAction.DENY else "n\r"))
        )
        ok = self.write_stdin(seq)
        if self.unit.pending_permission:
            self.unit.pending_permission.status = (
                "DENIED" if action == PermissionResolutionAction.DENY else "APPROVED"
            )
            self.unit.pending_permission.resolved_at = utc_now_iso()
            self.unit.pending_permission = None
        self.unit.game_state = AgentGameState.CHOMPING
        self.unit.score_xp += 250
        self.unit.updated_at = utc_now_iso()
        return ok

    def terminate(self) -> None:
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass


class PTYSessionManager:
    """Manages all active PTY-backed agents and simulated/hook-backed command queues."""

    def __init__(self) -> None:
        self.sessions: Dict[str, ManagedPTYSession] = {}
        self.command_outbox: Dict[str, List[str]] = {}

    async def spawn_pty_agent(
        self,
        unit: AgentUnit,
        command_str: str,
        on_state_change: Optional[Callable[[AgentUnit], None]] = None,
    ) -> ManagedPTYSession:
        cmd_parts = shlex.split(command_str)
        session = ManagedPTYSession(unit=unit, command=cmd_parts, cwd=unit.repo_path, on_state_change=on_state_change)
        await session.start()
        self.sessions[unit.id] = session
        return session

    def send_command(self, unit: AgentUnit, prompt: str, auto_enter: bool = True) -> bool:
        payload = prompt.rstrip("\r\n") + ("\r" if auto_enter else "")
        session = self.sessions.get(unit.id)
        if session and session.write_stdin(payload):
            unit.terminal_tail = (unit.terminal_tail + [f"🕹️ [PAC-NEON CMD] > {prompt}"])[-800:]
            unit.current_action = f"Executando comando remoto: {prompt[:100]}"
            unit.game_state = AgentGameState.CHOMPING
            unit.score_xp += 100
            unit.pellets_eaten = min(unit.pellets_total, unit.pellets_eaten + 1)
            unit.updated_at = utc_now_iso()
            return True

        # Fallback: Queue command in outbox for wrapped CLI / hook / SDK polling or interactive simulation
        self.command_outbox.setdefault(unit.id, []).append(prompt)
        unit.terminal_tail = (unit.terminal_tail + [f"🕹️ [PAC-NEON CMD] > {prompt}"])[-800:]
        unit.current_action = f"Executando ordem: {prompt[:110]}"
        unit.last_thought = f"Recebi instrução direta via Pac-Neon Command Center: '{prompt}'"
        unit.game_state = AgentGameState.CHOMPING
        unit.score_xp += 100
        unit.pellets_eaten = min(unit.pellets_total, unit.pellets_eaten + 1)
        unit.updated_at = utc_now_iso()
        return True

    def pop_queued_commands(self, agent_id: str) -> List[str]:
        return self.command_outbox.pop(agent_id, [])
