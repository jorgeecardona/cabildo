/** The `cabildo panel` process: its lines in, commands out. No `vscode` import, for tests. */

import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import * as os from "node:os";
import * as readline from "node:readline";

import { type Command, type Env, sessionEnv } from "./paths";
import type { Frame, Opened, PanelCommand, Scope } from "./types";

const KEPT_ERRORS = 4096;

export type FeedEvent =
  | Frame
  | Opened
  | { readonly type: "unavailable"; readonly reason: string };

/** Why the process ended, in words a person can act on. */
export function why(stderr: string, fallback: string): string {
  if (stderr.includes("invalid choice: 'panel'")) {
    return "this cabildo has no `panel` command: upgrade cabildo";
  }
  return stderr.trim().split("\n").pop() || fallback;
}

/** What the process is told again each time it starts. */
interface Wanted {
  readonly folder: string;
  readonly scope: Scope;
  readonly selected: string;
  readonly visible: boolean;
}

/**
 * Keeps one `cabildo panel` process running between `start` and `stop`. A process that ends is
 * reported as `unavailable` and started again after `retryMs`. It runs in the home folder, so
 * nothing in the open folder is imported.
 */
export class PanelFeed {
  private wanted: Wanted | undefined;
  private child: ChildProcessWithoutNullStreams | undefined;
  private retry: NodeJS.Timeout | undefined;

  constructor(
    private readonly command: () => Command,
    private readonly env: Env,
    private readonly listen: (event: FeedEvent) => void,
    private readonly retryMs = 10_000,
  ) {}

  get running(): boolean {
    return this.wanted !== undefined;
  }

  /** Start, or start again, for `folder` (empty: the home folder). */
  start(folder: string, scope: Scope): void {
    const visible = this.wanted?.visible ?? true;
    this.stop();
    this.wanted = { folder, scope, selected: "", visible };
    this.launch();
  }

  send(command: PanelCommand): void {
    if (!this.wanted) {
      return;
    }
    switch (command.cmd) {
      case "select":
        this.wanted = { ...this.wanted, selected: command.id };
        break;
      case "scope":
        this.wanted = { ...this.wanted, scope: command.scope, selected: "" };
        break;
      case "visible":
        this.wanted = { ...this.wanted, visible: command.visible };
        break;
      case "open":
        break;
    }
    this.write(command);
  }

  stop(): void {
    clearTimeout(this.retry);
    this.wanted = undefined;
    const child = this.child;
    this.child = undefined;
    child?.stdin.end(); // the process ends when its stdin closes
    child?.kill();
  }

  private write(command: PanelCommand): void {
    if (this.child?.stdin.writable) {
      this.child.stdin.write(`${JSON.stringify(command)}\n`);
    }
  }

  private launch(): void {
    const wanted = this.wanted!;
    const command = this.command();
    const child = spawn(
      command.path,
      [
        ...command.args,
        "panel",
        "--cwd",
        wanted.folder || os.homedir(),
        "--scope",
        wanted.scope,
      ],
      { cwd: os.homedir(), env: sessionEnv(this.env) },
    );
    this.child = child;
    let errors = "";
    let ended = false;
    const end = (fallback: string): void => {
      if (ended || this.child !== child) {
        return; // stopped, or replaced by a newer one
      }
      ended = true;
      this.child = undefined;
      this.listen({ type: "unavailable", reason: why(errors, fallback) });
      this.retry = setTimeout(() => this.launch(), this.retryMs);
    };
    child.stderr.on("data", (data: Buffer) => {
      errors = (errors + data.toString()).slice(-KEPT_ERRORS);
    });
    child.stdin.on("error", () => {}); // it ended: `close` reports it
    child.on("error", (error) => end(error.message));
    child.on("close", (code) => end(`cabildo panel ended (${code})`));
    readline.createInterface({ input: child.stdout }).on("line", (line) => {
      if (this.child !== child) {
        return;
      }
      let message: unknown;
      try {
        message = JSON.parse(line);
      } catch {
        return; // not one of ours
      }
      const type = (message as { type?: unknown } | null)?.type;
      if (type === "frame" || type === "open") {
        this.listen(message as Frame | Opened);
      }
    });
    if (wanted.selected) {
      this.write({ cmd: "select", id: wanted.selected });
    }
    if (!wanted.visible) {
      this.write({ cmd: "visible", visible: false });
    }
  }
}
