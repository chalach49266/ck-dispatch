# ck-dispatch step 1 (2026-11-02): `bin/ck-run` built, local git only, no agent CLI run
- Commands: run, review, thread, accept, show, route, limits. Python 3 stdlib; tests 40/40 pass offline (`python3 -I tests/run_all.py`).
- D1/D2/D4 applied: codex, cursor, agy edit only with --approved + --write-scope; claude edits ~/PhD_local repos only (plan).
- Raw folders from each AGENTS.md/CLAUDE.md block any overlapping scope (real path too); NV agents may write only in NV Project/.
- Every run: full logs, result.json/.md, agent-written summary.md (max 10 lines); stdout = summary path + summary only.
- Ledger agent_runs/ledger.jsonl: dispatch/review/result/refused/busy/accept with all asked fields; `thread` prints the chain.
- `review RUN --by X` sends file paths, never content; `--blind` sends only the task. Reviews are read-only, other agent.
- Hard refusals (skip-permissions, bypassPermissions, yolo, --force, auto modes, full-access) on ck-run argv and agent argv.
- Note: AQS/ is raw per NV Project/CLAUDE.md, so no agent can edit it via ck-run unless config/workspaces.json changes.
- Step 3 must confirm [assumed] flags live: agy --json-schema/-p/--mode in print mode, claude structured_output, codex -c key.
