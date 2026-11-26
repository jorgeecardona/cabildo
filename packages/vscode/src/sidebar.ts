/** The three lists in the cabildo side bar. */

import * as vscode from "vscode";

import { type AgentNode, agentNodes, type MessageNode, messageNodes, type Node } from "./nodes";
import { type SessionNode, sessionNodes } from "./nodes";
import type { HostSession, Status } from "./types";

/** What the daemon and the host last answered. */
export interface Snapshot {
  readonly status: Status;
  readonly sessions: readonly HostSession[];
  /** Why the session host did not answer, if it did not. */
  readonly hostError: string | undefined;
}

export interface Rows {
  readonly sessions: readonly SessionNode[];
  readonly agents: readonly AgentNode[];
  readonly messages: readonly MessageNode[];
}

const COLORS: Readonly<Record<string, string>> = {
  working: "testing.iconPassed",
  idle: "charts.yellow",
};

function icon(name: string, color?: string): vscode.ThemeIcon {
  return new vscode.ThemeIcon(name, color ? new vscode.ThemeColor(color) : undefined);
}

function treeItem(node: Node): vscode.TreeItem {
  const item = new vscode.TreeItem(node.label);
  item.description = node.description;
  item.tooltip = node.tooltip; // plain text: agents write parts of it
  item.contextValue = node.kind;
  const open = (command: string): vscode.Command => ({ command, title: "Open", arguments: [node] });
  switch (node.kind) {
    case "session":
      item.id = `session:${node.session.id}`;
      item.iconPath = icon("comment-discussion", COLORS[node.presence ?? ""]);
      item.command = open("cabildo.openSession");
      break;
    case "agent":
      item.id = `agent:${node.agent.session_id}`;
      item.iconPath = icon(
        node.agent.presence === "working" ? "circle-filled" : "circle-outline",
        COLORS[node.agent.presence],
      );
      if (node.session) {
        item.command = open("cabildo.openSession");
      }
      break;
    case "message":
      item.id = `message:${node.message.id}`;
      item.iconPath = node.message.urgent ? icon("warning", "charts.red") : icon("mail");
      item.command = open("cabildo.openMessage");
      break;
  }
  return item;
}

/** One flat list. */
class List<T extends Node> implements vscode.TreeDataProvider<T> {
  rows: readonly T[] = [];
  readonly view: vscode.TreeView<T>;
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;

  constructor(id: string) {
    this.view = vscode.window.createTreeView(id, { treeDataProvider: this });
  }

  show(rows: readonly T[], message?: string): void {
    this.view.message = message;
    if (JSON.stringify(rows) !== JSON.stringify(this.rows)) {
      this.rows = rows;
      this.changed.fire(); // only on a change: a redraw closes an open tooltip
    }
  }

  getTreeItem(node: T): vscode.TreeItem {
    return treeItem(node);
  }

  getChildren(node?: T): T[] {
    return node ? [] : [...this.rows];
  }

  dispose(): void {
    this.view.dispose();
    this.changed.dispose();
  }
}

export class Sidebar implements vscode.Disposable {
  private readonly sessions = new List<SessionNode>("cabildo.sessions");
  private readonly agents = new List<AgentNode>("cabildo.agents");
  private readonly messages = new List<MessageNode>("cabildo.messages");

  show({ status, sessions, hostError }: Snapshot, folders: readonly string[]): void {
    const online = status.kind === "online" ? status : undefined;
    const offline =
      status.kind === "offline" ? `cabildo daemon not reachable: ${status.reason}` : undefined;
    this.sessions.show(sessionNodes(sessions, online?.agents ?? [], folders), hostError);
    this.agents.show(agentNodes(online?.agents ?? [], sessions, folders), offline);
    const unread = messageNodes(online?.unread ?? []);
    this.messages.show(unread, offline);
    // The badge shows on the cabildo icon of the activity bar.
    this.messages.view.badge = unread.length
      ? { value: unread.length, tooltip: `${unread.length} unread cabildo messages` }
      : undefined;
  }

  rows(): Rows {
    return { sessions: this.sessions.rows, agents: this.agents.rows, messages: this.messages.rows };
  }

  dispose(): void {
    for (const list of [this.sessions, this.agents, this.messages]) {
      list.dispose();
    }
  }
}
