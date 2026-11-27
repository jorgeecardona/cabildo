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

export type Mark = "" | "waiting" | "working" | "alive";
/** Amber from 70% of a context window or a plan limit, red from 90%. */
export type Level = "" | "amber" | "red";

/** One conversation of the panel's list. Mirrors `panel.Item`. */
export interface Item {
  /** Its newest session id, or `host:<id>` before it has a transcript. */
  readonly id: string;
  readonly title: string;
  readonly dir: string;
  readonly branch: string;
  readonly age: string;
  readonly mark: Mark;
  readonly group: string;
  readonly tags: readonly string[];
  readonly notes: readonly string[];
  /** Its cabildo name, or empty when it was never on the bus. */
  readonly agent: string;
  readonly rooms: readonly string[];
  readonly cpu: string;
  readonly mem: string;
  /** The session of the session host that runs it, or empty. */
  readonly host_id: string;
  /** The active bus session to send a message to, or empty. */
  readonly bus_id: string;
}

/** What the context window of the selected conversation holds. Mirrors `panel.Context`. */
export interface Context {
  readonly used: string;
  /** Empty when the model's window is not known. */
  readonly window: string;
  readonly percent: number;
  readonly level: Level;
}

/** The selected conversation; empty for a value not known. Mirrors `panel.Detail`. */
export interface Detail {
  readonly agent: string;
  readonly model: string;
  /** Null before the first reply. */
  readonly context: Context | null;
  readonly effort: string;
  readonly upstream: string;
  readonly tokens_in: string;
  readonly tokens_out: string;
  readonly tokens_cache: string;
  readonly cost: string;
  readonly cpu: string;
  readonly mem: string;
}

/** One limit of the subscription. Mirrors `panel.Limit`. */
export interface Limit {
  readonly name: string;
  readonly percent: number;
  readonly level: Level;
  /** Percent of the window's time that passed; -1 when not known. */
  readonly elapsed: number;
  readonly pace: string;
  readonly resets: string;
  /** When it reaches 100% at this pace; empty when that is after the reset. */
  readonly full: string;
}

/** The subscription's limits. Mirrors `panel.Plan`. */
export interface Plan {
  readonly name: string;
  readonly limits: readonly Limit[];
  /** Another upstream serves the selected conversation. */
  readonly elsewhere: boolean;
}

/** One upstream of the gateway. Mirrors `panel.Upstream`. */
export interface Upstream {
  readonly name: string;
  readonly ready: boolean;
  readonly requests: number;
  readonly failovers: number;
  readonly set_aside: string;
  readonly reason: string;
  readonly forced: boolean;
  readonly serving: boolean;
  readonly today: string;
  readonly week: string;
}

/** One column of the spend table. Mirrors `panel.Period`. */
export interface Period {
  readonly name: string;
  readonly total: string;
  readonly billed: string;
  readonly unpriced: string;
}

/** One model's row of the spend table. Mirrors `panel.Model`. */
export interface Model {
  readonly name: string;
  /** One per period. */
  readonly costs: readonly string[];
  /** Percent of the 7-day cost; -1 when none. */
  readonly share: number;
  readonly tokens: string;
}

/** What the transcripts say was spent. Mirrors `panel.Spend`. */
export interface Spend {
  readonly label: string;
  readonly periods: readonly Period[];
  readonly models: readonly Model[];
  readonly billed: boolean;
}

/** One request to the daemon. Mirrors `panel.Intent`. */
export interface Intent {
  readonly action: string;
  readonly project: string;
  readonly status: string;
  readonly kind: "queue" | "done" | "failed";
  readonly age: string;
  readonly by: string;
  readonly reason: string;
}

/** One running Jupyter runner. Mirrors `panel.Runner`. */
export interface Runner {
  readonly name: string;
  readonly paused: boolean;
  readonly cpu: string;
  readonly mem: string;
}

/** Everything the panel measures, together. Mirrors `panel.Machine`. */
export interface Machine {
  readonly cpu: string;
  readonly mem: string;
}

export type Scope = "here" | "subfolders" | "all";

/** Everything the panel shows. Mirrors `panel.Frame`. */
export interface Frame {
  readonly type: "frame";
  readonly scope: Scope;
  readonly folder: string;
  readonly items: readonly Item[];
  readonly older: number;
  readonly selected: string;
  /** Null when no conversation is selected. */
  readonly detail: Detail | null;
  readonly plan: Plan | null;
  readonly gateway: readonly Upstream[];
  readonly spend: Spend | null;
  /** Null: this machine never had one. */
  readonly intents: readonly Intent[] | null;
  readonly runners: readonly Runner[];
  readonly machine: Machine | null;
  readonly reading: boolean;
}

/** The answer to `open`: what a terminal runs. Mirrors `panel.Opened`. */
export interface Opened {
  readonly type: "open";
  readonly id: string;
  readonly host_id: string;
  readonly command: readonly string[];
  readonly cwd: string;
  readonly title: string;
  readonly elsewhere: boolean;
  readonly error: string;
}

/** A line to `cabildo panel`. */
export type PanelCommand =
  | { readonly cmd: "select"; readonly id: string }
  | { readonly cmd: "scope"; readonly scope: Scope }
  | { readonly cmd: "visible"; readonly visible: boolean }
  | { readonly cmd: "open"; readonly id: string; readonly force: boolean };

/** What the daemon answered. `offline` means it did not answer. */
export type Status =
  | { readonly kind: "online"; readonly agents: readonly Agent[]; readonly unread: readonly Message[] }
  | { readonly kind: "offline"; readonly reason: string };
