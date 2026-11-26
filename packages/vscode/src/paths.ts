/** Where cabildo keeps its sockets, and how to run it. The same rules as `store.home()`. */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export type Env = Readonly<Record<string, string | undefined>>;

/** The program and leading arguments that run the cabildo command line. */
export interface Command {
  readonly path: string;
  readonly args: readonly string[];
}

export function home(env: Env, userHome: string = os.homedir()): string {
  const set = env.CABILDO_HOME || env.AGENT_CHAT_HOME;
  if (set) {
    return set;
  }
  const current = path.join(userHome, ".cabildo");
  const legacy = path.join(userHome, ".agent-chat");
  return !fs.existsSync(current) && fs.existsSync(legacy) ? legacy : current;
}

export function brokerSocket(env: Env): string {
  return env.CABILDO_BROKER_SOCKET || path.join(home(env), "bus.sock");
}

export function hostSocket(env: Env): string {
  return env.CABILDO_HOST_SOCKET || path.join(home(env), "host.sock");
}

/**
 * The environment for a session started from the extension host: without the variables that
 * make a child process act as a part of VS Code, and with the type of the terminal tab.
 */
export function sessionEnv(env: Env): Record<string, string> {
  const kept: Record<string, string> = {};
  for (const [name, value] of Object.entries(env)) {
    if (value !== undefined && !/^(VSCODE_|ELECTRON_)/.test(name)) {
      kept[name] = value;
    }
  }
  return { ...kept, TERM: "xterm-256color", COLORTERM: "truecolor" };
}

/**
 * How to run cabildo: the `cabildo.command` setting, else the Python that runs the daemon
 * (`daemon.json`), else `cabildo` on `PATH`. The extension host's `PATH` often lacks the
 * virtualenv, which is why the daemon's Python comes first. `-P` keeps Python from importing a
 * `cabildo` folder of the current directory.
 */
export function cabildoCommand(configured: string, env: Env): Command {
  if (configured) {
    return { path: configured, args: [] };
  }
  try {
    const raw: unknown = JSON.parse(fs.readFileSync(path.join(home(env), "daemon.json"), "utf8"));
    const exe = (raw as { exe?: unknown }).exe;
    if (typeof exe === "string" && fs.existsSync(exe)) {
      return { path: exe, args: ["-P", "-m", "cabildo.cli"] };
    }
  } catch {
    // No daemon has run here: fall through to PATH.
  }
  return { path: "cabildo", args: [] };
}
