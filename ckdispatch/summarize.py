"""Parse each agent's raw output into one result, then write result.json, result.md and summary.md."""
from __future__ import annotations

import json
import os
import re

from .prompt import SUMMARY_MAX_CHARS, SUMMARY_MAX_LINES

READ_TOOLS = {"Read", "NotebookRead"}


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _json_lines(text: str):
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _whole_json(text: str):
    try:
        v = json.loads(text.strip())
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def _short(s, n=2000) -> str:
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "..."


def _empty() -> dict:
    return {"final_text": "", "structured": None, "actions": [], "observed_reads": [],
            "session_id": None, "cost_usd": None, "agent_error": None, "finished_reason": None,
            "text_only": False}


def parse_claude(stdout: str) -> dict:
    out = _empty()
    events = list(_json_lines(stdout))
    if not events:
        w = _whole_json(stdout)
        events = [w] if w else []
    last_text = ""
    for ev in events:
        t = ev.get("type")
        if t == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    name, inp = c.get("name"), c.get("input") or {}
                    out["actions"].append({"tool": name, "input": _short(inp)})
                    if name in READ_TOOLS and isinstance(inp, dict) and inp.get("file_path"):
                        out["observed_reads"].append(inp["file_path"])
                    if name in ("StructuredOutput", "structured_output") and isinstance(inp, dict) and "summary" in inp:
                        out["structured"] = inp
                elif c.get("type") == "text":
                    last_text = c.get("text") or last_text
        elif t == "result":
            out["final_text"] = ev.get("result") or ""
            so = ev.get("structured_output")
            if isinstance(so, dict):
                out["structured"] = so
            out["session_id"] = ev.get("session_id")
            out["cost_usd"] = ev.get("total_cost_usd", ev.get("cost_usd"))
            if ev.get("is_error"):
                out["agent_error"] = str(ev.get("result") or ev.get("subtype") or "error")[:500]
    if not out["final_text"]:
        out["final_text"] = last_text
    if not events:
        out["final_text"], out["text_only"] = stdout, True
    return out


def _codex_item(item: dict, out: dict, state: dict) -> None:
    itype = item.get("type") or item.get("item_type")
    cmd = item.get("command")
    if cmd is not None:
        out["actions"].append({"tool": "shell", "input": _short(" ".join(cmd) if isinstance(cmd, list) else cmd)})
    if itype == "file_change":
        for ch in item.get("changes") or []:
            if isinstance(ch, dict):
                out["actions"].append({"tool": "file_change", "input": _short(ch.get("path", ""))})
    if itype in ("web_search", "mcp_tool_call"):
        out["actions"].append({"tool": itype, "input": _short(item)})
    if itype in ("agent_message", "assistant_message"):
        state["last_msg"] = item.get("text") or item.get("message") or state.get("last_msg", "")


def parse_codex(stdout: str, last_md: str) -> dict:
    out = _empty()
    state = {"last_msg": ""}
    n = 0
    for ev in _json_lines(stdout):
        n += 1
        et = ev.get("type", "")
        if et == "thread.started":
            out["session_id"] = ev.get("thread_id")
        if et in ("turn.failed", "error"):
            err = ev.get("error")
            out["agent_error"] = str((err or {}).get("message") if isinstance(err, dict) else (err or ev.get("message")))[:500]
        for key in ("item", "msg"):
            if isinstance(ev.get(key), dict):
                _codex_item(ev[key], out, state)
    out["final_text"] = last_md.strip() or state["last_msg"]
    if n == 0 and not out["final_text"]:
        out["final_text"], out["text_only"] = stdout, True
    return out


def parse_single_json(stdout: str) -> dict:
    """Cursor and agy print one JSON object (keys differ); fall back to plain text."""
    out = _empty()
    out["text_only"] = True  # no tool-call stream: rulecheck scans the whole output text
    obj = _whole_json(stdout)
    if obj is None:
        objs = list(_json_lines(stdout))
        obj = objs[-1] if objs else None
    if obj is None:
        out["final_text"] = stdout
        return out
    so = obj.get("structured_output")
    if isinstance(so, dict):
        out["structured"] = so
    for k in ("result", "response", "text", "output", "content", "message"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            out["final_text"] = v
            break
        if isinstance(v, dict) and "summary" in v and out["structured"] is None:
            out["structured"] = v
    out["session_id"] = obj.get("session_id") or obj.get("conversation_id") or obj.get("chatId")
    out["finished_reason"] = obj.get("finished_reason")
    if obj.get("is_error") or obj.get("error"):
        out["agent_error"] = str(obj.get("error") or obj.get("result") or "error")[:500]
    return out


def parse(agent: str, run_dir: str) -> dict:
    stdout = _read(os.path.join(run_dir, "stdout.log"))
    if agent == "claude":
        out = parse_claude(stdout)
    elif agent == "codex":
        out = parse_codex(stdout, _read(os.path.join(run_dir, "last.md")))
    else:
        out = parse_single_json(stdout)
    if out["structured"] is None:
        out["structured"] = extract_structured(out["final_text"])
    return out


_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S)


def extract_structured(text: str):
    """The result JSON: whole text, else the last ```json block, else the last {...} that has 'summary'."""
    if not text:
        return None
    w = _whole_json(text)
    if w is not None:
        return w
    for block in reversed(_FENCE.findall(text)):
        w = _whole_json(block)
        if w is not None:
            return w
    end = text.rfind("}")
    while end != -1:
        depth, start = 0, -1
        for i in range(end, -1, -1):
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    start = i
                    break
        if start == -1:
            break
        w = _whole_json(text[start:end + 1])
        if w is not None and "summary" in w:
            return w
        end = text.rfind("}", 0, start)
    return None


def summary_lines(structured, final_text: str, response_path: str) -> tuple:
    """(lines, source, note). source = 'agent' or 'fallback'. Never more than 10 lines."""
    raw = structured.get("summary") if isinstance(structured, dict) else None
    if isinstance(raw, str):
        raw = raw.splitlines()
    if isinstance(raw, list):
        lines = []
        for x in raw:
            for part in str(x).splitlines():
                part = part.strip()
                if part:
                    lines.append(part if len(part) <= SUMMARY_MAX_CHARS else part[:SUMMARY_MAX_CHARS - 3] + "...")
        if lines:
            note = None
            if len(lines) > SUMMARY_MAX_LINES:
                note = f"agent summary had {len(lines)} lines; kept the first {SUMMARY_MAX_LINES}"
                lines = lines[:SUMMARY_MAX_LINES]
            return lines, "agent", note
    lines = [f"[ck-run] The agent returned no summary. Full output: {response_path}"]
    for part in (final_text or "").splitlines():
        part = part.strip()
        if part and len(lines) < SUMMARY_MAX_LINES:
            lines.append(part if len(part) <= SUMMARY_MAX_CHARS else part[:SUMMARY_MAX_CHARS - 3] + "...")
    return lines, "fallback", "agent gave no summary; summary.md holds the first lines of its output"


_PATH_IN_SOURCE = re.compile(r"^(.+?\.[A-Za-z0-9]{1,8})(?=$|[:#\s,;)\]])")


def check_numbers(numbers, ws_root: str) -> list:
    """Tag each number: 'ok' (source path exists and verified), else 'UNVERIFIED' with a reason."""
    out = []
    for n in numbers or []:
        if not isinstance(n, dict):
            continue
        src = str(n.get("source") or "").strip()
        tag, why = "ok", ""
        if not src or src.upper().startswith("UNVERIFIED"):
            tag, why = "UNVERIFIED", "no source"
        else:
            m = _PATH_IN_SOURCE.match(src)
            if m:
                p = os.path.expanduser(m.group(1).strip("`'\" "))
                if not os.path.isabs(p):
                    p = os.path.join(ws_root, p)
                if not os.path.exists(p):
                    tag, why = "UNVERIFIED", "cited path not found"
            else:
                why = "source is not a file path (not checked)"
            if tag == "ok" and n.get("verified") is False:
                tag, why = "UNVERIFIED", "agent marked it unverified"
        out.append({"value": n.get("value"), "unit": n.get("unit"), "source": src, "tag": tag, "why": why})
    return out


def _md_list(items, empty="none") -> str:
    items = [i for i in (items or []) if str(i).strip()]
    return "\n".join(f"- {i}" for i in items) if items else f"- {empty}"


def render_result_md(rec: dict) -> str:
    ar = rec.get("agent_result") or {}
    L = [f"# {rec['title']}",
         "",
         f"run `{rec['run_id']}` · {rec['agent']} · {rec['workspace']} · mode {rec['mode']} · kind {rec['kind']}"
         + (f" · parent `{rec['parent_run_id']}`" if rec.get("parent_run_id") else ""),
         f"status **{rec['status']}** (agent said: {rec.get('agent_status') or 'n/a'}) · "
         f"{rec.get('started_at')} → {rec.get('finished_at')} ({rec.get('duration_s')} s)",
         "", "## Summary (written by the agent)" if rec.get("summary_source") == "agent" else "## Summary (fallback by ck-run)",
         _md_list(rec.get("summary_lines"))]
    if rec.get("notes"):
        L += ["", "## ck-run notes", _md_list(rec["notes"])]
    L += ["", "## Report", (ar.get("report_md") or rec.get("final_text") or "(empty)").strip()]
    if ar.get("findings"):
        L += ["", "## Findings"]
        for f in ar["findings"]:
            if isinstance(f, dict):
                L.append(f"- [{f.get('severity', '?')}] {f.get('claim', '')} — {f.get('evidence', '')}")
    if rec.get("numbers_checked"):
        L += ["", "## Numbers"]
        for n in rec["numbers_checked"]:
            tag = "" if n["tag"] == "ok" else f" **UNVERIFIED** ({n['why']})"
            L.append(f"- {n['value']} {n.get('unit') or ''} — {n['source'] or 'no source'}{tag}")
    if ar.get("proposed_changes"):
        L += ["", "## Proposed changes", ar["proposed_changes"].strip()]
    if ar.get("open_questions"):
        L += ["", "## Open questions", _md_list(ar["open_questions"])]
    if ar.get("needs_approval"):
        L += ["", "## Needs approval", _md_list(ar["needs_approval"])]
    rc = rec.get("rulecheck") or {}
    L += ["", "## Files read", _md_list(rec.get("files_read")),
          "", "## Files changed (seen by ck-run rulecheck)", _md_list(rec.get("files_changed")),
          "", "## Rule check",
          f"- clean: {rc.get('clean')}",
          *(f"- VIOLATION: {v}" for v in rc.get("violations", [])),
          *(f"- flag: {v}" for v in rc.get("flags", [])),
          *(f"- note: {v}" for v in (rc.get("notes") or [])[:30]),
          *([f"- ... +{len(rc['notes']) - 30} more notes in rulecheck.json"] if len(rc.get("notes") or []) > 30 else []),
          ""]
    return "\n".join(L)
