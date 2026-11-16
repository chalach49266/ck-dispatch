// Grok Bot mode for this session. Session state only: a new or resumed session starts with the mode off.
export type Mode = {
  on: boolean
  // CT time the mode was turned on, '' when off
  since: string
  // the raw session log this mode writes to, '' before the first write
  sessionLog: string
}

declare module 'claude-code' {
  interface PluginState {
    grokbot: {
      mode: Mode
      // the last status line the 20 s timer built (live runs, "needs you"); '' or unset = the plain ON line
      line: string
    }
  }
}
