// The minimal board (chalach 2026-11-05, decision 5: "minimal would be good just to have an idea of each bot"):
// while Grok Bot mode is on, the status line shows one short token per live run and how many things need chalach,
//   Grok Bot ON · sol 12m · opus 3m · grok 1m · 1 needs you
// Live runs come from ck-run's run records (agent_runs/.claims/*.json), the model alias and the "needs you" count
// from the ledger tail. `ck-run board` is the full view (/grokbot board). Pure, so tests call it.

export const STATUS = 'Grok Bot ON (/grokbot off)'
export const STATUS_MAX = 80
// A run record older than this belongs to a ck-run that died (the longest run timeout is 2 h); `ck-run active`
// cleans such records, the line just leaves them out.
export const LIVE_MAX_MS = 8 * 3600_000
const DAY_MS = 24 * 3600_000
const ALIAS_MAX = 12

export type LedgerEvent = Readonly<Record<string, unknown>>
export type LiveRun = { runId: string; alias: string; startedMs: number; title: string }
export type BoardFacts = { runs: LiveRun[]; needsYou: number }

const str = (v: unknown): string => (typeof v === 'string' ? v : '')
const ENDED = new Set(['result', 'stale', 'watchdog_stop'])

// One ledger event per JSON line; a cut first line (the tail starts mid-line) and anything else is skipped.
export function parseLedger(lines: readonly string[]): LedgerEvent[] {
  const events: LedgerEvent[] = []
  for (const line of lines) {
    if (!line.startsWith('{')) continue
    try {
      const ev: unknown = JSON.parse(line)
      if (typeof ev === 'object' && ev !== null && !Array.isArray(ev)) events.push(ev as LedgerEvent)
    } catch {
      // half a line
    }
  }
  return events
}

// Live runs (oldest first) and the number of things that need chalach, from the run records and the ledger tail.
// "Needs you" (last 24 h): an ask refused over budget and not approved, run or closed since; a stall of a live
// run; a flagged or violation result with no accept/reject since. Each ask or run counts once.
export function boardFacts(records: readonly unknown[], events: readonly LedgerEvent[], now: number): BoardFacts {
  const ended = new Set(events.filter(e => ENDED.has(str(e.event))).map(e => str(e.run_id)))
  const runs: LiveRun[] = []
  for (const record of records) {
    if (typeof record !== 'object' || record === null) continue
    const rec = record as LedgerEvent
    const runId = str(rec.run_id)
    if (runId === '' || ended.has(runId)) continue
    const parsed = Date.parse(str(rec.started_at))
    const startedMs = Number.isNaN(parsed) ? now : parsed
    if (now - startedMs > LIVE_MAX_MS) continue
    let opening: LedgerEvent | undefined
    for (let i = events.length - 1; i >= 0 && opening === undefined; i--) {
      const e = events[i]
      if (e && str(e.run_id) === runId && (e.event === 'dispatch' || e.event === 'review')) opening = e
    }
    const alias = str(rec.model_alias) || str(opening?.model_alias) || str(rec.agent) || str(opening?.agent) || 'run'
    runs.push({ runId, alias: alias.slice(0, ALIAS_MAX), startedMs, title: str(rec.title) })
  }
  runs.sort((a, b) => a.startedMs - b.startedMs)

  const live = new Set(runs.map(r => r.runId))
  const since = now - DAY_MS
  const needs = new Set<string>()
  events.forEach((e, i) => {
    const ts = Date.parse(str(e.ts))
    if (Number.isNaN(ts) || ts < since) return
    const ask = str(e.ask_id)
    const run = str(e.run_id)
    const later = () => events.slice(i + 1)
    if (e.event === 'over_budget' && e.status === 'refused') {
      const solved =
        ask !== '' &&
        later().some(
          l =>
            str(l.ask_id) === ask &&
            ((l.event === 'over_budget' && l.status === 'approved') ||
              l.event === 'dispatch' ||
              l.event === 'review' ||
              l.event === 'ask_close'),
        )
      if (!solved) needs.add(`ask ${ask || `#${i}`}`)
    } else if (e.event === 'stall' && live.has(run)) {
      needs.add(`run ${run}`)
    } else if (e.event === 'result' && (e.status === 'flagged' || e.status === 'violation')) {
      const decided = later().some(
        l =>
          l.event === 'accept' && str(l.run_id) === run,
      )
      if (!decided) needs.add(`run ${run || `#${i}`}`)
    }
  })
  return { runs, needsYou: needs.size }
}

// The status line: the mode, one "<alias> <minutes>m" token per live run (oldest first), and the "needs you"
// count; runs that do not fit in STATUS_MAX characters become "+N more". Nothing to show: the plain ON line.
export function statusLine(facts: BoardFacts, now: number): string {
  const { runs, needsYou } = facts
  if (runs.length === 0 && needsYou === 0) return STATUS
  const tokens = runs.map(r => `${r.alias} ${Math.max(0, Math.floor((now - r.startedMs) / 60_000))}m`)
  const tail = needsYou > 0 ? [`${needsYou} ${needsYou === 1 ? 'needs' : 'need'} you`] : []
  for (let shown = tokens.length; ; shown--) {
    const more = tokens.length - shown
    const line = ['Grok Bot ON', ...tokens.slice(0, shown), ...(more > 0 ? [`+${more} more`] : []), ...tail].join(' · ')
    if (line.length <= STATUS_MAX || shown <= 0) return line
  }
}
