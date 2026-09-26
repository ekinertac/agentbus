"""
Checks the Python protocol implementation against the shared vectors plus the behaviour that only
a live socket can show: an auth handshake, a real message crossing it, and cleanup.

Run: python3 -m unittest discover -s tests -t .   (stdlib only, works on the system python)

The vector cases are the contract the TypeScript adapters are held to as well, so a change here
that is not mirrored there fails the other suite rather than drifting silently.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agentbus import protocol as p

VECTORS = json.loads((Path(__file__).parent / "vectors.json").read_text())


class TestVectors(unittest.TestCase):
    def test_build_envelope(self):
        for case in VECTORS["build_envelope"]:
            with self.subTest(case.get("why")):
                self.assertEqual(
                    p.build_envelope(case["from"], case["name"], case["body"], case["mode"]),
                    case["wire"],
                )

    def test_parse_envelope(self):
        for case in VECTORS["parse_envelope"]:
            with self.subTest(case.get("why")):
                got = p.parse_envelope(case["content"])
                for key, want in case["expect"].items():
                    self.assertEqual(got.get(key), want)

    def test_derive_name(self):
        for case in VECTORS["derive_name"]:
            with self.subTest(case.get("why")):
                self.assertEqual(
                    p.derive_name(case["cwd"], case["sessionName"], case["taken"], case["prefix"]),
                    case["expect"],
                )

    def test_display_name(self):
        for case in VECTORS["display_name"]:
            with self.subTest(case.get("why")):
                self.assertEqual(p.display_name(case["entry"]), case["expect"])

    def test_pid_of_socket(self):
        for case in VECTORS["pid_of_socket"]:
            with self.subTest(case["sock"]):
                self.assertEqual(p.pid_of_socket(case["sock"]), case["expect"])


class TestReplyAddress(unittest.TestCase):
    def test_vectors(self):
        from agentbus import spool
        for case in VECTORS["reply_address"]:
            with self.subTest(case.get("why")):
                self.assertEqual(spool.reply_address(case["fromName"], case["from"]), case["expect"])


class TestProcStart(unittest.TestCase):
    def test_matches_the_format_claude_compares(self):
        # Claude string-compares this against its own reading, so the format is part of the protocol.
        self.assertRegex(
            p.proc_start(os.getpid()),
            r"^[A-Z][a-z]{2} [A-Z][a-z]{2} {1,2}\d{1,2} \d{2}:\d{2}:\d{2} \d{4}$",
        )


class TestPermissionMode(unittest.TestCase):
    def test_transcript_wins_and_follows_a_runtime_switch(self):
        sid = "00000000-1111-2222-3333-444444444444"
        project = p.PROJECTS_DIR / "agentbus-test-project"
        project.mkdir(parents=True, exist_ok=True)
        transcript = project / f"{sid}.jsonl"
        try:
            for case in VECTORS["permission_mode_map"]:
                with self.subTest(case["transcript"]):
                    # Compact, the way Claude writes it; the spaced form is covered by vectors.
                    transcript.write_text('{"permissionMode":"%s"}\n' % case["transcript"])
                    self.assertEqual(p.peer_mode(os.getpid(), sid), case["expect"])
            # Last entry wins: a session that switched into plan mode and back is bypass again.
            transcript.write_text(
                '{"permissionMode":"bypassPermissions"}\n{"permissionMode":"plan"}\n'
            )
            self.assertEqual(p.peer_mode(os.getpid(), sid), "prompting")
        finally:
            transcript.unlink(missing_ok=True)
            project.rmdir()

    def test_mode_line_matching(self):
        for case in VECTORS["permission_mode_line"]:
            with self.subTest(case.get("why")):
                hits = p.MODE_RE.findall(case["line"])
                self.assertEqual(hits[-1] if hits else None, case["expect"])

    def test_falls_back_to_the_command_line(self):
        # This test process is plain python: no uuid session, no --dangerously-skip-permissions.
        self.assertEqual(p.peer_mode(os.getpid(), "not-a-uuid"), "prompting")

    def test_unreadable_pid_assumes_bypass_rather_than_guaranteeing_a_hold(self):
        self.assertEqual(p.peer_mode(2 ** 30, "not-a-uuid"), "bypass")


class TestSocket(unittest.TestCase):
    """A real listener and a real sender, with the key file where read_token() looks for it."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="agentbus-"))
        self.sock = str(self.dir / "t.sock")
        self.token = "ab" * 16
        self.key = p.key_path(os.getpid(), self.sock)
        self.key.parent.mkdir(parents=True, exist_ok=True)
        self.key.write_text(json.dumps({"peerToken": self.token}))
        self.got: list = []
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.close()
        self.key.unlink(missing_ok=True)

    def _wait(self, n=1, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline and len(self.got) < n:
            time.sleep(0.02)

    def test_auth_then_user_frame_is_delivered_with_the_envelope_parsed(self):
        self.server = p.listen(self.sock, self.token, self.got.append)
        msg_id = p.send_user(self.sock, p.sock_path(999999), "ab-test", "ping")
        self._wait()
        self.assertEqual(len(self.got), 1)
        self.assertEqual(self.got[0]["body"], "ping")
        self.assertEqual(self.got[0]["fromName"], "ab-test")
        self.assertEqual(self.got[0]["from"], f"uds:{p.sock_path(999999)}")
        self.assertEqual(self.got[0]["msgId"], msg_id)

    def test_a_wrong_token_delivers_nothing(self):
        self.server = p.listen(self.sock, "cd" * 16, self.got.append)
        try:
            p.send_user(self.sock, p.sock_path(999999), "ab-test", "ping")
        except OSError:
            pass  # the listener hangs up on bad auth; either way nothing must arrive
        self._wait(timeout=0.5)
        self.assertEqual(self.got, [])

    def test_the_socket_is_private_to_this_user(self):
        # Claude refuses a sockets directory that is not 0700, and we must not widen it.
        self.server = p.listen(self.sock, self.token, self.got.append)
        self.assertEqual(os.stat(self.sock).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(p.SOCK_DIR).st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
