// Probe in a real VS Code window: `pnpm run probe`.
//
// Starts a throwaway session host with one `sh` session and a throwaway broker (seed.py) in a
// temporary cabildo home, writes one transcript in a temporary Claude config dir, opens VS Code
// with an empty profile, and runs suite.js inside it. Nothing reads or writes the real bus or the
// real transcripts. The sessions the extension starts run a script named `claude`, not Claude.
// CABILDO_BIN is the path of `cabildo`; the `python` beside it runs the broker.
// VSCODE_BINARY names the VS Code program (default /usr/share/code/code).
// PROBE_SHOT names a PNG file to save a picture of the window in (shot.py takes it).
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
// The extension runs this Python. Its shared files go to the probe's folder, not /run/user.
const wrapper = path.join(dir, "python");
fs.mkdirSync(path.join(dir, "run"));
fs.writeFileSync(wrapper, `#!/bin/sh\nXDG_RUNTIME_DIR='${dir}/run' exec '${python}' "$@"\n`, {
  mode: 0o755,
});
fs.writeFileSync(path.join(home, "daemon.json"), JSON.stringify({ exe: wrapper }));
fs.mkdirSync(path.join(dir, "bin"));
fs.writeFileSync(
  path.join(dir, "bin", "claude"),
  `#!/bin/sh
case "$1" in
  agents) cat "$PROBE_DIR/agents.json" ;;
  --resume) echo ready; read line; echo "$line" > "$PROBE_DIR/resumed"; sleep 60 ;;
  *) echo ready; read line; echo "$line" > "$PROBE_DIR/new"; sleep 60 ;;
esac
`,
  { mode: 0o755 },
);
const project = path.join(dir, "claude", "projects", dir.replace(/[^a-zA-Z0-9]/g, "-"));
fs.mkdirSync(project, { recursive: true });
const line = (entry) => JSON.stringify({ sessionId: "seeded", ...entry });
fs.writeFileSync(
  path.join(project, "seeded.jsonl"),
  [
    line({ type: "user", cwd: dir, gitBranch: "main", message: { role: "user", content: "q" } }),
    line({
      type: "assistant",
      cwd: dir,
      message: {
        id: "msg_1",
        role: "assistant",
        model: "claude-opus-5-5",
        content: [{ type: "text", text: "a" }],
        usage: { input_tokens: 300000, output_tokens: 900 },
      },
    }),
    line({ type: "ai-title", aiTitle: "Seeded conversation" }),
  ].join("\n") + "\n",
);
const env = {
  ...process.env,
  CABILDO_BIN: cabildo,
  CABILDO_HOME: home,
  CABILDO_BROKER_SOCKET: path.join(dir, "bus.sock"),
  CABILDO_HOST_SOCKET: path.join(dir, "host.sock"),
  PATH: `${path.join(dir, "bin")}:${process.env.PATH}`,
  PROBE_DIR: dir,
  CLAUDE_CONFIG_DIR: path.join(dir, "claude"),
  XDG_DATA_HOME: path.join(dir, "xdg-data"),
  XDG_STATE_HOME: path.join(dir, "xdg-state"),
};
const host = (...args) =>
  execFileSync(cabildo, ["host", ...args], { env, encoding: "utf8", stdio: "pipe" });

const script = 'echo ready; read line; echo "$line" > got; read line; echo "$line" > got2; sleep 60';
let code = 1;
let broker;
try {
  const id = host("new", "--cwd", dir, "--", "sh", "-c", script).trim();
  const pid = host("list").trim().split(/\s+/)[1];
  fs.writeFileSync(
    path.join(dir, "agents.json"),
    JSON.stringify([
      {
        sessionId: "probe-agent",
        kind: "interactive",
        name: "Probe session",
        cwd: dir,
        status: "busy",
        pid: Number(pid),
      },
    ]),
  );
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
  const shot = process.env.PROBE_SHOT;
  const port = 9300 + (process.pid % 500);
  // suite.js writes `shoot` when the window shows what the picture is of.
  const watch = setInterval(() => {
    if (shot && fs.existsSync(path.join(dir, "shoot"))) {
      clearInterval(watch);
      try {
        execFileSync(python, [path.join(here, "shot.py"), String(port), path.resolve(shot)], {
          stdio: "inherit",
          timeout: 30_000,
        });
      } finally {
        fs.writeFileSync(path.join(dir, "shot-done"), "");
      }
    }
  }, 500);
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
        ...(shot ? [`--remote-debugging-port=${port}`] : []),
        dir,
      ],
      { env: { ...env, PROBE_SESSION: id }, stdio: "inherit", timeout: 90_000 },
    );
    child.on("exit", (status) => resolve(status ?? 1));
  });
  clearInterval(watch);
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
