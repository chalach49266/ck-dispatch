"""Hard guards: forbidden permission flags, write-scope checks, and the child environment.

These rules are code, not config, so a config file cannot loosen them.
"""
from __future__ import annotations

import os

from .config import Config, Workspace, inside, overlap
from .errors import Refused

# Any token we hand to an agent CLI (except the prompt itself) that contains one of these
# (case-insensitive) is refused.
FORBIDDEN_SUBSTRINGS = (
    "dangerously",        # --dangerously-skip-permissions, --dangerously-bypass-approvals-and-sandbox, ...-hook-trust
    "skip-permissions",
    "bypasspermissions",  # claude --permission-mode bypassPermissions
    "bypass-approvals",
    "yolo",               # cursor --yolo, omp yolo
    "full-access",        # codex -s danger-full-access
    "full-auto",          # codex --full-auto
    "always-proceed",     # agy tool policy that auto-runs commands
    "approve-for-me",
    "auto-review",        # cursor server-side auto approval
    "approve-mcps",
    "dontask",
    "printenv",           # cursor --printenv dumps the environment
    "remote-control",
    "mic-serve",
    "ignore-rules",
    "auth-token",
    "api-key",
)
FORBIDDEN_FLAGS = {"-f", "--force", "--bare", "-w", "--worktree", "--auto"}
# --mode auto (2026-11-02, chalach approved 10:34 PM CT): each agent's safety-reviewed auto mode. These tokens
# are allowed ONLY in an argv that ck-run itself built for mode auto (check_child_argv(..., auto=True)).
# CK can never pass them to ck-run (check_ck_argv still refuses them); full bypass stays refused always.
AUTO_TOKENS = {"--auto-review"}                                   # cursor Smart Auto (server classifier)
# Allow-all flags per agent (2026-11-03, chalach approved: cursor "allow everything", with a mandatory read-only
# Claude review after every such run). Exact token only, only for that agent, only in an auto argv ck-run built.
# '-f', '--yolo' and every 'dangerously'/'bypass' token stay refused for every agent.
AUTO_AGENT_FLAGS = {"cursor": {"--force"}}
AUTO_MODE_VALUES = {"--permission-mode": {"auto"},                # claude auto mode (classifier)
                    "--mode": {"auto"}}                           # agy auto (prompts on irreversible actions)
AUTO_CONFIG_OVERRIDES = {'approval_policy="on-request"',          # codex: approvals routed to auto review
                         'approvals_reviewer="auto_review"'}      # = --approve-for-me, not the bypass
# Bare words that name a forbidden permission mode.
FORBIDDEN_WORDS = {"auto", "bypasspermissions", "dontask", "yolo", "danger-full-access",
                   "full-access", "full-auto", "always-proceed"}
# Allowed values for permission/sandbox flags (allowlist; anything else is refused).
MODE_VALUES = {
    "--permission-mode": {"plan", "acceptedits"},
    "--mode": {"plan", "ask", "accept-edits"},
    "-s": {"read-only", "workspace-write"},
    "--sandbox": {"read-only", "workspace-write", "enabled"},
    # codex approvals: only "on-request" (the model must ask the user). Needed for --live, because the
    # user's ~/.codex/config.toml sets approval_policy = "never". "never" stays refused.
    "-a": {"on-request"},
    "--ask-for-approval": {"on-request"},
    "--approval-mode": set(),
    "--approval-policy": set(),
}
CONFIG_FLAGS = {"-c", "--config"}
ALLOWED_CONFIG_OVERRIDES = {"sandbox_workspace_write.network_access=false"}
MODEL_FLAGS = {"--model", "-m"}

# ck-run's own options whose value is free text or a path (not checked for bare words).
CK_FREE_TEXT = {"--title", "--note", "--focus", "--approved", "--prompt-file", "--write-scope", "--claim", "--lead"}
# Permission flags CK may never pass to ck-run: ck-run sets them itself.
CK_PERMISSION_FLAGS = {"--permission-mode", "--sandbox", "-s", "--approval-mode", "--approval-policy",
                       "-a", "--ask-for-approval", "-c", "--config", "--settings", "--allowedtools",
                       "--allowed-tools", "--add-dir"}

HEAVY_NAMES = {".git", ".venv", "venv", "node_modules", "site-packages", "__pypackages__"}


def _bad_substring(tok: str) -> str | None:
    low = tok.lower()
    for s in FORBIDDEN_SUBSTRINGS:
        if s in low:
            return s
    return None


def check_ck_argv(argv: list) -> None:
    """Refuse forbidden permission flags or words in ck-run's own command line."""
    i = 0
    while i < len(argv):
        t = str(argv[i])
        low = t.lower()
        if low.startswith("-"):
            name, eq, val = low.partition("=")
            if name in FORBIDDEN_FLAGS:
                raise Refused(f"forbidden flag {t}")
            bad = _bad_substring(name)
            if bad:
                raise Refused(f"forbidden flag {t}")
            if name in CK_PERMISSION_FLAGS:
                raise Refused(f"{t} is not accepted: ck-run sets agent permissions itself (use --mode auto|read|edit)")
            value = val if eq else (str(argv[i + 1]) if i + 1 < len(argv) else None)
            if name == "--mode" and value is not None:
                if value.lower() not in ("read", "edit", "auto"):
                    raise Refused(f"forbidden mode '{value}': ck-run accepts only --mode auto, read or edit")
                if not eq:
                    i += 2  # ck-run's own --mode auto is not the bare forbidden word 'auto'
                    continue
            if name in MODEL_FLAGS and value is not None:
                if _bad_substring(value) or value.lower() in (FORBIDDEN_WORDS - {"auto"}):
                    raise Refused(f"forbidden model value '{value}'")
            if not eq and (name in CK_FREE_TEXT or name in MODEL_FLAGS):
                i += 2
                continue
        elif low in FORBIDDEN_WORDS:
            raise Refused(f"forbidden permission word '{t}'")
        elif _bad_substring(low) and not os.path.sep in t:
            raise Refused(f"forbidden permission word '{t}'")
        i += 1


def check_child_argv(argv: list, prompt_sentinel: object, auto: bool = False, agent: str | None = None) -> None:
    """Final check on the exact argv handed to an agent CLI. The prompt slot is skipped.

    auto=True only from flags.build/build_live for ck-run's own --mode auto: it admits exactly the
    AUTO_* tokens above (and AUTO_AGENT_FLAGS of `agent`) and nothing else; every bypass check still applies."""
    n = len(argv)
    agent_flags = AUTO_AGENT_FLAGS.get(agent or "", set()) if auto else set()
    for i, tok in enumerate(argv):
        if tok is prompt_sentinel:
            continue
        t = str(tok)
        low = t.lower()
        prev = str(argv[i - 1]).lower() if i else ""
        if t in agent_flags:  # exact, case-sensitive: '--force' for cursor auto
            continue
        if auto and (low in AUTO_TOKENS or (low in {v for s in AUTO_MODE_VALUES.values() for v in s}
                                            and prev in AUTO_MODE_VALUES)):
            continue
        bad = _bad_substring(low)
        if bad:
            raise Refused(f"forbidden token in agent argv: {t[:80]} (contains '{bad}')")
        if low.startswith("-"):
            name_orig, eq, _ = t.partition("=")
            val = low.partition("=")[2]
            # short flags are case-sensitive (codex -C <dir> is not -c <config>); long flags compare lower-case
            name = name_orig if len(name_orig) == 2 else name_orig.lower()
            if name in FORBIDDEN_FLAGS:
                raise Refused(f"forbidden flag in agent argv: {t}")
            nxt = None
            if eq:
                nxt = val
            elif i + 1 < n and argv[i + 1] is not prompt_sentinel and not str(argv[i + 1]).startswith("-"):
                nxt = str(argv[i + 1]).lower()
            allowed_vals = MODE_VALUES.get(name, set()) | (AUTO_MODE_VALUES.get(name, set()) if auto else set())
            if name in MODE_VALUES and nxt is not None and nxt not in allowed_vals:
                raise Refused(f"forbidden mode in agent argv: {name} {nxt}")
            overrides = ALLOWED_CONFIG_OVERRIDES | (AUTO_CONFIG_OVERRIDES if auto else set())
            if name in CONFIG_FLAGS and (nxt is None or nxt not in overrides):
                raise Refused(f"config override not allowed in agent argv: {t} {nxt}")
        elif low in FORBIDDEN_WORDS and prev not in MODEL_FLAGS:
            raise Refused(f"forbidden permission word in agent argv: {t}")


def nearest_git_root(path: str) -> str | None:
    p = path
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def validate_write_scopes(cfg: Config, ws: Workspace, agent: str, scopes: list, edit_areas: dict) -> list:
    """Return [{'path': real, 'literal': p, 'area': 'workspace'|'local'}] or raise Refused."""
    if not scopes:
        raise Refused("edit mode needs at least one --write-scope folder")
    allowed_areas = edit_areas.get(agent, [])
    if not allowed_areas:
        raise Refused(f"{agent} may not edit")
    out = []
    for s in scopes:
        p = os.path.expanduser(str(s))
        if not os.path.isabs(p):
            p = os.path.join(ws.root, p)
        p = os.path.normpath(p)
        if not os.path.isdir(p):
            raise Refused(f"write scope is not an existing folder: {p}")
        rp = os.path.realpath(p)
        parts = set(rp.split(os.sep)) | set(p.split(os.sep))
        heavy = parts & HEAVY_NAMES
        if heavy:
            raise Refused(f"write scope is inside a {sorted(heavy)[0]} folder: {p}")
        if inside(cfg.real_home, rp) or inside(cfg.home, p):
            raise Refused(f"write scope is your home folder or above it: {p}")
        if inside(rp, ws.real_root):  # judged by the real path, so a symlink cannot smuggle a scope in
            area = "workspace"
        elif inside(rp, cfg.real_local_root):
            area = "local"
        else:
            raise Refused(f"write scope must be inside {ws.name} or ~/PhD_local: {p}")
        if area not in allowed_areas:
            where = "the workspace" if area == "workspace" else "~/PhD_local"
            raise Refused(f"{agent} may not edit in {where} (allowed: {', '.join(allowed_areas)}): {p}")
        if area == "workspace":
            if rp == ws.real_root:
                raise Refused(f"write scope may not be the whole workspace root; pick a subfolder: {p}")
            for e in ws.protected_dirs:
                if e.overlaps(p, rp):
                    raise Refused(f"write scope overlaps the read-only folder '{e.rel}' ({ws.raw_source or 'ck-run protected list'}): {p}")
            # real path only: a symlink inside a writable folder cannot point the scope at read-only files
            if ws.writable_only and not any(inside(rp, e.real) for e in ws.writable_only):
                names = ", ".join(e.rel for e in ws.writable_only)
                raise Refused(f"in {ws.name} agents may only write inside {names} ({ws.raw_source}): {p}")
        else:
            if rp == cfg.real_local_root:
                raise Refused("write scope may not be ~/PhD_local itself; pick a repo folder")
            for prot in cfg.local_protected:
                if overlap(rp, prot):
                    raise Refused(f"write scope overlaps a protected ~/PhD_local folder ({prot}): {p}")
            for other in cfg.workspaces.values():
                for e in other.protected_dirs:
                    if e.overlaps(p, rp):
                        raise Refused(f"write scope overlaps the read-only folder '{e.rel}' of {other.name}: {p}")
            g = nearest_git_root(rp)
            if g is None or os.path.realpath(g) == cfg.real_home or not inside(os.path.realpath(g), cfg.real_local_root):
                raise Refused(f"a ~/PhD_local write scope must sit in its own git repo (your home folder is a git repo): {p}")
        if not any(o["path"] == rp for o in out):
            out.append({"path": rp, "literal": p, "area": area})
    return out


ENV_ALLOW = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "SHELL")


def child_env(cfg: Config) -> dict:
    """Clean environment for an agent: an allowlist plus a fixed PATH. No keys, no tokens."""
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    env["PATH"] = os.pathsep.join(cfg.bin_dirs)
    env["NO_COLOR"] = "1"
    return env
