// Runs inside the VS Code extension host. See run.mjs.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vscode = require("vscode");

const { listSessions } = require("../out/src/client");

async function until(what, check, ms = 20_000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const value = await check();
    if (value) {
      return value;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`timed out waiting for ${what}`);
}

exports.run = async () => {
  const { PROBE_DIR: dir, PROBE_SESSION: id, CABILDO_HOST_SOCKET: socket } = process.env;
  const result = { vscode: vscode.version };
  try {
    const api = await vscode.extensions.getExtension("jorgeecardona.cabildo").activate();
    const status = await api.refresh();
    assert.equal(status.kind, "online", status.reason);
    result.statusBar = api.statusText();

    await vscode.commands.executeCommand("cabildo.attach", id);
    const terminal = await until("the terminal tab", () =>
      vscode.window.terminals.find((t) => t.name.endsWith(id)),
    );
    result.tab = terminal.name;
    await terminal.processId;
    // The host nudges the session's window size once the viewer is registered.
    await new Promise((resolve) => setTimeout(resolve, 3000));
    assert.equal(terminal.exitStatus, undefined, "cabildo attach exited");
    terminal.sendText("typed in vscode");
    const got = path.join(dir, "got");
    result.sessionRead = await until("the session to read the line", () =>
      fs.existsSync(got) ? fs.readFileSync(got, "utf8").trim() : "",
    );
    assert.equal(result.sessionRead, "typed in vscode");

    terminal.dispose();
    await until("the tab to close", () => !vscode.window.terminals.includes(terminal));
    result.runningAfterClose = (await listSessions(socket)).some((s) => s.id === id);
    assert.ok(result.runningAfterClose, "closing the tab ended the session");
  } catch (error) {
    result.error = String(error.stack ?? error);
    throw error;
  } finally {
    fs.writeFileSync(path.join(dir, "result.json"), JSON.stringify(result, null, 2));
  }
};
