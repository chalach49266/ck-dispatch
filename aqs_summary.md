Commit 01a7f01 (local only, not pushed): nv AQS/ is writable (chalach, 2026-11-03: "Keep AQS writable from the NV workspace").
Tests: 109/109 pass (python3 -I tests/run_all.py, 74 s).
Changed: config (AQS moved from nv raw_readonly to writable_only), 2 tests (--write-scope AQS accepted; nv auto scopes = NV Project + AQS), README, docs/framework_v3 md + one-pager html (PDF regenerated, 1 page, untracked); the cursor/agy post-run review still flags AQS/ changes (not changed).
