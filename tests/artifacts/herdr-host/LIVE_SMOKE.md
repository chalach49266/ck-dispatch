# CK live smoke (not executed by this worker)

Use the worktree binary below. No installation, merge or push is required.
Run the Herdr block from a main agent terminal in Herdr with HC_TERMINAL_ID set.

```sh
CK_HOST_BIN="$HOME/PhD_local/ck-dispatch-herdr/bin/ck-run"
CK_HOST_SMOKE="herdr-host-smoke-$(date +%Y%m%dT%H%M%S)"
CK_HOST_PROMPT="$HOME/PhD_local/agent_runs/${CK_HOST_SMOKE}_prompt.md"
cat > "$CK_HOST_PROMPT" <<'TASK'
Read only. Follow the required wiki-first reads and the output contract in your header.
Report that this is a terminal-host smoke check. Read HC_TERMINAL_ID only (do not dump the environment).
List that terminal identity in report_md. Change only your result.md and summary.md in the assigned outbox.
Do not launch other agents. Finish with a valid result JSON and write summary.md last.
TASK
CK_HOST_ASK="$("$CK_HOST_BIN" ask open --size M --title 'Terminal host smoke' --words 'Test ck-run live runs following the host terminal' | head -n 1)"
test -n "${HC_TERMINAL_ID:-}" && env -u CK_RUN_HOST "$CK_HOST_BIN" launch "$CK_HOST_SMOKE" run \
  --model sol --workspace devfab --mode read --ask "$CK_HOST_ASK" \
  --title 'Herdr host smoke' --prompt-file "$CK_HOST_PROMPT"
```

Check that the child is in the main agent's Herdr tab, main left and child right.
After completion, check the green circle and retained pane. Launch another child
with a fresh slug to inspect equal-height stacking while other panes stay present:

```sh
CK_HOST_SECOND="${CK_HOST_SMOKE}-stack"
env -u CK_RUN_HOST "$CK_HOST_BIN" launch "$CK_HOST_SECOND" run \
  --model sol --workspace devfab --mode read --ask "$CK_HOST_ASK" \
  --title 'Herdr stacking smoke' --prompt-file "$CK_HOST_PROMPT"
herdr-app ls
```

For the unchanged iTerm path, run the following in iTerm. A separate ask keeps
these smoke runs within their own budget. HC_TERMINAL_ID is explicitly absent.

```sh
CK_HOST_BIN="$HOME/PhD_local/ck-dispatch-herdr/bin/ck-run"
CK_HOST_SMOKE="iterm-host-smoke-$(date +%Y%m%dT%H%M%S)"
CK_HOST_PROMPT="$HOME/PhD_local/agent_runs/${CK_HOST_SMOKE}_prompt.md"
cat > "$CK_HOST_PROMPT" <<'TASK'
Read only. Follow the required wiki-first reads and the output contract in your header.
Report that this is an iTerm host smoke check. Change only result.md and summary.md in the assigned outbox.
Do not launch other agents. Finish with valid result JSON and write summary.md last.
TASK
CK_HOST_ASK="$("$CK_HOST_BIN" ask open --size M --title 'iTerm host smoke' --words 'Test the unchanged iTerm live path' | head -n 1)"
env -u HC_TERMINAL_ID -u CK_RUN_HOST "$CK_HOST_BIN" launch "$CK_HOST_SMOKE" run \
  --model sol --workspace devfab --mode read --ask "$CK_HOST_ASK" \
  --title 'iTerm host smoke' --prompt-file "$CK_HOST_PROMPT"
```

Check the usual iTerm ask window/run tab. Inspect the generated shell scripts:
they record `export CK_RUN_HOST=herdr:<original-main>` or the configured terminal.
Do not rerun a successful smoke just to inspect the script: rerunning a launch
script starts new work and consumes the ask budget.

Yellow/red states, watchdog resume targeting, concurrency and conflict retries
were checked only with fixtures in this worker. Their real UI behavior remains
UNVERIFIED until CK chooses to exercise those situations.
