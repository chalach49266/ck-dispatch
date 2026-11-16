"""ck-run rules: what each agent may do now, chalach's decisions, and drift between them (v4, one truth).

  ck-run rules            a plain table per agent, read from the real code and config (where it may edit, web,
                          git push, pip install, cloud sims, post-run review, default models), then chalach's
                          decisions (config/policy.json: id, date, words = chalach verbatim, rule, enforced_by),
                          then DRIFT lines
  ck-run rules --check    only the DRIFT lines; exit 1 when there is drift, 0 when there is none

A DRIFT line names a place where the code or config disagrees with a decision, for example git push and pip install
(allowed by chalach on 2026-11-04) still denied in config/claude-guard.base.json. This module only reports: it never
changes the guard file, the config or the code (that is chalach's call). A check that cannot run (for example a
command another part of the build has not added yet) is listed as SKIPPED and is not drift.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from . import flags, guards, rulecheck
from .config import CLAUDE_GUARD_BASE, REPO, RESULT_SCHEMA, Config
from .errors import Refused

POLICY = os.path.join(REPO, "config", "policy.json")
MODELS = os.path.join(REPO, "config", "models.json")
KEYS = ("id", "date", "words", "rule", "enforced_by")
WEB_TOOLS = ("WebSearch", "WebFetch")
GIT_PUSH_DENY = re.compile(r"^Bash\(\s*git\s+push\b", re.I)
PIP_DENY = re.compile(r"^Bash\(\s*(?:pip3?|uv\s+pip|python3?\s+-m\s+pip)\s+install\b", re.I)
AREA_WORDS = {"workspace": "workspace*", "local": "repo"}


class Skip(Exception):
    """A check that cannot run now (not drift)."""


def load(path: str | None = None) -> list:
    p = path or POLICY
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f"cannot read {p}: {e}")
    if not isinstance(data, list):
        raise Refused(f"{p} must be a JSON list of decisions")
    seen = set()
    for i, d in enumerate(data):
        if not isinstance(d, dict) or any(not isinstance(d.get(k), str) or not d.get(k).strip() for k in KEYS):
            raise Refused(f"{p}: decision {i + 1} needs non-empty strings {', '.join(KEYS)}")
        if d["id"] in seen:
            raise Refused(f"{p}: decision id {d['id']} is used twice")
        seen.add(d["id"])
    return data


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _sub(parser, name: str):
    """The subparser `name` of a parser (argparse), or None."""
    if parser is None:
        return None
    for act in parser._actions:
        if isinstance(act, argparse._SubParsersAction):
            return act.choices.get(name)
    return None


def _opt(parser, flag: str):
    if parser is None:
        return None
    return next((act for act in parser._actions if flag in act.option_strings), None)


def _pair(argv: list, flag: str, value: str) -> bool:
    return any(argv[i] == flag and argv[i + 1] == value for i in range(len(argv) - 1))


class Ctx:
    """What the code and config say now. Every read is lazy and never writes anything."""

    def __init__(self, cfg: Config, guard_path: str | None = None, models_path: str | None = None):
        self.cfg = cfg
        self.guard_path = guard_path or CLAUDE_GUARD_BASE
        g = _read_json(self.guard_path)
        perms = (g or {}).get("permissions") or {}
        self.guard_ok = g is not None
        self.allow = [str(x) for x in perms.get("allow") or []]
        self.deny = [str(x) for x in perms.get("deny") or []]
        self.raw_cfg = _read_json(cfg.path) or {}
        self.models_path = models_path or MODELS
        m = _read_json(self.models_path)
        self.models = m if isinstance(m, dict) else None
        self.run_dir = os.path.join(cfg.runs_dir, ".rules-check")  # only a path inside argv; never created
        self._parser, self._parser_err = None, None
        self._prompt, self._prompt_err = None, None
        self._argv = {}

    def rel(self, path: str) -> str:
        r = os.path.realpath(path)
        return os.path.relpath(r, REPO) if r.startswith(os.path.realpath(REPO) + os.sep) else path

    def post_review_enabled(self) -> bool:
        v = getattr(self.cfg, "post_review_enabled", None)  # what the runner reads (config.Config)
        if isinstance(v, bool):
            return v
        pr = self.raw_cfg.get("post_review") or {}
        return bool(pr.get("enabled", False))  # the code default is false (chalach 2026-11-05)

    def parser(self):
        if self._parser is None and self._parser_err is None:
            try:
                from . import cli
                self._parser = cli._parser()
            except Exception as e:  # never let a view crash on another module
                self._parser_err = f"{type(e).__name__}: {e}"
        return self._parser

    def workspace(self):
        ws = self.cfg.workspaces
        return ws.get("devfab") or next(iter(ws.values()), None)

    def prompt_text(self) -> str | None:
        if self._prompt is None and self._prompt_err is None:
            ws = self.workspace()
            try:
                from . import prompt as P
                self._prompt = P.header(ws=ws, run_id="2026-01-01T000000-rules-general-codex-0000", agent="codex",
                                        kind="dispatch", mode="auto", approved=None, scopes=[])
            except Exception as e:
                self._prompt_err = f"{type(e).__name__}: {e}"
        return self._prompt

    def argv(self, agent: str, mode: str, scoped: bool = False) -> list | None:
        key = (agent, mode, scoped)
        if key not in self._argv:
            ws = self.workspace()
            scopes = [{"path": os.path.join(ws.root, "rules-check-scope"),
                       "literal": os.path.join(ws.root, "rules-check-scope")}] if scoped else []
            try:
                cmd = flags.build(self.cfg, ws, agent, mode=mode, scopes=scopes, model=None, run_dir=self.run_dir,
                                  schema_path=RESULT_SCHEMA, extra_read_dirs=[], timeout_s=600,
                                  claude_settings_path=os.path.join(self.run_dir, "claude-settings.json"),
                                  binary="/usr/bin/true")
                self._argv[key] = [str(x) for x in cmd.argv]
            except Exception:
                self._argv[key] = None
        return self._argv[key]


# ---- what each agent may do now --------------------------------------------------------------------------------

def _hard_clause(text: str | None, start: str) -> str | None:
    """The clause of the prompt's HARD RULES line that starts with `start` (up to the next ';')."""
    for line in (text or "").splitlines():
        if line.startswith("HARD RULES"):
            m = re.search(re.escape(start) + r"[^;.]*", line)
            return m.group(0).strip() if m else None
    return None


def model_cells(ctx: Ctx, agent: str) -> tuple:
    m = ctx.models
    if m:
        tiers = (m.get("tiers") or {}).get(agent) or {}
        st = m.get("size_tier") or {}
        s = tiers.get(st.get("S", "fast")) or "?"
        mm = tiers.get(st.get("M", "strong")) or "?"
        ll = tiers.get(st.get("L", "strong")) or "?"
        return s, mm if mm == ll else f"{mm} / {ll}"
    if agent == "codex":
        return flags.CODEX_AUTO_MODEL, flags.CODEX_AUTO_MODEL
    return "CLI default", "CLI default"


def agent_facts(ctx: Ctx) -> list:
    rows = []
    allowed_names = " ".join(n for n, _ in rulecheck.ALLOWED_ACTIONS)
    cloud_checked = any(cat == "cloud_sim" for cat, _ in rulecheck.PATTERNS)
    for agent in flags.AGENTS:
        auto = [AREA_WORDS.get(x, x) for x in flags.AUTO_AREAS.get(agent, [])]
        edit = [AREA_WORDS.get(x, x) for x in flags.EDIT_AREAS.get(agent, [])]
        if agent == "claude":
            web_denied = [x for x in ctx.deny if x.split("(")[0] in WEB_TOOLS]
            web = "NO (denied)" if web_denied else ("yes" if all(w in ctx.allow for w in WEB_TOOLS) else "NO (not allowed)")
            push = "NO (denied)" if any(GIT_PUSH_DENY.match(x) for x in ctx.deny) else "yes"
            pip = "NO (denied)" if any(PIP_DENY.match(x) for x in ctx.deny) else "yes"
        else:
            if agent == "codex":
                a = ctx.argv("codex", "read")
                web = "unknown" if a is None else ("yes" if 'web_search="live"' in a else "NO (no web_search)")
            else:
                web = "yes"  # no ck-run flag turns the agent's own web tools off
            push = "yes (a note)" if "git push" in allowed_names else "NO (violation)"
            pip = "yes (a note)" if "pip install" in allowed_names else "NO (violation)"
        if agent in flags.POST_REVIEWED:
            review = "yes, auto runs" if ctx.post_review_enabled() else "off"
        else:
            review = "no"
        ms, mml = model_cells(ctx, agent)
        rows.append({"agent": agent, "edit_auto": ", ".join(auto) or "nowhere",
                     "edit_approved": ", ".join(edit) or "nowhere", "web": web, "git_push": push,
                     "pip_install": pip, "cloud_sims": "no (violation)" if cloud_checked else "not checked",
                     "post_run_review": review, "model_s": ms, "model_ml": mml})
    return rows


def workspace_facts(cfg: Config) -> list:
    out = []
    for ws in cfg.workspaces.values():
        rels = [e.rel for e in ws.writable_only]
        if rels and all(re.search(r"/(solutions|prep)$", r) for r in rels):
            courses = sorted({r.rsplit("/", 1)[0] for r in rels})
            text = f"writes only in <course>/solutions/ and <course>/prep/ ({len(courses)} courses)"
        elif rels:
            text = "writes only in " + ", ".join(f"{r}/" for r in rels)
        else:
            text = f"writes anywhere except {len(ws.raw)} raw folders, {len(ws.extra)} agent/app folders and the rule files"
        out.append({"workspace": ws.key, "name": ws.name, "text": text, "raw": [e.rel for e in ws.raw],
                    "writable_only": rels})
    return out


# ---- drift checks (one per decision id; a decision without a check is only listed) -----------------------------

def _d1(ctx, d):
    return [] if "workspace" in flags.EDIT_AREAS.get("codex", []) else [
        "codex may not edit workspace folders (flags.EDIT_AREAS['codex'] has no 'workspace')"]


def _d2(ctx, d):
    out = []
    if "skip-permissions" not in guards.FORBIDDEN_SUBSTRINGS:
        out.append("skip-permissions is not refused (guards.FORBIDDEN_SUBSTRINGS)")
    a = ctx.argv("agy", "edit", scoped=True)
    if a is not None and not _pair(a, "--mode", "accept-edits"):
        out.append("agy edit argv has no --mode accept-edits")
    return out


def _d4(ctx, d):
    a = ctx.argv("cursor", "read")
    if a is None:
        raise Skip("could not build a cursor argv")
    out = []
    if "--trust" not in a:
        out.append("cursor argv has no --trust")
    if not _pair(a, "--mode", "plan"):
        out.append("cursor read argv has no --mode plan")
    return out


def _plan_claude(ctx, d):
    have = flags.EDIT_AREAS.get("claude")
    return [] if have == ["local"] else [f"claude edit mode may edit {have}, not only a ~/PhD_local repo"]


def _washu(ctx, d):
    ws = ctx.cfg.workspaces.get("washu")
    if ws is None:
        raise Skip("no washu workspace in the config")
    if not ws.writable_only:
        return ["washu has no writable_only list: course files are writable"]
    bad = [e.rel for e in ws.writable_only if not re.search(r"/(solutions|prep)$", e.rel)]
    return [f"washu writable_only has folders other than <course>/solutions or prep: {', '.join(bad)}"] if bad else []


def _auto_mode(ctx, d):
    run = _sub(ctx.parser(), "run")
    if run is None:
        raise Skip(f"no ck-run parser or run command ({ctx._parser_err or 'run missing'})")
    act = _opt(run, "--mode")
    have = act.default if act else None
    return [] if have == "auto" else [f"ck-run run --mode defaults to {have!r}, not auto"]


def _cursor_allow_all(ctx, d):
    a = ctx.argv("cursor", "auto")
    if a is None:
        raise Skip("could not build a cursor auto argv")
    out = []
    if "--force" not in a:
        out.append("cursor auto argv has no --force (allow-all)")
    if "--force" not in guards.AUTO_AGENT_FLAGS.get("cursor", set()):
        out.append("guards.AUTO_AGENT_FLAGS does not admit cursor --force")
    return out


def _agy_edit(ctx, d):
    a = ctx.argv("agy", "auto")
    if a is None:
        raise Skip("could not build an agy auto argv")
    out = [] if _pair(a, "--mode", "auto") else ["agy auto argv has no --mode auto"]
    out += [f"agy auto argv has {t}" for t in a if "skip-permissions" in t.lower()]
    return out


def _agy_dirs(ctx, d):
    a = ctx.argv("agy", "auto", scoped=True)
    if a is None:
        raise Skip("could not build an agy auto argv")
    ws = ctx.workspace()
    out = []
    if not _pair(a, "--add-dir", ws.root):
        out.append("agy auto argv has no --add-dir for the workspace root")
    if not _pair(a, "--add-dir", ctx.run_dir):
        out.append("agy auto argv has no --add-dir for the run folder")
    return out


def _aqs_writable(ctx, d):
    ws = ctx.cfg.workspaces.get("nv")
    if ws is None:
        raise Skip("no nv workspace in the config")
    out = []
    if not any(e.rel == "AQS" for e in ws.writable_only):
        out.append("nv writable_only does not list AQS")
    if any(e.rel == "AQS" for e in ws.raw):
        out.append("nv raw_readonly lists AQS")
    return out


def _aqs_review(ctx, d):
    from . import postreview
    return [] if "AQS" in postreview.WRITABLE_SEGMENTS.get("nv", set()) else [
        "the post-run review counts nv AQS/ as raw (postreview.WRITABLE_SEGMENTS)"]


def _auto_readonly(ctx, d):
    from . import runner
    return [] if callable(getattr(runner, "auto_readonly_reason", None)) else ["runner.auto_readonly_reason is gone"]


def _word_only(ctx, d):
    extra = sorted(set(rulecheck.TEXT_FLAG_CATEGORIES) - {"credential", "cloud_sim", "permission_bypass"})
    return [f"text-only mentions of {', '.join(extra)} are still flags (rulecheck.TEXT_FLAG_CATEGORIES)"] if extra else []


def _git_push_pip(ctx, d):
    out = []
    if not ctx.guard_ok:
        raise Skip(f"cannot read {ctx.guard_path}")
    gp = [x for x in ctx.deny if GIT_PUSH_DENY.match(x)]
    pip = [x for x in ctx.deny if PIP_DENY.match(x)]
    g = ctx.rel(ctx.guard_path)
    if gp:
        out.append(f"git push is allowed (2026-11-04), but {g} denies {', '.join(gp)}: claude runs cannot push")
    if pip:
        out.append(f"pip install is allowed (2026-11-04), but {g} denies {', '.join(pip)}: claude runs cannot "
                   f"pip install")
    names = " ".join(n for n, _ in rulecheck.ALLOWED_ACTIONS)
    if "git push" not in names:
        out.append("the rule check does not allow git push (rulecheck.ALLOWED_ACTIONS)")
    if "pip install" not in names:
        out.append("the rule check does not allow pip install (rulecheck.ALLOWED_ACTIONS)")
    text = ctx.prompt_text()
    push = _hard_clause(text, "no git push")
    if push:
        out.append(f'git push is allowed (2026-11-04), but the agent prompt says "{push}" (prompt.py HARD RULES)')
    clause = _hard_clause(text, "no git clone")
    if clause and re.search(r"\bpip\b", clause):
        out.append(f'pip install is allowed (2026-11-04), but the agent prompt says "{clause}" (prompt.py HARD RULES)')
    return out


def _web(ctx, d):
    if not ctx.guard_ok:
        raise Skip(f"cannot read {ctx.guard_path}")
    out = []
    g = ctx.rel(ctx.guard_path)
    denied = [x for x in ctx.deny if x.split("(")[0] in WEB_TOOLS]
    missing = [w for w in WEB_TOOLS if w not in ctx.allow]
    if denied:
        out.append(f"web is allowed (2026-11-05), but {g} denies {', '.join(denied)}")
    if missing:
        out.append(f"web is allowed (2026-11-05), but {g} does not allow {', '.join(missing)} "
                   f"(a headless claude run cannot ask)")
    for mode in ("read", "auto"):
        a = ctx.argv("codex", mode)
        if a is not None and 'web_search="live"' not in a:
            out.append(f'web is allowed (2026-11-05), but the codex {mode} argv has no -c web_search="live"')
    if 'web_search="live"' not in guards.ALLOWED_CONFIG_OVERRIDES:
        out.append('the guard refuses codex -c web_search="live" (guards.ALLOWED_CONFIG_OVERRIDES)')
    text = ctx.prompt_text()
    if text is not None and not re.search(r"^WEB: .*\ballowed\b", text, re.M):
        out.append("the agent prompt has no WEB line that says web search is allowed (prompt.WEB_RULE)")
    return out


def _post_review(ctx, d):
    if ctx.post_review_enabled():
        return [f"the post-run review is off by decision, but {ctx.rel(ctx.cfg.path)} has post_review.enabled = "
                f"true"]
    return []


def _find_budgets(block):
    if not isinstance(block, dict):
        return None
    if all(isinstance(block.get(k), dict) for k in "SML"):
        return block
    for v in block.values():
        if isinstance(v, dict) and all(isinstance(v.get(k), dict) for k in "SML"):
            return v
    return None


def _sizes(ctx, d):
    exp = d.get("expect") or {}
    out = []
    from . import runner
    want_exit = exp.get("over_budget_exit", 7)
    if runner.EXIT.get("over_budget") != want_exit:
        out.append(f"exit {want_exit} (over budget) is not in runner.EXIT")
    rv = _sub(ctx.parser(), "review")
    if rv is None:
        raise Skip(f"no ck-run parser or review command ({ctx._parser_err or 'review missing'})")
    act = _opt(rv, "--trigger")
    want = set(exp.get("review_triggers") or [])
    if act is None:
        out.append("ck-run review has no --trigger (a review needs one of the 4 reasons)")
    elif act.choices and want and set(act.choices) != want:
        out.append(f"ck-run review --trigger accepts {', '.join(sorted(act.choices))}, not the 4 reasons")
    budgets = _find_budgets(ctx.raw_cfg.get("asks"))
    for size, keys in (exp.get("asks_budgets") or {}).items():
        have = (budgets or {}).get(size) or {}
        for k, v in keys.items():
            if k in have and have[k] != v:
                out.append(f"ask budget {size}.{k} is {have[k]} in {ctx.rel(ctx.cfg.path)}, approved {v}")
    return out


def _s_asks(ctx, d):
    p = ctx.parser()
    if p is None:
        raise Skip(f"no ck-run parser ({ctx._parser_err})")
    ask = _sub(p, "ask")
    if ask is None:
        return ["ck-run has no ask command (ck-run ask answered records an S ask Grok Bot answered)"]
    return [] if _sub(ask, "answered") is not None else ["ck-run ask has no answered command"]


def _fleet(ctx, d):
    m = ctx.models
    if m is None:
        return [f"{ctx.rel(ctx.models_path)} is missing or not JSON: no model aliases, tiers or lanes"]
    exp = d.get("expect") or {}
    lanes = m.get("lanes") or {}
    out = []
    missing = [n for n in exp.get("lanes") or [] if n not in lanes]
    if missing:
        out.append(f"{ctx.rel(ctx.models_path)} has no lane {', '.join(missing)}")
    aliases = set((m.get("aliases") or {}).keys())
    for name, ln in lanes.items():
        if not isinstance(ln, dict):
            continue
        for who in [ln.get("worker"), ln.get("check")] + list(ln.get("also") or []):
            if who and who not in aliases | {"self", "race"}:
                out.append(f"lane {name} names {who}, which is not a model alias")
    if exp.get("race_default") and m.get("race_default") != exp["race_default"]:
        out.append(f"race_default is {m.get('race_default')}, approved {exp['race_default']}")
    return out


CHECKS = {
    "D1": _d1, "D2": _d2, "D4": _d4, "plan-claude-edit": _plan_claude, "washu-ownership": _washu,
    "auto-mode": _auto_mode, "cursor-allow-all": _cursor_allow_all, "agy-edit": _agy_edit,
    "agy-auto-dirs": _agy_dirs, "aqs-writable": _aqs_writable, "aqs-review": _aqs_review,
    "auto-readonly": _auto_readonly, "word-only-notes": _word_only, "git-push-pip": _git_push_pip,
    "web-search": _web, "post-review-off": _post_review, "sizes-budgets": _sizes, "s-asks-self": _s_asks,
    "fleet-lanes": _fleet,
}


def drift(ctx: Ctx, decisions: list, only: list | None = None) -> tuple:
    """([{'id', 'date', 'text'}], [{'id', 'why'}]): drift lines and checks that could not run."""
    lines, skipped = [], []
    for d in decisions:
        fn = CHECKS.get(d["id"])
        if fn is None or (only is not None and d["id"] not in only):
            continue
        try:
            found = fn(ctx, d)
        except Skip as e:
            skipped.append({"id": d["id"], "why": str(e)})
            continue
        except Exception as e:  # a broken check is reported, never a crash
            skipped.append({"id": d["id"], "why": f"the check failed: {type(e).__name__}: {e}"})
            continue
        lines += [{"id": d["id"], "date": d["date"], "text": t} for t in found]
    return lines, skipped


def drift_line(x: dict) -> str:
    return f"DRIFT {x['id']} ({x['date']}): {x['text']}"


# ---- text ------------------------------------------------------------------------------------------------------

def _cut(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def rules_text(ctx: Ctx, decisions: list, lines: list, skipped: list) -> str:
    rows = agent_facts(ctx)
    props = [("edits, auto mode", "edit_auto"), ("edits, approved", "edit_approved"), ("web search/fetch", "web"),
             ("git push", "git_push"), ("pip install", "pip_install"), ("cloud sims", "cloud_sims"),
             ("post-run review", "post_run_review"), ("model, S ask", "model_s"), ("model, M/L ask", "model_ml")]
    w0 = max(len(p) for p, _ in props) + 2
    w = max(16, max(len(str(r[k])) for r in rows for _, k in props) + 2)
    L = ["WHAT EACH AGENT MAY DO NOW (read from the code and config)",
         " " * w0 + "".join(r["agent"].ljust(w) for r in rows).rstrip()]
    for label, k in props:
        L.append((label.ljust(w0) + "".join(str(r[k]).ljust(w) for r in rows)).rstrip())
    L.append("* workspace = the run's workspace minus its raw folders and rule files; repo = a ~/PhD_local folder "
             "with its own git repo.")
    for f in workspace_facts(ctx.cfg):
        L.append(f"  {f['workspace']}: {f['text']}")
    L.append("")
    # DRIFT before the decisions: /grokbot rules shows only the first ~60 lines
    if lines:
        L.append(f"DRIFT ({len(lines)}): the code or config disagrees with a decision. ck-run only reports it.")
        L += [drift_line(x) for x in lines]
    else:
        L.append("No drift: the code and config match the decisions.")
    if skipped:
        L.append("Not checked now: " + "; ".join(f"{s['id']} ({_cut(s['why'], 70)})" for s in skipped))
    L.append("")
    L.append(f"DECISIONS ({len(decisions)}; config/policy.json, full words: ck-run rules --json)")
    for d in decisions:
        words = d["words"]
        said = "" if words.startswith("UNKNOWN") else f'  "{_cut(words, 60)}"'
        L.append(f"{d['date']}  {d['id']}: {_cut(d['rule'], 80)}{said}")
    return "\n".join(L) + "\n"


def cmd_rules(cfg: Config, a) -> int:
    decisions = load()
    ctx = Ctx(cfg)
    lines, skipped = drift(ctx, decisions)
    if a.json:
        print(json.dumps({"agents": agent_facts(ctx), "workspaces": workspace_facts(cfg), "decisions": decisions,
                          "drift": lines, "skipped": skipped}, indent=2, ensure_ascii=False))
        return 1 if (a.check and lines) else 0
    if a.check:
        for x in lines:
            print(drift_line(x))
        if not lines:
            print("No drift: the code and config match the decisions.")
        for s in skipped:
            print(f"SKIPPED {s['id']}: {s['why']}")
        sys.stdout.flush()
        return 1 if lines else 0
    sys.stdout.write(rules_text(ctx, decisions, lines, skipped))
    sys.stdout.flush()
    return 0


def register(sub) -> None:
    r = sub.add_parser("rules", help="read-only: what each agent may do now, chalach's decisions, and drift")
    r.add_argument("--check", action="store_true", help="only the DRIFT lines; exit 1 when there is drift")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_rules)
