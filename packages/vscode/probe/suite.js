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
    const api = await vscode.extensions.getExtension("cardona-oss.cabildo").activate();
    const status = await api.refresh();
    assert.equal(status.kind, "online", status.reason);
    result.statusBar = api.statusText();
    // The seed has one agent and the person: the person is not counted.
    assert.match(result.statusBar, /\$\(organization\) 1 /);

    // The side bar: show it, and wait until the page has drawn the panel.
    await vscode.commands.executeCommand("workbench.view.extension.cabildo");
    const { frame } = await until("the panel", () => {
      const panel = api.panel();
      assert.equal(panel.unavailable, "");
      const items = panel.frame?.items ?? [];
      return panel.rendered === 2 && items.some((i) => i.cpu) ? panel : undefined;
    });
    result.items = frame.items.map((i) => `${i.mark} | ${i.title} | ${i.host_id} | ${i.bus_id}`);
    const hosted = frame.items.find((i) => i.host_id === id);
    assert.equal(hosted?.title, "Probe session");
    assert.equal(hosted.bus_id, "probe-agent");
    const seeded = frame.items.find((i) => i.id === "seeded");
    assert.equal(seeded?.title, "Seeded conversation");

    await api.panelMessage({ type: "highlight", id: "seeded" });
    result.detail = await until("the detail of the conversation", () => {
      const found = api.panel().frame;
      return found?.selected === "seeded" && found.detail?.model ? found.detail : undefined;
    });
    assert.equal(result.detail.model, "opus 5.5");
    assert.ok(result.detail.context.window, "the context window is not known");
    await vscode.commands.executeCommand("cabildo.openMessage", api.unread()[0]);
    result.opened = vscode.window.activeTextEditor.document.getText().split("\n").slice(2, 4);
    assert.deepEqual(result.opened, ["hello from the probe", "second line"]);
    result.unreadAfterOpen = api.unread().length;
    assert.equal(result.unreadAfterOpen, 0, "opening did not mark the message read");

    // `cabildo.openIn` terminal: hosted sessions open in terminal tabs.
    const settings = () => vscode.workspace.getConfiguration("cabildo");
    await settings().update("openIn", "terminal", vscode.ConfigurationTarget.Global);

    // A click on a hosted conversation shows its terminal.
    await api.panelMessage({ type: "select", id: hosted.id });
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

    // A stopped conversation opens: the host starts `claude --resume` for it.
    await api.panelMessage({ type: "open", id: "seeded" });
    const resumed = await until("the resumed session", async () =>
      (await listSessions(socket)).find((s) => s.session_id === "seeded"),
    );
    result.resumed = resumed.command.join(" ");
    assert.equal(result.resumed, "claude --resume seeded");
    const view = await until("the tab of the resumed session", () =>
      vscode.window.terminals.find((t) => t.name.endsWith(resumed.id)),
    );
    await view.processId;
    await new Promise((resolve) => setTimeout(resolve, 3000));
    view.sendText("typed in the resumed session");
    result.resumedRead = await until("the resumed session to read the line", () => {
      const file = path.join(dir, "resumed");
      return fs.existsSync(file) ? fs.readFileSync(file, "utf8").trim() : "";
    });
    view.dispose();

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

    // The console view: the page loads, says its size, and that attaches it to the session.
    await settings().update("openIn", "console", vscode.ConfigurationTarget.Global);
    fs.rmSync(fresh);
    const shown = await vscode.commands.executeCommand("cabildo.new");
    const tabOf = () => api.console().find((t) => t.id === shown);
    await until("the console page to say its size", () => tabOf()?.connected);
    // The host sets the session's window size once the viewer is registered.
    await new Promise((resolve) => setTimeout(resolve, 3000));
    await api.consoleMessage({
      type: "input",
      id: shown,
      data: "typed in the console\r",
      binary: false,
    });
    result.consoleRead = await until("the session to read the console's line", () =>
      fs.existsSync(fresh) ? fs.readFileSync(fresh, "utf8").trim() : "",
    );
    assert.equal(result.consoleRead, "typed in the console");
    // The terminal echoes what is typed: that output reaches the console.
    await until("the console to receive output", () => tabOf()?.received > 0);
    if (process.env.PROBE_SHOT) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      fs.writeFileSync(path.join(dir, "shoot"), "");
      await until("the picture", () => fs.existsSync(path.join(dir, "shot-done")), 40_000);
    }

    // The host stops: the tab stays open with the reason until Enter.
    execFileSync(process.env.CABILDO_BIN, ["host", "stop", "--force"]);
    await new Promise((resolve) => setTimeout(resolve, 5000));
    assert.equal(tab.exitStatus, undefined, "the tab closed when the host stopped");
    result.openAfterHostStopped = true;
    tab.sendText("");
    await until("the tab to close on Enter", () => !vscode.window.terminals.includes(tab));
    result.closedOnEnter = true;
    result.consoleAfterHostStopped = await until("the console tab to say it", () => {
      const state = tabOf()?.state;
      return state && state !== "attached" ? state : "";
    });
  } catch (error) {
    result.error = String(error.stack ?? error);
    throw error;
  } finally {
    fs.writeFileSync(path.join(dir, "result.json"), JSON.stringify(result, null, 2));
  }
};
