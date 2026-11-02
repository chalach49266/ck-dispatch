STEP-3 PROBE (read-only). Do not call any tools: do not open, read, list or search files, do not run commands, do not edit anything. Answer only from what is already in your context.

Question: which project instruction or rule files (for example CLAUDE.md, AGENTS.md, GEMINI.md, .cursor/rules/*, .agents/*) were loaded into your context automatically at startup? Give each path as you saw it, plus the first heading line of each so the loading can be verified. If none were loaded, say none.

Reply starting with OK. If your CLI enforces a JSON result schema, put the answer in "summary" (1 to 4 lines) and "report_md", use "run_id": "step3-probe", "status": "ok", and [] or "" for every other field.
