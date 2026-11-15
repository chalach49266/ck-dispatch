# Concurrency summary (2026-11-03)

Commit `cf64c2c` (local, not pushed). Prompt: `~/PhD_local/agent_runs/concurrency_prompt.md`.

## What the old constraint was

`ckdispatch/runner.py` had a class `SlotLock`. Each workspace got `max_runs_per_workspace` flock slot files
(`agent_runs/.locks/<ws>.slot<i>.lock`; default 2, set in `config/workspaces.json`):

- A read run took one free slot.
- An edit or auto run took every slot. Auto is the default mode, so in practice one run per workspace.
- If nothing was free, the run got ledger `busy` and exit 6, "<workspace> already has 2 run(s), or an edit run, in progress".

It was per workspace (no global lock), and every bot hits it because every bot goes through ck-run. The ledger shows
it biting today: a bot's "Phase3 plan" codex and cursor runs in devfab got `busy` 11 times between 18:19 and 18:24.
At 18:22 someone left an uncommitted interim hack in the working tree (auto no longer exclusive, cap 4). This commit
replaces it.

## What changed (all in ck-run)

- **No workspace lock.** `SlotLock` and `max_runs_per_workspace` are gone (an old config key is ignored). Any number
  of runs, of any of the four agents, in the same or different workspaces, start at once. Nothing is serialized.
- **Per-path claims** (`ckdispatch/claims.py`, new). `ck-run run --claim PATH` (repeatable) declares a file or
  folder an auto/edit run will edit. The path must be inside the run's write area; read mode and reviews are
  refused. A claim that is the same as, inside, or around a live run's claim is refused: `busy`, exit 6, nothing
  started, no run folder left. The note names the other run, its agent, pid and start time. Runs without claims are
  never blocked.
- **Run registry.** Every run has `agent_runs/.claims/<run_id>.json` while it works, flock-held by ck-run. The check
  and the registration happen under `.claims/registry.lock`, held for milliseconds. `ck-run active` lists the
  live runs (JSON).
- **Stale cleanup.** A record whose flock is free means ck-run crashed or was killed. The next run (or `ck-run
  active`) deletes it and logs a ledger `stale` event (status failed; shown in `ck-run thread`). If the agent outlived
  its ck-run, the claims stay until that process ends. That covers the headless child (its pid is in the record) and
  a live window (`live.pid` without `live.exit`).
- **Agents are told.** Auto/edit prompts get a `CONCURRENCY:` line: this run's claims, other live runs' claims
  ("never edit them"), and "do not undo changes you did not make". Claude also gets `Edit` deny rules for those
  paths. The claims show in `cmd.txt` and `--dry-run`.
- **After-run checks are concurrency-aware.** Before this change, a read run next to a writer would have been a
  `violation` ("file changed during a read-only run"). Now:
  - A change the disk scan finds that lies in another overlapping run's claim goes to that run. So does a change only
    a concurrent writer could have made (a read run never writes; a writer's change outside this run's write area).
    It becomes a note and `rulecheck.json` `changed_by_others`, not this run's files or a violation.
  - A change both runs could have made stays this run's, with a note naming the other run.
  - A file this run's agent reported changing is a flag (exit 4) if a concurrent run claimed it or also reported
    changing it.
  - `rulecheck.json` `concurrent` lists the overlapping runs.
  - The cursor/agy post-run review gets the same split, a list of the other runs, and ignores deletions inside
    their claims.
- **Safe concurrent writes.**
  - The run folder is reserved with `mkdir` before anything else; on a clash ck-run picks a new id.
  - Ledger lines were already one `O_APPEND` write under `flock`. Reads now take a shared lock, and every line has
    a `claims` key.
  - `state.json` (rate limits) used a fixed `.tmp` name and an unlocked read-modify-write, so two runs could lose
    each other's update. It now runs under `state.json.lock` with a per-process temp file.
- **Docs.** README has a new section "Concurrent runs", plus updates to the exit-6 row, ledger keys, test list and
  file list. Also updated: `docs/framework_v3` (md, one-pager html, PDF regenerated with 1 page; the PDF is untracked
  as before) and `docs/architecture.mmd`.

## Tests

**134/134 pass** (`python3 -I tests/run_all.py`, about 136 s). That is the 113 existing tests (all passing), with the
old slot test replaced by "old slot files are ignored, no busy", plus **21 new** in `tests/test_concurrency.py`.
They use real ck-run processes started together:

- two runs in the same workspace with different agents (claude + codex) overlap in time and both end ok
- edit + auto + read in one workspace
- runs in different workspaces
- overlapping claims refused (inside, around, same absolute path; codex never started); allowed again once the
  claiming run ends
- non-overlapping claims run together
- runs without claims are never blocked
- other runs' claims reach the prompt and Claude's deny rules
- bad claims refused (read mode, raw folder, rule file, other workspace, outside `--write-scope`)
- dry run registers nothing
- **stale cleanup after a SIGKILLed ck-run**: the claim is kept while its orphaned agent runs, released after,
  with a `stale` ledger event and the thread view
- a live-window agent that outlived its ck-run keeps its claim
- `ck-run active`
- a read run is not blamed for a concurrent writer's change
- changes inside another run's claim are that run's
- the same file edited by two concurrent runs is flagged
- editing a file another live run claimed is flagged
- a cursor post-run review is not blamed for a claimed run's edits and deletions
- run-folder reservation never reuses an id
- 8 processes appending 480 ledger lines (all whole)
- the registry in-process
- `state.json` updates from 4 processes (none lost)

## Real proof (live CLIs, real config, 2026-11-03 18:47:36 CT)

Script: `/private/tmp/claude-501/-Users-ckasemtantikul-PhD-local-ck-dispatch/3977d430-3955-40bd-b45d-48088fa7b876/scratchpad/smoke_concurrency.py`.
Report: `.../scratchpad/smoke_report.json`. Both runs were started at the same moment in **devfab**, in **auto**
mode (the default, which the old lock made exclusive). They ran headless with `--write-scope
~/PhD_local/ck-smoke-scratch` and the prompt "read the two wiki entry files, then answer hello; change nothing".

| run | agent | claims | started | finished | status |
|---|---|---|---|---|---|
| 2026-11-03T184736-devfab-quick_lookup-claude-accb | claude | `ck-smoke-scratch/concurrency_claim.txt` | 18:47:36 | 18:47:56 | ok (exit 0) |
| 2026-11-03T184736-devfab-quick_lookup-codex-5848 | codex | none | 18:47:36 | 18:48:05 | ok (exit 0) |

- Neither was blocked. Both registered 0.21 s after launch, and they **overlapped for 20 s**. Both summaries say
  "hello", and neither changed a file.
- While both were live, `ck-run active` listed them. A third run (codex) claiming the same file as claude was
  **refused in 0.11 s**: exit 6, `BUSY: file claim conflict ... claimed by live run ...claude-accb, claude, mode
  auto, pid 72817`. Ledger `busy` run `...codex-3143`; no agent started.
- Also live during the smoke, from a bot (not me): the "Phase3 discuss" runs `183744-claude-e46f` (live iTerm2,
  still running when this was written), `183747-codex-d4d2` and `183750-cursor-f67b`. All three were in devfab at
  the same time and started on this code. Their `rulecheck.json` lists each other as concurrent, with no violations.
  codex-d4d2 and cursor-f67b ended `flagged` for reasons that have nothing to do with concurrency: a `cloud_sim`
  keyword in the agents' own "no Flexcompute/Tidy3D" text.

## Rules elsewhere that say "one run at a time" (listed, NOT edited)

Outside ck-dispatch:
- `~/Library/CloudStorage/OneDrive-WashingtonUniversityinSt.Louis/Documents/PhD/AI_Agent_Framework/AI_Agent_Delegation_Framework_v2.md:333`
  "Rules: at most 2 runs in parallel per workspace."
- `.../AI_Agent_Framework/AI_Agent_Delegation_Framework_v3.md`:
  - :128 "6 | busy | the workspace already has 2 runs, or an edit run"
  - :169 "**Locks:** an edit run locks its workspace. Read runs: at most 2 per workspace."
  - :19 and :35 list "workspace locks" among the guards
  - :21 and :203 list the `busy` event
  - This is the OneDrive copy; `ck-dispatch/docs/framework_v3/` has the updated text.
- `.../AI_Agent_Framework/framework_v3_onepager.pdf`: "✓ max 2 runs / workspace" and "Max 2 runs per workspace; an
  edit run locks it." (ck-dispatch's regenerated PDF has the new text.)
- `~/PhD_local/grok-bot-skills/ck-delegate/SKILL.md:35` "You may run up to 2 at a time per workspace, and an edit
  run locks its workspace (exit 6 = busy; retry after the running job's notice)."
- The four prompt skills say "6 busy … Busy (6): wait for the running job's notice, then retry." The exit code still
  exists, but busy now means only a claim conflict:
  - `~/PhD_local/grok-bot-skills/claude-code-prompt/SKILL.md:41,50-51`
  - `~/PhD_local/grok-bot-skills/codex-prompt/SKILL.md:41,50-51`
  - `~/PhD_local/grok-bot-skills/cursor-agent-prompt/SKILL.md:44,53-54`
  - `~/PhD_local/grok-bot-skills/antigravity-prompt/SKILL.md:42,51-52`
- Related but not a ck-run rule: `~/PhD_local/grok-bot-agents/skills/delegate-task/scripts/run-agent.sh:157`
  "Serialize every writer/reviewer by canonical workdir" (`worktree_guard.sh`/`.py`). That is a separate runner,
  and each of its runs gets its own worktree or work folder, so it only serializes runs that share one workdir
  (resumes). It does not limit one run per workspace.

Inside ck-dispatch (edited): `README.md` lines 40, 115, 168 and 195 (old lock text), and the ledger, test and file
sections. `docs/framework_v3/AI_Agent_Delegation_Framework_v3.md` lines 19, 21, 35, 128, 169 and 218.
`docs/framework_v3/framework_v3_onepager.html` (+ PDF). `docs/architecture.mmd:4`.

## Notes

- Claims are opt-in. Two runs without claims can still edit the same file. That is caught after the run (flagged
  when both agents report the file), not prevented. Claude is the only agent stopped by deny rules, and only for
  paths claimed when it started.
- The commit's `Co-Authored-By` line has no email: the rulebook-guard hook blocks email addresses in commit messages.
- Untracked and left alone: `.tmp_find_opt.py`, `config/workspaces.json.bak`, earlier `*_summary.md` files, and
  `docs/framework_v3/summary.md` and the PDF.

## Questions for chalach

1. Should I update the out-of-repo skills (grok-bot-skills `ck-delegate` and the 4 prompt skills) and the OneDrive
   framework copy (v3 md + PDF; v2 is historical)? The new wording would be "runs go in parallel; pass `--claim`
   for files you will edit; busy (6) = claim conflict, so wait for that run or claim other paths". As asked, I only
   listed them.
2. Should edit mode claim its `--write-scope` folders automatically, so that two edit runs in the same folder are
   refused before they start instead of flagged after? Claims are opt-in right now.
