"""
Installer behaviour against a throwaway HOME: idempotency, backups, round-tripping, and not
clobbering config the user already had. Nothing here touches the real config directories.

Idempotency and "leave what you did not write alone" are the two properties that decide whether
this is safe to run on someone's machine, so both are checked for every client.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agentbus import installers


class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="agentbus-home-"))

    def clients(self):
        # Claude installs nothing by design; pi has its own case below because it is the only
        # client whose targets depend on directories existing first.
        return [c for c in installers.all_clients(self.home) if c.name not in ("claude", "pi")]


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


class TestPi(InstallerCase):
    """pi is the one client installed by symlink, and the one with more than one target directory."""

    def setUp(self):
        super().setUp()
        # A wrapper's variable in the developer's own shell must not leak into these.
        self._saved = os.environ.pop("PI_CODING_AGENT_DIR", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["PI_CODING_AGENT_DIR"] = self._saved

    def test_links_into_the_standard_agent_directory(self):
        (self.home / ".pi/agent/extensions").mkdir(parents=True)
        pi = installers.by_name("pi", self.home)
        pi.install()
        link = self.home / ".pi/agent/extensions/agentbus"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), pi.adapter_source)
        self.assertTrue((link / "index.ts").exists())

    def test_a_wrapper_directory_keeps_extensions_at_its_top_level(self):
        wrapper = self.home / "wrapper"
        (wrapper / "extensions").mkdir(parents=True)
        (self.home / ".pi/agent/extensions").mkdir(parents=True)
        pi = installers.by_name("pi", self.home, extra_dirs=[wrapper])
        pi.install()
        self.assertTrue((wrapper / "extensions/agentbus").is_symlink())
        self.assertTrue((self.home / ".pi/agent/extensions/agentbus").is_symlink())

    def test_pi_coding_agent_dir_from_the_environment_is_included(self):
        wrapper = self.home / "envwrapper"
        (wrapper / "extensions").mkdir(parents=True)
        os.environ["PI_CODING_AGENT_DIR"] = str(wrapper)
        try:
            installers.by_name("pi", self.home).install()
        finally:
            del os.environ["PI_CODING_AGENT_DIR"]
        self.assertTrue((wrapper / "extensions/agentbus").is_symlink())

    def test_installing_twice_changes_nothing_and_uninstall_removes_every_link(self):
        wrapper = self.home / "wrapper"
        (wrapper / "extensions").mkdir(parents=True)
        pi = installers.by_name("pi", self.home, extra_dirs=[wrapper])
        pi.install()
        self.assertFalse(pi.install().changed)
        self.assertTrue(pi.installed())
        pi.uninstall()
        self.assertFalse(pi.installed())
        self.assertFalse((wrapper / "extensions/agentbus").exists())

    def test_a_stale_link_is_replaced_rather_than_left_pointing_elsewhere(self):
        ext = self.home / ".pi/agent/extensions"
        ext.mkdir(parents=True)
        (ext / "agentbus").symlink_to(self.home)  # e.g. a link from an older checkout
        pi = installers.by_name("pi", self.home)
        self.assertFalse(pi.installed())
        pi.install()
        self.assertEqual((ext / "agentbus").resolve(), pi.adapter_source)


class TestOpencode(InstallerCase):
    def test_writes_a_shim_that_imports_the_real_adapter_and_registers_it(self):
        oc = installers.by_name("opencode", self.home)
        oc.install()
        shim = oc.shim_path.read_text()
        self.assertIn("@opencode-ai/plugin", shim)
        self.assertIn(str(oc.adapter_source), shim)
        config = json.loads(oc.config_path.read_text())
        self.assertIn(oc._plugin_url(), config["plugin"])

    def test_keeps_the_users_other_plugins_and_settings(self):
        oc = installers.by_name("opencode", self.home)
        oc.config_path.parent.mkdir(parents=True, exist_ok=True)
        oc.config_path.write_text(json.dumps({
            "plugin": ["some-other-plugin"],
            "theme": "dark",
        }))
        oc.install()
        config = json.loads(oc.config_path.read_text())
        self.assertIn("some-other-plugin", config["plugin"])
        self.assertIn(oc._plugin_url(), config["plugin"])
        self.assertEqual(config["theme"], "dark")
        oc.uninstall()
        config = json.loads(oc.config_path.read_text())
        self.assertEqual(config["plugin"], ["some-other-plugin"])

    def test_a_missing_shim_makes_install_incomplete_even_with_the_entry_present(self):
        oc = installers.by_name("opencode", self.home)
        oc.install()
        oc.shim_path.unlink()
        self.assertFalse(oc.installed())
        self.assertTrue(oc.install().changed)


class TestAntigravity(InstallerCase):
    def test_writes_its_own_file_under_gemini_matching_what_agy_mcp_add_produces(self):
        # Confirmed against a real `agy mcp add` on a throwaway HOME: despite the binary name, its
        # config lands at .gemini/config/mcp_config.json, with "disabled": false on each server.
        ag = installers.by_name("antigravity", self.home)
        ag.install()
        self.assertEqual(ag.config_path, self.home / ".gemini/config/mcp_config.json")
        config = json.loads(ag.config_path.read_text())
        self.assertFalse(config["mcpServers"]["agentbus"]["disabled"])

    def test_does_not_collide_with_gemini_cli_sharing_the_gemini_directory(self):
        # Gemini CLI's own config is .gemini/settings.json, a different file under the same parent
        # directory; installing one must not create or touch the other's file.
        gemini = installers.by_name("gemini", self.home)
        antigravity = installers.by_name("antigravity", self.home)
        antigravity.install()
        self.assertFalse(gemini.config_path.exists())
        gemini.install()
        self.assertTrue(gemini.config_path.exists())
        self.assertNotEqual(gemini.config_path, antigravity.config_path)
        # Each still reports itself correctly, unaffected by the other having run first.
        self.assertTrue(gemini.installed())
        self.assertTrue(antigravity.installed())
