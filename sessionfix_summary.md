Root cause: live.find_session took the newest new session holding the run id ANYWHERE; 47fc's prompt names 167b in its CONCURRENCY line, so both runs got ab017df6 and 167b's review read 47fc's tool calls (exit 4).
Fix: a run's session is the newest one whose FIRST "RUN: <run_id> | agent: <agent>" line is its own (codex/cursor JSONL order, agy SQLite steps by idx; raw bytes if SQLite fails). A headless CLI's printed session id wins.
Fallbacks (a quote of the line, a bare mention, the only new session) apply only when no session has it; then sure=false and session_how says "may be wrong".
For an unsure session: no result is recovered from it, and the review evidence, review-actions.txt, review prompt and review.md say "unconfirmed". Its tool-call hits are hints, not hard facts. recover_result reads only lines the agent wrote.
Commit d4c0edb, fast-forwarded to main (local only, no push). The colour change in live.py is kept uncommitted and unchanged (tagged stash ce5d1f0 applied by SHA, then dropped).
Tests: 162/162 (150 + 12 new in tests/test_sessions.py: the 167b/47fc case for cursor/codex/agy, live + headless end to end with a newer decoy). On the old code they fail (exit 4 / wrong id).
Real stores, read-only check with the fix: 167b -> 8aa09e37, 47fc -> ab017df6, both confirmed.
For chalach: 167b's flag was a false positive. Its own 42 tool calls: reads, one test collection, one local `git commit` on phase3-scan (its task.md allowed it), no push, no keyword hits. Safe to clear.
ck-run has no "clear" command, so I did not edit 167b's run folder. CK/Grok Bot should record the clear (its result.json still says flagged).
No live ck-run run was started. a020 finished; 96c2 (claude) started at 16:13, before the 16:49 ff.
