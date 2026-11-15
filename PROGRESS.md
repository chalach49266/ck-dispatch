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
