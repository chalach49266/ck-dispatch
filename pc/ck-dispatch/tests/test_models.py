"""v4 models (BUILD_SPEC_v4 section 6): config/models.json, alias and tier resolution, effort plumbing per agent,
the guards for injected flags, `ck-run models` and `ck-run route --lane`. Stub CLIs only."""
import copy
import json
import os
import unittest
from unittest import mock

from helpers import Sandbox

from ckdispatch import guards, models, route
from ckdispatch.config import RESULT_SCHEMA, Config
from ckdispatch.errors import Refused
from ckdispatch.flags import PROMPT, build, build_live


def pairs(a):
    return [[str(a[i]), str(a[i + 1])] for i in range(len(a) - 1)]


class TestModelsJson(unittest.TestCase):
    def test_file_has_every_alias_tier_lane_of_the_spec(self):
        m = models.load()
        self.assertEqual(set(m["aliases"]), {"sol", "sol-fast", "sol-max", "astra", "opus", "opus-max", "sonnet", "haiku",
                                             "grok", "composer", "gemini"})
        self.assertEqual(m["tiers"]["codex"], {"fast": "sol-fast", "strong": "sol", "top": "astra"})
        self.assertEqual(m["tiers"]["claude"], {"fast": "sonnet", "strong": "opus", "top": "opus-max"})
        self.assertEqual(m["size_tier"], {"S": "fast", "M": "strong", "L": "strong"})
        self.assertEqual(m["race_default"], ["astra", "opus", "grok"])
        self.assertEqual(set(m["lanes"]), {"quick", "lab-code", "bug-fix", "numbers", "wiki", "writing", "decision",
                                           "loop", "repo", "scout", "mechanical"})
        for agent in ("claude", "codex", "cursor", "agy"):
            self.assertIn(agent, m["_verified"])
        self.assertEqual(m["aliases"]["sol"]["model"], "gpt-6.1-sol")
        self.assertEqual(m["aliases"]["astra"]["model"], "gpt-6-astra")
        self.assertIsNone(m["aliases"]["gemini"]["model"])

    def test_cursor_slugs_come_from_the_verified_list(self):
        m = models.load()
        listed = m["_verified"]["cursor"]["grok_4_7"] + m["_verified"]["cursor"]["composer_2_5"]
        for name in ("grok", "composer"):
            self.assertIn(m["aliases"][name]["model"], listed)
            self.assertNotIn("[", m["aliases"][name]["model"])  # no invented bracket parameters

    def test_every_alias_effort_is_verified_or_reported(self):
        m = models.load()
        for name, a in m["aliases"].items():
            if a["effort"]:
                with self.subTest(alias=name):
                    self.assertTrue(models.effort_verified(a["agent"], a["effort"], m), name)

    def test_a_bad_file_is_refused(self):
        m = copy.deepcopy(models.load())
        m["tiers"]["codex"]["fast"] = "opus"  # a claude alias in the codex row
        with self.assertRaises(Refused):
            models._check(m, "x.json")


class TestResolve(unittest.TestCase):
    def test_alias_picks_the_agent(self):
        r = models.resolve(None, "sol")
        self.assertEqual((r["agent"], r["model"], r["alias"], r["tier"], r["effort"], r["explicit"]),
                         ("codex", "gpt-6.1-sol", "sol", "strong", "high", True))
        r = models.resolve("codex", "SOL")  # case-insensitive, same agent is fine
        self.assertEqual(r["alias"], "sol")
        self.assertEqual(models.agent_for("grok"), "cursor")

    def test_alias_of_another_agent_is_refused(self):
        with self.assertRaises(Refused) as c:
            models.resolve("claude", "sol")
        self.assertIn("runs on codex", str(c.exception))

    def test_raw_model_id_needs_the_agent(self):
        r = models.resolve("codex", "gpt-6.1-sol")
        self.assertEqual((r["model"], r["alias"], r["effort"], r["explicit"]), ("gpt-6.1-sol", None, None, True))
        with self.assertRaises(Refused):
            models.resolve(None, "gpt-6.1-sol")
        with self.assertRaises(Refused):
            models.resolve(None, None)
        with self.assertRaises(Refused):
            models.resolve("codex", None, "huge")

    def test_tier_defaults(self):
        self.assertEqual(models.resolve("claude")["alias"], "opus")              # no ask: strong
        self.assertEqual(models.resolve("claude", size="S")["alias"], "sonnet")  # S: fast
        self.assertEqual(models.resolve("codex", size="M")["alias"], "sol")
        self.assertEqual(models.resolve("codex", size="L")["alias"], "sol")      # L: strong, not top
        self.assertEqual(models.resolve("codex", size="S", pattern="race")["alias"], "astra")  # race: top
        self.assertEqual(models.resolve("codex", None, "fast")["alias"], "sol-fast")
        self.assertEqual(models.resolve("codex", None, "fast")["effort"], "low")
        self.assertEqual(models.resolve("claude", None, "top")["effort"], "xhigh")
        r = models.resolve("agy", size="L")
        self.assertEqual((r["alias"], r["model"], r["effort"]), ("gemini", None, None))
        self.assertFalse(models.resolve("claude")["explicit"])
        self.assertTrue(models.resolve("claude", None, "top")["tier_explicit"])

    def test_unverified_effort_gives_no_flag_and_a_note(self):
        m = copy.deepcopy(models.load())
        m["aliases"]["sol"]["effort"] = "medium"  # not in _verified.codex.efforts
        r = models.resolve(None, "sol", m=m)
        self.assertIsNone(r["effort"])
        self.assertTrue(any("TODO(verify)" in n for n in r["notes"]), r["notes"])


class TestEffortPlumbing(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.env = mock.patch.dict(os.environ, {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config})
        self.env.start()
        self.cfg = Config()
        self.ws = self.cfg.workspace("devfab")

    def tearDown(self):
        self.env.stop()
        self.sb.cleanup()

    def _cmd(self, agent, alias, mode="read", live=False):
        r = models.resolve(None if alias else agent, alias) if alias else models.resolve(agent)
        binary = os.path.join(self.sb.stubs, {"cursor": "agent"}.get(agent, agent))
        if live:
            return build_live(self.cfg, self.ws, agent, mode=mode, scopes=[], model=r["model"], run_dir="/tmp/run",
                              extra_read_dirs=[], claude_settings_path="/tmp/run/s.json", binary=binary,
                              effort=r["effort"]).argv
        return build(self.cfg, self.ws, agent, mode=mode, scopes=[], model=r["model"], run_dir="/tmp/run",
                     schema_path=RESULT_SCHEMA, extra_read_dirs=[], timeout_s=600,
                     claude_settings_path="/tmp/run/s.json", binary=binary, effort=r["effort"]).argv

    def test_injected_flags_per_agent(self):
        for live in (False, True):
            with self.subTest(live=live):
                a = self._cmd("claude", "opus-max", live=live)
                self.assertIn(["--model", "opus"], pairs(a))
                self.assertIn(["--effort", "xhigh"], pairs(a))
                a = self._cmd("codex", "sol", mode="auto", live=live)
                self.assertIn(["-m", "gpt-6.1-sol"], pairs(a))
                self.assertIn(["-c", 'model_reasoning_effort="high"'], pairs(a))
                self.assertEqual(sum(1 for x in a if str(x).startswith("model_reasoning_effort")), 1)
                a = self._cmd("codex", "astra", live=live)  # effort null: no effort flag
                self.assertIn(["-m", "gpt-6-astra"], pairs(a))
                self.assertFalse(any("model_reasoning_effort" in str(x) for x in a))
                a = self._cmd("cursor", "grok", live=live)
                self.assertIn(["--model", "grok-4.7-high"], pairs(a))
                self.assertNotIn("--effort", a)
                a = self._cmd("agy", "gemini", live=live)
                self.assertNotIn("--model", a)
                self.assertNotIn("--effort", a)

    def test_live_claude_keeps_permission_mode_last(self):
        a = self._cmd("claude", "sonnet", mode="auto", live=True)
        self.assertEqual(a[-3:-1], ["--permission-mode", "auto"])
        self.assertIs(a[-1], PROMPT)
        self.assertLess(a.index("--effort"), a.index("--permission-mode"))

    def test_no_model_no_effort_is_the_old_argv(self):
        b = build(self.cfg, self.ws, "codex", mode="auto", scopes=[], model=None, run_dir="/tmp/run",
                  schema_path=RESULT_SCHEMA, extra_read_dirs=[], timeout_s=600, claude_settings_path="/tmp/s.json",
                  binary=os.path.join(self.sb.stubs, "codex")).argv
        self.assertIn(["-m", "gpt-6.1-sol"], pairs(b))
        self.assertFalse(any("model_reasoning_effort" in str(x) for x in b))


class TestGuards(unittest.TestCase):
    def test_ck_can_never_pass_effort_flags(self):
        for bad in (["--effort", "high"], ["--effort=max"], ["-c", 'model_reasoning_effort="high"'],
                    ["--model_reasoning_effort", "high"], ["--config", "model_reasoning_effort=high"]):
            with self.subTest(bad=bad):
                with self.assertRaises(Refused):
                    guards.check_ck_argv(["run", "--agent", "codex", *bad])
        guards.check_ck_argv(["run", "--model", "opus-max", "--tier", "top", "--ask", "A-1105-4"])  # fine

    def test_child_argv_admits_effort_only_for_its_agent(self):
        guards.check_child_argv(["claude", "-p", "--model", "opus", "--effort", "high", PROMPT], PROMPT,
                                agent="claude")
        guards.check_child_argv(["codex", "exec", "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="low"', "-"],
                                PROMPT, agent="codex")
        for argv, agent in (
                (["claude", "-p", "--effort", "ultra", PROMPT], "claude"),            # unknown level
                (["claude", "-p", "--effort", "high", PROMPT], None),                 # no agent: not injected
                (["agent", "-p", "--effort", "high", PROMPT], "cursor"),              # cursor has no effort flag
                (["codex", "exec", "--effort", "high", "-"], "codex"),
                (["codex", "exec", "-c", 'model_reasoning_effort="high"', "-"], "claude"),
                (["codex", "exec", "-c", 'model_reasoning_effort="high"', "-"], None),
                (["codex", "exec", "-c", 'model_reasoning_effort="bogus"', "-"], "codex"),
                (["codex", "exec", "-c", 'model_reasoning_effort="high";approval_policy="never"', "-"], "codex")):
            with self.subTest(argv=argv, agent=agent):
                with self.assertRaises(Refused):
                    guards.check_child_argv(argv, PROMPT, agent=agent)


class TestModelsCli(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def test_ck_run_models_prints_the_table(self):
        p = self.sb.ck("models")
        self.assertEqual(p.returncode, 0, p.stderr)
        for word in ("Aliases", "Tiers", "Lanes", "Race default: astra, opus, grok", "gpt-6.1-sol", "grok-4.7-high",
                     "opus-max", "mechanical"):
            self.assertIn(word, p.stdout)
        j = self.sb.ck("models", "--json")
        self.assertEqual(json.loads(j.stdout)["race_default"], ["astra", "opus", "grok"])

    def test_route_lane_resolves_and_skips_rate_limited_agents(self):
        p = self.sb.ck("route", "--lane", "lab-code")
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        self.assertEqual((d["pick"]["alias"], d["pick"]["agent"], d["pick"]["model"]), ("sol", "codex", "gpt-6.1-sol"))
        self.assertEqual(d["check"]["alias"], "opus")
        route.mark_limited(self.sb.runs, "codex", "rate limit")
        d = json.loads(self.sb.ck("route", "--lane", "lab-code").stdout)
        self.assertEqual(d["pick"]["alias"], "grok")  # astra is codex too: skipped
        self.assertIn("codex", d["skipped"])
        self.assertIn("fallback", d["note"])
        d = json.loads(self.sb.ck("route", "--lane", "quick").stdout)
        self.assertEqual(d["pick"]["alias"], "self")
        d = json.loads(self.sb.ck("route", "--lane", "decision").stdout)
        self.assertEqual([r["alias"] for r in d["race"]], ["opus", "grok"])  # astra skipped (codex limited)
        self.assertEqual(self.sb.ck("route", "--lane", "nope").returncode, 2)
        old = json.loads(self.sb.ck("route", "--task-type", "wiki_lint").stdout)  # the old behaviour stays
        self.assertEqual(old["agent"], "agy")


if __name__ == "__main__":
    unittest.main()
