/** The list of unread messages under the panel. It is there only while some are unread. */

import * as vscode from "vscode";

import { type MessageNode, messageNodes } from "./nodes";
import type { Status } from "./types";

function treeItem(node: MessageNode): vscode.TreeItem {
  const item = new vscode.TreeItem(node.label);
  item.id = `message:${node.message.id}`;
  item.description = node.description;
  item.tooltip = node.tooltip; // plain text: agents write parts of it
  item.contextValue = node.kind;
  item.iconPath = node.message.urgent
    ? new vscode.ThemeIcon("warning", new vscode.ThemeColor("charts.red"))
    : new vscode.ThemeIcon("mail");
  item.command = { command: "cabildo.openMessage", title: "Open", arguments: [node] };
  return item;
}

export class Unread implements vscode.TreeDataProvider<MessageNode>, vscode.Disposable {
  private nodes: readonly MessageNode[] = [];
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private readonly view = vscode.window.createTreeView("cabildo.messages", {
    treeDataProvider: this,
  });

  /** Show what the daemon last answered. An answer of an offline daemon keeps nothing. */
  show(status: Status): void {
    const nodes = messageNodes(status.kind === "online" ? status.unread : []);
    // The view's `when` clause reads this.
    void vscode.commands.executeCommand("setContext", "cabildo.hasUnread", nodes.length > 0);
    // The badge shows on the cabildo icon of the activity bar.
    this.view.badge = nodes.length
      ? { value: nodes.length, tooltip: `${nodes.length} unread cabildo messages` }
      : undefined;
    if (JSON.stringify(nodes) !== JSON.stringify(this.nodes)) {
      this.nodes = nodes;
      this.changed.fire(); // only on a change: a redraw closes an open tooltip
    }
  }

  rows(): readonly MessageNode[] {
    return this.nodes;
  }

  getTreeItem(node: MessageNode): vscode.TreeItem {
    return treeItem(node);
  }

  getChildren(node?: MessageNode): MessageNode[] {
    return node ? [] : [...this.nodes];
  }

  dispose(): void {
    this.view.dispose();
    this.changed.dispose();
  }
}
