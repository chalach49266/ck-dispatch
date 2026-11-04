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
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta

from .flags import PROMPT
from .summarize import extract_structured

# Files ck-run owns in a run folder. The live Claude settings deny edits to them.
CONTROL_FILES = ("task.md", "prompt.md", "prompt-live.md", "packet.json", "cmd.txt", "claude-settings.json",
                 "claude-settings-live.json", "live.sh", ".marker", "live.claim", "live.pid", "live.exit",
                 "result.json", "rulecheck.json", "stdout.log", "stderr.log", "last.md")

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
    'return ((id of w) as text) & "|" & how',  # not `tab`: inside a tell block that names the app's tab class
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
            "Allow Title Setting": False,   # the agent's TUI may not change the title
            "Title Components": 1,          # title = session name only (set by ck-run)
            "Badge Text": f"CK {agent}",
            "Use Tab Color": True,
            "Tab Color": {"Red Component": r / 255, "Green Component": g / 255, "Blue Component": b / 255,
                          "Alpha Component": 1, "Color Space": "sRGB"},
        })
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
                agent: str = "") -> str:
    """Bash launcher that the window runs as its command. The prompt is read from a file, never typed."""
    q = shlex.quote
    env_parts = [f"{k}={q(v)}" for k, v in sorted(env.items()) if k not in ("TERM", "NO_COLOR")]
    env_parts.append('TERM="${TERM:-xterm-256color}"')  # the window's terminal type; colours stay on
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


def open_window(osascript: str, command: str, title: str, env: dict, terminal: str = "Terminal",
                profile: str = "") -> dict:
    """Ask iTerm2 (terminal="iTerm2") or Terminal for a new window that runs `command` as its program.
    If iTerm2 fails, Terminal is tried once before giving up.
    Returns {ok, error, window_id, terminal, how}."""
    out = {"ok": False, "error": None, "window_id": None, "terminal": None, "how": None}
    if not (os.path.isfile(osascript) and os.access(osascript, os.X_OK)):
        out["error"] = f"osascript not found: {osascript}"
        return out
    tries = [("iTerm2", ITERM_APPLESCRIPT, profile or "")] if terminal.lower() in ("iterm", "iterm2") else []
    tries.append(("Terminal", APPLESCRIPT, ""))
    errors = []
    for name, script, prof in tries:
        ok, text, err = _osa(osascript, script, [command, title, prof], env)
        if ok:
            wid, _, how = text.strip().partition("|")
            out.update(ok=True, terminal=name, window_id=wid.strip() or None, how=how.strip() or None,
                       error=("; ".join(errors) or None))
            return out
        errors.append(f"{name}: {err}")
    out["error"] = "; ".join(errors)
    return out


def window_tail(osascript: str, window_id, env: dict, lines: int = 6) -> str | None:
    """The last non-empty lines shown in an iTerm2 window (to explain a window that did not start)."""
    if not window_id or not str(window_id).isdigit():
        return None
    ok, text, _ = _osa(osascript, ITERM_TAIL_APPLESCRIPT, [str(window_id)], env, timeout=20)
    if not ok:
        return None
    rows = [r.rstrip() for r in text.splitlines() if r.strip()]
    return " | ".join(rows[-lines:])[-600:] or None


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


def wait_done(run_dir: str, deadline: float, poll_s: float, result_grace_s: float = 10.0) -> dict:
    """Wait for summary.md (stable for one poll), the agent's exit, or the deadline.

    state: done (summary.md written) | exited (agent gone, no summary.md) | timeout
    """
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
        if time.time() >= deadline:
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
