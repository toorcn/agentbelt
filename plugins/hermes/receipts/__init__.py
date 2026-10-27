"""Hermes plugin: record when each cron run ends, for `receipt check` (agentbelt).

Install by copying this folder to ~/.hermes/plugins/receipts and running `hermes plugins enable
receipts`. Runs are appended to $RECEIPT_HOME/runs.jsonl, or ~/.receipts/runs.jsonl by default,
under the cron job's name. Chat sessions are ignored.

A run that ends with `completed` only means the agent produced a final reply. The agent still
files a receipt with `receipt add` once it has verified the work, and `receipt check` reports a run
that ended without one.
"""
import json
import os
import re
from datetime import datetime

CRON_SESSION = re.compile(r"^cron_(.+)_\d{8}_\d{6}$")


def runs_path():
    home = os.environ.get("RECEIPT_HOME") or os.path.expanduser("~/.receipts")
    return os.path.join(home, "runs.jsonl")


def outcome(completed, failed, interrupted):
    if interrupted:
        return "interrupted"
    if failed or not completed:
        return "failed"
    return "ok"


def run_record(run, session_id, platform, completed, failed, interrupted, reason, now=None):
    """The runs.jsonl line for one ended turn, or None when the turn isn't a cron run."""
    if run is not None:
        name, detail = run.job_name, {"execution": run.execution_id}
        if run.scheduled_instant is None:
            detail["manual"] = "yes"
    else:
        # Hermes versions without ctx.current_cron_execution(): cron sessions are cron_<job id>_<time>
        m = CRON_SESSION.match(session_id or "") if platform == "cron" else None
        if not m:
            return None
        name, detail = m.group(1), {}
    result = outcome(completed, failed, interrupted)
    if reason and result != "ok":
        detail["reason"] = str(reason)[:80]
    return {"name": name, "at": (now or datetime.now().astimezone()).isoformat(timespec="seconds"),
            "outcome": result, "source": "hermes", "detail": detail}


def register(ctx):
    current_run = getattr(ctx, "current_cron_execution", lambda: None)

    def on_session_end(session_id=None, platform=None, completed=False, failed=False, interrupted=False,
                       turn_exit_reason=None, **kwargs):
        rec = run_record(current_run(), session_id, platform, completed, failed, interrupted, turn_exit_reason)
        if rec is None:
            return
        path = runs_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    ctx.register_hook("on_session_end", on_session_end)
