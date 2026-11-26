/** VS Code wiring: the status bar item, and terminal tabs attached to hosted sessions. */

import * as os from "node:os";

import * as vscode from "vscode";

import { fetchStatus, listSessions } from "./client";
import { brokerSocket, type Command, cabildoCommand, hostSocket } from "./paths";
import { startSession } from "./start";
import type { HostSession, Status } from "./types";
import { pickerRows, sessionRows, statusText, statusTooltip } from "./view";

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
    vscode.commands.registerCommand("cabildo.new", () => startInFolder(tabs)),
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

function folderPaths(): string[] {
  return (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath);
}

function configuredCommand(): Command {
  const configured = vscode.workspace.getConfiguration("cabildo").get("command", "");
  return cabildoCommand(configured, process.env);
}

function showError(error: unknown): void {
  void vscode.window.showErrorMessage(`cabildo: ${error instanceof Error ? error.message : error}`);
}

/**
 * Start a Claude in the session host, in the open folder (asked for when there are several),
 * and show it in a terminal tab. Returns the new session's id.
 */
async function startInFolder(tabs: Map<string, vscode.Terminal>): Promise<string | undefined> {
  const folders = folderPaths();
  if (folders.length === 0) {
    void vscode.window.showWarningMessage("cabildo: open a folder to start a Claude session in it.");
    return undefined;
  }
  const folder =
    folders.length === 1
      ? folders[0]
      : (await vscode.window.showWorkspaceFolderPick({ placeHolder: "Folder for the session" }))?.uri
          .fsPath;
  if (!folder) {
    return undefined;
  }
  let id: string;
  try {
    id = await startSession(configuredCommand(), folder, process.env);
  } catch (error) {
    showError(error);
    return undefined;
  }
  await attach(tabs, id);
  return id;
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
    } else {
      const rows = pickerRows(sessions, folderPaths());
      if (rows.length === 0) {
        void vscode.window.showInformationMessage(
          "cabildo: the session host runs no session. Open a folder to start one.",
        );
        return;
      }
      const picked = await vscode.window.showQuickPick(rows, {
        placeHolder: "Claude session to open",
        matchOnDetail: true,
      });
      if (picked && !picked.session) {
        await startInFolder(tabs);
        return;
      }
      session = picked?.session;
    }
  } catch (error) {
    showError(error);
    return;
  }
  if (!session) {
    return;
  }
  let terminal = tabs.get(session.id);
  if (!terminal || terminal.exitStatus) {
    const command = configuredCommand();
    const label = sessionRows([session], [])[0]!.label;
    terminal = vscode.window.createTerminal({
      name: `claude ${label} ${session.id}`,
      shellPath: command.path,
      // `--hold`: the tab closes when its program exits, so the reason must stay on screen.
      shellArgs: [
        ...command.args,
        "attach",
        "--hold",
        "--socket",
        hostSocket(process.env),
        session.id,
      ],
      cwd: os.homedir(), // not the open folder: see `cabildoCommand`
      iconPath: new vscode.ThemeIcon("comment-discussion"),
      isTransient: true, // a restored tab would show a dead viewer
    });
    tabs.set(session.id, terminal);
  }
  terminal.show();
}
