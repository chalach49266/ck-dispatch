"""Incidental path words are not flags; commands and secret locations still are.

All credential values below are synthetic. No credential files or real CLIs are read.
"""
import json
import unittest

from helpers import Sandbox
from ckdispatch import rulecheck


SKILL_PATH = ("/Users/ckasemtantikul/Library/CloudStorage/"
              "OneDrive-WashingtonUniversityinSt.Louis/Documents/PhD/"
              "Device Fabrication/.agents/skills/flexcompute-rf/SKILL.md")
CACHE_PATH = ("/Users/ckasemtantikul/PhD_local/fnd_sin_mw_stack/release/"
              "best_designs_v1/01_nc03_turned_d06/run/cache_key.json")
HISTORY_PATH = "sim_history/5_tune4/laneOX/"
FAKE_KEY = "sk-" + "FAKEONLY" * 4


class TestPathWords(unittest.TestCase):
    def check_clean(self, text):
        result = rulecheck.scan_transcript({"actions": []}, text, "")
        self.assertEqual(result, {"violations": [], "flags": [], "notes": []})

    def test_real_skill_path_in_file_lists(self):
        self.check_clean(json.dumps({"files_read": [SKILL_PATH], "files_changed": [SKILL_PATH]}))

    def test_real_cache_path_and_sim_history_report(self):
        # The old credential match was _history in sim_history, not cache_key.
        self.check_clean(json.dumps({"files_read": [CACHE_PATH, HISTORY_PATH],
                                     "report_md": f"Matching run and design are in `{HISTORY_PATH}`."}))

    def test_path_like_strings_in_prose(self):
        for path in (SKILL_PATH, CACHE_PATH, HISTORY_PATH, "./docs/flexcompute.md",
                     "../sim_history/run.json", "/tmp/sim_history.json", "/tmp/tidy3d.web.md"):
            for wrapper in ('"{}"', "'{}'", "`{}`", "{}"):
                with self.subTest(path=path, wrapper=wrapper):
                    self.check_clean("Read " + wrapper.format(path) + "\n")

    def test_true_output_cases_still_flag(self):
        cases = [
            ("cloud_sim", "tidy3d.web.run(simulation)"),
            ("cloud_sim", "flexcompute upload simulation.json"),
            ("cloud_sim", "/opt/bin/flexcompute upload /tmp/simulation.json"),
            ("cloud_sim", "/opt/bin/flexcompute --upload /tmp/simulation.json"),
            ("cloud_sim", "/tmp/tidy3d.web.run(simulation)"),
            ("cloud_sim", "web.run(simulation)"),
            ("credential", FAKE_KEY),
            ("credential", "export EXAMPLE_KEY=FAKEONLY"),
            ("credential", 'token="' + "FAKEONLY" * 4 + '"'),
            ("credential", "Read ~/.ssh/id_rsa"),
            ("credential", "Read ~/.tidy3d/config"),
            ("credential", "cat .env"),
            ("credential", "Read /tmp/.env"),
            ("credential", "Read ~/.zsh_history"),
            ("credential", "Read ~/.zsh_history."),
            ("cloud_sim", "flexcompute-rf"),
        ]
        for category, text in cases:
            with self.subTest(text=text):
                result = rulecheck.scan_transcript({"actions": []}, text, "")
                self.assertEqual(result["violations"], [])
                self.assertTrue(any(f.startswith(category + " ") for f in result["flags"]), result)

    def test_paths_do_not_hide_other_matches(self):
        for category, text in (("cloud_sim", f"{SKILL_PATH}\nflexcompute upload simulation.json"),
                               ("credential", f"{HISTORY_PATH}\ncat ~/.ssh/id_rsa"),
                               ("credential", json.dumps({"files_read": ["~/.tidy3d/config", "/tmp/.env"]})),
                               ("credential", f"/tmp/{FAKE_KEY}/result.json")):
            with self.subTest(text=text):
                result = rulecheck.scan_transcript({"actions": []}, text, "")
                self.assertTrue(any(f.startswith(category + " ") for f in result["flags"]), result)

    def test_agent_actions_keep_violations(self):
        for command in ("tidy3d.web.run(simulation)", "flexcompute upload /tmp/simulation.json",
                        "cat ~/.ssh/id_rsa", "cat ~/.tidy3d/config", "cat .env",
                        "export EXAMPLE_KEY=FAKEONLY", f"echo {FAKE_KEY}"):
            with self.subTest(command=command):
                result = rulecheck.scan_transcript(
                    {"actions": [{"tool": "Bash", "input": {"command": command}}]}, "", "")
                self.assertTrue(result["violations"], result)
                self.assertEqual(result["flags"], [])


class TestPathFlagExitCodes(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def test_path_only_report_exits_ok(self):
        self.sb.control(cursor={"report": f"Read `{SKILL_PATH}`, `{CACHE_PATH}` and `{HISTORY_PATH}`."})
        process = self.sb.run("cursor")
        self.assertEqual(process.returncode, 0, process.stderr)
        rc = self.sb.result(self.sb.run_id_from(process))["rulecheck"]
        self.assertEqual((rc["violations"], rc["flags"]), ([], []))

    def test_true_output_still_exits_flagged(self):
        self.sb.control(cursor={"report": f"web.run(simulation); {FAKE_KEY}; read ~/.tidy3d/config"})
        process = self.sb.run("cursor")
        self.assertEqual(process.returncode, 4, process.stderr)
        rc = self.sb.result(self.sb.run_id_from(process))["rulecheck"]
        self.assertEqual({flag.split()[0] for flag in rc["flags"]}, {"cloud_sim", "credential"})


if __name__ == "__main__":
    unittest.main()
