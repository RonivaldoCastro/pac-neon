#!/usr/bin/env python3
"""
CLI & Auto-Trigger utility for Pac-Neon:
1. `python3 -m pac_neon.cli start`:
   Ensures the Pac-Neon server is running in the background and opens http://127.0.0.1:8765 in the browser.
2. `python3 -m pac_neon.cli install-trigger`:
   Installs the automatic trigger in ~/.claude/settings.json, ~/.gemini/settings.json, and ~/.zshrc
   so Pac-Neon starts and pops open automatically whenever an agent needs permission or via the `pac-neon` command!
3. `python3 -m pac_neon.cli wrap --provider claude -- claude`:
   Runs any agent inside a transparent local PTY bridge.
"""
from __future__ import annotations

import argparse
import json
import os
import pty
import select
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pac_neon.pty_manager import PERMISSION_PATTERNS, strip_ansi

PAC_NEON_URL = os.environ.get("PAC_NEON_URL", "http://127.0.0.1:8765")
HOOK_SCRIPT = Path(__file__).resolve().parent / "hooks" / "pac_neon_hook.py"


def _is_server_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{PAC_NEON_URL}/api/superset/status", timeout=0.8) as resp:
            return resp.status == 200
    except Exception:
        return False


PLIST_LABEL = "com.neon.pac-neon"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def ensure_daemon_and_open(open_browser: bool = True) -> int:
    """Starts the Pac-Neon server as a standalone OS background service if not running, and opens the browser."""
    if _is_server_alive():
        print(f"🟢 Pac-Neon já está ativo em segundo plano: {PAC_NEON_URL}")
        if open_browser:
            webbrowser.open(PAC_NEON_URL, new=0, autoraise=True)
        return 0

    # If macOS LaunchAgent is installed, start via launchctl first
    if sys.platform == "darwin" and PLIST_PATH.exists():
        subprocess.run(["launchctl", "load", "-w", str(PLIST_PATH)], capture_output=True)
        subprocess.run(["launchctl", "start", PLIST_LABEL], capture_output=True)
        for _ in range(15):
            time.sleep(0.15)
            if _is_server_alive():
                print(f"🚀 Pac-Neon iniciado via serviço nativo do macOS em {PAC_NEON_URL}!")
                if open_browser:
                    webbrowser.open(PAC_NEON_URL, new=0, autoraise=True)
                return 0

    log_path = Path.home() / ".pac_neon_server.log"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["PAC_NEON_NO_OPEN"] = "1"

    print(f"🕹️  Iniciando serviço standalone Pac-Neon em segundo plano (log: {log_path})...")
    with open(log_path, "ab") as lf:
        subprocess.Popen(
            [sys.executable, "-m", "pac_neon.server", "--no-open"],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=lf,
            stderr=lf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    for _ in range(25):
        time.sleep(0.15)
        if _is_server_alive():
            print(f"🚀 Pac-Neon pronto em {PAC_NEON_URL}!")
            if open_browser:
                webbrowser.open(PAC_NEON_URL, new=0, autoraise=True)
            return 0

    if open_browser:
        webbrowser.open(PAC_NEON_URL, new=0, autoraise=True)
    print(f"⚠️ Serviço iniciado em segundo plano. Acesse {PAC_NEON_URL}")
    return 0


def stop_daemon() -> int:
    """Stops the standalone Pac-Neon background service."""
    if sys.platform == "darwin" and PLIST_PATH.exists():
        subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    try:
        res = subprocess.run(["lsof", "-ti", "tcp:8765"], capture_output=True, text=True, timeout=2.0)
        for line in res.stdout.splitlines():
            if line.strip().isdigit():
                os.kill(int(line.strip()), 15)
    except Exception:
        pass
    print("🛑 Serviço Pac-Neon encerrado.")
    return 0


def restart_daemon(open_browser: bool = False) -> int:
    """Restarts the standalone Pac-Neon background service."""
    stop_daemon()
    time.sleep(0.4)
    if sys.platform == "darwin" and PLIST_PATH.exists():
        subprocess.run(["launchctl", "load", "-w", str(PLIST_PATH)], capture_output=True)
    return ensure_daemon_and_open(open_browser=open_browser)


def _install_macos_app_bundle(app_dir: Path) -> None:
    """Creates a native clickable macOS .app bundle (Spotlight / Dock / Finder) that opens Pac-Neon without a terminal or agent."""
    contents_dir = app_dir / "Contents"
    macos_dir = contents_dir / "MacOS"
    resources_dir = contents_dir / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)

    info_plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>Pac-Neon</string>
    <key>CFBundleIdentifier</key>
    <string>com.neon.pac-neon.app</string>
    <key>CFBundleName</key>
    <string>Pac-Neon</string>
    <key>CFBundleDisplayName</key>
    <string>Pac-Neon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
"""
    (contents_dir / "Info.plist").write_text(info_plist, encoding="utf-8")

    launcher_script = f"""#!/bin/zsh
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="{PROJECT_ROOT}"
if ! curl -fsS "{PAC_NEON_URL}/api/superset/status" >/dev/null 2>&1; then
    "{sys.executable}" -m pac_neon.cli start --no-open >/dev/null 2>&1 &
    for i in {{1..20}}; do
        sleep 0.15
        if curl -fsS "{PAC_NEON_URL}/api/superset/status" >/dev/null 2>&1; then
            break
        fi
    done
fi
open "{PAC_NEON_URL}"
"""
    exe_path = macos_dir / "Pac-Neon"
    exe_path.write_text(launcher_script, encoding="utf-8")
    exe_path.chmod(0o755)


def _install_global_cli_bins() -> None:
    """Installs a global `pac-neon` executable script in ~/.local/bin and /opt/homebrew/bin."""
    wrapper = f"""#!/bin/zsh
export PYTHONPATH="{PROJECT_ROOT}"
exec "{sys.executable}" -m pac_neon.cli "$@"
"""
    for bin_dir in [Path.home() / ".local" / "bin", Path("/opt/homebrew/bin")]:
        try:
            bin_dir.mkdir(parents=True, exist_ok=True)
            target = bin_dir / "pac-neon"
            target.write_text(wrapper, encoding="utf-8")
            target.chmod(0o755)
            print(f"✅ Comando global instalado: {target}")
        except Exception:
            pass


def install_auto_triggers(install_launch_agent: bool = True, open_browser: bool = False) -> int:
    """Installs standalone macOS LaunchAgent, native Pac-Neon.app, global `pac-neon` CLI, and Claude hooks."""
    hook_cmd = f"python3 {HOOK_SCRIPT}"

    # 1. Configure ~/.claude/settings.json (PreToolUse hook)
    claude_settings_path = Path.home() / ".claude" / "settings.json"
    try:
        claude_settings_path.parent.mkdir(parents=True, exist_ok=True)
        if claude_settings_path.exists():
            data = json.loads(claude_settings_path.read_text(encoding="utf-8"))
        else:
            data = {}
        hooks = data.setdefault("hooks", {})
        pre_tool = hooks.setdefault("PreToolUse", [])
        already_installed = any(
            "pac_neon_hook.py" in json.dumps(entry) for entry in pre_tool
        )
        if not already_installed:
            pre_tool.append(
                {
                    "matcher": "Bash|Write|Edit|MultiEdit",
                    "hooks": [{"type": "command", "command": hook_cmd}],
                }
            )
            claude_settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(f"✅ Trigger automático instalado no Claude Code: {claude_settings_path}")
    except Exception as exc:
        print(f"⚠️ Não foi possível atualizar {claude_settings_path}: {exc}")

    # 2. Install global `pac-neon` executable + ~/.zshrc alias
    _install_global_cli_bins()
    zshrc_path = Path.home() / ".zshrc"
    alias_line = f'alias pac-neon="PYTHONPATH={PROJECT_ROOT} {sys.executable} -m pac_neon.cli"'
    try:
        current_zshrc = zshrc_path.read_text(encoding="utf-8") if zshrc_path.exists() else ""
        if "alias pac-neon=" not in current_zshrc:
            with open(zshrc_path, "a", encoding="utf-8") as zf:
                zf.write(f"\n# Pac-Neon Multi-Agent Arcade Orchestrator Trigger\n{alias_line}\n")
    except Exception:
        pass

    # 3. Install native macOS App Bundle (Spotlight / Dock / ~/Applications)
    if sys.platform == "darwin":
        app_target = Path.home() / "Applications" / "Pac-Neon.app"
        try:
            _install_macos_app_bundle(app_target)
            print(f"✅ Aplicativo macOS instalado: {app_target}")
        except Exception as exc:
            print(f"⚠️ Erro ao criar {app_target}: {exc}")

    # 4. Install macOS LaunchAgent so Pac-Neon runs standalone in the OS background (NEVER allocating an AI agent)
    if install_launch_agent and sys.platform == "darwin":
        plist_dir = PLIST_PATH.parent
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>-m</string>
        <string>pac_neon.server</string>
        <string>--no-open</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key>
        <string>{PROJECT_ROOT}</string>
        <key>PAC_NEON_NO_OPEN</key>
        <string>1</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>{PROJECT_ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{Path.home() / ".pac_neon_server.log"}</string>
    <key>StandardErrorPath</key>
    <string>{Path.home() / ".pac_neon_server.log"}</string>
</dict>
</plist>
"""
        try:
            plist_dir.mkdir(parents=True, exist_ok=True)
            PLIST_PATH.write_text(plist_content, encoding="utf-8")
            subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
            subprocess.run(["launchctl", "load", "-w", str(PLIST_PATH)], capture_output=True)
            print(f"✅ Serviço nativo macOS (LaunchAgent) ativo: {PLIST_PATH}")
        except Exception as exc:
            print(f"⚠️ Erro ao registrar LaunchAgent: {exc}")

    return ensure_daemon_and_open(open_browser=open_browser)


def _post_json(path: str, payload: dict, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(
        f"{PAC_NEON_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def run_wrapper(provider: str, name: str | None, command: list[str]) -> int:
    if not command:
        print("Uso: python3 -m pac_neon.cli wrap --provider claude -- <comando>", file=sys.stderr)
        return 1

    ensure_daemon_and_open(open_browser=False)

    cwd = os.getcwd()
    repo_name = Path(cwd).name
    agent_id = f"{provider}-wrap-{os.getpid()}"
    agent_name = name or f"{provider.capitalize()} ({repo_name})"

    master_fd, slave_fd = pty.openpty()
    pid = os.fork()
    if pid == 0:
        os.setsid()
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        if master_fd > 2:
            os.close(master_fd)
        os.execvp(command[0], command)

    os.close(slave_fd)
    stop_flag = False
    recent_buffer = ""

    def poll_remote_commands() -> None:
        while not stop_flag:
            res = _post_json(
                "/api/bridge/heartbeat",
                {
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "provider": provider,
                    "repo_path": cwd,
                    "recent_output": recent_buffer[-300:],
                },
            )
            for cmd in res.get("pending_inputs", []):
                try:
                    os.write(master_fd, cmd.encode("utf-8"))
                except OSError:
                    pass
            time.sleep(0.6)

    poller = threading.Thread(target=poll_remote_commands, daemon=True)
    poller.start()

    try:
        while True:
            rlist, _, _ = select.select([master_fd, sys.stdin.fileno()], [], [], 0.2)
            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
                clean = strip_ansi(data.decode("utf-8", errors="replace"))
                recent_buffer = (recent_buffer + clean)[-800:]
                if any(p.search(recent_buffer[-300:]) for p in PERMISSION_PATTERNS):
                    _post_json(
                        "/api/bridge/permission-alert",
                        {
                            "agent_id": agent_id,
                            "agent_name": agent_name,
                            "provider": provider,
                            "repo_path": cwd,
                            "command_preview": clean.strip()[-140:] or "Aguardando confirmação (y/n)",
                        },
                    )
            if sys.stdin.fileno() in rlist:
                user_in = os.read(sys.stdin.fileno(), 1024)
                if not user_in:
                    break
                os.write(master_fd, user_in)
    finally:
        stop_flag = True
        try:
            os.close(master_fd)
        except OSError:
            pass

    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def run_cli_doctor(as_json: bool = False, prompt_accessibility: bool = False) -> int:
    """Runs the environment diagnostic for Neon collaborators and prints a friendly checklist."""
    from pac_neon.server import _is_macos_accessibility_trusted, run_environment_doctor

    if prompt_accessibility and sys.platform == "darwin":
        _is_macos_accessibility_trusted(prompt=True)

    report = run_environment_doctor()
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report.get("ok") else 1

    print("\n🕹️  PAC-NEON DOCTOR — Diagnóstico de Ambiente (Engenharia Neon)")
    print("=" * 68)
    print(f"👤 Colaborador : {report.get('user')} ({report.get('home')})")
    print(f"📂 Repo Padrão : {report.get('default_repo_path')} ({report.get('discovered_repos_count')} repos descobertos)")
    print(f"🎯 Prontidão   : {report.get('ready_score_pct')}% ({report.get('passed_checks')}/{report.get('total_checks')} checks OK)")
    print("-" * 68)
    for chk in report.get("checks", []):
        icon = "✅" if chk.get("ok") else "⚠️"
        print(f" {icon} {chk.get('title')}: {chk.get('detail')}")
        if not chk.get("ok") and chk.get("fix_cmd"):
            print(f"    ↳ Como resolver: `{chk.get('fix_cmd')}`")
    print("=" * 68)
    return 0


def uninstall_pac_neon() -> int:
    """Cleanly stops the server and removes LaunchAgent, macOS App bundle, and global CLI wrappers."""
    import shutil

    stop_daemon()
    if PLIST_PATH.exists():
        try:
            PLIST_PATH.unlink()
            print(f"🗑️ LaunchAgent removido: {PLIST_PATH}")
        except Exception:
            pass
    app_bundle = Path.home() / "Applications" / "Pac-Neon.app"
    if app_bundle.exists():
        try:
            shutil.rmtree(app_bundle)
            print(f"🗑️ Aplicativo removido: {app_bundle}")
        except Exception:
            pass
    for bin_dir in [Path.home() / ".local" / "bin", Path("/opt/homebrew/bin")]:
        target = bin_dir / "pac-neon"
        if target.exists():
            try:
                target.unlink()
                print(f"🗑️ Comando global removido: {target}")
            except Exception:
                pass
    print("✅ Pac-Neon desinstalado com sucesso.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="pac-neon", description="Pac-Neon Multi-Agent CLI & Auto-Trigger")
    sub = parser.add_subparsers(dest="subcommand")

    start_p = sub.add_parser("start", help="Inicia o serviço standalone do Pac-Neon e abre o navegador")
    start_p.add_argument("--no-open", action="store_true", help="Inicia o serviço sem abrir o navegador")

    sub.add_parser("stop", help="Encerra o serviço em segundo plano do Pac-Neon")
    sub.add_parser("restart", help="Reinicia o serviço em segundo plano do Pac-Neon")
    sub.add_parser("status", help="Verifica se o serviço do Pac-Neon está rodando")

    doc_p = sub.add_parser("doctor", help="Executa autodiagnóstico completo do ambiente do colaborador Neon")
    doc_p.add_argument("--json", action="store_true", help="Saída em formato JSON")
    doc_p.add_argument(
        "--prompt-accessibility",
        action="store_true",
        help="Solicita permissão de Acessibilidade do macOS (System Events)",
    )

    sub.add_parser("uninstall", help="Remove o LaunchAgent, Pac-Neon.app e atalhos globais")

    inst_p = sub.add_parser(
        "install-trigger",
        aliases=["install"],
        help="Instala o serviço nativo do macOS (LaunchAgent), App Pac-Neon.app e comando global pac-neon",
    )
    inst_p.add_argument(
        "--login-daemon",
        action="store_true",
        default=True,
        help="Instala o LaunchAgent no macOS para iniciar sempre sozinho no login",
    )
    inst_p.add_argument(
        "--no-open",
        action="store_true",
        help="Não abrir o navegador após instalar",
    )

    wrap_p = sub.add_parser("wrap", help="Executa um agente conectado ao controle remoto do Pac-Neon")
    wrap_p.add_argument("--provider", default="claude", help="Ex: claude, gemini, codex, antigravity")
    wrap_p.add_argument("--name", default=None, help="Nome amigável do agente no mapa Arcade")
    wrap_p.add_argument("cmd", nargs=argparse.REMAINDER, help="Comando do agente (após --)")

    sub.add_parser("show-hook-config", help="Exibe configuração de Hook para Claude Code / Agentes")

    args = parser.parse_args()
    if args.subcommand is None:
        return ensure_daemon_and_open(open_browser=True)
    if args.subcommand == "start":
        return ensure_daemon_and_open(open_browser=not getattr(args, "no_open", False))
    if args.subcommand == "stop":
        return stop_daemon()
    if args.subcommand == "restart":
        return restart_daemon(open_browser=False)
    if args.subcommand == "status":
        alive = _is_server_alive()
        print(f"{'🟢 ONLINE' if alive else '🔴 OFFLINE'} — {PAC_NEON_URL}")
        return 0 if alive else 1
    if args.subcommand == "doctor":
        return run_cli_doctor(
            as_json=getattr(args, "json", False),
            prompt_accessibility=getattr(args, "prompt_accessibility", False),
        )
    if args.subcommand == "uninstall":
        return uninstall_pac_neon()
    if args.subcommand in ("install-trigger", "install"):
        return install_auto_triggers(
            install_launch_agent=True,
            open_browser=not getattr(args, "no_open", False),
        )
    if args.subcommand == "wrap":
        cmd = args.cmd
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        return run_wrapper(args.provider, args.name, cmd)
    if args.subcommand == "show-hook-config":
        config = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash|Edit|Write",
                        "hooks": [{"type": "command", "command": f"python3 {HOOK_SCRIPT}"}],
                    }
                ]
            }
        }
        print(json.dumps(config, indent=2))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
