"""
The MCP adapter as a host drives it: a real subprocess, newline-delimited JSON-RPC on its stdio,
and the bus side-effects (registry entry, socket, spool) that make it a peer.

These are end-to-end on purpose. The handshake is the part a host silently gives up on, and an
in-process test of handle() would not catch a stdout that carries anything but RPC.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from agentbus import mcp
from agentbus import protocol as p
from agentbus import spool

REPO = Path(__file__).resolve().parent.parent


class Host:
    """Minimal stand-in for an MCP host."""

    def __init__(self, env_extra=None, cwd="/tmp"):
        env = dict(os.environ)
        # The host is started in another directory on purpose (that is what names the session),
        # so the package has to be found by path rather than by cwd.
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        env.update(env_extra or {})
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "agentbus", "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=env, text=True, bufsize=1,
        )
        self._id = 0

    def call(self, method, params=None, timeout=15.0):
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError("server closed stdout unexpectedly")
        return json.loads(line)

    def notify(self, method):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def initialize(self, client="kiro-cli"):
        return self.call("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": client, "version": "1"},
        })

    def close(self):
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def wait_for_registry(self, timeout=10.0):
        path = p.registry_path(self.proc.pid)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists():
                return json.loads(path.read_text())
            time.sleep(0.05)
        raise AssertionError("server never registered on the bus")


class TestHandshake(unittest.TestCase):
    def setUp(self):
        self.host = Host()
        self.addCleanup(self.host.close)

    def test_initialize_tools_list_and_a_call(self):
        init = self.host.initialize()
        self.assertEqual(init["result"]["serverInfo"]["name"], "agentbus")
        self.assertIn("tools", init["result"]["capabilities"])
        # The version the host asked for is echoed, so an older host is not told to speak a newer one.
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")

        self.host.notify("notifications/initialized")  # must not produce a response

        listed = self.host.call("tools/list")
        self.assertEqual(
            sorted(t["name"] for t in listed["result"]["tools"]),
            ["check_messages", "list_claude_sessions", "send_to_claude", "set_session_name"],
        )

        empty = self.host.call("tools/call", {"name": "check_messages", "arguments": {}})
        self.assertEqual(empty["result"]["content"][0]["text"], "(no new messages)")

    def test_unknown_method_is_a_jsonrpc_error_not_a_crash(self):
        self.host.initialize()
        bad = self.host.call("nope/nope")
        self.assertEqual(bad["error"]["code"], -32601)
        self.assertEqual(self.host.call("ping")["result"], {})

    def test_send_to_an_unknown_target_reports_an_error_result(self):
        self.host.initialize()
        res = self.host.call("tools/call", {
            "name": "send_to_claude",
            "arguments": {"to": "definitely-not-a-session", "message": "x"},
        })
        self.assertTrue(res["result"]["isError"])
        self.assertIn("No live session named", res["result"]["content"][0]["text"])


class TestReapStaleEntries(unittest.TestCase):
    """
    Some hosts (crush, confirmed) kill the MCP server with a signal our own handlers never see,
    which only SIGKILL explains: it leaves the registry entry and key behind with no way for this
    process to react. The next server that starts sweeps for exactly that, scoped to entries this
    code itself would have written.
    """

    def _write_dead_entry(self, pid: int, entrypoint: str = "agentbus") -> None:
        # Built by hand rather than via new_registry(): that shells out to `ps -p <pid>` for
        # procStart, which fails outright for a pid that was never real.
        sock = str(p.sock_path(pid))
        entry = {
            "pid": pid, "sessionId": f"agentbus-{pid}", "cwd": "/tmp", "startedAt": 0,
            "procStart": "", "version": entrypoint, "peerProtocol": p.PEER_PROTOCOL,
            "peerFeatures": [], "kind": "interactive", "entrypoint": entrypoint,
            "pidDomain": "darwin", "messagingSocketPath": sock, "name": "ab-stale",
            "nameSource": "derived", "nameSince": 0, "status": "idle", "updatedAt": 0,
            "statusUpdatedAt": 0,
        }
        p.write_registry(entry)
        # write_key() also shells out to `ps -p <pid>` for procStart; write the file directly.
        p.key_path(pid, sock).write_text(json.dumps({"peerToken": "ab" * 16}))

    def test_reaps_a_dead_agentbus_entry(self):
        dead_pid = 2**30  # never a real pid
        self._write_dead_entry(dead_pid)
        self.assertTrue(p.registry_path(dead_pid).exists())
        mcp._reap_stale_agentbus_entries()
        self.assertFalse(p.registry_path(dead_pid).exists())

    def test_leaves_a_live_entry_alone(self):
        # os.getpid() (this test process) is alive by definition.
        self._write_dead_entry(os.getpid())
        try:
            mcp._reap_stale_agentbus_entries()
            self.assertTrue(p.registry_path(os.getpid()).exists())
        finally:
            p.remove_files(os.getpid(), str(p.sock_path(os.getpid())))

    def test_leaves_a_dead_entry_from_a_different_entrypoint_alone(self):
        # A Claude Code session's own stale file is never ours to clean up.
        dead_pid = 2**30 - 1
        self._write_dead_entry(dead_pid, entrypoint="cli")
        try:
            mcp._reap_stale_agentbus_entries()
            self.assertTrue(p.registry_path(dead_pid).exists())
        finally:
            p.remove_files(dead_pid, str(p.sock_path(dead_pid)))


class TestBusMembership(unittest.TestCase):
    def test_registers_with_the_hosts_prefix_and_cleans_up_on_exit(self):
        host = Host()
        try:
            host.initialize("kiro-cli")
            entry = host.wait_for_registry()
            self.assertEqual(entry["entrypoint"], "agentbus")
            self.assertTrue(entry["name"].startswith("ki-"), entry["name"])
            self.assertTrue(Path(entry["messagingSocketPath"]).exists())
        finally:
            host.close()
        deadline = time.time() + 10
        while time.time() < deadline and p.registry_path(host.proc.pid).exists():
            time.sleep(0.05)
        self.assertFalse(p.registry_path(host.proc.pid).exists())

    def test_kiro_is_recognised_by_the_name_it_actually_reports(self):
        # Kiro sends "Q DEV CLI" in clientInfo, not "kiro-cli"; matching on the obvious name alone
        # silently gave every kiro session the generic prefix.
        host = Host()
        self.addCleanup(host.close)
        host.initialize("Q DEV CLI")
        self.assertTrue(host.wait_for_registry()["name"].startswith("ki-"))

    def test_codex_is_recognised_by_the_name_it_actually_reports(self):
        # codex-cli 0.155.1 sends "codex-mcp-client" in clientInfo, not "codex" or "codex-cli".
        host = Host()
        self.addCleanup(host.close)
        host.initialize("codex-mcp-client")
        self.assertTrue(host.wait_for_registry()["name"].startswith("cx-"))

    def test_antigravity_is_recognised_by_the_name_it_actually_reports(self):
        # antigravity-cli 1.2.11 sends "antigravity-client" in clientInfo.
        host = Host()
        self.addCleanup(host.close)
        host.initialize("antigravity-client")
        self.assertTrue(host.wait_for_registry()["name"].startswith("ag-"))

    def test_crush_is_recognised_by_the_name_it_actually_reports(self):
        # crush 0.96.1 sends "crush", verified against a live tool call, for once matching the
        # binary name.
        host = Host()
        self.addCleanup(host.close)
        host.initialize("crush")
        self.assertTrue(host.wait_for_registry()["name"].startswith("cr-"))

    def test_cleans_up_when_killed_rather_than_closed(self):
        # codex ends an MCP subprocess with a signal rather than closing stdin (confirmed against
        # 0.155.1: a live session's registry entry and key were still there after the process was
        # gone), which skips the `finally` around the stdin loop entirely.
        import signal

        host = Host()
        host.initialize("codex-mcp-client")
        entry = host.wait_for_registry()
        os.kill(host.proc.pid, signal.SIGTERM)
        host.proc.wait(timeout=10)
        deadline = time.time() + 5
        while time.time() < deadline and p.registry_path(host.proc.pid).exists():
            time.sleep(0.05)
        self.assertFalse(p.registry_path(host.proc.pid).exists())
        self.assertFalse(p.key_path(host.proc.pid, entry["messagingSocketPath"]).exists())
        self.assertFalse(Path(entry["messagingSocketPath"]).exists())

    def test_an_unknown_host_still_joins_under_the_generic_prefix(self):
        host = Host()
        self.addCleanup(host.close)
        host.initialize("some-editor-nobody-mapped")
        self.assertTrue(host.wait_for_registry()["name"].startswith("ab-"))

    def test_hermes_reports_mcp_and_is_deliberately_left_unmapped(self):
        # Hermes Agent 0.21.0 sends "mcp" in clientInfo, which is too generic to map without
        # risking misattributing some other, unrelated host that also calls itself "mcp". The
        # generic prefix is the correct outcome here, not a gap: this pins that decision so a
        # future edit adding "mcp" -> "hm-" to HOST_PREFIXES fails loudly instead of quietly.
        from agentbus.mcp import HOST_PREFIXES

        self.assertNotIn("mcp", HOST_PREFIXES)
        host = Host()
        self.addCleanup(host.close)
        host.initialize("mcp")
        self.assertTrue(host.wait_for_registry()["name"].startswith("ab-"))

    def test_agentbus_name_overrides_the_directory(self):
        host = Host(env_extra={"AGENTBUS_NAME": "chat v1!"})
        self.addCleanup(host.close)
        host.initialize("kiro-cli")
        self.assertEqual(host.wait_for_registry()["name"], "ki-chat-v1")

    def test_an_inbound_message_is_spooled_and_check_messages_clears_it(self):
        host = Host()
        self.addCleanup(host.close)
        host.initialize("kiro-cli")
        entry = host.wait_for_registry()

        sender_sock = str(p.sock_path(os.getpid()))
        p.send_user(entry["messagingSocketPath"], sender_sock, "cc-tester", "hello from the bus")

        deadline = time.time() + 5
        while time.time() < deadline and not spool.spool_path(host.proc.pid).exists():
            time.sleep(0.05)

        res = host.call("tools/call", {"name": "check_messages", "arguments": {}})
        text = res["result"]["content"][0]["text"]
        self.assertIn("hello from the bus", text)
        # The sender already carries an address prefix, so the reply address is not re-prefixed.
        self.assertIn('to: "cc-tester"', text)

        again = host.call("tools/call", {"name": "check_messages", "arguments": {}})
        self.assertEqual(again["result"]["content"][0]["text"], "(no new messages)")

    def test_rename_keeps_the_hosts_prefix(self):
        host = Host()
        self.addCleanup(host.close)
        host.initialize("kiro-cli")
        host.wait_for_registry()
        res = host.call("tools/call", {"name": "set_session_name", "arguments": {"name": "review"}})
        self.assertIn("ki-review", res["result"]["content"][0]["text"])
        self.assertEqual(json.loads(p.registry_path(host.proc.pid).read_text())["name"], "ki-review")


if __name__ == "__main__":
    unittest.main()
