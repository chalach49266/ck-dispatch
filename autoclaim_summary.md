# Autoclaim summary (2026-11-03)

Prompt: `~/PhD_local/agent_runs/autoclaim_prompt.md`. chalach (19:02 CT): "Yes, have edit runs claim their write
folders automatically". Follow-up to `cf64c2c` and `concurrency_summary.md`.

Commits, all local (nothing pushed):
- ck-dispatch `f30d308`: the code, the tests, README, and framework v3 md + one-pager html.
- ck-dispatch `e4af426`: one more v3 doc line. It used to say "max 2 runs per workspace is now a lock".
- grok-bot-skills `ff22b9d`: the 5 skills.

## 1. What ck-run does now

- **Edit runs claim their `--write-scope` folders automatically.** So does an auto run given an explicit
  `--write-scope`. Some cases are refused with `busy`, exit 6, before any agent starts:
  - a second edit run on the same folder;
  - a run whose folder is inside or around that folder;
  - an auto run whose `--write-scope` or `--claim` overlaps it.

  The stderr and the ledger note name the other run and say "(its --write-scope, claimed automatically)". The hint
  says to wait for that run or use other paths.
- **Shared folders are never claimed automatically**, because that would serialize every run in a workspace
  (`claims.shared_area()`). Never claimed:
  - a workspace root;
  - a default write folder (`writable_only`): nv `NV Project/` and `AQS/`, and each washu `<course>/solutions/` and
    `<course>/prep/`;
  - a folder that holds a wiki entry file (`wiki_first` or `open_questions`): devfab `Design Library/`,
    `Design Library/maps/` and `Design Library/questions/`, nv `NV Project/wiki/`, washu `HW Brain/`;
  - `~/PhD_local`, or any folder that contains one of the above.

  Such a scope still works as a write area, but claims nothing. `cmd.txt` and `--dry-run` list it as "not claimed
  automatically (reason; claim a subfolder or file with --claim)". A narrower subfolder is still claimed, for
  example `Design Library/pages`, `AQS/MCG V2`, `Projects/Notes` or a `~/PhD_local/<repo>`.
- **What claims nothing automatically:** auto runs without `--write-scope` (the default; this includes the nv/washu
  default folders), read runs and reviews. They are never blocked by auto-claims. The prompt tells them which paths
  other runs claimed, and Claude also gets deny rules for those paths.
- **Explicit `--claim` still works.** It adds paths. A claim inside an auto-claimed folder is covered by that folder.
  A claim in a shared scope is kept, for example `--write-scope "Design Library"` with `--claim "Design Library/pages/x.md"`.
- **Where the claims show:**
  - The run record and `ck-run active` have a new key, `claims_auto`.
  - The ledger `claims` key and `packet.json` `claims` include the automatic claims. The ledger keys are unchanged.
  - The prompt's `CONCURRENCY:` line says "other runs cannot claim these while it works".
  - The CLI help for `--write-scope` and `--claim` explains this.

Files: `ckdispatch/claims.py` (`merge`, `shared_area`, `from_scopes` and the docstring), `ckdispatch/runner.py`,
`ckdispatch/cli.py`, `ckdispatch/prompt.py`, `ckdispatch/ledger.py` (doc), `README.md` ("Concurrent runs", exit 6
row, ledger, tests), `docs/framework_v3/AI_Agent_Delegation_Framework_v3.md`, `docs/framework_v3/framework_v3_onepager.html`.

**Safe for live bots.** Bots run `bin/ck-run` straight from the working tree, so the code was built in a git worktree
on the branch `autoclaim`. Main was fast-forwarded only after the full suite passed, and only while `ck-run active`
listed no live runs. The worktree and the branch are removed.

## 2. Wording for bots (outside the repo)

New wording everywhere: runs go in parallel; edit runs claim their write folders automatically; pass `--claim` for
other files you will edit; busy (6) = file/folder claim conflict, so wait for that run or use other paths.

- `~/PhD_local/grok-bot-skills/ck-delegate/SKILL.md` §4: "You may run up to 2 at a time per workspace, and an edit
  run locks its workspace" is replaced by the new wording. It also notes that shared folders are never claimed (use a
  narrower subfolder) and mentions `ck-run active`.
- The 4 prompt skills (`claude-code-prompt`, `codex-prompt`, `cursor-agent-prompt`, `antigravity-prompt`), 3 places each:
  - a parallel/claims line in step 5;
  - "6 busy (file/folder claim conflict)" in the exit-code list;
  - step 9 "Busy (6) = a file/folder claim conflict with a live run (stderr names it): wait for that run's notice,
    or use other paths", replacing "wait for the running job's notice, then retry".
- OneDrive `Documents/PhD/AI_Agent_Framework/`: `AI_Agent_Delegation_Framework_v3.md` and `framework_v3_onepager.pdf`
  are replaced with the regenerated versions from `ck-dispatch/docs/framework_v3`.
  - Before copying I checked the OneDrive md. It equalled an older repo version (before `3f80233`) and had no text of
    its own.
  - Backups of both old files: `/private/tmp/claude-501/-Users-ckasemtantikul-PhD-local-ck-dispatch/397d31c1-c299-4d5f-96b3-3799dc075b54/scratchpad/onedrive-backup/`.
  - The PDF was regenerated with headless Chrome. It is 1 page; I rendered it and checked it by eye. The repo copy
    stays untracked, as before.
  - `AI_Agent_Delegation_Framework_v2.md` is untouched (historical).
- I re-checked with grep: none of these files says "max 2 runs per workspace", "2 at a time" or "locks its/the
  workspace" any more. The v3 md still mentions "at most 2 runs" twice, both times saying it is gone.
- Not touched: raw data, credentials, and `grok-bot-agents/.../run-agent.sh` (a different runner; see
  concurrency_summary.md).

## Tests

**143/143 pass** (`python3 -I tests/run_all.py`, 184 s, run on the worktree before the merge). That is the 134
existing tests, unchanged and all passing, plus 9 new ones in `tests/test_concurrency.py`.

`TestAutoClaims` (7; real ck-run processes started together):
- **Same folder:** two edit runs with the same `--write-scope`. The second gets busy 6, codex never starts and no run
  folder is left. The folder is free again once the first run ends.
- **Overlap:** each of these is refused while another run holds the folder:
  - a scope around the folder, or inside it;
  - an auto run with the same explicit scope;
  - a plain `--claim` inside it.
- **Different folders:** edit runs in different folders all start and overlap in time. That covers two workspace
  folders plus a `~/PhD_local` repo.
- **Shared areas are never claimed**, so two edit runs on the same one both run, with the reason in `cmd.txt`.
  Covered: devfab `Design Library/` and `Design Library/questions/`; nv `NV Project/`, `NV Project/wiki/` and `AQS/`;
  washu `theory II WashU/solutions/`.
- **Auto runs claim only an explicit `--write-scope`.**
  - An auto run on its default area claims nothing and is never blocked, but it is told about the other run's claim.
  - washu and nv auto runs get their default folders as scopes but claim nothing.
- **Explicit `--claim` next to automatic claims:** a claim inside the auto-claimed folder is covered; a claim in a
  shared scope still blocks.
- **Visibility:** `--dry-run` shows the claims and registers nothing; `ck-run active` shows `claims_auto`.

`TestSharedAreaUnit` (2): `shared_area()` for 11 shared and 6 narrow paths, and `from_scopes()` for each mode
(edit, auto with and without an explicit scope, read, review) plus `merge()`.

## Real proof (live CLIs, real config, `~/PhD_local/agent_runs`, 2026-11-03 19:17 CT)

Script: `/private/tmp/claude-501/-Users-ckasemtantikul-PhD-local-ck-dispatch/397d31c1-c299-4d5f-96b3-3799dc075b54/scratchpad/smoke_autoclaim.py`.
Report: `/private/tmp/claude-501/-Users-ckasemtantikul-PhD-local-ck-dispatch/397d31c1-c299-4d5f-96b3-3799dc075b54/scratchpad/smoke_autoclaim_report.json`.
Every run was in **devfab**, headless, with the prompt "read the two wiki entry files, answer hello, change no file".
The code was main at `e4af426`.

**A. Two concurrent auto runs, different agents, same workspace, no overlap: both ok.**

| run | agent | --write-scope (claimed automatically) | started | finished | status |
|---|---|---|---|---|---|
| 2026-11-03T191703-devfab-quick_lookup-claude-9df4 | claude | `ck-smoke-scratch/autoclaim-a` | 19:17:03 | 19:17:27 | ok (exit 0) |
| 2026-11-03T191703-devfab-quick_lookup-codex-b73f | codex | `ck-smoke-scratch/autoclaim-b` | 19:17:03 | 19:17:29 | ok (exit 0) |

- Both registered 0.11 s after launch and **overlapped for 24 s**.
- While both were working, `ck-run active` listed both with their claims.
- Each `rulecheck.json` lists the other run as concurrent, with no violations or flags. Neither changed a file.
- codex's `cmd.txt` shows "other live runs claim: …/autoclaim-a".

**B. Two edit runs with the same `--write-scope`: the second is refused.**

- First run, `2026-11-03T191730-devfab-quick_lookup-codex-8307`: codex, edit, `--write-scope ck-smoke-scratch/autoclaim-edit`.
  It was claimed automatically. The run took 19:17:30 to 19:17:49 and ended ok (exit 0), with no files changed.
- Second run, `…cursor-65d1`: cursor, edit, same `--write-scope`, started while the first was live.
  - It was **refused in 0.08 s with exit 6**: `ck-run: BUSY: file claim conflict: claim …/autoclaim-edit (its
    --write-scope, claimed automatically) overlaps …/autoclaim-edit, claimed by live run …codex-8307, codex, mode
    edit, pid 83641 …; nothing was started.`
  - Its only ledger line is `busy` (claims = the folder). No agent started and no run folder was left.
- The `--approved` text for both edit runs quoted chalach's 19:02 answer and this proof item.
- Afterwards `ck-run active` was empty. `ck-smoke-scratch` has 3 new empty folders (`autoclaim-a`, `-b`, `-edit`).

## Notes

- **A trade-off of the rule:** two edit runs on the same shared folder are still not refused, because claiming that
  folder would serialize the workspace. An example is `--write-scope "Design Library"`. Those runs are still checked
  after the run (a flag when both report the same file). To be refused up front, give a narrower `--write-scope`
  or `--claim`.
- **No email in `Co-Authored-By`:** the rulebook-guard hook blocks emails in commit messages, so I followed the
  earlier commits.
- **Untracked files left alone:** the earlier `*_summary.md` files, `.tmp_find_opt.py`, `config/workspaces.json.bak`,
  `docs/framework_v3/summary.md`, the PDF, and grok-bot-skills `step7_summary.md`.

## Questions for chalach

1. **Shared folders are never claimed automatically.** These are wiki folders, nv `NV Project/` and `AQS/`, and
   washu `solutions/`. Two edit runs there still both start, and an overlap is only flagged after the run. Is that
   what you want? The alternative is to claim them too, which would serialize all edit runs in that area.
2. **Push:** none of the commits are pushed. They are ck-dispatch `f30d308` and `e4af426`, and grok-bot-skills
   `ff22b9d`. Do you want them pushed?
