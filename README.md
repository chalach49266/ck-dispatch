# ck-dispatch: `ck-run`

One runner that CK (Grok Bot) calls on the Mac to send a task to one of four agent CLIs:
`claude`, `codex`, `cursor` (binary `agent`) or `agy`. It adds guards, a run folder, a summary of at most
10 lines, an interaction ledger and cross-review. Python 3 standard library only. Local git, no remote.

Status: **step 1 of the plan** (`~/.claude/plans/read-only-planning-task-do-cosmic-emerson.md`) built and
tested offline with stub CLIs. **Step 3** (2026-11-02): live read-only probes of claude, cursor and agy with
ck-run's own argv and env; results, session ids and resume commands in `docs/step3/results.md`. Codex was
not run live (the `merge-gate` mod in Claude Code denies `codex exec`). **Live view** (2026-11-02): `--live`
(default on) runs each agent in its own interactive CLI in a new iTerm2 window; see "Live mode" below.

## Commands

```
ck-run run    --agent claude|codex|cursor|agy --workspace devfab|nv|washu --title T --prompt-file F
              [--task-type T] [--model M] [--read-dir D]... [--timeout S] [--parent RUN_ID] [--lead L]
              [--mode auto|read|edit] [--write-scope FOLDER ...] [--claim PATH ...] [--interactive]
              (edit also needs --approved "<user's words + time>")
              [--live | --no-live] [--dry-run]
ck-run review RUN_ID --by AGENT [--blind] [--focus TEXT] [--model M] [--timeout S] [--live | --no-live]
ck-run thread RUN_ID [--no-summary]
ck-run accept RUN_ID [--reject] [--by CK|user] [--note TEXT]
ck-run show   RUN_ID
ck-run route  --task-type T [--review-of AGENT]
ck-run limits [--clear [AGENT]]
ck-run active                 live runs and their claims (JSON); cleans records of crashed runs
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
| 6 | busy | a claim (`--claim`, or an edit run's `--write-scope` folder, claimed automatically) overlaps a path that a live run claimed (nothing started). No workspace lock: see Concurrent runs |

`--task-type` keys for `route`: wiki_synthesis, python_debug, refactor, fdtd_review, microwave_review,
slides_web, writing, quick_lookup, multi_file_diff, wiki_lint, general (Framework v2 §4 plus agy lanes).

## Decisions (user, 2026-11-02)

| | Decision | Where in code |
|---|---|---|
| D1 | Codex may edit inside Device Fabrication / NV Compact Magnetometer after approval; raw folders are blocked | `flags.EDIT_AREAS`, `guards.validate_write_scopes` |
| D2 | agy may edit after approval with `--mode accept-edits`; `--mode auto` only when ck-run injects it (auto mode); never skip-permissions | `flags.build` |
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

## Auto mode (default since 2026-11-02; chalach approved 22:34 CST)

`--mode auto` is the default. Each agent runs in its **own safety-reviewed auto mode**; ck-run injects the flags and
the guards allow them only in the final child argv of an auto run (`check_child_argv(..., auto=True)`). CK/Grok Bot
can never pass them itself (`check_ck_argv` still refuses `--auto-review`, `--permission-mode`, `-c`, ...).

| agent | injected flags |
|---|---|
| claude | `--permission-mode auto`; settings deny Edit/Write on raw folders and rule files |
| codex | `-s workspace-write -c approval_policy="on-request" -c approvals_reviewer="auto_review" -m gpt-6.1-sol` (live adds `-a on-request`) |
| cursor | `--trust --sandbox enabled --force` (allow-all) + **mandatory Claude review** |
| agy | `--sandbox --mode auto --add-dir <workspace root> --add-dir <run folder>` (no prompts for reads or edits there) + **mandatory Claude review** |

**Cursor auto = allow-all + mandatory Claude review, approved by chalach on 2026-11-03** ("do cursor allow everything
setting and also have claude code check ... if it is anything important but otherwise always allow"), and **the
same for agy** ("allow agy to edit any file"). Cursor gets its allow-all flag `--force` (`agent --help`: "Force allow
commands unless explicitly denied") instead of `--auto-review`. agy (chalach, later the same day: "agy still asks
permission for the Device Fabrication folder ... pass agy --mode auto plus --add-dir for the workspace root and the run
folder") gets `--mode auto --add-dir <workspace root> --add-dir <run folder>` in headless and `--live` runs, so it never
prompts for reads or edits inside them (the cwd is the task folder; the root is not repeated when it is the cwd).
Interactive agy shows "Auto mode: edits & safe commands auto-approved" (checked 2026-11-03). In print mode agy says
"--mode auto is not available for this account; continuing in the default mode"; for agy auto runs the rule check keeps
that as a note, not a flag. Raw folders stay protected by the rule check and the post-run review, not by agy. Both
keep their sandbox (cursor also `--trust`), and run in the task folder (cwd/`--workspace` = the first write scope,
else the workspace).
`-f`, `--yolo` and agy's `--dangerously-skip-permissions` stay refused, and CK can never pass `--force` itself.

**Auto with no write area: `auto_readonly`** (chalach 2026-11-03 19:34 CT: "Change ck-run so auto mode in WashU runs
read-only when no writable folder exists"). In a workspace with a `writable_only` list (washu, nv) where none of
those folders exists (washu before any `<course>/solutions/` or `<course>/prep/` is made), auto mode without
`--write-scope` no longer exits 2. It starts, read-only:
- stderr: `ck-run: NOTE (<agent>): auto_readonly: no writable folder. None of the writable folders of WashU HW exists
  yet (...), so this auto run is read-only ...`; ledger key `auto_readonly: "no writable folder"` on every event of
  the run (the dispatch and result notes say it too), `rulecheck.json` `auto_readonly` + a note, `packet.json`,
  `result.json`, `cmd.txt`, `ck-run active`. `mode` stays `auto`.
- Each agent keeps its own auto mode; only the write area goes: claude `--permission-mode auto` with the read-mode
  deny rules (the whole workspace; `--live`: only its run folder open); codex `-C <ws> -s read-only` with the same
  `approval_policy="on-request"` + `approvals_reviewer="auto_review"` (its `--live` writes of `result.md` /
  `summary.md` go through auto review, no user prompt); cursor `--force` and agy `--mode auto` unchanged, with their
  mandatory post-run review (task folder: none, so any workspace write is `outside_task_folder`).
- The prompt says READ-ONLY for this run, do not create the writable folders, no `PROGRESS.md`, put needed changes in
  `proposed_changes`. The rule check treats the run as a read run: any workspace file change is a violation (exit 5),
  and so is a writable folder that now exists (`writable folder theory II WashU/solutions/ created during an auto run
  with no write area`). Concurrent runs never count it as a writer.
- ck-run creates no folder and never touches course files. `--claim` is refused (nothing to claim); an explicit
  `--write-scope` that does not exist is still refused. As soon as one writable folder exists, auto runs write there
  again as before.

**Post-run review** (`ckdispatch/postreview.py`), after every cursor or agy auto run, headless or `--live`:
1. ck-run writes `<run>/review-input.md`: files changed, files that disappeared (a name listing before and after),
   `git status`/`git diff` of the task folder if it has its own repo (never the home-folder repo), the rule check
   and keyword hits. It also writes `<run>/review-actions.txt`: the agent's tool calls from Cursor's JSONL
   transcript or agy's SQLite conversation store (the run's own session, see Session id under Live mode; a
   headless cursor run uses the session id the CLI printed). If ck-run could not confirm the session is this
   run's, the evidence, review-actions.txt, the review prompt and review.md say so, and that session's risky
   tool calls are hints for the reviewer, not hard facts.
2. A short read-only Claude Code check runs: `claude -p --permission-mode plan --json-schema schema/review.schema.json
   --settings <run>/claude-settings-review.json --add-dir <run>`, prompt on stdin, cwd = the task folder,
   timeout `post_review.timeout_s` (default 300 s; optional `post_review.model`).
3. Flagged: writes outside the task folder, raw data folders (`AQS/`, `raw/`, the workspace's raw list), deletions,
   git push, network uploads, credential files, paid cloud (Tidy3D/Flexcompute) jobs. In nv, `AQS/` is not raw
   for the review (chalach 2026-11-03: yes, since nv may write it; see Raw folders): an nv `AQS/` change inside the
   task folder is fine, one outside it is still `outside_task_folder`, and `AQS/.../raw/`, deletions in `AQS/` and
   risky commands are still flagged. `AQS/` from any other workspace is still raw. Hard facts flag the run even
   if Claude says ok; a review that does not finish is flagged.
4. Output: `<run>/review.md` and a ledger event `cursor_review` | `agy_review` (status `ok` | `flagged`). If
   flagged: a loud `!!` warning on stderr, run status `flagged` (exit 4), and summary line 1 =
   `FLAGGED by the post-run Claude review: ...`. `ck-run thread` shows the review.

- Write area: `--write-scope` folders if given (same rules as edit mode, no `--approved` needed), else the workspace
  root minus raw folders and protected rule files. rulecheck flags any change outside it, in a raw folder or a rule file.
- No workspace lock (chalach 2026-11-03): auto, edit and read runs of any agents run at the same time, also in one
  workspace; only overlapping claims are refused (see Concurrent runs). An auto run given `--write-scope` claims
  those folders automatically, like an edit run. Reviews stay read-only.
- The prompt tells the agent to work on its own, mark unknowns, keep `PROGRESS.md`, and end the summary with
  "Questions for chalach" for Grok Bot to relay.
- Still refused in every mode: bypassPermissions, `dangerously-*`, `-f`/`--yolo`, `--force` (except cursor auto,
  injected by ck-run), `danger-full-access`, `approval_policy=never`, `--full-auto`.
- `--interactive` (= `--live`) opens the run in an iTerm2 window (`live.terminal` in workspaces.json, falls back to Terminal).
- `--mode read` keeps the old look-only behaviour (plan / read-only).

## Live mode (`--live`, default on; `--no-live` = headless only)

`run` and `review` open a new iTerm2 window (`live.terminal`; `osascript` → `create window with profile
"CK Live <agent>" command "/bin/bash <run>/live.sh"`; Terminal's `do script` if iTerm2 fails) whose session
**runs** `<run>/live.sh` as its program. The script `cd`s to the agent's folder (the workspace, or the first
write scope) and starts the agent's **own interactive CLI** with `/usr/bin/env -i` (the same allowlisted env as
headless, minus `NO_COLOR`, with `TERM` from the window) and `<run>/prompt-live.md` as the first message. When
the agent exits, the window keeps a login shell. The default comes from `live.default` in `config/workspaces.json`.

**Live fix (2026-11-03).** Until then ck-run activated iTerm2 and *typed* `/bin/bash <run>/live.sh` into a new
shell (`write text`). The new window took keyboard focus, so whatever chalach was typing elsewhere landed in
front of the command (`is /bin/bash …` → `zsh: command not found: is`); live.sh never ran, and after 30 s the
run went headless with only a note. Seen for codex, cursor and agy. Now nothing is typed and no app is
activated, and every fallback is loud (see below).

- **Window name**: `CK · <agent> · <title> · <HHMMSS-hex of the run id>`, set as the iTerm2 session name.
- **Per-agent iTerm2 profile**: ck-run keeps `~/Library/Application Support/iTerm2/DynamicProfiles/ck-run-live.json`
  with one profile per agent (`CK Live claude|codex|cursor|agy`): its own tab color and badge, title = session
  name, and **"Allow Title Setting" off, so the agent's TUI cannot overwrite the title**. Everything else
  comes from your default profile. If iTerm2 has not loaded the profile yet, ck-run uses the default profile
  (title not locked; live.sh still sets the badge and tab color with iTerm2 escape codes).
- **Ledger**: `live_window_opened` (note: `window_id=<iTerm2 window id>`, terminal, profile, whether the title is
  locked, the title) and, if the window does not run the agent, `live_fallback` (status `fallback`, note: the
  reason, with the last lines the window shows). ck-run also prints `LIVE WINDOW FAILED ...` on stderr.

| Agent | Interactive argv (same permission flags as headless) | Initial prompt (from `--help`) |
|---|---|---|
| claude | `--strict-mcp-config --settings <run>/claude-settings-live.json --session-id <uuid> --add-dir <run> [--add-dir <scope>] [--allowedTools ...] --permission-mode plan\|acceptEdits` | positional `[prompt]` |
| codex | `-C <ws\|scope> -s read-only\|workspace-write [-c sandbox_workspace_write.network_access=false --add-dir <run>] -a on-request` | positional `[PROMPT]` |
| cursor | `--trust --sandbox enabled [--mode plan] --workspace <ws\|scope> --add-dir <run>` | positional `[prompt...]` |
| agy | `--sandbox --mode plan\|accept-edits\|auto --add-dir <run>` (auto also `--add-dir <workspace root>`) | `-i <prompt>` (`--prompt-interactive`) |

- No `-p`/`exec`/`--output-format`/`--json-schema` (interactive mode has none), and still no
  skip-permissions/yolo/force/full-auto/bypass flag. The same argv guard checks the live argv.
- **codex `-a on-request`** is new and needed: your `~/.codex/config.toml` sets `approval_policy = "never"`
  and `sandbox_mode = "danger-full-access"`; `-s` and `-a on-request` override both. `-a never` stays refused.
- **Output**: interactive mode has no result JSON on stdout, so the live prompt asks the agent to write
  `<run>/result.md` (full answer ending with the result JSON) and then `<run>/summary.md` (1-10 lines) LAST.
  ck-run polls for `summary.md`; when it is written (and stable), ck-run runs the rule check, writes
  `result.json`, cuts `summary.md` to 10 lines if needed, appends the `result` ledger line and prints the summary
  as usual. The agent's own `result.md` is kept. The window stays open for you to read or keep talking; anything
  you do in it after that is not checked by ck-run.
- **Read mode + these two writes**: plan / read-only modes stop the agent from writing them, so its CLI asks
  you first (claude: approve leaving plan mode, then the writes; codex: approve the write; cursor and agy:
  switch out of plan mode). The live Claude settings allow edits only in this run's folder (deny rules still
  cover both workspaces, the ledger, state, the run registry, every other run folder and ck-run's own files in
  this run).
  If you would rather not approve, quit the agent: ck-run then looks for the answer JSON in the agent's
  session log (claude, codex, cursor JSONL; not agy's SQLite store) and marks the run done with it.
- **Session id**: claude gets `--session-id <uuid>` from ck-run; for codex, cursor and agy ck-run finds the
  new session in `~/.codex/sessions/...`, `~/.cursor/projects/*/agent-transcripts/<id>/` or
  `~/.gemini/antigravity-cli/conversations/<id>.db` whose first `RUN: <run_id> | agent: <agent>` line (the
  prompt's own line) is this run's. A bare mention of the run id does not count: a parallel run's prompt names
  it in its CONCURRENCY line (2026-11-04: two cursor runs 1 s apart both got the newer one's session). Only if
  no new session has that line does ck-run fall back (a quote of the line, a mention, or the only new session);
  `session_how` then says "may be wrong", `session_sure` is false, no answer is recovered from that log, and
  the post-run review is told its tool calls may be another run's. Saved in `result.json`.
- **Timeout**: at `--timeout` ck-run stops the agent in the window (SIGTERM then SIGKILL to the `live.sh`
  process group) and the status is `timeout`. Quitting the agent without writing anything gives `failed`.
- **Fallback to headless** (same run folder, `prompt.md`, note in the result, ledger `live_fallback`, stderr
  `LIVE WINDOW FAILED`): `osascript` fails (for example macOS has not allowed the caller to control iTerm2 or
  Terminal), the window does not start the agent within `live.start_timeout_s` (30 s; ck-run then takes
  `<run>/live.claim`, so a late window does nothing, and quotes what the window shows), or the agent CLI exits
  with an error code within `live.early_exit_s` (20 s) without writing anything.

## Edit mode: write-scope rules

- Needs `--approved` (the user's words) and at least one existing `--write-scope` folder.
- Allowed: a subfolder of the run's workspace (codex, cursor, agy), or a `~/PhD_local` folder that is its
  own git repo (all four).
- Refused: the home folder or above; the whole workspace root; any folder that is, contains or sits
  inside a raw folder (checked on the literal and the real path, so a symlink cannot get around it);
  `.claude/`, `.cursor/`, `.obsidian/`, `.venv/` (and NV `.agents/`, venvs, `qick-dawg (backup)/`);
  any `.git`, `.venv`, `venv`, `node_modules` path; `~/PhD_local` itself, `ck-dispatch`, `agent_runs`,
  `grok-bot-agents` and the `~/PhD_local` copies of workspace trees; other OneDrive folders; in NV,
  anything outside `NV Project/` and `AQS/`; in WashU HW, anything outside a course's `solutions/` or `prep/`.
- Enforcement: codex's OS sandbox lets it write only in `-C <scope>` (network off). Cursor and agy start in
  the scope with their sandbox on. Claude gets deny rules. After every run the rule check looks at the disk.
- No workspace lock: other runs may work in the same workspace at the same time (see Concurrent runs).

## Concurrent runs (chalach 2026-11-03)

Any number of runs, any of the four agents, in the same or different workspaces, start at once. Nothing waits
and nothing is serialized. Until 2026-11-03 ck-run had a `SlotLock` (`runner.py`): flock slot files
`agent_runs/.locks/<ws>.slot<i>.lock`, at most `max_runs_per_workspace` (2) runs per workspace, and an edit or
auto run took every slot, so a second run in that workspace got `busy` (exit 6). That lock and the config key are
gone (an old key is ignored). The one conflict ck-run still prevents is two live runs editing the same file
(`ckdispatch/claims.py`):

- **Claims.** An auto or edit run may declare the files or folders it will edit: `--claim PATH` (repeatable;
  relative to the workspace root or absolute; may not exist yet; must be inside the run's write area). A claim that
  is the same as, inside, or around a path a live run claimed is refused: ledger `busy` with the other run's id,
  agent and pid in the note, exit 6, no agent started, no run folder left. Read runs, reviews and runs without
  claims never block anyone and are never blocked. `--claim` with `--mode read` is refused (exit 2).
- **Write folders are claimed automatically** (chalach 2026-11-03 19:02 CT: "Yes, have edit runs claim their write
  folders automatically"). An edit run, and an auto run given an explicit `--write-scope`, claims its `--write-scope`
  folders, so a second edit run in the same folder (or one inside or around it) gets `busy` before it starts instead
  of a flag after. `--claim` adds more paths; one inside an auto-claimed folder is covered by it. The point is
  parallel agents, so ck-run never claims automatically a folder that every run in a workspace may write: a
  workspace root, a default write folder (`writable_only`: nv `NV Project/` and `AQS/`, each washu
  `<course>/solutions/` and `<course>/prep/`), a folder that holds a wiki entry file (`wiki_first` /
  `open_questions`: devfab `Design Library/`, `Design Library/maps/`, `Design Library/questions/`, nv
  `NV Project/wiki/`, ...), `~/PhD_local`, or a folder around one of these. `cmd.txt` and `--dry-run` list such a
  scope as "not claimed automatically" with the reason; claim a narrower subfolder or file (`--write-scope` or
  `--claim`) instead. Auto runs without `--write-scope`, read runs and reviews claim nothing automatically. The run
  record and `ck-run active` show `claims_auto`.
- **Run registry.** Every run has `agent_runs/.claims/<run_id>.json` (agent, workspace, mode, claims, write scope,
  pid, agent pid) while ck-run works on it, flock-held by ck-run. Check-and-register runs under
  `.claims/registry.lock`, held for milliseconds. `ck-run active` lists the live runs.
- **Stale cleanup.** A record whose flock is free belongs to a ck-run that crashed or was killed. The next run
  (or `ck-run active`) deletes it and writes a `stale` ledger event for that run (status `failed`; `ck-run thread`
  shows it). If the agent CLI outlived its ck-run (the headless child's pid in the record, or a live window's
  `live.pid` without `live.exit`), its claims stay until that process ends.
- **Agents are told.** The auto/edit prompt has a `CONCURRENCY:` line: this run's claims, the paths other live runs
  claimed (never edit them), and "do not undo changes you did not make". Claude also gets `Edit` deny rules for
  those paths. `cmd.txt` and `--dry-run` show the claims (which came from `--write-scope`, which were not claimed).
- **After the run.** The disk scan sees every change in the workspace, also other runs'. A change inside another
  overlapping run's claim, or one only a concurrent writer could have made (a read run never writes; a writer's
  change outside this run's write area), goes to that run: a note, `rulecheck.json` `changed_by_others`, not this
  run's `files_changed` and not a violation. A change both runs could have made without claims stays this run's,
  with a note naming the other run. A file this run's agent reported changing that a concurrent run claimed or
  also reported changing is a flag (exit 4). `rulecheck.json` `concurrent` lists the overlapping runs. The cursor
  and agy post-run review gets the same split, a list of the other runs, and drops deletions inside their claims.
- **Safe shared files.** Run ids end in 4 hex digits; the run folder is created with `mkdir` before anything else,
  and a clash picks a new id. Ledger lines are one `O_APPEND` write under `flock` (readers take a shared lock).
  `state.json` (rate limits) is read-modified-written under `state.json.lock` with a per-process temp file.
- Limits: claims are declared (`--claim`) or come from `--write-scope`, not guessed; an agent that edits a file it
  did not claim is caught after the run (flag), not stopped (Claude excepted: deny rules for paths claimed when it
  started). Two runs without claims (auto on the default area, or a shared `--write-scope` that is not claimed
  automatically) may still edit the same file; when both report it, the later run is flagged.

## Raw folders (read-only), from the workspace rule files

- **Device Fabrication** (`.agents/AGENTS.md:78-83`, `CLAUDE.md:73-78`): `Projects/CK Design/`,
  `Projects/Microwave Design/`, `Fabrication/`, `Projects/ESEM/`, `Projects/Simulation/`, `Projects/Result/`,
  `Idea Inspiration/`, `Paper/`, `Material/`, `Design Yard/`, `Archive/`, `Photonics_Ecosystem/repos/`,
  `Microwave_Ecosystem/repos/`.
- **NV Compact Magnetometer** (`CLAUDE.md:15`: "the rest of this workspace ... holds the raw, human-owned
  sources"; list in `NV Project/CLAUDE.md`): `nv_magnetometer_project/`, `NV_Magnetometry_Software-main/`,
  `NV_ODMR_FPGA_ARM_PS_20260902/`, `Literature/`, `System_Design/`, `IMU/`, `Patent/`, `output/`,
  `qick/`, `qick-dawg/`, `PYNQ/`, `Archive/`. Agents may write only in `NV Project/` and `AQS/` (`writable_only`).
  **AQS is writable** (chalach 2026-11-03: "Keep AQS writable from the NV workspace"; the rule files agree:
  `CLAUDE.md:15` and `NV Project/CLAUDE.md:99`, "AQS/ is writable by all agents"). `--write-scope AQS` is
  accepted, and nv auto mode without `--write-scope` opens both `NV Project/` and `AQS/`. `AGENTS.md:6` still
  holds: never deploy the AQS website or send decks without approval. The cursor/agy post-run review does not
  flag nv `AQS/` edits inside the task folder either (chalach 2026-11-03, "yes"; `postreview.WRITABLE_SEGMENTS`);
  it still flags `AQS/` changes from other workspaces.
- **WashU HW** (`washu`, `~/Downloads/WashU HW`, not in OneDrive; `CLAUDE.md:67-75` "Ownership" and chalach
  2026-11-02): the original course files are read-only. Agents may write only in `<course>/solutions/` and
  `<course>/prep/` of the seven course folders (`writable_only`; add a pair when a course is added). Auto mode
  without `--write-scope` uses those that exist; if none does, it runs read-only (`auto_readonly: no writable
  folder`, see Auto mode; until 2026-11-03 19:34 CT it was refused with exit 2). Claude auto gets a deny rule for
  every other entry; the rule check flags any change elsewhere. Runs go to the shared ledger in `agent_runs`.
- Rule files are protected in every workspace (a change to them inside a scope is a violation).

## Run folder: `~/PhD_local/agent_runs/<run_id>/` (mode 700)

`task.md` (CK's text) · `prompt.md` (rules header + task + output contract) · `packet.json` (Framework §5.1) ·
`cmd.txt` · `claude-settings.json` (claude) · `stdout.log` / `stderr.log` (full raw output) · `last.md` (codex) ·
`result.json` (parsed result + checks) · `result.md` (readable full result) · `summary.md` (max 10 lines) ·
`rulecheck.json`. Live runs add `prompt-live.md`, `live.sh`, `claude-settings-live.json` (claude),
`live.claim`, `live.pid`, `live.exit`; their `result.md` is the agent's own, and `result.json` has a `live`
block (window error, state, session id and where it was found). Run id:
`YYYY-MM-DDTHHMMSS-<ws>-<task_type>-<agent>-<hex4>`.

## Token saving: `summary.md`

The prompt ends with an output contract. The agent returns one JSON object (schema
`schema/result.schema.json` = Framework §5.4 + `summary` as 1-10 lines + `report_md`); claude, codex and agy
get it as a CLI schema, cursor gets it in the prompt. ck-run writes `summary` to `summary.md` as is (cut to
10 lines, 200 characters each). No summary: `summary.md` starts with `[ck-run] The agent returned no summary`
and the status is `partial`.

## Interaction ledger: `~/PhD_local/agent_runs/ledger.jsonl`

One JSON line per event; every line has the same keys: `event` (dispatch, review, result, refused, busy (claim
conflict; `claims` includes auto-claimed `--write-scope` folders), stale (crashed ck-run), accept, ...), `run_id`, `parent_run_id`, `from` (CK or agent), `to`, `via`,
`title`, `workspace`, `task_type`, `agent`, `model`, `mode`, `auto_readonly` (null, or `"no writable folder"`: an auto
run with no write area, see Auto mode), `approved`, `write_scope`, `claims`, `prompt_path`, `response_path`, `summary_path`,
`files_read`, `files_changed`, `status`, `accepted`, `ts`, `started_at`, `finished_at`, `duration_s`,
`exit_code`, `note`. A live run's `dispatch` line has a `live:` note; its `result` line's `prompt_path` is
the prompt actually used (`prompt-live.md`, or `prompt.md` after a fallback). A review goes from the reviewed agent to the reviewer, via CK. `accepted` is set by
`ck-run accept`. `ck-run thread <run_id>` finds the root of the chain and prints it as markdown, with
reviews and follow-ups (`--parent`) nested and each run's summary quoted. A review's heading reads
`CK → <reviewer> · review of <agent>'s run` (CK dispatched it; the ledger line keeps `from` = reviewed agent).

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
`enabled`; codex `-a`/`--ask-for-approval`: only `on-request`). The prompt text is not scanned, so a task may talk about these flags.

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
150 tests, about 185 s. Stub CLIs (`tests/fake_cli.py`), a mock `osascript` (`tests/fake_osascript.py`: fails
by default, so every older test also covers the headless fallback; `run` runs `live.sh` detached the way a
Terminal window would; `silent` never starts it), a temp HOME, fake workspaces and a test config. No network,
no real agent, no Terminal window. They cover: no forbidden flag for any agent or mode; every refusal case;
every write-scope rule; env allowlist; rulecheck plants (read-mode write, raw write, symlinked raw repo, new
`.venv`, rule file, credential read, `git push`, text-only mention, agy mode warning); timeout kill;
no workspace lock (old slot files ignored); rate-limit fallback; summary cut and fallback; UNVERIFIED tags; ledger fields; review
sends paths not content; blind review; accept; thread view. Live (`tests/test_live.py`): interactive argv
keeps each agent's permission flags (codex `-a on-request`, `-a never` refused); launcher script (env -i,
claim); live Claude deny rules; each agent end to end in a mocked window (summary.md → ledger `ok`, session
id found); each run gets its own session when a newer parallel session names its run id (`tests/test_sessions.py`,
codex/cursor/agy, headless and live); Ctrl+C after summary.md is still ok; 10-line cut; rule check in live mode; answer recovered from the session log; timeout stops the
agent; review runs live; fallback on osascript error, on a window that never starts, and on an early error
exit; a late window does nothing; `--no-live`; `--dry-run` shows both commands. Window identity (2026-11-03):
the title format; iTerm2 runs live.sh as the session command (no `write text`, no `activate`) with the agent's
profile; the dynamic profiles (title locked, 4 tab colors); default-profile case; `live_window_opened` and
`live_fallback` ledger events with the window id, the reason and the window's last lines; iTerm2 → Terminal → headless.
Cursor/agy auto (`tests/test_postreview.py`, 2026-11-03): cursor gets `--force` (headless and live) and agy
`--mode auto` with `--add-dir` for the workspace root and the run folder (headless and live; never
`--dangerously-skip-permissions`), its print-mode "auto not available" warning is a note; `--force` only for a cursor
auto argv built by ck-run (never `-f`/`--yolo`, never from CK);
the review runs read-only (plan mode, run folder added, task folder as cwd, no secrets); `ok` review → ledger
`cursor_review`/`agy_review` `ok`; Claude `flagged` → exit 4, loud stderr warning, summary line 1; a review that
fails or times out is flagged; hard facts flag even when Claude says ok (deleted file, raw-folder and outside
writes, git diff deletions in a real repo, risky Cursor transcript and agy SQLite tool calls); `AQS/` and `raw/`
count as raw for the review whatever the config says, except `AQS/` in nv (2026-11-03): an nv cursor/agy AQS edit
in the task folder gets an ok review, while an nv AQS edit outside `--write-scope`, `AQS/.../raw/`, an AQS deletion,
the nv raw list and devfab `AQS/` are still flagged; claude/codex auto and read runs get no review; `--dry-run` names the review.
Concurrency (`tests/test_concurrency.py`, 2026-11-03; real ck-run processes started together): two runs of
different agents in one workspace overlap in time and both finish ok; edit + auto + read in one workspace; other
workspaces; overlapping claims refused (inside, around, same absolute path; codex never starts, no folder left) and
allowed again after the claiming run ends; non-overlapping claims run together; runs without claims never blocked;
other runs' claims in the prompt and Claude deny rules; bad claims refused (read mode, raw folder, rule file,
other workspace, outside `--write-scope`); `--dry-run` registers nothing; stale cleanup after a SIGKILLed ck-run
(claim kept while its orphaned agent runs, headless or in a live window, released after; `stale` ledger event;
thread view) and by `ck-run active`; a read run not blamed for a concurrent writer's change; changes inside another run's claim are that run's;
the same file reported by two concurrent runs, or edited inside another live run's claim, is flagged; a cursor
review not blamed for a claimed run's edits and deletions; run-folder reservation; 8 processes appending to the
ledger; the registry in-process; `state.json` updates from 4 processes. Automatic claims (`TestAutoClaims`,
`TestSharedAreaUnit`, 2026-11-03): two edit runs with the same `--write-scope`, the second refused (busy, codex
never starts, free again after); a scope around or inside, an auto run's explicit scope and a plain `--claim` inside
an auto-claimed folder all refused; edit runs in different folders (workspace and `~/PhD_local`) overlap in time;
shared areas never claimed (devfab wiki folders, nv `NV Project/`, `NV Project/wiki/`, `AQS/`, washu
`<course>/solutions/`; two edit runs there both run); auto runs claim only an explicit `--write-scope` (default area,
also washu/nv default folders: no claims, never blocked, told about others' claims); `--claim` next to auto-claims;
`--dry-run` and `ck-run active` show them; `shared_area()` and `from_scopes()` per path and mode.
Auto with no write area (`tests/test_washu.py` `TestWashuAutoReadOnly`, 2026-11-03 19:34 CT): washu with no
`solutions/` or `prep/`: claude, codex, cursor and agy all start in default auto mode and finish `ok` (NOTE on stderr;
`auto_readonly` in the ledger events, result, rule check, packet and cmd.txt; read-only prompt; claude whole-workspace
deny rules; codex `-s read-only` + auto review; cursor/agy review knows there is no task folder); live claude and
codex; a course-file write and a new `solutions/` folder are violations (and the cursor review flags them); normal
auto returns once a `prep/` folder exists; `--claim` and a missing `--write-scope` refused; the same for nv with
`NV Project/` and `AQS/` gone; rule check and attribution units. No test creates a folder in a real workspace.

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
- Live mode was tested only with the mock `osascript` and stub CLIs. Not yet tried on this Mac: a real
  Terminal window (macOS asks once whether the caller may control Terminal; until you allow it, runs fall
  back to headless), and each real TUI accepting this exact argv (`cursor --help` itself could not be run in
  the build session; its options were read from the installed bundle).
- Live mode has no tool log (no stdout stream), so the rule check sees only the disk and the agent's
  `result.md` text, as for cursor and agy headless. The prompt is an argument of the agent process (as for
  cursor and agy headless), so other local users could see it in `ps`.
- In live read mode the agent can only write `result.md`/`summary.md` after you approve it in its CLI;
  otherwise the run ends when you quit the agent (answer taken from the session log) or at the timeout.

## Files

```
bin/ck-run                  launcher (re-execs with python3 -I)
ckdispatch/cli.py           commands
ckdispatch/runner.py        one run: guards, run folder, child, parse, rulecheck, ledger, stdout
ckdispatch/claims.py        concurrent runs: run registry, --claim conflicts, stale cleanup, change attribution
ckdispatch/guards.py        forbidden flags, write scopes, env allowlist (code, not config)
ckdispatch/flags.py         per-agent argv map (headless build, interactive build_live)
ckdispatch/live.py          --live: iTerm2/Terminal window, profiles, live.sh launcher, wait, stop, session stores
ckdispatch/postreview.py    mandatory read-only Claude review after cursor/agy auto runs (review.md, ledger *_review)
ckdispatch/prompt.py        rules header, output contract, review task
ckdispatch/summarize.py     output parsers, summary, UNVERIFIED tags, result.md
ckdispatch/rulecheck.py     disk scan + transcript scan
ckdispatch/ledger.py        ledger.jsonl and thread view
ckdispatch/route.py         routing matrix, rate-limit state (parse_reset ported, MIT)
config/workspaces.json      workspace roots, wiki entry files, rule files, raw folders
config/claude-guard.base.json  Claude deny rules (ck-run adds Edit denies per run)
schema/result.schema.json   result schema (agents)   schema/packet.schema.json (packet self-check)
schema/review.schema.json   post-run review answer (status ok|flagged, findings by category)
docs/architecture.mmd       diagram source (PNG is step 10)
tests/                      offline suite (fake_cli.py stub agents, fake_osascript.py mock Terminal)
examples/sample_task.md     a task file for --dry-run
```

`route.parse_reset()` is ported from AgwaB/grok-bot-agents at commit `0aa87fc` (MIT); see
`LICENSE-THIRD-PARTY`. Nothing else is copied from that repo, and the plugin is not installed.
