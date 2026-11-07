"""Mock osascript for the offline tests: never talks to Terminal or iTerm2, never shows a notification.

The sandbox copies this file (with an absolute shebang) as stubs/osascript and points the test config's
live.osascript at it. The tests steer it with osascript.json next to the copy (Sandbox.window(mode, **extra));
the fake keeps its own state in osascript-state.json (windows it opened, tails served, closed sessions).

Kinds of call: a "-- ck-run:<kind>" line in the script (v4 scripts), else the script text (older scripts).
  window   a new iTerm2 window ("create window") or Terminal window ("do script"); argv: <command> <title> <profile>.
           "mode": fail (default) exit 1 like a denied Apple-events permission -> ck-run falls back to headless
                   run     act like the terminal: run the command detached, in its own session
                   silent  exit 0 but never run the command (a window that never starts the agent)
           prints "<window id>|<how>|<session id>" (iTerm2) or "<window id>|<how>" (Terminal). Window id:
           "window_id" (every window gets it, as before), else the next of "window_ids", else 4242.
           "profile_loaded": false -> "default profile (stub)" instead of "profile <name>".
  tab      a new tab in an existing iTerm2 window; argv: <command> <title> <profile> <window id>. Same modes.
           "tab_fail": true -> exit 1 (the window closed between the check and the call).
  exists   argv: <window id> <prefix>: "yes" if the fake opened that window and it is not closed
           ("closed_windows" in osascript.json, or state["closed_windows"]: see close_window()), else "no".
  tail     the window text. Old script (current session of a window id): the text. v4 script (one session by
           id or tty): "CKOK\\n<text>", or "CKNOTFOUND" for a session or tty in "missing_sessions".
           Text: "tails" (served in order, one per call; the last one repeats), else "tail_file" (read at each
           call), else "tail". After a write call, "after_write_tail" (when set) is served from then on.
  write    argv: <session id> <tty> <text>: "CKOK" (or "CKNOTFOUND").
  notify   argv: <message> <title>: "CKOK" (nothing is shown).
  close    argv: <session id> <tty>: "CKOK" (or "CKNOTFOUND"); the session is marked closed.
Every call is recorded in osascript-calls/NNN.json: argv, env, mode, kind, script (lines), args.
"""
import fcntl
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
STATE = os.path.join(HERE, "osascript-state.json")
MARKERS = ("exists", "tab", "tail", "write", "notify", "close")


def split_argv(argv):
    """(script lines, args): the values of the leading -e options, then the rest."""
    lines, i = [], 1
    while i + 1 < len(argv) and argv[i] == "-e":
        lines.append(argv[i + 1])
        i += 2
    return lines, argv[i:]


def kind_of(lines):
    for ln in lines:
        if ln.startswith("-- ck-run:"):
            k = ln[len("-- ck-run:"):].strip()
            return k if k in MARKERS else "unknown"
    text = "\n".join(lines)
    if "contents of" in text:
        return "tail"
    if "create window" in text or "do script" in text:
        return "window"
    if "display notification" in text:
        return "notify"
    if "write text" in text:
        return "write"
    return "unknown"


def record(rec):
    calls = os.path.join(HERE, "osascript-calls")
    os.makedirs(calls, exist_ok=True)
    n = len(os.listdir(calls))
    while True:  # several processes may call at once: never overwrite a record
        try:
            fd = os.open(os.path.join(calls, f"{n:03d}.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)


class State:
    """osascript-state.json under an flock (concurrent calls)."""

    def __enter__(self):
        self.fd = os.open(STATE + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        try:
            with open(STATE) as f:
                self.d = json.load(f)
        except (OSError, ValueError):
            self.d = {}
        for k, v in (("windows", {}), ("closed_windows", []), ("closed_sessions", []), ("tail_calls", 0),
                     ("sessions", 0), ("next_window", 0)):
            self.d.setdefault(k, v)
        return self.d

    def __exit__(self, *exc):
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f, indent=1)
        os.replace(tmp, STATE)
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def tail_text(ctrl, st):
    if st.get("tail_override") is not None:
        return st["tail_override"]
    if ctrl.get("tails"):
        tails = ctrl["tails"]
        i = st["tail_calls"]
        st["tail_calls"] = i + 1
        return tails[min(i, len(tails) - 1)]
    if ctrl.get("tail_file"):
        try:
            with open(ctrl["tail_file"]) as f:
                return f.read()
        except OSError:
            return ""
    return ctrl.get("tail", "")


def start_command(command):
    with open(os.path.join(HERE, "window.log"), "ab") as log:
        subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)


def new_session(st, wid):
    st["sessions"] += 1
    sid = f"sess-{wid}-{st['sessions']}"
    st["windows"].setdefault(str(wid), {"sessions": []})["sessions"].append(sid)
    return sid


def main():
    try:
        with open(os.path.join(HERE, "osascript.json")) as f:
            ctrl = json.load(f)
    except (OSError, ValueError):
        ctrl = {}
    mode = ctrl.get("mode", "fail")
    lines, args = split_argv(sys.argv)
    kind = kind_of(lines)
    record({"argv": sys.argv, "env": dict(os.environ), "mode": mode, "kind": kind, "script": lines, "args": args})
    missing = set(ctrl.get("missing_sessions", []))

    if kind == "notify":
        print("CKOK")
        return 0
    if kind == "exists":
        wid = args[0] if args else ""
        with State() as st:
            closed = set(map(str, ctrl.get("closed_windows", []))) | set(map(str, st["closed_windows"]))
            print("yes" if wid in st["windows"] and wid not in closed else "no")
        return 0
    if kind == "tail":
        with State() as st:
            text = tail_text(ctrl, st)
        if any(ln.startswith("-- ck-run:") for ln in lines):  # v4: one session by id or tty
            if (args[0] if args else "") in missing or (args[1] if len(args) > 1 else "") in missing:
                print("CKNOTFOUND")
            else:
                sys.stdout.write("CKOK\n" + text + "\n")
        else:
            sys.stdout.write(text + "\n")
        return 0
    if kind in ("write", "close"):
        target = [a for a in args[:2] if a]
        if any(t in missing for t in target):
            print("CKNOTFOUND")
            return 0
        with State() as st:
            if kind == "write" and ctrl.get("after_write_tail") is not None:
                st["tail_override"] = ctrl["after_write_tail"]
            if kind == "close":
                st["closed_sessions"].extend(target)
        print("CKOK")
        return 0
    if kind in ("window", "tab"):
        if mode == "fail" or (kind == "tab" and ctrl.get("tab_fail")):
            sys.stderr.write("execution error: Not authorized to send Apple events to Terminal. (-1743)\n")
            return 1
        iterm = any('tell application "iTerm"' in ln for ln in lines)
        if kind == "tab":
            command, profile, wid = args[0], args[2], str(args[3])
        else:
            command, profile = args[0], args[-1] if len(args) >= 3 else ""
        how = f"profile {profile}" if profile and ctrl.get("profile_loaded", True) else "default profile (stub)"
        with State() as st:
            if kind == "window":
                if "window_id" in ctrl:
                    wid = str(ctrl["window_id"])
                elif ctrl.get("window_ids"):
                    ids = ctrl["window_ids"]
                    wid = str(ids[min(st["next_window"], len(ids) - 1)])
                    st["next_window"] += 1
                else:
                    wid = "4242"
            sid = new_session(st, wid)
        if kind == "tab" or iterm:
            sys.stdout.write(f"{wid}|{how}|{sid}\n")
        else:
            sys.stdout.write(f"{wid}|{how}\n")
        if mode == "run":
            start_command(command)
        return 0
    return 0  # an unknown script: nothing to do


if __name__ == "__main__":
    sys.exit(main())
