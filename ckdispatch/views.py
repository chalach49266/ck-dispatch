"""Read-only views made from the ledger (v4, chalach 2026-11-05): ck-run board, digest, recall, scoreboard.

The ledger (agent_runs/ledger.jsonl) is the one truth. These views read it directly (every line, also events with
no run_id such as ask_open, andon or race), plus the run registry (agent_runs/.claims, for live runs) and each run's
result.json. They never change the ledger, a run folder or the registry. The one write is `board --md`, which writes
the board as markdown to a file (default <local_root>/BOARD.md) and never overwrites a file that is not a board.

Ask state comes from the ask events (spec section 4): ask_open, ask_park, ask_close, answered, later_add,
over_budget. An ask is closed after ask_close (or answered), parked after ask_park, else open.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import statistics
import sys
import threading
import time
from datetime import datetime, timedelta

from . import claims, ledger
from .config import Config
from .errors import Refused

ASK_RX = re.compile(r"^A-\d{4}-[1-9]\d{0,4}$")  # same as asks.ASK_ID_RE
RUN_KINDS = ("dispatch", "review")
ASK_ONLY_EVENTS = {"ask_open", "ask_park", "ask_close", "answered", "later_add", "over_budget", "race", "panel",
                   "launch"}
# verification levels, lowest first (result.json v4 `verification`)
VERIFY_ORDER = ["not_checked", "desk_check", "fake_or_tests", "local_sim", "hardware"]
VERIFY_WORDS = {"hardware": "hardware test", "local_sim": "local sim you ran", "fake_or_tests": "fake data or tests",
                "desk_check": "desk check", "not_checked": "not checked"}
CLOSE_WORDS = {"done": "DONE", "partial": "PARTIAL", "not_done": "NOT DONE", "answered": "ANSWERED"}
CLOSE_MARK = {"done": "ok", "answered": "ok", "partial": "~ ", "not_done": "x "}
NEED_MARK = {"parked": "?", "over_budget": "?", "question": "?", "flagged": "!", "violation": "!",
             "lost_watcher": "!", "stall": "~"}
CAP = 15            # lines per board section (the rest: "... N more")
LEGACY_HOURS = 24   # needs-you items of runs without an ask: only this recent
TAIL_BUDGET_S = 6.0  # all window tails together (osascript), then fall back to the newest file
BOARD_HEADER = "# Bots board"


def _now() -> datetime:
    return datetime.now().astimezone()


def parse_ts(s) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


def hm(t: datetime | None) -> str:
    return t.astimezone().strftime("%m-%d %H:%M") if t else "?"


def short(s, n: int = 60) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def norm_name(s) -> str:
    """A project or workspace name as a key: lower case, [a-z0-9_.-] (other characters become '-')."""
    s = re.sub(r"[^a-z0-9_.-]+", "-", str(s or "").strip().lower()).strip("-.")
    return s[:64]


def window_short(run_id: str) -> str:
    """'HHMMSS-hex' from a run id: the end of the run's iTerm window title (live.window_title)."""
    parts = str(run_id or "").split("-")
    if len(parts) >= 5 and "T" in parts[2]:
        return f"{parts[2].split('T')[-1]}-{parts[-1]}"
    return str(run_id or "")


def parse_since(text: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d{1,4})\s*([hdw])\s*", str(text or ""))
    if not m or int(m.group(1)) == 0:
        raise Refused(f"--since {text!r}: use a number and h, d or w, for example 1d, 7d, 12h or 2w")
    n, unit = int(m.group(1)), m.group(2)
    return {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]


def read_events(runs_dir: str) -> list:
    """Every event line of the ledger, in file order (also lines with no run_id). Shared lock like ledger.load."""
    out = []
    try:
        with open(ledger.path(runs_dir), encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)  # appends take LOCK_EX: never read half a line
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("event"):
                    out.append(ev)
    except OSError:
        pass
    return out


def ledger_append(runs_dir: str, **fields) -> dict:
    """ledger.append, but a field that ledger.KEYS does not know (yet) goes into the note instead of being lost."""
    known = set(ledger.KEYS)
    extra = {k: v for k, v in fields.items() if k not in known and v not in (None, "", [])}
    if extra:
        bits = "; ".join(f"{k}={v}" for k, v in extra.items())
        fields["note"] = f"{fields['note']}; {bits}" if fields.get("note") else bits
    return ledger.append(runs_dir, **{k: v for k, v in fields.items() if k in known})


def card_line(card: str | None, label: str) -> str:
    """The text after '<label>:' in a finish card (ask close), or ''."""
    for ln in str(card or "").splitlines():
        if ln.strip().lower().startswith(label.lower() + ":"):
            return ln.split(":", 1)[1].strip()
    return ""


def lowest_verification(levels) -> str | None:
    known = [v for v in levels if v in VERIFY_ORDER]
    return min(known, key=VERIFY_ORDER.index) if known else None


# ---- the snapshot: asks and runs from one read of the ledger -----------------------------------------------------

class Snapshot:
    """Asks and runs as they are now. Read-only."""

    def __init__(self, cfg: Config, now: datetime | None = None, events: list | None = None):
        self.cfg = cfg
        self.now = now or _now()
        self.events = read_events(cfg.runs_dir) if events is None else [dict(e) for e in events]
        for i, ev in enumerate(self.events):
            ev["_i"] = i
        self.asks: dict = {}
        self.runs: dict = {}
        self._results: dict = {}
        self._registry = None
        self._index()

    # -- building --
    def _ask(self, aid: str) -> dict:
        a = self.asks.get(aid)
        if a is None:
            a = self.asks[aid] = {"ask_id": aid, "known": False, "title": "", "words": "", "size": None,
                                  "pattern": None, "project": None, "budget": None, "opened": None, "events": [],
                                  "parks": [], "close": None, "answered": None, "later": [], "over": [],
                                  "runs": [], "workspace": None}
        return a

    def _index(self) -> None:
        for ev in self.events:
            name, aid, rid = ev.get("event"), ev.get("ask_id"), ev.get("run_id")
            if rid and name in RUN_KINDS and rid not in self.runs:
                self.runs[rid] = {"run_id": rid, "kind": name, "open": ev, "result": None, "accept": None,
                                  "events": [], "ask_id": None, "agent": ev.get("agent") or ev.get("to"),
                                  "model": ev.get("model"), "model_alias": ev.get("model_alias"),
                                  "lane": ev.get("lane"), "title": ev.get("title") or "",
                                  "workspace": ev.get("workspace"), "parent": ev.get("parent_run_id"),
                                  "started": parse_ts(ev.get("started_at") or ev.get("ts"))}
            r = self.runs.get(rid) if rid else None
            if r is not None:
                r["events"].append(ev)
                if name == "result":
                    r["result"] = ev
                elif name == "accept":
                    r["accept"] = ev
                if aid and not r["ask_id"]:
                    r["ask_id"] = aid
                if ev.get("model_alias") and not r["model_alias"]:
                    r["model_alias"] = ev.get("model_alias")
            if not aid:
                continue
            a = self._ask(aid)
            a["events"].append(ev)
            if name == "ask_open" and not a["known"]:
                a.update(known=True, title=ev.get("title") or "", words=ev.get("words") or "", size=ev.get("size"),
                         pattern=ev.get("pattern"), project=ev.get("project"), budget=ev.get("budget"),
                         opened=parse_ts(ev.get("ts")))
            elif name == "ask_park":
                a["parks"].append(ev)
            elif name == "ask_close" and a["close"] is None:
                a["close"] = ev
            elif name == "answered" and a["answered"] is None:
                a["answered"] = ev
            elif name == "later_add" and (ev.get("note") or "").strip():
                a["later"].append(ev["note"].strip())
            elif name == "over_budget":
                a["over"].append(ev)
        for rid, r in self.runs.items():
            if r["ask_id"]:
                a = self._ask(r["ask_id"])
                a["runs"].append(rid)
                a["workspace"] = a["workspace"] or r["workspace"]

    # -- asks --
    @staticmethod
    def status(a: dict) -> str:
        if a["close"] is not None or a["answered"] is not None:
            return "closed"
        return "parked" if a["parks"] else "open"

    @staticmethod
    def close_status(a: dict) -> str | None:
        if a["close"] is not None:
            return a["close"].get("status") or "done"
        return "answered" if a["answered"] is not None else None

    @staticmethod
    def closed_at(a: dict) -> datetime | None:
        ev = a["close"] or a["answered"]
        return parse_ts(ev.get("ts")) if ev else None

    @staticmethod
    def title(a: dict) -> str:
        return a["title"] or short(a["words"], 40) or "(no title)"

    def project_key(self, a: dict | None, run: dict | None = None) -> str:
        if a is not None and a.get("project"):
            return norm_name(a["project"]) or "(none)"
        ws = (a or {}).get("workspace") or (run or {}).get("workspace")
        return norm_name(ws) if ws else "(none)"

    def run_project(self, r: dict) -> str:
        a = self.asks.get(r["ask_id"]) if r["ask_id"] else None
        return self.project_key(a, r)

    def ask_of(self, r: dict) -> dict | None:
        return self.asks.get(r["ask_id"]) if r.get("ask_id") else None

    def resolve_project(self, name: str) -> str:
        """A project name, or a workspace name or alias (-> its key)."""
        try:
            return self.cfg.workspace(name).key
        except Refused:
            return norm_name(name)

    # -- runs --
    def result_json(self, run_id: str) -> dict:
        if run_id not in self._results:
            try:
                with open(os.path.join(self.cfg.runs_dir, run_id, "result.json"), encoding="utf-8") as f:
                    d = json.load(f)
                self._results[run_id] = d if isinstance(d, dict) else {}
            except (OSError, ValueError):
                self._results[run_id] = {}
        return self._results[run_id]

    def agent_result(self, run_id: str) -> dict:
        ar = self.result_json(run_id).get("agent_result")
        return ar if isinstance(ar, dict) else {}

    def verification(self, run_id: str) -> str | None:
        v = self.agent_result(run_id).get("verification")
        return v if v in VERIFY_ORDER else None

    def alias(self, r: dict) -> str:
        return r.get("model_alias") or r.get("model") or ""

    def registry(self) -> tuple:
        """(live records, stale records) of the run registry; read-only (nothing is cleaned)."""
        if self._registry is None:
            try:
                self._registry = claims.active(self.cfg.runs_dir, clean=False)
            except OSError:
                self._registry = ([], [])
        return self._registry

    def live_ids(self) -> set:
        return {r.get("run_id") for r in self.registry()[0]}

    def recent(self, ev: dict, hours: float = LEGACY_HOURS) -> bool:
        t = parse_ts(ev.get("ts"))
        return bool(t and self.now - t <= timedelta(hours=hours))

    def finished_dead(self, r: dict) -> bool:
        return any(e.get("event") in ("stale", "watchdog_stop") for e in r["events"])

    # -- needs you --
    def needs_you(self) -> list:
        out = []
        for a in sorted(self.asks.values(), key=lambda x: x["opened"] or self.now):
            if not a["known"] or self.status(a) == "closed":
                continue
            t = self.title(a)
            if self.status(a) == "parked":
                p = a["parks"][-1]
                out.append({"kind": "parked", "ask_id": a["ask_id"], "title": t, "ts": p.get("ts"),
                            "text": f"parked ({short(p.get('reason') or p.get('note') or 'no reason given', 80)}). "
                                    f"Close it, or go on only with chalach's words (--over)"})
            refused = [e for e in a["over"] if (e.get("status") or "refused") == "refused"]
            if refused:
                last = refused[-1]
                after = [e for e in a["events"] if e["_i"] > last["_i"]]
                resolved = any((e.get("event") == "over_budget" and e.get("status") == "approved")
                               or e.get("event") in RUN_KINDS for e in after)
                if not resolved:
                    out.append({"kind": "over_budget", "ask_id": a["ask_id"], "title": t, "ts": last.get("ts"),
                                "run_id": last.get("run_id"),
                                "text": "over budget: " + short(last.get("reason") or last.get("note")
                                                                or "a run was refused", 160)})
        for rid, r in self.runs.items():
            a = self.ask_of(r)
            res = r["result"]
            # flagged/violation runs wait for chalach even after `ask close` (it leaves them unaccepted)
            if res and res.get("status") in ("flagged", "violation") and r["accept"] is None:
                if a is not None or self.recent(res):
                    who = " ".join(x for x in (r["agent"], self.alias(r)) if x)
                    out.append({"kind": res["status"], "ask_id": r["ask_id"], "title": (a and self.title(a)) or r["title"],
                                "ts": res.get("ts"), "run_id": rid,
                                "text": f"{who} {r['kind'] if r['kind'] == 'review' else 'run'} {res['status']}, "
                                        f"not accepted yet (run {rid})"})
            if a is not None and self.status(a) == "closed":
                continue
            if res is None and not self.finished_dead(r):
                stalls = [e for e in r["events"] if e.get("event") == "stall"]
                if stalls and (a is not None or self.recent(stalls[-1])):
                    s = stalls[-1]
                    tail = short(s.get("note"), 70)
                    out.append({"kind": "stall", "ask_id": r["ask_id"], "title": (a and self.title(a)) or r["title"],
                                "ts": s.get("ts"), "run_id": rid,
                                "text": f"{r['agent']} run stalled ({s.get('reason') or 'unknown'}) at "
                                        f"{hm(parse_ts(s.get('ts')))[6:]}" + (f": {tail}" if tail else "")})
            if res is None and any(e.get("event") == "stale" for e in r["events"]) \
                    and not any(e.get("event") == "reattach" for e in r["events"]):
                if a is not None or self.recent(r["events"][-1]):
                    out.append({"kind": "lost_watcher", "ask_id": r["ask_id"],
                                "title": (a and self.title(a)) or r["title"], "ts": r["events"][-1].get("ts"),
                                "run_id": rid, "text": f"{r['agent']} run ended without a result (its ck-run "
                                                       f"stopped). Fix: ck-run reattach {rid}"})
        seen_lost = {n["run_id"] for n in out if n["kind"] == "lost_watcher"}
        live_recs, stale_recs = self.registry()
        for rec in stale_recs:  # ck-run gone, agent gone, no `stale` event yet
            rid = rec.get("run_id")
            r = self.runs.get(rid)
            if rid in seen_lost or (r is not None and r["result"] is not None):
                continue
            a = self.ask_of(r) if r else None
            out.append({"kind": "lost_watcher", "ask_id": r and r["ask_id"],
                        "title": (a and self.title(a)) or rec.get("title") or "", "ts": rec.get("started_at"),
                        "run_id": rid, "text": f"{rec.get('agent') or '?'} run ended without a result (its ck-run "
                                               f"stopped). Fix: ck-run reattach {rid}"})
        for rec in live_recs:
            if rec.get("orphan"):
                rid = rec.get("run_id")
                r = self.runs.get(rid)
                a = self.ask_of(r) if r else None
                out.append({"kind": "lost_watcher", "ask_id": r and r["ask_id"],
                            "title": (a and self.title(a)) or rec.get("title") or "", "ts": rec.get("started_at"),
                            "run_id": rid, "text": f"{rec.get('agent') or '?'} agent still works but its ck-run "
                                                   f"stopped. Fix: ck-run reattach {rid}"})
        for a in self.asks.values():  # open questions from results of open asks
            if not a["known"] or self.status(a) == "closed":
                continue
            for rid in a["runs"]:
                r = self.runs[rid]
                if r["result"] is None:
                    continue
                qs = self.agent_result(rid).get("open_questions") or []
                who = self.alias(r) or r["agent"]
                for q in [q for q in qs if isinstance(q, str) and q.strip()][:3]:
                    out.append({"kind": "question", "ask_id": a["ask_id"], "title": self.title(a),
                                "ts": r["result"].get("ts"), "run_id": rid,
                                "text": f"question from {who}: {short(q, 140)}"})
        return out

    # -- running --
    def running(self, tails: bool = True) -> list:
        out = []
        for rec in self.registry()[0]:
            rid = rec.get("run_id")
            r = self.runs.get(rid) or {}
            started = parse_ts(rec.get("started_at")) or r.get("started")
            mins = int((self.now - started).total_seconds() // 60) if started else None
            wid, term, sid, tty = None, None, None, None
            for e in r.get("events") or []:
                if e.get("event") == "live_window_opened":
                    note = e.get("note") or ""
                    m = re.search(r"window_id=(\w+)", note)
                    t = re.search(r"terminal=(\w+)", note)
                    s = re.search(r"session(?:_id)?=([\w:.-]+)", note)
                    y = re.search(r"tty=(/dev/[\w/.-]+)", note)
                    wid, term = (m.group(1) if m else None), (t.group(1) if t else None)
                    sid, tty = (s.group(1) if s else None), (y.group(1) if y else None)
                    if str(sid).lower() in ("none", "unknown", "null"):
                        sid = None
            a = self.ask_of(r) if r else None
            out.append({"run_id": rid, "agent": rec.get("agent") or r.get("agent"), "model_alias": r.get("model_alias"),
                        "model": r.get("model"), "ask_id": r.get("ask_id"),
                        "ask_title": self.title(a) if a else None,
                        "title": rec.get("title") or r.get("title") or "", "minutes": mins,
                        "kind": rec.get("kind") or r.get("kind"), "window": window_short(rid),
                        "window_id": wid if term == "iTerm2" else None, "session_id": sid, "tty": tty,
                        "orphan": bool(rec.get("orphan")), "last": None, "last_from": None})
        if tails:
            self._fill_last(out)
        else:
            for item in out:
                self._newest_file(item)
        out.sort(key=lambda x: -(x["minutes"] or 0))
        return out

    def _fill_last(self, items: list) -> None:
        # One window per ask (several tabs): without the run's own session id or tty, the window's current tab may
        # be another run's, so a shared window's text is not used (the newest file is shown instead).
        shared = {}
        for i in items:
            if i["window_id"]:
                shared[i["window_id"]] = shared.get(i["window_id"], 0) + 1
        want = [i for i in items if (i.get("session_id") or i.get("tty")) and i["window_id"]
                or (i["window_id"] and str(i["window_id"]).isdigit() and shared.get(i["window_id"]) == 1)]
        got = {}
        if want and getattr(self, "tails", True):
            from . import live
            from .guards import child_env
            try:
                env = child_env(self.cfg)
            except Exception:
                env = {"PATH": "/usr/bin:/bin"}

            def one(item):
                try:
                    if item.get("session_id") or item.get("tty"):
                        try:
                            got[item["run_id"]] = live.window_tail(
                                self.cfg.live_osascript, item["window_id"], env, lines=1,
                                session_id=item.get("session_id"), tty=item.get("tty"))
                            return
                        except TypeError:  # an older live.window_tail without session_id / tty
                            if shared.get(item["window_id"]) != 1:
                                got[item["run_id"]] = None
                                return
                    got[item["run_id"]] = live.window_tail(self.cfg.live_osascript, item["window_id"], env, lines=1)
                except Exception:
                    got[item["run_id"]] = None

            threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in want]
            for t in threads:
                t.start()
            deadline = time.time() + TAIL_BUDGET_S
            for t in threads:
                t.join(max(0.0, deadline - time.time()))
        for item in items:
            text = got.get(item["run_id"])
            if text:
                item["last"], item["last_from"] = short(text.split(" | ")[-1], 80), "window"
            else:
                self._newest_file(item)

    def _newest_file(self, item: dict) -> None:
        d = os.path.join(self.cfg.runs_dir, item["run_id"] or "")
        best = None
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_file(follow_symlinks=False) and not e.name.startswith("."):
                        st = e.stat(follow_symlinks=False)
                        if best is None or st.st_mtime > best[1]:
                            best = (e.name, st.st_mtime)
        except OSError:
            return
        if best:
            age = int(max(0, self.now.timestamp() - best[1]) // 60)
            item["last"], item["last_from"] = f"newest file {best[0]} ({age} min ago)", "file"

    # -- done today --
    def done_today(self) -> list:
        out = []
        today = self.now.astimezone().date()
        for a in self.asks.values():
            if not a["known"] or self.status(a) != "closed":
                continue
            t = self.closed_at(a)
            if not t or t.astimezone().date() != today:
                continue
            out.append(self.done_item(a))
        out.sort(key=lambda x: x["closed_at"] or "")
        return out

    def done_item(self, a: dict) -> dict:
        st = self.close_status(a)
        card = (a["close"] or {}).get("card") or ""
        how = card_line(card, "How sure")
        move = card_line(card, "Your move")
        if not how and a["runs"]:
            v = lowest_verification(self.verification(rid) for rid in a["runs"]
                                    if self.runs[rid]["kind"] == "dispatch")
            how = VERIFY_WORDS.get(v, "") if v else ""
        if not move:
            moves = [self.agent_result(rid).get("your_move") for rid in a["runs"]]
            moves = [m.strip() for m in moves if isinstance(m, str) and m.strip()]
            move = next((m for m in moves if m.lower() != "nothing"), "nothing" if moves else "")
        return {"ask_id": a["ask_id"], "title": self.title(a), "status": st, "how_sure": how, "your_move": move,
                "closed_at": (a["close"] or a["answered"] or {}).get("ts"),
                "answered": a["answered"] is not None, "answer": short((a["answered"] or {}).get("note"), 120)}

    def board(self, tails: bool = True) -> dict:
        return {"needs_you": self.needs_you(), "running": self.running(tails=tails), "done_today": self.done_today(),
                "generated_at": self.now.isoformat(timespec="seconds")}


# ---- rendering -------------------------------------------------------------------------------------------------

def _need_line(n: dict) -> str:
    who = f"{n['ask_id']}  {short(n.get('title'), 40)}: " if n.get("ask_id") else (
        f"{short(n.get('title'), 40)}: " if n.get("title") else "")
    return f" {NEED_MARK.get(n['kind'], '?')}  {who}{n['text']}"


def _run_line(r: dict) -> str:
    who = " ".join(x for x in (r["agent"], r.get("model_alias") or r.get("model")) if x)
    mins = f"{r['minutes']} min" if r["minutes"] is not None else "? min"
    bits = [f" >  {who}", r.get("ask_id") or "-", short(r["title"], 40), mins]
    if r.get("last"):
        bits.append(("last: " if r["last_from"] == "window" else "") + r["last"])
    if r.get("orphan"):
        bits.append("its ck-run stopped (ck-run reattach)")
    bits.append(f"[window {r['window']}]")
    return "  ".join(bits)


def _done_line(d: dict) -> str:
    st = d["status"] or "done"
    bits = [f" {CLOSE_MARK.get(st, 'ok')}  {d['ask_id']}", short(d["title"], 40), CLOSE_WORDS.get(st, st.upper())]
    if d.get("answered"):
        bits.append("answered by Grok Bot")
    if d.get("how_sure"):
        bits.append(f"how sure: {short(d['how_sure'], 40)}")
    if d.get("your_move"):
        bits.append(f"your move: {short(d['your_move'], 60)}")
    return "  ".join(bits)


def _section(title: str, items: list, line) -> list:
    L = [f"{title} ({len(items)})"]
    if not items:
        L.append(" -  nothing")
    for it in items[:CAP]:
        L.append(line(it))
    if len(items) > CAP:
        L.append(f" ... {len(items) - CAP} more (ck-run board --json)")
    return L


def board_text(b: dict, now: datetime) -> str:
    L = [f"BOTS BOARD  {now.strftime('%a %m-%d %H:%M')}"]
    L += _section("NEEDS YOU", b["needs_you"], _need_line)
    L += _section("RUNNING", b["running"], _run_line)
    L += _section("DONE TODAY", b["done_today"], _done_line)
    return "\n".join(L) + "\n"


def board_md(b: dict, now: datetime) -> str:
    L = [BOARD_HEADER, "", f"_Updated {now.isoformat(timespec='seconds')} by `ck-run board --md`. Read-only view of the "
                           f"ledger._", ""]
    for title, items, line in (("Needs you", b["needs_you"], _need_line), ("Running", b["running"], _run_line),
                               ("Done today", b["done_today"], _done_line)):
        L.append(f"## {title} ({len(items)})")
        L.append("")
        if not items:
            L.append("- nothing")
        for it in items:
            L.append("- " + line(it).strip())
        L.append("")
    return "\n".join(L)


def write_board_md(path: str, text: str) -> str:
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(os.path.dirname(p)):
        raise Refused(f"--md: folder does not exist: {os.path.dirname(p)}")
    if os.path.isdir(p):
        raise Refused(f"--md: {p} is a folder; give a file path")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                first = f.readline().rstrip("\n")
        except OSError as e:
            raise Refused(f"--md: cannot read {p}: {e}")
        if first != BOARD_HEADER:
            raise Refused(f"--md: {p} exists and is not a board file (first line is not '{BOARD_HEADER}'); "
                          f"not overwritten")
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)
    return p


# ---- digest ------------------------------------------------------------------------------------------------------

def digest(snap: Snapshot, since: timedelta) -> dict:
    start = snap.now - since
    proj: dict = {}

    def P(key: str) -> dict:
        return proj.setdefault(key, {"project": key, "asks_done": [], "runs": 0, "minutes": {}, "levels": [],
                                     "open": [], "later": 0})

    for a in snap.asks.values():
        if not a["known"]:
            continue
        key = snap.project_key(a)
        st = snap.status(a)
        t = snap.closed_at(a)
        if st == "closed" and t and t >= start:
            P(key)["asks_done"].append({"ask_id": a["ask_id"], "title": snap.title(a), "status": snap.close_status(a)})
            P(key)["later"] += len(a["later"])
        elif st != "closed":
            P(key)["open"].append({"ask_id": a["ask_id"], "title": snap.title(a), "status": st})
            P(key)["later"] += len(a["later"])
    for rid, r in snap.runs.items():
        if not r["started"] or r["started"] < start:
            continue
        d = P(snap.run_project(r))
        d["runs"] += 1
        res = r["result"]
        if res and isinstance(res.get("duration_s"), (int, float)):
            label = snap.alias(r) or r["agent"] or "?"
            d["minutes"][label] = d["minutes"].get(label, 0.0) + res["duration_s"] / 60.0
        if res and r["kind"] == "dispatch":
            v = snap.verification(rid)
            if v:
                d["levels"].append(v)
    out = []
    for key, d in proj.items():
        low = lowest_verification(d["levels"])
        out.append({"project": key, "asks_done": d["asks_done"], "runs": d["runs"],
                    "minutes": {k: round(v, 1) for k, v in sorted(d["minutes"].items(), key=lambda kv: -kv[1])},
                    "lowest_verification": low, "how_sure": VERIFY_WORDS.get(low, "") if low else "",
                    "open_asks": d["open"], "later_items": d["later"]})
    out.sort(key=lambda d: (-(len(d["asks_done"]) + d["runs"]), d["project"]))
    return {"since": start.isoformat(timespec="seconds"), "projects": out}


def digest_text(d: dict, label: str) -> str:
    L = [f"DIGEST since {hm(parse_ts(d['since']))} ({label}): {len(d['projects'])} project(s)"]
    if not d["projects"]:
        L.append("nothing happened in this time")
    total = 0.0
    for p in d["projects"]:
        L.append(p["project"])
        done = p["asks_done"]
        if done:
            items = "; ".join(f"{short(x['title'], 40)}: {CLOSE_WORDS.get(x['status'], x['status']).lower()}"
                              for x in done[:4])
            more = f"; +{len(done) - 4} more" if len(done) > 4 else ""
            L.append(f"  asks done: {len(done)} ({items}{more})")
        else:
            L.append("  asks done: 0")
        mins = ", ".join(f"{k} {v:.0f} min" for k, v in p["minutes"].items()) or "none"
        total += sum(p["minutes"].values())
        L.append(f"  runs: {p['runs']}  agent time: {mins}")
        L.append(f"  how sure (lowest): {p['how_sure'] or 'not reported'}")
        opens = p["open_asks"]
        open_txt = ", ".join(f"{o['ask_id']} {short(o['title'], 30)}" + (" (parked)" if o["status"] == "parked" else "")
                             for o in opens[:3]) + (f", +{len(opens) - 3} more" if len(opens) > 3 else "")
        L.append(f"  open: {len(opens)} ask(s){': ' + open_txt if opens else ''}; later items: {p['later_items']}")
    L.append(f"agent time in total: {total:.0f} min")
    return "\n".join(L) + "\n"


# ---- recall ------------------------------------------------------------------------------------------------------

def recall(snap: Snapshot, name: str) -> dict:
    key = snap.resolve_project(name)
    asks = [a for a in snap.asks.values() if a["known"] and snap.project_key(a) == key]
    asks.sort(key=lambda a: a["opened"] or snap.now)
    needs = snap.needs_you()
    running = snap.running(tails=False)
    by_ask_needs: dict = {}
    for n in needs:
        if n.get("ask_id"):
            by_ask_needs.setdefault(n["ask_id"], []).append(n)
    live_asks = {r["ask_id"] for r in running if r.get("ask_id")}
    items = []
    for a in asks:
        st = snap.status(a)
        if st == "closed":
            tag = "done"
        elif st == "parked":
            tag = "parked"
        elif by_ask_needs.get(a["ask_id"]):
            tag = "needs you"
        elif a["ask_id"] in live_asks:
            tag = "running"
        else:
            tag = "open"
        items.append({"ask_id": a["ask_id"], "title": snap.title(a), "tag": tag,
                      "status": snap.close_status(a) if st == "closed" else st})
    later = []
    for a in asks:
        for j, it in enumerate(a["later"], 1):
            later.append({"ask_id": a["ask_id"], "n": j, "item": it, "tag": "later"})
    # capsule (at most 5 lines)
    cap = []
    n_open = sum(1 for i in items if i["tag"] != "done")
    last_ev = max((e for a in asks for e in a["events"]), key=lambda e: e["_i"], default=None)
    if asks:
        cap.append(f"{key}: {len(asks)} ask(s), {n_open} open, {len(asks) - n_open} done. "
                   f"Last activity {hm(parse_ts(last_ev.get('ts')) if last_ev else None)}.")
    else:
        cap.append(f"{key}: no asks yet.")
    done = [a for a in asks if snap.status(a) == "closed"]
    if done:
        last = max(done, key=lambda a: snap.closed_at(a) or snap.now)
        di = snap.done_item(last)
        changed = card_line((last["close"] or {}).get("card"), "Changed")
        if not changed:
            ps = [p for rid in last["runs"] for p in (snap.agent_result(rid).get("plain_summary") or [])
                  if isinstance(p, str)]
            changed = ps[0] if ps else (di.get("answer") or "")
        cap.append(f"Last done: {short(di['title'], 40)} ({hm(snap.closed_at(last))[:5]})"
                   + (f": {short(changed, 80)}" if changed else "")
                   + (f". How sure: {di['how_sure']}" if di.get("how_sure") else ""))
    mine_running = [r for r in running if r.get("ask_id") in {a["ask_id"] for a in asks}]
    if mine_running:
        cap.append("Running now: " + "; ".join(
            f"{' '.join(x for x in (r['agent'], r.get('model_alias')) if x)} on {short(r['title'], 30)} "
            f"({r['minutes']} min)" for r in mine_running[:3]))
    try:
        from . import andon
        st = andon.state(snap.cfg, key)
    except Exception:
        st = None
    if st:
        cap.append(f"ANDON raised {hm(parse_ts(st.get('raised_at')))}: {short(st.get('reason'), 80)}")
    if key in snap.cfg.workspaces:
        week = snap.now - timedelta(days=7)
        legacy = [r for r in snap.runs.values() if not r["ask_id"] and r["workspace"] == key
                  and r["started"] and r["started"] >= week]
        if legacy:
            lr = max(legacy, key=lambda r: r["started"])
            cap.append(f"Runs without an ask in the last 7 days: {len(legacy)} (last: {short(lr['title'], 40)}, "
                       f"{hm(lr['started'])[:5]}).")
    cap = cap[:5]
    # next move
    mine_needs = [n for a in asks for n in by_ask_needs.get(a["ask_id"], [])]
    if mine_needs:
        n = mine_needs[0]
        nxt = f"{n['ask_id']}: {n['text']}"
    elif mine_running:
        nxt = f"wait for {len(mine_running)} running run(s); ck-run board shows them"
    elif any(i["tag"] == "open" for i in items):
        o = next(i for i in items if i["tag"] == "open")
        nxt = f"{o['ask_id']} is open with nothing running: start its next step, or close it (ck-run ask close {o['ask_id']})"
    elif later:
        nxt = "nothing waits; a Later item can start as a new ask (reply \"yes <n>\")"
    else:
        nxt = "nothing"
    return {"project": key, "capsule": cap, "asks": items, "later": later, "next_move": nxt}


def recall_text(d: dict, max_asks: int = 20, max_later: int = 10) -> str:
    """Every ask that is not done, plus the newest done ones up to max_asks lines (--json has all of them)."""
    L = list(d["capsule"])
    asks = d["asks"]
    not_done = [i for i in asks if i["tag"] != "done"]
    done = [i for i in asks if i["tag"] == "done"]
    room = max(0, max_asks - len(not_done))
    keep = {id(i) for i in not_done} | {id(i) for i in (done[-room:] if room else [])}
    hidden = sum(1 for i in done if id(i) not in keep)
    if hidden:
        L.append(f"... {hidden} older done ask(s) not shown (ck-run recall {d['project']} --json)")
    for i in asks:
        if id(i) in keep:
            L.append(f"{i['ask_id']}  {short(i['title'], 50)}  [{i['tag']}]")
    for it in d["later"][:max_later]:  # numbered per ask, like the finish card's Later list ("yes 2")
        L.append(f"  later {it['ask_id']} #{it['n']}: {short(it['item'], 70)}")
    if len(d["later"]) > max_later:
        L.append(f"  ... {len(d['later']) - max_later} more later item(s)")
    L.append(f"Next move: {d['next_move']}")
    return "\n".join(L) + "\n"


# ---- scoreboard ----------------------------------------------------------------------------------------------------

def scoreboard(snap: Snapshot, since: timedelta) -> dict:
    start = snap.now - since
    groups: dict = {}
    key_of = {}
    for rid, r in snap.runs.items():
        if not r["started"] or r["started"] < start:
            continue
        lane = r.get("lane") or ("review" if r["kind"] == "review" else "-")
        key = (lane, snap.alias(r) or "-", r["agent"] or "?")
        key_of[rid] = key
        g = groups.setdefault(key, {"runs": 0, "results": 0, "ok": 0, "decided": 0, "accepted": 0, "minutes": [],
                                    "fix_rounds": 0})
        g["runs"] += 1
        res = r["result"]
        if res:
            g["results"] += 1
            g["ok"] += 1 if res.get("status") == "ok" else 0
            if isinstance(res.get("duration_s"), (int, float)):
                g["minutes"].append(res["duration_s"] / 60.0)
        if r["accept"] is not None:
            g["decided"] += 1
            g["accepted"] += 1 if r["accept"].get("accepted") else 0
    for rid, r in snap.runs.items():  # a follow-up run (--parent) of a run in a group = one fix round for it
        if r["kind"] == "dispatch" and r["parent"] in key_of and rid in key_of:
            groups[key_of[r["parent"]]]["fix_rounds"] += 1
    rows = []
    for (lane, alias, agent), g in groups.items():
        rows.append({"lane": lane, "model": alias, "agent": agent, "runs": g["runs"], "results": g["results"],
                     "ok": g["ok"], "ok_rate": round(g["ok"] / g["results"], 3) if g["results"] else None,
                     "accepted": g["accepted"], "decided": g["decided"],
                     "accept_rate": round(g["accepted"] / g["decided"], 3) if g["decided"] else None,
                     "median_min": round(statistics.median(g["minutes"]), 1) if g["minutes"] else None,
                     "fix_rounds": g["fix_rounds"]})
    rows.sort(key=lambda x: (-x["runs"], x["lane"], x["model"]))
    return {"since": start.isoformat(timespec="seconds"), "rows": rows}


def _pct(n, d) -> str:
    return f"{n}/{d} {100 * n / d:.0f}%" if d else "-"


def scoreboard_text(s: dict, label: str) -> str:
    rows = s["rows"]
    L = [f"SCOREBOARD since {hm(parse_ts(s['since']))} ({label}): {sum(r['runs'] for r in rows)} run(s)"]
    if not rows:
        L.append("no runs in this time")
        return "\n".join(L) + "\n"
    head = ["lane", "model", "agent", "runs", "ok", "accepted", "median min", "fix rounds"]
    table = [head] + [[r["lane"], r["model"], r["agent"], str(r["runs"]), _pct(r["ok"], r["results"]),
                       _pct(r["accepted"], r["decided"]),
                       "-" if r["median_min"] is None else f"{r['median_min']:.0f}", str(r["fix_rounds"])]
                      for r in rows]
    widths = [min(24, max(len(row[i]) for row in table)) for i in range(len(head))]
    for row in table:
        L.append("  ".join(short(c, widths[i]).ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    L.append("ok = status ok of runs with a result; accepted = accepted of runs CK or chalach decided on; "
             "fix rounds = follow-up runs (--parent).")
    return "\n".join(L) + "\n"


# ---- commands ------------------------------------------------------------------------------------------------------

def _print(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def cmd_board(cfg: Config, a) -> int:
    snap = Snapshot(cfg)
    snap.tails = not getattr(a, "no_tail", False)
    b = snap.board()
    if a.md is not None:
        path = a.md or os.path.join(cfg.local_root, "BOARD.md")
        p = write_board_md(path, board_md(b, snap.now))
        print(f"ck-run: board written to {p}", file=sys.stderr)
    _print(json.dumps(b, indent=2, ensure_ascii=False, default=str) + "\n" if a.json else board_text(b, snap.now))
    return 0


def cmd_digest(cfg: Config, a) -> int:
    snap = Snapshot(cfg)
    d = digest(snap, parse_since(a.since))
    _print(json.dumps(d, indent=2, ensure_ascii=False) + "\n" if a.json else digest_text(d, a.since))
    return 0


def cmd_recall(cfg: Config, a) -> int:
    name = (a.project or "").strip()
    if not name:
        raise Refused("recall needs a project name, for example: ck-run recall fnd_sin")
    snap = Snapshot(cfg)
    d = recall(snap, name)
    _print(json.dumps(d, indent=2, ensure_ascii=False) + "\n" if a.json else recall_text(d))
    return 0


def cmd_scoreboard(cfg: Config, a) -> int:
    snap = Snapshot(cfg)
    s = scoreboard(snap, parse_since(a.since))
    _print(json.dumps(s, indent=2, ensure_ascii=False) + "\n" if a.json else scoreboard_text(s, a.since))
    return 0


def register(sub) -> None:
    b = sub.add_parser("board", help="read-only: what needs you, what runs now, what was done today")
    b.add_argument("--json", action="store_true", help="a dict with needs_you, running, done_today")
    b.add_argument("--no-tail", action="store_true", help="skip reading the iTerm window tails (quick; the grokbot status line uses it)")
    b.add_argument("--md", nargs="?", const="", default=None, metavar="PATH",
                   help="also write the board as markdown to PATH (default <local_root>/BOARD.md)")
    b.set_defaults(func=cmd_board)
    d = sub.add_parser("digest", help="read-only: per project: asks done, runs, agent minutes, how sure, open items")
    d.add_argument("--since", default="1d", help="1d (default), 7d, 12h, 2w")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_digest)
    r = sub.add_parser("recall", help="read-only: catch up on one project (at most 5 lines, then its asks)")
    r.add_argument("project", help="the ask project (e.g. fnd_sin) or a workspace (devfab, nv, washu)")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_recall)
    s = sub.add_parser("scoreboard", help="read-only: per lane, model and agent: runs, ok, accepted, minutes, fixes")
    s.add_argument("--since", default="30d", help="30d (default), 7d, ...")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_scoreboard)
