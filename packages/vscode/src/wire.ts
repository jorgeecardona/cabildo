/** One JSON line out, one JSON line back, over a unix socket. */

import * as net from "node:net";

const LINE_LIMIT = 8 * 1024 * 1024; // bus.MAX_LINE

export function askLine(socketPath: string, request: unknown, timeoutMs = 5000): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath);
    const chunks: Buffer[] = [];
    let size = 0;
    const fail = (error: Error): void => {
      socket.destroy();
      reject(error);
    };
    const timer = setTimeout(() => fail(new Error(`no answer from ${socketPath}`)), timeoutMs);
    socket.on("connect", () => socket.write(JSON.stringify(request) + "\n"));
    socket.on("data", (chunk: Buffer) => {
      const end = chunk.indexOf(0x0a);
      chunks.push(end === -1 ? chunk : chunk.subarray(0, end));
      size += chunk.length;
      if (end !== -1) {
        socket.destroy();
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
        } catch {
          reject(new Error(`bad answer from ${socketPath}`));
        }
      } else if (size > LINE_LIMIT) {
        fail(new Error(`answer from ${socketPath} is too large`));
      }
    });
    socket.on("error", fail);
    socket.on("close", () => {
      clearTimeout(timer);
      reject(new Error(`${socketPath} closed without an answer`));
    });
  });
}
