"""Benchmark peek on the largest data and log files in a folder, comparing output size to raw size.

Usage: python3 bench/bench_peek.py DIR [--limit N]
Writes per-file results to bench/peek_results.json.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PEEK = os.path.join(HERE, "..", "peek.py")
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", ".next", "dist", "build"}
EXTENSIONS = {".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".log", ".db", ".sqlite", ".sqlite3"}
MIN_BYTES = 50_000
MAX_DEPTH = 5


def tokens(n_chars):
    return n_chars / 4


def find_files(root):
    found = []
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirs, files in os.walk(root):
        depth = dirpath.count(os.sep) - base_depth
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and depth < MAX_DEPTH]
        for name in files:
            path = os.path.join(dirpath, name)
            if os.path.splitext(name)[1].lower() in EXTENSIONS and os.path.getsize(path) > MIN_BYTES:
                found.append(path)
    return found


def run_peek(path):
    start = time.time()
    proc = subprocess.run([sys.executable, PEEK, path], capture_output=True, text=True, timeout=120)
    elapsed = time.time() - start
    ok = proc.returncode == 0
    kind = proc.stdout.split("[", 1)[1].split(",", 1)[0] if ok and "[" in proc.stdout else "error"
    return {
        "file": path,
        "kind": kind,
        "bytes": os.path.getsize(path),
        "out": len(proc.stdout),
        "sec": round(elapsed, 2),
        "error": "" if ok else proc.stderr.strip()[-200:],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--limit", type=int, default=60)
    args = ap.parse_args()

    files = sorted(find_files(args.dir), key=os.path.getsize, reverse=True)[: args.limit]
    rows = [run_peek(f) for f in files]
    for r in rows:
        r["file"] = os.path.relpath(r["file"], args.dir)
    with open(os.path.join(HERE, "peek_results.json"), "w") as f:
        json.dump(rows, f, indent=1)

    print(f"{'kind':<7}{'file KB':>9}{'file tok':>11}{'peek tok':>10}{'ratio':>8}{'sec':>6}")
    for r in rows:
        ratio = r["bytes"] / max(r["out"], 1)
        print(f"{r['kind']:<7}{r['bytes'] / 1024:>9.0f}{tokens(r['bytes']):>11.0f}{tokens(r['out']):>10.0f}"
              f"{ratio:>7.0f}x{r['sec']:>6}  {r['error'][:80]}")

    ok = [r for r in rows if not r["error"]]
    if not ok:
        print("no files summarized")
        return
    outs = sorted(r["out"] for r in ok)
    print(f"\n{len(ok)}/{len(rows)} ok")
    print(f"peek output: median {tokens(outs[len(outs) // 2]):.0f} tok, max {tokens(outs[-1]):.0f} tok")
    print(f"total: {tokens(sum(r['bytes'] for r in ok)) / 1e6:.1f}M raw tok -> {tokens(sum(outs)) / 1e3:.0f}k tok")
    print(f"slowest file: {max(r['sec'] for r in ok)}s")


if __name__ == "__main__":
    main()
