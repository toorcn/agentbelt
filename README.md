# agentbelt

Four small command-line tools for agents. Each one replaces a sequence of shell commands that agents commonly get
wrong or spend too many tokens on, or catches a failure that agents don't report.

- **pw** runs dev servers in the background and reports when they are ready, crashed, or blocked by a port conflict.
- **peek** summarizes large JSON, JSONL, CSV, SQLite, and log files in a few hundred tokens.
- **doctor** reports how a repository is built and run, and what is missing on this machine.
- **receipt** notices when scheduled agent work, such as a daily digest or a backup, silently stops happening.

## Install

```bash
pip install .
```

This installs the `pw`, `peek`, `doctor` and `receipt` commands. They need Python 3.9 or newer and nothing else.
Each tool is also a standalone file, so `python3 src/agentbelt/peek.py data.json` works without installing.
Tested on macOS with Python 3.9, 3.11, 3.12, 3.13 and 3.14.

## pw

```console
$ pw start web -- node server.js
READY in 1.8s (port 3000 accepting connections)
web: running pid 47483, up 1s, http://localhost:3000 listening
  cmd: node server.js  (cwd .)
  last log line:
    ready on http://localhost:3000

$ pw start web2 --port 3000 -- node server.js
FAILED: port 3000 is already in use by node (pid 47483); web2 was not started

$ pw stop web
web: stopped (SIGTERM), port 3000 free
```

`start` blocks until the port accepts connections, a `--ready` regex matches, the process exits, or `--timeout`
passes. `stop` signals the whole process group, so `npm run dev` and the server it spawns both exit.
`status` and `logs --new` report errors written since the last check.

## peek

```console
$ peek runs.json
runs.json  [json, 1.6MB]
$: object
  runs: array (len 20000, schema from first 5000)
    []: object
      id: str  e.g. run_0000, run_0001  unique
      status: str  enum{failed, passed, queued}
      durationMs: int  range 89…8997  e.g. 1113, 8197
      retries: int  range 0…2  e.g. 1, 2
      error: str  enum{TimeoutError: page.goto exceeded 30000ms}  (in 715/5000)
```

Output stays under about 3k tokens regardless of file size. Use `--path 'runs[0]'` to drill into a subtree.
For logs, it reports level counts, test summaries (TAP, pytest, Jest/Vitest, Go), failing tests, and the most
repeated line patterns. Values that look like credentials are redacted by field name and by format.

## doctor

```console
$ doctor acme-web
## acme-web
stack: node, next.js@15.1.0, react@19.0.0, typescript@5.7.2, drizzle@0.38.0
toolchain:
  BAD pnpm: have MISSING, want 9.12.0 (packageManager)
  ok  node: have 22.16.0, want >=22.12 (engines.node)
commands:
  install   pnpm install
  dev       pnpm dev
  build     pnpm build
  test      pnpm test
  lint      pnpm lint
  other scripts: db:migrate
services: script `dev` serves on port 3001
env: files (none); examples .env.example; 3 vars read in code
problems:
  [error] pnpm is not installed
          fix: corepack enable  (or npm i -g pnpm)
  [error] dependencies not installed (no node_modules)
          fix: pnpm install
  [warn] 2 env vars required but not set: DATABASE_URL (src/config.ts), STRIPE_SECRET_KEY (src/config.ts)
          fix: cp .env.example .env.local and fill in values
note: unset but with a fallback in code (1): APP_REGION
```

doctor is read-only. It never installs anything or runs project code, and it reports environment variable names
but never their values. `--json` gives the same report as structured output.

## receipt

A scheduled agent run can end with a normal reply when the work never happened, because the context ran out, a
tool call timed out, or a message wasn't delivered. Process and container health checks pass. receipt checks the
outcome instead: the agent files a receipt with evidence after it verifies the work, and a watcher run from cron
outside the agents reports any receipt that doesn't arrive on time.

```console
$ receipt expect morning-digest --by 09:05 --grace 10m
morning-digest: by 09:05 daily, grace 10m

$ receipt add morning-digest --evidence message_id=1729 --evidence items=14
recorded morning-digest at 08:58, on time for today 09:05

$ receipt add feed-sync --evidence rows=1 --check "test -s /nonexistent/out.json"
NOT RECORDED: check exited 1: test -s /nonexistent/out.json

$ receipt check
ok       feed-sync       last 2h ago
MISSING  morning-digest  due today 09:05, no receipt (last receipt yesterday 08:58)
LATE     nightly-backup  due today 03:30, done today 04:12 (42m late); next due tomorrow 03:30
```

Expectations are a daily or weekly deadline (`--by HH:MM --days weekdays`) or a maximum gap (`--every 6h`).
`check` exits 1 when anything is missing, late or stale. `check --alert CMD` runs CMD once per missed deadline and
once on recovery, so it can run every few minutes from cron without repeating alerts.

[skills/receipts/SKILL.md](skills/receipts/SKILL.md) is an agent skill that teaches agents when to file a receipt,
and when not to: never before the result is verified, and never for partial work. It works with any harness that
loads `SKILL.md` files.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

The suite runs in about 15 seconds and covers credential redaction across every input format, doctor's version and
environment-variable logic (including that it never writes to the repository), the pw failure scenarios from the
evaluation, and receipt's deadline rules (late receipts, weekends, daylight saving time) and alert deduplication.

## Evaluation

[EVALUATION.md](EVALUATION.md) compares each tool with the shell commands it replaces, using 80 real repositories
and a set of process failure scenarios. To rerun the benchmarks on your own folder of repositories:

```bash
python3 bench/bench_peek.py path/to/repos
python3 bench/bench_doctor.py path/to/repos
```

## Status

Research prototypes. The interfaces may change.

## License

[MIT](LICENSE)
