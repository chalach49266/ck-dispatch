---
name: race
description: Send the same question to 2 or 3 model families at once and merge ONE answer that shows where they disagree. Word "race".
disable-model-invocation: true
---

# race

**Word**: `race` (model words choose the arms). **Default arms**: astra, opus, grok. **Lane** `decision`.
**Budget**: ck-run gives a race at least one run per arm (default 3 arms), all live at once, top tier allowed.
One race = one question, not three jobs.

## Steps
1. Open: `ck-run ask open --words "<chalach verbatim>" --size S|M|L --title "<the question in your words>" --pattern race --models astra,opus,grok --project <name>`
2. Name the pick rule before the race and say it with the start card: `merge` (the default for questions),
   `best-of` (3 to 6 checks written first), or `first good`.
3. One brief for every arm: `~/PhD_local/agent_runs/<ask>-race_prompt.md` (chalach's words, "Do only this", and
   "list your sources and your choices with tags").
4. Start: `ck-run race --ask <ask> --prompt-file ~/PhD_local/agent_runs/<ask>-race_prompt.md --title "<title>" --workspace devfab|nv|washu --models astra,opus,grok --mode auto [--timeout S]`
   Always pass `--mode auto`: `ck-run race` defaults to read. Auto is the normal mode. `--mode read` puts grok (cursor) in plan mode, where it waits for approval and hangs. The brief says:
   "You may write ONLY the two files in your run folder (OUTBOX)."
   It prints the slugs `<ask>-race-<alias>`. Watch each `.out` (ck-mode section 4).
5. When all have ended: `ck-run race show --ask <ask>` shows the summaries side by side.
6. Merge ONE answer:
   - where they agree (a strong sign);
   - where they disagree: each side with its source tag; do not average a disagreement;
   - answers that missed the brief are dropped, and you say so.
7. Close: `ck-run ask close <ask> --done yes|partial|no`. Put the merged answer above the finish card.

## Never
- Three reports to chalach. One answer.
- A race on a small job that one model can answer (principle 2: the fleet, not a pile).
