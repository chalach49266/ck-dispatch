"""--live: the agent works in its own interactive CLI (TUI) in a new macOS Terminal window.

Flow (runner._live_phase):
  1. ck-run writes <run>/live.sh and asks Terminal (osascript) to open a window that runs it.
  2. live.sh claims the run (<run>/live.claim, created with noclobber), records its pid, cd's to the
     agent's folder and starts the agent CLI with `env -i` (the same allowlisted env as a headless run)
     and the live prompt as the first message. When the agent exits it writes <run>/live.exit.
  3. ck-run waits for <run>/summary.md (the agent writes it last), the agent's exit, or the timeout.
  4. No claim within start_timeout_s: ck-run takes the claim itself (so a late window does nothing) and
     falls back to the headless run. The same happens when osascript fails.

Interactive mode has no result JSON on stdout, so the prompt asks the agent to write result.md and
summary.md into the run folder. Session ids come from each CLI's own session store.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
from datetime import datetime, timedelta

from .flags import PROMPT
from .summarize import extract_structured

# Files ck-run owns in a run folder. The live Claude settings deny edits to them.
CONTROL_FILES = ("task.md", "prompt.md", "prompt-live.md", "packet.json", "cmd.txt", "claude-settings.json",
                 "claude-settings-live.json", "live.sh", ".marker", "live.claim", "live.pid", "live.exit",
                 "result.json", "rulecheck.json", "stdout.log", "stderr.log", "last.md")

APPLESCRIPT = [
    "on run argv",
    'tell application "Terminal"',
    "activate",
    "set t to do script (item 1 of argv)",
    "try",
    "set custom title of t to (item 2 of argv)",
    "end try",
    "end tell",
    "end run",
]

ITERM_APPLESCRIPT = [
    "on run argv",
    'tell application "iTerm"',
    "activate",
    "set w to (create window with default profile)",
    "tell current session of w",
    "try",
    "set name to (item 2 of argv)",
    "end try",
    "write text (item 1 of argv)",
    "end tell",
    "end tell",
    "end run",
]

UUID_RX = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
READ_CAP = 32 * 1024 * 1024


def script_text(*, run_dir: str, cwd: str, env: dict, argv: list, prompt_path: str, title: str) -> str:
    """Bash launcher for the Terminal window. The prompt is read from a file, never pasted in the window."""
    q = shlex.quote
    env_parts = [f"{k}={q(v)}" for k, v in sorted(env.items()) if k not in ("TERM", "NO_COLOR")]
    env_parts.append('TERM="${TERM:-xterm-256color}"')  # the window's terminal type; colours stay on
    if "LANG" not in env:
        env_parts.append("LANG=en_US.UTF-8")
    agent = " ".join(f'"$(cat {q(prompt_path)})"' if a is PROMPT else q(str(a)) for a in argv)
    return "\n".join([
        "#!/bin/bash",
        "# ck-run --live launcher: Terminal runs this; it starts the agent's interactive CLI.",
        f"RUN={q(run_dir)}",
        'if ! (set -C; : > "$RUN/live.claim") 2>/dev/null; then',
        '  echo "ck-run: this run was already started, or ck-run fell back to a headless run. Nothing to do."',
        "  exit 0",
        "fi",
        'echo $$ > "$RUN/live.pid.tmp" && mv "$RUN/live.pid.tmp" "$RUN/live.pid"',
        f'cd {q(cwd)} || {{ echo 127 > "$RUN/live.exit"; exit 1; }}',
        f"printf '\\033]0;%s\\007' {q(title)}",
        f"echo {q('ck-run live: ' + title)}",
        f"/usr/bin/env -i {' '.join(env_parts)} {agent}",
        'rc=$?; echo $rc > "$RUN/live.exit.tmp" && mv "$RUN/live.exit.tmp" "$RUN/live.exit"',
        'echo "ck-run: the agent exited (code $rc). This window can be closed."',
        "",
    ])


def write_script(path: str, **kw) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(script_text(**kw))


def open_window(osascript: str, command: str, title: str, env: dict, terminal: str = "Terminal") -> tuple:
    """(ok, error). Asks iTerm2 (terminal="iTerm2") or Terminal for a new window that runs `command`.
    If iTerm2 fails, Terminal is tried once before giving up."""
    if not (os.path.isfile(osascript) and os.access(osascript, os.X_OK)):
        return False, f"osascript not found: {osascript}"
    if terminal.lower() in ("iterm", "iterm2"):
        ok, err = _osa(osascript, ITERM_APPLESCRIPT, command, title, env)
        if ok:
            return ok, err
        ok2, err2 = _osa(osascript, APPLESCRIPT, command, title, env)
        return ok2, (None if ok2 else f"iTerm2: {err}; Terminal: {err2}")
    return _osa(osascript, APPLESCRIPT, command, title, env)


def _osa(osascript: str, script: list, command: str, title: str, env: dict) -> tuple:
    argv = [osascript]
    for line in script:
        argv += ["-e", line]
    argv += [command, title]
    try:
        p = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"osascript: {e}"
    if p.returncode != 0:
        return False, f"osascript exit {p.returncode}: {(p.stderr or p.stdout).strip()[:300]}"
    return True, None


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

def _contains(path: str, needle: bytes) -> bool:
    files = [path]
    if os.path.isdir(path):
        try:
            files = [os.path.join(path, n) for n in os.listdir(path)]
        except OSError:
            return False
    for p in files:
        for q in (p, p + "-wal"):
            try:
                with open(q, "rb") as f:
                    if needle in f.read(READ_CAP):
                        return True
            except OSError:
                continue
    return False


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


def find_session(agent: str, home: str, since: float, run_id: str, known_id: str | None = None) -> dict:
    """{'id', 'path', 'how'}. claude: the id ck-run set with --session-id. Others: the newest session in the
    store that started after the window opened and holds this run id."""
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
        return {"id": known_id, "path": path,
                "how": "set by ck-run with --session-id" + ("" if path else " (transcript not found)")}
    cands = sorted(_candidates(agent, home, since), reverse=True)
    needle = run_id.encode("utf-8")
    for mt, sid, path in cands:
        if _contains(path, needle):
            return {"id": sid, "path": path, "how": "found in the session store (holds the run id)"}
    if len(cands) == 1:
        return {"id": cands[0][1], "path": cands[0][2],
                "how": "only new session in the store (run id not seen in it; may be wrong)"}
    return {"id": None, "path": None, "how": f"not found ({len(cands)} new session(s) in the store)"}


def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def recover_result(path: str | None, run_id: str):
    """(result_json, text) from a JSONL session log (claude, codex, cursor), or (None, ''): the last JSON
    object in the log whose run_id is this run and that has a summary (the prompt's schema has neither)."""
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
        for s in _strings(obj):
            if run_id in s and "summary" in s:
                r = extract_structured(s)
                if isinstance(r, dict) and r.get("run_id") == run_id and "summary" in r:
                    best, text = r, s
    return best, text
