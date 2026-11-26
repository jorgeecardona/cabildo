import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { test, type TestContext } from "node:test";

import { endSession, fetchStatus, HUMAN, listSessions, markRead, sendMessage } from "../src/client";

/** A unix socket that answers each request line with `answer(request)`, split in two writes. */
async function fakeSocket(t: TestContext, answer: (request: any) => unknown) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cab-"));
  const address = path.join(dir, "s.sock");
  const requests: any[] = [];
  const server = net.createServer((conn) => {
    conn.once("data", (data) => {
      const request = JSON.parse(data.toString());
      requests.push(request);
      const line = JSON.stringify(answer(request)) + "\n";
      conn.write(line.slice(0, 5));
      setImmediate(() => conn.end(line.slice(5)));
    });
  });
  await new Promise<void>((resolve) => server.listen(address, resolve));
  t.after(() => {
    server.close();
    fs.rmSync(dir, { recursive: true });
  });
  return { address, requests };
}

test("status asks the broker for agents and the person's unread messages", async (t) => {
  const broker = await fakeSocket(t, (r) => ({
    ok: true,
    result: r.op === "list_agents" ? [{ display: "@a" }] : [{ id: 1 }, { id: 2 }],
  }));
  const status = await fetchStatus(broker.address);
  assert.equal(status.kind, "online");
  assert.ok(status.kind === "online");
  assert.equal(status.agents.length, 1);
  assert.equal(status.unread.length, 2);
  assert.deepEqual(broker.requests.map((r) => r.op).sort(), ["inbox", "list_agents"]);
  assert.deepEqual(broker.requests[0].identity, HUMAN);
});

test("status is offline when the broker refuses", async (t) => {
  const broker = await fakeSocket(t, () => ({ ok: false, error: { message: "nope" } }));
  assert.deepEqual(await fetchStatus(broker.address), { kind: "offline", reason: "nope" });
});

test("status is offline when nothing listens", async () => {
  const status = await fetchStatus(path.join(os.tmpdir(), "cabildo-absent.sock"));
  assert.equal(status.kind, "offline");
});

test("sessions come from the host's list", async (t) => {
  const host = await fakeSocket(t, () => ({ ok: true, sessions: [{ id: "ab" }] }));
  assert.deepEqual(await listSessions(host.address), [{ id: "ab" }]);
  assert.deepEqual(host.requests, [{ op: "list" }]);
});

test("no host means no sessions", async () => {
  assert.deepEqual(await listSessions(path.join(os.tmpdir(), "cabildo-absent.sock")), []);
});

test("a host that refuses is an error", async (t) => {
  const host = await fakeSocket(t, () => ({ ok: false, error: "unknown op" }));
  await assert.rejects(listSessions(host.address), /unknown op/);
});

test("sending and marking read are broker operations of the person", async (t) => {
  const broker = await fakeSocket(t, () => ({ ok: true, result: {} }));
  await sendMessage(broker.address, "s1", "hello");
  await markRead(broker.address, [4, 5]);
  await markRead(broker.address, "all");
  assert.deepEqual(
    broker.requests.map((r) => [r.op, r.params]),
    [
      ["send", { to_session: "s1", body: "hello" }],
      ["mark_read", { message_ids: [4, 5] }],
      ["mark_read", { all_unread: true }],
    ],
  );
  assert.deepEqual(broker.requests[0].identity, HUMAN);
});

test("ending a session asks the host, and reports its refusal", async (t) => {
  const host = await fakeSocket(t, () => ({ ok: false, error: "no hosted session 'x'" }));
  await assert.rejects(endSession(host.address, "x"), /no hosted session/);
  assert.deepEqual(host.requests, [{ op: "kill", id: "x" }]);
});
