"""Patterns (v4): race and panel. Both start their runs with launch.start (detached, they survive Claude restarts).

  ck-run race --ask A --prompt-file F --title T --workspace W [--models astra,opus,grok] [--mode read|auto] [--timeout S]
      The same task to 2 or more models at once (default: race_default in config/models.json). For each alias: F is
      copied to <runs_dir>/<A>-race-<alias>_prompt.md and launched as slug <A>-race-<alias> with
      `run --model <alias> --ask A ...`. Ledger event `race` (ask_id, note: slugs). Prints the slugs, one per line.
  ck-run race show --ask A
      The race runs' summaries side by side (plain text), for Grok Bot to merge into ONE answer.
  ck-run panel RUN --by codex,claude,cursor --trigger T [--ask A] [--focus TEXT]
      One launched review of RUN per agent (slug <A>-panel-<agent>). Ledger event `panel` (ask_id, run_id = RUN,
      note: slugs). Prints the slugs. Without --focus each reviewer gets its lens from the rev2 panel table: claude
      physics and claims, codex code and numbers, cursor sources.

Checks before anything starts: the ask exists and is open (a parked ask is over budget: exit 7), the prompt file,
the workspace, the model aliases, the review reason, and that no slug was used before. Budgets are counted by the
ask gate of each launched run (a run over budget ends with exit 7 in its .out).
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import sys
import textwrap

from . import views
from .config import REPO, Config
from .errors import Refused
from .flags import AGENTS

TRIGGERS = ("leaves-lab", "hardware-or-raw", "agent-unsure", "you-asked")
SLUG_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULT_RACE = ["astra", "opus", "grok"]
MODELS_JSON = os.path.join(REPO, "config", "models.json")
# rev2 1.3 "panel": Opus (physics, claims), Sol (code, numbers), Grok (sources); by the agent that runs them
LENS = {"claude": "physics and claims", "codex": "code and numbers", "cursor": "sources"}
EXIT_RX = re.compile(r"ck-run exit=(\d+)")


def models_config() -> dict | None:
    try:
        with open(MODELS_JSON, encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def launch_start(cfg: Config, slug: str, argv: list, *, ask_id: str | None = None, cwd: str | None = None) -> dict:
    """launch.start (contract 10.4), imported when needed: a missing launch module is a clear refusal."""
    try:
        mod = importlib.import_module(f"{__package__}.launch")
    except ImportError as e:
        raise Refused(f"ck-run launch is not installed (ckdispatch/launch.py): {e}")
    return mod.start(cfg, slug, argv, ask_id=ask_id, cwd=cwd)


def over_budget(msg: str) -> Exception:
    try:
        from .asks import OverBudget
        return OverBudget(msg)
    except ImportError:
        return Refused(msg)


def need_open_ask(cfg: Config, ask_id: str, snap: views.Snapshot | None = None) -> dict:
    """The ask (views.Snapshot dict) if it exists and is open; else Refused (closed, unknown) or over budget (parked)."""
    aid = str(ask_id or "").strip()
    if not views.ASK_RX.match(aid):
        raise Refused(f"--ask {ask_id!r} is not an ask id (they look like A-1105-4; see ck-run ask list)")
    snap = snap or views.Snapshot(cfg)
    a = snap.asks.get(aid)
    if a is None or not a["known"]:
        raise Refused(f"unknown ask {aid}: open it first (ck-run ask open --words \"<chalach's words>\" --size S|M|L "
                      f"--title T)")
    st = snap.status(a)
    if st == "closed":
        raise Refused(f"ask {aid} is closed; open a new ask")
    if st == "parked":
        p = a["parks"][-1]
        raise over_budget(f"ask {aid} is parked ({p.get('reason') or p.get('note') or 'no reason given'}): new runs "
                          f"need chalach's words. Ask chalach, then use --over on a single run or open a new ask")
    return a


def parse_list(text: str | None) -> list:
    return [x.strip() for x in str(text or "").replace(" ", ",").split(",") if x.strip()]


def _check_slugs(cfg: Config, slugs: list, what: str) -> None:
    for s in slugs:
        if not SLUG_RX.match(s):
            raise Refused(f"slug {s!r} is not valid (letters, digits, '_', '.', '-'; at most 64)")
        used = [p for p in (os.path.join(cfg.runs_dir, f"{s}.sh"), os.path.join(cfg.runs_dir, f"{s}.out"))
                if os.path.exists(p)]
        if used:
            raise Refused(f"slug {s} was used before ({used[0]}): {what}. Open a new ask for a new one")


def _write_prompt(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _launch_all(cfg: Config, jobs: list, ask_id: str) -> tuple:
    """jobs: [(slug, argv)]. Returns (started [info], errors [text])."""
    started, errors = [], []
    for slug, argv in jobs:
        try:
            info = launch_start(cfg, slug, argv, ask_id=ask_id)
            started.append(dict(info or {}, slug=(info or {}).get("slug") or slug))
        except Refused as e:
            errors.append(f"{slug}: {e}")
    return started, errors


def _report(started: list, errors: list) -> int:
    for s in started:
        print(s["slug"])
    sys.stdout.flush()
    for e in errors:
        print(f"ck-run: NOT STARTED: {e}", file=sys.stderr)
    if errors:
        return 1 if started else 2
    return 0


# ---- race ----------------------------------------------------------------------------------------------------------

def race(cfg: Config, a) -> int:
    from .runner import read_task_file
    missing = [f for f, v in (("--prompt-file", a.prompt_file), ("--title", a.title), ("--workspace", a.workspace))
               if not v]
    if missing:
        raise Refused(f"race needs {', '.join(missing)} (to see a race: ck-run race show --ask {a.ask})")
    ask = need_open_ask(cfg, a.ask)
    ws = cfg.workspace(a.workspace)
    title = " ".join(str(a.title).split())[:150]
    if not title:
        raise Refused("--title is empty")
    src = os.path.abspath(os.path.expanduser(a.prompt_file))
    text = read_task_file(src)
    mc = models_config()
    aliases = parse_list(a.models) or list((mc or {}).get("race_default") or DEFAULT_RACE)
    if len(set(aliases)) != len(aliases):
        raise Refused(f"--models names a model twice: {', '.join(aliases)}")
    if not 2 <= len(aliases) <= 5:
        raise Refused(f"a race needs 2 to 5 models, not {len(aliases)} (one model is a plain ck-run run)")
    known = (mc or {}).get("aliases") or {}
    if mc is not None:
        bad = [x for x in aliases if x not in known]
        if bad:
            raise Refused(f"unknown model alias {', '.join(bad)}; use one of: {', '.join(known)} (ck-run models)")
    if a.timeout is not None and a.timeout < 1:
        raise Refused("--timeout must be at least 1 s")
    slugs = [f"{ask['ask_id']}-race-{x}" for x in aliases]
    _check_slugs(cfg, slugs, f"a race of {ask['ask_id']} with that model already started")
    jobs = []
    for alias, slug in zip(aliases, slugs):
        dst = os.path.join(cfg.runs_dir, f"{slug}_prompt.md")
        _write_prompt(dst, text)
        agent = (known.get(alias) or {}).get("agent")
        argv = (["run", "--model", alias] + (["--agent", agent] if agent in AGENTS else [])
                + ["--ask", ask["ask_id"], "--workspace", ws.key, "--title", f"{title} (race: {alias})",
                   "--prompt-file", dst, "--mode", a.mode]
                + (["--timeout", str(a.timeout)] if a.timeout else []))
        jobs.append((slug, argv))
    started, errors = _launch_all(cfg, jobs, ask["ask_id"])
    if started:
        views.ledger_append(cfg.runs_dir, event="race", ask_id=ask["ask_id"], title=title, workspace=ws.key,
                            mode=a.mode, status="dispatched", pattern="race",
                            note=f"slugs={','.join(s['slug'] for s in started)}; models={','.join(aliases)}; "
                                 f"prompt={src}" + (f"; not started: {len(errors)}" if errors else ""))
    return _report(started, errors)


def summary_run_id(text: str) -> str | None:
    """The run id in the `<runs_dir>/<run_id>/summary.md` line a finished ck-run run prints. Any line, not only
    the first: a launch's .out also holds stderr (e.g. 'LIVE WINDOW FAILED ...') before it."""
    from .runner import valid_run_id
    rid = None
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln.endswith("/summary.md"):
            cand = os.path.basename(os.path.dirname(ln))
            if valid_run_id(cand):
                rid = cand
    return rid


def _out_info(path: str) -> dict:
    """What a launch's .out shows: the exit line and the run id of the summary path."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(400_000)
    except OSError:
        return {"exists": False, "exit": None, "run_id": None}
    codes = EXIT_RX.findall(text)
    return {"exists": True, "exit": int(codes[-1]) if codes else None, "run_id": summary_run_id(text)}


def _summary_lines(snap: views.Snapshot, rid: str) -> list:
    ar = snap.agent_result(rid)
    ps = [p for p in ar.get("plain_summary") or [] if isinstance(p, str) and p.strip()]
    if ps:
        return ps
    try:
        with open(os.path.join(snap.cfg.runs_dir, rid, "summary.md"), encoding="utf-8", errors="replace") as f:
            return [ln.rstrip() for ln in f.read().splitlines() if ln.strip()][:10]
    except OSError:
        return []


def race_entries(snap: views.Snapshot, ask_id: str) -> list:
    """One entry per race slug of the ask: alias, slug, state, run id, summary lines, how sure, your move."""
    slugs = []
    for ev in snap.events:
        if ev.get("event") == "race" and ev.get("ask_id") == ask_id:
            m = re.search(r"slugs=([^;]*)", ev.get("note") or "")
            slugs += [s.strip() for s in (m.group(1) if m else "").split(",") if s.strip()]
    prefix = f"{ask_id}-race-"
    try:  # launches whose race event is missing (e.g. a crash between launch and the ledger line)
        slugs += sorted(n[:-len(".sh")] for n in os.listdir(snap.cfg.runs_dir)
                        if n.startswith(prefix) and n.endswith(".sh"))
    except OSError:
        pass
    live = snap.live_ids()
    out = []
    for slug in dict.fromkeys(slugs):
        alias = slug[len(prefix):] if slug.startswith(prefix) else slug
        info = _out_info(os.path.join(snap.cfg.runs_dir, f"{slug}.out"))
        rid = info["run_id"]
        if rid is None:  # still running (no summary path yet), or refused: find it in the ledger
            cands = [r for r in snap.runs.values() if r["ask_id"] == ask_id and r["kind"] == "dispatch"
                     and (r.get("model_alias") == alias or f"(race: {alias})" in (r.get("title") or ""))]
            if cands:
                rid = max(cands, key=lambda r: r["started"] or snap.now)["run_id"]
        r = snap.runs.get(rid) if rid else None
        res = (r or {}).get("result")
        if res:
            state = res.get("status") or "done"
        elif info["exit"] is not None:
            state = f"ended, exit {info['exit']}" + (" (over budget)" if info["exit"] == 7 else "")
        elif rid and rid in live:
            state = "running"
        else:
            state = "starting" if info["exists"] else "not started"
        mins = None
        if res and isinstance(res.get("duration_s"), (int, float)):
            mins = round(res["duration_s"] / 60.0, 1)
        ar = snap.agent_result(rid) if (rid and res) else {}
        out.append({"alias": alias, "slug": slug, "state": state, "run_id": rid,
                    "agent": (r or {}).get("agent"), "minutes": mins,
                    "how_sure": views.VERIFY_WORDS.get(ar.get("verification"), ""),
                    "your_move": ar.get("your_move") if isinstance(ar.get("your_move"), str) else "",
                    "summary": _summary_lines(snap, rid) if (rid and res) else []})
    return out


def side_by_side(blocks: list, width: int) -> str:
    """blocks: [(title, [lines])] as columns that fit `width`; stacked when a column would be under 24 characters."""
    if not blocks:
        return ""
    gap = 3
    col = (width - gap * (len(blocks) - 1)) // len(blocks)
    if col < 24:
        L = []
        for title, lines in blocks:
            L += [f"== {title}", *lines, ""]
        return "\n".join(L).rstrip() + "\n"
    cols = []
    for title, lines in blocks:
        c = textwrap.wrap(title, col) + ["-" * min(col, max(8, len(title)))]
        for ln in lines:
            c += textwrap.wrap(ln, col, subsequent_indent="  ") or [""]
        cols.append(c)
    h = max(len(c) for c in cols)
    rows = []
    for i in range(h):
        rows.append((" " * gap).join((c[i] if i < len(c) else "").ljust(col) for c in cols).rstrip())
    return "\n".join(rows) + "\n"


def race_show(cfg: Config, a) -> int:
    aid = str(a.ask or "").strip()
    if not views.ASK_RX.match(aid):
        raise Refused(f"--ask {a.ask!r} is not an ask id (they look like A-1105-4)")
    snap = views.Snapshot(cfg)
    ask = snap.asks.get(aid)
    if ask is None or not ask["known"]:
        raise Refused(f"unknown ask {aid}")
    entries = race_entries(snap, aid)
    if getattr(a, "json", False):
        print(json.dumps({"ask_id": aid, "title": snap.title(ask), "runs": entries}, indent=2, ensure_ascii=False))
        return 0
    head = f"RACE {aid}  {views.short(snap.title(ask), 60)}  ({len(entries)} model(s))"
    if not entries:
        print(head + "\nNo race runs for this ask (ck-run race --ask ... starts one).")
        return 0
    blocks = []
    for e in entries:
        meta = [f"state: {e['state']}" + (f", {e['minutes']} min" if e["minutes"] is not None else "")]
        if e["how_sure"]:
            meta.append(f"how sure: {e['how_sure']}")
        lines = meta + [""] + (e["summary"] or ["(no summary yet)"])
        if e["your_move"]:
            lines += ["", f"your move: {e['your_move']}"]
        blocks.append((f"{e['alias']}" + (f" ({e['agent']})" if e["agent"] else ""), lines))
    width = max(80, min(200, shutil.get_terminal_size((120, 40)).columns))
    sys.stdout.write(head + "\n\n" + side_by_side(blocks, width))
    done = sum(1 for e in entries if e["state"] not in ("running", "starting", "not started"))
    print(f"\n{done} of {len(entries)} finished. Merge them into ONE answer and say where they disagree.")
    return 0


def cmd_race(cfg: Config, a) -> int:
    if a.action == "show":
        return race_show(cfg, a)
    return race(cfg, a)


# ---- panel ---------------------------------------------------------------------------------------------------------

def panel(cfg: Config, a) -> int:
    from .runner import valid_run_id
    rid = str(a.run_id or "").strip()
    if not valid_run_id(rid):
        raise Refused(f"not a valid run id: {a.run_id}")
    if a.trigger not in TRIGGERS:
        raise Refused(f"--trigger must be one of: {', '.join(TRIGGERS)}")
    snap = views.Snapshot(cfg)
    r = snap.runs.get(rid)
    if r is None:
        raise Refused(f"run {rid} is not in the ledger")
    if r["result"] is None:
        raise Refused(f"run {rid} has no result yet; review it when it is done")
    agents = parse_list(a.by)
    bad = [x for x in agents if x not in AGENTS]
    if bad:
        raise Refused(f"--by: unknown agent {', '.join(bad)}; use agents from: {', '.join(AGENTS)}")
    if len(set(agents)) != len(agents):
        raise Refused(f"--by names an agent twice: {', '.join(agents)}")
    if len(agents) < 2:
        raise Refused("a panel needs 2 or 3 reviewers (one reviewer is a plain ck-run review)")
    if r["agent"] in agents:
        raise Refused(f"each reviewer must be another agent than the reviewed run's ({r['agent']})")
    ask_id = (a.ask or "").strip() or r["ask_id"]
    if not ask_id:
        raise Refused(f"run {rid} has no ask: give --ask A")
    ask = need_open_ask(cfg, ask_id, snap)
    if ask.get("size") == "S" and a.trigger != "you-asked":
        raise Refused(f"ask {ask_id} is size S: an S ask accepts only --trigger you-asked")
    slugs = [f"{ask_id}-panel-{x}" for x in agents]
    _check_slugs(cfg, slugs, f"a panel of {ask_id} with that reviewer already started")
    jobs = []
    for agent, slug in zip(agents, slugs):
        focus = (a.focus or "").strip() or LENS.get(agent)
        jobs.append((slug, ["review", rid, "--by", agent, "--trigger", a.trigger, "--ask", ask_id]
                     + (["--focus", focus] if focus else [])))
    started, errors = _launch_all(cfg, jobs, ask_id)
    if started:
        views.ledger_append(cfg.runs_dir, event="panel", ask_id=ask_id, run_id=rid, title=r["title"],
                            workspace=r["workspace"], trigger=a.trigger, status="dispatched", pattern="panel",
                            note=f"slugs={','.join(s['slug'] for s in started)}; by={','.join(agents)}; "
                                 f"trigger={a.trigger}" + (f"; not started: {len(errors)}" if errors else ""))
    return _report(started, errors)


def cmd_panel(cfg: Config, a) -> int:
    return panel(cfg, a)


def register(sub) -> None:
    r = sub.add_parser("race", help="the same task to 2 or 3 models at once (launched); `race show` puts the "
                                    "answers side by side")
    r.add_argument("action", nargs="?", choices=["show"], help="show: the race runs' summaries side by side")
    r.add_argument("--ask", required=True, help="the ask id (A-MMDD-N)")
    r.add_argument("--prompt-file", help="the task text (copied to <runs_dir>/<ask>-race-<alias>_prompt.md)")
    r.add_argument("--title")
    r.add_argument("--workspace", help="devfab | nv | washu")
    r.add_argument("--models", help="model aliases, comma-separated (default: race_default in config/models.json)")
    r.add_argument("--mode", choices=["read", "auto"], default="read", help="read (default, for a question) or auto")
    r.add_argument("--timeout", type=int, help="seconds per run (default: from the ask size)")
    r.add_argument("--json", action="store_true", help="race show: machine-readable")
    r.set_defaults(func=cmd_race)
    p = sub.add_parser("panel", help="2 or 3 launched reviews of one run by other agents, each with its own lens")
    p.add_argument("run_id")
    p.add_argument("--by", required=True, help="reviewer agents, comma-separated (e.g. codex,claude,cursor)")
    p.add_argument("--trigger", required=True, choices=TRIGGERS, help="the review reason")
    p.add_argument("--ask", help="the ask id (default: the reviewed run's ask)")
    p.add_argument("--focus", help="what every reviewer looks at first (default: a lens per agent)")
    p.set_defaults(func=cmd_panel)
