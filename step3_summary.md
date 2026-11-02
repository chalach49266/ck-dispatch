# ck-run step 3 (2026-11-02): live read-only checks. Details, models and resume commands: docs/step3/results.md
1 Flags: PASS claude structured_output + plan; agy -p value, inline --json-schema, --mode plan in print mode (log), --sandbox = terminal only, no always-proceed. Cursor --mode plan applied (chat meta). Edit-blocking not tried.
1 FAIL→FIXED: claude --strict-mcp-config keeps plugin MCP 'skins'; now mcp__skins + send/publish/schedule tools denied in config/claude-guard.base.json.
2 Rules: claude=CLAUDE.md (+MEMORY.md); cursor=.cursor/rules/*.mdc+CLAUDE.md+AGENTS.md+PhD/.cursor/rules; agy=AGENTS.md (+.agents/AGENTS.md in devfab). Same in both workspaces. No agy pointer file needed.
3 Claude read-block: FAIL→FIXED. Bare **/.env, **/*auth*.json, **/*credential* were cwd-only (fake fixtures READ); now //**/… all DENIED. ~/.ssh, ~/.tidy3d, abs paths with spaces DENIED. No real credential opened.
4 Models: agy gemini-3.8-flash-high (default), -medium, -low, claude-sonnet-4-6, claude-opus-4-6-thinking, gpt-oss-120b-medium. Cursor: 80+ ids, default auto (list in results.md).
5 Sessions persist: claude f4ff31c3, 661ea7cf, 4ac53497, 7f18d9ce (~/.claude/projects/<ws-slug>/); cursor 599c70d0, 3d3d0941 (~/.cursor/chats/); agy 69994519, 8625ab1b (~/.gemini/antigravity-cli/conversations/). Full ids in results.md.
Also: tests 42/42 pass (2 new); no workspace file changed; flags.py needed no argv change; committed locally, not pushed.
YOUR DECISION: codex not run live. The merge-gate mod denies `codex exec` in Claude Code. Run one codex probe yourself, or allow it, to verify -c network_access and --output-schema.
YOUR DECISION: claude workers load your hooks and MEMORY.md (no --bare, since --bare skips OAuth). Cursor's first run took 199 s, under the 1200 s default timeout.
