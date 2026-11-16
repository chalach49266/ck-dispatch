"""Routing suggestion (Framework v2 §4 matrix plus the agy lanes) and rate-limit state.

parse_reset() is ported from AgwaB/grok-bot-agents skills/delegate-task/scripts/route.py
at commit 0aa87fc (MIT License, Copyright (c) 2026 AgwaB). See LICENSE-THIRD-PARTY.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re

UTC = dt.timezone.utc
AGENTS = ("claude", "codex", "cursor", "agy")
# task type -> (primary, cross-check). Model IDs stay [assumed] until `agent --list-models` / `agy models`.
MATRIX = {
    "wiki_synthesis": ("claude", "agy"),
    "python_debug": ("codex", "claude"),
    "refactor": ("claude", "codex"),
    "fdtd_review": ("claude", "codex"),
    "microwave_review": ("claude", "codex"),
    "slides_web": ("claude", "codex"),
    "writing": ("claude", "agy"),
    "quick_lookup": ("codex", None),
    "multi_file_diff": ("cursor", "claude"),
    "wiki_lint": ("agy", "claude"),
    "general": ("claude", "codex"),
}
RATE_LIMIT_RX = re.compile(r"rate.?limit|usage limit|too many requests|\b429\b|quota exceeded|hit your (usage )?limit", re.I)


def now():
    o = os.environ.get("CK_ROUTE_NOW")  # tests only
    return dt.datetime.fromisoformat(o) if o else dt.datetime.now(UTC)


def state_path(runs_dir: str) -> str:
    return os.path.join(runs_dir, "state.json")


def load_state(runs_dir: str) -> dict:
    try:
        with open(state_path(runs_dir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(runs_dir: str, st: dict) -> None:
    os.makedirs(runs_dir, mode=0o700, exist_ok=True)
    tmp = f"{state_path(runs_dir)}.{os.getpid()}.tmp"  # per process: concurrent runs never share a temp file
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, state_path(runs_dir))


@contextlib.contextmanager
def _state_lock(runs_dir: str):
    """Read-modify-write of state.json by concurrent runs: one at a time (milliseconds)."""
    os.makedirs(runs_dir, mode=0o700, exist_ok=True)
    fd = os.open(state_path(runs_dir) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def parse_reset(msg, ref=None):
    """Best-effort reset time from a provider message. Falls back to ref + 1 h. (ported, MIT)"""
    ref = ref or now()
    m = msg or ""
    r = re.search(r"retry-after-ms=(\d+)", m)
    if r:
        return ref + dt.timedelta(milliseconds=int(r.group(1)))
    r = re.search(r"reset(?:s)? at (\d{1,2})-(\d{1,2}) (\d{1,2}):(\d{2})(?::(\d{2}))? UTC", m)
    if r:
        mo, d, h, mi, s = (int(x or 0) for x in r.groups())
        t = dt.datetime(ref.year, mo, d, h, mi, s, tzinfo=UTC)
        return t if t > ref else t.replace(year=ref.year + 1)
    r = re.search(r"resets? (?:at )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*\(([^)]+)\)", m, re.I)
    if r:
        from zoneinfo import ZoneInfo
        h = int(r.group(1)) % 12 if r.group(3) else int(r.group(1))
        mi = int(r.group(2) or 0)
        if (r.group(3) or "").lower() == "pm":
            h += 12
        try:
            tz = ZoneInfo(r.group(4).strip())
        except Exception:
            tz = UTC
        loc = ref.astimezone(tz)
        t = loc.replace(hour=h, minute=mi, second=0, microsecond=0)
        if t <= loc:
            t += dt.timedelta(days=1)
        return t.astimezone(UTC)
    return ref + dt.timedelta(hours=1)


def mark_limited(runs_dir: str, agent: str, message: str) -> dict:
    t = parse_reset(message)
    with _state_lock(runs_dir):
        st = load_state(runs_dir)
        st.setdefault("rate_limited", {})[agent] = {"until": t.astimezone(UTC).isoformat(), "message": (message or "")[:300]}
        save_state(runs_dir, st)
    return st["rate_limited"][agent]


def limited(st: dict, agent: str):
    x = (st.get("rate_limited") or {}).get(agent)
    return x if x and dt.datetime.fromisoformat(x["until"]) > now() else None


def clear(runs_dir: str, agent: str | None = None) -> None:
    with _state_lock(runs_dir):
        st = load_state(runs_dir)
        rl = st.get("rate_limited", {})
        if agent:
            rl.pop(agent, None)
        else:
            rl.clear()
        save_state(runs_dir, st)


def lane(runs_dir: str, name: str) -> dict:
    """v4 (BUILD_SPEC_v4 section 6): one lane of config/models.json, resolved to agent + model. `pick` = the worker,
    or the first `also` model whose agent is not rate-limited (state.json, as in suggest). worker 'self' = Grok Bot
    answers itself; 'race' = the race models (models.json race_default)."""
    from . import models as M
    lanes = M.lanes()
    if name not in lanes:
        from .errors import Refused
        raise Refused(f"unknown lane '{name}'; use one of: {', '.join(lanes)}")
    row = lanes[name]
    st = load_state(runs_dir)
    worker = M.describe(row.get("worker"))
    also = [M.describe(a) for a in row.get("also") or []]
    out = {"lane": name, "worker": worker, "also": also, "check": M.describe(row.get("check")), "pick": None,
           "skipped": {}, "note": ""}
    if worker["alias"] == "self":
        out["pick"], out["note"] = worker, "Grok Bot answers itself (no agent run)"
        return out
    if worker["alias"] == "race":
        race = [M.describe(a) for a in M.race_models()]
        for r in race:
            lim = limited(st, r["agent"])
            if lim:
                out["skipped"][r["agent"]] = f"rate-limited until {lim['until']}"
        out["race"] = [r for r in race if r["agent"] not in out["skipped"]]
        out["pick"] = worker
        out["note"] = "race: " + ", ".join(r["alias"] for r in out["race"]) + (
            f" (skipped: {', '.join(out['skipped'])})" if out["skipped"] else "")
        return out
    for cand in [worker] + also:
        if cand["agent"] is None:
            continue
        lim = limited(st, cand["agent"])
        if lim:
            out["skipped"][cand["agent"]] = f"rate-limited until {lim['until']}"
            continue
        out["pick"] = cand
        break
    if out["pick"] is None:
        out["note"] = "every model of this lane is rate-limited"
    elif out["pick"]["alias"] != worker["alias"]:
        out["note"] = f"fallback; worker {worker['alias']} skipped"
    return out


def suggest(runs_dir: str, task_type: str, review_of: str | None = None) -> dict:
    primary, cross = MATRIX.get(task_type, MATRIX["general"])
    if review_of:
        order = [a for a in ([cross, primary] + list(AGENTS)) if a and a != review_of]
    else:
        order = [a for a in ([primary, cross] + list(AGENTS)) if a]
    order = list(dict.fromkeys(order))
    st = load_state(runs_dir)
    skipped = {}
    for a in order:
        lim = limited(st, a)
        if lim:
            skipped[a] = f"rate-limited until {lim['until']}"
            continue
        return {"agent": a, "task_type": task_type if task_type in MATRIX else "general",
                "cross_check": cross if not review_of else None, "mode": "read",
                "order": order, "skipped": skipped,
                "note": "" if a == order[0] else f"fallback; first choice {order[0]} skipped"}
    return {"agent": None, "task_type": task_type, "order": order, "skipped": skipped,
            "note": "every agent is rate-limited"}
