"""v4 survive: stall watchdog, one window per ask (tabs), keep-awake, tab closer (live.py + watchdog.py, spec 10.5).

osascript is the fake (tests/fake_osascript.py), caffeinate is a stub script, the "agent" is a sleep process.
No window opens, nothing is typed into a real terminal, no notification is shown, the Mac's sleep is never touched.
"""
import contextlib
import inspect
import io
import json
import os
import pty
import secrets
import shlex
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from helpers import REPO, Sandbox

from ckdispatch import ledger, live, route, watchdog

V4_KEYS = ["ask_id", "size", "pattern", "tier", "lane", "trigger", "over", "slug", "pid", "reason", "words", "project",
           "budget", "card", "model_alias"]


def v4_sandbox(**live_kw):
    """A sandbox whose config needs no ask and never keeps the Mac awake or closes tabs (unless a test says so)."""
    sb = Sandbox()
    with open(sb.config) as f:
        c = json.load(f)
    c.setdefault("asks", {})["required"] = False
    c["live"].update({"keep_awake": False, "close_tab_after_s": 0, **live_kw})
    with open(sb.config, "w") as f:
        json.dump(c, f, indent=1)
    return sb


def alive(pid) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def orphan_sleep(seconds=60) -> int:
    """A stand-in agent: a sleep that leads its own process group and is NOT this process's child (like live.sh,
    which iTerm2 starts), so once stopped it is gone for good (no zombie that still looks alive)."""
    code = ("import subprocess, sys; p = subprocess.Popen(['/bin/sleep', sys.argv[1]], start_new_session=True, "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(p.pid)")
    r = subprocess.run([sys.executable, "-I", "-c", code, str(seconds)], capture_output=True, text=True, timeout=30)
    return int(r.stdout.strip())


def kill_group(pid) -> None:
    try:
        os.killpg(pid, 9)
    except OSError:
        pass


def wait_for(pred, timeout=20.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


class WatchBase(unittest.TestCase):
    def setUp(self):
        self.sb = v4_sandbox()
        self.env = mock.patch.dict(os.environ, {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config})
        self.env.start()
        self.keys = mock.patch.object(ledger, "KEYS", ledger.KEYS + [k for k in V4_KEYS if k not in ledger.KEYS])
        self.keys.start()
        self.osa = os.path.join(self.sb.stubs, "osascript")
        self.pids, self.threads = [], []

    def tearDown(self):
        for t in self.threads:
            t.join(timeout=10)
        for pid in self.pids:
            kill_group(pid)
        self.keys.stop()
        self.env.stop()
        self.sb.cleanup()

    def later(self, fn, *a):
        t = threading.Thread(target=fn, args=a, daemon=True)
        t.start()
        self.threads.append(t)

    def agent(self, seconds=60) -> int:
        pid = orphan_sleep(seconds)
        self.pids.append(pid)
        return pid

    def make_run(self, agent="codex", tty=None, alive_agent=True):
        rid = f"{time.strftime('%Y-%m-%dT%H%M%S')}-devfab-general-{agent}-{secrets.token_hex(2)}"
        rd = os.path.join(self.sb.runs, rid)
        os.makedirs(rd)
        with open(os.path.join(rd, "packet.json"), "w") as f:
            json.dump({"run_id": rid, "worker": agent, "title": "watch test", "workspace": "devfab"}, f)
        open(os.path.join(rd, "live.claim"), "w").close()
        if alive_agent:
            with open(os.path.join(rd, "live.pid"), "w") as f:
                f.write(str(self.agent()))
        if tty:
            with open(os.path.join(rd, "live.tty"), "w") as f:
                f.write(tty + "\n")
        return rd

    def watch(self, rd, **kw):
        w = {"stall_s": 1.0, "osascript": self.osa, "window_id": None, "terminal": "Terminal", "env": self.sb.env(),
             "runs_dir": self.sb.runs, "run_id": os.path.basename(rd), "ask_id": "A-1105-1", "agent": "codex",
             "title": "watch test", "keep_awake": False, "close_tab_after_s": 0}
        w.update(kw)
        return w

    def iterm(self, rd, **kw):
        return self.watch(rd, terminal="iTerm2", window_id="4242", **kw)

    def events(self, rd, name=None):
        rid = os.path.basename(rd)
        return [e for e in self.sb.ledger() if e.get("run_id") == rid and (name is None or e["event"] == name)]

    def calls(self, kind=None):
        return [c for c in self.sb.osascript_calls() if kind is None or c.get("kind") == kind]

    def finish(self, rd, after=0.0):
        """The agent writes result.md and summary.md (after `after` seconds)."""
        time.sleep(after)
        with open(os.path.join(rd, "result.md"), "w") as f:
            f.write("# result\n")
        with open(os.path.join(rd, "summary.md"), "w") as f:
            f.write("done\n")

    def wait(self, rd, w, timeout=60):
        t0 = time.monotonic()
        out = live.wait_done(rd, time.time() + timeout, 0.1, watch=w)
        return out, time.monotonic() - t0


class TestClassify(unittest.TestCase):
    def test_each_reason(self):
        cases = {
            "sleep": "⏺ Working\n  ⎿  API Error: Your computer went to sleep mid-response\n> ",
            "approval": "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. No",
            "rate_limit": "⎿  You've hit your usage limit · resets 7pm (America/Chicago)",
            "unknown": "⏺ Reading files\n  ⎿  Read 120 lines",
        }
        for reason, text in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(watchdog.classify(text)[0], reason)
        self.assertEqual(watchdog.classify(None), ("unknown", ""))
        self.assertEqual(watchdog.classify("Allow command? [y/n]")[0], "approval")
        self.assertEqual(watchdog.classify("Error: 429 Too Many Requests")[0], "rate_limit")

    def test_most_recent_line_wins(self):
        self.assertEqual(watchdog.classify("Do you want to proceed?\nAPI Error: Your computer went to sleep")[0],
                         "sleep")
        self.assertEqual(watchdog.classify("Your computer went to sleep\n> continue\nDo you want to proceed?")[0],
                         "approval")
        long = "rate limit hit\n" + "\n".join(f"line {i}" for i in range(40))  # far above the last lines: ignored
        self.assertEqual(watchdog.classify(long)[0], "unknown")

    def test_signature_ignores_timers_and_spinners_not_tokens_or_new_lines(self):
        s = live.text_signature
        self.assertEqual(s("✻ Thinking… (12s · esc to interrupt)"), s("✽ Thinking… (13s · esc to interrupt)"))
        self.assertEqual(s("Working (1m 05s • esc to interrupt)"), s("Working (1m 06s • esc to interrupt)"))
        self.assertEqual(s("⠋ waiting 12:01:07"), s("⠙ waiting 12:01:09"))
        self.assertNotEqual(s("✻ Thinking… (12s · ↓ 1.2k tokens)"), s("✻ Thinking… (13s · ↓ 1.4k tokens)"))
        self.assertNotEqual(s("line a"), s("line a\nline b"))
        self.assertIsNone(s(None))


class TestOldBehaviour(WatchBase):
    def test_signatures_keep_the_old_calls(self):
        p = list(inspect.signature(live.wait_done).parameters.values())
        self.assertEqual([x.name for x in p], ["run_dir", "deadline", "poll_s", "result_grace_s", "watch"])
        self.assertEqual((p[3].default, p[4].default), (10.0, None))
        p = list(inspect.signature(live.open_window).parameters.values())
        self.assertEqual([x.name for x in p], ["osascript", "command", "title", "env", "terminal", "profile",
                                               "group", "state_dir"])
        self.assertEqual([x.default for x in p[4:]], ["Terminal", "", None, None])

    def test_no_watch_no_watchdog(self):
        rd = self.make_run()
        self.later(self.finish, rd, 0.3)
        out = live.wait_done(rd, time.time() + 30, 0.1)  # the old positional call
        self.assertEqual(out, {"state": "done", "exit_code": None})
        rd2 = self.make_run()
        t0 = time.monotonic()
        out = live.wait_done(rd2, time.time() + 1.5, 0.1)  # quiet for longer than any stall_s used here: no watchdog
        self.assertEqual(out["state"], "timeout")
        self.assertGreaterEqual(time.monotonic() - t0, 1.4)
        self.assertEqual(self.calls(), [])
        self.assertEqual([e for e in self.sb.ledger() if e["event"] in ("stall", "watchdog_stop")], [])

    def test_quick_run_makes_no_osascript_calls(self):
        # stall_s 300: the window is read at most every 30 s, so a run that ends in a second never calls osascript
        # (older tests count osascript calls)
        rd = self.make_run(tty="/dev/ttys900")
        self.later(self.finish, rd, 1.0)
        out, _ = self.wait(rd, self.iterm(rd, stall_s=300))
        self.assertEqual(out["state"], "done")
        self.assertEqual(self.calls(), [])
        self.assertEqual(out["watchdog"]["reasons"], [])


class TestStall(WatchBase):
    def test_unknown_stall_notifies_once_and_stops_at_twice_stall_s(self):
        rd = self.make_run()
        agent_pid = int(open(os.path.join(rd, "live.pid")).read())
        out, took = self.wait(rd, self.watch(rd))
        self.assertEqual(out["state"], "stalled")
        self.assertEqual(out["reason"], "unknown")
        self.assertGreaterEqual(took, 2.0)  # 2 x stall_s with no activity
        self.assertLess(took, 12)
        self.assertTrue(wait_for(lambda: not alive(agent_pid), 10), "the stalled agent still runs")
        stalls = self.events(rd, "stall")
        self.assertEqual([(e["reason"], e["status"], e["ask_id"]) for e in stalls], [("unknown", "stalled", "A-1105-1")])
        self.assertIn("no window text", stalls[0]["note"])
        stops = self.events(rd, "watchdog_stop")
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["reason"], "unknown")
        self.assertIn("no activity for", stops[0]["note"])
        notes = self.calls("notify")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["args"][1], "ck-run · A-1105-1")
        self.assertIn("no activity", notes[0]["args"][0])
        self.assertEqual(self.calls("tail"), [])  # a Terminal window: its text is never read

    def test_file_activity_is_not_a_stall(self):
        rd = self.make_run()

        def work():
            for i in range(14):
                with open(os.path.join(rd, "progress.log"), "a") as f:
                    f.write(f"step {i}\n")
                time.sleep(0.2)
            self.finish(rd)
        self.later(work)
        out, took = self.wait(rd, self.watch(rd))
        self.assertEqual(out["state"], "done")
        self.assertGreater(took, 2.0)  # longer than 2 x stall_s, never stopped
        self.assertEqual(self.events(rd, "stall") + self.events(rd, "watchdog_stop"), [])

    def test_session_store_growth_is_activity(self):
        rd = self.make_run("codex")
        rid = os.path.basename(rd)
        day = os.path.join(self.sb.home, ".codex", "sessions", time.strftime("%Y/%m/%d"))
        os.makedirs(day)
        sess = os.path.join(day, f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S')}-0199aaaa-bbbb-4ccc-8ddd-eeeeffff0000.jsonl")
        with open(sess, "w") as f:
            f.write(json.dumps({"payload": {"role": "user", "content": [{"text": f"RUN: {rid} | agent: codex"}]}}) + "\n")

        def work():
            for i in range(14):
                with open(sess, "a") as f:
                    f.write(json.dumps({"payload": {"role": "assistant", "content": [{"text": f"step {i}"}]}}) + "\n")
                time.sleep(0.2)
            self.finish(rd)
        self.later(work)
        out, took = self.wait(rd, self.watch(rd))
        self.assertEqual(out["state"], "done")
        self.assertGreater(took, 2.0)
        self.assertEqual(self.events(rd, "stall"), [])

    def test_window_text_change_is_activity(self):
        self.sb.window("run", tails=[f"⏺ step {i}\n  ⎿  output line {i}" for i in range(80)])
        rd = self.make_run(tty="/dev/ttys900")
        self.later(self.finish, rd, 4.5)
        out, took = self.wait(rd, self.iterm(rd, stall_s=2.0))
        self.assertEqual(out["state"], "done")
        self.assertGreater(took, 4.0)  # longer than 2 x stall_s with quiet files: the window text kept it alive
        self.assertEqual(self.events(rd, "stall"), [])
        tails = self.calls("tail")
        self.assertGreater(len(tails), 3)
        self.assertEqual(tails[0]["args"], ["", "/dev/ttys900"])  # the run's own tab, by its tty

    def test_ticking_timer_alone_is_not_activity(self):
        self.sb.window("run", tails=[f"✻ Thinking… ({i}s · esc to interrupt)" for i in range(400)])
        rd = self.make_run(tty="/dev/ttys900")
        out, _ = self.wait(rd, self.iterm(rd))
        self.assertEqual(out["state"], "stalled")
        self.assertEqual([e["reason"] for e in self.events(rd, "stall")], ["unknown"])
        self.assertIn("Thinking", self.events(rd, "stall")[0]["note"])

    def test_sleep_sends_the_resume_key_once_then_stops(self):
        self.sb.window("run", tail="⏺ Writing the report\n  ⎿  API Error: Your computer went to sleep mid-response\n> ")
        rd = self.make_run(tty="/dev/ttys900")
        out, _ = self.wait(rd, self.iterm(rd))
        self.assertEqual(out["state"], "stalled")
        writes = self.calls("write")
        self.assertEqual(len(writes), 1)  # at most once per run
        self.assertEqual(writes[0]["args"], ["", "/dev/ttys900", "continue"])
        self.assertIn("ASCII character 13", "\n".join(writes[0]["script"]))  # then Return as its own keystroke
        sent = self.events(rd, "resume_sent")
        self.assertEqual(len(sent), 1)
        self.assertIn("went to sleep", sent[0]["note"])
        self.assertEqual([e["reason"] for e in self.events(rd, "stall")], ["sleep"])
        self.assertIn("did not wake", self.events(rd, "stall")[0]["note"])
        self.assertEqual(len(self.calls("notify")), 1)
        self.assertEqual(len(self.events(rd, "watchdog_stop")), 1)
        self.assertTrue(out["watchdog"]["resume_sent"])

    def test_resume_key_wakes_the_agent(self):
        self.sb.window("run", tail="API Error: Your computer went to sleep mid-response\n> ",
                       after_write_tail="> continue\n⏺ Picking up where I left off")
        rd = self.make_run(tty="/dev/ttys900")

        def agent_wakes():
            if not wait_for(lambda: self.calls("write"), 20):
                return
            for i in range(10):
                with open(os.path.join(rd, "progress.log"), "a") as f:
                    f.write(f"after wake {i}\n")
                time.sleep(0.2)
            self.finish(rd)
        self.later(agent_wakes)
        out, _ = self.wait(rd, self.iterm(rd))
        self.assertEqual(out["state"], "done")
        self.assertEqual(len(self.calls("write")), 1)
        self.assertEqual(len(self.events(rd, "resume_sent")), 1)
        self.assertEqual(self.events(rd, "stall") + self.events(rd, "watchdog_stop"), [])
        self.assertEqual(self.calls("notify"), [])

    def test_no_exact_tab_means_no_keystrokes(self):
        # iTerm2 window known, but neither the session id nor live.tty: never type into "the current session"
        self.sb.window("run", tail="API Error: Your computer went to sleep mid-response")
        rd = self.make_run()
        out, _ = self.wait(rd, self.iterm(rd))
        self.assertEqual(out["state"], "stalled")
        self.assertEqual(self.calls("write"), [])
        self.assertEqual(self.events(rd, "resume_sent"), [])
        stall = self.events(rd, "stall")
        self.assertEqual([e["reason"] for e in stall], ["sleep"])
        self.assertIn("could not type the resume key", stall[0]["note"])
        self.assertEqual(len(self.calls("notify")), 1)
        self.assertEqual(self.calls("tail")[0]["args"], ["4242"])  # reading the window is fine, typing is not

    def test_approval_notifies_once(self):
        self.sb.window("run", tail="Bash command\n  pip install foo\nDo you want to proceed?\n❯ 1. Yes\n  2. No")
        rd = self.make_run(tty="/dev/ttys900")
        out, _ = self.wait(rd, self.iterm(rd))
        self.assertEqual(out["state"], "stalled")
        self.assertEqual(out["reason"], "approval")
        self.assertEqual([e["reason"] for e in self.events(rd, "stall")], ["approval"])
        self.assertIn("Do you want to proceed?", self.events(rd, "stall")[0]["note"])
        notes = self.calls("notify")
        self.assertEqual(len(notes), 1)
        self.assertIn("waits for your OK", notes[0]["args"][0])
        self.assertEqual(self.calls("write"), [])
        self.assertGreater(len(self.calls("tail")), 3)  # checked again and again, notified once

    def test_rate_limit_marks_the_agent_limited(self):
        self.sb.window("run", tail="⎿  You've hit your usage limit · resets 7pm (America/Chicago)")
        rd = self.make_run(tty="/dev/ttys900")
        out, _ = self.wait(rd, self.iterm(rd))
        self.assertEqual(out["state"], "stalled")
        self.assertEqual([e["reason"] for e in self.events(rd, "stall")], ["rate_limit"])
        lim = route.load_state(self.sb.runs).get("rate_limited", {}).get("codex")
        self.assertIsNotNone(lim)
        self.assertIn("usage limit", lim["message"])
        self.assertEqual(len(self.calls("notify")), 1)

    def test_a_broken_watchdog_never_breaks_the_run(self):
        rd = self.make_run()
        self.later(self.finish, rd, 2.5)  # later than 2 x stall_s: a working watchdog would have stopped it
        err = io.StringIO()
        with mock.patch.object(watchdog.Activity, "poll", side_effect=RuntimeError("disk on fire")), \
                contextlib.redirect_stderr(err):
            out, _ = self.wait(rd, self.watch(rd))
        self.assertEqual(out["state"], "done")
        self.assertIn("disk on fire", out["watchdog"]["error"])
        self.assertIn("the stall watchdog stopped working", err.getvalue())
        self.assertEqual(self.events(rd, "watchdog_stop"), [])
        rd2 = self.make_run()
        self.later(self.finish, rd2, 0.3)
        with contextlib.redirect_stderr(io.StringIO()):
            out, _ = self.wait(rd2, self.watch(rd2, stall_s="soon"))  # a bad value: wait the old way
        self.assertEqual(out["state"], "done")
        self.assertIn("could not start", out["watchdog"]["error"])

    def test_mac_sleep_is_not_idle_time(self):
        rd = self.make_run()
        wd = watchdog.Watchdog(rd, self.watch(rd), 0.1)
        wd._wall -= 1000.0  # the wall clock moved 1000 s more than the monotonic one: the Mac slept
        self.assertIsNone(wd.tick())
        self.assertGreater(wd.slept_s, 990)
        self.assertEqual(self.events(rd), [])

        class FakeWD:  # the loop moves the deadline by the time the Mac slept
            slept_s = 100.0

            def __init__(s):
                s.n = 0

            def tick(s):
                s.n += 1
                if s.n == 3:
                    self.finish(rd)
        out = live._wait_loop(rd, time.time() - 1, 0.05, 10.0, FakeWD())
        self.assertEqual(out["state"], "done")


class TestKeepAwake(WatchBase):
    def stub(self):
        p, log = os.path.join(self.sb.stubs, "caffeinate"), os.path.join(self.sb.stubs, "caffeinate.log")
        with open(p, "w") as f:
            f.write(f'#!/bin/bash\necho "$$ $*" >> {shlex.quote(log)}\n'
                    'while kill -0 "$3" 2>/dev/null; do sleep 0.1; done\n')
        os.chmod(p, 0o755)
        return p, log

    def test_caffeinate_runs_while_waiting_and_stops_after(self):
        stub, log = self.stub()
        rd = self.make_run()
        self.later(self.finish, rd, 1.0)
        out, _ = self.wait(rd, self.watch(rd, keep_awake=True, caffeinate=stub, stall_s=300))
        self.assertEqual(out["state"], "done")
        self.assertTrue(out["watchdog"]["keep_awake"])
        cpid, args = open(log).read().split(" ", 1)
        self.assertEqual(args.split(), ["-i", "-w", str(os.getpid())])
        self.assertTrue(wait_for(lambda: not alive(int(cpid)), 5), "caffeinate still runs after the wait")

    def test_off_or_missing_binary_is_skipped(self):
        stub, log = self.stub()
        self.assertIsNone(live.keep_awake(enabled=False, caffeinate=stub))
        self.assertIsNone(live.keep_awake(enabled=True, caffeinate=os.path.join(self.sb.tmp, "no-caffeinate")))
        self.assertFalse(live.live_setting("keep_awake", True))  # the sandbox config turns it off
        self.assertIsNone(live.keep_awake(caffeinate=stub))  # ... and keep_awake() follows the config
        self.assertFalse(os.path.exists(log))
        rd = self.make_run()
        self.later(self.finish, rd, 0.3)
        out, _ = self.wait(rd, self.watch(rd, keep_awake=None, caffeinate=stub, stall_s=300))
        self.assertFalse(out["watchdog"]["keep_awake"])

    def test_never_blocks_exit(self):
        stub, log = self.stub()
        code = (f"import sys; sys.path.insert(0, {REPO!r}); from ckdispatch import live; "
                f"p = live.keep_awake(enabled=True, caffeinate={stub!r}); print(p.pid)")
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30)
        self.assertLess(time.monotonic() - t0, 8)  # ck-run's exit is not held up (no pipes shared, never waited)
        cpid = int(r.stdout.strip())
        self.assertTrue(wait_for(lambda: not alive(cpid), 10), "caffeinate outlived the process it watched")


class TestTabs(WatchBase):
    def open(self, group, title="CK · codex · t · 1", terminal="iTerm2", state_dir=None):
        return live.open_window(self.osa, "/usr/bin/true", title, self.sb.env(), terminal=terminal,
                                profile="CK Live codex", group=group,
                                state_dir=state_dir or os.path.join(self.sb.runs, ".windows"))

    def state(self, group):
        with open(os.path.join(self.sb.runs, ".windows", f"{group}.json")) as f:
            return json.load(f)

    def test_one_window_per_ask_one_tab_per_run(self):
        self.sb.window("run", window_ids=[101, 102, 103])
        w1 = self.open("A-1105-1")
        self.assertEqual((w1["ok"], w1["window_id"], w1["tab"], w1["session_id"]), (True, "101", False, "sess-101-1"))
        self.assertEqual(w1["how"], "profile CK Live codex")
        self.assertEqual(self.state("A-1105-1")["window_id"], "101")
        w2 = self.open("A-1105-1", title="CK · codex · t · 2")
        self.assertEqual((w2["ok"], w2["window_id"], w2["tab"], w2["session_id"]), (True, "101", True, "sess-101-2"))
        self.assertEqual(w2["how"], "profile CK Live codex")  # the runner's "title locked" check reads it
        self.assertEqual([c["kind"] for c in self.calls()], ["window", "exists", "tab"])
        self.assertEqual(self.calls("exists")[0]["args"], ["101", "CK · "])
        self.assertEqual(self.calls("tab")[0]["args"], ["/usr/bin/true", "CK · codex · t · 2", "CK Live codex", "101"])
        self.assertEqual(self.state("A-1105-1")["tabs"], 2)
        w3 = self.open("A-1105-2")  # another ask: its own window
        self.assertEqual((w3["window_id"], w3["tab"]), ("102", False))

    def test_closed_window_falls_back_to_a_new_window(self):
        self.sb.window("run", window_ids=[101, 102, 103])
        self.open("A-1105-1")
        self.sb.window("run", window_ids=[101, 102, 103], closed_windows=["101"])
        w = self.open("A-1105-1")
        self.assertEqual((w["ok"], w["window_id"], w["tab"]), (True, "102", False))
        self.assertIn("the window of A-1105-1 (id 101) is closed", w["error"])
        self.assertEqual([c["kind"] for c in self.calls()], ["window", "exists", "window"])
        self.assertEqual(self.state("A-1105-1")["window_id"], "102")

    def test_tab_failure_falls_back_to_a_new_window(self):
        self.sb.window("run", window_ids=[101, 102], tab_fail=True)
        self.open("A-1105-1")
        w = self.open("A-1105-1")
        self.assertEqual((w["ok"], w["window_id"], w["tab"]), (True, "102", False))
        self.assertIn("iTerm2 tab in window 101", w["error"])
        self.assertEqual([c["kind"] for c in self.calls()], ["window", "exists", "tab", "window"])

    def test_no_group_or_terminal_is_the_old_behaviour(self):
        self.sb.window("run", window_ids=[101, 102, 103])
        sd = os.path.join(self.sb.runs, ".windows")
        w = live.open_window(self.osa, "/usr/bin/true", "CK · a", self.sb.env(), terminal="iTerm2", profile="P")
        self.assertEqual((w["window_id"], w["tab"]), ("101", False))
        w = self.open(None)
        self.assertEqual((w["window_id"], w["tab"]), ("102", False))
        w = self.open("A-1105-1", terminal="Terminal")
        self.assertEqual((w["terminal"], w["tab"], w["session_id"]), ("Terminal", False, None))
        self.assertEqual([c["kind"] for c in self.calls()], ["window", "window", "window"])
        self.assertFalse(os.path.exists(sd))

    def test_an_old_group_window_is_not_reused(self):
        self.sb.window("run", window_ids=[101, 102])
        self.open("A-1105-1")
        p = os.path.join(self.sb.runs, ".windows", "A-1105-1.json")
        st = self.state("A-1105-1")
        st["updated_ts"] = time.time() - live.GROUP_MAX_AGE_S - 60
        with open(p, "w") as f:
            json.dump(st, f)
        w = self.open("A-1105-1")
        self.assertEqual((w["window_id"], w["tab"]), ("102", False))
        self.assertEqual([c["kind"] for c in self.calls()], ["window", "window"])  # no check of an old window

    def test_scripts_never_type_into_a_shell_or_activate(self):
        for name in ("ITERM_TAB_APPLESCRIPT", "ITERM_EXISTS_APPLESCRIPT", "ITERM_SESSION_TAIL_APPLESCRIPT",
                     "ITERM_CLOSE_APPLESCRIPT", "NOTIFY_APPLESCRIPT", "ITERM_APPLESCRIPT"):
            text = "\n".join(getattr(live, name))
            with self.subTest(script=name):
                self.assertNotIn("activate", text)
                self.assertNotIn("& tab &", text)
                self.assertNotIn("write text", text)
        self.assertIn("create tab with profile prof command cmd", "\n".join(live.ITERM_TAB_APPLESCRIPT))
        self.assertIn('& "|" & sid', "\n".join(live.ITERM_APPLESCRIPT))
        for name in ("ITERM_EXISTS_APPLESCRIPT", "ITERM_SESSION_TAIL_APPLESCRIPT", "ITERM_WRITE_APPLESCRIPT",
                     "ITERM_CLOSE_APPLESCRIPT"):
            self.assertIn('if application "iTerm" is not running then return', "\n".join(getattr(live, name)))

    def test_live_sh_records_its_tty(self):
        text = live.script_text(run_dir="/r", cwd="/w", env={"PATH": "/p"}, argv=["/bin/agent", live.PROMPT],
                                prompt_path="/r/p.md", title="t", agent="codex")
        line = next(ln for ln in text.splitlines() if ln.startswith("CKTTY="))
        self.assertIn("live.tty", live.CONTROL_FILES)
        rd = self.make_run(alive_agent=False)
        env = {"RUN": rd, "PATH": "/usr/bin:/bin"}
        subprocess.run(["/bin/bash", "-c", line], stdin=subprocess.DEVNULL, env=env, timeout=10)
        self.assertIsNone(live.read_tty(rd))  # no terminal (the tests' fake windows): nothing recorded
        m, s = pty.openpty()
        try:
            subprocess.run(["/bin/bash", "-c", line], stdin=s, env=env, timeout=10)
        finally:
            os.close(m)
            os.close(s)
        self.assertTrue((live.read_tty(rd) or "").startswith("/dev/"), live.read_tty(rd))


class TestCloseTab(WatchBase):
    def test_closes_an_idle_tab_after_the_delay(self):
        self.sb.window("run", tail="✻ Done. Summary written.\n> ")
        rd = self.make_run(tty="/dev/ttys901")
        p = live.close_tab_later(self.osa, self.sb.env(), tty="/dev/ttys901", delay_s=1.0, run_dir=rd)
        self.assertIsNotNone(p)
        self.assertEqual(p.wait(timeout=30), 0)
        closes = self.calls("close")
        self.assertEqual([c["args"] for c in closes], [["", "/dev/ttys901"]])
        self.assertEqual(len(self.calls("tail")), 2)

    def test_keeps_a_tab_somebody_uses(self):
        self.sb.window("run", tails=["> ", "> what about the second plot?"])
        rd = self.make_run(tty="/dev/ttys901")
        p = live.close_tab_later(self.osa, self.sb.env(), session_id="sess-1", delay_s=1.0, run_dir=rd)
        self.assertEqual(p.wait(timeout=30), 0)
        self.assertEqual(self.calls("close"), [])

    def test_never_without_an_exact_tab_or_with_zero_delay(self):
        self.assertIsNone(live.close_tab_later(self.osa, None, tty="/dev/ttys1", delay_s=0))
        self.assertIsNone(live.close_tab_later(self.osa, None, delay_s=600))
        self.assertIsNone(live.close_tab_later(os.path.join(self.sb.tmp, "none"), None, tty="/dev/ttys1"))

    def test_closer_ends_early_when_osascript_is_gone(self):
        t0 = time.monotonic()
        self.assertEqual(live._closer_main([os.path.join(self.sb.tmp, "gone"), "", "/dev/ttys1", "600", ""]), 0)
        self.assertLess(time.monotonic() - t0, 5)  # the test sandbox's closers never linger for 10 minutes

    def test_a_cleanly_finished_run_gets_its_tab_closed(self):
        self.sb.window("run", tail="✻ Done.\n> ")
        rd = self.make_run(tty="/dev/ttys902")
        self.later(self.finish, rd, 0.5)
        out, _ = self.wait(rd, self.iterm(rd, stall_s=300, close_tab_after_s=1.0))
        self.assertEqual(out["state"], "done")
        self.assertTrue(out["watchdog"]["tab_closer"])
        self.assertTrue(wait_for(lambda: self.calls("close"), 30), "the tab was not closed")
        self.assertEqual(self.calls("close")[0]["args"], ["", "/dev/ttys902"])

    def test_a_stopped_run_keeps_its_tab(self):
        rd = self.make_run(tty="/dev/ttys903")
        out, _ = self.wait(rd, self.iterm(rd, close_tab_after_s=1.0))
        self.assertEqual(out["state"], "stalled")
        self.assertFalse(out["watchdog"]["tab_closer"])


class TestThroughTheRunner(unittest.TestCase):
    """The watchdog and the tabs as the real runner uses them (runner._live_phase passes group/state_dir and
    watch). The sandbox's default config has the watchdog off; these tests turn it on."""

    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def test_a_stalled_live_run_is_stopped_with_status_timeout(self):
        self.sb.set_live(watchdog=True, stall_s=1)
        self.sb.window("run")  # Terminal: the window text is never read
        self.sb.control(default={"live": {"sleep": 60}})  # the agent says nothing and writes nothing
        t0 = time.monotonic()
        p = self.sb.run("codex")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertLess(time.monotonic() - t0, 40)  # caught by the watchdog, long before the run's timeout
        rid = self.sb.run_id_from(p)
        res = self.sb.result(rid)
        self.assertEqual(res["status"], "timeout")
        self.assertTrue(any("stopped by the watchdog: no activity for" in n for n in res["notes"]), res["notes"])
        evs = [e["event"] for e in self.sb.ledger() if e["run_id"] == rid]
        self.assertEqual(evs.count("stall"), 1)
        self.assertLess(evs.index("watchdog_stop"), evs.index("result"))
        self.assertEqual([c["kind"] for c in self.sb.osascript_calls()], ["window", "notify"])
        agent = self.sb.calls()[-1]["pid"]
        self.assertTrue(wait_for(lambda: not alive(agent), 10), "the stalled agent still runs")

    def test_runs_of_one_ask_share_one_window(self):
        self.sb.set_live(terminal="iTerm2")
        self.sb.window("run", window_id=777)
        ask = self.sb.open_ask(size="M", title="Tabs ask")
        for agent in ("codex", "cursor"):
            p = self.sb.run(agent, "devfab", "--ask", ask)
            self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([c["kind"] for c in self.sb.osascript_calls()], ["window", "exists", "tab"])
        opened = [e for e in self.sb.ledger() if e["event"] == "live_window_opened"]
        self.assertEqual(len(opened), 2)
        self.assertIn("window_id=777", opened[1]["note"])
        self.assertIn("title locked: yes", opened[1]["note"])
        with open(os.path.join(self.sb.runs, ".windows", f"{ask}.json")) as f:
            st = json.load(f)
        self.assertEqual((st["window_id"], st["tabs"]), ("777", 2))


class TestWindowHelpers(WatchBase):
    def test_session_text_and_window_tail_read_one_session(self):
        self.sb.window("run", tail="line1\nline2\n\nline3", missing_sessions=["/dev/ttys404"])
        self.assertEqual(live.window_tail(self.osa, None, self.sb.env(), tty="/dev/ttys5"), "line1 | line2 | line3")
        self.assertEqual(self.calls("tail")[-1]["args"], ["", "/dev/ttys5"])
        self.assertIsNone(live.session_text(self.osa, self.sb.env(), tty="/dev/ttys404"))
        self.assertEqual(live.window_tail(self.osa, "4242", self.sb.env()), "line1 | line2 | line3")  # the old way
        self.assertEqual(self.calls("tail")[-1]["args"], ["4242"])
        self.assertIsNone(live.window_tail(self.osa, None, self.sb.env()))

    def test_send_text_needs_the_exact_session(self):
        ok, err = live.send_text(self.osa, self.sb.env(), "continue")
        self.assertFalse(ok)
        self.assertIn("no exact iTerm2 session", err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(live.send_text(self.osa, self.sb.env(), "continue", session_id="sess-9"), (True, None))
        self.assertEqual(self.calls("write")[0]["args"], ["sess-9", "", "continue"])

    def test_notify_passes_text_as_arguments(self):
        msg = 'codex waits "for" you; do shell script "rm -rf ~"'
        self.assertTrue(live.notify(self.osa, self.sb.env(), msg, "ck-run · A-1105-1"))
        c = self.calls("notify")[0]
        self.assertEqual(c["args"], [msg, "ck-run · A-1105-1"])
        self.assertNotIn("rm -rf", "\n".join(c["script"]))


if __name__ == "__main__":
    unittest.main()
