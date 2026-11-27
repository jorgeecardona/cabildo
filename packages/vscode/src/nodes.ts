/** The rows of the unread messages. No `vscode` import, so tests run it under plain Node. */

import type { Message } from "./types";
import { preview, shortDate } from "./view";

/** An unread message. */
export interface MessageNode {
  readonly kind: "message";
  readonly message: Message;
  readonly label: string;
  readonly description: string;
  readonly tooltip: string;
}

/** Unread messages, newest first, each with when it was sent. */
export function messageNodes(unread: readonly Message[], now = new Date()): MessageNode[] {
  return [...unread]
    .sort((a, b) => b.id - a.id)
    .map((message) => ({
      kind: "message",
      message,
      label: message.from_display,
      description: `${shortDate(message.created_at, now)} · ${preview(message.body)}`,
      tooltip: messageText(message),
    }));
}

/** A whole message, as the document that opens for it. */
export function messageText(message: Message): string {
  const to = message.topic ? `#${message.topic}` : "you";
  const sent = new Date(message.created_at * 1000).toLocaleString();
  const urgent = message.urgent ? " · urgent" : "";
  return `${message.from_display} to ${to} · ${sent}${urgent}\n\n${message.body}\n`;
}
