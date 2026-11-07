"""ck-run reattach: finish runs whose ck-run watcher died (ck-run v4, BUILD_SPEC_v4 10.6, the E2 side).

    ck-run reattach [RUN] [--since 24h]

A run's watcher is the ck-run process that started it. It holds the lock on the run's record in
<runs_dir>/.claims/ for the whole run (claims.py). Before v4 a Claude restart killed it, and nobody logged the
result of an agent that kept working in its window. For each run that has a dispatch or review event and no result
(without RUN: the runs started in --since, default 24h; with RUN: that run, any age):
  - its ck-run still runs (the record's lock is held)       -> nothing to do;
  - its agent still runs (live.pid without live.exit, or the headless agent pid of an orphan record)
                                                             -> start a detached waiter (`ck-run launch`-style, slug
                                                                reattach-<HHMMSS>-<hex>): it waits until the agent
                                                                ends, then finishes the run;
  - else                                                     -> finish it now: runner.finalize_run(cfg, run_id,
                                                                note=...) reads what is on disk, runs the rule
                                                                check, writes the result event, prints the summary.
Ledger `reattach` (run_id, ask_id, note) before each waiter or finish. A run that has a result already: nothing to
do, so reattach is safe to rerun. Stale run records are released first and logged (`stale`), as `ck-run active`
does, so the ledger reads: dispatch, stale, reattach, result.

The waiter (`ck-run reattach RUN --wait`, started only by reattach) holds <run>/reattach.lock (one waiter per run)
and checks the agent every few seconds, for at most 8 h. A live agent that wrote summary.md and then showed no
activity for live.close_tab_after_s (the time after which ck-run closes a finished run's tab; 0 = never) has
finished its task and only sits in its window: the waiter stops it, then finishes the run.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import sys
import time
from datetime import datetime

from . import claims, launch, ledger, live, runner
from .config import Config
from .errors import Refused
from .guards import child_env

WAIT_CAP_S = 8 * 3600  # the mod also gives up on a launch after 8 h
DEFAULT_SINCE = "24h"
_SINCE_RX = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.I)


def parse_since(text: str) -> float:
    """'24h', '3d', '90m', '45s' (a bare number = hours) -> seconds."""
    m = _SINCE_RX.match(str(text or ""))
    if not m:
        raise Refused(f"--since must look like 24h, 3d or 90m, not {text!r}")
    unit = (m.group(2) or "h").lower()
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _alive(pid) -> bool:
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


def _ts(s):
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


def _run_dir(cfg: Config, run_id: str) -> str:
    return os.path.join(cfg.runs_dir, run_id)


def agent_pid(run_dir: str, rec: dict | None):
    """The pid of the run's agent if it still runs: an orphan record's headless agent, or the live window's live.sh
    (live.pid and no live.exit). None = gone (or never recorded)."""
    if rec and rec.get("orphan") and _alive(rec.get("agent_pid")):
        return int(rec["agent_pid"])
    if not os.path.exists(os.path.join(run_dir, "live.exit")):
        pid = live._read_int(os.path.join(run_dir, "live.pid"))
        if pid and _alive(pid):
            return pid
    return None


def state_of(cfg: Config, run, live_recs: list) -> tuple:
    """(state, detail): finished | watched (its ck-run holds the record) | agent_running | gone."""
    res = run.last("result")
    if res is not None:
        return "finished", {"status": res.get("status")}
    rec = next((r for r in live_recs if r.get("run_id") == run.run_id), None)
    if rec is not None and not rec.get("orphan"):
        return "watched", {"pid": rec.get("pid")}
    pid = agent_pid(_run_dir(cfg, run.run_id), rec)
    if pid:
        return "agent_running", {"agent_pid": pid}
    return "gone", {}


def release_stale(cfg: Config) -> list:
    """Release run records whose ck-run and agent are gone (ledger `stale`), like `ck-run active`. Returns the
    live records (ck-run alive, or ck-run gone and the agent still running: orphan)."""
    live_recs, stale = claims.active(cfg.runs_dir, clean=True)
    claims.log_stale(cfg.runs_dir, stale, by="ck-run reattach")
    return live_recs


def _event(cfg: Config, run, note: str, status: str) -> None:
    op = run.first("dispatch", "review") or {}
    ledger.append(cfg.runs_dir, event="reattach", run_id=run.run_id, ask_id=op.get("ask_id"), agent=op.get("agent"),
                  title=op.get("title"), workspace=op.get("workspace"), status=status, note=note[:1000],
                  **{"from": "CK", "to": op.get("agent")})


def finalize(cfg: Config, run_id: str, note: str) -> int:
    """runner.finalize_run (contract 10.6, E1): finish the run from disk, print its summary, return its exit code."""
    fn = getattr(runner, "finalize_run", None)
    if fn is None:
        raise Refused("this ck-run has no runner.finalize_run (the v4 core is not installed); the run stays open")
    return fn(cfg, run_id, note=note)


def _print_result(res: dict) -> None:
    p = res.get("summary_path")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            sys.stdout.write(p + "\n" + "\n".join(f.read().splitlines()[:10]) + "\n")
    except (OSError, TypeError):
        pass


def _waiter_running(run_dir: str) -> bool:
    try:
        fd = os.open(os.path.join(run_dir, "reattach.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def waiter_slug(cfg: Config, run_id: str) -> str:
    """reattach-<HHMMSS>-<hex> (the run id's tail); -2, -3, ... when that slug was used before."""
    parts = run_id.split("-")
    if len(parts) >= 5 and "T" in parts[2]:
        tail = f"{parts[2].split('T')[-1]}-{parts[-1]}"
    else:
        tail = re.sub(r"[^A-Za-z0-9_.-]", "", run_id)[-16:] or "run"
    base = f"reattach-{tail}"[:60]
    for n in range(1, 10):
        slug = base if n == 1 else f"{base}-{n}"
        if launch.slug_used(cfg, slug) is None:
            return slug
    raise Refused(f"every waiter slug {base}..-9 was used; check {cfg.runs_dir}/{base}*.out")


def reattach_run(cfg: Config, run_id: str, live_recs: list) -> int:
    """One run (see the module doc). Prints one line (plus finalize_run's output). Returns an exit code."""
    if not runner.valid_run_id(run_id):
        raise Refused(f"not a valid run id: {run_id}")
    run = ledger.find_run(cfg.runs_dir, run_id)
    if run is None:
        raise Refused(f"run {run_id} is not in the ledger")
    op = run.first("dispatch", "review")
    if op is None:
        raise Refused(f"run {run_id} never started (no dispatch or review event)")
    state, d = state_of(cfg, run, live_recs)
    if state == "finished":
        print(f"{run_id}: already finished ({d['status']}); nothing to do")
        _print_result(run.last("result"))
        return runner.EXIT.get(d["status"], 0)
    if state == "watched":
        print(f"{run_id}: its ck-run (pid {d['pid']}) still watches it; nothing to do")
        return 0
    run_dir = _run_dir(cfg, run_id)
    if state == "agent_running":
        if _waiter_running(run_dir):
            print(f"{run_id}: the agent still runs (pid {d['agent_pid']}) and a reattach waiter already waits for it")
            return 0
        slug = waiter_slug(cfg, run_id)
        _event(cfg, run, f"ck-run is gone; the agent still runs (pid {d['agent_pid']}): started a waiter "
                         f"({slug}) that finishes the run when the agent ends", "waiting")
        info = launch.start(cfg, slug, ["reattach", run_id, "--wait"], ask_id=op.get("ask_id"), cwd=cfg.runs_dir)
        print(f"{run_id}: the agent still runs (pid {d['agent_pid']}); waiter started: slug {slug}, "
              f"pid {info['pid']}, output {info['out']}")
        return 0
    _event(cfg, run, "ck-run is gone and so is the agent: finishing the run from what is on disk", "finalizing")
    print(f"{run_id}: ck-run and the agent are gone; finishing it from what is on disk")
    sys.stdout.flush()
    return finalize(cfg, run_id, "ck-run watcher gone; the agent had ended")


def reattach_all(cfg: Config, since_s: float, since_text: str = DEFAULT_SINCE) -> int:
    """Every unfinished run started in the last since_s seconds. Returns the highest exit code (0 = all fine)."""
    live_recs = release_stale(cfg)
    cutoff = time.time() - since_s
    cands = []
    for rid, run in ledger.runs(ledger.load(cfg.runs_dir)).items():
        op = run.first("dispatch", "review")
        if op is None or run.last("result") is not None:
            continue
        ts = _ts(op.get("started_at") or op.get("ts"))
        if ts is not None and ts >= cutoff:
            cands.append((ts, rid))
    if not cands:
        print(f"reattach: no unfinished run started in the last {since_text}; nothing to do")
        return 0
    worst = 0
    for _, rid in sorted(cands):
        try:
            code = reattach_run(cfg, rid, live_recs)
        except Refused as e:
            print(f"{rid}: REFUSED: {e}", file=sys.stderr)
            code = 2
        except Exception as e:  # one broken run must not stop the others
            print(f"{rid}: ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            code = 3
        worst = max(worst, code)
        sys.stdout.flush()
    return worst


def _activity(cfg: Config, run, run_dir: str, every: float):
    """Activity of an orphan live run, for the waiter's idle rule (window found from live_window_opened)."""
    from .watchdog import Activity
    op = run.first("dispatch", "review") or {}
    note = (run.last("live_window_opened") or {}).get("note") or ""
    mw, mt = re.search(r"window_id=(\d+)", note), re.search(r"terminal=([A-Za-z0-9]+)", note)
    return Activity(run_dir, agent=op.get("agent") or "", run_id=run.run_id, home=cfg.home,
                    osascript=cfg.live_osascript, env=child_env(cfg), iterm=live._is_iterm(mt.group(1) if mt else ""),
                    window_id=mw.group(1) if mw else None, every=every)


def wait_and_finalize(cfg: Config, run_id: str) -> int:
    """The detached waiter: wait until the agent ends (or a finished live agent sits idle), then finalize."""
    if not runner.valid_run_id(run_id):
        raise Refused(f"not a valid run id: {run_id}")
    run_dir = _run_dir(cfg, run_id)
    if not os.path.isdir(run_dir):
        raise Refused(f"no run folder {run_dir}")
    fd = os.open(os.path.join(run_dir, "reattach.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(f"{run_id}: another reattach waiter already waits for it")
            return 0
        poll = max(float(cfg.live_poll_s), 0.2)
        try:
            idle_s = float(live.live_setting("close_tab_after_s", 600, cfg) or 0)
        except (TypeError, ValueError):
            idle_s = 0.0
        t_end = time.monotonic() + WAIT_CAP_S
        act, quiet_since = None, None
        note = "ck-run watcher gone; the waiter saw the agent end"
        while True:
            run = ledger.find_run(cfg.runs_dir, run_id)
            if run is None:
                raise Refused(f"run {run_id} is not in the ledger")
            live_recs, _ = claims.active(cfg.runs_dir)
            state, d = state_of(cfg, run, live_recs)
            if state == "finished":
                print(f"{run_id}: already finished ({d['status']}); nothing to do")
                return runner.EXIT.get(d["status"], 0)
            if state == "watched":
                print(f"{run_id}: a ck-run (pid {d['pid']}) watches it again; nothing to do")
                return 0
            if state == "gone":
                break
            live_run = os.path.exists(os.path.join(run_dir, "live.pid"))
            if idle_s > 0 and live_run and os.path.exists(os.path.join(run_dir, "summary.md")):
                now = time.monotonic()
                if act is None:
                    act, quiet_since = _activity(cfg, run, run_dir, min(30.0, max(poll, idle_s / 6))), now
                elif act.poll(now, now - quiet_since):
                    quiet_since = now
                elif now - quiet_since >= idle_s:
                    res = live.stop(run_dir)
                    note = (f"ck-run watcher gone; the agent wrote summary.md, then showed no activity for "
                            f"{int(idle_s)} s, so the waiter stopped it ({res})")
                    break
            if time.monotonic() > t_end:
                _event(cfg, run, f"gave up after {WAIT_CAP_S // 3600} h: the agent still runs (pid "
                                 f"{d.get('agent_pid')}); the run stays open", "gave up")
                print(f"{run_id}: the agent still runs after {WAIT_CAP_S // 3600} h; the waiter gives up")
                return 3
            time.sleep(poll)
        release_stale(cfg)  # its record (if any) is stale now: released and logged before the result
        _event(cfg, run, note, "finalizing")
        return finalize(cfg, run_id, note)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---- CLI ----

def register(sub) -> None:
    p = sub.add_parser(
        "reattach", help="finish runs whose ck-run watcher died (e.g. after a Claude restart); safe to rerun",
        description="For each unfinished run whose ck-run is gone: if its agent still runs, start a detached waiter "
                    "that finishes the run when the agent ends; else finish it now from what is on disk. A run "
                    "with a result is left as it is.")
    p.add_argument("run_id", nargs="?", help="one run (default: every unfinished run started in --since)")
    p.add_argument("--since", default=DEFAULT_SINCE,
                   help=f"without RUN: runs started this long ago or later, e.g. 6h, 2d (default {DEFAULT_SINCE})")
    p.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)  # the detached waiter (reattach starts it)
    p.set_defaults(func=cmd_reattach)


def cmd_reattach(cfg: Config, a) -> int:
    if a.wait:
        if not a.run_id:
            raise Refused("--wait needs a run id")
        return wait_and_finalize(cfg, a.run_id)
    if a.run_id:
        return reattach_run(cfg, a.run_id, release_stale(cfg))
    return reattach_all(cfg, parse_since(a.since), a.since)
