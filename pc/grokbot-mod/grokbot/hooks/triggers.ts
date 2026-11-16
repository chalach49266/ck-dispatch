// Trigger words (framework v4; chalach 2026-11-05: "Trigger words: YES"). The first words of chalach's prompt can
// name the size, pattern, models and playbook: "race: why is the fibre coupling stuck at 5%?", "do sol: note the
// SPCM kit as a wiki page", "recall fnd_sin". The mod adds one [ck: ...] line for Grok Bot. Pure, so tests call it.

export type Size = 'S' | 'M' | 'L'

export type Triggers = {
  // the leading trigger tokens in order: words in lower case, sizes as typed (S, M, L)
  words: string[]
  size: Size | 'auto'
  // solo, pair, race, split, panel, loop, or auto (Grok Bot picks)
  pattern: string
  // model words in order; the race default when only "race" is given; [] = auto
  models: string[]
  // a ckstack playbook or skill, or auto
  playbook: string
}

type Rule = { size?: Size; pattern?: string; playbook?: string }

// What each word means (BUILD_SPEC_v4 section 11.1). A Map, so "constructor" or "toString" are never words.
const RULES: ReadonlyMap<string, Rule> = new Map<string, Rule>([
  // core words
  ['quick', { size: 'S', pattern: 'solo', playbook: 'quick-answer' }],
  ['do', { size: 'M', pattern: 'solo' }], // the lane is Grok Bot's choice
  ['plan', { size: 'L' }],
  ['race', { pattern: 'race' }],
  ['check', { pattern: 'pair' }],
  ['recall', { size: 'S', playbook: 'pickup' }],
  // extra words
  ['split', { size: 'L', pattern: 'split' }], // L unless a size word is given
  ['deep', { pattern: 'panel' }],
  ['fix', { size: 'M', playbook: 'bug-fix' }],
  ['loop', { size: 'L', pattern: 'loop', playbook: 'design-loop' }],
  ['why', { size: 'S', playbook: 'research-scout' }],
  ['number', { size: 'S', playbook: 'numbers' }],
  ['teach', { size: 'S', playbook: 'teach' }],
])

export const MODEL_WORDS: readonly string[] = ['sol', 'astra', 'opus', 'sonnet', 'haiku', 'grok', 'composer', 'gemini']
// config/models.json race_default in ck-dispatch (BUILD_SPEC_v4 section 6)
export const RACE_MODELS: readonly string[] = ['astra', 'opus', 'grok']
const SIZES: readonly string[] = ['S', 'M', 'L']
// "race sol and grok", "race sol, grok": joiners between model words do not end the leading words
const JOINERS = new Set(['and', 'or', 'vs', '&', '+'])
// a token without its edge punctuation: "quick," -> "quick", "(sol)" -> "sol"
const clean = (t: string) => t.replace(/^[,;(]+|[,.;!?)]+$/g, '')

// Reads the leading tokens of the first line, until the first ":" or the first word that is not a trigger word.
// Words match in any case (a phone writes "Quick"); sizes only in upper case. null = no trigger word.
export function parseTriggers(text: string): Triggers | null {
  const first = (text.split('\n')[0] ?? '').trim()
  const colon = first.indexOf(':')
  const tokens = (colon < 0 ? first : first.slice(0, colon)).split(/\s+/).map(clean).filter(t => t !== '')
  const words: string[] = []
  for (const [i, token] of tokens.entries()) {
    const word = token.toLowerCase()
    if (SIZES.includes(token)) words.push(token)
    else if (word === 'grok' && /^bots?$/i.test(tokens[i + 1] ?? '')) break // "Grok Bot" names the bot, not the model
    else if (RULES.has(word) || MODEL_WORDS.includes(word)) words.push(word)
    else if (JOINERS.has(word) && MODEL_WORDS.includes((tokens[i + 1] ?? '').toLowerCase())) continue
    else break
  }
  if (words.length === 0) return null
  // The cheat sheet says model words work anywhere in the first line (audit 2026-11-06).
  const line = first.split(/\s+/).map(t => clean(t).toLowerCase())
  const anywhere = line.filter((w, i) => MODEL_WORDS.includes(w) && !(w === 'grok' && /^bots?$/.test(line[i + 1] ?? '')))
  const rules = words.map(w => RULES.get(w)).filter((r): r is Rule => r !== undefined)
  // A size word wins; else the first word that implies a size.
  const size = (words.find(w => SIZES.includes(w)) as Size | undefined) ?? rules.find(r => r.size)?.size ?? 'auto'
  // A word that asks for more than one agent (race, pair, split, panel, loop) wins over "solo".
  const patterns = rules.map(r => r.pattern).filter((p): p is string => p !== undefined)
  const pattern = patterns.find(p => p !== 'solo') ?? patterns[0] ?? 'auto'
  const named = [...new Set([...words.filter(w => MODEL_WORDS.includes(w)), ...anywhere])]
  const models = named.length > 0 ? named : words.includes('race') ? [...RACE_MODELS] : []
  const playbook = rules.find(r => r.playbook)?.playbook ?? 'auto'
  return { words, size, pattern, models, playbook }
}

// The one line the mod appends to the per-prompt reminder.
export function ckLine(t: Triggers): string {
  const models = t.models.length > 0 ? t.models.join('+') : 'auto'
  return `[ck: trigger=${t.words.join('+')} size=${t.size} pattern=${t.pattern} models=${models} playbook=${t.playbook}]`
}
