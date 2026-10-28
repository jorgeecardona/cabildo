import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code/testing'

// The doorbell blocks in one `cabildo wait` child at a time and starts the
// next one when it exits (#37). These hooks stand for the engine and the CLI.
function world(on: On, waits: string[]) {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/t' })
  const spawned: string[][] = []
  const prompts: string[] = []
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
  on('process.run', (_, e) => ({
    value: {
      exitCode: 0,
      stdout: e.argv[1] === 'whoami' ? '{"session_id":"S1","is_human":false,"cursor":7}' : '',
      stderr: '',
    },
  }))
  on('prompt.submit', (_, e) => {
    prompts.push(e.text)
    return { text: e.text }
  })
  on('process.spawn', async function* (_, e) {
    spawned.push([...e.argv])
    const out = waits.shift()
    if (out === undefined) {
      await clock.sleep(1e12) // the last wait blocks: an idle session
      return { value: { code: 0, signal: null } }
    }
    if (out === '') return { value: { code: 1, signal: null } }
    // Two pieces, split mid-line: the mod must not parse a piece as a line.
    const cut = Math.floor(out.length / 2)
    yield { stream: 'stdout', text: out.slice(0, cut) }
    yield { stream: 'stdout', text: out.slice(cut) + '\n' }
    return { value: { code: 0, signal: null } }
  })
  return { clock, spawned, prompts }
}

const humanMsg = JSON.stringify({
  session_id: 'S1',
  cursor: 8,
  messages: [
    {
      id: 8,
      from_session: 'human',
      from_display: '@jorge',
      from_robot: 'r',
      from_human: true,
      to_session: 'S1',
      topic: null,
      urgent: false,
      read: false,
      created_at: 0,
      preview: 'hi',
      truncated: false,
    },
  ],
})

test('a wait that returns a human message starts one turn, then waits past it', async ($, on) => {
  const w = world(on, [humanMsg])
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await w.clock.settle()

  expect(w.spawned.length).toBe(2)
  expect(w.spawned[0]).toEqual(['cabildo', 'wait', '--session=S1', '--after=7', '--json'])
  expect(w.spawned[1]).toEqual(['cabildo', 'wait', '--session=S1', '--after=8', '--json'])
  expect(w.prompts.length).toBe(1)
  expect(w.prompts[0]).toContain('@jorge')

  // Idle: no new spawn however long the clock runs.
  await w.clock.advance(60_000)
  expect(w.spawned.length).toBe(2)
})

test('an empty timeout wait respawns at the same cursor and wakes nobody', async ($, on) => {
  const w = world(on, [JSON.stringify({ session_id: 'S1', cursor: 7, messages: [] })])
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await w.clock.settle()
  expect(w.spawned.map((a) => a[3])).toEqual(['--after=7', '--after=7'])
  expect(w.prompts.length).toBe(0)
})

test('a rebind after /clear moves the doorbell to the new session', async ($, on) => {
  const w = world(on, [JSON.stringify({ rebound: true, session_id: 'S2', cursor: 20 })])
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await w.clock.settle()
  expect(w.spawned[1]).toEqual(['cabildo', 'wait', '--session=S2', '--after=20', '--json'])
})

test('a wait that prints nothing re-learns the session after a pause', async ($, on) => {
  const w = world(on, [''])
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await w.clock.settle()
  expect(w.spawned.length).toBe(1)
  await w.clock.advance(1_000)
  expect(w.spawned.length).toBe(2)
})

const roommateDm = (wakes: boolean) =>
  JSON.stringify({
    session_id: 'S1',
    cursor: 9,
    messages: [
      {
        id: 9,
        from_session: 'S2',
        from_display: '@ana',
        from_robot: 'sparky-ana-12345',
        from_human: false,
        wakes,
        to_session: 'S1',
        topic: null,
        urgent: false,
        read: false,
        created_at: 0,
        preview: 'hi',
        truncated: false,
      },
    ],
  })

test('a DM cabildo marks wakes starts a turn with no push-accept file', async ($, on) => {
  const w = world(on, [roommateDm(true)])
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await w.clock.settle()
  expect(w.prompts.length).toBe(1)
})

test('a DM not marked wakes only toasts', async ($, on) => {
  const w = world(on, [roommateDm(false)])
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await w.clock.settle()
  expect(w.prompts.length).toBe(0)
})
