// State contract for the cabildo-push mod. The engine holds these values for
// the session and they survive a hot reload of the module's code; `claude
// plugin validate` holds every `$.state` key the module names to this file.

// A per-session, per-ask correlation record. cabildo has no `reply_to` column
// on `messages`, so the asker correlates a reply here instead of in the DB:
// `askId` is the id of the message the model sent with `cabildo_ask`,
// `handoff` says who owns the reply when it lands — the blocking ask hook
// while it still waits (`false`), or the doorbell once the hook gave up and
// left the late reply for a push wake (`true`) — and `at` is the `$.clock.now()`
// the ask was made, so a reply that never comes is pruned by TTL rather than
// lingering as a permanent accept-list bypass.
export type PushAsk = { askId: number; handoff: boolean; at: number }

// The token bucket that caps auto-turns: `tokens` left, and `refillAt`, the
// `$.clock.now()` stamp one token is added back at.
export type PushBucket = { tokens: number; refillAt: number }

declare module 'claude-code' {
  interface PluginState {
    'cabildo-push': {
      // This session's cabildo id, learned once via `cabildo whoami`.
      sessionId: string | null
      // The highest message id the doorbell has noticed. Its own pointer,
      // independent of read-state: gap-free by construction.
      cursor: number
      bucket: PushBucket
      // `$.clock.now()` of the last prompt the human typed here; recent means
      // the human is driving, so push defers to them.
      lastHumanAt: number
      // target session id -> the ask awaiting a reply from it.
      awaiting: Record<string, PushAsk>
      // Ids of messages whose wake was held back by DND, a present human, or an
      // empty bucket; retried on a timer, and only while still unread.
      deferred: number[]
      // `$.clock.now()` of a wake prompt no turn has ended after; 0 when none is queued.
      wakeAt: number
    }
  }
}
