"""v4 survive: ck-run launch (launch.py, BUILD_SPEC_v4 10.4).

The launched ck-run is a stub script (launch.CK_RUN patched), or, in the end-to-end tests, the real bin/ck-run with
the sandbox config (stub agent CLIs, fake osascript). Everything is written inside the test sandbox; no real agent
runs and no real window opens.
"""
import argparse
import contextlib
import io
import os
import shlex
import subprocess
import sys
import time
import unittest
from datetime import datetime
from unittest import mock

from helpers import REPO
from test_watchdog import V4_KEYS, alive, kill_group, v4_sandbox, wait_for

from ckdispatch import cli, launch, ledger
from ckdispatch.config import Config
from ckdispatch.errors import Refused

RUN = ["run", "--agent", "codex", "--workspace", "devfab", "--title", "Count files", "--mode", "read"]


def cli_has(cmd: str) -> bool:
    """Does cli._parser() register this command yet? (E1 wires the extension modules, contract 10.1.)"""
    try:
        ap = cli._parser()
    except Exception:
        return False
    for act in ap._actions:
        if isinstance(act, argparse._SubParsersAction):
            return cmd in act.choices
    return False


class LaunchBase(unittest.TestCase):
    def setUp(self):
        self.sb = v4_sandbox()
        # the launched ck-run inherits this environment: the sandbox config, never the real one
        self.env = mock.patch.dict(os.environ, self.sb.env())
        self.env.start()
        self.keys = mock.patch.object(ledger, "KEYS", ledger.KEYS + [k for k in V4_KEYS if k not in ledger.KEYS])
        self.keys.start()
        os.makedirs(self.sb.runs, exist_ok=True)
        self.cfg = Config()
        self.pids = []

    def tearDown(self):
        for pid in self.pids:
            kill_group(pid)
        self.keys.stop()
        self.env.stop()
        self.sb.cleanup()

    def stub(self, sleep=0.0, code=0):
        """A stand-in for bin/ck-run: records its argv (one per line) and cwd, prints two lines, sleeps, exits."""
        p = os.path.join(self.sb.stubs, f"ck-run-stub-{len(os.listdir(self.sb.stubs))}")
        argv_file, cwd_file = p + ".argv", p + ".cwd"
        with open(p, "w") as f:
            f.write("#!/bin/bash\n"
                    f'printf "%s\\n" "$@" > {shlex.quote(argv_file)}\n'
                    f"pwd > {shlex.quote(cwd_file)}\n"
                    'echo "stub ck-run started"\n'
                    f"sleep {sleep}\n"
                    'echo "/fake/run/summary.md"\n'
                    f"exit {code}\n")
        os.chmod(p, 0o755)
        return p, argv_file, cwd_file

    def prompt(self, slug, text="Do only this: count the files in Design Library/pages.\n"):
        p = os.path.join(self.sb.runs, f"{slug}_prompt.md")
        with open(p, "w") as f:
            f.write(text)
        return p

    def start(self, slug, argv, stub, **kw):
        with mock.patch.object(launch, "CK_RUN", stub):
            info = launch.start(self.cfg, slug, argv, **kw)
        self.pids.append(info["pid"])
        return info

    def launches(self, slug=None):
        return [e for e in self.sb.ledger() if e["event"] == "launch" and (slug is None or e.get("slug") == slug)]


class TestLaunchFile(LaunchBase):
    def test_sh_has_the_header_and_exactly_one_ck_run_line(self):
        stub, argv_file, cwd_file = self.stub()
        pf = self.prompt("count-1")
        info = self.start("count-1", RUN + ["--prompt-file", pf], stub, cwd=self.sb.devfab)
        self.assertEqual(set(info), {"slug", "sh", "out", "pid"})
        self.assertEqual((info["slug"], info["sh"], info["out"]),
                         ("count-1", os.path.join(self.sb.runs, "count-1.sh"), os.path.join(self.sb.runs, "count-1.out")))
        lines = open(info["sh"]).read().splitlines()
        self.assertEqual(lines[0], "#!/bin/bash")
        body = [ln for ln in lines[1:] if ln.strip() and not ln.startswith("#")]
        self.assertEqual(len(body), 1, lines)
        want = RUN + ["--prompt-file", pf, "--interactive"]
        self.assertEqual(body[0], f"cd {shlex.quote(self.sb.devfab)} && {shlex.quote(stub)} "
                                  f"{' '.join(shlex.quote(a) for a in want)} \"$@\"")
        self.assertIn("# what: Count files", lines)
        self.assertIn("# ask: none", lines)
        stamp = next(ln for ln in lines if ln.startswith("# launched: ")).split(": ", 1)[1]
        self.assertIsNotNone(datetime.fromisoformat(stamp).tzinfo)  # ledger.now_iso(), never typed by hand
        self.assertTrue(wait_for(lambda: launch.out_finished(info["out"]), 30))
        out = open(info["out"]).read().splitlines()
        self.assertEqual(out, ["stub ck-run started", "/fake/run/summary.md", "ck-run exit=0"])
        self.assertEqual(open(argv_file).read().splitlines(), want)  # "$@" adds nothing when launch starts it
        self.assertEqual(open(cwd_file).read().strip(), self.sb.devfab)
        self.assertTrue(os.access(info["sh"], os.X_OK))

    def test_prompt_file_comes_from_the_slug_and_ends_up_absolute(self):
        stub = self.stub()[0]
        pf = self.prompt("count-2")
        p = launch.plan(self.cfg, "count-2", RUN)
        self.assertEqual(p["argv"][-3:], ["--prompt-file", pf, "--interactive"])
        p = launch.plan(self.cfg, "count-3", RUN + ["--prompt-file=count-2_prompt.md"], cwd=self.sb.runs)
        self.assertIn(pf, p["argv"])  # relative to the launch folder -> absolute
        self.assertNotIn("--prompt-file=count-2_prompt.md", p["argv"])
        info = self.start("count-2", RUN, stub)
        self.assertIn(f"--prompt-file {shlex.quote(pf)}", open(info["sh"]).read())

    def test_live_flags_and_commands(self):
        self.prompt("f")
        argv = lambda extra, cmd=RUN: launch.plan(self.cfg, "f", cmd + extra)["argv"]  # noqa: E731
        self.assertNotIn("--interactive", argv(["--no-live"]))
        self.assertNotIn("--interactive", argv(["--no-interactive"]))
        self.assertEqual(argv(["--live"]).count("--live") + argv(["--live"]).count("--interactive"), 1)
        self.assertEqual(argv(["--interactive"]).count("--interactive"), 1)
        rid = "2026-11-05T120000-devfab-general-codex-ab12"
        rv = launch.plan(self.cfg, "rv", ["review", rid, "--by", "claude", "--trigger", "you-asked"])
        self.assertEqual(rv["argv"], ["review", rid, "--by", "claude", "--trigger", "you-asked", "--interactive"])
        self.assertEqual(rv["title"], f"review of {rid} by claude")
        task = self.sb.task()  # a loop may read its prompt from elsewhere; it is made absolute, no --interactive
        lp = launch.plan(self.cfg, "lp", ["loop", "--ask", "A-1105-1", "--agent", "codex", "--prompt-file", task,
                                          "--workspace", "devfab", "--metric", "echo 1", "--target", ">=1", "--max", "2"])
        self.assertNotIn("--interactive", lp["argv"])
        self.assertIn(task, lp["argv"])
        ra = launch.plan(self.cfg, "ra", ["reattach", rid, "--wait"])
        self.assertEqual(ra["argv"], ["reattach", rid, "--wait"])

    def test_ask_words_go_in_the_header_and_the_ledger_gets_launch(self):
        ledger.append(self.sb.runs, event="ask_open", ask_id="A-1105-1", words="count the files\nand say how many",
                      title="Count", size="S")
        stub = self.stub()[0]
        self.prompt("ask-1")
        info = self.start("ask-1", RUN, stub, ask_id="A-1105-1")
        text = open(info["sh"]).read()
        self.assertIn("# ask: A-1105-1", text)
        self.assertIn("# chalach (verbatim): count the files\n#   and say how many\n", text)
        self.assertIn(" --ask A-1105-1 ", text)  # launch adds --ask for the ask it was given
        ev = self.launches("ask-1")
        self.assertEqual(len(ev), 1)
        ev = ev[0]
        self.assertEqual((ev["pid"], ev["ask_id"], ev["status"], ev["run_id"], ev["to"], ev["title"]),
                         (info["pid"], "A-1105-1", "dispatched", None, "codex", "Count files"))
        self.assertEqual(ev["note"], f"sh={info['sh']} out={info['out']}")


class TestDetach(LaunchBase):
    def test_returns_before_the_child_ends(self):
        stub = self.stub(sleep=4)[0]
        self.prompt("slow-1")
        t0 = time.monotonic()
        info = self.start("slow-1", RUN, stub)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(alive(info["pid"]))
        self.assertFalse(launch.out_finished(info["out"]))
        # its own session: closing the terminal or killing the Claude session that launched it does not reach it
        self.assertEqual(os.getsid(info["pid"]), info["pid"])
        self.assertNotEqual(os.getsid(info["pid"]), os.getsid(0))
        self.assertTrue(wait_for(lambda: launch.out_finished(info["out"]), 30))
        self.assertTrue(open(info["out"]).read().rstrip().endswith("ck-run exit=0"))

    def test_no_pipes_shared_with_the_launcher(self):
        stub = self.stub(sleep=5)[0]
        self.prompt("pipe-1")
        code = (f"import sys; sys.path.insert(0, {REPO!r}); from ckdispatch import launch; "
                f"from ckdispatch.config import Config; launch.CK_RUN = {stub!r}; "
                f"print(launch.start(Config(), 'pipe-1', {RUN!r})['pid'])")
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60,
                           env=self.sb.env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.monotonic() - t0, 4.0)  # capture_output would wait 5 s if the run held the pipes
        pid = int(r.stdout.split()[-1])
        self.pids.append(pid)
        self.assertTrue(alive(pid))  # the launcher is gone, the run goes on
        out = os.path.join(self.sb.runs, "pipe-1.out")
        self.assertTrue(wait_for(lambda: launch.out_finished(out), 30))

    def test_exit_code_is_recorded(self):
        stub = self.stub(code=3)[0]
        self.prompt("fail-1")
        info = self.start("fail-1", RUN, stub)
        self.assertTrue(wait_for(lambda: launch.out_finished(info["out"]), 30))
        self.assertEqual(open(info["out"]).read().splitlines()[-1], "ck-run exit=3")


class TestRefusals(LaunchBase):
    def test_used_or_running_slugs(self):
        stub = self.stub(sleep=2)[0]
        self.prompt("dup-1")
        info = self.start("dup-1", RUN, stub)
        with self.assertRaisesRegex(Refused, r"slug used; pick a new one .*no 'ck-run exit=' line yet"):
            launch.start(self.cfg, "dup-1", RUN)  # still running
        self.assertTrue(wait_for(lambda: launch.out_finished(info["out"]), 30))
        with self.assertRaisesRegex(Refused, "slug used; pick a new one"):
            launch.start(self.cfg, "dup-1", RUN)  # used before
        os.unlink(info["sh"])
        os.unlink(info["out"])
        with self.assertRaisesRegex(Refused, "the ledger has a launch with this slug"):
            launch.start(self.cfg, "dup-1", RUN)
        self.assertEqual(len(self.launches("dup-1")), 1)
        with open(os.path.join(self.sb.runs, "hand.sh"), "w") as f:  # a hand-made v3 launch file, never started
            f.write("#!/bin/bash\n")
        self.prompt("hand")
        with self.assertRaisesRegex(Refused, "slug used"):
            launch.start(self.cfg, "hand", RUN)

    def test_bad_input(self):
        self.prompt("ok-slug")
        bad = {
            "bad slug": [("", RUN), ("-x", RUN), ("../x", RUN), ("a b", RUN), ("x" * 65, RUN)],
            "no prompt file": [("no-prompt", RUN)],
            "must be a file in": [("outside", RUN + ["--prompt-file", self.sb.task()])],
            "not an existing file": [("missing", RUN + ["--prompt-file", os.path.join(self.sb.runs, "nope.md")])],
            "forbidden flag": [("ok-slug", RUN + ["--dangerously-skip-permissions"])],
            "starts run, review or loop": [("ok-slug", ["accept", "2026-11-05T120000-devfab-general-codex-ab12"]),
                                           ("ok-slug", [])],
        }
        for why, cases in bad.items():
            for slug, argv in cases:
                with self.subTest(why=why, slug=slug):
                    with self.assertRaisesRegex(Refused, why):
                        launch.start(self.cfg, slug, argv)
        with self.assertRaisesRegex(Refused, "--ask A-1105-2 but the launch is for ask A-1105-1"):
            launch.start(self.cfg, "ok-slug", RUN + ["--ask", "A-1105-2"], ask_id="A-1105-1")
        with self.assertRaisesRegex(Refused, "cwd is not a folder"):
            launch.start(self.cfg, "ok-slug", RUN, cwd=os.path.join(self.sb.tmp, "no-such-folder"))
        self.assertEqual([n for n in os.listdir(self.sb.runs) if n.endswith((".sh", ".out"))], [])
        self.assertEqual(self.launches(), [])


class TestLaunchCli(LaunchBase):
    def parse(self, *argv):
        ap = argparse.ArgumentParser(prog="ck-run")
        sub = ap.add_subparsers(dest="cmd", required=True)
        launch.register(sub)
        return ap.parse_args(list(argv))

    def run_cli(self, a, stub=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as st:
            if stub:
                st.enter_context(mock.patch.object(launch, "CK_RUN", stub))
            st.enter_context(contextlib.redirect_stdout(out))
            st.enter_context(contextlib.redirect_stderr(err))
            code = a.func(self.cfg, a)
        return code, out.getvalue(), err.getvalue()

    def test_prints_four_lines(self):
        stub = self.stub()[0]
        self.prompt("cli-1")
        code, out, _ = self.run_cli(self.parse("launch", "cli-1", *RUN), stub)
        self.assertEqual(code, 0)
        rows = out.splitlines()
        self.assertEqual([r.split(": ", 1)[0] for r in rows], ["slug", "sh", "out", "pid"])
        self.assertEqual(rows[0], "slug: cli-1")
        self.pids.append(int(rows[3].split(": ")[1]))
        self.assertTrue(wait_for(lambda: launch.out_finished(rows[2].split(": ", 1)[1]), 30))

    def test_dry_run_and_help_start_nothing(self):
        self.prompt("dry-1")
        code, out, err = self.run_cli(self.parse("launch", "dry-1", *RUN, "--dry-run"))  # the real bin/ck-run
        self.assertEqual(code, 0, err)
        self.assertIn("launch (dry run): nothing written, nothing started", out)
        line = next(ln for ln in out.splitlines() if ln.startswith("cd "))  # the .sh line it would write
        self.assertIn(" --dry-run ", line)
        self.assertTrue(line.endswith(" --interactive \"$@\""), line)
        self.assertIn("live.sh", out)  # the ck-run dry run itself: the live window command
        code, out, err = self.run_cli(self.parse("launch", "dry-1", "run", "--help"))
        self.assertEqual(code, 0, err)
        self.assertIn("--prompt-file", out)
        self.assertEqual([n for n in os.listdir(self.sb.runs) if n.endswith((".sh", ".out"))], [])
        self.assertEqual(self.launches(), [])
        self.assertEqual(self.sb.calls(), [])


@unittest.skipUnless(cli_has("launch"), "cli._parser() does not register `launch` yet (E1 wires extension modules)")
class TestLaunchEndToEnd(LaunchBase):
    def test_a_real_run_goes_on_after_the_launcher_returns(self):
        self.sb.control(default={"sleep": 3})
        pf = self.prompt("e2e-1", "Do only this: UNIQUE-LAUNCH-TASK-71\n")
        t0 = time.monotonic()
        p = self.sb.ck("launch", "e2e-1", *RUN[:5], "--title", "E2E launch", "--mode", "read")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertLess(time.monotonic() - t0, 3.0)  # sb.ck captures the output: a held pipe would wait 3 s
        rows = dict(r.split(": ", 1) for r in p.stdout.splitlines())
        self.pids.append(int(rows["pid"]))
        self.assertTrue(wait_for(lambda: launch.out_finished(rows["out"]), 90))
        self.assertIn("ck-run exit=0", open(rows["out"]).read())
        disp = [e for e in self.sb.ledger() if e["event"] == "dispatch" and e.get("title") == "E2E launch"]
        self.assertEqual(len(disp), 1)
        res = self.sb.result(disp[0]["run_id"])
        self.assertEqual(res["status"], "ok")
        self.assertIn("UNIQUE-LAUNCH-TASK-71", self.sb.calls()[-1]["prompt"])
        self.assertEqual(disp[0].get("prompt_path") and os.path.dirname(disp[0]["prompt_path"]),
                         os.path.join(self.sb.runs, disp[0]["run_id"]))
        self.assertEqual(len(self.launches("e2e-1")), 1)
        self.assertIn(f"--prompt-file {shlex.quote(pf)}", open(rows["sh"]).read())

    def test_refusal_exit_2(self):
        p = self.sb.ck("launch", "bad slug!", *RUN)
        self.assertEqual(p.returncode, 2)
        self.assertIn("REFUSED: bad slug", p.stderr)


if __name__ == "__main__":
    unittest.main()
