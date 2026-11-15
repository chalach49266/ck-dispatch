"""One run: guards -> run dir -> agent with timeout -> parse -> rulecheck -> ledger -> summary on stdout.

v4 (2026-11-05, BUILD_SPEC_v4): before the claims, the ask gate (asks.gate: budgets, review reasons, exit 7 over
budget) and the andon (andon.check, when that module exists); the model comes from an alias or the tier
(models.resolve); every event carries the ask id, size, pattern, tier and model alias; the post-run review of
cursor/agy auto runs runs only when config post_review.enabled is true; the live watchdog may stop a stalled run
(status timeout, exit 3); finalize_run() finishes a run whose ck-run watcher died (ck-run reattach)."""
from __future__ import annotations

import contextlib
import fcntl
import inspect
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

from . import VERSION, herdr, asks, claims, ledger, live, models, postreview, route, rulecheck, summarize
from . import prompt as P
from .config import CLAUDE_GUARD_BASE, PACKET_SCHEMA, REPO, RESULT_SCHEMA, Config, Workspace, inside
from .errors import Refused
from .flags import AGENTS, AUTO_AREAS, CODEX_WEB_CONFIG, EDIT_AREAS, SCHEMA_ENFORCED_BY_FLAG, build, build_live, redacted
from .guards import child_env, validate_write_scopes
from .schema import load as load_schema
from .schema import validate

RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}-[a-z0-9]+-[a-z0-9_]+-[a-z]+-[0-9a-f]{4}$")
TASK_TYPE_RE = re.compile(r"^[a-z0-9_]{1,32}$")
EXIT = {"ok": 0, "partial": 1, "refused": 2, "failed": 3, "timeout": 3, "flagged": 4, "violation": 5, "busy": 6,
        "over_budget": 7}
MAX_TASK_BYTES = 200_000
# chalach 2026-11-05 (BUILD_SPEC_v4 decision 7), verbatim in every result note of a cursor/agy auto run
POST_REVIEW_OFF = ('post-run review off (chalach 2026-11-05: "no need review, since those one are for simple '
                   'stupid task anyways")')
WINDOWS_DIR = ".windows"  # <runs_dir>/.windows/<ask>.json: the iTerm2 window of an ask (live.open_window, E2)


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
        self.claims_in = list(kw.get("claims_in") or [])  # --claim: paths this run will edit (claims.py)
        self.auto_claims, self.auto_skipped = [], []  # --write-scope folders claimed automatically / not (shared)
        self.read_dirs = list(kw.get("read_dirs") or [])
        self.timeout_s = kw.get("timeout_s")
        self.parent_run_id = kw.get("parent_run_id")
        self.kind = kw.get("kind") or "dispatch"
        self.from_ = kw.get("from_") or "CK"
        self.review = kw.get("review")  # dict for reviews
        self.lead = kw.get("lead")
        self.live = bool(kw.get("live"))  # agent in its own interactive CLI, in an iTerm2/Terminal window
        self.auto_readonly = None  # auto run with no write area (auto_readonly_reason), else None
        self.run_id = ""
        # v4 (BUILD_SPEC_v4 5, 6): the ask, the review reason, chalach's --over words, the model request
        self.ask_id = kw.get("ask_id")
        self.trigger = kw.get("trigger")
        self.over = kw.get("over")
        self.model_req = kw.get("model")    # --model as given: an alias or a raw model id
        self.tier_req = kw.get("tier")      # --tier as given
        self.model_alias = self.tier = self.effort = None  # set by prepare_v4 (self.model = the resolved model id)
        self.model_explicit, self.model_notes = bool(self.model_req), []
        self.size = self.pattern = self.project = self.budget = None
        self.stall_s = None
        self.over_info = None

    @property
    def check_mode(self) -> str:
        """The mode the rule check, claims and attribution use: an auto run downgraded to read-only is a reader."""
        return "read" if self.auto_readonly else self.mode


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


def reserve_run_dir(cfg: Config, make_id) -> tuple:
    """Pick a run id whose folder did not exist and create that folder (mkdir is atomic: two ck-runs started in
    the same second with the same workspace, task type and agent can never share a folder)."""
    old_umask = os.umask(0o077)
    try:
        os.makedirs(cfg.runs_dir, mode=0o700, exist_ok=True)
        for _ in range(100):
            rid = make_id()
            run_dir = os.path.join(cfg.runs_dir, rid)
            try:
                os.mkdir(run_dir, 0o700)
                return rid, run_dir
            except FileExistsError:
                continue
    finally:
        os.umask(old_umask)
    raise Refused("could not reserve a unique run folder")


def _unreserve(run_dir: str) -> None:
    try:
        os.rmdir(run_dir)  # only if still empty
    except OSError:
        pass


AUTO_READONLY_NO_WRITABLE = "no writable folder"


def auto_readonly_reason(ws: Workspace) -> str | None:
    """chalach 2026-11-03 19:34 CT: auto mode without --write-scope in a writable-only workspace none of whose
    writable folders exists (washu before any <course>/solutions/ or prep/ is made) still runs, read-only:
    AUTO_READONLY_NO_WRITABLE. None = normal auto (a writable folder exists, or the workspace has no list)."""
    if ws.writable_only and not any(os.path.isdir(e.literal) for e in ws.writable_only):
        return AUTO_READONLY_NO_WRITABLE
    return None


def auto_readonly_note(ws: Workspace) -> str:
    missing = ", ".join(f"{e.rel}/" for e in ws.writable_only)
    return (f"auto_readonly: {AUTO_READONLY_NO_WRITABLE}. None of the writable folders of {ws.name} exists yet "
            f"({missing}), so this auto run is read-only: the agent keeps its own auto mode but has no write area "
            f"(only its run-folder output files). ck-run creates none of those folders; once one exists, auto runs "
            f"write there again")


def default_auto_scopes(ws: Workspace) -> list:
    """Auto mode without --write-scope. A writable-only workspace: those of its writable folders that exist
    (washu: each course's solutions/ and prep/ that is there). Other workspaces: [] (the whole workspace
    minus its read-only folders). Callers check auto_readonly_reason() first; the refusal below is only a
    backstop, so [] never silently means "the whole workspace" for a writable-only one."""
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


# chalach 2026-11-05 ~18:55 CT: "I want to clear the restriction" (web search for agent deployments). Claude's
# built-in web tools: config/claude-guard.base.json allows them and must never deny them (tests/test_web.py).
CLAUDE_WEB_TOOLS = ("WebSearch", "WebFetch")


def claude_base_permissions() -> dict:
    """{'allow': [...], 'deny': [...]} from config/claude-guard.base.json."""
    with open(CLAUDE_GUARD_BASE, encoding="utf-8") as f:
        perms = json.load(f)["permissions"]
    return {"allow": list(perms.get("allow") or []), "deny": list(perms.get("deny") or [])}


def web_line(agent: str) -> str:
    """cmd.txt / --dry-run line: the agent's built-in web search / fetch (allowed, chalach 2026-11-05)."""
    if agent == "claude":
        perms = claude_base_permissions()
        allow = [t for t in perms["allow"] if t.split("(")[0] in CLAUDE_WEB_TOOLS]
        deny = [t for t in perms["deny"] if t.split("(")[0] in CLAUDE_WEB_TOOLS]
        how = f"Claude settings allow {', '.join(allow) or 'none'}; deny {', '.join(deny) or 'none of them'}"
    else:
        how = {"codex": f"codex {' '.join(CODEX_WEB_CONFIG)} (= --search, live web search; the sandbox network "
                        "rule for shell commands is unchanged)",
               "cursor": "cursor's built-in WebSearch/WebFetch: no ck-run flag turns them off (whether a search asks "
                         "first is cursor's own autoAcceptWebSearch in ~/.cursor/cli-config.json)",
               "agy": "agy's built-in search_web: no ck-run flag turns it off (--sandbox restricts terminal commands "
                      "only)"}[agent]
    return (f"# web (chalach 2026-11-05: built-in web search/fetch allowed): {how}; shell network tools (curl, wget, "
            "scp, rsync, ssh) unchanged")


def write_claude_settings(cfg: Config, path: str, live_run_dir: str | None = None, auto_ws=None,
                          other_claims: list | None = None) -> None:
    """Allow + deny rules for one run. Allow: the base's (WebSearch, WebFetch; chalach 2026-11-05). Deny: the
    base's, plus no edits anywhere in agent_runs (headless). Live: the agent must write
    result.md and summary.md in its own run folder, so agent_runs is denied piece by piece instead: the
    ledger, state, the run registry, every other run folder that exists now, and ck-run's own files in this
    run. other_claims: paths other live runs claimed when this run started (never edit them)."""
    base = claude_base_permissions()
    deny = list(base["deny"])
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
    for c in dict.fromkeys(other_claims or []):  # a claim may be a file or a folder (or not exist yet)
        deny += [f"Edit(/{c})", f"Edit(/{c}/**)"]
    if live_run_dir:
        rid = os.path.basename(live_run_dir)
        for rb in run_bases:
            deny += [f"Edit(/{rb}/ledger.jsonl)", f"Edit(/{rb}/state.json)", f"Edit(/{rb}/.locks/**)",
                     f"Edit(/{rb}/{claims.DIR}/**)"]
            try:
                others = sorted(n for n in os.listdir(rb) if n != rid and not n.startswith(".")
                                and os.path.isdir(os.path.join(rb, n)))
            except OSError:
                others = []
            deny += [f"Edit(/{rb}/{n}/**)" for n in others]
            deny += [f"Edit(/{rb}/{rid}/{n})" for n in live.CONTROL_FILES]
    _write(path, json.dumps({"permissions": {"allow": list(dict.fromkeys(base["allow"])),
                                             "deny": list(dict.fromkeys(deny))}}, indent=1))


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


def run_child(cmd, prompt_text: str, env: dict, run_dir: str, timeout_s: int, on_start=None):
    argv = cmd.final_argv(prompt_text)
    timed_out = interrupted = False
    old = signal.signal(signal.SIGTERM, _on_sigterm)
    with open(os.path.join(run_dir, "stdout.log"), "wb") as out, open(os.path.join(run_dir, "stderr.log"), "wb") as err:
        proc = subprocess.Popen(argv, cwd=cmd.cwd, env=env,
                                stdin=subprocess.PIPE if cmd.stdin_prompt else subprocess.DEVNULL,
                                stdout=out, stderr=err, start_new_session=True, close_fds=True)
        if on_start is not None:  # the run record keeps the agent's pid: if ck-run dies, claims stay while it runs
            try:
                on_start(proc.pid)
            except Exception:
                pass
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


def prepare_v4(cfg: Config, spec: Spec) -> None:
    """The ask and the model of a run (BUILD_SPEC_v4 5, 6). --ask: the ask must exist (asks.gate checks closed and
    budgets later); its size, pattern, project and budget go on the spec. No --ask while config asks.required: refused
    with the exact `ck-run ask open` line. The model: models.resolve (alias, raw id, or tiers[agent][tier] with the
    tier from --tier / the ask). No --timeout: the ask size's timeout_s. Watchdog stall_s: the ask's, else
    config live.stall_s."""
    if spec.ask_id:
        st = asks.need(cfg, spec.ask_id)
        spec.ask_id = st["ask_id"]
        spec.size, spec.pattern, spec.project, spec.budget = st["size"], st["pattern"], st["project"], st["budget"]
    elif cfg.asks_required:
        raise Refused(asks.required_message(spec))
    res = models.resolve(spec.agent, spec.model_req, spec.tier_req, size=spec.size, pattern=spec.pattern)
    if res["agent"] != spec.agent:
        raise Refused(f"--model {spec.model_req} runs on {res['agent']}, not {spec.agent}")
    spec.model, spec.model_alias, spec.tier, spec.effort = res["model"], res["alias"], res["tier"], res["effort"]
    spec.model_explicit, spec.model_notes = res["explicit"], list(res["notes"])
    if spec.timeout_s is None and spec.budget:
        spec.timeout_s = min(int(spec.budget["timeout_s"]), cfg.max_timeout_s)
    spec.stall_s = float(spec.budget["stall_s"]) if spec.budget else float(cfg.live_stall_s)


def andon_check(cfg: Config, spec: Spec) -> None:
    """BUILD_SPEC_v4 10.3: andon.check(cfg, project, workspace_key) raises Refused while an andon is raised for the
    ask's project or the workspace. Skipped when ckdispatch/andon.py is not there; a broken andon module refuses
    (a stop switch fails safe)."""
    try:
        from . import andon
    except ImportError:
        return
    except Exception as e:
        raise Refused(f"the andon check could not load ({type(e).__name__}: {e}); fix ckdispatch/andon.py")
    check = getattr(andon, "check", None)
    if not callable(check):
        return
    try:
        check(cfg, spec.project, spec.ws.key)
    except Refused:
        raise
    except Exception as e:
        raise Refused(f"the andon check failed ({type(e).__name__}: {e})")


def execute(cfg: Config, spec: Spec, dry_run: bool = False) -> int:
    ws = spec.ws
    make_id = lambda: new_run_id(ws.key, spec.task_type, spec.agent)  # noqa: E731
    reserved = not dry_run and spec.agent in AGENTS and bool(TASK_TYPE_RE.match(spec.task_type or ""))
    if reserved:  # the run folder is created now, so no other ck-run can get the same id
        spec.run_id, run_dir = reserve_run_dir(cfg, make_id)
    else:
        spec.run_id = make_id()
        run_dir = os.path.join(cfg.runs_dir, spec.run_id)
    paths = {k: os.path.join(run_dir, v) for k, v in {
        "prompt": "prompt.md", "task": "task.md", "result_md": "result.md", "result_json": "result.json",
        "summary": "summary.md", "settings": "claude-settings.json", "packet": "packet.json",
        "cmd": "cmd.txt", "rulecheck": "rulecheck.json", "marker": ".marker",
        "prompt_live": "prompt-live.md", "settings_live": "claude-settings-live.json", "live_sh": "live.sh"}.items()}
    base = {"run_id": spec.run_id, "parent_run_id": spec.parent_run_id, "title": spec.title,
            "workspace": ws.key, "task_type": spec.task_type, "agent": spec.agent, "model": spec.model,
            "mode": spec.mode, "approved": spec.approved, "via": "CK" if spec.kind == "review" else None,
            "ask_id": spec.ask_id, "trigger": spec.trigger}
    claim_paths = []
    try:
        if spec.agent not in AGENTS:
            raise Refused(f"unknown agent '{spec.agent}'")
        if not TASK_TYPE_RE.match(spec.task_type):
            raise Refused("task type must match [a-z0-9_]{1,32}")
        prepare_v4(cfg, spec)
        base.update(ask_id=spec.ask_id, size=spec.size, pattern=spec.pattern, tier=spec.tier, project=spec.project,
                    model=spec.model, model_alias=spec.model_alias)
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
            spec.auto_readonly = None if spec.scopes_in else auto_readonly_reason(ws)
            if spec.auto_readonly:  # chalach 2026-11-03 19:34 CT: launch anyway, with no write area
                if spec.claims_in:
                    raise Refused(f"--claim: this auto run is read-only ({auto_readonly_note(ws)}); "
                                  f"there is nothing to claim")
                scopes = []
            else:
                scopes_in = spec.scopes_in or default_auto_scopes(ws)
                scopes = validate_write_scopes(cfg, ws, spec.agent, scopes_in, AUTO_AREAS) if scopes_in else []
        elif spec.mode == "read":
            if spec.scopes_in:
                raise Refused("--write-scope is only for --mode edit")
            scopes = []
        else:
            raise Refused(f"forbidden mode '{spec.mode}'")
        read_dirs = _validate_read_dirs(cfg, spec.read_dirs)
        claim_paths = claims.normalize(cfg, ws, spec.mode, spec.kind, scopes, spec.claims_in)
        # chalach 2026-11-03 19:02 CT: edit runs (and auto runs given --write-scope) claim their write folders
        # automatically; never a root, default write folder or wiki-entry folder (that would serialize every run)
        spec.auto_claims, spec.auto_skipped = claims.from_scopes(cfg, spec.mode, spec.kind, bool(spec.scopes_in), scopes)
        claim_paths = claims.merge(spec.auto_claims, claim_paths)
        base["claims"] = claim_paths
        base["auto_readonly"] = spec.auto_readonly
        ro = bool(spec.auto_readonly)
        cmd = build(cfg, ws, spec.agent, mode=spec.mode, scopes=scopes, model=spec.model, run_dir=run_dir,
                    schema_path=RESULT_SCHEMA, extra_read_dirs=read_dirs, timeout_s=int(spec.timeout_s),
                    claude_settings_path=paths["settings"], readonly=ro, effort=spec.effort)
        lv = None
        if spec.live:
            sid = str(uuid.uuid4()) if spec.agent == "claude" else None
            lv = {"session_id": sid, "cmd": build_live(
                cfg, ws, spec.agent, mode=spec.mode, scopes=scopes, model=spec.model, run_dir=run_dir,
                extra_read_dirs=read_dirs, claude_settings_path=paths["settings_live"], session_id=sid,
                readonly=ro, effort=spec.effort)}
    except Refused as e:
        if reserved:
            _unreserve(run_dir)
        if not dry_run:
            ledger.append(cfg.runs_dir, **base, event="refused", status="refused", note=str(e),
                          **{"from": spec.from_, "to": spec.agent})
        print(f"ck-run: REFUSED: {e}", file=sys.stderr)
        return EXIT["refused"]
    if spec.auto_readonly:
        print(f"ck-run: NOTE ({spec.agent}): {auto_readonly_note(ws)}.", file=sys.stderr)
        sys.stderr.flush()

    # v4: the ask gate (budgets, review reasons; exit 7 over budget) and the andon, before the claims. With an ask,
    # gate + claims run under <runs_dir>/.asks.lock, so two runs of one ask cannot both take its last slot.
    held = None
    who = {"from": spec.from_, "to": spec.agent}
    if dry_run:
        try:
            spec.over_info = asks.gate(cfg, spec, base=dict(base, **who), dry_run=True)
            andon_check(cfg, spec)
        except asks.OverBudget as e:
            print(f"ck-run: OVER BUDGET: {e}", file=sys.stderr)
            return EXIT["over_budget"]
        except Refused as e:
            print(f"ck-run: REFUSED: {e}", file=sys.stderr)
            return EXIT["refused"]
        others_live, _ = claims.active(cfg.runs_dir)
    else:
        with (asks.lock(cfg) if spec.ask_id else contextlib.nullcontext()):
            try:
                spec.over_info = asks.gate(cfg, spec, base=dict(base, **who))
                andon_check(cfg, spec)
            except asks.OverBudget as e:  # asks.gate wrote the over_budget event
                _unreserve(run_dir)
                print(f"ck-run: OVER BUDGET: {e}", file=sys.stderr)
                return EXIT["over_budget"]
            except Refused as e:
                _unreserve(run_dir)
                ledger.append(cfg.runs_dir, **base, event="refused", status="refused", note=str(e)[:1000], **who)
                print(f"ck-run: REFUSED: {e}", file=sys.stderr)
                return EXIT["refused"]
            # No workspace lock (chalach 2026-11-03): any number of runs, any agents, same or other workspaces. Only
            # a --claim that overlaps a live run's claim is refused (claims.py). Every run is registered, claims or not.
            try:
                got = claims.acquire(cfg.runs_dir, {
                    "run_id": spec.run_id, "agent": spec.agent, "workspace": ws.key, "mode": spec.mode,
                    "kind": spec.kind, "title": spec.title, "task_type": spec.task_type, "claims": claim_paths,
                    "claims_auto": spec.auto_claims, "write_scope": [s["path"] for s in scopes], "run_dir": run_dir,
                    "auto_readonly": spec.auto_readonly, "parent_run_id": spec.parent_run_id,
                    "ask_id": spec.ask_id, "size": spec.size, "pattern": spec.pattern, "project": spec.project,
                    "model": spec.model, "model_alias": spec.model_alias, "tier": spec.tier})
            except BaseException:
                _unreserve(run_dir)
                raise
        claims.log_stale(cfg.runs_dir, got["stale"], by=f"run {spec.run_id}")
        if not got["ok"]:
            _unreserve(run_dir)
            note = "; ".join(f"claim {m}{' (its --write-scope, claimed automatically)' if m in spec.auto_claims else ''}"
                             f" overlaps {t}, claimed by live {claims.describe(r)}" for m, t, r in got["conflicts"][:5])
            note = f"file claim conflict: {note}; nothing was started"
            ledger.append(cfg.runs_dir, **base, event="busy", status="busy", note=note[:1000],
                          **{"from": spec.from_, "to": spec.agent})
            print(f"ck-run: BUSY: {note}. Wait for that run, or use other paths (another --write-scope folder or "
                  f"--claim; runs without overlapping claims always start).", file=sys.stderr)
            return EXIT["busy"]
        held, others_live = got["held"], got["others"]
    try:
        return _prepare_and_run(cfg, spec, ws, scopes, cmd, lv, run_dir, paths, base, claim_paths,
                                others_live, held, dry_run)
    finally:
        if held is not None:
            held.release()


def _prepare_and_run(cfg, spec, ws, scopes, cmd, lv, run_dir, paths, base, claim_paths, others_live, held,
                     dry_run) -> int:
    other_claims = [(c, r) for r in others_live for c in r.get("claims") or []]
    head = P.header(ws=ws, run_id=spec.run_id, agent=spec.agent, kind=spec.kind, mode=spec.mode,
                    approved=spec.approved, scopes=scopes, claims=claim_paths, other_claims=other_claims,
                    auto_readonly=spec.auto_readonly, post_review=cfg.post_review_enabled)
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
        redacted(cmd, RESULT_SCHEMA, paths["prompt"]),
        web_line(spec.agent),
        model_line(spec)]
    if spec.ask_id:
        lines.append(f"# ask: {spec.ask_id} (size {spec.size}, pattern {spec.pattern}"
                     + (f", project {spec.project}" if spec.project else "")
                     + (f", review reason {spec.trigger}" if spec.trigger else "") + ")"
                     + (f"; over budget, passed with chalach's words: \"{spec.over_info['approved']}\""
                        if (spec.over_info or {}).get("over") else ""))
    if lv:
        lv["prompt"] = P.assemble(
            P.header(ws=ws, run_id=spec.run_id, agent=spec.agent, kind=spec.kind, mode=spec.mode,
                     approved=spec.approved, scopes=scopes, live_outbox=run_dir, claims=claim_paths,
                     other_claims=other_claims, auto_readonly=spec.auto_readonly,
                     post_review=cfg.post_review_enabled),
            spec.title, spec.task_text,
            P.footer(run_id=spec.run_id, include_schema=True, schema_path=RESULT_SCHEMA, live_outbox=run_dir))
        host = herdr.detect_host(cfg.live_terminal)
        if host.startswith("herdr:"):
            lines.append(herdr.plan(spec.run_id, lv["cmd"].cwd, paths["live_sh"], host))
        lines += [f"# live (default; --no-live for headless only): an {host} window runs {paths['live_sh']},",
                  f"# which starts the interactive CLI with env -i (same env, TERM from the window) and "
                  f"{paths['prompt_live']} as the first message. The command above is the fallback.",
                  redacted(lv["cmd"], RESULT_SCHEMA, paths["prompt_live"])]
    if spec.auto_readonly:
        lines += [f"# auto_readonly: {spec.auto_readonly} (none of the writable folders of {ws.name} exists; ck-run "
                  f"creates none): auto mode, no write area"
                  + {"claude": "; Claude settings deny edits in the whole workspace",
                     "codex": "; codex sandbox -s read-only (approvals still go to auto_review)"}.get(spec.agent, "")
                  + "; any change in the workspace is a rule-check violation"]
    if postreview.applies(spec.agent, spec.mode, spec.kind) and cfg.post_review_enabled:
        lines += [f"# after the run: MANDATORY read-only Claude review ({spec.agent} auto = "
                  f"{'--force, allow-all' if spec.agent == 'cursor' else '--mode auto + workspace/run --add-dir'}; "
                  f"chalach 2026-11-03; on while config post_review.enabled is true):",
                  f"# claude -p --permission-mode plan --json-schema schema/review.schema.json over {run_dir}/review-input.md"
                  f" -> {run_dir}/review.md, ledger event {postreview.event_name(spec.agent)} ok|flagged"]
    elif postreview.applies(spec.agent, spec.mode, spec.kind):
        lines += [f"# after the run: {POST_REVIEW_OFF}; the rule check still runs"]
    lines.append(f"# claims (no workspace lock; only overlapping claims are refused): "
                 + (", ".join(claim_paths) or "none")
                 + (f"; claimed automatically (--write-scope): {', '.join(spec.auto_claims)}" if spec.auto_claims else "")
                 + "".join(f"; not claimed automatically: {p} ({why}; claim a subfolder or file with --claim)"
                           for p, why in spec.auto_skipped)
                 + (f"; other live runs claim: {', '.join(c for c, _ in other_claims)}" if other_claims else ""))
    cmd_txt = "\n".join(lines + [""])
    if dry_run:
        sys.stdout.write(cmd_txt)
        return 0
    return _run_registered(cfg, spec, ws, scopes, cmd, prompt_text, env, cmd_txt, run_dir, paths, base, lv,
                           claim_paths=claim_paths, other_claims=[c for c, _ in other_claims], held=held)


def model_line(spec: Spec) -> str:
    """cmd.txt / --dry-run: the model ck-run chose and why (models.resolve)."""
    if spec.model_explicit:
        how = "named with --model"
    elif spec.tier_req:
        how = f"tier {spec.tier} (--tier)"
    elif spec.pattern == "race":
        how = f"tier {spec.tier} (race ask)"
    else:
        how = f"tier {spec.tier} ({'ask size ' + spec.size if spec.size else 'default'})"
    eff = f", effort {spec.effort}" if spec.effort else ", no effort flag"
    name = f"{spec.model_alias} = " if spec.model_alias else ""
    return (f"# model: {name}{spec.model or spec.agent + ' default'}{eff} ({how})"
            + "".join(f"; {n}" for n in spec.model_notes))


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
            "session_path": None, "session_how": None, "session_sure": None, "recovered_from_session": False,
            "recover_skipped": None, "summary_text": "",
            "window": None}
    title = live.window_title(spec.agent, spec.title, spec.run_id)
    host = herdr.detect_host(cfg.live_terminal)
    is_herdr = host.startswith("herdr:")
    live.write_script(paths["live_sh"], run_dir=run_dir, cwd=lv["cmd"].cwd, env=env, argv=lv["cmd"].argv,
                      prompt_path=paths["prompt_live"], title=title, agent=spec.agent,
                      **({"herdr_host": True} if is_herdr else {}))
    profile = ""
    t0 = time.time()
    win = None
    if is_herdr:
        win = herdr.open_window(spec.run_id, lv["cmd"].cwd, paths["live_sh"], host.partition(":")[2],
                                cfg.runs_dir, run_dir)
        if not win["ok"]:
            reason = win["error"]
            print(f"ck-run: Herdr unavailable; falling back to iTerm path: {reason}", file=sys.stderr)
            ledger.append(cfg.runs_dir, **base, event="live_host_fallback", status="dispatched", note=reason)
            win = None
    if win is None:
        terminal = cfg.live_terminal if is_herdr else host
        if terminal.lower() in ("iterm", "iterm2"):
            _, new_file, perr = live.ensure_iterm_profiles(cfg.home)
            profile = "" if perr else live.profile_name(spec.agent)
            if new_file:
                time.sleep(1.5)
        if not is_herdr:
            t0 = time.time()  # preserve the original iTerm timing after profile setup
        group_kw = {}
        if spec.ask_id and _accepts(live.open_window, "group", "state_dir"):
            group_kw = {"group": spec.ask_id, "state_dir": os.path.join(cfg.runs_dir, WINDOWS_DIR)}
        win = live.open_window(cfg.live_osascript, f"/bin/bash {shlex.quote(paths['live_sh'])}", title, env,
                               terminal=terminal, profile=profile, **group_kw)
        # The script is shared with a possibly late hc launch; live.claim still permits only one agent.
        herdr.save(run_dir, {"host": terminal, "window": win})
    info["window"] = {"terminal": win["terminal"], "window_id": win["window_id"], "how": win["how"], "title": title,
                      "session_id": win.get("session_id"), "tab": win.get("tab"),
                      "herdr_terminal": win.get("herdr_terminal")}
    if not win["ok"]:
        live.take_claim(run_dir)  # a window that opens anyway later finds the claim and does nothing
        _live_fallback(cfg, spec, base, info, f"osascript could not open a window: {win['error']}")
        return info, None
    locked = bool(profile) and (win["how"] or "").startswith("profile ")
    ledger.append(cfg.runs_dir, **base, event="live_window_opened", status="dispatched",
                  note=(f"window_id={win['window_id'] or 'unknown'}; terminal={win['terminal']}; {win['how']}; "
                        + (f"a new tab in the window of ask {spec.ask_id}; " if win.get("tab") else "")
                        + f"title locked: {'yes' if locked else 'no'}; title: {title}")[:1000],
                  **{"from": "CK", "to": spec.agent})
    if not live.wait_started(run_dir, cfg.live_start_timeout_s, min(cfg.live_poll_s, 0.5)):
        tail = live.window_tail(cfg.live_osascript, win["window_id"], env) if win["terminal"] == "iTerm2" else (
            herdr.read(win["herdr_terminal"], lines=6) if win.get("herdr_terminal") else None)
        _live_fallback(cfg, spec, base, info,
                       f"the {win['terminal']} window (id {win['window_id'] or 'unknown'}) did not start the agent "
                       f"within {cfg.live_start_timeout_s:g} s" + (f"; the window shows: {tail}" if tail else ""))
        return info, None
    info["used"] = True
    # v4 (BUILD_SPEC_v4 10.5): the stall watchdog, when live.wait_done takes `watch` and config live.watchdog is on
    wait_kw = {}
    if cfg.live_watchdog and _accepts(live.wait_done, "watch"):
        wait_kw["watch"] = {
            "stall_s": float(spec.stall_s or cfg.live_stall_s), "osascript": cfg.live_osascript,
            "window_id": win["window_id"], "terminal": win["terminal"], "env": env, "runs_dir": cfg.runs_dir,
            "run_id": spec.run_id, "ask_id": spec.ask_id, "agent": spec.agent, "title": spec.title,
            # optional keys live.wait_done / watchdog.py read: the tab, HOME for the session stores, config values
            "session_id": win.get("session_id"), "herdr_terminal": win.get("herdr_terminal"), "home": cfg.home,
            "keep_awake": cfg.live_keep_awake, "close_tab_after_s": cfg.live_close_tab_after_s}
    old = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        w = live.wait_done(run_dir, t0 + int(spec.timeout_s), cfg.live_poll_s, **wait_kw)
    except KeyboardInterrupt:
        w = {"state": "interrupted", "exit_code": None}
    finally:
        signal.signal(signal.SIGTERM, old)
    if w["state"] in ("timeout", "interrupted", "stalled"):
        info["stop"] = live.stop(run_dir)  # the watchdog stops a stalled run itself; a second stop is harmless
    if w["state"] == "stalled":
        idle = w.get("idle_s") or w.get("stalled_s") or 2 * float(spec.stall_s or cfg.live_stall_s)
        info["watchdog_note"] = f"stopped by the watchdog: no activity for {float(idle):.0f} s"
    if w.get("watchdog") is not None:
        info["watchdog"] = w["watchdog"]  # what the watchdog saw and did (live.wait_done)
    info["state"] = w["state"]
    info["approval_stall"] = w.get("reason") == "approval"
    result_text = _read_text(paths["result_md"])
    if (w["state"] == "exited" and w["exit_code"] not in (None, 0, 130) and not result_text
            and time.time() - t0 < cfg.live_early_exit_s):
        info["used"] = False
        _live_fallback(cfg, spec, base, info, f"the agent CLI exited with code {w['exit_code']} in the window after "
                                              f"{time.time() - t0:.0f} s, before writing anything")
        return info, None
    info["summary_text"] = _read_text(paths["summary"], 200_000)
    sess = live.find_session(spec.agent, cfg.home, t0 - 5, spec.run_id, known_id=lv["session_id"])
    info.update(session_id=sess["id"], session_path=sess["path"], session_how=sess["how"], session_sure=sess["sure"])
    structured = summarize.extract_structured(result_text)
    if not (isinstance(structured, dict) and "summary" in structured):
        structured = None
    final_text = result_text
    if structured is None and sess["sure"]:
        got, text = live.recover_result(sess["path"], spec.run_id)
        if got is not None:
            structured, final_text = got, text
            info["recovered_from_session"] = True
    elif structured is None and sess["path"]:  # another run's session can quote this run's result
        info["recover_skipped"] = f"session {sess['id']} is not confirmed as this run's ({sess['how']})"
    parsed = {"final_text": final_text, "structured": structured, "actions": [], "observed_reads": [],
              "session_id": sess["id"], "cost_usd": None, "agent_error": None, "finished_reason": None,
              "text_only": True}
    info["exit_code"] = w.get("exit_code")
    # Once summary.md is written the work is done; how the user later leaves the CLI (Ctrl+C = 130) does not count.
    code = None if w["state"] == "done" else w.get("exit_code")
    # a stalled run (watchdog) ends like a timeout: status timeout, exit 3
    return info, (code, w["state"] in ("timeout", "stalled"), w["state"] == "interrupted", parsed)


def _accepts(fn, *names) -> bool:
    """True if `fn` takes all these keyword arguments (E2 adds them to live.py in v4; older live.py does not)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return True
    return all(n in params for n in names)


def _run_registered(cfg, spec, ws, scopes, cmd, prompt_text, env, cmd_txt, run_dir, paths, base, lv=None, *,
                    claim_paths=(), other_claims=(), held=None) -> int:
    """One run in its reserved folder, registered in the run registry (claims.py) by execute()."""
    started = ledger.now_iso()
    _write(paths["task"], spec.task_text)
    _write(paths["prompt"], prompt_text)
    _write(paths["cmd"], cmd_txt)
    # an auto run downgraded to read-only gets the read/edit deny rules: the whole workspace stays closed
    auto_ws = ws if spec.mode == "auto" and not spec.auto_readonly else None
    if spec.agent == "claude":
        write_claude_settings(cfg, paths["settings"], auto_ws=auto_ws, other_claims=other_claims)
    if lv:
        _write(paths["prompt_live"], lv["prompt"])
        if spec.agent == "claude":
            write_claude_settings(cfg, paths["settings_live"], live_run_dir=run_dir, auto_ws=auto_ws,
                                  other_claims=other_claims)
    packet = {
        "run_id": spec.run_id, "kind": spec.kind, "parent_run_id": spec.parent_run_id, "from": spec.from_,
        "workspace": ws.key, "workspace_root": ws.root, "lead": spec.lead, "task_type": spec.task_type,
        "title": spec.title, "worker": spec.agent, "model": spec.model, "cwd": cmd.cwd,
        "mode": {"read": "plan", "edit": "edit-approved", "auto": "auto"}[spec.mode],
        "approved_actions": [spec.approved] if spec.mode == "edit" else [],
        "write_scope": [s["path"] for s in scopes], "claims": list(claim_paths),
        "auto_readonly": spec.auto_readonly,
        "deliverable": "result.md + result.json + summary.md",
        "budget": {"timeout_s": int(spec.timeout_s)}, "review": spec.review, "task_source": spec.task_source,
        "created_at": started, "config_path": cfg.path, "ck_dispatch_version": VERSION}
    perr = validate(packet, load_schema(PACKET_SCHEMA))
    if perr:
        raise Refused(f"internal: packet does not match its schema: {perr[:3]}")
    _write(paths["packet"], json.dumps(packet, indent=2, ensure_ascii=False))
    ledger.append(cfg.runs_dir, **base, event=spec.kind, status="dispatched",
                  prompt_path=paths["prompt_live"] if lv else paths["prompt"], response_path=paths["result_md"],
                  summary_path=paths["summary"], write_scope=packet["write_scope"], started_at=started,
                  note="; ".join(n for n in [
                      f"auto_readonly: {spec.auto_readonly} (none of the writable folders of {ws.name} exists; "
                      f"auto mode, no write area)" if spec.auto_readonly else None,
                      f"live: interactive CLI in an {cfg.live_terminal} window (headless if the window fails)"
                      if lv else None] if n) or None,
                  **{"from": spec.from_, "to": spec.agent})

    # v4 (chalach 2026-11-05, decision 7): the post-run review of cursor/agy auto runs only when post_review.enabled
    review_on = postreview.applies(spec.agent, spec.mode, spec.kind)
    reviewed = review_on and cfg.post_review_enabled
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
        on_start = (lambda pid: held.update(agent_pid=pid)) if held is not None else None
        exit_code, timed_out, interrupted = run_child(cmd, prompt_text, env, run_dir, int(spec.timeout_s), on_start)
        parsed = summarize.parse(spec.agent, run_dir)
        stdout = _read_text(os.path.join(run_dir, "stdout.log"))
        stderr = _read_text(os.path.join(run_dir, "stderr.log"), 200_000)
    else:
        exit_code, timed_out, interrupted, parsed = outcome
        stdout, stderr = parsed["final_text"], ""  # text only: the rule check scans the agent's result text
    return _finish(cfg, spec, ws, scopes, run_dir, paths, base, started=started, t0=t0, marker_ns=marker_ns,
                   roots=roots, snap=snap, reviewed=reviewed, review_off=review_on and not reviewed,
                   exit_code=exit_code, timed_out=timed_out, interrupted=interrupted, parsed=parsed, stdout=stdout,
                   stderr=stderr, live_info=live_info, claim_paths=claim_paths)


def _finish(cfg, spec, ws, scopes, run_dir, paths, base, *, started, t0, marker_ns, roots, snap, reviewed,
            review_off, exit_code, timed_out, interrupted, parsed, stdout, stderr, live_info, claim_paths,
            end_ts=None, finalized=None, extra_notes=()) -> int:
    """After the agent: the rule check (and the post-run review when it is on), the status, result.json, result.md,
    summary.md, the ledger `result` event and the summary on stdout; returns the exit code. finalize_run uses it too:
    end_ts = when the agent's last output was written, finalized = its note."""
    is_live = bool(live_info and live_info["used"])
    end = end_ts or time.time()
    duration = round(end - t0, 1)
    finished = datetime.fromtimestamp(end).astimezone().isoformat(timespec="seconds") if end_ts else ledger.now_iso()

    scan = rulecheck.scan_changed(roots, marker_ns, cfg.scan_budget_s, cfg.scan_max_entries)
    # Other runs may have worked at the same time (no workspace lock): split the scan's changes between them.
    others = claims.concurrent(cfg, spec.run_id, marker_ns / 1e9, end_ts or time.time())
    att = claims.attribute(cfg, ws, spec.check_mode, scopes, list(claim_paths), scan["changed"], others)
    cls = rulecheck.classify(cfg, ws, spec.check_mode, scopes, att["mine"], run_dir, auto_readonly=spec.auto_readonly)
    tr = rulecheck.scan_transcript(parsed, stdout, stderr, agent=spec.agent, mode=spec.mode)
    rc = {"violations": cls["violations"] + tr["violations"], "flags": cls["flags"] + tr["flags"],
          "notes": cls["notes"] + tr.get("notes", []) + att["notes"], "changed_in_scope": cls["changed_in_scope"],
          "scan": {"roots": roots, "entries": scan["entries"],
                   "truncated": scan["truncated"], "seconds": scan["seconds"]},
          "claims": list(claim_paths),
          "concurrent": [{k: o.get(k) for k in ("run_id", "agent", "workspace", "mode", "claims", "finished")}
                         for o in others],
          "changed_by_others": att["theirs"], "auto_readonly": spec.auto_readonly}
    if spec.auto_readonly:
        rc["notes"].insert(0, f"auto_readonly: {spec.auto_readonly}: none of the writable folders of {ws.name} existed "
                              f"when the run started, so it ran in auto mode with no write area")
    if spec.check_mode in claims.WRITER_MODES and others:
        st = parsed["structured"] if isinstance(parsed["structured"], dict) else {}
        mine_rep = [_norm(f, ws.root) for f in st.get("files_changed") or [] if isinstance(f, str) and f.strip()]
        rc["flags"] += claims.same_file(cfg, mine_rep, others)

    review = None
    if reviewed:  # cursor/agy auto never asked before acting: mandatory read-only Claude check (postreview.py)
        try:
            sess = postreview.session_info(spec.agent, cfg.home, t0 - 5, spec.run_id, live_info,
                                           known_id=None if is_live else parsed.get("session_id"))
            review = postreview.run(
                cfg, agent=spec.agent, run_id=spec.run_id, ws=ws, scopes=scopes, run_dir=run_dir,
                run_paths={"result_md": paths["result_md"],
                           "stdout": None if is_live else os.path.join(run_dir, "stdout.log")},
                snap=snap, roots=roots, changed=att["mine"], rc=rc, concurrent=others,
                foreign=lambda p: claims.owner(cfg, ws, spec.check_mode, scopes, list(claim_paths), p, others)[0] is not None,
                session_path=sess["path"], session_how=sess["how"], session_sure=sess["sure"],
                text="\n".join([parsed.get("final_text") or "", "" if is_live else stdout[-200_000:], stderr[-50_000:]]),
                write_settings=lambda p: write_claude_settings(cfg, p), readonly=spec.auto_readonly)
        except Exception as e:  # never let the check crash ck-run; an unchecked run is a flagged run
            review = postreview.failed(spec.agent, run_dir, e)
        ledger.append(cfg.runs_dir, **dict(base, via="CK"), event=review["event"], status=review["status"],
                      prompt_path=None, response_path=review["review_md"], summary_path=None,
                      files_changed=rc["changed_in_scope"], write_scope=[s["path"] for s in scopes],
                      duration_s=review["seconds"],
                      note=(f"{review['status']}: {postreview.headline(review) if review['status'] == 'flagged' else review['summary']}")[:1000],
                      **{"from": "claude", "to": "CK"})
        if review["status"] == "ok" and review.get("actions"):
            # Text-only mentions of credential/cloud_sim/permission_bypass were flags because Cursor/agy give no
            # tool log on stdout (other categories are notes already). The review read the agent's real tool
            # calls from its own (confirmed) session and found nothing: keep them as notes.
            text_only = [f for f in rc["flags"] if rulecheck.TEXT_FLAG in f]
            rc["flags"] = [f for f in rc["flags"] if f not in text_only]
            rc["notes"] += [f"checked by the post-run review ({review['actions']} tool calls, ok): {f}" for f in text_only]
        if review["status"] == "flagged":
            rc["flags"].append(f"post-run Claude review flagged the run: {postreview.headline(review)} "
                               f"(see {review['review_md']})")
            print(postreview.loud_warning(spec.agent, spec.run_id, review), file=sys.stderr)
            sys.stderr.flush()
    rc["clean"] = not rc["violations"] and not rc["flags"]

    structured = parsed["structured"] if isinstance(parsed["structured"], dict) else None
    schema_errors = validate(structured, load_schema(RESULT_SCHEMA)) if structured else ["no result JSON found"]
    v4_missing = []
    if structured:  # v4 (2026-11-05): a result without the new fields still counts; the finish card shows less
        schema_errors, v4_missing = summarize.split_v4_errors(schema_errors)
    agent_summary = (live_info or {}).get("summary_text", "").strip() if is_live else ""
    if agent_summary:  # live: the agent wrote summary.md itself
        lines, src, sum_note = summarize.summary_lines({"summary": agent_summary}, "", paths["result_md"])
    else:
        lines, src, sum_note = summarize.summary_lines(structured, parsed["final_text"], paths["result_md"])
    if review and review["status"] == "flagged":  # CK reads summary.md first: the warning goes on top
        lines = [f"FLAGGED by the post-run Claude review: {postreview.headline(review)[:120]} (review.md)"] + lines[:9]
    notes = [n for n in [f"finalized by ck-run reattach: {finalized}" if finalized is not None else None,
                         f"auto_readonly: {spec.auto_readonly} (auto mode, no write area)" if spec.auto_readonly
                         else None, sum_note] if n]
    if (spec.over_info or {}).get("over"):
        notes.append(f"over the ask's budget ({'; '.join(spec.over_info.get('reasons') or [])}), passed with "
                     f"chalach's words: \"{spec.over_info.get('approved')}\"")
    notes += list(spec.model_notes or [])
    if live_info and not is_live:
        notes.append(f"live window not used ({live_info['window_error']}); ran headless")
    if is_live:
        notes.append(f"live: ran in a {(live_info.get('window') or {}).get('terminal') or 'terminal'} window; state {live_info['state']}; session "
                     f"{live_info['session_id'] or 'unknown'} ({live_info['session_how']})")
        if live_info["recovered_from_session"]:
            notes.append("result recovered from the session log: the agent did not write it to result.md")
        if live_info.get("recover_skipped"):
            notes.append(f"result not recovered from the session log: {live_info['recover_skipped']}")
        if live_info.get("watchdog_note"):
            notes.append(live_info["watchdog_note"])
        if not agent_summary and live_info["state"] not in ("timeout", "stalled"):
            notes.append("the agent did not write summary.md")
    if review_off:
        notes.append(POST_REVIEW_OFF)
    notes += list(extra_notes)
    if v4_missing:
        notes.append(f"the result has no v4 field(s) {', '.join(v4_missing)}: the finish card shows less for this run")
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
    approval = status == "timeout" and (live_info or {}).get("approval_stall", False)
    herdr.label(run_dir, herdr.circle_status(status, problems), approval=bool(approval))

    if status in ("failed", "partial", "timeout") and (exit_code != 0 or parsed["agent_error"]):
        msg = (stderr[-4000:] + "\n" + (parsed["agent_error"] or ""))
        if route.RATE_LIMIT_RX.search(msg):
            lim = route.mark_limited(cfg.runs_dir, spec.agent, msg)
            notes.append(f"{spec.agent} looks rate-limited until {lim['until']} (saved in state.json)")

    rec = {
        "run_id": spec.run_id, "kind": spec.kind, "parent_run_id": spec.parent_run_id, "title": spec.title,
        "agent": spec.agent, "model": spec.model, "workspace": ws.key, "mode": spec.mode,
        "ask_id": spec.ask_id, "size": spec.size, "pattern": spec.pattern, "project": spec.project,
        "tier": spec.tier, "model_alias": spec.model_alias, "effort": spec.effort, "trigger": spec.trigger,
        "over_budget": spec.over_info if (spec.over_info or {}).get("over") else None,
        "v4": summarize.v4_fields(structured), "v4_missing": v4_missing,
        "post_review_off": bool(review_off), "finalized": finalized,
        "auto_readonly": spec.auto_readonly, "status": status, "agent_status": agent_status, "exit_code": exit_code, "timed_out": timed_out,
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
        "post_review": ({k: review.get(k) for k in ("status", "summary", "review_md", "findings", "error", "seconds",
                                                    "session")}
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
        # after a failed window, or when finalizing a run whose ck-run died, a stray file may exist
        put = _write_replace if (live_info or finalized is not None) else _write
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


def review_spec(cfg: Config, orig_run_id: str, by: str | None, *, blind: bool, focus: str | None,
                title: str | None, model: str | None, timeout_s: int | None, live: bool = False,
                ask_id: str | None = None, trigger: str | None = None, tier: str | None = None,
                over: str | None = None) -> Spec:
    """v4: --by may be left out when --model is an alias (the alias picks the agent); without --ask the review
    belongs to the ask of the reviewed run; --trigger is the review reason (asks.gate checks it)."""
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
    if by is None:
        by = models.agent_for(model)  # Refused unless --model is an alias
    if by == orig_agent:
        raise Refused(f"cross-review needs a different agent than {orig_agent}")
    ask_id = ask_id or op.get("ask_id")
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
                read_dirs=[] if blind else [run_dir], live=live, ask_id=ask_id, trigger=trigger, tier=tier,
                over=over)


# ---- v4: finish a run whose ck-run watcher died (BUILD_SPEC_v4 10.6; `ck-run reattach` calls it) ---------------

SESSION_ID_RX = re.compile(r"--session-id['\"]?\s+['\"]?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_int_file(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _iso_ts(text):
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError):
        return None


def print_result(res: dict) -> int:
    """Print a result event's summary path and summary (like a finished run) and return its exit code."""
    p = res.get("summary_path")
    lines = _read_text(p, 200_000).splitlines()[:10] if p else []
    sys.stdout.write((p or "(no summary.md)") + "\n" + "\n".join(lines) + "\n")
    sys.stdout.flush()
    return EXIT.get(res.get("status"), EXIT["failed"])


def finalize_run(cfg: Config, run_id: str, *, note: str) -> int:
    """Finish a run whose ck-run watcher died, from what is on disk: packet.json, the agent's result.md / summary.md
    (live) or stdout.log (headless), the agent session (live.find_session); the normal rule check (the post-run review
    is not run), the `result` event (note "finalized by ck-run reattach: <note>"), the summary on stdout; returns the
    exit code. Idempotent: a run that has a result event prints it and returns its code. Refused while the agent (or
    the ck-run that watches it) still runs: "agent still running"."""
    if not valid_run_id(run_id):
        raise Refused(f"not a valid run id: {run_id}")
    run_dir = os.path.join(cfg.runs_dir, run_id)
    if not os.path.isdir(run_dir):
        raise Refused(f"run {run_id} has no run folder")
    fd = os.open(os.path.join(run_dir, ".finalize.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # two reattach calls at once: the second one prints the first one's result
        r = ledger.find_run(cfg.runs_dir, run_id)
        if r is None:
            raise Refused(f"run {run_id} is not in the ledger")
        res = r.last("result")
        if res is not None:
            return print_result(res)
        op = r.first("dispatch", "review")
        if op is None:
            raise Refused(f"run {run_id} never started (no dispatch or review event)")
        live_records, _ = claims.active(cfg.runs_dir)
        rec = next((x for x in live_records if x.get("run_id") == run_id), None)
        if rec is not None and rec.get("orphan"):
            raise Refused(f"agent still running (pid {rec.get('agent_pid')}); finalize the run after it ends")
        if rec is not None:
            raise Refused(f"agent still running: its ck-run (pid {rec.get('pid')}) still watches run {run_id}")
        if (not os.path.exists(os.path.join(run_dir, "live.exit"))
                and _pid_alive(_read_int_file(os.path.join(run_dir, "live.pid")))):
            raise Refused(f"agent still running (live window, pid {_read_int_file(os.path.join(run_dir, 'live.pid'))})")
        return _finalize(cfg, r, op, run_dir, note)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _finalize(cfg: Config, r, op: dict, run_dir: str, note: str) -> int:
    run_id = r.run_id
    paths = {k: os.path.join(run_dir, v) for k, v in {
        "prompt": "prompt.md", "task": "task.md", "result_md": "result.md", "result_json": "result.json",
        "summary": "summary.md", "settings": "claude-settings.json", "packet": "packet.json",
        "cmd": "cmd.txt", "rulecheck": "rulecheck.json", "marker": ".marker",
        "prompt_live": "prompt-live.md", "settings_live": "claude-settings-live.json", "live_sh": "live.sh"}.items()}
    try:
        with open(paths["packet"], encoding="utf-8") as f:
            packet = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f"run {run_id}: cannot read packet.json ({e})")
    ws = cfg.workspace(op.get("workspace") or packet.get("workspace") or "")
    mode = {"plan": "read", "edit-approved": "edit", "auto": "auto"}.get(packet.get("mode"), op.get("mode") or "read")
    kind = packet.get("kind") or ("review" if op.get("event") == "review" else "dispatch")
    spec = Spec(agent=op.get("agent") or packet.get("worker"), ws=ws, title=op.get("title") or packet.get("title") or "",
                task_text=_read_text(paths["task"]), task_source=packet.get("task_source"),
                task_type=op.get("task_type") or packet.get("task_type") or "general", mode=mode,
                approved=op.get("approved"), timeout_s=(packet.get("budget") or {}).get("timeout_s"),
                parent_run_id=op.get("parent_run_id"), kind=kind, from_=op.get("from") or "CK",
                review=packet.get("review"), ask_id=op.get("ask_id"), trigger=op.get("trigger"))
    spec.run_id = run_id
    spec.auto_readonly = packet.get("auto_readonly") or op.get("auto_readonly")
    spec.model, spec.model_alias, spec.tier = op.get("model") or packet.get("model"), op.get("model_alias"), op.get("tier")
    spec.size, spec.pattern, spec.project = op.get("size"), op.get("pattern"), op.get("project")
    spec.model_explicit = False
    scopes = [{"path": p, "literal": p, "area": "workspace" if inside(p, ws.real_root) else "local"}
              for p in packet.get("write_scope") or []]
    claim_paths = list(packet.get("claims") or op.get("claims") or [])
    base = {"run_id": run_id, "parent_run_id": spec.parent_run_id, "title": spec.title, "workspace": ws.key,
            "task_type": spec.task_type, "agent": spec.agent, "model": spec.model, "mode": spec.mode,
            "approved": spec.approved, "via": "CK" if kind == "review" else None, "ask_id": spec.ask_id,
            "trigger": spec.trigger, "size": spec.size, "pattern": spec.pattern, "tier": spec.tier,
            "project": spec.project, "model_alias": spec.model_alias, "claims": claim_paths,
            "auto_readonly": spec.auto_readonly}
    started = op.get("started_at") or packet.get("created_at") or op.get("ts")
    try:
        marker_ns = os.stat(paths["marker"]).st_mtime_ns
    except OSError:
        marker_ns = int((_iso_ts(started) or time.time()) * 1e9)
    t0 = _iso_ts(started) or marker_ns / 1e9
    outputs = [paths["summary"], paths["result_md"], os.path.join(run_dir, "live.exit"),
               os.path.join(run_dir, "stdout.log"), os.path.join(run_dir, "last.md")]
    stamps = [os.stat(x).st_mtime for x in outputs if os.path.exists(x)]
    end_ts = max(stamps) if stamps else time.time()
    is_live = os.path.exists(os.path.join(run_dir, "live.pid")) and r.last("live_fallback") is None
    extra = ["the post-run review was not run (finalized after ck-run's watcher died)"
             if postreview.applies(spec.agent, spec.mode, spec.kind) and cfg.post_review_enabled else None]
    if is_live:
        result_text = _read_text(paths["result_md"])
        summary_text = _read_text(paths["summary"], 200_000)
        exit_code = _read_int_file(os.path.join(run_dir, "live.exit"))
        m = SESSION_ID_RX.search(_read_text(paths["live_sh"]) + "\n" + _read_text(paths["cmd"]))
        sess = live.find_session(spec.agent, cfg.home, t0 - 5, run_id, known_id=m.group(1) if m else None)
        structured = summarize.extract_structured(result_text)
        if not (isinstance(structured, dict) and "summary" in structured):
            structured = None
        final_text, recovered, skipped = result_text, False, None
        if structured is None and sess["sure"]:
            got, text = live.recover_result(sess["path"], run_id)
            if got is not None:
                structured, final_text, recovered = got, text, True
        elif structured is None and sess["path"]:
            skipped = f"session {sess['id']} is not confirmed as this run's ({sess['how']})"
        state = "done" if summary_text.strip() else "exited"
        live_info = {"requested": True, "used": True, "window_error": None, "state": state,
                     "session_id": sess["id"], "session_path": sess["path"], "session_how": sess["how"],
                     "session_sure": sess["sure"], "recovered_from_session": recovered, "recover_skipped": skipped,
                     "summary_text": summary_text, "window": herdr.saved(run_dir).get("window"), "exit_code": exit_code}
        parsed = {"final_text": final_text, "structured": structured, "actions": [], "observed_reads": [],
                  "session_id": sess["id"], "cost_usd": None, "agent_error": None, "finished_reason": None,
                  "text_only": True}
        code = None if state == "done" else exit_code
        stdout, stderr = final_text, ""
    else:
        parsed = summarize.parse(spec.agent, run_dir)
        stdout = _read_text(os.path.join(run_dir, "stdout.log"))
        stderr = _read_text(os.path.join(run_dir, "stderr.log"), 200_000)
        live_info, code = None, None  # the headless agent's exit code died with its ck-run
        extra.append("headless run: the agent's exit code is unknown (its ck-run died)")
    rc = _finish(cfg, spec, ws, scopes, run_dir, paths, base, started=started, t0=t0, marker_ns=marker_ns,
                 roots=rulecheck.scan_roots(ws, scopes), snap=None, reviewed=False,
                 review_off=postreview.applies(spec.agent, spec.mode, spec.kind) and not cfg.post_review_enabled,
                 exit_code=code, timed_out=False, interrupted=False, parsed=parsed, stdout=stdout, stderr=stderr,
                 live_info=live_info, claim_paths=claim_paths, end_ts=end_ts, finalized=(note or "").strip(),
                 extra_notes=[x for x in extra if x])
    # this run's registry record is stale now: remove it quietly (its result is logged); log the others' as usual
    _, stale = claims.active(cfg.runs_dir, clean=True)
    claims.log_stale(cfg.runs_dir, [x for x in stale if x.get("run_id") != run_id], by=f"ck-run reattach ({run_id})")
    return rc
