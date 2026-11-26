/** The records the cabildo daemon and session host send, as the extension reads them. */

/** One active session on the bus (`list_agents`). */
export interface Agent {
  readonly session_id: string;
  readonly display: string;
  readonly presence: string;
  readonly project_name: string | null;
  readonly cwd: string | null;
  readonly claude_pid: number | null;
}

/** One message in an inbox (`inbox`). */
export interface Message {
  readonly id: number;
  readonly from_session: string;
  readonly from_display: string;
  readonly topic: string | null;
  readonly body: string;
  readonly urgent: boolean;
  /** Seconds since 1970. */
  readonly created_at: number;
}

/** One Claude session the session host owns (`list`). Mirrors `host.Record`. */
export interface HostSession {
  readonly id: string;
  readonly cwd: string;
  readonly command: readonly string[];
  readonly pid: number;
  readonly rows: number;
  readonly cols: number;
  readonly started_at: number;
  readonly group: string;
  readonly session_id: string;
  readonly adopted: boolean;
  readonly parent: string;
}

/** One line of `cabildo stats`. Mirrors `stats.Row`. */
export interface StatRow {
  readonly label: string;
  readonly value: string;
  readonly level: "" | "warn" | "alert";
  readonly detail: string;
}

/** The rows of one source. Mirrors `stats.Group`. */
export interface StatGroup {
  readonly key: string;
  readonly title: string;
  readonly rows: readonly StatRow[];
}

/** What `cabildo stats` printed, or why there is nothing. */
export type Stats =
  | { readonly kind: "online"; readonly groups: readonly StatGroup[] }
  | { readonly kind: "unavailable"; readonly reason: string };

/** What the daemon answered. `offline` means it did not answer. */
export type Status =
  | { readonly kind: "online"; readonly agents: readonly Agent[]; readonly unread: readonly Message[] }
  | { readonly kind: "offline"; readonly reason: string };
