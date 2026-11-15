"""--live: the agent works in its own interactive CLI (TUI) in a new iTerm2 (or Terminal) window.

Flow (runner._live_phase):
  1. ck-run writes <run>/live.sh and asks iTerm2 (osascript) for a new window whose session RUNS live.sh as
     its command. Nothing is typed into a shell and iTerm2 is not activated, so keystrokes the user types
     elsewhere cannot land in front of the command (2026-11-03 bug: the old `write text` + `activate` turned
     the line into `is /bin/bash .../live.sh` -> "zsh: command not found: is", the window never started the
     agent and the run silently went headless). The window id goes to the ledger (live_window_opened).
  2. live.sh claims the run (<run>/live.claim, created with noclobber), records its pid, cd's to the
     agent's folder and starts the agent CLI with `env -i` (the same allowlisted env as a headless run)
     and the live prompt as the first message. When the agent exits it writes <run>/live.exit, then (in a
     real terminal only) leaves a login shell in the window.
  3. ck-run waits for <run>/summary.md (the agent writes it last), the agent's exit, or the timeout.
  4. No claim within start_timeout_s: ck-run takes the claim itself (so a late window does nothing), reads
     the last lines of the window, logs the reason (stderr + ledger event live_fallback) and falls back to
     the headless run. The same happens when osascript fails.

Window identity (iTerm2): the session name is "CK · <agent> · <title> · <time-hex>". Each agent gets a
dynamic profile "CK Live <agent>" (~/Library/Application Support/iTerm2/DynamicProfiles/ck-run-live.json)
with its own tab color and badge and "Allow Title Setting" off, so the agent's TUI cannot overwrite the
title. If iTerm2 has not loaded the profile yet, the default profile is used and the title is not locked
(live.sh still sets the badge and tab color with iTerm2 escape codes).

Interactive mode has no result JSON on stdout, so the prompt asks the agent to write result.md and
summary.md into the run folder. Session ids come from each CLI's own session store: the session whose first
"RUN: <run_id> | agent: <agent>" line is this run's (find_session); a session found any other way is marked
unsure, and the post-run review is told so.

v4 (2026-11-05, BUILD_SPEC_v4 10.5). Every new argument defaults to the old behaviour.
  - One window per ask, one tab per run: open_window(..., group=<ask id>, state_dir=<runs_dir>/.windows) with
    iTerm2 opens a new TAB in the window stored in <state_dir>/<group>.json when that window is still open and
    still holds a CK session; otherwise a new window, whose id is stored. A tab that cannot be made falls back to a
    new window. The result also has session_id (iTerm2) and tab (True when a tab was used).
  - live.sh records its terminal in <run>/live.tty, so ck-run can find the run's own session (tab) later, also
    when other runs' tabs share the window. Typing into a window (the resume key) needs that exact session.
  - wait_done(..., watch={...}) runs the stall watchdog (watchdog.py): activity = the window text changed
    (iTerm2), a file in the run folder changed, or the agent's session store grew. No activity for stall_s: find
    out why from the window text (sleep -> the resume key once; approval, rate_limit, unknown -> ledger `stall` +
    one Mac notification each). No activity for 2 x stall_s: stop the agent, state "stalled".
  - keep_awake(): /usr/bin/caffeinate -i -w <ck-run pid> while a live run waits (config live.keep_awake).
  - close_tab_later(): a small detached process closes the tab of a cleanly finished run after
    live.close_tab_after_s (0 = never), only if nobody used the tab in the second half of that time.
  All osascript calls use the configured path (tests: tests/fake_osascript.py).
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

from . import ledger
from .config import DEFAULT_CONFIG, REPO
from .flags import PROMPT
from .summarize import extract_structured

# Files ck-run owns in a run folder. The live Claude settings deny edits to them.
CONTROL_FILES = ("task.md", "prompt.md", "prompt-live.md", "packet.json", "cmd.txt", "claude-settings.json",
                 "claude-settings-live.json", "live.sh", ".marker", "live.claim", "live.pid", "live.exit",
                 "result.json", "rulecheck.json", "stdout.log", "stderr.log", "last.md", "live.tty",
                 "reattach.lock", "live-host.json", "herdr.log")

# argv after the script lines: <command> <title> <profile>. Neither script activates the app: a window that
# takes keyboard focus would receive whatever the user is typing at that moment. Both print "<window id>|<how>".
APPLESCRIPT = [
    "on run argv",
    'tell application "Terminal"',
    "set t to do script (item 1 of argv)",
    "try",
    "set custom title of t to (item 2 of argv)",
    "end try",
    'set wid to ""',
    "try",
    "set wid to (id of (first window whose selected tab is t)) as text",
    "end try",
    'return wid & "|" & "Terminal do script"',
    "end tell",
    "end run",
]

ITERM_APPLESCRIPT = [
    "on run argv",
    "set cmd to item 1 of argv",
    "set ttl to item 2 of argv",
    "set prof to item 3 of argv",
    'tell application "iTerm"',
    'set how to "profile " & prof',
    "try",
    'if prof is "" then error "no ck-run profile"',
    "set w to (create window with profile prof command cmd)",
    "on error errMsg",
    'set how to "default profile (" & errMsg & ")"',
    "set w to (create window with default profile command cmd)",
    "end try",
    "try",
    "tell current session of w to set name to ttl",
    "end try",
    'set sid to ""',
    "try",
    "set sid to (id of current session of w) as text",  # v4: the run's own session, for tail / write / close
    "end try",
    # not `tab`: inside a tell block that names the app's tab class
    'return ((id of w) as text) & "|" & how & "|" & sid',
    "end tell",
    "end run",
]

ITERM_TAIL_APPLESCRIPT = [
    "on run argv",
    'tell application "iTerm"',
    "return contents of current session of (first window whose id is ((item 1 of argv) as integer))",
    "end tell",
    "end run",
]

# v4: a new tab in an existing window. argv: <command> <title> <profile> <window id>. Prints "<window id>|<how>|<sid>";
# <how> starts with "profile " when the ck-run profile was used (the runner reads that as "title locked").
ITERM_TAB_APPLESCRIPT = [
    "-- ck-run:tab",
    "on run argv",
    "set cmd to item 1 of argv",
    "set ttl to item 2 of argv",
    "set prof to item 3 of argv",
    "set wid to (item 4 of argv) as integer",
    'tell application "iTerm"',
    "set w to (first window whose id is wid)",
    'set how to "profile " & prof',
    "try",
    'if prof is "" then error "no ck-run profile"',
    "tell w to set nt to (create tab with profile prof command cmd)",
    "on error errMsg",
    'set how to "default profile (" & errMsg & ")"',
    "tell w to set nt to (create tab with default profile command cmd)",
    "end try",
    "try",
    "set s to current session of nt",
    "on error",
    "set s to current session of w",  # the new tab is the selected one
    "end try",
    "try",
    "tell s to set name to ttl",
    "end try",
    'set sid to ""',
    "try",
    "set sid to (id of s) as text",
    "end try",
    'return ((id of w) as text) & "|" & how & "|" & sid',
    "end tell",
    "end run",
]

# v4: is window <id> still open, and does it still hold a CK session (a window id can be reused after an iTerm2
# restart)? argv: <window id> <session-name prefix>. Prints yes | other | no. Never starts iTerm2.
ITERM_EXISTS_APPLESCRIPT = [
    "-- ck-run:exists",
    "on run argv",
    'if application "iTerm" is not running then return "no"',
    "set wid to (item 1 of argv) as integer",
    "set pre to item 2 of argv",
    'tell application "iTerm"',
    "repeat with w in windows",
    "if (id of w) is wid then",
    "repeat with t in tabs of w",
    "repeat with s in sessions of t",
    "try",
    'if (name of s) starts with pre then return "yes"',
    "end try",
    "end repeat",
    "end repeat",
    'return "other"',
    "end if",
    "end repeat",
    "end tell",
    'return "no"',
    "end run",
]
SESSION_NAME_PREFIX = "CK · "  # window_title() starts every session name with it


def _session_script(marker: str, action: list) -> list:
    """AppleScript that finds ONE iTerm2 session by its id (item 1 of argv) or its tty (item 2) and runs `action`
    on it (as s). Prints CKNOTFOUND when no session matches, and never starts iTerm2."""
    return [
        f"-- ck-run:{marker}",
        "on run argv",
        'if application "iTerm" is not running then return "CKNOTFOUND"',
        "set sid to item 1 of argv",
        "set ttyName to item 2 of argv",
        'tell application "iTerm"',
        "repeat with w in windows",
        "repeat with t in tabs of w",
        "repeat with s in sessions of t",
        "set hit to false",
        "try",
        'if sid is not "" and ((id of s) as text) is sid then set hit to true',
        "end try",
        "try",
        'if (not hit) and ttyName is not "" and ((tty of s) as text) is ttyName then set hit to true',
        "end try",
        "if hit then",
        *action,
        "end if",
        "end repeat",
        "end repeat",
        "end repeat",
        "end tell",
        'return "CKNOTFOUND"',
        "end run",
    ]


ITERM_SESSION_TAIL_APPLESCRIPT = _session_script("tail", ['return "CKOK" & linefeed & (contents of s)'])
# The resume key (GROKBOT lesson 2026-11-04): the text without a newline, then Return as its own keystroke, so the
# agent's TUI submits it. argv item 3 = the text.
ITERM_WRITE_APPLESCRIPT = _session_script("write", [
    "tell s to write text (item 3 of argv) newline NO",
    "delay 0.3",
    "tell s to write text (ASCII character 13) newline NO",
    'return "CKOK"'])
ITERM_CLOSE_APPLESCRIPT = _session_script("close", ["tell s to close", 'return "CKOK"'])
NOTIFY_APPLESCRIPT = [
    "-- ck-run:notify",
    "on run argv",
    "display notification (item 1 of argv) with title (item 2 of argv)",
    'return "CKOK"',
    "end run",
]

# Tab colors (sRGB 0-255) and badges per agent, for telling the windows apart at a glance.
AGENT_COLORS = {"claude": (217, 119, 87), "codex": (16, 163, 127), "cursor": (59, 130, 246), "agy": (147, 51, 234)}
PROFILE_FILE = ("Library", "Application Support", "iTerm2", "DynamicProfiles", "ck-run-live.json")


def profile_name(agent: str) -> str:
    return f"CK Live {agent}"


def window_title(agent: str, title: str, run_id: str, max_title: int = 60) -> str:
    """'CK · <agent> · <title> · <HHMMSS-hex>' (the run id's time and random suffix)."""
    t = " ".join(str(title).split())
    if len(t) > max_title:
        t = t[:max_title - 1].rstrip() + "…"
    parts = run_id.split("-")
    short = f"{parts[2].split('T')[-1]}-{parts[-1]}" if len(parts) >= 5 and "T" in parts[2] else run_id
    return f"CK · {agent} · {t} · {short}"


def iterm_profiles() -> dict:
    out = []
    for agent, (r, g, b) in AGENT_COLORS.items():
        out.append({
            "Name": profile_name(agent),
            "Guid": f"ck-run-live-{agent}",
            "Dynamic Profile Parent Name": "Default",  # inherit the user's Default profile colours
            "Allow Title Setting": False,   # the agent's TUI may not change the title
            "Title Components": 1,          # title = session name only (set by ck-run)
            "Badge Text": f"CK {agent}",
            "Use Tab Color": True,
            "Tab Color": {"Red Component": r / 255, "Green Component": g / 255, "Blue Component": b / 255,
                          "Alpha Component": 1, "Color Space": "sRGB"},
        })
    try:  # colours copied from the user's Default profile (dark variant)
        cp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "iterm_colors.json")
        with open(cp, encoding="utf-8") as f:
            cols = json.load(f)
        for p in out:
            p.update(cols)
    except (OSError, ValueError):
        pass
    return {"Profiles": out}


def ensure_iterm_profiles(home: str) -> tuple:
    """(path, changed, error). Writes the ck-run dynamic profiles if missing or different. Never raises."""
    path = os.path.join(home, *PROFILE_FILE)
    text = json.dumps(iterm_profiles(), indent=1) + "\n"
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return path, False, None
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".ck-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        return path, True, None
    except OSError as e:
        return path, False, f"could not write {path}: {e}"


UUID_RX = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
READ_CAP = 32 * 1024 * 1024


def script_text(*, run_dir: str, cwd: str, env: dict, argv: list, prompt_path: str, title: str,
                agent: str = "", herdr_host: bool = False) -> str:
    """Bash launcher that the window runs as its command. The prompt is read from a file, never typed."""
    q = shlex.quote
    env_parts = [f"{k}={q(v)}" for k, v in sorted(env.items()) if k not in ("TERM", "NO_COLOR")]
    env_parts.append('TERM="${TERM:-xterm-256color}"')  # the window's terminal type; colours stay on
    if herdr_host:
        # hc assigns the child's own identity. Pass it through env -i so nested ck-run follows Herdr.
        env_parts.append('HC_TERMINAL_ID="${HC_TERMINAL_ID:-}"')
    if "LANG" not in env:
        env_parts.append("LANG=en_US.UTF-8")
    agent_cmd = " ".join(f'"$(cat {q(prompt_path)})"' if a is PROMPT else q(str(a)) for a in argv)
    # A real terminal keeps a login shell in the window afterwards (iTerm2 would otherwise close it).
    hold = 'if [ -t 0 ] && [ -t 1 ]; then exec "${SHELL:-/bin/zsh}" -l; fi'
    iterm = []
    if agent in AGENT_COLORS:  # badge + tab color by escape code, also when the ck-run profile is not loaded
        r, g, b = AGENT_COLORS[agent]
        badge = base64.b64encode(f"CK {agent}".encode()).decode()
        iterm = ['if [ "$TERM_PROGRAM" = "iTerm.app" ]; then',
                 f"  printf '\\033]1337;SetBadgeFormat=%s\\007' {q(badge)}",
                 f"  printf '\\033]6;1;bg;red;brightness;{r}\\007\\033]6;1;bg;green;brightness;{g}\\007"
                 f"\\033]6;1;bg;blue;brightness;{b}\\007'",
                 "fi"]
    return "\n".join([
        "#!/bin/bash",
        "# ck-run --live launcher: the iTerm2/Terminal window runs this; it starts the agent's interactive CLI.",
        f"RUN={q(run_dir)}",
        'if ! (set -C; : > "$RUN/live.claim") 2>/dev/null; then',
        '  echo "ck-run: this run was already started, or ck-run fell back to a headless run. Nothing to do."',
        f"  {hold}",
        "  exit 0",
        "fi",
        'echo $$ > "$RUN/live.pid.tmp" && mv "$RUN/live.pid.tmp" "$RUN/live.pid"',
        # v4: this window's terminal, so ck-run can find this run's own iTerm2 session (tab) later
        'CKTTY=$(tty 2>/dev/null) && echo "$CKTTY" > "$RUN/live.tty.tmp" && mv "$RUN/live.tty.tmp" "$RUN/live.tty"',
        f'cd {q(cwd)} || {{ echo 127 > "$RUN/live.exit"; exit 1; }}',
        f"printf '\\033]0;%s\\007' {q(title)}",
        *iterm,
        f"echo {q('ck-run live: ' + title)}",
        f"/usr/bin/env -i {' '.join(env_parts)} {agent_cmd}",
        'rc=$?; echo $rc > "$RUN/live.exit.tmp" && mv "$RUN/live.exit.tmp" "$RUN/live.exit"',
        'echo "ck-run: the agent exited (code $rc). This window can be closed."',
        hold,
        "",
    ])


def write_script(path: str, **kw) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(script_text(**kw))


def _is_iterm(terminal) -> bool:
    return str(terminal or "").lower() in ("iterm", "iterm2")


def _executable(path) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _parse_window_reply(text: str) -> tuple:
    """'<window id>|<how>[|<session id>]' -> (window_id, how, session_id)."""
    parts = text.strip().split("|")
    wid = parts[0].strip() or None
    if len(parts) >= 3:
        return wid, "|".join(parts[1:-1]).strip() or None, parts[-1].strip() or None
    return wid, (parts[1].strip() if len(parts) > 1 else "") or None, None


# A group's window is reused only while its state file is this fresh (a window id can come back after a reboot).
GROUP_MAX_AGE_S = 12 * 3600


def _group_file(state_dir: str, group: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(group))[:80] or "_"
    return os.path.join(state_dir, f"{safe}.json")


def _read_group(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_group(path: str, group: str, win: dict, old: dict) -> None:
    same = old.get("window_id") == str(win["window_id"])
    now = ledger.now_iso()
    rec = {"group": str(group), "terminal": "iTerm2", "window_id": str(win["window_id"]),
           "created_at": (old.get("created_at") if same else None) or now, "updated_at": now,
           "updated_ts": time.time(), "tabs": (int(old.get("tabs") or 0) if same else 0) + 1}
    try:
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass  # the next run of the group opens a new window: no harm


def open_window(osascript: str, command: str, title: str, env: dict, terminal: str = "Terminal",
                profile: str = "", group=None, state_dir=None) -> dict:
    """Ask iTerm2 (terminal="iTerm2") or Terminal for a new window that runs `command` as its program.
    If iTerm2 fails, Terminal is tried once before giving up.
    v4: with `group` (the ask id) and `state_dir` and iTerm2, open a new TAB in the group's window when that window
    is still open (state in <state_dir>/<group>.json); else a new window, whose id is stored for the next run of the
    group. group=None: the old behaviour (a new window).
    Returns {ok, error, window_id, terminal, how, session_id, tab}."""
    if not (group and state_dir and _is_iterm(terminal)):
        return _open_new(osascript, command, title, env, terminal, profile)
    try:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        path = _group_file(state_dir, group)
        lock = os.open(path[:-len(".json")] + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return _open_new(osascript, command, title, env, terminal, profile)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)  # two runs of one ask (a race) must not both open a new window
        old = _read_group(path)
        notes = []
        wid = old.get("window_id")
        fresh = time.time() - float(old.get("updated_ts") or 0) < GROUP_MAX_AGE_S
        if wid and old.get("terminal") == "iTerm2" and fresh and _executable(osascript):
            if window_exists(osascript, wid, env):
                ok, text, err = _osa(osascript, ITERM_TAB_APPLESCRIPT, [command, title, profile or "", str(wid)], env)
                if ok:
                    w, how, sid = _parse_window_reply(text)
                    out = {"ok": True, "error": None, "window_id": w or str(wid), "terminal": "iTerm2", "how": how,
                           "session_id": sid, "tab": True}
                    _write_group(path, group, out, old)
                    return out
                notes.append(f"iTerm2 tab in window {wid}: {err}")
            else:
                notes.append(f"the window of {group} (id {wid}) is closed or holds no CK tab any more")
        out = _open_new(osascript, command, title, env, terminal, profile)
        if out["ok"] and out["terminal"] == "iTerm2" and out["window_id"]:
            _write_group(path, group, out, old)
        if notes:
            out["error"] = "; ".join(notes + ([out["error"]] if out["error"] else []))
        return out
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            os.close(lock)


def _open_new(osascript: str, command: str, title: str, env: dict, terminal: str, profile: str) -> dict:
    """The old open_window: a new iTerm2 window, else a new Terminal window."""
    out = {"ok": False, "error": None, "window_id": None, "terminal": None, "how": None, "session_id": None,
           "tab": False}
    if not (os.path.isfile(osascript) and os.access(osascript, os.X_OK)):
        out["error"] = f"osascript not found: {osascript}"
        return out
    tries = [("iTerm2", ITERM_APPLESCRIPT, profile or "")] if _is_iterm(terminal) else []
    tries.append(("Terminal", APPLESCRIPT, ""))
    errors = []
    for name, script, prof in tries:
        ok, text, err = _osa(osascript, script, [command, title, prof], env)
        if ok:
            wid, how, sid = _parse_window_reply(text)
            out.update(ok=True, terminal=name, window_id=wid, how=how, session_id=sid if name == "iTerm2" else None,
                       error=("; ".join(errors) or None))
            return out
        errors.append(f"{name}: {err}")
    out["error"] = "; ".join(errors)
    return out


def window_exists(osascript: str, window_id, env: dict) -> bool:
    """True if iTerm2 window <window_id> is open and still holds a CK session (never starts iTerm2)."""
    if not window_id or not str(window_id).isdigit() or not _executable(osascript):
        return False
    ok, text, _ = _osa(osascript, ITERM_EXISTS_APPLESCRIPT, [str(window_id), SESSION_NAME_PREFIX], env, timeout=20)
    return ok and text.strip() == "yes"


def read_tty(run_dir: str | None) -> str | None:
    """The terminal live.sh recorded (/dev/ttysNNN), or None (not started yet, or not a terminal)."""
    if not run_dir:
        return None
    try:
        with open(os.path.join(run_dir, "live.tty"), encoding="utf-8") as f:
            t = f.read().strip()
    except OSError:
        return None
    return t if t.startswith("/dev/") else None


def session_text(osascript: str, env: dict, *, session_id=None, tty=None, window_id=None,
                 timeout: int = 20, herdr_terminal=None):
    """What an iTerm2 session shows now (its visible text), or None. With session_id or tty: exactly that session
    (tab). Else the current session of window_id (the old way; in a shared window that can be another run's tab)."""
    if herdr_terminal:
        from . import herdr
        return herdr.read(herdr_terminal, timeout=timeout)
    if not _executable(osascript):
        return None
    if session_id or tty:
        ok, text, _ = _osa(osascript, ITERM_SESSION_TAIL_APPLESCRIPT, [str(session_id or ""), str(tty or "")], env,
                           timeout=timeout)
        if not ok or not text.startswith("CKOK"):
            return None
        return text[len("CKOK"):].lstrip("\r\n")
    if not window_id or not str(window_id).isdigit():
        return None
    ok, text, _ = _osa(osascript, ITERM_TAIL_APPLESCRIPT, [str(window_id)], env, timeout=timeout)
    return text if ok else None


def window_tail(osascript: str, window_id, env: dict, lines: int = 6, *, session_id=None, tty=None,
                herdr_terminal=None) -> str | None:
    """The last non-empty lines shown in an iTerm2 window (to explain a window that did not start).
    v4: session_id / tty pick the run's own tab in a shared window."""
    if not (session_id or tty or herdr_terminal) and (not window_id or not str(window_id).isdigit()):
        return None
    kw = {"herdr_terminal": herdr_terminal} if herdr_terminal else {}
    text = session_text(osascript, env, session_id=session_id, tty=tty, window_id=window_id, **kw)
    if text is None:
        return None
    rows = [r.rstrip() for r in text.splitlines() if r.strip()]
    return " | ".join(rows[-lines:])[-600:] or None


def send_text(osascript: str, env: dict, text: str, *, session_id=None, tty=None, herdr_terminal=None) -> tuple:
    """Type `text` + Return into ONE iTerm2 session (the run's own tab). (ok, error). Never guesses a session:
    without its id or tty nothing is typed (another run's agent could get the keystrokes)."""
    if herdr_terminal:
        from . import herdr
        return herdr.send(herdr_terminal, text)
    if not (session_id or tty):
        return False, "no exact iTerm2 session (id or tty) to type into"
    if not _executable(osascript):
        return False, f"osascript not found: {osascript}"
    ok, out, err = _osa(osascript, ITERM_WRITE_APPLESCRIPT, [str(session_id or ""), str(tty or ""), text], env,
                        timeout=20)
    if ok and out.strip() == "CKOK":
        return True, None
    return False, err or "the run's iTerm2 session was not found"


def close_session(osascript: str, env: dict, *, session_id=None, tty=None) -> bool:
    """Close ONE iTerm2 session (the run's tab). False when it is not found (already closed)."""
    if not (session_id or tty) or not _executable(osascript):
        return False
    ok, out, _ = _osa(osascript, ITERM_CLOSE_APPLESCRIPT, [str(session_id or ""), str(tty or "")], env, timeout=20)
    return ok and out.strip() == "CKOK"


def notify(osascript: str, env: dict, message: str, title: str = "ck-run") -> bool:
    """A Mac notification (osascript display notification). Text goes in as arguments, never into the script."""
    if not _executable(osascript):
        return False
    ok, _, _ = _osa(osascript, NOTIFY_APPLESCRIPT, [" ".join(str(message).split())[:240],
                                                     " ".join(str(title).split())[:80]], env, timeout=20)
    return ok


# ---- v4 settings, keep-awake and the tab closer ----

def live_setting(key: str, default, cfg=None):
    """A key of the config's `live` block (keep_awake, close_tab_after_s, caffeinate, ...): the Config attribute
    live_<key> when there is one, else the raw file (the runner's config: CK_DISPATCH_CONFIG or the default)."""
    if cfg is not None:
        if hasattr(cfg, f"live_{key}"):
            return getattr(cfg, f"live_{key}")
        raw = getattr(cfg, "raw", None)
        if isinstance(raw, dict):
            return (raw.get("live") or {}).get(key, default)
    path = getattr(cfg, "path", None) or os.environ.get("CK_DISPATCH_CONFIG") or DEFAULT_CONFIG
    try:
        with open(path, encoding="utf-8") as f:
            lv = json.load(f).get("live") or {}
    except (OSError, ValueError, AttributeError):
        return default
    return lv.get(key, default) if isinstance(lv, dict) else default


CAFFEINATE = "/usr/bin/caffeinate"
_AWAKE = {}  # pid -> Popen: one caffeinate per ck-run process


def keep_awake(cfg=None, *, pid: int | None = None, enabled=None, caffeinate: str | None = None):
    """Keep the Mac from idle sleep while a live run waits: `caffeinate -i -w <pid>` (pid = this ck-run), detached,
    never waited on, so it can never block ck-run's exit; it ends by itself when ck-run ends. Returns the Popen, or
    None when off (live.keep_awake false), when the binary is missing, or when it cannot start. Idempotent per pid."""
    if enabled is None:
        enabled = bool(live_setting("keep_awake", True, cfg))
    if not enabled:
        return None
    binary = caffeinate or live_setting("caffeinate", CAFFEINATE, cfg) or CAFFEINATE
    if not _executable(binary):
        return None
    pid = int(pid or os.getpid())
    p = _AWAKE.get(pid)
    if p is not None and p.poll() is None:
        return p
    try:
        p = subprocess.Popen([binary, "-i", "-w", str(pid)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        return None
    _AWAKE[pid] = p
    return p


def stop_keep_awake(p) -> None:
    if p is None:
        return
    for k, v in list(_AWAKE.items()):
        if v is p:
            _AWAKE.pop(k, None)
    try:
        if p.poll() is None:
            p.terminate()
            p.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


# Spinners and clocks are not activity: a hung request shows only a ticking timer. Token counts and new lines are.
_TIMER_RX = re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)\b"
                       r"|\b\d{1,2}:\d{2}(?::\d{2})?\b", re.I)
_SPINNER_RX = re.compile("[⠀-⣿✠-❏◐-◓◴-◷⏺·•…|/\\\\-]")


def text_signature(text: str | None) -> str | None:
    """A hash of what a window shows, without timers and spinner frames (None for no text)."""
    if text is None:
        return None
    t = _SPINNER_RX.sub("", _TIMER_RX.sub("", text))
    return hashlib.sha1(" ".join(t.split()).encode("utf-8", "replace")).hexdigest()


_CLOSER = ("import sys; sys.path.insert(0, sys.argv[1]); from ckdispatch import live; "
           "sys.exit(live._closer_main(sys.argv[2:]))")


def close_tab_later(osascript: str, env: dict | None, *, session_id=None, tty=None, delay_s: float = 600,
                    run_dir: str | None = None):
    """Close the run's iTerm2 tab after delay_s in a small detached process (never blocks ck-run's exit). It closes
    only that exact session (id or tty), and only if its text did not change in the second half of delay_s (so a
    tab chalach is using stays). delay_s <= 0 = never. Returns the Popen or None."""
    try:
        delay_s = float(delay_s)
    except (TypeError, ValueError):
        return None
    if delay_s <= 0 or not (session_id or tty) or not _executable(osascript):
        return None
    argv = [sys.executable, "-I", "-c", _CLOSER, REPO, osascript, str(session_id or ""), str(tty or ""),
            f"{delay_s:g}", run_dir or ""]
    try:
        return subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        return None


def _closer_main(args: list) -> int:
    """The detached tab closer (close_tab_later). Ends early when the osascript or the run folder is gone."""
    osascript, sid, tty, delay_s, run_dir = args[0], args[1] or None, args[2] or None, float(args[3]), args[4] or None
    env = dict(os.environ)

    def wait(seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(max(0.01, min(2.0, end - time.monotonic())))
            if not os.path.exists(osascript) or (run_dir and not os.path.isdir(run_dir)):
                return False
        return True

    if not wait(delay_s / 2):
        return 0
    base = text_signature(session_text(osascript, env, session_id=sid, tty=tty))
    if base is None or not wait(delay_s / 2):
        return 0  # the tab is gone already, or the test sandbox was removed
    if text_signature(session_text(osascript, env, session_id=sid, tty=tty)) != base:
        return 0  # somebody used the tab: keep it
    close_session(osascript, env, session_id=sid, tty=tty)
    return 0


def _osa(osascript: str, script: list, args: list, env: dict, timeout: int = 60) -> tuple:
    """(ok, stdout, error)."""
    argv = [osascript]
    for line in script:
        argv += ["-e", line]
    argv += list(args)
    try:
        p = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "", f"osascript: {e}"
    if p.returncode != 0:
        return False, p.stdout or "", f"osascript exit {p.returncode}: {(p.stderr or p.stdout).strip()[:300]}"
    return True, p.stdout or "", None


def take_claim(run_dir: str) -> bool:
    """True if ck-run took the claim (the window has not started and now never will)."""
    try:
        os.close(os.open(os.path.join(run_dir, "live.claim"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        return True
    except FileExistsError:
        return False


def wait_started(run_dir: str, start_timeout_s: float, poll_s: float) -> bool:
    claim = os.path.join(run_dir, "live.claim")
    deadline = time.time() + start_timeout_s
    while time.time() < deadline:
        if os.path.exists(claim):
            return True
        time.sleep(poll_s)
    return not take_claim(run_dir)


def _read_int(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _alive(pid) -> bool:
    if not pid:
        return True  # unknown yet
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _stat(path: str):
    try:
        return os.stat(path)
    except OSError:
        return None


def wait_done(run_dir: str, deadline: float, poll_s: float, result_grace_s: float = 10.0, watch=None) -> dict:
    """Wait for summary.md (stable for one poll), the agent's exit, or the deadline.

    state: done (summary.md written) | exited (agent gone, no summary.md) | timeout
           | stalled (v4, only with `watch`: no activity for 2 x stall_s; the watchdog stopped the agent)

    watch (v4, None = the old behaviour): {"stall_s", "osascript", "window_id", "terminal", "env", "runs_dir",
    "run_id", "ask_id", "agent", "title"}; optional "session_id" (open_window's), "keep_awake", "caffeinate",
    "close_tab_after_s" (else from the config's live block), "home". See watchdog.py. With watch, time the Mac
    slept is added to the deadline, the Mac is kept awake while waiting, and on "done" the run's tab is closed
    after live.close_tab_after_s. The result then also has "watchdog" (what it saw and did).
    """
    if not watch:
        return _wait_loop(run_dir, deadline, poll_s, result_grace_s, None)
    from .watchdog import Watchdog  # here, not at the top: watchdog.py imports this module
    try:
        wd = Watchdog(run_dir, watch, poll_s)
        wd.start()
    except Exception as e:  # the watchdog must never break a run: wait the old way
        err = f"{type(e).__name__}: {e}"[:300]
        print(f"ck-run: the stall watchdog could not start ({err}); waiting without it", file=sys.stderr)
        sys.stderr.flush()
        out = _wait_loop(run_dir, deadline, poll_s, result_grace_s, None)
        out["watchdog"] = {"error": f"could not start: {err}"}
        return out
    try:
        out = _wait_loop(run_dir, deadline, poll_s, result_grace_s, wd)
    finally:
        wd.close()
    return wd.finished(out)


def _wait_loop(run_dir: str, deadline: float, poll_s: float, result_grace_s: float, wd) -> dict:
    summary, result = os.path.join(run_dir, "summary.md"), os.path.join(run_dir, "result.md")
    exit_path, pid_path = os.path.join(run_dir, "live.exit"), os.path.join(run_dir, "live.pid")
    last, seen_at, pid = None, None, None
    while True:
        st = _stat(summary)
        if st and st.st_size > 0:
            sig = (st.st_size, st.st_mtime_ns)
            seen_at = seen_at or time.time()
            if sig == last and (os.path.exists(result) or time.time() - seen_at >= result_grace_s):
                return {"state": "done", "exit_code": _read_int(exit_path)}
            last = sig
        code = _read_int(exit_path)
        pid = pid or _read_int(pid_path)
        gone = code is not None or (pid is not None and not _alive(pid))
        if gone and not (st and st.st_size > 0):
            time.sleep(poll_s)  # the agent may have written summary.md just before it exited
            st = _stat(summary)
            if st and st.st_size > 0:
                last = None
                continue
            return {"state": "exited", "exit_code": _read_int(exit_path)}
        if wd is not None:
            stalled = wd.tick()
            if stalled is not None:
                return stalled
        if time.time() >= deadline + (wd.slept_s if wd is not None else 0.0):
            return {"state": "timeout", "exit_code": None}
        time.sleep(poll_s)


def _children(pid: int) -> list:
    try:
        p = subprocess.run(["/usr/bin/pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=10)
        return [int(x) for x in p.stdout.split() if x.isdigit()]
    except (OSError, subprocess.TimeoutExpired):
        return []


def stop(run_dir: str, grace_s: float = 5.0) -> str:
    """Stop the agent in the window (timeout or ck-run interrupted). Never signals ck-run's own group."""
    pid = _read_int(os.path.join(run_dir, "live.pid"))
    if not pid or not _alive(pid):
        return "not running"
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None
    group = pgid == pid and pgid != os.getpgrp()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if group:  # live.sh leads its own process group (a Terminal job): the agent and its children
                os.killpg(pid, sig)
            else:
                for child in _children(pid):
                    try:
                        os.kill(child, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            return "stopped"
        t0 = time.time()
        while time.time() - t0 < grace_s:
            if not _alive(pid):
                return "stopped"
            time.sleep(0.1)
    return "stopped (SIGKILL)" if not _alive(pid) else "could not stop"


# ---- session stores (paths from docs/step3/results.md) ----

# A run's own session starts with its prompt, whose 2nd line is "RUN: <run_id> | agent: <agent> | kind: ..."
# (prompt.header). Other runs' prompts can NAME this run id too (their CONCURRENCY line lists other live runs'
# claims), so a bare mention says nothing about whose session it is: on 2026-11-04 two cursor runs started 1 s
# apart both got the newer one's session, and the older run's review read the wrong tool calls. The marker has
# no character that JSON escapes, so it reads the same in JSONL transcripts and in agy's SQLite blobs.
MARKER_RX = re.compile(rb"RUN: (\d{4}-\d{2}-\d{2}T\d{6}-[A-Za-z0-9_-]+) \| agent: ([a-z]+)")


def run_marker(run_id: str, agent: str) -> str:
    return f"RUN: {run_id} | agent: {agent}"


def _agy_steps(path: str):
    """agy's conversation steps in order (SQLite table `steps`), or None if the store cannot be read that way."""
    if not path.endswith(".db") or not os.path.isfile(path):
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT step_payload FROM steps ORDER BY idx").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return b"\n".join(r[0].encode("utf-8") if isinstance(r[0], str) else bytes(r[0]) for r in rows if r[0])


def _session_bytes(path: str) -> tuple:
    """(bytes, ordered). JSONL transcripts are in time order. agy's store is read step by step (time order) when
    SQLite can read it and its steps hold a RUN line; otherwise as raw file bytes (page order, not time order)."""
    steps = _agy_steps(path)
    if steps is not None and MARKER_RX.search(steps):
        return steps, True
    files = [path]
    if os.path.isdir(path):  # cursor: <id>/<id>.jsonl first, then any other file in the folder
        try:
            names = sorted(os.listdir(path))
        except OSError:
            return b"", True
        main = os.path.basename(path) + ".jsonl"
        files = [os.path.join(path, n) for n in ([main] if main in names else []) + [n for n in names if n != main]]
    chunks = []
    for p in files:
        for q in (p, p + "-wal"):
            try:
                with open(q, "rb") as f:
                    chunks.append(f.read(READ_CAP))
            except OSError:
                continue
    return b"\n".join(chunks), not path.endswith(".db")


def session_match(path: str, run_id: str, agent: str) -> str:
    """How a session relates to a run:
    'own'     its first RUN line is this run's (the session was started with this run's prompt);
    'quote'   it holds this run's RUN line, but after another run's (that agent read this run's prompt file);
    'mention' only the bare run id (another run's prompt or output names it); 'none'."""
    data, ordered = _session_bytes(path)
    me = (run_id.encode("utf-8"), agent.encode("utf-8"))
    marks = [m.groups() for m in MARKER_RX.finditer(data)]
    if me in marks:
        own = marks[0] == me if ordered else set(marks) == {me}  # raw SQLite pages: no order, so no other run
        return "own" if own else "quote"
    return "mention" if me[0] in data else "none"


def _scan(base: str, since: float, want_dir: bool, suffix: str = ""):
    try:
        it = list(os.scandir(base))
    except OSError:
        return []
    out = []
    for e in it:
        try:
            if e.is_dir() != want_dir or not e.name.endswith(suffix) or e.stat().st_mtime < since:
                continue
        except OSError:
            continue
        ids = UUID_RX.findall(e.name)
        if ids:
            out.append((e.stat().st_mtime, ids[-1], e.path))
    return out


def _candidates(agent: str, home: str, since: float) -> list:
    if agent == "codex":  # ~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl
        base = os.path.join(home, ".codex", "sessions")
        days = {(datetime.fromtimestamp(since) + timedelta(days=d)).strftime("%Y/%m/%d") for d in (0, 1)}
        days.add(datetime.now().strftime("%Y/%m/%d"))
        out = []
        for d in days:
            out += [c for c in _scan(os.path.join(base, d), since, False, ".jsonl")
                    if os.path.basename(c[2]).startswith("rollout-")]
        return out
    if agent == "cursor":  # ~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl
        out = []
        try:
            projects = list(os.scandir(os.path.join(home, ".cursor", "projects")))
        except OSError:
            return []
        for p in projects:
            t = os.path.join(p.path, "agent-transcripts")
            st = _stat(t)
            if st and st.st_mtime >= since:
                out += _scan(t, since, True)
        return out
    if agent == "agy":  # ~/.gemini/antigravity-cli/conversations/<id>.db
        return _scan(os.path.join(home, ".gemini", "antigravity-cli", "conversations"), since, False, ".db")
    return []


FOUND_OWN = "found in the session store (its first RUN line is this run's)"


def find_session(agent: str, home: str, since: float, run_id: str, known_id: str | None = None) -> dict:
    """{'id', 'path', 'how', 'sure'}. claude: the id ck-run set with --session-id. Others: the session the CLI
    itself reported (known_id, headless), else the newest session in the store that started after the run and
    whose first RUN line is this run's own (session_match 'own'). Only when no session has that, a fallback
    (sure=False, and 'how' says so): one that holds this run's RUN line after another run's, one that only
    mentions the run id, or the only new session."""
    if agent == "claude":
        path = None
        try:
            for d in os.scandir(os.path.join(home, ".claude", "projects")):
                p = os.path.join(d.path, f"{known_id}.jsonl")
                if os.path.isfile(p):
                    path = p
                    break
        except OSError:
            pass
        return {"id": known_id, "path": path, "sure": True,
                "how": "set by ck-run with --session-id" + ("" if path else " (transcript not found)")}
    cands = sorted(_candidates(agent, home, since), reverse=True)  # newest first
    if known_id:
        for _, sid, path in cands:
            if sid == known_id:
                return {"id": sid, "path": path, "sure": True, "how": "the session id the CLI printed"}
    fallback = {}
    for _, sid, path in cands:
        m = session_match(path, run_id, agent)
        if m == "own":
            return {"id": sid, "path": path, "sure": True, "how": FOUND_OWN}
        fallback.setdefault(m, (sid, path))
    for m, how in (("quote", "this run's RUN line seen only after another run's (a quote?); may be wrong"),
                   ("mention", "run id seen only as a mention, not as this run's RUN line; may be wrong")):
        if m in fallback:
            return {"id": fallback[m][0], "path": fallback[m][1], "sure": False, "how": how}
    if len(cands) == 1:
        return {"id": cands[0][1], "path": cands[0][2], "sure": False,
                "how": "only new session in the store (run id not seen in it; may be wrong)"}
    return {"id": None, "path": None, "sure": False, "how": f"not found ({len(cands)} new session(s) in the store)"}


def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


# Log lines that the agent did not write: the prompt, tool output (a file it read may quote another run's result
# with this run's id), CLI metadata. claude: type user (incl. tool_result); cursor: role user/tool; codex:
# payload role user/developer, *_output items, command output events, user_message events, turn_context/session_meta.
_NOT_AGENT = {"user", "tool", "system", "developer", "tool_result", "function_call_output", "custom_tool_call_output",
              "local_shell_call_output", "exec_command_end", "mcp_tool_call_end", "commandexecution",
              "command_execution", "user_message", "usermessage", "turn_context", "session_meta"}


def _by_agent(obj) -> bool:
    if not isinstance(obj, dict):
        return True
    keys = [obj.get("type"), obj.get("role")]
    for k in ("payload", "message", "item"):
        v = obj.get(k)
        if isinstance(v, dict):
            keys += [v.get("type"), v.get("role")]
            if isinstance(v.get("item"), dict):
                keys += [v["item"].get("type"), v["item"].get("role")]
    return not any(isinstance(k, str) and k.lower() in _NOT_AGENT for k in keys)


def recover_result(path: str | None, run_id: str):
    """(result_json, text) from a JSONL session log (claude, codex, cursor), or (None, ''): the last JSON
    object in the log whose run_id is this run and that has a summary (the prompt's schema has neither),
    from a line the agent wrote (not the prompt or tool output). Call it only with a session find_session
    is sure of: another run's session can quote this run's result."""
    if not path:
        return None, ""
    if os.path.isdir(path):
        path = os.path.join(path, os.path.basename(path) + ".jsonl")
    if not path.endswith(".jsonl"):
        return None, ""
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(max(0, size - READ_CAP))
            lines = f.read().splitlines()
    except OSError:
        return None, ""
    best, text = None, ""
    for line in lines:
        if run_id not in line or "summary" not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not _by_agent(obj):
            continue
        for s in _strings(obj):
            if run_id in s and "summary" in s:
                r = extract_structured(s)
                if isinstance(r, dict) and r.get("run_id") == run_id and "summary" in r:
                    best, text = r, s
    return best, text
