"""Fake agent CLI for the offline tests. No network, no real agent.

The test sandbox copies this file (with an absolute shebang) as claude, codex, agent and agy.
Behaviour comes from control.json next to the copy: {"default": {...}, "<agent>": {...}}.
Every call is recorded in calls/NNN-<agent>.json (argv, cwd, env, prompt, pid, live).
A claude call whose prompt starts with "CK POST-RUN REVIEW" acts as ck-run's post-run reviewer (main_review).
Interactive argv (ck-run --live): main_live() writes result.md + summary.md into the prompt's OUTBOX and a fake
session file under $HOME; control keys under "live" apply only there (write_outbox, session_answer, sleep, exit).
"""
import json
import os
import sys
import time
import uuid

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


def is_live():
    """Interactive argv (ck-run --live): no -p / exec; agy gets the prompt with -i."""
    if AGENT == "codex":
        return "exec" not in sys.argv
    if AGENT == "agy":
        return "-i" in sys.argv
    return "-p" not in sys.argv


def get_prompt():
    if is_live():
        return arg_after("-i") if AGENT == "agy" else sys.argv[-1]
    if AGENT in ("claude", "codex"):
        return sys.stdin.read()
    if AGENT == "agy":
        return arg_after("-p") or ""
    return sys.argv[-1]


def parse_outbox(p):
    for line in p.splitlines():
        if line.startswith("OUTBOX: "):
            return line[len("OUTBOX: "):].strip()
    return None


def write_session(ctrl, prompt, answer):
    """Fake each CLI's session store under $HOME (paths as in docs/step3/results.md)."""
    if not ctrl.get("session", True):
        return
    home = os.environ["HOME"]
    sid = arg_after("--session-id") or str(uuid.uuid4())
    user = {"role": "user", "content": [{"type": "text", "text": prompt}]}
    lines = [{"type": "user", "message": user}]
    if answer:
        lines.append({"type": "assistant", "message": {"role": "assistant",
                                                       "content": [{"type": "text", "text": answer}]}})
    if AGENT == "claude":
        path = os.path.join(home, ".claude", "projects", "-fake-workspace", f"{sid}.jsonl")
    elif AGENT == "codex":
        now = time.localtime()
        path = os.path.join(home, ".codex", "sessions", time.strftime("%Y/%m/%d", now),
                            f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S', now)}-{sid}.jsonl")
        lines = [{"type": "session_meta", "payload": {"id": sid, "cwd": os.getcwd()}}] + [
            {"type": "response_item", "payload": {"type": "message", **m["message"]}} for m in lines]
    elif AGENT == "cursor":
        path = os.path.join(home, ".cursor", "projects", "fake-workspace", "agent-transcripts", sid, f"{sid}.jsonl")
    else:
        path = os.path.join(home, ".gemini", "antigravity-cli", "conversations", f"{sid}.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        if AGENT == "agy":
            f.write("SQLite format 3\0" + prompt)
        else:
            f.write("".join(json.dumps(x) + "\n" for x in lines))


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
        "files_changed": ctrl.get("files_changed", []),
        "numbers": ctrl.get("numbers", []),
        "proposed_changes": "",
        "open_questions": [],
        "needs_approval": [],
    }
    for k in ctrl.get("drop", []):
        r.pop(k, None)
    return r


def main_live(ctrl, prompt):
    """Interactive mode as the tests see it: do the task, write result.md + summary.md to the OUTBOX."""
    lc = dict(ctrl, **ctrl.get("live", {}))
    for d in lc.get("mkdir", []):
        os.makedirs(d, exist_ok=True)
    for p in lc.get("write", []):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            f.write("stub edit\n")
    run_id, first, extra = parse_prompt(prompt)
    res = result(lc, run_id, first, extra)
    answer = f"Done.\n```json\n{json.dumps(res)}\n```" if lc.get("session_answer") else ""
    write_session(lc, prompt, answer)
    if lc.get("sleep"):
        time.sleep(lc["sleep"])
    outbox = parse_outbox(prompt)
    if outbox and lc.get("write_outbox", True):
        with open(os.path.join(outbox, "result.md"), "w") as f:
            f.write(f"# Live answer\n\n{res['report_md']}\n\n```json\n{json.dumps(res)}\n```\n")
        with open(os.path.join(outbox, "summary.md"), "w") as f:
            f.write("\n".join(lc.get("summary_file", res["summary"])) + "\n")
    if lc.get("linger"):
        time.sleep(lc["linger"])
    sys.exit(int(lc.get("exit", 0)))


def write_tool_session(ctrl, prompt):
    """Headless cursor/agy: a session store holding the prompt and ctrl["tool_calls"] ([[name, {input}], ...]),
    in each CLI's real format (cursor JSONL transcript; agy SQLite steps with protobuf-like blobs)."""
    calls = ctrl.get("tool_calls")
    if calls is None:
        return
    home, sid = os.environ["HOME"], str(uuid.uuid4())
    if AGENT == "cursor":
        path = os.path.join(home, ".cursor", "projects", "fake-workspace", "agent-transcripts", sid, f"{sid}.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": prompt}]}}) + "\n")
            for name, inp in calls:
                f.write(json.dumps({"role": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": name, "input": inp}]}}) + "\n")
    elif AGENT == "agy":
        import sqlite3
        path = os.path.join(home, ".gemini", "antigravity-cli", "conversations", f"{sid}.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE steps (idx integer, step_type integer, step_payload blob)")
        con.execute("INSERT INTO steps VALUES (0, 14, ?)", (b"\x0a\x02" + prompt.encode(),))
        for i, (name, inp) in enumerate(calls, 1):
            payload = (b"\x12\x08" + f"call_{i}00".encode() + b"\x1a\x0b" + name.encode() + b"\x12\x01"
                       + json.dumps(inp).encode() + b"\x01\x02")
            con.execute("INSERT INTO steps VALUES (?, 132, ?)", (i, payload))
        con.commit()
        con.close()


def main_review(ctrl, prompt):
    """claude as ck-run's post-run reviewer (cursor/agy auto). control: {"claude": {"review": {...}}}
    status, summary, findings, output (json|text|none), sleep, exit."""
    rv = ctrl.get("review", {})
    if rv.get("sleep"):
        time.sleep(rv["sleep"])
    res = {"status": rv.get("status", "ok"), "summary": rv.get("summary", "Stub review: nothing important."),
           "findings": rv.get("findings", [])}
    mode = rv.get("output", "json")
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "stub-review"}))
    for line in prompt.splitlines():  # the reviewer reads the evidence file ck-run names
        if line.startswith("1. Read "):
            print(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": line.split()[2]}}]}}))
    ev = {"type": "result", "subtype": "success", "is_error": False,
          "result": json.dumps(res) if mode == "json" else "I could not decide.", "session_id": "stub-review"}
    if mode == "json":
        ev["structured_output"] = res
    if mode != "none":
        print(json.dumps(ev))
    sys.stdout.flush()
    sys.exit(int(rv.get("exit", 0)))


def main():
    ctrl = control()
    prompt = get_prompt()
    calls = os.path.join(HERE, "calls")
    os.makedirs(calls, exist_ok=True)
    rec = {"agent": AGENT, "argv": sys.argv, "cwd": os.getcwd(), "env": dict(os.environ), "prompt": prompt,
           "pid": os.getpid(), "live": is_live(), "isatty": sys.stdin.isatty()}
    n = len(os.listdir(calls))
    with open(os.path.join(calls, f"{n:03d}-{AGENT}.json"), "w") as f:
        json.dump(rec, f)
    if AGENT == "claude" and prompt.startswith("CK POST-RUN REVIEW"):
        main_review(ctrl, prompt)
    if rec["live"]:
        main_live(ctrl, prompt)
    for d in ctrl.get("mkdir", []):
        os.makedirs(d, exist_ok=True)
    for p in ctrl.get("write", []):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            f.write("stub edit\n")
    for p in ctrl.get("delete", []):
        os.remove(p)
    write_tool_session(ctrl, prompt)
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
