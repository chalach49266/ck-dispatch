# PROGRESS, webunblock task (2026-11-05): built-in web search / fetch allowed for every agent run

chalach ~18:55 CT via Grok Bot (verbatim): "Why are you setting all the bots agent deployment web search
restriction. I want to clear the restriction. What are other restriction right now". Cause: claude-guard.base.json
denied WebFetch + WebSearch (step 3), so claude runs 141724-c82c / 182052-9b59 could not read datasheets/docs.
Worktree scratchpad wt-webunblock, branch webunblock, ff main. Local commit only, no push.

- [x] claude: WebFetch/WebSearch out of deny, into a new base "allow"; write_claude_settings writes allow + deny
      (headless, live, auto, post-run review). No --tools / --disallowedTools / --restricted in any argv;
      --strict-mcp-config is MCP only (claude 2.1.318 --help)
- [x] codex: -c web_search="live" in every argv (read/edit/auto/auto_readonly, headless + live) = --search;
      guards.ALLOWED_CONFIG_OVERRIDES admits that exact value. Key/values checked with `codex debug prompt-input
      -c web_search="bogus"` (codex-cli 0.166.0). Old default UNVERIFIED (`codex exec --help` blocked by merge-gate)
- [x] cursor / agy: not blocked by ck-run (no web flag in agent/agy --help; agy ck-run sessions list search_web).
      cursor's own ~/.cursor/cli-config.json has autoAcceptWebSearch=false (outside write scope, not changed)
- [x] rulecheck.WEB_TOOLS: web query/URL patterns are notes, never violation/flag/review hard fact; review
      evidence/prompt/review.md say so; prompt.WEB_RULE; cmd.txt / --dry-run "# web" line
- [x] kept: curl, wget, scp, rsync, ssh, gh, wrangler, mail, credential reads, codex network_access=false, cloud sims,
      bypass
- [x] tests/test_web.py (17); full suite 194/194 (222 s) in the worktree; 81 (web/live/units/flagfix) on merged main
- [x] README (Web search section + rule check, review, refusals, tests), framework v3 md row 3
- [x] commit f7e653b, ff main 18:50:52 CT; live.py colour diff byte-identical, untracked files untouched; no live runs
      before/after (`ck-run active` empty). Worktree + branch removed
- [x] live check: claude + codex `--dry-run` (no run folder made); generated claude settings allow WebSearch,
      WebFetch, deny neither, keep curl/wget/scp/rsync/ssh/gh/wrangler/mail
- [x] webunblock_summary.md
- Open (needs chalach): the guard still denies Bash(git push:*), Bash(pip install:*), Bash(pip3 install:*),
  Bash(uv pip install:*) (contradicts 2026-11-04); cursor autoAcceptWebSearch; push?; the OneDrive framework v3 copy

# PROGRESS, flagfix task (2026-11-04)

chalach ~23:00 CT via the Grok Bot stand-in (verbatim): "yes you can change the ck-run to treat a word only metion
and note and you can always allow those git push and pip install". False positives that day: 160052-claude-a020
and 170226-cursor-c61a ("git push" in a Questions section), 224256-claude-0d30 ("pip install nvmw[mcp]" in a doc).
Worktree scratchpad wt-flagfix, branch flagfix, ff main. Local commit only, no remote.

- [x] rulecheck: output-text mentions (no tool log) are notes "<cat> mentioned in output text only (not a
      command)", except TEXT_FLAG_CATEGORIES = credential, cloud_sim, permission_bypass (still flags, exit 4:
      the Grok Bot stand-in's safety carve-out). runner matches rulecheck.TEXT_FLAG (unchanged wording)
- [x] rulecheck.ALLOWED_ACTIONS (named constant citing chalach): git push; pip/pip3/python -m pip/uv pip install.
      In agent actions -> note "allowed by chalach 2026-11-04: <excerpt>", never violation/flag. Every other
      push_send / heavy_install pattern unchanged, also in the same command as an allowed one
- [x] result.md Rule check lists rulecheck notes (max 30); rulecheck.json notes as before
- [x] postreview: git_push + new pip_install keyword hits marked allowed, never hard facts; evidence, prompt
      ("Allowed, do not flag: ...") and review.md state chalach's rule; publish/deploy, uploads, deletions,
      credentials, paid cloud, raw/outside writes unchanged. prompt.py's "are flagged" list says deploys, not git push
- [x] tests/test_flagfix.py (15); test_runs/test_postreview/test_sessions updated (session decoy runs an upload
      instead of git push so the old-bug tests still fail on the old code)
- [x] README (Rule check, Post-run review step 3, Tests: 177 tests)
- [x] full suite 177/177 (162 + 15) in the worktree, 219 s; commit d4c7f44 on flagfix, ff main 23:32:04 CT. No
      stash needed (the commit does not touch live.py): colour diff + iterm_colors.json byte-identical after, still
      uncommitted; untracked files untouched. test_flagfix + test_live + test_sessions green on merged main (54).
      Worktree + branch removed. Live runs: 07a1 (claude, 23:02) started before the ff and finished; 8ce9 (codex)
      started 23:32:46, after the ff (new code). No ck-run run started by this task
- [x] ckflagfix_summary.md
- Not changed (needs chalach): Claude deny rules (config/claude-guard.base.json denies Bash(git push:*),
  Bash(pip install:*), Bash(pip3 install:*)) and the agent prompt HARD RULES ("no git push ... pip/npm install")

# PROGRESS, sessionfix task (2026-11-04): each run gets its own agent session

chalach ~16:00 CT via the Grok Bot stand-in: "you can clear the flagged run, and can you also fix the ck-run bug".
Bug: cursor runs 154810-…-167b and 154811-…-47fc (1 s apart, devfab) both got session ab017df6 (47fc's): the newer
prompt's CONCURRENCY line names 167b, and find_session took the newest session holding the run id anywhere. 167b's
post-run review read 47fc's tool calls and flagged it (exit 4). Worktree scratchpad wt-sessionfix, branch
sessionfix, ff main. Local commit only, no push.

- [x] live.find_session: session = the newest new one whose FIRST "RUN: <run_id> | agent: <agent>" line is this
      run's (JSONL in file order; agy via SQLite steps ORDER BY idx, raw bytes if SQLite can't read it). Fallbacks
      only if none: quote of the line / bare mention / only new session -> sure=False, how says "may be wrong".
      Headless: the session id the CLI printed wins (cursor). Returns 'sure'.
- [x] recover_result: only lines the agent wrote (no prompt/tool output); runner recovers only from a sure session
- [x] postreview.session_info + run(session_how, session_sure): evidence, review-actions.txt, review prompt and
      review.md say when the session is unconfirmed; its tool-call hits are hints, not hard facts; actions=0 so
      text-only flags are not cleared by it; result.json post_review.session {path, how, sure}
- [x] tests/test_sessions.py (12): the 167b/47fc case for cursor/codex/agy, quote, mention fallback, CLI id, agy raw,
      recover filter, review evidence, end-to-end live + headless with a newer decoy session (fake_cli
      decoy_session). The e2e tests fail on the old code (exit 4 / decoy id). Full suite 162/162 (150 + 12)
- [x] real stores, read-only: 167b -> 8aa09e37 (sure), 47fc -> ab017df6 (sure). 167b's own 42 tool calls: no
      keyword hits; only `git commit` (allowed by its task.md), no push
- [x] README (Live mode: Session id; Post-run review step 1; tests list)
- [x] commit d4c0edb on sessionfix, ff main 16:49 CT. git refused (dirty live.py): tagged stash sessionfix-9d9e65
      (ce5d1f0), ff, `stash apply <sha>`, colour hunks byte-identical, entry dropped. Colour change still
      uncommitted, untracked files untouched. test_sessions + test_live green on merged main. Worktree + branch
      removed. Live runs: a020 (claude) finished; 96c2 (claude) started 16:13, before the ff
- [x] sessionfix_summary.md
- Open: clearing 167b's flag is CK's call (ck-run has no clear command; its run folder was not edited)

Prompt: ~/PhD_local/agent_runs/washuauto_prompt.md (chalach 19:34 CT: "Change ck-run so auto mode in WashU runs
read-only when no writable folder exists"). Local commit only, no push. Worktree scratchpad wt-washuauto, branch
washuauto, ff main when tests pass.

- [x] runner: auto_readonly_reason() / auto_readonly_note(); execute() downgrades instead of refusing; Spec.check_mode
      ("read" for the rule check, claims, attribution); stderr NOTE; ledger key auto_readonly (ledger.KEYS), dispatch +
      result notes; rulecheck.json, packet.json (schema), result.json, cmd.txt, `ck-run active`
- [x] flags: codex auto readonly = -C <ws> -s read-only + the same auto_review approvals (headless + live); invariants
- [x] claude: whole-workspace deny rules (auto_ws=None); cursor/agy argv unchanged + review knows task folder = none
- [x] prompt: READ-ONLY for this run, do not create the writable folders, no PROGRESS.md
- [x] rulecheck: violation for any workspace change and for a writable folder that now exists; claims: a readonly run
      is never a concurrent writer
- [x] tests: TestWashuAutoReadOnly (7), 2 washu tests updated, test_overlapping_claims_are_refused race fixed; 150/150
- [x] README + framework v3 md; commit 09c524c on main (ff, no live runs before/after)
- [x] real proof 23:20 CT: claude-7346, codex-4c7c, cursor-2543, agy-5b0d together in washu, auto default, --live:
      all exit 0, status ok (58-180 s), cursor/agy reviews ok, no busy/refused/fallback, no permission stops (codex's
      outbox write approved by auto_review); WashU HW tree identical before/after; no solutions/ or prep/ created
- [x] washuauto_summary.md; worktree + branch removed
- Open (needs chalach): push?; agents found "six courses" wording in protected rule files (course map says seven)

# PROGRESS, autoclaim task (2026-11-03): edit runs claim their write folders automatically

Prompt: ~/PhD_local/agent_runs/autoclaim_prompt.md (chalach 19:02 CT: "Yes, have edit runs claim their write folders
automatically"). Local commit only, no push. Code work in a git worktree (scratchpad wt-autoclaim, branch autoclaim)
so live bot runs never see half an edit; fast-forward main when tests pass.

- [x] claims.py: from_scopes() (edit runs, and auto runs with an explicit --write-scope), shared_area() (never a
      workspace root, writable_only folder, folder holding a wiki entry file, ~/PhD_local, or a folder around one),
      merge(); runner: claims = auto + --claim, record claims_auto, busy note says "claimed automatically",
      cmd.txt lists auto / not-auto scopes; cli help + `ck-run active` claims_auto; prompt line; ledger doc
- [x] tests: TestAutoClaims (7) + TestSharedAreaUnit (2) in tests/test_concurrency.py
- [x] README (Concurrent runs, exit 6, ledger, tests), docs/framework_v3 md + one-pager html; PDF regenerated (1 page)
- [x] grok-bot-skills: ck-delegate + 4 prompt skills
- [x] full suite 143/143 on the worktree; commit f30d308; ff main (no live runs); worktree + branch removed
- [x] e4af426: v3 md "max 2 runs per workspace is now a lock" -> runs go in parallel
- [x] OneDrive v3 md + PDF replaced (old ones backed up in the scratchpad onedrive-backup/); v2 untouched
- [x] grok-bot-skills ff22b9d (local, not pushed)
- [x] real proof 19:17 CT: A claude-9df4 + codex-b73f auto, own scopes, both ok, overlapped 24 s;
      B codex-8307 edit ok, cursor-65d1 same scope refused in 0.08 s (exit 6, busy, no folder)
- [x] autoclaim_summary.md
- Open (needs chalach): shared folders (wiki, NV Project/, AQS/, washu solutions/) are never auto-claimed; push?

# PROGRESS, concurrency task (2026-11-03): parallel runs, per-file claims, no workspace lock

Prompt: ~/PhD_local/agent_runs/concurrency_prompt.md. Local commit only, no push, no credentials.

- [x] Old constraint found: runner.SlotLock (flock slots agent_runs/.locks/<ws>.slot<i>.lock, max_runs_per_workspace=2;
      edit and auto took every slot -> event busy, exit 6). An uncommitted interim hack (auto non-exclusive, cap 4) was
      in the tree at 18:22; superseded and folded in.
- [x] ckdispatch/claims.py: run registry (.claims/<run_id>.json, flock-held), --claim conflicts (exit 6 busy), stale
      cleanup (+ ledger `stale`), change attribution after the run, same-file flags; `ck-run active`
- [x] runner: SlotLock removed; run folder reserved with mkdir (unique id); prompt CONCURRENCY line; Claude deny rules
      for other runs' claims; agent pid in the record; postreview gets the attribution
- [x] ledger `claims` key + LOCK_SH reads; state.json under a lock with per-process temp file; config key removed
- [x] tests/test_concurrency.py (21 new); full suite 134/134
- [x] docs: README (Concurrent runs), framework_v3 md + one-pager html + PDF (1 page, untracked), architecture.mmd
- [x] real smoke 18:47: claude-accb + codex-5848 in devfab auto, both ok, overlapped 20 s; same-claim codex refused
      in 0.11 s (exit 6). Bot Phase3 runs (claude-e46f, codex-d4d2, cursor-f67b) also ran together in devfab.
- [x] commit cf64c2c (local, not pushed; no email in Co-Authored-By: rulebook-guard hook); concurrency_summary.md
- Open (needs chalach): update grok-bot-skills ck-delegate + 4 prompt skills and the OneDrive framework copy
  (listed in concurrency_summary.md, not edited); whether edit mode should auto-claim its write scope.

# PROGRESS, AQS review task (2026-11-03): the cursor/agy review stops flagging AQS/ in nv

Prompt: ~/PhD_local/agent_runs/aqsreview_prompt.md (chalach 15:07 CT: "yes"). Local commit only, no push.

- [x] postreview.py: WRITABLE_SEGMENTS = {"nv": {"AQS"}}; _is_raw drops AQS/ for nv only; the evidence file,
      Claude prompt ("Expected, do not flag by itself: AQS/ is writable in the nv workspace") and review.md
      say so. Kept: AQS/ raw in every other workspace; nv AQS/ outside the task folder = outside_task_folder;
      AQS/.../raw/ = raw_data; AQS/ deletion = deletion; nv raw list; risky commands
- [x] tests: unit (_is_raw per key) + TestNvAqsReview (4 tests; the nv ones fail on the old code)
- [x] README + docs/framework_v3 (md, one-pager html; AQS open item closed; PDF regenerated, 1 page, untracked;
      summary.md (untracked) got an update line)
- [x] full suite 113/113; commit 1914397 (local, not pushed); aqsreview_summary.md

# PROGRESS, AQS task (2026-11-03): keep nv AQS writable

Prompt: ~/PhD_local/agent_runs/aqs_prompt.md (chalach 14:48 CT: "Keep AQS writable from the NV workspace").
Local commit only, no push.

- [x] keep config/workspaces.json change (AQS off nv raw_readonly, into writable_only)
- [x] NV rule files already agree (11:42 today): CLAUDE.md:15 and NV Project/CLAUDE.md:99 "AQS/ is writable by all agents"
- [x] tests: test_runs edit-scope rules (AQS accepted; nv raw refusal now nv_magnetometer_project), test_washu default nv auto scopes
- [x] README, docs/framework_v3 (md + one-pager html; PDF regenerated, 1 page, untracked); rulecheck.py comment
- [x] full suite 109/109; commit 01a7f01 (local, not pushed); aqs_summary.md
- Not changed (needs chalach): postreview.py RAW_SEGMENTS still flags any AQS/ change in cursor/agy auto runs.

# PROGRESS, combo task (2026-11-03): Part A live fix, Part B cursor/agy allow + Claude review

Prompt: ~/PhD_local/agent_runs/combo_prompt.md. Local commits only, never push, never read credentials.

## Part A: live-window fix (commits 2e81963 + f1cdacc), DONE
- [x] Root cause: ck-run activated iTerm2 and typed `/bin/bash <run>/live.sh` (`write text`); the new window
      took focus and chalach's keystrokes landed in front ("is /bin/bash ..." -> zsh: command not found: is).
- [x] Fix: iTerm2 runs live.sh as the session command, no activate; CK title + per-agent dynamic profiles;
      ledger live_window_opened / live_fallback; f1cdacc fixed the window-id parse (`tab` keyword in AppleScript)
- [x] Real smoke: 4 windows ok, names confirmed with osascript; recheck shows window_id + title locked: yes
- [x] livefix_summary.md

## Part B: cursor allow-all + agy auto + mandatory Claude review (3f80233, cae28e3, 3520c06), DONE
- [x] flags/guards: cursor auto --force (only token, only cursor auto, ck-run-built)
- [x] agy auto (chalach addendum): --mode auto + --add-dir <workspace root> + --add-dir <run folder>, headless
      and live; never --dangerously-skip-permissions. Interactive auto works ("edits & safe commands
      auto-approved"); print mode falls back to default mode (warning = note for agy auto)
- [x] postreview.py + schema/review.schema.json; ledger cursor_review / agy_review; loud warning; status flagged
- [x] 3520c06: reviewer told the agy fallback is expected; tool-log-checked text mentions become notes
- [x] tests: 109/109 on a clean checkout of 3520c06
- [x] README + docs/framework_v3 (md + one-pager html + regenerated 1-page PDF, untracked)
- [x] real smoke: cursor headless 131908-ce79 ok (review ok 25.6 s), cursor live 132034-98ea ok,
      agy live new flags 133853-07b4 ok in 107 s (old flags: 398 s waiting on prompts), agy headless
      133612-6dcb flagged (fallback + rule restating) -> fixed in 3520c06
- [x] agy headless recheck on 3520c06: 134833-0f2b ok, review ok 28.8 s (30 tool calls)
- [x] cursorallow_summary.md

## Open
- (resolved in 01a7f01) nv AQS writable; the review still treats AQS/ as raw (see AQS task above).
- ~/PhD_local/ck-smoke-scratch was created for the smoke runs (own git repo); safe to delete.
- iTerm2 dynamic profiles written by ck-run: ~/Library/Application Support/iTerm2/DynamicProfiles/ck-run-live.json.
- docs/framework_v3/framework_v3_onepager.pdf regenerated (1 page) but untracked, like summary.md there.


# Progress

Task: host-aware live runs in the authorized herdr-host worktree.

Done:
- Read all required guides and inputs; no missing guide. Initial branch was clean.
- Implemented host selection and export in launch scripts, stdlib hc transport,
  locked version-checked layout, CK-space/iTerm fallbacks, saved exact terminal,
  watchdog reads/resume, retained panes, and final state labels after rule checks.
- Added fake hc/herdr-app tests for host/layout/conflicts/fallbacks/status/reattach.
- Isolated Herdr dry-run completed; tests/artifacts/herdr-host/herdr-dry-run.log shows the plan and exit success.
  Production-config dry-run could not acquire the shared registry lock within this
  run's write scope; its diagnostic is tests/artifacts/herdr-host/herdr-dry-run-production-denied.log.

Verification complete:
- Full suite: 420 tests passed (tests/artifacts/herdr-host/full-suite-final.log:3-5).
- Focused Herdr suite: 21 tests passed (tests/artifacts/herdr-host/herdr-tests.log:30-32).
- Original live regression suite and corrected launch/reattach/policy fixtures pass.
- Diff check clean. Local commit and outbox handoff prepared at completion; use
  `git log -1 --oneline` for the commit and the run folder's result.md for the finish record.

Remaining for CK:
- Run the real smoke commands in tests/artifacts/herdr-host/LIVE_SMOKE.md.
- Real Herdr visual behavior remains UNVERIFIED; deliberately not exercised here.

Verification: fake_or_tests only. No real Herdr terminal or agent started.

Existing policy provenance needed by the unchanged policy regression:
- The worktree had no PROGRESS.md. The production repository retains this quote at
  /Users/ckasemtantikul/PhD_local/ck-dispatch/PROGRESS.md:32-33:
  "yes you can change the ck-run to treat a word only metion and note and you can always allow those git push and pip install".
- This is historical test provenance only. This task still forbids pushing or installing.
- Historical web-search quote, same source file at lines 3-4:
  "Why are you setting all the bots agent deployment web search restriction. I want to clear the restriction. What are other restriction right now".
