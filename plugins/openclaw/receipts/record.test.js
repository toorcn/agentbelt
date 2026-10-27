import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { appendRun, runRecord, runsPath } from "./record.js";

const finished = (extra) => ({ action: "finished", jobId: "job-1", job: { id: "job-1", name: "morning-digest" }, ...extra });
const NOW = Date.UTC(2026, 9, 27, 9, 1, 2, 345);

test("records a finished run under the job name", () => {
  const rec = runRecord(finished({ status: "ok", completionStatus: "succeeded", deliveryStatus: "delivered", runId: "r1" }), NOW);
  assert.deepEqual(rec, {
    name: "morning-digest",
    at: "2026-10-27T09:01:02+00:00",
    outcome: "ok",
    source: "openclaw",
    detail: { delivery: "delivered", run: "r1" },
  });
});

test("keeps the details that explain a missing receipt", () => {
  const rec = runRecord(finished({ status: "ok", completionStatus: "unknown", deliveryStatus: "not-delivered" }), NOW);
  assert.equal(rec.outcome, "ok");
  assert.deepEqual(rec.detail, { completion: "unknown", delivery: "not-delivered" });
  assert.equal(runRecord(finished({ status: "error" }), NOW).outcome, "failed");
  assert.equal(runRecord(finished({ status: "skipped" }), NOW).outcome, "skipped");
});

test("never records error text, which can contain prompt or credential content", () => {
  const rec = runRecord(finished({ status: "error", error: "401 for key sk-live-something", summary: "secret summary" }), NOW);
  assert.ok(!JSON.stringify(rec).includes("sk-live"));
  assert.ok(!JSON.stringify(rec).includes("summary"));
});

test("falls back to the job id and ignores other lifecycle events", () => {
  assert.equal(runRecord({ action: "finished", jobId: "job-2", status: "ok" }, NOW).name, "job-2");
  for (const action of ["added", "updated", "removed", "started", "scheduled"]) {
    assert.equal(runRecord({ action, jobId: "job-1" }, NOW), undefined);
  }
});

test("appends one JSON line per run under RECEIPT_HOME", () => {
  const home = mkdtempSync(join(tmpdir(), "receipts-"));
  const path = runsPath({ RECEIPT_HOME: join(home, "nested") });
  appendRun(runRecord(finished({ status: "ok" }), NOW), path);
  appendRun(runRecord(finished({ status: "error" }), NOW), path);
  const lines = readFileSync(path, "utf8").trim().split("\n").map((l) => JSON.parse(l));
  assert.deepEqual(lines.map((l) => l.outcome), ["ok", "failed"]);
});
