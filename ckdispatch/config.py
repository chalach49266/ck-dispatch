"""Load config/workspaces.json. Paths are expanded with the current HOME."""
from __future__ import annotations

import json
import os

from .errors import Refused

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DEFAULT_CONFIG = os.path.join(REPO, "config", "workspaces.json")
SCHEMA_DIR = os.path.join(REPO, "schema")
RESULT_SCHEMA = os.path.join(SCHEMA_DIR, "result.schema.json")
PACKET_SCHEMA = os.path.join(SCHEMA_DIR, "packet.schema.json")
CLAUDE_GUARD_BASE = os.path.join(REPO, "config", "claude-guard.base.json")


def xp(p: str) -> str:
    """Expand ~ and normalise (no symlink resolution)."""
    return os.path.normpath(os.path.expanduser(p))


def real(p: str) -> str:
    return os.path.realpath(xp(p))


def inside(child: str, parent: str) -> bool:
    """True if child == parent or child is below parent (plain string paths)."""
    child, parent = child.rstrip("/") or "/", parent.rstrip("/") or "/"
    return child == parent or child.startswith(parent + "/") or parent == "/"


def overlap(a: str, b: str) -> bool:
    return inside(a, b) or inside(b, a)


class Entry:
    """A folder or file named relative to a workspace root, with its literal and real path."""

    def __init__(self, root: str, rel: str):
        self.rel = rel.rstrip("/")
        self.literal = os.path.normpath(os.path.join(root, self.rel))
        self.real = os.path.realpath(self.literal)

    def matches(self, path_literal: str, path_real: str) -> bool:
        return inside(path_literal, self.literal) or inside(path_real, self.real) or inside(path_literal, self.real)

    def overlaps(self, path_literal: str, path_real: str) -> bool:
        return (overlap(path_literal, self.literal) or overlap(path_real, self.real)
                or overlap(path_literal, self.real) or overlap(path_real, self.literal))


class Workspace:
    def __init__(self, key: str, d: dict):
        self.key = key
        self.name = d["name"]
        self.root = xp(d["root"])
        self.real_root = os.path.realpath(self.root)
        self.aliases = {a.lower() for a in d.get("aliases", [])} | {key.lower(), self.name.lower()}
        self.wiki_first = [os.path.join(self.root, p) for p in d.get("wiki_first", [])]
        oq = d.get("open_questions")
        self.open_questions = os.path.join(self.root, oq) if oq else None
        self.rule_files = [os.path.join(self.root, p) for p in d.get("rule_files", [])]
        self.raw_source = d.get("raw_source", "")
        self.raw = [Entry(self.root, r) for r in d.get("raw_readonly", [])]
        self.extra = [Entry(self.root, r) for r in d.get("extra_protected_dirs", [])]
        self.writable_only = [Entry(self.root, r) for r in d.get("writable_only", []) or []]
        self.protected_files = [Entry(self.root, r) for r in d.get("protected_files", [])]

    @property
    def protected_dirs(self) -> list:
        return self.raw + self.extra


class Config:
    def __init__(self, path: str | None = None):
        path = path or os.environ.get("CK_DISPATCH_CONFIG") or DEFAULT_CONFIG
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError) as e:
            raise Refused(f"cannot read config {path}: {e}")
        self.path = os.path.realpath(path)
        self.raw = d  # the whole file, for keys without an attribute below (read only)
        self.is_default = self.path == os.path.realpath(DEFAULT_CONFIG)
        self.home = xp("~")
        self.real_home = os.path.realpath(self.home)
        self.onedrive_root = xp(d["onedrive_root"])
        self.local_root = xp(d["local_root"])
        self.real_local_root = os.path.realpath(self.local_root)
        self.runs_dir = xp(d["runs_dir"])
        self.bin_dirs = [xp(p) for p in d["bin_dirs"]]
        self.local_protected = [os.path.realpath(os.path.join(self.local_root, n)) for n in d.get("local_protected", [])]
        self.local_protected.append(os.path.realpath(REPO))
        self.default_timeout_s = int(d.get("default_timeout_s", 1200))
        self.max_timeout_s = int(d.get("max_timeout_s", 7200))
        # max_runs_per_workspace is gone (2026-11-03): no per-workspace run limit; an old key is ignored
        self.scan_budget_s = float(d.get("scan_budget_s", 120))
        self.scan_max_entries = int(d.get("scan_max_entries", 1000000))
        lv = d.get("live") or {}
        self.live_default = bool(lv.get("default", True))
        self.live_osascript = xp(lv.get("osascript", "/usr/bin/osascript"))
        self.live_terminal = str(lv.get("terminal", "Terminal"))  # "iTerm2" or "Terminal"
        self.live_start_timeout_s = float(lv.get("start_timeout_s", 30))
        self.live_early_exit_s = float(lv.get("early_exit_s", 20))
        self.live_poll_s = float(lv.get("poll_s", 1.0))
        # v4 (2026-11-05, BUILD_SPEC_v4 10.5): the stall watchdog in live.wait_done (runner passes `watch` when on),
        # stall_s when the run has no ask (an ask's size sets it), caffeinate while a live run waits, and closing
        # the tab of a cleanly finished run after close_tab_after_s (0 = never)
        self.live_watchdog = bool(lv.get("watchdog", True))
        self.live_stall_s = float(lv.get("stall_s", 300))
        self.live_keep_awake = bool(lv.get("keep_awake", True))
        self.live_close_tab_after_s = float(lv.get("close_tab_after_s", 600))
        # read-only Claude review after cursor/agy auto runs (postreview.py). v4: OFF unless post_review.enabled is
        # true (chalach 2026-11-05: "no need review, since those one are for simple stupid task anyways")
        rv = d.get("post_review") or {}
        self.post_review_enabled = bool(rv.get("enabled", False))
        self.review_timeout_s = float(rv.get("timeout_s", 300))
        self.review_model = rv.get("model") or None
        # v4 asks (asks.py): required = run/review need --ask; budgets per size (code defaults in asks.DEFAULT_BUDGETS)
        ak = d.get("asks") or {}
        self.asks_required = bool(ak.get("required", False))
        self.asks_budgets = {s: dict(v) for s, v in (ak.get("budgets") or {}).items() if isinstance(v, dict)}
        self.workspaces = {k: Workspace(k, v) for k, v in d["workspaces"].items()}

    def workspace(self, name: str) -> Workspace:
        n = (name or "").strip().lower()
        for ws in self.workspaces.values():
            if n in ws.aliases:
                return ws
        raise Refused(f"unknown workspace '{name}'; use one of: {', '.join(self.workspaces)}")
