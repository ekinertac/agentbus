"""
CLI behaviour that scripts and agents depend on: exit codes, the stdout/stderr split, --json
shapes, and stdin input. These are the parts that cannot change without breaking callers, so they
are pinned here rather than left to manual checking.

Most cases call main() in-process for speed; one runs the module as a subprocess to prove the
entry point and the stream split are real and not an artefact of the test harness.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agentbus import cli
from agentbus import protocol as p

REPO = Path(__file__).resolve().parent.parent


def run(argv, stdin: str = ""):
    """Returns (exit_code, stdout, stderr) for an in-process invocation."""
    out, err = io.StringIO(), io.StringIO()
    saved_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    finally:
        sys.stdin = saved_stdin
    return code, out.getvalue(), err.getvalue()


class TestUsage(unittest.TestCase):
    def test_no_arguments_prints_help_rather_than_an_error(self):
        code, out, _ = run([])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("agentbus list", out)

    def test_unknown_command_exits_usage(self):
        with self.assertRaises(SystemExit) as cm:
            run(["nope"])
        self.assertEqual(cm.exception.code, cli.EXIT_USAGE)


class TestList(unittest.TestCase):
    def test_json_is_a_parseable_array_of_records(self):
        code, out, _ = run(["list", "--json"])
        self.assertEqual(code, cli.EXIT_OK)
        rows = json.loads(out)
        self.assertIsInstance(rows, list)
        for row in rows:
            self.assertEqual(
                sorted(row), ["agent", "cwd", "name", "pid", "registryName", "socket", "status"]
            )

    def test_human_output_goes_to_stdout_and_nothing_else_does(self):
        code, out, err = run(["list"])
        self.assertEqual(code, cli.EXIT_OK)
        if out.strip():
            self.assertEqual(err, "")


class TestSend(unittest.TestCase):
    def test_unknown_target_exits_three_and_explains_on_stderr(self):
        code, out, err = run(["send", "definitely-not-a-session", "hi"])
        self.assertEqual(code, cli.EXIT_NO_TARGET)
        self.assertEqual(out, "")
        self.assertIn("No live session named", err)

    def test_empty_message_is_refused(self):
        code, _, err = run(["send", "whoever", "   "])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("empty", err)

    def test_unreachable_socket_exits_one(self):
        code, _, err = run(["send", "/tmp/cc-socks/does-not-exist.sock", "hi", "--timeout", "1"])
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertIn("Could not reach", err)


class TestSendEndToEnd(unittest.TestCase):
    """Against a real listener, including the dash-means-stdin contract."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="agentbus-cli-"))
        self.sock = str(self.dir / "t.sock")
        self.token = "ef" * 16
        self.key = p.key_path(os.getpid(), self.sock)
        self.key.parent.mkdir(parents=True, exist_ok=True)
        self.key.write_text(json.dumps({"peerToken": self.token}))
        self.got: list = []
        self.server = p.listen(self.sock, self.token, self.got.append)

    def tearDown(self):
        self.server.close()
        self.key.unlink(missing_ok=True)

    def _wait(self, n=1, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline and len(self.got) < n:
            time.sleep(0.02)

    def test_message_argument_is_delivered_and_json_reports_the_id(self):
        code, out, _ = run(["send", self.sock, "from the cli", "--json", "--from-name", "tester"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(out)["to"], self.sock)
        self._wait()
        self.assertEqual(self.got[0]["body"], "from the cli")
        self.assertEqual(self.got[0]["fromName"], "tester")

    def test_dash_reads_the_body_from_stdin(self):
        code, _, _ = run(["send", self.sock, "-", "-q"], stdin="piped body\nsecond line\n")
        self.assertEqual(code, cli.EXIT_OK)
        self._wait()
        self.assertEqual(self.got[0]["body"], "piped body\nsecond line\n")

    def test_quiet_prints_nothing_on_success(self):
        code, out, _ = run(["send", self.sock, "hush", "-q"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(out, "")


class TestDoctor(unittest.TestCase):
    def test_json_reports_every_check_with_a_verdict(self):
        code, out, _ = run(["doctor", "--json"])
        report = json.loads(out)
        self.assertIn("checks", report)
        self.assertEqual(code, cli.EXIT_OK if report["ok"] else cli.EXIT_NOT_WIRED)
        for check in report["checks"]:
            self.assertEqual(sorted(check), ["check", "detail", "ok"])


class TestModuleEntryPoint(unittest.TestCase):
    def test_python_m_agentbus_works_and_keeps_errors_off_stdout(self):
        proc = subprocess.run(
            [sys.executable, "-m", "agentbus", "send", "definitely-not-a-session", "hi"],
            cwd=REPO, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, cli.EXIT_NO_TARGET)
        self.assertEqual(proc.stdout, "")
        self.assertIn("No live session named", proc.stderr)


if __name__ == "__main__":
    unittest.main()
