# Evaluation

Each tool was compared with the shell commands an agent would otherwise run to get the same answer.
The tests used 80 real repositories (web apps, hackathon projects, ML experiments, monorepos, and a few non-code
folders), opened read-only, plus scripted reproductions of common dev-server failures. Token counts are estimated as
characters divided by four.

| | pw | peek | doctor |
|---|---|---|---|
| Replaces | `cmd &`, `sleep`, `curl`, `kill` | `cat`, `head`, repeated `jq` | about 8 orientation commands |
| Correctness | 4 of 4 scenarios (baseline: 0 of 4) | matched `jq` on spot checks | 7 of 8 sampled findings confirmed |
| Output size | 150 to 400 tokens | median 828, max 2,877 tokens | median 137 tokens (baseline: 822) |
| Speed | returns as soon as ready | under 0.5 s per file | median 0.18 s, under 4 s worst case |
| Verdict | build first | build second | fold into something larger |

## pw

Four scenarios, each run once with the usual `cmd & sleep N; curl` pattern and once with `pw start`.

| Scenario | Baseline | pw |
|---|---|---|
| Server takes 3 s to start | `curl` after `sleep 2` is refused, and the agent concludes the server is broken | waits, then reports `READY in 3.3s` with the port read from the log |
| Server crashes on startup | stderr is gone; the agent has to rerun in the foreground | reports the exit code and the error line |
| Previous instance still holds the port | the new build dies with `EADDRINUSE`, the old one answers `200`, and the agent tests stale code | refuses to start and names the process holding the port |
| Server started through a wrapper, as with `npm run dev` | `kill $!` stops the shell, and the child keeps the port | stops the process group and confirms the port is free |

The third row matters most. The agent gets a successful response, has no reason to doubt it, and reports a fix that was
never running. This happens often in edit-and-restart loops, and an agent can't detect it from the output it sees.

Some agent harnesses can already run commands in the background, but readiness checks, port-conflict detection and
process state that survives between sessions aren't standard features. The bigger obstacle is discovery: an agent that doesn't know it
tested stale code won't search for a fix, so pw has to be installed by the developer, through a project instructions file or
a plugin.

## peek

The 60 largest data and log files in the corpus, up to 75 MB each, add up to 122.7M tokens. peek's summaries of all 60
total 64k tokens. A 35 MB JSON snapshot became a 666-token summary that agreed with `jq` on structure and counts.

Compared with `cat`, the savings are enormous, but that isn't a fair baseline, since most agents already truncate large
reads. Compared with a careful `head -c` followed by a few `jq` queries, peek takes one call instead of three to five. It also
reports things those queries rarely show: the set of values a field takes, how often an optional field is present, numeric
ranges, and whether an object is a record or a map keyed by IDs.

The first version printed sample rows from a SQLite database that included a password column. Any tool that puts data in
front of an agent should redact credentials. peek now masks values by field name and by known key formats (OpenAI, Stripe,
GitHub, Slack, AWS, Google, JWTs, private keys). It can still miss a secret stored in a generic key/value table under an
unremarkable column name, unless the value itself has a recognizable format.

Not yet supported: Parquet, Excel, and compressed files.

## doctor

doctor ran on all 80 repositories without errors. It found dev, build, or test commands in 54. Most of the other 26 had
no manifest at all. It split 17 folders containing several projects (such as `client/` and `server/`) into separate reports.

Its useful findings were the ones no README can provide, because they describe this machine:

- 29 repositories had no dependencies installed
- 3 had a toolchain version mismatch with `.nvmrc`, `engines.node`, or `.python-version`
- 2 had lockfiles from two different package managers
- several used a package manager or compiler that wasn't installed

Environment variable detection was the weakest part. On one repository, the first version reported 52 required
variables. Most came from generated files, tests, or reads with a default value. After it learned to respect
`.gitignore`, skip tests, recognize fallbacks (`??`, `||`, optional chaining), flag comparisons, and commented-out example
entries, it reported 4. About half of those are arguably optional. This part will always be heuristic.

doctor costs about a sixth of the tokens of manual orientation, but it doesn't replace reading the README, which explains
why a project is set up the way it is. Its list of commands overlaps with what agents already write into project instruction
files. Each new ecosystem adds rules to maintain. It works best as the first step of a broader onboarding workflow.

## Limitations

- All 80 repositories came from one machine and lean toward Node and TypeScript.
- The pw scenarios are reproductions, not samples from real agent sessions.
- These results measure output size and correctness of individual calls. They don't measure whether agents complete
  real tasks faster or more reliably, which would need an A/B test on a task suite.

## Reproducing

```bash
python3 bench/bench_peek.py path/to/repos
python3 bench/bench_doctor.py path/to/repos
```

The pw scenarios use the scripts in `fixtures/`.
