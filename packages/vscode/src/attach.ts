/**
 * A viewer of one hosted session: the `attach` op of the session host (`host.py`). One JSON line
 * out, one JSON line back, then frames of one byte of kind, four bytes of length and the payload.
 * No `vscode` import, so tests run it under plain Node.
 */

import * as net from "node:net";

export const DATA = 0;
export const RESIZE = 1;
export const EXIT = 2;

const HEADER = 5;
const LINE_LIMIT = 1024 * 1024;
const FRAME_LIMIT = 16 * 1024 * 1024;

export interface Size {
  readonly rows: number;
  readonly cols: number;
}

/** What a viewer hears. `exit` and `closed` are the last event; only one of them comes. */
export type ViewerEvent =
  | { readonly type: "data"; readonly data: Buffer }
  | { readonly type: "exit"; readonly code: number }
  | { readonly type: "closed"; readonly reason: string };

export interface Viewer {
  /** Type into the session's terminal. */
  write(data: Buffer): void;
  resize(size: Size): void;
  /** Detach: the session keeps running. No event follows. */
  close(): void;
}

export function packFrame(kind: number, payload: Buffer): Buffer {
  const head = Buffer.alloc(HEADER);
  head.writeUInt8(kind, 0);
  head.writeUInt32BE(payload.length, 1);
  return Buffer.concat([head, payload]);
}

export function packSize({ rows, cols }: Size): Buffer {
  const payload = Buffer.alloc(4);
  payload.writeUInt16BE(clamp(rows), 0);
  payload.writeUInt16BE(clamp(cols), 2);
  return payload;
}

function clamp(value: number): number {
  return Math.max(1, Math.min(65535, Math.floor(value)));
}

/** Attach to session `id`. What is written before the host answers is sent after it does. */
export function attachSession(
  socketPath: string,
  id: string,
  size: Size,
  on: (event: ViewerEvent) => void,
): Viewer {
  const socket = net.createConnection(socketPath);
  let buffer: Buffer = Buffer.alloc(0);
  let accepted = false;
  let over = false;
  const waiting: Buffer[] = [];

  const finish = (event?: ViewerEvent): void => {
    if (over) {
      return;
    }
    over = true;
    socket.destroy();
    if (event) {
      on(event);
    }
  };
  const closed = (reason: string): void => finish({ type: "closed", reason });
  const send = (frame: Buffer): void => {
    if (over) {
      return;
    }
    if (accepted) {
      socket.write(frame);
    } else {
      waiting.push(frame);
    }
  };

  /** Take the host's answer off the front of the buffer. False until it is complete. */
  const readAnswer = (): boolean => {
    const end = buffer.indexOf(0x0a);
    if (end === -1) {
      if (buffer.length > LINE_LIMIT) {
        closed("the session host's answer is too large");
      }
      return false;
    }
    let reply: { ok?: boolean; error?: string };
    try {
      reply = JSON.parse(buffer.subarray(0, end).toString("utf8")) as typeof reply;
    } catch {
      closed("the session host's answer is not JSON");
      return false;
    }
    if (!reply.ok) {
      closed(reply.error ?? "the session host refused");
      return false;
    }
    buffer = buffer.subarray(end + 1);
    accepted = true;
    for (const frame of waiting.splice(0)) {
      socket.write(frame);
    }
    return true;
  };

  const readFrames = (): void => {
    while (!over && buffer.length >= HEADER) {
      const kind = buffer.readUInt8(0);
      const length = buffer.readUInt32BE(1);
      if (length > FRAME_LIMIT) {
        closed("the session host sent a frame that is too large");
        return;
      }
      if (buffer.length < HEADER + length) {
        return;
      }
      const payload = buffer.subarray(HEADER, HEADER + length);
      buffer = buffer.subarray(HEADER + length);
      if (kind === DATA) {
        on({ type: "data", data: payload });
      } else if (kind === EXIT) {
        finish({ type: "exit", code: payload.length === 4 ? payload.readInt32BE(0) : -1 });
      }
    }
  };

  socket.on("connect", () => socket.write(JSON.stringify({ op: "attach", id, ...size }) + "\n"));
  socket.on("data", (chunk: Buffer) => {
    buffer = buffer.length ? Buffer.concat([buffer, chunk]) : chunk;
    if (accepted || readAnswer()) {
      readFrames();
    }
  });
  socket.on("error", (error) => closed(error.message));
  socket.on("close", () => closed("the session host closed the connection"));

  return {
    write: (data) => send(packFrame(DATA, data)),
    resize: (next) => send(packFrame(RESIZE, packSize(next))),
    close: () => finish(),
  };
}
