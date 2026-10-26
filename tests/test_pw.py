"""Integration tests for pw, using the failure scenarios from EVALUATION.md."""
import os
import socket
import subprocess
import sys
import tempfile
import unittest

from agentbelt import pw

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, PW_HOME=self.home.name)

    def tearDown(self):
        self.pw("stop", "--all")
        self.home.cleanup()

    def pw(self, *args):
        return subprocess.run([sys.executable, pw.__file__, *args], capture_output=True, text=True,
                              env=self.env, timeout=60)

    def server(self, port, delay=1):
        return [sys.executable, os.path.join(FIXTURES, "slow_server.py"), str(port), str(delay)]

    def test_waits_for_slow_startup_and_detects_port(self):
        port = free_port()
        r = self.pw("start", "web", "--", *self.server(port, delay=1.5))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("READY", r.stdout)
        self.assertIn(f"http://localhost:{port} listening", r.stdout)

    def test_reports_crash_with_its_error(self):
        r = self.pw("start", "api", "--", sys.executable, os.path.join(FIXTURES, "crash.py"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("exited with code 1", r.stdout)
        self.assertIn("Missing required env var DATABASE_URL", r.stdout)

    def test_refuses_a_port_that_is_already_taken(self):
        port = free_port()
        self.assertEqual(self.pw("start", "old", "--", *self.server(port, delay=0)).returncode, 0)
        r = self.pw("start", "new", "--port", str(port), "--", *self.server(port))
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"port {port} is already in use", r.stdout)

    def test_stop_kills_children_of_a_wrapper(self):
        port = free_port()
        self.assertEqual(self.pw("start", "w", "--port", str(port), "--", os.path.join(FIXTURES, "wrapper.sh"),
                                 str(port)).returncode, 0)
        r = self.pw("stop", "w")
        self.assertIn(f"port {port} free", r.stdout)
        self.assertFalse(pw.listening(port))

    def test_timeout_leaves_process_running(self):
        port = free_port()
        r = self.pw("start", "slow", "--timeout", "1", "--", *self.server(port, delay=4))
        self.assertEqual(r.returncode, 2)
        self.assertIn("TIMEOUT", r.stdout)
        self.assertIn("slow: running", self.pw("status", "slow").stdout)

    def test_logs_new_returns_only_unseen_lines(self):
        port = free_port()
        self.pw("start", "web", "--", *self.server(port, delay=0))
        self.pw("logs", "web", "--new")
        with socket.create_connection(("127.0.0.1", port)) as s:
            s.sendall(b"GET /boom HTTP/1.0\r\n\r\n")
            s.recv(100)
        out = self.pw("logs", "web", "--new").stdout
        self.assertIn("Cannot read properties of undefined", out)
        self.assertNotIn("compiling", out)


if __name__ == "__main__":
    unittest.main()
