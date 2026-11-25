/** The two requests the extension makes: to the daemon's broker and to the session host. */

import type { Agent, HostSession, Message, Status } from "./types";
import { askLine } from "./wire";

/** The person on the bus. Mirrors `store.HUMAN_SESSION` and `store.HUMAN_HANDLE`. */
export const HUMAN = {
  session: "human:jorge",
  handle: "jorge",
  project: null,
  project_name: null,
} as const;

/** The most unread messages one request asks for. The status bar shows `200+` at the cap. */
export const UNREAD_LIMIT = 200;

interface BrokerReply {
  readonly ok: boolean;
  readonly result?: unknown;
  readonly error?: { readonly message?: string };
}

async function bus<T>(socketPath: string, op: string, params: object): Promise<T> {
  const reply = (await askLine(socketPath, { op, params, identity: HUMAN })) as BrokerReply;
  if (!reply.ok) {
    throw new Error(reply.error?.message ?? `the daemon refused ${op}`);
  }
  return reply.result as T;
}

/** The active agents and the person's unread messages. Never throws: a failure is `offline`. */
export async function fetchStatus(socketPath: string): Promise<Status> {
  try {
    const [agents, unread] = await Promise.all([
      bus<Agent[]>(socketPath, "list_agents", {}),
      bus<Message[]>(socketPath, "inbox", { unread_only: true, limit: UNREAD_LIMIT }),
    ]);
    return { kind: "online", agents, unread };
  } catch (error) {
    return { kind: "offline", reason: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * The sessions the host owns. Empty when no host runs: the host starts only when something
 * starts a session in it.
 */
export async function listSessions(socketPath: string): Promise<HostSession[]> {
  let reply: { ok: boolean; error?: string; sessions?: HostSession[] };
  try {
    reply = (await askLine(socketPath, { op: "list" })) as typeof reply;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code === "ENOENT" || code === "ECONNREFUSED") {
      return [];
    }
    throw error;
  }
  if (!reply.ok) {
    throw new Error(reply.error ?? "the session host refused the request");
  }
  return reply.sessions ?? [];
}
