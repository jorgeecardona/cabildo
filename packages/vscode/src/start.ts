/** Starting a hosted session. No `vscode` import, so tests run it under plain Node. */

import { execFile } from "node:child_process";
import * as os from "node:os";

import { type Command, type Env, sessionEnv } from "./paths";

const TIMEOUT_MS = 30_000;

/**
 * Run `cabildo host new` for `folder` and return the new session's id. The program runs in the
 * home folder: `--cwd` says where the session starts, and nothing in `folder` is imported.
 */
export function startSession(command: Command, folder: string, env: Env): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      command.path,
      [...command.args, "host", "new", "--cwd", folder],
      { cwd: os.homedir(), env: sessionEnv(env), timeout: TIMEOUT_MS },
      (error, stdout, stderr) => {
        if (error) {
          reject(new Error(stderr.trim() || error.message));
        } else {
          resolve(stdout.trim());
        }
      },
    );
  });
}
