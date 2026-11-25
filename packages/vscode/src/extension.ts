/** VS Code wiring: the status bar item, and terminal tabs attached to hosted sessions. */

import * as vscode from "vscode";

import { fetchStatus, listSessions } from "./client";
import { brokerSocket, cabildoCommand, hostSocket } from "./paths";
import type { HostSession, Status } from "./types";
import { sessionRows, statusText, statusTooltip } from "./view";

/** What other code, and the probe, can call. */
export interface Api {
  refresh(): Promise<Status>;
  statusText(): string;
}

export function activate(context: vscode.ExtensionContext): Api {
  const item = vscode.window.createStatusBarItem("cabildo.status", vscode.StatusBarAlignment.Left);
  item.name = "cabildo";
  item.command = "cabildo.attach";
  item.text = "$(sync~spin) cabildo";
  item.show();

  const tabs = new Map<string, vscode.Terminal>();
  let timer: NodeJS.Timeout | undefined;
  let stopped = false;

  const refresh = async (): Promise<Status> => {
    const status = await fetchStatus(brokerSocket(process.env));
    if (!stopped) {
      item.text = statusText(status);
      item.tooltip = statusTooltip(status);
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

  context.subscriptions.push(
    item,
    vscode.commands.registerCommand("cabildo.refresh", refresh),
    vscode.commands.registerCommand("cabildo.attach", (id?: string) => attach(tabs, id)),
    vscode.window.onDidCloseTerminal((closed) => {
      for (const [id, terminal] of tabs) {
        if (terminal === closed) {
          tabs.delete(id);
        }
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
  return { refresh, statusText: () => item.text };
}

/**
 * Show the hosted session `id` (asked for when absent) in a terminal tab. The tab runs
 * `cabildo attach`; closing it detaches and leaves the session running.
 */
async function attach(tabs: Map<string, vscode.Terminal>, id?: string): Promise<void> {
  let session: HostSession | undefined;
  try {
    const sessions = await listSessions(hostSocket(process.env));
    if (id) {
      session = sessions.find((s) => s.id === id);
      if (!session) {
        void vscode.window.showWarningMessage(`cabildo: no hosted session ${id}.`);
        return;
      }
    } else if (sessions.length === 0) {
      void vscode.window.showInformationMessage(
        "cabildo: the session host runs no session. Start one with `cabildo host new`.",
      );
      return;
    } else {
      const folders = (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath);
      const picked = await vscode.window.showQuickPick(sessionRows(sessions, folders), {
        placeHolder: "Claude session to attach to",
        matchOnDetail: true,
      });
      session = picked?.session;
    }
  } catch (error) {
    void vscode.window.showErrorMessage(`cabildo: ${error instanceof Error ? error.message : error}`);
    return;
  }
  if (!session) {
    return;
  }
  let terminal = tabs.get(session.id);
  if (!terminal || terminal.exitStatus) {
    const configured = vscode.workspace.getConfiguration("cabildo").get("command", "");
    const command = cabildoCommand(configured, process.env);
    const label = sessionRows([session], [])[0]!.label;
    terminal = vscode.window.createTerminal({
      name: `claude ${label} ${session.id}`,
      shellPath: command.path,
      shellArgs: [...command.args, "attach", "--socket", hostSocket(process.env), session.id],
      iconPath: new vscode.ThemeIcon("comment-discussion"),
      isTransient: true, // a restored tab would show a dead viewer
    });
    tabs.set(session.id, terminal);
  }
  terminal.show();
}
