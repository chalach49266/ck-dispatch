// grokbot mod: /grokbot turns Claude Code into the Grok Bot stand-in for this session.
//   /grokbot            mode on (the playbook is sent once; every later prompt carries a short reminder)
//   /grokbot <task>     mode on, then <task> is sent as chalach's next prompt
//   /grokbot status     mode, since when, and the log paths
//   /grokbot off        mode off
//   /grokbot board | today | recall <project> | rules | models
//                       read-only views from ck-run (any mode): board, digest --since 1d, recall, rules, models
// The rules live in ~/PhD_local/grokbot-mod/GROKBOT.md (read when the mode turns on, so edits apply on the next /grokbot);
// the playbook loads ckstack (~/PhD_local/ckstack/ck-mode/SKILL.md), which the mod attaches too.
// No prompt.compose hook: on Team and Enterprise plans the built-in guard cc-plugin-sec-default skips user mods there.
// While on, chalach's prompts are logged verbatim to ~/PhD_local/grokbot_router/sessions/<date>_<session>.md, and a
// prompt that starts with trigger words (quick, do, plan, race, ...) gets one [ck: ...] line for Grok Bot.
// While on, the status line shows each live run and what needs chalach (refreshed every 20 s).
// While off, Claude Code may not start runs through ck-run (chalach 2026-11-04: allow ck-run in Claude Code "only when
// i ask for it", i.e. when Grok usage is out and chalach turns on /grokbot). Read-only ck-run commands stay allowed.
// Launches made in this session are watched: when one ends, the mod logs it, notifies, and wakes Grok Bot once.
import type { EngineInterface, ProcessRunResult, Register, Timer } from 'claude-code'

import type { Mode } from '../types'
import { boardFacts, parseLedger, STATUS, statusLine } from './board'
import { ckLine, parseTriggers } from './triggers'

const MODE = { plugin: 'grokbot', key: 'mode' } as const
const LINE = { plugin: 'grokbot', key: 'line' } as const
const OFF: Mode = { on: false, since: '', sessionLog: '' }
const QUOTE_CAP = 4000
const TICK_MS = 20_000

// A heredoc opener (`<<EOF`, `<<-'EOF'`, `<< "EOF"`), and a line head that hands the heredoc to a shell.
const HEREDOC = /<<-?\s*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\1/
const FEEDS_SHELL = /(^|[\s;&|(])(?:[^\s;&|()]+\/)?(bash|sh|zsh)(\s|$)/
// Quoted text that is itself shell code: `bash -c '...'`, `sh -lc "..."`, `eval "..."`, `osascript ... -e '...'`.
const CODE_QUOTE = /(\b(bash|sh|zsh)(\s+-[a-zA-Z]+)*\s+-[a-zA-Z]*c|\beval|\bosascript\b.*\s-e)\s*$/
// A quoted word that the shell runs or writes to is code too (bypasses found in the v4 build, 2026-11-05):
// the program "$HOME/.../ck-run" at a command's start, a launch file run as bash "$HOME/PhD_local/agent_runs/x.sh",
// and a launch's output > "$HOME/PhD_local/agent_runs/x.out". Other quoted words stay text.
const COMMAND_START = /(^|[;&|(`]|\b(then|do|else|exec|nohup|time))\s*(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;&|]*\s+)*$/
const SCRIPT_START = /(^|[;&|(`]|\b(then|do|else|nohup|time))\s*(exec\s+)?((\S*\/)?(bash|sh|zsh)(\s+-\S+)*\s+|(source|\.)\s+)?$/
const STDOUT_TO = /(^|[^0-9&])(1|&)?>>?\s*$/
const QUOTED_CK_RUN = /^(["'])([^"'\n]*\/)?ck-run\1$/
const QUOTED_SCRIPT = /^(["'])[^"'\n]*agent_runs\/[^"'\s]+\.sh\1$/
const QUOTED_OUT = /^(["'])[^"'\s]*\.out\1$/

function quotedIsCode(quoted: string, lineSoFar: string): boolean {
  return (
    CODE_QUOTE.test(lineSoFar) ||
    (QUOTED_CK_RUN.test(quoted) && COMMAND_START.test(lineSoFar)) ||
    (QUOTED_SCRIPT.test(quoted) && SCRIPT_START.test(lineSoFar)) ||
    (QUOTED_OUT.test(quoted) && STDOUT_TO.test(lineSoFar))
  )
}

// Inside double quotes the shell still runs $( ... ) and ` ... `. Kept: those that run ck-run by its path or a
// launch file (OUT="$(~/PhD_local/ck-dispatch/bin/ck-run launch ...)"). A bare `ck-run` is not on chalach's PATH, so
// log text like "ran `ck-run review R`" stays text, as before. Everything else is blanked, lengths kept.
const RUNS_CK = /\/ck-run(\s|$)|agent_runs\/[^\s"'`)]+\.sh\b/
const blank = (text: string) => text.replace(/[^\n]/g, ' ')

function substitutionsOnly(quoted: string): string {
  let out = ''
  let i = 0
  while (i < quoted.length) {
    const c = quoted[i] ?? ''
    let span = ''
    if (c === '\\') {
      span = quoted.slice(i, i + 2) // an escaped $ or ` is text
      out += blank(span)
      i += span.length
      continue
    }
    if (quoted.startsWith('$(', i)) {
      let depth = 0
      let j = i + 1
      for (; j < quoted.length; j++) {
        if (quoted[j] === '(') depth++
        else if (quoted[j] === ')' && --depth === 0) break
      }
      span = quoted.slice(i, j + 1)
    } else if (c === '`') {
      const close = quoted.indexOf('`', i + 1)
      span = quoted.slice(i, close < 0 ? quoted.length : close + 1)
    }
    if (span !== '') {
      out += RUNS_CK.test(span) ? span : blank(span)
      i += span.length
    } else {
      out += c === '\n' ? '\n' : ' '
      i++
    }
  }
  return out
}

// The part of a Bash command that runs as shell code: heredoc bodies that do not go to a shell, and quoted strings
// that are not code, are blanked. So writing a prompt file, a router log or a .sh file that MENTIONS a ck-run
// dispatch is not a dispatch (false positive found live 2026-11-04 17:30), while `bash -c "ck-run run ..."` still is.
// Every line is kept (a blanked heredoc line is empty) and every kept line keeps its columns.
export function shellCode(command: string): string {
  const lines = command.split('\n')
  const kept: string[] = []
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? ''
    kept.push(line)
    const opener = HEREDOC.exec(line)
    if (!opener) continue
    const word = opener[2] ?? ''
    const toShell = FEEDS_SHELL.test(line.slice(0, opener.index))
    let j = i + 1
    while (j < lines.length && (lines[j] ?? '').replace(/^\t+/, '') !== word) {
      kept.push(toShell ? (lines[j] ?? '') : '')
      j++
    }
    if (j < lines.length) kept.push(lines[j] ?? '')
    i = j
  }
  const text = kept.join('\n')
  let code = ''
  let k = 0
  while (k < text.length) {
    const c = text[k] ?? ''
    if (c === '\\') {
      code += text.slice(k, k + 2)
      k += 2
    } else if (c === "'" || c === '"') {
      let end = k + 1
      while (end < text.length && text[end] !== c) end += c === '"' && text[end] === '\\' ? 2 : 1
      const quoted = text.slice(k, end + 1)
      const lineSoFar = code.slice(code.lastIndexOf('\n') + 1)
      code += quotedIsCode(quoted, lineSoFar) ? quoted : c === '"' ? substitutionsOnly(quoted) : blank(quoted)
      k = end + 1
    } else {
      code += c
      k++
    }
  }
  return code
}

// `--help` and `--dry-run` start nothing (a dry run prints the run id and keeps no folder).
const NO_RUN = /\s(--help|-h|--dry-run)\b/

// The words of the simple command at the start of `text`: quotes removed, escapes and line continuations resolved.
// It stops at an unquoted ; & | < > ( ) ` or newline; `end` is where (an index into `text`).
export function shellWords(text: string): { words: string[]; end: number } {
  const words: string[] = []
  let word = ''
  let has = false
  let i = 0
  const push = () => {
    if (has) words.push(word)
    word = ''
    has = false
  }
  while (i < text.length) {
    const c = text[i] ?? ''
    if (c === '\\') {
      if (text[i + 1] !== '\n') {
        word += text[i + 1] ?? ''
        has = true
      }
      i += 2
    } else if (c === "'") {
      const close = text.indexOf("'", i + 1)
      const stop = close < 0 ? text.length : close
      word += text.slice(i + 1, stop)
      has = true
      i = stop + 1
    } else if (c === '"') {
      let j = i + 1
      while (j < text.length && text[j] !== '"') {
        const next = text[j + 1] ?? ''
        if (text[j] === '\\' && next !== '' && '"\\$`'.includes(next)) {
          word += next
          j += 2
        } else {
          word += text[j] ?? ''
          j++
        }
      }
      has = true
      i = j + 1
    } else if (c === ' ' || c === '\t') {
      push()
      i++
    } else if (c === '\n' || ';&|<>()`'.includes(c)) {
      break
    } else {
      word += c
      has = true
      i++
    }
  }
  push()
  return { words, end: Math.min(i, text.length) }
}

// A place in shellCode's output is the same line and column in the command.
function rawIndex(command: string, code: string, at: number): number {
  const lineStart = code.lastIndexOf('\n', at - 1) + 1
  const line = code.slice(0, lineStart).split('\n').length - 1
  const lines = command.split('\n')
  let offset = 0
  for (let i = 0; i < line; i++) offset += (lines[i] ?? '').length + 1
  return offset + (at - lineStart)
}

// The shell code of one simple command from `start`: to the first ; & | < > ( ) ` or newline (continuations kept).
function segment(code: string, start: number): string {
  let end = start
  while (end < code.length) {
    const c = code[end] ?? ''
    if (c === '\\' && code[end + 1] === '\n') end += 2
    else if (c === '\n' || ';&|<>()`'.includes(c)) break
    else end++
  }
  return code.slice(start, end)
}

// ---- The gate (BUILD_SPEC_v4 11.5). Read-only ck-run commands always pass. In mode OFF every command that starts
// runs or changes asks is refused. In mode ON they pass, except a typed `ck-run run|dispatch|review`: those go
// through `ck-run launch` or a documented agent_runs/<slug>.sh.

// A ck-run program word: its path may sit in quotes of its own ("$HOME/.../ck-run"), or right after the opening quote
// of code given to bash -c / osascript -e (that quote is not part of the word).
const CK_CALL = /(^|[;&|(`'"]\s*|\s)(["']?)([^\s"'();&|<>`]*\/)?ck-run(?=["']?(\s|$))/gm
const READ_PAIRS = new Set(['ask list', 'ask show', 'andon list', 'race show'])
const TYPED_RUNS = new Set(['run', 'dispatch', 'review'])
const WRITE_SUBS = new Set(['accept', 'launch', 'race', 'panel', 'loop', 'reattach', 'ask', 'andon'])

// typed-run: run, dispatch or review typed directly; write: accept, launch, race, panel, loop, reattach,
// ask open|close|park|later|answered, andon raise|clear; read: everything else (show thread active limits route
// board digest recall rules models scoreboard, ask list|show, andon list, race show, --help, --dry-run, and a word
// that is no ck-run subcommand: `grep x bin/ck-run tests/*.py` names ck-run as a file, found in real commands).
export type CkKind = 'read' | 'typed-run' | 'write'
export type CkCall = { args: string[]; sub: string; kind: CkKind; text: string }

// Every ck-run command in the shell code of a Bash command: its words after `ck-run` (quotes removed), its
// subcommand, its kind, and its text as written.
// A ck-run path kept in a shell variable (`CK=~/PhD_local/ck-dispatch/bin/ck-run; $CK launch ...`, the form Claude
// typed on 2026-11-04): each use ($CK, "$CK", ${CK}) counts as the ck-run program word (audit 2026-11-06).
const CK_VAR = /(?:^|[\s;&|(])(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(["']?)(?:[^\s"';&|()<>`]*\/)?ck-run\2(?=[\s;&|)]|$)/gm
export function withCkVars(command: string): string {
  const names = new Set<string>()
  for (const m of command.matchAll(CK_VAR)) if (m[1]) names.add(m[1])
  let out = command
  for (const n of names) out = out.replace(new RegExp(`\\$\\{${n}\\}|\\$${n}(?![A-Za-z0-9_])`, 'g'), 'ck-run')
  return out
}

export function ckRunCalls(raw: string): CkCall[] {
  const command = withCkVars(raw)
  const code = shellCode(command)
  const calls: CkCall[] = []
  for (const m of code.matchAll(CK_CALL)) {
    const quote = m[2] ?? ''
    const wordStart = (m.index ?? 0) + (m[1] ?? '').length
    // a quote closed right after `ck-run` is the program word's own; any other opens code around it
    const start = quote !== '' && code[(m.index ?? 0) + m[0].length] !== quote ? wordStart + 1 : wordStart
    const at = rawIndex(command, code, start)
    const { words, end } = shellWords(command.slice(at))
    const args = words.slice(1)
    const i = args.findIndex(a => !a.startsWith('-'))
    const sub = i < 0 ? '' : (args[i] ?? '')
    let kind: CkKind = 'read'
    if (i < 0 || NO_RUN.test(segment(code, start)) || READ_PAIRS.has(`${sub} ${args[i + 1] ?? ''}`)) kind = 'read'
    else if (TYPED_RUNS.has(sub)) kind = 'typed-run'
    else if (WRITE_SUBS.has(sub)) kind = 'write'
    calls.push({ args, sub, kind, text: command.slice(at, at + end).replace(/\s+/g, ' ').trim() })
  }
  return calls
}

// The launch files a command runs (agent_runs/<x>.sh), in order.
const LAUNCH_PATHS =
  /(?:^|[;&|(`'"]\s*)\s*(?:exec\s+)?(?:(?:\S*\/)?(?:bash|sh|zsh)\s+(?:-\S+\s+)*|(?:source|\.)\s+)?(\S*agent_runs\/[^\s;&|'"]+\.sh)\b/gm

// The launch files a command runs, except those given --help or --dry-run.
function launchFiles(command: string): string[] {
  const code = shellCode(command)
  const paths: string[] = []
  for (const m of code.matchAll(LAUNCH_PATHS)) {
    const path = m[1] ?? ''
    const start = (m.index ?? 0) + m[0].length - path.length
    if (!NO_RUN.test(segment(code, start))) paths.push(path)
  }
  return paths
}

// True when a command starts runs or changes asks (refused while the mode is off).
export function isDispatch(command: string): boolean {
  return ckRunCalls(command).some(c => c.kind !== 'read') || launchFiles(command).length > 0
}

export const DENY_DISPATCH =
  'grokbot: ck-run commands that start runs or change asks are allowed from Claude Code only in Grok Bot mode ' +
  '(explicit Grok Bot mode required). Do not retry or work around this. Tell chalach: type /grokbot to ' +
  'let Claude stand in for Grok Bot, or run it from Grok Bot. Read-only ck-run commands work without the mode: show, ' +
  'thread, active, limits, route, board, digest, recall, rules, models, scoreboard, ask list, ask show, andon list, ' +
  'race show, and any --help or --dry-run.'

// ---- Documentation rule (chalach 2026-11-04: "every run or agent deployment is documented in the PhD_local agent_run").
// In Grok Bot mode an agent run starts from `ck-run launch <slug>` (v4: ck-run writes agent_runs/<slug>.sh and .out
// itself) or from a launch file ~/PhD_local/agent_runs/<slug>.sh whose ck-run lines use --interactive and a
// --prompt-file in agent_runs, with the output saved to agent_runs/<slug>*.out. The mod then writes each launch,
// accept and result to ~/PhD_local/agent_runs/grokbot_standin_log.md itself.

const RECIPE =
  'Write the task to ~/PhD_local/agent_runs/<slug>_prompt.md and the one ck-run line (with --interactive and ' +
  '--prompt-file ~/PhD_local/agent_runs/<slug>_prompt.md; a review needs no prompt file) to ~/PhD_local/agent_runs/<slug>.sh, ' +
  'then start it in the background with: bash ~/PhD_local/agent_runs/<slug>.sh > ~/PhD_local/agent_runs/<slug>.out 2>&1; ' +
  'echo "ck-run exit=$?" >> ~/PhD_local/agent_runs/<slug>.out'

const LAUNCH_HOWTO =
  'Easiest: write the task to ~/PhD_local/agent_runs/<slug>_prompt.md, then run ~/PhD_local/ck-dispatch/bin/ck-run ' +
  'launch <slug> run --agent <agent> --workspace <workspace> --title "<title>" --ask <ask id> --prompt-file ' +
  '~/PhD_local/agent_runs/<slug>_prompt.md (a review: ck-run launch <slug> review <run id> --by <agent> --trigger <reason>). ' +
  'ck-run writes <slug>.sh and <slug>.out in agent_runs and starts the run detached in an iTerm window; the mod wakes ' +
  'you when it ends.'

export const DENY_UNDOCUMENTED =
  'grokbot: in Grok Bot mode every agent run must be documented in ~/PhD_local/agent_runs (chalach 2026-11-04), so ' +
  `ck-run run/dispatch/review may not be typed directly. ${LAUNCH_HOWTO} Or use a launch file. ${RECIPE}`

export type Launch = {
  slug: string
  sh: string
  prompts: string[]
  outs: string[]
  // the window is the record: a direct agent session opened by osascript, with no ck-run line and no .out
  isWindowOnly: boolean
}

// One watched output: the result is logged when it holds "ck-run exit=N"; then this session gets one wake.
export type Pending = {
  out: string
  slug: string
  at: number
  stamp: string
  session: string
  // v4 (optional: entries an older mod stored still read)
  kind?: string // sh | launch | race | panel | reattach
  group?: string // one wake when every output of the group has ended (a race, a panel, a .sh with two outputs)
  label?: string // the group's name in the wake
  exits?: number[] // set once the result is logged; the entry then waits for its session's wake
  notified?: boolean // the Mac notification was sent while the mode was off; the wake still waits for /grokbot
  told?: boolean // a member of a group (race, panel) whose end already woke Grok Bot early (the group's wake still comes)
}

// `~/x`, `$HOME/x`, `${HOME}/x` or `/abs/x` (quotes stripped); null for a relative path or one with `..`.
export function resolvePath(raw: string, home: string): string | null {
  const p = raw.replace(/^['"]+|['"]+$/g, '')
  let abs: string | null = null
  if (p.startsWith('~/')) abs = `${home}${p.slice(1)}`
  else if (p.startsWith('$HOME/')) abs = `${home}${p.slice(5)}`
  else if (p.startsWith('${HOME}/')) abs = `${home}${p.slice(7)}`
  else if (p.startsWith('/')) abs = p
  if (abs === null || abs.includes('/../')) return null
  return abs
}

const agentRuns = (home: string) => `${home}/PhD_local/agent_runs/`
const baseName = (path: string) => path.slice(path.lastIndexOf('/') + 1)

// Where stdout goes: `> f`, `>> f`, `&> f`, `1> f` (not `2>&1`).
const REDIRECTS = /(?:^|[^0-9&])(?:1|&)?>>?\s*([^\s;&|<>()]+)/gm
const CK_RUN_LINE = /(^|[;&|(`'"]\s*|\s)(\S*\/)?ck-run\s+(run|dispatch|review)\b/
const PROMPT_FILE = /--prompt-file\s+(\S+)/g
const PROMPT_REF = /(\S*agent_runs\/[^\s'"`)]+_prompt\.md)/g

// Logical lines of shell code (backslash continuations joined).
const codeLines = (code: string) => code.replace(/\\\n/g, ' ').split('\n')

export type LaunchCheck = { ok: true; launches: Launch[] } | { ok: false; why: string }

// Checks that every launch file a command runs is documented as chalach asked. Pure, so the tests can call it.
export async function checkLaunches(
  command: string,
  home: string,
  readSh: (path: string) => Promise<string | null>,
): Promise<LaunchCheck> {
  const code = shellCode(command)
  const deny = (why: string): LaunchCheck => ({ ok: false, why: `grokbot: ${why} ${RECIPE}` })
  const launches: Launch[] = []
  const viaOsascript = /\bosascript\b/.test(code)
  for (const m of code.matchAll(LAUNCH_PATHS)) {
    const raw = m[1] ?? ''
    const sh = resolvePath(raw, home)
    if (sh === null || !sh.startsWith(agentRuns(home))) {
      return deny(`launch file ${raw} must be given as a full path inside ~/PhD_local/agent_runs/ (e.g. ~/PhD_local/agent_runs/<slug>.sh).`)
    }
    const text = await readSh(sh)
    if (text === null) return deny(`launch file ${sh} does not exist.`)
    const slug = baseName(sh).replace(/\.sh$/, '')
    const shCode = shellCode(text)
    const runLines = codeLines(shCode).filter(l => CK_RUN_LINE.test(l) && !NO_RUN.test(l))
    for (const line of runLines) {
      if (!/--(interactive|live)\b/.test(line) || /--no-(interactive|live)\b/.test(line)) {
        return deny(`a ck-run line in ${sh} has no --interactive (live windows required).`)
      }
    }
    const prompts: string[] = []
    for (const p of shCode.matchAll(PROMPT_FILE)) {
      const abs = resolvePath(p[1] ?? '', home)
      if (abs === null || !abs.startsWith(agentRuns(home))) {
        return deny(`--prompt-file ${p[1]} in ${sh} must be a full path inside ~/PhD_local/agent_runs/.`)
      }
      if ((await readSh(abs)) === null) return deny(`prompt file ${abs} (named in ${sh}) does not exist.`)
      prompts.push(abs)
    }
    if (runLines.some(l => /ck-run\s+(run|dispatch)\b/.test(l)) && prompts.length === 0) {
      return deny(`the ck-run run line in ${sh} has no --prompt-file.`)
    }
    const isWindowOnly = runLines.length === 0
    if (isWindowOnly) {
      // A direct agent session (the ck-dispatch exception): its prompt must still be saved in agent_runs.
      for (const p of text.matchAll(PROMPT_REF)) {
        const abs = resolvePath(p[1] ?? '', home)
        if (abs !== null && abs.startsWith(agentRuns(home)) && (await readSh(abs)) !== null) prompts.push(abs)
      }
      if (prompts.length === 0) return deny(`${sh} runs no ck-run line and names no saved prompt agent_runs/<slug>_prompt.md.`)
    }
    // Output written by the ck-run lines inside the file: each must be agent_runs/<slug>*.out.
    const outs: string[] = []
    for (const l of runLines) {
      for (const r of l.matchAll(REDIRECTS)) {
        const t = r[1] ?? ''
        if (t.startsWith('/dev/')) continue
        const abs = resolvePath(t, home)
        if (abs === null || !abs.startsWith(agentRuns(home)) || !baseName(abs).startsWith(slug) || !abs.endsWith('.out')) {
          return deny(`output ${t} in ${sh} must go to ~/PhD_local/agent_runs/${slug}.out (or ${slug}_<name>.out), not a scratchpad or /tmp.`)
        }
        if (!outs.includes(abs)) outs.push(abs)
      }
    }
    launches.push({ slug, sh, prompts, outs, isWindowOnly })
  }
  // Output redirected by the launching command: each .out (or agent_runs file) belongs to the launch whose slug it
  // starts with; other redirects (a log, /dev/null) are not ck-run output and are left alone.
  for (const r of code.matchAll(REDIRECTS)) {
    const t = r[1] ?? ''
    if (t.startsWith('/dev/')) continue
    const abs = resolvePath(t, home)
    const owner = abs === null ? undefined : launches.find(l => baseName(abs).startsWith(l.slug))
    if (abs !== null && owner && abs.startsWith(agentRuns(home)) && abs.endsWith('.out')) {
      if (!owner.outs.includes(abs)) owner.outs.push(abs)
    } else if (t.endsWith('.out') || (abs !== null && abs.startsWith(agentRuns(home)))) {
      const slug = launches[0]?.slug ?? '<slug>'
      return deny(`output ${t} must go to ~/PhD_local/agent_runs/${slug}.out (or ${slug}_<name>.out), not a scratchpad or /tmp.`)
    }
  }
  for (const l of launches) {
    if (l.outs.length === 0 && !(l.isWindowOnly && viaOsascript)) {
      return deny(`the output of ${l.sh} is not saved: redirect it to ~/PhD_local/agent_runs/${l.slug}.out.`)
    }
  }
  return { ok: true, launches }
}

// `ck-run launch <slug> <run|review|loop> [args...]` (BUILD_SPEC_v4 10.4), as typed in a Bash command.
export type LaunchCall = { slug: string; verb: string; ask: string; prompt: string; text: string; why: string }

const SLUG = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/

function optionValue(args: readonly string[], name: string): string {
  for (let i = 0; i < args.length; i++) {
    const a = args[i] ?? ''
    if (a === name) return args[i + 1] ?? ''
    if (a.startsWith(`${name}=`)) return a.slice(name.length + 1)
  }
  return ''
}

// The launch's slug, verb, ask and prompt file; `why` is set when the gate must refuse it.
export function parseLaunch(call: CkCall): LaunchCall {
  const i = call.args.indexOf('launch')
  const slug = call.args[i + 1] ?? ''
  const verb = call.args[i + 2] ?? ''
  const rest = call.args.slice(i + 3)
  let why = ''
  if (/[$`]/.test(slug)) {
    why = 'write the slug of ck-run launch as plain text (letters, digits, _ . -), not a $variable, so the mod can log the run and wake you when it ends.'
  } else if (rest.some(a => a === '--no-live' || a === '--no-interactive')) {
    why = 'ck-run launch with --no-live: chalach wants every run in an iTerm window (live windows required). Drop --no-live; ck-run launch adds --interactive itself.'
  }
  return { slug, verb, ask: optionValue(rest, '--ask'), prompt: optionValue(rest, '--prompt-file'), text: call.text, why }
}

// What a finished .out holds: each run's summary path (line 1 of ck-run's stdout), its summary, and the exit lines.
export function parseOut(text: string): { runIds: string[]; exits: number[]; summary: string[] } {
  const runIds = [...text.matchAll(/agent_runs\/(\d{4}-\d\d-\d\dT\d{6}-[^/\s]+)\/summary\.md/g)].map(m => m[1] ?? '')
  const exits = [...text.matchAll(/^ck-run exit=(\d+)\s*$/gm)].map(m => Number(m[1]))
  const summary = text
    .split('\n')
    .filter(l => l.trim() !== '' && !/^ck-run exit=/.test(l))
    .slice(0, 24)
  return { runIds, exits, summary }
}

const STANDIN_HEADER =
  '# Grok Bot stand-in: agent run log (written by the grokbot mod)\n\n' +
  'One entry per launch, accept/reject and result while Claude Code stands in for Grok Bot (`/grokbot`).\n' +
  'Each launch names its files in this folder: `<slug>_prompt.md` (task), `<slug>.sh` (launch), `<slug>.out` (ck-run output).\n' +
  'A `ck-run launch` (and race, panel, reattach) writes the `.sh` and `.out` itself; the mod logs each one it starts.\n' +
  'ck-run itself writes the run folder `<run id>/` and `ledger.jsonl`. Raw prompts: `~/PhD_local/grokbot_router/sessions/`.\n' +
  'Per project: router log + dispatch index (for example `~/PhD_local/project_router/PROGRESS.md`, `DISPATCHES.md`).\n\n'

const PENDING_KEY = 'pending'

async function readPending($: EngineInterface): Promise<Pending[]> {
  const raw = await $.store.get(PENDING_KEY)
  if (!Array.isArray(raw)) return []
  return raw.filter(
    (p): p is Pending =>
      typeof p === 'object' && p !== null && typeof (p as Pending).out === 'string' && typeof (p as Pending).at === 'number',
  )
}

// The waiting list is read, changed and written by the gate, the timer and prompts: one change at a time.
let queue: Promise<unknown> = Promise.resolve()
function serial<T>(work: () => Promise<T>): Promise<T> {
  const run = queue.then(work, work)
  queue = run.catch(() => undefined)
  return run
}

async function addPending($: EngineInterface, added: Pending[]): Promise<void> {
  if (added.length === 0) return
  await serial(async () => {
    const pending = await readPending($)
    const fresh = added.filter(a => !pending.some(p => p.out === a.out && p.exits === undefined))
    await $.store.set(PENDING_KEY, [...pending, ...fresh])
  })
}

const STALE_MS = 8 * 3600_000 // ck-run's longest timeout is 2 h; 8 h without an exit line = the run's watcher died

async function homeDir($: EngineInterface): Promise<string> {
  return (await $.env.get('HOME')) ?? '{{WSL_HOME}}'
}

const tilde = (home: string, path: string) => (path.startsWith(`${home}/`) ? `~${path.slice(home.length)}` : path)
const standinLog = (home: string) => `${agentRuns(home)}grokbot_standin_log.md`
const clip = (text: string, n: number) => (text.length > n ? `${text.slice(0, n)} [...]` : text)
const nameOf = (p: Pending) => baseName(p.out).replace(/\.out$/, '')
const groupOf = (p: Pending) => p.group ?? p.out

async function appendStandin($: EngineInterface, home: string, entry: string): Promise<void> {
  const path = standinLog(home)
  const old = (await $.fs.exists(path)) ? await $.fs.read(path) : STANDIN_HEADER
  await $.fs.write(path, `${old}${entry}\n`)
}

const whoLine = (home: string, mode: Mode, session: string) =>
  `Claude session \`${session.slice(0, 8)}\`${mode.sessionLog ? ` · raw prompts \`${tilde(home, mode.sessionLog)}\`` : ''}`

// Writes the launch (or accept) to agent_runs/grokbot_standin_log.md and remembers each .out to log its result later.
async function recordDispatch(
  $: EngineInterface,
  home: string,
  mode: Mode,
  launches: Launch[],
  accept: string | null,
): Promise<void> {
  const ms = await $.clock.now()
  const now = centralTime(ms)
  const session = await $.session.id()
  const who = whoLine(home, mode, session)
  const added: Pending[] = []
  for (const l of launches) {
    const sh = await $.fs.read(l.sh)
    // Log the line as written (quoted titles kept); the code view is only for finding it.
    const codeView = codeLines(shellCode(sh))
    const rawView = codeLines(sh)
    const at = codeView.findIndex(x => CK_RUN_LINE.test(x))
    const runLine = at < 0 ? undefined : rawView.length === codeView.length ? rawView[at] : codeView[at]
    const lines = [
      `## ${now.stamp} · launch · ${l.slug}`,
      `- launch file \`${tilde(home, l.sh)}\` · prompt ${l.prompts.map(p => `\`${tilde(home, p)}\``).join(', ') || '(none: review)'}`,
      `- output ${l.outs.map(o => `\`${tilde(home, o)}\``).join(', ') || '(none: direct session in an iTerm window, see the router log)'}`,
      runLine ? `- ck-run: \`${clip(runLine.replace(/\s+/g, ' ').trim(), 400)}\`` : `- direct agent session (no ck-run line)`,
      `- ${who}`,
      '',
    ]
    await appendStandin($, home, lines.join('\n'))
    for (const out of l.outs) {
      added.push({ out, slug: l.slug, at: ms, stamp: now.stamp, session: session.slice(0, 8), kind: 'sh', group: l.sh })
    }
  }
  if (accept !== null) {
    const code = shellCode(accept)
    const run = /ck-run\s+accept\s+(\S+)/.exec(code)?.[1] ?? '(run id not read)'
    const by = /--by\s+(\S+)/.exec(code)?.[1] ?? 'CK'
    const note = /--note\s+("((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+))/.exec(accept)
    const noteText = note ? (note[2] ?? note[3] ?? note[4] ?? '') : ''
    const verdict = /\s--reject\b/.test(code) ? 'reject' : 'accept'
    const lines = [
      `## ${now.stamp} · ${verdict} · \`${run}\``,
      `- by ${by}${noteText ? `: ${clip(noteText, 600)}` : ''}`,
      `- ${who}`,
      '',
    ]
    await appendStandin($, home, lines.join('\n'))
  }
  await addPending($, added)
}

// ---- Launches that ck-run starts itself (v4): `ck-run launch` (its slug says where its .out goes), and race,
// panel and reattach, whose new .out files are found by listing agent_runs before and after the command.

type Started = { name: string; out: string; kind: string; group: string; label: string; ask: string; prompt: string | null; how: string }
type Before = { fresh: Set<string>; outs: Set<string> | null }

const SPAWNERS = new Set(['race', 'panel', 'reattach'])

async function outNames($: EngineInterface, home: string): Promise<Set<string> | null> {
  try {
    const entries = await $.fs.list(agentRuns(home).slice(0, -1))
    return new Set(entries.filter(f => f.name.endsWith('.out')).map(f => f.name))
  } catch {
    return null
  }
}

// Before the command runs: which launch slugs are new (ck-run refuses a used one), and which .out files exist.
async function beforeRun($: EngineInterface, home: string, launches: LaunchCall[], spawners: CkCall[]): Promise<Before> {
  const fresh = new Set<string>()
  for (const l of launches) {
    if (!SLUG.test(l.slug)) continue
    const base = `${agentRuns(home)}${l.slug}`
    if (!(await $.fs.exists(`${base}.sh`)) && !(await $.fs.exists(`${base}.out`))) fresh.add(l.slug)
  }
  return { fresh, outs: spawners.length > 0 ? await outNames($, home) : null }
}

// After the command ran: the launches it started (a launch that ck-run refused wrote no .out and failed).
async function afterRun(
  $: EngineInterface,
  home: string,
  launches: LaunchCall[],
  spawners: CkCall[],
  before: Before,
  failed: boolean,
): Promise<Started[]> {
  const ms = await $.clock.now()
  const started: Started[] = []
  for (const l of launches) {
    const out = `${agentRuns(home)}${l.slug}.out`
    if (!before.fresh.has(l.slug) || started.some(s => s.out === out)) continue
    if (failed && !(await $.fs.exists(out))) continue
    // a prompt path the mod cannot place (relative) is logged as typed
    const prompt = l.prompt ? (resolvePath(l.prompt, home) ?? l.prompt) : null
    started.push({ name: l.slug, out, kind: 'launch', group: out, label: l.slug, ask: l.ask, prompt, how: l.text })
  }
  const was = before.outs
  const now = was ? await outNames($, home) : null
  if (was && now) {
    for (const c of spawners) {
      const ask = optionValue(c.args, '--ask')
      const target = c.args[c.args.indexOf(c.sub) + 1] ?? ''
      const mine = (n: string) =>
        c.sub === 'race'
          ? n.startsWith(ask ? `${ask}-race-` : '') && n.includes('-race-')
          : c.sub === 'panel'
            ? n.startsWith(ask ? `${ask}-panel-` : '') && n.includes('-panel-')
            : n.startsWith('reattach-')
      const label = c.sub === 'race' ? `race ${ask}` : c.sub === 'panel' ? `panel of ${target}` : ''
      for (const n of [...now].filter(x => !was.has(x) && mine(x)).sort()) {
        const out = `${agentRuns(home)}${n}`
        if (started.some(s => s.out === out)) continue
        const name = n.replace(/\.out$/, '')
        const group = c.sub === 'reattach' ? out : `${c.sub} ${ask || target} ${ms}`
        started.push({ name, out, kind: c.sub, group, label: label || name, ask, prompt: null, how: c.text })
      }
    }
  }
  for (const s of started) {
    if (s.prompt !== null) continue
    const saved = `${agentRuns(home)}${s.name}_prompt.md`
    if (await $.fs.exists(saved)) s.prompt = saved
  }
  return started
}

// Logs each started launch and adds its .out to the waiting list.
async function recordStarted($: EngineInterface, home: string, mode: Mode, started: Started[]): Promise<void> {
  if (started.length === 0) return
  const ms = await $.clock.now()
  const now = centralTime(ms)
  const session = await $.session.id()
  const who = whoLine(home, mode, session)
  for (const s of started) {
    const how = s.kind === 'launch' ? 'ck-run launch' : `started by ck-run ${s.kind}`
    const lines = [
      `## ${now.stamp} · launch · ${s.name}`,
      `- ${how} (detached) · ask ${s.ask || '(none given)'} · prompt ${s.prompt ? `\`${tilde(home, s.prompt)}\`` : '(none)'}`,
      `- output \`${tilde(home, s.out)}\` · launch file \`${tilde(home, s.out.replace(/\.out$/, '.sh'))}\` (written by ck-run)`,
      `- ck-run: \`${clip(s.how, 400)}\``,
      `- ${who}`,
      '',
    ]
    await appendStandin($, home, lines.join('\n'))
  }
  await addPending(
    $,
    started.map(s => ({ out: s.out, slug: s.name, at: ms, stamp: now.stamp, session: session.slice(0, 8), kind: s.kind, group: s.group, label: s.label })),
  )
}

// ---- Results and wakes. A finished launch is logged in any session; the session that launched it gets one
// notification and one wake (chalach 2026-11-05: the mod wakes Grok Bot when a run ends, never twice).

// waiting: an early wake for runs of a group that ended while others still run (chalach 2026-11-16: "inform the main
// bot leader right away"); the group's own wake still comes once when the last run ends.
export type Wake = { label: string; names: string[]; outs: string[]; exits: number[][]; notified?: boolean; waiting?: string[] }

const exitList = (exits: readonly number[]) => (exits.length > 0 ? exits.join(', ') : '?')

export function wakeText(w: Wake, home: string): string {
  if (w.waiting !== undefined) {
    const each = w.names.map((n, i) => `${n} (exit ${exitList(w.exits[i] ?? [])})`).join(', ')
    return (
      `[ck-run] ${w.label}: ${each} finished; still running: ${w.waiting.join(', ')}. ` +
      `Read ${w.outs.map(o => tilde(home, o)).join(', ')} now if useful; write the finish card only after the last run ends ` +
      '(one more wake comes then).'
    )
  }
  if (w.outs.length === 1) {
    return (
      `[ck-run] ${w.names[0]} finished (exit ${exitList(w.exits[0] ?? [])}). Read ${tilde(home, w.outs[0] ?? '')}, ` +
      'then write the finish card (ck-run ask close) or the next step.'
    )
  }
  const each = w.names.map((n, i) => `${n} (exit ${exitList(w.exits[i] ?? [])})`).join(', ')
  return (
    `[ck-run] ${w.label} finished: ${each}. Read their .out files in ~/PhD_local/agent_runs/, ` +
    'then write the finish card (ck-run ask close) or the next step.'
  )
}

const notifyText = (w: Wake) =>
  w.waiting !== undefined
    ? `${w.label}: ${w.names.join(', ')} finished, exit ${w.exits.map(exitList).join(' / ')} (${w.waiting.length} still running)`
    : w.outs.length === 1
    ? `${w.names[0]}: finished, exit ${exitList(w.exits[0] ?? [])}`
    : `${w.label}: ${w.outs.length} runs finished, exit ${w.exits.map(exitList).join(' / ')}`

async function notifyWakes($: EngineInterface, wakes: Wake[]): Promise<void> {
  for (const w of wakes) {
    try {
      await $.ui.notify(notifyText(w))
    } catch {
      // no terminal, or notifications off: the log and the wake still happen
    }
  }
}

// Logs the result of every finished launch (its .out holds "ck-run exit=N"); drops ones with no exit line after 8 h.
// With `wake`, this session's groups whose outputs have all ended leave the list and come back as wakes (once).
// With `onlyMine` (the 20 s timer) other sessions' launches are left alone: every session runs the timer, and the
// waiting list is shared, so only a prompt or /grokbot status touches another session's entries (as in v3).
async function checkPending($: EngineInterface, wake: boolean, onlyMine = false, consume = true): Promise<Wake[]> {
  return serial(async () => {
    const pending = await readPending($)
    if (pending.length === 0) return []
    const home = await homeDir($)
    const ms = await $.clock.now()
    const now = centralTime(ms)
    const me = (await $.session.id()).slice(0, 8)
    const kept: Pending[] = []
    let changed = false
    for (const p of pending) {
      if (p.exits !== undefined || (onlyMine && p.session !== me)) {
        kept.push(p)
        continue
      }
      const text = (await $.fs.exists(p.out)) ? await $.fs.read(p.out) : ''
      const parsed = parseOut(text)
      if (parsed.exits.length > 0) {
        const runs = parsed.runIds.map(id => `\`${id}\``).join(', ') || '(no run id in the output: ck-run refused or failed early)'
        const lines = [
          `## ${now.stamp} · result · ${nameOf(p)} (launched ${p.stamp})`,
          `- run ${runs} · exit ${parsed.exits.join(', ')}`,
          `- run folder${parsed.runIds.length > 1 ? 's' : ''} ${parsed.runIds.map(id => `\`~/PhD_local/agent_runs/${id}/\``).join(', ') || '-'} · output \`${tilde(home, p.out)}\``,
          `- summary (from the .out):`,
          ...parsed.summary.map(l => `  > ${clip(l, 300)}`),
          '',
        ]
        await appendStandin($, home, lines.join('\n'))
        kept.push({ ...p, exits: parsed.exits })
        changed = true
      } else if (ms - p.at > STALE_MS) {
        await appendStandin(
          $,
          home,
          `## ${now.stamp} · no result · ${nameOf(p)} (launched ${p.stamp})\n- no "ck-run exit=" line in \`${tilde(home, p.out)}\` after 8 h: ` +
            `the run's watcher probably died (a restart or a crash). Check \`ck-run active\`, \`ck-run reattach\` and the router log.\n`,
        )
        changed = true
      } else {
        kept.push(p)
      }
    }
    const gone = new Set<Pending>()
    const wakes: Wake[] = []
    if (wake) {
      const mine = kept.filter(p => p.session === me)
      for (const group of new Set(mine.filter(p => p.exits !== undefined).map(groupOf))) {
        const members = mine.filter(p => groupOf(p) === group)
        if (members.some(p => p.exits === undefined)) {
          // early wake (mode on only): the runs of this group that ended since the last tick
          const fresh = members.filter(p => p.exits !== undefined && !p.told)
          if (consume && fresh.length > 0) {
            for (const p of fresh) p.told = true
            changed = true
            wakes.push({
              label: members[0]?.label ?? nameOf(fresh[0] as Pending),
              names: fresh.map(nameOf),
              outs: fresh.map(p => p.out),
              exits: fresh.map(p => p.exits ?? []),
              waiting: members.filter(p => p.exits === undefined).map(nameOf),
            })
          }
          continue
        }
        const told = members.every(p => p.notified)
        if (!consume && told) continue // mode off: notified once already; keep it for the wake on the next /grokbot
        if (consume) for (const p of members) gone.add(p)
        else {
          for (const p of members) p.notified = true
          changed = true
        }
        const first = members[0]
        wakes.push({
          label: first?.label ?? (first ? nameOf(first) : ''),
          names: members.map(nameOf),
          outs: members.map(p => p.out),
          exits: members.map(p => p.exits ?? []),
          notified: told,
        })
      }
    }
    // A finished launch of another session waits for that session's wake; after 8 h it is dropped (its result is logged).
    if (!onlyMine) for (const p of kept) if (p.exits !== undefined && p.session !== me && ms - p.at > STALE_MS) gone.add(p)
    if (changed || gone.size > 0) await $.store.set(PENDING_KEY, kept.filter(p => !gone.has(p)))
    return wakes
  })
}

type Paths = {
  playbook: string
  ckmode: string
  ckrun: string
  routerLog: string
  sessionsDir: string
  framework: string
}

async function pathsFor($: EngineInterface): Promise<Paths> {
  const home = (await $.env.get('HOME')) ?? '{{WSL_HOME}}'
  return {
    playbook: `${home}/PhD_local/grokbot-mod/GROKBOT.md`,
    ckmode: `${home}/PhD_local/ckstack/ck-mode/SKILL.md`,
    ckrun: `${home}/PhD_local/ck-dispatch/bin/ck-run`,
    routerLog: `${home}/PhD_local/grokbot_router/PROGRESS.md`,
    sessionsDir: `${home}/PhD_local/grokbot_router/sessions`,
    framework: `{{PHD_ROOT}}/AI_Agent_Framework/Claude_Code_as_Grok_Bot_v4.md`,
  }
}

// US Central time without Intl: CDT (UTC-5) from the 2nd Sunday of March 02:00 to the 1st Sunday of November 02:00.
function nthSundayUtc(year: number, month: number, n: number, hourUtc: number): number {
  const firstDow = new Date(Date.UTC(year, month, 1)).getUTCDay()
  const firstSunday = 1 + ((7 - firstDow) % 7)
  return Date.UTC(year, month, firstSunday + 7 * (n - 1), hourUtc)
}

export function centralTime(ms: number): { date: string; stamp: string } {
  const year = new Date(ms).getUTCFullYear()
  const dstStart = nthSundayUtc(year, 2, 2, 8) // 02:00 CST = 08:00 UTC
  const dstEnd = nthSundayUtc(year, 10, 1, 7) // 02:00 CDT = 07:00 UTC
  const isDst = ms >= dstStart && ms < dstEnd
  const local = new Date(ms + (isDst ? -5 : -6) * 3600_000)
  const pad = (n: number) => String(n).padStart(2, '0')
  const date = `${local.getUTCFullYear()}-${pad(local.getUTCMonth() + 1)}-${pad(local.getUTCDate())}`
  const stamp = `${date} ${pad(local.getUTCHours())}:${pad(local.getUTCMinutes())} ${isDst ? 'CDT' : 'CST'}`
  return { date, stamp }
}

export function quote(text: string): string {
  const cut =
    text.length > QUOTE_CAP ? `${text.slice(0, QUOTE_CAP)}\n[... ${text.length - QUOTE_CAP} more characters not logged]` : text
  return cut
    .split('\n')
    .map(line => `  > ${line}`)
    .join('\n')
}

async function readMode($: EngineInterface): Promise<Mode> {
  const { value } = await $.state.get(MODE)
  return value ?? OFF
}

async function sessionLogPath($: EngineInterface, paths: Paths, date: string): Promise<string> {
  const id = await $.session.id()
  return `${paths.sessionsDir}/${date}_${id.slice(0, 8)}.md`
}

// Appends one entry to the raw session log, creating it with a header the first time.
async function appendLog($: EngineInterface, path: string, entry: string): Promise<void> {
  let old = ''
  if (await $.fs.exists(path)) old = await $.fs.read(path)
  if (old === '') {
    const id = await $.session.id()
    old =
      `# Grok Bot stand-in: raw session log (written by the grokbot mod)\n\n` +
      `Claude Code session \`${id}\`. chalach's prompts verbatim while Grok Bot mode is on, plus mode changes.\n` +
      `The curated router log (dispatches, run ids, decisions) is kept by Claude in ~/PhD_local/grokbot_router/PROGRESS.md\n` +
      `or the project's own router folder (for example ~/PhD_local/project_router/PROGRESS.md).\n\n`
  }
  await $.fs.write(path, `${old}${entry}\n`)
}

async function playbookText($: EngineInterface, paths: Paths): Promise<string> {
  try {
    return await $.fs.read(paths.playbook)
  } catch {
    return (
      `# Grok Bot mode is ON\n` +
      `The playbook file ${paths.playbook} could not be read. Act as Grok Bot (orchestrator only): follow ` +
      `${paths.ckmode} and ~/PhD_local/grok-bot-skills/ck-delegate/SKILL.md, start runs with ${paths.ckrun} launch, ` +
      `log everything in ~/PhD_local, and tell chalach the playbook is missing.`
    )
  }
}

async function readIfThere($: EngineInterface, path: string): Promise<string | null> {
  try {
    return (await $.fs.exists(path)) ? await $.fs.read(path) : null
  } catch {
    return null
  }
}

// Sent once, as a hidden note after the /grokbot output: the whole playbook and ck-mode, so no Read is needed.
function activationNote(paths: Paths, mode: Mode, task: string, playbook: string, ckmode: string | null): string {
  const triggers = task ? parseTriggers(task) : null
  return [
    `GROK BOT MODE IS ON (chalach typed /grokbot at ${mode.since}).`,
    `From now until /grokbot off you are Grok Bot (CK), chalach's orchestrator, standing in while Grok usage is out.`,
    `Follow the playbook below (file: ${paths.playbook}). It loads ckstack: the ck-mode router ${paths.ckmode}` +
      (ckmode === null ? ' (not found: tell chalach).' : ', attached after the playbook.'),
    `Every later prompt carries a short [Grok Bot mode ON] reminder; whenever the playbook or ck-mode is no longer in your`,
    `context (for example after a compaction), Read them again before you dispatch.`,
    `The mod logs chalach's prompts verbatim to ${mode.sessionLog}, logs each launch in ~/PhD_local/agent_runs/grokbot_standin_log.md,`,
    `and wakes you with a "[ck-run] <slug> finished" prompt when a launch made in this session ends.`,
    task
      ? `chalach's task follows as the next prompt.${triggers ? ` The mod's reading of its first words: ${ckLine(triggers)}` : ''}`
      : `Wait for chalach's task. On your next reply, first confirm Grok Bot mode in one line.`,
    ``,
    playbook,
    ...(ckmode === null ? [] : [``, `---- ${paths.ckmode} ----`, ckmode]),
  ].join('\n')
}

// Attached to every prompt while the mode is on. The built-in guard (cc-plugin-sec-default, Team and Enterprise
// plans) keeps user mods out of the system prompt, so this per-prompt note is what keeps the mode through compaction.
// `ck` is the [ck: ...] line of chalach's trigger words, when his prompt starts with them.
export function reminder(paths: Paths, mode: Mode, ck: string | null = null): string {
  const text =
    `[Grok Bot mode ON since ${mode.since}. You are Grok Bot (CK), the orchestrator. The playbook ${paths.playbook} ` +
    `loads ckstack (${paths.ckmode}); if either is not in your context now, Read it before acting. ` +
    `Each request is one ask (ck-run ask open, then the finish card with ck-run ask close); answer S asks yourself; ` +
    `a review only with a reason (--trigger). Start agent runs with ck-run launch <slug> run|review|loop ... ` +
    `(detached; the mod logs each launch in ~/PhD_local/agent_runs/grokbot_standin_log.md and wakes you when it ends) ` +
    `or a documented ~/PhD_local/agent_runs/<slug>.sh; a typed ck-run run/dispatch/review is refused. ` +
    `Log chalach's words and decisions in the router PROGRESS.md (${paths.routerLog} or the project's own). ` +
    `End with "Questions for chalach". When chalach starts with trigger words, a [ck: ...] line follows: the mod's ` +
    `reading of them (size, pattern, models, playbook); use it unless his words mean something else. /grokbot off ends the mode.]`
  return ck ? `${text}\n${ck}` : text
}

// ---- The minimal board: the status line (mode on), refreshed every 20 s.

const TAIL_BYTES = 3 * 1024 * 1024 // a process's output is read up to 4 MiB; the ledger tail stays under that
const TAIL_LINES = 2000

async function readRecords($: EngineInterface, home: string): Promise<unknown[]> {
  const dir = `${agentRuns(home)}.claims`
  let names: string[]
  try {
    names = (await $.fs.list(dir)).filter(f => f.kind === 'file' && f.name.endsWith('.json')).map(f => f.name)
  } catch {
    return [] // no runs yet
  }
  const records: unknown[] = []
  for (const name of names) {
    try {
      records.push(JSON.parse(await $.fs.read(`${dir}/${name}`)))
    } catch {
      // a record that ended or is being rewritten
    }
  }
  return records
}

async function ledgerTail($: EngineInterface, home: string): Promise<string[]> {
  try {
    const tail = await $.process.run(['/usr/bin/tail', '-c', String(TAIL_BYTES), `${agentRuns(home)}ledger.jsonl`], {
      timeoutMs: 10_000,
    })
    return tail.exitCode === 0 ? tail.stdout.split('\n').slice(-TAIL_LINES) : []
  } catch {
    return []
  }
}

// The "needs you" count comes from `ck-run board` itself (audit 2026-11-06), so the status line and /grokbot board
// always agree; asked at most once a minute, --no-tail: no iTerm calls. Falls back to the mod's own count.
let needsCache: { at: number; n: number } | undefined
async function boardNeeds($: EngineInterface, now: number): Promise<number | undefined> {
  if (needsCache && now - needsCache.at < 60_000) return needsCache.n
  try {
    const paths = await pathsFor($)
    const r = await $.process.run([paths.ckrun, 'board', '--json', '--no-tail'], { timeoutMs: 30_000 })
    if (r.exitCode !== 0) return undefined
    const parsed = JSON.parse(r.stdout) as { needs_you?: unknown[] }
    if (!Array.isArray(parsed.needs_you)) return undefined
    needsCache = { at: now, n: parsed.needs_you.length }
    return needsCache.n
  } catch {
    return undefined
  }
}

async function boardLine($: EngineInterface): Promise<string> {
  const home = await homeDir($)
  const now = await $.clock.now()
  const records = await readRecords($, home)
  const events = parseLedger(await ledgerTail($, home))
  const facts = boardFacts(records, events, now)
  const n = await boardNeeds($, now)
  return statusLine(n === undefined ? facts : { ...facts, needsYou: n }, now)
}

async function shownLine($: EngineInterface): Promise<string> {
  const { value } = await $.state.get(LINE)
  return value || STATUS
}

async function refreshStatus($: EngineInterface): Promise<void> {
  const line = await boardLine($)
  if (!(await readMode($)).on) return // turned off meanwhile
  await $.state.set(LINE, line)
  $.ui.status(line)
}

// Each session's timer writes a heartbeat; a session with the mode on adopts the waiting launches of sessions that
// are gone (no heartbeat for 2 min: a restart, /clear, /resume), so their results still wake Grok Bot (rev2 promise).
const BEATS_KEY = 'beats'
const GONE_MS = 120_000
async function beatAndAdopt($: EngineInterface, adopt: boolean): Promise<void> {
  await serial(async () => {
    const me = (await $.session.id()).slice(0, 8)
    const ms = await $.clock.now()
    const raw = await $.store.get(BEATS_KEY)
    const beats: Record<string, number> = raw && typeof raw === 'object' ? { ...(raw as Record<string, number>) } : {}
    beats[me] = ms
    for (const [s, t] of Object.entries(beats)) if (typeof t !== 'number' || ms - t > 24 * 3600_000) delete beats[s]
    await $.store.set(BEATS_KEY, beats)
    if (!adopt) return
    const pending = await readPending($)
    let moved = false
    const next = pending.map(p => {
      if (p.session === me) return p
      const t = beats[p.session]
      if (t !== undefined && ms - t < GONE_MS) return p // that session is alive: its own timer wakes it
      moved = true
      return { ...p, session: me }
    })
    if (moved) await $.store.set(PENDING_KEY, next)
  })
}

let ticking = false
let timer: Timer | undefined
// The activation note of an auto-on (CK_GROKBOT_AUTO=1), attached once by the prompt.submit hook to chalach's first
// prompt.
let autoNote: string[] | null = null

// Every 20 s: log finished launches, notify and wake (wake only while the mode is on), refresh the status line.
async function tick($: EngineInterface): Promise<void> {
  if (ticking) return
  ticking = true
  try {
    const mode = await readMode($)
    await beatAndAdopt($, mode.on)
    const wakes = await checkPending($, true, true, mode.on)
    if (wakes.length > 0) {
      const fresh = wakes.filter(w => !w.notified)
      if (fresh.length > 0) await notifyWakes($, fresh)
      if (mode.on) {
        const home = await homeDir($)
        // not awaited: it resolves only when the session is idle and the wake's turn starts
        void $.prompt.submit({ text: wakes.map(w => wakeText(w, home)).join('\n') }).catch(() => undefined)
      }
    }
    if (mode.on) await refreshStatus($)
  } catch {
    // the timer never fails
  } finally {
    ticking = false
  }
}

// ---- Read-only views (any mode): /grokbot board | today | recall <project> | rules | models.

const VIEW_LINES = 60

// The ck-run arguments for a view; 'usage' for a bare recall; null when the words are a task, not a view.
export function viewArgs(arg: string): string[] | 'usage' | null {
  const [word = '', ...rest] = arg.trim().split(/\s+/)
  switch (word.toLowerCase()) {
    case 'board':
      return rest.length === 0 ? ['board'] : null
    case 'today':
      return rest.length === 0 ? ['digest', '--since', '1d'] : null
    case 'rules':
      return rest.length === 0 ? ['rules'] : null
    case 'models':
      return rest.length === 0 ? ['models'] : null
    case 'recall':
      return rest.length === 0 ? 'usage' : rest.length === 1 ? ['recall', rest[0] ?? ''] : null
    default:
      return null
  }
}

async function runView($: EngineInterface, paths: Paths, args: string[]): Promise<string> {
  const home = await homeDir($)
  const shown = `ck-run ${args.join(' ')}`
  let ran: ProcessRunResult
  try {
    ran = await $.process.run([paths.ckrun, ...args], { timeoutMs: 120_000 })
  } catch (err) {
    return `Could not run ${tilde(home, paths.ckrun)} ${args.join(' ')}: ${err instanceof Error ? err.message : String(err)}`
  }
  const out = ran.stdout.replace(/\s+$/, '')
  const lines = out === '' ? [] : out.split('\n')
  const body =
    lines.length > VIEW_LINES
      ? [...lines.slice(0, VIEW_LINES), `... ${lines.length - VIEW_LINES} more lines: run ${tilde(home, paths.ckrun)} ${args.join(' ')}`]
      : lines
  if (ran.exitCode !== 0) {
    const err = ran.stderr.trim().split('\n').filter(l => l !== '').slice(0, 10)
    return [`${shown} ended with exit ${ran.exitCode}.`, ...err, ...body].join('\n')
  }
  return body.length > 0 ? body.join('\n') : `${shown}: no output.`
}

// Turns Grok Bot mode on: the /grokbot command, or CK_GROKBOT_AUTO=1 at session start. Sets the state, the status
// line and the session log, hands a task over as the next prompt, and returns the command's answer with the
// activation note for Claude's context. `note` is added to the log line (how the mode was turned on).
async function turnOn($: EngineInterface, arg: string, note: string): Promise<{ text: string; context: string[] }> {
  const paths = await pathsFor($)
  const now = centralTime(await $.clock.now())
  const mode = await readMode($)
  const log = mode.sessionLog || (await sessionLogPath($, paths, now.date))
  const wasOn = mode.on
  const updated: Mode = { on: true, since: wasOn ? mode.since : now.stamp, sessionLog: log }
  await $.state.set(MODE, updated)
  $.ui.status(wasOn ? await shownLine($) : STATUS)
  await appendLog(
    $,
    log,
    `- ${now.stamp} /grokbot ${wasOn ? '(mode already ON)' : 'ON'}${note}${arg ? `, task:\n${quote(arg)}` : ''}`,
  )
  // A command hook may not submit (it would wait on the turn it holds): hand the task over just after it returns.
  if (arg) $.clock.after(50, () => void $.prompt.submit({ text: arg, asUser: true }).catch(() => undefined))
  // Show the live runs now rather than at the next 20 s tick, and deliver results that ended while the mode was off.
  $.clock.after(100, () => void refreshStatus($).catch(() => undefined))
  $.clock.after(200, () => void tick($).catch(() => undefined))

  return {
    text: [
      wasOn ? `Grok Bot mode is already ON (since ${updated.since}).` : `Grok Bot mode ON. Claude is your Grok Bot stand-in now.`,
      arg
        ? `Routing your task through ck-run (agents open in iTerm2 windows).`
        : `Type a task: I route it through ck-run and the agents open in iTerm2 windows.`,
      `Log: ${log}`,
      `/grokbot status · /grokbot off · /grokbot board`,
    ].join('\n'),
    context: [activationNote(paths, updated, arg, await playbookText($, paths), await readIfThere($, paths.ckmode))],
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'grokbot',
      description:
        'Grok Bot mode: Claude routes work to claude/codex/cursor/agy through ck-run (off | status | board | today | recall | rules | models)',
      argumentHint: '[task | status | off | board | today | recall <project> | rules | models]',
    })
    const mode = await readMode($)
    $.ui.status(mode.on ? await shownLine($) : undefined)
    // The board and the wake: every 20 s (a reload drops the old timer with its environment).
    timer?.cancel()
    timer = $.clock.every(TICK_MS, () => void tick($))
    // Auto-on (chalach 2026-11-16, A-1116-2): the shell shortcuts cdev / cnv / gb start `claude` with
    // CK_GROKBOT_AUTO=1, and the mode turns on as if chalach typed /grokbot. Only when the mode was never on in this
    // session (no session log yet), so a later /grokbot off stays off after a reload. A mod's $.command.run skips its
    // own command hook, so turnOn runs directly, just after the session's start; the activation note goes to Claude
    // with chalach's first prompt (a prompt the mod submits would skip the mod's own prompt.submit hook).
    if (!mode.on && !mode.sessionLog && (await $.env.get('CK_GROKBOT_AUTO')) === '1') {
      $.clock.after(300, () =>
        void (async () => {
          const ran = await turnOn($, '', ' (auto: CK_GROKBOT_AUTO=1 from a shell shortcut)')
          $.ui.toast(ran.text.split('\n')[0] ?? 'Grok Bot mode ON')
          autoNote = ran.context
        })().catch(() => undefined),
      )
    }

    return next(e)
  })

  // The gate: ck-run commands that start runs or change asks only in Grok Bot mode, and only documented runs (a
  // `ck-run launch`, or a launch file in agent_runs with its prompt and output there). It judges before next, so a
  // failure refuses (fails closed); it records what ck-run started after next.
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const acting = ckRunCalls(e.command).filter(c => c.kind !== 'read')
    const files = launchFiles(e.command)
    if (acting.length === 0 && files.length === 0) return next(e)
    const mode = await readMode($)
    if (!mode.on) return { deny: DENY_DISPATCH }
    if (acting.some(c => c.kind === 'typed-run')) return { deny: DENY_UNDOCUMENTED }
    const launches = acting.filter(c => c.sub === 'launch').map(parseLaunch)
    const refused = launches.find(l => l.why !== '')
    if (refused) return { deny: `grokbot: ${refused.why}` }
    const home = await homeDir($)
    const readFile = async (path: string) => ((await $.fs.exists(path)) ? await $.fs.read(path) : null)
    const check: LaunchCheck = files.length > 0 ? await checkLaunches(e.command, home, readFile) : { ok: true, launches: [] }
    if (!check.ok) return { deny: check.why }
    try {
      await recordDispatch($, home, mode, check.launches, acting.some(c => c.sub === 'accept') ? e.command : null)
    } catch {
      $.ui.toast(`grokbot: could not write ${tilde(home, standinLog(home))}; log this run in the router log by hand.`)
    }
    const spawners = acting.filter(c => SPAWNERS.has(c.sub))
    const before = await beforeRun($, home, launches, spawners)

    const ran = await next(e)
    try {
      const started = await afterRun($, home, launches, spawners, before, ran.deny !== undefined || ran.isError === true)
      await recordStarted($, home, mode, started)
    } catch {
      $.ui.toast(`grokbot: could not log a ck-run launch in ${tilde(home, standinLog(home))}; log it in the router log by hand.`)
    }
    return ran
  }).catch(($, e, next) =>
    next.called ? next(e) : { deny: 'grokbot: the documentation gate itself failed on this command. Tell chalach; do not work around it.' },
  )

  // /clear, /resume and /branch start a new conversation: the engine resets $.state (mode off) and skips
  // session.start, so the status line would still say ON. End the mode cleanly and say so (found live 2026-11-04).
  for (const command of ['clear', 'resume', 'branch'] as const) {
    on('command.run', { command }, async ($, e, next) => {
      const before = await readMode($)
      const ran = await next(e)
      $.ui.status(undefined)
      if (before.on) {
        await $.state.set(MODE, OFF)
        if (before.sessionLog) {
          const now = centralTime(await $.clock.now())
          await appendLog($, before.sessionLog, `- ${now.stamp} /${command} ended Grok Bot mode (mode OFF)`)
        }
        $.ui.toast(`Grok Bot mode ended with /${command}. Type /grokbot to turn it on again.`)
      }

      return ran
    }).catch(($, e, next) => next(e))
  }

  on('command.run', { command: 'grokbot' }, async ($, e) => {
    const arg = (e.args ?? '').trim()
    const paths = await pathsFor($)

    // Read-only views work in any mode and change nothing.
    const view = viewArgs(arg)
    if (view === 'usage') return { text: 'Use: /grokbot recall <project>, for example /grokbot recall fnd_sin (runs ck-run recall).' }
    if (view !== null) return { text: await runView($, paths, view) }

    const now = centralTime(await $.clock.now())
    const mode = await readMode($)
    const log = mode.sessionLog || (await sessionLogPath($, paths, now.date))

    if (/^(off|stop|exit)$/i.test(arg)) {
      if (!mode.on) return { text: 'Grok Bot mode is already off.' }
      await $.state.set(MODE, { ...OFF, sessionLog: log })
      await $.state.set(LINE, '')
      $.ui.status(undefined)
      await appendLog($, log, `- ${now.stamp} /grokbot off (mode OFF)`)

      return {
        text: `Grok Bot mode OFF. Back to normal Claude Code.\nSession log: ${log}`,
        context: [
          `GROK BOT MODE IS OFF (chalach typed /grokbot off at ${now.stamp}). Stop acting as the orchestrator and work as ` +
            `normal Claude Code. Runs already dispatched keep going; if any are still live, finish their router log entries ` +
            `(${paths.routerLog} or the project's own) when their notices arrive.`,
        ],
      }
    }

    if (/^(status|help|\?)$/i.test(arg)) {
      const hasPlaybook = await $.fs.exists(paths.playbook)
      const hasCkmode = await $.fs.exists(paths.ckmode)
      const home = await homeDir($)
      await checkPending($, false)
      const waiting = (await readPending($)).filter(p => p.exits === undefined)
      return {
        text: [
          `Grok Bot mode: ${mode.on ? `ON since ${mode.since}` : 'off'}`,
          `Playbook:    ${paths.playbook}${hasPlaybook ? '' : '  (MISSING)'}`,
          `ck-mode:     ${paths.ckmode}${hasCkmode ? '' : '  (MISSING)'}`,
          `Run log:     ${standinLog(home)} (every launch, accept and result, written by the mod)`,
          `Waiting:     ${waiting.length === 0 ? 'no runs' : waiting.map(p => `${nameOf(p)} (${p.stamp})`).join(', ')}`,
          `Router log:  ${paths.routerLog} (or the project's own, e.g. ~/PhD_local/project_router/PROGRESS.md)`,
          `Session log: ${mode.on || mode.sessionLog ? log : '(none yet)'}`,
          `Framework:   ${paths.framework}`,
          `Use: /grokbot [task] · status · off · board · today · recall <project> · rules · models`,
        ].join('\n'),
      }
    }

    return turnOn($, arg, '')
  })

  // While the mode is on: log chalach's own prompts verbatim (Grok Bot keeps chalach's words), and attach the
  // reminder to every prompt that starts a turn (chalach's, the /grokbot task, a finished ck-run job's notice),
  // with the [ck: ...] line when chalach's prompt starts with trigger words, and any launch that just ended.
  on('prompt.submit', async ($, e, next) => {
    let mode: Mode = OFF
    const isPerson = e.origin.kind === 'composer' || e.origin.kind === 'bridge'
    try {
      mode = await readMode($)
      if (mode.on && mode.sessionLog && isPerson && !e.command && !e.isMeta) {
        const now = centralTime(await $.clock.now())
        await appendLog($, mode.sessionLog, `- ${now.stamp} chalach:\n${quote(e.text)}`)
      }
    } catch {
      // logging must never block a prompt
    }
    const live = mode.on && !e.command && !e.isMeta
    let wakes: Wake[] = []
    try {
      wakes = await checkPending($, live) // a finished run's notice arrives as a prompt: log its result now (in any mode)
    } catch {
      // never block a prompt
    }
    try {
      $.ui.status(mode.on ? await shownLine($) : undefined) // keep the status line true to the state
    } catch {
      // never block a prompt
    }
    if (!live) return next(e)

    const home = await homeDir($)
    await notifyWakes($, wakes)
    const triggers = isPerson ? parseTriggers(e.text) : null
    const notes = [reminder(await pathsFor($), mode, triggers ? ckLine(triggers) : null), ...wakes.map(w => wakeText(w, home))]
    if (autoNote) {
      notes.unshift(...autoNote) // the auto-on's activation note, once
      autoNote = null
    }
    return next({ ...e, context: [...(e.context ?? []), ...notes] })
  }).catch(($, e, next) => next(e))
}
