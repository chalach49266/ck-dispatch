"""v4 core end to end (BUILD_SPEC_v4 sections 3-8, 10.1, 10.2, 10.5, 10.6): asks.required, budgets and exit 7 through
`ck-run run/review`, review reasons and ask inheritance, the model from an alias / tier / ask size and its CLI flags,
the size timeout, CK-passed effort flags refused, post-run review off/on, result.json v4 (schema + parser + finish
card), the live watchdog hand-off (stalled -> timeout), extension registration, and finalize_run. Stub CLIs only."""
import io
import json
import os
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import FAKE_CLI, Sandbox

from ckdispatch import asks, cli, ledger, live, runner, summarize
from ckdispatch.config import RESULT_SCHEMA, Config
from ckdispatch.errors import Refused
from ckdispatch.schema import load as load_schema
from ckdispatch.schema import validate

# a fake agent whose result also has the v4 fields of control["v4"] (the shared fake_cli.py emits the old fields)
V4_PATCH = '''
_orig_result = result


def result(ctrl, run_id, first, extra):
    r = _orig_result(ctrl, run_id, first, extra)
    r.update(ctrl.get("v4", {}))
    return r

'''
V4 = {"plain_summary": ["Saved the kit next to your script.", "Wrote one wiki page."],
      "verification": "fake_or_tests",
      "decisions": [{"what": "count on ctr0", "why": "the kit does", "source": "datasheet", "ref": "USB-6343 p.12"}],
      "later": ["datasheet check"], "you_should_know": ["The SPCM sends one short pulse per photon."],
      "your_move": "Try script 01 on the lab PC."}
WATCH_KEYS = {"stall_s", "osascript", "window_id", "terminal", "env", "runs_dir", "run_id", "ask_id", "agent", "title"}


def pairs(a):
    return [[str(a[i]), str(a[i + 1])] for i in range(len(a) - 1)]


class Base(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def run_ck(self, *extra, agent="claude", title="Test task", live=False):
        """ck-run run (read mode, headless unless live) with extra args; --agent only when agent is given."""
        args = ["run"] + (["--agent", agent] if agent else []) + ["--workspace", "devfab", "--title", title,
                                                                    "--prompt-file", self.sb.task()]
        if not any(str(x) == "--mode" or str(x).startswith("--mode=") for x in extra):  # (not --model)
            args += ["--mode", "read"]
        return self.sb.ck(*args, *extra, "--live" if live else "--no-live")

    def events(self, name=None, run_id=None):
        return [e for e in self.sb.ledger() if (name is None or e["event"] == name)
                and (run_id is None or e["run_id"] == run_id)]

    def last_call(self, agent):
        return [c for c in self.sb.calls() if c["agent"] == agent][-1]

    def install_v4_stub(self, name="claude"):
        with open(FAKE_CLI) as f:
            src = f.read()
        marker = 'if __name__ == "__main__":'
        p = os.path.join(self.sb.stubs, name)
        with open(p, "w") as f:
            f.write(f"#!{sys.executable}\n" + src.replace(marker, V4_PATCH + marker))
        os.chmod(p, 0o755)


class TestAsksRequired(Base):
    def test_run_needs_an_ask_and_every_event_carries_it(self):
        self.sb.set_section("asks", required=True)
        p = self.run_ck(title="MMI width")
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("--ask is required", p.stderr)
        self.assertIn("ck-run ask open --words \"<chalach's words, verbatim>\" --size S|M|L --title 'MMI width'",
                      p.stderr)
        self.assertEqual(self.sb.calls(), [])
        a = self.sb.open_ask("M")
        p = self.run_ck("--ask", a)
        self.assertEqual(p.returncode, 0, p.stderr)
        rid = self.sb.run_id_from(p)
        evs = self.events(run_id=rid)
        self.assertEqual([e["event"] for e in evs], ["dispatch", "result"])
        for e in evs:
            self.assertEqual((e["ask_id"], e["size"], e["pattern"], e["tier"], e["model_alias"], e["model"]),
                             (a, "M", "solo", "strong", "opus", "opus"), e["event"])
        res = self.sb.result(rid)
        self.assertEqual((res["ask_id"], res["model_alias"], res["effort"]), (a, "opus", "high"))
        with open(os.path.join(self.sb.runs, rid, "cmd.txt")) as f:
            cmd = f.read()
        self.assertIn("# model: opus = opus, effort high (tier strong (ask size M))", cmd)
        self.assertIn(f"# ask: {a} (size M, pattern solo)", cmd)
        rec = self.sb.ck("active")
        self.assertEqual(rec.returncode, 0)
        p = self.run_ck("--ask", "A-0101-99")
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown ask", p.stderr)
        self.assertEqual(self.events("refused")[-1]["ask_id"], "A-0101-99")


class TestBudgetsEndToEnd(Base):
    def test_s_budget_exit_7_and_over(self):
        a = self.sb.open_ask("S")
        self.assertEqual(self.run_ck("--ask", a).returncode, 0)
        p = self.run_ck("--ask", a)
        self.assertEqual(p.returncode, 7, p.stderr)
        self.assertIn(f"ck-run: OVER BUDGET: ask {a} (S) used 1/1 runs. To go over, add --over \"<chalach's words>\"",
                      p.stderr)
        ob = self.events("over_budget")
        self.assertEqual([(e["ask_id"], e["status"]) for e in ob], [(a, "refused")])
        self.assertEqual(len([c for c in self.sb.calls()]), 1)  # nothing started
        self.assertFalse(os.path.isdir(os.path.join(self.sb.runs, ob[0]["run_id"])))  # the reserved folder is gone
        p = self.run_ck("--ask", a, "--over", "chalach 18:02: one more is fine")
        self.assertEqual(p.returncode, 0, p.stderr)
        rid = self.sb.run_id_from(p)
        ob = self.events("over_budget")[-1]
        self.assertEqual((ob["status"], ob["over"], ob["run_id"]), ("approved", "chalach 18:02: one more is fine", rid))
        self.assertTrue(any("passed with chalach's words" in n for n in self.sb.result(rid)["notes"]))
        thread = self.sb.ck("thread", rid).stdout
        self.assertIn("CK → claude · dispatch", thread)  # the approved over_budget event is not the opening
        self.assertIn("over budget **approved**", thread)
        self.assertEqual(self.run_ck("--ask", a, "--over", " ").returncode, 2)

    def test_m_fix_rounds_and_top_tier(self):
        a = self.sb.open_ask("M")
        first = self.sb.run_id_from(self.run_ck("--ask", a))
        p = self.run_ck("--ask", a, "--parent", first)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.run_ck("--ask", a, "--parent", first)
        self.assertEqual(p.returncode, 7)
        self.assertIn("used 2/2 runs; used 1/1 fixes", p.stderr)
        b = self.sb.open_ask("M")
        p = self.run_ck("--ask", b, "--tier", "top")
        self.assertEqual(p.returncode, 7)
        self.assertIn("top-tier", p.stderr)
        p = self.run_ck("--ask", b, "--model", "astra", agent=None)  # a top model chalach named: allowed
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(["-m", "gpt-6-astra"], pairs(self.last_call("codex")["argv"]))

    def test_race_budget_runs_top_tier(self):
        a = self.sb.open_ask("S", "race: why is the coupling stuck", "race: why?", "--pattern", "race")
        for alias, agent in (("astra", "codex"), ("opus", "claude"), ("grok", "cursor")):
            with self.subTest(alias=alias):
                p = self.run_ck("--ask", a, "--model", alias, agent=None)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(self.last_call(agent)["agent"], agent)
        p = self.run_ck("--ask", a, "--model", "opus", agent=None)
        self.assertEqual(p.returncode, 7)
        self.assertIn("used 3/3 runs", p.stderr)
        b = self.sb.open_ask("S", "race b", "race b", "--pattern", "race")
        p = self.run_ck("--ask", b)  # no --model: race runs default to the top tier
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(["--effort", "xhigh"], pairs(self.last_call("claude")["argv"]))

    def test_size_sets_the_default_timeout(self):
        for size, want in (("S", 600), ("M", 1800), ("L", 7200)):
            with self.subTest(size=size):
                a = self.sb.open_ask(size)
                p = self.run_ck("--ask", a)
                self.assertEqual(p.returncode, 0, p.stderr)
                with open(os.path.join(self.sb.runs, self.sb.run_id_from(p), "packet.json")) as f:
                    self.assertEqual(json.load(f)["budget"]["timeout_s"], want)
        a = self.sb.open_ask("S")
        with open(os.path.join(self.sb.runs, self.sb.run_id_from(self.run_ck("--ask", a, "--timeout", "90")),
                               "packet.json")) as f:
            self.assertEqual(json.load(f)["budget"]["timeout_s"], 90)

    def test_closed_and_parked_asks(self):
        a = self.sb.open_ask("M")
        self.assertEqual(self.sb.ck("ask", "park", a, "--reason", "waits for chalach").returncode, 0)
        p = self.run_ck("--ask", a)
        self.assertEqual(p.returncode, 7)
        self.assertIn("is parked (waits for chalach)", p.stderr)
        self.assertEqual(self.run_ck("--ask", a, "--over", "chalach: go").returncode, 0)
        self.assertEqual(self.sb.ck("ask", "close", a).returncode, 0)
        p = self.run_ck("--ask", a, "--over", "chalach: go")
        self.assertEqual(p.returncode, 2)
        self.assertIn("closed; open a new ask", p.stderr)

    def test_dry_run_checks_the_budget_but_writes_nothing(self):
        a = self.sb.open_ask("S")
        self.run_ck("--ask", a)
        n = len(self.sb.ledger())
        p = self.run_ck("--ask", a, "--dry-run")
        self.assertEqual(p.returncode, 7)
        self.assertEqual(len(self.sb.ledger()), n)


class TestReviewReasons(Base):
    def test_trigger_rules_and_ask_inheritance(self):
        a = self.sb.open_ask("M")
        rid = self.sb.run_id_from(self.run_ck("--ask", a))
        p = self.sb.ck("review", rid, "--by", "codex", "--no-live")
        self.assertEqual(p.returncode, 2)
        self.assertIn("needs --trigger", p.stderr)
        p = self.sb.ck("review", rid, "--by", "codex", "--trigger", "leaves-lab", "--no-live")  # no --ask
        self.assertEqual(p.returncode, 0, p.stderr)
        rev = self.sb.run_id_from(p)
        ev = self.events("review", rev)[0]
        self.assertEqual((ev["ask_id"], ev["trigger"]), (a, "leaves-lab"))  # inherited from the reviewed run
        p = self.sb.ck("review", rid, "--by", "agy", "--trigger", "agent-unsure", "--no-live")
        self.assertEqual(p.returncode, 7)
        self.assertIn("used 1/1 reviews", p.stderr)
        s = self.sb.open_ask("S")
        rid = self.sb.run_id_from(self.run_ck("--ask", s))
        p = self.sb.ck("review", rid, "--by", "codex", "--trigger", "leaves-lab", "--no-live")
        self.assertEqual(p.returncode, 2)
        self.assertIn("you-asked", p.stderr)
        p = self.sb.ck("review", rid, "--model", "sol", "--trigger", "you-asked", "--no-live")  # --by from the alias
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.events("review", self.sb.run_id_from(p))[0]["agent"], "codex")
        self.assertEqual(self.sb.ck("review", rid, "--by", "agy", "--trigger", "you-asked", "--no-live").returncode, 7)

    def test_review_without_ask_keeps_the_old_behaviour_unless_required(self):
        rid = self.sb.run_id_from(self.run_ck())
        self.assertEqual(self.sb.ck("review", rid, "--by", "codex", "--no-live").returncode, 0)
        self.sb.set_section("asks", required=True)
        p = self.sb.ck("review", rid, "--by", "agy", "--no-live", "--trigger", "you-asked")
        self.assertEqual(p.returncode, 2)
        self.assertIn("ck-run ask open", p.stderr)


class TestModelsEndToEnd(Base):
    def test_alias_picks_agent_model_and_effort(self):
        p = self.run_ck("--model", "sol", agent=None)
        self.assertEqual(p.returncode, 0, p.stderr)
        a = self.last_call("codex")["argv"]
        self.assertIn(["-m", "gpt-6.1-sol"], pairs(a))
        self.assertIn(["-c", 'model_reasoning_effort="high"'], pairs(a))
        ev = self.events("dispatch", self.sb.run_id_from(p))[0]
        self.assertEqual((ev["agent"], ev["model"], ev["model_alias"], ev["tier"]), ("codex", "gpt-6.1-sol", "sol", "strong"))
        p = self.run_ck("--model", "sol", agent="claude")
        self.assertEqual(p.returncode, 2)
        self.assertIn("runs on codex", p.stderr)
        p = self.run_ck("--model", "gpt-9-made-up", agent=None)
        self.assertEqual(p.returncode, 2)
        self.assertIn("not an alias", p.stderr)

    def test_default_models_per_agent(self):
        self.assertEqual(self.run_ck(agent="claude").returncode, 0)
        a = self.last_call("claude")["argv"]
        self.assertIn(["--model", "opus"], pairs(a))
        self.assertIn(["--effort", "high"], pairs(a))
        s = self.sb.open_ask("S")
        self.assertEqual(self.run_ck("--ask", s, agent="claude").returncode, 0)
        self.assertIn(["--effort", "medium"], pairs(self.last_call("claude")["argv"]))  # S: sonnet
        self.assertIn(["--model", "sonnet"], pairs(self.last_call("claude")["argv"]))
        self.assertEqual(self.run_ck(agent="cursor").returncode, 0)
        self.assertIn(["--model", "grok-4.7-high"], pairs(self.last_call("cursor")["argv"]))
        self.assertEqual(self.run_ck(agent="agy").returncode, 0)
        a = self.last_call("agy")["argv"]
        self.assertNotIn("--model", a)
        self.assertNotIn("--effort", a)
        self.assertEqual(self.run_ck("--tier", "fast", agent="codex").returncode, 0)
        self.assertIn(["-c", 'model_reasoning_effort="low"'], pairs(self.last_call("codex")["argv"]))

    def test_ck_passed_effort_flags_are_refused(self):
        for bad in (["--effort", "high"], ["-c", 'model_reasoning_effort="high"']):
            with self.subTest(bad=bad):
                p = self.run_ck(*bad, agent="codex")
                self.assertEqual(p.returncode, 2)
                self.assertIn("not accepted", p.stderr)
        self.assertEqual(self.sb.calls(), [])

    def test_live_claude_argv_with_effort(self):
        self.sb.window("run")
        p = self.run_ck("--model", "opus-max", agent=None, live=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        call = [c for c in self.sb.calls() if c["live"]][-1]
        self.assertIn(["--effort", "xhigh"], pairs(call["argv"]))
        self.assertEqual(call["argv"][-3:-1], ["--permission-mode", "plan"])


class TestPostReviewSwitch(Base):
    def auto_cursor(self):
        return self.sb.ck("run", "--agent", "cursor", "--workspace", "devfab", "--title", "t", "--prompt-file",
                          self.sb.task(), "--mode", "auto", "--write-scope", "Projects/Notes", "--no-live")

    def test_off_skips_the_review_and_says_why(self):
        self.sb.set_section("post_review", enabled=False)
        p = self.auto_cursor()
        self.assertEqual(p.returncode, 0, p.stderr)
        rid = self.sb.run_id_from(p)
        self.assertEqual([e["event"] for e in self.events(run_id=rid)], ["dispatch", "result"])
        res = self.sb.result(rid)
        self.assertIsNone(res["post_review"])
        self.assertTrue(res["post_review_off"])
        self.assertIn(runner.POST_REVIEW_OFF, res["notes"])
        self.assertIn('"no need review, since those one are for simple stupid task anyways"',
                      self.events("result", rid)[0]["note"])
        self.assertFalse([c for c in self.sb.calls() if c["agent"] == "claude"])  # no reviewer call
        self.assertNotIn("Claude Code review checks this run", self.last_call("cursor")["prompt"])
        dry = self.sb.ck("run", "--agent", "agy", "--workspace", "devfab", "--title", "t", "--prompt-file",
                         self.sb.task(), "--mode", "auto", "--dry-run")
        self.assertIn("post-run review off (chalach 2026-11-05", dry.stdout)

    def test_on_keeps_the_review(self):
        p = self.auto_cursor()  # the sandbox default: post_review.enabled true
        self.assertEqual(p.returncode, 0, p.stderr)
        rid = self.sb.run_id_from(p)
        self.assertEqual([e["event"] for e in self.events(run_id=rid)], ["dispatch", "cursor_review", "result"])
        self.assertNotIn(runner.POST_REVIEW_OFF, self.sb.result(rid)["notes"])


class TestResultV4(Base):
    def test_schema_and_parser_accept_v4(self):
        schema = load_schema(RESULT_SCHEMA)
        r = {"run_id": "r", "status": "ok", "summary": ["a"], "report_md": "x", "findings": [], "files_read": [],
             "files_changed": [], "proposed_changes": "", "open_questions": [], "needs_approval": [],
             "numbers": [{"value": "5", "unit": "%", "source": "a.csv:3", "verified": True,
                          "what_else": "not the detector dead time (checked at 1 kHz)", "limit": "fibre NA"}], **V4}
        self.assertEqual(validate(r, schema), [])
        got = summarize.extract_structured(f"Done.\n```json\n{json.dumps(r)}\n```\n")
        self.assertEqual(got, r)
        v = summarize.v4_fields(got)
        self.assertEqual((v["verification"], v["plain_summary"], v["your_move"]),
                         ("fake_or_tests", V4["plain_summary"], "Try script 01 on the lab PC."))
        self.assertEqual(v["decisions"][0]["source"], "datasheet")
        old = {k: r[k] for k in ("run_id", "status", "summary", "report_md", "findings", "files_read",
                                 "files_changed", "proposed_changes", "open_questions", "needs_approval")}
        old["numbers"] = [{"value": "5", "unit": "%", "source": "a.csv:3", "verified": True}]
        hard, missing = summarize.split_v4_errors(validate(old, schema))
        self.assertEqual(hard, [])
        self.assertEqual(set(missing), set(summarize.V4_FIELDS) | {"what_else", "limit"})
        hard, _ = summarize.split_v4_errors(validate(dict(r, verification="pretty sure"), schema))
        self.assertTrue(hard)
        self.assertEqual(summarize.lowest_verification(["hardware", "desk_check", None]), "desk_check")
        self.assertEqual(summarize.v4_fields({"your_move": "Nothing."})["your_move"], "")

    def test_a_v4_run_feeds_the_finish_card(self):
        self.install_v4_stub("claude")
        self.sb.control(claude={"v4": V4})
        a = self.sb.open_ask("M", "SPCM kit -> wiki", "just use this and note it as wiki for now")
        p = self.run_ck("--ask", a)
        self.assertEqual(p.returncode, 0, p.stderr)
        rid = self.sb.run_id_from(p)
        res = self.sb.result(rid)
        self.assertEqual(res["status"], "ok", res["notes"])
        self.assertEqual(res["v4"]["verification"], "fake_or_tests")
        self.assertEqual(res["v4_missing"], [])
        with open(os.path.join(self.sb.runs, rid, "result.md")) as f:
            md = f.read()
        self.assertIn("## For chalach (plain words)", md)
        self.assertIn("[datasheet: USB-6343 p.12]", md)
        old = self.sb.run_id_from(self.run_ck(agent="codex"))  # the plain stub: no v4 fields -> a note, still ok
        self.assertEqual(self.sb.result(old)["status"], "ok")
        self.assertTrue(any("no v4 field" in n for n in self.sb.result(old)["notes"]))
        card = self.sb.ck("ask", "close", a).stdout
        self.assertIn("DONE", card)
        self.assertIn("Changed: Saved the kit next to your script.", card)
        self.assertIn("How sure: fake data or tests", card)
        self.assertIn("You should know: The SPCM sends one short pulse per photon.", card)
        self.assertIn("Your move: Try script 01 on the lab PC.", card)
        self.assertIn("Later (1): 1) datasheet check", card)
        self.assertNotIn(rid, card)
        self.assertLessEqual(len(card.strip().splitlines()), 8)
        self.assertEqual([e["run_id"] for e in self.events("accept")], [rid])

    def test_prompt_tells_the_agent_the_v4_rules(self):
        self.assertEqual(self.run_ck().returncode, 0)
        prompt = self.last_call("claude")["prompt"]
        for text in ("SCOPE: do only the steps in the task", '"later"', '"plain_summary"', '"verification"',
                     '"decisions"', "Never invent a number", '"what_else"', '"your_move"', "the LOWEST level",
                     "FIRST: read ~/PhD_local/ckstack/agents/ck-worker.md"):
            self.assertIn(text, prompt)


class TestLiveHandOff(Base):
    """E2's live.py gains group/state_dir and watch (BUILD_SPEC_v4 10.5). The runner passes them only when the
    signature has them, and maps the watchdog's 'stalled' to status timeout, exit 3."""

    def test_accepts(self):
        self.assertTrue(runner._accepts(lambda a, watch=None: 0, "watch"))
        self.assertFalse(runner._accepts(lambda a, b=1: 0, "watch"))
        self.assertTrue(runner._accepts(lambda a, **kw: 0, "group", "state_dir"))

    def test_stalled_is_a_timeout_with_the_watchdog_note(self):
        self.sb.set_live(watchdog=True)
        self.sb.window("run")
        a = self.sb.open_ask("S")
        seen = {}
        real_open = live.open_window

        def fake_open(osascript, command, title, env, terminal="Terminal", profile="", group=None, state_dir=None):
            seen["group"], seen["state_dir"] = group, state_dir
            return real_open(osascript, command, title, env, terminal=terminal, profile=profile)

        def fake_wait(run_dir, deadline, poll_s, result_grace_s=10.0, watch=None):
            seen["watch"] = watch
            return {"state": "stalled", "exit_code": None}

        out, err = io.StringIO(), io.StringIO()
        env = {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config}
        with mock.patch.dict(os.environ, env), mock.patch.object(live, "open_window", fake_open), \
                mock.patch.object(live, "wait_done", fake_wait), redirect_stdout(out), redirect_stderr(err):
            cfg = Config()
            spec = runner.Spec(agent="claude", ws=cfg.workspace("devfab"), title="stall test", task_text="do it",
                               mode="read", live=True, ask_id=a)
            code = runner.execute(cfg, spec)
        self.assertEqual(code, 3, err.getvalue())
        self.assertEqual((seen["group"], seen["state_dir"]), (a, os.path.join(self.sb.runs, ".windows")))
        self.assertTrue(WATCH_KEYS <= set(seen["watch"]), seen["watch"])
        self.assertEqual((seen["watch"]["stall_s"], seen["watch"]["ask_id"], seen["watch"]["run_id"]),
                         (180.0, a, spec.run_id))
        res = self.sb.result(spec.run_id)
        self.assertEqual((res["status"], res["live"]["state"]), ("timeout", "stalled"))
        self.assertIn("stopped by the watchdog: no activity for 360 s", res["notes"])

    def test_old_live_signatures_get_no_new_arguments(self):
        self.sb.set_live(watchdog=True)
        self.sb.window("run")
        a = self.sb.open_ask("M")
        real_open, real_wait = live.open_window, live.wait_done

        def old_open(osascript, command, title, env, terminal="Terminal", profile=""):
            return real_open(osascript, command, title, env, terminal=terminal, profile=profile)

        def old_wait(run_dir, deadline, poll_s, result_grace_s=10.0):
            return real_wait(run_dir, deadline, poll_s, result_grace_s)

        out, err = io.StringIO(), io.StringIO()
        env = {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config}
        with mock.patch.dict(os.environ, env), mock.patch.object(live, "open_window", old_open), \
                mock.patch.object(live, "wait_done", old_wait), redirect_stdout(out), redirect_stderr(err):
            cfg = Config()
            spec = runner.Spec(agent="codex", ws=cfg.workspace("devfab"), title="old live", task_text="do it",
                               mode="read", live=True, ask_id=a)
            code = runner.execute(cfg, spec)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(self.sb.result(spec.run_id)["live"]["state"], "done")


class TestExtensions(Base):
    def test_missing_and_broken_extensions_are_skipped(self):
        bad = types.ModuleType("ckdispatch.badext")
        bad.register = lambda sub: 1 / 0
        err = io.StringIO()
        with mock.patch.object(cli, "EXTENSIONS", ("nosuchext", "badext") + cli.EXTENSIONS), \
                mock.patch.dict(sys.modules, {"ckdispatch.badext": bad}), redirect_stderr(err):
            ap = cli._parser()
        self.assertNotIn("nosuchext", err.getvalue())
        self.assertIn("the badext commands did not register", err.getvalue())
        self.assertEqual(ap.parse_args(["ask", "list"]).ask_cmd, "list")
        self.assertEqual(ap.parse_args(["models"]).cmd, "models")


class TestFinalize(Base):
    def call(self, run_id, note="test note"):
        out, err = io.StringIO(), io.StringIO()
        env = {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config}
        with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
            code = runner.finalize_run(Config(), run_id, note=note)
        return code, out.getvalue(), err.getvalue()

    def drop_result(self, rid):
        """Make the run look like its ck-run watcher died: no result event, no ck-run files after the agent."""
        path = os.path.join(self.sb.runs, "ledger.jsonl")
        with open(path) as f:
            lines = [x for x in f if not (json.loads(x)["run_id"] == rid and json.loads(x)["event"] == "result")]
        with open(path, "w") as f:
            f.writelines(lines)
        for n in ("result.json", "rulecheck.json"):
            os.remove(os.path.join(self.sb.runs, rid, n))

    def test_finished_run_is_printed_not_redone(self):
        rid = self.sb.run_id_from(self.run_ck())
        code, out, _ = self.call(rid)
        self.assertEqual(code, 0)
        self.assertIn("Stub claude finished the task.", out)
        self.assertEqual(len(self.events("result", rid)), 1)

    def test_headless_run_is_finalized_once(self):
        a = self.sb.open_ask("M")
        rid = self.sb.run_id_from(self.run_ck("--ask", a))
        self.drop_result(rid)
        for n in ("result.md", "summary.md"):
            os.remove(os.path.join(self.sb.runs, rid, n))
        code, out, _ = self.call(rid, note="watcher died at 18:20")
        self.assertEqual(code, 0, out)
        self.assertIn("Stub claude finished the task.", out)
        res = self.events("result", rid)
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["note"].startswith("finalized by ck-run reattach: watcher died at 18:20"))
        self.assertEqual((res[0]["ask_id"], res[0]["status"]), (a, "ok"))
        self.assertEqual(self.sb.result(rid)["finalized"], "watcher died at 18:20")
        for n in ("result.json", "result.md", "summary.md", "rulecheck.json"):
            self.assertTrue(os.path.exists(os.path.join(self.sb.runs, rid, n)), n)
        code2, out2, _ = self.call(rid, note="again")
        self.assertEqual((code2, len(self.events("result", rid))), (0, 1))
        self.assertIn("Stub claude finished the task.", out2)

    def test_live_run_is_finalized_from_its_outbox(self):
        self.sb.window("run")
        rid = self.sb.run_id_from(self.run_ck(agent="codex", live=True))
        self.drop_result(rid)
        code, out, _ = self.call(rid)
        self.assertEqual(code, 0, out)
        res = self.sb.result(rid)
        self.assertEqual((res["status"], res["live"]["state"]), ("ok", "done"))

    def test_refused_while_the_agent_still_runs(self):
        rid = self.sb.run_id_from(self.run_ck())
        self.drop_result(rid)
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with open(os.path.join(self.sb.runs, rid, "live.pid"), "w") as f:
                f.write(str(sleeper.pid))
            with self.assertRaises(Refused) as c:
                self.call(rid)
            self.assertIn("agent still running", str(c.exception))
            self.assertEqual(self.events("result", rid), [])
        finally:
            sleeper.kill()
            sleeper.wait()
        for bad in ("nope", "2026-01-01T000000-devfab-general-claude-abcd"):
            with self.assertRaises(Refused):
                self.call(bad)


if __name__ == "__main__":
    unittest.main()
