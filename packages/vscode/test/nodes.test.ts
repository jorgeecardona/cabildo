import * as assert from "node:assert/strict";
import { test } from "node:test";

import { agentNodes, messageNodes, messageText, sessionNodes } from "../src/nodes";
import type { Agent, HostSession, Message } from "../src/types";
import { shortDate } from "../src/view";

const session = (id: string, pid: number, cwd: string): HostSession => ({
  id, cwd, pid, started_at: pid, command: ["claude"], rows: 24, cols: 80,
  group: "", session_id: "", adopted: false, parent: "",
});

const agent = (id: string, presence: string, cwd: string, pid: number | null = null): Agent => ({
  session_id: id, display: id, presence, project_name: null, cwd, claude_pid: pid,
});

test("a hosted session is named after the agent its process runs", () => {
  const [named, bare] = sessionNodes(
    [session("a1", 10, "/work/repo"), session("b2", 20, "/work/other")],
    [agent("zippy", "idle", "/work/repo", 10)],
    [],
  ).reverse();
  assert.deepEqual([named!.label, named!.description], ["zippy", "repo · idle"]);
  assert.deepEqual([bare!.label, bare!.description], ["other", "b2"]);
});

test("agents of the open folder come first, then working ones", () => {
  const nodes = agentNodes(
    [
      agent("away", "working", "/other"),
      agent("quiet", "idle", "/work/repo", 10),
      agent("busy", "working", "/work/repo/sub"),
    ],
    [session("a1", 10, "/work/repo")],
    ["/work/repo"],
  );
  assert.deepEqual(nodes.map((n) => n.label), ["busy", "quiet", "away"]);
  assert.equal(nodes[1]!.session?.id, "a1");
  assert.equal(nodes[0]!.session, undefined);
  assert.equal(nodes[2]!.description, "other · working");
});

test("messages are listed newest first, and open with the whole body", () => {
  const message = (id: number): Message => ({
    id, from_session: "s", from_display: "@a", topic: "general", body: `one\ntwo ${id}`,
    urgent: true, created_at: 0,
  });
  const now = new Date(0);
  const nodes = messageNodes([message(1), message(2)], now);
  const sent = shortDate(0, now);
  assert.deepEqual(nodes.map((n) => n.description), [`${sent} · one two 2`, `${sent} · one two 1`]);
  assert.match(messageText(message(3)), /^@a to #general · .* · urgent\n\none\ntwo 3\n$/);
});
