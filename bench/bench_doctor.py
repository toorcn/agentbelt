"""Benchmark doctor on every repository in a folder against a typical manual orientation sequence.

Usage: python3 bench/bench_doctor.py DIR
Writes per-repository results to bench/doctor_results.json.
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DOCTOR = os.path.join(HERE, "..", "src", "agent_tools", "doctor.py")
SELF = os.path.basename(os.path.dirname(HERE))

# What an agent typically runs to orient itself in an unfamiliar repository.
BASELINE = [
    "ls -la",
    "cat package.json 2>/dev/null",
    "cat pyproject.toml requirements.txt 2>/dev/null",
    "cat README.md 2>/dev/null",
    "cat .env.example .env.sample 2>/dev/null",
    "git status 2>/dev/null",
    "node --version; python3 --version",
    "ls node_modules 2>/dev/null | head -5; ls -d .venv 2>/dev/null",
]


def tokens(n_chars):
    return n_chars / 4


def median(values):
    values = sorted(values)
    return values[len(values) // 2]


def shell(cmd, cwd):
    try:
        return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=20).stdout
    except subprocess.TimeoutExpired:
        return ""


def doctor(path, *flags):
    return subprocess.run([sys.executable, DOCTOR, path, *flags], capture_output=True, text=True, timeout=120)


def problem_kind(problem):
    """Group messages like '12 env vars required…' and '3 env vars required…' together."""
    head = problem["message"].split(":")[0].split("(")[0]
    return f"[{problem['severity']}] " + re.sub(r"\d+(\.\d+)*", "N", head).strip()


def evaluate(path):
    start = time.time()
    text = doctor(path)
    elapsed = time.time() - start
    try:
        data = json.loads(doctor(path, "--json").stdout)
    except ValueError:
        data = {}
    split = isinstance(data, list)
    reports = data if split else [data]
    return {
        "dir": os.path.basename(path),
        "ok": text.returncode == 0,
        "error": text.stderr.strip()[-150:],
        "sec": round(elapsed, 2),
        "out": len(text.stdout),
        "baseline": sum(len(shell(c, path)) for c in BASELINE),
        "problems": [p for r in reports for p in r.get("problems", [])],
        "has_stack": any(r.get("stack") for r in reports),
        "has_commands": any(set(r.get("commands", {})) & {"dev", "build", "test"} for r in reports),
        "split": split,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    args = ap.parse_args()

    dirs = sorted(d for d in os.listdir(args.dir)
                  if os.path.isdir(os.path.join(args.dir, d)) and not d.startswith(".") and d != SELF)
    rows = [evaluate(os.path.join(args.dir, d)) for d in dirs]
    with open(os.path.join(HERE, "doctor_results.json"), "w") as f:
        json.dump(rows, f, indent=1)

    ok = [r for r in rows if r["ok"]]
    print(f"repositories: {len(rows)}  ok: {len(ok)}  crashed: {len(rows) - len(ok)}")
    for r in rows:
        if not r["ok"]:
            print(f"  crash in {r['dir']}: {r['error']}")
    if not ok:
        return
    print(f"time: median {median(r['sec'] for r in ok)}s, max {max(r['sec'] for r in ok)}s")
    print(f"doctor output: median {tokens(median(r['out'] for r in ok)):.0f} tok, "
          f"max {tokens(max(r['out'] for r in ok)):.0f} tok")
    print(f"manual baseline ({len(BASELINE)} calls): median {tokens(median(r['baseline'] for r in ok)):.0f} tok, "
          f"max {tokens(max(r['baseline'] for r in ok)):.0f} tok")
    print(f"found dev/build/test commands: {sum(r['has_commands'] for r in ok)}/{len(ok)}")
    print(f"no manifest: {sum(not r['has_stack'] for r in ok)}  split into sub-projects: {sum(r['split'] for r in ok)}")
    print(f"with at least one problem: {sum(bool(r['problems']) for r in ok)}/{len(ok)}")
    print("problem kinds:")
    kinds = collections.Counter(problem_kind(p) for r in ok for p in r["problems"])
    for kind, count in kinds.most_common(20):
        print(f"  {count:>3}  {kind}")


if __name__ == "__main__":
    main()
