"""Replay the SPCM day through the integrated CLI. Only Sandbox stubs and fake osascript.

Worker runs and reviews have separate budgets: the M ask gets a worker, one fix, and one review.
The third worker run must exit 7. No real agent, window, or live ledger is used.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
import unittest

from helpers import FAKE_CLI, REAL_CONFIG, Sandbox
from test_v4_core import V4, V4_PATCH
from test_watchdog import alive, kill_group, wait_for


class TestV4Integration(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.sb.set_section("asks", required=True)
        # Use the actual configured switch, with all paths and executables still sandboxed.
        self.sb.set_section("post_review", **json.loads(Path(REAL_CONFIG).read_text())["post_review"])
        self.sb.set_live(terminal="iTerm2")
        self.sb.window("run", window_id=777)
        self.pids = []
        source = Path(FAKE_CLI).read_text().replace('if __name__ == "__main__":',
                                                 V4_PATCH + 'if __name__ == "__main__":')
        for name in ("claude", "codex", "agent", "agy"):
            Path(self.sb.stubs, name).write_text(f"#!{sys.executable}\n" + source)
        self.sb.control(default={"v4": V4})

    def tearDown(self):
        for event in self.sb.ledger():
            if event.get("event") == "launch" and event.get("pid"):
                self.pids.append(event["pid"])
        for pid in self.pids:
            kill_group(pid)
        self.sb.cleanup()

    def ck(self, *argv, code=0):
        result = self.sb.ck(*argv)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def ask(self, size="M", pattern="solo", title="SPCM kit -> wiki"):
        return self.sb.open_ask(size, title, "note the SPCM kit as wiki for now",
                                "--project", "spcm", "--pattern", pattern)

    def prompt(self, slug):
        path = Path(self.sb.runs, slug + "_prompt.md")
        path.write_text("Do only this: note the SPCM kit in the wiki. Stub test task.\n")
        return str(path)

    def events(self, name, ask=None):
        return [e for e in self.sb.ledger() if e["event"] == name and (ask is None or e.get("ask_id") == ask)]

    def wait_out(self, slug, code=0):
        path = Path(self.sb.runs, slug + ".out")
        def finished():
            text = path.read_text() if path.exists() else ""
            return text if re.search(r"^ck-run exit=\d+\s*$", text, re.M) else ""
        text = wait_for(finished, 60)
        self.assertTrue(text, f"no exit for {slug}")
        self.assertIn(f"ck-run exit={code}", text)
        return text

    def worker(self, ask, *extra, code=0):
        return self.ck("run", "--ask", ask, "--model", "sol", "--workspace", "devfab", "--title", "SPCM note",
                       "--prompt-file", self.sb.task(), "--mode", "read", "--no-live", *extra, code=code)

    def test_spcm_day(self):
        small = self.ask("S", title="SPCM wiring answer")
        answer = self.ck("ask", "answered", small, "--summary", "The stub answer uses the counter input.")
        self.assertIn("0 runs", answer.stdout)
        self.assertEqual(self.ck("ask", "close", small).stdout, answer.stdout)
        self.assertEqual(self.sb.calls(), [])

        medium = self.ask()
        prompt = self.prompt("spcm-note")
        self.ck("launch", "spcm-note", "run", "--ask", medium, "--model", "sol", "--workspace", "devfab",
                "--title", "SPCM note", "--mode", "read", "--prompt-file", prompt, "--no-live")
        self.wait_out("spcm-note")
        first = self.events("dispatch", medium)[0]["run_id"]
        self.ck("review", first, "--model", "opus", "--no-live", code=2)
        self.ck("review", first, "--model", "opus", "--trigger", "hardware-or-raw", "--no-live")
        self.assertEqual(self.events("review", medium)[0]["ask_id"], medium)
        self.worker(medium, "--parent", first)
        self.worker(medium, code=7)
        words = "chalach: do one more stub run"
        self.worker(medium, "--over", words)
        self.assertEqual(self.events("over_budget", medium)[-1]["over"], words)
        self.ck("ask", "later", medium, "Check the datasheet on the lab PC")
        card = self.ck("ask", "close", medium).stdout
        self.assertIn("How sure:", card)
        self.assertIn("Later (", card)
        self.assertIn("Check the datasheet", card)
        self.assertNotIn(first, card)
        self.assertLessEqual(len(card.splitlines()), 8)
        self.worker(medium, "--over", words, code=2)  # over cannot reopen an ask

        race = self.ask("S", "race", "Three views of fibre coupling")
        started = self.ck("race", "--ask", race, "--prompt-file", self.prompt("spcm-race"),
                          "--title", "Fibre views", "--workspace", "devfab")
        for slug in started.stdout.split():
            self.wait_out(slug)
        runs = self.events("dispatch", race)
        self.assertEqual({(e["model_alias"], e["agent"]) for e in runs},
                         {("astra", "codex"), ("opus", "claude"), ("grok", "cursor")})
        self.assertEqual(len(runs), 3)
        self.ck("race", "show", "--ask", race)
        self.ck("ask", "close", race)

        blocked = self.ask("S")
        self.ck("andon", "raise", "--project", "spcm", "--reason", "Check the cable first")
        refused = self.worker(blocked, "--over", words, code=2)
        self.assertIn("ANDON", refused.stderr)
        self.assertEqual(self.events("dispatch", blocked), [])
        self.ck("andon", "clear", "--project", "spcm", "--note", "Stub cable checked")
        self.worker(blocked)
        self.ck("ask", "close", blocked)

        # A killed watcher leaves its independent fake window alive. Reattach must finish from disk.
        recovery = self.ask("S", title="Recover after watcher restart")
        self.sb.control(default={"v4": V4, "live": {"sleep": 5}})
        self.prompt("spcm-recover")
        launched = self.ck("launch", "spcm-recover", "run", "--ask", recovery, "--model", "sol",
                           "--workspace", "devfab", "--title", "Recovery", "--mode", "read")
        supervisor = int(dict(line.split(": ", 1) for line in launched.stdout.splitlines())["pid"])
        def agent_started():
            records = self.events("dispatch", recovery)
            if not records:
                return None
            rid = records[0]["run_id"]
            pidfile = Path(self.sb.runs, rid, "live.pid")
            return (rid, int(pidfile.read_text())) if pidfile.exists() and pidfile.read_text().strip() else None
        pair = wait_for(agent_started, 30)
        self.assertTrue(pair)
        rid, agent_pid = pair
        self.pids.append(agent_pid)
        os.killpg(supervisor, signal.SIGKILL)
        self.assertTrue(alive(agent_pid))
        time.sleep(0.2)
        self.ck("reattach", rid)
        result = wait_for(lambda: self.events("result", recovery), 40)
        self.assertTrue(result)
        self.assertEqual(result[0]["status"], "ok", result[0].get("note"))
        self.assertIn("finalized by ck-run reattach", result[0]["note"])
        self.ck("reattach", rid)
        self.assertEqual(len(self.events("result", recovery)), 1)

        # Short watchdog budget in the fake config only. The sleep text gets exactly one resume key.
        self.sb.set_section("asks", budgets={"S": {"stall_s": 1}})
        self.sb.set_live(watchdog=True)
        self.sb.window("run", window_id=777, tail="Agent went to sleep")
        self.sb.control(default={"live": {"sleep": 60}})
        stalled = self.ask("S", title="Sleeping stub")
        self.ck("run", "--ask", stalled, "--model", "sol", "--workspace", "devfab", "--title", "Sleep",
                "--prompt-file", self.sb.task(), "--mode", "read", "--live", code=3)
        self.assertEqual(len(self.events("resume_sent", stalled)), 1)
        self.assertEqual(len(self.events("watchdog_stop", stalled)), 1)
        self.assertEqual(self.events("result", stalled)[0]["status"], "timeout")

        self.sb.set_live(watchdog=False)
        self.sb.control(default={"v4": V4})
        auto = self.ask("S", title="Small wiki edit")
        done = self.ck("run", "--ask", auto, "--model", "composer", "--workspace", "devfab", "--title", "Edit",
                       "--prompt-file", self.sb.task(), "--mode", "auto", "--write-scope", "Projects/Notes", "--no-live")
        record = self.sb.result(self.sb.run_id_from(done))
        self.assertIsNone(record["post_review"])
        self.assertTrue(record["post_review_off"])
        self.assertFalse([c for c in self.sb.calls() if c["agent"] == "claude"])

    def test_concurrent_ask_ids_and_last_budget_slot(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: self.ask("S"), range(12)))
        self.assertEqual(len(set(ids)), 12)
        self.assertEqual(sorted(int(a.rsplit("-", 1)[1]) for a in ids), list(range(1, 13)))
        task = self.sb.task()
        argv = ("run", "--ask", ids[0], "--model", "sol", "--workspace", "devfab", "--title", "Concurrent",
                "--prompt-file", task, "--mode", "read", "--no-live")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.sb.ck(*argv), range(4)))
        self.assertEqual(sorted(p.returncode for p in results), [0, 7, 7, 7])
        self.assertEqual(len(self.events("dispatch", ids[0])), 1)

    def test_launch_treats_shell_text_as_literal_arguments(self):
        ask = self.ask("S")
        marker = Path(self.sb.tmp, "must-not-exist")
        title = f"literal $(touch {marker}) `touch {marker}`; quoted ' text"
        prompt = self.prompt("quotes")
        self.ck("launch", "quotes", "run", "--ask", ask, "--model", "sol", "--workspace", "devfab",
                "--title", title, "--prompt-file", prompt, "--mode", "read", "--no-live")
        self.wait_out("quotes")
        self.assertFalse(marker.exists())
        self.assertEqual(self.events("dispatch", ask)[0]["title"], title[:200])
        self.ck("launch", "quotes", "run", "--ask", ask, "--model", "sol", "--workspace", "devfab",
                "--title", "reuse", "--prompt-file", prompt, code=2)


if __name__ == "__main__":
    unittest.main()
