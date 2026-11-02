"""One run: guards -> run dir -> agent with timeout -> parse -> rulecheck -> ledger -> summary on stdout."""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime

from . import VERSION, ledger, route, rulecheck, summarize
from . import prompt as P
from .config import CLAUDE_GUARD_BASE, PACKET_SCHEMA, REPO, RESULT_SCHEMA, Config, Workspace, inside
from .errors import Refused
from .flags import AGENTS, EDIT_AREAS, SCHEMA_ENFORCED_BY_FLAG, build, redacted
from .guards import child_env, validate_write_scopes
from .schema import load as load_schema
from .schema import validate

RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}-[a-z0-9]+-[a-z0-9_]+-[a-z]+-[0-9a-f]{4}$")
TASK_TYPE_RE = re.compile(r"^[a-z0-9_]{1,32}$")
EXIT = {"ok": 0, "partial": 1, "refused": 2, "failed": 3, "timeout": 3, "flagged": 4, "violation": 5, "busy": 6}
MAX_TASK_BYTES = 200_000


class Spec:
    def __init__(self, **kw):
        self.agent = kw["agent"]
        self.ws: Workspace = kw["ws"]
        self.title = kw["title"]
        self.task_text = kw["task_text"]
        self.task_source = kw.get("task_source")
        self.task_type = kw.get("task_type") or "general"
        self.model = kw.get("model")
        self.mode = kw.get("mode") or "read"
        self.approved = kw.get("approved")
        self.scopes_in = list(kw.get("scopes_in") or [])
        self.read_dirs = list(kw.get("read_dirs") or [])
        self.timeout_s = kw.get("timeout_s")
        self.parent_run_id = kw.get("parent_run_id")
        self.kind = kw.get("kind") or "dispatch"
        self.from_ = kw.get("from_") or "CK"
        self.review = kw.get("review")  # dict for reviews
        self.lead = kw.get("lead")
        self.run_id = ""


def new_run_id(ws_key: str, task_type: str, agent: str) -> str:
    return f"{datetime.now():%Y-%m-%dT%H%M%S}-{ws_key}-{task_type}-{agent}-{secrets.token_hex(2)}"


def valid_run_id(rid: str) -> bool:
    return bool(RUN_ID_RE.match(rid or ""))


def read_task_file(path: str) -> str:
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        raise Refused(f"prompt file not found: {path}")
    if os.path.getsize(p) > MAX_TASK_BYTES:
        raise Refused(f"prompt file is over {MAX_TASK_BYTES} bytes; put the bulk in a file and name its path")
    with open(p, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if not text.strip():
        raise Refused("prompt file is empty")
    return text


class SlotLock:
    """At most N runs per workspace; an edit run takes every slot (so nothing else changes files meanwhile)."""

    def __init__(self, cfg: Config, ws_key: str, exclusive: bool):
        self.dir = os.path.join(cfg.runs_dir, ".locks")
        self.paths = [os.path.join(self.dir, f"{ws_key}.slot{i}.lock") for i in range(max(1, cfg.max_runs_per_workspace))]
        self.exclusive = exclusive
        self.fds = []

    def acquire(self) -> bool:
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        for p in self.paths:
            fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                if self.exclusive:
                    self.release()
                    return False
                continue
            self.fds.append(fd)
            if not self.exclusive:
                return True
        return bool(self.fds) and (not self.exclusive or len(self.fds) == len(self.paths))

    def release(self) -> None:
        for fd in self.fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self.fds = []


def write_claude_settings(cfg: Config, path: str) -> None:
    with open(CLAUDE_GUARD_BASE, encoding="utf-8") as f:
        base = json.load(f)
    deny = list(base["permissions"]["deny"])
    targets = []
    for ws in cfg.workspaces.values():
        targets += [ws.root, ws.real_root]
        targets += [e.real for e in ws.protected_dirs if not inside(e.real, ws.real_root)]
    targets += [os.path.realpath(REPO), cfg.runs_dir, os.path.realpath(cfg.runs_dir)]
    for t in dict.fromkeys(targets):
        deny.append(f"Edit(/{t}/**)")  # '//abs/path' = absolute path in Claude permission rules
    _write(path, json.dumps({"permissions": {"deny": deny}}, indent=1))


def _write(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _kill_group(proc) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt()


def run_child(cmd, prompt_text: str, env: dict, run_dir: str, timeout_s: int):
    argv = cmd.final_argv(prompt_text)
    timed_out = interrupted = False
    old = signal.signal(signal.SIGTERM, _on_sigterm)
    with open(os.path.join(run_dir, "stdout.log"), "wb") as out, open(os.path.join(run_dir, "stderr.log"), "wb") as err:
        proc = subprocess.Popen(argv, cwd=cmd.cwd, env=env,
                                stdin=subprocess.PIPE if cmd.stdin_prompt else subprocess.DEVNULL,
                                stdout=out, stderr=err, start_new_session=True, close_fds=True)
        try:
            proc.communicate(input=prompt_text.encode("utf-8") if cmd.stdin_prompt else None, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
        except KeyboardInterrupt:
            interrupted = True
            _kill_group(proc)
        finally:
            signal.signal(signal.SIGTERM, old)
        try:  # stray background children left in the agent's process group
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    return proc.returncode, timed_out, interrupted


def _norm(p: str, root: str) -> str:
    p = os.path.expanduser(str(p).strip().strip("`"))
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(root, p))


def _includes(files: list, target: str) -> bool:
    t, tr = os.path.normpath(target), os.path.realpath(target)
    for f in files:
        if f == t or f == tr or os.path.realpath(f) == tr:
            return True
    return False


def _read_text(path: str, cap: int = 5_000_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(cap)
    except OSError:
        return ""


def _validate_read_dirs(cfg: Config, dirs: list) -> list:
    out = []
    for d in dirs:
        p = os.path.realpath(os.path.expanduser(d))
        if not os.path.isdir(p):
            raise Refused(f"--read-dir is not an existing folder: {d}")
        ok_roots = [cfg.real_local_root, os.path.realpath(cfg.runs_dir)] + [w.real_root for w in cfg.workspaces.values()]
        if inside(cfg.real_home, p) or not any(inside(p, r) for r in ok_roots):
            raise Refused(f"--read-dir must be inside ~/PhD_local or a workspace: {d}")
        out.append(p)
    return list(dict.fromkeys(out))


def execute(cfg: Config, spec: Spec, dry_run: bool = False) -> int:
    ws = spec.ws
    spec.run_id = new_run_id(ws.key, spec.task_type, spec.agent)
    run_dir = os.path.join(cfg.runs_dir, spec.run_id)
    paths = {k: os.path.join(run_dir, v) for k, v in {
        "prompt": "prompt.md", "task": "task.md", "result_md": "result.md", "result_json": "result.json",
        "summary": "summary.md", "settings": "claude-settings.json", "packet": "packet.json",
        "cmd": "cmd.txt", "rulecheck": "rulecheck.json", "marker": ".marker"}.items()}
    base = {"run_id": spec.run_id, "parent_run_id": spec.parent_run_id, "title": spec.title,
            "workspace": ws.key, "task_type": spec.task_type, "agent": spec.agent, "model": spec.model,
            "mode": spec.mode, "approved": spec.approved, "via": "CK" if spec.kind == "review" else None}
    try:
        if spec.agent not in AGENTS:
            raise Refused(f"unknown agent '{spec.agent}'")
        if not TASK_TYPE_RE.match(spec.task_type):
            raise Refused("task type must match [a-z0-9_]{1,32}")
        if spec.timeout_s is None:
            spec.timeout_s = cfg.default_timeout_s
        if not (1 <= int(spec.timeout_s) <= cfg.max_timeout_s):
            raise Refused(f"timeout must be 1..{cfg.max_timeout_s} s")
        if spec.mode == "edit":
            if spec.kind == "review":
                raise Refused("reviews are read-only")
            if not (spec.approved or "").strip():
                raise Refused('edit mode needs --approved "<the user\'s words and time>"')
            if spec.read_dirs:
                raise Refused("--read-dir is only for read mode")
            scopes = validate_write_scopes(cfg, ws, spec.agent, spec.scopes_in, EDIT_AREAS)
        elif spec.mode == "read":
            if spec.scopes_in:
                raise Refused("--write-scope is only for --mode edit")
            scopes = []
        else:
            raise Refused(f"forbidden mode '{spec.mode}'")
        read_dirs = _validate_read_dirs(cfg, spec.read_dirs)
        cmd = build(cfg, ws, spec.agent, mode=spec.mode, scopes=scopes, model=spec.model, run_dir=run_dir,
                    schema_path=RESULT_SCHEMA, extra_read_dirs=read_dirs, timeout_s=int(spec.timeout_s),
                    claude_settings_path=paths["settings"])
    except Refused as e:
        if not dry_run:
            ledger.append(cfg.runs_dir, **base, event="refused", status="refused", note=str(e),
                          **{"from": spec.from_, "to": spec.agent})
        print(f"ck-run: REFUSED: {e}", file=sys.stderr)
        return EXIT["refused"]

    head = P.header(ws=ws, run_id=spec.run_id, agent=spec.agent, kind=spec.kind, mode=spec.mode,
                    approved=spec.approved, scopes=scopes)
    foot = P.footer(run_id=spec.run_id, include_schema=not SCHEMA_ENFORCED_BY_FLAG[spec.agent], schema_path=RESULT_SCHEMA)
    prompt_text = P.assemble(head, spec.title, spec.task_text, foot)
    env = child_env(cfg)
    cmd_txt = "\n".join([
        f"# ck-run {VERSION} · run {spec.run_id}",
        f"# binary: {cmd.binary}",
        f"# cwd: {cmd.cwd}",
        f"# env: only {', '.join(sorted(env))}; PATH={env['PATH']}",
        f"# prompt: {paths['prompt']} ({'stdin' if cmd.stdin_prompt else 'last argument'})",
        f"# config: {cfg.path}",
        redacted(cmd, RESULT_SCHEMA, paths["prompt"]), ""])
    if dry_run:
        sys.stdout.write(cmd_txt)
        return 0

    lock = SlotLock(cfg, ws.key, exclusive=spec.mode == "edit")
    if not lock.acquire():
        note = f"{ws.name} already has {cfg.max_runs_per_workspace} run(s), or an edit run, in progress"
        ledger.append(cfg.runs_dir, **base, event="busy", status="busy", note=note, **{"from": spec.from_, "to": spec.agent})
        print(f"ck-run: BUSY: {note}; try again later", file=sys.stderr)
        return EXIT["busy"]
    try:
        return _run_locked(cfg, spec, ws, scopes, cmd, prompt_text, env, cmd_txt, run_dir, paths, base)
    finally:
        lock.release()


def _run_locked(cfg, spec, ws, scopes, cmd, prompt_text, env, cmd_txt, run_dir, paths, base) -> int:
    old_umask = os.umask(0o077)
    try:
        os.makedirs(cfg.runs_dir, mode=0o700, exist_ok=True)
        os.mkdir(run_dir, 0o700)
    finally:
        os.umask(old_umask)
    started = ledger.now_iso()
    _write(paths["task"], spec.task_text)
    _write(paths["prompt"], prompt_text)
    _write(paths["cmd"], cmd_txt)
    if spec.agent == "claude":
        write_claude_settings(cfg, paths["settings"])
    packet = {
        "run_id": spec.run_id, "kind": spec.kind, "parent_run_id": spec.parent_run_id, "from": spec.from_,
        "workspace": ws.key, "workspace_root": ws.root, "lead": spec.lead, "task_type": spec.task_type,
        "title": spec.title, "worker": spec.agent, "model": spec.model, "cwd": cmd.cwd,
        "mode": "plan" if spec.mode == "read" else "edit-approved",
        "approved_actions": [spec.approved] if spec.mode == "edit" else [],
        "write_scope": [s["path"] for s in scopes], "deliverable": "result.md + result.json + summary.md",
        "budget": {"timeout_s": int(spec.timeout_s)}, "review": spec.review, "task_source": spec.task_source,
        "created_at": started, "config_path": cfg.path, "ck_dispatch_version": VERSION}
    perr = validate(packet, load_schema(PACKET_SCHEMA))
    if perr:
        raise Refused(f"internal: packet does not match its schema: {perr[:3]}")
    _write(paths["packet"], json.dumps(packet, indent=2, ensure_ascii=False))
    ledger.append(cfg.runs_dir, **base, event=spec.kind, status="dispatched",
                  prompt_path=paths["prompt"], response_path=paths["result_md"], summary_path=paths["summary"],
                  write_scope=packet["write_scope"], started_at=started,
                  **{"from": spec.from_, "to": spec.agent})

    marker_ns = rulecheck.touch_marker(paths["marker"])
    t0 = time.time()
    exit_code, timed_out, interrupted = run_child(cmd, prompt_text, env, run_dir, int(spec.timeout_s))
    duration = round(time.time() - t0, 1)
    finished = ledger.now_iso()

    parsed = summarize.parse(spec.agent, run_dir)
    stdout = _read_text(os.path.join(run_dir, "stdout.log"))
    stderr = _read_text(os.path.join(run_dir, "stderr.log"), 200_000)
    scan = rulecheck.scan_changed(rulecheck.scan_roots(ws, scopes), marker_ns, cfg.scan_budget_s, cfg.scan_max_entries)
    cls = rulecheck.classify(cfg, ws, spec.mode, scopes, scan["changed"], run_dir)
    tr = rulecheck.scan_transcript(parsed, stdout, stderr)
    rc = {"violations": cls["violations"] + tr["violations"], "flags": cls["flags"] + tr["flags"],
          "notes": cls["notes"], "changed_in_scope": cls["changed_in_scope"],
          "scan": {"roots": rulecheck.scan_roots(ws, scopes), "entries": scan["entries"],
                   "truncated": scan["truncated"], "seconds": scan["seconds"]}}
    rc["clean"] = not rc["violations"] and not rc["flags"]

    structured = parsed["structured"] if isinstance(parsed["structured"], dict) else None
    schema_errors = validate(structured, load_schema(RESULT_SCHEMA)) if structured else ["no result JSON found"]
    lines, src, sum_note = summarize.summary_lines(structured, parsed["final_text"], paths["result_md"])
    notes = [n for n in [sum_note] if n]
    reported = [_norm(f, ws.root) for f in (structured or {}).get("files_read") or [] if isinstance(f, str)]
    observed = [_norm(f, ws.root) for f in parsed["observed_reads"]]
    files_read = list(dict.fromkeys(reported + observed))
    if spec.kind == "review" and not (spec.review or {}).get("blind"):
        targets = [spec.review["result_md"], spec.review["result_json"]]
        read_ok = any(_includes(files_read, t) for t in targets)
        if not read_ok:
            notes.append("the reviewer did not list the result file in files_read")
    else:
        missing = [p for p in ws.wiki_first if not _includes(files_read, p)]
        read_ok = not missing
        if missing:
            notes.append("wiki-first not shown: files_read lacks " + ", ".join(os.path.relpath(m, ws.root) for m in missing))
    numbers = summarize.check_numbers((structured or {}).get("numbers"), ws.root)
    agent_status = (structured or {}).get("status")

    problems = []
    if structured is None:
        problems.append("no result JSON")
    elif schema_errors:
        problems.append(f"result JSON does not match the schema ({len(schema_errors)} problem(s))")
    if src == "fallback":
        problems.append("no agent summary")
    if agent_status in ("partial", "blocked"):
        problems.append(f"agent status {agent_status}")
    if not read_ok:
        problems.append("required reads not shown")
    if scan["truncated"]:
        problems.append("rulecheck scan truncated")
    if exit_code not in (0, None) and not timed_out:
        problems.append(f"exit code {exit_code}")
    if parsed["agent_error"]:
        problems.append(f"agent error: {parsed['agent_error'][:160]}")
    if parsed.get("finished_reason") == "print_timeout":
        problems.append("agy print timeout: the response is partial")

    if rc["violations"]:
        status = "violation"
    elif rc["flags"]:
        status = "flagged"
    elif timed_out:
        status = "timeout"
    elif interrupted or agent_status == "failed" or (structured is None and (exit_code != 0 or parsed["agent_error"])):
        status = "failed"
    elif problems:
        status = "partial"
    else:
        status = "ok"
    notes += problems

    if status in ("failed", "partial", "timeout") and (exit_code != 0 or parsed["agent_error"]):
        msg = (stderr[-4000:] + "\n" + (parsed["agent_error"] or ""))
        if route.RATE_LIMIT_RX.search(msg):
            lim = route.mark_limited(cfg.runs_dir, spec.agent, msg)
            notes.append(f"{spec.agent} looks rate-limited until {lim['until']} (saved in state.json)")

    rec = {
        "run_id": spec.run_id, "kind": spec.kind, "parent_run_id": spec.parent_run_id, "title": spec.title,
        "agent": spec.agent, "model": spec.model, "workspace": ws.key, "mode": spec.mode,
        "status": status, "agent_status": agent_status, "exit_code": exit_code, "timed_out": timed_out,
        "summary_lines": lines, "summary_source": src, "notes": notes, "schema_errors": schema_errors[:20],
        "agent_result": structured, "final_text": None if structured else parsed["final_text"][:20000],
        "files_read": files_read, "files_read_observed": observed,
        "files_changed": rc["changed_in_scope"],
        "files_changed_reported": (structured or {}).get("files_changed") or [],
        "numbers_checked": numbers, "rulecheck": rc,
        "session_id": parsed["session_id"], "cost_usd": parsed["cost_usd"],
        "started_at": started, "finished_at": finished, "duration_s": duration,
        "paths": paths, "actions_count": len(parsed["actions"]),
    }
    _write(paths["result_json"], json.dumps(rec, indent=2, ensure_ascii=False, default=str))
    _write(paths["result_md"], summarize.render_result_md(rec))
    _write(paths["rulecheck"], json.dumps(rc, indent=2, ensure_ascii=False))
    _write(paths["summary"], "\n".join(lines) + "\n")

    ledger.append(cfg.runs_dir, **base, event="result", status=status,
                  prompt_path=paths["prompt"], response_path=paths["result_md"], summary_path=paths["summary"],
                  files_read=files_read, files_changed=rc["changed_in_scope"], write_scope=[s["path"] for s in scopes],
                  started_at=started, finished_at=finished, duration_s=duration, exit_code=exit_code,
                  note="; ".join(notes + rc["violations"] + rc["flags"])[:1000] or None,
                  **{"from": spec.agent, "to": "CK"})

    sys.stdout.write(paths["summary"] + "\n" + "\n".join(lines) + "\n")
    sys.stdout.flush()
    if status != "ok":
        why = (rc["violations"] + rc["flags"] + notes)[:1]
        print(f"ck-run: status={status} run_id={spec.run_id}" + (f" ({why[0][:200]})" if why else "")
              + f"; details: {paths['result_json']}", file=sys.stderr)
    return EXIT[status]


def review_spec(cfg: Config, orig_run_id: str, by: str, *, blind: bool, focus: str | None,
                title: str | None, model: str | None, timeout_s: int | None) -> Spec:
    if not valid_run_id(orig_run_id):
        raise Refused(f"not a valid run id: {orig_run_id}")
    r = ledger.find_run(cfg.runs_dir, orig_run_id)
    if r is None:
        raise Refused(f"run {orig_run_id} is not in the ledger")
    res = r.last("result")
    if res is None:
        raise Refused(f"run {orig_run_id} has no result yet")
    op = r.opening
    orig_agent = op.get("agent") or op.get("to")
    if by == orig_agent:
        raise Refused(f"cross-review needs a different agent than {orig_agent}")
    run_dir = os.path.join(cfg.runs_dir, orig_run_id)
    orig = {"run_id": orig_run_id, "agent": orig_agent, "title": op.get("title") or "",
            "task_path": os.path.join(run_dir, "task.md"), "result_md": os.path.join(run_dir, "result.md"),
            "result_json": os.path.join(run_dir, "result.json"), "blind": blind}
    for k in ("task_path", "result_md", "result_json"):
        if not os.path.isfile(orig[k]):
            raise Refused(f"missing {orig[k]}")
    ws = cfg.workspace(op.get("workspace") or "")
    task_text = P.review_task(orig=orig, blind=blind, focus=focus,
                              orig_task_text=_read_text(orig["task_path"]) if blind else None)
    default_title = f"{'Blind check' if blind else 'Review'} of {orig_run_id}: {orig['title']}"[:200]
    return Spec(agent=by, ws=ws, title=title or default_title, task_text=task_text, task_source=None,
                task_type=op.get("task_type") or "general", model=model, mode="read", timeout_s=timeout_s,
                parent_run_id=orig_run_id, kind="review", from_=orig_agent, review=orig,
                read_dirs=[] if blind else [run_dir])
