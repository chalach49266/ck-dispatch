"""After a run: what changed on disk (metadata only, no file reads) and what the transcript shows.

The disk scan compares mtimes with a marker touched just before the agent started. It never
opens files, so it does not download OneDrive cloud-only files. It does not follow symlinks.
"""
from __future__ import annotations

import os
import re
import time

from .config import Config, Workspace, inside
from .guards import HEAVY_NAMES

IGNORE_NAMES = {".DS_Store", "Icon\r", ".localized"}
IGNORE_DIRS = {".obsidian"}  # the Obsidian app writes here while it is open

PATTERNS = [
    ("credential", re.compile(
        r"\.ssh/|\.gnupg|\.aws/|\.netrc|\.pgpass|\.grokbot/|\.tidy3d/|\.codex/auth\.json|"
        r"\.claude/\.credentials|Library/Keychains|\bid_(rsa|ed25519|ecdsa)\b|"
        r"security\s+(find|dump)-(generic|internet)-password|\bprintenv\b|local-exec-daemon-credential|"
        r"(^|[\s/'\"])\.env(\.[A-Za-z]+)?($|[\s'\"])|_history\b|"
        r"\bexport\s+[A-Za-z_]\w*_KEY\s*=|"
        r"\b(?:api[_-]?key|access[_-]?token|token)\s*['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_./+-]{16,}|"
        r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{20,}", re.I)),
    ("cloud_sim", re.compile(r"\bweb\.(run|upload|start|run_async)\s*\(|tidy3d\.web\b|\bBatch\s*\(|flexcompute", re.I)),
    ("push_send", re.compile(
        r"\bgit\s+push\b|\bgh\s+(pr|release|repo|api|gist|issue)\b|\bwrangler\s+(deploy|publish|pages)\b|"
        r"\bcurl\s|\bwget\s|\bscp\s|\brsync\s|\bsendmail\b|\bmail\s+-s\b", re.I)),
    ("heavy_install", re.compile(
        r"\bgit\s+clone\b|-m\s+venv\b|\bvirtualenv\b|\bpip3?\s+install\b|\bnpm\s+(install|i|ci)\b|"
        r"\bconda\s+(create|install)\b|\buv\s+(venv|pip\s+install|sync)\b", re.I)),
    ("permission_bypass", re.compile(
        r"auto-approving all tool permissions|bypassPermissions|dangerously-skip-permissions|--yolo\b", re.I)),
]
# Output-text mentions (no tool log) of these categories stay FLAGS (exit 4): safety carve-out by the Grok Bot
# stand-in when chalach chose (2026-11-04 ~23:00 CT) "treat a word only mention as note". Every other category
# (push_send, heavy_install, any new one) mentioned only in text is a NOTE.
TEXT_FLAG_CATEGORIES = {"credential", "cloud_sim", "permission_bypass"}
TEXT_FLAG = "mentioned in output text (no tool log to confirm)"  # runner.py matches this text
TEXT_NOTE = "mentioned in output text only (not a command)"
# chalach 2026-11-04 ~23:00 CT (via the Grok Bot stand-in): "you can always allow those git push and pip install".
# In an agent ACTION these are a note, never a violation or flag (rule check and post-run review). Edit this list
# to change the rule; everything else in push_send / heavy_install (gh, wrangler, curl, wget, scp, rsync, mail,
# git clone, venv, npm, conda, uv venv/sync) is unchanged.
ALLOWED_ACTIONS = [
    ("git push", re.compile(r"\bgit\s+push\b", re.I)),
    ("pip install (pip, pip3, python -m pip, uv pip)",
     re.compile(r"(?:\bpython(?:3(?:\.\d+)?)?\s+-m\s+|\buv\s+|\b)pip3?\s+install\b", re.I)),
]
ALLOWED_NOTE = "allowed by chalach 2026-11-04"
# chalach 2026-11-05 ~18:55 CT (via Grok Bot): "I want to clear the restriction" (web search for agent deployments).
# The agents' BUILT-IN web search / fetch tool calls are allowed. Their input is a search query or a URL, not a
# command or a file access, so a pattern in it (a search for "tidy3d.web.run docs", "pip install nidaqmx", ".env
# syntax") is a note, never a violation or flag (rule check and post-run review). Names checked 2026-11-05: claude
# WebSearch / WebFetch (tool_use), codex exec item type web_search, cursor WebSearch / WebFetch (its transcripts), agy
# search_web (the tool list of its ck-run sessions). MCP fetch tools and shell commands (curl, wget, scp, rsync, ssh)
# are NOT web tools here and keep every rule.
WEB_TOOLS = {"WebSearch", "WebFetch", "web_search", "search_web"}
WEB_NOTE = "web tool call, allowed by chalach 2026-11-05"
# agy print mode on chalach's account (2026-11-03): "--mode auto is not available for this account; continuing in
# the default mode". For an agy auto run that is expected (default mode is stricter than auto): a note, not a flag.
AGY_AUTO_FALLBACK = re.compile(r"--mode auto is not available for this account", re.I)
STDERR_PATTERNS = [
    ("mode_ignored", re.compile(r"not supported in print mode|continuing in the default mode", re.I)),
    ("permission_bypass", re.compile(r"auto-approving all tool permissions", re.I)),
]
# Only the ambiguous word alternatives may be ignored in output paths. Credential
# locations and call syntax keep their original rules, as do all agent actions.
PATH_WORDS = {("cloud_sim", "flexcompute"), ("cloud_sim", "tidy3d.web"), ("credential", "_history")}
PATH_TOKEN = re.compile(
    r"(?P<quote>['\"`])(?P<quoted>(?:/|~/|\.\.?/|[^\s/'\"`]+/)[^'\"`\n]+)(?P=quote)|"
    r"(?P<bare>(?:/|~/|\.\.?/|[\w.-]+/)[^\s'\"`<>{}\[\],;()]+)")


def path_word_mention(category: str, text: str, match) -> bool:
    """An incidental word in a path, never a secret location or cloud command."""
    if (category, match.group().lower()) not in PATH_WORDS:
        return False
    for path_match in PATH_TOKEN.finditer(text):
        group = "quoted" if path_match.group("quoted") is not None else "bare"
        start, end = path_match.span(group)
        if start <= match.start() and match.end() <= end:
            path = path_match.group(group)
            # Shell/interpreter history files remain credential locations.
            if category == "credential" and re.search(r"/\.[^/]*_history\b", path, re.I):
                return False
            # A cloud executable path followed by an operation is still a command.
            if category == "cloud_sim":
                if re.match(r"\.(?:run|upload|start|run_async)\s*\(", text[match.end():], re.I):
                    return False
                if re.match(r"\s+(?:--)?(?:upload|run|start|run_async)\b", text[path_match.end():], re.I):
                    return False
            return True
    return False


def touch_marker(path: str) -> int:
    with open(path, "w") as f:
        f.write("ck-run marker\n")
    time.sleep(0.02)
    return os.stat(path).st_mtime_ns


def scan_roots(ws: Workspace, scopes: list) -> list:
    roots = [ws.root]
    for s in scopes:
        if not inside(s["path"], ws.real_root):
            roots.append(s["path"])
    for e in ws.protected_dirs:
        if os.path.isdir(e.real) and not inside(e.real, ws.real_root):
            roots.append(e.real)  # symlinked raw folders, e.g. repos kept in ~/PhD_local
    out = []
    for r in roots:
        if not any(inside(r, o) for o in out):
            out = [o for o in out if not inside(o, r)] + [r]
    return out


def scan_changed(roots: list, marker_ns: int, budget_s: float, max_entries: int) -> dict:
    t0 = time.time()
    changed, n, truncated = [], 0, False
    for root in roots:
        try:
            st = os.lstat(root)
            if st.st_mtime_ns > marker_ns:
                changed.append({"path": root, "dir": True})
        except OSError:
            continue
        stack = [root]
        while stack and not truncated:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    n += 1
                    if n > max_entries or (n % 2000 == 0 and time.time() - t0 > budget_s):
                        truncated = True
                        break
                    if e.name in IGNORE_NAMES:
                        continue
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        if is_dir and e.name in IGNORE_DIRS:
                            continue
                        mt = e.stat(follow_symlinks=False).st_mtime_ns
                    except OSError:
                        continue
                    new = mt > marker_ns
                    if new:
                        changed.append({"path": e.path, "dir": is_dir, "heavy": e.name in HEAVY_NAMES or e.name == "pyvenv.cfg"})
                    if is_dir and not (new and e.name in HEAVY_NAMES):
                        stack.append(e.path)
    return {"changed": changed, "entries": n, "truncated": truncated, "seconds": round(time.time() - t0, 2)}


def classify(cfg: Config, ws: Workspace, mode: str, scopes: list, changed: list, run_dir: str,
             auto_readonly: str | None = None) -> dict:
    """violations: hard rule broken. flags: needs a human look. notes: folder-mtime noise (often OneDrive).
    auto_readonly: an auto run with no write area (the caller passes mode "read"): every workspace change is a
    violation, and so is a writable folder (e.g. a washu <course>/solutions/) that now exists, since none did."""
    violations, flags, notes, in_scope = [], [], [], []
    ro_why = f"during an auto run with no write area (auto_readonly: {auto_readonly})" if auto_readonly else ""
    for c in changed:
        p = c["path"]
        rp = os.path.realpath(p)
        if inside(p, run_dir) or inside(rp, run_dir) or inside(p, cfg.runs_dir):
            continue
        in_od = inside(p, cfg.onedrive_root)
        if c.get("heavy") and in_od:
            violations.append(f"new {os.path.basename(p)} inside OneDrive: {p}")
            continue
        raw = next((e for e in ws.protected_dirs if e.matches(p, rp)), None)
        pfile = next((e for e in ws.protected_files if p == e.literal or rp == e.real), None)
        if mode == "auto" and not scopes:  # auto without --write-scope: the workspace, minus raw/protected
            scoped = inside(p, ws.root) or inside(rp, ws.real_root)
        else:
            scoped = mode in ("edit", "auto") and any(inside(rp, s["path"]) or inside(p, s["literal"]) for s in scopes)
        if scoped and ws.writable_only and (inside(p, ws.root) or inside(rp, ws.real_root)) \
                and not any(inside(rp, e.real) for e in ws.writable_only):
            scoped = False  # writable-only workspace (nv: NV Project, AQS; washu: <course>/solutions, <course>/prep)
        if c["dir"]:
            # A folder's own mtime moves when entries are added, removed or renamed (also by OneDrive sync).
            made = next((e for e in ws.writable_only if p == e.literal or rp == e.real), None) if auto_readonly else None
            if made:
                violations.append(f"writable folder {made.rel}/ created {ro_why}: {p}")
            elif raw:
                flags.append(f"folder changed in read-only '{raw.rel}' (entry added/removed/renamed, or OneDrive sync): {p}")
            elif mode == "read":
                notes.append(f"folder mtime changed during a read-only run (entry added/removed, or OneDrive sync): {p}")
            elif not scoped:
                notes.append(f"folder mtime changed outside the write scope (entry added/removed, or OneDrive sync): {p}")
            continue
        if raw:
            violations.append(f"file changed in read-only folder '{raw.rel}': {p}")
        elif pfile:
            violations.append(f"protected rule file changed: {p}")
        elif mode == "read":
            violations.append(f"file changed {ro_why}: {p}" if ro_why else f"file changed during a read-only run: {p}")
        elif mode == "auto" and not scoped:
            violations.append(f"file changed outside the auto-mode write area: {p}")
        elif not scoped:
            violations.append(f"file changed outside the write scope: {p}")
        else:
            in_scope.append(p)
    return {"violations": violations, "flags": flags, "notes": notes, "changed_in_scope": sorted(set(in_scope))}


def is_web_tool(tool) -> bool:
    """An agent's built-in web search / fetch tool (WEB_TOOLS; chalach 2026-11-05)."""
    return str(tool or "") in WEB_TOOLS


def allowed(text: str, m) -> str | None:
    """Name of the ALLOWED_ACTIONS entry whose match in text covers match m (e.g. 'git push'), else None."""
    for name, arx in ALLOWED_ACTIONS:
        if any(a.start() <= m.start() and m.end() <= a.end() for a in arx.finditer(text)):
            return name
    return None


def scan_transcript(parsed: dict, stdout: str, stderr: str, agent: str | None = None, mode: str | None = None) -> dict:
    """Actions (commands, tool inputs) count as attempts -> violation, except ALLOWED_ACTIONS -> note and web
    tool calls (WEB_TOOLS, chalach 2026-11-05) -> note. Free text only (no tool log) -> note, or flag for
    TEXT_FLAG_CATEGORIES."""
    violations, flags, notes = [], [], []
    if parsed.get("actions"):
        for a in parsed["actions"]:
            text = f"{a.get('tool')}: {a.get('input')}"
            if is_web_tool(a.get("tool")):  # a query or URL (chalach 2026-11-05): a pattern in it is only a note
                for cat, rx in PATTERNS:
                    m = rx.search(text)
                    if m:
                        notes.append(f"{WEB_NOTE} ({cat} pattern in the query/URL): {_excerpt(text, m)}")
                continue
            for cat, rx in PATTERNS:
                hits = list(rx.finditer(text))
                bad = next((m for m in hits if not allowed(text, m)), None)
                ok = next((m for m in hits if allowed(text, m)), None)
                if bad:
                    violations.append(f"{cat} in agent action: {_excerpt(text, bad)}")
                if ok:
                    notes.append(f"{ALLOWED_NOTE}: {_excerpt(text, ok)}")
    elif stdout:
        seen = set()
        for cat, rx in PATTERNS:
            for m in rx.finditer(stdout):
                if path_word_mention(cat, stdout, m):
                    continue
                key = (cat, m.group(0).lower())
                if key in seen:
                    continue
                seen.add(key)
                if cat in TEXT_FLAG_CATEGORIES:
                    flags.append(f"{cat} {TEXT_FLAG}: {_excerpt(stdout, m)}")
                else:
                    notes.append(f"{cat} {TEXT_NOTE}: {_excerpt(stdout, m)}")
                if len(seen) > 20:
                    break
    for cat, rx in STDERR_PATTERNS:
        m = rx.search(stderr or "")
        if m:
            msg = f"{cat} on stderr: {_excerpt(stderr, m)}"
            if cat == "mode_ignored" and agent == "agy" and mode == "auto" and AGY_AUTO_FALLBACK.search(stderr or "") \
                    and not re.search(r"not supported in print mode", stderr or "", re.I):
                notes.append("agy print mode: --mode auto is not available for this account, so agy ran in its "
                             "default mode (stricter than auto); the post-run Claude review still checks the run")
                continue
            (violations if cat == "permission_bypass" else flags).append(msg)
    return {"violations": violations, "flags": flags, "notes": notes}


def _excerpt(text: str, m, width: int = 60) -> str:
    a, b = max(0, m.start() - width), min(len(text), m.end() + width)
    return text[a:b].replace("\n", " ").strip()[:200]
