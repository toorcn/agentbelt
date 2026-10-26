import json
import os
import subprocess
import sys
import tempfile
import unittest

from agentbelt import doctor


class VersionRanges(unittest.TestCase):
    def test_satisfies(self):
        cases = [
            ("22.16.0", ">=22.12", True),
            ("22.16.0", ">=22.20", False),
            ("22.16.0", "22", True),
            ("22.16.0", "20", False),
            ("22.16.0", "v22.16.0", True),
            ("22.16.0", "22.22.1", False),
            ("22.16.0", "^22.1.0", True),
            ("23.0.0", "^22.1.0", False),
            ("22.16.0", "~22.16.0", True),
            ("22.17.0", "~22.16.0", False),
            ("20.11.0", "^18 || ^20", True),
            ("3.12.12", ">=3.10 <3.13", True),
            ("3.13.1", ">=3.10 <3.13", False),
        ]
        for installed, spec, expected in cases:
            with self.subTest(installed=installed, spec=spec):
                self.assertIs(doctor.satisfies(installed, spec), expected)

    def test_unknown_inputs(self):
        self.assertIsNone(doctor.satisfies(None, ">=20"))
        self.assertIsNone(doctor.satisfies("20.0.0", ""))


class Repository(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "app")
        os.makedirs(os.path.join(self.root, "src"))
        os.makedirs(os.path.join(self.root, "tests"))
        self.write("package.json", json.dumps({
            "name": "app", "engines": {"node": ">=18"},
            "scripts": {"dev": "vite", "build": "vite build", "test": "vitest run"},
            "devDependencies": {"vite": "^6.0.0"},
        }))
        self.write("package-lock.json", "{}")
        self.write(".env.example", "DATABASE_URL=\n# SENTRY_DSN=\n")
        self.write("src/config.ts", "\n".join([
            "export const db = process.env.DATABASE_URL;",
            "export const key = process.env.STRIPE_KEY;",
            'export const region = process.env.APP_REGION ?? "us-west-2";',
            "export const debug = process.env.VERBOSE === '1';",
            "if (process.env.SENTRY_DSN) init();",
        ]))
        self.write("tests/config.test.ts", "process.env.TEST_ONLY_VAR = 'x';")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        with open(os.path.join(self.root, rel), "w") as f:
            f.write(text)

    def snapshot(self):
        return sorted((p, os.path.getmtime(os.path.join(p, f)))
                      for p, _, files in os.walk(self.root) for f in files)

    def test_commands_and_env_classification(self):
        rep = doctor.diagnose(self.root)
        self.assertEqual(rep.commands["install"], "npm ci")
        self.assertEqual(rep.commands["dev"], "npm run dev")
        self.assertEqual(rep.commands["test"], "npm run test")
        self.assertEqual(rep.env["missing"], ["DATABASE_URL", "STRIPE_KEY"])
        self.assertEqual(rep.env["missing (has fallback)"], ["APP_REGION", "SENTRY_DSN", "VERBOSE"])
        self.assertTrue(any("not installed" in msg for _, msg, _ in rep.problems))

    def test_is_read_only(self):
        before = self.snapshot()
        subprocess.run([sys.executable, doctor.__file__, self.root, "--json"], capture_output=True, check=True, timeout=60)
        self.assertEqual(before, self.snapshot())

    def test_env_values_are_never_printed(self):
        self.write(".env", "DATABASE_URL=postgres://u:hunter2@localhost/db\n")
        out = subprocess.run([sys.executable, doctor.__file__, self.root, "--json"],
                             capture_output=True, text=True, check=True, timeout=60).stdout
        self.assertNotIn("hunter2", out)
        self.assertNotIn("DATABASE_URL", json.loads(out)["env"]["missing"])


if __name__ == "__main__":
    unittest.main()
