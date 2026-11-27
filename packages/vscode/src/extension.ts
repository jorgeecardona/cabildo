/** VS Code wiring: the status bar item, the side bar, and the commands. */

import * as vscode from "vscode";

import { fetchStatus } from "./client";
import { type Connected, ConsoleView, type FromConsole } from "./consoleView";
import { Messages } from "./messages";
import type { MessageNode } from "./nodes";
import { brokerSocket } from "./paths";
import { type FromView, PanelView } from "./panelView";
import { Unread } from "./sidebar";
import { showError, Terminals } from "./terminals";
import type { Frame, Status } from "./types";
import { statusText, statusTooltip } from "./view";

/** What other code, and the probe, can call. */
export interface Api {
  refresh(): Promise<Status>;
  statusText(): string;
  unread(): readonly MessageNode[];
  panel(): { frame: Frame | undefined; unavailable: string; rendered: number };
  panelMessage(message: FromView): Promise<void>;
  console(): readonly Connected[];
  consoleMessage(message: FromConsole): Promise<void>;
}

export function activate(context: vscode.ExtensionContext): Api {
  const item = vscode.window.createStatusBarItem("cabildo.status", vscode.StatusBarAlignment.Left);
  item.name = "cabildo";
  item.command = "cabildo.attach";
  item.text = "$(sync~spin) cabildo";
  item.show();

  const unread = new Unread();
  const terminals = new Terminals();
  const messages = new Messages();
  const consoles: ConsoleView = new ConsoleView(context, {
    shown: (id) => panel.select(id),
    end: (session, title) => terminals.end(session, title),
  });
  terminals.console = consoles;
  const panel = new PanelView(context, terminals, messages, (frame) => consoles.update(frame));
  let timer: NodeJS.Timeout | undefined;
  let stopped = false;
  let asked = 0;

  const refresh = async (): Promise<Status> => {
    const mine = ++asked;
    const status = await fetchStatus(brokerSocket(process.env));
    if (!stopped && mine === asked) {
      // a slower, older answer must not replace a newer one
      item.text = statusText(status);
      item.tooltip = statusTooltip(status);
      unread.show(status);
    }
    return status;
  };

  // One request chain at a time: the next one is scheduled after the last answers.
  const poll = async (): Promise<void> => {
    await refresh();
    if (!stopped) {
      const seconds = vscode.workspace.getConfiguration("cabildo").get("refreshSeconds", 10);
      timer = setTimeout(poll, Math.max(2, seconds) * 1000);
    }
  };

  /** Register a command that changes something: report a failure, then show the new state. */
  const command = <A extends unknown[], R>(
    name: string,
    run: (...args: A) => Promise<R>,
  ): vscode.Disposable =>
    vscode.commands.registerCommand(name, async (...args: A): Promise<R | undefined> => {
      try {
        return await run(...args);
      } catch (error) {
        showError(error);
        return undefined;
      } finally {
        await refresh();
      }
    });

  context.subscriptions.push(
    item,
    unread,
    terminals,
    messages,
    panel,
    consoles,
    vscode.window.registerWebviewViewProvider("cabildo.panel", panel),
    vscode.window.registerWebviewViewProvider("cabildo.console", consoles, {
      webviewOptions: { retainContextWhenHidden: true }, // the terminals keep their screens
    }),
    vscode.commands.registerCommand("cabildo.refresh", () => {
      panel.restart();
      return refresh();
    }),
    command("cabildo.attach", (id?: string) => terminals.attach(id)),
    command("cabildo.new", async () => {
      const started = await terminals.startInFolder();
      if (started) {
        panel.select(started);
      }
      return started;
    }),
    command("cabildo.openMessage", (node: MessageNode) => messages.open(node.message)),
    command("cabildo.reply", (node: MessageNode) => messages.reply(node.message)),
    command("cabildo.markRead", (node: MessageNode) => messages.markRead(node.message)),
    command("cabildo.markAllRead", () => messages.markAllRead()),
    vscode.workspace.onDidChangeWorkspaceFolders(() => panel.restart()),
    vscode.workspace.onDidChangeConfiguration((change) => {
      if (change.affectsConfiguration("cabildo.command")) {
        panel.restart();
      }
      if (
        change.affectsConfiguration("terminal.integrated") ||
        change.affectsConfiguration("editor.fontFamily")
      ) {
        consoles.font();
      }
    }),
    {
      dispose: () => {
        stopped = true;
        clearTimeout(timer);
      },
    },
  );
  void poll();
  return {
    refresh,
    statusText: () => item.text,
    unread: () => unread.rows(),
    panel: () => ({
      frame: panel.frame(),
      unavailable: panel.unavailable(),
      rendered: panel.rendered(),
    }),
    panelMessage: (message) => panel.handle(message),
    console: () => consoles.tabs(),
    consoleMessage: (message) => consoles.handle(message),
  };
}
