# ck-dispatch: `ck-run`

One runner that CK (Grok Bot) calls on the Mac to send a task to one of four agent CLIs:
`claude`, `codex`, `cursor` (binary `agent`) or `agy`. It adds guards, a run folder, a summary of at most
10 lines, an interaction ledger and cross-review. Python 3 standard library only. Local git, no remote.

Status: **step 1 of the plan** (`~/.claude/plans/read-only-planning-task-do-cosmic-emerson.md`) built and
tested offline with stub CLIs. **Step 3** (2026-11-02): live read-only probes of claude, cursor and agy with
ck-run's own argv and env; results, session ids and resume commands in `docs/step3/results.md`. Codex was
not run live (the `merge-gate` mod in Claude Code denies `codex exec`).

## Commands

```
ck-run run    --agent claude|codex|cursor|agy --workspace devfab|nv --title T --prompt-file F
              [--task-type T] [--model M] [--read-dir D]... [--timeout S] [--parent RUN_ID] [--lead L]
              [--mode edit --approved "<user's words + time>" --write-scope FOLDER [--write-scope ...]]
              [--dry-run]
ck-run review RUN_ID --by AGENT [--blind] [--focus TEXT] [--model M] [--timeout S]
ck-run thread RUN_ID [--no-summary]
ck-run accept RUN_ID [--reject] [--by CK|user] [--note TEXT]
ck-run show   RUN_ID
ck-run route  --task-type T [--review-of AGENT]
ck-run limits [--clear [AGENT]]
```

`run` and `review` print **only** the `summary.md` path (line 1) and the summary (max 10 lines) on stdout.
The status is the exit code. When the status is not `ok`, one line also goes to stderr.

| exit | status | meaning |
|---|---|---|
| 0 | ok | result JSON valid, summary present, required reads shown, rulecheck clean |
| 1 | partial | missing/invalid result JSON or summary, wiki-first not shown, agent said partial/blocked, scan cut short |
| 2 | refused | a guard said no (nothing ran), or usage error |
| 3 | failed / timeout | agent error, no result, or killed at `--timeout` (default 1200 s) |
| 4 | flagged | needs a human look (see rule check) |
| 5 | violation | a hard rule was broken (see rule check) |
| 6 | busy | workspace already has 2 runs, or an edit run |

`--task-type` keys for `route`: wiki_synthesis, python_debug, refactor, fdtd_review, microwave_review,
slides_web, writing, quick_lookup, multi_file_diff, wiki_lint, general (Framework v2 §4 plus agy lanes).

## Decisions (user, 2026-11-02)

| | Decision | Where in code |
|---|---|---|
| D1 | Codex may edit inside Device Fabrication / NV Compact Magnetometer after approval; raw folders are blocked | `flags.EDIT_AREAS`, `guards.validate_write_scopes` |
| D2 | agy may edit after approval with `--mode accept-edits`; never auto or skip-permissions | `flags.build` |
| D3 | Framework v3 goes in the real PhD folder later (not in this step) | none |
| D4 | Cursor always gets `--trust`; `--mode plan` unless the edit is approved | `flags.build` |
| (plan) | Claude edits only a `~/PhD_local` repo (its settings deny Edit on both workspaces) | `flags.EDIT_AREAS["claude"]` |

## What each agent gets

| Agent | Read (default) | Edit (only with `--approved` + `--write-scope`) | Prompt |
|---|---|---|---|
| claude | `-p --output-format stream-json --verbose --strict-mcp-config --json-schema <inline> --settings <run>/claude-settings.json --permission-mode plan`, cwd = workspace | `--permission-mode acceptEdits --allowedTools Read,Grep,Glob,Edit,Write,Bash(pytest:*) --add-dir <scope>` | stdin |
| codex | `exec --json -o <run>/last.md --output-schema schema/result.schema.json -C <workspace> -s read-only -` | `-C <scope> -s workspace-write -c sandbox_workspace_write.network_access=false [--add-dir <more scopes>]`, cwd = scope | stdin |
| cursor | `-p --output-format json --trust --sandbox enabled --mode plan --workspace <workspace>` | same without `--mode plan`, `--workspace <scope>`, cwd = scope | last argument |
| agy | `--output-format json --sandbox --print-timeout <S-15>s --json-schema <inline> --mode plan -p <prompt>` | `--mode accept-edits`, cwd = scope | `-p` value |

Every run: the agent gets a clean environment (HOME, USER, LOGNAME, LANG, LC_*, TERM, TMPDIR, SHELL, a fixed
PATH, NO_COLOR; no keys, no tokens). Its process group is killed at the timeout. `cmd.txt` holds the
command without the prompt body and without env values.

## Edit mode: write-scope rules

- Needs `--approved` (the user's words) and at least one existing `--write-scope` folder.
- Allowed: a subfolder of the run's workspace (codex, cursor, agy), or a `~/PhD_local` folder that is its
  own git repo (all four).
- Refused: the home folder or above; the whole workspace root; any folder that is, contains or sits
  inside a raw folder (checked on the literal and the real path, so a symlink cannot get around it);
  `.claude/`, `.cursor/`, `.obsidian/`, `.venv/` (and NV `.agents/`, venvs, `qick-dawg (backup)/`);
  any `.git`, `.venv`, `venv`, `node_modules` path; `~/PhD_local` itself, `ck-dispatch`, `agent_runs`,
  `grok-bot-agents` and the `~/PhD_local` copies of workspace trees; other OneDrive folders; in NV,
  anything outside `NV Project/`.
- Enforcement: codex's OS sandbox lets it write only in `-C <scope>` (network off). Cursor and agy start in
  the scope with their sandbox on. Claude gets deny rules. After every run the rule check looks at the disk.
- An edit run locks its workspace (no other run there at the same time). Read runs: at most 2 per workspace.

## Raw folders (read-only), from the workspace rule files

- **Device Fabrication** (`.agents/AGENTS.md:78-83`, `CLAUDE.md:73-78`): `Projects/CK Design/`,
  `Projects/Microwave Design/`, `Fabrication/`, `Projects/ESEM/`, `Projects/Simulation/`, `Projects/Result/`,
  `Idea Inspiration/`, `Paper/`, `Material/`, `Design Yard/`, `Archive/`, `Photonics_Ecosystem/repos/`,
  `Microwave_Ecosystem/repos/`.
- **NV Compact Magnetometer** (`CLAUDE.md:15`: "the rest of this workspace ... holds the raw, human-owned
  sources"; list in `NV Project/CLAUDE.md`): `nv_magnetometer_project/`, `NV_Magnetometry_Software-main/`,
  `NV_ODMR_FPGA_ARM_PS_20260902/`, `Literature/`, `System_Design/`, `IMU/`, `Patent/`, `AQS/`, `output/`,
  `qick/`, `qick-dawg/`, `PYNQ/`, `Archive/`. Agents may write only in `NV Project/`.
  **Note:** `AQS/` is raw by these rules, so no agent can edit AQS through ck-run. To change that, edit
  `config/workspaces.json`.
- Rule files are protected in both workspaces (a change to them inside a scope is a violation).

## Run folder: `~/PhD_local/agent_runs/<run_id>/` (mode 700)

`task.md` (CK's text) · `prompt.md` (rules header + task + output contract) · `packet.json` (Framework §5.1) ·
`cmd.txt` · `claude-settings.json` (claude) · `stdout.log` / `stderr.log` (full raw output) · `last.md` (codex) ·
`result.json` (parsed result + checks) · `result.md` (readable full result) · `summary.md` (max 10 lines) ·
`rulecheck.json`. Run id: `YYYY-MM-DDTHHMMSS-<ws>-<task_type>-<agent>-<hex4>`.

## Token saving: `summary.md`

The prompt ends with an output contract. The agent returns one JSON object (schema
`schema/result.schema.json` = Framework §5.4 + `summary` as 1-10 lines + `report_md`); claude, codex and agy
get it as a CLI schema, cursor gets it in the prompt. ck-run writes `summary` to `summary.md` as is (cut to
10 lines, 200 characters each). No summary: `summary.md` starts with `[ck-run] The agent returned no summary`
and the status is `partial`.

## Interaction ledger: `~/PhD_local/agent_runs/ledger.jsonl`

One JSON line per event; every line has the same keys: `event` (dispatch, review, result, refused, busy,
accept), `run_id`, `parent_run_id`, `from` (CK or agent), `to`, `via`, `title`, `workspace`, `task_type`,
`agent`, `model`, `mode`, `approved`, `write_scope`, `prompt_path`, `response_path`, `summary_path`,
`files_read`, `files_changed`, `status`, `accepted`, `ts`, `started_at`, `finished_at`, `duration_s`,
`exit_code`, `note`. A review goes from the reviewed agent to the reviewer, via CK. `accepted` is set by
`ck-run accept`. `ck-run thread <run_id>` finds the root of the chain and prints it as markdown, with
reviews and follow-ups (`--parent`) nested and each run's summary quoted.

## Cross-review

`ck-run review <run_id> --by codex` sends the reviewer **file paths** (task.md, result.md, result.json),
never the content, and gives it read access to that run folder (`--add-dir` for claude, cursor and agy; the
codex read-only sandbox can already read). Reviews are read-only, must use a different agent, and mark each
original finding AGREE / DISAGREE / UNSURE plus NEW. `--blind` gives the reviewer only the original task:
no result path, no folder access; CK compares the two answers.

## Hard refusals

Refused in ck-run's own command line (checked before parsing; logged to the ledger as `refused`):
`--dangerously-*`, `bypassPermissions`, `yolo`, `--force`/`-f`, `--auto`, `--full-auto`, `full-access`,
`always-proceed`, `dontAsk`, any `--mode` other than read/edit, and any permission flag
(`--permission-mode`, `--sandbox`/`-s`, `-c`/`--config`, `--settings`, `--add-dir`, `--approval-mode`, ...),
because ck-run sets those itself. The exact agent argv is checked again: a denylist plus an allowlist of
values for permission flags (`plan`, `acceptEdits`, `accept-edits`, `ask`, `read-only`, `workspace-write`,
`enabled`). The prompt text is not scanned, so a task may talk about these flags.

## Rule check after each run

- **Disk** (metadata only, never opens a file, so no OneDrive downloads; no symlink following): files and
  folders newer than a marker in the workspace, the write scopes and symlinked raw folders. On 2026-11-02 this
  was about 70k entries and 2.3 s per workspace.
- **violation** (exit 5): any file change in read mode; a change in a raw folder, a protected rule file or
  outside the scope; a new `.git`/`.venv`/`venv`/`node_modules`/`site-packages` in OneDrive; a credential path,
  cloud sim call, push/send, clone/venv/install or permission bypass in the agent's actions (claude tool
  calls, codex commands).
- **flagged** (exit 4): the same patterns found only in output text (cursor and agy give no tool log), a
  folder change inside a raw folder (could be a delete), or agy printing that `--mode` is not supported in
  print mode.
- **notes only**: folder mtime changes elsewhere (often OneDrive sync).
- **Wiki first**: `files_read` must show `index.md` and the retrieval guide (reviews: the result file), else `partial`.
- **Numbers**: no source, a cited path that does not exist, or `verified: false` → **UNVERIFIED** in result.md.

## Tests

```
cd ~/PhD_local/ck-dispatch && python3 -I tests/run_all.py
```
42 tests, about 16 s. Stub CLIs (`tests/fake_cli.py`), a temp HOME, fake workspaces and a test config.
No network, no real agent. They cover: no forbidden flag for any agent or mode; every refusal case; every
write-scope rule; env allowlist; rulecheck plants (read-mode write, raw write, symlinked raw repo, new
`.venv`, rule file, credential read, `git push`, text-only mention, agy mode warning); timeout kill;
workspace locks; rate-limit fallback; summary cut and fallback; UNVERIFIED tags; ledger fields; review
sends paths not content; blind review; accept; thread view.

## Step 3 live checks (2026-11-02; claude 2.1.316, codex-cli 0.166.0, agent 2026.10.22, agy 1.4.12)

Confirmed live (read mode, ck-run argv + clean env, tiny prompts; no workspace file changed):
- claude: schema result is in `structured_output`; plan mode; OAuth works in the clean env. Directory deny
  rules (`~/.ssh/**`, `//<abs path with spaces>/**`) block reads. **Fixed:** bare `**/.env`-style rules are
  cwd-relative and missed files outside the workspace, so they are now `//**/...`. **Fixed:**
  `--strict-mcp-config` keeps plugin MCP servers (`skins`), so `mcp__skins` and the send/publish/schedule tools
  are denied in `config/claude-guard.base.json` (they then drop out of the tool list).
- agy: `-p <prompt>` takes the prompt as its value; inline `--json-schema` gives `structured_output`;
  `--mode plan` is applied in print mode (agy log: "applying agent mode plan", no warning); `--sandbox` =
  terminal sandbox only (log: "enabling terminal sandbox"); tool permission is `request-review`, not
  `always-proceed`; it loads `AGENTS.md` and `.agents/AGENTS.md`, not `CLAUDE.md`.
- cursor: `--mode plan` is recorded in the chat store (`"mode":"plan"`); it loads `.cursor/rules/*.mdc`,
  `CLAUDE.md`, `AGENTS.md` and the parent `PhD/.cursor/rules`. First run took 199 s wall (33 s API).

Still [assumed]: agy and cursor plan mode actually *blocking* a write (not tried: edits were out of scope);
`--sandbox enabled` covering cursor's edit tool; all codex live behaviour (`-c sandbox_workspace_write.
network_access=false`, `--output-schema` with this strict schema, which loads `~/.codex/AGENTS.md` plus the
workspace `AGENTS.md`; the git root is `~`). `codex help exec` confirms every flag name ck-run uses.

## Known gaps

- Codex, Cursor and agy sandboxes can still **read** credential files. For them the rule rests on the prompt
  plus the transcript scan, and only claude and codex give a real tool log.
- Cursor and agy edits are kept in the scope only as far as their own sandboxes enforce it. ck-run detects
  stray writes after the run; it cannot undo them.
- The disk check uses mtimes: OneDrive sync, or your own edits during a run, can show up (folder-only
  changes are notes; file changes are violations).
- Codex runs shell commands through a login shell, so keys exported in shell rc files can reach those commands.
- Your home folder `~` is a git repo. Codex's sandbox keeps `~/.git` read-only because it is outside the scope.
- Claude workers still load your user hooks and plugins (for example the superpowers SessionStart text) and
  your auto-memory `MEMORY.md` index, because ck-run does not pass `--bare` (it is refused: it also skips
  keychain/OAuth login).

## Files

```
bin/ck-run                  launcher (re-execs with python3 -I)
ckdispatch/cli.py           commands
ckdispatch/runner.py        one run: guards, run folder, child, parse, rulecheck, ledger, stdout
ckdispatch/guards.py        forbidden flags, write scopes, env allowlist (code, not config)
ckdispatch/flags.py         per-agent argv map
ckdispatch/prompt.py        rules header, output contract, review task
ckdispatch/summarize.py     output parsers, summary, UNVERIFIED tags, result.md
ckdispatch/rulecheck.py     disk scan + transcript scan
ckdispatch/ledger.py        ledger.jsonl and thread view
ckdispatch/route.py         routing matrix, rate-limit state (parse_reset ported, MIT)
config/workspaces.json      workspace roots, wiki entry files, rule files, raw folders
config/claude-guard.base.json  Claude deny rules (ck-run adds Edit denies per run)
schema/result.schema.json   result schema (agents)   schema/packet.schema.json (packet self-check)
docs/architecture.mmd       diagram source (PNG is step 10)
tests/                      offline suite
examples/sample_task.md     a task file for --dry-run
```

`route.parse_reset()` is ported from AgwaB/grok-bot-agents at commit `0aa87fc` (MIT); see
`LICENSE-THIRD-PARTY`. Nothing else is copied from that repo, and the plugin is not installed.
