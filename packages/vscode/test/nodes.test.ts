import * as assert from "node:assert/strict";
import { test } from "node:test";

import { messageNodes, messageText } from "../src/nodes";
import type { Message } from "../src/types";
import { shortDate } from "../src/view";

test("messages are listed newest first, and open with the whole body", () => {
  const message = (id: number): Message => ({
    id, from_session: "s", from_display: "@a", topic: "general", body: `one\ntwo ${id}`,
    urgent: true, created_at: 0,
  });
  const now = new Date(0);
  const nodes = messageNodes([message(1), message(2)], now);
  const sent = shortDate(0, now);
  assert.deepEqual(nodes.map((n) => n.description), [`${sent} · one two 2`, `${sent} · one two 1`]);
  assert.match(messageText(message(3)), /^@a to #general · .* · urgent\n\none\ntwo 3\n$/);
});
