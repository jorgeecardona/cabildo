/** The text the extension shows. No `vscode` import, so tests run it under plain Node. */

import * as path from "node:path";

import { UNREAD_LIMIT } from "./client";
import type { HostSession, Status } from "./types";

const PREVIEW = 80;
const LINES = 10;

/** The status bar label: agent count, then unread count. */
export function statusText(status: Status): string {
  if (status.kind === "offline") {
    return "$(debug-disconnect) cabildo";
  }
  const count = status.unread.length;
  const unread = count >= UNREAD_LIMIT ? `${UNREAD_LIMIT}+` : String(count);
  return `$(organization) ${status.agents.length} $(mail) ${unread}`;
}

/** The status bar tooltip, as plain text lines. */
export function statusTooltip(status: Status, now = new Date()): string {
  if (status.kind === "offline") {
    return `cabildo daemon not reachable: ${status.reason}`;
  }
  const agents = status.agents
    .slice(0, LINES)
    .map((a) => `${a.display} (${a.presence}${a.project_name ? `, ${a.project_name}` : ""})`);
  const newest = status.unread.slice(-LINES).reverse();
  const messages = newest.map((m) => {
    const sent = shortDate(m.created_at, now);
    return `${m.urgent ? "! " : ""}${sent} ${m.from_display}: ${preview(m.body)}`;
  });
  return [
    `Agents: ${status.agents.length}`,
    ...agents,
    ...more(status.agents.length - agents.length),
    "",
    `Unread: ${status.unread.length}`,
    ...messages,
    ...more(status.unread.length - messages.length),
    "",
    "Click to open a Claude session.",
  ].join("\n");
}

/**
 * When a message was sent, short: the time for today, else the day and the time. A day of
 * another year shows the year in place of the time.
 */
export function shortDate(seconds: number, now: Date): string {
  const sent = new Date(seconds * 1000);
  const time = sent.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (sent.toDateString() === now.toDateString()) {
    return time;
  }
  const day = { day: "numeric", month: "short" } as const;
  return sent.getFullYear() === now.getFullYear()
    ? `${sent.toLocaleDateString(undefined, day)} ${time}`
    : sent.toLocaleDateString(undefined, { ...day, year: "numeric" });
}

/** A body on one line, cut short. */
export function preview(body: string): string {
  const line = body.replace(/\s+/g, " ").trim();
  return line.length > PREVIEW ? `${line.slice(0, PREVIEW - 1)}…` : line;
}

function more(count: number): string[] {
  return count > 0 ? [`and ${count} more`] : [];
}

/** Whether `cwd` is `folder` or under it. */
export function within(folder: string, cwd: string): boolean {
  const relative = path.relative(folder, cwd);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

/** One row of the session picker. */
export interface SessionRow {
  readonly label: string;
  readonly description: string;
  readonly detail: string;
  readonly session: HostSession;
}

/** The picker rows: sessions under an open folder first, then the newest first. */
export function sessionRows(
  sessions: readonly HostSession[],
  folders: readonly string[],
): SessionRow[] {
  const here = (s: HostSession): number => (folders.some((f) => within(f, s.cwd)) ? 1 : 0);
  return [...sessions]
    .sort((a, b) => here(b) - here(a) || b.started_at - a.started_at)
    .map((session) => ({
      label: path.basename(session.cwd) || session.cwd,
      description: `${session.id} · pid ${session.pid}`,
      detail: `${session.cwd} · ${session.command.join(" ").slice(0, PREVIEW)}`,
      session,
    }));
}

/** The picker row that starts a session instead of picking one. */
export interface NewRow {
  readonly label: string;
  readonly description: string;
  readonly detail: string;
  readonly session?: undefined;
}

export const NEW_ROW: NewRow = {
  label: "$(add) New Claude session in this folder",
  description: "",
  detail: "",
};

/** The session rows, then the row that starts one when a folder is open. */
export function pickerRows(
  sessions: readonly HostSession[],
  folders: readonly string[],
): (SessionRow | NewRow)[] {
  const rows = sessionRows(sessions, folders);
  return folders.length ? [...rows, NEW_ROW] : rows;
}
