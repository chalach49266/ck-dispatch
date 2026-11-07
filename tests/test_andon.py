"""v4 andon (BUILD_SPEC_v4 10.3 and 10.9): one switch that stops new runs and reviews of a project.

andon.check(cfg, project, workspace_key) is what runner.execute calls after the ask gate; ck-run andon raise|clear|list
are the commands. Sandbox only (fixture ledger, test config); no agent CLI is started except in the end-to-end test,
which uses the stub CLIs and stops at the andon (nothing runs).
"""
import json
import os
import unittest

from test_views import Fixture, FixtureCase, cli_registers

from ckdispatch import andon
from ckdispatch.errors import Refused


class TestCheck(FixtureCase):
    def test_check_message_project_and_workspace(self):
        fx = self.fx
        andon.check(fx.cfg, "fnd_sin", "devfab")  # nothing raised: no error
        changed, st = andon.raise_andon(fx.cfg, "fnd_sin", "exit 5 on the counter run: raw folder write")
        self.assertTrue(changed)
        with self.assertRaises(Refused) as cm:
            andon.check(fx.cfg, "fnd_sin", "devfab")
        self.assertEqual(str(cm.exception),
                         f"ANDON raised for fnd_sin: exit 5 on the counter run: raw folder write (raised "
                         f"{st['raised_at']}). Clear it: ck-run andon clear --project fnd_sin --note \"<words>\"")
        andon.check(fx.cfg, "other_project", "devfab")  # another project in the same workspace still runs
        andon.check(fx.cfg, None, "devfab")             # a run without a project uses the workspace key
        andon.raise_andon(fx.cfg, "Device Fabrication", "stop devfab")  # a workspace alias = its key
        self.assertTrue(os.path.isfile(os.path.join(fx.runs, ".andon", "devfab.json")))
        with self.assertRaises(Refused) as cm:
            andon.check(fx.cfg, None, "devfab")
        self.assertIn("ANDON raised for devfab: stop devfab", str(cm.exception))
        with self.assertRaises(Refused) as cm:  # an andon on the workspace also stops its projects
            andon.check(fx.cfg, "other_project", "devfab")
        self.assertIn("ANDON raised for devfab", str(cm.exception))
        andon.check(fx.cfg, None, "nv")

    def test_unreadable_file_fails_safe(self):
        fx = self.fx
        d = os.path.join(fx.runs, ".andon")
        os.makedirs(d)
        with open(os.path.join(d, "nv.json"), "w") as f:
            f.write("{broken")
        with self.assertRaises(Refused) as cm:
            andon.check(fx.cfg, None, "nv")
        self.assertIn("cannot be read", str(cm.exception))
        changed, _ = andon.clear_andon(fx.cfg, "nv", "fixed the file")
        self.assertTrue(changed)
        andon.check(fx.cfg, None, "nv")

    def test_bad_names_and_empty_reason(self):
        fx = self.fx
        for bad in ("", "   ", "../x", "!!!"):
            with self.assertRaises(Refused, msg=bad):
                andon.raise_andon(fx.cfg, bad, "r")
        with self.assertRaises(Refused):
            andon.raise_andon(fx.cfg, "fnd_sin", "  ")
        self.assertEqual(andon.name_for(fx.cfg, "FND_SiN"), "fnd_sin")
        self.assertEqual(andon.name_for(fx.cfg, "NV Compact Magnetometer"), "nv")
        andon.check(fx.cfg, "../../etc", "washu")  # an odd project name never breaks the check


class TestCommands(FixtureCase):
    def test_raise_list_clear_and_ledger(self):
        fx = self.fx
        code, out, err = fx.cli("andon", "list")
        self.assertEqual((code, out.strip()), (0, "No andon raised. Every project may start runs."), err)
        code, out, err = fx.cli("andon", "raise", "--project", "fnd_sin", "--reason", "flagged twice today")
        self.assertEqual(code, 0, err)
        self.assertIn("ANDON raised for fnd_sin: flagged twice today", out)
        code, out, _ = fx.cli("andon", "raise", "--project", "fnd_sin", "--reason", "again")
        self.assertEqual(code, 0)
        self.assertIn("already raised", out)
        code, out, _ = fx.cli("andon", "list")
        self.assertIn("ANDON raised for fnd_sin since ", out)
        self.assertIn("flagged twice today", out)
        code, out, _ = fx.cli("andon", "list", "--json")
        self.assertEqual([r["name"] for r in json.loads(out)], ["fnd_sin"])
        code, out, _ = fx.cli("andon", "clear", "--project", "nv")
        self.assertEqual(code, 0)
        self.assertIn("No andon is raised for nv; nothing to clear. Raised now: fnd_sin.", out)
        code, out, err = fx.cli("andon", "clear", "--project", "FND_SiN", "--note", "chalach: ok, go on")
        self.assertEqual(code, 0, err)
        self.assertIn("ANDON cleared for fnd_sin", out)
        self.assertFalse(os.path.exists(os.path.join(fx.runs, ".andon", "fnd_sin.json")))
        evs = [e for e in fx.sb.ledger() if e["event"] == "andon"]
        self.assertEqual([(e["status"], e.get("project")) for e in evs], [("raised", "fnd_sin"), ("cleared", "fnd_sin")])
        self.assertEqual(evs[0].get("reason"), "flagged twice today")
        self.assertEqual(evs[1].get("note"), "chalach: ok, go on")
        self.assertIsNone(evs[0].get("run_id"))
        code, _, err = fx.cli("andon", "raise", "--project", "fnd_sin")
        self.assertEqual(code, 2)  # --reason is required (argparse)

    def test_recall_shows_the_andon(self):
        fx = self.fx
        fx.ask("A-1105-1", "counter", project="fnd_sin")
        andon.raise_andon(fx.cfg, "fnd_sin", "exit 5")
        code, out, _ = fx.cli("recall", "fnd_sin")
        self.assertIn("ANDON raised", out)
        self.assertIn("exit 5", out)


class TestEndToEnd(unittest.TestCase):
    """A real ck-run run (stub CLIs) is refused while its workspace's andon is raised (runner.execute, E1)."""

    def setUp(self):
        self.fx = Fixture()
        if not cli_registers("andon"):
            self.fx.close()
            self.skipTest("cli._parser does not register ckdispatch.andon yet (E1 integration)")

    def tearDown(self):
        self.fx.close()

    def test_run_refused_then_allowed(self):
        sb = self.fx.sb
        p = sb.ck("andon", "raise", "--project", "devfab", "--reason", "stop for the test")
        self.assertEqual(p.returncode, 0, p.stderr)
        p = sb.run("codex", "devfab", "--no-live")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("ANDON raised for devfab: stop for the test", p.stderr)
        self.assertEqual(sb.calls(), [], "no agent may start while the andon is raised")
        p = sb.ck("andon", "clear", "--project", "devfab", "--note", "test done")
        self.assertEqual(p.returncode, 0, p.stderr)
        p = sb.run("codex", "devfab", "--no-live")
        self.assertNotIn("ANDON", p.stderr)


if __name__ == "__main__":
    unittest.main()
