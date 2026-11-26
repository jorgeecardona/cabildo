/** The workspace's numbers, from `cabildo stats`. No `vscode` import, for tests under Node. */

import { execFile } from "node:child_process";
import * as os from "node:os";

import { type Command, type Env, sessionEnv } from "./paths";
import type { StatGroup, Stats } from "./types";

// The first run on a machine reads every transcript.
const TIMEOUT_MS = 120_000;
const MAX_OUTPUT = 4 * 1024 * 1024;

/** Why `cabildo stats` gave nothing, in words a person can act on. */
function why(stderr: string, fallback: string): string {
  if (stderr.includes("invalid choice: 'stats'")) {
    return "this cabildo has no `stats` command: upgrade cabildo";
  }
  return stderr.trim().split("\n").pop() || fallback;
}

/**
 * Run `cabildo stats --json`. `folder` picks the settings that apply (empty: the home folder).
 * The program runs in the home folder, so nothing in `folder` is imported.
 */
export function fetchStats(command: Command, folder: string, env: Env): Promise<Stats> {
  return new Promise((resolve) => {
    execFile(
      command.path,
      [...command.args, "stats", "--json", "--cwd", folder || os.homedir()],
      { cwd: os.homedir(), env: sessionEnv(env), timeout: TIMEOUT_MS, maxBuffer: MAX_OUTPUT },
      (error, stdout, stderr) => {
        if (error) {
          resolve({ kind: "unavailable", reason: why(stderr, error.message) });
          return;
        }
        try {
          const { groups } = JSON.parse(stdout) as { groups: readonly StatGroup[] };
          resolve({ kind: "online", groups });
        } catch {
          resolve({ kind: "unavailable", reason: "`cabildo stats` did not print JSON" });
        }
      },
    );
  });
}
