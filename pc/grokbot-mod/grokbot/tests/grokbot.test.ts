import { expect, mock, test } from 'claude-code/testing'
import type { On, ToolCallResult } from 'claude-code'

import { boardFacts, parseLedger, statusLine } from '../hooks/board'
import { centralTime, checkLaunches, ckRunCalls, isDispatch, shellWords, viewArgs } from '../hooks/register'
import { ckLine, parseTriggers } from '../hooks/triggers'

const HOME = '{{WSL_HOME}}'
const PLAYBOOK = `${HOME}/PhD_local/grokbot-mod/GROKBOT.md`
// 2026-11-04 22:15 UTC = 16:15 CST (DST ended 2026-11-01)
const NOW = Date.UTC(2026, 10, 4, 22, 15)
const LOG = `${HOME}/PhD_local/grokbot_router/sessions/2026-11-04_abcdef12.md`
// What the engine stamps on a command typed at the prompt.
const TYPED = { origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } } as const

type Submitted = { text: string; context: readonly string[] }
// What a host command answers in the tests (ck-run and tail are never really run).
type Proc = { exitCode?: number; stdout?: string; stderr?: string }
const proc = (p: Proc) => ({
  exitCode: p.exitCode ?? 0,
  stdout: p.stdout ?? '',
  stderr: p.stderr ?? '',
  isStdoutTruncated: false,
  isStderrTruncated: false,
})

// The world beneath the plugin: a small in-memory disk, a session id, a clock, HOME; host commands (ck-run and the
// ledger tail) are answered from memory, and `toolResult` / `onBash` stand for what a Bash command did.
function world(on: On, store: Record<string, unknown> = {}, env: Record<string, string> = {}) {
  const files = new Map<string, string>([[PLAYBOOK, '# Grok Bot mode: test playbook\n']])
  const submitted: Submitted[] = []
  const status: (string | undefined)[] = []
  const notes: string[] = []
  const runs: (readonly string[])[] = []
  const w = {
    ck: (_argv: readonly string[]): Proc => ({}),
    toolResult: { result: {} } as ToolCallResult,
    onBash: undefined as (() => void) | undefined,
  }
  mock.env(on, { HOME, ...env })
  mock.store(on, store)
  const clock = mock.clock(on, { now: NOW })
  on('ui.notify', (_, e) => {
    notes.push(e.text)
    return { value: { isSent: true, channel: 'iterm2' } }
  })
  on('process.run', (_, e) => {
    runs.push(e.argv)
    if (e.argv[0] === '/usr/bin/tail') {
      const text = files.get(e.argv.at(-1) ?? '')
      return { value: proc(text === undefined ? { exitCode: 1, stderr: 'No such file' } : { stdout: text }) }
    }
    return { value: proc(w.ck(e.argv)) }
  })
  on('fs.list', (_, e) => {
    const dir = e.path.replace(/\/+$/, '')
    const entries = new Map<string, { name: string; kind: 'file' | 'dir'; size: number; mtimeMs: number; isLink: boolean }>()
    for (const [path, text] of files) {
      if (!path.startsWith(`${dir}/`)) continue
      const rest = path.slice(dir.length + 1)
      const name = rest.split('/')[0] ?? rest
      entries.set(name, { name, kind: rest.includes('/') ? 'dir' : 'file', size: text.length, mtimeMs: 0, isLink: false })
    }
    if (entries.size === 0) throw new Error(`missing ${e.path}`)
    return { value: [...entries.values()] }
  })
  on('fs.exists', (_, e) => ({ value: files.has(e.path) }))
  on('fs.read', (_, e) => {
    const text = files.get(e.path)
    if (text === undefined) throw new Error(`missing ${e.path}`)
    return { value: text }
  })
  on('fs.write', (_, e) => {
    files.set(e.path, e.text)
    return { value: undefined }
  })
  on('session.id', () => ({ value: 'abcdef12-3456-7890' }))
  on('command.register', () => ({ value: { command: 'grokbot' } }))
  on('ui.status', (_, e) => {
    status.push(e.text)
    return { value: undefined }
  })
  on('prompt.submit', (_, e) => {
    submitted.push({ text: e.text, context: e.context ?? [] })
    return { text: e.text }
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  const toasts: string[] = []
  on('ui.toast', (_, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  // The engine's own built-in commands (/clear, ...) beneath the plugin.
  on('command.run', () => ({ text: '' }))
  on('tool.call', () => {
    w.onBash?.()
    return w.toolResult
  })
  return Object.assign(w, { files, submitted, status, clock, toasts, notes, runs })
}

// chalach typing a prompt at the terminal.
const typed = (text: string) => ({ text, origin: { kind: 'composer' }, wait: false }) as const

test('central time handles CST and CDT', () => {
  expect(centralTime(NOW).stamp).toBe('2026-11-04 16:15 CST')
  expect(centralTime(Date.UTC(2026, 6, 1, 17, 0)).stamp).toBe('2026-07-01 12:00 CDT')
})

test('/grokbot turns the mode on, sends the playbook once, and logs', async ($, on) => {
  const w = world(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const ran = await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  expect(ran.text).toContain('Grok Bot mode ON')
  expect(ran.context?.[0]).toContain('GROK BOT MODE IS ON')
  expect(ran.context?.[0]).toContain('test playbook')
  expect(w.status).toContain('Grok Bot ON (/grokbot off)')
  expect(w.files.get(LOG)).toContain('2026-11-04 16:15 CST /grokbot ON')
})

test('CK_GROKBOT_AUTO=1 turns the mode on at session start (the cdev / cnv / gb shortcuts)', async ($, on) => {
  const w = world(on, {}, { CK_GROKBOT_AUTO: '1' })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await w.clock.advance(400)
  expect(w.status).toContain('Grok Bot ON (/grokbot off)')
  expect(w.files.get(LOG)).toContain('2026-11-04 16:15 CST /grokbot ON (auto: CK_GROKBOT_AUTO=1')
  // chalach's first prompt carries the activation note (playbook included) and the reminder; the second only the reminder
  await $.prompt.submit(typed('first task'))
  await $.prompt.submit(typed('second task'))
  const note = (c: string) => c.includes('GROK BOT MODE IS ON') && c.includes('test playbook')
  expect(w.submitted[0]?.context.some(note)).toBe(true)
  expect(w.submitted[0]?.context.some(c => c.includes('[Grok Bot mode ON'))).toBe(true)
  expect(w.submitted[1]?.context.some(note)).toBe(false)
})

test('without CK_GROKBOT_AUTO the mode stays off at session start', async ($, on) => {
  const w = world(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await w.clock.advance(400)
  expect(w.status).not.toContain('Grok Bot ON (/grokbot off)')
  expect(w.files.get(LOG)).toBeUndefined()
})

test('while on, each prompt carries the reminder and is logged verbatim', async ($, on) => {
  const w = world(on)
  await $.prompt.submit(typed('before the mode'))
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  await $.prompt.submit(typed('run the M6 step with codex'))

  expect(w.submitted[0]?.context.some(c => c.includes('[Grok Bot mode ON'))).toBe(false)
  expect(w.submitted[1]?.context.some(c => c.includes('[Grok Bot mode ON since 2026-11-04 16:15 CST'))).toBe(true)
  const log = w.files.get(LOG) ?? ''
  expect(log).toContain('chalach:\n  > run the M6 step with codex')
  expect(log).not.toContain('before the mode')
})

// The mod's own $.prompt.submit skips its own prompt.submit hook (engine rule), so the task carries no reminder:
// it does not need one, as it arrives right after the activation note that holds the whole playbook.
test('/grokbot <task> sends the task as the next prompt', async ($, on) => {
  const w = world(on)
  const ran = await $.command.run({ ...TYPED, command: 'grokbot', args: 'check phase 3 status' })
  expect(ran.context?.[0]).toContain("chalach's task follows as the next prompt")
  expect(w.submitted.some(s => s.text === 'check phase 3 status')).toBe(false)
  await w.clock.advance(100)
  expect(w.submitted.some(s => s.text === 'check phase 3 status')).toBe(true)
  expect(w.files.get(LOG)).toContain('> check phase 3 status')
})

test('/grokbot off ends the reminder; status reports the mode', async ($, on) => {
  const w = world(on)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  const off = await $.command.run({ ...TYPED, command: 'grokbot', args: 'off' })
  expect(off.text).toContain('Grok Bot mode OFF')
  expect(off.context?.[0]).toContain('GROK BOT MODE IS OFF')

  await $.prompt.submit(typed('normal work again'))
  expect(w.submitted.at(-1)?.context.some(c => c.includes('[Grok Bot mode ON'))).toBe(false)

  const status = await $.command.run({ ...TYPED, command: 'grokbot', args: 'status' })
  expect(status.text).toContain('Grok Bot mode: off')
  expect(w.files.get(LOG)).toContain('/grokbot off (mode OFF)')
})

test('/clear ends the mode and clears the status line (found live 2026-11-04)', async ($, on) => {
  const w = world(on)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  expect(w.status.at(-1)).toBe('Grok Bot ON (/grokbot off)')
  await $.command.run({ ...TYPED, command: 'clear', args: '' })
  expect(w.status.at(-1)).toBeUndefined()
  expect(w.toasts.at(-1)).toContain('Grok Bot mode ended with /clear')
  expect(w.files.get(LOG)).toContain('/clear ended Grok Bot mode (mode OFF)')
  const status = await $.command.run({ ...TYPED, command: 'grokbot', args: 'status' })
  expect(status.text).toContain('Grok Bot mode: off')
})

test('/clear with the mode off changes nothing', async ($, on) => {
  const w = world(on)
  await $.command.run({ ...TYPED, command: 'clear', args: '' })
  expect(w.toasts.length).toBe(0)
  expect(w.files.has(LOG)).toBe(false)
})

test('the gate knows a dispatch from a read', () => {
  const dispatches = [
    '~/PhD_local/ck-dispatch/bin/ck-run run --agent codex --workspace devfab --title x --prompt-file p.md',
    'cd "$PHD" && ~/PhD_local/ck-dispatch/bin/ck-run review 2026-11-04T1-x --by claude --interactive',
    'ck-run accept 2026-11-04T1-x --note ok',
    'bash ~/PhD_local/agent_runs/p3m4.sh > ~/PhD_local/agent_runs/p3m4.out 2>&1; echo "ck-run exit=$?" >> x',
    'cd ~ && ~/PhD_local/agent_runs/sessionfix.sh',
    // shell code inside quotes or a heredoc that a shell runs is still a dispatch
    'bash -c "cd ~/PhD_local && ck-dispatch/bin/ck-run run --agent claude --workspace nv --title t --prompt-file p.md"',
    'bash <<EOF\ncd ~/PhD_local\nck-dispatch/bin/ck-run accept R --note ok\nEOF',
    `osascript -e 'tell application "iTerm2" to create window with default profile command "/bin/bash ~/PhD_local/agent_runs/x.sh"'`,
    'cd "$PHD/Device Fabrication" && ~/PhD_local/ck-dispatch/bin/ck-run run --agent codex --workspace devfab --title "a b" --prompt-file p.md',
  ]
  const reads = [
    '~/PhD_local/ck-dispatch/bin/ck-run show 2026-11-04T1-x',
    'ck-run active; ck-run limits; ck-run thread R --no-summary; ck-run route --task-type writing',
    'ck-run route --help',
    '~/PhD_local/ck-dispatch/bin/ck-run run --help | head -3',
    'bash ~/PhD_local/agent_runs/p3m4.sh --dry-run',
    'grep -n "ck-run run" ~/PhD_local/ck-dispatch/README.md',
    'cat ~/PhD_local/agent_runs/p3m4.sh',
    'echo "ck-run exit=$?"',
    // text that only MENTIONS a dispatch (the false positive found live 2026-11-04 17:30)
    "python3 - <<'EOF'\nt = 'a020 already cleared (`ck-run accept ... --by user`, in the ledger)'\nEOF",
    "cat > ~/PhD_local/agent_runs/p3x.sh <<'EOF'\n#!/bin/bash\ncd ~/PhD_local && ck-dispatch/bin/ck-run run --agent codex --interactive\nEOF",
    'echo "- 17:30 ran `ck-run review R --by codex`" >> ~/PhD_local/grokbot_router/PROGRESS.md',
    `git commit -q -m 'Skills: allow ck-run run from Claude Code only in /grokbot mode'`,
    "python3 -I -c 'print(\"ck-run accept\")'",
  ]
  for (const c of dispatches) expect([c, isDispatch(c)]).toEqual([c, true])
  for (const c of reads) expect([c, isDispatch(c)]).toEqual([c, false])
})


const AR = `${HOME}/PhD_local/agent_runs`
const RUN_LINE =
  'cd ~/PhD_local && ~/PhD_local/ck-dispatch/bin/ck-run run --agent codex --workspace devfab --task-type python_debug \\\n' +
  '  --title "t one" --prompt-file ~/PhD_local/agent_runs/t1_prompt.md --timeout 3600 --interactive "$@"\n'
const LAUNCH = 'bash ~/PhD_local/agent_runs/t1.sh > ~/PhD_local/agent_runs/t1.out 2>&1; echo "ck-run exit=$?" >> ~/PhD_local/agent_runs/t1.out'

// A world with one documented launch file t1.sh and its prompt.
function withLaunch(on: On) {
  const w = world(on)
  w.files.set(`${AR}/t1.sh`, `#!/bin/bash\n# t1, dispatched by Grok Bot stand-in\n${RUN_LINE}`)
  w.files.set(`${AR}/t1_prompt.md`, '# task t1\n')
  return w
}

test('mode off: every ck-run dispatch is refused, reads run', async ($, on) => {
  withLaunch(on)
  const run = '~/PhD_local/ck-dispatch/bin/ck-run run --agent codex --workspace devfab --title t --prompt-file p.md'
  expect((await $.tool.call({ tool: 'Bash', command: run })).deny).toContain('only in Grok Bot mode')
  expect((await $.tool.call({ tool: 'Bash', command: LAUNCH })).deny).toContain('only in Grok Bot mode')
  expect((await $.tool.call({ tool: 'Bash', command: '~/PhD_local/ck-dispatch/bin/ck-run active' })).deny).toBeUndefined()
})

test('mode on: a ck-run run typed directly is refused (not documented)', async ($, on) => {
  withLaunch(on)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  const run = 'cd ~/PhD_local && ~/PhD_local/ck-dispatch/bin/ck-run run --agent codex --workspace devfab --title t --prompt-file /tmp/p.md --interactive'
  expect((await $.tool.call({ tool: 'Bash', command: run })).deny).toContain('must be documented in ~/PhD_local/agent_runs')
})

test('mode on: a documented launch runs, is logged, and its result is logged when the .out finishes', async ($, on) => {
  const w = withLaunch(on)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  const ran = await $.tool.call({ tool: 'Bash', command: LAUNCH })
  expect(ran.deny).toBeUndefined()
  const log = () => w.files.get(`${AR}/grokbot_standin_log.md`) ?? ''
  expect(log()).toContain('· launch · t1')
  expect(log()).toContain('`~/PhD_local/agent_runs/t1_prompt.md`')
  expect(log()).toContain('`~/PhD_local/agent_runs/t1.out`')
  expect(log()).toContain('ck-run run --agent codex')
  expect(log()).toContain('--title "t one"')

  // still running: the notice of another job changes nothing
  await $.prompt.submit({ text: 'a task finished', origin: { kind: 'task-notification' }, wait: false })
  expect(log()).not.toContain('result · t1')

  const rid = '2026-11-04T180000-devfab-python_debug-codex-ab12'
  w.files.set(`${AR}/t1.out`, `${AR}/${rid}/summary.md\nM6 done: 3 files changed, 12 tests pass.\nQuestions for chalach: none\nck-run exit=0\n`)
  await $.prompt.submit({ text: 'background job done', origin: { kind: 'task-notification' }, wait: false })
  expect(log()).toContain('· result · t1')
  expect(log()).toContain(`\`${rid}\` · exit 0`)
  expect(log()).toContain('> M6 done: 3 files changed, 12 tests pass.')

  // logged once only
  await $.prompt.submit(typed('next'))
  expect(log().split('· result · t1').length).toBe(2)
})

test('mode on: an accept/reject is logged with its note', async ($, on) => {
  const w = withLaunch(on)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  const rid = '2026-11-04T160052-devfab-microwave_review-claude-a020'
  const ran = await $.tool.call({ tool: 'Bash', command: `~/PhD_local/ck-dispatch/bin/ck-run accept ${rid} --by user --note "false positive: git push only in text"` })
  expect(ran.deny).toBeUndefined()
  const log = w.files.get(`${AR}/grokbot_standin_log.md`) ?? ''
  expect(log).toContain(`· accept · \`${rid}\``)
  expect(log).toContain('by user: false positive: git push only in text')
})

test('a launch with no exit line after 8 h is logged as no result', async ($, on) => {
  const w = withLaunch(on)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  await $.tool.call({ tool: 'Bash', command: LAUNCH })
  await w.clock.advance(8 * 3600_000 + 60_000)
  await $.prompt.submit(typed('status?'))
  expect(w.files.get(`${AR}/grokbot_standin_log.md`)).toContain('· no result · t1')
})

test('the documentation check refuses what would leave a run undocumented', async () => {
  const files = new Map<string, string>()
  const read = async (p: string) => files.get(p) ?? null
  const check = (cmd: string) => checkLaunches(cmd, HOME, read)
  files.set(`${AR}/t1_prompt.md`, 'x')
  files.set(`${AR}/t1.sh`, RUN_LINE)

  expect((await check(LAUNCH)).ok).toBe(true)
  // relative launch path, a launch file that does not exist
  expect(await check('bash agent_runs/t1.sh > agent_runs/t1.out')).toMatchObject({ ok: false })
  expect(await check('bash ~/PhD_local/agent_runs/nope.sh > ~/PhD_local/agent_runs/nope.out')).toMatchObject({ ok: false })
  // output not saved, or saved to a scratchpad
  expect(await check('bash ~/PhD_local/agent_runs/t1.sh')).toMatchObject({ ok: false })
  expect(await check('bash ~/PhD_local/agent_runs/t1.sh > /tmp/ck_t1.out 2>&1')).toMatchObject({ ok: false })
  // no --interactive
  files.set(`${AR}/t2_prompt.md`, 'x')
  files.set(`${AR}/t2.sh`, RUN_LINE.replace('t1_prompt', 't2_prompt').replace(' --interactive', ''))
  const noLive = await check('bash ~/PhD_local/agent_runs/t2.sh > ~/PhD_local/agent_runs/t2.out')
  expect(noLive.ok ? '' : noLive.why).toContain('no --interactive')
  // prompt file outside agent_runs, or missing
  files.set(`${AR}/t3.sh`, RUN_LINE.replace('~/PhD_local/agent_runs/t1_prompt.md', '/tmp/ck_t3.md'))
  expect(await check('bash ~/PhD_local/agent_runs/t3.sh > ~/PhD_local/agent_runs/t3.out')).toMatchObject({ ok: false })
  files.set(`${AR}/t4.sh`, RUN_LINE.replace('t1_prompt', 't4_prompt'))
  expect(await check('bash ~/PhD_local/agent_runs/t4.sh > ~/PhD_local/agent_runs/t4.out')).toMatchObject({ ok: false })
})

test('the documentation check accepts the launch styles used on 2026-11-04', async () => {
  const files = new Map<string, string>()
  const read = async (p: string) => files.get(p) ?? null
  // two reviews in one file, output redirected by the launch (p3m4review.sh)
  files.set(
    `${AR}/rv.sh`,
    '#!/bin/bash\nF="$(cat ~/PhD_local/agent_runs/rv_focus.txt)"\n' +
      '~/PhD_local/ck-dispatch/bin/ck-run review R1 --by claude --title "a" --focus "$F" --timeout 3600 --interactive\n' +
      '~/PhD_local/ck-dispatch/bin/ck-run review R1 --by cursor --model grok-4.7-high --title "b" --focus "$F" --interactive\n',
  )
  const two = await checkLaunches('bash ~/PhD_local/agent_runs/rv.sh > ~/PhD_local/agent_runs/rv.out 2>&1; echo "ck-run exit=$?" >> ~/PhD_local/agent_runs/rv.out', HOME, read)
  expect(two.ok && two.launches[0]?.outs).toEqual([`${AR}/rv.out`])
  // parallel reviews writing their own .out inside the file (p3m5owners.sh, with outputs moved to agent_runs)
  files.set(
    `${AR}/own.sh`,
    '( ~/PhD_local/ck-dispatch/bin/ck-run review R2 --by codex --focus "$F" --interactive > ~/PhD_local/agent_runs/own_codex.out 2>&1; echo "ck-run exit=$?" >> ~/PhD_local/agent_runs/own_codex.out ) &\n' +
      '( ~/PhD_local/ck-dispatch/bin/ck-run review R3 --by cursor --focus "$F" --interactive > ~/PhD_local/agent_runs/own_grok.out 2>&1; echo "ck-run exit=$?" >> ~/PhD_local/agent_runs/own_grok.out ) &\nwait\n',
  )
  const par = await checkLaunches('bash ~/PhD_local/agent_runs/own.sh', HOME, read)
  expect(par.ok && par.launches[0]?.outs).toEqual([`${AR}/own_codex.out`, `${AR}/own_grok.out`])
  // the same file with outputs in a session scratchpad is refused
  files.set(`${AR}/own2.sh`, (files.get(`${AR}/own.sh`) ?? '').replaceAll('~/PhD_local/agent_runs/own_', '$S/ck_own_'))
  expect(await checkLaunches('bash ~/PhD_local/agent_runs/own2.sh', HOME, read)).toMatchObject({ ok: false })
  // a direct agent session opened in iTerm by osascript (sessionfix.sh): no ck-run line, prompt saved, no .out needed
  files.set(`${AR}/sessionfix_prompt.md`, 'fix')
  files.set(`${AR}/sessionfix.sh`, 'cd ~/PhD_local/ck-dispatch && exec claude --permission-mode auto "$(cat ~/PhD_local/agent_runs/sessionfix_prompt.md)"\n')
  const osa = `osascript -e 'tell application "iTerm2" to create window with default profile command "/bin/bash ~/PhD_local/agent_runs/sessionfix.sh"'`
  const direct = await checkLaunches(osa, HOME, read)
  expect(direct.ok && direct.launches[0]?.isWindowOnly).toBe(true)
  // two launches in one command: each output belongs to its own launch
  files.set(`${AR}/a_prompt.md`, 'x')
  files.set(`${AR}/a.sh`, RUN_LINE.replace('t1_prompt', 'a_prompt'))
  files.set(`${AR}/b_prompt.md`, 'x')
  files.set(`${AR}/b.sh`, RUN_LINE.replace('t1_prompt', 'b_prompt'))
  const both = await checkLaunches('bash ~/PhD_local/agent_runs/a.sh > ~/PhD_local/agent_runs/a.out 2>&1 & bash ~/PhD_local/agent_runs/b.sh > ~/PhD_local/agent_runs/b.out 2>&1 & wait', HOME, read)
  expect(both.ok && both.launches.map(l => l.outs)).toEqual([[`${AR}/a.out`], [`${AR}/b.out`]])
})

// ---- v4 (BUILD_SPEC_v4 section 11): trigger words, the gate for the new ck-run commands, ck-run launch, the
// status line, wake and notify, and the read-only views.

const CK = '~/PhD_local/ck-dispatch/bin/ck-run'
const START = { cwd: '/', surface: 'terminal', isInteractive: true } as const
const RID = '2026-11-04T221000-devfab-lab_code-codex-ab12'
const standin = (w: { files: Map<string, string> }) => w.files.get(`${AR}/grokbot_standin_log.md`) ?? ''
const wakes = (w: { submitted: Submitted[] }) => w.submitted.filter(s => s.text.startsWith('[ck-run]')).map(s => s.text)
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()

test('trigger words: the first words give size, pattern, models and playbook', () => {
  const line = (text: string) => {
    const t = parseTriggers(text)
    return t ? ckLine(t) : null
  }
  // the cheat sheet's examples
  expect(line('quick: how do I wire the SPCM to the DAQ?')).toBe('[ck: trigger=quick size=S pattern=solo models=auto playbook=quick-answer]')
  expect(line('do sol: note the SPCM kit as a wiki page')).toBe('[ck: trigger=do+sol size=M pattern=solo models=sol playbook=auto]')
  expect(line('fix: spcm_counter.py drops counts at high rate')).toBe('[ck: trigger=fix size=M pattern=auto models=auto playbook=bug-fix]')
  expect(line('race: why is the fibre coupling stuck at 5%?')).toBe('[ck: trigger=race size=auto pattern=race models=astra+opus+grok playbook=auto]')
  expect(line('plan split: SPCM bring-up kit for the lab PC')).toBe('[ck: trigger=plan+split size=L pattern=split models=auto playbook=auto]')
  expect(line('loop astra: raise the omega-loop Rabi frequency to the spec target')).toBe('[ck: trigger=loop+astra size=L pattern=loop models=astra playbook=design-loop]')
  expect(line('deep: the AQS 1-pager numbers before I send it')).toBe('[ck: trigger=deep size=auto pattern=panel models=auto playbook=auto]')
  expect(line('recall fnd_sin')).toBe('[ck: trigger=recall size=S pattern=auto models=auto playbook=pickup]')
  // the other words
  expect(line('check: the wiki page')).toBe('[ck: trigger=check size=auto pattern=pair models=auto playbook=auto]')
  expect(line('why did the run stop?')).toBe('[ck: trigger=why size=S pattern=auto models=auto playbook=research-scout]')
  expect(line('number: 158 nT at 1 s')).toBe('[ck: trigger=number size=S pattern=auto models=auto playbook=numbers]')
  expect(line('teach me the lock-in')).toBe('[ck: trigger=teach size=S pattern=auto models=auto playbook=teach]')
  // a size word wins; split is L unless a size is given; named models replace the race default
  expect(line('split M: two scripts')).toBe('[ck: trigger=split+M size=M pattern=split models=auto playbook=auto]')
  expect(line('race sol grok: which bonding layer?')).toBe('[ck: trigger=race+sol+grok size=auto pattern=race models=sol+grok playbook=auto]')
  expect(line('L: the whole bring-up')).toBe('[ck: trigger=L size=L pattern=auto models=auto playbook=auto]')
  // a word that asks for more agents wins over solo; words in any case; model words alone
  expect(line('quick race: x')).toBe('[ck: trigger=quick+race size=S pattern=race models=astra+opus+grok playbook=quick-answer]')
  expect(line('Quick: hi')).toBe('[ck: trigger=quick size=S pattern=solo models=auto playbook=quick-answer]')
  expect(line('grok: scout the datasheets')).toBe('[ck: trigger=grok size=auto pattern=auto models=grok playbook=auto]')
  // no trigger: sizes are upper case only, "Grok Bot" is the bot, the scan stops at the first other word, first line only
  expect(line('m: a lower-case m is not a size')).toBeNull()
  expect(line('Grok Bot is out again')).toBeNull()
  expect(line('run the M6 step with codex')).toBeNull()
  expect(line('note this: do it')).toBeNull()
  expect(line('constructor: x')).toBeNull()
  expect(line('')).toBeNull()
  expect(line('hello\nrace: second line')).toBeNull()
  expect(line('do it now\nrace: second line')).toBe('[ck: trigger=do size=M pattern=solo models=auto playbook=auto]')
})

test('mode on: a prompt that starts with trigger words gets one [ck: ...] line after the reminder', async ($, on) => {
  const w = world(on)
  const ran = await $.command.run({ ...TYPED, command: 'grokbot', args: 'do sol: note the SPCM kit as a wiki page' })
  expect(ran.context?.[0]).toContain(
    "chalach's task follows as the next prompt. The mod's reading of its first words: [ck: trigger=do+sol size=M pattern=solo models=sol playbook=auto]",
  )
  expect(ran.context?.[0]).toContain(`${HOME}/PhD_local/ckstack/ck-mode/SKILL.md (not found: tell chalach)`)

  await $.prompt.submit(typed('race: why is the fibre coupling stuck at 5%?\nmore lines'))
  const note = w.submitted.at(-1)?.context.find(c => c.startsWith('[Grok Bot mode ON')) ?? ''
  expect(note).toContain(`loads ckstack (${HOME}/PhD_local/ckstack/ck-mode/SKILL.md)`)
  expect(note.split('\n')).toEqual([
    expect.stringContaining('a [ck: ...] line follows'),
    '[ck: trigger=race size=auto pattern=race models=astra+opus+grok playbook=auto]',
  ])
  const ckLines = () => (w.submitted.at(-1)?.context ?? []).join('\n').split('\n').filter(l => l.startsWith('[ck: trigger='))
  await $.prompt.submit(typed('what changed today?'))
  expect(ckLines()).toEqual([])
  // a task notice is not chalach typing
  await $.prompt.submit({ text: 'race: a task notice', origin: { kind: 'task-notification' }, wait: false })
  expect(ckLines()).toEqual([])
  // with ckstack installed, ck-mode is attached to the activation note
  w.files.set(`${HOME}/PhD_local/ckstack/ck-mode/SKILL.md`, '# ck-mode: test router\n')
  const again = await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  expect(again.context?.[0]).toContain('ck-mode/SKILL.md, attached after the playbook.')
  expect(again.context?.[0]).toContain('# ck-mode: test router')
  // mode off: no reminder, no trigger line
  await $.command.run({ ...TYPED, command: 'grokbot', args: 'off' })
  await $.prompt.submit(typed('race: anything'))
  expect(w.submitted.at(-1)?.context).toEqual([])
})

test('the gate reads each ck-run command the way the shell does', () => {
  const text = `${CK} launch s1 run --title "a b" --note 'x;y' a\\ b \\\n  --ask A-1; echo "ck-run run"`
  const { words, end } = shellWords(text)
  expect(words).toEqual([CK, 'launch', 's1', 'run', '--title', 'a b', '--note', 'x;y', 'a b', '--ask', 'A-1'])
  expect(text.slice(end)).toStartWith('; echo')
  const calls = ckRunCalls(`cd ~/x && ${CK} ask list; ${CK} launch s1 run --ask A-1 && cat <<'EOF'\n${CK} run --agent x\nEOF`)
  expect(calls.map(c => [c.sub, c.kind])).toEqual([
    ['ask', 'read'],
    ['launch', 'write'],
  ])
  expect(calls[1]?.text).toBe(`${CK} launch s1 run --ask A-1`)
})

test('the gate sees a quoted ck-run, a quoted launch file, and code that starts with a ck-run path', async () => {
  const runs = [
    `"$HOME/PhD_local/ck-dispatch/bin/ck-run" run --agent codex --title t --prompt-file p.md`,
    `cd ~ && '${HOME}/PhD_local/ck-dispatch/bin/ck-run' launch s1 run --agent codex`,
    `bash -c "~/PhD_local/ck-dispatch/bin/ck-run run --agent claude --title t --prompt-file p.md"`,
    `osascript -e 'tell application "Terminal" to do script "~/PhD_local/ck-dispatch/bin/ck-run review R --by codex"'`,
    'bash "$HOME/PhD_local/agent_runs/t1.sh" > "$HOME/PhD_local/agent_runs/t1.out" 2>&1',
    'source "$HOME/PhD_local/agent_runs/t1.sh"',
    // inside double quotes, $( ) and backticks still run
    `OUT="$(${CK} launch s1 run --agent codex --ask A-1)"`,
    'echo "started: `bash ~/PhD_local/agent_runs/t1.sh`"',
  ]
  const reads = [
    `"$HOME/PhD_local/ck-dispatch/bin/ck-run" board`,
    `echo "~/PhD_local/ck-dispatch/bin/ck-run" run`,
    'cat "$HOME/PhD_local/agent_runs/t1.sh"',
    `cat > "$HOME/PhD_local/agent_runs/t1.sh" <<'EOF'\n#!/bin/bash\n${CK} run --agent codex --interactive\nEOF`,
    'echo done > "$HOME/PhD_local/agent_runs/t1.out"',
    `echo "today: \\$(${CK} run --agent x)"`, // an escaped $( is text
    'echo "see $(cat ~/PhD_local/agent_runs/t1.sh) and $(date)"',
    `OUT="$(${CK} board)"`,
    // ck-run named as a file, not run (real commands from 2026-11-03..05)
    'grep -n -i -E "lock|flock|busy" ckdispatch/*.py bin/ck-run tests/*.py | cut -c1-220',
    'ls -la bin/ck-run; wc -l bin/ck-run',
  ]
  for (const c of runs) expect([c, isDispatch(c)]).toEqual([c, true])
  for (const c of reads) expect([c, isDispatch(c)]).toEqual([c, false])
  expect(ckRunCalls(runs[2] ?? '').map(c => [c.sub, c.kind])).toEqual([['run', 'typed-run']])
  expect(ckRunCalls(runs[0] ?? '')[0]?.args.slice(0, 3)).toEqual(['run', '--agent', 'codex'])
  // a quoted launch is checked like any other: the documented one passes and its output is found
  const files = new Map<string, string>([
    [`${AR}/t1.sh`, RUN_LINE],
    [`${AR}/t1_prompt.md`, 'x'],
  ])
  const quoted = await checkLaunches(runs[4] ?? '', HOME, async p => files.get(p) ?? null)
  expect(quoted.ok && quoted.launches[0]?.outs).toEqual([`${AR}/t1.out`])
})

const V4_READS = [
  `${CK} board`,
  `${CK} board --json`,
  `${CK} digest --since 1d`,
  `${CK} recall fnd_sin`,
  `${CK} rules --check`,
  `${CK} models`,
  `${CK} scoreboard --since 30d`,
  `${CK} route --lane lab-code`,
  `${CK} ask list --open`,
  `${CK} ask show A-EXAMPLE-4`,
  `${CK} andon list`,
  `${CK} race show --ask A-EXAMPLE-4`,
  `${CK} launch a1 run --help`,
  `${CK} ask open --help`,
  `${CK} run --agent codex --title t --prompt-file p.md --dry-run`,
  `${CK} --version`,
]
const V4_WRITES = [
  `${CK} launch a1105-4-sol run --agent codex --workspace devfab --title "SPCM kit" --ask A-EXAMPLE-4 --prompt-file ~/PhD_local/agent_runs/t1_prompt.md`,
  `${CK} launch a1105-4-rv review ${RID} --by claude --trigger you-asked --ask A-EXAMPLE-4`,
  `${CK} race --ask A-EXAMPLE-4 --prompt-file ~/PhD_local/agent_runs/t1_prompt.md --title fibre --workspace devfab`,
  `${CK} panel ${RID} --by codex,claude --trigger leaves-lab --ask A-EXAMPLE-4`,
  `${CK} loop --ask A-EXAMPLE-4 --agent codex --prompt-file ~/PhD_local/agent_runs/t1_prompt.md --workspace devfab --metric "python3 m.py" --target ">=5" --max 3`,
  `${CK} reattach`,
  `${CK} accept ${RID} --note ok`,
  `${CK} ask open --words "note the SPCM kit as a wiki page" --size M --title "SPCM kit -> wiki"`,
  `${CK} ask close A-EXAMPLE-4 --done yes`,
  `${CK} ask park A-EXAMPLE-4 --reason "waits for chalach"`,
  `${CK} ask later A-EXAMPLE-4 "check the trace buffer"`,
  `${CK} ask answered A-EXAMPLE-4 --summary "Use PFI0."`,
  `${CK} andon raise --project nv --reason "laser is off"`,
  `${CK} andon clear --project nv --note "laser back"`,
]
const V4_TYPED = [
  `${CK} run --agent codex --workspace devfab --title t --ask A-EXAMPLE-4 --prompt-file ~/PhD_local/agent_runs/t1_prompt.md --interactive`,
  `${CK} dispatch --agent codex --workspace devfab --title t --prompt-file ~/PhD_local/agent_runs/t1_prompt.md`,
  `${CK} review ${RID} --by claude --trigger you-asked --interactive`,
  // a read, a --help or a --dry-run elsewhere in the command no longer hides a run (judged per command since v4)
  `${CK} board; ${CK} run --agent codex --title t --prompt-file p.md`,
  `ls --help; ${CK} run --agent codex --title t --prompt-file p.md`,
  `bash ~/PhD_local/agent_runs/t1.sh --dry-run && ${CK} review ${RID} --by claude`,
]

test('the v4 gate: reads always pass; mode off refuses every write; mode on refuses only a typed run', async ($, on) => {
  withLaunch(on)
  for (const c of V4_READS) expect([c, isDispatch(c)]).toEqual([c, false])
  for (const c of [...V4_WRITES, ...V4_TYPED]) expect([c, isDispatch(c)]).toEqual([c, true])
  const deny = async (command: string) => (await $.tool.call({ tool: 'Bash', command })).deny
  for (const c of V4_READS) expect([c, await deny(c)]).toEqual([c, undefined])
  for (const c of [...V4_WRITES, ...V4_TYPED]) expect([c, await deny(c)]).toEqual([c, expect.stringContaining('only in Grok Bot mode')])
  expect(await deny(V4_WRITES[0] ?? '')).toContain('Read-only ck-run commands work without the mode: show, thread, active')

  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  for (const c of V4_READS) expect([c, await deny(c)]).toEqual([c, undefined])
  for (const c of V4_WRITES) expect([c, await deny(c)]).toEqual([c, undefined])
  for (const c of V4_TYPED) {
    expect([c, await deny(c)]).toEqual([c, expect.stringContaining('must be documented in ~/PhD_local/agent_runs')])
  }
  expect(await deny(V4_TYPED[0] ?? '')).toContain('ck-run launch <slug> run --agent <agent>')
  // ck-run launch: a plain slug (the mod logs and watches it), and an iTerm window
  expect(await deny(`${CK} launch "$SLUG" run --agent codex --title t --ask A-1`)).toContain('plain text')
  expect(await deny(`${CK} launch s2 run --agent codex --title t --ask A-1 --no-live`)).toContain('iTerm window')
})

test('the gate covers env prefixes and absolute shell heredocs with quoted program paths', async ($, on) => {
  withLaunch(on)
  const commands = [
    `env TEST=1 "${CK}" ask open --words x --size S --title t`,
    `TEST=1 "${CK}" ask open --words x --size S --title t`,
    `/bin/bash <<'EOF'\n${CK} ask open --words x --size S --title t\nEOF`,
    `env TEST=1 /bin/bash -c '"${CK}" ask open --words x --size S --title t'`,
  ]
  for (const command of commands) {
    expect([command, isDispatch(command)]).toEqual([command, true])
    expect((await $.tool.call({ tool: 'Bash', command })).deny).toContain('only in Grok Bot mode')
  }
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  for (const command of commands) expect((await $.tool.call({ tool: 'Bash', command })).deny).toBeUndefined()
})

test('closing an ask does not hide an unaccepted flagged result', () => {
  const events = [
    { event: 'result', run_id: 'R1', ask_id: 'A-1105-1', status: 'flagged', ts: ago(2) },
    { event: 'ask_close', ask_id: 'A-1105-1', status: 'done', ts: ago(1) },
  ]
  expect(boardFacts([], events, NOW).needsYou).toBe(1)
  expect(boardFacts([], [...events, { event: 'accept', run_id: 'R1', accepted: false, ts: ago(0) }], NOW).needsYou).toBe(0)
})

test('mode on: ck-run launch is logged, watched, and wakes this session once when it ends', async ($, on) => {
  const w = world(on)
  await $.session.start(START)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  w.files.set(`${AR}/a1_prompt.md`, '# task a1\n')
  const cmd = `${CK} launch a1 run --agent codex --workspace devfab --title "SPCM kit" --ask A-EXAMPLE-4 --prompt-file ~/PhD_local/agent_runs/a1_prompt.md`
  expect((await $.tool.call({ tool: 'Bash', command: cmd })).deny).toBeUndefined()
  expect(standin(w)).toContain('· launch · a1')
  expect(standin(w)).toContain('- ck-run launch (detached) · ask A-EXAMPLE-4 · prompt `~/PhD_local/agent_runs/a1_prompt.md`')
  expect(standin(w)).toContain('- output `~/PhD_local/agent_runs/a1.out` · launch file `~/PhD_local/agent_runs/a1.sh` (written by ck-run)')
  expect(standin(w)).toContain(`- ck-run: \`${cmd}\``)

  // still running: the timer changes nothing
  await w.clock.advance(20_000)
  expect(w.notes).toEqual([])
  expect(wakes(w)).toEqual([])
  const status = await $.command.run({ ...TYPED, command: 'grokbot', args: 'status' })
  expect(status.text).toContain('Waiting:     a1 (2026-11-04 16:15 CST)')

  // it ends: result logged, one notification, one wake prompt
  w.files.set(`${AR}/a1.out`, `${AR}/${RID}/summary.md\nSPCM kit noted in the wiki.\nck-run exit=0\n`)
  await w.clock.advance(20_000)
  expect(standin(w)).toContain('· result · a1')
  expect(standin(w)).toContain(`\`${RID}\` · exit 0`)
  expect(w.notes).toEqual(['a1: finished, exit 0'])
  expect(wakes(w)).toEqual([
    '[ck-run] a1 finished (exit 0). Read ~/PhD_local/agent_runs/a1.out, then write the finish card (ck-run ask close) or the next step.',
  ])

  // never twice
  await w.clock.advance(60_000)
  await $.prompt.submit(typed('next'))
  expect(w.notes.length).toBe(1)
  expect(wakes(w).length).toBe(1)
  expect(standin(w).split('· result · a1').length).toBe(2)
})

test('a launch that ends between ticks is reported on the prompt that starts the next turn', async ($, on) => {
  const w = withLaunch(on)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  await $.tool.call({ tool: 'Bash', command: LAUNCH })
  w.files.set(`${AR}/t1.out`, `${AR}/${RID}/summary.md\nfailed: tests red\nck-run exit=3\n`)
  await $.prompt.submit({ text: 'background job done', origin: { kind: 'task-notification' }, wait: false })
  expect(w.submitted.at(-1)?.context).toContain(
    '[ck-run] t1 finished (exit 3). Read ~/PhD_local/agent_runs/t1.out, then write the finish card (ck-run ask close) or the next step.',
  )
  expect(w.notes).toEqual(['t1: finished, exit 3'])
  // reported once: the next prompt carries no second report
  await $.prompt.submit(typed('ok'))
  expect(w.submitted.at(-1)?.context.some(c => c.startsWith('[ck-run]'))).toBe(false)
})

test('a launch made in another session is logged here but wakes only its own session', async ($, on) => {
  const other = { out: `${AR}/other.out`, slug: 'other', at: NOW, stamp: '2026-11-04 16:00 CST', session: 'zzzzzzzz', kind: 'launch' }
  // that session is alive (its timer wrote a heartbeat), so this session leaves its launch alone
  const w = world(on, { pending: [other], beats: { zzzzzzzz: NOW } })
  w.files.set(`${AR}/other.out`, 'ck-run exit=0\n')
  await $.session.start(START)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  // the timer leaves other sessions' launches alone (the waiting list is shared by every session)
  await w.clock.advance(40_000)
  expect(standin(w)).not.toContain('· result · other')
  // a prompt logs its result, as in v3, but neither notifies nor wakes this session
  await $.prompt.submit(typed('anything new?'))
  expect(standin(w).split('· result · other').length).toBe(2)
  expect(w.submitted.at(-1)?.context.some(c => c.startsWith('[ck-run]'))).toBe(false)
  await w.clock.advance(40_000)
  await $.prompt.submit(typed('and now?'))
  expect(standin(w).split('· result · other').length).toBe(2)
  expect(w.notes).toEqual([])
  expect(wakes(w)).toEqual([])
})

test('ck-run race: its runs are found, logged, and wake once when all of them have ended', async ($, on) => {
  const w = world(on)
  w.files.set(`${AR}/r_prompt.md`, 'why is the fibre coupling stuck at 5%?\n')
  await $.session.start(START)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  const out = (alias: string) => `${AR}/A-EXAMPLE-4-race-${alias}.out`
  // what ck-run race does: one launch per model, each with its prompt copy, .sh and .out
  w.onBash = () => {
    for (const alias of ['astra', 'opus', 'grok']) {
      w.files.set(`${AR}/A-EXAMPLE-4-race-${alias}_prompt.md`, 'why?\n')
      w.files.set(`${AR}/A-EXAMPLE-4-race-${alias}.sh`, '#!/bin/bash\n')
      w.files.set(out(alias), '')
    }
  }
  const cmd = `${CK} race --ask A-EXAMPLE-4 --prompt-file ~/PhD_local/agent_runs/r_prompt.md --title "fibre" --workspace devfab`
  expect((await $.tool.call({ tool: 'Bash', command: cmd })).deny).toBeUndefined()
  w.onBash = undefined
  expect(standin(w)).toContain('· launch · A-EXAMPLE-4-race-astra')
  expect(standin(w)).toContain(
    '- started by ck-run race (detached) · ask A-EXAMPLE-4 · prompt `~/PhD_local/agent_runs/A-EXAMPLE-4-race-opus_prompt.md`',
  )

  // two of three end: one early wake for those two (chalach 2026-11-16: "inform the main bot leader right away")
  w.files.set(out('astra'), 'ck-run exit=0\n')
  w.files.set(out('opus'), 'ck-run exit=0\n')
  await w.clock.advance(20_000)
  expect(standin(w)).toContain('· result · A-EXAMPLE-4-race-opus')
  expect(w.notes).toEqual(['race A-EXAMPLE-4: A-EXAMPLE-4-race-astra, A-EXAMPLE-4-race-opus finished, exit 0 / 0 (1 still running)'])
  expect(wakes(w)).toEqual([
    '[ck-run] race A-EXAMPLE-4: A-EXAMPLE-4-race-astra (exit 0), A-EXAMPLE-4-race-opus (exit 0) finished; still running: A-EXAMPLE-4-race-grok. ' +
      'Read ~/PhD_local/agent_runs/A-EXAMPLE-4-race-astra.out, ~/PhD_local/agent_runs/A-EXAMPLE-4-race-opus.out now if useful; ' +
      'write the finish card only after the last run ends (one more wake comes then).',
  ])
  await w.clock.advance(20_000)
  expect(wakes(w).length).toBe(1) // never twice for the same run

  // the last one ends: one notification and one wake for the race
  w.files.set(out('grok'), 'ck-run exit=3\n')
  await w.clock.advance(20_000)
  expect(w.notes.at(-1)).toBe('race A-EXAMPLE-4: 3 runs finished, exit 0 / 3 / 0')
  expect(wakes(w).at(-1)).toBe(
    '[ck-run] race A-EXAMPLE-4 finished: A-EXAMPLE-4-race-astra (exit 0), A-EXAMPLE-4-race-grok (exit 3), A-EXAMPLE-4-race-opus (exit 0). ' +
      'Read their .out files in ~/PhD_local/agent_runs/, then write the finish card (ck-run ask close) or the next step.',
  )
  await w.clock.advance(40_000)
  expect(wakes(w).length).toBe(2)
})

test('a ck-run launch that ck-run refuses is not watched', async ($, on) => {
  const w = withLaunch(on)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  // the slug was used before (its .out exists): ck-run refuses it
  w.files.set(`${AR}/old.out`, 'ck-run exit=0\n')
  await $.tool.call({ tool: 'Bash', command: `${CK} launch old run --agent codex --ask A-1 --title t` })
  // the command failed and wrote no .out (no prompt file, say)
  w.toolResult = { isError: true, result: null, text: 'Exit code 2\nck-run: no prompt file' }
  await $.tool.call({ tool: 'Bash', command: `${CK} launch nofile run --agent codex --ask A-1 --title t` })
  expect(standin(w)).not.toContain('· launch · old')
  expect(standin(w)).not.toContain('· launch · nofile')
  const status = await $.command.run({ ...TYPED, command: 'grokbot', args: 'status' })
  expect(status.text).toContain('Waiting:     no runs')
})

test('the status line: one token per live run, the needs-you count, and +N more when long', () => {
  const ev = (o: Record<string, unknown>) => JSON.stringify(o)
  const records = [
    { run_id: 'R1', agent: 'codex', title: 'counter', started_at: ago(12) },
    { run_id: 'R2', agent: 'claude', title: 'wiki', started_at: ago(3) },
    { run_id: 'R3', agent: 'cursor', title: 'scout', started_at: ago(1) },
    { run_id: 'R4', agent: 'codex', title: 'ended', started_at: ago(40) }, // has a result: not live
    { run_id: 'R5', agent: 'agy', title: 'dead', started_at: ago(9 * 60) }, // older than 8 h: its ck-run died
    'not a record',
  ]
  const lines = [
    '"cut by tail"}',
    ev({ event: 'result', run_id: 'R9', status: 'flagged', ts: ago(25 * 60) }), // older than 24 h
    ev({ event: 'over_budget', ask_id: 'A-1105-2', status: 'refused', ts: ago(60) }),
    ev({ event: 'over_budget', ask_id: 'A-1105-2', status: 'approved', over: 'go on', ts: ago(58) }),
    ev({ event: 'dispatch', run_id: 'R4', agent: 'codex', model_alias: 'sol', ts: ago(40) }),
    ev({ event: 'result', run_id: 'R6', ask_id: 'A-1105-3', status: 'flagged', ts: ago(35) }), // needs you (1)
    ev({ event: 'result', run_id: 'R7', status: 'violation', ts: ago(34) }),
    ev({ event: 'accept', run_id: 'R7', accepted: false, ts: ago(33) }),
    ev({ event: 'result', run_id: 'R8', ask_id: 'A-1105-1', status: 'flagged', ts: ago(32) }),
    ev({ event: 'ask_close', ask_id: 'A-1105-1', status: 'done', ts: ago(31) }),
    ev({ event: 'stall', run_id: 'R4', reason: 'unknown', ts: ago(30) }), // R4 has ended since
    ev({ event: 'result', run_id: 'R4', status: 'ok', ts: ago(20) }),
    ev({ event: 'dispatch', run_id: 'R1', agent: 'codex', model_alias: 'sol', ts: ago(12) }),
    ev({ event: 'over_budget', ask_id: 'A-EXAMPLE-4', status: 'refused', ts: ago(5) }), // needs you (2)
    ev({ event: 'over_budget', ask_id: 'A-EXAMPLE-4', status: 'refused', ts: ago(4) }), // the same ask: counted once
    ev({ event: 'dispatch', run_id: 'R2', agent: 'claude', model_alias: 'opus', ts: ago(3) }),
    ev({ event: 'review', run_id: 'R3', agent: 'cursor', model_alias: 'grok', ts: ago(1) }),
    ev({ event: 'stall', run_id: 'R2', reason: 'approval', ts: ago(1) }), // needs you (3)
  ]
  const facts = boardFacts(records, parseLedger(lines), NOW)
  expect(facts.runs.map(r => r.alias)).toEqual(['sol', 'opus', 'grok'])
  expect(facts.needsYou).toBe(4) // R8 still needs a decision even though its ask closed
  expect(statusLine(facts, NOW)).toBe('Grok Bot ON · sol 12m · opus 3m · grok 1m · 4 need you')
  expect(statusLine({ runs: [], needsYou: 0 }, NOW)).toBe('Grok Bot ON (/grokbot off)')
  expect(statusLine({ runs: [], needsYou: 1 }, NOW)).toBe('Grok Bot ON · 1 needs you')
  // a run whose ledger lines are not in the tail shows its agent
  expect(boardFacts([{ run_id: 'R1', agent: 'codex', started_at: ago(2) }], [], NOW).runs[0]?.alias).toBe('codex')
  // runs that do not fit in 80 characters become "+N more"
  const many = Array.from({ length: 12 }, (_, i) => ({ runId: `X${i}`, alias: 'composer', startedMs: NOW - (100 + i) * 60_000, title: '' }))
  const long = statusLine({ runs: many, needsYou: 2 }, NOW)
  expect(long).toBe('Grok Bot ON · composer 100m · composer 101m · +10 more · 2 need you')
  expect(long.length).toBeLessThanOrEqual(80)
})

test('mode on: the status line shows each live run and what needs you, refreshed every 20 s', async ($, on) => {
  const w = world(on)
  w.files.set(`${AR}/.claims/R1.json`, JSON.stringify({ run_id: 'R1', agent: 'codex', title: 'counter', started_at: ago(12) }))
  w.files.set(`${AR}/.claims/registry.lock`, '')
  w.files.set(`${AR}/.claims/broken.json`, '{bad json')
  const ledger = (...events: Record<string, unknown>[]) => events.map(e => `${JSON.stringify(e)}\n`).join('')
  w.files.set(
    `${AR}/ledger.jsonl`,
    ledger(
      { event: 'dispatch', run_id: 'R1', agent: 'codex', model_alias: 'sol', ts: ago(12) },
      { event: 'over_budget', ask_id: 'A-EXAMPLE-4', status: 'refused', ts: ago(2) },
    ),
  )
  await $.session.start(START)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  expect(w.status.at(-1)).toBe('Grok Bot ON (/grokbot off)')
  await w.clock.advance(20_000)
  expect(w.status.at(-1)).toBe('Grok Bot ON · sol 12m · 1 needs you')
  expect(w.runs.some(argv => argv[0] === '/usr/bin/tail' && argv.at(-1) === `${AR}/ledger.jsonl`)).toBe(true)
  // a prompt keeps the line
  await $.prompt.submit(typed('hi'))
  expect(w.status.at(-1)).toBe('Grok Bot ON · sol 12m · 1 needs you')
  // the run ends and chalach approves going over: back to the plain line
  w.files.delete(`${AR}/.claims/R1.json`)
  w.files.set(
    `${AR}/ledger.jsonl`,
    `${w.files.get(`${AR}/ledger.jsonl`)}${ledger({ event: 'over_budget', ask_id: 'A-EXAMPLE-4', status: 'approved', over: 'go on', ts: ago(0) })}`,
  )
  await w.clock.advance(20_000)
  expect(w.status.at(-1)).toBe('Grok Bot ON (/grokbot off)')
  // mode off: the timer leaves the status line alone
  await $.command.run({ ...TYPED, command: 'grokbot', args: 'off' })
  await w.clock.advance(20_000)
  expect(w.status.at(-1)).toBeUndefined()
})

test('/grokbot board, today, recall, rules and models show ck-run output in any mode', async ($, on) => {
  const w = world(on)
  w.ck = argv => ({ stdout: `out of ${argv.slice(1).join(' ')}\n` })
  const view = async (args: string) => (await $.command.run({ ...TYPED, command: 'grokbot', args })).text ?? ''
  expect(await view('board')).toBe('out of board')
  expect(await view('today')).toBe('out of digest --since 1d')
  expect(await view('recall fnd_sin')).toBe('out of recall fnd_sin')
  expect(await view('rules')).toBe('out of rules')
  expect(await view('models')).toBe('out of models')
  expect(w.runs.map(argv => argv[0])).toEqual(Array(5).fill(`${HOME}/PhD_local/ck-dispatch/bin/ck-run`))
  expect(await view('recall')).toContain('Use: /grokbot recall <project>')
  expect(await view('status')).toContain('Grok Bot mode: off') // the views changed nothing
  // long output: the first 60 lines, then how many more
  w.ck = () => ({ stdout: Array.from({ length: 75 }, (_, i) => `line ${i + 1}`).join('\n') })
  const long = (await view('board')).split('\n')
  expect(long.length).toBe(61)
  expect(long[59]).toBe('line 60')
  expect(long[60]).toBe('... 15 more lines: run ~/PhD_local/ck-dispatch/bin/ck-run board')
  // a failure: the exit code and ck-run's error
  w.ck = () => ({ exitCode: 2, stderr: 'ck-run: no such project: nope\n' })
  expect(await view('recall nope')).toBe('ck-run recall nope ended with exit 2.\nck-run: no such project: nope')
  // ck-run cannot start
  w.ck = () => {
    throw new Error('not found')
  }
  expect(await view('models')).toContain('Could not run ~/PhD_local/ck-dispatch/bin/ck-run models')
  // more words after a view word are a task: the mode turns on
  expect(await view('board the slides with sol')).toContain('Grok Bot mode ON')
  expect(viewArgs('Today')).toEqual(['digest', '--since', '1d'])
  expect(viewArgs('recall fnd_sin and nv')).toBeNull()
})

// ---- audit fixes 2026-11-06

test('the gate sees ck-run kept in a shell variable ($CK, "$CK", ${CK})', async ($, on) => {
  withLaunch(on)
  const set = 'CK=~/PhD_local/ck-dispatch/bin/ck-run; '
  expect(isDispatch(`${set}$CK launch a1 run --ask A-1`)).toBe(true)
  expect(isDispatch(`${set}"$CK" ask open --words "x" --size S --title t`)).toBe(true)
  expect(isDispatch(`export CK="$HOME/PhD_local/ck-dispatch/bin/ck-run" && \${CK} run --agent codex`)).toBe(true)
  expect(isDispatch(`${set}$CK board`)).toBe(false)
  const deny = async (command: string) => (await $.tool.call({ tool: 'Bash', command })).deny
  expect(await deny(`${set}$CK launch a1 run --ask A-1`)).toContain('only in Grok Bot mode')
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  expect(await deny(`${set}$CK run --agent codex --workspace devfab --title t`)).toContain('must be documented in ~/PhD_local/agent_runs')
})

test('model words count anywhere in the first line; commas and "and" do not cut them', () => {
  expect(parseTriggers('race sol, grok: which bonding layer?')?.models).toEqual(['sol', 'grok'])
  expect(parseTriggers('race sol and grok: which bonding layer?')?.models).toEqual(['sol', 'grok'])
  expect(parseTriggers('do: note the SPCM kit as a wiki page with sol')?.models).toEqual(['sol'])
  expect(parseTriggers('quick, how do I wire the SPCM to the DAQ?')?.words).toEqual(['quick'])
  expect(parseTriggers('race: ask Grok Bot why')?.models).toEqual(['astra', 'opus', 'grok'])
  expect(parseTriggers('how do I use sol here?')).toBeNull()
})

test('the status line takes its "needs you" count from ck-run board', async ($, on) => {
  const w = world(on)
  w.ck = argv => (argv.includes('board') ? { stdout: JSON.stringify({ needs_you: [{}, {}], running: [], done_today: [] }) } : {})
  await $.session.start(START)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  await w.clock.advance(1_000)
  expect(w.status.at(-1)).toContain('2 need you')
  expect(w.runs.some(r => r.includes('board') && r.includes('--no-tail'))).toBe(true)
})

test('after a restart, a launch of a session that is gone wakes this session once', async ($, on) => {
  const old = { out: `${AR}/o1.out`, slug: 'o1', at: NOW, stamp: '2026-11-04 16:00 CST', session: 'deadbeef', kind: 'launch' }
  const w = world(on, { pending: [old] })
  await $.session.start(START)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  w.files.set(`${AR}/o1.out`, `${AR}/${RID}/summary.md\ndone.\nck-run exit=0\n`)
  await w.clock.advance(20_000)
  expect(standin(w)).toContain('· result · o1')
  expect(w.notes).toEqual(['o1: finished, exit 0'])
  expect(wakes(w).length).toBe(1)
  await w.clock.advance(60_000)
  expect(wakes(w).length).toBe(1)
})

test('mode off: a finished launch notifies once, and wakes Grok Bot on the next /grokbot', async ($, on) => {
  const w = world(on)
  await $.session.start(START)
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  w.files.set(`${AR}/a1_prompt.md`, '# task a1\n')
  const cmd = `${CK} launch a1 run --agent codex --workspace devfab --title "SPCM kit" --ask A-EXAMPLE-4 --prompt-file ~/PhD_local/agent_runs/a1_prompt.md`
  expect((await $.tool.call({ tool: 'Bash', command: cmd })).deny).toBeUndefined()
  await $.command.run({ ...TYPED, command: 'grokbot', args: 'off' })
  w.files.set(`${AR}/a1.out`, `${AR}/${RID}/summary.md\nSPCM kit noted.\nck-run exit=0\n`)
  await w.clock.advance(20_000)
  await w.clock.advance(20_000)
  expect(w.notes).toEqual(['a1: finished, exit 0'])
  expect(wakes(w)).toEqual([])
  await $.command.run({ ...TYPED, command: 'grokbot', args: '' })
  await w.clock.advance(1_000)
  expect(wakes(w).length).toBe(1)
  expect(w.notes.length).toBe(1)
})

