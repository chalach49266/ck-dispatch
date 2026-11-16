"""Herdr live transport. Layout helpers adapted from ck-herdr (9b7d1cd).

Only the selected terminal is read/sent/labeled. No pane is automatically closed.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

STATE_FILE = "live-host.json"


def detect_host(configured, environ=None):
    """An explicit recorded host wins over the current calling terminal."""
    env = os.environ if environ is None else environ
    override = env.get("CK_RUN_HOST")
    if override:
        if override == "herdr" or override.startswith("herdr:"):
            main = override.partition(":")[2] or env.get("HC_TERMINAL_ID", "")
            return "herdr:" + main
        if override.lower() in ("iterm", "iterm2", "terminal"):
            return override
        raise ValueError(f"invalid CK_RUN_HOST: {override!r}")
    main = env.get("HC_TERMINAL_ID")
    return "herdr:" + main if main else configured


def hc_path():
    return os.environ.get("CK_HERDR_HC") or shutil.which("hc") or str(Path.home() / ".local/bin/hc")


def app_path():
    return os.environ.get("CK_HERDR_APP") or shutil.which("herdr-app") or str(Path.home() / ".local/bin/herdr-app")


def safe_name(run_id):
    # hc names are lowercase, limited to 32 characters. Retain uniqueness after shortening.
    slug = re.sub(r"[^a-z0-9_-]", "-", run_id.lower())
    return "ck-" + slug[:19] + "-" + hashlib.sha256(run_id.encode()).hexdigest()[:8]


def saved(run_dir):
    try:
        data = json.loads((Path(run_dir) / STATE_FILE).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(run_dir, data):
    path = Path(run_dir) / STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def space_label(runs_dir, log):
    """Called under the launch lock: resolve/create the CK space like ck-herdr."""
    def lookup():
        spaces = invoke([app_path(), "ls"], log)["spaces"]
        return next((s["id"] for s in spaces if s.get("name") == "CK"), None)
    space = lookup()
    if not space:
        invoke([app_path(), "do", "space.new", "--name", "CK", "--folder", str(runs_dir)], log)
        space = lookup()
    if not isinstance(space, str) or not space:
        raise ValueError("CK space has no ID")
    return ["--label", f"herdr.space={space}"]


def home_pane(main, log):
    """The Herdr space/tab/pane that shows terminal `main` (the agent that launches), or None."""
    try:
        for space in invoke([app_path(), "ls"], log)["spaces"]:
            for tab in space.get("tabs", []):
                for term in tab.get("terminals", []):
                    if term.get("terminal") == main:
                        return space["id"], tab["id"], term.get("pane")
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        pass
    return None


def doc_rpc(method, params):
    """One JSON-RPC call to herdr-core (hc connect). The app keeps its splits in the doc 'herdr-app'."""
    req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n"
    proc = subprocess.run([hc_path(), "connect", "--json"], input=req, capture_output=True, text=True, timeout=15)
    for line in proc.stdout.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if data.get("id") == 1:
            if "error" in data:
                raise ValueError(f"{method}: {data['error']}")
            return data["result"]
    raise ValueError(f"{method}: no reply from hc connect (exit {proc.returncode})")


def _leaves(node):
    if node.get("type") == "block":
        yield node
    for child in node.get("children", []):
        yield from _leaves(child)


def _node_id():
    return "s" + os.urandom(16).hex()


def beside_tree(root, main, agents):
    """The tab's new split tree: main on the left half, the agents stacked in the right half, equal sizes.
    Other panes in the tab stay, under main in the left half. None when main is not in the tab."""
    leaves = list(_leaves(root))
    main_leaf = next((leaf for leaf in leaves if leaf.get("content", {}).get("terminal") == main), None)
    right = [leaf for leaf in leaves if leaf.get("content", {}).get("terminal") in agents]
    if main_leaf is None or not right:
        return None
    others = [leaf for leaf in leaves if leaf is not main_leaf and leaf not in right]
    one = {"weight": 1.0}
    left = dict(main_leaf, size=one)
    if others:
        left = {"type": "column", "id": _node_id(), "size": one,
                "children": [dict(leaf, size=one) for leaf in [main_leaf, *others]]}
    column = {"type": "column", "id": _node_id(), "size": one,
              "children": [dict(leaf, size=one) for leaf in right]}
    return {"type": "row", "id": root["id"], "children": [left, column]}


def arrange_beside(tab_id, main, new, log, timeout=20.0):
    """Wait until Herdr shows terminal `new` in tab `tab_id`, then lay the tab out: main left half, every
    CK agent of this main (label ck.main=<main>) in the right half, equal heights. The doc write is
    version-checked (a conflict re-reads and retries). Returns True when the layout was written."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        doc = doc_rpc("doc.get", {"name": "herdr-app"})
        value = doc.get("value") or {}
        tab = next((t for s in value.get("spaces", []) for t in s.get("tabs", []) if t.get("id") == tab_id), None)
        if tab is None:
            raise ValueError(f"tab {tab_id} not found in the Herdr doc")
        if new not in {leaf.get("content", {}).get("terminal") for leaf in _leaves(tab["root"])}:
            time.sleep(0.4)  # the app has not adopted the new terminal yet
            continue
        listed = invoke([hc_path(), "ls", "--json", "--label", f"ck.main={main}"])
        agents = {t["address"] for t in listed.get("terminals", [])} | {new}
        tree = beside_tree(tab["root"], main, agents)
        if tree is None:
            return False
        tab["root"] = tree
        try:
            doc_rpc("doc.set", {"name": "herdr-app", "value": value, "version": doc["version"]})
        except ValueError as exc:
            if "conflict" in str(exc):
                continue  # somebody else changed the layout: read again
            raise
        log.write(f"ck-run: Herdr layout: {main} left half, {len(agents)} agent pane(s) right half, equal sizes\n")
        return True
    raise ValueError(f"Herdr did not show terminal {new} in the main tab within {timeout:.0f} s")


def invoke(argv, log=None, timeout=30):
    if log:
        log.write("$ " + shlex.join(argv) + "\n")
        log.flush()
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if log:
        log.write(proc.stdout)
        log.write(proc.stderr)
        log.flush()
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise ValueError(f"hc returned no JSON (exit {proc.returncode}): {proc.stderr.strip()}")
    if not isinstance(data, dict):
        raise ValueError("hc returned a non-object JSON reply")
    if "error" in data:
        raise ValueError(str(data["error"]))
    if proc.returncode:
        raise ValueError(f"hc failed (exit {proc.returncode}): {proc.stderr.strip()}")
    return data


# Herdr picks a pane's icon from the name of the program hc starts. Cursor's CLI starts as `node` with its
# full path as argv0, so a Cursor/Grok pane showed a plain terminal. Starting the launcher through a bash
# symlink named `agent` (Cursor's command in Herdr's program table) gives it the Cursor icon (chalach 2026-11-15).
ICON_SHELLS = {"cursor": Path(__file__).resolve().parent.parent / "bin" / "herdr-icons" / "agent"}


def pane_shell(agent):
    shim = ICON_SHELLS.get(agent or "")
    return str(shim) if shim and os.access(shim, os.X_OK) else "/bin/bash"


TITLE_MAX = 48


def job_label(title):
    """ck.title=<short job description>: the CK Herdr mods show it next to the run name (chalach 2026-11-15)."""
    t = " ".join(str(title or "").split())
    if not t:
        return []
    if len(t) > TITLE_MAX:
        t = t[:TITLE_MAX - 1].rstrip() + "…"
    return ["--label", f"ck.title={t}"]


def run_argv(run_id, cwd, script, main, labels, agent=None, title=None):
    return [hc_path(), "run", "--json", "--bg", "--outlive-parent", "--name", safe_name(run_id),
            "--cwd", str(cwd), "--label", "ck-run=true", "--label", f"ck.main={main}",
            *labels, *job_label(title), "--", pane_shell(agent), str(script)]


def plan(run_id, cwd, script, host):
    main = host.partition(":")[2]
    labels = ["--label", "herdr.space=<main's space>", "--label", "herdr.tab=<main's tab>"]
    return (f"# host: {host}; resolve main with herdr-app ls; absent main -> new tab in CK space\n"
            + shlex.join(run_argv(run_id, cwd, script, main, labels)) + "\n"
            + "# layout: main left half, agents stacked right with equal weights; version-checked herdr-app doc.set\n"
            + "# hc unavailable/failing -> configured iTerm path; Herdr panes retained; final ck.state label")


# A new pane splits the agents' column, so the panes above it shrink. Codex crashed (ratatui "index outside
# of buffer") when its pane shrank while it was still starting (A-1115-46). Launches in one runs directory
# are already serialized by the launch lock; each one now waits until the previous pane had this long to
# start (chalach 2026-11-15: "sure").
LAUNCH_GAP_S = float(os.environ.get("CK_HERDR_LAUNCH_GAP_S", "5"))
LAST_LAUNCH = ".herdr-last-launch"


def settle(runs_dir, log=None, gap_s=None):
    gap = float(os.environ.get("CK_HERDR_LAUNCH_GAP_S", LAUNCH_GAP_S)) if gap_s is None else gap_s
    try:
        last = float((Path(runs_dir) / LAST_LAUNCH).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0.0
    wait = min(gap, max(0.0, gap - (time.time() - last)))
    if wait > 0:
        if log:
            log.write(f"ck-run: Herdr waits {wait:.1f} s so the previous agent pane can finish starting\n")
        time.sleep(wait)
    return wait


def mark_launch(runs_dir):
    try:
        (Path(runs_dir) / LAST_LAUNCH).write_text(f"{time.time():.3f}\n")
    except OSError:
        pass


def open_window(run_id, cwd, script, main, runs_dir, run_dir, agent=None, title=None):
    out = {"ok": False, "error": None, "window_id": None, "terminal": "Herdr", "how": None,
           "session_id": None, "tab": False, "herdr_terminal": None}
    with (Path(run_dir) / "herdr.log").open("a") as log:
        try:
            # Serialize lookup, creation, launch and layout across all asks in this runs directory.
            with (Path(runs_dir) / ".herdr-launch.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                settle(runs_dir, log)
                home = home_pane(main, log) if main else None
                if home:
                    space, tab, _ = home
                    labels = ["--label", f"herdr.space={space}", "--label", f"herdr.tab={tab}"]
                else:
                    log.write(f"ck-run: main {main!r} not found in Herdr; falling back to new tab in CK space\n")
                    labels = space_label(runs_dir, log)
                terminal = invoke(run_argv(run_id, cwd, script, main, labels, agent, title), log)
                address = terminal.get("address") or terminal.get("id")
                if not isinstance(address, str) or not address:
                    raise ValueError("hc run returned no terminal address")
                out.update(ok=True, window_id=home[1] if home else None, herdr_terminal=address,
                           how="Herdr beside main" if home else "Herdr new CK tab", tab=bool(home))
                save(run_dir, {"host": "herdr:" + main, "terminal": address, "window": out})
                if home:
                    try:
                        if not arrange_beside(home[1], main, address, log):
                            raise ValueError("main pane disappeared before layout")
                    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
                        # The program already exists: never duplicate it because placement failed.
                        log.write(f"ck-run: Herdr layout warning; terminal retained: {exc}\n")
                mark_launch(runs_dir)
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            out["error"] = str(exc)
            log.write(f"ck-run: Herdr launch failed; falling back to iTerm path: {exc}\n")
    return out


def read(terminal, lines=100, timeout=20):
    try:
        proc = subprocess.run([hc_path(), "read", "--lines", str(lines), terminal],
                              capture_output=True, text=True, timeout=timeout)
        return proc.stdout if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def send(terminal, text):
    try:
        invoke([hc_path(), "send", "--json", terminal, text])
        return True, None
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


BOOKKEEPING_ONLY = {"required reads not shown"}


def circle_status(status, problems):
    """Status for the Herdr circle only (the ledger keeps the real status).
    A partial run whose only problems are bookkeeping (wiki-first reads not listed) shows done (green);
    chalach 2026-11-15: yellow must mean the run needs him."""
    if status == "partial" and problems and set(problems) <= BOOKKEEPING_ONLY:
        return "ok"
    return status


def state_for(status, approval=False):
    if status in ("ok", "done"):
        return "done"
    if status in ("partial", "needs-you", "blocked", "flagged") or approval:
        return "needs"  # flagged = the rule check wants chalach to look, not a failure (chalach 2026-11-15)
    return "failed"


def label(run_dir, status, approval=False):
    terminal = saved(run_dir).get("terminal")
    if not terminal:
        return
    with (Path(run_dir) / "herdr.log").open("a") as log:
        try:
            invoke([hc_path(), "label", "--json", terminal, f"ck.state={state_for(status, approval)}"], log)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            log.write(f"ck-run: Herdr status label failed: {exc}\n")


# --- closing a finished run's pane (chalach 2026-11-15: "the 10 minutes rule ... isn't working ... set it to 5")
REPO = str(Path(__file__).resolve().parent.parent)
_CLOSER = ("import sys; sys.path.insert(0, sys.argv[1]); from ckdispatch import herdr; "
           "sys.exit(herdr._closer_main(sys.argv[2:]))")


def close_later(terminal, delay_s, run_dir=None):
    """Close a cleanly finished run's Herdr pane after delay_s in a small detached process (the iTerm rule of
    live.close_tab_later): only that terminal, and only if its screen did not change in the second half of
    delay_s (a pane chalach is using stays). The other agent panes of the same main then get equal heights
    again. delay_s <= 0 = never. Returns the Popen or None."""
    try:
        delay_s = float(delay_s)
    except (TypeError, ValueError):
        return None
    if delay_s <= 0 or not terminal:
        return None
    argv = [sys.executable, "-I", "-c", _CLOSER, REPO, str(terminal), f"{delay_s:g}", run_dir or ""]
    try:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        return None


def _closer_main(args):
    from . import live
    terminal, delay_s, run_dir = args[0], float(args[1]), (args[2] or None)

    def wait(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(max(0.01, min(2.0, end - time.monotonic())))
            if run_dir and not os.path.isdir(run_dir):
                return False
        return True

    if not wait(delay_s / 2):
        return 0
    base = live.text_signature(read(terminal))
    if base is None or not wait(delay_s / 2):
        return 0  # the pane is gone already, or the run folder was removed
    if live.text_signature(read(terminal)) != base:
        return 0  # somebody used the pane: keep it
    if run_dir:
        with (Path(run_dir) / "herdr.log").open("a") as log:
            close_pane(terminal, log)
    else:
        close_pane(terminal, None)
    return 0


def _hc(args, log=None, timeout=20):
    try:
        proc = subprocess.run([hc_path(), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if log:
            log.write(f"ck-run: hc {' '.join(args)} failed: {exc}\n")
        return None
    if log:
        log.write(f"$ hc {' '.join(args)}\n{proc.stdout}{proc.stderr}")
    return proc


def close_pane(terminal, log=None):
    """Remove one CK run terminal: keep off, end its program, remove it; then re-equalize its main's agents."""
    proc = _hc(["get", "--json", terminal], log)
    try:
        labels = json.loads(proc.stdout).get("labels") or {} if proc and proc.returncode == 0 else None
    except ValueError:
        labels = None
    if not labels or labels.get("ck-run") != "true":
        return False  # gone already, or not a ck-run pane: never touch it
    _hc(["keep", terminal, "--off"], log)
    _hc(["kill", terminal], log)
    removed = False
    for _ in range(20):
        proc = _hc(["rm", "--json", terminal], log)
        if proc and proc.returncode == 0 and "removed" in proc.stdout:
            removed = True
            break
        time.sleep(0.5)
    if log:
        log.write(f"ck-run: Herdr pane {terminal} {'closed' if removed else 'NOT closed'} after the close delay\n")
    if removed and labels.get("ck.main") and labels.get("herdr.tab"):
        try:
            equalize(labels["herdr.tab"], labels["ck.main"], terminal, log)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            if log:
                log.write(f"ck-run: Herdr re-layout failed: {exc}\n")
    return removed


def equalize(tab_id, main, gone, log=None, timeout=10.0):
    """After pane `gone` closed: main left half, the remaining CK agents of main stacked right, equal heights."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        doc = doc_rpc("doc.get", {"name": "herdr-app"})
        value = doc.get("value") or {}
        tab = next((t for s in value.get("spaces", []) for t in s.get("tabs", []) if t.get("id") == tab_id), None)
        if tab is None:
            return False
        if gone in {leaf.get("content", {}).get("terminal") for leaf in _leaves(tab["root"])}:
            time.sleep(0.4)  # the app has not dropped the closed pane yet
            continue
        listed = invoke([hc_path(), "ls", "--json", "--label", f"ck.main={main}"])
        agents = {t["address"] for t in listed.get("terminals", [])} - {gone}
        tree = beside_tree(tab["root"], main, agents)
        if tree is None:
            return False  # no agent pane left beside main: the app's own layout is fine
        tab["root"] = tree
        try:
            doc_rpc("doc.set", {"name": "herdr-app", "value": value, "version": doc["version"]})
        except ValueError as exc:
            if "conflict" in str(exc):
                continue
            raise
        if log:
            log.write(f"ck-run: Herdr layout after close: {len(agents)} agent pane(s) right half, equal sizes\n")
        return True
    return False

