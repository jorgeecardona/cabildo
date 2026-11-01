// cabildo-push — Tier 1 push delivery (GitLab #30).
//
// cabildo messaging is pull: a message lands in SQLite and an idle session,
// running no turn and firing no hook, never notices until it next acts. This
// mod is the doorbell. A `$.clock` timer runs while the session is idle,
// reads new rows by an integer cursor through the `cabildo` CLI, and — when
// the sender clears the anti-nuisance gates — submits ONE tiny prompt that
// starts a turn and tells the model to drain its inbox. Bodies never ride the
// prompt; the model reads them through the existing `inbox` tool.
//
// The feed is the repo's own `cabildo wait --json` (an id cursor): one child
// process that blocks until a row lands past the cursor, prints it in `pull`'s
// shape and exits (#37). The inbox definition stays single-sourced in Python
// and the mod never touches the schema.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { PushAsk } from '../types'

const PLUGIN = 'cabildo-push'

// Pause before respawning `cabildo wait` after it failed or printed nothing,
// and after a batch that could not move the cursor (an active ask holds it).
const RESPAWN_MS = 1000
// Token bucket: at most BUCKET_CAP auto-turns held, one refilled per REFILL_MS.
const BUCKET_CAP = 10
const REFILL_MS = 120_000
// The human is driving if they typed here within this window; while so, push
// coalesces to a badge/toast instead of starting a turn.
const HUMAN_DEFER_MS = 120_000
// cabildo_ask holds the asker's turn this long waiting for a reply, then hands
// the late reply off to the doorbell. Kept under a tool.call hook's 10 s
// own-time budget (each $.clock.sleep counts against it).
const ASK_WAIT_MS = 6_000
const ASK_POLL_MS = 1_000
// On the ask path we want the reply whole, not a preview.
const ASK_PREVIEW = 100_000
// An ask whose reply never comes is pruned after this, so a stale entry cannot
// linger as a permanent accept-list bypass.
const ASK_TTL_MS = 600_000

const sessionIdA = atom({ plugin: 'cabildo-push', key: 'sessionId' } as const, null)
const cursorA = atom({ plugin: 'cabildo-push', key: 'cursor' } as const, 0)
const bucketA = atom({ plugin: 'cabildo-push', key: 'bucket' } as const, {
  tokens: BUCKET_CAP,
  refillAt: 0,
})
const lastHumanA = atom({ plugin: 'cabildo-push', key: 'lastHumanAt' } as const, 0)
const awaitingA = atom({ plugin: 'cabildo-push', key: 'awaiting' } as const, {})
// Wake labels held back by DND / human-present / empty bucket. A suppressed wake
// is retried by the doorbell's retry tick instead of being dropped with the row.
const deferredA = atom({ plugin: 'cabildo-push', key: 'deferred' } as const, [] as number[])
// How often a held-back wake is retried while the doorbell waits for new rows.
const RETRY_MS = 30_000

// Module-level, reset on hot reload (fine): one doorbell loop per module, so a
// second session.start never runs two loops on the same cursor.
let doorbellRunning = false
let retryRunning = false

// --------------------------------------------------------------------------- //
// Shelling the cabildo CLI
// --------------------------------------------------------------------------- //
type PullRow = {
  id: number
  from_session: string
  from_display: string
  from_robot: string
  from_human: boolean
  // cabildo decides: a DM from a roommate or a paired machine's agent, or an
  // urgent room post. Absent from an older cabildo, which reads as false.
  wakes?: boolean
  to_session: string | null
  topic: string | null
  urgent: boolean
  read: boolean
  created_at: number
  preview: string
  truncated: boolean
}

async function cab($: EngineInterface, argv: readonly string[]): Promise<unknown> {
  const bin = (await $.env.get('CABILDO_BIN')) || 'cabildo'
  const r = await $.process.run([bin, ...argv])
  if (r.exitCode !== 0) {
    throw new Error(`cabildo ${argv[0]} exited ${r.exitCode}: ${r.stderr.trim()}`)
  }
  const out = r.stdout.trim()
  return out ? JSON.parse(out) : null
}

async function home($: EngineInterface): Promise<string> {
  const h = await $.env.get('CABILDO_HOME')
  if (h) return h.replace(/\/+$/, '')
  const base = (await $.env.get('HOME')) ?? '~'
  return `${base.replace(/\/+$/, '')}/.cabildo`
}

// Global Do-Not-Disturb: one human, many sessions. The file's presence means
// DND unless its content reads off/0/false. ($.fs cannot delete, so the toggle
// is content-based rather than existence-based.)
async function isDnd($: EngineInterface): Promise<boolean> {
  const f = `${await home($)}/dnd`
  try {
    if (!(await $.fs.exists(f))) return false
    const t = (await $.fs.read(f)).trim().toLowerCase()
    return t !== 'off' && t !== '0' && t !== 'false'
  } catch {
    return false
  }
}

// Who else this session accepts an interrupt (auto-turn) from, beyond the human,
// replies to its own asks, and rows cabildo marks `wakes` (a DM from a roommate
// or a paired machine's agent, an urgent room post). One id / robot-name per line in ~/.cabildo/push-accept;
// a bare `*` or `all` accepts everyone. Missing file means: nobody else. Handles are deliberately NOT matched:
// they are self-assigned and non-unique, so a peer could set a handle to a
// listed name and win an auto-turn. Robot names derive from the session id and
// are spoof-resistant.
async function acceptSet($: EngineInterface): Promise<Set<string>> {
  const f = `${await home($)}/push-accept`
  try {
    const text = await $.fs.read(f)
    return new Set(
      text
        .split('\n')
        .map((l) => l.trim().replace(/^@/, ''))
        .filter((l) => l.length > 0 && !l.startsWith('#')),
    )
  } catch {
    return new Set()
  }
}

function accepts(accept: Set<string>, m: PullRow): boolean {
  if (accept.has('*') || accept.has('all')) return true
  return accept.has(m.from_session) || accept.has(m.from_robot)
}

// A sender label safe to drop into an auto-submitted prompt: a peer can set its
// own handle to arbitrary text (newlines, instructions), so anything that is
// not a short word-ish token falls back to the derived robot name.
function safeLabel(m: PullRow): string {
  const d = m.from_display || m.from_robot
  return /^@?[\w.-]{1,40}$/.test(d) ? d : m.from_robot
}

function uniq(xs: readonly string[]): string[] {
  return [...new Set(xs)]
}

// A `tool.call` hook serving a registered tool resolves to `{ result }` whose
// value is the model-visible text (a string), or `{ deny }` to hand the model
// an error result. The MCP `{ content:[...], isError }` envelope is NOT the
// shape here — the engine validates the resolved value against the tool's
// output schema (string/array/undefined for a schema-less custom tool) and
// rejects an object, which is what broke the first live call.
function toolResult(text: string, isError = false): { result: string } | { deny: string } {
  return isError ? { deny: text } : { result: text }
}

// --------------------------------------------------------------------------- //
// Session identity + token bucket
// --------------------------------------------------------------------------- //
type WhoAmI = { session_id?: string; is_human?: boolean; cursor?: number }

async function whoami($: EngineInterface): Promise<WhoAmI | null> {
  try {
    return (await cab($, ['whoami', '--json'])) as WhoAmI
  } catch {
    return null
  }
}

async function ensureSession($: EngineInterface): Promise<string | null> {
  const known = await read($, sessionIdA)
  if (known) return known
  const me = await whoami($)
  const sid = me?.session_id ?? null
  // The pid walk can resolve to the human before this session's row is bound;
  // don't latch onto that; the doorbell retries after RESPAWN_MS.
  if (!sid || me?.is_human) return null
  await update($, sessionIdA, () => sid)
  await update($, cursorA, (c) => (c > 0 ? c : (me?.cursor ?? 0)))
  return sid
}

async function spendToken($: EngineInterface, now: number): Promise<boolean> {
  let spent = false
  await update($, bucketA, (b) => {
    let tokens = b.tokens
    let refillAt = b.refillAt || now + REFILL_MS
    while (now >= refillAt && tokens < BUCKET_CAP) {
      tokens = Math.min(BUCKET_CAP, tokens + 1)
      refillAt += REFILL_MS
    }
    if (refillAt <= now) refillAt = now + REFILL_MS
    if (tokens > 0) {
      tokens -= 1
      spent = true
    }
    return { tokens, refillAt }
  })
  return spent
}

// --------------------------------------------------------------------------- //
// The doorbell
// --------------------------------------------------------------------------- //
type WaitOut =
  | { rebound: true; session_id: string; cursor: number }
  | { rebound?: undefined; cursor: number; messages: PullRow[] }

// One `cabildo wait` child, run to its exit; its last stdout line, parsed.
// Pieces are not lines, so the whole output is collected first. Null when it
// printed nothing (it failed, or its parent check fired).
async function waitOnce($: EngineInterface, sid: string, after: number): Promise<WaitOut | null> {
  const bin = (await $.env.get('CABILDO_BIN')) || 'cabildo'
  const child = $.process.spawn({
    argv: [bin, 'wait', `--session=${sid}`, `--after=${after}`, '--json'],
  })
  let out = ''
  for await (const { stream, text } of child) {
    if (stream === 'stdout') out += text
  }
  const line = out.trim().split('\n').pop()
  return line ? (JSON.parse(line) as WaitOut) : null
}

// The doorbell: for the session's life, block in `cabildo wait`, handle what
// it returns, start the next one. Idle costs one sleeping process, not a spawn
// per second (#37). Ends when a $ call throws, which is the module unloading.
async function runDoorbell($: EngineInterface): Promise<void> {
  if (doorbellRunning) return
  doorbellRunning = true
  try {
    for (;;) {
      const sid = await ensureSession($)
      if (!sid) {
        await $.clock.sleep(RESPAWN_MS)
        continue
      }
      const after = await read($, cursorA)
      let r: WaitOut | null = null
      try {
        r = await waitOnce($, sid, after)
      } catch (err) {
        $.ui.log(`${PLUGIN}: wait failed: ${String(err)}`, { to: 'debug' })
      }
      if (r === null) {
        // Unknown session (a retired id) also lands here: re-learn it.
        await update($, sessionIdA, () => null)
        await $.clock.sleep(RESPAWN_MS)
        continue
      }
      if (r.rebound) {
        // A /clear: new session id, same pid, no session.start.
        const next = r
        await update($, sessionIdA, () => next.session_id)
        await update($, cursorA, () => next.cursor)
        await update($, awaitingA, () => ({})) // the old session's asks are moot
        continue
      }
      let moved = true
      try {
        moved = await handleBatch($, sid, after, r.messages)
      } catch (err) {
        $.ui.log(`${PLUGIN}: doorbell failed: ${String(err)}`, { to: 'debug' })
      }
      if (!moved) await $.clock.sleep(RESPAWN_MS)
    }
  } catch {
    // the module is unloading; a reload starts a fresh loop
  } finally {
    doorbellRunning = false
  }
}

// Gates one batch and maybe starts a turn. False when the cursor could not
// move (an active ask owns the first row), so the caller paces the respawn.
async function handleBatch(
  $: EngineInterface,
  sid: string,
  after: number,
  msgs: readonly PullRow[],
): Promise<boolean> {
  if (msgs.length === 0) return true

  const now = await $.clock.now()
  await pruneAwaiting($, now)
  const awaiting = await read($, awaitingA)
  const dnd = await isDnd($)
  const humanActive = now - (await read($, lastHumanA)) < HUMAN_DEFER_MS
  const accept = await acceptSet($)

  const wakers: PullRow[] = []
  const toasts: PullRow[] = []
  const clears: string[] = [] // handed-off asks a fired wake would resolve
  let advanceTo = after
  for (const m of msgs) {
    const aw = awaiting[m.from_session]
    const isReply = aw !== undefined && m.id > aw.askId && m.to_session === sid
    // An active (not handed-off) ask owns a reply from this target: stop here
    // and do NOT advance the cursor past it, so the reply is re-pulled (and,
    // once the ask hands off, woken on) rather than swallowed. Everything from
    // here on is reprocessed by the next wait, cheap and idempotent.
    if (isReply && aw && !aw.handoff) break
    advanceTo = m.id
    if (m.read) continue
    if (isReply && aw?.handoff) {
      wakers.push(m)
      clears.push(m.from_session)
      continue
    }
    if (m.from_human || m.wakes === true || accepts(accept, m)) wakers.push(m)
    else toasts.push(m)
  }
  await update($, cursorA, () => advanceTo)
  const moved = advanceTo !== after

  if (wakers.length && !dnd && !humanActive && (await spendToken($, now))) {
    const who = uniq(wakers.map(safeLabel)).join(', ')
    void $.prompt.submit({
      text: `cabildo: ${wakers.length} new message(s) from ${who} — call the inbox tool to read them.`,
    })
    await update($, deferredA, () => []) // this turn drains the inbox, held wakes included
    await cab($, ['meter', `--session=${sid}`, '--event=push_wake', '--json']).catch(() => {})
    if (clears.length) {
      await update($, awaitingA, (a) => {
        const next = { ...a }
        for (const t of clears) delete next[t]
        return next
      })
    }
    if (toasts.length) notifyPending($, toasts)
    else $.ui.status(undefined)
    return moved
  }

  // Suppressed (DND / human present / bucket empty): coalesce everything into a
  // badge + toast, start no turn, and remember the wakers so `retryDeferred`
  // starts the turn once the gate opens.
  if (wakers.length) {
    const ids = wakers.map((m) => m.id)
    await update($, deferredA, (d) => [...new Set([...d, ...ids])])
  }
  // Handed-off ask entries are intentionally left in place so the interrupt grant survives to a later, un-suppressed wake.
  const pending = [...wakers, ...toasts]
  if (pending.length) notifyPending($, pending)
  return moved
}

// Start the turn a suppressed batch could not, once DND is off, the human has
// been away HUMAN_DEFER_MS, and the bucket has a token. Runs on its own timer
// because the doorbell is blocked in `cabildo wait` until a new row lands.
async function retryDeferred($: EngineInterface): Promise<void> {
  try {
    for (;;) {
      await $.clock.sleep(RETRY_MS)
      if ((await read($, deferredA)).length === 0) continue
      const now = await $.clock.now()
      if (await isDnd($)) continue
      if (now - (await read($, lastHumanA)) < HUMAN_DEFER_MS) continue
      const sid = await read($, sessionIdA)
      if (!sid) continue
      // Take the held ids and clear them in one update, so a second retry loop
      // (left over from a plugin reload) finds nothing and cannot wake twice.
      let held: number[] = []
      await update($, deferredA, (d) => {
        held = d
        return []
      })
      if (held.length === 0) continue
      // The session may have read them in a human turn meanwhile: wake only for
      // ids that are still unread.
      const r = (await cab($, [
        'pull',
        `--session=${sid}`,
        `--after=${Math.min(...held) - 1}`,
        '--preview-chars=0',
        '--json',
      ]).catch(() => null)) as { messages: PullRow[] } | null
      const unread = (r?.messages ?? []).filter((m) => held.includes(m.id) && !m.read)
      if (unread.length === 0) {
        $.ui.status(undefined)
        continue
      }
      if (!(await spendToken($, now))) {
        const ids = unread.map((m) => m.id)
        await update($, deferredA, (d) => [...new Set([...d, ...ids])])
        continue
      }
      const who = uniq(unread.map(safeLabel)).join(', ')
      void $.prompt.submit({
        text: `cabildo: ${unread.length} held message(s) from ${who} — call the inbox tool to read them.`,
      })
      await cab($, ['meter', `--session=${sid}`, '--event=push_wake_retry', '--json']).catch(
        () => {},
      )
      $.ui.status(undefined)
    }
  } catch {
    // the module is unloading
  }
}

async function pruneAwaiting($: EngineInterface, now: number): Promise<void> {
  const awaiting = await read($, awaitingA)
  const stale = Object.entries(awaiting).filter(([, a]) => now - a.at > ASK_TTL_MS)
  if (stale.length === 0) return
  await update($, awaitingA, (a) => {
    const next = { ...a }
    for (const [t] of stale) delete next[t]
    return next
  })
}

function notifyPending($: EngineInterface, pending: readonly PullRow[]): void {
  const who = uniq(pending.map(safeLabel)).join(', ')
  $.ui.toast(`cabildo: ${pending.length} new from ${who}`)
  $.ui.status(`cabildo: ${pending.length} unread — call the inbox tool`)
}

// --------------------------------------------------------------------------- //
// Registration
// --------------------------------------------------------------------------- //
export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    await $.tool.register({
      name: 'cabildo_ask',
      description:
        'Send a direct message to another cabildo session and wait briefly for a reply, ' +
        'holding this turn. If no reply arrives in a few seconds, it returns and the reply ' +
        'wakes you when it lands. Params: to (session id/@handle/robot name), body, urgent?.',
      inputSchema: {
        type: 'object',
        properties: {
          to: { type: 'string', description: 'recipient: session id, @handle, or robot name' },
          body: { type: 'string', description: 'the message' },
          urgent: { type: 'boolean', description: 'flag it urgent' },
        },
        required: ['to', 'body'],
      },
      isDeferred: false,
    })
    await $.tool.register({
      name: 'cabildo_reply',
      description:
        'Reply to a cabildo session that messaged you (a direct message back). ' +
        'Params: to (session id/@handle/robot name), body.',
      inputSchema: {
        type: 'object',
        properties: {
          to: { type: 'string', description: 'who to reply to' },
          body: { type: 'string', description: 'the reply' },
        },
        required: ['to', 'body'],
      },
      isDeferred: false,
    })
    await $.command.register({
      name: 'cabildo-dnd',
      description: 'Toggle cabildo push Do-Not-Disturb (suppress auto-turns from push).',
    })

    // Bind the session id and seed the cursor to "now" so we wake on what
    // arrives after start, not on the backlog.
    await ensureSession($)
    void runDoorbell($)
    if (!retryRunning) {
      retryRunning = true
      void retryDeferred($).finally(() => {
        retryRunning = false
      })
    }
    return next(e)
  })

  // Track local human presence so push defers while they are driving. Never let
  // a bookkeeping failure drop the human's prompt.
  on('prompt.submit', async ($, e, next) => {
    if (e.origin?.kind === 'composer' || e.origin?.kind === 'bridge') {
      const now = await $.clock.now()
      await update($, lastHumanA, () => now)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'cabildo-dnd' }, async ($) => {
    const f = `${await home($)}/dnd`
    const on_ = await isDnd($)
    await $.fs.write(f, on_ ? 'off\n' : 'on\n')
    return {
      text: on_
        ? 'cabildo push DND off — auto-turns resume.'
        : 'cabildo push DND on — auto-turns suppressed.',
    }
  })

  on('tool.call', { tool: 'mcp__cabildo-push__cabildo_ask' }, async ($, e) => {
    const sid = await ensureSession($)
    if (!sid) return toolResult('cabildo-push is not bound to a session yet; try again shortly.', true)
    const to = String(e.to ?? '').trim()
    const body = String(e.body ?? '').trim()
    if (!to || !body) return toolResult('cabildo_ask needs `to` and `body`.', true)

    let sent: { id: number; to: string } | null
    try {
      sent = (await cab($, [
        'send',
        `--session=${sid}`,
        `--to=${to}`,
        '--json',
        ...(e.urgent ? ['--urgent'] : []),
        '--',
        body,
      ])) as typeof sent
    } catch (err) {
      return toolResult(`cabildo_ask send failed: ${String(err)}`, true)
    }
    if (!sent) return toolResult('cabildo_ask: send returned nothing.', true)
    const askId = sent.id
    const target = sent.to
    const at = await $.clock.now()

    // The ask owns the reply while it waits (handoff: false).
    await update($, awaitingA, (a) => ({ ...a, [target]: { askId, handoff: false, at } }))

    const lookForReply = async (): Promise<PullRow | undefined> => {
      const r = (await cab($, [
        'pull',
        `--session=${sid}`,
        `--after=${askId}`,
        `--preview-chars=${ASK_PREVIEW}`,
        '--json',
      ])) as { messages: PullRow[] } | null
      // DM-only: a routine channel post by the target is not "the reply".
      return (r?.messages ?? []).find((m) => m.from_session === target && m.to_session === sid)
    }

    const consume = async (reply: PullRow) => {
      await cab($, ['mark-read', `--session=${sid}`, `--ids=${reply.id}`, '--json']).catch(() => {})
      await update($, awaitingA, (a) => {
        const next = { ...a }
        delete next[target]
        return next
      })
      return toolResult(`${safeLabel(reply)} replied (#${reply.id}):\n${reply.preview}`)
    }

    const deadline = at + ASK_WAIT_MS
    while ((await $.clock.now()) < deadline) {
      await $.clock.sleep(ASK_POLL_MS)
      let reply: PullRow | undefined
      try {
        reply = await lookForReply()
      } catch {
        continue
      }
      if (reply) return consume(reply)
    }
    // One final look so a reply in the last interval is not missed, then hand
    // off: the doorbell holds the cursor at this target, so a later reply still
    // wakes the session ("you asked, you get woken").
    const last = await lookForReply().catch(() => undefined)
    if (last) return consume(last)
    await update($, awaitingA, (a) => ({ ...a, [target]: { askId, handoff: true, at } }))
    return toolResult(
      `Sent #${askId} to ${target}. No reply within ${ASK_WAIT_MS / 1000}s — ` +
        `I'll wake you when it arrives.`,
    )
  }).catch(async ($) => {
    // Cut short (budget/throw): never leave a handoff:false entry, which would
    // silently swallow every future message from that target.
    await update($, awaitingA, (a) => {
      const next = { ...a }
      for (const t of Object.keys(next)) {
        if (!next[t].handoff) next[t] = { ...next[t], handoff: true }
      }
      return next
    }).catch(() => {})
    return toolResult('cabildo_ask was cut short; the message may still have been sent.', true)
  })

  on('tool.call', { tool: 'mcp__cabildo-push__cabildo_reply' }, async ($, e) => {
    const sid = await ensureSession($)
    if (!sid) return toolResult('cabildo-push is not bound to a session yet; try again shortly.', true)
    const to = String(e.to ?? '').trim()
    const body = String(e.body ?? '').trim()
    if (!to || !body) return toolResult('cabildo_reply needs `to` and `body`.', true)
    try {
      const r = (await cab($, ['send', `--session=${sid}`, `--to=${to}`, '--json', '--', body])) as {
        id: number
        to: string
      }
      return toolResult(`Replied to ${r.to} (#${r.id}).`)
    } catch (err) {
      return toolResult(`cabildo_reply failed: ${String(err)}`, true)
    }
  }).catch(() => toolResult('cabildo_reply was cut short.', true))
}
