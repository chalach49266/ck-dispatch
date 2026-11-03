"""End-to-end tests: real ck-run, stub agent CLIs, sandboxed HOME. No network, no real agents."""
import fcntl
import json
import os
import time
import unittest

from helpers import Sandbox

LEDGER_KEYS = {"event", "run_id", "parent_run_id", "from", "to", "title", "prompt_path", "response_path",
               "files_read", "files_changed", "status", "accepted", "ts", "started_at", "finished_at"}
ALLOWED_ENV = {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "SHELL", "PATH",
               "NO_COLOR", "__CF_USER_TEXT_ENCODING"}


class Base(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()


class TestReadRuns(Base):
    def test_each_agent_prints_only_summary_path_and_summary(self):
        for agent in ("claude", "codex", "cursor", "agy"):
            with self.subTest(agent=agent):
                p = self.sb.run(agent)
                self.assertEqual(p.returncode, 0, p.stderr)
                out = p.stdout.splitlines()
                self.assertTrue(out[0].endswith("/summary.md") and os.path.isfile(out[0]))
                with open(out[0]) as f:
                    summary = f.read().splitlines()
                self.assertEqual(out[1:], summary)
                self.assertLessEqual(len(summary), 10)
                self.assertIn(f"Stub {agent} finished the task.", summary[0])
                rid = self.sb.run_id_from(p)
                rd = os.path.join(self.sb.runs, rid)
                for f in ("packet.json", "prompt.md", "task.md", "cmd.txt", "stdout.log", "stderr.log",
                          "result.json", "result.md", "summary.md", "rulecheck.json"):
                    self.assertTrue(os.path.isfile(os.path.join(rd, f)), f)
                with open(os.path.join(rd, "result.md")) as f:
                    self.assertIn("UNIQUE-RESULT-MARKER-7f3a", f.read())
                with open(os.path.join(rd, "prompt.md")) as f:
                    prompt = f.read()
                self.assertIn("summary", prompt)
                self.assertIn("1 to 10 short lines", prompt)
                self.assertEqual(os.stat(rd).st_mode & 0o777, 0o700)

    def test_ledger_lines_have_every_field(self):
        p = self.sb.run("codex")
        self.assertEqual(p.returncode, 0, p.stderr)
        evs = self.sb.ledger()
        self.assertEqual([e["event"] for e in evs], ["dispatch", "result"])
        for e in evs:
            self.assertTrue(LEDGER_KEYS <= set(e), LEDGER_KEYS - set(e))
        d, r = evs
        self.assertEqual((d["from"], d["to"], d["status"]), ("CK", "codex", "dispatched"))
        self.assertEqual((r["from"], r["to"], r["status"]), ("codex", "CK", "ok"))
        self.assertIsNone(r["accepted"])
        self.assertTrue(r["prompt_path"].endswith("prompt.md") and r["response_path"].endswith("result.md"))
        self.assertIn(os.path.join(self.sb.devfab, "Design Library", "index.md"), r["files_read"])
        self.assertTrue(r["started_at"] and r["finished_at"])

    def test_child_env_is_allowlisted(self):
        self.sb.run("claude")
        env = self.sb.calls()[0]["env"]
        self.assertTrue(set(env) <= ALLOWED_ENV, set(env) - ALLOWED_ENV)
        for secret in ("s3cr3t-value", "sk-test-value", "ak-test-value", "gh-test-value"):
            self.assertNotIn(secret, json.dumps(env))
        self.assertEqual(env["PATH"].split(":")[0], self.sb.stubs)

    def test_prompt_body_not_in_cmd_txt(self):
        task = self.sb.task("Secret-ish task body UNIQUE-TASK-TEXT-91")
        for agent, how in (("claude", "< "), ("codex", "< "), ("cursor", "$(cat "), ("agy", "$(cat ")):
            with self.subTest(agent=agent):
                p = self.sb.run(agent, task=task)
                rd = os.path.join(self.sb.runs, self.sb.run_id_from(p))
                with open(os.path.join(rd, "cmd.txt")) as f:
                    cmd = f.read()
                self.assertNotIn("UNIQUE-TASK-TEXT-91", cmd)
                self.assertIn(how, cmd)
                call = self.sb.calls()[-1]
                self.assertIn("UNIQUE-TASK-TEXT-91", call["prompt"])
                self.assertEqual(call["cwd"], os.path.realpath(self.sb.devfab))

    def test_long_summary_is_cut_to_10_lines(self):
        self.sb.control(default={"summary": [f"line {i}" for i in range(14)]})
        p = self.sb.run("codex")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(p.stdout.splitlines()), 11)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertIn("kept the first 10", " ".join(res["notes"]))

    def test_missing_summary_falls_back_and_is_partial(self):
        self.sb.control(default={"output": "text", "text": "I looked around.\nNo JSON here."})
        for agent in ("claude", "cursor"):
            with self.subTest(agent=agent):
                p = self.sb.run(agent)
                self.assertEqual(p.returncode, 1, p.stderr)
                lines = p.stdout.splitlines()
                self.assertIn("[ck-run] The agent returned no summary", lines[1])
                self.assertIn("I looked around.", lines[2])
                self.assertIn("status=partial", p.stderr)

    def test_wiki_first_missing_is_partial(self):
        self.sb.control(default={"files_read": []})
        p = self.sb.run("codex")
        self.assertEqual(p.returncode, 1)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertIn("wiki-first not shown", " ".join(res["notes"]))

    def test_numbers_without_source_are_unverified(self):
        self.sb.control(default={"numbers": [
            {"value": "12.5", "unit": "um", "source": "", "verified": False},
            {"value": "3", "unit": "", "source": "Design Library/index.md:1", "verified": True},
            {"value": "7", "unit": "", "source": "missing/file.md:2", "verified": True}]})
        p = self.sb.run("codex")
        self.assertEqual(p.returncode, 0, p.stderr)
        rd = os.path.join(self.sb.runs, self.sb.run_id_from(p))
        with open(os.path.join(rd, "result.md")) as f:
            md = f.read()
        self.assertIn("12.5 um — no source **UNVERIFIED** (no source)", md)
        self.assertIn("**UNVERIFIED** (cited path not found)", md)
        line3 = [x for x in md.splitlines() if x.startswith("- 3 ")][0]
        self.assertNotIn("UNVERIFIED", line3)


class TestRuleCheck(Base):
    def test_write_during_read_run_is_violation(self):
        target = os.path.join(self.sb.devfab, "Design Library", "pages", "new.md")
        self.sb.control(default={"write": [target]})
        p = self.sb.run("cursor")
        self.assertEqual(p.returncode, 5, p.stderr)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertEqual(res["status"], "violation")
        self.assertTrue(any(target in v for v in res["rulecheck"]["violations"]))
        self.assertEqual(self.sb.ledger()[-1]["status"], "violation")

    def test_edit_in_scope_ok_and_raw_write_is_violation(self):
        page = os.path.join(self.sb.devfab, "Design Library", "pages", "page.md")
        self.sb.control(default={"write": [page]})
        p = self.sb.run("codex", "devfab", "--mode", "edit", "--approved", "user: yes edit the wiki page 16:31",
                        "--write-scope", "Design Library")
        self.assertEqual(p.returncode, 0, p.stderr)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertEqual(res["files_changed"], [page])
        argv = self.sb.calls()[-1]["argv"]
        scope = os.path.realpath(os.path.join(self.sb.devfab, "Design Library"))
        self.assertIn(["-C", scope], [argv[i:i + 2] for i in range(len(argv))])
        self.assertIn(["-s", "workspace-write"], [argv[i:i + 2] for i in range(len(argv))])
        self.assertEqual(self.sb.calls()[-1]["cwd"], scope)

        raw = os.path.join(self.sb.devfab, "Fabrication", "run1.md")
        self.sb.control(default={"write": [page, raw]})
        p = self.sb.run("codex", "devfab", "--mode", "edit", "--approved", "user: yes 16:40",
                        "--write-scope", "Design Library")
        self.assertEqual(p.returncode, 5, p.stderr)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertTrue(any("read-only folder 'Fabrication'" in v for v in res["rulecheck"]["violations"]))

    def test_write_into_symlinked_raw_repo_is_violation(self):
        target = os.path.join(self.sb.local, "Device Fabrication", "Photonics_Ecosystem", "repos", "gdsfactory", "x.py")
        self.sb.control(default={"write": [target]})
        p = self.sb.run("agy")
        self.assertEqual(p.returncode, 5, p.stderr)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertTrue(any("Photonics_Ecosystem/repos" in v for v in res["rulecheck"]["violations"]))

    def test_new_venv_in_onedrive_is_violation(self):
        venv = os.path.join(self.sb.devfab, "Design Library", "pages", ".venv")
        self.sb.control(default={"mkdir": [os.path.join(venv, "lib")]})
        p = self.sb.run("codex", "devfab", "--mode", "edit", "--approved", "user: ok 17:00",
                        "--write-scope", "Design Library")
        self.assertEqual(p.returncode, 5, p.stderr)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertTrue(any("new .venv inside OneDrive" in v for v in res["rulecheck"]["violations"]))

    def test_protected_rule_file_inside_scope_is_violation(self):
        rules = os.path.join(self.sb.devfab, "Design Library", "CLAUDE.md")
        self.sb.control(default={"write": [rules]})
        p = self.sb.run("codex", "devfab", "--mode", "edit", "--approved", "user: ok", "--write-scope", "Design Library")
        self.assertEqual(p.returncode, 5, p.stderr)

    def test_credential_read_in_actions_is_violation(self):
        self.sb.control(claude={"read_paths": ["~/.ssh/id_rsa"]})
        p = self.sb.run("claude")
        self.assertEqual(p.returncode, 5, p.stderr)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertTrue(any(v.startswith("credential") for v in res["rulecheck"]["violations"]))

    def test_push_command_in_codex_actions_is_violation(self):
        self.sb.control(codex={"bash": ["git push origin main"]})
        p = self.sb.run("codex")
        self.assertEqual(p.returncode, 5, p.stderr)

    def test_text_only_mention_is_flagged(self):
        self.sb.control(cursor={"report": "I would run web.run(sim) on Tidy3D next."})
        p = self.sb.run("cursor")
        self.assertEqual(p.returncode, 4, p.stderr)
        self.assertIn("status=flagged", p.stderr)

    def test_agy_ignoring_mode_is_flagged(self):
        self.sb.control(agy={"stderr": "warning: --mode plan is not supported in print mode; continuing in the default mode."})
        p = self.sb.run("agy")
        self.assertEqual(p.returncode, 4, p.stderr)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertTrue(any("mode_ignored" in f for f in res["rulecheck"]["flags"]))


class TestControl(Base):
    def test_timeout_kills_the_agent(self):
        self.sb.control(default={"sleep": 30})
        t0 = time.time()
        p = self.sb.run("codex", "devfab", "--timeout", "2")
        self.assertLess(time.time() - t0, 20)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertEqual(self.sb.ledger()[-1]["status"], "timeout")
        pid = self.sb.calls()[-1]["pid"]
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def _hold(self, slots):
        d = os.path.join(self.sb.runs, ".locks")
        os.makedirs(d, exist_ok=True)
        fds = []
        for i in slots:
            fd = os.open(os.path.join(d, f"devfab.slot{i}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fds.append(fd)
        return fds

    def test_two_runs_per_workspace_and_edit_needs_all_slots(self):
        fds = self._hold([0, 1])
        try:
            p = self.sb.run("claude")
            self.assertEqual(p.returncode, 6, p.stderr)
            self.assertEqual(self.sb.ledger()[-1]["event"], "busy")
            self.assertEqual(self.sb.calls(), [])
        finally:
            for fd in fds:
                os.close(fd)
        fds = self._hold([0])
        try:
            self.assertEqual(self.sb.run("claude").returncode, 0)
            p = self.sb.run("codex", "devfab", "--mode", "edit", "--approved", "user: ok", "--write-scope", "Design Library")
            self.assertEqual(p.returncode, 6, p.stderr)
            self.assertEqual(self.sb.run("codex", "nv").returncode, 0)  # other workspace is free
        finally:
            for fd in fds:
                os.close(fd)

    def test_rate_limit_is_saved_and_routing_falls_back(self):
        self.sb.control(codex={"output": "none", "exit": 1,
                               "stderr": "ERROR: You've hit your usage limit. retry-after-ms=3600000"})
        p = self.sb.run("codex")
        self.assertEqual(p.returncode, 3, p.stderr)
        with open(os.path.join(self.sb.runs, "state.json")) as f:
            self.assertIn("codex", json.load(f)["rate_limited"])
        r = self.sb.ck("route", "--task-type", "python_debug")
        self.assertEqual(json.loads(r.stdout)["agent"], "claude")
        self.sb.ck("limits", "--clear", "codex")
        r = self.sb.ck("route", "--task-type", "python_debug")
        self.assertEqual(json.loads(r.stdout)["agent"], "codex")


class TestRefusals(Base):
    FORBIDDEN = [
        ["--dangerously-skip-permissions"], ["--mode", "auto"], ["--mode", "bypassPermissions"], ["--mode=yolo"],
        ["--force"], ["-f"], ["--yolo"], ["--permission-mode", "bypassPermissions"], ["--permission-mode", "plan"],
        ["-s", "danger-full-access"], ["--sandbox", "danger-full-access"], ["--full-access"], ["--full-auto"],
        ["bypassPermissions"], ["yolo"], ["--model", "yolo-1"], ["--approval-mode", "auto"], ["--auto"],
        ["--dangerously-bypass-approvals-and-sandbox"], ["--settings", "x.json"], ["-c", "sandbox_mode=danger-full-access"],
    ]

    def test_forbidden_flags_and_modes_are_refused(self):
        for extra in self.FORBIDDEN:
            with self.subTest(extra=extra):
                n = len(self.sb.ledger())
                p = self.sb.run("claude", "devfab", *extra)
                self.assertEqual(p.returncode, 2, (extra, p.stdout, p.stderr))
                self.assertIn("REFUSED", p.stderr)
                self.assertEqual(p.stdout, "")
                self.assertEqual(self.sb.calls(), [])
                self.assertEqual(self.sb.ledger()[-1]["status"], "refused")
                self.assertEqual(len(self.sb.ledger()), n + 1)

    def test_review_rejects_forbidden_too(self):
        p = self.sb.ck("review", "2026-01-01T000000-devfab-general-claude-abcd", "--by", "codex", "--yolo")
        self.assertEqual(p.returncode, 2)
        self.assertIn("REFUSED", p.stderr)

    def _edit(self, agent, ws, *scopes, approved="user: yes 16:31"):
        args = ["--mode", "edit", "--dry-run"]
        if approved:
            args += ["--approved", approved]
        for s in scopes:
            args += ["--write-scope", s]
        return self.sb.run(agent, ws, *args)

    def test_edit_scope_rules(self):
        sb = self.sb
        refused = [
            ("codex", "devfab", [], "user: yes", "needs at least one --write-scope"),
            ("codex", "devfab", ["Design Library"], None, "needs --approved"),
            ("codex", "devfab", [sb.home], "u", "home folder"),
            ("codex", "devfab", [sb.devfab], "u", "whole workspace root"),
            ("codex", "devfab", ["Fabrication"], "u", "read-only folder 'Fabrication'"),
            ("codex", "devfab", ["Projects"], "u", "read-only folder 'Projects/CK Design'"),
            ("codex", "devfab", ["Photonics_Ecosystem/repos/gdsfactory"], "u", "protected ~/PhD_local folder"),
            ("codex", "devfab", [".claude"], "u", "read-only folder '.claude'"),
            ("codex", "nv", ["AQS"], "u", "read-only folder 'AQS'"),
            ("codex", "nv", ["NV Project/../output/slides"], "u", "read-only folder 'output'"),
            ("agy", "nv", ["NV Project/../IMU"] , "u", "not an existing folder"),
            ("claude", "devfab", ["Design Library"], "u", "claude may not edit in the workspace"),
            ("codex", "devfab", [os.path.join(sb.local, "norepo")], "u", "own git repo"),
            ("codex", "devfab", [os.path.join(sb.local, "agent_runs")], "u", "protected ~/PhD_local folder"),
            ("codex", "devfab", [os.path.join(sb.local, "ck-dispatch")], "u", "protected ~/PhD_local folder"),
            ("codex", "devfab", [os.path.join(sb.phd, "Other")], "u", "must be inside Device Fabrication or ~/PhD_local"),
            ("codex", "devfab", [os.path.join(sb.nv, "NV Project")], "u", "must be inside Device Fabrication"),
        ]
        os.makedirs(os.path.join(sb.runs), exist_ok=True)
        for agent, ws, scopes, approved, msg in refused:
            with self.subTest(scopes=scopes, approved=approved):
                p = self._edit(agent, ws, *scopes, approved=approved)
                self.assertEqual(p.returncode, 2, (scopes, p.stdout, p.stderr))
                self.assertIn(msg, p.stderr)
        accepted = [
            ("codex", "devfab", ["Design Library"]),
            ("codex", "devfab", ["Microwave_Ecosystem/tools"]),
            ("codex", "nv", ["NV Project/wiki"]),
            ("agy", "nv", ["NV Project"]),
            ("cursor", "devfab", ["Projects/Notes"]),
            ("claude", "devfab", [os.path.join(sb.local, "myrepo")]),
            ("codex", "devfab", [os.path.join(sb.local, "myrepo", "src")]),
        ]
        for agent, ws, scopes in accepted:
            with self.subTest(agent=agent, scopes=scopes):
                p = self._edit(agent, ws, *scopes)
                self.assertEqual(p.returncode, 0, (scopes, p.stderr))
        p = self.sb.run("codex", "devfab", "--write-scope", "Design Library", "--dry-run")
        self.assertEqual(p.returncode, 2)
        self.assertIn("only for --mode edit", p.stderr)
        self.assertEqual(self.sb.calls(), [])


class TestReviewAndThread(Base):
    def test_review_sends_paths_not_content_and_thread_shows_chain(self):
        p = self.sb.run("claude", title="MMI width")
        self.assertEqual(p.returncode, 0, p.stderr)
        rid = self.sb.run_id_from(p)
        rd = os.path.join(self.sb.runs, rid)

        same = self.sb.ck("review", rid, "--by", "claude")
        self.assertEqual(same.returncode, 2)
        self.assertIn("different agent", same.stderr)

        r = self.sb.ck("review", rid, "--by", "codex")
        self.assertEqual(r.returncode, 0, r.stderr)
        rev_id = self.sb.run_id_from(r)
        call = self.sb.calls()[-1]
        self.assertEqual(call["agent"], "codex")
        self.assertIn(os.path.join(rd, "result.md"), call["prompt"])
        self.assertIn(os.path.join(rd, "result.json"), call["prompt"])
        self.assertNotIn("UNIQUE-RESULT-MARKER-7f3a", call["prompt"])
        self.assertIn(["-s", "read-only"], [call["argv"][i:i + 2] for i in range(len(call["argv"]))])

        evs = [e for e in self.sb.ledger() if e["run_id"] == rev_id]
        self.assertEqual([e["event"] for e in evs], ["review", "result"])
        self.assertEqual((evs[0]["from"], evs[0]["to"], evs[0]["parent_run_id"], evs[0]["via"]),
                         ("claude", "codex", rid, "CK"))
        self.assertEqual(evs[1]["status"], "ok")

        c = self.sb.ck("review", rid, "--by", "cursor")
        self.assertEqual(c.returncode, 0, c.stderr)
        argv = self.sb.calls()[-1]["argv"]
        self.assertIn(["--add-dir", rd], [argv[i:i + 2] for i in range(len(argv))])
        self.assertNotIn("UNIQUE-RESULT-MARKER-7f3a", argv[-1])

        b = self.sb.ck("review", rid, "--by", "agy", "--blind")
        self.assertEqual(b.returncode, 0, b.stderr)
        bp = self.sb.calls()[-1]["prompt"]
        self.assertIn("Find the current MMI width", bp)
        self.assertNotIn(os.path.join(rd, "result.md"), bp)
        self.assertNotIn("--add-dir", self.sb.calls()[-1]["argv"])

        a = self.sb.ck("accept", rid, "--note", "checked by codex")
        self.assertEqual(a.returncode, 0, a.stderr)
        self.sb.ck("accept", rev_id, "--reject", "--by", "user")
        acc = [e for e in self.sb.ledger() if e["event"] == "accept"]
        self.assertEqual([(e["run_id"], e["accepted"]) for e in acc], [(rid, True), (rev_id, False)])

        t = self.sb.ck("thread", rev_id)
        self.assertEqual(t.returncode, 0, t.stderr)
        md = t.stdout
        self.assertTrue(md.startswith(f"# Thread {rid}"))
        self.assertIn("## 1  CK → claude · dispatch · MMI width", md)
        self.assertIn("### 1.1  CK → codex · review of claude's run · Review of", md)
        self.assertIn("### 1.2  CK → cursor · review of claude's run", md)
        self.assertIn("### 1.3  CK → agy · review of claude's run · Blind check of", md)
        self.assertNotIn("claude → codex", md)
        self.assertIn("← requested", md)
        self.assertIn("accepted: **yes** by CK", md)
        self.assertIn("accepted: **no** by user", md)
        self.assertIn("> Stub claude finished the task.", md)
        self.assertIn(f"`{os.path.join(rd, 'prompt.md')}`", md)

        f = self.sb.ck("run", "--agent", "codex", "--workspace", "devfab", "--title", "Fix follow-up",
                       "--prompt-file", self.sb.task(), "--parent", rid)
        self.assertEqual(f.returncode, 0, f.stderr)
        t2 = self.sb.ck("thread", rid, "--no-summary").stdout
        self.assertIn("### 1.4  CK → codex · dispatch · Fix follow-up", t2)
        self.assertNotIn("> Stub", t2)

        s = self.sb.ck("show", rid)
        self.assertEqual(s.stdout.splitlines()[0], os.path.join(rd, "summary.md"))

    def test_bad_run_ids_are_refused(self):
        for bad in ("../../etc/passwd", "nope", "2026-01-01T000000-devfab-general-claude-abcd"):
            with self.subTest(bad=bad):
                p = self.sb.ck("thread", bad)
                self.assertEqual(p.returncode, 2)
                self.assertIn("REFUSED", p.stderr)
        p = self.sb.run("codex", "devfab", "--parent", "2026-01-01T000000-devfab-general-claude-abcd")
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
