# ck-run step 3: live read-only checks (2026-11-02)

CLIs: claude 2.1.316 · codex-cli 0.166.0 · agent (cursor) 2026.10.22-27dcf5f · agy 1.4.12.
Method: `docs/step3/probe.py` builds each argv with ck-run's own `flags.build` (read mode) and
`guards.child_env`, then sends a tiny prompt (`docs/step3/prompts/`). Raw logs are in `docs/step3/probes/`
(git-ignored). No workspace file changed: `find <both workspaces> -newer <first probe> -not -type d` was empty.

## 1. [assumed] flags

| Agent | Item | Result |
|---|---|---|
| claude | schema result in `structured_output` | PASS (result event has `structured_output`; StructuredOutput tool used) |
| claude | `--permission-mode plan` | PASS (init `permissionMode: plan`) |
| claude | `--strict-mcp-config` | PARTIAL: plugin MCP `skins` still connects. **Fixed** by denying `mcp__skins` |
| claude | `//abs path with spaces/**` rules | PASS (Read deny on `.../credguard/space dir/**` blocked) |
| claude | Read denies | **FAIL then fixed**: `**/.env`, `**/*auth*.json`, `**/*credential*` are cwd-relative; now `//**/...` |
| agy | `-p <prompt>` value | PASS |
| agy | inline `--json-schema` | PASS (`structured_output` + `json_schema` keys in stdout) |
| agy | `--mode plan` in print mode | PASS: log "applying agent mode plan"; no "not supported" text |
| agy | `--sandbox` scope | terminal sandbox only (log "enabling terminal sandbox for this session") |
| agy | `always-proceed` policy | not set: settings has only `model`, `trustedWorkspaces`; log `toolPermission=request-review` |
| cursor | `--mode plan` in print mode | PASS that the mode is applied (chat meta `"mode":"plan"`). Write-blocking not tried |
| cursor | `--sandbox enabled` covers edits | NOT TESTED (needs a write attempt) |
| codex | all | NOT RUN: Claude Code's `merge-gate` mod denies `codex exec`. `codex help exec` confirms `-o`, `--output-schema`, `-s read-only`, `-C`, `--add-dir`, `-c` |

Also seen: claude tools in this version have no `Grep`/`Glob` (the edit allowlist names them; harmless).
The tools denied in the guard (PublishPlugin, ShareOnboardingGuide, SendToProject(Context), SendMessage,
PushNotification, DesignSync, CronCreate, ScheduleWakeup, Workflow, mcp__skins) disappear from the tool list.

## 2. Rule files loaded at startup (no tool calls)

| Agent | Device Fabrication | NV Compact Magnetometer |
|---|---|---|
| claude | `CLAUDE.md` (+ user `MEMORY.md`, superpowers hook text) | `CLAUDE.md` (+ user `MEMORY.md`) |
| cursor | `.cursor/rules/claude-rules.mdc`, `.cursor/rules/flexagent.mdc`, `CLAUDE.md`, `AGENTS.md`, `PhD/.cursor/rules/flexagent.mdc` | `.cursor/rules/claude-rules.mdc`, `CLAUDE.md`, `AGENTS.md`, `PhD/.cursor/rules/flexagent.mdc` |
| agy | `AGENTS.md`, `.agents/AGENTS.md` | `AGENTS.md` |
| codex | not run; by its docs `~/.codex/AGENTS.md` + workspace `AGENTS.md` (git root is `~`) | same |

None loads the nested wiki rule files (`Design Library/CLAUDE.md`, `NV Project/CLAUDE.md`); ck-run's header
names them, so that is fine. agy does not need an extra pointer file: every root `AGENTS.md` already
points to the Claude rule files.

## 3. Claude credential read-block (fake fixtures in `tests/fixtures/credguard/`, missing probe names)

| Path | before fix | after fix |
|---|---|---|
| `credguard/control.txt` (control) | READ | READ |
| `credguard/.env`, `probe_auth.json`, `probe_credentials.txt` | **READ** | DENIED |
| `credguard/space dir/denied.txt` (extra `//` rule) | DENIED | DENIED |
| `~/.ssh/ck_step3_probe_missing`, `~/.tidy3d/ck_step3_probe_missing` | DENIED | DENIED |
| `~/.codex/ck_step3_probe_auth.json` (missing) | MISSING (= not blocked) | DENIED |

No real credential file was opened: the home paths are names that do not exist, so a broken rule could
only have shown "file does not exist".

## 4. Models

- agy (`agy models`): gemini-3.8-flash-high (default, "Gemini 3.8 Flash (High)"), gemini-3.8-flash-medium,
  gemini-3.8-flash-low, claude-sonnet-4-6, claude-opus-4-6-thinking, gpt-oss-120b-medium.
- cursor (`agent --list-models`, 80+ ids, default `auto`): e.g. gemini-3.7-flash-high, gpt-5.3-codex,
  gpt-5.6-sol-high, composer-2.5, claude-opus-5-5-medium, claude-sonnet-5-thinking-high, grok-4.7-medium.

## 5. Sessions (all persist; resume read-only)

| Agent | Probe | Session id | Stored at |
|---|---|---|---|
| claude | devfab rules | f4ff31c3-7049-4700-a46b-751a74476053 | `~/.claude/projects/-Users-ckasemtantikul-Library-CloudStorage-OneDrive-WashingtonUniversityinSt-Louis-Documents-PhD-Device-Fabrication/<id>.jsonl` |
| claude | devfab credguard v1 | 4ac53497-2dcf-472e-a2a3-497ddbb6e079 | same folder |
| claude | devfab credguard v2 | 7f18d9ce-0e2e-4ada-b8c7-201f9fd07fc2 | same folder |
| claude | nv rules | 661ea7cf-da7a-4f18-ae78-480bc5f2445a | `~/.claude/projects/-Users-...-PhD-NV-Compact-Magnetometer/<id>.jsonl` |
| cursor | devfab rules | 599c70d0-8bf4-410f-aec6-52724504e5e3 | `~/.cursor/chats/9b0ba4ef46416e33025a58bd602e7937/<id>/store.db` + `~/.cursor/projects/<ws-slug>/agent-transcripts/<id>/` |
| cursor | nv rules | 3d3d0941-746a-4ef4-821e-6021770daab7 | `~/.cursor/chats/296c023264fde7825d40aacebe0409cf/<id>/store.db` + `~/.cursor/projects/<ws-slug>/agent-transcripts/<id>/` |
| agy | devfab rules | 69994519-e3bc-401e-80cf-0b5cb222d13a | `~/.gemini/antigravity-cli/conversations/<id>.db` |
| agy | nv rules | 8625ab1b-33c9-4031-bd1d-2dd5d62ff14b | same folder |
| codex | not run | n/a | would be `~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread_id>.jsonl` |

Resume (from the workspace folder, keep it read-only):
`claude --resume <id> --permission-mode plan` · `agent --resume <id> --mode plan` · `agy --conversation <id> --mode plan`.
