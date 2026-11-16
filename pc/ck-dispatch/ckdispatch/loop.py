"""ck-run loop: improve one number step by step (v4; the hillclimb idea from pstack: one change, one measurement).

  ck-run loop --ask A (--agent X | --model ALIAS) --prompt-file F --workspace W --metric "<shell cmd>"
              --target "<op><number>" --max N [--cwd DIR] [--title T] [--timeout S] [--tier T] [--write-scope D]...

Start it detached with `ck-run launch <slug> loop ...`. First the metric is measured once (the baseline). Then up to N
rounds. Each round runs `ck-run run ... --ask A [--parent <previous run>]` in the foreground of the loop, then the
metric command (/bin/bash -c with a clean env, no keys, in --cwd, default the workspace root; timeout 600 s), and takes the LAST number the
metric printed. Each measurement is a row in <runs_dir>/<A>/loop_decisions.tsv (ts, i, run_id, metric, kept; the
baseline is i=0, kept=baseline) and each round a `loop_iter` ledger event (note `i=<n> metric=<value> kept=<yes/no>`).
kept=yes when the metric beat the best value so far in the target's direction (>=, > : higher is better;
<=, < : lower is better). ck-run never undoes files: kept=no is a record for the next round's prompt.

The next round's prompt = F plus a short "Loop so far" block (the metric history). The loop stops at the target
(exit 0), after N rounds or 2 rounds in a row without improvement (exit 1), or when a run exits 2, 4, 5 or 7 (the loop
exits with that code). The metric must be a cheap local check: a metric that names a cloud simulation, a credential
file or a permission bypass is refused before anything runs.
"""
from __future__ import annotations

import hashlib
import operator
import os
import re
import shlex
import subprocess
import sys

from . import ledger, views
from .config import REPO, Config
from .errors import Refused
from .flags import AGENTS

CK_RUN = os.path.join(REPO, "bin", "ck-run")
OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}
TARGET_RX = re.compile(r"^\s*(>=|<=|>|<)\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$")
NUM_RX = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
STOP_EXITS = {2: "refused", 4: "flagged", 5: "violation", 7: "over budget"}
RUN_STATUS = {0: "ok", 1: "partial", 2: "refused", 3: "failed", 4: "flagged", 5: "violation", 6: "busy",
              7: "over_budget"}  # ck-run exit code -> status word (README exit table, spec section 3)
METRIC_TIMEOUT_S = 600
MAX_ROUNDS = 100
NO_GAIN_LIMIT = 2
TSV_HEAD = "ts\ti\trun_id\tmetric\tkept\n"
METRIC_BLOCKED = ("credential", "cloud_sim", "permission_bypass")  # rulecheck.PATTERNS categories


def parse_target(text: str) -> tuple:
    m = TARGET_RX.match(str(text or ""))
    if not m:
        raise Refused(f"--target {text!r}: use an operator and a number, for example \">=12.5\" (operators: >=, <=, "
                      f">, <)")
    return m.group(1), float(m.group(2))


def last_number(text: str):
    nums = NUM_RX.findall(text or "")
    if not nums:
        return None
    try:
        return float(nums[-1])
    except ValueError:
        return None


def fmt(v) -> str:
    return "none" if v is None else format(v, "g")


def better(op: str, new: float, old: float) -> bool:
    return new > old if op in (">=", ">") else new < old


def metric_refusal(metric: str) -> str | None:
    from . import rulecheck
    for cat, rx in rulecheck.PATTERNS:
        if cat in METRIC_BLOCKED:
            m = rx.search(metric)
            if m:
                return f"the metric names a {cat.replace('_', ' ')} ({m.group(0).strip()!r}); a loop metric must " \
                       f"be a cheap local check"
    return None


def run_once(argv: list) -> tuple:
    """One `ck-run run ...` in the foreground. Returns (exit code, run id or None, its stdout)."""
    from .patterns import summary_run_id
    p = subprocess.run([sys.executable, "-I", CK_RUN, *argv], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=None, text=True)
    out = p.stdout or ""
    return p.returncode, summary_run_id(out), out


def metric_env(cfg: Config) -> dict:
    """The metric's environment: the agents' clean allowlist (no keys, no tokens) with the caller's PATH, so the
    metric finds the same python3 as before (chalach 2026-11-06: no leak of personal information)."""
    from .guards import child_env
    env = dict(child_env(cfg))
    env["PATH"] = os.environ.get("PATH", env.get("PATH", "/usr/bin:/bin"))
    return env


INTERPRETERS = {"python", "python3", "bash", "sh", "zsh", "node", "ruby", "perl", "julia", "Rscript"}


def _sha(path: str):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def metric_scripts(metric: str, cwd: str) -> dict:
    """The code files the metric runs (each command's program, or the script given to an interpreter), with their
    sha256. Data files it only reads (cat value.txt) are not watched. A round that changes one stops the loop."""
    out = {}
    for seg in re.split(r"\|\||&&|[;|&\n]", metric):
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if not words:
            continue
        cand = [words[0]]
        if os.path.basename(words[0]) in INTERPRETERS:
            cand = [w for w in words[1:] if not w.startswith("-")][:1]
        for w in cand:
            path = os.path.expanduser(w if os.path.isabs(w) or w.startswith("~") else os.path.join(cwd, w))
            if os.path.isfile(path):
                h = _sha(path)
                if h:
                    out[os.path.realpath(path)] = h
    return out


def measure(metric: str, cwd: str, timeout_s: float = METRIC_TIMEOUT_S, env: dict | None = None) -> dict:
    """Run the metric command; value = the last number in its stdout (None if none). run_loop passes a clean env."""
    try:
        p = subprocess.run(["/bin/bash", "-c", metric], cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"value": None, "exit": None, "note": f"the metric timed out after {timeout_s:g} s"}
    except OSError as e:
        return {"value": None, "exit": None, "note": f"the metric could not start: {e}"}
    v = last_number(p.stdout)
    notes = []
    if p.returncode != 0:
        notes.append(f"metric exit {p.returncode}")
    if v is None:
        notes.append("no number in the metric output")
    return {"value": v, "exit": p.returncode, "note": "; ".join(notes)}


def loop_dir(cfg: Config, ask_id: str) -> str:
    return os.path.join(cfg.runs_dir, ask_id)


def _row(path: str, ts: str, i: int, run_id: str | None, value, kept: str) -> None:
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write(TSV_HEAD)
        f.write(f"{ts}\t{i}\t{run_id or '-'}\t{'' if value is None else fmt(value)}\t{kept}\n")


def so_far_block(spec: dict, history: list, i: int) -> str:
    L = ["", "---", f"Loop so far (ck-run loop, ask {spec['ask_id']}; this is round {i} of {spec['max']}; target: "
                    f"metric {spec['op']} {fmt(spec['target'])}; the metric is the last number printed by: "
                    f"{spec['metric']})"]
    best = None
    for h in history:
        if h["i"] == 0:
            L.append(f"- baseline (before any change): {fmt(h['value'])}")
        else:
            word = "kept (better than before)" if h["kept"] == "yes" else "not kept (no better than the best)"
            L.append(f"- round {h['i']} (run {h['run_id'] or 'none'}): {fmt(h['value'])}, {word}"
                     + (f"; {h['note']}" if h.get("note") else ""))
        if h["value"] is not None and (best is None or better(spec["op"], h["value"], best[0])):
            best = (h["value"], h["i"])
    if best:
        L.append(f"Best so far: {fmt(best[0])} ({'baseline' if best[1] == 0 else 'round ' + str(best[1])}).")
    return "\n".join(L) + "\n"


def run_loop(cfg: Config, spec: dict, *, run=None, metric=None, out=None) -> int:
    """spec: ask_id, who (['--agent', X] or ['--model', A]), prompt_text, workspace, metric, op, target, max, cwd,
    title, extra (more run flags). run / metric default to run_once / measure. Returns the loop's exit code."""
    run = run or run_once
    if metric is None:
        env = metric_env(cfg)
        metric = lambda m, c: measure(m, c, env=env)  # noqa: E731
    out = out or sys.stdout

    def say(text: str) -> None:
        out.write(text + "\n")
        out.flush()

    d = loop_dir(cfg, spec["ask_id"])
    os.makedirs(d, mode=0o700, exist_ok=True)
    tsv = os.path.join(d, "loop_decisions.tsv")
    op, target = spec["op"], spec["target"]
    hit = OPS[op]
    history = []
    base = metric(spec["metric"], spec["cwd"])
    _row(tsv, ledger.now_iso(), 0, None, base["value"], "baseline")
    history.append({"i": 0, "run_id": None, "value": base["value"], "kept": "baseline", "note": base["note"]})
    best = base["value"]
    watched = metric_scripts(spec["metric"], spec["cwd"])
    say(f"loop {spec['ask_id']}: baseline metric {fmt(best)} (target {op} {fmt(target)}; at most {spec['max']} "
        f"round(s))" + (f" [{base['note']}]" if base["note"] else ""))
    if best is not None and hit(best, target):
        say(f"loop {spec['ask_id']} stopped: the target is already met before any change. Best {fmt(best)}.")
        return 0
    prev, no_gain, reason, code = None, 0, None, 1
    for i in range(1, spec["max"] + 1):
        # the pid keeps two loops of one ask from overwriting each other's prompts
        prompt_path = os.path.join(d, f"loop_prompt_{i}_{os.getpid()}.md")
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(spec["prompt_text"].rstrip("\n") + "\n" + so_far_block(spec, history, i))
        argv = (["run"] + spec["who"] + ["--workspace", spec["workspace"],
                                         "--title", f"{spec['title']} (loop round {i}/{spec['max']})",
                                         "--prompt-file", prompt_path, "--ask", spec["ask_id"]]
                + (["--parent", prev] if prev else []) + list(spec.get("extra") or []))
        say(f"loop {spec['ask_id']}: round {i}/{spec['max']}: ck-run run" + (f" --parent {prev}" if prev else ""))
        rc, rid, text = run(argv)
        if text:
            out.write(text if text.endswith("\n") else text + "\n")
            out.flush()
        refused = rc in (2, 7) and rid is None  # nothing ran: nothing to measure
        changed = [] if refused else [p for p, h in watched.items() if _sha(p) != h]
        if refused:
            m = {"value": None, "exit": None, "note": f"no run (exit {rc})"}
        elif changed:  # never run agent-edited code outside the sandbox
            m = {"value": None, "exit": None,
                 "note": "the metric script changed in this round: " + ", ".join(os.path.basename(p) for p in changed)}
        else:
            m = metric(spec["metric"], spec["cwd"])
        v = m["value"]
        kept = v is not None and (best is None or better(op, v, best))
        if kept:
            best, no_gain = v, 0
        else:
            no_gain += 1
        ts = ledger.now_iso()
        _row(tsv, ts, i, rid, v, "yes" if kept else "no")
        views.ledger_append(cfg.runs_dir, event="loop_iter", ask_id=spec["ask_id"], run_id=rid,
                            title=spec["title"], workspace=spec["workspace"], status=RUN_STATUS.get(rc, "failed"),
                            exit_code=rc, pattern="loop", reason=m["note"] or None,
                            note=f"i={i} metric={fmt(v)} kept={'yes' if kept else 'no'}")
        history.append({"i": i, "run_id": rid, "value": v, "kept": "yes" if kept else "no", "note": m["note"]})
        say(f"loop {spec['ask_id']}: round {i}: run exit {rc}, metric {fmt(v)}, kept {'yes' if kept else 'no'}"
            + (f" [{m['note']}]" if m["note"] else ""))
        prev = rid or prev
        if changed:
            reason, code = ("the metric script changed during this round (an agent edited it), so ck-run did not run "
                            "it outside the sandbox. Check the script, then start a new loop"), 4
            break
        if rc in STOP_EXITS:
            reason, code = f"the run exited {rc} ({STOP_EXITS[rc]})", rc
            break
        if v is not None and hit(v, target):
            reason, code = f"the target {op} {fmt(target)} is met", 0
            break
        if no_gain >= NO_GAIN_LIMIT:
            reason = f"{NO_GAIN_LIMIT} rounds in a row without improvement"
            break
    else:
        reason = f"the last round ({spec['max']}) is done"
    say(f"loop {spec['ask_id']} stopped: {reason}. Best {fmt(best)} (target {op} {fmt(target)}). "
        f"Rows: {tsv}")
    return code


def cmd_loop(cfg: Config, a) -> int:
    from .patterns import models_config, need_open_ask
    from .runner import read_task_file
    ask = need_open_ask(cfg, a.ask)
    ws = cfg.workspace(a.workspace)
    src = os.path.abspath(os.path.expanduser(a.prompt_file))
    text = read_task_file(src)
    op, target = parse_target(a.target)
    if not 1 <= a.max <= MAX_ROUNDS:
        raise Refused(f"--max must be 1 to {MAX_ROUNDS}")
    metric = (a.metric or "").strip()
    if not metric:
        raise Refused("--metric is empty: give a shell command that prints the number to improve")
    why = metric_refusal(metric)
    if why:
        raise Refused(why)
    cwd = os.path.abspath(os.path.expanduser(a.cwd)) if a.cwd else ws.root
    if not os.path.isdir(cwd):
        raise Refused(f"--cwd is not a folder: {a.cwd}")
    if a.agent:
        if a.agent not in AGENTS:
            raise Refused(f"--agent must be one of: {', '.join(AGENTS)}")
        who = ["--agent", a.agent]
    else:
        mc = models_config()
        known = (mc or {}).get("aliases") or {}
        if mc is not None and a.model not in known:
            raise Refused(f"--model {a.model}: not a model alias ({', '.join(known)}); or use --agent")
        agent = (known.get(a.model) or {}).get("agent")
        who = ["--model", a.model] + (["--agent", agent] if agent in AGENTS else [])
    extra = []
    if a.timeout:
        extra += ["--timeout", str(a.timeout)]
    if a.tier:
        extra += ["--tier", a.tier]
    for s in a.write_scope or []:
        extra += ["--write-scope", s]
    title = " ".join(str(a.title or "design loop").split())[:150]
    spec = {"ask_id": ask["ask_id"], "who": who, "prompt_text": text, "workspace": ws.key, "metric": metric,
            "op": op, "target": target, "max": a.max, "cwd": cwd, "title": title, "extra": extra}
    try:
        return run_loop(cfg, spec)
    except KeyboardInterrupt:
        print(f"loop {ask['ask_id']} stopped: interrupted", flush=True)
        return 3


def register(sub) -> None:
    p = sub.add_parser("loop", help="improve one number step by step: a run, a metric, keep or not, until a target "
                                    "(start it with ck-run launch <slug> loop ...)")
    p.add_argument("--ask", required=True, help="the ask id (A-MMDD-N); every round is a run of this ask")
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--agent", choices=AGENTS)
    who.add_argument("--model", help="a model alias (sol-fast, composer, ...; see ck-run models)")
    p.add_argument("--prompt-file", required=True, help="the task (F); each round gets F plus 'Loop so far'")
    p.add_argument("--workspace", required=True, help="devfab | nv | washu")
    p.add_argument("--metric", required=True, help="shell command; its LAST printed number is the metric")
    p.add_argument("--target", required=True, help='an operator and a number, e.g. ">=12.5" (>=, <=, >, <)')
    p.add_argument("--max", required=True, type=int, help=f"at most this many rounds (1-{MAX_ROUNDS})")
    p.add_argument("--cwd", help="folder the metric runs in (default: the workspace root)")
    p.add_argument("--title", help="short title for the rounds (default: design loop)")
    p.add_argument("--timeout", type=int, help="seconds per round's run (default: from the ask size)")
    p.add_argument("--tier", choices=["fast", "strong", "top"], help="passed to each round's ck-run run")
    p.add_argument("--write-scope", action="append", default=[], help="passed to each round's ck-run run "
                                                                       "(repeatable)")
    p.set_defaults(func=cmd_loop)
