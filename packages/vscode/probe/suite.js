// Runs inside the VS Code extension host. See run.mjs.
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
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
    const api = await vscode.extensions.getExtension("JorgeCardona.cabildo").activate();
    const status = await api.refresh();
    assert.equal(status.kind, "online", status.reason);
    result.statusBar = api.statusText();

    // The side bar: show it, then read its three lists.
    await vscode.commands.executeCommand("workbench.view.extension.cabildo");
    await api.refresh();
    const rows = api.rows();
    result.sidebar = Object.fromEntries(
      Object.entries(rows).map(([list, nodes]) => [
        list,
        nodes.map((n) => `${n.label} | ${n.description}`),
      ]),
    );
    assert.equal(rows.sessions[0].label, "@probe-agent", "the session is not named by its agent");
    assert.equal(rows.agents[0].session?.id, id);

    await vscode.commands.executeCommand("cabildo.openMessage", rows.messages[0]);
    result.opened = vscode.window.activeTextEditor.document.getText().split("\n").slice(2, 4);
    assert.deepEqual(result.opened, ["hello from the probe", "second line"]);
    result.unreadAfterOpen = api.rows().messages.length;
    assert.equal(result.unreadAfterOpen, 0, "opening did not mark the message read");

    await vscode.commands.executeCommand("cabildo.openSession", api.rows().sessions[0]);
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

    // A viewer must stay attached while nobody types.
    await new Promise((resolve) => setTimeout(resolve, 8000));
    assert.equal(terminal.exitStatus, undefined, "the tab closed while idle");
    terminal.sendText("typed after a pause");
    const later = path.join(dir, "got2");
    result.readAfterPause = await until("the session to read the second line", () =>
      fs.existsSync(later) ? fs.readFileSync(later, "utf8").trim() : "",
    );

    terminal.dispose();
    await until("the tab to close", () => !vscode.window.terminals.includes(terminal));
    result.runningAfterClose = (await listSessions(socket)).some((s) => s.id === id);
    assert.ok(result.runningAfterClose, "closing the tab ended the session");

    // A new session in the open folder; its `claude` is the script run.mjs put on PATH.
    const started = await vscode.commands.executeCommand("cabildo.new");
    const record = (await listSessions(socket)).find((s) => s.id === started);
    assert.equal(record?.cwd, dir, "the session did not start in the folder");
    result.newSession = `${record.command.join(" ")} in ${record.cwd}`;
    const tab = await until("the new tab", () =>
      vscode.window.terminals.find((t) => t.name.endsWith(started)),
    );
    await tab.processId;
    await new Promise((resolve) => setTimeout(resolve, 3000));
    tab.sendText("typed in the new session");
    const fresh = path.join(dir, "new");
    result.newSessionRead = await until("the new session to read the line", () =>
      fs.existsSync(fresh) ? fs.readFileSync(fresh, "utf8").trim() : "",
    );

    // The host stops: the tab stays open with the reason until Enter.
    execFileSync(process.env.CABILDO_BIN, ["host", "stop", "--force"]);
    await new Promise((resolve) => setTimeout(resolve, 5000));
    assert.equal(tab.exitStatus, undefined, "the tab closed when the host stopped");
    result.openAfterHostStopped = true;
    tab.sendText("");
    await until("the tab to close on Enter", () => !vscode.window.terminals.includes(tab));
    result.closedOnEnter = true;
  } catch (error) {
    result.error = String(error.stack ?? error);
    throw error;
  } finally {
    fs.writeFileSync(path.join(dir, "result.json"), JSON.stringify(result, null, 2));
  }
};
