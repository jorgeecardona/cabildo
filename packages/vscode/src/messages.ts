/** Reading, answering and sending messages from the side bar. */

import * as vscode from "vscode";

import { markRead, sendMessage } from "./client";
import { messageText } from "./nodes";
import { brokerSocket } from "./paths";
import type { Message } from "./types";

const SCHEME = "cabildo-message";

export class Messages implements vscode.TextDocumentContentProvider, vscode.Disposable {
  private readonly opened = new Map<string, string>();
  private readonly registration = vscode.workspace.registerTextDocumentContentProvider(SCHEME, this);

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.opened.get(uri.path) ?? "Open the message again from the cabildo side bar.\n";
  }

  /** Show the whole message in a read-only tab, and mark it read. */
  async open(message: Message): Promise<void> {
    // `.md`: VS Code wraps long lines in Markdown. The text is shown, never rendered.
    const uri = vscode.Uri.from({
      scheme: SCHEME,
      path: `/${message.from_display} ${message.id}.md`,
    });
    this.opened.set(uri.path, messageText(message));
    await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri), {
      preview: true,
    });
    await markRead(brokerSocket(process.env), [message.id]);
  }

  /** Ask for a text and send it to `to` as a direct message. False when nothing was sent. */
  async send(to: string, name: string): Promise<boolean> {
    const body = await vscode.window.showInputBox({
      title: `Message to ${name}`,
      prompt: "Sent on the cabildo bus as you. Enter sends, Escape cancels.",
      ignoreFocusOut: true,
    });
    if (!body?.trim()) {
      return false;
    }
    await sendMessage(brokerSocket(process.env), to, body);
    void vscode.window.setStatusBarMessage(`cabildo: sent to ${name}`, 5000);
    return true;
  }

  /** Answer the sender, then mark the message read. */
  async reply(message: Message): Promise<void> {
    if (await this.send(message.from_session, message.from_display)) {
      await markRead(brokerSocket(process.env), [message.id]);
    }
  }

  async markRead(message: Message): Promise<void> {
    await markRead(brokerSocket(process.env), [message.id]);
  }

  async markAllRead(): Promise<void> {
    const yes = "Mark All Read";
    const answer = await vscode.window.showWarningMessage(
      "Mark every unread cabildo message as read?",
      { modal: true },
      yes,
    );
    if (answer === yes) {
      await markRead(brokerSocket(process.env), "all");
    }
  }

  dispose(): void {
    this.registration.dispose();
  }
}
