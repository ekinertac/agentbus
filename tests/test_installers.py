"""
Installer behaviour against a throwaway HOME: idempotency, backups, round-tripping, and not
clobbering config the user already had. Nothing here touches the real config directories.

Idempotency and "leave what you did not write alone" are the two properties that decide whether
this is safe to run on someone's machine, so both are checked for every client.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentbus import installers


class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="agentbus-home-"))

    def clients(self):
        # Claude installs nothing by design, so it is not part of the write/undo contract.
        return [c for c in installers.all_clients(self.home) if c.name != "claude"]


class TestInstall(InstallerCase):
    def test_install_then_uninstall_leaves_no_trace(self):
        for client in self.clients():
            with self.subTest(client.name):
                first = client.install()
                self.assertTrue(first.changed)
                self.assertTrue(client.installed())
                removed = client.uninstall()
                self.assertTrue(removed.changed)
                self.assertFalse(client.installed())

    def test_installing_twice_changes_nothing_the_second_time(self):
        for client in self.clients():
            with self.subTest(client.name):
                client.install()
                second = client.install()
                self.assertFalse(second.changed)
                self.assertIn("already configured", second.detail)

    def test_dry_run_writes_nothing(self):
        for client in self.clients():
            with self.subTest(client.name):
                result = client.install(dry_run=True)
                self.assertTrue(result.changed)
                self.assertFalse(client.installed())

    def test_uninstalling_something_never_installed_is_not_an_error(self):
        for client in self.clients():
            with self.subTest(client.name):
                result = client.uninstall()
                self.assertFalse(result.changed)


class TestExistingConfig(InstallerCase):
    def test_json_hosts_keep_the_users_other_servers_and_settings(self):
        kiro = installers.by_name("kiro", self.home)
        kiro.config_path.parent.mkdir(parents=True, exist_ok=True)
        kiro.config_path.write_text(json.dumps({
            "mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}},
            "someOtherSetting": True,
        }))
        kiro.install()
        after = json.loads(kiro.config_path.read_text())
        self.assertEqual(after["mcpServers"]["fetch"]["command"], "uvx")
        self.assertTrue(after["someOtherSetting"])
        self.assertIn("agentbus", after["mcpServers"])
        kiro.uninstall()
        after = json.loads(kiro.config_path.read_text())
        self.assertEqual(list(after["mcpServers"]), ["fetch"])

    def test_the_original_file_is_backed_up_before_any_change(self):
        gemini = installers.by_name("gemini", self.home)
        gemini.config_path.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps({"mcpServers": {}, "theme": "dark"})
        gemini.config_path.write_text(original)
        result = gemini.install()
        self.assertIsNotNone(result.backup)
        self.assertEqual(Path(result.backup).read_text(), original)

    def test_codex_keeps_the_rest_of_the_toml(self):
        codex = installers.by_name("codex", self.home)
        codex.config_path.parent.mkdir(parents=True, exist_ok=True)
        codex.config_path.write_text('model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "x"\n')
        codex.install()
        text = codex.config_path.read_text()
        self.assertIn('model = "gpt-5"', text)
        self.assertIn("[mcp_servers.other]", text)
        self.assertIn("[mcp_servers.agentbus]", text)
        codex.uninstall()
        text = codex.config_path.read_text()
        self.assertIn('model = "gpt-5"', text)
        self.assertIn("[mcp_servers.other]", text)
        self.assertNotIn("agentbus", text)

    def test_codex_replaces_its_own_block_rather_than_stacking_them(self):
        codex = installers.by_name("codex", self.home)
        codex.install()
        # Simulate a stale block from an older version by changing the recorded command.
        stale = codex.config_path.read_text().replace(installers.server_command()["command"], "/old/python")
        codex.config_path.write_text(stale)
        codex.install()
        self.assertEqual(codex.config_path.read_text().count("[mcp_servers.agentbus]"), 1)
        self.assertNotIn("/old/python", codex.config_path.read_text())


class TestKiroAgent(InstallerCase):
    def test_writes_an_agent_that_tells_the_model_to_pull_messages(self):
        kiro = installers.by_name("kiro", self.home)
        kiro.install()
        agent = json.loads(kiro.agent_path.read_text())
        self.assertIn("check_messages", agent["prompt"])
        # Kiro's TUI runs no hooks, but the engines that do should still drain automatically.
        self.assertIn("userPromptSubmit", agent["hooks"])
        self.assertIn("@agentbus", agent["tools"])

    def test_install_is_not_complete_until_both_files_exist(self):
        kiro = installers.by_name("kiro", self.home)
        kiro.install()
        kiro.agent_path.unlink()
        self.assertFalse(kiro.installed())
        self.assertTrue(kiro.install().changed)


if __name__ == "__main__":
    unittest.main()
