"""v4 loop (BUILD_SPEC_v4 10.11): ck-run loop, a hillclimb of runs and a metric until a target.

Unit tests give run_loop a fake `run` (no ck-run starts) and a real /bin/bash metric that reads a number from a file
in the sandbox. The end-to-end test starts `ck-run launch <slug> loop ...` (E2) in the Sandbox with the stub CLIs.
"""
import os
import time
import unittest
from unittest import mock

from test_views import Fixture, FixtureCase, cli_registers

from ckdispatch import loop
from ckdispatch.errors import Refused


class FakeRuns:
    """Stands in for loop.run_once: each call is one 'run' that may change the metric file."""

    def __init__(self, fx, value_file, steps, codes=None):
        self.fx, self.value_file, self.steps = fx, value_file, list(steps)
        self.codes = list(codes or [])
        self.calls = []

    def __call__(self, argv):
        i = len(self.calls)
        self.calls.append(list(argv))
        code = self.codes[i] if i < len(self.codes) else 0
        if code in (2, 7):
            return code, None, ""
        step = self.steps[i] if i < len(self.steps) else 0
        with open(self.value_file) as f:
            v = float(f.read().split()[-1])
        with open(self.value_file, "w") as f:
            f.write(f"metric {v + step:g}\n")
        rid = self.fx.new_id()
        return code, rid, os.path.join(self.fx.runs, rid, "summary.md") + "\nchanged one thing\n"


class LoopCase(FixtureCase):
    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.fx.sb.tmp, "work")
        os.makedirs(self.work)
        self.value = os.path.join(self.work, "value.txt")
        with open(self.value, "w") as f:
            f.write("metric 10\n")

    def spec(self, **kw):
        s = {"ask_id": "A-1105-7", "who": ["--agent", "codex"], "prompt_text": "Raise the Rabi frequency. One change.",
             "workspace": "devfab", "metric": "cat value.txt", "op": ">=", "target": 13.0, "max": 5, "cwd": self.work,
             "title": "rabi loop", "extra": []}
        s.update(kw)
        return s

    def go(self, runs, **kw):
        import io
        out = io.StringIO()
        code = loop.run_loop(self.fx.cfg, self.spec(**kw), run=runs, out=out)
        return code, out.getvalue()

    def tsv(self):
        with open(os.path.join(self.fx.runs, "A-1105-7", "loop_decisions.tsv")) as f:
            return [ln.rstrip("\n").split("\t") for ln in f]

    def iters(self):
        return [e for e in self.fx.sb.ledger() if e["event"] == "loop_iter"]


class TestPieces(unittest.TestCase):
    def test_target_number_direction(self):
        self.assertEqual(loop.parse_target(">=12.5"), (">=", 12.5))
        self.assertEqual(loop.parse_target(" < -3e-2 "), ("<", -0.03))
        for bad in ("12", "=>5", ">= x", "", "~5", ">=5 6"):
            with self.assertRaises(Refused, msg=bad):
                loop.parse_target(bad)
        self.assertEqual(loop.last_number("Rabi 3.2 MHz at 10 dBm\nscore: 41.5\n"), 41.5)
        self.assertEqual(loop.last_number("err -1.5e-3"), -0.0015)
        self.assertIsNone(loop.last_number("no digits here"))
        self.assertTrue(loop.better(">=", 2, 1) and loop.better("<", 1, 2))
        self.assertFalse(loop.better(">", 1, 1) or loop.better("<=", 2, 1))

    def test_metric_guard(self):
        self.assertIsNone(loop.metric_refusal("python3 score.py --quick"))
        for bad in ("python3 -c 'import tidy3d.web as w'", "cat ~/.ssh/id_rsa", "run --dangerously-skip-permissions"):
            self.assertIsNotNone(loop.metric_refusal(bad), bad)

    def test_measure(self):
        fx = Fixture()
        try:
            got = loop.measure("echo 'gain 3.5'; echo 'gain 4.25'", fx.sb.tmp)
            self.assertEqual((got["value"], got["exit"], got["note"]), (4.25, 0, ""))
            got = loop.measure("echo 7; exit 3", fx.sb.tmp)
            self.assertEqual(got["value"], 7.0)
            self.assertIn("metric exit 3", got["note"])
            got = loop.measure("echo none", fx.sb.tmp)
            self.assertIsNone(got["value"])
            got = loop.measure("sleep 5", fx.sb.tmp, timeout_s=0.5)
            self.assertIn("timed out", got["note"])
        finally:
            fx.close()


class TestRunLoop(LoopCase):
    def test_reaches_the_target(self):
        runs = FakeRuns(self.fx, self.value, steps=[1, 1, 1, 1])
        code, out = self.go(runs)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(runs.calls), 3)
        self.assertIn("stopped: the target >= 13 is met", out)
        rows = self.tsv()
        self.assertEqual(rows[0], ["ts", "i", "run_id", "metric", "kept"])
        self.assertEqual([r[1:] for r in rows[1:2]], [["0", "-", "10", "baseline"]])
        self.assertEqual([(r[1], r[3], r[4]) for r in rows[2:]], [("1", "11", "yes"), ("2", "12", "yes"),
                                                                    ("3", "13", "yes")])
        ev = self.iters()
        self.assertEqual([e["note"] for e in ev], ["i=1 metric=11 kept=yes", "i=2 metric=12 kept=yes",
                                                   "i=3 metric=13 kept=yes"])
        self.assertTrue(all(e.get("ask_id") == "A-1105-7" and e["run_id"] for e in ev))
        first, second = runs.calls[0], runs.calls[1]
        self.assertNotIn("--parent", first)
        self.assertEqual(second[second.index("--parent") + 1], ev[0]["run_id"])
        self.assertEqual(first[:3], ["run", "--agent", "codex"])
        self.assertEqual(first[first.index("--ask") + 1], "A-1105-7")
        self.assertEqual(first[first.index("--title") + 1], "rabi loop (loop round 1/5)")
        p2 = second[second.index("--prompt-file") + 1]
        self.assertEqual(p2, os.path.join(self.fx.runs, "A-1105-7", f"loop_prompt_2_{os.getpid()}.md"))
        with open(p2) as f:
            text = f.read()
        self.assertTrue(text.startswith("Raise the Rabi frequency. One change.\n"))
        self.assertIn("Loop so far (ck-run loop, ask A-1105-7; this is round 2 of 5; target: metric >= 13", text)
        self.assertIn("- baseline (before any change): 10", text)
        self.assertIn(f"- round 1 (run {ev[0]['run_id']}): 11, kept (better than before)", text)
        self.assertIn("Best so far: 11 (round 1).", text)

    def test_stops_after_two_rounds_without_gain(self):
        runs = FakeRuns(self.fx, self.value, steps=[1, 0, -1, 5])
        code, out = self.go(runs)
        self.assertEqual(code, 1)
        self.assertEqual(len(runs.calls), 3)
        self.assertIn("2 rounds in a row without improvement", out)
        self.assertEqual([r[4] for r in self.tsv()[2:]], ["yes", "no", "no"])

    def test_max_rounds_and_lower_is_better(self):
        with open(self.value, "w") as f:
            f.write("10\n")
        runs = FakeRuns(self.fx, self.value, steps=[-1, -1, -1, -1])
        code, out = self.go(runs, op="<=", target=1.0, max=2)
        self.assertEqual(code, 1)
        self.assertEqual(len(runs.calls), 2)
        self.assertIn("the last round (2) is done", out)
        self.assertEqual([(r[3], r[4]) for r in self.tsv()[2:]], [("9", "yes"), ("8", "yes")])

    def test_stop_exits(self):
        runs = FakeRuns(self.fx, self.value, steps=[1], codes=[0, 7])
        code, out = self.go(runs)
        self.assertEqual(code, 7)
        self.assertIn("the run exited 7 (over budget)", out)
        last = self.tsv()[-1]
        self.assertEqual((last[1], last[2], last[3], last[4]), ("2", "-", "", "no"))
        last_ev = self.iters()[-1]
        self.assertEqual(last_ev["note"], "i=2 metric=none kept=no")  # exactly the spec's note format
        self.assertEqual((last_ev.get("reason"), last_ev.get("status")), ("no run (exit 7)", "over_budget"))
        for c in (2, 4, 5):
            fx_dir = os.path.join(self.fx.runs, "A-1105-7")
            for n in os.listdir(fx_dir):
                os.unlink(os.path.join(fx_dir, n))
            with open(self.value, "w") as f:
                f.write("10\n")
            runs = FakeRuns(self.fx, self.value, steps=[1], codes=[c])
            code, out = self.go(runs)
            self.assertEqual(code, c, out)
            self.assertEqual(len(runs.calls), 1)

    def test_target_met_before_any_run_and_bad_metric_output(self):
        runs = FakeRuns(self.fx, self.value, steps=[])
        code, out = self.go(runs, target=9.0)
        self.assertEqual((code, runs.calls), (0, []))
        self.assertIn("already met before any change", out)
        os.unlink(os.path.join(self.fx.runs, "A-1105-7", "loop_decisions.tsv"))
        runs = FakeRuns(self.fx, self.value, steps=[1, 1])
        code, out = self.go(runs, metric="echo no number")
        self.assertEqual(code, 1)
        self.assertIn("no number in the metric output", out)
        self.assertEqual([r[3] for r in self.tsv()[1:]], ["", "", ""])


class TestCommand(LoopCase):
    def setUp(self):
        super().setUp()
        self.fx.ask("A-1105-7", "rabi loop", size="L", pattern="loop")
        self.task = self.fx.sb.task("Raise the Rabi frequency. One change.", "loop_task.md")

    def args(self, *more):
        return ["loop", "--ask", "A-1105-7", "--prompt-file", self.task, "--workspace", "devfab",
                "--metric", "cat value.txt", "--target", ">=12", "--max", "3", "--cwd", self.work] + list(more)

    def test_command_passes_flags_to_each_run(self):
        runs = FakeRuns(self.fx, self.value, steps=[1, 1])
        with mock.patch.object(loop, "run_once", runs):
            code, out, err = self.fx.cli(*self.args("--agent", "codex", "--timeout", "900", "--tier", "fast",
                                                    "--write-scope", "Design Library", "--title", "rabi"))
        self.assertEqual(code, 0, err)
        a = runs.calls[0]
        self.assertEqual(a[a.index("--timeout") + 1], "900")
        self.assertEqual(a[a.index("--tier") + 1], "fast")
        self.assertEqual(a[a.index("--write-scope") + 1], "Design Library")
        self.assertEqual(a[a.index("--title") + 1], "rabi (loop round 1/3)")
        self.assertIn("stopped: the target >= 12 is met", out)

    def test_model_alias(self):
        runs = FakeRuns(self.fx, self.value, steps=[5])
        with mock.patch.object(loop, "run_once", runs):
            code, out, err = self.fx.cli(*self.args("--model", "sol-fast"))
        self.assertEqual(code, 0, err)
        a = runs.calls[0]
        self.assertEqual(a[a.index("--model") + 1], "sol-fast")
        from ckdispatch import patterns
        mc = patterns.models_config()
        if mc:
            self.assertEqual(a[a.index("--agent") + 1], mc["aliases"]["sol-fast"]["agent"])

    def test_refusals(self):
        fx = self.fx
        fx.ask("A-1105-8", "closed")
        fx.close_ask("A-1105-8")
        base = ["--prompt-file", self.task, "--workspace", "devfab", "--metric", "cat value.txt", "--target", ">=12",
                "--max", "3", "--agent", "codex"]
        cases = [
            (["loop", "--ask", "A-1105-9"] + base, "unknown ask"),
            (["loop", "--ask", "A-1105-8"] + base, "is closed"),
            (self.args("--agent", "codex", "--target", "12"), "--target"),
            (self.args("--agent", "codex", "--max", "0"), "--max must be 1 to"),
            (self.args("--agent", "codex", "--metric", "python3 -c 'import tidy3d.web'"), "cloud sim"),
            (self.args("--agent", "codex", "--metric", "  "), "--metric is empty"),
            (self.args("--agent", "codex", "--cwd", "/no/such/folder"), "--cwd is not a folder"),
            (self.args("--agent", "codex", "--workspace", "mars"), "unknown workspace"),
        ]
        from ckdispatch import patterns
        if patterns.models_config():
            cases.append((self.args("--model", "gpt-9"), "not a model alias"))
        for argv, text in cases:
            code, out, err = fx.cli(*argv)
            self.assertEqual(code, 2, f"{argv}: {err}")
            self.assertIn(text, err, argv)
        code, _, _ = fx.cli(*self.args("--agent", "codex", "--model", "sol"))
        self.assertEqual(code, 2)  # argparse: --agent or --model, not both
        self.assertFalse(os.path.exists(os.path.join(fx.runs, "A-1105-7", "loop_decisions.tsv")))


class EndToEnd(unittest.TestCase):
    """ck-run ask open (E1) -> ck-run launch <slug> loop ... (E2) -> two real ck-run runs (stub CLIs) -> metric."""

    def setUp(self):
        self.fx = Fixture()
        missing = [c for c in ("loop", "launch", "ask") if not cli_registers(c)]
        if missing:
            self.fx.close()
            self.skipTest(f"ck-run does not have {', '.join(missing)} yet (E1/E2 integration)")
        self.sb = self.fx.sb

    def tearDown(self):
        self.fx.close()

    def test_launched_loop_stops_after_two_flat_rounds(self):
        sb = self.sb
        p = sb.ck("ask", "open", "--words", "loop test", "--size", "L", "--title", "e2e loop", "--pattern", "loop")
        self.assertEqual(p.returncode, 0, p.stderr)
        ask = p.stdout.split()[0]
        task = os.path.join(sb.runs, "e2e-loop_prompt.md")
        with open(task, "w") as f:
            f.write("Make one small change that should raise the metric.")
        p = sb.ck("launch", "e2e-loop", "loop", "--ask", ask, "--agent", "codex", "--prompt-file", task,
                  "--workspace", "devfab", "--metric", "echo 5", "--target", ">=10", "--max", "4", "--cwd", sb.tmp)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = os.path.join(sb.runs, "e2e-loop.out")
        end, text = time.time() + 180, ""
        while time.time() < end:
            try:
                with open(out) as f:
                    text = f.read()
            except OSError:
                text = ""
            if "ck-run exit=" in text:
                break
            time.sleep(0.5)
        self.assertIn("ck-run exit=1", text)  # 5, 5, 5: two rounds without improvement
        self.assertIn("2 rounds in a row without improvement", text)
        iters = [e for e in sb.ledger() if e["event"] == "loop_iter"]
        self.assertEqual([e["note"] for e in iters], ["i=1 metric=5 kept=no", "i=2 metric=5 kept=no"])
        runs = {e["run_id"]: e for e in sb.ledger() if e["event"] == "dispatch"}
        r1, r2 = (e["run_id"] for e in iters)
        self.assertEqual((runs[r1].get("ask_id"), runs[r2].get("ask_id")), (ask, ask))
        self.assertEqual(runs[r2]["parent_run_id"], r1)
        with open(os.path.join(sb.runs, ask, "loop_decisions.tsv")) as f:
            self.assertEqual(len(f.read().splitlines()), 4)  # header, baseline, 2 rounds


if __name__ == "__main__":
    unittest.main()


class TestMetricSafety(LoopCase):
    """chalach 2026-11-06: no leak of keys; never run agent-edited code outside the sandbox."""

    def test_metric_env_has_no_keys(self):
        os.environ["CK_TEST_SECRET_KEY"] = "s3cr3t"
        try:
            env = loop.metric_env(self.fx.cfg)
        finally:
            del os.environ["CK_TEST_SECRET_KEY"]
        self.assertNotIn("CK_TEST_SECRET_KEY", env)
        self.assertEqual(env["PATH"], os.environ.get("PATH"))

    def test_watches_scripts_not_data(self):
        script = os.path.join(self.work, "metric.sh")
        with open(script, "w") as f:
            f.write("echo 10\n")
        self.assertEqual(loop.metric_scripts("cat value.txt", self.work), {})
        self.assertEqual(list(loop.metric_scripts("bash metric.sh", self.work)), [os.path.realpath(script)])

    def test_stops_when_an_agent_edits_the_metric_script(self):
        script = os.path.join(self.work, "metric.sh")
        with open(script, "w") as f:
            f.write("echo 10\n")

        def run(argv):
            with open(script, "a") as f:
                f.write("echo agent was here\n")
            return 0, "2026-11-06T000000-devfab-general-codex-aaaa", ""

        code, out = self.go(run, metric="bash metric.sh")
        self.assertEqual(code, 4, out)
        self.assertIn("the metric script changed during this round", out)
        self.assertEqual(len(self.iters()), 1)
