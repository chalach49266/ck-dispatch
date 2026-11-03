# ck-run --live (2026-11-02): each agent in its own interactive CLI in a new Terminal window; default on, --no-live = headless
- claude: --strict-mcp-config --settings <run>/claude-settings-live.json --session-id <uuid> --add-dir <run> [--allowedTools ... (edit)] --permission-mode plan|acceptEdits "<prompt>"
- codex: -C <ws|scope> -s read-only|workspace-write [-c sandbox_workspace_write.network_access=false --add-dir <run>] -a on-request "<prompt>" (your ~/.codex/config.toml says approval never + danger-full-access; these override it)
- cursor (agent): --trust --sandbox enabled [--mode plan] --workspace <ws|scope> --add-dir <run> "<prompt>"
- agy: --sandbox --mode plan|accept-edits --add-dir <run> -i "<prompt>" (--prompt-interactive)
- All 4 accept an initial prompt interactively: none is missing it. No skip-permissions/yolo/force/full-auto; a window failure falls back to headless.
- Catch: in read (plan/read-only) mode each CLI asks you before writing result.md/summary.md; or quit it and ck-run takes the answer from its session log (claude, codex, cursor; not agy).
- 62/62 offline tests pass (osascript mocked, stub CLIs); a real Terminal window and the real TUIs were not tried yet. Committed locally on main, not pushed.
