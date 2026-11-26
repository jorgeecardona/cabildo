import * as assert from "node:assert/strict";
import { test } from "node:test";

import { UNREAD_LIMIT } from "../src/client";
import type { HostSession, Message, Status } from "../src/types";
import { NEW_ROW, pickerRows, sessionRows, shortDate, statusText, statusTooltip } from "../src/view";

const message = (id: number): Message => ({
  id, from_session: "s", from_display: "@a", topic: null, body: "x\ny", urgent: false, created_at: 0,
});

function online(unread: number): Status {
  return {
    kind: "online",
    agents: [{
      session_id: "s", display: "@a", presence: "idle", project_name: "p", cwd: null, claude_pid: null,
    }],
    unread: Array.from({ length: unread }, (_, i) => message(i)),
  };
}

test("the label shows both counts, and marks the cap", () => {
  assert.equal(statusText(online(2)), "$(organization) 1 $(mail) 2");
  assert.equal(statusText(online(UNREAD_LIMIT)), `$(organization) 1 $(mail) ${UNREAD_LIMIT}+`);
  assert.equal(statusText({ kind: "offline", reason: "r" }), "$(debug-disconnect) cabildo");
});

test("the tooltip lists the newest messages on one line each", () => {
  const text = statusTooltip(online(12));
  assert.match(text, /@a \(idle, p\)/);
  assert.match(text, /@a: x y/);
  assert.match(text, /and 2 more/);
});

test("a date is the time today, the day before that, and the year before that", () => {
  const now = new Date(2026, 10, 26, 18, 0);
  const at = (...day: [number, number, number]) => new Date(...day, 9, 5).getTime() / 1000;
  const time = new Date(at(2026, 10, 26) * 1000)
    .toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  assert.equal(shortDate(at(2026, 10, 26), now), time);
  const earlier = shortDate(at(2026, 10, 24), now);
  assert.ok(earlier.includes("24") && earlier.endsWith(time) && earlier !== time, earlier);
  const lastYear = shortDate(at(2025, 10, 24), now);
  assert.ok(lastYear.includes("2025") && !lastYear.includes(time), lastYear);
});

test("sessions under an open folder come first, then the newest", () => {
  const session = (id: string, cwd: string, started_at: number): HostSession => ({
    id, cwd, started_at, command: ["claude"], pid: 1, rows: 24, cols: 80,
    group: "", session_id: "", adopted: false, parent: "",
  });
  const rows = sessionRows(
    [session("new", "/work/other", 3), session("in", "/work/repo/sub", 1),
     session("sibling", "/work/repo-2", 2), session("root", "/work/repo", 0)],
    ["/work/repo"],
  );
  assert.deepEqual(rows.map((r) => r.session.id), ["in", "root", "new", "sibling"]);
  assert.equal(rows[0]!.label, "sub");
});

test("the picker offers a new session only when a folder is open", () => {
  assert.deepEqual(pickerRows([], ["/work/repo"]), [NEW_ROW]);
  assert.deepEqual(pickerRows([], []), []);
});
