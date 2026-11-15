"""v4 survive: ck-run reattach and its waiter (reattach.py, BUILD_SPEC_v4 10.6, the E2 side).

The unit tests stub runner.finalize_run (E1's side of the contract) so they test reattach's choices alone; the
end-to-end tests use the real one. The "agents" are sleep processes or the stub CLIs; osascript is the fake.
Everything stays inside the test sandbox.
"""
import argparse
import contextlib
import fcntl
import io
import os
import secrets
import signal
import subprocess
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from test_launch import cli_has
from test_watchdog import V4_KEYS, alive, kill_group, orphan_sleep, v4_sandbox, wait_for

from ckdispatch import claims, launch, ledger, reattach, runner
from ckdispatch.config import Config
from ckdispatch.errors import Refused


def dead_pid() -> int:
    p = subprocess.Popen(["/usr/bin/true"])
    p.wait()
    return p.pid


class ReattachBase(unittest.TestCase):
    stub_finalize = True

    def setUp(self):
        self.sb = v4_sandbox()
        self.env = mock.patch.dict(os.environ, self.sb.env())
        self.env.start()
        self.keys = mock.patch.object(ledger, "KEYS", ledger.KEYS + [k for k in V4_KEYS if k not in ledger.KEYS])
        self.keys.start()
        os.makedirs(self.sb.runs, exist_ok=True)
        self.cfg = Config()
        self.pids, self.held, self.finalized = [], [], []
        self.fin = None
        if self.stub_finalize:
            self.fin = mock.patch.object(runner, "finalize_run", self.fake_finalize, create=True)
            self.fin.start()

    def tearDown(self):
        if self.fin:
            self.fin.stop()
        for h in self.held:
            h.release()
        for pid in self.pids:
            kill_group(pid)
        self.keys.stop()
        self.env.stop()
        self.sb.cleanup()

    def fake_finalize(self, cfg, run_id, *, note):
        """Contract 10.6 (E1): write the result event, print the summary path + summary, return the exit code."""
        self.finalized.append((run_id, note))
        ledger.append(cfg.runs_dir, event="result", run_id=run_id, status="ok",
                      note=f"finalized by ck-run reattach: {note}", **{"from": "codex", "to": "CK"})
        print(f"{cfg.runs_dir}/{run_id}/summary.md\nfinalized from disk")
        return 0

    def agent(self, seconds=60) -> int:
        pid = orphan_sleep(seconds)
        self.pids.append(pid)
        return pid

    def fake_run(self, agent="codex", *, alive_agent=False, exited=False, live_run=True, started=None,
                 ask="A-1105-1", title="Reattach test"):
        """A run whose ck-run is gone: a dispatch event and a run folder (live: live.claim and live.pid)."""
        rid = f"{time.strftime('%Y-%m-%dT%H%M%S')}-devfab-general-{agent}-{secrets.token_hex(2)}"
        rd = os.path.join(self.sb.runs, rid)
        os.makedirs(rd)
        ledger.append(self.sb.runs, event="dispatch", run_id=rid, agent=agent, title=title, workspace="devfab",
                      mode="read", status="dispatched", ask_id=ask, started_at=started or ledger.now_iso(),
                      **{"from": "CK", "to": agent})
        if live_run:
            open(os.path.join(rd, "live.claim"), "w").close()
            with open(os.path.join(rd, "live.pid"), "w") as f:
                f.write(str(self.agent() if alive_agent else dead_pid()))
            if exited:
                with open(os.path.join(rd, "live.exit"), "w") as f:
                    f.write("0\n")
        return rid, rd

    def record(self, rid, rd, *, crashed=True, agent_pid=None):
        """The run's record in .claims: crashed = its ck-run died (lock free), else held as by a live ck-run."""
        got = claims.acquire(self.sb.runs, {"run_id": rid, "agent": "codex", "workspace": "devfab", "mode": "read",
                                            "kind": "dispatch", "title": "T", "claims": [], "run_dir": rd})
        held = got["held"]
        if agent_pid:
            held.update(agent_pid=agent_pid)
        if crashed:
            os.close(held.fd)
            held.fd = None
        else:
            self.held.append(held)
        return claims.record_path(self.sb.runs, rid)

    def events(self, rid, name=None):
        return [e for e in self.sb.ledger() if e.get("run_id") == rid and (name is None or e["event"] == name)]

    def one(self, rid):
        """reattach_run for one run, as `ck-run reattach RUN`; (code, stdout)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = reattach.reattach_run(self.cfg, rid, reattach.release_stale(self.cfg))
        return code, out.getvalue()

    def cli(self, *argv):
        ap = argparse.ArgumentParser(prog="ck-run")
        sub = ap.add_subparsers(dest="cmd", required=True)
        reattach.register(sub)
        a = ap.parse_args(["reattach", *argv])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = a.func(self.cfg, a)
        return code, out.getvalue(), err.getvalue()


class TestReattachChoices(ReattachBase):
    def fake_start(self, calls):
        def start(cfg, slug, argv, **kw):
            calls.append((slug, argv, kw))
            return {"slug": slug, "sh": f"{cfg.runs_dir}/{slug}.sh", "out": f"{cfg.runs_dir}/{slug}.out", "pid": 4321}
        return start

    def test_agent_gone_finalizes_now(self):
        rid, _ = self.fake_run(exited=True)
        code, out = self.one(rid)
        self.assertEqual(code, 0)
        self.assertEqual([r for r, _ in self.finalized], [rid])
        self.assertIn("agent had ended", self.finalized[0][1])
        self.assertEqual([e["event"] for e in self.events(rid)], ["dispatch", "reattach", "result"])
        ev = self.events(rid, "reattach")[0]
        self.assertEqual((ev["status"], ev["ask_id"], ev["agent"]), ("finalizing", "A-1105-1", "codex"))
        self.assertIn("finishing it from what is on disk", out)
        self.assertIn("finalized from disk", out)  # finalize_run's own output is passed on

    def test_dead_live_pid_counts_as_gone(self):
        rid, _ = self.fake_run()  # live.pid of a process that ended, no live.exit (killed with its window)
        self.assertEqual(self.one(rid)[0], 0)
        self.assertEqual(len(self.finalized), 1)

    def test_agent_alive_gets_a_waiter(self):
        rid, _ = self.fake_run(alive_agent=True)
        calls = []
        with mock.patch.object(reattach.launch, "start", self.fake_start(calls)):
            code, out = self.one(rid)
        self.assertEqual(code, 0)
        self.assertEqual(self.finalized, [])
        parts = rid.split("-")
        slug = f"reattach-{parts[2].split('T')[1]}-{parts[-1]}"
        self.assertEqual(calls, [(slug, ["reattach", rid, "--wait"], {"ask_id": "A-1105-1", "cwd": self.sb.runs})])
        ev = self.events(rid, "reattach")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["status"], "waiting")
        self.assertIn(slug, ev[0]["note"])
        self.assertIn("waiter started", out)

    def test_the_waiter_is_a_real_detached_launch(self):
        rid, _ = self.fake_run(alive_agent=True)
        stub = os.path.join(self.sb.stubs, "ck-run-stub")
        with open(stub, "w") as f:
            f.write('#!/bin/bash\necho "stub: $*"\n')
        os.chmod(stub, 0o755)
        out = io.StringIO()
        with mock.patch.object(launch, "CK_RUN", stub), contextlib.redirect_stdout(out):
            self.assertEqual(reattach.reattach_run(self.cfg, rid, reattach.release_stale(self.cfg)), 0)
        ev = [e for e in self.sb.ledger() if e["event"] == "launch"]
        self.assertEqual(len(ev), 1)
        slug = ev[0]["slug"]
        self.pids.append(ev[0]["pid"])
        sh = open(os.path.join(self.sb.runs, f"{slug}.sh")).read()
        line = [ln for ln in sh.splitlines() if ln.startswith("cd ")][0]
        self.assertIn("export CK_RUN_HOST=", sh)
        self.assertTrue(line.endswith(f" reattach {rid} --wait \"$@\""), line)  # no --interactive, no prompt file
        self.assertIn(f"# what: reattach waiter for {rid}", sh)
        outp = os.path.join(self.sb.runs, f"{slug}.out")
        self.assertTrue(wait_for(lambda: launch.out_finished(outp), 30))
        self.assertIn(f"stub: reattach {rid} --wait", open(outp).read())
        # a second slug for a later waiter of the same run
        self.assertEqual(reattach.waiter_slug(self.cfg, rid), f"{slug}-2")

    def test_finished_run_is_a_no_op(self):
        rid, _ = self.fake_run(exited=True)
        ledger.append(self.sb.runs, event="result", run_id=rid, status="flagged", summary_path=None,
                      **{"from": "codex", "to": "CK"})
        code, out = self.one(rid)
        self.assertEqual(code, runner.EXIT["flagged"])
        self.assertIn("already finished (flagged); nothing to do", out)
        self.assertEqual(self.finalized, [])
        self.assertEqual(self.events(rid, "reattach"), [])

    def test_watched_run_is_left_alone(self):
        rid, rd = self.fake_run(alive_agent=True)
        self.record(rid, rd, crashed=False)  # its ck-run holds the record: alive
        calls = []
        with mock.patch.object(reattach.launch, "start", self.fake_start(calls)):
            code, out = self.one(rid)
        self.assertEqual(code, 0)
        self.assertIn("still watches it", out)
        self.assertEqual((calls, self.finalized, self.events(rid, "reattach")), ([], [], []))

    def test_stale_record_is_released_then_the_run_finalized(self):
        rid, rd = self.fake_run(exited=True)
        rec = self.record(rid, rd)  # its ck-run crashed: the record stays, its lock is free
        self.assertEqual(self.one(rid)[0], 0)
        self.assertFalse(os.path.exists(rec))
        self.assertEqual([e["event"] for e in self.events(rid)], ["dispatch", "stale", "reattach", "result"])

    def test_orphan_headless_agent_gets_a_waiter(self):
        rid, rd = self.fake_run(live_run=False)
        rec = self.record(rid, rd, agent_pid=self.agent())  # ck-run gone, its headless agent still runs
        calls = []
        with mock.patch.object(reattach.launch, "start", self.fake_start(calls)):
            self.assertEqual(self.one(rid)[0], 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(os.path.exists(rec))  # the claims stay while the agent runs
        self.assertEqual(self.finalized, [])

    def test_all_unfinished_runs_in_the_window(self):
        alive_rid, _ = self.fake_run(alive_agent=True, title="alive")
        gone_rid, _ = self.fake_run(exited=True, title="gone")
        done_rid, _ = self.fake_run(exited=True, title="done")
        ledger.append(self.sb.runs, event="result", run_id=done_rid, status="ok", **{"from": "codex", "to": "CK"})
        old = (datetime.now().astimezone() - timedelta(days=3)).isoformat(timespec="seconds")
        old_rid, _ = self.fake_run(exited=True, started=old, title="old")
        calls = []
        with mock.patch.object(reattach.launch, "start", self.fake_start(calls)):
            code, out, err = self.cli()
            self.assertEqual((code, err), (0, ""))
            self.assertEqual([c[1][1] for c in calls], [alive_rid])
            self.assertEqual([r for r, _ in self.finalized], [gone_rid])
            self.assertNotIn(done_rid, out)
            self.assertNotIn(old_rid, out)
            code, out, err = self.cli("--since", "4d")
            self.assertEqual(code, 0)
            self.assertEqual([r for r, _ in self.finalized], [gone_rid, old_rid])
            code, out, _ = self.cli("--since", "1h")
        self.assertIn(alive_rid, out)  # the agent still runs: a waiter again (this test's waiters are stubs)
        self.assertEqual(len(self.finalized), 2)
        self.assertEqual(len(calls), 3)

    def test_nothing_to_do(self):
        code, out, _ = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("no unfinished run started in the last 24h", out)

    def test_bad_input(self):
        with self.assertRaisesRegex(Refused, "not a valid run id"):
            self.cli("not-a-run")
        with self.assertRaisesRegex(Refused, "not in the ledger"):
            self.cli("2026-11-05T120000-devfab-general-codex-ab12")
        with self.assertRaisesRegex(Refused, "--since must look like"):
            self.cli("--since", "soon")
        with self.assertRaisesRegex(Refused, "--wait needs a run id"):
            self.cli("--wait")
        self.assertEqual(reattach.parse_since("90m"), 5400)
        self.assertEqual(reattach.parse_since("2d"), 172800)

    def test_without_finalize_run_the_run_stays_open(self):
        rid, _ = self.fake_run(exited=True)
        with mock.patch.object(runner, "finalize_run", None):
            with self.assertRaisesRegex(Refused, "no runner.finalize_run"):
                self.one(rid)


class TestWaiter(ReattachBase):
    def wait(self, rid):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = reattach.wait_and_finalize(self.cfg, rid)
        return code, out.getvalue()

    def test_waits_for_the_agent_then_finalizes(self):
        rid, rd = self.fake_run(alive_agent=False)
        pid = self.agent(1.5)
        with open(os.path.join(rd, "live.pid"), "w") as f:
            f.write(str(pid))
        t0 = time.monotonic()
        code, out = self.wait(rid)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(time.monotonic() - t0, 1.0)
        self.assertEqual([r for r, _ in self.finalized], [rid])
        self.assertIn("saw the agent end", self.finalized[0][1])
        self.assertEqual([e["event"] for e in self.events(rid)], ["dispatch", "reattach", "result"])

    def test_one_waiter_per_run(self):
        rid, rd = self.fake_run(alive_agent=True)
        fd = os.open(os.path.join(rd, "reattach.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)  # a waiter (another process) holds it
        try:
            code, out = self.wait(rid)
            self.assertEqual(code, 0)
            self.assertIn("another reattach waiter already waits", out)
            calls = []
            with mock.patch.object(reattach.launch, "start", lambda *a, **k: calls.append(a)):
                code, out = self.one(rid)
            self.assertIn("a reattach waiter already waits for it", out)
            self.assertEqual(calls, [])
        finally:
            os.close(fd)
        self.assertEqual(self.finalized, [])

    def test_an_idle_finished_live_agent_is_stopped_then_finalized(self):
        self.sb.set_live(close_tab_after_s=1)
        self.cfg = Config()
        rid, rd = self.fake_run(alive_agent=True)
        pid = int(open(os.path.join(rd, "live.pid")).read())
        for n in ("result.md", "summary.md"):
            with open(os.path.join(rd, n), "w") as f:
                f.write("done\n")
        code, _ = self.wait(rid)
        self.assertEqual(code, 0)
        self.assertTrue(wait_for(lambda: not alive(pid), 10))
        self.assertEqual(len(self.finalized), 1)
        self.assertIn("showed no activity for 1 s, so the waiter stopped it", self.finalized[0][1])

    def test_a_result_from_elsewhere_ends_the_wait(self):
        rid, _ = self.fake_run(alive_agent=True)

        def other():
            time.sleep(0.5)
            ledger.append(self.sb.runs, event="result", run_id=rid, status="ok", **{"from": "codex", "to": "CK"})
        t = threading.Thread(target=other, daemon=True)
        t.start()
        code, out = self.wait(rid)
        t.join()
        self.assertEqual(code, 0)
        self.assertIn("already finished (ok)", out)
        self.assertEqual(self.finalized, [])


@unittest.skipUnless(cli_has("reattach") and callable(getattr(runner, "finalize_run", None)),
                     "cli does not register `reattach` yet, or runner.finalize_run (E1) is missing")
class TestReattachEndToEnd(ReattachBase):
    stub_finalize = False

    def test_a_finished_run_and_an_empty_sweep(self):
        p = self.sb.run("codex", "devfab", "--no-live")
        self.assertEqual(p.returncode, 0, p.stderr)
        rid = self.sb.run_id_from(p)
        r = self.sb.ck("reattach", rid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already finished (ok); nothing to do", r.stdout)
        r = self.sb.ck("reattach")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no unfinished run", r.stdout)

    def test_kill_the_watcher_mid_run_and_the_result_still_lands(self):
        # the window (fake osascript, mode run) starts live.sh in its own session, like iTerm2; the agent stub works
        # 8 s. The ck-run watcher is killed like a Claude restart kills it; reattach starts a waiter, the waiter
        # finishes the run through runner.finalize_run when the agent ends.
        self.sb.window("run")
        self.sb.control(default={"live": {"sleep": 8}})
        with open(os.path.join(self.sb.runs, "kill-1_prompt.md"), "w") as f:
            f.write("Do only this: UNIQUE-REATTACH-TASK-12\n")
        p = self.sb.ck("launch", "kill-1", "run", "--agent", "codex", "--workspace", "devfab", "--title",
                       "Reattach e2e", "--mode", "read")
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = dict(r.split(": ", 1) for r in p.stdout.splitlines())
        watcher = int(rows["pid"])
        self.pids.append(watcher)

        def started():
            d = [e for e in self.sb.ledger() if e["event"] == "dispatch" and e.get("title") == "Reattach e2e"]
            return d and os.path.exists(os.path.join(self.sb.runs, d[0]["run_id"], "live.pid")) and d[0]["run_id"]
        rid = wait_for(started, 60)
        self.assertTrue(rid, "the launched run did not start its window")
        agent_pid = int(open(os.path.join(self.sb.runs, rid, "live.pid")).read())
        self.pids.append(agent_pid)
        os.killpg(watcher, signal.SIGKILL)  # the watcher dies; the agent's window lives on

        def watcher_gone():
            rec = next((x for x in claims.active(self.sb.runs)[0] if x.get("run_id") == rid), None)
            return rec is None or rec.get("orphan")
        self.assertTrue(wait_for(watcher_gone, 20))
        self.assertTrue(alive(agent_pid))
        r = self.sb.ck("reattach")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("waiter started", r.stdout)
        res = wait_for(lambda: [e for e in self.events(rid, "result")], 90)
        self.assertTrue(res, f"no result for {rid}; ledger: {[e['event'] for e in self.events(rid)]}")
        self.assertIn("finalized by ck-run reattach", res[0]["note"] or "")
        self.assertEqual(res[0]["status"], "ok", res[0]["note"])
        evs = [e["event"] for e in self.events(rid)]
        self.assertLess(evs.index("reattach"), evs.index("result"))
        waiter = [e for e in self.sb.ledger() if e["event"] == "launch" and str(e.get("slug")).startswith("reattach-")]
        self.assertEqual(len(waiter), 1)
        self.pids.append(waiter[0]["pid"])
        out = os.path.join(self.sb.runs, f"{waiter[0]['slug']}.out")
        self.assertTrue(wait_for(lambda: launch.out_finished(out), 30))
        self.assertIn("ck-run exit=0", open(out).read())
        r = self.sb.ck("reattach", rid)  # safe to rerun
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already finished (ok)", r.stdout)


if __name__ == "__main__":
    unittest.main()
