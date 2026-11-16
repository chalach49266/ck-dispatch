# Playbook: wiki-records

**When**: notes, wiki pages, logs, indexes and records in the Device Fabrication Design Library or the NV Project wiki.
**Trigger**: `do`. **Size** M; a one-line note is S. **Pattern** solo. "For now" means a note, not an audit.
**Lane** `wiki`: sol; composer for sweeps; gemini for lint; sonnet checks (only with a reason).
**Read first**: `~/PhD_local/grok-bot-skills/phd-wiki-rules/SKILL.md` (wiki first, index, log, open questions),
`~/PhD_local/grok-bot-skills/fab-records/SKILL.md` (fab recipes, run logs, ESEM images, measurements).
**Proof needed**: desk check is usual. Numbers only from sources.

## Steps
1. Open the ask (M). Brief = chalach's words + "Do only this" (at most 4 steps).
2. Launch Sol with a narrow write scope (a shared folder such as the whole wiki is never claimed):
   `ck-run launch <ask>-wiki run --model sol --workspace devfab|nv --title "<title>" --ask <ask> --write-scope <wiki subfolder>`
3. The agent follows the wiki rules: the page, plus the index and the log; links both ways; unknowns go to the
   workspace's open questions file.
4. Datasheet or paper values carry the page or figure. Else TODO(verify).
5. Code problems seen while writing: `later`. A wiki ask does not fix code.

## Sweeps and lint
- Renames, links, tables across many pages: lane `mechanical` (composer; gemini also).
  The check is a diff check or the wiki lint, not a review.
- Wiki lint: gemini or composer in read mode; fixes go in a second run only if chalach says so.

## Review
Only with a reason: `leaves-lab` when the page feeds a slide, a paper or an email; `you-asked`.
`ck-run launch <ask>-review-claude review <run> --by claude --model sonnet --trigger leaves-lab --ask <ask> --focus "<numbers and claims>"`

## Done means
- The page exists. The index and the log are updated.
- Every number has a source or TODO(verify).
- `verification` = `desk_check` (or higher, only when something really ran).
