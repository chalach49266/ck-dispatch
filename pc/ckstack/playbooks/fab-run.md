# Playbook: fab-run

**When**: fabrication: a new run card, a recipe change, a dose test, the record of a run (intended vs actual),
SEM or ESEM notes.
**Trigger**: `plan` (a new run card, L) or `do` (records, M). **Pattern** solo; a pair when a review reason exists.
**Lane** `wiki`: sol writes in the Device Fabrication workspace. A new run card gets an opus check when a number in it
is a guess (`agent-unsure`) or chalach asks (`you-asked`).
**Read first**: `~/PhD_local/grok-bot-skills/fab-records/SKILL.md`, `~/PhD_local/grok-bot-skills/gdsfactory-design/SKILL.md`
(masks and layouts), `~/PhD_local/grok-bot-skills/phd-wiki-rules/SKILL.md`.
**Proof needed**: desk check for a card. `hardware` only after chalach runs it on the real tools.

## Steps
1. A new run card: a plan card first (the steps, the check after each step, a block diagram). chalach OKs it.
2. Every dose, time, temperature and power comes from the fab records (`measured`), the tool manual (`datasheet`) or a
   paper (`paper`), with the reference. Else TODO(verify). Never invent a dose.
3. Each step ends with a check: a dose test, develop, an SEM look, a liftoff check.
4. Records: intended and actual side by side. Raw images and tool logs stay read-only.
5. A changed card is a new file (v2). Keep the old one.

## Launch
`ck-run launch <ask>-fab run --model sol --workspace devfab --title "<title>" --ask <ask> --write-scope <run folder in Device Fabrication>`
Check a new card: `ck-run launch <ask>-review-claude review <run> --by claude --model opus --trigger agent-unsure --ask <ask> --focus "every dose, time and power: source?"`

## Done means
- Each step has a check. Every number has a source tag or TODO(verify).
- chalach decides the run. `your_move` names the first step to run in the cleanroom.
