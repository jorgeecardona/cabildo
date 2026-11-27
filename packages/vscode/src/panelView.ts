/** The panel of the side bar: what `cabildo panel` sends, drawn by `media/panel.js`. */

import { randomBytes } from "node:crypto";

import * as vscode from "vscode";

import { listSessions } from "./client";
import { type FeedEvent, PanelFeed } from "./feed";
import type { Messages } from "./messages";
import { hostSocket } from "./paths";
import { configuredCommand, folderPaths, showError, type Terminals } from "./terminals";
import type { Frame, Item, Opened, Scope } from "./types";

const SCOPE_KEY = "cabildo.scope";
const SCOPES: readonly Scope[] = ["here", "subfolders", "all"];

/** A message from the webview. */
export type FromView =
  | { readonly type: "ready" }
  | { readonly type: "rendered"; readonly rows: number }
  | { readonly type: "scope"; readonly scope: Scope }
  | { readonly type: "select" | "highlight" | "open" | "end" | "message"; readonly id: string };

export class PanelView implements vscode.WebviewViewProvider, vscode.Disposable {
  private view: vscode.WebviewView | undefined;
  private readonly feed = new PanelFeed(configuredCommand, process.env, (e) => this.receive(e));
  private last: Frame | undefined;
  private reason = "";
  private drawn = -1;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly terminals: Terminals,
    private readonly messages: Messages,
  ) {}

  /** The last frame cabildo sent. */
  frame(): Frame | undefined {
    return this.last;
  }

  /** Why there is no frame, or empty. */
  unavailable(): string {
    return this.reason;
  }

  /** How many conversations the webview last drew; -1 before it drew any. */
  rendered(): number {
    return this.drawn;
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const media = vscode.Uri.joinPath(this.context.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    view.webview.onDidReceiveMessage((message: FromView) =>
      this.handle(message).catch(showError),
    );
    view.webview.html = html(view.webview, media);
    view.onDidChangeVisibility(() => this.feed.send({ cmd: "visible", visible: view.visible }));
    view.onDidDispose(() => {
      this.view = undefined;
      this.feed.stop();
    });
    this.restart();
  }

  /** Start `cabildo panel` again, for the open folder. */
  restart(): void {
    if (!this.view) {
      return;
    }
    this.last = undefined;
    const scope = this.context.workspaceState.get<Scope>(SCOPE_KEY);
    const folder = folderPaths()[0] ?? "";
    this.feed.start(folder, scope && SCOPES.includes(scope) ? scope : folder ? "here" : "all");
  }

  /** Show the new conversation of the hosted session `hostId` as the selected one. */
  select(hostId: string): void {
    this.feed.send({ cmd: "select", id: `host:${hostId}` });
  }

  /** Act on a message of the webview. */
  async handle(message: FromView): Promise<void> {
    switch (message.type) {
      case "ready":
        this.post();
        return;
      case "rendered":
        this.drawn = message.rows;
        return;
      case "scope":
        await this.context.workspaceState.update(SCOPE_KEY, message.scope);
        this.feed.send({ cmd: "scope", scope: message.scope });
        return;
    }
    const item = this.last?.items.find((i) => i.id === message.id);
    if (!item) {
      return;
    }
    switch (message.type) {
      case "highlight":
        this.feed.send({ cmd: "select", id: item.id });
        return;
      case "select":
        this.feed.send({ cmd: "select", id: item.id });
        if (item.host_id) {
          await this.terminals.attach(item.host_id);
        }
        return;
      case "open":
        this.feed.send({ cmd: "open", id: item.id, force: false });
        return;
      case "end":
        await this.end(item);
        return;
      case "message":
        if (item.bus_id) {
          await this.messages.send(item.bus_id, item.title);
        }
        return;
    }
  }

  dispose(): void {
    this.feed.stop();
  }

  private async end(item: Item): Promise<void> {
    const sessions = await listSessions(hostSocket(process.env));
    const session = sessions.find((s) => s.id === item.host_id);
    if (session) {
      await this.terminals.end(session, item.title);
    }
  }

  private receive(event: FeedEvent): void {
    switch (event.type) {
      case "frame":
        this.last = event;
        this.reason = "";
        this.post();
        return;
      case "unavailable":
        this.reason = event.reason;
        this.post();
        return;
      case "open":
        void this.opened(event).catch(showError);
        return;
    }
  }

  /** Show what `open` answered in a terminal tab. */
  private async opened(answer: Opened): Promise<void> {
    if (answer.error) {
      showError(`not opened: ${answer.error}`);
    } else if (answer.host_id) {
      await this.terminals.attach(answer.host_id);
    } else if (answer.command.length) {
      this.terminals.run(answer.id, answer.command, answer.cwd, answer.title);
    } else if (answer.elsewhere) {
      const yes = "Open Anyway";
      const chosen = await vscode.window.showWarningMessage(
        `${answer.title} runs in another terminal.`,
        {
          modal: true,
          detail: "Opening it here starts a second Claude on the same conversation.",
        },
        yes,
      );
      if (chosen === yes) {
        this.feed.send({ cmd: "open", id: answer.id, force: true });
      }
    }
  }

  private post(): void {
    void this.view?.webview.postMessage({ frame: this.last, reason: this.reason });
  }
}

/** The page. `panel.js` builds all of it, and sets every text as text, never as HTML. */
function html(webview: vscode.Webview, media: vscode.Uri): string {
  const file = (name: string): string =>
    webview.asWebviewUri(vscode.Uri.joinPath(media, name)).toString();
  const nonce = randomBytes(16).toString("base64");
  const policy = `default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}'`;
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${policy}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="${file("panel.css")}">
<title>cabildo</title>
</head>
<body>
<script nonce="${nonce}" src="${file("panel.js")}"></script>
</body>
</html>`;
}
