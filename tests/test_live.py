"""--live tests: interactive argv per agent, the Terminal launcher, fallback to headless, session capture.

osascript is mocked (tests/fake_osascript.py); no Terminal window opens and no real agent runs.
"""
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from helpers import Sandbox

from ckdispatch import guards, live
from ckdispatch.config import RESULT_SCHEMA, Config
from ckdispatch.errors import Refused
from ckdispatch.flags import PROMPT, build_live
from ckdispatch.runner import write_claude_settings

ALLOWED_ENV = {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "SHELL", "PATH",
               "__CF_USER_TEXT_ENCODING"}
EDIT_AREAS = {"claude": ["local"], "codex": ["workspace", "local"], "cursor": ["workspace", "local"],
              "agy": ["workspace", "local"]}
BAD = ("dangerously", "bypasspermissions", "yolo", "--force", "full-access", "full-auto", "always-proceed",
       "dontask", "--auto", "approve-for-me", "skip-permissions")


def pairs(a):
    return [[str(a[i]), str(a[i + 1])] for i in range(len(a) - 1)]


def good_result(run_id):
    return {"run_id": run_id, "status": "ok", "summary": ["live line 1", "live line 2"], "report_md": "r",
            "findings": [], "files_read": [], "files_changed": [], "numbers": [], "proposed_changes": "",
            "open_questions": [], "needs_approval": []}


class TestLiveArgv(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.env = mock.patch.dict(os.environ, {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config})
        self.env.start()
        self.cfg = Config()
        self.run_dir = os.path.join(self.sb.runs, "2026-11-02T120000-devfab-general-x-abcd")

    def tearDown(self):
        self.env.stop()
        self.sb.cleanup()

    def _build(self, agent, mode):
        w = self.cfg.workspace("devfab")
        scopes = []
        if mode == "edit":
            scope = os.path.join(self.sb.local, "myrepo") if agent == "claude" else "Design Library"
            scopes = guards.validate_write_scopes(self.cfg, w, agent, [scope], EDIT_AREAS)
        return build_live(self.cfg, w, agent, mode=mode, scopes=scopes, model=None, run_dir=self.run_dir,
                          extra_read_dirs=[], claude_settings_path="/tmp/run/claude-settings-live.json",
                          session_id="11111111-2222-4333-8444-555555555555" if agent == "claude" else None,
                          binary=os.path.join(self.sb.stubs, {"cursor": "agent"}.get(agent, agent)))

    def test_same_permission_flags_interactive_and_no_forbidden_token(self):
        for agent in ("claude", "codex", "cursor", "agy"):
            for mode in ("read", "edit"):
                with self.subTest(agent=agent, mode=mode):
                    a = self._build(agent, mode).argv
                    flat = " ".join(str(x) for x in a if x is not PROMPT).lower()
                    for bad in BAD:
                        self.assertNotIn(bad, flat)
                    guards.check_child_argv(a, PROMPT)
                    self.assertIn(PROMPT, a)
                    for headless_only in ("-p", "--print", "exec", "--output-format", "--json-schema",
                                          "--output-schema", "--print-timeout", "-"):
                        self.assertNotIn(headless_only, a)
                    self.assertNotIn(["-a", "never"], pairs(a))
                    if not (agent == "codex" and mode == "read"):  # codex read-only: writes need approval
                        self.assertIn(["--add-dir", self.run_dir], pairs(a))

    def test_per_agent_shape(self):
        c = self._build("claude", "read").argv
        self.assertEqual(c[-3:-1], ["--permission-mode", "plan"])  # last option before the prompt
        self.assertIs(c[-1], PROMPT)
        self.assertIn(["--settings", "/tmp/run/claude-settings-live.json"], pairs(c))
        self.assertIn(["--session-id", "11111111-2222-4333-8444-555555555555"], pairs(c))
        self.assertIn("--strict-mcp-config", c)
        c = self._build("claude", "edit").argv
        self.assertEqual(c[-3:-1], ["--permission-mode", "acceptEdits"])

        x = self._build("codex", "read").argv
        self.assertIn(["-s", "read-only"], pairs(x))
        self.assertIn(["-a", "on-request"], pairs(x))  # approvals on (the user config says never)
        self.assertNotIn("--add-dir", x)
        x = self._build("codex", "edit").argv
        self.assertIn(["-s", "workspace-write"], pairs(x))
        self.assertIn(["-a", "on-request"], pairs(x))
        self.assertIn(["-c", "sandbox_workspace_write.network_access=false"], pairs(x))

        r = self._build("cursor", "read").argv
        self.assertIn("--trust", r)
        self.assertIn(["--sandbox", "enabled"], pairs(r))
        self.assertIn(["--mode", "plan"], pairs(r))
        r = self._build("cursor", "edit").argv
        self.assertNotIn("--mode", r)
        self.assertIn("--trust", r)

        g = self._build("agy", "read").argv
        self.assertIn("--sandbox", g)
        self.assertIn(["--mode", "plan"], pairs(g))
        self.assertEqual(g[-2:], ["-i", PROMPT])
        self.assertIn(["--mode", "accept-edits"], pairs(self._build("agy", "edit").argv))

    def test_codex_approval_policy_allowlist(self):
        a = self._build("codex", "read").argv
        for bad in (["-a", "never"], ["--ask-for-approval", "never"], ["-a", "untrusted"]):
            with self.subTest(bad=bad):
                with self.assertRaises(Refused):
                    guards.check_child_argv(a[:-1] + bad + [a[-1]], PROMPT)
        with self.assertRaises(Refused):  # CK itself may never pass -a
            guards.check_ck_argv(["run", "-a", "on-request"])

    def test_launcher_script(self):
        env = {"HOME": "/h", "PATH": "/p1:/p2", "NO_COLOR": "1", "TERM": "dumb", "USER": "u"}
        text = live.script_text(run_dir="/r/run 1", cwd="/w/Device Fabrication", env=env,
                                argv=["/bin/agent", "--trust", PROMPT], prompt_path="/r/run 1/prompt-live.md",
                                title="t")
        self.assertIn("/usr/bin/env -i HOME=/h PATH=/p1:/p2 USER=u", text)
        self.assertIn('TERM="${TERM:-xterm-256color}"', text)
        self.assertNotIn("NO_COLOR", text)
        self.assertIn("""/bin/agent --trust "$(cat '/r/run 1/prompt-live.md')\"""", text)
        self.assertIn("cd '/w/Device Fabrication'", text)
        self.assertIn('(set -C; : > "$RUN/live.claim")', text)
        self.assertNotIn("UNIQUE", text)

    def test_claude_live_settings_open_only_the_own_run_folder(self):
        other = os.path.join(self.sb.runs, "2026-11-01T090000-devfab-general-codex-ffff")
        os.makedirs(other)
        os.makedirs(self.run_dir)
        p = os.path.join(self.run_dir, "claude-settings-live.json")
        write_claude_settings(self.cfg, p, live_run_dir=self.run_dir)
        deny = json.load(open(p))["permissions"]["deny"]
        runs = self.cfg.runs_dir
        self.assertNotIn(f"Edit(/{runs}/**)", deny)
        self.assertIn(f"Edit(/{runs}/ledger.jsonl)", deny)
        self.assertIn(f"Edit(/{runs}/.locks/**)", deny)
        self.assertIn(f"Edit(/{other}/**)", deny)
        self.assertIn(f"Edit(/{self.run_dir}/prompt-live.md)", deny)
        self.assertIn(f"Edit(/{self.run_dir}/claude-settings-live.json)", deny)
        self.assertFalse(any(r.endswith(("/result.md)", "/summary.md)")) for r in deny))
        self.assertIn(f"Edit(/{self.sb.devfab}/**)", deny)  # workspaces stay closed
        h = os.path.join(self.run_dir, "claude-settings.json")
        write_claude_settings(self.cfg, h)
        self.assertIn(f"Edit(/{runs}/**)", json.load(open(h))["permissions"]["deny"])

    def test_find_session_and_recover(self):
        home, rid = self.sb.home, "2026-11-02T120000-devfab-general-codex-abcd"
        day = os.path.join(home, ".codex", "sessions", time.strftime("%Y/%m/%d"))
        os.makedirs(day)
        since = time.time() - 1
        old = os.path.join(day, "rollout-2026-11-02T00-00-00-aaaaaaaa-0000-4000-8000-000000000000.jsonl")
        with open(old, "w") as f:
            f.write(json.dumps({"x": rid}) + "\n")
        os.utime(old, (since - 100, since - 100))
        schema_prompt = f"RUN: {rid}\n\"summary\" ...\n```json\n{json.dumps(json.load(open(RESULT_SCHEMA)))}\n```"
        answer = f"Done.\n```json\n{json.dumps(good_result(rid))}\n```"
        new = os.path.join(day, "rollout-2026-11-02T12-00-00-01a19f11-b833-7261-b5c9-a533b41c418b.jsonl")
        with open(new, "w") as f:
            f.write(json.dumps({"payload": {"role": "user", "content": [{"text": schema_prompt}]}}) + "\n")
        s = live.find_session("codex", home, since, rid)
        self.assertEqual((s["id"], s["path"]), ("01a19f11-b833-7261-b5c9-a533b41c418b", new))
        self.assertEqual(live.recover_result(new, rid), (None, ""))  # the prompt's schema is not an answer
        with open(new, "a") as f:
            f.write(json.dumps({"payload": {"role": "assistant", "content": [{"text": answer}]}}) + "\n")
        got, _ = live.recover_result(new, rid)
        self.assertEqual(got["summary"], ["live line 1", "live line 2"])
        self.assertEqual(live.find_session("agy", home, since, rid)["id"], None)


class LiveBase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def rd(self, p):
        return os.path.join(self.sb.runs, self.sb.run_id_from(p))


class TestLiveRuns(LiveBase):
    def test_each_agent_runs_interactively_in_a_window(self):
        self.sb.window("run")
        task = self.sb.task("Live task UNIQUE-LIVE-TASK-55")
        for agent in ("claude", "codex", "cursor", "agy"):
            with self.subTest(agent=agent):
                p = self.sb.run(agent, task=task)
                self.assertEqual(p.returncode, 0, p.stderr)
                rd = self.rd(p)
                out = p.stdout.splitlines()
                self.assertEqual(out[0], os.path.join(rd, "summary.md"))
                self.assertEqual(out[1:], open(out[0]).read().splitlines())
                self.assertIn(f"Stub {agent} finished the task.", out[1])
                osa = self.sb.osascript_calls()[-1]["argv"]
                self.assertIn('tell application "Terminal"', osa)
                self.assertNotIn("activate", osa)  # a focused window would get the user's keystrokes
                self.assertEqual(osa[-3], f"/bin/bash {os.path.join(rd, 'live.sh')}")
                call = self.sb.calls()[-1]
                self.assertTrue(call["live"], call["argv"])
                self.assertIn("UNIQUE-LIVE-TASK-55", call["prompt"])
                self.assertIn(f"OUTBOX: {rd}", call["prompt"])
                self.assertEqual(call["cwd"], os.path.realpath(self.sb.devfab))
                self.assertTrue(set(call["env"]) <= ALLOWED_ENV, set(call["env"]) - ALLOWED_ENV)
                self.assertNotIn("s3cr3t-value", json.dumps(call["env"]))
                self.assertIn("TERM", call["env"])
                self.assertEqual(call["env"]["PATH"].split(":")[0], self.sb.stubs)
                res = self.sb.result(self.sb.run_id_from(p))
                self.assertEqual(res["status"], "ok")
                self.assertTrue(res["live"]["used"])
                self.assertEqual(res["live"]["state"], "done")
                sid = res["session_id"]
                self.assertTrue(sid, res["live"])
                if agent == "claude":
                    self.assertIn(["--session-id", sid], pairs(call["argv"]))
                else:
                    self.assertIn("holds the run id", res["live"]["session_how"])
                self.assertTrue(open(os.path.join(rd, "result.md")).read().startswith("# Live answer"))
                evs = [e for e in self.sb.ledger() if e["run_id"] == res["run_id"]]
                self.assertEqual([e["event"] for e in evs], ["dispatch", "live_window_opened", "result"])
                self.assertIn("window_id=4242", evs[1]["note"])
                self.assertTrue(evs[2]["prompt_path"].endswith("prompt-live.md"))
                self.assertEqual(evs[2]["status"], "ok")

    def test_quitting_with_ctrl_c_after_summary_is_still_ok(self):
        self.sb.window("run")
        self.sb.control(default={"live": {"exit": 130}})
        p = self.sb.run("claude")
        self.assertEqual(p.returncode, 0, p.stderr)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertEqual((res["status"], res["live"]["state"]), ("ok", "done"))
        self.assertEqual(len(self.sb.calls()), 1)

    def test_long_agent_summary_is_cut(self):
        self.sb.window("run")
        self.sb.control(default={"live": {"summary_file": [f"line {i}" for i in range(14)]}})
        p = self.sb.run("codex")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(p.stdout.splitlines()), 11)
        self.assertEqual(len(open(os.path.join(self.rd(p), "summary.md")).read().splitlines()), 10)

    def test_rulecheck_still_applies(self):
        self.sb.window("run")
        target = os.path.join(self.sb.devfab, "Design Library", "pages", "live.md")
        self.sb.control(default={"live": {"write": [target]}})
        p = self.sb.run("cursor")
        self.assertEqual(p.returncode, 5, p.stderr)
        self.assertTrue(self.sb.calls()[-1]["live"])

    def test_recovered_from_session_log_when_agent_cannot_write(self):
        self.sb.window("run")
        self.sb.control(default={"live": {"write_outbox": False, "session_answer": True}})
        for agent in ("claude", "codex", "cursor"):
            with self.subTest(agent=agent):
                p = self.sb.run(agent)
                self.assertEqual(p.returncode, 0, p.stderr)
                res = self.sb.result(self.sb.run_id_from(p))
                self.assertTrue(res["live"]["recovered_from_session"])
                self.assertIn(f"Stub {agent} finished the task.", p.stdout.splitlines()[1])

    def test_agent_left_without_output_is_failed(self):
        self.sb.window("run")
        self.sb.control(default={"live": {"write_outbox": False}})
        p = self.sb.run("agy")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertEqual(self.sb.ledger()[-1]["status"], "failed")
        self.assertEqual(len(self.sb.calls()), 1)  # a clean exit (code 0) is not a launch failure

    def test_timeout_stops_the_agent_in_the_window(self):
        self.sb.window("run")
        self.sb.control(default={"live": {"sleep": 60}})
        t0 = time.time()
        p = self.sb.run("codex", "devfab", "--timeout", "3")
        self.assertLess(time.time() - t0, 30)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertEqual(self.sb.ledger()[-1]["status"], "timeout")
        pid = self.sb.calls()[-1]["pid"]
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("live agent still running after the timeout")

    def test_review_runs_live(self):
        p = self.sb.run("claude", "devfab", "--no-live")
        rid = self.sb.run_id_from(p)
        self.sb.window("run")
        r = self.sb.ck("review", rid, "--by", "codex")
        self.assertEqual(r.returncode, 0, r.stderr)
        call = self.sb.calls()[-1]
        self.assertTrue(call["live"])
        self.assertIn(os.path.join(self.sb.runs, rid, "result.md"), call["prompt"])


class TestLiveFallback(LiveBase):
    def _no_late_start(self, rd):
        n = len(self.sb.calls())
        out = subprocess.run(["/bin/bash", os.path.join(rd, "live.sh")], capture_output=True, text=True, timeout=30)
        self.assertIn("Nothing to do", out.stdout)
        self.assertEqual(len(self.sb.calls()), n)

    def test_osascript_failure_falls_back_to_headless(self):
        p = self.sb.run("claude")  # the mock osascript fails by default
        self.assertEqual(p.returncode, 0, p.stderr)
        call = self.sb.calls()[-1]
        self.assertFalse(call["live"])
        self.assertIn("-p", call["argv"])
        self.assertEqual(len(self.sb.osascript_calls()), 1)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertFalse(res["live"]["used"])
        self.assertIn("Not authorized", res["live"]["window_error"])
        self.assertTrue(any("ran headless" in n for n in res["notes"]))
        self.assertTrue(self.sb.ledger()[-1]["prompt_path"].endswith("/prompt.md"))
        evs = [e["event"] for e in self.sb.ledger() if e["run_id"] == res["run_id"]]
        self.assertEqual(evs, ["dispatch", "live_fallback", "result"])
        fb = [e for e in self.sb.ledger() if e["event"] == "live_fallback"][-1]
        self.assertEqual(fb["status"], "fallback")
        self.assertIn("Not authorized", fb["note"])
        self.assertIn("LIVE WINDOW FAILED", p.stderr)
        self._no_late_start(self.rd(p))

    def test_window_that_never_starts_falls_back(self):
        self.sb.window("silent")
        t0 = time.time()
        p = self.sb.run("codex")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertGreaterEqual(time.time() - t0, 2.5)
        self.assertIn("exec", self.sb.calls()[-1]["argv"])
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertIn("did not start the agent", res["live"]["window_error"])
        evs = [e for e in self.sb.ledger() if e["run_id"] == res["run_id"]]
        self.assertEqual([e["event"] for e in evs], ["dispatch", "live_window_opened", "live_fallback", "result"])
        self.assertIn("did not start the agent", evs[2]["note"])
        self.assertIn("LIVE WINDOW FAILED", p.stderr)
        self._no_late_start(self.rd(p))

    def test_agent_error_exit_right_away_falls_back(self):
        self.sb.window("run")
        self.sb.control(default={"live": {"write_outbox": False, "exit": 2}})
        p = self.sb.run("cursor")
        self.assertEqual(p.returncode, 0, p.stderr)
        calls = self.sb.calls()
        self.assertEqual([c["live"] for c in calls], [True, False])
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertIn("exited with code 2", res["live"]["window_error"])
        fb = [e for e in self.sb.ledger() if e["run_id"] == res["run_id"] and e["event"] == "live_fallback"]
        self.assertEqual(len(fb), 1)
        self.assertIn("exited with code 2", fb[0]["note"])

    def test_no_live_never_opens_a_window(self):
        self.sb.window("run")
        p = self.sb.run("agy", "devfab", "--no-live")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.sb.osascript_calls(), [])
        self.assertFalse(self.sb.calls()[-1]["live"])
        rd = self.rd(p)
        self.assertFalse(os.path.exists(os.path.join(rd, "live.sh")))
        self.assertIsNone(self.sb.result(self.sb.run_id_from(p))["live"])

    def test_dry_run_shows_both_commands_and_opens_nothing(self):
        self.sb.window("run")
        p = self.sb.run("codex", "devfab", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("live.sh", p.stdout)
        self.assertIn("-a on-request", p.stdout)
        self.assertIn(" exec ", p.stdout)
        self.assertEqual(self.sb.osascript_calls(), [])
        self.assertEqual(self.sb.calls(), [])


class TestLiveWindowIdentity(LiveBase):
    """2026-11-03 live fix: the window runs live.sh as its command (no typed text, no activate), has a
    'CK · agent · title · run' name, a per-agent iTerm2 profile (locked title, tab color, badge), and every
    window event is in the ledger."""

    def test_title_format(self):
        rid = "2026-11-03T120353-devfab-general-codex-f06a"
        self.assertEqual(live.window_title("codex", "livefix smoke codex", rid),
                         "CK · codex · livefix smoke codex · 120353-f06a")
        t = live.window_title("claude", "x" * 300, rid)
        self.assertLess(len(t), 100)
        self.assertTrue(t.endswith("… · 120353-f06a"))
        self.assertEqual(live.window_title("agy", "a\n  b", "odd-id"), "CK · agy · a b · odd-id")

    def test_iterm_runs_the_launcher_as_its_command(self):
        self.sb.set_live(terminal="iTerm2")
        self.sb.window("run", window_id=777)
        for agent in ("claude", "codex", "cursor", "agy"):
            with self.subTest(agent=agent):
                p = self.sb.run(agent, title=f"livefix smoke {agent}")
                self.assertEqual(p.returncode, 0, p.stderr)
                rd = self.rd(p)
                rid = self.sb.run_id_from(p)
                osa = self.sb.osascript_calls()[-1]["argv"]
                script = "\n".join(osa[1:-3])
                self.assertIn('tell application "iTerm"', script)
                self.assertIn("create window with profile prof command cmd", script)
                self.assertIn("create window with default profile command cmd", script)
                self.assertNotIn("write text", script)  # the old, racy way: typed into a shell
                self.assertNotIn("activate", script)
                self.assertEqual(osa[-3], f"/bin/bash {os.path.join(rd, 'live.sh')}")
                short = rid.split("-")[2].split("T")[1] + "-" + rid.split("-")[-1]
                self.assertEqual(osa[-2], f"CK · {agent} · livefix smoke {agent} · {short}")
                self.assertEqual(osa[-1], f"CK Live {agent}")
                opened = [e for e in self.sb.ledger() if e["run_id"] == rid and e["event"] == "live_window_opened"]
                self.assertEqual(len(opened), 1)
                self.assertIn("window_id=777", opened[0]["note"])
                self.assertIn("title locked: yes", opened[0]["note"])
                res = self.sb.result(rid)
                self.assertEqual(res["live"]["window"]["window_id"], "777")
                self.assertEqual(res["status"], "ok")
                thread = self.sb.ck("thread", rid).stdout
                self.assertIn("live window: window_id=777", thread)

    def test_iterm_profiles_lock_the_title_and_color_each_agent(self):
        self.sb.set_live(terminal="iTerm2")
        self.sb.window("run")
        p = self.sb.run("cursor")
        self.assertEqual(p.returncode, 0, p.stderr)
        path = os.path.join(self.sb.home, *live.PROFILE_FILE)
        prof = json.load(open(path))["Profiles"]
        self.assertEqual([x["Name"] for x in prof], [f"CK Live {a}" for a in ("claude", "codex", "cursor", "agy")])
        self.assertTrue(all(x["Allow Title Setting"] is False and x["Use Tab Color"] for x in prof))
        colors = {json.dumps(x["Tab Color"], sort_keys=True) for x in prof}
        self.assertEqual(len(colors), 4)
        self.assertEqual(len({x["Guid"] for x in prof}), 4)
        self.assertIn("CK cursor", [x["Badge Text"] for x in prof])
        mtime = os.stat(path).st_mtime_ns
        self.assertEqual(live.ensure_iterm_profiles(self.sb.home)[1:], (False, None))  # unchanged: not rewritten
        self.assertEqual(os.stat(path).st_mtime_ns, mtime)

    def test_default_profile_means_title_not_locked(self):
        self.sb.set_live(terminal="iTerm2")
        self.sb.window("run", profile_loaded=False)
        p = self.sb.run("codex")
        self.assertEqual(p.returncode, 0, p.stderr)
        opened = [e for e in self.sb.ledger() if e["event"] == "live_window_opened"][-1]
        self.assertIn("title locked: no", opened["note"])
        sh = open(os.path.join(self.rd(p), "live.sh")).read()
        self.assertIn('if [ "$TERM_PROGRAM" = "iTerm.app" ]; then', sh)  # badge + tab color by escape code
        self.assertIn("SetBadgeFormat", sh)
        self.assertIn("bg;green;brightness;163", sh)

    def test_window_that_never_starts_reports_what_it_shows(self):
        self.sb.set_live(terminal="iTerm2")
        self.sb.window("silent", tail="(base) user@mac ~ % is /bin/bash live.sh\nzsh: command not found: is")
        p = self.sb.run("codex")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("exec", self.sb.calls()[-1]["argv"])  # ran headless
        fb = [e for e in self.sb.ledger() if e["event"] == "live_fallback"][-1]
        self.assertIn("iTerm2 window (id 4242) did not start the agent", fb["note"])
        self.assertIn("zsh: command not found: is", fb["note"])
        self.assertIn("zsh: command not found: is", p.stderr)

    def test_iterm_failure_tries_terminal_then_falls_back(self):
        self.sb.set_live(terminal="iTerm2")
        p = self.sb.run("agy")  # the mock osascript fails by default
        self.assertEqual(p.returncode, 0, p.stderr)
        calls = self.sb.osascript_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn('tell application "iTerm"', calls[0]["argv"])
        self.assertIn('tell application "Terminal"', calls[1]["argv"])
        fb = [e for e in self.sb.ledger() if e["event"] == "live_fallback"][-1]
        self.assertIn("iTerm2: osascript exit 1", fb["note"])
        self.assertIn("Terminal: osascript exit 1", fb["note"])

    def test_launcher_holds_the_window_only_in_a_real_terminal(self):
        text = live.script_text(run_dir="/r", cwd="/w", env={"PATH": "/p"}, argv=["/bin/agent", PROMPT],
                                prompt_path="/r/p.md", title="CK · cursor · t · 1-a", agent="cursor")
        hold = 'if [ -t 0 ] && [ -t 1 ]; then exec "${SHELL:-/bin/zsh}" -l; fi'
        self.assertEqual(text.count(hold), 2)  # after the agent, and after "Nothing to do"
        self.assertIn("printf '\\033]0;%s\\007' 'CK · cursor · t · 1-a'", text)


if __name__ == "__main__":
    unittest.main()
