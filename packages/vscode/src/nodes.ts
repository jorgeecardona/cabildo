/** The rows of the sidebar. No `vscode` import, so tests run it under plain Node. */

import * as path from "node:path";

import { HUMAN } from "./client";
import type { Agent, HostSession, Message, StatGroup, StatRow } from "./types";
import { preview, sessionRows, within } from "./view";

/** The bus session the daemon posts its notices as. */
const DAEMON = "daemon";

const PRESENCE_ORDER = ["working", "idle"];

interface Text {
  readonly label: string;
  readonly description: string;
  readonly tooltip: string;
}

/** A hosted session, with the agent that runs in it once it is on the bus. */
export interface SessionNode extends Text {
  readonly kind: "session";
  readonly session: HostSession;
  readonly presence: string | undefined;
}

/** An agent on the bus, with the hosted session it runs in when the host owns it. */
export interface AgentNode extends Text {
  readonly kind: "agent";
  readonly agent: Agent;
  readonly session: HostSession | undefined;
}

/** An unread message. */
export interface MessageNode extends Text {
  readonly kind: "message";
  readonly message: Message;
}

/** One line of the stats. */
export interface StatNode extends Text {
  readonly kind: "stat";
  readonly id: string;
  readonly level: StatRow["level"];
}

/** A heading of the stats, with its lines. */
export interface StatGroupNode extends Text {
  readonly kind: "statGroup";
  readonly id: string;
  readonly rows: readonly StatNode[];
}

export type Node = SessionNode | AgentNode | MessageNode | StatGroupNode | StatNode;

function runs(agent: Agent, session: HostSession): boolean {
  return agent.claude_pid === session.pid || agent.session_id === session.session_id;
}

function folderName(cwd: string | null): string {
  return cwd ? path.basename(cwd) || cwd : "";
}

/** Hosted sessions: those under an open folder first, then the newest. */
export function sessionNodes(
  sessions: readonly HostSession[],
  agents: readonly Agent[],
  folders: readonly string[],
): SessionNode[] {
  return sessionRows(sessions, folders).map(({ session }) => {
    const agent = agents.find((a) => runs(a, session));
    const folder = folderName(session.cwd);
    return {
      kind: "session",
      session,
      presence: agent?.presence,
      label: agent?.display ?? folder,
      description: agent ? `${folder} · ${agent.presence}` : session.id,
      tooltip: [
        session.cwd,
        session.command.join(" "),
        `id ${session.id} · pid ${session.pid}`,
      ].join("\n"),
    };
  });
}

/** Agents: those under an open folder first, then working before idle, then by name. */
export function agentNodes(
  agents: readonly Agent[],
  sessions: readonly HostSession[],
  folders: readonly string[],
): AgentNode[] {
  const here = (a: Agent): number => (a.cwd && folders.some((f) => within(f, a.cwd!)) ? 0 : 1);
  const rank = (a: Agent): number => {
    const index = PRESENCE_ORDER.indexOf(a.presence);
    return index === -1 ? PRESENCE_ORDER.length : index;
  };
  return agents
    .filter((a) => a.session_id !== DAEMON && a.session_id !== HUMAN.session)
    .sort((a, b) => here(a) - here(b) || rank(a) - rank(b) || a.display.localeCompare(b.display))
    .map((agent) => ({
      kind: "agent",
      agent,
      session: sessions.find((s) => runs(agent, s)),
      label: agent.display,
      description: `${agent.project_name ?? folderName(agent.cwd)} · ${agent.presence}`,
      tooltip: [agent.display, agent.cwd ?? "", agent.presence].filter(Boolean).join("\n"),
    }));
}

/** Unread messages, newest first. */
export function messageNodes(unread: readonly Message[]): MessageNode[] {
  return [...unread]
    .sort((a, b) => b.id - a.id)
    .map((message) => ({
      kind: "message",
      message,
      label: message.from_display,
      description: preview(message.body),
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

/** The stats as headings and lines, in the order cabildo prints them. */
export function statNodes(groups: readonly StatGroup[]): StatGroupNode[] {
  return groups.map((group) => ({
    kind: "statGroup",
    id: group.key,
    label: group.title,
    description: "",
    tooltip: group.title,
    rows: group.rows.map((row, index) => ({
      kind: "stat",
      id: `${group.key}:${index}:${row.label}`,
      level: row.level,
      label: row.label,
      description: row.value,
      tooltip: [row.label, row.value, row.detail].filter(Boolean).join("\n"),
    })),
  }));
}
