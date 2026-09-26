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

    def test_an_unknown_host_still_joins_under_the_generic_prefix(self):
        host = Host()
        self.addCleanup(host.close)
        host.initialize("some-editor-nobody-mapped")
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
