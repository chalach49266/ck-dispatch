"""Concurrent runs (chalach 2026-11-03): no workspace lock; only overlapping --claim paths are refused.

Real ck-run processes with stub agent CLIs (tests/fake_cli.py), started at the same time with Popen.
"""
import glob
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from datetime import datetime

from helpers import CK_RUN, Sandbox

from ckdispatch import claims
from ckdispatch.runner import reserve_run_dir


class Base(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        for c in self.sb.calls():  # stray stub agents (a test that kills ck-run on purpose)
            try:
                os.kill(c["pid"], signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.sb.cleanup()

    def start(self, agent, ws="devfab", *extra, title="Concurrent task"):
        mode = [] if any(str(x).startswith("--mode") for x in extra) else ["--mode", "auto"]
        p = subprocess.Popen([sys.executable, "-I", CK_RUN, "run", "--agent", agent, "--workspace", ws,
                              "--title", title, "--prompt-file", self.sb.task(), "--no-live", *mode, *extra],
                             env=self.sb.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        return p

    def finish(self, p, timeout=90):
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err

    def records(self):
        out = []
        for f in sorted(glob.glob(os.path.join(self.sb.runs, claims.DIR, "*.json"))):
            try:
                with open(f) as fh:
                    out.append(json.load(fh))
            except (OSError, ValueError):
                pass
        return out

    def wait_for(self, cond, timeout=20.0, what="condition"):
        t0 = time.time()
        while time.time() - t0 < timeout:
            v = cond()
            if v:
                return v
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def wait_registered(self, n=1, **match):
        def got():
            rs = [r for r in self.records() if all(r.get(k) == v for k, v in match.items())]
            return rs if len(rs) >= n else None
        return self.wait_for(got, what=f"{n} registered run(s) {match}")

    def results(self):
        return [e for e in self.sb.ledger() if e["event"] == "result"]

    @staticmethod
    def window(ev):
        return (datetime.fromisoformat(ev["started_at"]).timestamp(),
                datetime.fromisoformat(ev["finished_at"]).timestamp())


class TestNoWorkspaceLock(Base):
    def test_two_runs_same_workspace_different_agents_run_at_once(self):
        self.sb.control(default={"sleep": 3})
        a = self.start("claude")
        b = self.start("codex")
        (ca, oa, ea), (cb, ob, eb) = self.finish(a), self.finish(b)
        self.assertEqual((ca, cb), (0, 0), ea + eb)
        evs = self.sb.ledger()
        self.assertNotIn("busy", [e["event"] for e in evs])
        res = self.results()
        self.assertEqual(sorted(e["agent"] for e in res), ["claude", "codex"])
        (s1, f1), (s2, f2) = self.window(res[0]), self.window(res[1])
        self.assertLessEqual(max(s1, s2), min(f1, f2), "the two runs did not overlap in time (serialized?)")
        # each run lists the other as concurrent in its rule check
        for e in res:
            rc = self.sb.result(e["run_id"])["rulecheck"]
            self.assertEqual(len(rc["concurrent"]), 1, rc["concurrent"])
        self.assertEqual(self.records(), [])  # both records released

    def test_edit_auto_and_read_runs_in_one_workspace_all_start(self):
        self.sb.control(default={"sleep": 3})
        ps = [self.start("codex", "devfab", "--mode", "edit", "--approved", "user: ok 2026-11-03",
                         "--write-scope", "Projects/Notes"),
              self.start("claude", "devfab", "--mode", "auto"),
              self.start("cursor", "devfab", "--mode", "read")]
        codes = [self.finish(p)[0] for p in ps]
        self.assertEqual(codes, [0, 0, 0], [p.stderr for p in ps])
        self.assertNotIn("busy", [e["event"] for e in self.sb.ledger()])

    def test_runs_in_different_workspaces_run_at_once(self):
        self.sb.control(default={"sleep": 2})
        a, b = self.start("claude", "devfab"), self.start("agy", "nv", "--mode", "read")
        self.assertEqual((self.finish(a)[0], self.finish(b)[0]), (0, 0))


class TestClaims(Base):
    def test_overlapping_claims_are_refused(self):
        self.sb.control(claude={"sleep": 5})
        a = self.start("claude", "devfab", "--claim", "Design Library/pages")
        rec = self.wait_registered(agent="claude")[0]
        self.assertEqual(rec["claims"], [os.path.join(self.sb.devfab, "Design Library", "pages")])
        for claim in ("Design Library/pages/page.md",            # inside A's claim
                      "Design Library",                           # contains A's claim
                      os.path.join(self.sb.devfab, "Design Library", "pages")):  # the same path, absolute
            with self.subTest(claim=claim):
                p = self.sb.ck("run", "--agent", "codex", "--workspace", "devfab", "--title", "Clash",
                               "--prompt-file", self.sb.task(), "--mode", "auto", "--no-live", "--claim", claim)
                self.assertEqual(p.returncode, 6, p.stderr)
                self.assertIn("BUSY: file claim conflict", p.stderr)
                self.assertIn(rec["run_id"], p.stderr)
                busy = self.sb.ledger()[-1]
                self.assertEqual((busy["event"], busy["status"], busy["agent"]), ("busy", "busy", "codex"))
                self.assertIn(rec["run_id"], busy["note"])
                self.assertFalse(os.path.exists(os.path.join(self.sb.runs, busy["run_id"])))  # folder released
        # codex never started. The registry record exists before claude's own call record, so wait for that one
        # (on a loaded machine the stub claude can still be starting here).
        self.wait_for(lambda: self.sb.calls() or None, what="claude's call record")
        self.assertEqual([c["agent"] for c in self.sb.calls()], ["claude"])
        self.assertEqual(self.finish(a)[0], 0)
        # once A is done its claim is gone
        p = self.sb.run("codex", "devfab", "--mode", "auto", "--no-live", "--claim", "Design Library/pages/page.md")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_non_overlapping_claims_run_at_once(self):
        self.sb.control(claude={"sleep": 4}, codex={"sleep": 1})
        a = self.start("claude", "devfab", "--claim", "Design Library/pages")
        self.wait_registered(agent="claude")
        b = self.start("codex", "devfab", "--claim", "Projects/Notes/note.md", "--claim", "Design Library/new-page.md")
        (ca, _, ea), (cb, _, eb) = self.finish(a), self.finish(b)
        self.assertEqual((ca, cb), (0, 0), ea + eb)
        self.assertNotIn("busy", [e["event"] for e in self.sb.ledger()])
        d = next(e for e in self.sb.ledger() if e["event"] == "dispatch" and e["agent"] == "codex")
        self.assertEqual(d["claims"], [os.path.join(self.sb.devfab, "Projects", "Notes", "note.md"),
                                       os.path.join(self.sb.devfab, "Design Library", "new-page.md")])

    def test_a_run_without_claims_is_never_blocked_by_claims(self):
        self.sb.control(claude={"sleep": 3})
        a = self.start("claude", "devfab", "--claim", "Design Library")
        self.wait_registered(agent="claude")
        self.assertEqual(self.sb.run("codex", "devfab", "--mode", "auto", "--no-live").returncode, 0)
        self.assertEqual(self.sb.run("cursor", "devfab", "--mode", "read", "--no-live").returncode, 0)
        self.assertEqual(self.finish(a)[0], 0)

    def test_other_runs_claims_reach_the_prompt_and_claude_deny_rules(self):
        self.sb.control(codex={"sleep": 4})
        a = self.start("codex", "devfab", "--claim", "Design Library/pages")
        rec = self.wait_registered(agent="codex")[0]
        p = self.sb.run("claude", "devfab", "--mode", "auto", "--no-live", "--claim", "Projects/Notes")
        self.assertEqual(p.returncode, 0, p.stderr)
        rd = os.path.join(self.sb.runs, self.sb.run_id_from(p))
        claim = os.path.join(self.sb.devfab, "Design Library", "pages")
        with open(os.path.join(rd, "claude-settings.json")) as f:
            deny = json.load(f)["permissions"]["deny"]
        self.assertIn(f"Edit(/{claim})", deny)
        self.assertIn(f"Edit(/{claim}/**)", deny)
        with open(os.path.join(rd, "prompt.md")) as f:
            prompt = f.read()
        self.assertIn("CONCURRENCY:", prompt)
        self.assertIn(f"Never edit paths that other live runs claimed: {claim} (run {rec['run_id']}, codex)", prompt)
        self.assertIn(f"This run claimed: {os.path.join(self.sb.devfab, 'Projects', 'Notes')}", prompt)
        with open(os.path.join(rd, "packet.json")) as f:
            self.assertEqual(json.load(f)["claims"], [os.path.join(self.sb.devfab, "Projects", "Notes")])
        self.assertEqual(self.finish(a)[0], 0)

    def test_bad_claims_are_refused(self):
        cases = [
            (["--mode", "read", "--claim", "Design Library/pages"], "read runs never block anyone"),
            (["--mode", "auto", "--claim", "Archive/old.md"], "outside this run's write area"),      # raw folder
            (["--mode", "auto", "--claim", "CLAUDE.md"], "outside this run's write area"),           # rule file
            (["--mode", "auto", "--claim", self.sb.nv], "outside this run's write area"),            # other workspace
            (["--mode", "edit", "--approved", "user: ok", "--write-scope", "Projects/Notes",
              "--claim", "Design Library/pages"], "outside this run's write area"),                 # outside the scope
        ]
        for extra, msg in cases:
            with self.subTest(extra=extra):
                p = self.sb.run("codex", "devfab", *extra, "--no-live")
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn(msg, p.stderr)
        self.assertEqual(self.sb.calls(), [])
        self.assertEqual([d for d in os.listdir(self.sb.runs) if not d.startswith(".") and
                          os.path.isdir(os.path.join(self.sb.runs, d))], [])  # refused runs leave no folder

    def test_dry_run_shows_claims_and_registers_nothing(self):
        p = self.sb.run("codex", "devfab", "--mode", "auto", "--claim", "Projects/Notes", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("# claims (no workspace lock; only overlapping claims are refused): "
                      + os.path.join(self.sb.devfab, "Projects", "Notes"), p.stdout)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.sb.ledger(), [])


class TestAutoClaims(Base):
    """chalach 2026-11-03 19:02 CT: "Yes, have edit runs claim their write folders automatically". Never a workspace
    root, a default write folder or a folder holding a wiki entry file: that would serialize every run there."""

    EDIT = ("--mode", "edit", "--approved", "user: ok 2026-11-03")

    def notes(self):
        return os.path.join(self.sb.devfab, "Projects", "Notes")

    def test_two_edit_runs_with_the_same_write_scope_second_is_refused(self):
        self.sb.control(cursor={"sleep": 5})
        a = self.start("cursor", "devfab", *self.EDIT, "--write-scope", "Projects/Notes")
        rec = self.wait_registered(agent="cursor")[0]
        self.assertEqual((rec["claims"], rec["claims_auto"]), ([self.notes()], [self.notes()]))
        p = self.sb.run("codex", "devfab", *self.EDIT, "--write-scope", "Projects/Notes", "--no-live")
        self.assertEqual(p.returncode, 6, p.stderr)
        self.assertIn("BUSY: file claim conflict", p.stderr)
        self.assertIn("(its --write-scope, claimed automatically)", p.stderr)
        self.assertIn(rec["run_id"], p.stderr)
        busy = self.sb.ledger()[-1]
        self.assertEqual((busy["event"], busy["status"], busy["agent"], busy["claims"]),
                         ("busy", "busy", "codex", [self.notes()]))
        self.assertFalse(os.path.exists(os.path.join(self.sb.runs, busy["run_id"])))
        self.assertNotIn("codex", [c["agent"] for c in self.sb.calls()])  # codex never started
        self.assertEqual(self.finish(a)[0], 0)
        d = next(e for e in self.sb.ledger() if e["event"] == "dispatch" and e["agent"] == "cursor")
        self.assertEqual(d["claims"], [self.notes()])
        # once A is done, the same folder is free again
        p = self.sb.run("codex", "devfab", *self.EDIT, "--write-scope", "Projects/Notes", "--no-live")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_write_scopes_inside_around_or_claimed_paths_are_refused(self):
        os.makedirs(os.path.join(self.notes(), "sub", "deep"))
        self.sb.control(cursor={"sleep": 5})
        a = self.start("cursor", "devfab", *self.EDIT, "--write-scope", "Projects/Notes/sub")
        rec = self.wait_registered(agent="cursor")[0]
        for extra in ((*self.EDIT, "--write-scope", "Projects/Notes"),                 # around A's folder
                      (*self.EDIT, "--write-scope", "Projects/Notes/sub/deep"),        # inside it
                      ("--mode", "auto", "--write-scope", "Projects/Notes/sub"),       # auto with an explicit scope
                      ("--mode", "auto", "--claim", "Projects/Notes/sub/new.md")):     # a plain --claim inside it
            with self.subTest(extra=extra):
                p = self.sb.run("codex", "devfab", *extra, "--no-live")
                self.assertEqual(p.returncode, 6, p.stderr)
                self.assertIn(rec["run_id"], p.stderr)
        self.assertNotIn("codex", [c["agent"] for c in self.sb.calls()])
        self.assertEqual(self.finish(a)[0], 0)

    def test_edit_runs_in_different_folders_run_at_once(self):
        self.sb.control(default={"sleep": 3})
        a = self.start("cursor", "devfab", *self.EDIT, "--write-scope", "Projects/Notes")
        b = self.start("codex", "devfab", *self.EDIT, "--write-scope", "Design Library/pages")
        c = self.start("claude", "devfab", *self.EDIT, "--write-scope", os.path.join(self.sb.local, "myrepo"))
        codes = [self.finish(p)[0] for p in (a, b, c)]
        self.assertEqual(codes, [0, 0, 0], [p.stderr for p in (a, b, c)])
        self.assertNotIn("busy", [e["event"] for e in self.sb.ledger()])
        res = {e["agent"]: e for e in self.results()}
        (s1, f1), (s2, f2) = self.window(res["cursor"]), self.window(res["codex"])
        self.assertLessEqual(max(s1, s2), min(f1, f2), "the two edit runs did not overlap in time")
        claimed = {e["agent"]: e["claims"] for e in self.sb.ledger() if e["event"] == "dispatch"}
        self.assertEqual(claimed, {"cursor": [self.notes()],
                                   "codex": [os.path.join(self.sb.devfab, "Design Library", "pages")],
                                   "claude": [os.path.join(self.sb.local, "myrepo")]})

    def test_shared_write_areas_are_never_claimed_automatically(self):
        """Two runs on the same root-level shared area both start: wiki folders, default write folders."""
        cases = [("devfab", "Design Library", "Design Library/index.md, a wiki entry file"),
                 ("devfab", "Design Library/questions", "open-questions.md, a wiki entry file"),
                 ("nv", "NV Project", "a default write folder of NV Compact Magnetometer (NV Project/)"),
                 ("nv", "NV Project/wiki", "ai_retrieval_guide.md, a wiki entry file"),
                 ("nv", "AQS", "a default write folder of NV Compact Magnetometer (AQS/)"),
                 ("washu", "Course A/solutions", "a default write folder of WashU HW (Course A/solutions/)")]
        for ws, scope, why in cases:
            with self.subTest(ws=ws, scope=scope):
                self.sb.control(default={"sleep": 2})
                ps = [self.start("cursor", ws, *self.EDIT, "--write-scope", scope),
                      self.start("codex", ws, *self.EDIT, "--write-scope", scope)]
                outs = [self.finish(p) for p in ps]
                self.assertEqual([o[0] for o in outs], [0, 0], [o[2] for o in outs])
                for o in outs:
                    rd = os.path.dirname(o[1].splitlines()[0])
                    with open(os.path.join(rd, "cmd.txt")) as f:
                        cmd = f.read()
                    self.assertIn("refused): none; not claimed automatically: ", cmd)
                    self.assertIn(why, cmd)
                    with open(os.path.join(rd, "packet.json")) as f:
                        self.assertEqual(json.load(f)["claims"], [])
        self.assertNotIn("busy", [e["event"] for e in self.sb.ledger()])

    def test_auto_runs_claim_only_an_explicit_write_scope(self):
        self.sb.control(claude={"sleep": 5})
        a = self.start("claude", "devfab", "--mode", "auto", "--write-scope", "Projects/Notes")
        rec = self.wait_registered(agent="claude")[0]
        self.assertEqual(rec["claims_auto"], [self.notes()])
        p = self.sb.run("codex", "devfab", "--mode", "auto", "--write-scope", "Projects/Notes", "--no-live")
        self.assertEqual(p.returncode, 6, p.stderr)
        # an auto run on its default area claims nothing and is never blocked; it is told about A's claim
        p = self.sb.run("codex", "devfab", "--mode", "auto", "--no-live")
        self.assertEqual(p.returncode, 0, p.stderr)
        rd = os.path.join(self.sb.runs, self.sb.run_id_from(p))
        with open(os.path.join(rd, "packet.json")) as f:
            self.assertEqual(json.load(f)["claims"], [])
        with open(os.path.join(rd, "prompt.md")) as f:
            self.assertIn(f"Never edit paths that other live runs claimed: {self.notes()} (run {rec['run_id']}, claude)",
                          f.read())
        # washu/nv auto runs without --write-scope get their default folders as scopes, but claim none of them
        for ws in ("washu", "nv"):
            q = self.sb.run("codex", ws, "--mode", "auto", "--no-live")
            self.assertEqual(q.returncode, 0, q.stderr)
            with open(os.path.join(self.sb.runs, self.sb.run_id_from(q), "packet.json")) as f:
                pk = json.load(f)
            self.assertTrue(pk["write_scope"])
            self.assertEqual(pk["claims"], [])
        self.assertEqual(self.finish(a)[0], 0)

    def test_explicit_claims_still_work_next_to_auto_claims(self):
        self.sb.control(codex={"sleep": 5})
        # a --claim inside the auto-claimed folder is covered by it; one in a second scope is kept
        a = self.start("codex", "devfab", *self.EDIT, "--write-scope", "Projects/Notes", "--write-scope", "Design Library",
                       "--claim", "Projects/Notes/note.md", "--claim", "Design Library/pages/page.md")
        rec = self.wait_registered(agent="codex")[0]
        page = os.path.join(self.sb.devfab, "Design Library", "pages", "page.md")
        self.assertEqual(rec["claims"], [self.notes(), page])
        self.assertEqual(rec["claims_auto"], [self.notes()])  # Design Library holds the wiki index: not claimed
        p = self.sb.run("claude", "devfab", "--mode", "auto", "--no-live", "--claim", "Design Library/pages")
        self.assertEqual(p.returncode, 6, p.stderr)
        self.assertNotIn("claimed automatically", p.stderr)  # this clash is the explicit --claim
        p = self.sb.run("claude", "devfab", "--mode", "auto", "--no-live", "--claim", "Design Library/maps")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.finish(a)[0], 0)

    def test_dry_run_and_active_show_auto_claims(self):
        p = self.sb.run("codex", "devfab", *self.EDIT, "--write-scope", "Projects/Notes", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"refused): {self.notes()}; claimed automatically (--write-scope): {self.notes()}", p.stdout)
        self.assertEqual(self.records(), [])
        self.sb.control(cursor={"sleep": 4})
        a = self.start("cursor", "devfab", *self.EDIT, "--write-scope", "Projects/Notes")
        self.wait_registered(agent="cursor")
        act = json.loads(self.sb.ck("active").stdout)
        runs = act["live"] if isinstance(act, dict) else act
        self.assertEqual([r["claims_auto"] for r in runs], [[self.notes()]])
        self.assertEqual(self.finish(a)[0], 0)


class TestSharedAreaUnit(unittest.TestCase):
    def setUp(self):
        from ckdispatch.config import Config
        self.sb = Sandbox()
        self.cfg = Config(self.sb.config)

    def tearDown(self):
        self.sb.cleanup()

    def area(self, path):
        return claims.shared_area(self.cfg, path, os.path.realpath(path))

    def test_shared_area(self):
        d, nv, w = self.sb.devfab, self.sb.nv, self.sb.washu
        for p in (d, os.path.dirname(d), os.path.join(d, "Design Library"), os.path.join(d, "Design Library", "maps"),
                  os.path.join(nv, "NV Project"), os.path.join(nv, "NV Project", "wiki"), os.path.join(nv, "AQS"),
                  os.path.join(w, "Course A", "solutions"), os.path.join(w, "Course A"),
                  os.path.join(w, "HW Brain"), self.sb.local):
            with self.subTest(shared=p):
                self.assertIsNotNone(self.area(p))
        for p in (os.path.join(d, "Projects", "Notes"), os.path.join(d, "Design Library", "pages"),
                  os.path.join(nv, "AQS", "MCG V2"), os.path.join(nv, "NV Project", "wiki", "pages"),
                  os.path.join(w, "Course A", "solutions", "PS9"), os.path.join(self.sb.local, "myrepo")):
            with self.subTest(narrow=p):
                self.assertIsNone(self.area(p))

    def test_from_scopes_by_mode(self):
        notes = os.path.join(self.sb.devfab, "Projects", "Notes")
        s = [{"path": notes, "literal": notes}]
        self.assertEqual(claims.from_scopes(self.cfg, "edit", "dispatch", True, s), ([notes], []))
        self.assertEqual(claims.from_scopes(self.cfg, "auto", "dispatch", True, s), ([notes], []))
        self.assertEqual(claims.from_scopes(self.cfg, "auto", "dispatch", False, s), ([], []))  # default area
        self.assertEqual(claims.from_scopes(self.cfg, "read", "dispatch", True, s), ([], []))
        self.assertEqual(claims.from_scopes(self.cfg, "edit", "review", True, s), ([], []))
        self.assertEqual(claims.merge([notes], [os.path.join(notes, "a.md"), "/x"], ["/x"]), [notes, "/x"])


class TestStaleClaims(Base):
    def test_crashed_run_claim_is_cleaned_up(self):
        self.sb.control(claude={"sleep": 60})
        a = self.start("claude", "devfab", "--claim", "Design Library/pages")
        rec = self.wait_for(lambda: next((r for r in self.records() if r.get("agent_pid")), None),
                            what="record with the agent pid")
        agent_pid = rec["agent_pid"]
        os.kill(a.pid, signal.SIGKILL)  # ck-run crashes; its agent CLI (own session) keeps running
        a.wait()
        again = ["run", "--agent", "codex", "--workspace", "devfab", "--title", "After crash",
                 "--prompt-file", self.sb.task(), "--mode", "auto", "--no-live", "--claim", "Design Library/pages/page.md"]
        p = self.sb.ck(*again)
        self.assertEqual(p.returncode, 6, "an orphaned agent still running keeps its claim: " + p.stderr)
        self.assertIn(f"agent pid {agent_pid} still running", p.stderr)
        os.kill(agent_pid, signal.SIGKILL)
        self.wait_for(lambda: not _alive(agent_pid), what="the stub agent to exit")
        p = self.sb.ck(*again)
        self.assertEqual(p.returncode, 0, p.stderr)
        stale = [e for e in self.sb.ledger() if e["event"] == "stale"]
        self.assertEqual(len(stale), 1)
        self.assertEqual((stale[0]["run_id"], stale[0]["status"], stale[0]["agent"]), (rec["run_id"], "failed", "claude"))
        self.assertEqual(stale[0]["claims"], [os.path.join(self.sb.devfab, "Design Library", "pages")])
        self.assertIn("ended without a result", stale[0]["note"])
        self.assertEqual(self.records(), [])
        thread = self.sb.ck("thread", rec["run_id"])
        self.assertIn("**stale**", thread.stdout)

    def test_live_window_agent_that_outlived_ck_run_keeps_its_claims(self):
        d = os.path.join(self.sb.runs, claims.DIR)
        os.makedirs(d)
        rd = os.path.join(self.sb.runs, "2026-11-03T120000-devfab-general-claude-11ve")
        os.makedirs(rd)
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with open(os.path.join(rd, "live.pid"), "w") as f:
                f.write(str(sleeper.pid))
            claim = os.path.join(self.sb.devfab, "Design Library", "pages")
            with open(os.path.join(d, "2026-11-03T120000-devfab-general-claude-11ve.json"), "w") as f:
                json.dump({"run_id": os.path.basename(rd), "agent": "claude", "workspace": "devfab", "mode": "auto",
                           "pid": 999999, "claims": [claim], "run_dir": rd}, f)
            p = self.sb.run("codex", "devfab", "--mode", "auto", "--no-live", "--claim", "Design Library/pages")
            self.assertEqual(p.returncode, 6, p.stderr)
            self.assertIn(f"agent pid {sleeper.pid} still running", p.stderr)
            with open(os.path.join(rd, "live.exit"), "w") as f:  # the window's agent has exited
                f.write("0\n")
            p = self.sb.run("codex", "devfab", "--mode", "auto", "--no-live", "--claim", "Design Library/pages")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual([e["run_id"] for e in self.sb.ledger() if e["event"] == "stale"], [os.path.basename(rd)])
        finally:
            sleeper.kill()
            sleeper.wait()

    def test_ck_run_active_lists_live_runs_and_cleans_stale_records(self):
        d = os.path.join(self.sb.runs, claims.DIR)
        os.makedirs(d)
        dead = "2026-11-03T120000-devfab-general-codex-dead"
        with open(os.path.join(d, f"{dead}.json"), "w") as f:  # nobody holds its flock: ck-run is gone
            json.dump({"run_id": dead, "agent": "codex", "workspace": "devfab", "mode": "auto", "pid": 999999,
                       "claims": ["/x/y"]}, f)
        with open(os.path.join(d, f"{dead}.json.4242.tmp"), "w") as f:
            f.write("{")
        self.sb.control(claude={"sleep": 3})
        a = self.start("claude", "devfab", "--claim", "Projects/Notes")
        live = self.wait_registered(agent="claude")[0]
        p = self.sb.ck("active")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual([r["run_id"] for r in out["live"]], [live["run_id"]])
        self.assertEqual(out["live"][0]["claims"], [os.path.join(self.sb.devfab, "Projects", "Notes")])
        self.assertEqual(out["stale_cleaned"], [])  # the new run already cleaned the dead record when it started
        self.assertEqual([e["run_id"] for e in self.sb.ledger() if e["event"] == "stale"], [dead])
        self.assertEqual(sorted(os.listdir(d)), sorted(["registry.lock", f"{live['run_id']}.json"]))
        self.assertEqual(self.finish(a)[0], 0)


class TestAttribution(Base):
    def test_read_run_is_not_blamed_for_a_concurrent_writers_change(self):
        note = os.path.join(self.sb.devfab, "Projects", "Notes", "concurrent.md")
        self.sb.control(claude={"sleep": 5}, codex={"write": [note], "files_changed": [note]})
        a = self.start("claude", "devfab", "--mode", "read")
        self.wait_registered(agent="claude")
        time.sleep(1.2)  # after A's marker
        self.assertEqual(self.sb.run("codex", "devfab", "--mode", "auto", "--no-live").returncode, 0)
        code, out, err = self.finish(a)
        self.assertEqual(code, 0, err)  # before: "file changed during a read-only run" = violation, exit 5
        rid = next(e["run_id"] for e in self.results() if e["agent"] == "claude")
        rc = self.sb.result(rid)["rulecheck"]
        self.assertEqual(rc["violations"], [])
        codex_rid = next(e["run_id"] for e in self.results() if e["agent"] == "codex")
        self.assertEqual(list(rc["changed_by_others"]), [codex_rid])
        self.assertIn(note, rc["changed_by_others"][codex_rid])  # (plus the folder whose mtime moved)
        self.assertTrue(any("attributed to concurrent run" in n and "read-only run does not write" in n
                            for n in rc["notes"]), rc["notes"])

    def test_changes_inside_another_runs_claim_are_that_runs(self):
        note = os.path.join(self.sb.devfab, "Projects", "Notes", "claimed.md")
        self.sb.control(claude={"sleep": 5}, codex={"write": [note], "files_changed": [note]})
        a = self.start("claude", "devfab", "--mode", "auto")
        self.wait_registered(agent="claude")
        time.sleep(1.2)
        p = self.sb.run("codex", "devfab", "--mode", "auto", "--no-live", "--claim", "Projects/Notes")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.sb.result(self.sb.run_id_from(p))["files_changed"], [note])
        self.assertEqual(self.finish(a)[0], 0)
        rid = next(e["run_id"] for e in self.results() if e["agent"] == "claude")
        res = self.sb.result(rid)
        self.assertEqual(res["files_changed"], [])
        self.assertTrue(any("its claim" in n for n in res["rulecheck"]["notes"]), res["rulecheck"]["notes"])

    def test_same_file_edited_by_two_concurrent_runs_is_flagged(self):
        f = os.path.join(self.sb.devfab, "Projects", "Notes", "note.md")
        self.sb.control(claude={"sleep": 4, "write": [f], "files_changed": [f]},
                        codex={"write": [f], "files_changed": ["Projects/Notes/note.md"]})
        a = self.start("claude", "devfab", "--mode", "auto")
        self.wait_registered(agent="claude")
        self.assertEqual(self.sb.run("codex", "devfab", "--mode", "auto", "--no-live").returncode, 0)
        code, out, err = self.finish(a)
        self.assertEqual(code, 4, err)  # the later run sees the earlier run's report: flagged
        rid = next(e["run_id"] for e in self.results() if e["agent"] == "claude")
        flags = self.sb.result(rid)["rulecheck"]["flags"]
        self.assertTrue(any("same file edited by this run and concurrent run" in x and f in x for x in flags), flags)

    def test_editing_a_file_another_live_run_claimed_is_flagged(self):
        f = os.path.join(self.sb.devfab, "Design Library", "pages", "page.md")
        self.sb.control(claude={"sleep": 4}, codex={"write": [f], "files_changed": [f]})
        a = self.start("claude", "devfab", "--mode", "auto", "--claim", "Design Library/pages")
        self.wait_registered(agent="claude")
        p = self.sb.run("codex", "devfab", "--mode", "auto", "--no-live")
        self.assertEqual(p.returncode, 4, p.stderr)
        flags = self.sb.result(self.sb.run_id_from(p))["rulecheck"]["flags"]
        self.assertTrue(any("edited a file claimed by concurrent run" in x for x in flags), flags)
        self.assertEqual(self.finish(a)[0], 0)


class TestPostReviewWithConcurrentRuns(Base):
    def test_cursor_review_does_not_blame_cursor_for_a_claimed_runs_edits_and_deletions(self):
        new = os.path.join(self.sb.devfab, "Projects", "Notes", "codex-new.md")
        old = os.path.join(self.sb.devfab, "Projects", "Notes", "note.md")
        self.sb.control(cursor={"sleep": 5}, codex={"write": [new], "delete": [old], "files_changed": [new, old]})
        a = self.start("cursor", "devfab", "--mode", "auto")
        self.wait_registered(agent="cursor")
        time.sleep(1.2)
        p = self.sb.run("codex", "devfab", "--mode", "auto", "--no-live", "--claim", "Projects/Notes")
        self.assertEqual(p.returncode, 0, p.stderr)
        code, out, err = self.finish(a)
        self.assertEqual(code, 0, err)
        rid = next(e["run_id"] for e in self.results() if e["agent"] == "cursor")
        res = self.sb.result(rid)
        self.assertEqual(res["post_review"]["status"], "ok", res["post_review"])
        self.assertEqual([f for f in res["post_review"]["findings"] if f["source"] == "ck-run"], [])
        with open(os.path.join(self.sb.runs, rid, "review-input.md")) as f:
            evidence = f.read()
        self.assertIn("## Other ck-run runs working at the same time", evidence)
        self.assertIn(f"claims: {os.path.join(self.sb.devfab, 'Projects', 'Notes')}", evidence)
        self.assertIn(new, evidence.split("ck-run attributed these changes")[1])
        reviewer = next(c for c in self.sb.calls() if c["prompt"].startswith("CK POST-RUN REVIEW"))
        self.assertIn("Other ck-run runs worked at the same time", reviewer["prompt"])


class TestConcurrentWritesAreSafe(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def test_run_folder_reservation_never_reuses_an_id(self):
        from ckdispatch.config import Config
        cfg = Config(self.sb.config)
        ids = iter(["2026-11-03T120000-devfab-general-codex-aaaa"] * 3 + ["2026-11-03T120000-devfab-general-codex-bbbb"])
        first, d1 = reserve_run_dir(cfg, lambda: "2026-11-03T120000-devfab-general-codex-aaaa")
        second, d2 = reserve_run_dir(cfg, lambda: next(ids))
        self.assertEqual((first, second), ("2026-11-03T120000-devfab-general-codex-aaaa",
                                           "2026-11-03T120000-devfab-general-codex-bbbb"))
        self.assertTrue(os.path.isdir(d1) and os.path.isdir(d2))
        self.assertEqual(os.stat(d2).st_mode & 0o777, 0o700)

    def test_ledger_appends_from_many_processes_stay_whole_lines(self):
        runs = os.path.join(self.sb.tmp, "ledger-test")
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from ckdispatch import ledger\n"
                "for i in range(60): ledger.append(sys.argv[2], event='result', run_id=f'{sys.argv[3]}-{i}', "
                "note='x' * 3000, files_read=['/p'] * 150)")
        repo = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        ps = [subprocess.Popen([sys.executable, "-I", "-c", code, repo, runs, f"w{n}"]) for n in range(8)]
        self.assertEqual([p.wait(timeout=60) for p in ps], [0] * 8)
        with open(os.path.join(runs, "ledger.jsonl")) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 8 * 60)
        evs = [json.loads(x) for x in lines]  # every line parses
        self.assertEqual(len({e["run_id"] for e in evs}), 8 * 60)

    def test_registry_in_process(self):
        runs = os.path.join(self.sb.tmp, "reg")
        a = claims.acquire(runs, {"run_id": "A", "claims": ["/w/x"]})
        self.assertTrue(a["ok"])
        b = claims.acquire(runs, {"run_id": "B", "claims": ["/w/x/y.md"]})
        self.assertFalse(b["ok"])
        self.assertEqual([(m, t, r["run_id"]) for m, t, r in b["conflicts"]], [("/w/x/y.md", "/w/x", "A")])
        c = claims.acquire(runs, {"run_id": "C", "claims": ["/w/xy"]})  # /w/xy is not inside /w/x
        self.assertTrue(c["ok"])
        self.assertEqual([r["run_id"] for r in c["others"]], ["A"])
        live, stale = claims.active(runs)
        self.assertEqual(sorted(r["run_id"] for r in live), ["A", "C"])
        a["held"].release()
        c["held"].release()
        self.assertEqual(claims.active(runs), ([], []))
        self.assertTrue(claims.acquire(runs, {"run_id": "B", "claims": ["/w/x/y.md"]})["ok"])

    def test_state_json_updates_from_many_processes_keep_every_agent(self):
        runs = os.path.join(self.sb.tmp, "state-test")
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from ckdispatch import route\n"
                "for i in range(20): route.mark_limited(sys.argv[2], sys.argv[3], 'rate limit')")
        repo = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        ps = [subprocess.Popen([sys.executable, "-I", "-c", code, repo, runs, a])
              for a in ("claude", "codex", "cursor", "agy")]
        self.assertEqual([p.wait(timeout=60) for p in ps], [0] * 4)
        with open(os.path.join(runs, "state.json")) as f:
            self.assertEqual(sorted(json.load(f)["rate_limited"]), ["agy", "claude", "codex", "cursor"])


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie child of nobody we can reap: treat as gone once it is not running
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(out) and not out.startswith("Z")
    except OSError:
        return True


if __name__ == "__main__":
    unittest.main()
