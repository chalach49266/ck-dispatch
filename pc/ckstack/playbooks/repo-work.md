# Playbook: repo-work

**When**: code repos in `~/PhD_local` and the Photonics_Ecosystem repos: refactors, renames, multi-file changes,
cutovers.
**Trigger**: `plan` or `split` (L), `do` (M). **Pattern** solo, or split for independent parts.
**Lane** `repo`: sol; astra for the hard step; opus checks.
**Read first**: the repo's own README, AGENTS.md or CLAUDE.md;
`~/PhD_local/grok-bot-skills/photonforge-repo-maintenance/SKILL.md` (Photonics_Ecosystem repos).
**Proof needed**: fake data or tests: the full test suite passes, with the count taken from the real run.

## Steps
1. L: a plan card: steps that each end with a test run, and a block diagram. chalach OKs it.
2. Subtract first: delete dead code and stale paths before adding anything.
3. `cd` into the repo (or a git worktree), then launch with the repo as the write scope. A `~/PhD_local` git repo can
   be the write scope for all four agents.
4. One step per run. The tests pass after each step. Commits stay local.
5. The hard step (cross-cutting design, tricky concurrency): `--model astra`, on an L step chalach marks hard.
6. Push, merge and GitHub renames: only with chalach's OK for that action. Grok Bot runs approved git steps itself,
   one at a time, and logs each command and its output.
7. ck-dispatch and the grokbot mod are protected (agents cannot edit them): ask chalach first.

## Launch
`ck-run launch <ask>-repo run --model sol --workspace devfab --title "<title>" --ask <ask> --write-scope <repo folder>`
Check, only with a reason (`you-asked`, `agent-unsure`, or `hardware-or-raw` for instrument code):
`ck-run launch <ask>-review-claude review <run> --by claude --model opus --trigger <reason> --ask <ask> --focus "<the risky change>"`

## Done means
- The full suite passes; the pass count comes from the run, not from memory.
- The change list is short and in plain words. chalach OKs the push.
