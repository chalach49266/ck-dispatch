"""ck-run command line. See README.md."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import VERSION, ledger, route
from .config import Config
from .errors import Refused
from .flags import AGENTS
from .guards import check_ck_argv
from .runner import Spec, execute, new_run_id, read_task_file, review_spec, valid_run_id


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ck-run", description="CK's runner for claude, codex, cursor (agent) and agy.")
    ap.add_argument("--version", action="version", version=f"ck-run {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", aliases=["dispatch"], help="dispatch one task to one agent")
    r.add_argument("--agent", required=True, choices=AGENTS)
    r.add_argument("--workspace", required=True, help="devfab | nv")
    r.add_argument("--title", required=True, help="short task title (ledger, thread)")
    r.add_argument("--prompt-file", required=True, help="CK's task text")
    r.add_argument("--task-type", default="general", help=f"one of {', '.join(route.MATRIX)} (free form allowed)")
    r.add_argument("--model")
    r.add_argument("--mode", choices=["read", "edit"], default="read")
    r.add_argument("--approved", help="edit only: the user's approval, in their words, with the time")
    r.add_argument("--write-scope", action="append", default=[], help="edit only: folder the agent may change (repeatable)")
    r.add_argument("--read-dir", action="append", default=[], help="read only: extra folder to read (repeatable)")
    r.add_argument("--timeout", type=int, help="seconds")
    r.add_argument("--parent", help="run id this task follows up")
    r.add_argument("--lead", help="lead bot that supplied the domain context")
    r.add_argument("--dry-run", action="store_true", help="check and print the agent command; run nothing")
    r.set_defaults(func=cmd_run)

    v = sub.add_parser("review", help="cross-review a finished run by another agent (sends file paths, not content)")
    v.add_argument("run_id")
    v.add_argument("--by", required=True, choices=AGENTS)
    v.add_argument("--blind", action="store_true", help="independent check: reviewer gets the task, not the result")
    v.add_argument("--focus", help="what the reviewer should look at first")
    v.add_argument("--title")
    v.add_argument("--model")
    v.add_argument("--timeout", type=int)
    v.add_argument("--dry-run", action="store_true")
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

    o = sub.add_parser("route", help="suggest an agent for a task type")
    o.add_argument("--task-type", default="general")
    o.add_argument("--review-of", choices=AGENTS)
    o.set_defaults(func=cmd_route)

    m = sub.add_parser("limits", help="show (or clear) saved rate limits")
    m.add_argument("--clear", nargs="?", const="__all__", metavar="AGENT")
    m.set_defaults(func=cmd_limits)
    return ap


def cmd_run(cfg: Config, a) -> int:
    ws = cfg.workspace(a.workspace)
    if a.parent:
        if not valid_run_id(a.parent) or ledger.find_run(cfg.runs_dir, a.parent) is None:
            raise Refused(f"--parent {a.parent} is not a run in the ledger")
    title = a.title.strip()[:200]
    if not title:
        raise Refused("--title is empty")
    task = read_task_file(a.prompt_file)
    spec = Spec(agent=a.agent, ws=ws, title=title, task_text=task, task_source=os.path.abspath(os.path.expanduser(a.prompt_file)),
                task_type=a.task_type, model=a.model, mode=a.mode, approved=a.approved, scopes_in=a.write_scope,
                read_dirs=a.read_dir, timeout_s=a.timeout, parent_run_id=a.parent, kind="dispatch",
                from_="CK", lead=a.lead)
    return execute(cfg, spec, dry_run=a.dry_run)


def cmd_review(cfg: Config, a) -> int:
    spec = review_spec(cfg, a.run_id, a.by, blind=a.blind, focus=a.focus, title=a.title,
                       model=a.model, timeout_s=a.timeout)
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
                  prompt_path=res.get("prompt_path"), response_path=res.get("response_path"),
                  summary_path=res.get("summary_path"), status=res.get("status"),
                  accepted=not a.reject, note=a.note, **{"from": a.by, "to": op.get("agent")})
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
    print(json.dumps(route.suggest(cfg.runs_dir, a.task_type, a.review_of), indent=2))
    return 0


def cmd_limits(cfg: Config, a) -> int:
    if a.clear:
        route.clear(cfg.runs_dir, None if a.clear == "__all__" else a.clear)
    print(json.dumps(route.load_state(cfg.runs_dir), indent=2))
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
        ledger.append(cfg.runs_dir, event="refused", run_id=new_run_id(ws_key, "refused", agent or "none"),
                      title=(opt("--title") or "")[:200] or None, workspace=ws_key if ws else None, agent=agent,
                      status="refused", note=f"{reason} | argv: {' '.join(argv)[:500]}",
                      **{"from": "CK", "to": agent})
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
    except Refused as e:
        print(f"ck-run: REFUSED: {e}", file=sys.stderr)
        return 2
