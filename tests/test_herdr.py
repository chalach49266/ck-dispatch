"""Host detection, fake Herdr transport/layout/status and live integration. No real hc calls."""
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import Sandbox
from ckdispatch import herdr, launch, ledger, live, reattach, runner, watchdog
from ckdispatch.config import Config


class HerdrTests(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.sb.set_live(terminal="iTerm2", keep_awake=False, close_tab_after_s=0)
        self.sb.set_section("post_review", enabled=False)
        self.fixture = Path(self.sb.stubs) / "herdr-fixture.json"
        self.control()
        src = Path(__file__).with_name("fake_herdr.py").read_text()
        for name in ("hc", "herdr-app"):
            p = Path(self.sb.stubs) / name
            p.write_text(f"#!{sys.executable}\n" + src)
            p.chmod(0o755)
        self.env = self.sb.env(HC_TERMINAL_ID="t-main", CK_HERDR_HC=str(Path(self.sb.stubs) / "hc"),
                               CK_HERDR_APP=str(Path(self.sb.stubs) / "herdr-app"))
        self.patch = mock.patch.dict(os.environ, self.env, clear=True)
        self.patch.start()
        self.rd = Path(self.sb.runs) / "fixture-run"
        self.rd.mkdir(parents=True)

    def tearDown(self):
        self.patch.stop()
        self.sb.cleanup()

    def control(self, **kw):
        self.fixture.write_text(json.dumps(kw))

    def state(self):
        return json.loads((Path(self.sb.stubs) / "herdr-state.json").read_text())

    def calls(self, command):
        return [x for x in self.state()["calls"] if x["argv"][0] == command and not x["app"]]

    def open(self, name="fixture-run"):
        return herdr.open_window(name, self.sb.devfab, self.rd / "live.sh", "t-main", self.sb.runs, self.rd)

    def run_live(self, *extra):
        return subprocess.run([sys.executable, "-I", launch.CK_RUN, "run", "--agent", "claude", "--mode", "read",
                               "--workspace", "devfab", "--title", "Herdr fixture", "--prompt-file", self.sb.task(),
                               *extra], env=dict(os.environ), capture_output=True, text=True, timeout=40)

    def test_host_detection_and_explicit_override(self):
        self.assertEqual(herdr.detect_host("iTerm2", {}), "iTerm2")
        self.assertEqual(herdr.detect_host("Terminal", {}), "Terminal")
        self.assertEqual(herdr.detect_host("iTerm2", {"HC_TERMINAL_ID": "t-main"}), "herdr:t-main")
        self.assertEqual(herdr.detect_host("iTerm2", {"HC_TERMINAL_ID": "other", "CK_RUN_HOST": "iterm"}), "iterm")
        self.assertEqual(herdr.detect_host("iTerm2", {"HC_TERMINAL_ID": "other", "CK_RUN_HOST": "herdr:t-original"}),
                         "herdr:t-original")

    def test_run_argv_labels_and_slug(self):
        out = self.open("2026-11-15T123456-LONG.with.UNSAFE.chars-codex-abcd")
        self.assertTrue(out["ok"], out)
        a = self.calls("run")[0]["argv"]
        self.assertEqual(a[:5], ["run", "--json", "--bg", "--outlive-parent", "--name"])
        self.assertRegex(a[5], r"^[a-z0-9_-]{1,32}$")
        self.assertEqual(a[-3:], ["--", "/bin/bash", str(self.rd / "live.sh")])
        self.assertIn(self.sb.devfab, a)
        for label in ("ck-run=true", "ck.main=t-main", "herdr.space=space-main", "herdr.tab=tab-main"):
            self.assertIn(label, a)
        self.assertEqual(herdr.saved(self.rd)["terminal"], out["herdr_terminal"])
        self.assertNotEqual(herdr.safe_name("x" * 100 + "a"), herdr.safe_name("x" * 100 + "b"))

    def test_layout_equal_agents_other_panes_kept(self):
        self.open("first")
        self.open("second")
        root = self.state()["doc"]["spaces"][0]["tabs"][0]["root"]
        self.assertEqual(root["type"], "row")
        left, right = root["children"]
        self.assertEqual(left["size"], right["size"])
        self.assertEqual([x["content"]["terminal"] for x in herdr._leaves(left)], ["t-main", "t-other"])
        self.assertEqual([x["content"]["terminal"] for x in herdr._leaves(right)], ["t-run-1", "t-run-2"])
        self.assertEqual([x["size"]["weight"] for x in right["children"]], [1.0, 1.0])

    def test_layout_preserves_nonterminal_panes_and_other_tabs(self):
        self.open()
        state = self.state()
        mod = {"type": "block", "id": "notes-pane", "content": {"mod": "notes"}}
        state["doc"]["spaces"][0]["tabs"][0]["root"]["children"].append(mod)
        other_tab = {"id": "unrelated", "root": {"id": "other-root", "type": "block", "content": {"terminal": "unrelated"}}}
        state["doc"]["spaces"][0]["tabs"].append(other_tab)
        (Path(self.sb.stubs) / "herdr-state.json").write_text(json.dumps(state))
        self.open("second")
        tabs = self.state()["doc"]["spaces"][0]["tabs"]
        self.assertEqual(tabs[1], other_tab)
        self.assertIn("notes-pane", [x["id"] for x in herdr._leaves(tabs[0]["root"])])

    def test_version_conflict_rereads_preserves_concurrent_pane(self):
        self.control(conflict=True)
        self.assertTrue(self.open()["ok"])
        gets = [x for x in self.calls("connect") if x["rpc"]["method"] == "doc.get"]
        sets = [x for x in self.calls("connect") if x["rpc"]["method"] == "doc.set"]
        self.assertEqual(len(gets), 2)
        self.assertEqual([x["rpc"]["params"]["version"] for x in sets], [1, 2])
        root = self.state()["doc"]["spaces"][0]["tabs"][0]["root"]
        self.assertIn("t-concurrent", [x["content"]["terminal"] for x in herdr._leaves(root)])

    def test_concurrent_launches_serialized(self):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            results = list(pool.map(self.open, ["one", "two", "three"]))
        self.assertTrue(all(x["ok"] for x in results), results)
        calls = [x for x in self.state()["calls"] if not x["app"]]
        depth = 0
        for c in calls:
            if c["argv"][0] == "run":
                self.assertEqual(depth, 0)
                depth += 1
            elif c.get("rpc") and c["rpc"]["method"] == "doc.set":
                depth -= 1
        self.assertEqual(depth, 0)

    def test_missing_main_creates_ck_space_and_new_tab(self):
        self.control(main_missing=True)
        self.assertTrue(self.open()["ok"])
        a = self.calls("run")[0]["argv"]
        self.assertIn("herdr.space=space-ck", a)
        self.assertFalse(any(x.startswith("herdr.tab=") for x in a))
        self.assertEqual(self.calls("connect"), [])
        self.assertIn("falling back to new tab in CK", (self.rd / "herdr.log").read_text())
        self.open("second")
        self.assertEqual(len([x for x in self.state()["calls"] if x["app"] and x["argv"][:2] == ["do", "space.new"]]), 1)

    def test_layout_failure_retains_started_terminal(self):
        self.control(fail="connect")
        self.assertTrue(self.open()["ok"])
        self.assertEqual(len(self.calls("run")), 1)
        self.assertIn("layout warning", (self.rd / "herdr.log").read_text())

    def test_hc_run_failure_falls_back_to_iterm(self):
        self.control(fail="run")
        self.sb.window("run")
        p = self.run_live()
        self.assertEqual(p.returncode, 0, p.stderr)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertEqual(res["live"]["window"]["terminal"], "iTerm2")
        self.assertIn("falling back to iTerm", p.stderr)
        self.assertIn("live_host_fallback", [x["event"] for x in self.sb.ledger()])
        self.assertEqual(len(self.sb.calls()), 1)

    def test_missing_hc_falls_back_to_iterm(self):
        os.environ["CK_HERDR_HC"] = str(self.rd / "absent-hc")
        self.sb.window("run")
        p = self.run_live()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("absent-hc", p.stderr)
        self.assertEqual(self.sb.result(self.sb.run_id_from(p))["live"]["window"]["terminal"], "iTerm2")

    def test_app_failure_falls_back_to_iterm(self):
        self.control(fail="app")
        self.sb.window("run")
        p = self.run_live()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("falling back to iTerm", p.stderr)
        self.assertEqual(self.calls("run"), [])

    def test_live_completion_status_and_child_identity(self):
        self.control(run_script=True)
        p = self.run_live()
        self.assertEqual(p.returncode, 0, p.stderr)
        res = self.sb.result(self.sb.run_id_from(p))
        self.assertEqual(res["live"]["window"]["terminal"], "Herdr")
        self.assertEqual(self.sb.calls()[0]["env"]["HC_TERMINAL_ID"], "t-run-1")
        self.assertEqual(self.state()["terminals"][0]["labels"]["ck.state"], "done")
        self.assertEqual(self.sb.osascript_calls(), [])

    def test_final_rulecheck_violation_overrides_done(self):
        self.control(run_script=True)
        self.sb.control(default={"live": {"write": [str(Path(self.sb.devfab) / "Fabrication" / "unauthorized")]}})
        p = self.run_live()
        self.assertEqual(p.returncode, 5, p.stderr)
        self.assertEqual(self.state()["terminals"][0]["labels"]["ck.state"], "failed")

    def test_watchdog_approval_stall_yellow_other_stall_red(self):
        self.sb.set_live(watchdog=True, stall_s=0.4)
        self.sb.control(default={"live": {"sleep": 60}})
        for tail, expected in (("Do you want to approve?", "needs"), ("still waiting", "failed")):
            with self.subTest(tail=tail):
                self.control(run_script=True, tail=tail)
                p = self.run_live("--timeout", "4")
                self.assertEqual(p.returncode, 3, p.stderr)
                self.assertEqual(self.state()["terminals"][-1]["labels"]["ck.state"], expected)
                res = self.sb.result(self.sb.run_id_from(p))
                self.assertEqual(res["live"]["state"], "stalled")

    def test_state_mapping_and_label_exact_target(self):
        self.open()
        for status in ("ok", "done", "partial", "needs-you", "blocked", "failed", "timeout", "stalled", "flagged", "violation"):
            expected = "done" if status in ("ok", "done") else "needs" if status in ("partial", "needs-you", "blocked") else "failed"
            herdr.label(self.rd, status)
            self.assertEqual(self.calls("label")[-1]["argv"], ["label", "--json", "t-run-1", "ck.state=" + expected])
        herdr.label(self.rd, "timeout", approval=True)
        self.assertEqual(self.calls("label")[-1]["argv"][-1], "ck.state=needs")

    def test_bookkeeping_only_partial_circle_is_green(self):
        self.assertEqual(herdr.circle_status("partial", ["required reads not shown"]), "ok")
        self.assertEqual(herdr.circle_status("partial", ["required reads not shown", "agent status partial"]), "partial")
        self.assertEqual(herdr.circle_status("partial", ["no agent summary"]), "partial")
        self.assertEqual(herdr.circle_status("partial", []), "partial")
        self.assertEqual(herdr.circle_status("timeout", ["required reads not shown"]), "timeout")
        self.assertEqual(herdr.state_for(herdr.circle_status("partial", ["required reads not shown"])), "done")

    def test_read_send_and_window_tail_use_exact_terminal(self):
        self.control(tail="first\nlast")
        self.assertEqual(live.window_tail("missing-osascript", None, {}, lines=1, herdr_terminal="t-exact"), "last")
        self.assertEqual(live.send_text("missing-osascript", {}, "continue", herdr_terminal="t-exact"), (True, None))
        self.assertEqual(self.calls("read")[-1]["argv"], ["read", "--lines", "100", "t-exact"])
        self.assertEqual(self.calls("send")[-1]["argv"], ["send", "--json", "t-exact", "continue"])

    def test_watchdog_and_reattach_read_saved_terminal_no_autoclose(self):
        self.open()
        self.control(tail="Computer went to sleep")
        wd = watchdog.Watchdog(str(self.rd), {"terminal": "Herdr", "stall_s": 1, "keep_awake": False,
                                "close_tab_after_s": 0.1}, poll_s=0.1)
        self.assertTrue(wd.act.herdr_terminal)
        wd.act.sample_tail(time.monotonic())
        with mock.patch.object(live, "close_tab_later") as close:
            wd._stalled(time.monotonic(), 1.1)
            self.assertTrue(wd.resume_sent)
            self.assertIsNotNone(wd.resume_at)
            wd.finished({"state": "done"})
            close.assert_not_called()
        self.assertEqual(self.calls("read")[-1]["argv"][-1], "t-run-1")
        self.assertEqual(self.calls("send")[-1]["argv"][-2], "t-run-1")

    def test_launch_records_host_survives_different_caller(self):
        cfg = Config()
        task = Path(self.sb.runs) / "probe_prompt.md"
        task.write_text("read only")
        argv = ["run", "--agent", "claude", "--workspace", "devfab", "--title", "probe"]
        p = launch.plan(cfg, "probe", argv)
        self.assertIn("export CK_RUN_HOST=herdr:t-main", p["text"])
        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "another", "CK_RUN_HOST": "iterm"}):
            self.assertIn("export CK_RUN_HOST=iterm", launch.plan(cfg, "probe", argv)["text"])
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(herdr.detect_host("iTerm2"), "iTerm2")
        os.environ.pop("HC_TERMINAL_ID")
        self.assertIn("export CK_RUN_HOST=iTerm2", launch.plan(cfg, "probe", argv)["text"])

    def test_reattach_finalizes_saved_host_without_current_herdr_env(self):
        self.control(run_script=True)
        p = self.run_live()
        self.assertEqual(p.returncode, 0, p.stderr)
        rid = self.sb.run_id_from(p)
        rd = Path(self.sb.runs) / rid
        # The fake CLI exits on its own; wait only for its existing live.exit.
        end = time.monotonic() + 5
        while not (rd / "live.exit").exists() and time.monotonic() < end:
            time.sleep(0.05)
        self.assertTrue((rd / "live.exit").exists())
        events = [e for e in self.sb.ledger() if e.get("event") != "result"]
        Path(ledger.path(self.sb.runs)).write_text("".join(json.dumps(e) + "\n" for e in events))
        cfg = Config()
        os.environ.pop("HC_TERMINAL_ID")
        os.environ["CK_RUN_HOST"] = "iterm"
        run = ledger.find_run(self.sb.runs, rid)
        act = reattach._activity(cfg, run, str(rd), 0.1)
        self.assertEqual(act.herdr_terminal, "t-run-1")
        with contextlib.redirect_stdout(io.StringIO()):
            code = runner.finalize_run(cfg, rid, note="fixture reattach")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls("label")[-1]["argv"], ["label", "--json", "t-run-1", "ck.state=done"])
        self.assertEqual(len(self.calls("run")), 1)

    def test_written_launch_script_pins_host_when_rerun(self):
        task = Path(self.sb.runs) / "saved_prompt.md"
        task.write_text("fixture")
        fake = self.rd / "fake-ck-run"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$CK_RUN_HOST"\n')
        fake.chmod(0o755)
        with mock.patch.object(launch, "CK_RUN", str(fake)):
            result = launch.start(Config(), "saved", ["run", "--agent", "claude", "--workspace", "devfab", "--title", "fixture"])
        self.assertIn("export CK_RUN_HOST=herdr:t-main", Path(result["sh"]).read_text())
        end = time.monotonic() + 5
        while not launch.out_finished(result["out"]) and time.monotonic() < end:
            time.sleep(0.05)
        self.assertTrue(launch.out_finished(result["out"]))
        env = dict(os.environ, CK_RUN_HOST="iterm", HC_TERMINAL_ID="different")
        rerun = subprocess.run(["/bin/bash", result["sh"]], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(rerun.stdout.strip(), "herdr:t-main")

    def test_herdr_dry_run_makes_no_transport_calls(self):
        p = self.run_live("--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("# host: herdr:t-main", p.stdout)
        self.assertIn("--outlive-parent", p.stdout)
        self.assertIn("version-checked", p.stdout)
        self.assertFalse((Path(self.sb.stubs) / "herdr-state.json").exists())
        self.assertEqual(self.sb.calls(), [])


if __name__ == "__main__":
    unittest.main()
