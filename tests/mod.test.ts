import { expect, mock, test } from 'claude-code/testing'

type Reply = { status: number; text: string }

function setup(on: any, replies: Reply[], env: Record<string, string> = {}) {
  const started: { argv: readonly string[]; env?: Record<string, string> }[] = []
  const submitted: { text: string; asUser?: boolean }[] = []
  const opened: string[] = []
  const linked: string[] = []
  const calls: string[] = []
  const state = { up: false, id: 'session-1' }

  on('session.start', () => ({ cwd: '/work/shop' }))
  on('session.end', ($: any, e: any) => ({ sessionId: e.sessionId }))
  on('command.register', ($: any, e: any) => ({ value: { command: e.name } }))
  on('ui.status', () => ({ value: undefined }))
  on('env.get', ($: any, e: any) => ({ value: env[e.name] ?? '/home/op' }))
  on('fs.read', () => ({ value: 'token-1\n' }))
  on('session.id', () => ({ value: state.id }))
  on('session.cwd', () => ({ value: '/work/shop' }))
  on('process.run', ($: any, e: any) => {
    started.push({ argv: e.argv, env: e.init?.env })
    state.up = true
    return { value: { exitCode: 0, stdout: 'started', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('prompt.submit', ($: any, e: any) => {
    submitted.push({ text: e.text, asUser: e.origin.kind === 'plugin' ? e.origin.asUser : undefined })
    return { text: e.text }
  })
  on('http.fetch', ($: any, e: any) => {
    const path = new URL(e.url).pathname
    const reply = (status: number, text = '') => ({ value: { status, ok: status < 300, headers: {}, text } })
    if (path !== '/claude/next') calls.push(`${path} ${e.init?.headers?.['x-claude-session'] ?? ''}`.trim())
    if (path === '/claude/health') {
      return state.up ? reply(200, JSON.stringify({ service: 'voice-relay', problems: [] })) : reply(502)
    }
    if (path === '/claude/link') {
      const body = JSON.parse(e.init.body)
      linked.push(`${body.name} ${body.cwd}`)
      return reply(200, JSON.stringify({ name: 'shop' }))
    }
    if (path === '/desktop/open') {
      opened.push(JSON.parse(e.init.body).page)
      return reply(204)
    }
    const next = path === '/claude/next' ? (replies.shift() ?? { status: 503, text: '' }) : { status: 204, text: '' }
    return reply(next.status, next.text)
  })
  return { started, submitted, opened, linked, calls, state }
}

function command(args: string) {
  return { command: 'voiceagent', args, origin: { kind: 'composer' as const }, presentation: { isFullscreen: false, columns: 80 } }
}

test('/voiceagent starts the server, links the session by folder and submits a voice task', { options: { port: 7999 } }, async ($, on) => {
  const clock = mock.clock(on)
  const task = { status: 200, text: JSON.stringify({ kind: 'task', text: 'The operator says: "run the lint"' }) }
  const run = setup(on, [task])
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/shop' })
  const answer = await $.command.run(command(''))
  expect(answer.text).toContain('http://127.0.0.1:7999/voice')
  await clock.advance(2000)
  await clock.settle()

  expect(run.started).toHaveLength(1)
  expect(run.started[0]?.argv.slice(-2)).toEqual(['voice-relay', 'start'])
  expect(run.started[0]?.env).toEqual({ VOICE_RELAY_PORT: '7999' })
  expect(run.linked).toEqual(['shop /work/shop'])
  expect(run.opened).toEqual(['client'])
  expect(run.submitted).toEqual([{ text: 'The operator says: "run the lint"', asUser: true }])
  await $.command.run(command('off'))
  await clock.settle()
})

test('/voiceagent links again after the server forgot the session', { options: { port: 7999 } }, async ($, on) => {
  const clock = mock.clock(on)
  const run = setup(on, [{ status: 409, text: '' }])
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/shop' })
  await $.command.run(command(''))
  await clock.advance(2000)
  await clock.settle()
  expect(run.linked).toEqual(['shop /work/shop', 'shop /work/shop'])
  await $.command.run(command('off'))
  await clock.settle()
})

test('after /clear the link ends, and /voiceagent links the new session id', { options: { port: 7999 } }, async ($, on) => {
  const clock = mock.clock(on)
  const run = setup(on, [])
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/shop' })
  await $.command.run(command(''))
  await clock.advance(2000)
  await clock.settle()
  await $.session.end({ reason: 'clear', sessionId: 'session-1', resume: { id: 'session-1' } } as any)
  run.state.id = 'session-2'
  await $.command.run(command(''))
  await clock.advance(2000)
  await clock.settle()
  expect(run.calls.filter(call => /link/.test(call))).toEqual([
    '/claude/link session-1',
    '/claude/unlink session-1',
    '/claude/link session-2',
  ])
  await $.command.run(command('off'))
  await clock.settle()
})

test('a session started with VOICE_RELAY_LINK=1 links with no command', { options: { port: 7999 } }, async ($, on) => {
  const clock = mock.clock(on)
  const run = setup(on, [], { VOICE_RELAY_LINK: '1' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work/shop' })
  await clock.advance(2000)
  await clock.settle()
  expect(run.linked).toEqual(['shop /work/shop'])
  await $.command.run(command('off'))
  await clock.settle()
})
