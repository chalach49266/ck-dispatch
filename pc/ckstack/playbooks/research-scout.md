# Playbook: research-scout

**When**: literature, datasheets, vendor pages, standards; "where did this come from"; "is there a paper on this".
**Trigger**: `why` (S), `race` for a hard open question (`skills/race`). **Pattern** solo.
**Lane** `scout`: grok; sol also good (web search is on since 2026-11-05); opus checks and writes the summary.
**Read first**: `~/PhD_local/grok-bot-skills/phd-wiki-rules/SKILL.md` (what the wiki already says; where answers go).
**Proof needed**: desk check, with a source for every claim.

## Steps
1. Look at what exists first: `decisions[]` in past results (`ck-run show <run>`, `ck-run thread <run>`), the wiki,
   git log, the router PROGRESS.md. Often no agent is needed (`skills/why`).
2. A scout run, auto mode (the normal mode; `--mode read` makes grok wait for approval, chalach 2026-11-15):
   `ck-run launch <ask>-scout run --model grok --workspace devfab|nv --title "<title>" --ask <ask>`
3. Every claim has a source: a URL, a DOI with the figure or equation, or a datasheet with the page.
4. The vendor's curve beats the vendor's summary. Read the plot at the point that matters (e.g. the weakest
   wavelength in the band). Check every model in a product family.
5. Sources disagree: show both sides with their tags. Do not average them.
6. A site hides data from scripts: say so. Do not guess the value.

## Review
Numbers or claims that leave the lab: `--trigger leaves-lab`, by opus:
`ck-run launch <ask>-review-claude review <run> --by claude --model opus --trigger leaves-lab --ask <ask> --focus "each claim: is the source real and does it say this?"`

## Done means
- Every claim has a source tag and a reference. Unknowns are marked UNKNOWN.
- Disagreements are shown, not hidden. Ideas for more reading go to `later`.
