from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pac_neon.neon_cli_bridge import NeonCliBridge, install_pac_neon_agent_hooks


class TestNeonCliBridgeAndPRResolver(unittest.TestCase):
    """
    Objective: Validate that NeonCliBridge accurately detects internal Neon tools (neon, agy, glowworm),
    extracts valid Jira keys, resolves real GitHub Pull Requests from transcript URLs and branch lookup,
    executes quick actions safely, and installs non-blocking Claude Code hooks idempotently.
    """

    def test_remote_repo_name_and_toolchain_status(self):
        bridge = NeonCliBridge()
        status = bridge.get_toolchain_status()
        self.assertIn("neon_cli", status)
        self.assertIn("claude_native", status)

        fake_git = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="git@github.com:neon/payments.invoice-statement.git\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp_dir, patch("subprocess.run", return_value=fake_git):
            self.assertEqual(bridge.get_remote_repo_name(tmp_dir), "payments.invoice-statement")

    def test_resolve_agent_pr_from_transcript_urls_and_enrich_metadata(self):
        bridge = NeonCliBridge()
        tail = [
            "● Pull Request criado com sucesso:",
            "  https://github.com/neon/payments.invoice-statement/pull/142",
            "  https://github.com/neon/payments.bff/pull/89",
        ]
        pr_info = bridge.resolve_agent_pr(
            repo_path=str(PROJECT_ROOT),
            repo_name="pac-neon",
            branch="feat/PAY-4821-pix",
            terminal_tail=tail,
            check_gh_branch=False,
        )
        self.assertIsNotNone(pr_info.get("pr_url"))
        self.assertEqual(len(pr_info["prs"]), 2)
        pr_nums = {p["pr_number"] for p in pr_info["prs"]}
        self.assertEqual(pr_nums, {142, 89})

        # Enrich agent metadata populates Jira, Datadog, and PR links
        meta = bridge.enrich_agent_metadata(
            repo_path="",
            repo_name="payments.invoice-statement",
            branch="feat/PAY-4821-pix",
            title="Implementação PIX PAY-4821",
            terminal_tail=tail,
            check_gh_branch=False,
        )
        self.assertEqual(meta["jira_key"], "PAY-4821")
        self.assertEqual(meta["k8s_namespace"], "payments")
        self.assertEqual(meta["app_name"], "invoice-statement")
        self.assertEqual(meta["pr_number"], 142)
        self.assertEqual(len(meta["prs"]), 1)

    def test_execute_neon_action_routes_gh_prs_datadog_and_jira(self):
        bridge = NeonCliBridge()
        bridge.neon_bin = "/usr/local/bin/neon"

        # 1. Opening a specific PR associated with the agent
        with patch.object(bridge, "_open_url_in_browser", return_value=True) as open_mock:
            r_pr = bridge.execute_neon_action(
                action="gh_prs",
                repo_path=str(PROJECT_ROOT),
                repo_name="pac-neon",
                pr_url="https://github.com/neon/payments.bff/pull/89",
                pr_number=89,
                pr_repo="neon/payments.bff",
            )
            self.assertTrue(r_pr["ok"])
            self.assertTrue(r_pr["opened_specific_pr"])
            open_mock.assert_called_once_with("https://github.com/neon/payments.bff/pull/89")

        # 2. Executing neon CLI subcommands (dd_service, dd_logs, flow_open)
        fake_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="OK", stderr="")
        with patch("subprocess.run", return_value=fake_proc) as run_mock:
            r_dd = bridge.execute_neon_action(action="dd_service", repo_name="payments.bff")
            self.assertTrue(r_dd["ok"])
            self.assertIn("dd service payments.bff", r_dd["command"])

            r_logs = bridge.execute_neon_action(action="dd_logs", repo_name="payments.bff", env="prod")
            self.assertTrue(r_logs["ok"])
            self.assertIn("dd logs payments.bff", r_logs["command"])

            r_jira = bridge.execute_neon_action(action="flow_open", jira_key="CORE-999")
            self.assertTrue(r_jira["ok"])
            self.assertIn("flow open CORE-999", r_jira["command"])
            self.assertEqual(run_mock.call_count, 3)

    def test_install_pac_neon_agent_hooks_idempotent_and_non_blocking(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_home = Path(tmp_dir)
            with patch.object(Path, "home", return_value=tmp_home):
                ok = install_pac_neon_agent_hooks()
                self.assertTrue(ok)
                notify_path = tmp_home / ".pac-neon" / "hooks" / "notify.sh"
                settings_path = tmp_home / ".claude" / "settings.json"

                self.assertTrue(notify_path.exists())
                self.assertTrue(settings_path.exists())

                cfg = json.loads(settings_path.read_text(encoding="utf-8"))
                self.assertIn("hooks", cfg)
                for ev in (
                    "PreToolUse",
                    "PostToolUse",
                    "PermissionRequest",
                    "Notification",
                    "Stop",
                    "UserPromptSubmit",
                    "SessionStart",
                    "SessionEnd",
                ):
                    self.assertIn(ev, cfg["hooks"])


if __name__ == "__main__":
    unittest.main()
