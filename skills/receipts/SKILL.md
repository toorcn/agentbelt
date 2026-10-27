---
name: receipts
description: File a receipt with the `receipt` command after finishing scheduled or recurring work (digests, reports, backups, syncs, monitors), so a watcher outside the agent can tell when that work silently stops happening. Use at the end of any scheduled job, and only after verifying the result exists.
---

# Receipts

Your operator watches for work that should have happened and didn't. Scheduled runs can fail without anyone
noticing: the context runs out, a tool call times out, a message is never delivered, yet the run still ends with a
final reply. A receipt is how you say "this work is done, and here is the proof." A watcher outside your container
reports any expected receipt that doesn't arrive on time.

## When to file a receipt

File one at the end of a scheduled or recurring job, once you have confirmed the result exists outside your own
context:

- the message was delivered, and the API returned its ID
- the file was written and isn't empty
- the database rows changed, or the upload returned a URL or ID

Then run:

```bash
receipt add JOB_NAME --evidence KEY=VALUE [--evidence KEY=VALUE] [--note "short summary"]
```

`JOB_NAME` is the name your operator gave the job. Run `receipt expect` to list the names. Evidence is something a
person could check later: a message ID, a file path with its size, a row count, a URL.

```bash
receipt add morning-digest --evidence message_id=1729 --evidence items=14
receipt add nightly-backup --evidence file=/backups/db-2026-10-27.sql.gz --check "test -s /backups/db-2026-10-27.sql.gz"
```

When a shell command can verify the result, pass it with `--check`. The receipt is recorded only if the command exits 0.

## When not to file a receipt

- **The work isn't verified.** If you sent a message but got no confirmation, or you can't check the output, don't
  file one. A missing receipt makes the watcher alert, which is what should happen.
- **You did part of the work.** Do not file a receipt for a partial result. Say what is missing in your reply instead.
- **You are about to try.** A receipt records a result, never an intention or a plan.

If `receipt add` prints `NOT RECORDED`, the check failed. Report that and don't retry with a weaker check.

## Rules for evidence

- Never put secrets, tokens, passwords or personal data in evidence or notes. Evidence is stored in plain text.
- Keep values short. An ID or a path is enough. Don't paste output.
- Use the same keys every time for the same job, so receipts are easy to compare.

## For the operator

Declare each job once, then run the watcher from cron on the host, outside the agents:

```bash
receipt expect morning-digest --by 09:05 --grace 10m
receipt expect nightly-backup --by 03:30 --days weekdays --tz America/New_York
receipt expect feed-sync --every 6h

# crontab: check every 5 minutes, alert once per missed deadline and once on recovery
*/5 * * * * receipt check --alert 'curl -fsS -d @- https://example.com/your-webhook' >/dev/null
```

Agents in containers need the `receipt` command and a shared `RECEIPT_HOME` directory, mounted from the host.
The OpenClaw and Hermes plugins in agentbelt's `plugins/` folder also record when each cron run ends, so a missing
receipt shows whether the run happened.
