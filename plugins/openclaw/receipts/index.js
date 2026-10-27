// OpenClaw plugin: record when each cron job run finishes, for `receipt check` (agentbelt).
//
// A finished run with status "ok" only means the agent turn ended. The agent still files a
// receipt with `receipt add` once it has verified the work, and `receipt check` reports a run that
// finished without one. Runs go to $RECEIPT_HOME/runs.jsonl, or ~/.receipts/runs.jsonl by default.
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { appendRun, runRecord } from "./record.js";

export default definePluginEntry({
  id: "receipts",
  name: "Receipts",
  description: "Record cron run outcomes so missing work can be told apart from runs that never happened.",
  register(api) {
    api.on("cron_changed", (event) => {
      const record = runRecord(event);
      if (record) {
        appendRun(record);
      }
    });
  },
});
