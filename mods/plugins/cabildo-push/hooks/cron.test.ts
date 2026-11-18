import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code/testing'

// CronCreate / CronDelete / CronList / ScheduleWakeup are stored in cabildo and
// fired by the mod; a cabildo failure falls back to Claude Code's own scheduler.

type World = {
  clock: ReturnType<typeof mock.clock>
  cabRuns: string[][]
  nativeCalls: string[]
  submitted: string[]
  commands: string[]
  due: unknown[]
}

function world(on: On, opts: { cabFails?: boolean; nativeCopy?: boolean } = {}): World {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { HOME: '/home/t' })
  const w: World = { clock, cabRuns: [], nativeCalls: [], submitted: [], commands: [], due: [] }
  on('tool.register', () => ({ value: undefined }))
  on('command.register', () => ({ value: { command: 'cabildo-dnd' } }))
  on('command.list', () => ({ value: [{ name: 'loop', description: 'loop' }] }))
  on('command.run', (_, e) => {
    w.commands.push(`/${e.command} ${e.args}`.trim())
    return {}
  })
  on('prompt.submit', (_, e) => {
    w.submitted.push(e.text)
    return { text: e.text }
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('fs.exists', () => ({ value: false }))
  on('fs.read', () => {
    throw new Error('ENOENT')
  })
  on('process.run', (_, e) => {
    const argv = [...e.argv].slice(1)
    if (argv[0] === 'whoami') {
      return { value: { exitCode: 0, stdout: '{"session_id":"S1","cursor":0}', stderr: '' } }
    }
    if (argv[0] !== 'cron') return { value: { exitCode: 0, stdout: '', stderr: '' } }
    w.cabRuns.push(argv)
    if (opts.cabFails) return { value: { exitCode: 1, stdout: '', stderr: 'boom' } }
    const job = {
      id: 'cab-1',
      kind: 'cron',
      cron: '*/5 * * * *',
      humanSchedule: 'every 5 minutes',
      prompt: 'check the deploy',
      recurring: true,
      nextFireAt: 1_300_000,
    }
    const out: Record<string, unknown> = {
      add: job,
      wakeup: {
        ...job,
        kind: 'wakeup',
        cron: null,
        recurring: false,
        clampedDelaySeconds: 60,
        wasClamped: true,
      },
      rm: { id: 'cab-1', deleted: true },
      list: { jobs: [job] },
      'stop-wakeups': { cancelled: 2 },
      due: { jobs: w.due.splice(0), nextFireAt: null },
    }
    return { value: { exitCode: 0, stdout: JSON.stringify(out[argv[1] ?? ''] ?? {}), stderr: '' } }
  })
  on('process.spawn', async function* () {
    await clock.sleep(1e12) // the doorbell idles
    return { value: { code: 0, signal: null } }
  })
  // Claude Code's own scheduler, beneath the mod.
  on('tool.call', (_, e) => {
    w.nativeCalls.push(e.tool)
    if (e.tool === 'CronCreate') {
      return { result: { id: 'abcd1234', humanSchedule: 'native', recurring: true } }
    }
    if (e.tool === 'CronList') {
      const jobs = [{ id: 'abcd1234', cron: '0 * * * *', humanSchedule: 'n', prompt: 'x' }]
      // A resumed session: Claude Code restored the cabildo job under the same id.
      if (opts.nativeCopy) jobs.push({ id: 'cab-1', cron: '*/5 * * * *', humanSchedule: 'n', prompt: 'x' })
      return { result: { jobs } }
    }
    if (e.tool === 'ScheduleWakeup') {
      return {
        result: { scheduledFor: 0, clampedDelaySeconds: 0, wasClamped: false, cancelledWakeups: 1 },
      }
    }
    return { result: { id: String(e.id ?? '') } }
  })
  return w
}

async function start($: Parameters<Parameters<typeof test>[1]>[0]) {
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
}

test('CronCreate is stored in cabildo, not in Claude Code', async ($, on) => {
  const w = world(on)
  await start($)
  const r = await $.tool.call({ tool: 'CronCreate', cron: '*/5 * * * *', prompt: 'check the deploy' })
  expect(JSON.stringify(r)).toContain('cab-1')
  expect(w.nativeCalls).toEqual([])
  expect(w.cabRuns[0]).toEqual([
    'cron',
    'add',
    '--session=S1',
    '--cron=*/5 * * * *',
    '--json',
    '--',
    'check the deploy',
  ])
})

test('a one-shot CronCreate passes --once', async ($, on) => {
  const w = world(on)
  await start($)
  await $.tool.call({ tool: 'CronCreate', cron: '30 14 * * *', prompt: 'p', recurring: false })
  expect(w.cabRuns[0]).toContain('--once')
})

test('CronCreate falls back to Claude Code when cabildo fails', async ($, on) => {
  const w = world(on, { cabFails: true })
  await start($)
  const r = await $.tool.call({ tool: 'CronCreate', cron: '*/5 * * * *', prompt: 'p' })
  expect(JSON.stringify(r)).toContain('abcd1234')
  expect(w.nativeCalls).toEqual(['CronCreate'])
})

test('an autonomous-loop sentinel stays with Claude Code', async ($, on) => {
  const w = world(on)
  await start($)
  await $.tool.call({ tool: 'CronCreate', cron: '*/5 * * * *', prompt: '<<autonomous-loop>>' })
  expect(w.nativeCalls).toEqual(['CronCreate'])
  expect(w.cabRuns).toEqual([])
})

test('CronDelete sends cab- ids to cabildo and other ids to Claude Code', async ($, on) => {
  const w = world(on)
  await start($)
  await $.tool.call({ tool: 'CronDelete', id: 'cab-1' })
  expect(w.cabRuns[0]?.slice(0, 3)).toEqual(['cron', 'rm', 'cab-1'])
  expect(w.nativeCalls).toEqual(['CronDelete']) // a possible resumed copy, same id (#53)
  w.nativeCalls.length = 0
  await $.tool.call({ tool: 'CronDelete', id: 'abcd1234' })
  expect(w.nativeCalls).toEqual(['CronDelete'])
  expect(w.cabRuns.length).toBe(1)
})

test('CronDelete of a cab- id also removes the copy Claude Code restored on resume (#53)', async ($, on) => {
  const w = world(on, { nativeCopy: true })
  await start($)
  const r = JSON.stringify(await $.tool.call({ tool: 'CronDelete', id: 'cab-1' }))
  expect(r).toContain('cab-1')
  expect(w.nativeCalls).toEqual(['CronDelete'])
})

test('CronList shows a restored cab- job once (#53)', async ($, on) => {
  world(on, { nativeCopy: true })
  await start($)
  const r = JSON.stringify(await $.tool.call({ tool: 'CronList' }))
  expect(r.split('"cab-1"').length - 1).toBe(1)
})

test('CronList shows Claude Code jobs and cabildo jobs together', async ($, on) => {
  world(on)
  await start($)
  const r = JSON.stringify(await $.tool.call({ tool: 'CronList' }))
  expect(r).toContain('abcd1234')
  expect(r).toContain('cab-1')
})

test('ScheduleWakeup is stored in cabildo with the clamp cabildo reports', async ($, on) => {
  const w = world(on)
  await start($)
  const r = JSON.stringify(
    await $.tool.call({ tool: 'ScheduleWakeup', delaySeconds: 5, reason: 'r', prompt: 'go' }),
  )
  expect(r).toContain('"wasClamped":true')
  expect(r).toContain('"clampedDelaySeconds":60')
  expect(w.nativeCalls).toEqual([])
})

test('ScheduleWakeup stop cancels both and adds the counts', async ($, on) => {
  const w = world(on)
  await start($)
  const r = JSON.stringify(await $.tool.call({ tool: 'ScheduleWakeup', stop: true }))
  expect(r).toContain('"cancelledWakeups":3')
  expect(w.nativeCalls).toEqual(['ScheduleWakeup'])
})

test('a due cron fires its prompt; a due wakeup re-enters /loop', async ($, on) => {
  const w = world(on)
  await start($)
  w.due.push(
    { id: 'cab-1', kind: 'cron', cron: '* * * * *', prompt: 'check the deploy', recurring: true },
    { id: 'cab-2', kind: 'wakeup', cron: null, prompt: 'watch CI', recurring: false },
  )
  await w.clock.advance(16_000)
  expect(w.submitted).toEqual(['check the deploy'])
  expect(w.commands).toEqual(['/loop watch CI'])
})
