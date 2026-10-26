# agent-tools-lab

Three small command-line tools for coding agents. Each one replaces a sequence of shell commands that agents
commonly get wrong or spend too many tokens on.

- **pw** runs dev servers in the background and reports when they are ready, crashed, or blocked by a port conflict.
- **peek** summarizes large JSON, JSONL, CSV, SQLite, and log files in a few hundred tokens.
- **doctor** reports how a repository is built and run, and what is missing on this machine.

Each tool is a single Python file with no dependencies. They were tested with Python 3.12 on macOS.

## pw

```console
$ python3 pw.py start web -- node server.js
READY in 1.8s (port 3000 accepting connections)
web: running pid 47483, up 1s, http://localhost:3000 listening
  cmd: node server.js  (cwd .)
  last log line:
    ready on http://localhost:3000

$ python3 pw.py start web2 --port 3000 -- node server.js
FAILED: port 3000 is already in use by node (pid 47483); web2 was not started

$ python3 pw.py stop web
web: stopped (SIGTERM), port 3000 free
```

`start` blocks until the port accepts connections, a `--ready` regex matches, the process exits, or `--timeout`
passes. `stop` signals the whole process group, so `npm run dev` and the server it spawns both exit.
`status` and `logs --new` report errors written since the last check.

## peek

```console
$ python3 peek.py runs.json
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
$ python3 doctor.py acme-web
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
