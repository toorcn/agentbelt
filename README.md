# agentbelt

[![tests](https://github.com/toorcn/agentbelt/actions/workflows/test.yml/badge.svg)](https://github.com/toorcn/agentbelt/actions/workflows/test.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Four small command-line tools for AI agents. Each one replaces a sequence of shell commands that agents often get
wrong or spend too many tokens on, or catches a failure that agents don't report.

| Tool | Use it to | Instead of |
|---|---|---|
| `pw` | run a dev server and know when it's ready, crashed, or blocked by a stale instance | `cmd &`, `sleep`, `curl`, `kill` |
| `peek` | see the structure of a large JSON, JSONL, CSV, SQLite or log file in a few hundred tokens | `cat`, `head`, repeated `jq` |
| `doctor` | find out how a repository is built and run, and what's missing on this machine | reading manifests, configs and CI files |
| `receipt` | notice when scheduled agent work, like a daily digest, silently stops happening | container health checks |

No dependencies, Python 3.9+, macOS and Linux. Each tool is a single file that also runs without installing.

## Install

```bash
uv tool install git+https://github.com/toorcn/agentbelt
```

or

```bash
pip install git+https://github.com/toorcn/agentbelt
```

To try one tool without installing anything:

```bash
uvx --from git+https://github.com/toorcn/agentbelt peek data.json
```

## Quick start

```bash
pw start web -- npm run dev   # returns once the server answers, or says why it didn't
peek logs/app.log             # levels, failing tests and repeated errors in a large log
doctor                        # how this repo is built, and what's missing here
receipt check                 # scheduled jobs that didn't report their work as done
```

Every tool has `--help`. `peek`, `doctor` and `receipt check` take `--json` for structured output.

## Use with your agent

Agents don't go looking for a tool when they don't know they have a problem. An agent testing a stale server
gets a `200` and moves on. Tell it the tools exist by adding this to your `AGENTS.md`, `CLAUDE.md`, or whatever
instructions file your harness reads:

```markdown
## Command-line tools

- Start dev servers and watchers with `pw start NAME -- CMD`, not `CMD &`. It returns once the server is
  ready and fails if an old instance still holds the port. `pw logs NAME --new` shows new output and
  `pw stop NAME` stops it.
- Before reading a JSON, JSONL, CSV, SQLite or log file larger than about 100 KB, run `peek FILE`.
- In a repository you haven't worked in before, run `doctor` first.
```

For scheduled agents, install the [receipts skill](skills/receipts/SKILL.md), and the
[OpenClaw or Hermes Agent plugin](plugins/README.md) if you use one of those.

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

The [OpenClaw and Hermes Agent plugins](plugins/README.md) record when each cron run ends. With them, a missing
receipt comes with the reason: either a run ended and reported success without doing the work, or no run happened
at all.

## How it compares

- **pm2, Overmind, or your harness's background shell** keep processes running. pw is built for an agent's
  edit-and-restart loop: one call that returns when the server is ready, and fails loudly when an old instance
  still owns the port, which is how agents end up testing code that isn't running.
- **jq and DuckDB** answer questions you already know to ask. peek shows what's in the file, including value
  sets, optional fields and maps keyed by ID, so you know what to ask.
- **A README** explains how a project is meant to be set up. doctor checks this machine against it: toolchain
  versions, installed dependencies, and required environment variables.
- **Healthchecks.io and Cronitor** do the deadline-and-alert part well and integrate with more alert channels.
  If you already use one, have the agent ping it instead of running `receipt add`. receipt is for keeping it
  local, attaching evidence, refusing a receipt when a verification command fails, and, with the plugins,
  telling a run that did no work apart from a run that never happened.
- **rtk** compresses the output of commands an agent already runs. agentbelt replaces multi-step workflows with
  one command. The two work together.

## Evaluation

[EVALUATION.md](EVALUATION.md) compares each tool with the shell commands it replaces, using 80 real repositories
and a set of process failure scenarios. To rerun the benchmarks on your own folder of repositories:

```bash
python3 bench/bench_peek.py path/to/repos
python3 bench/bench_doctor.py path/to/repos
```

## Contributing

Issues and pull requests are welcome. Keep each tool a single file with no dependencies outside the standard
library, so it can be copied onto any machine.

```bash
git clone https://github.com/toorcn/agentbelt && cd agentbelt
PYTHONPATH=src python3 -m unittest discover -s tests
```

The suite runs in under 20 seconds. It covers credential redaction across every input format, doctor's version
and environment-variable logic (including that it never writes to the repository), the pw failure scenarios from
the evaluation, receipt's deadline rules (late receipts, weekends, daylight saving time) and alert deduplication,
and both plugins. The OpenClaw plugin tests need Node and are skipped without it.

## Status

Early, and the interfaces may still change. Not yet on PyPI.

## License

[MIT](LICENSE)
