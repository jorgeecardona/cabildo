import * as assert from "node:assert/strict";
import * as os from "node:os";
import { test } from "node:test";

import { startSession } from "../src/start";

// Stands in for cabildo: prints what it was given, as one line.
const echo = {
  path: process.execPath,
  args: [
    "-e",
    "console.log(JSON.stringify([process.argv.slice(1), process.cwd(), process.env.TERM]))",
    "--",
  ],
};

test("a new session is asked for the folder, from the home folder, with a terminal type", async () => {
  const printed = await startSession(echo, "/work/repo", { PATH: process.env.PATH });
  assert.deepEqual(JSON.parse(printed), [
    ["host", "new", "--cwd", "/work/repo"],
    os.homedir(),
    "xterm-256color",
  ]);
});

test("the error is what cabildo printed", async () => {
  const fail = {
    path: process.execPath,
    args: ["-e", "console.error('cabildo host: command not found: claude'); process.exit(1)"],
  };
  await assert.rejects(startSession(fail, "/w", {}), /command not found: claude/);
});
