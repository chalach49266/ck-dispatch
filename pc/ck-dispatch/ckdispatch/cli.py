"""ck-run command line. See README.md and docs/v4.md (v4: asks, budgets, models, extension commands)."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys

from . import VERSION, asks, claims, ledger, models, route
from .config import Config
from .errors import Refused
from .flags import AGENTS
from .guards import check_ck_argv
from .runner import Spec, execute, new_run_id, read_task_file, review_spec, valid_run_id


LIVE_HELP = ("run the agent in its own interactive CLI in a new iTerm2 or Terminal window (config live.terminal; --interactive = --live; default on, from config "
             "live.default); --no-live: headless only. If the window fails, the run falls back to headless")
ASK_HELP = ("the ask this run belongs to (A-MMDD-N, from `ck-run ask open`). Required when config asks.required is true; "
            "its size sets the budget, the default tier and the default --timeout")
OVER_HELP = ("chalach's words that allow ONE dispatch over the ask's budget (logged as over_budget approved). Never "
             "passes guards, claims, the andon or a safety rule")
MODEL_HELP = ("a model alias (sol, sol-fast, astra, opus, opus-max, sonnet, haiku, grok, composer, gemini; see ck-run "
              "models), which also picks the agent, or a raw model id for --agent")
TIER_HELP = "fast, strong or top: the model is the agent's tier model (ck-run models). Default: from the ask size, else strong"
# v4 extension commands (BUILD_SPEC_v4 10.1): each module's register(sub); a module that is not there is skipped
EXTENSIONS = ("launch", "reattach", "views", "policy", "andon", "loop", "patterns")


def _live(cfg: Config, a) -> bool:
    return cfg.live_default if a.live is None else a.live


def _warn(msg: str) -> None:
    print(f"ck-run: warning: {msg}", file=sys.stderr)


def _register_extensions(sub) -> None:
    for name in EXTENSIONS:
        full = f"{__package__}.{name}"
        try:
            mod = importlib.import_module(full)
        except ImportError as e:
            if getattr(e, "name", None) != full:  # the module is there but something it imports is not
                _warn(f"the {name} commands did not load ({type(e).__name__}: {e})")
            continue
        except Exception as e:  # a broken extension must not break the core commands
            _warn(f"the {name} commands did not load ({type(e).__name__}: {e})")
            continue
        reg = getattr(mod, "register", None)
        if not callable(reg):
            continue
        try:
            reg(sub)
        except Exception as e:
            _warn(f"the {name} commands did not register ({type(e).__name__}: {e})")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ck-run", description="CK's runner for claude, codex, cursor (agent) and agy.")
    ap.add_argument("--version", action="version", version=f"ck-run {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", aliases=["dispatch"], help="dispatch one task to one agent")
    r.add_argument("--agent", choices=AGENTS, help="claude, codex, cursor or agy (optional when --model is an alias)")
    r.add_argument("--workspace", required=True, help="devfab | nv | washu (or an alias from config/workspaces.json)")
    r.add_argument("--title", required=True, help="short task title (ledger, thread)")
    r.add_argument("--prompt-file", required=True, help="CK's task text")
    r.add_argument("--task-type", default="general", help=f"one of {', '.join(route.MATRIX)} (free form allowed)")
    r.add_argument("--model", help=MODEL_HELP)
    r.add_argument("--tier", choices=models.TIERS, help=TIER_HELP)
    r.add_argument("--ask", help=ASK_HELP)
    r.add_argument("--over", help=OVER_HELP)
    r.add_argument("--mode", choices=["auto", "read", "edit"], default="auto",
                   help="auto (default): each agent's safety-reviewed auto mode; read: plan/read-only; "
                        "edit: accept-edits with --approved")
    r.add_argument("--approved", help="edit only: the user's approval, in their words, with the time")
    r.add_argument("--write-scope", action="append", default=[],
                   help="auto/edit: folder the agent may change (repeatable; auto default = workspace minus raw folders). "
                        "An edit run, or an auto run given --write-scope, claims these folders automatically (see "
                        "--claim), except a workspace root, a default write folder or a folder holding a wiki entry file")
    r.add_argument("--claim", action="append", default=[], metavar="PATH",
                   help="auto/edit: another file or folder this run will edit (repeatable; relative to the workspace "
                        "root). Refused (exit 6, busy) only if it, or an automatically claimed --write-scope folder, "
                        "overlaps a path a live run claimed. There is no workspace lock: any number of runs, any "
                        "agents, may run at once")
    r.add_argument("--read-dir", action="append", default=[], help="read only: extra folder to read (repeatable)")
    r.add_argument("--timeout", type=int, help="seconds (default: the ask size's timeout, else config default_timeout_s)")
    r.add_argument("--parent", help="run id this task follows up (in the same ask: a fix round)")
    r.add_argument("--lead", help="lead bot that supplied the domain context")
    r.add_argument("--dry-run", action="store_true", help="check and print the agent command; run nothing")
    r.add_argument("--live", "--interactive", dest="live", action=argparse.BooleanOptionalAction, default=None, help=LIVE_HELP)
    r.set_defaults(func=cmd_run)

    v = sub.add_parser("review", help="cross-review a finished run by another agent (sends file paths, not content)")
    v.add_argument("run_id")
    v.add_argument("--by", choices=AGENTS, help="the reviewing agent (optional when --model is an alias)")
    v.add_argument("--trigger", choices=asks.TRIGGERS,
                   help="why a review: leaves-lab, hardware-or-raw, agent-unsure or you-asked (needed with an ask; "
                        "an S ask takes only you-asked)")
    v.add_argument("--ask", help="the ask (default: the ask of the reviewed run)")
    v.add_argument("--over", help=OVER_HELP)
    v.add_argument("--blind", action="store_true", help="independent check: reviewer gets the task, not the result")
    v.add_argument("--focus", help="what the reviewer should look at first")
    v.add_argument("--title")
    v.add_argument("--model", help=MODEL_HELP)
    v.add_argument("--tier", choices=models.TIERS, help=TIER_HELP)
    v.add_argument("--timeout", type=int)
    v.add_argument("--dry-run", action="store_true")
    v.add_argument("--live", "--interactive", dest="live", action=argparse.BooleanOptionalAction, default=None, help=LIVE_HELP)
    v.set_defaults(func=cmd_review)

    t = sub.add_parser("thread", help="print the conversation chain of a run as markdown")
    t.add_argument("run_id")
    t.add_argument("--no-summary", action="store_true")
    t.set_defaults(func=cmd_thread)

    a = sub.add_parser("accept", help="record whether CK/user accepted a run's result")
    a.add_argument("run_id")
    a.add_argument("--reject", action="store_true")
    a.add_argument("--by", choices=["CK", "user"], default="CK")
    a.add_argument("--note")
    a.set_defaults(func=cmd_accept)

    s = sub.add_parser("show", help="print a run's summary path and summary")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_show)

    o = sub.add_parser("route", help="suggest an agent for a task type, or the models of a lane (--lane)")
    o.add_argument("--task-type", default="general")
    o.add_argument("--review-of", choices=AGENTS)
    o.add_argument("--lane", help="a lane of config/models.json (quick, lab-code, bug-fix, numbers, wiki, writing, "
                                  "decision, loop, repo, scout, mechanical): worker, also, check as agent + model, "
                                  "skipping rate-limited agents")
    o.set_defaults(func=cmd_route)

    c = sub.add_parser("active", help="list live runs and their claims (JSON); removes stale records of crashed runs")
    c.set_defaults(func=cmd_active)

    m = sub.add_parser("limits", help="show (or clear) saved rate limits")
    m.add_argument("--clear", nargs="?", const="__all__", metavar="AGENT")
    m.set_defaults(func=cmd_limits)

    md = sub.add_parser("models", help="read-only: model aliases, tiers, size defaults, race models and lanes")
    md.add_argument("--json", action="store_true", help="print config/models.json")
    md.set_defaults(func=cmd_models)

    k = sub.add_parser("ask", help="asks: one record per request from chalach (open, close, park, later, answered, "
                                   "list, show)")
    ks = k.add_subparsers(dest="ask_cmd", required=True)
    ko = ks.add_parser("open", help="open an ask; prints its id, then the start card")
    ko.add_argument("--words", required=True, help="chalach's words, verbatim")
    ko.add_argument("--size", required=True, choices=asks.SIZES)
    ko.add_argument("--title", required=True, help="the goal in one line (your restatement of chalach's ask)")
    ko.add_argument("--project", help="a project name (e.g. fnd_sin); the andon and the views use it")
    ko.add_argument("--pattern", choices=asks.PATTERNS, default="solo")
    ko.add_argument("--models", help="model aliases, comma-separated (race: the race models)")
    ko.add_argument("--parallel", type=int, help="runs live at once (needs --because)")
    ko.add_argument("--because", help="chalach's words that asked for --parallel")
    ko.add_argument("--plan", help="L only: a plan file with a line 'budget: runs=8 parallel=3 reviews=2 fixes=2 wall_min=240'")
    ko.set_defaults(func=cmd_ask_open)
    kc = ks.add_parser("close", help="close an ask: prints the finish card and accepts its open runs (safe to rerun)")
    kc.add_argument("ask")
    kc.add_argument("--done", choices=list(asks.DONE_WORDS), help="yes, partial or no (default: from the runs)")
    kc.add_argument("--note")
    kc.set_defaults(func=cmd_ask_close)
    kp = ks.add_parser("park", help="park an ask (it waits for chalach; new runs need --over)")
    kp.add_argument("ask")
    kp.add_argument("--reason", required=True)
    kp.set_defaults(func=cmd_ask_park)
    kl = ks.add_parser("later", help="add a Later item (with ITEM) or list the ask's Later items (without)")
    kl.add_argument("ask")
    kl.add_argument("item", nargs="?")
    kl.set_defaults(func=cmd_ask_later)
    kn = ks.add_parser("answered", help="Grok Bot answered the ask itself (no agent): record it and close the ask")
    kn.add_argument("ask")
    kn.add_argument("--summary", required=True, help="the answer in 1 to 3 short lines")
    kn.set_defaults(func=cmd_ask_answered)
    kli = ks.add_parser("list", help="read-only: asks with their budget use")
    kli.add_argument("--open", action="store_true", help="only open and parked asks")
    kli.add_argument("--since", help="only asks opened in this time, e.g. 7d, 12h")
    kli.add_argument("--json", action="store_true")
    kli.set_defaults(func=cmd_ask_list)
    ksh = ks.add_parser("show", help="read-only: one ask: card, use, runs, Later list, finish card")
    ksh.add_argument("ask")
    ksh.add_argument("--json", action="store_true")
    ksh.set_defaults(func=cmd_ask_show)

    _register_extensions(sub)
    return ap


def cmd_run(cfg: Config, a) -> int:
    agent = a.agent or models.agent_for(a.model)  # an alias picks the agent; prepare_v4 checks --agent + alias
    ws = cfg.workspace(a.workspace)
    if a.parent:
        if not valid_run_id(a.parent) or ledger.find_run(cfg.runs_dir, a.parent) is None:
            raise Refused(f"--parent {a.parent} is not a run in the ledger")
    title = a.title.strip()[:200]
    if not title:
        raise Refused("--title is empty")
    if a.over is not None and not a.over.strip():
        raise Refused("--over is empty: give chalach's words")
    task = read_task_file(a.prompt_file)
    spec = Spec(agent=agent, ws=ws, title=title, task_text=task, task_source=os.path.abspath(os.path.expanduser(a.prompt_file)),
                task_type=a.task_type, model=a.model, mode=a.mode, approved=a.approved, scopes_in=a.write_scope,
                claims_in=a.claim,
                read_dirs=a.read_dir, timeout_s=a.timeout, parent_run_id=a.parent, kind="dispatch",
                from_="CK", lead=a.lead, live=_live(cfg, a), ask_id=a.ask, over=a.over, tier=a.tier)
    return execute(cfg, spec, dry_run=a.dry_run)


def cmd_review(cfg: Config, a) -> int:
    if a.over is not None and not a.over.strip():
        raise Refused("--over is empty: give chalach's words")
    spec = review_spec(cfg, a.run_id, a.by, blind=a.blind, focus=a.focus, title=a.title,
                       model=a.model, timeout_s=a.timeout, live=_live(cfg, a), ask_id=a.ask, trigger=a.trigger,
                       tier=a.tier, over=a.over)
    return execute(cfg, spec, dry_run=a.dry_run)


def _need_run(cfg: Config, run_id: str):
    if not valid_run_id(run_id):
        raise Refused(f"not a valid run id: {run_id}")
    r = ledger.find_run(cfg.runs_dir, run_id)
    if r is None:
        raise Refused(f"run {run_id} is not in the ledger")
    return r


def cmd_thread(cfg: Config, a) -> int:
    _need_run(cfg, a.run_id)
    sys.stdout.write(ledger.render_thread(ledger.load(cfg.runs_dir), a.run_id, with_summary=not a.no_summary))
    return 0


def cmd_accept(cfg: Config, a) -> int:
    r = _need_run(cfg, a.run_id)
    res = r.last("result")
    if res is None:
        raise Refused(f"run {a.run_id} has no result to accept")
    op = r.opening
    ledger.append(cfg.runs_dir, event="accept", run_id=a.run_id, parent_run_id=op.get("parent_run_id"),
                  title=op.get("title"), workspace=op.get("workspace"), task_type=op.get("task_type"),
                  agent=op.get("agent"), model=op.get("model"), mode=op.get("mode"),
                  auto_readonly=op.get("auto_readonly"), prompt_path=res.get("prompt_path"), response_path=res.get("response_path"),
                  summary_path=res.get("summary_path"), status=res.get("status"),
                  accepted=not a.reject, note=a.note, ask_id=op.get("ask_id"), size=op.get("size"),
                  pattern=op.get("pattern"), tier=op.get("tier"), model_alias=op.get("model_alias"),
                  project=op.get("project"), **{"from": a.by, "to": op.get("agent")})
    print(f"{a.run_id}: {'accepted' if not a.reject else 'rejected'} by {a.by}")
    return 0


def cmd_show(cfg: Config, a) -> int:
    r = _need_run(cfg, a.run_id)
    res = r.last("result")
    if res is None or not res.get("summary_path"):
        raise Refused(f"run {a.run_id} has no summary yet")
    with open(res["summary_path"], encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()[:10]
    sys.stdout.write(res["summary_path"] + "\n" + "\n".join(lines) + "\n")
    return 0


def cmd_route(cfg: Config, a) -> int:
    if a.lane:
        print(json.dumps(route.lane(cfg.runs_dir, a.lane), indent=2))
        return 0
    print(json.dumps(route.suggest(cfg.runs_dir, a.task_type, a.review_of), indent=2))
    return 0


def cmd_active(cfg: Config, a) -> int:
    live, stale = claims.active(cfg.runs_dir, clean=True)
    claims.log_stale(cfg.runs_dir, stale, by="ck-run active")
    keys = ("run_id", "agent", "workspace", "mode", "auto_readonly", "kind", "title", "claims", "claims_auto",
            "write_scope", "pid", "agent_pid", "orphan", "started_at", "ask_id", "model_alias", "tier")
    print(json.dumps({"live": [{k: r.get(k) for k in keys if k in r} for r in live],
                      "stale_cleaned": [r.get("run_id") for r in stale]}, indent=2))
    return 0


def cmd_limits(cfg: Config, a) -> int:
    if a.clear:
        route.clear(cfg.runs_dir, None if a.clear == "__all__" else a.clear)
    print(json.dumps(route.load_state(cfg.runs_dir), indent=2))
    return 0


def cmd_models(cfg: Config, a) -> int:
    if a.json:
        print(json.dumps(models.load(), indent=2))
    else:
        sys.stdout.write(models.table())
    return 0


# ---- ck-run ask ... (asks.py) -------------------------------------------------------------------------------

def cmd_ask_open(cfg: Config, a) -> int:
    st = asks.open_ask(cfg, words=a.words, size=a.size, title=a.title, project=a.project, pattern=a.pattern,
                       models=a.models, parallel=a.parallel, because=a.because, plan=a.plan)
    sys.stdout.write(st["ask_id"] + "\n" + asks.start_card(st) + "\n")
    return 0


def cmd_ask_close(cfg: Config, a) -> int:
    out = asks.close(cfg, a.ask, done=a.done, note=a.note)
    sys.stdout.write(out["card"] + "\n")
    if out["already"]:
        print(f"ck-run: {out['ask_id']} was already closed; this is its finish card.", file=sys.stderr)
    if out["still_live"]:
        print(f"ck-run: NOTE: {len(out['still_live'])} run(s) of {out['ask_id']} still running: "
              f"{', '.join(out['still_live'])}", file=sys.stderr)
    return 0


def cmd_ask_park(cfg: Config, a) -> int:
    st = asks.park(cfg, a.ask, a.reason)
    print(f"{st['ask_id']}: parked ({st['park_reason']}). New runs need --over \"<chalach's words>\".")
    return 0


def cmd_ask_later(cfg: Config, a) -> int:
    items = asks.later_add(cfg, a.ask, a.item) if a.item is not None else asks.later_list(cfg, a.ask)
    if a.item is not None:
        print(f"{asks.need(cfg, a.ask)['ask_id']}: Later item {len(items)} added.")
    if not items:
        print("Later: none")
    for i, x in enumerate(items, 1):
        print(f"{i}) {x}")
    return 0


def cmd_ask_answered(cfg: Config, a) -> int:
    out = asks.answered(cfg, a.ask, a.summary)
    sys.stdout.write(out["card"] + "\n")
    return 0


def cmd_ask_list(cfg: Config, a) -> int:
    rows = asks.list_asks(cfg, open_only=a.open, since=a.since)
    if a.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("no asks" + (" open" if a.open else "") + (f" since {a.since}" if a.since else ""))
    for st in rows:
        print(asks.list_line(st))
    return 0


def cmd_ask_show(cfg: Config, a) -> int:
    if a.json:
        events = ledger.load(cfg.runs_dir)
        st = asks.need(cfg, a.ask, events)
        st["usage"] = asks.usage(cfg, st["ask_id"], events)
        st["later"] = asks.later_list(cfg, st["ask_id"], events)
        print(json.dumps(st, indent=2, default=str))
        return 0
    sys.stdout.write(asks.show(cfg, a.ask))
    return 0


def _log_cli_refusal(argv: list, reason: str) -> None:
    """Best effort: a refused command line still gets a ledger line."""
    def opt(name):
        for i, t in enumerate(argv):
            if t == name and i + 1 < len(argv):
                return argv[i + 1]
            if t.startswith(name + "="):
                return t.split("=", 1)[1]
        return None
    try:
        cfg = Config()
        agent = opt("--agent") or opt("--by")
        agent = agent if agent in AGENTS else None
        ws = opt("--workspace")
        ws_key = cfg.workspace(ws).key if ws else "none"
        ask_id = opt("--ask")
        ledger.append(cfg.runs_dir, event="refused", run_id=new_run_id(ws_key, "refused", agent or "none"),
                      title=(opt("--title") or "")[:200] or None, workspace=ws_key if ws else None, agent=agent,
                      status="refused", note=f"{reason} | argv: {' '.join(argv)[:500]}",
                      ask_id=ask_id if asks.valid_id(ask_id) else None, **{"from": "CK", "to": agent})
    except Exception:
        pass


def main(argv: list) -> int:
    try:
        check_ck_argv(argv)
    except Refused as e:
        _log_cli_refusal(argv, str(e))
        print(f"ck-run: REFUSED: {e}", file=sys.stderr)
        return 2
    a = _parser().parse_args(argv)
    try:
        cfg = Config()
        return a.func(cfg, a)
    except asks.OverBudget as e:  # before Refused: OverBudget is a Refused (BUILD_SPEC_v4 10.2)
        print(f"ck-run: OVER BUDGET: {e}", file=sys.stderr)
        return 7
    except Refused as e:
        print(f"ck-run: REFUSED: {e}", file=sys.stderr)
        return 2
