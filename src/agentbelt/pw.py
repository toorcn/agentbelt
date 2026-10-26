#!/usr/bin/env python3
"""pw: run long-lived processes (dev servers, watchers) the way an agent needs them.

Usage:
  pw start NAME [--port P] [--ready REGEX] [--timeout S] -- CMD...
      Start CMD detached and block until it is ready, has crashed, or timed out.
  pw status [NAME]
      Whether each process is alive and listening, plus errors logged since the last check.
  pw logs NAME [--tail N] [--grep RE] [--new]
      Recent output; --new returns only lines since the previous `logs --new`.
  pw stop NAME | --all
      SIGTERM the whole process group, then SIGKILL after 5 seconds.
  pw restart NAME

A process is ready when its port accepts connections or REGEX appears in its output.
Without --port, the port is taken from the first localhost URL it prints.
State is kept in $PW_HOME, or ./.pw by default.
"""
import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time

HOME = os.environ.get("PW_HOME", os.path.join(os.getcwd(), ".pw"))
URL_RX = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]):(\d{2,5})")
ERR_RX = re.compile(r"\b(error|exception|traceback|fatal|failed|EADDRINUSE|ECONNREFUSED|panic|unhandled)\b", re.I)


def state_path(name):
    return os.path.join(HOME, f"{name}.json")


def log_path(name):
    return os.path.join(HOME, f"{name}.log")


def load(name):
    try:
        with open(state_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(st):
    os.makedirs(HOME, exist_ok=True)
    with open(state_path(st["name"]), "w") as f:
        json.dump(st, f, indent=1)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # reap if it's our zombie child (only possible within `start`)
    try:
        r, _ = os.waitpid(pid, os.WNOHANG)
        return r == 0
    except ChildProcessError:
        return True


def listening(port):
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            continue
    return False


def port_owner(port):
    try:
        cmd = ["lsof", "+c", "0", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
        lines = out.strip().splitlines()[1:]
        if lines:
            parts = lines[0].split()
            return f"{parts[0]} (pid {parts[1]})"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown process"


def tail(path, n, grep=None, offset=0):
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    end = offset + len(data)
    lines = data.decode("utf-8", "replace").splitlines()
    lines = [strip_ansi(l) for l in lines]
    if grep:
        rx = re.compile(grep, re.I)
        lines = [l for l in lines if rx.search(l)]
    lines = [l for l in lines if l.strip()]
    return lines[-n:], end


def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\r", "", s)


def display_path(path):
    rel = os.path.relpath(path)
    return path if rel.startswith("..") else rel


def ago(t):
    d = int(time.time() - t)
    return f"{d}s" if d < 120 else f"{d // 60}m" if d < 7200 else f"{d // 3600}h"


def describe(st, lines=6):
    pid = st["pid"]
    up = alive(pid)
    port = st.get("port")
    lst = listening(port) if port and up else False
    head = f"{st['name']}: {'running' if up else 'STOPPED'} pid {pid}, up {ago(st['started'])}"
    if port:
        head += f", http://localhost:{port} {'listening' if lst else 'NOT listening'}"
    if not up and st.get("exit") is not None:
        head += f", exit code {st['exit']}"
    out = [head, f"  cmd: {' '.join(st['cmd'])}  (cwd {display_path(st['cwd'])})"]
    log_lines, _ = tail(log_path(st["name"]), 2000, offset=st.get("seen", 0))
    errs = [l for l in log_lines if ERR_RX.search(l)]
    if errs:
        out.append(f"  {len(errs)} error-looking lines since last `logs --new`, latest:")
        out += ["    " + l[:220] for l in errs[-3:]]
    if lines:
        last, _ = tail(log_path(st["name"]), lines)
        out.append("  last log line:" if len(last) == 1 else f"  last {len(last)} log lines:")
        out += ["    " + l[:220] for l in last]
    return "\n".join(out)


def cmd_start(a):
    if not a.cmd:
        sys.exit("pw start: missing command after --")
    old = load(a.name)
    if old and alive(old["pid"]):
        print(f"{a.name} is already running (use `pw restart {a.name}` or `pw stop {a.name}`)")
        print(describe(old))
        return 0
    if a.port and listening(a.port):
        print(f"FAILED: port {a.port} is already in use by {port_owner(a.port)}; {a.name} was not started")
        return 1
    os.makedirs(HOME, exist_ok=True)
    # plain output, and stop dev servers from opening a browser
    env = dict(os.environ, FORCE_COLOR="0", NO_COLOR="1", BROWSER="none")
    if a.port:
        env.setdefault("PORT", str(a.port))
    t0 = time.time()
    with open(log_path(a.name), "wb") as log:
        # own session, so `stop` can signal the whole process group (npm -> sh -> node)
        proc = subprocess.Popen(a.cmd, cwd=a.cwd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, env=env)
    st = {"name": a.name, "pid": proc.pid, "cmd": a.cmd, "cwd": os.path.abspath(a.cwd), "port": a.port,
          "ready_rx": a.ready, "started": t0, "seen": 0, "exit": None}
    save(st)
    ready_rx = re.compile(a.ready) if a.ready else None
    reason = None
    while time.time() - t0 < a.timeout:
        code = proc.poll()
        if code is not None:
            st["exit"] = code
            save(st)
            lines, _ = tail(log_path(a.name), 25)
            print(f"FAILED: {a.name} exited with code {code} after {time.time() - t0:.1f}s")
            in_use = next((l for l in lines if "EADDRINUSE" in l or "address already in use" in l.lower()), None)
            if in_use:
                p = URL_RX.search(in_use) or re.search(r":(\d{2,5})", in_use)
                print("  cause: port in use" + (f" ({port_owner(int(p.group(1)))})" if p else ""))
            print("  last log lines:")
            print("\n".join("    " + l[:220] for l in lines))
            return 1
        text = "\n".join(tail(log_path(a.name), 200)[0])
        if not st["port"]:
            m = URL_RX.search(text)
            if m:
                st["port"] = int(m.group(1))
                save(st)
        if ready_rx and ready_rx.search(text):
            reason = f"matched /{a.ready}/"
        elif st["port"] and listening(st["port"]) and not ready_rx:
            reason = f"port {st['port']} accepting connections"
        if reason:
            break
        time.sleep(0.25)
    if reason:
        print(f"READY in {time.time() - t0:.1f}s ({reason})")
    else:
        print(f"TIMEOUT: still not ready after {a.timeout}s; process is still running (pw logs {a.name} / pw stop {a.name})")
    st["seen"] = os.path.getsize(log_path(a.name))
    save(st)
    print(describe(st, lines=8))
    return 0 if reason else 2


def names():
    if not os.path.isdir(HOME):
        return []
    return sorted(f[:-5] for f in os.listdir(HOME) if f.endswith(".json"))


def cmd_status(a):
    targets = [a.name] if a.name else names()
    if not targets:
        print("no processes managed here")
        return 0
    for n in targets:
        st = load(n)
        if not st:
            print(f"{n}: unknown")
            continue
        print(describe(st, lines=3 if not a.name else 6))
    return 0


def cmd_logs(a):
    st = load(a.name)
    if not st:
        sys.exit(f"pw: unknown process {a.name}")
    offset = st.get("seen", 0) if a.new else 0
    lines, end = tail(log_path(a.name), a.tail, a.grep, offset)
    if a.new:
        st["seen"] = end
        save(st)
        print(f"{len(lines)} new lines" + (f" matching /{a.grep}/" if a.grep else "") + ":")
    print("\n".join(l[:300] for l in lines))
    return 0


def stop(st):
    pid = st["pid"]
    if not alive(pid):
        return "already stopped"
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if not alive(pid):
            return "stopped (SIGTERM)"
        time.sleep(0.125)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return "killed (SIGKILL after 5s)"


def cmd_stop(a):
    targets = names() if a.all else [a.name]
    for n in targets:
        st = load(n)
        if not st:
            print(f"{n}: unknown")
            continue
        result = stop(st)
        port = st.get("port")
        if port:
            result += f", port {port} " + (f"still in use by {port_owner(port)}" if listening(port) else "free")
        print(f"{n}: {result}")
    return 0


def cmd_restart(a):
    st = load(a.name)
    if not st:
        sys.exit(f"pw: unknown process {a.name}")
    print(f"{a.name}: {stop(st)}")
    a.cmd, a.cwd, a.port, a.ready = st["cmd"], st["cwd"], st.get("port"), st.get("ready_rx")
    for _ in range(20):  # wait for the port to free up
        if not a.port or not listening(a.port):
            break
        time.sleep(0.25)
    return cmd_start(a)


def main():
    argv = sys.argv[1:]
    cmd = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="pw", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="op", required=True)
    s = sub.add_parser("start")
    s.add_argument("name")
    s.add_argument("--port", type=int)
    s.add_argument("--ready", help="regex in output that means ready")
    s.add_argument("--timeout", type=float, default=60)
    s.add_argument("--cwd", default=".")
    s = sub.add_parser("status")
    s.add_argument("name", nargs="?")
    s = sub.add_parser("logs")
    s.add_argument("name")
    s.add_argument("--tail", type=int, default=40)
    s.add_argument("--grep")
    s.add_argument("--new", action="store_true")
    s = sub.add_parser("stop")
    s.add_argument("name", nargs="?")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("restart")
    s.add_argument("name")
    s.add_argument("--timeout", type=float, default=60)
    a = ap.parse_args(argv)
    a.cmd = cmd
    if a.op == "stop" and not (a.name or a.all):
        ap.error("stop needs NAME or --all")
    sys.exit({"start": cmd_start, "status": cmd_status, "logs": cmd_logs, "stop": cmd_stop, "restart": cmd_restart}[a.op](a))


if __name__ == "__main__":
    main()
