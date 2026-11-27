import * as assert from "node:assert/strict";
import { test, type TestContext } from "node:test";

import { type FeedEvent, PanelFeed } from "../src/feed";

/** Stands in for cabildo: runs `script` with the arguments it was given. */
function feed(t: TestContext, script: string) {
  const events: FeedEvent[] = [];
  const waiting: (() => void)[] = [];
  const found = new PanelFeed(
    () => ({ path: process.execPath, args: ["-e", script, "--"] }),
    { PATH: process.env.PATH },
    (event) => {
      events.push(event);
      waiting.splice(0).forEach((wake) => wake());
    },
    50,
  );
  t.after(() => found.stop());
  const until = async (count: number): Promise<any[]> => {
    while (events.length < count) {
      await new Promise<void>((resolve) => waiting.push(resolve));
    }
    return events;
  };
  return { found, events, until };
}

// Prints a frame with its arguments, then one for every command it reads.
const ECHO = `
const say = (extra) => console.log(JSON.stringify({ type: "frame", ...extra }));
console.log("not json");
say({ args: process.argv.slice(1) });
require("node:readline").createInterface({ input: process.stdin })
  .on("line", (line) => say({ got: JSON.parse(line) }));
`;

test("the panel is asked for the folder and scope, and gets the commands", async (t) => {
  const { found, until } = feed(t, ECHO);
  found.start("/work/repo", "here");
  assert.deepEqual((await until(1))[0].args, ["panel", "--cwd", "/work/repo", "--scope", "here"]);
  found.send({ cmd: "select", id: "abc" });
  assert.deepEqual((await until(2))[1].got, { cmd: "select", id: "abc" });
});

test("a panel that ends is reported, then started again with what was asked", async (t) => {
  const once = `${ECHO}
require("node:readline").createInterface({ input: process.stdin }).on("line", (line) => {
  if (line.includes("scope")) { console.error("boom"); process.exit(3); }
});`;
  const { found, until } = feed(t, once);
  found.start("/work/repo", "here");
  await until(1);
  found.send({ cmd: "visible", visible: false });
  await until(2);
  found.send({ cmd: "scope", scope: "all" });
  const events = await until(6);
  assert.deepEqual(events.find((e) => e.type === "unavailable"), {
    type: "unavailable",
    reason: "boom",
  });
  const again = events.slice(events.findIndex((e) => e.type === "unavailable") + 1);
  assert.deepEqual((again[0] as any).args.slice(-2), ["--scope", "all"]);
  assert.deepEqual((again[1] as any).got, { cmd: "visible", visible: false });
});

test("an old cabildo is named as the reason", async (t) => {
  const old = `console.error("cabildo: error: argument cmd: invalid choice: 'panel'"); process.exit(2)`;
  const { found, until } = feed(t, old);
  found.start("", "all");
  assert.deepEqual((await until(1))[0], {
    type: "unavailable",
    reason: "this cabildo has no `panel` command: upgrade cabildo",
  });
});

test("a stopped feed says nothing more", async (t) => {
  const { found, events, until } = feed(t, ECHO);
  found.start("/work/repo", "here");
  await until(1);
  found.stop();
  await new Promise((resolve) => setTimeout(resolve, 200));
  assert.equal(events.length, 1);
  assert.equal(found.running, false);
});
