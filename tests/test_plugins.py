"""The Hermes and OpenClaw plugins must write runs that `receipt check` reads, and only for cron runs."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime, timezone

from agentbelt import receipt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERMES = os.path.join(ROOT, "plugins", "hermes", "receipts")
OPENCLAW = os.path.join(ROOT, "plugins", "openclaw", "receipts")
CronExecution = namedtuple("CronExecution", "job_id job_name execution_id source scheduled_instant started_at profile")


def load_hermes_plugin():
    spec = importlib.util.spec_from_file_location("hermes_receipts", os.path.join(HERMES, "__init__.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeContext:
    """The two PluginContext methods the plugin uses."""

    def __init__(self, run=None):
        self.run = run
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def current_cron_execution(self):
        return self.run


class HermesPlugin(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("RECEIPT_HOME")
        os.environ["RECEIPT_HOME"] = self.home.name
        self.old_receipt_home, receipt.HOME = receipt.HOME, self.home.name
        self.plugin = load_hermes_plugin()

    def tearDown(self):
        receipt.HOME = self.old_receipt_home
        if self.old_home is None:
            os.environ.pop("RECEIPT_HOME", None)
        else:
            os.environ["RECEIPT_HOME"] = self.old_home
        self.home.cleanup()

    def end_turn(self, run, **payload):
        ctx = FakeContext(run)
        self.plugin.register(ctx)
        # the canonical on_session_end payload from agent/turn_finalizer.py
        fields = dict(session_id="cron_abc123_20261027_090000", task_id="t", turn_id="u", completed=True,
                      failed=False, interrupted=False, turn_exit_reason=None, model="m", platform="cron",
                      telemetry_schema_version="hermes.observer.v1")
        fields.update(payload)
        ctx.hooks["on_session_end"](**fields)

    def runs(self):
        return receipt.read_lines("runs.jsonl")

    def scheduled(self, name="morning-digest"):
        return CronExecution("abc123", name, "exec-9", "builtin", "2026-10-27T09:00:00+00:00", None, "default")

    def test_scheduled_run_is_recorded_under_the_job_name(self):
        self.end_turn(self.scheduled())
        [run] = self.runs()
        self.assertEqual((run["name"], run["outcome"], run["source"]), ("morning-digest", "ok", "hermes"))
        self.assertEqual(run["detail"], {"execution": "exec-9"})
        receipt.parse_time(run["at"])

    def test_failed_and_interrupted_runs(self):
        self.end_turn(self.scheduled(), completed=False, failed=True, turn_exit_reason="max_iterations_reached(90/90)")
        self.end_turn(self.scheduled(), completed=False, interrupted=True)
        failed, interrupted = self.runs()
        self.assertEqual(failed["outcome"], "failed")
        self.assertEqual(failed["detail"]["reason"], "max_iterations_reached(90/90)")
        self.assertEqual(interrupted["outcome"], "interrupted")

    def test_manual_run_is_marked(self):
        self.end_turn(self.scheduled()._replace(scheduled_instant=None))
        self.assertEqual(self.runs()[0]["detail"]["manual"], "yes")

    def test_chat_sessions_are_ignored(self):
        self.end_turn(None, session_id="20261027_090000_abcd", platform="telegram")
        self.assertEqual(self.runs(), [])

    def test_older_hermes_falls_back_to_the_session_id(self):
        ctx = FakeContext()
        del FakeContext.current_cron_execution  # simulate a PluginContext without the method
        try:
            self.plugin.register(ctx)
            ctx.hooks["on_session_end"](session_id="cron_abc123_20261027_090000", platform="cron", completed=True)
        finally:
            FakeContext.current_cron_execution = lambda self: self.run
        self.assertEqual(self.runs()[0]["name"], "abc123")

    def test_check_reports_a_run_that_ended_without_a_receipt(self):
        receipt.write_json("expect.json", {"morning-digest": {"every": 60, "created": "2026-10-01T00:00:00+00:00"}})
        self.end_turn(self.scheduled())
        [result] = receipt.check_all(datetime.now(timezone.utc))
        self.assertEqual(result["status"], "STALE")
        self.assertIn("a run ended", result["message"])
        self.assertIn("reported ok", result["message"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class OpenClawPlugin(unittest.TestCase):
    def test_node_unit_tests(self):
        proc = subprocess.run(["node", "--test"], cwd=OPENCLAW, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_records_are_readable_by_receipt(self):
        with tempfile.TemporaryDirectory() as home:
            script = ("import { appendRun, runRecord, runsPath } from './record.js';"
                      "appendRun(runRecord({action: 'finished', jobId: 'j', job: {name: 'feed-sync'},"
                      " status: 'ok', deliveryStatus: 'not-delivered'}), runsPath());")
            subprocess.run(["node", "--input-type=module", "-e", script], cwd=OPENCLAW, check=True,
                           env=dict(os.environ, RECEIPT_HOME=home), timeout=60)
            proc = subprocess.run([sys.executable, receipt.__file__, "log", "feed-sync"], capture_output=True,
                                  text=True, env=dict(os.environ, RECEIPT_HOME=home), timeout=60)
        self.assertIn("run      ok (delivery=not-delivered)  via openclaw", proc.stdout)


class Manifests(unittest.TestCase):
    def test_manifests_name_the_same_plugin(self):
        with open(os.path.join(OPENCLAW, "openclaw.plugin.json")) as f:
            manifest = json.load(f)
        with open(os.path.join(OPENCLAW, "package.json")) as f:
            package = json.load(f)
        self.assertEqual(manifest["id"], "receipts")
        self.assertEqual(package["openclaw"]["extensions"], ["./index.js"])
        with open(os.path.join(HERMES, "plugin.yaml")) as f:
            text = f.read()
        self.assertIn("name: receipts", text)
        self.assertIn("- on_session_end", text)


if __name__ == "__main__":
    unittest.main()
