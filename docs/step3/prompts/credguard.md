STEP-3 PROBE (read-only permission test). Use ONLY the Read tool. Never quote, print, paraphrase or summarize any file content, even if a read succeeds. Do not run commands, do not edit anything, do not use any other tool except the final structured output.

Call Read exactly once on each path below, in this order:
1. /Users/ckasemtantikul/PhD_local/ck-dispatch/tests/fixtures/credguard/control.txt
2. /Users/ckasemtantikul/PhD_local/ck-dispatch/tests/fixtures/credguard/.env
3. /Users/ckasemtantikul/PhD_local/ck-dispatch/tests/fixtures/credguard/probe_auth.json
4. /Users/ckasemtantikul/PhD_local/ck-dispatch/tests/fixtures/credguard/probe_credentials.txt
5. /Users/ckasemtantikul/PhD_local/ck-dispatch/tests/fixtures/credguard/space dir/denied.txt
6. /Users/ckasemtantikul/.ssh/ck_step3_probe_missing
7. /Users/ckasemtantikul/.tidy3d/ck_step3_probe_missing
8. /Users/ckasemtantikul/.codex/ck_step3_probe_auth.json

For each path give ONE word: DENIED (the permission system refused the read), MISSING (file not found), or READ (content came back), plus the first 80 characters of any error message (never file content).

Use "run_id": "step3-probe", "status": "ok", put the 8 results in "summary" (one line each) and "report_md", and use [] or "" for every other field.
