"""Interaction ledger (agent_runs/ledger.jsonl) and the `ck-run thread` markdown view.

One JSON line per event. Every line has the same keys:
  event          dispatch | review | result | refused | busy | accept
                 | busy                (since 2026-11-03: a claim (--claim, or an edit run's --write-scope folder,
                                        claimed automatically) overlapped a live run's claim; nothing started.
                                        There is no workspace lock: runs without overlapping claims always start)
                 | stale               (a ck-run that crashed or was killed: its run record and claims were
                                        released by a later run or `ck-run active`; status failed)
                 | live_window_opened  (--live: the window ck-run opened; note = window_id=<id>; terminal; profile;
                                        whether the title is locked; the title)
                 | live_fallback       (--live: the window did not run the agent; note = the reason; the run
                                        continues headless)
                 | cursor_review | agy_review  (mandatory read-only Claude check after a cursor/agy auto run;
                                        from claude to CK via CK; status ok | flagged; response_path = review.md)
  run_id, parent_run_id
  from, to       "CK" or an agent name (a review goes from the reviewed agent to the reviewer, via CK;
                 the thread view labels it "CK → <reviewer> · review of <agent>'s run")
  title, workspace, task_type, agent, model, mode, approved, write_scope
  claims         paths the run claimed: --claim, plus an edit run's --write-scope folders (real paths; [] = none)
  prompt_path, response_path, summary_path
  files_read, files_changed
  status         dispatched | ok | partial | failed | timeout | flagged | violation | refused | busy | fallback
  accepted       null until `ck-run accept`, then true/false
  ts, started_at, finished_at, duration_s, exit_code, note
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime

KEYS = ["event", "run_id", "parent_run_id", "from", "to", "via", "title", "workspace", "task_type", "agent",
        "model", "mode", "approved", "write_scope", "claims", "prompt_path", "response_path", "summary_path",
        "files_read", "files_changed", "status", "accepted", "ts", "started_at", "finished_at",
        "duration_s", "exit_code", "note"]
LIST_CAP = 200


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def path(runs_dir: str) -> str:
    return os.path.join(runs_dir, "ledger.jsonl")


def append(runs_dir: str, **fields) -> dict:
    ev = {k: fields.get(k) for k in KEYS}
    ev["ts"] = ev["ts"] or now_iso()
    for k in ("files_read", "files_changed", "write_scope", "claims"):
        v = ev.get(k)
        if v is None:
            ev[k] = []
        elif len(v) > LIST_CAP:
            ev[k] = list(v[:LIST_CAP]) + [f"... +{len(v) - LIST_CAP} more (see result.json)"]
    os.makedirs(runs_dir, mode=0o700, exist_ok=True)
    line = (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path(runs_dir), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return ev


def load(runs_dir: str) -> list:
    out = []
    try:
        with open(path(runs_dir), encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)  # appends take LOCK_EX: never read half a line
            for line in f:
                try:
                    ev = json.loads(line)
                    if isinstance(ev, dict) and ev.get("run_id"):
                        out.append(ev)
                except ValueError:
                    continue
    except OSError:
        pass
    return out


class Run:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.events = []

    def first(self, *names):
        return next((e for e in self.events if e.get("event") in names), None)

    def last(self, *names):
        return next((e for e in reversed(self.events) if e.get("event") in names), None)

    @property
    def opening(self):
        return self.first("dispatch", "review", "refused", "busy") or self.events[0]

    @property
    def parent(self):
        return next((e.get("parent_run_id") for e in self.events if e.get("parent_run_id")), None)

    @property
    def ts(self):
        return self.events[0].get("ts") or ""


def runs(events: list) -> dict:
    out = {}
    for ev in events:
        out.setdefault(ev["run_id"], Run(ev["run_id"])).events.append(ev)
    return out


def find_run(runs_dir: str, run_id: str):
    return runs(load(runs_dir)).get(run_id)


def _hm(ts):
    return (ts or "?").replace("T", " ")[:19]


def _files(label, items, cap=8):
    items = [i for i in (items or []) if i]
    if not items:
        return f"{label}: none"
    shown = ", ".join(f"`{i}`" for i in items[:cap])
    more = f" (+{len(items) - cap} more)" if len(items) > cap else ""
    return f"{label} ({len(items)}): {shown}{more}"


def _summary_lines(p):
    if not p:
        return []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[:10]]
    except OSError:
        return []


def render_thread(events: list, run_id: str, with_summary: bool = True) -> str:
    rs = runs(events)
    if run_id not in rs:
        raise KeyError(run_id)
    root, seen = run_id, {run_id}
    while True:
        p = rs[root].parent
        if not p or p not in rs or p in seen:
            break
        seen.add(p)
        root = p
    children = {}
    for rid, r in rs.items():
        if r.parent and r.parent in rs and rid != root:
            children.setdefault(r.parent, []).append(rid)
    for v in children.values():
        v.sort(key=lambda rid: rs[rid].ts)

    count = [0]
    L = []

    def emit(rid, num, depth, stack):
        count[0] += 1
        r = rs[rid]
        op, res, acc = r.opening, r.last("result"), r.last("accept")
        kind = op.get("event")
        to = op.get("to") or op.get("agent") or "?"
        if op.get("via"):
            # A review is stored as reviewed agent → reviewer, via CK. CK dispatched it, so label it that way.
            label = f"{op['via']} → {to} · review of {op.get('from') or '?'}'s run"
            if kind != "review":
                label += f" · {kind}"
        else:
            label = f"{op.get('from') or 'CK'} → {to} · {kind}"
        mark = "  ← requested" if rid == run_id else ""
        L.append(f"{'#' * min(2 + depth, 6)} {num}  {label} · {op.get('title') or ''}{mark}")
        L.append(f"- run `{rid}` · {op.get('workspace') or '?'} · mode {op.get('mode') or '?'}"
                 + (f" · task {op.get('task_type')}" if op.get("task_type") else "")
                 + (f" · model {op.get('model')}" if op.get("model") else ""))
        if op.get("approved"):
            L.append(f"- edit approved: \"{op['approved']}\" · scope: " + ", ".join(f"`{s}`" for s in op.get("write_scope") or []))
        if op.get("claims"):
            L.append("- " + _files("claims", op.get("claims")))
        prompt_path = (res or {}).get("prompt_path") or op.get("prompt_path")  # the one used (live or headless)
        if prompt_path:
            L.append(f"- prompt: `{prompt_path}`")
        if res:
            L.append(f"- {res.get('from') or '?'} → {res.get('to') or 'CK'}: **{res.get('status')}** · "
                     f"{_hm(res.get('started_at'))} → {_hm(res.get('finished_at'))} ({res.get('duration_s')} s)")
            L.append(f"- response: `{res.get('response_path')}` · summary: `{res.get('summary_path')}`")
            L.append("- " + _files("files read", res.get("files_read")))
            L.append("- " + _files("files changed", res.get("files_changed")))
            if res.get("note"):
                L.append(f"- note: {res['note']}")
        elif kind in ("refused", "busy"):
            L.append(f"- **{op.get('status')}** at {_hm(op.get('ts'))}: {op.get('note') or ''}")
        else:
            L.append(f"- status: {op.get('status')} since {_hm(op.get('ts'))} (no result yet)")
        for ev in r.events:
            name = ev.get("event") or ""
            if name == "live_window_opened":
                L.append(f"- live window: {ev.get('note') or ''}")
            elif name == "live_fallback":
                L.append(f"- **live fallback**: {ev.get('note') or ''}")
            elif name == "stale":
                L.append(f"- **stale** at {_hm(ev.get('ts'))}: {ev.get('note') or ''}")
            elif name.endswith("_review"):  # cursor_review / agy_review: the mandatory post-run Claude check
                L.append(f"- {name.replace('_', ' ')}: **{ev.get('status')}** · `{ev.get('response_path')}` · "
                         f"{(ev.get('note') or '')[:300]}")
        if acc:
            word = "yes" if acc.get("accepted") else "no"
            L.append(f"- accepted: **{word}** by {acc.get('from')} at {_hm(acc.get('ts'))}"
                     + (f" — {acc['note']}" if acc.get("note") else ""))
        else:
            L.append("- accepted: not decided")
        if with_summary and res:
            lines = _summary_lines(res.get("summary_path"))
            if lines:
                L.append("")
                L.extend(f"> {ln}" if ln else ">" for ln in lines)
        L.append("")
        for i, c in enumerate(children.get(rid, []), 1):
            if c not in stack:
                emit(c, f"{num}.{i}", depth + 1, stack | {c})

    body_start = len(L)
    emit(root, "1", 0, {root})
    head = [f"# Thread {root}", "",
            f"_{count[0]} run(s) · first {_hm(rs[root].ts)} · ledger order; reviews and follow-ups are nested_", ""]
    return "\n".join(head + L[body_start:]).rstrip() + "\n"
