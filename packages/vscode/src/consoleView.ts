/**
 * The console view: hosted sessions in tabs, drawn by `media/console.js` with xterm.js. This file
 * holds the connections to the session host; the page only draws and types.
 */

import { randomBytes } from "node:crypto";
import * as path from "node:path";

import * as vscode from "vscode";

import { attachSession, type Viewer, type ViewerEvent } from "./attach";
import { hostSocket } from "./paths";
import type { Context, Frame, HostSession, Mark } from "./types";

/** `attached`: connected, or waiting for the page's size to connect. */
export type TabState = "attached" | "ended" | "disconnected";

/** One tab as the page draws it. Empty strings for what is not known. */
export interface TabInfo {
  readonly id: string;
  readonly title: string;
  readonly cwd: string;
  readonly state: TabState;
  readonly mark: Mark | "";
  readonly model: string;
  readonly context: Context | null;
  readonly cost: string;
}

/** A tab and its connection, for the probe. */
export interface Connected extends TabInfo {
  readonly connected: boolean;
  /** Bytes of output received. */
  readonly received: number;
  readonly active: boolean;
}

export interface Font {
  readonly family: string;
  readonly size: number;
}

/** A message to the page. */
export type ToConsole =
  | { readonly type: "font"; readonly font: Font }
  | { readonly type: "tabs"; readonly tabs: readonly TabInfo[]; readonly active: string }
  | { readonly type: "data"; readonly id: string; readonly data: Uint8Array }
  | { readonly type: "notice"; readonly id: string; readonly text: string }
  | { readonly type: "reset"; readonly id: string }
  | { readonly type: "focus" };

/** A message from the page. */
export type FromConsole =
  | { readonly type: "ready" }
  | { readonly type: "new" }
  | { readonly type: "size"; readonly id: string; readonly rows: number; readonly cols: number }
  | {
      readonly type: "input";
      readonly id: string;
      readonly data: string;
      /** The characters are bytes (mouse reports), not text. */
      readonly binary: boolean;
    }
  | { readonly type: "show" | "detach" | "end"; readonly id: string }
  | { readonly type: "link"; readonly url: string };

/** What the view asks the rest of the extension to do. */
export interface Actions {
  /** The person looks at this session now. */
  shown(id: string): void;
  /** Ask, then end the session. */
  end(session: HostSession, title: string): Promise<boolean>;
}

type Details = Pick<TabInfo, "title" | "mark" | "model" | "context" | "cost">;

interface Tab {
  readonly session: HostSession;
  details: Details;
  state: TabState;
  viewer: Viewer | undefined;
  /** Output not yet sent to the page. */
  chunks: Buffer[];
  /** Why it is not attached, printed in the terminal. */
  notice: string;
  received: number;
}

export class ConsoleView implements vscode.WebviewViewProvider, vscode.Disposable {
  private view: vscode.WebviewView | undefined;
  private ready = false;
  private readonly open = new Map<string, Tab>();
  private active = "";
  private frame: Frame | undefined;
  private sent = "";

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly actions: Actions,
  ) {}

  /** The tabs as the page has them, and their connections. */
  tabs(): readonly Connected[] {
    return [...this.open].map(([id, tab]) => ({
      ...this.info(id),
      connected: tab.viewer !== undefined,
      received: tab.received,
      active: id === this.active,
    }));
  }

  /** Show the hosted `session` in its tab, and reveal the view. */
  async show(session: HostSession): Promise<void> {
    let tab = this.open.get(session.id);
    if (!tab) {
      tab = {
        session,
        details: {
          title: path.basename(session.cwd) || session.cwd,
          mark: "",
          model: "",
          context: null,
          cost: "",
        },
        state: "attached",
        viewer: undefined,
        chunks: [],
        notice: "",
        received: 0,
      };
      this.open.set(session.id, tab);
      this.describe(tab);
    }
    this.activate(session.id);
    if (this.view) {
      this.view.show(false);
    } else {
      await vscode.commands.executeCommand("cabildo.console.focus");
    }
    this.post({ type: "focus" });
  }

  /** Drop the tab of session `id`, if there is one. The session keeps running. */
  detach(id: string): void {
    const tab = this.open.get(id);
    if (!tab) {
      return;
    }
    tab.viewer?.close();
    this.open.delete(id);
    if (this.active === id) {
      this.active = [...this.open.keys()].at(-1) ?? "";
    }
    this.publish();
  }

  /** Take titles, states and the selected conversation's numbers from the panel's frame. */
  update(frame: Frame): void {
    this.frame = frame;
    for (const tab of this.open.values()) {
      this.describe(tab);
    }
    this.publish();
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    this.ready = false;
    const media = vscode.Uri.joinPath(this.context.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    view.webview.onDidReceiveMessage((message: FromConsole) => void this.handle(message));
    view.webview.html = html(view.webview, media);
    view.onDidDispose(() => {
      this.view = undefined;
      this.ready = false;
      this.drop();
    });
  }

  /** Send the page the terminal font of the settings. */
  font(): void {
    const terminal = vscode.workspace.getConfiguration("terminal.integrated");
    const editor = vscode.workspace.getConfiguration("editor");
    const family = terminal.get("fontFamily", "") || editor.get("fontFamily", "monospace");
    this.post({ type: "font", font: { family, size: terminal.get("fontSize", 14) } });
  }

  /** Act on a message of the page. */
  async handle(message: FromConsole): Promise<void> {
    switch (message.type) {
      case "ready":
        this.ready = true;
        this.drop(); // a new page has empty terminals: attach again, and the host repaints
        this.font();
        this.sent = "";
        this.publish();
        for (const [id, tab] of this.open) {
          if (tab.notice) {
            this.post({ type: "notice", id, text: tab.notice });
          }
        }
        return;
      case "new":
        await vscode.commands.executeCommand("cabildo.new");
        return;
      case "link":
        if (/^https?:\/\//i.test(message.url)) {
          await vscode.env.openExternal(vscode.Uri.parse(message.url));
        }
        return;
    }
    const tab = this.open.get(message.id);
    if (!tab) {
      return;
    }
    switch (message.type) {
      case "size":
        this.sized(tab, message.rows, message.cols);
        return;
      case "input":
        tab.viewer?.write(Buffer.from(message.data, message.binary ? "binary" : "utf8"));
        return;
      case "show":
        if (tab.state === "disconnected") {
          tab.state = "attached"; // the page answers `reset` with its size, which connects
          tab.notice = "";
          this.post({ type: "reset", id: message.id });
        }
        this.activate(message.id);
        return;
      case "detach":
        this.detach(message.id);
        return;
      case "end":
        if (tab.state !== "ended") {
          await this.actions.end(tab.session, tab.details.title);
        }
        return;
    }
  }

  dispose(): void {
    this.drop();
  }

  private activate(id: string): void {
    this.active = id;
    this.publish();
    this.actions.shown(id);
  }

  /** Close every connection, and keep the tabs. */
  private drop(): void {
    for (const tab of this.open.values()) {
      tab.viewer?.close();
      tab.viewer = undefined;
      tab.chunks = [];
    }
  }

  private sized(tab: Tab, rows: number, cols: number): void {
    if (tab.state !== "attached") {
      return;
    }
    if (tab.viewer) {
      tab.viewer.resize({ rows, cols });
      return;
    }
    const id = tab.session.id;
    tab.viewer = attachSession(hostSocket(process.env), id, { rows, cols }, (event) =>
      this.heard(tab, event),
    );
  }

  private heard(tab: Tab, event: ViewerEvent): void {
    const id = tab.session.id;
    if (event.type === "data") {
      tab.received += event.data.length;
      // One message for what arrives together: Claude redraws its screen in many pieces.
      if (tab.chunks.push(event.data) === 1) {
        setImmediate(() => this.flush(tab));
      }
      return;
    }
    this.flush(tab);
    tab.viewer = undefined;
    tab.state = event.type === "exit" ? "ended" : "disconnected";
    tab.notice =
      event.type === "exit"
        ? `The session ended (exit code ${event.code}).`
        : `Disconnected: ${event.reason}. Click the tab to attach again.`;
    this.post({ type: "notice", id, text: tab.notice });
    this.publish();
  }

  private flush(tab: Tab): void {
    if (tab.chunks.length) {
      // A copy: `concat` may return a part of a larger shared buffer.
      const data = new Uint8Array(Buffer.concat(tab.chunks.splice(0)));
      this.post({ type: "data", id: tab.session.id, data });
    }
  }

  private describe(tab: Tab): void {
    const frame = this.frame;
    const item = frame?.items.find((i) => i.host_id === tab.session.id);
    if (!frame || !item) {
      return;
    }
    // The frame has the numbers of the selected conversation only: the others keep theirs.
    const detail = frame.selected === item.id ? frame.detail : null;
    tab.details = {
      title: item.title,
      mark: item.mark,
      model: detail?.model ?? tab.details.model,
      context: detail ? detail.context : tab.details.context,
      cost: detail?.cost ?? tab.details.cost,
    };
  }

  private info(id: string): TabInfo {
    const tab = this.open.get(id)!;
    return { id, cwd: tab.session.cwd, state: tab.state, ...tab.details };
  }

  /** Send the tabs when they differ from what the page has. */
  private publish(): void {
    const tabs = [...this.open.keys()].map((id) => this.info(id));
    const next = JSON.stringify([tabs, this.active]);
    if (this.ready && next !== this.sent) {
      this.sent = next;
      this.post({ type: "tabs", tabs, active: this.active });
    }
  }

  private post(message: ToConsole): void {
    if (this.ready) {
      void this.view?.webview.postMessage(message);
    }
  }
}

/** The page. `console.js` builds all of it. xterm.js writes style elements, hence inline styles. */
function html(webview: vscode.Webview, media: vscode.Uri): string {
  const file = (...name: string[]): string =>
    webview.asWebviewUri(vscode.Uri.joinPath(media, ...name)).toString();
  const nonce = randomBytes(16).toString("base64");
  const source = webview.cspSource;
  const policy = `default-src 'none'; style-src ${source} 'unsafe-inline'; font-src ${source}; script-src 'nonce-${nonce}'`;
  const scripts = [
    ["vendor", "xterm.js"],
    ["vendor", "addon-fit.js"],
    ["vendor", "addon-web-links.js"],
    ["vendor", "addon-unicode11.js"],
    ["console.js"],
  ];
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${policy}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="${file("vendor", "xterm.css")}">
<link rel="stylesheet" href="${file("console.css")}">
<title>cabildo console</title>
</head>
<body>
${scripts.map((s) => `<script nonce="${nonce}" src="${file(...s)}"></script>`).join("\n")}
</body>
</html>`;
}
