"""Model aliases, tiers, size defaults, race models and lanes (config/models.json, BUILD_SPEC_v4 section 6).

chalach types an alias (sol, astra, opus, grok, ...). ck-run turns it into the agent and the CLI flags:

  claude  --model <opus|sonnet|haiku> [--effort <level>]
  codex   -m <model> [-c model_reasoning_effort="<level>"]
  cursor  --model <slug>                      (the effort is part of the slug, e.g. grok-4.7-high)
  agy     no flag (gemini = agy's default model)

An effort flag is passed only when models.json `_verified` lists that effort for the agent; otherwise the alias still
works, with no effort flag and a note (TODO(verify)). CK can never pass --effort or -c itself (guards.py).

Model choice for a run (resolve):
  --model <alias>  -> that alias; it implies the agent (--agent optional; another --agent is refused)
  --model <raw id> -> passed as is (--agent required)
  no --model       -> tiers[agent][tier]; tier = --tier, else the ask's default: race asks -> top, else
                      size_tier[size] (S fast, M strong, L strong), no ask -> strong.
Top tier for an S or M ask needs chalach's words (asks.gate: over budget unless --over), except a model chalach named.
"""
from __future__ import annotations

import json
import os

from .config import REPO
from .errors import Refused

MODELS_JSON = os.path.join(REPO, "config", "models.json")
TIERS = ("fast", "strong", "top")
AGENTS = ("claude", "codex", "cursor", "agy")
_CACHE: dict = {}


def path() -> str:
    return os.environ.get("CK_MODELS_CONFIG") or MODELS_JSON


def load(p: str | None = None) -> dict:
    """config/models.json (checked: every tier and lane names a known alias). Cached per file mtime."""
    p = p or path()
    try:
        st = os.stat(p)
        key = (p, st.st_mtime_ns, st.st_size)
        if key in _CACHE:
            return _CACHE[key]
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f"cannot read the model list {p}: {e}")
    _check(m, p)
    _CACHE.clear()
    _CACHE[key] = m
    return m


def _check(m: dict, p: str) -> None:
    al = m.get("aliases")
    if not isinstance(al, dict) or not al:
        raise Refused(f"{p}: no aliases")
    for name, a in al.items():
        if not isinstance(a, dict) or a.get("agent") not in AGENTS or a.get("tier") not in TIERS:
            raise Refused(f"{p}: alias {name} needs an agent ({', '.join(AGENTS)}) and a tier ({', '.join(TIERS)})")
    for agent, row in (m.get("tiers") or {}).items():
        for tier, name in (row or {}).items():
            if name not in al or al[name]["agent"] != agent:
                raise Refused(f"{p}: tiers.{agent}.{tier} = {name} is not a {agent} alias")
    for agent in AGENTS:
        if set((m.get("tiers") or {}).get(agent) or {}) != set(TIERS):
            raise Refused(f"{p}: tiers.{agent} needs fast, strong and top")
    for name in m.get("race_default") or []:
        if name not in al:
            raise Refused(f"{p}: race_default names an unknown alias {name}")
    for lane, row in (m.get("lanes") or {}).items():
        for name in [row.get("worker"), row.get("check")] + list(row.get("also") or []):
            if name is not None and name not in al and name not in ("self", "race"):
                raise Refused(f"{p}: lanes.{lane} names an unknown alias {name}")


def aliases(m: dict | None = None) -> dict:
    return (m or load())["aliases"]


def alias(name: str | None, m: dict | None = None) -> dict | None:
    if not name:
        return None
    return aliases(m).get(str(name).strip().lower())


def is_alias(name: str | None, m: dict | None = None) -> bool:
    return alias(name, m) is not None


def agent_for(model: str | None, m: dict | None = None) -> str:
    """The agent an alias implies. Refused for a raw model id (then --agent is needed)."""
    a = alias(model, m)
    if a is None:
        if model:
            raise Refused(f"--model {model} is not an alias ({', '.join(aliases(m))}): add --agent (--by for a "
                          "review): claude, codex, cursor or agy")
        raise Refused("give --agent (--by for a review: claude, codex, cursor or agy) or a --model alias "
                      "(see: ck-run models)")
    return a["agent"]


def verified_efforts(agent: str, m: dict | None = None) -> list:
    v = ((m or load()).get("_verified") or {}).get(agent) or {}
    return [str(x) for x in v.get("efforts") or []]


def effort_verified(agent: str, effort: str | None, m: dict | None = None) -> bool:
    return bool(effort) and effort in verified_efforts(agent, m)


def default_tier(size: str | None = None, pattern: str | None = None, m: dict | None = None) -> str:
    """race runs: top; else the ask size (size_tier); no ask: strong."""
    if pattern == "race":
        return "top"
    if size:
        return (m or load()).get("size_tier", {}).get(size, "strong")
    return "strong"


def resolve(agent: str | None, model: str | None = None, tier: str | None = None, *, size: str | None = None,
            pattern: str | None = None, m: dict | None = None) -> dict:
    """{agent, model (CLI model id or None), alias, tier, effort (flag value or None), family, explicit (chalach
    named the model with --model), tier_explicit, notes}. Refused: unknown tier, an alias of another agent, a raw
    model id without --agent."""
    m = m or load()
    al = m["aliases"]
    if tier is not None and tier not in TIERS:
        raise Refused(f"unknown tier '{tier}': use fast, strong or top")
    notes = []
    name = str(model).strip().lower() if model else None
    if name and name in al:
        a = al[name]
        if agent and agent != a["agent"]:
            raise Refused(f"--model {name} runs on {a['agent']}, not {agent}. Drop --agent/--by, or pick a {agent} "
                          f"model: {', '.join(n for n, x in al.items() if x['agent'] == agent)}")
        if tier and tier != a["tier"]:
            notes.append(f"--tier {tier} not used: --model {name} is the {a['tier']} tier")
        out = {"agent": a["agent"], "model": a.get("model"), "alias": name, "tier": a["tier"],
               "effort": a.get("effort"), "family": a.get("family"), "explicit": True}
    elif model:
        if not agent:
            agent_for(model, m)  # raises with the alias list
        out = {"agent": agent, "model": str(model).strip(), "alias": None, "tier": tier, "effort": None,
               "family": None, "explicit": True}
    else:
        if not agent:
            agent_for(None, m)  # raises
        t = tier or default_tier(size, pattern, m)
        name = m["tiers"][agent][t]
        a = al[name]
        out = {"agent": agent, "model": a.get("model"), "alias": name, "tier": t, "effort": a.get("effort"),
               "family": a.get("family"), "explicit": False}
    if out["effort"] and not effort_verified(out["agent"], out["effort"], m):
        notes.append(f"effort {out['effort']} is not verified for {out['agent']} (models.json _verified): "
                     "no effort flag. TODO(verify)")
        out["effort"] = None
    out["tier_explicit"] = tier is not None
    out["notes"] = notes
    return out


def race_models(m: dict | None = None) -> list:
    return list((m or load()).get("race_default") or [])


def lanes(m: dict | None = None) -> dict:
    return dict((m or load()).get("lanes") or {})


def describe(name: str | None, m: dict | None = None) -> dict | None:
    """One lane entry resolved: {alias, agent, model, effort, tier} ('self' / 'race' are kept as words)."""
    if name is None:
        return None
    if name in ("self", "race"):
        return {"alias": name, "agent": None, "model": None, "effort": None, "tier": None}
    a = alias(name, m)
    eff = a.get("effort") if a and effort_verified(a["agent"], a.get("effort"), m) else None
    return {"alias": name, "agent": a["agent"], "model": a.get("model"), "effort": eff, "tier": a["tier"]}


def _row(cells: list, widths: list) -> str:
    return "  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()


def _table(head: list, rows: list) -> list:
    widths = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    return [_row(head, widths)] + [_row(r, widths) for r in rows]


def table(m: dict | None = None) -> str:
    """`ck-run models`: aliases, tiers, size -> tier, race default, lanes, verification."""
    m = m or load()
    al = m["aliases"]
    L = [f"Models (source: {path()})", "", "Aliases (ck-run run --model <alias>; the alias picks the agent)"]
    rows = []
    for name, a in al.items():
        eff = a.get("effort") or "-"
        if a.get("effort") and not effort_verified(a["agent"], a["effort"], m):
            eff += " (not verified: no flag)"
        rows.append([name, a["agent"], a.get("model") or "(agent default)", eff, a["tier"]])
    L += _table(["alias", "agent", "model", "effort", "tier"], rows)
    L += ["", "Tiers (the model when --model is not given)"]
    L += _table(["agent"] + list(TIERS), [[ag] + [m["tiers"][ag][t] for t in TIERS] for ag in AGENTS])
    st = m.get("size_tier") or {}
    L += ["", "Default tier: " + ", ".join(f"{s} {t}" for s, t in st.items()) + "; no ask: strong; race: top. "
          "Top tier for S and M asks only when chalach names the model or says --over."]
    L += [f"Race default: {', '.join(race_models(m))}"]
    L += ["", "Lanes (ck-run route --lane <name>)"]
    L += _table(["lane", "worker", "also", "check"],
                [[n, r.get("worker"), ", ".join(r.get("also") or []) or "-", r.get("check") or "-"]
                 for n, r in lanes(m).items()])
    v = m.get("_verified") or {}
    eff = "; ".join(f"{ag} {', '.join(verified_efforts(ag, m))}" for ag in AGENTS if verified_efforts(ag, m))
    L += ["", f"Verified {v.get('checked_at') or 'UNKNOWN'}: effort flags only for {eff or 'none'}. Details: _verified."]
    return "\n".join(L) + "\n"
