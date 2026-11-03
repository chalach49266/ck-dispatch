"""One run: guards -> run dir -> agent with timeout -> parse -> rulecheck -> ledger -> summary on stdout."""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime

from . import VERSION, ledger, live, postreview, route, rulecheck, summarize
from . import prompt as P
from .config import CLAUDE_GUARD_BASE, PACKET_SCHEMA, REPO, RESULT_SCHEMA, Config, Workspace, inside
from .errors import Refused
from .flags import AGENTS, AUTO_AREAS, EDIT_AREAS, SCHEMA_ENFORCED_BY_FLAG, build, build_live, redacted
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
        self.mode = kw.get("mode") or "auto"
        self.approved = kw.get("approved")
        self.scopes_in = list(kw.get("scopes_in") or [])
        self.read_dirs = list(kw.get("read_dirs") or [])
        self.timeout_s = kw.get("timeout_s")
        self.parent_run_id = kw.get("parent_run_id")
        self.kind = kw.get("kind") or "dispatch"
        self.from_ = kw.get("from_") or "CK"
        self.review = kw.get("review")  # dict for reviews
        self.lead = kw.get("lead")
        self.live = bool(kw.get("live"))  # agent in its own interactive CLI, in an iTerm2/Terminal window
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


def default_auto_scopes(ws: Workspace) -> list:
    """Auto mode without --write-scope. A writable-only workspace: those of its writable folders that exist
    (washu: each course's solutions/ and prep/ that is there). Other workspaces: [] (the whole workspace
    minus its read-only folders)."""
    if not ws.writable_only:
        return []
    have = [e.literal for e in ws.writable_only if os.path.isdir(e.literal)]
    if not have:
        raise Refused(f"none of the writable folders of {ws.name} exists yet "
                      f"({', '.join(e.rel for e in ws.writable_only)}); create the one you need, or use --mode read")
    return have


def outside_writable(ws: Workspace) -> tuple:
    """(folders, files) of a writable-only workspace that exist now outside every writable folder and are
    not on the way to one: the root's entries, then the entries of each folder that holds a writable folder
    (a course folder holds solutions/ and prep/). Claude deny rules cannot reopen a folder inside a denied
    one, so these are denied one by one. Entries made later are caught by the rule check after the run."""
    keep = [e.literal for e in ws.writable_only]
    dirs, files, stack = [], [], [ws.root]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                entries = sorted(it, key=lambda x: x.name)
        except OSError:
            continue
        for e in entries:
            if any(inside(e.path, k) for k in keep):
                continue
            try:
                is_link, is_dir = e.is_symlink(), e.is_dir()
            except OSError:
                is_link, is_dir = False, False
            if is_dir and not is_link and any(inside(k, e.path) for k in keep):
                stack.append(e.path)
            else:
                (dirs if is_dir else files).append(e.path)
    if ws.real_root != ws.root:  # the same entries under the root's real path
        dirs += [ws.real_root + p[len(ws.root):] for p in dirs]
        files += [ws.real_root + p[len(ws.root):] for p in files]
    return dirs, files


def write_claude_settings(cfg: Config, path: str, live_run_dir: str | None = None, auto_ws=None) -> None:
    """Deny rules for one run. Headless: no edits anywhere in agent_runs. Live: the agent must write
    result.md and summary.md in its own run folder, so agent_runs is denied piece by piece instead: the
    ledger, state, locks, every other run folder that exists now, and ck-run's own files in this run."""
    with open(CLAUDE_GUARD_BASE, encoding="utf-8") as f:
        base = json.load(f)
    deny = list(base["permissions"]["deny"])
    targets = []
    for ws in cfg.workspaces.values():
        if auto_ws is not None and ws.key == auto_ws.key:
            # auto mode may edit its own workspace: deny only its raw/protected folders and rule files
            targets += [e.real for e in ws.protected_dirs] + [e.literal for e in ws.protected_dirs]
            deny += [f"Edit(/{e.real})" for e in ws.protected_files]
            if ws.writable_only:  # e.g. washu: only <course>/solutions and <course>/prep stay open
                dirs, files = outside_writable(ws)
                targets += dirs
                deny += [f"Edit(/{p})" for p in files]
            continue
        targets += [ws.root, ws.real_root]
        targets += [e.real for e in ws.protected_dirs if not inside(e.real, ws.real_root)]
    run_bases = list(dict.fromkeys([cfg.runs_dir, os.path.realpath(cfg.runs_dir)]))
    targets += [os.path.realpath(REPO)] + ([] if live_run_dir else run_bases)
    for t in dict.fromkeys(targets):
        deny.append(f"Edit(/{t}/**)")  # '//abs/path' = absolute path in Claude permission rules
    if live_run_dir:
        rid = os.path.basename(live_run_dir)
        for rb in run_bases:
            deny += [f"Edit(/{rb}/ledger.jsonl)", f"Edit(/{rb}/state.json)", f"Edit(/{rb}/.locks/**)"]
            try:
                others = sorted(n for n in os.listdir(rb) if n not in (rid, ".locks") and os.path.isdir(os.path.join(rb, n)))
            except OSError:
                others = []
            deny += [f"Edit(/{rb}/{n}/**)" for n in others]
            deny += [f"Edit(/{rb}/{rid}/{n})" for n in live.CONTROL_FILES]
    _write(path, json.dumps({"permissions": {"deny": list(dict.fromkeys(deny))}}, indent=1))


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
        "cmd": "cmd.txt", "rulecheck": "rulecheck.json", "marker": ".marker",
        "prompt_live": "prompt-live.md", "settings_live": "claude-settings-live.json", "live_sh": "live.sh"}.items()}
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
        elif spec.mode == "auto":
            if spec.kind == "review":
                raise Refused("reviews are read-only")
            scopes_in = spec.scopes_in or default_auto_scopes(ws)
            scopes = validate_write_scopes(cfg, ws, spec.agent, scopes_in, AUTO_AREAS) if scopes_in else []
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
        lv = None
        if spec.live:
            sid = str(uuid.uuid4()) if spec.agent == "claude" else None
            lv = {"session_id": sid, "cmd": build_live(
                cfg, ws, spec.agent, mode=spec.mode, scopes=scopes, model=spec.model, run_dir=run_dir,
                extra_read_dirs=read_dirs, claude_settings_path=paths["settings_live"], session_id=sid)}
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
    lines = [
        f"# ck-run {VERSION} · run {spec.run_id}",
        f"# binary: {cmd.binary}",
        f"# cwd: {cmd.cwd}",
        f"# env: only {', '.join(sorted(env))}; PATH={env['PATH']}",
        f"# prompt: {paths['prompt']} ({'stdin' if cmd.stdin_prompt else 'last argument'})",
        f"# config: {cfg.path}",
        redacted(cmd, RESULT_SCHEMA, paths["prompt"])]
    if lv:
        lv["prompt"] = P.assemble(
            P.header(ws=ws, run_id=spec.run_id, agent=spec.agent, kind=spec.kind, mode=spec.mode,
                     approved=spec.approved, scopes=scopes, live_outbox=run_dir),
            spec.title, spec.task_text,
            P.footer(run_id=spec.run_id, include_schema=True, schema_path=RESULT_SCHEMA, live_outbox=run_dir))
        lines += [f"# live (default; --no-live for headless only): an {cfg.live_terminal} window runs {paths['live_sh']},",
                  f"# which starts the interactive CLI with env -i (same env, TERM from the window) and "
                  f"{paths['prompt_live']} as the first message. The command above is the fallback.",
                  redacted(lv["cmd"], RESULT_SCHEMA, paths["prompt_live"])]
    if postreview.applies(spec.agent, spec.mode, spec.kind):
        lines += [f"# after the run: MANDATORY read-only Claude review ({spec.agent} auto = "
                  f"{'--force, allow-all' if spec.agent == 'cursor' else '--mode auto + workspace/run --add-dir'}; chalach 2026-11-03):",
                  f"# claude -p --permission-mode plan --json-schema schema/review.schema.json over {run_dir}/review-input.md"
                  f" -> {run_dir}/review.md, ledger event {postreview.event_name(spec.agent)} ok|flagged"]
    cmd_txt = "\n".join(lines + [""])
    if dry_run:
        sys.stdout.write(cmd_txt)
        return 0

    lock = SlotLock(cfg, ws.key, exclusive=spec.mode in ("edit", "auto"))
    if not lock.acquire():
        note = f"{ws.name} already has {cfg.max_runs_per_workspace} run(s), or an edit run, in progress"
        ledger.append(cfg.runs_dir, **base, event="busy", status="busy", note=note, **{"from": spec.from_, "to": spec.agent})
        print(f"ck-run: BUSY: {note}; try again later", file=sys.stderr)
        return EXIT["busy"]
    try:
        return _run_locked(cfg, spec, ws, scopes, cmd, prompt_text, env, cmd_txt, run_dir, paths, base, lv)
    finally:
        lock.release()


def _write_replace(path: str, text: str) -> None:
    tmp = f"{path}.ck-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _live_fallback(cfg, spec, base, info, reason: str) -> None:
    """The window did not run the agent: say so loudly, log it, and let the caller run headless."""
    info["window_error"] = reason
    ledger.append(cfg.runs_dir, **base, event="live_fallback", status="fallback", note=f"running headless: {reason}"[:1000],
                  **{"from": "CK", "to": spec.agent})
    print(f"ck-run: LIVE WINDOW FAILED for {spec.agent} (run {spec.run_id}): {reason}\n"
          f"ck-run: running the same task headless instead (ledger event live_fallback).", file=sys.stderr)
    sys.stderr.flush()


def _live_phase(cfg, spec, ws, run_dir, paths, lv, env, base=None):
    """Run the agent in an iTerm2/Terminal window. Returns (info, outcome); outcome None = fall back to headless."""
    base = base or {}
    info = {"requested": True, "used": False, "window_error": None, "state": None, "session_id": None,
            "session_path": None, "session_how": None, "recovered_from_session": False, "summary_text": "",
            "window": None}
    title = live.window_title(spec.agent, spec.title, spec.run_id)
    live.write_script(paths["live_sh"], run_dir=run_dir, cwd=lv["cmd"].cwd, env=env, argv=lv["cmd"].argv,
                      prompt_path=paths["prompt_live"], title=title, agent=spec.agent)
    profile = ""
    if cfg.live_terminal.lower() in ("iterm", "iterm2"):
        _, new_file, perr = live.ensure_iterm_profiles(cfg.home)
        profile = "" if perr else live.profile_name(spec.agent)
        if new_file:
            time.sleep(1.5)  # iTerm2 loads a new dynamic-profile file within about a second
    t0 = time.time()
    win = live.open_window(cfg.live_osascript, f"/bin/bash {shlex.quote(paths['live_sh'])}", title, env,
                           terminal=cfg.live_terminal, profile=profile)
    info["window"] = {"terminal": win["terminal"], "window_id": win["window_id"], "how": win["how"], "title": title}
    if not win["ok"]:
        live.take_claim(run_dir)  # a window that opens anyway later finds the claim and does nothing
        _live_fallback(cfg, spec, base, info, f"osascript could not open a window: {win['error']}")
        return info, None
    locked = bool(profile) and (win["how"] or "").startswith("profile ")
    ledger.append(cfg.runs_dir, **base, event="live_window_opened", status="dispatched",
                  note=(f"window_id={win['window_id'] or 'unknown'}; terminal={win['terminal']}; {win['how']}; "
                        f"title locked: {'yes' if locked else 'no'}; title: {title}")[:1000],
                  **{"from": "CK", "to": spec.agent})
    if not live.wait_started(run_dir, cfg.live_start_timeout_s, min(cfg.live_poll_s, 0.5)):
        tail = live.window_tail(cfg.live_osascript, win["window_id"], env) if win["terminal"] == "iTerm2" else None
        _live_fallback(cfg, spec, base, info,
                       f"the {win['terminal']} window (id {win['window_id'] or 'unknown'}) did not start the agent "
                       f"within {cfg.live_start_timeout_s:g} s" + (f"; the window shows: {tail}" if tail else ""))
        return info, None
    info["used"] = True
    old = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        w = live.wait_done(run_dir, t0 + int(spec.timeout_s), cfg.live_poll_s)
    except KeyboardInterrupt:
        w = {"state": "interrupted", "exit_code": None}
    finally:
        signal.signal(signal.SIGTERM, old)
    if w["state"] in ("timeout", "interrupted"):
        info["stop"] = live.stop(run_dir)
    info["state"] = w["state"]
    result_text = _read_text(paths["result_md"])
    if (w["state"] == "exited" and w["exit_code"] not in (None, 0, 130) and not result_text
            and time.time() - t0 < cfg.live_early_exit_s):
        info["used"] = False
        _live_fallback(cfg, spec, base, info, f"the agent CLI exited with code {w['exit_code']} in the window after "
                                              f"{time.time() - t0:.0f} s, before writing anything")
        return info, None
    info["summary_text"] = _read_text(paths["summary"], 200_000)
    sess = live.find_session(spec.agent, cfg.home, t0 - 5, spec.run_id, known_id=lv["session_id"])
    info.update(session_id=sess["id"], session_path=sess["path"], session_how=sess["how"])
    structured = summarize.extract_structured(result_text)
    if not (isinstance(structured, dict) and "summary" in structured):
        structured = None
    final_text = result_text
    if structured is None:
        got, text = live.recover_result(sess["path"], spec.run_id)
        if got is not None:
            structured, final_text = got, text
            info["recovered_from_session"] = True
    parsed = {"final_text": final_text, "structured": structured, "actions": [], "observed_reads": [],
              "session_id": sess["id"], "cost_usd": None, "agent_error": None, "finished_reason": None,
              "text_only": True}
    info["exit_code"] = w["exit_code"]
    # Once summary.md is written the work is done; how the user later leaves the CLI (Ctrl+C = 130) does not count.
    code = None if w["state"] == "done" else w["exit_code"]
    return info, (code, w["state"] == "timeout", w["state"] == "interrupted", parsed)


def _run_locked(cfg, spec, ws, scopes, cmd, prompt_text, env, cmd_txt, run_dir, paths, base, lv=None) -> int:
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
        write_claude_settings(cfg, paths["settings"], auto_ws=ws if spec.mode == "auto" else None)
    if lv:
        _write(paths["prompt_live"], lv["prompt"])
        if spec.agent == "claude":
            write_claude_settings(cfg, paths["settings_live"], live_run_dir=run_dir,
                                  auto_ws=ws if spec.mode == "auto" else None)
    packet = {
        "run_id": spec.run_id, "kind": spec.kind, "parent_run_id": spec.parent_run_id, "from": spec.from_,
        "workspace": ws.key, "workspace_root": ws.root, "lead": spec.lead, "task_type": spec.task_type,
        "title": spec.title, "worker": spec.agent, "model": spec.model, "cwd": cmd.cwd,
        "mode": {"read": "plan", "edit": "edit-approved", "auto": "auto"}[spec.mode],
        "approved_actions": [spec.approved] if spec.mode == "edit" else [],
        "write_scope": [s["path"] for s in scopes], "deliverable": "result.md + result.json + summary.md",
        "budget": {"timeout_s": int(spec.timeout_s)}, "review": spec.review, "task_source": spec.task_source,
        "created_at": started, "config_path": cfg.path, "ck_dispatch_version": VERSION}
    perr = validate(packet, load_schema(PACKET_SCHEMA))
    if perr:
        raise Refused(f"internal: packet does not match its schema: {perr[:3]}")
    _write(paths["packet"], json.dumps(packet, indent=2, ensure_ascii=False))
    ledger.append(cfg.runs_dir, **base, event=spec.kind, status="dispatched",
                  prompt_path=paths["prompt_live"] if lv else paths["prompt"], response_path=paths["result_md"],
                  summary_path=paths["summary"], write_scope=packet["write_scope"], started_at=started,
                  note=f"live: interactive CLI in an {cfg.live_terminal} window (headless if the window fails)" if lv else None,
                  **{"from": spec.from_, "to": spec.agent})

    reviewed = postreview.applies(spec.agent, spec.mode, spec.kind)
    roots = rulecheck.scan_roots(ws, scopes)
    try:
        snap = postreview.before(cfg, ws, scopes, roots) if reviewed else None
    except Exception as e:  # the review then flags the run (no 'before' listing to compare with)
        snap = {"listing": {"files": set(), "truncated": True}, "git": {}, "error": str(e)}
    marker_ns = rulecheck.touch_marker(paths["marker"])
    t0 = time.time()
    live_info, outcome = (None, None)
    if lv:
        live_info, outcome = _live_phase(cfg, spec, ws, run_dir, paths, lv, env, base)
    if outcome is None:
        exit_code, timed_out, interrupted = run_child(cmd, prompt_text, env, run_dir, int(spec.timeout_s))
        parsed = summarize.parse(spec.agent, run_dir)
        stdout = _read_text(os.path.join(run_dir, "stdout.log"))
        stderr = _read_text(os.path.join(run_dir, "stderr.log"), 200_000)
    else:
        exit_code, timed_out, interrupted, parsed = outcome
        stdout, stderr = parsed["final_text"], ""  # text only: the rule check scans the agent's result text
    is_live = bool(live_info and live_info["used"])
    duration = round(time.time() - t0, 1)
    finished = ledger.now_iso()

    scan = rulecheck.scan_changed(roots, marker_ns, cfg.scan_budget_s, cfg.scan_max_entries)
    cls = rulecheck.classify(cfg, ws, spec.mode, scopes, scan["changed"], run_dir)
    tr = rulecheck.scan_transcript(parsed, stdout, stderr, agent=spec.agent, mode=spec.mode)
    rc = {"violations": cls["violations"] + tr["violations"], "flags": cls["flags"] + tr["flags"],
          "notes": cls["notes"] + tr.get("notes", []), "changed_in_scope": cls["changed_in_scope"],
          "scan": {"roots": roots, "entries": scan["entries"],
                   "truncated": scan["truncated"], "seconds": scan["seconds"]}}

    review = None
    if reviewed:  # cursor/agy auto never asked before acting: mandatory read-only Claude check (postreview.py)
        try:
            review = postreview.run(
                cfg, agent=spec.agent, run_id=spec.run_id, ws=ws, scopes=scopes, run_dir=run_dir,
                run_paths={"result_md": paths["result_md"],
                           "stdout": None if is_live else os.path.join(run_dir, "stdout.log")},
                snap=snap, roots=roots, changed=scan["changed"], rc=rc,
                session_path=postreview.session_for(spec.agent, cfg.home, t0 - 5, spec.run_id, live_info),
                text="\n".join([parsed.get("final_text") or "", "" if is_live else stdout[-200_000:], stderr[-50_000:]]),
                write_settings=lambda p: write_claude_settings(cfg, p))
        except Exception as e:  # never let the check crash ck-run; an unchecked run is a flagged run
            review = postreview.failed(spec.agent, run_dir, e)
        ledger.append(cfg.runs_dir, **dict(base, via="CK"), event=review["event"], status=review["status"],
                      prompt_path=None, response_path=review["review_md"], summary_path=None,
                      files_changed=rc["changed_in_scope"], write_scope=[s["path"] for s in scopes],
                      duration_s=review["seconds"],
                      note=(f"{review['status']}: {postreview.headline(review) if review['status'] == 'flagged' else review['summary']}")[:1000],
                      **{"from": "claude", "to": "CK"})
        if review["status"] == "flagged":
            rc["flags"].append(f"post-run Claude review flagged the run: {postreview.headline(review)} "
                               f"(see {review['review_md']})")
            print(postreview.loud_warning(spec.agent, spec.run_id, review), file=sys.stderr)
            sys.stderr.flush()
    rc["clean"] = not rc["violations"] and not rc["flags"]

    structured = parsed["structured"] if isinstance(parsed["structured"], dict) else None
    schema_errors = validate(structured, load_schema(RESULT_SCHEMA)) if structured else ["no result JSON found"]
    agent_summary = (live_info or {}).get("summary_text", "").strip() if is_live else ""
    if agent_summary:  # live: the agent wrote summary.md itself
        lines, src, sum_note = summarize.summary_lines({"summary": agent_summary}, "", paths["result_md"])
    else:
        lines, src, sum_note = summarize.summary_lines(structured, parsed["final_text"], paths["result_md"])
    if review and review["status"] == "flagged":  # CK reads summary.md first: the warning goes on top
        lines = [f"FLAGGED by the post-run Claude review: {postreview.headline(review)[:120]} (review.md)"] + lines[:9]
    notes = [n for n in [sum_note] if n]
    if live_info and not is_live:
        notes.append(f"live window not used ({live_info['window_error']}); ran headless")
    if is_live:
        notes.append(f"live: ran in a {(live_info.get('window') or {}).get('terminal') or 'terminal'} window; state {live_info['state']}; session "
                     f"{live_info['session_id'] or 'unknown'} ({live_info['session_how']})")
        if live_info["recovered_from_session"]:
            notes.append("result recovered from the session log: the agent did not write it to result.md")
        if not agent_summary and live_info["state"] != "timeout":
            notes.append("the agent did not write summary.md")
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

    # live: the agent left the window without writing anything ck-run can use
    live_gave_nothing = is_live and live_info["state"] == "exited" and structured is None and not agent_summary
    if rc["violations"]:
        status = "violation"
    elif rc["flags"]:
        status = "flagged"
    elif timed_out:
        status = "timeout"
    elif (interrupted or agent_status == "failed" or live_gave_nothing
          or (structured is None and (exit_code not in (0, None) or parsed["agent_error"]))):
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
        "live": ({k: v for k, v in live_info.items() if k != "summary_text"} if live_info else None),
        "post_review": ({k: review[k] for k in ("status", "summary", "review_md", "findings", "error", "seconds")}
                        if review else None),
    }
    summary_text = "\n".join(lines) + "\n"
    if is_live:
        # The agent's own result.md stays (ck-run's view of it is result.json). summary.md is rewritten only
        # to cut it to 10 lines, so it always matches what ck-run prints.
        _write_replace(paths["result_json"], json.dumps(rec, indent=2, ensure_ascii=False, default=str))
        if not os.path.exists(paths["result_md"]):
            _write_replace(paths["result_md"], summarize.render_result_md(rec))
        _write_replace(paths["rulecheck"], json.dumps(rc, indent=2, ensure_ascii=False))
        if _read_text(paths["summary"], 200_000) != summary_text:
            _write_replace(paths["summary"], summary_text)
    else:
        put = _write_replace if live_info else _write  # after a failed window, a stray file may exist
        put(paths["result_json"], json.dumps(rec, indent=2, ensure_ascii=False, default=str))
        put(paths["result_md"], summarize.render_result_md(rec))
        put(paths["rulecheck"], json.dumps(rc, indent=2, ensure_ascii=False))
        put(paths["summary"], summary_text)

    ledger.append(cfg.runs_dir, **base, event="result", status=status,
                  prompt_path=paths["prompt_live"] if is_live else paths["prompt"],
                  response_path=paths["result_md"], summary_path=paths["summary"],
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
                title: str | None, model: str | None, timeout_s: int | None, live: bool = False) -> Spec:
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
                read_dirs=[] if blind else [run_dir], live=live)
