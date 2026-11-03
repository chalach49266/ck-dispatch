"""Unit tests: argv map per agent and mode, guards, parsers, schema, ledger thread view, routing."""
import datetime as dt
import json
import os
import unittest
from unittest import mock

from helpers import Sandbox

from ckdispatch import guards, ledger, route, summarize
from ckdispatch.config import RESULT_SCHEMA, Config
from ckdispatch.errors import Refused
from ckdispatch.flags import PROMPT, build
from ckdispatch.schema import load, validate


def pairs(a):
    return [[str(a[i]), str(a[i + 1])] for i in range(len(a) - 1)]


class TestArgvMap(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.env = mock.patch.dict(os.environ, {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config})
        self.env.start()
        self.cfg = Config()

    def tearDown(self):
        self.env.stop()
        self.sb.cleanup()

    def _build(self, agent, mode, ws="devfab", scope="Design Library"):
        w = self.cfg.workspace(ws)
        scopes = []
        if mode == "edit":
            scopes = guards.validate_write_scopes(self.cfg, w, agent, [scope], {"claude": ["local"],
                                                  "codex": ["workspace", "local"], "cursor": ["workspace", "local"],
                                                  "agy": ["workspace", "local"]})
        return build(self.cfg, w, agent, mode=mode, scopes=scopes, model=None, run_dir="/tmp/run",
                     schema_path=RESULT_SCHEMA, extra_read_dirs=[], timeout_s=600,
                     claude_settings_path="/tmp/run/claude-settings.json",
                     binary=os.path.join(self.sb.stubs, {"cursor": "agent"}.get(agent, agent)))

    def test_no_forbidden_token_for_any_agent_or_mode(self):
        local_repo = os.path.join(self.sb.local, "myrepo")
        for agent in ("claude", "codex", "cursor", "agy"):
            for mode in ("read", "edit"):
                with self.subTest(agent=agent, mode=mode):
                    scope = local_repo if agent == "claude" else "Design Library"
                    cmd = self._build(agent, mode, scope=scope)
                    flat = " ".join(str(x) for x in cmd.argv if x is not PROMPT).lower()
                    for bad in ("dangerously", "bypasspermissions", "yolo", "--force", "full-access", "full-auto",
                                "always-proceed", "dontask", "--auto", "approve-for-me", "skip-permissions"):
                        self.assertNotIn(bad, flat)
                    self.assertNotIn(" auto ", f" {flat} ")
                    guards.check_child_argv(cmd.argv, PROMPT)  # does not raise

    def test_auto_mode_flags_per_agent(self):
        want = {"claude": [["--permission-mode", "auto"]],
                "codex": [["-s", "workspace-write"], ["-c", 'approval_policy="on-request"'],
                          ["-c", 'approvals_reviewer="auto_review"'], ["-m", "gpt-6.1-sol"]],
                "cursor": [["--sandbox", "enabled"]],
                "agy": [["--mode", "accept-edits"]]}  # 2026-11-03: agy --mode auto is not on this account
        for agent in ("claude", "codex", "cursor", "agy"):
            with self.subTest(agent=agent):
                argv = self._build(agent, "auto").argv
                flat = " ".join(str(x) for x in argv if x is not PROMPT).lower()
                for pair in want.get(agent, []):
                    self.assertIn(pair, pairs(argv), (agent, argv))
                if agent == "cursor":  # 2026-11-03 (chalach): allow-all + mandatory Claude review
                    self.assertIn("--force", argv)
                    self.assertIn("--trust", argv)
                    self.assertNotIn("--auto-review", argv)
                    self.assertNotIn("--mode", argv)
                    flat = flat.replace("--force", "")
                for bad in ("dangerously", "bypasspermissions", "yolo", "--force", "full-access", "full-auto",
                            "skip-permissions", "approval_policy=never", 'approval_policy="never"', "-f "):
                    self.assertNotIn(bad, flat + " ")
                guards.check_child_argv(argv, PROMPT, auto=True, agent=agent)  # allowed when ck-run injects them
                if agent != "agy":  # agy auto = accept-edits, the same flag as approved edit mode
                    with self.assertRaises(Refused):
                        guards.check_child_argv(argv, PROMPT)  # same tokens refused outside auto
                if agent == "cursor":
                    with self.assertRaises(Refused):
                        guards.check_child_argv(argv, PROMPT, auto=True, agent="codex")  # --force is cursor's only

    def test_cursor_force_is_the_only_new_allow_all_and_only_for_cursor_auto(self):
        a = ["agent", "-p", "--trust", "--sandbox", "enabled", "--force", "--workspace", "/w", PROMPT]
        guards.check_child_argv(a, PROMPT, auto=True, agent="cursor")
        for agent, auto in (("cursor", False), ("agy", True), ("claude", True), ("codex", True), (None, True)):
            with self.subTest(agent=agent, auto=auto):
                with self.assertRaises(Refused):
                    guards.check_child_argv(a, PROMPT, auto=auto, agent=agent)
        for bad in (["-f"], ["--yolo"], ["--Force"], ["--force=1"], ["--dangerously-skip-permissions"],
                    ["--approve-mcps"]):
            with self.subTest(bad=bad):
                with self.assertRaises(Refused):
                    guards.check_child_argv(["agent", "-p", *bad, PROMPT], PROMPT, auto=True, agent="cursor")
        with self.assertRaises(Refused):
            guards.check_ck_argv(["run", "--agent", "cursor", "--force"])  # CK can never pass it

    def test_auto_does_not_open_bypass_in_child_guard(self):
        for bad in (["--dangerously-skip-permissions"], ["--permission-mode", "bypassPermissions"],
                    ["--dangerously-bypass-approvals-and-sandbox"], ["--force"], ["--yolo"],
                    ["-s", "danger-full-access"], ["-c", 'approval_policy="never"'], ["-c", "approval_policy=never"]):
            with self.subTest(bad=bad):
                with self.assertRaises(Refused):
                    guards.check_child_argv(["claude", "-p", *bad, PROMPT], PROMPT, auto=True)

    def test_ck_cannot_pass_auto_flags_itself(self):
        guards.check_ck_argv(["run", "--mode", "auto"])  # the mode name is fine
        for bad in (["--auto-review"], ["--permission-mode", "auto"], ["-c", 'approvals_reviewer="auto_review"'],
                    ["--approve-for-me"], ["--mode", "bypassPermissions"], ["--yolo"], ["--force"]):
            with self.subTest(bad=bad):
                with self.assertRaises(Refused):
                    guards.check_ck_argv(["run", "--mode", "auto", *bad])

    def test_permission_flags_per_agent(self):
        p = pairs(self._build("claude", "read").argv)
        self.assertIn(["--permission-mode", "plan"], p)
        self.assertIn(["--settings", "/tmp/run/claude-settings.json"], p)
        self.assertIn("--strict-mcp-config", self._build("claude", "read").argv)
        p = pairs(self._build("claude", "edit", scope=os.path.join(self.sb.local, "myrepo")).argv)
        self.assertIn(["--permission-mode", "acceptEdits"], p)
        self.assertIn(["--add-dir", os.path.realpath(os.path.join(self.sb.local, "myrepo"))], p)

        c = self._build("codex", "read")
        self.assertIn(["-s", "read-only"], pairs(c.argv))
        self.assertEqual(c.argv[-1], "-")
        self.assertTrue(c.stdin_prompt)
        c = self._build("codex", "edit")
        scope = os.path.realpath(os.path.join(self.sb.devfab, "Design Library"))
        self.assertIn(["-s", "workspace-write"], pairs(c.argv))
        self.assertIn(["-C", scope], pairs(c.argv))
        self.assertIn(["-c", "sandbox_workspace_write.network_access=false"], pairs(c.argv))
        self.assertEqual(c.cwd, scope)

        r = self._build("cursor", "read")  # D4: --trust always, --mode plan unless edit approved
        self.assertIn("--trust", r.argv)
        self.assertIn(["--mode", "plan"], pairs(r.argv))
        self.assertIn(["--sandbox", "enabled"], pairs(r.argv))
        self.assertIs(r.argv[-1], PROMPT)
        e = self._build("cursor", "edit")
        self.assertIn("--trust", e.argv)
        self.assertNotIn("--mode", e.argv)
        self.assertNotIn("--force", e.argv)
        self.assertIn(["--workspace", scope], pairs(e.argv))

        a = self._build("agy", "read")  # D2: accept-edits only after approval, never auto
        self.assertIn(["--mode", "plan"], pairs(a.argv))
        self.assertIn("--sandbox", a.argv)
        self.assertEqual(a.argv[-2:], ["-p", PROMPT])
        e = self._build("agy", "edit")
        self.assertIn(["--mode", "accept-edits"], pairs(e.argv))
        self.assertEqual(e.cwd, scope)

    def test_injected_tokens_are_refused(self):
        base = self._build("codex", "read").argv
        for extra in (["--dangerously-bypass-approvals-and-sandbox"], ["-s", "danger-full-access"],
                      ["--full-auto"], ["-c", "approval_policy=never"], ["--mode", "auto"], ["--yolo"],
                      ["--force"], ["-f"], ["--permission-mode", "bypassPermissions"], ["bypassPermissions"],
                      ["--sandbox", "disabled"], ["--mode", "always-proceed"], ["--printenv"]):
            with self.subTest(extra=extra):
                with self.assertRaises(Refused):
                    guards.check_child_argv(base[:-1] + extra + [base[-1]], PROMPT)

    def test_prompt_text_is_not_scanned(self):
        a = self._build("cursor", "read").argv
        guards.check_child_argv(a, PROMPT)  # PROMPT slot skipped even if the text says --yolo
        final = [("review the --yolo default" if x is PROMPT else x) for x in a]
        self.assertIn("review the --yolo default", final)

    def test_claude_may_not_edit_workspace(self):
        with self.assertRaises(Refused):
            self._build("claude", "edit", scope="Design Library")


class TestCkArgv(unittest.TestCase):
    def test_normal_command_lines_pass(self):
        guards.check_ck_argv(["run", "--agent", "codex", "--workspace", "devfab", "--title", "fix yolo default in repo",
                              "--prompt-file", "/tmp/p.md", "--mode", "edit", "--approved", "user said --force is bad",
                              "--write-scope", "Design Library", "--model", "auto"])
        guards.check_ck_argv(["thread", "2026-11-02T163112-devfab-general-claude-ab12"])

    def test_forbidden_command_lines_fail(self):
        for argv in (["run", "--dangerously-skip-permissions"], ["run", "--mode=full-auto"],
                     ["run", "--yolo"], ["run", "--force"], ["run", "bypassPermissions"], ["run", "--full-access"],
                     ["run", "--permission-mode", "plan"], ["run", "-s", "workspace-write"], ["run", "--add-dir", "/"]):
            with self.subTest(argv=argv):
                with self.assertRaises(Refused):
                    guards.check_ck_argv(argv)


class TestParsers(unittest.TestCase):
    def setUp(self):
        self.schema = load(RESULT_SCHEMA)
        self.good = {"run_id": "r", "status": "ok", "summary": ["a"], "report_md": "x", "findings": [],
                     "files_read": [], "files_changed": [], "numbers": [], "proposed_changes": "",
                     "open_questions": [], "needs_approval": []}

    def test_schema(self):
        self.assertEqual(validate(self.good, self.schema), [])
        bad = dict(self.good, status="great", extra=1)
        bad.pop("summary")
        errs = validate(bad, self.schema)
        self.assertTrue(any("missing 'summary'" in e for e in errs))
        self.assertTrue(any("unexpected 'extra'" in e for e in errs))
        self.assertTrue(any("'great'" in e for e in errs))
        self.assertTrue(validate(dict(self.good, numbers=[{"value": "1"}]), self.schema))

    def test_extract_structured(self):
        j = json.dumps(self.good)
        self.assertEqual(summarize.extract_structured(j)["run_id"], "r")
        self.assertEqual(summarize.extract_structured(f"text\n```json\n{j}\n```\n")["run_id"], "r")
        self.assertEqual(summarize.extract_structured(f"blah {{not json}} then {j} end")["run_id"], "r")
        self.assertIsNone(summarize.extract_structured("no json at all"))

    def test_parse_claude_stream(self):
        lines = [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/a/index.md"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "result", "result": "done", "structured_output": self.good, "session_id": "s", "total_cost_usd": 0.5},
        ]
        out = summarize.parse_claude("\n".join(json.dumps(x) for x in lines))
        self.assertEqual(out["observed_reads"], ["/a/index.md"])
        self.assertEqual(len(out["actions"]), 2)
        self.assertEqual(out["structured"]["run_id"], "r")
        self.assertEqual(out["cost_usd"], 0.5)

    def test_parse_codex_events(self):
        ev = [{"type": "thread.started", "thread_id": "t"},
              {"type": "item.completed", "item": {"type": "command_execution", "command": "bash -lc 'cat x'"}},
              {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "/w/a.md", "kind": "update"}]}},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}}]
        out = summarize.parse_codex("\n".join(json.dumps(x) for x in ev), "")
        self.assertEqual(out["final_text"], "hello")
        self.assertEqual([a["tool"] for a in out["actions"]], ["shell", "file_change"])
        self.assertEqual(out["session_id"], "t")

    def test_summary_lines(self):
        lines, src, note = summarize.summary_lines({"summary": "one\n\ntwo"}, "", "/r.md")
        self.assertEqual((lines, src, note), (["one", "two"], "agent", None))
        lines, src, _ = summarize.summary_lines({"summary": ["x" * 500]}, "", "/r.md")
        self.assertEqual(len(lines[0]), 200)
        lines, src, _ = summarize.summary_lines(None, "a\nb", "/r.md")
        self.assertEqual(src, "fallback")
        self.assertLessEqual(len(lines), 10)


class TestStep3Live(unittest.TestCase):
    """Facts confirmed by the step-3 live probes (2026-11-02); see docs/step3/results.md."""

    def test_claude_path_rules_are_anchored(self):
        # claude 2.1.316: a bare '**/x' rule is relative to the cwd, so it missed a .env outside the workspace.
        from ckdispatch.config import CLAUDE_GUARD_BASE
        with open(CLAUDE_GUARD_BASE, encoding="utf-8") as f:
            deny = json.load(f)["permissions"]["deny"]
        for rule in deny:
            if rule.startswith(("Read(", "Edit(", "Write(")):
                path = rule[rule.index("(") + 1:-1]
                self.assertTrue(path.startswith(("//", "~/")), f"unanchored path rule: {rule}")
        for tool in ("mcp__skins", "PublishPlugin", "ShareOnboardingGuide", "SendToProject", "SendMessage",
                     "PushNotification", "Workflow", "CronCreate"):
            self.assertIn(tool, deny)

    def test_parse_live_agy_and_cursor_shapes(self):
        good = {"run_id": "r", "status": "ok", "summary": ["a"], "report_md": "x", "findings": [],
                "files_read": [], "files_changed": [], "numbers": [], "proposed_changes": "",
                "open_questions": [], "needs_approval": []}
        agy = {"conversation_id": "c1", "duration_seconds": 1.0, "json_schema": {}, "num_turns": 1,
               "response": json.dumps(good), "status": "SUCCESS", "structured_output": good, "usage": {}}
        out = summarize.parse_single_json(json.dumps(agy))
        self.assertEqual((out["session_id"], out["structured"]["run_id"]), ("c1", "r"))
        self.assertIsNone(out["agent_error"])
        cur = {"type": "result", "subtype": "success", "is_error": False, "result": "OK\n" + json.dumps(good),
               "session_id": "s1", "request_id": "q", "usage": {}}
        out = summarize.parse_single_json(json.dumps(cur))
        self.assertEqual(out["session_id"], "s1")
        self.assertEqual(summarize.extract_structured(out["final_text"])["run_id"], "r")


class TestThreadView(unittest.TestCase):
    def test_cycle_and_orphans_are_safe(self):
        ev = [
            {"event": "dispatch", "run_id": "A", "parent_run_id": "B", "from": "CK", "to": "claude", "title": "a", "ts": "1"},
            {"event": "dispatch", "run_id": "B", "parent_run_id": "A", "from": "CK", "to": "codex", "title": "b", "ts": "2"},
            {"event": "dispatch", "run_id": "C", "parent_run_id": "Z", "from": "CK", "to": "agy", "title": "c", "ts": "3"},
        ]
        md = ledger.render_thread(ev, "A")
        self.assertIn("# Thread", md)
        self.assertEqual(md.count("CK → "), 2)
        self.assertIn("no result yet", md)
        self.assertIn("# Thread C", ledger.render_thread(ev, "C"))
        with self.assertRaises(KeyError):
            ledger.render_thread(ev, "missing")

    def test_review_label_shows_ck_as_dispatcher(self):
        ev = [
            {"event": "dispatch", "run_id": "A", "from": "CK", "to": "claude", "via": None, "title": "a", "ts": "1"},
            {"event": "result", "run_id": "A", "from": "claude", "to": "CK", "status": "ok", "ts": "2"},
            {"event": "review", "run_id": "R", "parent_run_id": "A", "from": "claude", "to": "codex",
             "via": "CK", "title": "Review of A: a", "ts": "3"},
            {"event": "result", "run_id": "R", "from": "codex", "to": "CK", "status": "ok", "ts": "4"},
            {"event": "busy", "run_id": "B", "parent_run_id": "A", "from": "claude", "to": "agy",
             "via": "CK", "status": "busy", "title": "Review of A: a", "ts": "5"},
        ]
        md = ledger.render_thread(ev, "R")
        self.assertIn("## 1  CK → claude · dispatch · a", md)
        self.assertIn("### 1.1  CK → codex · review of claude's run · Review of A: a  ← requested", md)
        self.assertIn("### 1.2  CK → agy · review of claude's run · busy · Review of A: a", md)
        self.assertIn("- codex → CK: **ok**", md)
        self.assertNotIn("claude → codex", md)
        self.assertNotIn("claude → agy", md)


class TestRoute(unittest.TestCase):
    def test_parse_reset(self):
        ref = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(route.parse_reset("retry-after-ms=60000", ref), ref + dt.timedelta(minutes=1))
        self.assertEqual(route.parse_reset("nothing", ref), ref + dt.timedelta(hours=1))
        t = route.parse_reset("resets 3pm (America/Chicago)", ref)
        self.assertEqual(t.hour, 21)

    def test_matrix_and_review(self):
        sb = Sandbox()
        try:
            self.assertEqual(route.suggest(sb.runs, "wiki_lint")["agent"], "agy")
            self.assertEqual(route.suggest(sb.runs, "multi_file_diff")["agent"], "cursor")
            self.assertEqual(route.suggest(sb.runs, "fdtd_review", review_of="claude")["agent"], "codex")
            self.assertEqual(route.suggest(sb.runs, "writing", review_of="claude")["agent"], "agy")
            self.assertEqual(route.suggest(sb.runs, "unknown_type")["agent"], "claude")
        finally:
            sb.cleanup()


if __name__ == "__main__":
    unittest.main()
