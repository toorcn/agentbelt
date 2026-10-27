# Plugins

These plugins record when each scheduled agent run ends, so `receipt check` can explain a missing
receipt. Without them, check can only say the receipt is missing. With them, it also says which of
two things happened:

```console
$ receipt check
MISSING  morning-digest  due today 09:05, no receipt (last receipt yesterday 08:58); a run ended today 09:01 and reported ok (delivery=not-delivered)
MISSING  nightly-backup  due today 03:30, no receipt (last receipt yesterday 03:12); no run recorded since yesterday 15:30
```

The first job ran and its framework reported success, yet the work never happened. The second job
never ran at all, so look at the scheduler. Neither plugin files receipts: a run that reports `ok`
only means the agent turn ended, which is the gap receipts exist to close.

Runs are appended to `$RECEIPT_HOME/runs.jsonl`, or `~/.receipts/runs.jsonl` by default, under the
job's name. Use the same name in `receipt expect`. Only cron runs are recorded, never chat sessions,
and no message content or error text is stored.

## Hermes Agent

The plugin listens to `on_session_end` and reads the job from `ctx.current_cron_execution()`.

```bash
cp -r plugins/hermes/receipts ~/.hermes/plugins/receipts
hermes plugins enable receipts
```

Then restart the gateway with `RECEIPT_HOME` set in its environment.

## OpenClaw

The plugin listens to `cron_changed` events with `action: "finished"`. It records the run status,
completion status and delivery status, using `job.name` or, when a job has no name, its ID.

```bash
openclaw plugins install --link ./plugins/openclaw/receipts --force
openclaw plugins enable receipts
```

Then restart the gateway with `RECEIPT_HOME` set in its environment. Run its tests with `node --test`
from `plugins/openclaw/receipts`.

## Any other scheduler

Record runs from a wrapper script with the CLI:

```bash
receipt run nightly-backup --outcome ok --source systemd --detail exit=0
```

## Agent setup

Agents also need the `receipt` command and the skill that tells them when to file a receipt:

```bash
pip install .                                      # inside each agent container
cp -r skills/receipts ~/.hermes/skills/receipts    # Hermes
cp -r skills/receipts ~/.openclaw/skills/receipts  # OpenClaw
```

Mount the same `RECEIPT_HOME` directory into every container and into the host where
`receipt check` runs from cron.
