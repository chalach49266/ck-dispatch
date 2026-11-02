#!/usr/bin/env python3
"""Step-3 live probe: run ONE agent CLI with ck-run's exact read-mode argv and clean env
(flags.build + guards.child_env), but with a tiny prompt instead of the full rules header.

  python3 -I docs/step3/probe.py LABEL AGENT WS PROMPT_FILE [--read-dir D] [--extra-deny RULE] [--timeout S]

Writes docs/step3/probes/LABEL/{cmd.txt,stdout.log,stderr.log,claude-settings.json,report.json}
and prints a short report. Tool-result CONTENT is never printed (only errors, cut to 200 chars).
Read mode only: there is no option for edit mode here.
"""
import argparse
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, REPO)
sys.dont_write_bytecode = True

from ckdispatch import flags, runner, summarize  # noqa: E402
from ckdispatch.config import RESULT_SCHEMA, Config  # noqa: E402
from ckdispatch.guards import child_env  # noqa: E402


def claude_events(stdout: str) -> dict:
    rep = {"init": None, "tool_uses": [], "tool_results": []}
    for ev in summarize._json_lines(stdout):
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            rep["init"] = {k: ev.get(k) for k in ("session_id", "cwd", "model", "permissionMode", "mcp_servers",
                                                  "apiKeySource", "memory_paths")}
            rep["init"]["tools"] = ev.get("tools")
        elif t == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    rep["tool_uses"].append({"name": c.get("name"),
                                             "path": inp.get("file_path") or inp.get("path") or inp.get("pattern")})
        elif t == "user":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    body = c.get("content")
                    text = body if isinstance(body, str) else json.dumps(body)
                    if c.get("is_error"):
                        rep["tool_results"].append({"error": True, "text": text[:200]})
                    else:
                        rep["tool_results"].append({"error": False, "chars": len(text)})
        elif t == "result":
            rep["result_keys"] = sorted(ev.keys())
            rep["has_structured_output"] = isinstance(ev.get("structured_output"), dict)
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("label")
    ap.add_argument("agent", choices=["claude", "cursor", "agy"])  # codex: blocked by the merge-gate hook
    ap.add_argument("ws")
    ap.add_argument("prompt_file")
    ap.add_argument("--read-dir", action="append", default=[])
    ap.add_argument("--extra-deny", action="append", default=[])
    ap.add_argument("--timeout", type=int, default=240)
    a = ap.parse_args()

    cfg = Config()
    ws = cfg.workspace(a.ws)
    pdir = os.path.join(REPO, "docs", "step3", "probes", a.label)
    os.makedirs(pdir, mode=0o700, exist_ok=False)
    settings = os.path.join(pdir, "claude-settings.json")
    read_dirs = [os.path.realpath(os.path.expanduser(d)) for d in a.read_dir]
    cmd = flags.build(cfg, ws, a.agent, mode="read", scopes=[], model=None, run_dir=pdir,
                      schema_path=RESULT_SCHEMA, extra_read_dirs=read_dirs, timeout_s=a.timeout,
                      claude_settings_path=settings)
    if a.agent == "claude":
        runner.write_claude_settings(cfg, settings)
        if a.extra_deny:
            with open(settings, encoding="utf-8") as f:
                s = json.load(f)
            s["permissions"]["deny"] += a.extra_deny
            with open(settings, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=1)
    with open(a.prompt_file, encoding="utf-8") as f:
        prompt = f.read()
    env = child_env(cfg)
    with open(os.path.join(pdir, "cmd.txt"), "w", encoding="utf-8") as f:
        f.write(flags.redacted(cmd, RESULT_SCHEMA, os.path.realpath(a.prompt_file)) + "\n")
    t0 = time.time()
    code, timed_out, interrupted = runner.run_child(cmd, prompt, env, pdir, a.timeout)
    dur = round(time.time() - t0, 1)
    parsed = summarize.parse(a.agent, pdir)
    stdout = runner._read_text(os.path.join(pdir, "stdout.log"))
    stderr = runner._read_text(os.path.join(pdir, "stderr.log"), 200_000)
    rep = {"label": a.label, "agent": a.agent, "ws": ws.key, "cwd": cmd.cwd, "exit": code,
           "timed_out": timed_out, "seconds": dur, "session_id": parsed["session_id"],
           "agent_error": parsed["agent_error"], "finished_reason": parsed.get("finished_reason"),
           "structured_found": isinstance(parsed["structured"], dict),
           "structured_summary": (parsed["structured"] or {}).get("summary") if isinstance(parsed["structured"], dict) else None,
           "final_text_head": (parsed["final_text"] or "")[:1200],
           "stderr_tail": stderr[-800:]}
    if a.agent == "claude":
        rep["claude"] = claude_events(stdout)
    else:
        obj = summarize._whole_json(stdout)
        rep["stdout_top_keys"] = sorted(obj.keys()) if obj else None
        rep["stdout_scalars"] = {k: v for k, v in (obj or {}).items()
                                 if isinstance(v, (int, float, bool)) or (isinstance(v, str) and len(v) < 120)}
    with open(os.path.join(pdir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1, ensure_ascii=False, default=str)
    print(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
