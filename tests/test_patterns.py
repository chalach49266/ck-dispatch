"""v4 patterns (BUILD_SPEC_v4 10.10): ck-run race, ck-run race show, ck-run panel.

Unit tests replace launch.start with a recorder (nothing starts). The end-to-end tests use E2's real launch.start in
the test Sandbox: the launched ck-run runs use the stub CLIs and the mock osascript; they are skipped when launch or
the CLI registration is not there yet.
"""
import json
import os
import re
import time
import unittest
from unittest import mock

from test_views import Fixture, FixtureCase, cli_registers

from ckdispatch import patterns
from ckdispatch.errors import Refused


class Recorder:
    """Stands in for launch.start: records (slug, argv, ask_id), never starts anything."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, cfg, slug, argv, *, ask_id=None, cwd=None):
        if slug in self.fail:
            raise Refused("slug used; pick a new one")
        self.calls.append({"slug": slug, "argv": list(argv), "ask_id": ask_id})
        return {"slug": slug, "sh": os.path.join(cfg.runs_dir, slug + ".sh"),
                "out": os.path.join(cfg.runs_dir, slug + ".out"), "pid": 4321}


def opt(argv, name):
    return next((argv[i + 1] for i, t in enumerate(argv[:-1]) if t == name), None)


class PatternCase(FixtureCase):
    def setUp(self):
        super().setUp()
        self.rec = Recorder()
        self.patch = mock.patch.object(patterns, "launch_start", self.rec)
        self.patch.start()
        self.prompt = self.fx.sb.task("Why is the fibre coupling stuck at 5%? Answer in one paragraph.", "q.md")

    def tearDown(self):
        self.patch.stop()
        super().tearDown()


class TestRace(PatternCase):
    def test_race_launches_one_run_per_model(self):
        fx = self.fx
        fx.ask("A-1105-7", "fibre coupling stuck", size="S", pattern="race")
        code, out, err = fx.cli("race", "--ask", "A-1105-7", "--prompt-file", self.prompt, "--title", "fibre at 5%",
                                "--workspace", "Device Fabrication", "--timeout", "600")
        self.assertEqual(code, 0, err)
        mc = patterns.models_config()
        models = (mc or {}).get("race_default") or patterns.DEFAULT_RACE
        slugs = [f"A-1105-7-race-{m}" for m in models]
        self.assertEqual(out.split(), slugs)
        self.assertEqual([c["slug"] for c in self.rec.calls], slugs)
        for alias, c in zip(models, self.rec.calls):
            a = c["argv"]
            self.assertEqual(a[0], "run")
            self.assertEqual(c["ask_id"], "A-1105-7")
            self.assertEqual(opt(a, "--model"), alias)
            self.assertEqual(opt(a, "--ask"), "A-1105-7")
            self.assertEqual(opt(a, "--workspace"), "devfab")
            self.assertEqual(opt(a, "--mode"), "read")
            self.assertEqual(opt(a, "--timeout"), "600")
            self.assertEqual(opt(a, "--title"), f"fibre at 5% (race: {alias})")
            copy = opt(a, "--prompt-file")
            self.assertEqual(copy, os.path.join(fx.runs, f"A-1105-7-race-{alias}_prompt.md"))
            with open(copy) as f:
                self.assertEqual(f.read(), "Why is the fibre coupling stuck at 5%? Answer in one paragraph.")
            if mc:
                self.assertEqual(opt(a, "--agent"), mc["aliases"][alias]["agent"])
        ev = [e for e in fx.sb.ledger() if e["event"] == "race"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0].get("ask_id"), "A-1105-7")
        self.assertIn("slugs=" + ",".join(slugs), ev[0]["note"])

    def test_models_option_and_auto_mode(self):
        fx = self.fx
        fx.ask("A-1105-8", "decision", size="L")
        code, out, err = fx.cli("race", "--ask", "A-1105-8", "--prompt-file", self.prompt, "--title", "pick a shape",
                                "--workspace", "nv", "--models", "sol, opus", "--mode", "auto")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.split(), ["A-1105-8-race-sol", "A-1105-8-race-opus"])
        self.assertEqual([opt(c["argv"], "--mode") for c in self.rec.calls], ["auto", "auto"])
        self.assertIsNone(opt(self.rec.calls[0]["argv"], "--timeout"))

    def test_refusals_start_nothing(self):
        fx = self.fx
        fx.ask("A-1105-1", "open")
        fx.ask("A-1105-2", "closed")
        fx.close_ask("A-1105-2")
        fx.ask("A-1105-3", "parked")
        fx.add(event="ask_park", ask_id="A-1105-3", reason="over time")
        base = ["--prompt-file", self.prompt, "--title", "t", "--workspace", "devfab"]
        cases = [
            (["race", "--ask", "A-1105-9"] + base, 2, "unknown ask A-1105-9"),
            (["race", "--ask", "bad-id"] + base, 2, "is not an ask id"),
            (["race", "--ask", "A-1105-2"] + base, 2, "is closed; open a new ask"),
            (["race", "--ask", "A-1105-3"] + base, 7, "is parked (over time)"),
            (["race", "--ask", "A-1105-1", "--title", "t", "--workspace", "devfab"], 2, "race needs --prompt-file"),
            (["race", "--ask", "A-1105-1", "--prompt-file", "/nope.md", "--title", "t", "--workspace", "devfab"], 2,
             "prompt file not found"),
            (["race", "--ask", "A-1105-1", "--workspace", "mars", "--prompt-file", self.prompt, "--title", "t"], 2,
             "unknown workspace"),
            (["race", "--ask", "A-1105-1", "--models", "opus"] + base, 2, "needs 2 to 5 models"),
            (["race", "--ask", "A-1105-1", "--models", "opus,opus"] + base, 2, "names a model twice"),
        ]
        if patterns.models_config():
            cases.append((["race", "--ask", "A-1105-1", "--models", "opus,gpt-9"] + base, 2,
                          "unknown model alias gpt-9"))
        for argv, want, text in cases:
            code, out, err = fx.cli(*argv)
            self.assertEqual(code, want, f"{argv}: {err}")
            self.assertIn(text, err, argv)
        self.assertEqual(self.rec.calls, [])
        self.assertFalse([e for e in fx.sb.ledger() if e["event"] == "race"])

    def test_used_slug_and_partial_start(self):
        fx = self.fx
        fx.ask("A-1105-4", "race twice")
        with open(os.path.join(fx.runs, "A-1105-4-race-opus.out"), "w") as f:
            f.write("ck-run exit=0\n")
        code, _, err = fx.cli("race", "--ask", "A-1105-4", "--prompt-file", self.prompt, "--title", "t",
                              "--workspace", "devfab", "--models", "sol,opus")
        self.assertEqual(code, 2)
        self.assertIn("slug A-1105-4-race-opus was used before", err)
        self.assertEqual(self.rec.calls, [])
        fx.ask("A-1105-5", "one fails")
        self.rec.fail = {"A-1105-5-race-opus"}
        code, out, err = fx.cli("race", "--ask", "A-1105-5", "--prompt-file", self.prompt, "--title", "t",
                                "--workspace", "devfab", "--models", "sol,opus,grok")
        self.assertEqual(code, 1)
        self.assertEqual(out.split(), ["A-1105-5-race-sol", "A-1105-5-race-grok"])
        self.assertIn("NOT STARTED: A-1105-5-race-opus", err)
        ev = [e for e in fx.sb.ledger() if e["event"] == "race"][-1]
        self.assertIn("slugs=A-1105-5-race-sol,A-1105-5-race-grok", ev["note"])

    def test_missing_launch_module_is_a_clear_refusal(self):
        self.patch.stop()
        try:
            with mock.patch("importlib.import_module", side_effect=ImportError("no launch")):
                with self.assertRaises(Refused) as cm:
                    patterns.launch_start(self.fx.cfg, "s", ["run"])
            self.assertIn("ck-run launch is not installed", str(cm.exception))
        finally:
            self.patch.start()


class TestRaceShow(PatternCase):
    def test_side_by_side(self):
        fx = self.fx
        fx.ask("A-1105-7", "fibre coupling stuck", pattern="race")
        rid = fx.run("A-1105-7", agent="codex", alias="astra", title="fibre (race: astra)", duration_s=420,
                     agent_result={"plain_summary": ["Mode mismatch limits it.", "Check the lensed fibre spot."],
                                   "verification": "desk_check", "your_move": "measure the spot size"})
        with open(os.path.join(fx.runs, "A-1105-7-race-astra.out"), "w") as f:
            f.write(os.path.join(fx.runs, rid, "summary.md") + "\nsummary text\nck-run exit=0\n")
        open(os.path.join(fx.runs, "A-1105-7-race-astra.sh"), "w").close()
        live = fx.run("A-1105-7", agent="claude", alias="opus", title="fibre (race: opus)", finished=False)
        fx.live(live, agent="claude")
        with open(os.path.join(fx.runs, "A-1105-7-race-opus.out"), "w") as f:
            f.write("")
        with open(os.path.join(fx.runs, "A-1105-7-race-grok.out"), "w") as f:
            f.write("ck-run: OVER BUDGET: ask A-1105-7 (M) used 2/2 runs\nck-run exit=7\n")
        fx.add(event="race", ask_id="A-1105-7",
               note="slugs=A-1105-7-race-astra,A-1105-7-race-opus,A-1105-7-race-grok; models=astra,opus,grok")
        with mock.patch.dict(os.environ, {"COLUMNS": "150"}):
            code, out, err = fx.cli("race", "show", "--ask", "A-1105-7")
        self.assertEqual(code, 0, err)
        head = out.splitlines()[2]
        self.assertRegex(head, r"^astra \(codex\)\s+opus \(claude\)\s+grok$")
        self.assertIn("state: ok, 7.0 min", out)
        self.assertIn("Mode mismatch limits it.", out)
        self.assertIn("how sure: desk check", out)
        self.assertIn("your move: measure the spot size", out)
        self.assertIn("state: running", out)
        self.assertIn("state: ended, exit 7 (over budget)", out)
        self.assertIn("2 of 3 finished", out)
        code, out, _ = fx.cli("race", "show", "--ask", "A-1105-7", "--json")
        d = json.loads(out)
        self.assertEqual([r["alias"] for r in d["runs"]], ["astra", "opus", "grok"])
        self.assertEqual(d["runs"][0]["run_id"], rid)
        self.assertEqual(d["runs"][0]["summary"], ["Mode mismatch limits it.", "Check the lensed fibre spot."])

    def test_out_file_with_stderr_first(self):
        """A launch's .out holds stderr too: the summary path is not always line 1."""
        fx = self.fx
        rid = fx.new_id()
        text = ("ck-run: LIVE WINDOW FAILED for codex (run x): osascript could not open a window\n"
                "ck-run: running the same task headless instead (ledger event live_fallback).\n"
                f"{os.path.join(fx.runs, rid, 'summary.md')}\nsummary line\nck-run exit=0\n")
        self.assertEqual(patterns.summary_run_id(text), rid)
        self.assertIsNone(patterns.summary_run_id("no/summary.md here\n/x/not-a-run/summary.md\n"))
        fx.ask("A-1105-3", "only the .out knows")
        run = fx.run("A-1105-3", alias=None, model=None, title="plain title")  # no alias, no race marker
        with open(os.path.join(fx.runs, "A-1105-3-race-sol.out"), "w") as f:
            f.write(text.replace(rid, run))
        fx.add(event="race", ask_id="A-1105-3", note="slugs=A-1105-3-race-sol; models=sol")
        entries = patterns.race_entries(fx.snap(), "A-1105-3")
        self.assertEqual([(e["alias"], e["run_id"], e["state"]) for e in entries], [("sol", run, "ok")])

    def test_narrow_stacks_and_no_race(self):
        txt = patterns.side_by_side([("a", ["x" * 30]), ("b", ["y"]), ("c", ["z"])], 60)
        self.assertTrue(txt.startswith("== a\n"))
        self.fx.ask("A-1105-1", "no race here")
        code, out, _ = self.fx.cli("race", "show", "--ask", "A-1105-1")
        self.assertEqual(code, 0)
        self.assertIn("No race runs for this ask", out)
        code, _, err = self.fx.cli("race", "show", "--ask", "A-1105-9")
        self.assertEqual(code, 2)
        self.assertIn("unknown ask", err)


class TestPanel(PatternCase):
    def test_panel_launches_one_review_per_agent(self):
        fx = self.fx
        fx.ask("A-1105-9", "AQS 1-pager numbers", size="L", pattern="panel")
        rid = fx.run("A-1105-9", agent="claude", alias="opus", title="AQS 1-pager")
        code, out, err = fx.cli("panel", rid, "--by", "codex,cursor", "--trigger", "leaves-lab")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.split(), ["A-1105-9-panel-codex", "A-1105-9-panel-cursor"])
        a0, a1 = (c["argv"] for c in self.rec.calls)
        self.assertEqual(a0[:4], ["review", rid, "--by", "codex"])
        self.assertEqual((opt(a0, "--trigger"), opt(a0, "--ask")), ("leaves-lab", "A-1105-9"))
        self.assertEqual(opt(a0, "--focus"), "code and numbers")  # the rev2 lens per agent
        self.assertEqual(opt(a1, "--focus"), "sources")
        ev = [e for e in fx.sb.ledger() if e["event"] == "panel"][-1]
        self.assertEqual((ev.get("ask_id"), ev["run_id"]), ("A-1105-9", rid))
        self.assertIn("slugs=A-1105-9-panel-codex,A-1105-9-panel-cursor", ev["note"])

    def test_focus_and_inherited_ask(self):
        fx = self.fx
        fx.ask("A-1105-9", "x", size="L")
        rid = fx.run("A-1105-9", agent="codex")
        code, out, err = fx.cli("panel", rid, "--by", "claude,agy", "--trigger", "hardware-or-raw",
                                "--focus", "the DAQ timing")
        self.assertEqual(code, 0, err)
        self.assertEqual([opt(c["argv"], "--focus") for c in self.rec.calls], ["the DAQ timing", "the DAQ timing"])

    def test_refusals(self):
        fx = self.fx
        fx.ask("A-1105-1", "small", size="S")
        small = fx.run("A-1105-1", agent="codex")
        fx.ask("A-1105-2", "big", size="L")
        big = fx.run("A-1105-2", agent="codex")
        unfinished = fx.run("A-1105-2", agent="codex", finished=False)
        no_ask = fx.run(None, agent="codex")
        cases = [
            ([big, "--by", "codex,claude", "--trigger", "you-asked"], "another agent than the reviewed run's"),
            ([big, "--by", "claude", "--trigger", "you-asked"], "needs 2 or 3 reviewers"),
            ([big, "--by", "claude,gpt", "--trigger", "you-asked"], "unknown agent gpt"),
            ([big, "--by", "claude,claude", "--trigger", "you-asked"], "names an agent twice"),
            ([unfinished, "--by", "claude,cursor", "--trigger", "you-asked"], "has no result yet"),
            ([small, "--by", "claude,cursor", "--trigger", "leaves-lab"], "accepts only --trigger you-asked"),
            ([no_ask, "--by", "claude,cursor", "--trigger", "you-asked"], "has no ask: give --ask A"),
            (["2026-11-05T120000-devfab-general-codex-ffff", "--by", "claude,cursor", "--trigger", "you-asked"],
             "is not in the ledger"),
            (["nonsense", "--by", "claude,cursor", "--trigger", "you-asked"], "not a valid run id"),
        ]
        for argv, text in cases:
            code, out, err = fx.cli("panel", *argv)
            self.assertEqual(code, 2, f"{argv}: {err}")
            self.assertIn(text, err, argv)
        code, _, _ = fx.cli("panel", big, "--by", "claude,cursor", "--trigger", "because")
        self.assertEqual(code, 2)  # argparse: not one of the 4 reasons
        self.assertEqual(self.rec.calls, [])


class EndToEnd(unittest.TestCase):
    """Real ck-run commands in the Sandbox: ask open (E1), race/panel (E3) -> launch.start (E2) -> ck-run run/review
    with the stub CLIs. The launched runs fall back to headless (the mock osascript fails by default)."""

    def setUp(self):
        self.fx = Fixture()
        missing = [c for c in ("race", "panel", "launch", "ask") if not cli_registers(c)]
        if missing:
            self.fx.close()
            self.skipTest(f"ck-run does not have {', '.join(missing)} yet (E1/E2 integration)")
        self.sb = self.fx.sb

    def tearDown(self):
        self.fx.close()

    def open_ask(self, size, pattern):
        p = self.sb.ck("ask", "open", "--words", "test words", "--size", size, "--title", f"e2e {pattern}",
                       "--pattern", pattern)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.split()[0]

    def wait_outs(self, slugs, timeout=120):
        end = time.time() + timeout
        done = {}
        while time.time() < end and len(done) < len(slugs):
            for s in slugs:
                try:
                    with open(os.path.join(self.sb.runs, s + ".out")) as f:
                        m = re.findall(r"ck-run exit=(\d+)", f.read())
                except OSError:
                    m = []
                if m:
                    done[s] = int(m[-1])
            time.sleep(0.5)
        self.assertEqual(sorted(done), sorted(slugs), "launched runs did not finish in time")
        return done

    def test_race_then_show_then_panel(self):
        ask = self.open_ask("L", "race")
        task = self.sb.task("Which fibre is better? One line.", "race_task.md")
        p = self.sb.ck("race", "--ask", ask, "--prompt-file", task, "--title", "fibre pick", "--workspace", "devfab",
                       "--models", "sol,opus")
        self.assertEqual(p.returncode, 0, p.stderr)
        slugs = p.stdout.split()
        self.assertEqual(slugs, [f"{ask}-race-sol", f"{ask}-race-opus"])
        codes = self.wait_outs(slugs)
        self.assertTrue(all(c not in (2, 7) for c in codes.values()), codes)
        p = self.sb.ck("race", "show", "--ask", ask, "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        runs = json.loads(p.stdout)["runs"]
        self.assertEqual([r["alias"] for r in runs], ["sol", "opus"])
        self.assertTrue(all(r["run_id"] for r in runs), runs)
        target = runs[0]["run_id"]  # a codex run: review it with claude and cursor
        p = self.sb.ck("panel", target, "--by", "claude,cursor", "--trigger", "you-asked")
        self.assertEqual(p.returncode, 0, p.stderr)
        pslugs = p.stdout.split()
        self.assertEqual(pslugs, [f"{ask}-panel-claude", f"{ask}-panel-cursor"])
        codes = self.wait_outs(pslugs)
        self.assertTrue(all(c not in (2,) for c in codes.values()), codes)


if __name__ == "__main__":
    unittest.main()
