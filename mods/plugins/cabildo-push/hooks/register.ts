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
// Tier 1 means zero broker changes: the doorbell polls. The feed is the
// repo's own `cabildo pull --json` (an id cursor), so the inbox definition
// stays single-sourced in Python and the mod never touches the schema.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { PushAsk } from '../types'

const PLUGIN = 'cabildo-push'
const HUMAN_SESSION = 'human:jorge'

// How often the idle doorbell looks. Each tick spawns one `cabildo` process,
// so this is deliberately gentle rather than the ~500 ms the issue allows;
// Tier 2 replaces the poll with a blocking `cabildo wait`.
const POLL_MS = 1000
// Token bucket: at most BUCKET_CAP auto-turns held, one refilled per REFILL_MS.
const BUCKET_CAP = 10
const REFILL_MS = 120_000
// The human is driving if they typed here within this window; defer to them.
const HUMAN_DEFER_MS = 120_000
// cabildo_ask holds the asker's turn this long waiting for a reply, then hands
// the late reply off to the doorbell. Kept short to stay inside a tool.call
// hook's budget; a longer in-hook wait is a Tier 2 / budget-config follow-up.
const ASK_WAIT_MS = 8_000
const ASK_POLL_MS = 1_000
// On the ask path we want the reply whole, not a preview.
const ASK_PREVIEW = 100_000

const sessionIdA = atom({ plugin: 'cabildo-push', key: 'sessionId' } as const, null)
const cursorA = atom({ plugin: 'cabildo-push', key: 'cursor' } as const, 0)
const bucketA = atom({ plugin: 'cabildo-push', key: 'bucket' } as const, {
  tokens: BUCKET_CAP,
  refillAt: 0,
})
const lastHumanA = atom({ plugin: 'cabildo-push', key: 'lastHumanAt' } as const, 0)
const awaitingA = atom({ plugin: 'cabildo-push', key: 'awaiting' } as const, {})

// --------------------------------------------------------------------------- //
// Shelling the cabildo CLI
// --------------------------------------------------------------------------- //
type PullRow = {
  id: number
  from_session: string
  from_display: string
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

// Who this session accepts an interrupt (auto-turn) from, beyond the human and
// replies to its own asks. One id / handle / robot-name per line in
// ~/.cabildo/push-accept; a bare `*` or `all` accepts everyone. Missing file
// means: wake for nobody (badge/toast only) — the safe default.
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
  const handle = (m.from_display || '').replace(/^@/, '')
  return accept.has(m.from_session) || accept.has(handle)
}

function uniq(xs: readonly string[]): string[] {
  return [...new Set(xs)]
}

function toolResult(text: string, isError = false): {
  result: { content: { type: 'text'; text: string }[]; isError: boolean }
} {
  return { result: { content: [{ type: 'text', text }], isError } }
}

// --------------------------------------------------------------------------- //
// Session identity + token bucket
// --------------------------------------------------------------------------- //
async function ensureSession($: EngineInterface): Promise<string | null> {
  const known = await read($, sessionIdA)
  if (known) return known
  let me: { session_id?: string; is_human?: boolean; cursor?: number } | null
  try {
    me = (await cab($, ['whoami', '--json'])) as typeof me
  } catch {
    return null
  }
  const sid = me?.session_id ?? null
  // The pid walk can resolve to the human before this session's row is bound;
  // don't latch onto that — retry next tick.
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
async function poll($: EngineInterface): Promise<void> {
  const sid = await ensureSession($)
  if (!sid) return

  const after = await read($, cursorA)
  let pulled: { cursor: number; messages: PullRow[] } | null
  try {
    pulled = (await cab($, [
      'pull',
      '--session',
      sid,
      '--after',
      String(after),
      '--json',
    ])) as typeof pulled
  } catch (err) {
    $.ui.log(`${PLUGIN}: pull failed: ${String(err)}`, { to: 'debug' })
    return
  }
  const msgs = pulled?.messages ?? []
  if (msgs.length === 0) return
  await update($, cursorA, () => pulled?.cursor ?? after)

  const dnd = await isDnd($)
  const now = await $.clock.now()
  const humanActive = now - (await read($, lastHumanA)) < HUMAN_DEFER_MS
  const accept = await acceptSet($)
  const awaiting = await read($, awaitingA)

  const wakers: PullRow[] = []
  const toasts: PullRow[] = []
  for (const m of msgs) {
    if (m.read) continue
    const aw = awaiting[m.from_session]
    if (aw && m.id > aw.askId) {
      // A reply to one of my asks. If the ask hook is still waiting (not
      // handed off) it owns the reply — skip. Once handed off, waking on it
      // is the "you asked, you get woken" interrupt grant.
      if (aw.handoff) wakers.push(m)
      continue
    }
    if (m.from_session === HUMAN_SESSION || accepts(accept, m)) wakers.push(m)
    else toasts.push(m)
  }

  // Clear the handed-off asks we are about to wake on.
  if (wakers.length) {
    await update($, awaitingA, (a) => {
      const next = { ...a }
      for (const m of wakers) {
        const aw = next[m.from_session]
        if (aw && m.id > aw.askId && aw.handoff) delete next[m.from_session]
      }
      return next
    })
  }

  if (wakers.length && !dnd && !humanActive && (await spendToken($, now))) {
    const who = uniq(wakers.map((m) => m.from_display)).join(', ')
    void $.prompt.submit({
      text: `cabildo: ${wakers.length} new message(s) from ${who} — call the inbox tool to read them.`,
    })
    await cab($, ['meter', '--session', sid, '--event', 'push_wake', '--json']).catch(() => {})
    $.ui.status(undefined)
    if (toasts.length) notifyPending($, toasts)
    return
  }

  // Suppressed (DND / human present / bucket empty): coalesce everything into
  // a badge + toast. No turn is started.
  const pending = [...wakers, ...toasts]
  if (pending.length) notifyPending($, pending)
}

function notifyPending($: EngineInterface, pending: readonly PullRow[]): void {
  const who = uniq(pending.map((m) => m.from_display)).join(', ')
  $.ui.toast(`cabildo: ${pending.length} new from ${who}`)
  $.ui.status(`cabildo: ${pending.length} unread — /inbox or call the inbox tool`)
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
    $.clock.every(POLL_MS, () => {
      void poll($)
    })
    return next(e)
  })

  // Track local human presence so push defers while they are driving. Never
  // let a bookkeeping failure drop the human's prompt.
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
    return { text: on_ ? 'cabildo push DND off — auto-turns resume.' : 'cabildo push DND on — auto-turns suppressed.' }
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
        '--session',
        sid,
        '--to',
        to,
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

    // The ask owns the reply while it waits (handoff: false).
    await update($, awaitingA, (a) => ({ ...a, [target]: { askId, handoff: false } }))

    const deadline = (await $.clock.now()) + ASK_WAIT_MS
    while ((await $.clock.now()) < deadline) {
      await $.clock.sleep(ASK_POLL_MS)
      let r: { messages: PullRow[] } | null
      try {
        r = (await cab($, [
          'pull',
          '--session',
          sid,
          '--after',
          String(askId),
          '--preview-chars',
          String(ASK_PREVIEW),
          '--json',
        ])) as typeof r
      } catch {
        continue
      }
      const reply = (r?.messages ?? []).find((m) => m.from_session === target)
      if (reply) {
        await cab($, ['mark-read', '--session', sid, '--ids', String(reply.id), '--json']).catch(
          () => {},
        )
        await update($, awaitingA, (a) => {
          const next = { ...a }
          delete next[target]
          return next
        })
        return toolResult(`${reply.from_display} replied (#${reply.id}):\n${reply.preview}`)
      }
    }

    // Timed out: hand the late reply off to the doorbell.
    await update($, awaitingA, (a) => ({ ...a, [target]: { askId, handoff: true } }))
    return toolResult(
      `Sent #${askId} to ${target}. No reply within ${ASK_WAIT_MS / 1000}s — ` +
        `I'll wake you when it arrives.`,
    )
  }).catch(() => toolResult('cabildo_ask was cut short; the message may still have been sent.', true))

  on('tool.call', { tool: 'mcp__cabildo-push__cabildo_reply' }, async ($, e) => {
    const sid = await ensureSession($)
    if (!sid) return toolResult('cabildo-push is not bound to a session yet; try again shortly.', true)
    const to = String(e.to ?? '').trim()
    const body = String(e.body ?? '').trim()
    if (!to || !body) return toolResult('cabildo_reply needs `to` and `body`.', true)
    try {
      const r = (await cab($, ['send', '--session', sid, '--to', to, '--json', '--', body])) as {
        id: number
        to: string
      }
      return toolResult(`Replied to ${r.to} (#${r.id}).`)
    } catch (err) {
      return toolResult(`cabildo_reply failed: ${String(err)}`, true)
    }
  }).catch(() => toolResult('cabildo_reply was cut short.', true))
}
