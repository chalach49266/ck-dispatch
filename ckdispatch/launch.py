"""ck-run launch: start a ck-run run, review or loop DETACHED, so it survives the Claude session that started it
(ck-run v4, BUILD_SPEC_v4 10.4).

    ck-run launch <slug> <run|review|loop> [args...]      prints 4 lines: slug, sh, out, pid

It replaces the hand-made launch (prompt file, .sh with "$@", .out, background job, exit line):
  - <runs_dir>/<slug>.sh: "#!/bin/bash", a comment header (what = title, ask id, chalach's words from the ask,
    time from ledger.now_iso()), an exported CK_RUN_HOST, then one command line:
    cd <cwd> && <ck-run> <argv quoted> "$@"
  - started as /bin/bash -c '<sh> > <out> 2>&1; echo "ck-run exit=$?" >> <out>' in a new session, stdin/stdout/stderr
    not shared with the caller: launch returns at once and the run goes on after Claude restarts or quits.
  - a run argv needs --prompt-file: an existing file inside runs_dir; without one, <runs_dir>/<slug>_prompt.md is
    used when it exists. run and review get --interactive unless --live/--interactive/--no-live is there.
  - a used slug is refused (its .sh or .out exists, or the ledger has its launch): "slug used; pick a new one".
  - ledger event `launch` (slug, pid, ask_id, note "sh=<path> out=<path>", status dispatched).
  - with --dry-run or --help in the args nothing is written or started: launch shows the .sh it would write and
    runs that ck-run command (a dry run / help only) in the foreground.
The mod (Grok Bot mode) waits for "ck-run exit=N" in <slug>.out and wakes Grok Bot when it appears.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys

from . import herdr, ledger
from .config import REPO, Config, inside
from .errors import Refused
from .guards import check_ck_argv

CK_RUN = os.path.join(REPO, "bin", "ck-run")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
COMMANDS = ("run", "dispatch", "review", "loop", "reattach")  # what start() accepts (the CLI: run, review, loop)
LIVE_FLAGS = ("--live", "--interactive", "--no-live", "--no-interactive")
EXIT_LINE_RX = re.compile(r"^ck-run exit=(\d+)\s*$", re.M)


def _opt(argv: list, name: str):
    """The value of option `name` in argv (`name value` or `name=value`), else None."""
    for i, t in enumerate(argv):
        if t == name and i + 1 < len(argv):
            return argv[i + 1]
        if t.startswith(name + "="):
            return t.split("=", 1)[1]
    return None


def _set_opt(argv: list, name: str, value: str) -> list:
    """argv with option `name` set to value (replaced in place, else appended), always as two tokens."""
    out, i, done = [], 0, False
    while i < len(argv):
        t = argv[i]
        if t == name and i + 1 < len(argv):
            out += [name, value]
            i, done = i + 2, True
            continue
        if t.startswith(name + "="):
            out += [name, value]
            i, done = i + 1, True
            continue
        out.append(t)
        i += 1
    return out if done else out + [name, value]


def _raw_events(runs_dir: str, names: tuple) -> list:
    """Ledger events by name, read line by line (events with no run_id included)."""
    out = []
    try:
        with open(ledger.path(runs_dir), encoding="utf-8") as f:
            for line in f:
                if not any(f'"{n}"' in line for n in names):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("event") in names:
                    out.append(ev)
    except OSError:
        pass
    return out


def ask_words(runs_dir: str, ask_id: str | None) -> str | None:
    """chalach's verbatim words of an ask (its ask_open event), or None."""
    if not ask_id:
        return None
    for ev in reversed(_raw_events(runs_dir, ("ask_open",))):
        if ev.get("ask_id") == ask_id:
            return ev.get("words") or None
    return None


def out_finished(out_path: str) -> bool:
    """True if a launch's .out has its 'ck-run exit=' line (the launch ended)."""
    try:
        with open(out_path, encoding="utf-8", errors="replace") as f:
            return bool(EXIT_LINE_RX.search(f.read()))
    except OSError:
        return False


def paths(cfg: Config, slug: str) -> tuple:
    return os.path.join(cfg.runs_dir, f"{slug}.sh"), os.path.join(cfg.runs_dir, f"{slug}.out")


def slug_used(cfg: Config, slug: str) -> str | None:
    """Why `slug` cannot be launched again (None = free)."""
    sh, out = paths(cfg, slug)
    if os.path.exists(sh) and not out_finished(out):
        return f"{sh} exists and {out} has no 'ck-run exit=' line yet (still running, or never started)"
    if os.path.exists(out):
        return f"{out} exists"
    if any(ev.get("slug") == slug for ev in _raw_events(cfg.runs_dir, ("launch",))):
        return "the ledger has a launch with this slug"
    return None


def _clean(text) -> str:
    return " ".join(str(text or "").replace("\r", " ").split())


def _comment(label: str, text) -> list:
    """Comment lines for the .sh header; every line starts with '#'."""
    rows = [r.replace("\r", " ").rstrip() for r in str(text or "").split("\n")] or [""]
    rows = [re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", r) for r in rows]
    out = [f"# {label}: {rows[0]}".rstrip()]
    out += [f"#   {r}".rstrip() for r in rows[1:]]
    return out


def _title(argv: list, slug: str) -> str:
    t = _opt(argv, "--title")
    if t:
        return _clean(t)[:200]
    if argv and argv[0] == "review" and len(argv) > 1 and not argv[1].startswith("-"):
        by = _opt(argv, "--by")
        return f"review of {argv[1]}" + (f" by {by}" if by else "")
    if argv and argv[0] == "reattach" and len(argv) > 1:
        return f"reattach waiter for {argv[1]}"
    return slug


def plan(cfg: Config, slug: str, argv: list, *, ask_id: str | None = None, cwd: str | None = None,
         check_slug: bool = True) -> dict:
    """Check everything and build the launch (nothing is written). Raises Refused."""
    if not SLUG_RE.match(slug or ""):
        raise Refused(f"bad slug {slug!r}: use letters, digits, '_', '.', '-' (start with a letter or digit, "
                      f"at most 64), e.g. spcm-counter-1")
    argv = [str(a) for a in argv]
    if not argv or argv[0] not in COMMANDS:
        raise Refused(f"ck-run launch starts run, review or loop, not {argv[0] if argv else 'nothing'!r}; e.g. "
                      f"ck-run launch {slug} run --agent codex --workspace devfab --title \"...\" --ask A-1105-1")
    check_ck_argv(argv)  # the same check the launched ck-run makes: refuse now, not in the .out
    if check_slug:
        why = slug_used(cfg, slug)
        if why:
            raise Refused(f"slug used; pick a new one ({slug}: {why})")
    try:
        cwd = os.path.abspath(os.path.expanduser(cwd or os.getcwd()))
    except OSError as e:
        raise Refused(f"the current folder is gone ({e}); cd to a folder first")
    if not os.path.isdir(cwd):
        raise Refused(f"cwd is not a folder: {cwd}")
    cmd = argv[0]
    argv_ask = _opt(argv, "--ask")
    if ask_id and argv_ask and argv_ask != ask_id:
        raise Refused(f"the args say --ask {argv_ask} but the launch is for ask {ask_id}")
    if ask_id and not argv_ask and cmd in ("run", "dispatch", "review", "loop"):
        argv = argv + ["--ask", ask_id]
    ask = ask_id or argv_ask
    runs_real = os.path.realpath(cfg.runs_dir)
    if cmd in ("run", "dispatch", "loop"):
        pf = _opt(argv, "--prompt-file")
        if pf is None:
            default = os.path.join(cfg.runs_dir, f"{slug}_prompt.md")
            if not os.path.isfile(default):
                raise Refused(f"no prompt file: write {default} first, or pass --prompt-file <file in {cfg.runs_dir}>")
            pf = default
        p = os.path.abspath(os.path.join(cwd, os.path.expanduser(pf)))
        if not os.path.isfile(p):
            raise Refused(f"--prompt-file is not an existing file: {pf}")
        if cmd != "loop" and not inside(os.path.realpath(p), runs_real):
            raise Refused(f"--prompt-file must be a file in {cfg.runs_dir} (every run is documented there), "
                          f"e.g. {os.path.join(cfg.runs_dir, slug + '_prompt.md')}: {pf}")
        argv = _set_opt(argv, "--prompt-file", p)
    if cmd in ("run", "dispatch", "review") and not any(t in LIVE_FLAGS for t in argv):
        argv = argv + ["--interactive"]
    sh, out = paths(cfg, slug)
    title = _title(argv, slug)
    words = ask_words(cfg.runs_dir, ask)
    line = f"cd {shlex.quote(cwd)} && {shlex.quote(CK_RUN)} {' '.join(shlex.quote(a) for a in argv)} \"$@\""
    host = herdr.detect_host(cfg.live_terminal)
    text = "\n".join(["#!/bin/bash",
                      f"# ck-run launch {slug} (started detached by ck-run launch; the .out ends with 'ck-run exit=N')",
                      *_comment("what", title),
                      f"# ask: {ask or 'none'}",
                      *_comment("chalach (verbatim)", words if words else "(no ask words found)"),
                      f"# launched: {ledger.now_iso()}",
                      f"# out: {out}",
                      f"export CK_RUN_HOST={shlex.quote(host)}",
                      line, ""])
    return {"slug": slug, "sh": sh, "out": out, "argv": argv, "cwd": cwd, "ask_id": ask, "title": title,
            "text": text}


def start(cfg: Config, slug: str, argv: list, *, ask_id: str | None = None, cwd: str | None = None) -> dict:
    """Write <runs_dir>/<slug>.sh and start it detached (contract 10.4). Returns {slug, sh, out, pid}.
    argv = the ck-run arguments after `ck-run`, e.g. ["run", "--agent", "codex", ...]. Raises Refused."""
    p = plan(cfg, slug, argv, ask_id=ask_id, cwd=cwd)
    os.makedirs(cfg.runs_dir, mode=0o700, exist_ok=True)
    try:  # O_EXCL: two launches with one slug can never share the files
        fd = os.open(p["sh"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    except FileExistsError:
        raise Refused(f"slug used; pick a new one ({slug}: {p['sh']} exists)")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(p["text"])
    q = shlex.quote
    script = f"/bin/bash {q(p['sh'])} > {q(p['out'])} 2>&1; echo \"ck-run exit=$?\" >> {q(p['out'])}"
    try:
        proc = subprocess.Popen(["/bin/bash", "-c", script], cwd=p["cwd"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                close_fds=True)
    except OSError as e:
        try:
            os.unlink(p["sh"])
        except OSError:
            pass
        raise Refused(f"could not start {p['sh']}: {e}")
    agent = _opt(p["argv"], "--agent") or _opt(p["argv"], "--by")
    ledger.append(cfg.runs_dir, event="launch", slug=slug, pid=proc.pid, ask_id=p["ask_id"], title=p["title"],
                  status="dispatched", note=f"sh={p['sh']} out={p['out']}",
                  **{"from": "CK", "to": agent if agent in ("claude", "codex", "cursor", "agy") else None})
    return {"slug": slug, "sh": p["sh"], "out": p["out"], "pid": proc.pid}


# ---- CLI ----

def register(sub) -> None:
    p = sub.add_parser(
        "launch", help="start a ck-run run, review or loop detached (survives a Claude restart); writes "
                       "<slug>.sh and <slug>.out in agent_runs",
        description="Start `ck-run run|review|loop ...` detached. Writes <runs_dir>/<slug>.sh (one ck-run line, "
                    "--interactive added for run and review) and <runs_dir>/<slug>.out (ends with 'ck-run exit=N'). "
                    "A run needs its prompt in agent_runs: --prompt-file <file there>, or <slug>_prompt.md. "
                    "With --dry-run or --help in the args nothing is started.")
    p.add_argument("slug", help="name of the files: letters, digits, '_', '.', '-' (max 64), e.g. spcm-counter-1")
    p.add_argument("what", choices=["run", "dispatch", "review", "loop"], metavar="run|review|loop")
    p.add_argument("args", nargs=argparse.REMAINDER, help="the ck-run arguments of that command")
    p.set_defaults(func=cmd_launch)


def _foreground(argv: list, cwd: str | None = None) -> int:
    """Run a read-only ck-run command (help, dry run) and pass its output on."""
    try:
        p = subprocess.run([CK_RUN] + argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Refused(f"could not run ck-run {' '.join(argv[:2])}: {e}")
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    return p.returncode


def cmd_launch(cfg: Config, a) -> int:
    args = list(a.args or [])
    if args[:1] == ["--"]:
        args = args[1:]
    argv = [a.what] + args
    if any(t in ("-h", "--help") for t in args):  # the command's own help; nothing is started
        return _foreground([a.what, "--help"])
    if "--dry-run" in args:
        p = plan(cfg, a.slug, argv)
        sys.stdout.write(f"launch (dry run): nothing written, nothing started\nslug: {p['slug']}\nsh: {p['sh']}\n"
                         f"out: {p['out']}\n--- {p['sh']} would be:\n{p['text']}--- the ck-run dry run:\n")
        return _foreground(p["argv"], cwd=p["cwd"])
    info = start(cfg, a.slug, argv)
    sys.stdout.write(f"slug: {info['slug']}\nsh: {info['sh']}\nout: {info['out']}\npid: {info['pid']}\n")
    return 0
