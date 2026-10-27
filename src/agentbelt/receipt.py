#!/usr/bin/env python3
"""receipt: notice when scheduled agent work silently doesn't happen.

An agent files a receipt after it has verified that a piece of work is done. A watcher, run from
cron outside the agents, compares receipts with what was expected and reports anything missing.

Usage:
  receipt expect NAME --by HH:MM [--days daily|weekdays|mon,wed,...] [--grace 10m] [--tz ZONE]
      NAME must be done by HH:MM on each of those days.
  receipt expect NAME --every 6h [--grace 10m]
      NAME must be done at least once every 6 hours.
  receipt expect [NAME --remove]
      List expectations, or remove one.
  receipt add NAME --evidence KEY=VALUE [--evidence ...] [--note TEXT] [--check CMD]
      Record that NAME is done. With --check, CMD must exit 0 or nothing is recorded.
  receipt check [--alert CMD] [--json]
      Report every expectation as ok, waiting, MISSING, LATE or STALE. Exits 1 if any is a problem.
      --alert runs CMD once per new problem (and once on recovery), with the message on stdin and
      RECEIPT_NAME, RECEIPT_STATUS and RECEIPT_MESSAGE in its environment.
  receipt run NAME --outcome ok|failed|interrupted|skipped [--source S] [--detail KEY=VALUE ...]
      Record that a run of NAME ended, as reported by the scheduler. The OpenClaw and Hermes plugins
      in plugins/ do this automatically. A receipt is still needed: a run can end with a normal
      reply when the work never happened.
  receipt log NAME [--limit N]
      Recent receipts and runs.

A receipt counts toward the deadline it is closest to, and is on time if it arrives no later than
the deadline plus the grace period. When runs are recorded for a job, check says whether a run
ended without a receipt (the work silently failed) or no run happened at all (the scheduler
didn't fire it). State is kept in $RECEIPT_HOME, or ~/.receipts by default: expect.json,
receipts.jsonl, runs.jsonl (one JSON object per line: name, at, outcome, source, detail) and
alerts.json.
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

HOME = os.environ.get("RECEIPT_HOME", os.path.expanduser("~/.receipts"))
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_SETS = {"daily": DAYS, "weekdays": DAYS[:5], "weekends": DAYS[5:]}
DURATION = re.compile(r"^(\d+)([smhd])$")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
PROBLEMS = {"MISSING", "LATE", "STALE"}


def path(name):
    return os.path.join(HOME, name)


def read_json(name, default):
    try:
        with open(path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(name, data):
    os.makedirs(HOME, exist_ok=True)
    tmp = path(name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path(name))


def read_lines(name):
    out = []
    try:
        with open(path(name)) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # a partial line from an interrupted write
    except OSError:
        pass
    return out


def read_receipts():
    return read_lines("receipts.jsonl")


def append_line(name, rec):
    os.makedirs(HOME, exist_ok=True)
    # one write() of one short line, so concurrent agents don't interleave
    with open(path(name), "a") as f:
        f.write(json.dumps(rec) + "\n")


def runs_by_name():
    out = {}
    for r in read_lines("runs.jsonl"):
        try:
            out.setdefault(r["name"], []).append(dict(r, t=parse_time(r["at"])))
        except (KeyError, TypeError, ValueError):
            continue
    for runs in out.values():
        runs.sort(key=lambda r: r["t"])
    return out


def now():
    return datetime.now().astimezone()


def parse_time(s):
    return datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)


def stamp(t):
    return t.isoformat(timespec="seconds")


def parse_duration(s):
    m = DURATION.match(s.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration {s!r}; use e.g. 30s, 10m, 6h, 2d")
    return int(m.group(1)) * UNITS[m.group(2)]


def show_duration(sec):
    sec = int(sec)
    if sec < 120:
        return f"{sec}s"
    if sec < 7200:
        return f"{sec // 60}m"
    if sec < 172800:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def show_time(t, ref):
    """Short local time relative to ref: '09:05', 'yesterday 09:05', 'Mon 09:05', '2026-10-01 09:05'."""
    t = t.astimezone(ref.tzinfo)
    days = (t.date() - ref.date()).days
    hm = t.strftime("%H:%M")
    if days == 0:
        return f"today {hm}"
    if days == -1:
        return f"yesterday {hm}"
    if days == 1:
        return f"tomorrow {hm}"
    if -6 <= days <= 6:
        return f"{DAYS[t.weekday()].capitalize()} {hm}"
    return t.strftime("%Y-%m-%d %H:%M")


def zone(spec):
    if not spec.get("tz"):
        return None
    from zoneinfo import ZoneInfo
    return ZoneInfo(spec["tz"])


def deadlines(spec, around):
    """Every deadline from 9 days before to 9 days after `around`, oldest first."""
    tz = zone(spec)
    local = around.astimezone(tz) if tz else around
    hh, mm = map(int, spec["by"].split(":"))
    days = set(spec["days"])
    out = []
    for offset in range(-9, 10):
        d = local.date() + timedelta(days=offset)
        if DAYS[d.weekday()] not in days:
            continue
        naive = datetime(d.year, d.month, d.day, hh, mm)
        out.append(naive.replace(tzinfo=tz) if tz else naive.astimezone())
    return out


def describe_run(run):
    detail = run.get("detail") or {}
    extra = ", ".join(f"{k}={v}" for k, v in detail.items())
    return run.get("outcome", "?") + (f" ({extra})" if extra else "")


def run_note(runs, since, at):
    """What the scheduler reported since `since`, appended to a problem message.

    Empty when no run was ever recorded for the job, which means no plugin reports its runs.
    """
    if not runs:
        return ""
    recent = [r for r in runs if since < r["t"] <= at]
    if not recent:
        return f"; no run recorded since {show_time(since, at)}"
    r = recent[-1]
    return f"; a run ended {show_time(r['t'], at)} and reported {describe_run(r)}"


def evaluate(spec, times, at, runs=None):
    """Status of one expectation at time `at`, given the times of its receipts and recorded runs.

    Returns a dict with status (ok, waiting, MISSING, LATE, STALE), message, and key. The key
    identifies the specific problem, so an alert fires once per missed deadline, not once per check.
    """
    times = sorted(times)
    last = times[-1] if times else None
    created = parse_time(spec["created"])
    grace = timedelta(seconds=spec.get("grace", 0))

    if "every" in spec:
        every = timedelta(seconds=spec["every"])
        if last and at - last <= every + grace:
            return {"status": "ok", "message": f"last {show_duration((at - last).total_seconds())} ago", "key": None}
        if not last and at - created <= every + grace:
            return {"status": "waiting", "message": f"first due {show_time(created + every, at)}", "key": None}
        since = last or created
        what = f"last receipt {show_time(last, at)}" if last else "no receipts yet"
        return {"status": "STALE", "key": f"stale@{stamp(since)}",
                "message": f"nothing in {show_duration((at - since).total_seconds())}, expected every "
                           f"{show_duration(every.total_seconds())} ({what})" + run_note(runs, since, at)}

    ds = deadlines(spec, at)
    due = [i for i, d in enumerate(ds) if d > created and d + grace <= at]
    upcoming = next((d for d in ds if d > at), None)
    nxt = f"next due {show_time(upcoming, at)}" if upcoming else ""
    if not due:
        first = next((d for d in ds if d > created), None)
        return {"status": "waiting", "message": f"first due {show_time(first, at)}" if first else "", "key": None}

    i = due[-1]
    d = ds[i]
    lo = d - (d - ds[i - 1]) / 2 if i > 0 else d - timedelta(days=7)
    hi = d + (ds[i + 1] - d) / 2 if i + 1 < len(ds) else d + timedelta(days=7)
    mine = [t for t in times if lo < t <= hi]
    on_time = [t for t in mine if t <= d + grace]
    if on_time:
        return {"status": "ok", "message": f"done {show_time(on_time[0], at)} for {show_time(d, at)}; {nxt}".rstrip("; "),
                "key": None}
    if mine:
        late = show_duration((mine[0] - d).total_seconds())
        return {"status": "LATE", "key": f"late@{stamp(d)}",
                "message": f"due {show_time(d, at)}, done {show_time(mine[0], at)} ({late} late); {nxt}".rstrip("; ")}
    before = f"last receipt {show_time(last, at)}" if last else "no receipts yet"
    return {"status": "MISSING", "key": f"missing@{stamp(d)}",
            "message": f"due {show_time(d, at)}, no receipt ({before})" + run_note(runs, lo, at)}


def describe_spec(spec):
    if "every" in spec:
        out = f"every {show_duration(spec['every'])}"
    else:
        days = spec["days"]
        label = next((k for k, v in DAY_SETS.items() if v == days), ",".join(days))
        out = f"by {spec['by']} {label}"
        if spec.get("tz"):
            out += f" {spec['tz']}"
    if spec.get("grace"):
        out += f", grace {show_duration(spec['grace'])}"
    return out


def cmd_expect(a):
    specs = read_json("expect.json", {})
    if not a.name:
        if not specs:
            print("no expectations; add one with: receipt expect NAME --by HH:MM")
        for name, spec in sorted(specs.items()):
            print(f"{name}: {describe_spec(spec)}")
        return 0
    if a.remove:
        if specs.pop(a.name, None) is None:
            print(f"no expectation named {a.name}")
            return 1
        write_json("expect.json", specs)
        print(f"{a.name}: removed")
        return 0
    if bool(a.by) == bool(a.every):
        sys.exit("receipt expect: give exactly one of --by HH:MM or --every DURATION")
    spec = {"created": stamp(now())}
    if a.every:
        spec["every"] = a.every
    else:
        if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", a.by):
            sys.exit(f"receipt expect: bad time {a.by!r}; use HH:MM in 24-hour time")
        days = DAY_SETS.get(a.days) or [d.strip()[:3].lower() for d in a.days.split(",")]
        bad = [d for d in days if d not in DAYS]
        if bad:
            sys.exit(f"receipt expect: unknown day {bad[0]!r}; use daily, weekdays, weekends or mon,tue,...")
        spec.update(by=a.by, days=[d for d in DAYS if d in days])
        if a.tz:
            try:
                zone({"tz": a.tz})
            except Exception:
                sys.exit(f"receipt expect: unknown time zone {a.tz!r}")
            spec["tz"] = a.tz
    if a.grace:
        spec["grace"] = a.grace
    if a.name in specs:
        spec["created"] = specs[a.name]["created"]
    specs[a.name] = spec
    write_json("expect.json", specs)
    print(f"{a.name}: {describe_spec(spec)}")
    return 0


def cmd_add(a):
    evidence = {}
    for item in a.evidence:
        key, sep, value = item.partition("=")
        if not sep or not key.strip() or not value.strip():
            sys.exit(f"receipt add: bad evidence {item!r}; use KEY=VALUE, e.g. message_id=1234")
        evidence[key.strip()] = value.strip()
    if a.check:
        try:
            proc = subprocess.run(a.check, shell=True, capture_output=True, text=True, timeout=a.check_timeout)
        except subprocess.TimeoutExpired:
            print(f"NOT RECORDED: check timed out after {a.check_timeout:g}s: {a.check}")
            return 1
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            print(f"NOT RECORDED: check exited {proc.returncode}: {a.check}")
            if detail:
                print(f"  {detail[-1][:220]}")
            return 1
    t = now()
    rec = {"name": a.name, "at": stamp(t), "evidence": evidence}
    if a.note:
        rec["note"] = a.note
    if a.check:
        rec["check"] = a.check
    append_line("receipts.jsonl", rec)
    specs = read_json("expect.json", {})
    spec = specs.get(a.name)
    if not spec:
        print(f"recorded {a.name} at {t.strftime('%H:%M')}")
        hint = difflib.get_close_matches(a.name, specs, n=1)
        print(f"warning: no expectation named {a.name}, so `receipt check` won't watch it"
              + (f" (did you mean {hint[0]}?)" if hint else ""))
        return 0
    grace = timedelta(seconds=spec.get("grace", 0))
    if "every" in spec:
        print(f"recorded {a.name} at {t.strftime('%H:%M')}; next due by "
              f"{show_time(t + timedelta(seconds=spec['every']) + grace, t)}")
        return 0
    d = min(deadlines(spec, t), key=lambda d: abs(d - t))  # the deadline this receipt counts toward
    when = "on time" if t <= d + grace else f"{show_duration((t - d).total_seconds())} late"
    print(f"recorded {a.name} at {t.strftime('%H:%M')}, {when} for {show_time(d, t)}")
    return 0


def cmd_run(a):
    detail = {}
    for item in a.detail:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            sys.exit(f"receipt run: bad detail {item!r}; use KEY=VALUE")
        detail[key.strip()] = value.strip()
    rec = {"name": a.name, "at": stamp(now()), "outcome": a.outcome, "source": a.source}
    if detail:
        rec["detail"] = detail
    append_line("runs.jsonl", rec)
    print(f"run of {a.name} recorded: {describe_run(rec)}")
    return 0


def check_all(at):
    specs = read_json("expect.json", {})
    runs = runs_by_name()
    by_name = {}
    for r in read_receipts():
        try:
            by_name.setdefault(r["name"], []).append(parse_time(r["at"]))
        except (KeyError, TypeError, ValueError):
            continue
    return [dict(name=name, **evaluate(spec, by_name.get(name, []), at, runs.get(name)))
            for name, spec in sorted(specs.items())]


def alert(cmd, name, status, message):
    env = dict(os.environ, RECEIPT_NAME=name, RECEIPT_STATUS=status, RECEIPT_MESSAGE=message)
    try:
        proc = subprocess.run(cmd, shell=True, input=f"{name}: {status}, {message}\n", text=True,
                              capture_output=True, env=env, timeout=30)
    except subprocess.TimeoutExpired:
        print(f"warning: alert command timed out for {name}; will retry next check", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"warning: alert command exited {proc.returncode} for {name}; will retry next check", file=sys.stderr)
        return False
    return True


def cmd_check(a):
    results = check_all(now())
    if a.alert:
        sent = read_json("alerts.json", {})
        for r in results:
            if r["status"] in PROBLEMS and sent.get(r["name"]) != r["key"]:
                if alert(a.alert, r["name"], r["status"], r["message"]):
                    sent[r["name"]] = r["key"]
            elif r["status"] not in PROBLEMS and r["name"] in sent:
                if alert(a.alert, r["name"], "RECOVERED", r["message"]):
                    del sent[r["name"]]
        for name in list(sent):
            if name not in {r["name"] for r in results}:
                del sent[name]
        write_json("alerts.json", sent)
    problems = [r for r in results if r["status"] in PROBLEMS]
    if a.json:
        print(json.dumps({"problems": len(problems), "results": results}, indent=1))
    elif not results:
        print("no expectations; add one with: receipt expect NAME --by HH:MM")
    else:
        width = max(len(r["name"]) for r in results)
        for r in results:
            print(f"{r['status']:<8} {r['name']:<{width}}  {r['message']}")
    return 1 if problems else 0


def cmd_log(a):
    rows = []
    for r in read_receipts():
        if r.get("name") == a.name:
            ev = " ".join(f"{k}={v}" for k, v in r.get("evidence", {}).items())
            rows.append((parse_time(r["at"]), f"receipt  {ev}" + (f"  # {r['note']}" if r.get("note") else "")))
    for r in runs_by_name().get(a.name, []):
        rows.append((r["t"], f"run      {describe_run(r)}" + (f"  via {r['source']}" if r.get("source") else "")))
    if not rows:
        print(f"no receipts or runs for {a.name}")
        return 1
    rows.sort(key=lambda row: row[0])
    ref = now()
    for t, text in rows[-a.limit:]:
        print(f"{show_time(t, ref)}  {text}")
    if len(rows) > a.limit:
        print(f"({len(rows) - a.limit} older not shown; --limit to see more)")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="receipt", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="op", required=True)
    s = sub.add_parser("expect")
    s.add_argument("name", nargs="?")
    s.add_argument("--by", help="deadline, HH:MM in 24-hour time")
    s.add_argument("--days", default="daily", help="daily, weekdays, weekends, or mon,tue,...")
    s.add_argument("--every", type=parse_duration, help="maximum gap between receipts, e.g. 6h")
    s.add_argument("--grace", type=parse_duration, help="how late still counts as on time, e.g. 10m")
    s.add_argument("--tz", help="time zone for --by, e.g. America/New_York (default: system local time)")
    s.add_argument("--remove", action="store_true")
    s = sub.add_parser("add")
    s.add_argument("name")
    s.add_argument("--evidence", action="append", required=True, metavar="KEY=VALUE",
                   help="something checkable that proves the work happened; repeatable")
    s.add_argument("--note")
    s.add_argument("--check", metavar="CMD", help="shell command that must exit 0 for the receipt to be recorded")
    s.add_argument("--check-timeout", type=float, default=60)
    s = sub.add_parser("check")
    s.add_argument("--alert", metavar="CMD")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("run")
    s.add_argument("name")
    s.add_argument("--outcome", required=True, choices=["ok", "failed", "interrupted", "skipped"])
    s.add_argument("--source", default="cli")
    s.add_argument("--detail", action="append", default=[], metavar="KEY=VALUE")
    s = sub.add_parser("log")
    s.add_argument("name")
    s.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()
    if a.op == "expect" and a.remove and not a.name:
        ap.error("expect --remove needs NAME")
    sys.exit({"expect": cmd_expect, "add": cmd_add, "run": cmd_run, "check": cmd_check, "log": cmd_log}[a.op](a))


if __name__ == "__main__":
    main()
