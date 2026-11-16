"""Asks and budgets (BUILD_SPEC_v4 section 5): one record per request from chalach.

An ask is opened before any agent starts (`ck-run ask open`), every run and review carries its id (--ask), and it is
closed with one finish card (`ck-run ask close`). There is no second database: the state of an ask comes from the
ledger (ask_open, ask_park, ask_close, answered, later_add, over_budget, and the runs whose events carry the ask id).

Budgets per size (config asks.budgets; the code defaults below are the same):
              S                        M                L
  runs        1                        2 (1 + 1 fix)    6
  parallel    1                        1                3      (runs live at once)
  reviews     0 (1 with you-asked)     1                2
  fixes       0                        1                2      (runs with --parent in the same ask)
  wall_min    15                       45               180
  timeout_s   600                      1800             7200   (per run, when --timeout is not given)
  stall_s     180                      300              480    (live watchdog)
Pattern race: runs = max(runs, number of race models), parallel = number of race models (models.json race_default
when --models is not given). `--parallel N --because "<words>"` sets parallel N (runs >= N). `--plan FILE` (L only):
a line `budget: runs=8 parallel=3 reviews=2 fixes=2 wall_min=240` sets those keys.

gate() runs in runner.execute before the claims: unknown or closed ask -> Refused (exit 2); a review needs one of the 4
triggers (an S ask only `you-asked`); over budget -> OverBudget (exit 7) and an `over_budget` ledger event, unless
--over "<chalach's words>" lets this one dispatch pass (`over_budget` status approved). --over never bypasses guards,
claims, the andon or any safety rule. Over the wall time also parks the ask (`ask_park`, reason "over time").
Loop rounds with --parent count as fixes too; an L plan can set the needed fix budget explicitly.
"""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import re
import shlex
from datetime import datetime, timedelta

from . import claims, ledger
from . import models as M
from .errors import Refused
from .summarize import VERIFICATION_WORDS, lowest_verification, v4_fields

SIZES = ("S", "M", "L")
PATTERNS = ("solo", "pair", "race", "split", "panel", "loop")
TRIGGERS = ("leaves-lab", "hardware-or-raw", "agent-unsure", "you-asked")
BUDGET_KEYS = ("runs", "parallel", "reviews", "fixes", "wall_min", "timeout_s", "stall_s")
PLAN_KEYS = ("runs", "parallel", "reviews", "fixes", "wall_min")
DEFAULT_BUDGETS = {
    "S": {"runs": 1, "parallel": 1, "reviews": 0, "fixes": 0, "wall_min": 15, "timeout_s": 600, "stall_s": 180},
    "M": {"runs": 2, "parallel": 1, "reviews": 1, "fixes": 1, "wall_min": 45, "timeout_s": 1800, "stall_s": 300},
    "L": {"runs": 6, "parallel": 3, "reviews": 2, "fixes": 2, "wall_min": 180, "timeout_s": 7200, "stall_s": 480},
}
RACE_DEFAULT_N = 3
ASK_ID_RE = re.compile(r"^A-(\d{4})-([1-9]\d{0,4})$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
RUN_ID_IN_TEXT = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{6}-[a-z0-9]+-[a-z0-9_]+-[a-z]+-[0-9a-f]{4}\b")
PLAN_LINE = re.compile(r"^\s*(?:[-*]\s*)?budget\s*:\s*(.*)$", re.I | re.M)
LOCK_NAME = ".asks.lock"
ESCAPE = "To go over, add --over \"<chalach's words>\""
DONE_WORDS = {"yes": "done", "partial": "partial", "no": "not_done"}
STATUS_WORDS = {"done": "DONE", "partial": "PARTIAL", "not_done": "NOT DONE"}
CARD_MAX_LINES = 8
MAX_WORDS_CHARS = 4000


class OverBudget(Refused):
    """The ask has no budget left for this dispatch. ck-run exits 7; --over "<chalach's words>" lets one pass."""


# ---- small helpers ------------------------------------------------------------------------------------------

@contextlib.contextmanager
def lock(cfg):
    """<runs_dir>/.asks.lock: new ask ids, closes, and (runner) gate + claims of one dispatch, one at a time."""
    os.makedirs(cfg.runs_dir, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(cfg.runs_dir, LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def valid_id(ask_id) -> bool:
    return bool(ASK_ID_RE.match(str(ask_id or "")))


def _need_id(ask_id) -> str:
    a = str(ask_id or "").strip()
    if not valid_id(a):
        raise Refused(f"not an ask id: {ask_id!r} (ask ids look like A-1105-4; see ck-run ask list)")
    return a


def _now():
    return datetime.now().astimezone()


def _parse_ts(ts):
    try:
        t = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


def minutes_between(a, b=None) -> float | None:
    ta, tb = _parse_ts(a), (_parse_ts(b) if b else _now())
    if ta is None or tb is None:
        return None
    return max(0.0, (tb - ta).total_seconds() / 60.0)


def parse_since(text: str | None):
    """'7d', '12h', '30m' (a bare number = days) -> timedelta; None -> None."""
    if not text:
        return None
    m = re.match(r"^\s*(\d+)\s*([dhm]?)\s*$", str(text).lower())
    if not m:
        raise Refused(f"--since {text!r}: use a number with d, h or m, e.g. 7d")
    n, unit = int(m.group(1)), m.group(2) or "d"
    return timedelta(days=n) if unit == "d" else timedelta(hours=n) if unit == "h" else timedelta(minutes=n)


def parse_models(models) -> list:
    """'astra,opus,grok' / 'astra+opus' / a list -> known aliases (models.json). 'auto' or '' -> []."""
    if not models:
        return []
    items = models if isinstance(models, (list, tuple)) else re.split(r"[,+\s]+", str(models))
    out = []
    for x in items:
        x = str(x).strip().lower()
        if not x or x == "auto":
            continue
        if not M.is_alias(x):
            raise Refused(f"--models: unknown alias '{x}'; use some of: {', '.join(M.aliases())}")
        if x not in out:
            out.append(x)
    return out


def parse_plan(path: str) -> dict:
    """The budget line of an L plan: 'budget: runs=8 parallel=3 reviews=2 fixes=2 wall_min=240' -> dict ({} if
    the file has no budget line)."""
    p = os.path.expanduser(str(path))
    if not os.path.isfile(p):
        raise Refused(f"--plan file not found: {path}")
    with io.open(p, encoding="utf-8", errors="replace") as f:
        text = f.read(200_000)
    m = PLAN_LINE.search(text)
    if not m:
        return {}
    out = {}
    for tok in m.group(1).replace(",", " ").split():
        k, eq, v = tok.partition("=")
        k = k.strip().lower()
        if not eq or k not in PLAN_KEYS:
            raise Refused(f"--plan {path}: '{tok}' in the budget line is not key=number with a key from "
                          f"{', '.join(PLAN_KEYS)}")
        try:
            n = int(v)
        except ValueError:
            raise Refused(f"--plan {path}: '{tok}' needs a whole number")
        if n < (1 if k in ("runs", "parallel", "wall_min") else 0):
            raise Refused(f"--plan {path}: '{tok}' is too small")
        out[k] = n
    return out


def _note(models: list, plan: str | None, because: str | None) -> str | None:
    parts = []
    if models:
        parts.append("models=" + ",".join(models))
    if plan:
        parts.append("plan=" + plan)
    if because:
        parts.append("because=" + because)  # last: chalach's words may hold any character
    return "; ".join(parts) or None


def _parse_note(note) -> dict:
    s = str(note or "")
    out = {"models": [], "plan": None, "because": None}
    b = re.search(r"(?:^|; )because=(.*)$", s, re.S)
    head = s[:b.start()] if b else s
    if b:
        out["because"] = b.group(1)
    m = re.search(r"(?:^|; )models=([^;]*)", head)
    if m:
        out["models"] = [x for x in m.group(1).split(",") if x]
    p = re.search(r"(?:^|; )plan=([^;]*)", head)
    if p:
        out["plan"] = p.group(1)
    return out


# ---- budgets ------------------------------------------------------------------------------------------------

def budget_for(cfg, size: str, pattern: str | None = "solo", models: list | None = None, parallel: int | None = None,
               plan: dict | None = None) -> dict:
    """The budget of a new ask: the size's defaults (config asks.budgets), then race, --parallel and --plan."""
    if size not in SIZES:
        raise Refused(f"size must be S, M or L (got {size!r})")
    b = dict(DEFAULT_BUDGETS[size])
    for k, v in ((getattr(cfg, "asks_budgets", None) or {}).get(size) or {}).items():
        if k in BUDGET_KEYS:
            try:
                b[k] = int(v)
            except (TypeError, ValueError):
                raise Refused(f"config asks.budgets.{size}.{k} must be a whole number")
    if pattern == "race":
        try:
            n = len(models) if models else len(M.race_models()) or RACE_DEFAULT_N
        except Refused:
            n = RACE_DEFAULT_N
        b["runs"], b["parallel"] = max(b["runs"], n), n
    if parallel:
        b["parallel"], b["runs"] = int(parallel), max(b["runs"], int(parallel))
    for k, v in (plan or {}).items():
        if k in PLAN_KEYS:
            b[k] = int(v)
    return b


def budget_words(b: dict, size: str) -> str:
    runs = f"{b['runs']} run{'s' if b['runs'] != 1 else ''}"
    if b.get("fixes"):
        runs += f" incl. {b['fixes']} fix{'es' if b['fixes'] != 1 else ''}"
    if b.get("reviews"):
        rev = f"{b['reviews']} review{'s' if b['reviews'] != 1 else ''} (only with a reason)"
    else:
        rev = "no review (only if you ask)" if size == "S" else "no review"
    return (f"{runs}, {rev}, {b['parallel']} at a time, {b['wall_min']} min "
            f"({max(1, round(b['timeout_s'] / 60))} min per run)")


# ---- state from the ledger ----------------------------------------------------------------------------------

def _state(ask_id: str, events: list) -> dict | None:
    op, parks, close, later, answered, overs = None, [], None, [], [], []
    for ev in events:
        if ev.get("ask_id") != ask_id:
            continue
        name = ev.get("event")
        if name == "ask_open" and op is None:
            op = ev
        elif name == "ask_park":
            parks.append(ev)
        elif name == "ask_close" and close is None:
            close = ev
        elif name == "later_add" and str(ev.get("note") or "").strip():
            later.append(str(ev["note"]).strip())
        elif name == "answered":
            answered.append(str(ev.get("note") or ""))
        elif name == "over_budget":
            overs.append(ev)
    if op is None:
        return None
    size = op.get("size") if op.get("size") in SIZES else "M"
    pattern = op.get("pattern") if op.get("pattern") in PATTERNS else "solo"
    budget = dict(DEFAULT_BUDGETS[size])
    if isinstance(op.get("budget"), dict):
        budget.update({k: v for k, v in op["budget"].items() if k in BUDGET_KEYS and isinstance(v, int)})
    note = _parse_note(op.get("note"))
    return {"ask_id": ask_id, "words": op.get("words") or "", "title": op.get("title") or "", "size": size,
            "pattern": pattern, "project": op.get("project"), "budget": budget, "models": note["models"],
            "because": note["because"], "plan": note["plan"], "opened_at": op.get("ts"),
            "status": "closed" if close else ("parked" if parks else "open"),
            "park_reason": parks[-1].get("reason") if parks else None,
            "parked_at": parks[-1].get("ts") if parks else None,
            "closed_at": (close or {}).get("ts"), "done": (close or {}).get("status"),
            "card": (close or {}).get("card"), "close_note": (close or {}).get("note"),
            "later_added": later, "answered": answered, "over_budget": overs}


def get(cfg, ask_id: str, events: list | None = None) -> dict | None:
    """The ask's state (see _state), or None if no ask_open event has that id."""
    if not valid_id(ask_id):
        return None
    return _state(ask_id, ledger.load(cfg.runs_dir) if events is None else events)


def need(cfg, ask_id: str, events: list | None = None) -> dict:
    a = _need_id(ask_id)
    st = get(cfg, a, events)
    if st is None:
        raise Refused(f"unknown ask {a} (see ck-run ask list; open one with ck-run ask open)")
    return st


def _ask_of(run) -> str | None:
    op = run.first("dispatch", "review") or run.opening
    return op.get("ask_id")


def usage(cfg, ask_id: str, events: list | None = None, live_records: list | None = None) -> dict:
    """What the ask has used: runs (dispatch), reviews, fixes (dispatch runs whose --parent is a run of the same
    ask), live runs now (the run registry), and the run ids. Runs registered but not yet in the ledger count too."""
    events = ledger.load(cfg.runs_dir) if events is None else events
    rs = ledger.runs(events)
    mine = {rid for rid, r in rs.items() if r.first("dispatch", "review") is not None and _ask_of(r) == ask_id}
    dispatch, reviews, fixes = [], [], []
    for rid in sorted(mine, key=lambda x: rs[x].ts):
        op = rs[rid].first("dispatch", "review")
        if op.get("event") == "review":
            reviews.append(rid)
        else:
            dispatch.append(rid)
            if op.get("parent_run_id") in mine:
                fixes.append(rid)
    if live_records is None:
        live_records, _ = claims.active(cfg.runs_dir)
    live = [r for r in live_records if r.get("ask_id") == ask_id]
    for rec in live:
        rid = rec.get("run_id")
        if not rid or rid in mine:
            continue
        if rec.get("kind") == "review":
            reviews.append(rid)
        else:
            dispatch.append(rid)
            if rec.get("parent_run_id") in mine:
                fixes.append(rid)
    return {"runs": len(dispatch), "reviews": len(reviews), "fixes": len(fixes), "live": len(live),
            "dispatch_ids": dispatch, "review_ids": reviews, "fix_ids": fixes,
            "live_ids": [r.get("run_id") for r in live]}


# ---- open, park, later, answered ----------------------------------------------------------------------------

def _next_id(events: list) -> str:
    """A-MMDD-N: N = 1 + the number of ask_open events with that MMDD (never below 1 + the highest N seen)."""
    mmdd = datetime.now().strftime("%m%d")
    count = top = 0
    for ev in events:
        if ev.get("event") != "ask_open":
            continue
        m = ASK_ID_RE.match(str(ev.get("ask_id") or ""))
        if m and m.group(1) == mmdd:
            count += 1
            top = max(top, int(m.group(2)))
    return f"A-{mmdd}-{max(count, top) + 1}"


def open_ask(cfg, *, words: str, size: str, title: str, project: str | None = None, pattern: str | None = None,
             models=None, parallel: int | None = None, because: str | None = None, plan: str | None = None) -> dict:
    """Write ask_open and return the ask's state. Ids are made under <runs_dir>/.asks.lock."""
    words = (words or "").strip()
    if not words:
        raise Refused("--words is empty: give chalach's words, verbatim")
    size = (size or "").strip().upper()
    if size not in SIZES:
        raise Refused("--size must be S, M or L")
    title = " ".join((title or "").split())[:200]
    if not title:
        raise Refused("--title is empty: put your one-line goal (chalach's ask in your words)")
    pattern = (pattern or "solo").strip().lower()
    if pattern not in PATTERNS:
        raise Refused(f"--pattern must be one of {', '.join(PATTERNS)}")
    if project is not None:
        project = project.strip()
        if not PROJECT_RE.match(project):
            raise Refused("--project: letters, digits and _ . - only (at most 64), e.g. fnd_sin")
    mlist = parse_models(models)
    because = (because or "").strip() or None
    if parallel is not None:
        if not because:
            raise Refused("--parallel N needs --because \"<chalach's words>\"")
        if not 1 <= int(parallel) <= 10:
            raise Refused("--parallel must be 1 to 10")
    elif because:
        raise Refused("--because goes with --parallel N")
    plan_budget, plan_path = {}, None
    if plan:
        if size != "L":
            raise Refused("--plan is for L asks only")
        plan_budget, plan_path = parse_plan(plan), os.path.abspath(os.path.expanduser(plan))
    budget = budget_for(cfg, size, pattern, mlist, parallel, plan_budget)
    with lock(cfg):
        ask_id = _next_id(ledger.load(cfg.runs_dir))
        ledger.append(cfg.runs_dir, event="ask_open", ask_id=ask_id, words=words[:MAX_WORDS_CHARS], title=title,
                      size=size, pattern=pattern, project=project, budget=budget, status="open",
                      note=_note(mlist, plan_path, because), **{"from": "CK"})
    return need(cfg, ask_id)


def park(cfg, ask_id: str, reason: str) -> dict:
    reason = " ".join((reason or "").split())
    if not reason:
        raise Refused("--reason is empty")
    st = need(cfg, ask_id)
    if st["status"] == "closed":
        raise Refused(f"ask {st['ask_id']} is closed; open a new ask")
    ledger.append(cfg.runs_dir, event="ask_park", ask_id=st["ask_id"], reason=reason[:500], status="parked",
                  title=st["title"], size=st["size"], pattern=st["pattern"], project=st["project"], **{"from": "CK"})
    return need(cfg, ask_id)


def later_add(cfg, ask_id: str, item: str) -> list:
    item = " ".join((item or "").split())
    if not item:
        raise Refused("the Later item is empty")
    st = need(cfg, ask_id)
    ledger.append(cfg.runs_dir, event="later_add", ask_id=st["ask_id"], note=item[:500], title=st["title"],
                  size=st["size"], pattern=st["pattern"], project=st["project"], **{"from": "CK"})
    return later_list(cfg, ask_id)


def _results(cfg, ids: list) -> dict:
    """run id -> result.json (ck-run's record) or {}."""
    out = {}
    for rid in ids:
        try:
            with io.open(os.path.join(cfg.runs_dir, rid, "result.json"), encoding="utf-8") as f:
                rec = json.load(f)
            out[rid] = rec if isinstance(rec, dict) else {}
        except (OSError, ValueError):
            out[rid] = {}
    return out


def later_list(cfg, ask_id: str, events: list | None = None) -> list:
    """Later items: the agents' later[] (run order), then the ones added with ck-run ask later. No duplicates."""
    events = ledger.load(cfg.runs_dir) if events is None else events
    st = need(cfg, ask_id, events)
    u = usage(cfg, st["ask_id"], events, live_records=[])
    recs = _results(cfg, u["dispatch_ids"] + u["review_ids"])
    out, seen = [], set()
    for rid in u["dispatch_ids"] + u["review_ids"]:
        for x in v4_fields(recs[rid].get("agent_result")).get("later") or []:
            if x.lower() not in seen:
                seen.add(x.lower())
                out.append(x)
    for x in st["later_added"]:
        if x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


def answered(cfg, ask_id: str, summary: str) -> dict:
    """Grok Bot answered the ask itself (no agent): write `answered`, then close the ask as done with its card."""
    lines = [" ".join(x.split()) for x in (summary or "").splitlines() if x.strip()]
    if not lines:
        raise Refused("--summary is empty: 1 to 3 lines")
    st = need(cfg, ask_id)
    if st["status"] == "closed":
        raise Refused(f"ask {st['ask_id']} is closed; open a new ask")
    ledger.append(cfg.runs_dir, event="answered", ask_id=st["ask_id"], note="\n".join(x[:300] for x in lines[:3]),
                  title=st["title"], size=st["size"], pattern=st["pattern"], project=st["project"],
                  status="done", **{"from": "CK"})
    return close(cfg, ask_id, note="answered by Grok Bot (no agent)")


# ---- gate (runner.execute, before the claims) --------------------------------------------------------------

def required_message(spec) -> str:
    title = (getattr(spec, "title", "") or "").strip() or "<one-line goal>"
    cmd = "review" if getattr(spec, "kind", "dispatch") == "review" else "run"
    return ("--ask is required (config asks.required). Open the ask first:\n"
            f"  ck-run ask open --words \"<chalach's words, verbatim>\" --size S|M|L --title {shlex.quote(title)}\n"
            f"then add --ask <the id it prints> to this ck-run {cmd}.")


def _top_not_allowed(st: dict, spec) -> bool:
    """Top tier needs chalach's words for S and M asks; race runs, L asks and a model chalach named are fine."""
    return (getattr(spec, "tier", None) == "top" and not getattr(spec, "model_explicit", False)
            and st["size"] in ("S", "M") and st["pattern"] != "race")


def gate(cfg, spec, *, base: dict | None = None, dry_run: bool = False) -> dict | None:
    """Check the ask of one run or review. Returns None (no ask), {"over": False}, or {"over": True, "approved":
    words, "reasons": [...]}. Raises Refused (exit 2) or OverBudget (exit 7). Writes over_budget / ask_park
    events (not in a dry run). Call it under lock(cfg) together with the run's claims."""
    ask_id = getattr(spec, "ask_id", None)
    kind = getattr(spec, "kind", "dispatch") or "dispatch"
    trigger = getattr(spec, "trigger", None)
    if not ask_id:
        if getattr(cfg, "asks_required", False):
            raise Refused(required_message(spec))
        if trigger is not None and trigger not in TRIGGERS:
            raise Refused(f"--trigger must be one of {', '.join(TRIGGERS)}")
        return None
    events = ledger.load(cfg.runs_dir)
    st = need(cfg, ask_id, events)
    ask_id = st["ask_id"]
    if st["status"] == "closed":
        raise Refused(f"ask {ask_id} is closed; open a new ask")
    size, b = st["size"], st["budget"]
    if kind == "review":
        if not trigger:
            raise Refused(f"ck-run review needs --trigger, one of: {', '.join(TRIGGERS)}")
        if trigger not in TRIGGERS:
            raise Refused(f"--trigger must be one of {', '.join(TRIGGERS)}")
        if size == "S" and trigger != "you-asked":
            raise Refused(f"ask {ask_id} is size S: a review needs --trigger you-asked (only when chalach asks)")
    elif trigger is not None:
        raise Refused("--trigger is for ck-run review")
    u = usage(cfg, ask_id, events)
    reasons = []
    if st["status"] == "parked":
        reasons.append(f"is parked ({st['park_reason'] or 'waits for chalach'})")
    elapsed = minutes_between(st["opened_at"])
    over_time = elapsed is not None and elapsed > b["wall_min"]
    if over_time:
        reasons.append(f"used {elapsed:.0f}/{b['wall_min']} min")
    if kind == "review":
        limit = max(b["reviews"], 1) if (size == "S" and trigger == "you-asked") else b["reviews"]
        if u["reviews"] >= limit:
            reasons.append(f"used {u['reviews']}/{limit} reviews")
    else:
        if u["runs"] >= b["runs"]:
            reasons.append(f"used {u['runs']}/{b['runs']} runs")
        parent = getattr(spec, "parent_run_id", None)
        prun = ledger.runs(events).get(parent) if parent else None
        if prun is not None and _ask_of(prun) == ask_id and u["fixes"] >= b["fixes"]:
            reasons.append(f"used {u['fixes']}/{b['fixes']} fixes")
    if u["live"] >= b["parallel"]:
        reasons.append(f"has {u['live']}/{b['parallel']} runs live at once")
    if _top_not_allowed(st, spec):
        reasons.append(f"allows 0 top-tier runs at size {size} (--tier top)")
    if not reasons:
        return {"over": False}
    msg = f"ask {ask_id} ({size}) " + "; ".join(reasons) + ". " + ESCAPE
    words = (getattr(spec, "over", None) or "").strip()
    ev = dict(base or {}, ask_id=ask_id, size=size, pattern=st["pattern"], project=st["project"])
    ev.pop("event", None)
    if not dry_run and over_time and st["status"] != "parked":
        ledger.append(cfg.runs_dir, event="ask_park", ask_id=ask_id, reason="over time", status="parked",
                      title=st["title"], size=size, pattern=st["pattern"], project=st["project"],
                      note=f"wall time {elapsed:.0f}/{b['wall_min']} min", **{"from": "CK"})
    if words:
        if not dry_run:
            ledger.append(cfg.runs_dir, **dict(ev, event="over_budget", status="approved", reason="; ".join(reasons),
                                               over=words, note=f"--over passed: {msg}"[:1000]))
        return {"over": True, "approved": words, "reasons": reasons}
    if not dry_run:
        ledger.append(cfg.runs_dir, **dict(ev, event="over_budget", status="refused", reason="; ".join(reasons),
                                           note=msg[:1000]))
    raise OverBudget(msg)


# ---- cards, close, list, show -------------------------------------------------------------------------------

def start_card(st: dict) -> str:
    """At most 4 lines: id, size, pattern, project; goal (= the title); models (if given); budget."""
    head = f"{st['ask_id']}  size {st['size']}  pattern {st['pattern']}" + (f"  project {st['project']}"
                                                                            if st.get("project") else "")
    L = [head, f"Goal: {st['title']}"]
    if st.get("models"):
        L.append(f"Models: {', '.join(st['models'])}")
    L.append(f"Budget: {budget_words(st['budget'], st['size'])}")
    return "\n".join(L[:4])


def _clean(text: str, cap: int = 160) -> str:
    s = " ".join(RUN_ID_IN_TEXT.sub("", str(text or "")).split())
    return s if len(s) <= cap else s[:cap - 3] + "..."


def _derive_done(st: dict, u: dict, recs: dict) -> str:
    stats = [recs[r].get("status") for r in u["dispatch_ids"] if recs.get(r)]
    if not stats:
        return "done" if st["answered"] else "not_done"
    if stats[-1] == "ok":
        return "done"
    if any(s in ("ok", "partial") for s in stats):
        return "partial"
    return "not_done"


def finish_card(cfg, st: dict, u: dict | None = None, done: str | None = None, events: list | None = None,
                closed_at: str | None = None) -> tuple:
    """(card, status). Plain words, at most 8 lines, no run ids:
      <id>  <title>   DONE   <n> runs, <m> min
      Changed: <plain_summary lines of the runs, merged, at most 3 lines>
      How sure: <lowest verification of the runs>
      You should know: <at most 2 lines>
      Your move: <your_move lines, or nothing>
      Later (N): <items, numbered, short>"""
    events = ledger.load(cfg.runs_dir) if events is None else events
    u = u or usage(cfg, st["ask_id"], events, live_records=[])
    ids = u["dispatch_ids"] + u["review_ids"]
    recs = _results(cfg, ids)
    v4 = {rid: v4_fields(recs[rid].get("agent_result")) for rid in ids}
    changed = []
    for rid in ids:
        for x in v4[rid]["plain_summary"]:
            if _clean(x) not in changed:
                changed.append(_clean(x))
    if not changed:
        for a in st["answered"]:
            changed += [_clean(x) for x in a.splitlines() if x.strip()]
    if not changed:  # results without v4 fields: the first summary line of each run
        changed = [_clean((recs[rid].get("summary_lines") or [""])[0]) for rid in u["dispatch_ids"]]
        changed = [x for x in changed if x and not x.startswith("[ck-run]")]
    changed = changed[:3] or ["nothing reported"]
    level = lowest_verification([v4[rid]["verification"] for rid in ids])
    if level is None:
        level = "desk_check" if (st["answered"] and not ids) else "not_checked"
    know = []
    for rid in ids:
        for x in v4[rid]["you_should_know"]:
            if _clean(x) not in know:
                know.append(_clean(x))
    bad = len([rid for rid in ids if recs[rid].get("status") in ("flagged", "violation")])
    if bad:
        know.insert(0, f"{bad} run{'s' if bad != 1 else ''} flagged by the rule check: look before you trust "
                       f"{'them' if bad != 1 else 'it'} (ck-run accept)")
    know = know[:2]
    moves = []
    for rid in ids:
        mv = v4[rid]["your_move"]
        if mv and _clean(mv) not in moves:
            moves.append(_clean(mv))
    later = later_list(cfg, st["ask_id"], events)
    status = DONE_WORDS.get(done, done) if done else _derive_done(st, u, recs)
    if status not in STATUS_WORDS:
        raise Refused("--done must be yes, partial or no")
    mins = minutes_between(st["opened_at"], closed_at)
    n_runs, n_rev = len(u["dispatch_ids"]), len(u["review_ids"])
    work = f"{n_runs} run{'s' if n_runs != 1 else ''}" + (f" + {n_rev} review{'s' if n_rev != 1 else ''}"
                                                            if n_rev else "")
    head = f"{st['ask_id']}  {_clean(st['title'], 60)}   {STATUS_WORDS[status]}   {work}" + (
        f", {mins:.0f} min" if mins is not None else "")
    later_items = "; ".join(f"{i}) {_clean(x, 60)}" for i, x in enumerate(later, 1))
    later_line = f"Later ({len(later)}): " + (_clean(later_items, 300) if later else "none")
    how_line = f"How sure: {VERIFICATION_WORDS[level]}"
    know_lines = [("You should know: " if i == 0 else "                 ") + x for i, x in enumerate(know)]
    move_line = f"Your move: {'; '.join(moves)[:300] if moves else 'nothing'}"
    fixed = 4  # head, How sure, Your move, Later
    if fixed + len(changed) + len(know_lines) > CARD_MAX_LINES and len(know_lines) > 1:
        know_lines = ["You should know: " + "; ".join(know)]  # 9 lines at most -> merge the 2 into 1
    changed = changed[:max(1, CARD_MAX_LINES - fixed - len(know_lines))]
    lines = ([head] + [("Changed: " if i == 0 else "         ") + x for i, x in enumerate(changed)]
             + [how_line] + know_lines + [move_line, later_line])
    return "\n".join(lines[:CARD_MAX_LINES]), status


def close(cfg, ask_id: str, *, done: str | None = None, note: str | None = None) -> dict:
    """Write ask_close with the finish card; accept the ask's open runs. Safe to rerun: a closed ask returns its card.
    Open runs: ok/partial -> accepted (rejected with --done no); failed/timeout -> rejected; flagged/violation are
    left for chalach (the card says so)."""
    if done is not None and done not in DONE_WORDS:
        raise Refused("--done must be yes, partial or no")
    with lock(cfg):
        events = ledger.load(cfg.runs_dir)
        st = need(cfg, ask_id, events)
        if st["status"] == "closed":
            return {"ask_id": st["ask_id"], "card": st["card"] or "", "status": st["done"], "already": True,
                    "still_live": []}
        u = usage(cfg, st["ask_id"], events)
        closed_at = ledger.now_iso()
        card, status = finish_card(cfg, st, u, done, events, closed_at=closed_at)
        rs = ledger.runs(events)
        for rid in u["dispatch_ids"] + u["review_ids"]:
            r = rs.get(rid)
            res = r.last("result") if r else None
            if res is None or r.last("accept") is not None:
                continue
            if res.get("status") in ("ok", "partial"):
                ok = status != "not_done"
            elif res.get("status") in ("failed", "timeout"):
                ok = False
            else:
                continue  # flagged / violation: chalach decides (ck-run accept)
            op = r.opening
            ledger.append(cfg.runs_dir, event="accept", run_id=rid, parent_run_id=op.get("parent_run_id"),
                          title=op.get("title"), workspace=op.get("workspace"), task_type=op.get("task_type"),
                          agent=op.get("agent"), model=op.get("model"), mode=op.get("mode"),
                          prompt_path=res.get("prompt_path"), response_path=res.get("response_path"),
                          summary_path=res.get("summary_path"), status=res.get("status"), accepted=ok,
                          ask_id=st["ask_id"], size=st["size"], pattern=st["pattern"], tier=op.get("tier"),
                          model_alias=op.get("model_alias"), project=st["project"],
                          note=f"closed with ask {st['ask_id']} ({STATUS_WORDS[status].lower()})",
                          **{"from": "CK", "to": op.get("agent")})
        still = [rid for rid in u["live_ids"] if rid]
        n = "; ".join(x for x in [(note or "").strip(),
                                  f"{len(still)} run(s) still running: {', '.join(still)}" if still else ""] if x)
        ledger.append(cfg.runs_dir, event="ask_close", ask_id=st["ask_id"], status=status, card=card,
                      note=n[:1000] or None, title=st["title"], size=st["size"], pattern=st["pattern"],
                      project=st["project"], **{"from": "CK"})
    return {"ask_id": st["ask_id"], "card": card, "status": status, "already": False, "still_live": still}


def list_asks(cfg, *, open_only: bool = False, since: str | None = None) -> list:
    """Every ask (newest last) with its state and use; open_only: open and parked ones; since: '7d' etc."""
    events = ledger.load(cfg.runs_dir)
    delta = parse_since(since)
    cutoff = _now() - delta if delta else None
    live_records, _ = claims.active(cfg.runs_dir)
    out = []
    for ev in events:
        if ev.get("event") != "ask_open" or not valid_id(ev.get("ask_id")):
            continue
        st = _state(ev["ask_id"], events)
        if st is None or any(o["ask_id"] == st["ask_id"] for o in out):
            continue
        if open_only and st["status"] == "closed":
            continue
        t = _parse_ts(st["opened_at"])
        if cutoff and (t is None or t < cutoff):
            continue
        st["usage"] = usage(cfg, st["ask_id"], events, live_records)
        out.append(st)
    return out


def list_line(st: dict) -> str:
    u, b = st.get("usage") or {}, st["budget"]
    mins = minutes_between(st["opened_at"], st["closed_at"])
    return (f"{st['ask_id']:<11} {st['status']:<7} {st['size']} {st['pattern']:<6} runs {u.get('runs', 0)}/{b['runs']}"
            f"  reviews {u.get('reviews', 0)}/{b['reviews']}  live {u.get('live', 0)}"
            f"  {(f'{mins:.0f}' if mins is not None else '?')}/{b['wall_min']} min  {_clean(st['title'], 70)}")


def show(cfg, ask_id: str) -> str:
    """Everything about one ask, for Grok Bot (run ids included)."""
    events = ledger.load(cfg.runs_dir)
    st = need(cfg, ask_id, events)
    u = usage(cfg, st["ask_id"], events)
    rs = ledger.runs(events)
    L = [start_card(st), "", f"Status: {st['status']}"
         + (f" ({st['park_reason']})" if st["status"] == "parked" else "")
         + (f", {STATUS_WORDS.get(st['done'], st['done'])}" if st["status"] == "closed" else ""),
         f"chalach's words: {st['words']}"]
    if st.get("because"):
        L.append(f"Parallel because: {st['because']}")
    mins = minutes_between(st["opened_at"], st["closed_at"])
    L.append(f"Used: {u['runs']}/{st['budget']['runs']} runs ({u['fixes']}/{st['budget']['fixes']} fixes), "
             f"{u['reviews']} reviews, {u['live']} live, "
             f"{(f'{mins:.0f}' if mins is not None else '?')}/{st['budget']['wall_min']} min")
    for rid in u["dispatch_ids"] + u["review_ids"]:
        r = rs.get(rid)
        op = (r.first("dispatch", "review") or {}) if r else {}
        res = r.last("result") if r else None
        L.append(f"- {rid}  {op.get('event') or 'run'}  {op.get('agent') or '?'}"
                 + (f" ({op.get('model_alias')})" if op.get("model_alias") else "")
                 + f"  {(res or {}).get('status') or ('running' if rid in u['live_ids'] else 'no result')}")
    for ev in st["over_budget"]:
        L.append(f"- over budget {ev.get('status')}: {ev.get('reason') or ''}"
                 + (f" (chalach: \"{ev.get('over')}\")" if ev.get("over") else ""))
    later = later_list(cfg, st["ask_id"], events)
    if later:
        L += [f"Later ({len(later)}):"] + [f"  {i}) {x}" for i, x in enumerate(later, 1)]
    if st["card"]:
        L += ["", "Finish card:", st["card"]]
    return "\n".join(L) + "\n"


open = open_ask  # noqa: A001  (the contract name; this module uses io.open for files)
