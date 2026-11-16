"""Per-agent argv map. There is no code path that emits a forbidden flag.

Auto mode (DEFAULT since 2026-11-02, chalach approved 10:34 PM CT): each agent's own safety-reviewed auto
mode. Writes go to the --write-scope folders if given, else the workspace minus its raw/protected folders
(rulecheck enforces both). Never a full bypass:

  claude  auto: -p ... --settings <run>/claude-settings.json --permission-mode auto [--add-dir <scope>]
  codex   auto: exec ... -C <scope|ws> -s workspace-write -c approval_policy="on-request"
                -c approvals_reviewer="auto_review" -m <model|gpt-6.1-sol> -     (= --approve-for-me)
  cursor  auto: -p --output-format json --trust --sandbox enabled --force --workspace <scope|ws> <prompt>
  agy     auto: --output-format json --sandbox ... --mode auto --add-dir <workspace root> --add-dir <run> -p <prompt>

Auto with no write area (auto_readonly, chalach 2026-11-03 19:34 CT: a writable-only workspace such as washu where
none of the writable folders exists yet): the same auto argv, except codex -C <ws> -s read-only (approvals still
go to auto_review); Claude's settings deny edits in the whole workspace; cursor/agy are held by the prompt, the
rule check (any workspace change = violation) and their mandatory post-run Claude review.

Cursor and agy auto (2026-11-03, approved by chalach: "do cursor allow everything setting ... otherwise always
allow"; "allow agy to edit any file"): cursor gets its allow-all flag `--force` ("Force allow commands unless
explicitly denied", `agent --help`, 2026.10.22) instead of `--auto-review`. agy (chalach, 2026-11-03, later:
"agy still asks permission for the Device Fabrication folder ... pass agy --mode auto plus --add-dir for the
workspace root and the run folder") gets `--mode auto --add-dir <workspace root> --add-dir <run folder>`, so
reads and edits inside them need no prompt. Interactive agy shows "Auto mode: edits & safe commands
auto-approved" (checked 2026-11-03); print mode warns "--mode auto is not available for this account;
continuing in the default mode" (rulecheck keeps that as a note for agy auto, not a flag). Both keep
--trust/--sandbox, the cwd and --workspace stay the task folder, and every such run gets a MANDATORY read-only
Claude Code review afterwards (ckdispatch/postreview.py). `-f`, `--yolo` and agy's
`--dangerously-skip-permissions` stay refused; CK can never pass --force itself.

Read mode (default) and edit mode (only with --approved and --write-scope):

  claude  read: -p --permission-mode plan --strict-mcp-config --output-format stream-json --verbose
                --json-schema <inline> --settings <run>/claude-settings.json            (prompt on stdin)
          edit: same, but --permission-mode acceptEdits --allowedTools ... --add-dir <scope>
                (scope must be a ~/PhD_local repo; settings deny Edit/Write on both workspaces)
  codex   read: exec --json -o <run>/last.md --output-schema <file> -C <workspace> -s read-only -    (stdin)
          edit: exec ... -C <scope> -s workspace-write -c sandbox_workspace_write.network_access=false
                [--add-dir <more scopes>] -       (D1: scope may be inside a workspace, never a raw folder)
  cursor  read: -p --output-format json --trust --sandbox enabled --mode plan --workspace <ws> <prompt>
          edit: -p --output-format json --trust --sandbox enabled --workspace <scope> <prompt>   (D4)
  agy     read: --output-format json --sandbox --print-timeout <S>s --json-schema <inline> --mode plan -p <prompt>
          edit: same with --mode accept-edits, cwd = scope                                         (D2)

Step 3 live checks (2026-11-02, docs/step3/results.md): confirmed that agy --json-schema takes inline
JSON, agy -p takes the prompt as its value, agy applies --mode plan in print mode (ck-run still flags
'not supported in print mode'), and claude puts the schema result in 'structured_output'. No flag
needed changing. Codex was not run live (blocked by the merge-gate mod), so its argv stays [assumed].

--live (build_live, 2026-11-02): the same permission flags on each CLI's interactive mode (cursor auto: --force,
agy auto: --mode auto + --add-dir <workspace root> + --add-dir <run>). Initial
prompt, per each CLI's --help: claude [prompt], codex [PROMPT], cursor agent [prompt...], agy -i <prompt>.

Web search (chalach 2026-11-05 ~18:55 CT: "I want to clear the restriction"): every codex argv above (all modes,
headless and --live) also gets -c web_search="live" (CODEX_WEB_CONFIG). Claude's WebSearch/WebFetch are allowed
by its settings file (config/claude-guard.base.json). Cursor and agy have no web flag; ck-run never turned theirs off.

Model and effort (v4, 2026-11-05, BUILD_SPEC_v4 section 6; the runner resolves them with models.resolve):
  claude  --model <opus|sonnet|haiku> [--effort <level>]          (before --permission-mode in --live)
  codex   -m <model> [-c model_reasoning_effort="<level>"]
  cursor  --model <slug>                                         (the effort is in the slug)
  agy     [--model <raw id>] only; never an effort flag             (gemini = agy's default: no flag)
effort is passed only when models.json _verified lists it; guards.check_child_argv admits these tokens only for that
agent, and check_ck_argv refuses them from CK. model=None, effort=None gives the argv of before v4.
"""
from __future__ import annotations

import json
import os
import shutil

from .config import Config, Workspace
from .errors import Refused
from .guards import check_child_argv

AGENTS = ("claude", "codex", "cursor", "agy")
BINARIES = {"claude": "claude", "codex": "codex", "cursor": "agent", "agy": "agy"}
# Where each agent may edit once the user approved it (decisions D1, D2, D4; Claude per the plan).
EDIT_AREAS = {
    "claude": ["local"],
    "codex": ["workspace", "local"],
    "cursor": ["workspace", "local"],
    "agy": ["workspace", "local"],
}
PROMPT_ON_STDIN = {"claude": True, "codex": True, "cursor": False, "agy": False}
SCHEMA_ENFORCED_BY_FLAG = {"claude": True, "codex": True, "cursor": False, "agy": False}
MODES = ("auto", "read", "edit")
AUTO_AREAS = {a: ["workspace", "local"] for a in AGENTS}   # where an auto-mode --write-scope may point
POST_REVIEWED = ("cursor", "agy")  # auto runs of these get the mandatory read-only Claude review (postreview.py)
CODEX_AUTO_MODEL = "gpt-6.1-sol"  # chalach: Astra 6 only for complex tasks (pass --model gpt-6-astra)
CODEX_AUTO_CONFIG = ["-c", 'approval_policy="on-request"', "-c", 'approvals_reviewer="auto_review"']
# Web search (chalach 2026-11-05 ~18:55 CT: "I want to clear the restriction"), every codex run, every mode, headless
# and --live. codex-cli 0.166.0 `codex --help`: "--search  Enable live web search. When enabled, the native Responses
# `web_search` tool is available to the model"; the config key is `web_search` = disabled|cached|indexed|live (checked
# 2026-11-05: `codex debug prompt-input -c web_search="bogus"` -> "unknown variant `bogus`, expected one of
# `disabled`, `cached`, `indexed`, `live` in `web_search`"). `-c` instead of `--search` because `codex exec --help`
# could not be read (the merge-gate mod blocks any `codex exec` command line). The tool runs on OpenAI's side; the
# sandbox network rule (-s, sandbox_workspace_write.network_access=false) still covers shell commands (curl etc.).
CODEX_WEB_CONFIG = ["-c", 'web_search="live"']


class _Prompt:
    def __repr__(self):
        return "<prompt>"


PROMPT = _Prompt()  # placeholder for the prompt text in argv (agents that take it as an argument)


class Command:
    def __init__(self, agent, argv, cwd, stdin_prompt, binary):
        self.agent = agent
        self.argv = argv            # list; may contain PROMPT
        self.cwd = cwd
        self.stdin_prompt = stdin_prompt
        self.binary = binary

    def final_argv(self, prompt_text: str) -> list:
        return [prompt_text if a is PROMPT else a for a in self.argv]


def resolve_binary(cfg: Config, agent: str) -> str:
    name = BINARIES[agent]
    path = shutil.which(name, path=os.pathsep.join(cfg.bin_dirs))
    if not path:
        raise Refused(f"{agent} CLI '{name}' not found in {os.pathsep.join(cfg.bin_dirs)}")
    return path


def build(cfg: Config, ws: Workspace, agent: str, *, mode: str, scopes: list, model: str | None,
          run_dir: str, schema_path: str, extra_read_dirs: list, timeout_s: int,
          claude_settings_path: str, binary: str | None = None, readonly: bool = False,
          effort: str | None = None) -> Command:
    """readonly: an auto run with no write area (runner.auto_readonly_reason, chalach 2026-11-03 19:34 CT). Each
    agent keeps its auto argv; only codex's write area is a flag (-s workspace-write -> -s read-only, approvals
    still routed to auto_review). Claude: the workspace-wide deny rules come from the settings file (runner)."""
    if agent not in AGENTS:
        raise Refused(f"unknown agent '{agent}'; use one of: {', '.join(AGENTS)}")
    if mode not in MODES:
        raise Refused(f"forbidden mode '{mode}'")
    if mode == "edit" and not scopes:
        raise Refused("edit mode needs a validated write scope")
    binary = binary or resolve_binary(cfg, agent)
    with open(schema_path, encoding="utf-8") as f:
        schema_inline = json.dumps(json.load(f), separators=(",", ":"))
    scope_paths = [s["path"] for s in scopes]
    root = scope_paths[0] if scope_paths else ws.root  # auto: first scope, else the workspace
    a = [binary]
    if agent == "claude":
        a += ["-p", "--output-format", "stream-json", "--verbose", "--strict-mcp-config",
              "--json-schema", schema_inline, "--settings", claude_settings_path]
        if mode == "read":
            a += ["--permission-mode", "plan"]
        elif mode == "auto":
            a += ["--permission-mode", "auto"]
            for s in scope_paths:
                a += ["--add-dir", s]
        else:
            a += ["--permission-mode", "acceptEdits", "--allowedTools", "Read,Grep,Glob,Edit,Write,Bash(pytest:*)"]
            for s in scope_paths:
                a += ["--add-dir", s]
        for d in extra_read_dirs:
            a += ["--add-dir", d]
        if model:
            a += ["--model", model]
        a += _effort(agent, effort)
        cwd = ws.root
    elif agent == "codex":
        a += ["exec", "--json", "-o", os.path.join(run_dir, "last.md"), "--output-schema", schema_path]
        if mode == "read":
            a += ["-C", ws.root, "-s", "read-only"]
            cwd = ws.root
        elif mode == "auto" and readonly:  # auto approvals, read-only sandbox: no write area
            a += ["-C", ws.root, "-s", "read-only"] + CODEX_AUTO_CONFIG
            cwd = ws.root
        elif mode == "auto":
            a += ["-C", root, "-s", "workspace-write"] + CODEX_AUTO_CONFIG
            for s in scope_paths[1:]:
                a += ["--add-dir", s]
            cwd = root
        else:
            a += ["-C", scope_paths[0], "-s", "workspace-write",
                  "-c", "sandbox_workspace_write.network_access=false"]
            for s in scope_paths[1:]:
                a += ["--add-dir", s]
            cwd = scope_paths[0]
        a += CODEX_WEB_CONFIG  # live web search in every mode (chalach 2026-11-05)
        if model or mode == "auto":
            a += ["-m", model or CODEX_AUTO_MODEL]
        a += _effort(agent, effort)
        a += ["-"]  # read the prompt from stdin
    elif agent == "cursor":
        a += ["-p", "--output-format", "json", "--trust", "--sandbox", "enabled"]
        if mode == "read":
            a += ["--mode", "plan", "--workspace", ws.root]
            cwd = ws.root
        elif mode == "auto":
            a += ["--force", "--workspace", root]  # allow-all (chalach 2026-11-03) + mandatory Claude review
            for s in scope_paths[1:]:
                a += ["--add-dir", s]
            cwd = root
        else:
            a += ["--workspace", scope_paths[0]]
            for s in scope_paths[1:]:
                a += ["--add-dir", s]
            cwd = scope_paths[0]
        for d in extra_read_dirs:
            a += ["--add-dir", d]
        if model:
            a += ["--model", model]
        a += [PROMPT]
    else:  # agy
        print_timeout = max(30, int(timeout_s) - 15)
        a += ["--output-format", "json", "--sandbox", "--print-timeout", f"{print_timeout}s",
              "--json-schema", schema_inline]
        if mode == "read":
            a += ["--mode", "plan"]
            cwd = ws.root
        elif mode == "auto":
            # chalach 2026-11-03: auto + the workspace root and the run folder as workspace dirs, so agy never asks
            # before reading or editing inside them; raw folders stay guarded by rulecheck + the Claude review
            a += ["--mode", "auto"]
            for s in _unique(scope_paths[1:] + [ws.root, run_dir], skip=root):
                a += ["--add-dir", s]
            cwd = root
        else:
            a += ["--mode", "accept-edits"]
            for s in scope_paths[1:]:
                a += ["--add-dir", s]
            cwd = scope_paths[0]
        for d in extra_read_dirs:
            a += ["--add-dir", d]
        if model:
            a += ["--model", model]
        a += ["-p", PROMPT]
    _assert_invariants(agent, mode, a, readonly=readonly)
    check_child_argv(a, PROMPT, auto=mode == "auto", agent=agent)
    return Command(agent, a, cwd, PROMPT_ON_STDIN[agent], binary)


def build_live(cfg: Config, ws: Workspace, agent: str, *, mode: str, scopes: list, model: str | None,
               run_dir: str, extra_read_dirs: list, claude_settings_path: str, session_id: str | None = None,
               binary: str | None = None, readonly: bool = False, effort: str | None = None) -> Command:
    """argv for --live: the agent's own interactive CLI (TUI), with the prompt as its first message.

    Same permission flags as build(); the print/exec/output/schema flags are dropped (interactive mode has
    none). The run folder is added as a directory so the agent can write result.md and summary.md there.

      claude  --strict-mcp-config --settings <live settings> --session-id <uuid> --add-dir <run> [...]
              [--allowedTools ...] [--model M] --permission-mode plan|acceptEdits <prompt>
              (--permission-mode comes last: --add-dir and --allowedTools take several values and would
              swallow the prompt)
      codex   -C <ws|scope> -s read-only|workspace-write [-c ...network_access=false --add-dir <run>]
              -a on-request [-m M] <prompt>
      cursor  --trust --sandbox enabled [--mode plan | --force (auto)] --workspace <ws|scope> --add-dir <run> [...] <prompt>
      agy     --sandbox --mode plan|accept-edits|auto --add-dir <run> [--add-dir <workspace root> (auto)] [...] -i <prompt>
    """
    if agent not in AGENTS:
        raise Refused(f"unknown agent '{agent}'; use one of: {', '.join(AGENTS)}")
    if mode not in MODES:
        raise Refused(f"forbidden mode '{mode}'")
    if mode == "edit" and not scopes:
        raise Refused("edit mode needs a validated write scope")
    binary = binary or resolve_binary(cfg, agent)
    scope_paths = [s["path"] for s in scopes]
    root = scope_paths[0] if scope_paths else ws.root  # auto: first scope, else the workspace
    a = [binary]
    if agent == "claude":
        a += ["--strict-mcp-config", "--settings", claude_settings_path]
        if session_id:
            a += ["--session-id", session_id]
        for d in [run_dir] + (scope_paths if mode != "read" else []) + list(extra_read_dirs):
            a += ["--add-dir", d]
        if mode == "edit":
            a += ["--allowedTools", "Read,Grep,Glob,Edit,Write,Bash(pytest:*)"]
        if model:
            a += ["--model", model]
        a += _effort(agent, effort)
        a += ["--permission-mode", {"read": "plan", "edit": "acceptEdits", "auto": "auto"}[mode]]
        cwd = ws.root
    elif agent == "codex":
        if mode == "read":
            a += ["-C", ws.root, "-s", "read-only"]  # writes outside the sandbox need the user's approval
            cwd = ws.root
        elif mode == "auto" and readonly:  # no write area: result.md/summary.md go through auto_review approvals
            a += ["-C", ws.root, "-s", "read-only"] + CODEX_AUTO_CONFIG
            cwd = ws.root
        elif mode == "auto":
            a += ["-C", root, "-s", "workspace-write"] + CODEX_AUTO_CONFIG
            for s in scope_paths[1:] + [run_dir]:
                a += ["--add-dir", s]
            cwd = root
        else:
            a += ["-C", scope_paths[0], "-s", "workspace-write",
                  "-c", "sandbox_workspace_write.network_access=false"]
            for s in scope_paths[1:] + [run_dir]:
                a += ["--add-dir", s]
            cwd = scope_paths[0]
        a += CODEX_WEB_CONFIG  # live web search in every mode (chalach 2026-11-05)
        a += ["-a", "on-request"]
        if model or mode == "auto":
            a += ["-m", model or CODEX_AUTO_MODEL]
        a += _effort(agent, effort)
    elif agent == "cursor":
        a += ["--trust", "--sandbox", "enabled"]
        if mode == "read":
            a += ["--mode", "plan", "--workspace", ws.root]
            cwd = ws.root
        elif mode == "auto":
            a += ["--force", "--workspace", root]  # allow-all (chalach 2026-11-03) + mandatory Claude review
            for s in scope_paths[1:]:
                a += ["--add-dir", s]
            cwd = root
        else:
            a += ["--workspace", scope_paths[0]]
            for s in scope_paths[1:]:
                a += ["--add-dir", s]
            cwd = scope_paths[0]
        for d in [run_dir] + list(extra_read_dirs):
            a += ["--add-dir", d]
        if model:
            a += ["--model", model]
    else:  # agy
        a += ["--sandbox", "--mode", {"read": "plan", "edit": "accept-edits", "auto": "auto"}[mode]]
        if mode == "read":
            cwd = ws.root
        else:
            for s in scope_paths[1:]:
                a += ["--add-dir", s]
            cwd = root
        if mode == "auto":  # chalach 2026-11-03: the workspace root and the run folder, so agy never asks inside them
            extra = _unique([ws.root, run_dir] + list(extra_read_dirs), skip=root, already=scope_paths[1:])
        else:
            extra = [run_dir] + list(extra_read_dirs)
        for d in extra:
            a += ["--add-dir", d]
        if model:
            a += ["--model", model]
        a += ["-i"]  # --prompt-interactive: run the prompt, then stay in the session
    a += [PROMPT]
    _assert_invariants(agent, mode, a, live=True, readonly=readonly)
    check_child_argv(a, PROMPT, auto=mode == "auto", agent=agent)
    return Command(agent, a, cwd, False, binary)


def _effort(agent: str, effort: str | None) -> list:
    """The effort flag ck-run injects (claude --effort, codex -c model_reasoning_effort); cursor and agy get none."""
    if not effort:
        return []
    if agent == "claude":
        return ["--effort", effort]
    if agent == "codex":
        return ["-c", f'model_reasoning_effort="{effort}"']
    return []


def _unique(paths: list, skip: str | None = None, already: list | None = None) -> list:
    """paths in order, without duplicates, without `skip` (the cwd) and without those in `already`."""
    out, seen = [], {os.path.normpath(p) for p in (already or [])}
    if skip:
        seen.add(os.path.normpath(skip))
    for p in paths:
        n = os.path.normpath(p)
        if n not in seen:
            seen.add(n)
            out.append(p)
    return out


def _pair(a: list, flag: str, value: str) -> bool:
    return any(a[i] == flag and a[i + 1] == value for i in range(len(a) - 1))


def _assert_invariants(agent: str, mode: str, a: list, live: bool = False, readonly: bool = False) -> None:
    """Defence in depth: refuse if the built argv lacks the expected permission flag."""
    ok = True
    if mode == "auto":
        if agent == "claude":
            ok = _pair(a, "--permission-mode", "auto") and "--settings" in a
        elif agent == "codex":
            ok = (_pair(a, "-s", "read-only" if readonly else "workspace-write")
                  and not (readonly and _pair(a, "-s", "workspace-write"))
                  and _pair(a, "-c", 'approvals_reviewer="auto_review"')
                  and _pair(a, "-c", 'approval_policy="on-request"'))
        elif agent == "cursor":  # allow-all, but still trusted-workspace + sandbox, never the --yolo alias
            ok = ("--trust" in a and _pair(a, "--sandbox", "enabled") and "--force" in a
                  and "--yolo" not in a and "--mode" not in a)
        elif agent == "agy":
            ok = _pair(a, "--mode", "auto") and "--sandbox" in a and "--add-dir" in a
        if not ok:
            raise Refused(f"internal guard: {agent} argv lacks its auto-mode permission flag")
        return
    if agent == "claude":
        ok = _pair(a, "--permission-mode", "plan" if mode == "read" else "acceptEdits") and "--settings" in a
    elif agent == "codex":
        ok = _pair(a, "-s", "read-only" if mode == "read" else "workspace-write")
        if live:
            ok = ok and _pair(a, "-a", "on-request")
    elif agent == "cursor":
        ok = ("--trust" in a and _pair(a, "--sandbox", "enabled") and (_pair(a, "--mode", "plan") == (mode == "read"))
              and "--force" not in a)
    elif agent == "agy":
        ok = _pair(a, "--mode", "plan" if mode == "read" else "accept-edits") and "--sandbox" in a
    if not ok:
        raise Refused(f"internal guard: {agent} argv lacks its {mode}-mode permission flag")


def redacted(cmd: Command, schema_path: str, prompt_path: str) -> str:
    """Shell-like one-liner for cmd.txt, without the prompt body or the inline schema."""
    import shlex
    parts = []
    skip_next_schema = False
    for x in cmd.argv:
        if x is PROMPT:
            parts.append(f'"$(cat {shlex.quote(prompt_path)})"')
        elif skip_next_schema:
            parts.append(f"'<schema {schema_path}>'")
            skip_next_schema = False
        else:
            parts.append(shlex.quote(str(x)))
            if x == "--json-schema":
                skip_next_schema = True
    line = f"cd {shlex.quote(cmd.cwd)} && " + " ".join(parts)
    if cmd.stdin_prompt:
        line += f" < {shlex.quote(prompt_path)}"
    return line
