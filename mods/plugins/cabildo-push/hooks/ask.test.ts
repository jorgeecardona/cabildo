import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code/testing'

// cabildo_ask blocks in `cabildo wait` while it holds the turn, not in a `pull` per second (#37).
function row(id: number, from: string, to: string | null, preview: string) {
  return {
    id,
    from_session: from,
    from_display: from,
    from_robot: from,
    from_human: false,
    to_session: to,
    topic: to ? null : 'general',
    urgent: false,
    read: false,
    created_at: 0,
    preview,
    truncated: false,
  }
}

function world(on: On, askWaits: string[]) {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/t' })
  const runs: string[] = []
  const askSpawns: string[][] = []
  on('tool.register', () => ({ value: undefined }))
  on('command.register', () => ({ value: { command: 'cabildo-dnd' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('fs.exists', () => ({ value: false }))
  on('fs.read', () => {
    throw new Error('ENOENT')
  })
  on('process.run', (_, e) => {
    runs.push(e.argv[1])
    const out: Record<string, string> = {
      whoami: '{"session_id":"S1","is_human":false,"cursor":7}',
      send: '{"id":10,"to":"T"}',
      pull: '{"messages":[]}',
    }
    return { value: { exitCode: 0, stdout: out[e.argv[1]] ?? '', stderr: '' } }
  })
  on('process.spawn', async function* (_, e) {
    const isAsk = e.argv.some((a: string) => a.startsWith('--timeout='))
    if (isAsk) {
      askSpawns.push([...e.argv])
      const out = askWaits.shift()
      if (out) yield { stream: 'stdout', text: out + '\n' }
      return { value: { code: 0, signal: null } }
    }
    await clock.sleep(1e12) // the doorbell idles
    return { value: { code: 0, signal: null } }
  })
  return { clock, runs, askSpawns }
}

const ASK = 'mcp__cabildo-push__cabildo_ask'

test('a reply after an unrelated message returns it, with no pull poll', async ($, on) => {
  const w = world(on, [
    JSON.stringify({ session_id: 'S1', cursor: 11, messages: [row(11, 'X', null, 'noise')] }),
    JSON.stringify({ session_id: 'S1', cursor: 12, messages: [row(12, 'T', 'S1', 'the answer')] }),
  ])
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const r = JSON.stringify(await $.tool.call({ tool: ASK, to: 'T', body: 'q?' }))
  expect(r).toContain('the answer')
  expect(w.askSpawns.map((a) => a[3])).toEqual(['--after=10', '--after=11'])
  expect(w.runs).not.toContain('pull')
})

test('an empty wait does one last pull, then hands off', async ($, on) => {
  const w = world(on, [JSON.stringify({ session_id: 'S1', cursor: 10, messages: [] })])
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const r = JSON.stringify(await $.tool.call({ tool: ASK, to: 'T', body: 'q?' }))
  expect(r).toContain('wake you')
  expect(w.askSpawns.length).toBe(1)
  expect(w.runs.filter((c) => c === 'pull').length).toBe(1)
})
