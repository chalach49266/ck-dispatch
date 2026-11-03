"""washu workspace (WashU HW coursework, chalach 2026-11-02): --workspace washu and its aliases are accepted,
paths with spaces survive every step, the original course files are read-only (agents edit only in
<course>/solutions and <course>/prep), runs go to the shared ledger, and the live iTerm2 launcher cds there.

Everything below runs in the test sandbox (helpers.Sandbox); the real ~/Downloads/WashU HW is never opened.
"""
import json
import os
import shlex
import shutil
import unittest
from unittest import mock

from helpers import REAL_CONFIG, Sandbox

from ckdispatch import guards, rulecheck
from ckdispatch.config import Config, inside, xp
from ckdispatch.errors import Refused
from ckdispatch.flags import AUTO_AREAS, EDIT_AREAS
from ckdispatch.runner import default_auto_scopes, write_claude_settings

COURSES = ["theory II WashU", "quantum II WashU", "stats mech WashU", "quantum hardware WashU",
           "classical mechanics WashU", "quantum information WashU", "AI physics WashU"]


def pairs(a):
    return [[str(a[i]), str(a[i + 1])] for i in range(len(a) - 1)]


class TestWashuConfigEntry(unittest.TestCase):
    """The real config/workspaces.json entry, read as JSON only (no path in it is opened)."""

    def setUp(self):
        with open(REAL_CONFIG) as f:
            self.cfg = json.load(f)
        self.w = self.cfg["workspaces"]["washu"]

    def test_root_ledger_and_keys_mirror_devfab_and_nv(self):
        self.assertEqual(self.w["root"], "/Users/ckasemtantikul/Downloads/WashU HW")
        self.assertEqual(xp(self.cfg["runs_dir"]), xp("~/PhD_local/agent_runs"))  # one ledger for every workspace
        self.assertEqual(set(self.w), set(self.cfg["workspaces"]["nv"]))
        self.assertEqual(set(self.w), set(self.cfg["workspaces"]["devfab"]))
        self.assertIn("washu", self.w["aliases"])

    def test_only_solutions_and_prep_of_each_course_are_writable(self):
        self.assertEqual(sorted(self.w["writable_only"]),
                         sorted(f"{c}/{sub}" for c in COURSES for sub in ("solutions", "prep")))
        self.assertEqual(self.w["raw_readonly"], [])  # read-only comes from writable_only, not from raw folders
        self.assertTrue({"CLAUDE.md", "HW Brain/CLAUDE.md"} <= set(self.w["protected_files"]))


class WashuUnit(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.env = mock.patch.dict(os.environ, {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config})
        self.env.start()
        self.cfg = Config()
        self.ws = self.cfg.workspace("washu")
        self.root = self.sb.washu
        self.sol = os.path.join(self.root, "theory II WashU", "solutions")
        self.prep = os.path.join(self.root, "theory II WashU", "prep")
        self.qsol = os.path.join(self.root, "quantum II WashU", "solutions")

    def tearDown(self):
        self.env.stop()
        self.sb.cleanup()

    def test_names_with_and_without_spaces(self):
        for name in ("washu", "WASHU", "WashU", "washu-hw", "WashU HW", "  WashU HW  "):
            with self.subTest(name=name):
                self.assertIs(self.cfg.workspace(name), self.ws)
        self.assertEqual(self.ws.root, self.root)
        self.assertEqual(self.ws.name, "WashU HW")
        for name in ("WashU HW2", "WashU_HW", ""):
            with self.subTest(name=name):
                with self.assertRaises(Refused):
                    self.cfg.workspace(name)

    def test_write_scopes_only_in_solutions_and_prep(self):
        os.makedirs(os.path.join(self.sol, "PS9 part a"))
        for agent in ("codex", "cursor", "agy"):
            for s in ("theory II WashU/solutions", "theory II WashU/prep", "quantum II WashU/solutions",
                      "theory II WashU/solutions/PS9 part a", os.path.join(self.sol, "..", "prep")):
                with self.subTest(agent=agent, scope=s):
                    got = guards.validate_write_scopes(self.cfg, self.ws, agent, [s], EDIT_AREAS)
                    self.assertEqual(got[0]["area"], "workspace")
                    self.assertTrue(inside(got[0]["path"], self.sol) or inside(got[0]["path"], self.prep)
                                    or got[0]["path"] == self.qsol, got)
        os.symlink(os.path.join(self.root, "theory II WashU", "PS9_5020"), os.path.join(self.sol, "orig"))
        refused = {
            "theory II WashU": "may only write inside",
            "theory II WashU/PS9_5020": "may only write inside",
            "theory II WashU/solutions/../PS9_5020": "may only write inside",
            "theory II WashU/solutions/orig": "may only write inside",  # symlink to the original files
            "classical mechanics WashU/HW1": "may only write inside",
            "HW Brain": "may only write inside",
            "classical mechanics WashU/solutions": "not an existing folder",
            self.root: "whole workspace root",
            ".claude": "read-only folder '.claude'",
            "v242": "read-only folder 'v242'",
        }
        for s, msg in refused.items():
            with self.subTest(scope=s):
                with self.assertRaises(Refused) as cm:
                    guards.validate_write_scopes(self.cfg, self.ws, "codex", [s], EDIT_AREAS)
                self.assertIn(msg, str(cm.exception))
        with self.assertRaises(Refused) as cm:  # claude edits workspaces only in auto mode
            guards.validate_write_scopes(self.cfg, self.ws, "claude", ["theory II WashU/solutions"], EDIT_AREAS)
        self.assertIn("claude may not edit in the workspace", str(cm.exception))
        guards.validate_write_scopes(self.cfg, self.ws, "claude", ["theory II WashU/solutions"], AUTO_AREAS)

    def test_auto_default_scopes_are_the_solutions_and_prep_that_exist(self):
        self.assertEqual(default_auto_scopes(self.ws), [self.sol, self.prep, self.qsol])
        self.assertEqual(default_auto_scopes(self.cfg.workspace("devfab")), [])
        self.assertEqual(default_auto_scopes(self.cfg.workspace("nv")), [os.path.join(self.sb.nv, "NV Project")])
        for d in (self.sol, self.prep, self.qsol):
            shutil.rmtree(d)
        with self.assertRaises(Refused) as cm:
            default_auto_scopes(self.ws)
        self.assertIn("none of the writable folders of WashU HW exists yet", str(cm.exception))

    def test_rulecheck_course_files_are_read_only(self):
        orig = os.path.join(self.root, "theory II WashU", "PS9_5020", "PS9_5020.tex")
        brain = os.path.join(self.root, "HW Brain", "log.md")
        mine = os.path.join(self.sol, "PS9 notes.tex")
        changed = [{"path": p, "dir": False} for p in (orig, brain, mine)]
        scopes = guards.validate_write_scopes(self.cfg, self.ws, "codex", default_auto_scopes(self.ws), AUTO_AREAS)
        run_dir = os.path.join(self.sb.runs, "2026-11-02T225000-washu-general-codex-abcd")
        for mode, sc in (("auto", scopes), ("auto", []), ("edit", scopes[:1])):
            with self.subTest(mode=mode, scopes=len(sc)):
                r = rulecheck.classify(self.cfg, self.ws, mode, sc, changed, run_dir)
                self.assertEqual(r["changed_in_scope"], [mine])
                self.assertEqual(len(r["violations"]), 2, r)
                self.assertTrue(any(orig in v for v in r["violations"]), r)
                self.assertTrue(any(brain in v for v in r["violations"]), r)

    def test_claude_auto_settings_leave_only_solutions_and_prep_open(self):
        p = os.path.join(self.sb.tmp, "claude-settings.json")
        write_claude_settings(self.cfg, p, auto_ws=self.ws)
        deny = json.load(open(p))["permissions"]["deny"]
        R = self.root
        for want in (f"Edit(/{R}/theory II WashU/PS9_5020/**)", f"Edit(/{R}/theory II WashU/theory project plan.md)",
                     f"Edit(/{R}/quantum II WashU/PS4_5240/**)", f"Edit(/{R}/classical mechanics WashU/HW1/**)",
                     f"Edit(/{R}/HW Brain/**)", f"Edit(/{R}/.claude/**)", f"Edit(/{R}/v242/**)",
                     f"Edit(/{R}/CLAUDE.md)", f"Edit(/{self.sb.devfab}/**)", f"Edit(/{self.sb.nv}/**)"):
            self.assertIn(want, deny)
        self.assertNotIn(f"Edit(/{R}/**)", deny)
        for rule in deny:  # no deny rule covers a writable folder (deny always wins in Claude)
            if rule.startswith("Edit(//"):
                target = rule[len("Edit(/"):-1]
                target = target[:-3] if target.endswith("/**") else target
                for w in self.ws.writable_only:
                    self.assertFalse(inside(w.literal, target), (rule, w.rel))
        h = os.path.join(self.sb.tmp, "claude-settings-read.json")
        write_claude_settings(self.cfg, h)  # read/edit: the whole workspace stays closed to Claude
        self.assertIn(f"Edit(/{R}/**)", json.load(open(h))["permissions"]["deny"])


class TestWashuRuns(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.root = os.path.realpath(self.sb.washu)
        self.sol = os.path.join(self.root, "theory II WashU", "solutions")

    def tearDown(self):
        self.sb.cleanup()

    def test_read_run_by_alias_with_space_lands_in_the_shared_ledger(self):
        p = self.sb.run("codex", "WashU HW", "--no-live")  # helpers pins --mode read
        self.assertEqual(p.returncode, 0, p.stderr)
        call = self.sb.calls()[-1]
        self.assertEqual(call["cwd"], self.root)
        self.assertIn(["-C", self.root], pairs(call["argv"]))
        self.assertIn(os.path.join(self.root, "HW Brain", "index.md"), call["prompt"])
        self.assertIn("READ-ONLY: everything in this workspace except theory II WashU/solutions/", call["prompt"])
        self.assertNotIn("sources are: \n", call["prompt"] + "\n")
        ev = self.sb.ledger()[-1]
        self.assertEqual((ev["workspace"], ev["status"], ev["event"]), ("washu", "ok", "result"))
        self.assertIn("-washu-", ev["run_id"])
        self.assertTrue(os.path.isfile(os.path.join(self.sb.runs, ev["run_id"], "summary.md")))

    def test_edit_in_solutions_ok_and_course_file_change_is_violation(self):
        mine = os.path.join(self.sol, "PS9 answer.tex")
        orig = os.path.join(self.root, "theory II WashU", "PS9_5020", "PS9_5020.tex")
        args = ("--mode", "edit", "--approved", "chalach: write the PS9 answer 22:50",
                "--write-scope", "theory II WashU/solutions", "--no-live")
        self.sb.control(default={"write": [mine]})
        p = self.sb.run("codex", "washu", *args)
        self.assertEqual(p.returncode, 0, p.stderr)
        call = self.sb.calls()[-1]
        self.assertEqual(call["cwd"], self.sol)
        self.assertIn(["-C", self.sol], pairs(call["argv"]))
        self.assertEqual(self.sb.result(self.sb.run_id_from(p))["files_changed"], [mine])
        self.sb.control(default={"write": [mine, orig]})
        p = self.sb.run("codex", "washu", *args)
        self.assertEqual(p.returncode, 5, p.stderr)
        v = self.sb.result(self.sb.run_id_from(p))["rulecheck"]["violations"]
        self.assertTrue(any(orig in x for x in v), v)

    def test_auto_mode_uses_existing_solutions_and_prep(self):
        p = self.sb.run("codex", "washu", "--mode", "auto", "--dry-run", "--no-live")
        self.assertEqual(p.returncode, 0, p.stderr)
        q = shlex.quote
        self.assertIn(f"cd {q(self.sol)} && ", p.stdout)
        self.assertIn(f"-C {q(self.sol)}", p.stdout)
        self.assertIn(f"--add-dir {q(os.path.join(self.root, 'theory II WashU', 'prep'))}", p.stdout)
        self.assertIn(f"--add-dir {q(os.path.join(self.root, 'quantum II WashU', 'solutions'))}", p.stdout)
        self.assertNotIn("classical mechanics WashU", p.stdout)
        p = self.sb.run("claude", "washu", "--mode", "auto", "--write-scope", "theory II WashU/PS9_5020", "--dry-run")
        self.assertEqual(p.returncode, 2)
        self.assertIn("may only write inside", p.stderr)
        for d in ("theory II WashU/solutions", "theory II WashU/prep", "quantum II WashU/solutions"):
            shutil.rmtree(os.path.join(self.root, d))
        p = self.sb.run("codex", "washu", "--mode", "auto", "--dry-run", "--no-live")
        self.assertEqual(p.returncode, 2)
        self.assertIn("none of the writable folders of WashU HW exists yet", p.stderr)
        self.assertEqual(self.sb.calls(), [])

    def test_live_iterm2_launcher_cds_into_the_workspace(self):
        self.sb.set_live(terminal="iTerm2")
        self.sb.window("run")
        for agent, mode, cwd in (("claude", "read", self.root), ("codex", "read", self.root),
                                 ("claude", "auto", self.root), ("codex", "auto", self.sol)):
            with self.subTest(agent=agent, mode=mode):
                p = self.sb.run(agent, "washu", "--mode", mode)
                self.assertEqual(p.returncode, 0, p.stderr)
                rd = os.path.join(self.sb.runs, self.sb.run_id_from(p))
                osa = self.sb.osascript_calls()[-1]["argv"]
                self.assertIn('tell application "iTerm"', osa)
                self.assertEqual(osa[-3], "/bin/bash " + shlex.quote(os.path.join(rd, "live.sh")))
                self.assertTrue(osa[-2].startswith(f"CK · {agent} · Test task · "), osa[-2])  # window title
                with open(os.path.join(rd, "live.sh")) as f:
                    self.assertIn(f"cd {shlex.quote(cwd)} ||", f.read())
                call = self.sb.calls()[-1]
                self.assertTrue(call["live"], call["argv"])
                self.assertEqual(call["cwd"], cwd)
                res = self.sb.result(self.sb.run_id_from(p))
                self.assertEqual((res["status"], res["live"]["state"]), ("ok", "done"), res.get("notes"))


if __name__ == "__main__":
    unittest.main()
