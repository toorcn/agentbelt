"""peek must never print credentials, whatever format they arrive in.

Fake credentials are assembled at runtime so that no secret-shaped literal appears in the source
(which would trip secret scanners on push).
"""
import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_tools import peek

PEEK = peek.__file__


def fake(prefix, n, alphabet="aB3xY7kQ9mZ2"):
    return prefix + (alphabet * (n // len(alphabet) + 1))[:n]


OPENAI = fake("sk-", 48)
ANTHROPIC = fake("sk-ant-api03-", 40)
STRIPE = fake("sk_" + "live_", 24)
GITHUB = fake("ghp" + "_", 36)
GITHUB_FINE = fake("github_" + "pat_", 60)
SLACK = fake("xox" + "b-", 30, "1234567890-")
AWS = "AKIA" + "IOSFODNN7EXAMPL0"
GOOGLE = fake("AI" + "za", 35)
JWT = fake("ey" + "J", 30) + "." + fake("ey" + "J", 40) + "." + fake("", 30)
PRIVATE_KEY = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow" + fake("", 40)
DB_URL = "postgres://app:" + fake("", 18) + "@db.internal:5432/app"
FORMATTED = [OPENAI, ANTHROPIC, STRIPE, GITHUB, GITHUB_FINE, SLACK, AWS, GOOGLE, JWT, PRIVATE_KEY]
PLAIN_PASSWORD = "correct-horse-battery-staple"


def run_peek(path, *flags):
    proc = subprocess.run([sys.executable, PEEK, path, *flags], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"peek failed: {proc.stderr}")
    return proc.stdout


class RedactByName(unittest.TestCase):
    def test_secret_field_names_are_masked(self):
        for name in ["password", "user_password", "passwd", "secret", "client_secret", "api_key", "apiKey", "APIKEY",
                     "x-api-key", "access_key", "private_key", "token", "access_token", "refreshToken", "auth",
                     "authorization", "session", "sessionId", "cookie", "credentials", "salt", "STRIPE_SECRET_KEY"]:
            with self.subTest(name=name):
                self.assertTrue(peek.is_secret_name(name), name)
                self.assertIn("redacted", peek.short("hunter2-value", name))

    def test_ordinary_field_names_are_kept(self):
        for name in ["author", "authority", "max_tokens", "tokens", "tokenizer", "key", "keyboard", "status",
                     "description", "passed", "compass", "name", "monkey", "secretary_name", "title", "id", "hash"]:
            with self.subTest(name=name):
                self.assertFalse(peek.is_secret_name(name), name)
                self.assertEqual(peek.short("plain value", name), "plain value")

    def test_only_strings_are_masked(self):
        self.assertEqual(peek.redact(42, "token"), 42)
        self.assertIsNone(peek.redact(None, "password"))
        self.assertEqual(peek.redact("", "password"), "")


class RedactByValue(unittest.TestCase):
    def test_known_credential_formats_are_masked_under_any_name(self):
        for value in FORMATTED + [DB_URL]:
            with self.subTest(value=value[:12]):
                self.assertIn("redacted", peek.short(value, "note"))

    def test_ordinary_values_are_kept(self):
        for value in ["passed", "https://example.com/docs?page=2", "postgres://db.internal:5432/app",
                      "550e8400-e29b-41d4-a716-446655440000", "e081b566a42d3a83b8102157bd7806cf4ff63b17",
                      "2026-10-26T09:00:00Z", "skeleton-key", "sk-short", "akia is a word"]:
            with self.subTest(value=value):
                self.assertEqual(peek.redact(value, "note"), value)

    def test_lookalikes_inside_longer_strings_are_kept(self):
        # regressions from real data: a product URL slug and inline base64 image data
        slug = "https://shop.example.com/products/desk-lamp-adjustable-white-led"
        image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAB" + fake("AI" + "za", 35) + "AAElFTkSuQmCC"
        for value in [slug, image]:
            with self.subTest(value=value[:30]):
                self.assertEqual(peek.redact(value, "url"), value)

    def test_blobs_are_described_not_printed(self):
        self.assertEqual(peek.redact(b"\x00\x01secret", "data"), "<blob 8B>")


class RedactText(unittest.TestCase):
    def test_key_value_pairs_in_log_lines(self):
        for line, secret in [
            (f"login ok password={PLAIN_PASSWORD} user=sam", PLAIN_PASSWORD),
            (f'config {{"api_key": "{PLAIN_PASSWORD}"}}', PLAIN_PASSWORD),
            (f"Authorization: Bearer {fake('', 32)}", fake("", 32)),
            (f"retrying with token {GITHUB}", GITHUB),
            (f"DATABASE_URL={DB_URL}", DB_URL.split("@")[0]),
        ]:
            with self.subTest(line=line[:30]):
                out = peek.redact_text(line)
                self.assertNotIn(secret, out)
                self.assertIn("redacted", out)

    def test_numbers_and_ordinary_text_survive(self):
        for line in ["max_tokens=4096 temperature=0.2", "GET /api/session 200 in 12ms", "3 passed, 1 failed in 0.52s"]:
            with self.subTest(line=line):
                self.assertEqual(peek.redact_text(line), line)


class EndToEnd(unittest.TestCase):
    """Write each supported format to disk with secrets in it and check none reach stdout."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.secrets = FORMATTED + [PLAIN_PASSWORD, DB_URL.split("@")[0]]

    def tearDown(self):
        self.dir.cleanup()

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def assert_clean(self, output):
        for secret in self.secrets:
            # the first line of a PEM block is a header, not the secret itself
            needle = secret.splitlines()[-1] if secret.startswith("-----") else secret
            self.assertNotIn(needle, output, f"leaked {needle[:12]}…")
        self.assertIn("redacted", output)

    def records(self, n=300):
        return [{"id": i, "email": f"user{i}@example.com", "password": PLAIN_PASSWORD,
                 "credentials": {"user": "svc", "pass": PLAIN_PASSWORD, "dsn": DB_URL},
                 "note": FORMATTED[i % len(FORMATTED)]} for i in range(n)]

    def test_json(self):
        with open(self.path("users.json"), "w") as f:
            json.dump({"users": self.records()}, f)
        self.assert_clean(run_peek(self.path("users.json")))
        self.assert_clean(run_peek(self.path("users.json"), "--json"))
        self.assert_clean(run_peek(self.path("users.json"), "--path", "users[0]"))

    def test_jsonl(self):
        with open(self.path("users.jsonl"), "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in self.records())
        self.assert_clean(run_peek(self.path("users.jsonl"), "--rows", "20"))

    def test_csv(self):
        with open(self.path("users.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "email", "password", "note"])
            for r in self.records():
                w.writerow([r["id"], r["email"], r["password"], r["note"]])
        self.assert_clean(run_peek(self.path("users.csv"), "--rows", "20"))

    def test_sqlite_columns_and_key_value_tables(self):
        con = sqlite3.connect(self.path("app.db"))
        con.execute("create table users (id integer primary key, email text, password text, api_token text, avatar blob)")
        con.execute("create table options (key text primary key, value text)")
        for r in self.records(20):
            con.execute("insert into users values (?, ?, ?, ?, ?)", (r["id"], r["email"], PLAIN_PASSWORD, r["note"], b"\x89PNG"))
        con.executemany("insert into options values (?, ?)",
                        [("SMTPPassword", PLAIN_PASSWORD), ("StripeSecretKey", "plain-" + PLAIN_PASSWORD), ("Theme", "dark")])
        con.commit()
        con.close()
        out = run_peek(self.path("app.db"), "--rows", "3")
        self.secrets.append("plain-" + PLAIN_PASSWORD)
        self.assert_clean(out)
        self.assertIn("dark", out, "non-secret settings should still be shown")

    def test_logs(self):
        lines = [f"INFO request {i} ok in {i % 90}ms" for i in range(500)]
        lines += [f"ERROR auth failed for password={PLAIN_PASSWORD}", f"DEBUG using key {OPENAI}",
                  f"WARN connecting to {DB_URL}", f"Authorization: Bearer {JWT}"]
        lines += [f"INFO tail {i}" for i in range(5)]
        with open(self.path("app.log"), "w") as f:
            f.write("\n".join(lines))
        self.assert_clean(run_peek(self.path("app.log"), "--rows", "20"))

    def test_small_files_shown_in_full_are_redacted_too(self):
        with open(self.path("tiny.env.json"), "w") as f:
            json.dump({"api_key": PLAIN_PASSWORD, "region": "us-west-2"}, f)
        out = run_peek(self.path("tiny.env.json"))
        self.assertNotIn(PLAIN_PASSWORD, out)
        self.assertIn("us-west-2", out)


if __name__ == "__main__":
    unittest.main()
