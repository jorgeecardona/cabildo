import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import { brokerSocket, cabildoCommand, home, hostSocket } from "../src/paths";

test("environment variables override the sockets", () => {
  assert.equal(brokerSocket({ CABILDO_HOME: "/h" }), "/h/bus.sock");
  assert.equal(hostSocket({ CABILDO_HOME: "/h" }), "/h/host.sock");
  assert.equal(brokerSocket({ CABILDO_BROKER_SOCKET: "/b" }), "/b");
  assert.equal(hostSocket({ CABILDO_HOST_SOCKET: "/s" }), "/s");
});

test("home falls back to the old folder only when the new one is missing", (t) => {
  const user = fs.mkdtempSync(path.join(os.tmpdir(), "cab-"));
  t.after(() => fs.rmSync(user, { recursive: true }));
  assert.equal(home({}, user), path.join(user, ".cabildo"));
  fs.mkdirSync(path.join(user, ".agent-chat"));
  assert.equal(home({}, user), path.join(user, ".agent-chat"));
});

test("the command is the setting, then the daemon's Python, then PATH", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cab-"));
  t.after(() => fs.rmSync(dir, { recursive: true }));
  const env = { CABILDO_HOME: dir };
  assert.deepEqual(cabildoCommand("", env), { path: "cabildo", args: [] });
  fs.writeFileSync(path.join(dir, "daemon.json"), JSON.stringify({ exe: process.execPath }));
  assert.deepEqual(cabildoCommand("", env), { path: process.execPath, args: ["-m", "cabildo.cli"] });
  assert.deepEqual(cabildoCommand("/x/cabildo", env), { path: "/x/cabildo", args: [] });
});
