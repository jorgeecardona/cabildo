import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import { attachSession, DATA, EXIT, packFrame, packSize, RESIZE, type ViewerEvent } from "../src/attach";

/** A host that runs `serve` for each connection. */
async function fakeHost(serve: (socket: net.Socket) => void): Promise<[string, () => void]> {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cab-attach-"));
  const socketPath = path.join(dir, "host.sock");
  const server = net.createServer(serve);
  await new Promise<void>((resolve) => server.listen(socketPath, resolve));
  return [
    socketPath,
    () => {
      server.close();
      fs.rmSync(dir, { recursive: true, force: true });
    },
  ];
}

/** Every event until the last one. */
function events(socketPath: string): Promise<ViewerEvent[]> {
  return new Promise((resolve) => {
    const seen: ViewerEvent[] = [];
    attachSession(socketPath, "s1", { rows: 30, cols: 100 }, (event) => {
      seen.push(event);
      if (event.type !== "data") {
        resolve(seen);
      }
    });
  });
}

test("output arrives whole when the frames are cut anywhere, and the exit code ends it", async () => {
  let asked = "";
  const code = Buffer.alloc(4);
  code.writeInt32BE(3);
  const bytes = Buffer.concat([
    Buffer.from('{"ok": true}\n'),
    packFrame(DATA, Buffer.from("héllo")),
    packFrame(DATA, Buffer.from(" world")),
    packFrame(EXIT, code),
  ]);
  const [socketPath, stop] = await fakeHost((socket) => {
    socket.once("data", async (line) => {
      asked = line.toString();
      for (const byte of bytes) {
        socket.write(Buffer.from([byte]));
        await new Promise((resolve) => setImmediate(resolve));
      }
    });
  });
  try {
    const seen = await events(socketPath);
    const output = seen.flatMap((e) => (e.type === "data" ? [e.data] : []));
    assert.equal(Buffer.concat(output).toString("utf8"), "héllo world");
    assert.deepEqual(seen.at(-1), { type: "exit", code: 3 });
    assert.deepEqual(JSON.parse(asked), { op: "attach", id: "s1", rows: 30, cols: 100 });
  } finally {
    stop();
  }
});

test("keys and sizes written before the answer are sent after it", async () => {
  const expected = Buffer.concat([
    packFrame(DATA, Buffer.from("ls\r")),
    packFrame(RESIZE, packSize({ rows: 40, cols: 120 })),
  ]);
  let done: (got: Buffer) => void = () => {};
  const received = new Promise<Buffer>((resolve) => (done = resolve));
  const [socketPath, stop] = await fakeHost((socket) => {
    let got = Buffer.alloc(0);
    socket.once("data", () => {
      socket.write('{"ok": true}\n');
      socket.on("data", (chunk: Buffer) => {
        got = Buffer.concat([got, chunk]);
        if (got.length >= expected.length) {
          done(got);
        }
      });
    });
  });
  try {
    const viewer = attachSession(socketPath, "s1", { rows: 24, cols: 80 }, () => {});
    viewer.write(Buffer.from("ls\r"));
    viewer.resize({ rows: 40, cols: 120 });
    assert.deepEqual(await received, expected);
    viewer.close();
  } finally {
    stop();
  }
});

test("a refusal is the reason", async () => {
  const [socketPath, stop] = await fakeHost((socket) => {
    socket.once("data", () => socket.end('{"ok": false, "error": "no hosted session \'s1\'"}\n'));
  });
  try {
    assert.deepEqual(await events(socketPath), [
      { type: "closed", reason: "no hosted session 's1'" },
    ]);
  } finally {
    stop();
  }
});

test("no host is a reason, not a throw", async () => {
  const seen = await events(path.join(os.tmpdir(), "cab-attach-none.sock"));
  assert.equal(seen.length, 1);
  assert.equal(seen[0]!.type, "closed");
});
