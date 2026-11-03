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
        r"(^|[\s/'\"])\.env(\.[A-Za-z]+)?($|[\s'\"])|_history\b", re.I)),
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
STDERR_PATTERNS = [
    ("mode_ignored", re.compile(r"not supported in print mode|continuing in the default mode", re.I)),
    ("permission_bypass", re.compile(r"auto-approving all tool permissions", re.I)),
]


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


def classify(cfg: Config, ws: Workspace, mode: str, scopes: list, changed: list, run_dir: str) -> dict:
    """violations: hard rule broken. flags: needs a human look. notes: folder-mtime noise (often OneDrive)."""
    violations, flags, notes, in_scope = [], [], [], []
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
        if c["dir"]:
            # A folder's own mtime moves when entries are added, removed or renamed (also by OneDrive sync).
            if raw:
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
            violations.append(f"file changed during a read-only run: {p}")
        elif mode == "auto" and not scoped:
            violations.append(f"file changed outside the auto-mode write area: {p}")
        elif not scoped:
            violations.append(f"file changed outside the write scope: {p}")
        else:
            in_scope.append(p)
    return {"violations": violations, "flags": flags, "notes": notes, "changed_in_scope": sorted(set(in_scope))}


def scan_transcript(parsed: dict, stdout: str, stderr: str) -> dict:
    """Actions (commands, tool inputs) count as attempts -> violation. Free text only -> flag."""
    violations, flags = [], []
    if parsed.get("actions"):
        for a in parsed["actions"]:
            text = f"{a.get('tool')}: {a.get('input')}"
            for cat, rx in PATTERNS:
                m = rx.search(text)
                if m:
                    violations.append(f"{cat} in agent action: {_excerpt(text, m)}")
    elif stdout:
        seen = set()
        for cat, rx in PATTERNS:
            for m in rx.finditer(stdout):
                key = (cat, m.group(0).lower())
                if key in seen:
                    continue
                seen.add(key)
                flags.append(f"{cat} mentioned in output text (no tool log to confirm): {_excerpt(stdout, m)}")
                if len(seen) > 20:
                    break
    for cat, rx in STDERR_PATTERNS:
        m = rx.search(stderr or "")
        if m:
            msg = f"{cat} on stderr: {_excerpt(stderr, m)}"
            (violations if cat == "permission_bypass" else flags).append(msg)
    return {"violations": violations, "flags": flags}


def _excerpt(text: str, m, width: int = 60) -> str:
    a, b = max(0, m.start() - width), min(len(text), m.end() + width)
    return text[a:b].replace("\n", " ").strip()[:200]
