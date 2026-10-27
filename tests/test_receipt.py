"""receipt must report a missed deadline exactly once, and never count a receipt toward the wrong day."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from agentbelt import receipt

UTC = timezone.utc


def t(day, hm):
    """2026-10-DAY HH:MM UTC. 2026-10-26 is a Monday."""
    h, m = map(int, hm.split(":"))
    return datetime(2026, 10, day, h, m, tzinfo=UTC)


def daily(by="09:05", grace=600, days=None, created=None):
    return {"by": by, "days": days or receipt.DAYS, "grace": grace, "tz": "UTC",
            "created": receipt.stamp(created or t(1, "00:00"))}


class Deadlines(unittest.TestCase):
    def status(self, spec, times, at):
        return receipt.evaluate(spec, times, at)["status"]

    def test_on_time_receipt_is_ok(self):
        self.assertEqual(self.status(daily(), [t(26, "08:58")], t(26, "10:00")), "ok")

    def test_receipt_within_grace_is_ok(self):
        self.assertEqual(self.status(daily(), [t(26, "09:12")], t(26, "10:00")), "ok")

    def test_no_receipt_after_grace_is_missing(self):
        r = receipt.evaluate(daily(), [t(25, "08:58")], t(26, "09:16"))
        self.assertEqual(r["status"], "MISSING")
        self.assertIn("yesterday 08:58", r["message"])

    def test_still_inside_grace_reports_the_previous_deadline(self):
        # at 09:10 today's deadline isn't due yet, so yesterday's receipt decides the status
        self.assertEqual(self.status(daily(), [t(25, "09:00")], t(26, "09:10")), "ok")

    def test_late_receipt_is_late(self):
        r = receipt.evaluate(daily(), [t(26, "10:20")], t(26, "11:00"))
        self.assertEqual(r["status"], "LATE")
        self.assertIn("75m late", r["message"])

    def test_late_receipt_does_not_count_for_the_next_day(self):
        self.assertEqual(self.status(daily(), [t(25, "10:20")], t(26, "09:30")), "MISSING")

    def test_evening_receipt_counts_for_the_next_morning(self):
        self.assertEqual(self.status(daily(), [t(25, "22:00")], t(26, "09:30")), "ok")

    def test_weekdays_skip_the_weekend(self):
        spec = daily(days=receipt.DAY_SETS["weekdays"])
        # Sunday 25th: the last deadline was Friday 23rd
        self.assertEqual(self.status(spec, [t(23, "09:00")], t(25, "12:00")), "ok")
        self.assertEqual(self.status(spec, [t(22, "09:00")], t(25, "12:00")), "MISSING")

    def test_new_expectation_waits_for_its_first_deadline(self):
        spec = daily(created=t(26, "10:00"))
        r = receipt.evaluate(spec, [], t(26, "11:00"))
        self.assertEqual(r["status"], "waiting")
        self.assertIn("tomorrow 09:05", r["message"])

    def test_each_missed_deadline_has_its_own_key(self):
        a = receipt.evaluate(daily(), [], t(26, "10:00"))["key"]
        b = receipt.evaluate(daily(), [], t(26, "23:00"))["key"]
        c = receipt.evaluate(daily(), [], t(27, "10:00"))["key"]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_deadlines_follow_daylight_saving_time(self):
        # US clocks go back on 2026-11-01, so 09:05 New York moves from 13:05 to 14:05 UTC
        spec = dict(daily(), tz="America/New_York")
        hours = {d.astimezone(UTC).date().isoformat(): d.astimezone(UTC).strftime("%H:%M")
                 for d in receipt.deadlines(spec, datetime(2026, 11, 1, 12, 0, tzinfo=UTC))}
        self.assertEqual(hours["2026-10-31"], "13:05")
        self.assertEqual(hours["2026-11-02"], "14:05")


class Intervals(unittest.TestCase):
    def spec(self, created=t(26, "00:00")):
        return {"every": 6 * 3600, "grace": 600, "created": receipt.stamp(created)}

    def test_recent_receipt_is_ok(self):
        self.assertEqual(receipt.evaluate(self.spec(), [t(26, "06:00")], t(26, "11:00"))["status"], "ok")

    def test_old_receipt_is_stale(self):
        r = receipt.evaluate(self.spec(), [t(26, "01:00")], t(26, "11:00"))
        self.assertEqual(r["status"], "STALE")
        self.assertIn("10h", r["message"])

    def test_never_received(self):
        self.assertEqual(receipt.evaluate(self.spec(), [], t(26, "03:00"))["status"], "waiting")
        self.assertEqual(receipt.evaluate(self.spec(), [], t(26, "07:00"))["status"], "STALE")


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, RECEIPT_HOME=self.home.name)
        self.alerts = os.path.join(self.home.name, "alerts.txt")

    def tearDown(self):
        self.home.cleanup()

    def receipt(self, *args):
        return subprocess.run([sys.executable, receipt.__file__, *args], capture_output=True, text=True,
                              env=self.env, timeout=60)

    def sent(self):
        try:
            with open(self.alerts) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def test_failed_check_records_nothing(self):
        self.receipt("expect", "export", "--every", "1h")
        r = self.receipt("add", "export", "--evidence", "file=out.csv", "--check", "exit 3")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOT RECORDED", r.stdout)
        self.assertEqual(self.receipt("log", "export").returncode, 1)

    def test_passing_check_records_evidence(self):
        self.receipt("expect", "export", "--every", "1h")
        r = self.receipt("add", "export", "--evidence", "rows=120", "--note", "nightly", "--check", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("next due by", r.stdout)
        self.assertIn("rows=120", self.receipt("log", "export").stdout)

    def test_add_names_the_deadline_it_counts_toward(self):
        self.receipt("expect", "digest", "--by", "09:05")
        r = self.receipt("add", "digest", "--evidence", "id=1")
        self.assertRegex(r.stdout, r"recorded digest at \d\d:\d\d, (on time|\S+ late) for \S+ 09:05")

    def test_evidence_is_required(self):
        self.assertEqual(self.receipt("add", "export").returncode, 2)
        self.assertNotEqual(self.receipt("add", "export", "--evidence", "nothing").returncode, 0)

    def test_unknown_name_warns_with_suggestion(self):
        self.receipt("expect", "digest", "--by", "09:05")
        r = self.receipt("add", "digset", "--evidence", "id=1")
        self.assertIn("did you mean digest", r.stdout)

    def test_alerts_once_per_problem_and_once_on_recovery(self):
        alert = f"cat >> {self.alerts}"
        self.receipt("expect", "sync", "--every", "3s")
        self.receipt("add", "sync", "--evidence", "rows=1")
        time.sleep(3.5)
        self.assertEqual(self.receipt("check", "--alert", alert).returncode, 1)
        self.receipt("check", "--alert", alert)
        self.assertEqual(len(self.sent()), 1)
        self.assertIn("sync: STALE", self.sent()[0])
        self.receipt("add", "sync", "--evidence", "rows=2")
        self.assertEqual(self.receipt("check", "--alert", alert).returncode, 0)
        self.receipt("check", "--alert", alert)
        self.assertEqual(len(self.sent()), 2)
        self.assertIn("sync: RECOVERED", self.sent()[1])

    def test_failed_alert_is_retried(self):
        self.receipt("expect", "sync", "--every", "1s")
        time.sleep(2.2)
        r = self.receipt("check", "--alert", "exit 1")
        self.assertIn("will retry", r.stderr)
        self.receipt("check", "--alert", f"cat >> {self.alerts}")
        self.assertEqual(len(self.sent()), 1)

    def test_json_output(self):
        self.receipt("expect", "digest", "--by", "09:05")
        out = json.loads(self.receipt("check", "--json").stdout)
        self.assertEqual(out["problems"], 0)
        self.assertEqual(out["results"][0]["status"], "waiting")

    def test_bad_input_is_rejected(self):
        for args in [["expect", "x", "--by", "25:00"], ["expect", "x", "--by", "09:00", "--days", "funday"],
                     ["expect", "x", "--every", "soon"], ["expect", "x"], ["expect", "x", "--by", "09:00", "--tz", "Mars/Base"]]:
            with self.subTest(args=args):
                self.assertNotEqual(self.receipt(*args).returncode, 0)

    def test_truncated_receipt_line_is_skipped(self):
        self.receipt("expect", "sync", "--every", "1h")
        self.receipt("add", "sync", "--evidence", "rows=1")
        with open(os.path.join(self.home.name, "receipts.jsonl"), "a") as f:
            f.write('{"name": "sync", "at": "2026-')
        self.assertEqual(self.receipt("check").returncode, 0)


if __name__ == "__main__":
    unittest.main()
