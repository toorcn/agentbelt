// Turns an OpenClaw cron_changed event into a runs.jsonl line for `receipt check` (agentbelt).
// Kept free of OpenClaw imports so it can be tested with plain `node --test`.
import { appendFileSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export function runsPath(env = process.env) {
  return join(env.RECEIPT_HOME || join(homedir(), ".receipts"), "runs.jsonl");
}

// ISO time with a numeric offset, since older Python can't parse a trailing "Z".
function stamp(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

export function runRecord(event, nowMs = Date.now()) {
  if (event?.action !== "finished" || !event.jobId) {
    return undefined;
  }
  const outcome = event.status === "error" ? "failed" : event.status === "skipped" ? "skipped" : "ok";
  const detail = {};
  if (event.completionStatus && event.completionStatus !== "succeeded") {
    detail.completion = event.completionStatus;
  }
  if (event.deliveryStatus && event.deliveryStatus !== "not-requested") {
    detail.delivery = event.deliveryStatus;
  }
  if (event.runId) {
    detail.run = event.runId;
  }
  return {
    name: event.job?.name || event.jobId,
    at: stamp(nowMs),
    outcome,
    source: "openclaw",
    detail,
  };
}

export function appendRun(record, path = runsPath()) {
  mkdirSync(dirname(path), { recursive: true });
  appendFileSync(path, JSON.stringify(record) + "\n");
}
