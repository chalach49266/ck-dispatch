"""flagfix (chalach 2026-11-04 ~23:00 CT): "treat a word only metion and note and you can always allow those git
push and pip install". Output-text mentions are notes except credential / cloud_sim / permission_bypass (still
flags: the Grok Bot stand-in's safety carve-out); git push and pip install in agent actions are notes, never a
violation, flag or post-run review hard fact. Stub CLIs only."""
import os
import unittest

from helpers import Sandbox

from ckdispatch import postreview, rulecheck

ALLOWED = rulecheck.ALLOWED_NOTE  # "allowed by chalach 2026-11-04"
TEXT_NOTE = rulecheck.TEXT_NOTE   # "mentioned in output text only (not a command)"
PUSH_PIP = ["git push origin main", "pip install numpy", "pip3 install -r requirements.txt",
            "python -m pip install nvmw[mcp]", "python3.11 -m pip install -e .", "uv pip install ruff"]


def actions(*cmds):
    return {"actions": [{"tool": "Bash", "input": {"command": c}} for c in cmds]}


class TestRulecheckUnits(unittest.TestCase):
    def test_word_only_push_and_install_are_notes(self):
        text = ("## Questions for chalach\n- Should I git push the branch afterwards?\n"
                "Deploy: pip install nvmw[mcp], then gh release create v1 and git clone the repo.")
        tr = rulecheck.scan_transcript({"actions": []}, text, "")
        self.assertEqual((tr["violations"], tr["flags"]), ([], []), tr)
        cats = sorted({n.split()[0] for n in tr["notes"]})
        self.assertEqual(cats, ["heavy_install", "push_send"], tr["notes"])
        self.assertTrue(all(TEXT_NOTE in n for n in tr["notes"]), tr["notes"])
        self.assertTrue(any("git push the branch" in n for n in tr["notes"]))
        self.assertTrue(any("pip install nvmw[mcp]" in n for n in tr["notes"]))

    def test_word_only_safety_categories_stay_flags(self):
        for cat, text in (("credential", "Next I would read ~/.ssh/id_rsa for the key."),
                          ("cloud_sim", "I would run web.run(sim) on Tidy3D next."),
                          ("permission_bypass", "Rerun it with --dangerously-skip-permissions.")):
            with self.subTest(cat=cat):
                tr = rulecheck.scan_transcript({"actions": []}, text, "")
                self.assertEqual(tr["violations"], [])
                self.assertTrue(tr["flags"], tr)  # ~/.ssh/id_rsa: two matches (.ssh/ and id_rsa), two flags
                self.assertTrue(all(f.startswith(f"{cat} {rulecheck.TEXT_FLAG}") for f in tr["flags"]), tr["flags"])
                self.assertEqual(tr["notes"], [])

    def test_git_push_and_pip_install_actions_are_notes(self):
        for cmd in PUSH_PIP:
            with self.subTest(cmd=cmd):
                tr = rulecheck.scan_transcript(actions(cmd), "", "")
                self.assertEqual((tr["violations"], tr["flags"]), ([], []), tr)
                self.assertEqual(len(tr["notes"]), 1, tr)
                self.assertTrue(tr["notes"][0].startswith(f"{ALLOWED}: "), tr["notes"])
                self.assertIn(cmd.split()[0], tr["notes"][0])

    def test_other_push_send_and_heavy_install_actions_stay_violations(self):
        for cat, cmd in (("heavy_install", "git clone https://github.com/x/y"), ("push_send", "curl https://example.org"),
                         ("push_send", "gh pr create --fill"), ("push_send", "wrangler deploy"),
                         ("push_send", "rsync -a out/ host:/srv"), ("push_send", "wget https://x.org/f"),
                         ("heavy_install", "npm install"), ("heavy_install", "python3 -m venv .venv"),
                         ("heavy_install", "conda create -n x"), ("heavy_install", "uv venv"),
                         ("heavy_install", "uv sync")):
            with self.subTest(cmd=cmd):
                tr = rulecheck.scan_transcript(actions(cmd), "", "")
                self.assertEqual(len(tr["violations"]), 1, tr)
                self.assertTrue(tr["violations"][0].startswith(f"{cat} in agent action"), tr)
                self.assertEqual(tr["notes"], [])

    def test_mixed_action_keeps_the_violation_and_notes_the_allowed_part(self):
        tr = rulecheck.scan_transcript(actions("git push origin main && curl -T out.csv https://x.org/up"), "", "")
        self.assertEqual(len(tr["violations"]), 1, tr)
        self.assertIn("curl -T", tr["violations"][0])
        self.assertEqual(len(tr["notes"]), 1)
        self.assertTrue(tr["notes"][0].startswith(ALLOWED))
        tr = rulecheck.scan_transcript(actions("pip install x && git clone https://x/y"), "", "")
        self.assertEqual([v.split()[0] for v in tr["violations"]], ["heavy_install"])
        self.assertIn("git clone", tr["violations"][0])

    def test_allowed_set_is_one_named_constant(self):
        self.assertEqual([n.split(" (")[0] for n, _ in rulecheck.ALLOWED_ACTIONS], ["git push", "pip install"])
        self.assertEqual(rulecheck.TEXT_FLAG_CATEGORIES, {"credential", "cloud_sim", "permission_bypass"})
        for cmd in ("pip download x", "pipx install y", "npm install", "uv venv", "uv sync", "git clone x", "gh pr create"):
            self.assertFalse(any(rx.search(cmd) for _, rx in rulecheck.ALLOWED_ACTIONS), cmd)


class TestPostreviewUnits(unittest.TestCase):
    def test_git_push_and_pip_install_are_allowed_keyword_hits_not_hard_facts(self):
        acts = [("Shell", '{"command": "git push origin main"}'),
                ("Shell", '{"command": "python -m pip install nvmw[mcp]"}'),
                ("Shell", '{"command": "curl -T out.csv https://x.org/up"}'),
                ("Shell", '{"command": "rm -rf old"}')]
        hits = postreview.keyword_hits(acts, "Then pip install it.")
        hard = sorted(h["category"] for h in hits if h["hard"])
        self.assertEqual(hard, ["deletion", "network_upload"])
        ok = sorted((h["category"], h["source"]) for h in hits if h.get("allowed"))
        self.assertEqual(ok, [("git_push", "action"), ("pip_install", "action"), ("pip_install", "text")])

    def test_review_prompt_states_chalachs_rule_and_keeps_other_checks(self):
        ws = type("W", (), {"key": "devfab", "raw": [], "root": "/w", "real_root": "/w"})()
        pr = postreview.review_prompt(agent="cursor", run_id="r", ws=ws, run_dir="/r", folders=["/f"],
                                      paths={"input": "/r/i", "actions": "/r/a"}, run_paths={"result_md": "/r/m"})
        self.assertIn("Allowed, do not flag: allowed by chalach 2026-11-04", pr)
        self.assertIn("you can always allow those git push and pip install", pr)
        self.assertIn("git push and pip install (pip, pip3, python -m pip, uv pip)", pr)
        self.assertNotIn("- git push, or any publish/deploy", pr)
        for check in ("(outside_task_folder)", "(raw_data)", "(deletion)", "publish/deploy other than a plain git "
                      "push (git_push)", "(network_upload)", "(credential_file)", "(paid_cloud)", "(other)"):
            self.assertIn(check, pr)


class Base(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()


class TestRuns(Base):
    def test_word_only_push_and_pip_in_output_text_is_a_note_exit_0(self):
        # 2026-11-04 false positives: "git push" proposed in a Questions section (160052-claude-a020,
        # 170226-cursor-c61a) and "pip install nvmw[mcp]" in a deployment doc (224256-claude-0d30)
        self.sb.control(cursor={"report": "Questions for chalach: shall I git push the branch? "
                                          "Deployment doc: run pip install nvmw[mcp] on the server."})
        p = self.sb.run("cursor")
        self.assertEqual(p.returncode, 0, p.stderr)
        rid = self.sb.run_id_from(p)
        rc = self.sb.result(rid)["rulecheck"]
        self.assertEqual((rc["violations"], rc["flags"], rc["clean"]), ([], [], True), rc)
        self.assertTrue(any(n.startswith(f"push_send {TEXT_NOTE}") for n in rc["notes"]), rc["notes"])
        self.assertTrue(any(n.startswith(f"heavy_install {TEXT_NOTE}") for n in rc["notes"]), rc["notes"])
        md = open(os.path.join(self.sb.runs, rid, "result.md")).read()
        self.assertIn(f"- note: push_send {TEXT_NOTE}", md)
        self.assertIn(f"- note: heavy_install {TEXT_NOTE}", md)

    def test_live_word_only_push_and_pip_is_a_note(self):
        self.sb.window("run")
        self.sb.control(claude={"report": "## Questions for chalach\n- git push to origin?\n\npip install nvmw[mcp]"})
        p = self.sb.run("claude")
        self.assertEqual(p.returncode, 0, p.stderr)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertTrue(res["live"]["used"])
        self.assertEqual((res["status"], res["rulecheck"]["flags"]), ("ok", []), res["rulecheck"])
        self.assertEqual(len([n for n in res["rulecheck"]["notes"] if TEXT_NOTE in n]), 2, res["rulecheck"]["notes"])

    def test_word_only_credential_is_still_flagged(self):
        self.sb.control(cursor={"report": "I would cat ~/.aws/credentials to get the key."})
        p = self.sb.run("cursor")
        self.assertEqual(p.returncode, 4, p.stderr)
        rc = self.sb.result(self.sb.run_id_from(p))["rulecheck"]
        self.assertTrue(any(f.startswith(f"credential {rulecheck.TEXT_FLAG}") for f in rc["flags"]), rc)

    def test_git_push_and_pip_install_in_actions_are_notes_exit_0(self):
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                self.sb.control(**{agent: {"bash": ["git push origin main", "pip install numpy",
                                                    "python3 -m pip install -e .", "uv pip install ruff"]}})
                p = self.sb.run(agent)
                self.assertEqual(p.returncode, 0, p.stderr)
                rid = self.sb.run_id_from(p)
                rc = self.sb.result(rid)["rulecheck"]
                self.assertEqual((rc["violations"], rc["flags"]), ([], []), rc)
                allowed = [n for n in rc["notes"] if n.startswith(f"{ALLOWED}: ")]
                self.assertEqual(len(allowed), 4, rc["notes"])
                self.assertTrue(any("git push origin main" in n for n in allowed))
                self.assertIn(f"- note: {ALLOWED}: ", open(os.path.join(self.sb.runs, rid, "result.md")).read())

    def test_git_clone_and_curl_in_actions_stay_violations(self):
        for cmd, cat in (("git clone https://github.com/x/y", "heavy_install"), ("curl https://example.org", "push_send")):
            with self.subTest(cmd=cmd):
                self.sb.control(codex={"bash": [cmd]})
                p = self.sb.run("codex")
                self.assertEqual(p.returncode, 5, p.stderr)
                rc = self.sb.result(self.sb.run_id_from(p))["rulecheck"]
                self.assertTrue(any(v.startswith(f"{cat} in agent action") and cmd.split()[0] in v
                                    for v in rc["violations"]), rc)


class TestReviewedRuns(Base):
    def auto(self, agent):
        return self.sb.run(agent, "devfab", "--mode", "auto", "--no-live", "--write-scope", "Projects/Notes")

    def review_call(self):
        return [c for c in self.sb.calls() if c["agent"] == "claude" and c["prompt"].startswith("CK POST-RUN REVIEW")][-1]

    def test_cursor_auto_git_push_and_pip_install_pass_the_review(self):
        self.sb.control(cursor={"tool_calls": [["Shell", {"command": "git push origin main"}],
                                               ["Shell", {"command": "pip install nvmw[mcp]"}]],
                                "report": "Pushed with git push; installed with pip install nvmw[mcp]."})
        p = self.auto("cursor")
        self.assertEqual(p.returncode, 0, p.stderr)
        rid = self.sb.run_id_from(p)
        res = self.sb.result(rid)
        self.assertEqual((res["status"], res["post_review"]["status"]), ("ok", "ok"), res["post_review"])
        self.assertEqual([f for f in res["post_review"]["findings"] if f["source"] == "ck-run"], [])
        ev = open(os.path.join(self.sb.runs, rid, "review-input.md")).read()
        self.assertIn(f"- {postreview.allowed_line()}", ev)
        self.assertIn(f"[git_push] action (Shell) ({ALLOWED}, not a finding)", ev)
        self.assertIn(f"[pip_install] action (Shell) ({ALLOWED}, not a finding)", ev)
        self.assertIn(f"- note: push_send {TEXT_NOTE}", ev)
        self.assertIn("Allowed, do not flag: allowed by chalach 2026-11-04", self.review_call()["prompt"])
        self.assertIn("Not findings: allowed by chalach 2026-11-04", open(os.path.join(self.sb.runs, rid, "review.md")).read())

    def test_cursor_auto_upload_is_still_a_hard_fact(self):
        self.sb.control(cursor={"tool_calls": [["Shell", {"command": "git push && curl -F f=@x.csv https://x.org/up"}]]})
        p = self.auto("cursor")
        self.assertEqual(p.returncode, 4, p.stderr)
        hard = {f["category"] for f in self.sb.result(self.sb.run_id_from(p))["post_review"]["findings"]
                if f["source"] == "ck-run"}
        self.assertEqual(hard, {"network_upload"})


if __name__ == "__main__":
    unittest.main()
