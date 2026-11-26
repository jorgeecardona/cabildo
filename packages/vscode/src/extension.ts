/** VS Code wiring: the status bar item, the side bar, and the commands. */

import * as vscode from "vscode";

import { fetchStatus, listSessions } from "./client";
import { Messages } from "./messages";
import type { AgentNode, MessageNode, SessionNode } from "./nodes";
import { brokerSocket, hostSocket } from "./paths";
import { type Rows, Sidebar } from "./sidebar";
import { folderPaths, showError, Terminals } from "./terminals";
import type { HostSession, Status } from "./types";
import { statusText, statusTooltip } from "./view";

/** What other code, and the probe, can call. */
export interface Api {
  refresh(): Promise<Status>;
  statusText(): string;
  rows(): Rows;
}

export function activate(context: vscode.ExtensionContext): Api {
  const item = vscode.window.createStatusBarItem("cabildo.status", vscode.StatusBarAlignment.Left);
  item.name = "cabildo";
  item.command = "cabildo.attach";
  item.text = "$(sync~spin) cabildo";
  item.show();

  const sidebar = new Sidebar();
  const terminals = new Terminals();
  const messages = new Messages();
  let timer: NodeJS.Timeout | undefined;
  let stopped = false;
  let asked = 0;

  const refresh = async (): Promise<Status> => {
    const mine = ++asked;
    let hostError: string | undefined;
    const [status, sessions] = await Promise.all([
      fetchStatus(brokerSocket(process.env)),
      listSessions(hostSocket(process.env)).catch((error: unknown): HostSession[] => {
        hostError = `session host: ${error instanceof Error ? error.message : error}`;
        return [];
      }),
    ]);
    if (!stopped && mine === asked) {
      // a slower, older answer must not replace a newer one
      item.text = statusText(status);
      item.tooltip = statusTooltip(status);
      sidebar.show({ status, sessions, hostError }, folderPaths());
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
    sidebar,
    terminals,
    messages,
    vscode.commands.registerCommand("cabildo.refresh", refresh),
    command("cabildo.attach", (id?: string) => terminals.attach(id)),
    command("cabildo.new", () => terminals.startInFolder()),
    command("cabildo.openSession", (node: SessionNode | AgentNode) =>
      terminals.attach(node.session!.id),
    ),
    command("cabildo.endSession", (node: SessionNode) => terminals.end(node.session, node.label)),
    command("cabildo.sendMessage", (node: AgentNode) =>
      messages.send(node.agent.session_id, node.label),
    ),
    command("cabildo.openMessage", (node: MessageNode) => messages.open(node.message)),
    command("cabildo.reply", (node: MessageNode) => messages.reply(node.message)),
    command("cabildo.markRead", (node: MessageNode) => messages.markRead(node.message)),
    command("cabildo.markAllRead", () => messages.markAllRead()),
    vscode.workspace.onDidChangeWorkspaceFolders(() => void refresh()),
    {
      dispose: () => {
        stopped = true;
        clearTimeout(timer);
      },
    },
  );
  void poll();
  return { refresh, statusText: () => item.text, rows: () => sidebar.rows() };
}
