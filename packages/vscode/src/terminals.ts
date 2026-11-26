/** Hosted sessions in terminal tabs: open, start and end them. */

import * as os from "node:os";

import * as vscode from "vscode";

import { endSession, listSessions } from "./client";
import { type Command, cabildoCommand, hostSocket } from "./paths";
import { startSession } from "./start";
import type { HostSession } from "./types";
import { pickerRows, sessionRows } from "./view";

export function folderPaths(): string[] {
  return (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath);
}

export function showError(error: unknown): void {
  void vscode.window.showErrorMessage(`cabildo: ${error instanceof Error ? error.message : error}`);
}

export function configuredCommand(): Command {
  const configured = vscode.workspace.getConfiguration("cabildo").get("command", "");
  return cabildoCommand(configured, process.env);
}

export class Terminals implements vscode.Disposable {
  private readonly tabs = new Map<string, vscode.Terminal>();
  private readonly closing = vscode.window.onDidCloseTerminal((closed) => {
    for (const [id, terminal] of this.tabs) {
      if (terminal === closed) {
        this.tabs.delete(id);
      }
    }
  });

  /**
   * Start a Claude in the session host, in the open folder (asked for when there are several),
   * and show it in a terminal tab. Returns the new session's id.
   */
  async startInFolder(): Promise<string | undefined> {
    const folders = folderPaths();
    if (folders.length === 0) {
      void vscode.window.showWarningMessage(
        "cabildo: open a folder to start a Claude session in it.",
      );
      return undefined;
    }
    const folder =
      folders.length === 1
        ? folders[0]
        : (await vscode.window.showWorkspaceFolderPick({ placeHolder: "Folder for the session" }))
            ?.uri.fsPath;
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
    await this.attach(id);
    return id;
  }

  /** Show the hosted session `id` in a terminal tab; ask which one when `id` is absent. */
  async attach(id?: string): Promise<void> {
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
          await this.startInFolder();
          return;
        }
        session = picked?.session;
      }
    } catch (error) {
      showError(error);
      return;
    }
    if (session) {
      this.open(session);
    }
  }

  /** Ask, then hang the session up: its Claude stops. */
  async end(session: HostSession, name: string): Promise<boolean> {
    const yes = "End Session";
    const answer = await vscode.window.showWarningMessage(
      `End the session ${name}?`,
      { modal: true, detail: `Claude stops. Folder: ${session.cwd}` },
      yes,
    );
    if (answer !== yes) {
      return false;
    }
    // The tab goes first: one whose session is hung up exits with an error, and VS Code says so.
    this.tabs.get(session.id)?.dispose();
    await endSession(hostSocket(process.env), session.id);
    return true;
  }

  dispose(): void {
    this.closing.dispose();
  }

  /** The tab runs `cabildo attach`; closing it detaches and leaves the session running. */
  private open(session: HostSession): void {
    let terminal = this.tabs.get(session.id);
    if (!terminal || terminal.exitStatus) {
      const command = configuredCommand();
      terminal = vscode.window.createTerminal({
        name: `claude ${sessionRows([session], [])[0]!.label} ${session.id}`,
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
      this.tabs.set(session.id, terminal);
    }
    terminal.show();
  }
}
