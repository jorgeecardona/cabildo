// Probe in a real VS Code window: `pnpm run probe`.
//
// Starts a throwaway session host with one `sh` session, opens VS Code with an empty profile,
// and runs suite.js inside it. The status bar reads the real daemon (read only). The session the
// extension starts runs a script named `claude`, not Claude.
// VSCODE_BINARY names the VS Code program (default /usr/share/code/code).
import { execFileSync, spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
// Unix socket paths are limited to about 100 bytes, so the folder name is short.
const dir = fs.mkdtempSync(path.join(process.env.XDG_RUNTIME_DIR ?? os.tmpdir(), "cabp-"));
const cabildo = process.env.CABILDO_BIN ?? "cabildo";
fs.mkdirSync(path.join(dir, "bin"));
fs.writeFileSync(
  path.join(dir, "bin", "claude"),
  '#!/bin/sh\necho ready; read line; echo "$line" > "$PROBE_DIR/new"; sleep 60\n',
  { mode: 0o755 },
);
const env = {
  ...process.env,
  CABILDO_BIN: cabildo,
  CABILDO_HOST_SOCKET: path.join(dir, "host.sock"),
  PATH: `${path.join(dir, "bin")}:${process.env.PATH}`,
  PROBE_DIR: dir,
};
const host = (...args) =>
  execFileSync(cabildo, ["host", ...args], { env, encoding: "utf8", stdio: "pipe" });

const script = 'echo ready; read line; echo "$line" > got; read line; echo "$line" > got2; sleep 60';
let code = 1;
try {
  const id = host("new", "--cwd", dir, "--", "sh", "-c", script).trim();
  code = await new Promise((resolve) => {
    const child = spawn(
      process.env.VSCODE_BINARY ?? "/usr/share/code/code",
      [
        `--user-data-dir=${dir}/data`,
        `--extensions-dir=${dir}/ext`,
        `--extensionDevelopmentPath=${path.dirname(here)}`,
        `--extensionTestsPath=${path.join(here, "suite.js")}`,
        "--disable-workspace-trust",
        "--force-disable-user-env", // the login shell's PATH would hide the script
        "--skip-welcome",
        "--skip-release-notes",
        dir,
      ],
      { env: { ...env, PROBE_SESSION: id }, stdio: "inherit", timeout: 90_000 },
    );
    child.on("exit", (status) => resolve(status ?? 1));
  });
  const result = path.join(dir, "result.json");
  console.log(fs.existsSync(result) ? fs.readFileSync(result, "utf8") : "no result written");
} finally {
  try {
    host("stop", "--force");
  } catch {
    // The suite stopped it.
  }
  fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
}
console.log(code === 0 ? "probe passed" : `probe failed (${code})`);
process.exit(code);
