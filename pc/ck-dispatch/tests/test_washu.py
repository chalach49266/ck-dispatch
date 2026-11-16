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

from ckdispatch import claims, guards, rulecheck
from ckdispatch.config import Config, inside, xp
from ckdispatch.errors import Refused
from ckdispatch.flags import AUTO_AREAS, EDIT_AREAS
from ckdispatch.runner import auto_readonly_reason, default_auto_scopes, write_claude_settings

COURSES = ["Course A", "Course B", "Course C", "Course D",
           "Course E", "Course F", "Course G"]


def pairs(a):
    return [[str(a[i]), str(a[i + 1])] for i in range(len(a) - 1)]


class TestWashuConfigEntry(unittest.TestCase):
    """The real config/workspaces.json entry, read as JSON only (no path in it is opened)."""

    def setUp(self):
        with open(REAL_CONFIG) as f:
            self.cfg = json.load(f)
        self.w = self.cfg["workspaces"]["washu"]

    def test_root_ledger_and_keys_mirror_devfab_and_nv(self):
        # Mac, or the Windows + WSL copy (2026-11-15, config/workspaces_mac_2026-11-15.json.bak has the Mac file)
        self.assertTrue(self.w["root"].endswith("/Coursework"), self.w["root"])
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
        self.sol = os.path.join(self.root, "Course A", "solutions")
        self.prep = os.path.join(self.root, "Course A", "prep")
        self.qsol = os.path.join(self.root, "Course B", "solutions")

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
            for s in ("Course A/solutions", "Course A/prep", "Course B/solutions",
                      "Course A/solutions/PS9 part a", os.path.join(self.sol, "..", "prep")):
                with self.subTest(agent=agent, scope=s):
                    got = guards.validate_write_scopes(self.cfg, self.ws, agent, [s], EDIT_AREAS)
                    self.assertEqual(got[0]["area"], "workspace")
                    self.assertTrue(inside(got[0]["path"], self.sol) or inside(got[0]["path"], self.prep)
                                    or got[0]["path"] == self.qsol, got)
        os.symlink(os.path.join(self.root, "Course A", "PS9_5020"), os.path.join(self.sol, "orig"))
        refused = {
            "Course A": "may only write inside",
            "Course A/PS9_5020": "may only write inside",
            "Course A/solutions/../PS9_5020": "may only write inside",
            "Course A/solutions/orig": "may only write inside",  # symlink to the original files
            "Course E/HW1": "may only write inside",
            "HW Brain": "may only write inside",
            "Course E/solutions": "not an existing folder",
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
            guards.validate_write_scopes(self.cfg, self.ws, "claude", ["Course A/solutions"], EDIT_AREAS)
        self.assertIn("claude may not edit in the workspace", str(cm.exception))
        guards.validate_write_scopes(self.cfg, self.ws, "claude", ["Course A/solutions"], AUTO_AREAS)

    def test_auto_default_scopes_are_the_solutions_and_prep_that_exist(self):
        self.assertEqual(default_auto_scopes(self.ws), [self.sol, self.prep, self.qsol])
        self.assertEqual(default_auto_scopes(self.cfg.workspace("devfab")), [])
        self.assertEqual(default_auto_scopes(self.cfg.workspace("nv")),  # chalach 2026-11-03: AQS writable too
                         [os.path.join(self.sb.nv, "NV Project"), os.path.join(self.sb.nv, "AQS")])
        self.assertIsNone(auto_readonly_reason(self.ws))
        for d in (self.sol, self.prep, self.qsol):
            shutil.rmtree(d)
        # chalach 2026-11-03 19:34 CT: no writable folder -> the run goes read-only (execute() checks this first);
        # default_auto_scopes keeps refusing as a backstop, so [] never means "the whole workspace" here
        self.assertEqual(auto_readonly_reason(self.ws), "no writable folder")
        with self.assertRaises(Refused) as cm:
            default_auto_scopes(self.ws)
        self.assertIn("none of the writable folders of WashU HW exists yet", str(cm.exception))
        for d in (self.sol, self.prep, self.qsol):
            self.assertFalse(os.path.exists(d))

    def test_rulecheck_course_files_are_read_only(self):
        orig = os.path.join(self.root, "Course A", "PS9_5020", "PS9_5020.tex")
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
        for want in (f"Edit(/{R}/Course A/PS9_5020/**)", f"Edit(/{R}/Course A/theory project plan.md)",
                     f"Edit(/{R}/Course B/PS4_5240/**)", f"Edit(/{R}/Course E/HW1/**)",
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
        self.sol = os.path.join(self.root, "Course A", "solutions")

    def tearDown(self):
        self.sb.cleanup()

    def test_read_run_by_alias_with_space_lands_in_the_shared_ledger(self):
        p = self.sb.run("codex", "WashU HW", "--no-live")  # helpers pins --mode read
        self.assertEqual(p.returncode, 0, p.stderr)
        call = self.sb.calls()[-1]
        self.assertEqual(call["cwd"], self.root)
        self.assertIn(["-C", self.root], pairs(call["argv"]))
        self.assertIn(os.path.join(self.root, "HW Brain", "index.md"), call["prompt"])
        self.assertIn("READ-ONLY: everything in this workspace except Course A/solutions/", call["prompt"])
        self.assertNotIn("sources are: \n", call["prompt"] + "\n")
        ev = self.sb.ledger()[-1]
        self.assertEqual((ev["workspace"], ev["status"], ev["event"]), ("washu", "ok", "result"))
        self.assertIn("-washu-", ev["run_id"])
        self.assertTrue(os.path.isfile(os.path.join(self.sb.runs, ev["run_id"], "summary.md")))

    def test_edit_in_solutions_ok_and_course_file_change_is_violation(self):
        mine = os.path.join(self.sol, "PS9 answer.tex")
        orig = os.path.join(self.root, "Course A", "PS9_5020", "PS9_5020.tex")
        args = ("--mode", "edit", "--approved", "chalach: write the PS9 answer 22:50",
                "--write-scope", "Course A/solutions", "--no-live")
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
        self.assertIn(f"--add-dir {q(os.path.join(self.root, 'Course A', 'prep'))}", p.stdout)
        self.assertIn(f"--add-dir {q(os.path.join(self.root, 'Course B', 'solutions'))}", p.stdout)
        self.assertNotIn("Course E", p.stdout)
        p = self.sb.run("claude", "washu", "--mode", "auto", "--write-scope", "Course A/PS9_5020", "--dry-run")
        self.assertEqual(p.returncode, 2)
        self.assertIn("may only write inside", p.stderr)
        for d in ("Course A/solutions", "Course A/prep", "Course B/solutions"):
            shutil.rmtree(os.path.join(self.root, d))
        p = self.sb.run("codex", "washu", "--mode", "auto", "--dry-run", "--no-live")
        self.assertEqual(p.returncode, 0, p.stderr)  # 2026-11-03 19:34 CT: read-only auto, not exit 2
        self.assertIn("auto_readonly: no writable folder", p.stderr)
        self.assertIn(f"-C {q(self.root)} -s read-only", p.stdout)
        self.assertNotIn("workspace-write", p.stdout)
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


class TestWashuAutoReadOnly(unittest.TestCase):
    """chalach 2026-11-03 19:34 CT: "Change ck-run so auto mode in WashU runs read-only when no writable folder
    exists". With no <course>/solutions/ or prep/, auto mode still starts, in the agent's own auto mode, with no
    write area; ck-run says so (stderr NOTE, ledger/rulecheck auto_readonly) and creates no folder."""

    WRITABLE = ("Course A/solutions", "Course A/prep", "Course B/solutions")

    def setUp(self):
        self.sb = Sandbox()
        self.root = os.path.realpath(self.sb.washu)
        for d in self.WRITABLE:
            shutil.rmtree(os.path.join(self.root, d))
        self.course_file = os.path.join(self.root, "Course A", "PS9_5020", "PS9_5020.tex")
        self.sol = os.path.join(self.root, "Course A", "solutions")

    def tearDown(self):
        self.sb.cleanup()

    @staticmethod
    def js(path):
        with open(path) as f:
            return json.load(f)

    def no_writable_folder(self):
        cfg = self.js(self.sb.config)
        for rel in cfg["workspaces"]["washu"]["writable_only"]:
            self.assertFalse(os.path.exists(os.path.join(self.root, rel)), rel)

    def auto(self, agent, *extra):  # ck-run's default mode (no --mode flag)
        return self.sb.ck("run", "--agent", agent, "--workspace", "washu", "--title", "List courses",
                          "--prompt-file", self.sb.task("List the course folders, reply hello, change nothing."),
                          "--no-live", *extra)

    def test_all_four_agents_start_read_only_and_finish_ok(self):
        with open(self.course_file) as f:
            before = f.read()
        for agent in ("claude", "codex", "cursor", "agy"):
            with self.subTest(agent=agent):
                p = self.auto(agent)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn(f"ck-run: NOTE ({agent}): auto_readonly: no writable folder. None of the writable "
                              "folders of WashU HW exists yet (Course A/solutions/", p.stderr)
                self.assertNotIn("REFUSED", p.stderr)
                rid = self.sb.run_id_from(p)
                rd = os.path.join(self.sb.runs, rid)
                res = self.sb.result(rid)
                self.assertEqual((res["status"], res["mode"], res["auto_readonly"]), ("ok", "auto", "no writable folder"))
                self.assertEqual(res["files_changed"], [])
                self.assertIn("auto_readonly: no writable folder (auto mode, no write area)", res["notes"])
                rc = self.js(os.path.join(rd, "rulecheck.json"))
                self.assertEqual(rc["auto_readonly"], "no writable folder")
                self.assertTrue(rc["notes"][0].startswith("auto_readonly: no writable folder: none of the writable "
                                                          "folders of WashU HW existed"), rc["notes"])
                self.assertEqual(self.js(os.path.join(rd, "packet.json"))["auto_readonly"], "no writable folder")
                evs = [e for e in self.sb.ledger() if e["run_id"] == rid]
                self.assertEqual({e["auto_readonly"] for e in evs}, {"no writable folder"})
                disp = next(e for e in evs if e["event"] == "dispatch")
                self.assertEqual((disp["mode"], disp["write_scope"], disp["claims"]), ("auto", [], []))
                self.assertIn("auto_readonly: no writable folder", disp["note"])
                self.assertIn("auto_readonly: no writable folder", next(e for e in evs if e["event"] == "result")["note"])
                with open(os.path.join(rd, "cmd.txt")) as f:
                    self.assertIn("# auto_readonly: no writable folder", f.read())
                call = self.sb.calls()[0]
                self.assertEqual(call["cwd"], self.root)
                self.assertIn("READ-ONLY for this run (auto_readonly: no writable folder)", call["prompt"])
                self.assertIn("do not create any of those folders", call["prompt"])
                self.assertIn("READ-ONLY: everything in this workspace (none of its writable folders exists yet",
                              call["prompt"])
                self.assertNotIn("Keep PROGRESS.md", call["prompt"])
                self.assertNotIn("Change files ONLY inside", call["prompt"])
                a = call["argv"]
                if agent == "claude":
                    self.assertIn(["--permission-mode", "auto"], pairs(a))
                    self.assertNotIn("--add-dir", a)
                    deny = self.js(os.path.join(rd, "claude-settings.json"))["permissions"]["deny"]
                    self.assertIn(f"Edit(/{self.root}/**)", deny)  # the whole workspace stays closed
                elif agent == "codex":
                    for pr in (["-C", self.root], ["-s", "read-only"], ["-c", 'approval_policy="on-request"'],
                               ["-c", 'approvals_reviewer="auto_review"']):
                        self.assertIn(pr, pairs(a))
                    self.assertNotIn("workspace-write", a)
                elif agent == "cursor":
                    self.assertIn("--force", a)
                    self.assertIn(["--workspace", self.root], pairs(a))
                else:
                    self.assertIn(["--mode", "auto"], pairs(a))
                if agent in ("cursor", "agy"):  # the mandatory post-run review still runs, and knows the run is read-only
                    rv = next(e for e in evs if e["event"] == f"{agent}_review")
                    self.assertEqual(rv["status"], "ok", rv["note"])
                    with open(os.path.join(rd, "review-input.md")) as f:
                        self.assertIn("none: this auto run had no write area (auto_readonly: no writable folder)", f.read())
                    review_call = self.sb.calls()[-1]
                    self.assertIn("none: this auto run had no write area", review_call["prompt"])
                self.sb.control()
        self.no_writable_folder()
        with open(self.course_file) as f:
            self.assertEqual(f.read(), before)

    def test_live_claude_and_codex_keep_auto_mode_with_no_write_area(self):
        self.sb.set_live(terminal="iTerm2")
        self.sb.window("run")
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                p = self.sb.run(agent, "washu", "--mode", "auto", "--live")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("auto_readonly: no writable folder", p.stderr)
                rid = self.sb.run_id_from(p)
                rd = os.path.join(self.sb.runs, rid)
                res = self.sb.result(rid)
                self.assertEqual((res["status"], res["live"]["state"], res["auto_readonly"]),
                                 ("ok", "done", "no writable folder"), res.get("notes"))
                call = self.sb.calls()[-1]
                self.assertTrue(call["live"])
                self.assertIn(f"the only files you write are the two output files result.md and summary.md in {rd}",
                              call["prompt"])
                if agent == "codex":
                    for pr in (["-s", "read-only"], ["-a", "on-request"], ["-c", 'approvals_reviewer="auto_review"']):
                        self.assertIn(pr, pairs(call["argv"]))
                    self.assertNotIn("workspace-write", call["argv"])
                else:
                    self.assertEqual(call["argv"][-2:], ["auto", call["argv"][-1]])  # --permission-mode auto <prompt>
                    deny = self.js(os.path.join(rd, "claude-settings-live.json"))["permissions"]["deny"]
                    self.assertIn(f"Edit(/{self.root}/**)", deny)
                    self.assertFalse(any(r.startswith(f"Edit(/{rd}/**") for r in deny))  # its outbox stays open
                self.sb.control()
        self.no_writable_folder()

    def test_any_change_or_new_writable_folder_is_a_violation(self):
        self.sb.control(codex={"write": [self.course_file]})
        p = self.auto("codex")
        self.assertEqual(p.returncode, 5, p.stderr)
        v = self.sb.result(self.sb.run_id_from(p))["rulecheck"]["violations"]
        self.assertIn("file changed during an auto run with no write area (auto_readonly: no writable folder): "
                      + self.course_file, v)
        mine = os.path.join(self.sol, "PS9 answer.tex")
        self.sb.control(cursor={"mkdir": [self.sol], "write": [mine]})
        p = self.auto("cursor")
        self.assertEqual(p.returncode, 5, p.stderr)
        res = self.sb.result(self.sb.run_id_from(p))
        v = res["rulecheck"]["violations"]
        self.assertIn("writable folder Course A/solutions/ created during an auto run with no write area "
                      f"(auto_readonly: no writable folder): {self.sol}", v)
        self.assertTrue(any(mine in x and "no write area" in x for x in v), v)
        self.assertEqual(res["files_changed"], [])
        self.assertEqual(res["post_review"]["status"], "flagged")
        self.assertTrue(any(f["category"] == "outside_task_folder" and f["evidence"] == mine
                            for f in res["post_review"]["findings"]), res["post_review"]["findings"])

    def test_normal_auto_returns_once_a_writable_folder_exists(self):
        p = self.sb.run("codex", "washu", "--mode", "auto", "--dry-run", "--no-live")
        self.assertIn("auto_readonly", p.stderr)
        prep = os.path.join(self.root, "Course B", "prep")
        os.makedirs(prep)  # chalach (or CK) makes the folder; ck-run never does
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                p = self.auto(agent)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertNotIn("auto_readonly", p.stderr)
                rid = self.sb.run_id_from(p)
                self.assertIsNone(self.sb.result(rid)["auto_readonly"])
                disp = next(e for e in self.sb.ledger() if e["run_id"] == rid and e["event"] == "dispatch")
                self.assertEqual((disp["auto_readonly"], disp["write_scope"]), (None, [prep]))
                call = self.sb.calls()[0]
                self.assertIn("Change files ONLY inside: " + prep, call["prompt"])
                if agent == "codex":
                    self.assertIn(["-C", prep], pairs(call["argv"]))
                    self.assertIn(["-s", "workspace-write"], pairs(call["argv"]))
                else:
                    self.assertIn(["--add-dir", prep], pairs(call["argv"]))
                self.sb.control()
        self.assertFalse(os.path.exists(self.sol))

    def test_claim_and_missing_write_scope_are_still_refused(self):
        p = self.auto("codex", "--claim", "Course A/solutions")
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("REFUSED: --claim: this auto run is read-only (auto_readonly: no writable folder.", p.stderr)
        p = self.auto("codex", "--write-scope", "Course A/solutions")  # a named folder must exist
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("not an existing folder", p.stderr)
        self.assertEqual(self.sb.calls(), [])
        self.no_writable_folder()

    def test_generic_any_workspace_whose_writable_folders_are_all_missing(self):
        with mock.patch.dict(os.environ, {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config}):
            cfg = Config()
        nv = cfg.workspace("nv")  # nv: the same rule once NV Project/ and AQS/ are gone
        self.assertIsNone(auto_readonly_reason(nv))
        self.assertIsNone(auto_readonly_reason(cfg.workspace("devfab")))  # no writable list: whole workspace
        for d in ("NV Project", "AQS"):
            shutil.rmtree(os.path.join(self.sb.nv, d))
        self.assertEqual(auto_readonly_reason(nv), "no writable folder")
        p = self.sb.run("codex", "nv", "--mode", "auto", "--dry-run", "--no-live")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"None of the writable folders of {nv.name} exists yet (NV Project/, AQS/)", p.stderr)
        self.assertIn("-s read-only", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.sb.nv, "NV Project")))

    def test_rulecheck_and_attribution_units(self):
        with mock.patch.dict(os.environ, {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config}):
            cfg = Config()
        ws = cfg.workspace("washu")
        rd = os.path.join(self.sb.runs, "2026-11-03T193400-washu-general-codex-abcd")
        changed = [{"path": self.sol, "dir": True}, {"path": os.path.join(self.root, "Course A"), "dir": True},
                   {"path": os.path.join(self.sol, "a.md"), "dir": False}, {"path": os.path.join(rd, "result.md"), "dir": False}]
        r = rulecheck.classify(cfg, ws, "read", [], changed, rd, auto_readonly="no writable folder")
        self.assertEqual(len(r["violations"]), 2, r)
        self.assertTrue(r["violations"][0].startswith("writable folder Course A/solutions/ created"), r)
        self.assertEqual(len(r["notes"]), 1, r)  # the course folder's own mtime
        self.assertEqual(r["changed_in_scope"], [])
        plain = rulecheck.classify(cfg, ws, "read", [], changed, rd)  # a plain read run: unchanged wording
        self.assertIn(f"file changed during a read-only run: {os.path.join(self.sol, 'a.md')}", plain["violations"])
        rec = {"run_id": "x", "workspace": "washu", "mode": "auto", "write_scope": []}
        target = os.path.join(self.sol, "b.md")
        self.assertTrue(claims._other_may_write(cfg, rec, target))
        self.assertFalse(claims._other_may_write(cfg, dict(rec, auto_readonly="no writable folder"), target))


if __name__ == "__main__":
    unittest.main()
