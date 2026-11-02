"""Per-agent argv map. There is no code path that emits a forbidden flag.

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

[assumed] items, to confirm in step 3 (live checks): agy --json-schema takes inline JSON; agy -p takes
the prompt as its value; agy honours --mode in print mode (ck-run flags the run if agy prints
'not supported in print mode'); claude puts the schema result in 'structured_output'.
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
          claude_settings_path: str, binary: str | None = None) -> Command:
    if agent not in AGENTS:
        raise Refused(f"unknown agent '{agent}'; use one of: {', '.join(AGENTS)}")
    if mode not in ("read", "edit"):
        raise Refused(f"forbidden mode '{mode}'")
    if mode == "edit" and not scopes:
        raise Refused("edit mode needs a validated write scope")
    binary = binary or resolve_binary(cfg, agent)
    with open(schema_path, encoding="utf-8") as f:
        schema_inline = json.dumps(json.load(f), separators=(",", ":"))
    scope_paths = [s["path"] for s in scopes]
    a = [binary]
    if agent == "claude":
        a += ["-p", "--output-format", "stream-json", "--verbose", "--strict-mcp-config",
              "--json-schema", schema_inline, "--settings", claude_settings_path]
        if mode == "read":
            a += ["--permission-mode", "plan"]
        else:
            a += ["--permission-mode", "acceptEdits", "--allowedTools", "Read,Grep,Glob,Edit,Write,Bash(pytest:*)"]
            for s in scope_paths:
                a += ["--add-dir", s]
        for d in extra_read_dirs:
            a += ["--add-dir", d]
        if model:
            a += ["--model", model]
        cwd = ws.root
    elif agent == "codex":
        a += ["exec", "--json", "-o", os.path.join(run_dir, "last.md"), "--output-schema", schema_path]
        if mode == "read":
            a += ["-C", ws.root, "-s", "read-only"]
            cwd = ws.root
        else:
            a += ["-C", scope_paths[0], "-s", "workspace-write",
                  "-c", "sandbox_workspace_write.network_access=false"]
            for s in scope_paths[1:]:
                a += ["--add-dir", s]
            cwd = scope_paths[0]
        if model:
            a += ["-m", model]
        a += ["-"]  # read the prompt from stdin
    elif agent == "cursor":
        a += ["-p", "--output-format", "json", "--trust", "--sandbox", "enabled"]
        if mode == "read":
            a += ["--mode", "plan", "--workspace", ws.root]
            cwd = ws.root
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
    _assert_invariants(agent, mode, a)
    check_child_argv(a, PROMPT)
    return Command(agent, a, cwd, PROMPT_ON_STDIN[agent], binary)


def _pair(a: list, flag: str, value: str) -> bool:
    return any(a[i] == flag and a[i + 1] == value for i in range(len(a) - 1))


def _assert_invariants(agent: str, mode: str, a: list) -> None:
    """Defence in depth: refuse if the built argv lacks the expected permission flag."""
    ok = True
    if agent == "claude":
        ok = _pair(a, "--permission-mode", "plan" if mode == "read" else "acceptEdits") and "--settings" in a
    elif agent == "codex":
        ok = _pair(a, "-s", "read-only" if mode == "read" else "workspace-write")
    elif agent == "cursor":
        ok = "--trust" in a and _pair(a, "--sandbox", "enabled") and (_pair(a, "--mode", "plan") == (mode == "read"))
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
