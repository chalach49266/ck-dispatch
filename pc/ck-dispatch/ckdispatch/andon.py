"""Andon: one switch that stops new runs in a project (v4; the andon idea from Cursor's orchestrate plugin).

  ck-run andon raise --project P --reason "<words>"   stop new runs and reviews of P
  ck-run andon clear --project P [--note TEXT]        allow them again
  ck-run andon list                                   what is raised now

A raised andon is the file <runs_dir>/.andon/<name>.json. Each raise and clear writes a ledger event `andon`
(project, status raised|cleared, reason, note). runner.execute calls check() right after the ask gate (run and
review): while P is raised, ck-run refuses (exit 2) with

  ANDON raised for <name>: <reason> (raised <ts>). Clear it: ck-run andon clear --project <name> --note "<words>"

<name> = the ask's project if it has one, else the workspace key. A workspace name or alias given as P (devfab,
"Device Fabrication", nv, ...) means that workspace's key, so an andon on a workspace stops every run there, also
runs whose ask names a project. An andon file that cannot be read counts as raised (a stop switch fails safe).
"""
from __future__ import annotations

import json
import os
import re
import sys

from . import ledger
from .config import Config
from .errors import Refused

DIR = ".andon"
NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")  # = asks.PROJECT_RE (a project name)


def andon_dir(runs_dir: str) -> str:
    return os.path.join(runs_dir, DIR)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", str(s or "").strip().lower()).strip("-.")[:64]


def name_for(cfg: Config, project: str | None) -> str:
    """The andon name: a workspace name or alias -> its key; else the project name in lower case ([a-z0-9_.-])."""
    p = (project or "").strip()
    if not p:
        raise Refused("--project is empty: give a project (e.g. fnd_sin) or a workspace (devfab, nv, washu)")
    for ws in cfg.workspaces.values():
        if p.lower() in ws.aliases:
            return ws.key
    if not NAME_RX.match(p):
        raise Refused(f"--project {project!r}: use letters, digits, '_', '.' or '-' (e.g. fnd_sin), or a workspace "
                      f"(devfab, nv, washu)")
    return p.lower()


def state_path(cfg: Config, name: str) -> str:
    return os.path.join(andon_dir(cfg.runs_dir), f"{name}.json")


def state(cfg: Config, name: str) -> dict | None:
    """The raised andon of <name>, or None. A file that cannot be read counts as raised."""
    p = state_path(cfg, name)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        return {"name": name, "status": "raised", "raised_at": None,
                "reason": f"the andon file {p} cannot be read ({e}); clear it to allow runs again"}
    if not isinstance(d, dict):
        return {"name": name, "status": "raised", "raised_at": None,
                "reason": f"the andon file {p} is not a JSON object; clear it to allow runs again"}
    return d if d.get("status", "raised") == "raised" else None


def message(name: str, st: dict) -> str:
    return (f"ANDON raised for {name}: {st.get('reason') or 'no reason given'} "
            f"(raised {st.get('raised_at') or 'at an unknown time'}). "
            f"Clear it: ck-run andon clear --project {name} --note \"<words>\"")


def check(cfg: Config, project: str | None, workspace_key: str) -> None:
    """Refuse (raise Refused) when an andon is raised for the ask's project or for the run's workspace."""
    names = []
    if project and str(project).strip():
        try:
            names.append(name_for(cfg, project))
        except Refused:
            n = _norm(project)
            if n:
                names.append(n)
    if workspace_key:
        names.append(str(workspace_key))
    for n in dict.fromkeys(names):
        st = state(cfg, n)
        if st:
            raise Refused(message(n, st))


def raised(cfg: Config) -> list:
    d = andon_dir(cfg.runs_dir)
    try:
        names = sorted(n[:-len(".json")] for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return []
    out = []
    for n in names:
        st = state(cfg, n)
        if st:
            out.append(dict(st, name=n))
    return out


def _append(cfg: Config, **fields) -> None:
    from .views import ledger_append  # keeps fields ledger.KEYS does not know yet (in the note)
    ledger_append(cfg.runs_dir, event="andon", **fields)


def raise_andon(cfg: Config, project: str, reason: str) -> tuple:
    """(changed, state). An andon that is already raised stays as it is (changed False)."""
    name = name_for(cfg, project)
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise Refused('--reason is empty: say why new runs must stop (chalach\'s words or the run\'s problem)')
    cur = state(cfg, name)
    if cur:
        return False, dict(cur, name=name)
    d = andon_dir(cfg.runs_dir)
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = {"name": name, "status": "raised", "reason": reason[:500], "raised_at": ledger.now_iso(),
          "project_given": project}
    tmp = os.path.join(d, f".{name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)
    try:
        os.link(tmp, state_path(cfg, name))  # atomic: fails if another raise won the race
    except FileExistsError:
        return False, dict(state(cfg, name) or st, name=name)
    finally:
        os.unlink(tmp)
    _append(cfg, project=name, status="raised", reason=st["reason"], note=f"ck-run andon raise --project {project}")
    return True, st


def clear_andon(cfg: Config, project: str, note: str | None) -> tuple:
    """(changed, the state that was cleared or None)."""
    name = name_for(cfg, project)
    cur = state(cfg, name)
    try:
        os.unlink(state_path(cfg, name))
    except FileNotFoundError:
        return False, None
    _append(cfg, project=name, status="cleared", reason=(cur or {}).get("reason"),
            note=(note or "").strip() or "cleared with no note")
    return True, cur


# ---- commands ------------------------------------------------------------------------------------------------------

def cmd_raise(cfg: Config, a) -> int:
    changed, st = raise_andon(cfg, a.project, a.reason)
    name = st.get("name")
    if changed:
        print(f"ANDON raised for {name}: {st['reason']}")
        print(f"New runs and reviews of {name} are refused until: ck-run andon clear --project {name} --note \"<words>\"")
    else:
        print(f"ANDON was already raised for {name} (since {st.get('raised_at') or 'an unknown time'}): "
              f"{st.get('reason')}. Nothing changed.")
    return 0


def cmd_clear(cfg: Config, a) -> int:
    changed, st = clear_andon(cfg, a.project, a.note)
    name = name_for(cfg, a.project)
    if changed:
        print(f"ANDON cleared for {name}. New runs of {name} may start again."
              + (f" (It was raised {st.get('raised_at')}: {st.get('reason')})" if st else ""))
    else:
        others = [r["name"] for r in raised(cfg)]
        print(f"No andon is raised for {name}; nothing to clear."
              + (f" Raised now: {', '.join(others)}." if others else ""))
    return 0


def cmd_list(cfg: Config, a) -> int:
    rows = raised(cfg)
    if getattr(a, "json", False):
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("No andon raised. Every project may start runs.")
    for r in rows:
        print(f"ANDON raised for {r['name']} since {r.get('raised_at') or '?'}: {r.get('reason')}")
    sys.stdout.flush()
    return 0


def register(sub) -> None:
    p = sub.add_parser("andon", help="stop new runs in a project (raise), allow them again (clear), or list")
    s = p.add_subparsers(dest="andon_cmd", required=True)
    r = s.add_parser("raise", help="stop new runs and reviews of a project or workspace")
    r.add_argument("--project", required=True, help="project (e.g. fnd_sin) or workspace (devfab, nv, washu)")
    r.add_argument("--reason", required=True, help="why, in plain words")
    r.set_defaults(func=cmd_raise)
    c = s.add_parser("clear", help="allow new runs of a project again")
    c.add_argument("--project", required=True)
    c.add_argument("--note", help="chalach's words, or why it is safe again")
    c.set_defaults(func=cmd_clear)
    ls = s.add_parser("list", help="list raised andons")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)
