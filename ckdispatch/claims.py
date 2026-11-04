"""Concurrent runs (chalach 2026-11-03): any number of runs, any agents, same or different workspaces, always.

There is no workspace lock. The one conflict ck-run prevents is two live runs editing the same file:

- A run that changes files (--mode auto or edit) may claim paths: `ck-run run --claim PATH` (repeatable; a file or
  a folder, relative to the workspace root or absolute; it need not exist yet). A claim that overlaps a claim of a
  live run (same path, or one inside the other) is refused: ledger event `busy`, exit 6, nothing started. Read runs
  and reviews never claim and never block anyone.
- Automatic claims (chalach 2026-11-03 19:02 CT: "Yes, have edit runs claim their write folders automatically"): an
  edit run, and an auto run given an explicit --write-scope, also claims its --write-scope folders, so two edit runs
  in the same folder are refused before they start. Never claimed automatically, because that would serialize every
  run in a workspace (the point is parallel agents): a workspace root, a workspace's default write folders
  (writable_only, e.g. nv `NV Project/` and `AQS/`, each washu `<course>/solutions/`), a folder that holds a wiki
  entry file (wiki_first / open_questions: index, retrieval guide, open questions; e.g. devfab `Design Library/`,
  nv `NV Project/wiki/`), which every run reads and updates, ~/PhD_local, or a folder around any of these. Auto runs
  without --write-scope claim nothing. cmd.txt lists such a scope as not claimed automatically; a narrower
  subfolder, or --claim, still claims.
- Every run (claims or not) has a record <runs_dir>/.claims/<run_id>.json while ck-run works on it. ck-run holds an
  flock on that file for the whole run. A record whose flock is free belongs to a ck-run that crashed or was killed:
  the next run (or `ck-run active`) deletes it and logs a `stale` ledger event for that run. If the agent CLI outlived
  its ck-run (the headless child's recorded pid, or a live window's live.pid without live.exit), the record stays
  until that process ends (the agent may still be editing).
- Check-and-register happens under <runs_dir>/.claims/registry.lock, held for a few milliseconds only (a mutex for
  the registry folder, not a run lock).
- After a run, ck-run's disk scan sees every change in the workspace, also other runs' changes. attribute() moves
  a change to another run when it lies in that run's claim, or when only a concurrent writer could have made it (a
  read run never writes; a writer's change outside this run's write area). same_file() flags a file this run's agent
  reported changing that a concurrent run claimed or also reported changing.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from datetime import datetime

from . import ledger, rulecheck
from .config import Config, Workspace, inside, overlap
from .errors import Refused

DIR = ".claims"
REGISTRY_LOCK = "registry.lock"
WRITER_MODES = ("auto", "edit")
SLACK_S = 1.0  # ledger times have 1 s resolution


def claims_dir(runs_dir: str) -> str:
    return os.path.join(runs_dir, DIR)


def record_path(runs_dir: str, run_id: str) -> str:
    return os.path.join(claims_dir(runs_dir), f"{run_id}.json")


# ---- which paths a run may write --------------------------------------------------------------------------

def may_write(cfg: Config, ws: Workspace, mode: str, scopes: list, path: str) -> bool:
    """Would a change at `path` be inside this run's write area? (The rule check's own answer.)"""
    if mode not in WRITER_MODES:
        return False
    r = rulecheck.classify(cfg, ws, mode, scopes, [{"path": path, "dir": False}], os.path.join(cfg.runs_dir, DIR))
    return bool(r["changed_in_scope"])


def normalize(cfg: Config, ws: Workspace, mode: str, kind: str, scopes: list, raw: list) -> list:
    """--claim values -> real absolute paths inside this run's write area (refused otherwise)."""
    if not raw:
        return []
    if mode not in WRITER_MODES or kind == "review":
        raise Refused("--claim is for runs that change files (--mode auto or edit); read runs never block anyone")
    out = []
    for c in raw:
        s = str(c).strip()
        if not s:
            raise Refused("--claim is empty")
        p = os.path.expanduser(s)
        p = os.path.normpath(p if os.path.isabs(p) else os.path.join(ws.root, p))
        if not may_write(cfg, ws, mode, scopes, p):
            raise Refused(f"--claim {s} is outside this run's write area (raw/read-only folder, rule file, "
                          f"outside --write-scope or outside {ws.name})")
        out.append(os.path.realpath(p))
    return merge(out)


def merge(*lists) -> list:
    """Claims from several lists, in order, without duplicates; a folder covers the paths inside it."""
    out = list(dict.fromkeys(p for lst in lists for p in lst))
    return [p for p in out if not any(q != p and inside(p, q) for q in out)]


# ---- automatic claims: the write folders of an edit run (chalach 2026-11-03 19:02 CT) ----------------------------

def _wiki_files(ws: Workspace) -> list:
    """A workspace's wiki entry files (wiki_first and open_questions: index, retrieval guide, open questions)."""
    return list(dict.fromkeys(list(ws.wiki_first) + ([ws.open_questions] if ws.open_questions else [])))


def shared_area(cfg: Config, literal: str, real: str) -> str | None:
    """Why a write folder is never claimed automatically (None = claim it): it is, or holds, something every run in a
    workspace may write (its root, a default write folder, a wiki entry file), so claiming it would serialize them."""
    def holds(lit: str, rl: str) -> bool:
        return inside(lit, literal) or inside(rl, real) or inside(lit, real) or inside(rl, literal)

    for w in cfg.workspaces.values():
        if holds(w.root, w.real_root):
            return f"the root of {w.name}"
        for e in w.writable_only:
            if holds(e.literal, e.real):
                return f"a default write folder of {w.name} ({e.rel}/), shared by every run there"
        for f in _wiki_files(w):
            if holds(f, os.path.realpath(f)):
                return (f"it holds {os.path.relpath(f, w.root)}, a wiki entry file every run in {w.name} reads "
                        f"and updates")
    if holds(cfg.local_root, cfg.real_local_root):
        return "~/PhD_local"
    return None


def from_scopes(cfg: Config, mode: str, kind: str, explicit: bool, scopes: list) -> tuple:
    """(claims, skipped [(path, why)]): the --write-scope folders this run claims automatically. Edit runs, and auto
    runs given an explicit --write-scope; never reviews, read runs, auto runs on their default area, or a shared
    area (shared_area())."""
    if kind == "review" or mode not in WRITER_MODES or (mode == "auto" and not explicit):
        return [], []
    got, skipped = [], []
    for s in scopes:
        why = shared_area(cfg, s.get("literal") or s["path"], s["path"])
        if why:
            skipped.append((s["path"], why))
        else:
            got.append(s["path"])
    return merge(got), skipped


# ---- the registry -------------------------------------------------------------------------------------------

@contextlib.contextmanager
def _registry(runs_dir: str, create: bool = True):
    d = claims_dir(runs_dir)
    if create:
        os.makedirs(d, mode=0o700, exist_ok=True)
    elif not os.path.isdir(d):
        yield None
        return
    fd = os.open(os.path.join(d, REGISTRY_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield d
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_int(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _orphan_pid(rec: dict):
    """The pid of an agent CLI that outlived its ck-run, if any: the headless child ck-run recorded, or the live
    window's live.sh (live.pid, and no live.exit yet)."""
    if _pid_alive(rec.get("agent_pid")):
        return int(rec["agent_pid"])
    rd = rec.get("run_dir")
    if isinstance(rd, str) and rd and not os.path.exists(os.path.join(rd, "live.exit")):
        pid = _read_int(os.path.join(rd, "live.pid"))
        if _pid_alive(pid):
            return pid
    return None


def _read_fd(fd: int) -> dict:
    chunks = []
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        b = os.read(fd, 65536)
        if not b:
            break
        chunks.append(b)
    try:
        rec = json.loads(b"".join(chunks).decode("utf-8"))
        return rec if isinstance(rec, dict) else {}
    except ValueError:
        return {}


def _scan(d: str, clean: bool) -> tuple:
    """(live records, stale records). Call under the registry lock."""
    live, stale = [], []
    for n in sorted(os.listdir(d)):
        if not (n.endswith(".json") or n.endswith(".tmp")):
            continue
        p = os.path.join(d, n)
        try:
            fd = os.open(p, os.O_RDONLY)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                free = True
            except OSError:
                free = False
            if n.endswith(".tmp"):  # a ck-run died between creating and publishing its record
                if free and clean:
                    os.unlink(p)
                continue
            rec = _read_fd(fd)
            rec.setdefault("run_id", n[:-len(".json")])
            rec["claims"] = [c for c in rec.get("claims") or [] if isinstance(c, str)]
            if free:
                orphan = _orphan_pid(rec)
                if orphan:
                    rec["orphan"], rec["agent_pid"] = True, orphan  # ck-run is gone, its agent still runs: keep claims
                    free = False
            if free:
                if clean:
                    os.unlink(p)
                stale.append(rec)
            else:
                live.append(rec)
        finally:
            os.close(fd)
    return live, stale


class Held:
    """This run's record, flock-held until release()."""

    def __init__(self, runs_dir: str, path: str, fd: int, record: dict):
        self.runs_dir, self.path, self.fd, self.record = runs_dir, path, fd, record

    def update(self, **fields) -> None:
        if self.fd is None:
            return
        self.record.update(fields)
        data = json.dumps(self.record, ensure_ascii=False).encode("utf-8")
        with _registry(self.runs_dir):  # readers scan under the same lock: no torn reads
            os.ftruncate(self.fd, 0)
            os.pwrite(self.fd, data, 0)

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            with _registry(self.runs_dir):  # unlink before unlocking, under the registry lock: never seen as stale
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.path)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def conflicts(mine: list, live: list) -> list:
    """[(my claim, their claim, their record)] for every overlap."""
    return [(m, t, rec) for rec in live for t in rec.get("claims") or [] for m in mine if overlap(m, t)]


def acquire(runs_dir: str, record: dict) -> dict:
    """Clean stale records, refuse overlapping claims, else publish this run's record (flock held).
    Returns {"ok", "held" (Held or None), "conflicts", "stale", "others" (other live records)}."""
    rec = dict(record)
    rec["claims"] = list(rec.get("claims") or [])
    rec.setdefault("pid", os.getpid())
    rec.setdefault("started_at", ledger.now_iso())
    path = record_path(runs_dir, rec["run_id"])
    with _registry(runs_dir) as d:
        live, stale = _scan(d, clean=True)
        live = [r for r in live if r.get("run_id") != rec["run_id"]]
        clash = conflicts(rec["claims"], live)
        if clash:
            return {"ok": False, "held": None, "conflicts": clash, "stale": stale, "others": live}
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, json.dumps(rec, ensure_ascii=False).encode("utf-8"))
            os.replace(tmp, path)  # the flock stays on the inode
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    return {"ok": True, "held": Held(runs_dir, path, fd, rec), "conflicts": [], "stale": stale, "others": live}


def active(runs_dir: str, clean: bool = False) -> tuple:
    """(live records, stale records) right now. clean=True also deletes the stale ones."""
    with _registry(runs_dir, create=clean) as d:
        if d is None:
            return [], []
        return _scan(d, clean=clean)


def log_stale(runs_dir: str, stale: list, by: str) -> None:
    for s in stale:
        ledger.append(runs_dir, event="stale", run_id=s.get("run_id"), title=s.get("title"),
                      workspace=s.get("workspace"), task_type=s.get("task_type"), agent=s.get("agent"),
                      mode=s.get("mode"), write_scope=s.get("write_scope"), claims=s.get("claims"), status="failed",
                      note=(f"ck-run (pid {s.get('pid')}) ended without a result (crash or kill); its run record and "
                            f"claims {s.get('claims') or []} were released by {by}")[:1000],
                      **{"from": "CK", "to": s.get("agent")})


def describe(rec: dict) -> str:
    bits = [f"run {rec.get('run_id')}", rec.get("agent") or "?", f"mode {rec.get('mode') or '?'}",
            f"pid {rec.get('pid')}"]
    if rec.get("orphan"):
        bits.append(f"ck-run gone, agent pid {rec.get('agent_pid')} still running")
    if rec.get("started_at"):
        bits.append(f"since {rec['started_at']}")
    return ", ".join(bits)


# ---- after a run: who else was there, and whose changes are whose ----------------------------------------------

def _ts(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


def concurrent(cfg: Config, run_id: str, t_start: float, t_end: float) -> list:
    """Other runs whose working time overlapped [t_start, t_end]: live records now, plus runs the ledger shows
    as finished (result) or dead (stale) inside the window. A run with neither and no live record is ignored
    (a crash from before the registry existed)."""
    out = {}
    live, _ = active(cfg.runs_dir)
    for r in live:
        if r.get("run_id") == run_id:
            continue
        st = _ts(r.get("started_at"))
        if st is not None and st > t_end + SLACK_S:
            continue
        out[r["run_id"]] = dict(r, finished=False)
    for rid, run in ledger.runs(ledger.load(cfg.runs_dir)).items():
        if rid == run_id:
            continue
        op = run.first("dispatch", "review")
        end_ev = run.last("result") or run.last("stale")
        if op is None or end_ev is None:
            continue
        st = _ts(op.get("started_at") or op.get("ts"))
        en = _ts(end_ev.get("finished_at") or end_ev.get("ts"))
        if st is None or en is None or st > t_end + SLACK_S or en < t_start - SLACK_S:
            continue
        rec = out.get(rid) or {}
        out[rid] = {"run_id": rid, "agent": op.get("agent"), "workspace": op.get("workspace"),
                    "mode": op.get("mode"), "write_scope": op.get("write_scope") or [],
                    "claims": op.get("claims") or rec.get("claims") or [], "pid": rec.get("pid"),
                    "started_at": op.get("started_at"), "finished": end_ev.get("event") == "result"}
    return sorted(out.values(), key=lambda r: r.get("started_at") or "")


def _other_may_write(cfg: Config, rec: dict, path: str) -> bool:
    ws = cfg.workspaces.get(rec.get("workspace") or "")
    if ws is None or rec.get("mode") not in WRITER_MODES:
        return False
    scopes = [{"path": s, "literal": s} for s in rec.get("write_scope") or []]
    return may_write(cfg, ws, rec["mode"], scopes, path)


def owner(cfg: Config, ws: Workspace, mode: str, scopes: list, my_claims: list, path: str, others: list):
    """Who made a change this run's scan found? (None = this run, how) or (other record, how)."""
    rp = os.path.realpath(path)
    if any(inside(rp, c) or inside(path, c) for c in my_claims):
        return None, "own claim"
    for o in others:
        if any(inside(rp, c) or inside(path, c) for c in o.get("claims") or []):
            return o, "its claim"
    could = [o for o in others if _other_may_write(cfg, o, path)]
    if not could:
        return None, "only this run could"
    if mode in WRITER_MODES and may_write(cfg, ws, mode, scopes, path):
        return None, "ambiguous:" + ",".join(o["run_id"] for o in could)
    return could[0], ("a read-only run does not write" if mode not in WRITER_MODES
                      else "outside this run's write area, inside theirs")


def attribute(cfg: Config, ws: Workspace, mode: str, scopes: list, my_claims: list, changed: list,
              others: list) -> dict:
    """Split the scan's changes: {"mine": [entries], "theirs": {run_id: [paths]}, "ambiguous": {path: [ids]},
    "notes": [...]}."""
    mine, theirs, ambiguous, why = [], {}, {}, {}
    for c in changed:
        o, how = owner(cfg, ws, mode, scopes, my_claims, c["path"], others) if others else (None, "")
        if o is None:
            mine.append(c)
            if how.startswith("ambiguous:") and not c.get("dir"):
                ambiguous[c["path"]] = how.split(":", 1)[1].split(",")
            continue
        theirs.setdefault(o["run_id"], []).append(c["path"])
        why.setdefault(o["run_id"], (o, how))
    notes = []
    for rid, paths in theirs.items():
        o, how = why[rid]
        notes.append(f"{len(paths)} change(s) attributed to concurrent run {rid} ({o.get('agent')}, {how}), "
                     f"not this run: " + ", ".join(paths[:5]) + (" ..." if len(paths) > 5 else ""))
    if ambiguous:
        ids = sorted({i for v in ambiguous.values() for i in v})
        notes.append(f"{len(ambiguous)} changed file(s) could be this run's or concurrent run(s) {', '.join(ids)}'s "
                     f"(both could write there; no claims): " + ", ".join(list(ambiguous)[:5])
                     + (" ..." if len(ambiguous) > 5 else ""))
    return {"mine": mine, "theirs": theirs, "ambiguous": ambiguous, "notes": notes}


def _reported(cfg: Config, rec: dict) -> set:
    """Files a finished run's agent reported changing (its result.json)."""
    try:
        with open(os.path.join(cfg.runs_dir, rec["run_id"], "result.json"), encoding="utf-8") as f:
            res = json.load(f)
    except (OSError, ValueError, KeyError):
        return set()
    ws = cfg.workspaces.get(rec.get("workspace") or "")
    root = ws.root if ws else "/"
    out = set()
    for p in res.get("files_changed_reported") or []:
        if isinstance(p, str) and p.strip():
            p = os.path.expanduser(p.strip().strip("`"))
            out.add(os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p)))
    return out


def same_file(cfg: Config, my_reported: list, others: list) -> list:
    """Flags: a file this run's agent reported changing that a concurrent run claimed or also reported changing."""
    flags = []
    mine = list(dict.fromkeys(os.path.realpath(p) for p in my_reported))
    for o in others:
        theirs = _reported(cfg, o) if o.get("finished") else set()
        for f in mine:
            c = next((c for c in o.get("claims") or [] if inside(f, c)), None)
            if c:
                flags.append(f"edited a file claimed by concurrent run {o['run_id']} ({o.get('agent')}; claim {c}): {f}")
            elif f in theirs:
                flags.append(f"same file edited by this run and concurrent run {o['run_id']} ({o.get('agent')}): {f}")
    return flags
