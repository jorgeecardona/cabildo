import * as assert from "node:assert/strict";
import { test } from "node:test";

import { statNodes } from "../src/nodes";
import { fetchStats } from "../src/stats";
import type { StatGroup } from "../src/types";

const groups: StatGroup[] = [
  {
    key: "plan",
    title: "max plan",
    rows: [{ label: "7 days", value: "95% used", level: "alert", detail: "resets Sat" }],
  },
];

/** Stands in for cabildo: runs `script` with the arguments it was given. */
const fake = (script: string) => ({ path: process.execPath, args: ["-e", script, "--"] });

test("stats are asked for the folder and read as groups", async () => {
  const echo = fake(
    "console.log(JSON.stringify({ groups: [{ key: process.argv.slice(1).join(' '), " +
      "title: 't', rows: [] }] }))",
  );
  const stats = await fetchStats(echo, "/work/repo", { PATH: process.env.PATH });
  assert.equal(stats.kind, "online");
  assert.equal(
    stats.kind === "online" && stats.groups[0]!.key,
    "stats --json --cwd /work/repo",
  );
});

test("an older cabildo is told to upgrade", async () => {
  const old = fake(
    "console.error(\"cabildo: error: argument cmd: invalid choice: 'stats'\"); process.exit(2)",
  );
  assert.deepEqual(await fetchStats(old, "", {}), {
    kind: "unavailable",
    reason: "this cabildo has no `stats` command: upgrade cabildo",
  });
});

test("output that is not JSON is reported", async () => {
  const stats = await fetchStats(fake("console.log('hello')"), "", {});
  assert.equal(stats.kind, "unavailable");
});

test("a group is a heading with its lines, and the detail is in the tooltip", () => {
  const [plan] = statNodes(groups);
  assert.equal(plan!.label, "max plan");
  assert.deepEqual(
    plan!.rows.map((r) => [r.label, r.description, r.level, r.tooltip]),
    [["7 days", "95% used", "alert", "7 days\n95% used\nresets Sat"]],
  );
});
