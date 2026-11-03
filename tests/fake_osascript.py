"""Mock osascript for the offline tests: never talks to Terminal.

The sandbox copies this file (with an absolute shebang) as stubs/osascript and points the test config's
live.osascript at it. Behaviour comes from osascript.json next to the copy: {"mode": ...}
  fail    (default) exit 1 like a denied Apple-events permission -> ck-run falls back to headless
  run     act like Terminal: run the window command (item 1 of argv) detached, in its own session
  silent  exit 0 but never run the command (a window that never starts the agent)
argv after the script lines: <command> <title> <profile>. A window call prints "<window id>\t<how>" like the real
scripts; the window-contents call (script with "contents of") prints {"tail": ...} from osascript.json.
Every call is recorded in osascript-calls/NNN.json (argv, env).
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))


def main():
    try:
        with open(os.path.join(HERE, "osascript.json")) as f:
            ctrl = json.load(f)
    except (OSError, ValueError):
        ctrl = {}
    mode = ctrl.get("mode", "fail")
    calls = os.path.join(HERE, "osascript-calls")
    os.makedirs(calls, exist_ok=True)
    n = len(os.listdir(calls))
    with open(os.path.join(calls, f"{n:03d}.json"), "w") as f:
        json.dump({"argv": sys.argv, "env": dict(os.environ), "mode": mode}, f)
    script = " ".join(sys.argv[1:])
    if "contents of" in script:  # live.window_tail
        sys.stdout.write(ctrl.get("tail", "") + "\n")
        sys.exit(0)
    if mode == "fail":
        sys.stderr.write("execution error: Not authorized to send Apple events to Terminal. (-1743)\n")
        sys.exit(1)
    command, profile = sys.argv[-3], sys.argv[-1]  # osascript -e ... -e ... <command> <title> <profile>
    how = f"profile {profile}" if profile and ctrl.get("profile_loaded", True) else "default profile (stub)"
    sys.stdout.write(f"{ctrl.get('window_id', 4242)}\t{how}\n")
    if mode == "run":
        with open(os.path.join(HERE, "window.log"), "ab") as log:
            subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, close_fds=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
