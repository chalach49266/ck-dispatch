# washuauto: auto mode with no writable folder runs read-only (2026-11-03)

chalach (19:34 CT): "Change ck-run so auto mode in WashU runs read-only when no writable folder exists".
Commit **09c524c** on `main` (local, not pushed). 150/150 tests (143 old + 7 new).

## What changed

Before, auto mode without `--write-scope` in washu exited 2 ("none of the writable folders of WashU HW exists
yet") while no course had `solutions/` or `prep/`. Now that case starts, read-only:

- **The rule is generic.** It applies to any workspace with a `writable_only` list (washu, nv) where none of those
  folders exists. `runner.auto_readonly_reason()` returns `"no writable folder"`.
- **Each agent keeps its own auto mode; only the write area is removed:**
  - claude: `--permission-mode auto` with the whole-workspace deny rules. In a live run only its run folder stays
    open.
  - codex: `-C <ws> -s read-only`, with the same `approval_policy="on-request"` and
    `approvals_reviewer="auto_review"`.
  - cursor and agy: `--force` and `--mode auto` are unchanged, and the mandatory post-run Claude review still runs.
    The review is told there is no task folder, so any write in the workspace counts as outside it.
- **The downgrade is reported in several places:**
  - stderr: `ck-run: NOTE (<agent>): auto_readonly: no writable folder. None of the writable folders of WashU HW
    exists yet (...)`.
  - ledger: a new key `auto_readonly` on every event of the run. The dispatch and result notes say it too.
  - `rulecheck.json`: `auto_readonly` plus a note.
  - `packet.json` (schema updated), `result.json`, `cmd.txt` and `ck-run active` also carry it.
  - `mode` stays `auto`.
- **Prompt:** says READ-ONLY for this run, do not create the writable folders, and do not write `PROGRESS.md`.
  Needed changes go in `proposed_changes`.
- **Rule check:** the run is treated as a read run.
  - Any file change in the workspace is a violation (exit 5).
  - A writable folder that exists after the run is also a violation (none existed at the start).
  - Concurrent runs never count this run as a writer.
- **What stays the same:** ck-run creates no folder. `--claim` is refused (nothing to claim). An explicit
  `--write-scope` that does not exist is still refused. Once one writable folder exists, auto runs write there as
  before. `default_auto_scopes()` keeps its refusal as a backstop.
- **Tests:** `TestWashuAutoReadOnly` (7 new tests) and 2 washu tests updated. A timing race in
  `test_overlapping_claims_are_refused` also failed on unchanged `main` (3 of 4 runs). I fixed it by having the
  test wait for claude's call record.
- **Docs:** README (Auto mode, WashU HW, ledger, tests) and the framework v3 md. The one-pager does not mention
  washu, so I left it and the PDF alone. The grok-bot-skills files don't mention washu or the old refusal, so they
  need no change.

## Proof (real agents, 23:20 CT)

claude, codex, cursor and agy were started together in washu: default auto mode (no `--mode`), `--live`, prompt
"list the course folders, reply hello, change nothing". Run ids are `2026-11-03T232023-washu-quick_lookup-*`.

| agent | exit | status | duration | review |
|---|---|---|---|---|
| codex (4c7c) | 0 | ok | 58 s | n/a |
| agy (5b0d) | 0 | ok | 85 s | agy_review ok |
| claude (7346) | 0 | ok | 110 s | n/a |
| cursor (2543) | 0 | ok | 180 s | cursor_review ok |

- All four dispatched in the same second and ran at the same time, each in its own iTerm2 window.
- No exit 2, no busy, no refused, no live_fallback. Ledger `mode` auto, `auto_readonly` "no writable folder".
  Rule check: 0 violations, 0 flags.
- No permission stops; nobody touched the windows, and each run finished on its own in 58–180 s.
  - Claude's session has no tool errors. Its only writes were the run folder's `result.md` and `summary.md`.
  - Codex's sandbox was `read-only` with reviewer `auto_review`. Its one write request ("writing result.md and
    summary.md in the specified run outbox") was approved by auto review with no user prompt.
- The WashU HW tree (1,903 entries; names, mtimes and sizes) is identical before and after. No `solutions/` or
  `prep/` folder exists.
- The four iTerm2 windows stay open, as live runs always do.

## Questions for chalach

1. Push 09c524c? (Not pushed.)
2. claude and cursor both noticed that `CLAUDE.md:3`, `HW Brain/CLAUDE.md` and `HW Brain/index.md:20` say six
   courses, while the course map lists seven. Those are protected rule files, so the agents did not change them.
   Update the wording?
