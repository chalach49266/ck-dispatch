"""Stall watchdog for live runs (ck-run v4, BUILD_SPEC_v4 10.5). live.wait_done(..., watch={...}) runs it.

Why: before v4, stuck runs waited for the full timeout (5 runs, 190 min), and a Mac that went to sleep left agents
waiting at "Your computer went to sleep mid-response" until someone typed into the window.

Activity (any one resets the clock; the watchdog never stops a run that shows any of them):
  - a file in the run folder changed (checked every poll),
  - the agent's session store grew: its own session once found, else every session of that agent that started
    after the run did (another run's growth then also counts: that can only delay a stall, never cause one),
  - the window text changed (iTerm2: the run's own tab, found by the session id or the tty live.sh recorded).
    Ticking timers and spinner frames do not count (a hung request shows only those); new lines and token
    counts do.
  The session store and the window are read at most every min(30 s, stall_s / 6), and only while files are quiet.
  Time the Mac slept does not count (monotonic clock), and it is added to the run's deadline.
No activity for stall_s: the window text says why (the most recent matching line wins):
  rate_limit  route.RATE_LIMIT_RX: route.mark_limited (the next runs avoid the agent), ledger `stall`, notification
  sleep       "went to sleep": type the resume key ("continue" + Return) into the run's own tab, ONCE per run,
              ledger `resume_sent`. If it cannot be typed (no exact tab, Terminal), or the agent is still quiet
              stall_s / 2 later: ledger `stall` + notification
  approval    "Do you want", "Allow", "[y/n]", "approve", ...: ledger `stall`, notification
  unknown     anything else, or no window text (Terminal): ledger `stall`, notification
  Each reason gets at most one `stall` event and one notification per run.
No activity for 2 x stall_s: live.stop (the agent and its children), ledger `watchdog_stop`, state "stalled"
(the runner records status timeout, exit 3). Every osascript call uses the configured path.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import time

from . import herdr, ledger, live, route

STALL_DEFAULT_S = 300.0
SAMPLE_MAX_S = 30.0    # read the session store / the window at most this often
SLEEP_GAP_S = 5.0      # wall clock ahead of the monotonic clock by more than this: the Mac slept
TAIL_LINES = 12        # classify only the last lines the window shows
DIR_CAP = 500          # entries per folder in a run-folder scan
FIND_TRIES = 5         # attempts to find the run's own session before watching all new sessions of the agent
RESUME_TEXT = "continue"
NOTE_MAX = 300         # spec: stall note = window tail excerpt, at most 300 chars

SLEEP_RX = re.compile(r"went to sleep|computer (?:was |is )?(?:asleep|sleeping)|(?:woke|wake) (?:up )?from sleep"
                      r"|sleep mid-response", re.I)
APPROVAL_RX = re.compile(
    r"do you want|would you like|\ballow\b|\bapprove\b|\bapproval\b|\[y/n\]|\(y/n\)|\by/n\b|\(y\)es"
    r"|proceed\?|don'?t ask again|run this command\?|press enter to (?:confirm|continue)"
    r"|waiting for (?:your )?(?:approval|confirmation|permission)|do you trust|trust this folder", re.I)
STATUS = {"stall": "stalled", "resume_sent": "sent", "watchdog_stop": "stopped"}


def classify(text: str | None) -> tuple:
    """(reason, the line that decided it) for a stalled window. The most recent matching line wins; within a line
    rate_limit > sleep > approval. No text or no match: ('unknown', '')."""
    if not text:
        return "unknown", ""
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()][-TAIL_LINES:]
    for ln in reversed(lines):
        if route.RATE_LIMIT_RX.search(ln):
            return "rate_limit", ln
        if SLEEP_RX.search(ln):
            return "sleep", ln
        if APPROVAL_RX.search(ln):
            return "approval", ln
    return "unknown", ""


def excerpt(text: str | None, limit: int = NOTE_MAX) -> str:
    """The last lines a window shows, joined with ' | ', at most `limit` chars (the end is kept)."""
    rows = [r.strip() for r in str(text or "").splitlines() if r.strip()]
    return " | ".join(rows[-6:])[-limit:]


def duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 60} min" if s >= 120 else f"{s} s"


def _size_mtime(path: str) -> tuple:
    """(bytes, newest mtime_ns) of a session file (plus its SQLite -wal), or of the files in a session folder."""
    paths = [path, path + "-wal"]
    if os.path.isdir(path):
        try:
            paths = [os.path.join(path, n) for n in sorted(os.listdir(path))[:DIR_CAP]]
        except OSError:
            paths = []
    total, newest = 0, 0
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            total += st.st_size
            newest = max(newest, st.st_mtime_ns)
    return total, newest


class Activity:
    """Is a live run doing anything? Run folder files, the agent's session store, the window text (iTerm2).
    Also used by `ck-run reattach`'s waiter to see that a finished agent sits idle."""

    def __init__(self, run_dir: str, *, agent: str, run_id: str, home: str, osascript: str, env, iterm: bool,
                 window_id=None, session_id=None, herdr_terminal=None, every: float = SAMPLE_MAX_S):
        self.run_dir, self.agent, self.run_id, self.home = run_dir, agent or "", run_id, home
        self.osascript, self.env, self.iterm = osascript, env, iterm
        self.window_id, self.session_id, self.every = window_id, session_id, max(0.05, float(every))
        self.herdr_terminal = herdr_terminal or herdr.saved(run_dir).get("terminal")
        self.since = self._since()
        self.dir_sig = self._dir_sig()
        self.store_sig = self.store_at = None
        self.tail_sig = self.tail_text = self.tail_at = None
        self.own_path, self.find_tries, self._claude_sid = None, 0, None

    def _since(self) -> float:
        for n in ("live.claim", "live.pid", "live.sh"):
            try:
                return os.stat(os.path.join(self.run_dir, n)).st_mtime - 5
            except OSError:
                continue
        return time.time() - 5

    def _dir_sig(self) -> tuple:
        out = []
        try:
            entries = sorted(os.scandir(self.run_dir), key=lambda e: e.name)[:DIR_CAP]
        except OSError:
            return ()
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    for e2 in sorted(os.scandir(e.path), key=lambda x: x.name)[:DIR_CAP]:
                        st = e2.stat(follow_symlinks=False)
                        out.append((f"{e.name}/{e2.name}", st.st_size, st.st_mtime_ns))
                else:
                    st = e.stat(follow_symlinks=False)
                    out.append((e.name, st.st_size, st.st_mtime_ns))
            except OSError:
                continue
        return tuple(out)

    def files_changed(self) -> bool:
        sig = self._dir_sig()
        if sig != self.dir_sig:
            self.dir_sig = sig
            return True
        return False

    def due(self, at, now: float) -> bool:
        return at is None or now - at >= self.every

    def target(self) -> tuple:
        """(session id, tty) of the run's own iTerm2 session; either may be None."""
        return self.session_id, live.read_tty(self.run_dir)

    def _claude_session_id(self):
        if self._claude_sid is None:
            try:
                with open(os.path.join(self.run_dir, "live.sh"), encoding="utf-8") as f:
                    m = re.search(r"--session-id\s+'?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                                  f.read())
            except OSError:
                m = None
            self._claude_sid = m.group(1) if m else ""
        return self._claude_sid or None

    def _store_paths(self) -> list:
        if self.own_path:
            return [self.own_path]
        if self.agent == "claude":  # live claude: ck-run set the session id (--session-id in live.sh)
            sid = self._claude_session_id()
            if sid:
                s = live.find_session("claude", self.home, self.since, self.run_id, known_id=sid)
                if s.get("path"):
                    self.own_path = s["path"]
                    return [self.own_path]
            return []
        if self.find_tries < FIND_TRIES:
            self.find_tries += 1
            s = live.find_session(self.agent, self.home, self.since, self.run_id)
            if s.get("sure") and s.get("path"):
                self.own_path = s["path"]
                return [self.own_path]
        return [c[2] for c in live._candidates(self.agent, self.home, self.since)]

    def sample_store(self, now: float) -> bool:
        """True if the session store grew since the last sample (the first sample is the baseline)."""
        self.store_at = now
        try:
            sig = tuple(sorted((p, _size_mtime(p)) for p in self._store_paths()))
        except OSError:
            return False
        changed = self.store_sig is not None and sig != self.store_sig
        self.store_sig = sig
        return changed

    def sample_tail(self, now: float) -> bool:
        """True if the window text changed since the last sample (timers and spinners ignored)."""
        self.tail_at = now
        sid, tty = self.target()
        kw = {"herdr_terminal": self.herdr_terminal} if self.herdr_terminal else {}
        text = live.session_text(self.osascript, self.env, session_id=sid, tty=tty, window_id=self.window_id, **kw)
        if text is None:
            return False
        sig = live.text_signature(text)
        changed = self.tail_sig is not None and sig != self.tail_sig
        self.tail_sig, self.tail_text = sig, text
        return changed

    def rebaseline_tail(self, now: float) -> None:
        """Take the window text as it is now, without counting the change as activity (our own keystrokes)."""
        self.tail_sig = None
        self.sample_tail(now)

    def poll(self, now: float, idle: float) -> bool:
        """One check: files every time; the store and the window when quiet for `every` and due."""
        if self.files_changed():
            return True
        if idle >= self.every:
            if self.due(self.store_at, now) and self.sample_store(now):
                return True
            if (self.iterm or self.herdr_terminal) and self.due(self.tail_at, now) and self.sample_tail(now):
                return True
        return False


class Watchdog:
    """One live run's watchdog. live.wait_done calls start(), tick() every poll, close(), then finished()."""

    def __init__(self, run_dir: str, watch: dict, poll_s: float):
        w = dict(watch or {})
        self.run_dir = run_dir
        packet = self._packet()
        self.stall_s = float(w["stall_s"]) if w.get("stall_s") is not None else STALL_DEFAULT_S
        self.osascript = w.get("osascript") or "/usr/bin/osascript"
        self.terminal = w.get("terminal") or ""
        self.env = w.get("env")
        self.runs_dir = w.get("runs_dir") or os.path.dirname(os.path.abspath(run_dir))
        self.run_id = w.get("run_id") or os.path.basename(os.path.abspath(run_dir))
        self.ask_id = w.get("ask_id") or None
        self.agent = w.get("agent") or packet.get("worker") or ""
        self.title = w.get("title") or packet.get("title") or ""
        self.workspace = packet.get("workspace")

        def opt(key, default):
            v = w.get(key)
            return live.live_setting(key, default) if v is None else v

        self.keep = bool(opt("keep_awake", True))
        self.caffeinate = w.get("caffeinate") or live.live_setting("caffeinate", live.CAFFEINATE)
        try:
            self.close_after = float(opt("close_tab_after_s", 600) or 0)
        except (TypeError, ValueError):
            self.close_after = 0.0
        every = min(SAMPLE_MAX_S, max(float(poll_s), self.stall_s / 6)) if self.stall_s > 0 else SAMPLE_MAX_S
        self.act = Activity(run_dir, agent=self.agent, run_id=self.run_id, home=w.get("home") or os.path.expanduser("~"),
                            osascript=self.osascript, env=self.env, iterm=live._is_iterm(self.terminal),
                            window_id=w.get("window_id"), session_id=w.get("session_id"),
                            herdr_terminal=w.get("herdr_terminal"), every=every)
        now = time.monotonic()
        self.last_activity = now
        self._wall, self._mono = time.time(), now
        self.slept_s = 0.0
        self.logged, self.notified = set(), set()
        self.resume_sent, self.resume_at, self.resume_error = False, None, None
        self.limited = False
        self.last_reason = None
        self.awake, self._own_awake = None, False
        self.error = None

    def _packet(self) -> dict:
        try:
            with open(os.path.join(self.run_dir, "packet.json"), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    # ---- lifecycle ----

    def start(self) -> None:
        had = live._AWAKE.get(os.getpid())
        self.awake = live.keep_awake(enabled=self.keep, caffeinate=self.caffeinate)
        self._own_awake = self.awake is not None and self.awake is not had

    def close(self) -> None:
        if self._own_awake:
            live.stop_keep_awake(self.awake)

    def finished(self, out: dict) -> dict:
        info = {"stall_s": self.stall_s, "reasons": sorted(self.logged), "resume_sent": self.resume_sent,
                "slept_s": round(self.slept_s, 1), "keep_awake": self.awake is not None, "tab_closer": False,
                "error": self.error}
        try:
            if out.get("state") == "done" and self.act.iterm and not self.act.herdr_terminal and self.close_after > 0:
                sid, tty = self.act.target()
                p = live.close_tab_later(self.osascript, self.env, session_id=sid, tty=tty,
                                         delay_s=self.close_after, run_dir=self.run_dir)
                info["tab_closer"] = p is not None
        except Exception as e:  # closing a tab later is a nicety; it never changes the run's result
            info["error"] = info["error"] or f"tab closer: {type(e).__name__}: {e}"
        out = dict(out)
        out["watchdog"] = info
        return out

    # ---- one poll ----

    def tick(self):
        """None while the run goes on; the final wait result when the watchdog stopped the run. A watchdog that
        fails (a bug, a disk error) switches itself off for this run and says so: it never breaks the run."""
        if self.error:
            return None
        try:
            return self._tick()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"[:300]
            print(f"ck-run: the stall watchdog stopped working for run {self.run_id} ({self.error}); "
                  f"the run goes on without it", file=sys.stderr)
            sys.stderr.flush()
            return None

    def _tick(self):
        now, wall = time.monotonic(), time.time()
        gap = (wall - self._wall) - (now - self._mono)
        if gap > SLEEP_GAP_S:  # the Mac slept: that time is not idle time, and the run's deadline moves on
            self.slept_s += gap
        self._wall, self._mono = wall, now
        if self.stall_s <= 0:
            return None
        if self.act.poll(now, now - self.last_activity):
            self.last_activity = now
            return None
        idle = now - self.last_activity
        if idle >= 2 * self.stall_s:
            return self._stop(idle)
        if idle >= self.stall_s:
            self._stalled(now, idle)
        return None

    def _stalled(self, now: float, idle: float) -> None:
        reason, line = classify(self.act.tail_text if (self.act.iterm or self.act.herdr_terminal) else None)
        self.last_reason = reason
        if reason == "sleep":
            if not self.resume_sent:
                self.resume_sent = True  # at most once per run, whatever happens next
                sid, tty = self.act.target()
                kw = {"herdr_terminal": self.act.herdr_terminal} if self.act.herdr_terminal else {}
                ok, err = (live.send_text(self.osascript, self.env, RESUME_TEXT, session_id=sid, tty=tty, **kw)
                           if (self.act.iterm or self.act.herdr_terminal)
                           else (False, f"a {self.terminal or 'Terminal'} window (not iTerm2)"))
                if ok:
                    self.resume_at = now
                    self._event("resume_sent", "sleep",
                                f"the window said: {line[:150]}; typed '{RESUME_TEXT}' + Return into the run's own "
                                f"tab ({self.act.herdr_terminal or ('session ' + sid if sid else tty)})")
                    time.sleep(0.3)
                    self.act.rebaseline_tail(time.monotonic())  # our keystrokes are not the agent's activity
                    return
                self.resume_error = err
                self._stall("sleep", idle, f"could not type the resume key: {err}")
                return
            if self.resume_at is not None and now - self.resume_at < self.stall_s / 2:
                return  # give the resume key time to work
            self._stall("sleep", idle, "the resume key did not wake it" if self.resume_at is not None else None)
            return
        if reason == "rate_limit" and not self.limited and self.agent:
            self.limited = True
            try:
                route.mark_limited(self.runs_dir, self.agent, line or self.act.tail_text or "")
            except OSError:
                pass
        self._stall(reason, idle)

    def _stall(self, reason: str, idle: float, extra: str | None = None) -> None:
        if reason in self.logged:
            return
        self.logged.add(reason)
        window = excerpt(self.act.tail_text) if self.act.tail_text else ""
        note = window or f"no window text; no activity for {int(idle)} s"
        if extra:
            note = f"{extra}; {note}"
        self._event("stall", reason, note[:NOTE_MAX])
        if reason not in self.notified:
            self.notified.add(reason)
            title = f"ck-run · {self.ask_id}" if self.ask_id else "ck-run"
            live.notify(self.osascript, self.env, self._message(reason), title)

    def _message(self, reason: str) -> str:
        who = self.agent or "the agent"
        what = f" ({self.title})" if self.title else ""
        stop = f"If nothing moves, ck-run stops it at {duration(2 * self.stall_s)} with no activity."
        if reason == "approval":
            return f"{who} waits for your OK in its window{what}. {stop}"
        if reason == "rate_limit":
            return f"{who} hit a rate limit{what}. Next runs avoid it. {stop}"
        if reason == "sleep":
            return f"{who} stopped when the Mac slept and did not wake up{what}. {stop}"
        return f"{who}: no activity for {duration(self.stall_s)}{what}. {stop}"

    def _stop(self, idle: float) -> dict:
        reason = self.last_reason or classify(self.act.tail_text if (self.act.iterm or self.act.herdr_terminal) else None)[0]
        res = live.stop(self.run_dir)
        self._event("watchdog_stop", reason,
                    f"no activity for {int(idle)} s (stall_s {self.stall_s:g}, last reason {reason}); "
                    f"stopped the agent: {res}")
        return {"state": "stalled", "exit_code": None, "reason": reason, "idle_s": int(idle),
                "stall_s": self.stall_s, "stop": res}

    def _event(self, event: str, reason: str | None, note: str) -> None:
        try:
            ledger.append(self.runs_dir, event=event, run_id=self.run_id, ask_id=self.ask_id, agent=self.agent or None,
                          title=self.title or None, workspace=self.workspace, reason=reason,
                          status=STATUS.get(event), note=note[:1000], **{"from": "CK", "to": self.agent or None})
        except OSError:
            pass
