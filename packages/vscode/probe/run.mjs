// Probe in a real VS Code window: `pnpm run probe`.
//
// Starts a throwaway session host with one `sh` session and a throwaway broker (seed.py) in a
// temporary cabildo home, opens VS Code with an empty profile, and runs suite.js inside it.
// Nothing reads or writes the real bus. The session the extension starts runs a script named
// `claude`, not Claude.
// CABILDO_BIN is the path of `cabildo`; the `python` beside it runs the broker.
// VSCODE_BINARY names the VS Code program (default /usr/share/code/code).
import { execFileSync, spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
// Unix socket paths are limited to about 100 bytes, so the folder name is short.
const dir = fs.mkdtempSync(path.join(process.env.XDG_RUNTIME_DIR ?? os.tmpdir(), "cabp-"));
const cabildo = process.env.CABILDO_BIN;
if (!cabildo) {
  throw new Error("set CABILDO_BIN to the path of cabildo");
}
const python = path.join(path.dirname(cabildo), "python");
const home = path.join(dir, "home");
fs.mkdirSync(home);
fs.writeFileSync(path.join(home, "daemon.json"), JSON.stringify({ exe: python }));
fs.mkdirSync(path.join(dir, "bin"));
fs.writeFileSync(
  path.join(dir, "bin", "claude"),
  '#!/bin/sh\necho ready; read line; echo "$line" > "$PROBE_DIR/new"; sleep 60\n',
  { mode: 0o755 },
);
const env = {
  ...process.env,
  CABILDO_BIN: cabildo,
  CABILDO_HOME: home,
  CABILDO_BROKER_SOCKET: path.join(dir, "bus.sock"),
  CABILDO_HOST_SOCKET: path.join(dir, "host.sock"),
  PATH: `${path.join(dir, "bin")}:${process.env.PATH}`,
  PROBE_DIR: dir,
};
const host = (...args) =>
  execFileSync(cabildo, ["host", ...args], { env, encoding: "utf8", stdio: "pipe" });

const script = 'echo ready; read line; echo "$line" > got; read line; echo "$line" > got2; sleep 60';
let code = 1;
let broker;
try {
  const id = host("new", "--cwd", dir, "--", "sh", "-c", script).trim();
  const pid = host("list").trim().split(/\s+/)[1];
  broker = spawn(python, [path.join(here, "seed.py"), env.CABILDO_BROKER_SOCKET, pid, dir], {
    env,
    stdio: "inherit",
  });
  for (let tries = 0; !fs.existsSync(env.CABILDO_BROKER_SOCKET); tries++) {
    if (tries > 100 || broker.exitCode !== null) {
      throw new Error("the probe's broker did not start");
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
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
  broker?.kill();
  try {
    host("stop", "--force");
  } catch {
    // The suite stopped it.
  }
  fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
}
console.log(code === 0 ? "probe passed" : `probe failed (${code})`);
process.exit(code);
