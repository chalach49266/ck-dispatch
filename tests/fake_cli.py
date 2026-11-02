"""Fake agent CLI for the offline tests. No network, no real agent.

The test sandbox copies this file (with an absolute shebang) as claude, codex, agent and agy.
Behaviour comes from control.json next to the copy: {"default": {...}, "<agent>": {...}}.
Every call is recorded in calls/NNN-<agent>.json (argv, cwd, env, prompt, pid).
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
NAME = os.path.basename(sys.argv[0])
AGENT = "cursor" if NAME == "agent" else NAME


def control():
    try:
        with open(os.path.join(HERE, "control.json")) as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    out = dict(c.get("default", {}))
    out.update(c.get(AGENT, {}))
    return out


def arg_after(flag):
    a = sys.argv
    for i, t in enumerate(a):
        if t == flag and i + 1 < len(a):
            return a[i + 1]
    return None


def get_prompt():
    if AGENT in ("claude", "codex"):
        return sys.stdin.read()
    if AGENT == "agy":
        return arg_after("-p") or ""
    return sys.argv[-1]


def parse_prompt(p):
    run_id, first, extra = "", [], []
    for line in p.splitlines():
        if line.startswith("RUN: "):
            run_id = line[5:].split(" |")[0].strip()
        if line.startswith("FIRST: read "):
            rest = line[len("FIRST: read "):].split(", then the rule files")[0]
            first = [x.strip() for x in rest.split(", ")]
        if line.startswith("- result to review: "):
            extra.append(line[len("- result to review: "):].strip())
    return run_id, first, extra


def result(ctrl, run_id, first, extra):
    r = {
        "run_id": run_id,
        "status": ctrl.get("status", "ok"),
        "summary": ctrl.get("summary", [f"Stub {AGENT} finished the task.", "Evidence: " + (first[0] if first else "none")]),
        "report_md": ctrl.get("report", "Full stub report. UNIQUE-RESULT-MARKER-7f3a"),
        "findings": ctrl.get("findings", [{"claim": "stub claim", "evidence": "stub evidence", "severity": "info"}]),
        "files_read": ctrl.get("files_read", first) + extra,
        "files_changed": [],
        "numbers": ctrl.get("numbers", []),
        "proposed_changes": "",
        "open_questions": [],
        "needs_approval": [],
    }
    for k in ctrl.get("drop", []):
        r.pop(k, None)
    return r


def main():
    ctrl = control()
    prompt = get_prompt()
    calls = os.path.join(HERE, "calls")
    os.makedirs(calls, exist_ok=True)
    rec = {"agent": AGENT, "argv": sys.argv, "cwd": os.getcwd(), "env": dict(os.environ), "prompt": prompt,
           "pid": os.getpid()}
    n = len(os.listdir(calls))
    with open(os.path.join(calls, f"{n:03d}-{AGENT}.json"), "w") as f:
        json.dump(rec, f)
    for d in ctrl.get("mkdir", []):
        os.makedirs(d, exist_ok=True)
    for p in ctrl.get("write", []):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            f.write("stub edit\n")
    if ctrl.get("stderr"):
        sys.stderr.write(ctrl["stderr"] + "\n")
        sys.stderr.flush()
    if ctrl.get("sleep"):
        time.sleep(ctrl["sleep"])
    run_id, first, extra = parse_prompt(prompt)
    res = result(ctrl, run_id, first, extra)
    mode = ctrl.get("output", "json")
    final = json.dumps(res) if mode == "json" else ctrl.get("text", "plain text answer with no JSON")
    if mode == "none":
        final = ""
    if AGENT == "claude":
        print(json.dumps({"type": "system", "subtype": "init", "session_id": "stub-claude"}))
        reads = (first if mode == "json" else []) + ctrl.get("read_paths", [])
        for f in reads:
            print(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": f}}]}}))
        for c in ctrl.get("bash", []):
            print(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": c}}]}}))
        ev = {"type": "result", "subtype": "success", "is_error": False, "result": final,
              "session_id": "stub-claude", "total_cost_usd": 0.01}
        if mode == "json":
            ev["structured_output"] = res
        if mode != "none":
            print(json.dumps(ev))
    elif AGENT == "codex":
        print(json.dumps({"type": "thread.started", "thread_id": "stub-codex"}))
        for c in ["sed -n 1,80p index.md"] + ctrl.get("bash", []):
            print(json.dumps({"type": "item.completed", "item": {"type": "command_execution",
                                                                 "command": f"bash -lc '{c}'", "exit_code": 0}}))
        if final:
            print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": final}}))
            out = arg_after("-o")
            if out:
                with open(out, "w") as f:
                    f.write(final)
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
    elif AGENT == "cursor":
        text = f"Here is my answer.\n```json\n{final}\n```" if mode == "json" else final
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                          "session_id": "stub-cursor"}))
    else:  # agy
        print(json.dumps({"response": final, "finished_reason": ctrl.get("finished_reason", "stop")}))
    sys.stdout.flush()
    sys.exit(int(ctrl.get("exit", 0)))


if __name__ == "__main__":
    main()
