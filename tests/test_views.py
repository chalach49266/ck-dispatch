"""v4 views (BUILD_SPEC_v4 10.7): ck-run board, digest, recall, scoreboard. Read-only views of the ledger.

Every test writes its own fixture ledger and run folders in a test Sandbox (fake HOME, test config, mock osascript);
nothing reads or writes the real ledger. The Fixture class here is shared by test_andon, test_policy, test_patterns
and test_loop.
"""
import argparse
import contextlib
import io
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

from helpers import Sandbox

from ckdispatch import claims, ledger, views
from ckdispatch.config import Config
from ckdispatch.errors import Refused

RUN_ID_RX = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}-[a-z0-9]+-[a-z0-9_]+-[a-z]+-[0-9a-f]{4}")
E3_MODULES = ("views", "policy", "andon", "patterns", "loop")


def cli_registers(command: str) -> bool:
    """True when bin/ck-run (cli._parser, owned by E1) knows `command` (the extension modules are registered)."""
    try:
        from ckdispatch import cli
        ap = cli._parser()
    except Exception:
        return False
    for act in ap._actions:
        if isinstance(act, argparse._SubParsersAction):
            return command in act.choices
    return False


class Fixture:
    """A test Sandbox plus helpers that write fixture ledger lines, run folders and run-registry records."""

    def __init__(self):
        self.sb = Sandbox()
        self.env = mock.patch.dict(os.environ, {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config})
        self.env.start()
        self.cfg = Config()
        self.runs = self.cfg.runs_dir
        os.makedirs(self.runs, exist_ok=True)
        # noon today: "today" checks never cross midnight while a test runs
        self.now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        self.n = 0
        self.held = []

    def close(self):
        for h in self.held:
            h.release()
        self.env.stop()
        self.sb.cleanup()

    def ts(self, minutes_ago: float = 0) -> str:
        return (self.now - timedelta(minutes=minutes_ago)).isoformat()

    def add(self, **ev) -> dict:
        ev.setdefault("ts", self.ts())
        with open(ledger.path(self.runs), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")
        return ev

    def ask(self, aid, title, size="M", project=None, minutes_ago=120, pattern="solo", words="chalach's words"):
        return self.add(event="ask_open", ask_id=aid, title=title, size=size, project=project, pattern=pattern,
                        words=words, budget={"runs": 2, "parallel": 1}, ts=self.ts(minutes_ago))

    def close_ask(self, aid, status="done", card=None, minutes_ago=0):
        return self.add(event="ask_close", ask_id=aid, status=status, card=card, ts=self.ts(minutes_ago))

    def new_id(self, ws="devfab", agent="codex", minutes_ago=0) -> str:
        self.n += 1
        t = self.now - timedelta(minutes=minutes_ago)
        return f"{t:%Y-%m-%dT%H%M%S}-{ws}-general-{agent}-{self.n:04x}"

    def run(self, ask_id=None, agent="codex", alias="sol", model="gpt-6.1-sol", title="a task", ws="devfab",
            kind="dispatch", status="ok", minutes_ago=30, duration_s=600.0, parent=None, lane=None, agent_result=None,
            summary=("did the task",), finished=True, accepted=None, note=None) -> str:
        rid = self.new_id(ws, agent, minutes_ago)
        base = dict(run_id=rid, ask_id=ask_id, agent=agent, model=model, model_alias=alias, title=title,
                    workspace=ws, parent_run_id=parent, lane=lane)
        self.add(event=kind, status="dispatched", started_at=self.ts(minutes_ago), ts=self.ts(minutes_ago), **base)
        d = os.path.join(self.runs, rid)
        os.makedirs(d, exist_ok=True)
        if finished:
            fin = max(0.0, minutes_ago - duration_s / 60.0)
            self.add(event="result", status=status, duration_s=duration_s, ts=self.ts(fin), note=note, **base)
            with open(os.path.join(d, "result.json"), "w") as f:
                json.dump({"run_id": rid, "status": status, "agent_result": agent_result or {}}, f)
            with open(os.path.join(d, "summary.md"), "w") as f:
                f.write("\n".join(summary) + "\n")
        if accepted is not None:
            self.add(event="accept", accepted=accepted, ts=self.ts(max(0.0, minutes_ago - 1)), **base)
        return rid

    def live(self, rid, agent="codex", title="a task", minutes_ago=5.0, kind="dispatch"):
        got = claims.acquire(self.runs, {"run_id": rid, "agent": agent, "title": title, "kind": kind,
                                         "started_at": self.ts(minutes_ago), "run_dir": os.path.join(self.runs, rid),
                                         "claims": []})
        self.held.append(got["held"])
        return got["held"]

    def stale_record(self, rid, agent="codex", title="a task"):
        d = claims.claims_dir(self.runs)
        os.makedirs(d, exist_ok=True)
        with open(claims.record_path(self.runs, rid), "w") as f:
            json.dump({"run_id": rid, "agent": agent, "title": title, "started_at": self.ts(20), "claims": []}, f)

    def snap(self) -> views.Snapshot:
        return views.Snapshot(self.cfg, now=self.now)

    def cli(self, *argv) -> tuple:
        """Run an E3 command in-process (a parser with the E3 modules' register(), like cli._parser builds).
        Returns (exit code, stdout, stderr)."""
        import importlib
        ap = argparse.ArgumentParser(prog="ck-run")
        sub = ap.add_subparsers(dest="cmd", required=True)
        for name in E3_MODULES:
            importlib.import_module(f"ckdispatch.{name}").register(sub)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(views, "_now", return_value=self.now):
            try:
                a = ap.parse_args([str(x) for x in argv])
                code = a.func(self.cfg, a)
            except Refused as e:
                print(f"ck-run: {'OVER BUDGET' if type(e).__name__ == 'OverBudget' else 'REFUSED'}: {e}",
                      file=sys.stderr)
                code = 7 if type(e).__name__ == "OverBudget" else 2
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 2
        return code, out.getvalue(), err.getvalue()


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()


class TestNeedsYou(FixtureCase):
    def kinds(self, b):
        return sorted((n["kind"], n.get("ask_id") or "-") for n in b["needs_you"])

    def test_empty_board(self):
        code, out, err = self.fx.cli("board")
        self.assertEqual(code, 0, err)
        self.assertIn("NEEDS YOU (0)", out)
        self.assertIn("RUNNING (0)", out)
        self.assertIn("DONE TODAY (0)", out)
        code, out, _ = self.fx.cli("board", "--json")
        self.assertEqual(code, 0)
        b = json.loads(out)
        for k in ("needs_you", "running", "done_today"):
            self.assertIsInstance(b[k], list)

    def test_parked_and_over_budget(self):
        fx = self.fx
        fx.ask("A-1105-1", "SPCM kit -> wiki")
        fx.add(event="ask_park", ask_id="A-1105-1", reason="over time", ts=fx.ts(10))
        fx.ask("A-1105-2", "fix spcm_counter.py")
        fx.add(event="over_budget", ask_id="A-1105-2", status="refused", ts=fx.ts(9),
               reason='ask A-1105-2 (M) used 2/2 runs. To go over, add --over "<chalach\'s words>"')
        fx.ask("A-1105-3", "approved later")
        fx.add(event="over_budget", ask_id="A-1105-3", status="refused", reason="used 1/1 runs", ts=fx.ts(9))
        fx.add(event="over_budget", ask_id="A-1105-3", status="approved", over="yes go on", ts=fx.ts(8))
        fx.ask("A-1105-4", "a later dispatch passed")
        fx.add(event="over_budget", ask_id="A-1105-4", status="refused", reason="1 live, parallel 1", ts=fx.ts(9))
        fx.run("A-1105-4", minutes_ago=5, finished=False)
        fx.ask("A-1105-5", "closed ask")
        fx.add(event="over_budget", ask_id="A-1105-5", status="refused", reason="used 2/2", ts=fx.ts(9))
        fx.close_ask("A-1105-5")
        b = fx.snap().board(tails=False)
        self.assertEqual(self.kinds(b), [("over_budget", "A-1105-2"), ("parked", "A-1105-1")])
        over = next(n for n in b["needs_you"] if n["kind"] == "over_budget")
        self.assertIn("used 2/2 runs", over["text"])
        text = views.board_text(b, fx.now)
        self.assertIn("A-1105-1  SPCM kit -> wiki: parked (over time)", text)

    def test_flagged_and_violation_until_accepted(self):
        fx = self.fx
        fx.ask("A-1105-1", "open ask")
        flagged = fx.run("A-1105-1", status="flagged")
        fx.run("A-1105-1", status="flagged", accepted=False)          # decided: rejected
        fx.run("A-1105-1", status="flagged", accepted=True)           # decided: accepted
        fx.ask("A-1105-2", "closed ask")
        violation = fx.run("A-1105-2", status="violation")            # ask close leaves it for chalach
        fx.close_ask("A-1105-2")
        fx.run(None, status="flagged", minutes_ago=3 * 24 * 60)      # old run without an ask: noise
        recent = fx.run(None, status="violation", minutes_ago=60)     # recent run without an ask
        b = fx.snap().board(tails=False)
        got = sorted(n["run_id"] for n in b["needs_you"] if n["kind"] in ("flagged", "violation"))
        self.assertEqual(got, sorted([flagged, violation, recent]))
        line = next(views._need_line(n) for n in b["needs_you"] if n.get("run_id") == flagged)
        self.assertTrue(line.startswith(" !  A-1105-1"), line)
        self.assertIn("flagged, not accepted yet", line)

    def test_stall_of_live_run_and_questions(self):
        fx = self.fx
        fx.ask("A-1105-1", "SPCM bring-up kit")
        rid = fx.run("A-1105-1", agent="claude", alias="opus", finished=False, minutes_ago=12)
        fx.add(event="stall", run_id=rid, ask_id="A-1105-1", reason="approval",
               note="Do you want to make this edit? [y/n]", ts=fx.ts(4))
        done = fx.run("A-1105-1", minutes_ago=40)
        fx.add(event="stall", run_id=done, ask_id="A-1105-1", reason="unknown", ts=fx.ts(35))  # finished since
        fx.run("A-1105-1", minutes_ago=20, agent_result={"open_questions": ["Which BNC port do you use?"]})
        fx.ask("A-1105-2", "closed")
        fx.run("A-1105-2", agent_result={"open_questions": ["old question"]})
        fx.close_ask("A-1105-2")
        b = fx.snap().board(tails=False)
        stalls = [n for n in b["needs_you"] if n["kind"] == "stall"]
        self.assertEqual([n["run_id"] for n in stalls], [rid])
        self.assertIn("claude run stalled (approval)", stalls[0]["text"])
        qs = [n for n in b["needs_you"] if n["kind"] == "question"]
        self.assertEqual(len(qs), 1)
        self.assertIn("question from sol: Which BNC port do you use?", qs[0]["text"])

    def test_lost_watcher(self):
        fx = self.fx
        fx.ask("A-1105-1", "lost one")
        rid = fx.run("A-1105-1", finished=False, minutes_ago=30)
        fx.stale_record(rid)
        old = fx.run("A-1105-1", finished=False, minutes_ago=50)
        fx.add(event="stale", run_id=old, ask_id="A-1105-1", status="failed", ts=fx.ts(45))
        b = fx.snap().board(tails=False)
        lost = sorted(n["run_id"] for n in b["needs_you"] if n["kind"] == "lost_watcher")
        self.assertEqual(lost, sorted([rid, old]))
        self.assertTrue(all("ck-run reattach" in n["text"] for n in b["needs_you"] if n["kind"] == "lost_watcher"))
        self.assertTrue(os.path.exists(claims.record_path(fx.runs, rid)), "the board must not clean the registry")


class TestRunning(FixtureCase):
    def test_live_run_with_window_tail(self):
        fx = self.fx
        fx.ask("A-1105-6", "SPCM bring-up kit")
        rid = fx.run("A-1105-6", agent="codex", alias="sol", title="counter script", finished=False, minutes_ago=9)
        fx.add(event="live_window_opened", run_id=rid, ask_id="A-1105-6", agent="codex",
               note="window_id=4242; terminal=iTerm2; profile CK Live codex; title locked: yes; title: x")
        fx.live(rid, title="counter script", minutes_ago=9)
        fx.sb.window("fail", tail="pytest -q\n12 passed\nrunning pytest on the counter")
        code, out, err = fx.cli("board")
        self.assertEqual(code, 0, err)
        line = next(ln for ln in out.splitlines() if ln.startswith(" >  "))
        self.assertIn("codex sol", line)
        self.assertIn("A-1105-6", line)
        self.assertIn("counter script", line)
        self.assertIn("9 min", line)
        self.assertIn("last: running pytest on the counter", line)
        self.assertIn(f"[window {views.window_short(rid)}]", line)
        calls = fx.sb.osascript_calls()
        self.assertTrue(calls, "the window text comes from the mock osascript")
        self.assertTrue(all(c["argv"][0].startswith(fx.sb.stubs) for c in calls))

    def test_newest_file_without_window_and_in_shared_window(self):
        fx = self.fx
        fx.ask("A-1105-7", "two tabs, one window")
        r1 = fx.run("A-1105-7", agent="claude", alias="opus", finished=False, minutes_ago=3)
        r2 = fx.run("A-1105-7", agent="codex", alias="sol", finished=False, minutes_ago=2)
        for rid in (r1, r2):
            fx.add(event="live_window_opened", run_id=rid, ask_id="A-1105-7",
                   note="window_id=77; terminal=iTerm2; profile x; title locked: no; title: y")
            fx.live(rid, minutes_ago=2)
            with open(os.path.join(fx.runs, rid, "result.md"), "w") as f:
                f.write("partial\n")
        fx.sb.window("fail", tail="other run's tab text")
        b = fx.snap().board()
        self.assertEqual(len(b["running"]), 2)
        for r in b["running"]:
            self.assertEqual(r["last_from"], "file", r)
            self.assertIn("newest file", r["last"])
        self.assertEqual(fx.sb.osascript_calls(), [], "a shared window without the run's session is not read")

    def test_shared_window_with_session_ids_reads_each_tab(self):
        fx = self.fx
        fx.ask("A-1105-8", "two tabs with session ids")
        rids = []
        for k, agent in enumerate(("claude", "codex"), 1):
            rid = fx.run("A-1105-8", agent=agent, alias=None, finished=False, minutes_ago=2)
            fx.add(event="live_window_opened", run_id=rid, ask_id="A-1105-8",
                   note=f"window_id=88; terminal=iTerm2; session_id=sess-88-{k}; title locked: yes; title: t")
            fx.live(rid, agent=agent, minutes_ago=2)
            rids.append(rid)
        fx.sb.window("fail", tail="working on step 2")
        b = fx.snap().board()
        self.assertEqual(sorted(r["run_id"] for r in b["running"]), sorted(rids))
        for r in b["running"]:
            self.assertEqual((r["last_from"], r["last"]), ("window", "working on step 2"), r)
        sent = sorted(a for c in fx.sb.osascript_calls() for a in c["argv"] if str(a).startswith("sess-88-"))
        self.assertEqual(sent, ["sess-88-1", "sess-88-2"], "each run's own tab (session id) is read")

    def test_orphan_is_flagged_for_reattach(self):
        fx = self.fx
        rid = fx.run(None, finished=False, minutes_ago=4)
        held = fx.live(rid, minutes_ago=4)
        held.update(orphan=True)  # what claims._scan reports when ck-run is gone and its agent still runs
        snap = fx.snap()
        with mock.patch.object(snap, "registry", return_value=([dict(held.record, orphan=True)], [])):
            b = snap.board(tails=False)
        self.assertIn("its ck-run stopped", views._run_line(b["running"][0]))
        self.assertTrue(any(n["kind"] == "lost_watcher" and n["run_id"] == rid for n in b["needs_you"]))


class TestDoneToday(FixtureCase):
    CARD = ("A-1105-1  AutoNV-MW rename   DONE   2 runs, 41 min\nChanged: renamed the repo\n"
            "How sure: fake data or tests\nYou should know: nothing new\nYour move: B8/B9 name\nLater (0):")

    def test_done_today_from_card_and_results(self):
        fx = self.fx
        fx.ask("A-1105-1", "AutoNV-MW rename")
        r1 = fx.run("A-1105-1")
        fx.close_ask("A-1105-1", card=self.CARD)
        fx.ask("A-1105-2", "SPCM kit -> wiki")
        r2 = fx.run("A-1105-2", agent_result={"verification": "fake_or_tests", "your_move": "try script 01"})
        fx.run("A-1105-2", agent_result={"verification": "desk_check", "your_move": "nothing"})
        fx.run("A-1105-2", kind="review", agent_result={"verification": "not_checked"})  # reviews do not count
        fx.close_ask("A-1105-2", status="partial")
        fx.ask("A-1105-3", "closed yesterday")
        fx.close_ask("A-1105-3", minutes_ago=24 * 60)
        fx.ask("A-1105-4", "quick answer")
        fx.add(event="answered", ask_id="A-1105-4", note="one paragraph", status="done")
        fx.close_ask("A-1105-4")
        code, out, err = fx.cli("board")
        self.assertEqual(code, 0, err)
        done = out.split("DONE TODAY")[1].splitlines()[1:]
        self.assertEqual(len(done), 3, out)
        self.assertIn(" ok  A-1105-1  AutoNV-MW rename  DONE  how sure: fake data or tests  your move: B8/B9 name",
                      done)
        partial = next(ln for ln in done if "A-1105-2" in ln)
        self.assertIn("PARTIAL", partial)
        self.assertIn("how sure: desk check", partial)
        self.assertIn("your move: try script 01", partial)
        self.assertIn("answered by Grok Bot", next(ln for ln in done if "A-1105-4" in ln))
        for ln in done:
            self.assertIsNone(RUN_ID_RX.search(ln), f"run id in a DONE line: {ln}")
        self.assertNotIn(r1, out.split("DONE TODAY")[1])
        self.assertNotIn(r2, out)


class TestBoardMarkdown(FixtureCase):
    def test_md_default_path_custom_path_and_no_clobber(self):
        fx = self.fx
        fx.ask("A-1105-1", "parked one")
        fx.add(event="ask_park", ask_id="A-1105-1", reason="over time")
        code, out, err = fx.cli("board", "--md")
        self.assertEqual(code, 0, err)
        p = os.path.join(fx.cfg.local_root, "BOARD.md")
        with open(p) as f:
            md = f.read()
        self.assertTrue(md.startswith(views.BOARD_HEADER + "\n"))
        self.assertIn("## Needs you (1)", md)
        self.assertIn("A-1105-1", md)
        self.assertIn("BOTS BOARD", out)  # the text board is still printed
        code, _, err = fx.cli("board", "--md")  # rewriting a board file is fine
        self.assertEqual(code, 0, err)
        other = os.path.join(fx.sb.tmp, "custom-board.md")
        code, _, err = fx.cli("board", "--md", other, "--json")
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(other))
        notes = os.path.join(fx.sb.tmp, "notes.md")
        with open(notes, "w") as f:
            f.write("# my notes\n")
        code, _, err = fx.cli("board", "--md", notes)
        self.assertEqual(code, 2)
        self.assertIn("not a board file", err)
        with open(notes) as f:
            self.assertEqual(f.read(), "# my notes\n")


class TestDigest(FixtureCase):
    def test_per_project_digest(self):
        fx = self.fx
        fx.ask("A-1105-1", "SPCM kit -> wiki", project="fnd_sin")
        fx.run("A-1105-1", alias="sol", duration_s=1200, agent_result={"verification": "desk_check"})
        fx.run("A-1105-1", agent="claude", alias="opus", model="opus", duration_s=600,
               agent_result={"verification": "fake_or_tests"})
        fx.add(event="later_add", ask_id="A-1105-1", note="datasheet check")
        fx.close_ask("A-1105-1", minutes_ago=5)
        fx.ask("A-1105-2", "fix spcm_counter.py", project="FND_SiN")  # same project, other case
        fx.add(event="ask_park", ask_id="A-1105-2", reason="over time")
        fx.run(None, agent="agy", alias=None, model=None, ws="nv", duration_s=300)     # no ask: workspace nv
        fx.run(None, agent="codex", ws="nv", minutes_ago=3 * 24 * 60)                 # outside 1d
        d = views.digest(fx.snap(), views.parse_since("1d"))
        by = {p["project"]: p for p in d["projects"]}
        self.assertEqual(sorted(by), ["fnd_sin", "nv"])
        f = by["fnd_sin"]
        self.assertEqual([x["ask_id"] for x in f["asks_done"]], ["A-1105-1"])
        self.assertEqual(f["runs"], 2)
        self.assertEqual(f["minutes"], {"sol": 20.0, "opus": 10.0})
        self.assertEqual(f["lowest_verification"], "desk_check")
        self.assertEqual([o["ask_id"] for o in f["open_asks"]], ["A-1105-2"])
        self.assertEqual(f["later_items"], 1)
        self.assertEqual(by["nv"]["runs"], 1)
        self.assertEqual(by["nv"]["minutes"], {"agy": 5.0})
        text = views.digest_text(d, "1d")
        self.assertIn("fnd_sin", text)
        self.assertIn("agent time: sol 20 min, opus 10 min", text)
        self.assertIn("how sure (lowest): desk check", text)
        self.assertIn("A-1105-2 fix spcm_counter.py (parked)", text)
        self.assertIsNone(RUN_ID_RX.search(text))
        week = views.digest(fx.snap(), views.parse_since("7d"))
        self.assertEqual({p["project"]: p["runs"] for p in week["projects"]}["nv"], 2)
        code, out, err = fx.cli("digest", "--since", "7d")
        self.assertEqual(code, 0, err)
        self.assertIn("DIGEST since", out)

    def test_since_is_checked(self):
        for bad in ("", "1", "1y", "0d", "-1d", "d"):
            with self.assertRaises(Refused, msg=bad):
                views.parse_since(bad)
        self.assertEqual(views.parse_since("12h"), timedelta(hours=12))
        self.assertEqual(views.parse_since("2w"), timedelta(weeks=2))
        code, _, err = self.fx.cli("digest", "--since", "soon")
        self.assertEqual(code, 2)
        self.assertIn("--since", err)


class TestRecall(FixtureCase):
    def test_recall_lines_tags_and_next_move(self):
        fx = self.fx
        fx.ask("A-1105-1", "SPCM wiring answer", project="fnd_sin", minutes_ago=300)
        fx.add(event="answered", ask_id="A-1105-1", note="one paragraph", ts=fx.ts(290))
        fx.close_ask("A-1105-1", minutes_ago=290)
        fx.ask("A-1105-2", "SPCM kit -> wiki", project="fnd_sin", minutes_ago=200)
        fx.run("A-1105-2", minutes_ago=190, agent_result={"plain_summary": ["kit saved, one wiki page"]})
        fx.add(event="later_add", ask_id="A-1105-2", note="datasheet check")
        fx.close_ask("A-1105-2", minutes_ago=100, card="A-1105-2 ...\nChanged: kit saved; new wiki page\n"
                                                         "How sure: desk check\nYour move: try script 01")
        fx.ask("A-1105-3", "fix counter", project="fnd_sin", minutes_ago=90)
        fx.add(event="ask_park", ask_id="A-1105-3", reason="over time")
        fx.ask("A-1105-4", "bring-up kit", project="fnd_sin", minutes_ago=60)
        live = fx.run("A-1105-4", finished=False, minutes_ago=7)
        fx.live(live, minutes_ago=7)
        fx.ask("A-1105-5", "flagged one", project="fnd_sin", minutes_ago=50)
        fx.run("A-1105-5", status="flagged", minutes_ago=40)
        fx.ask("A-1105-6", "idle one", project="fnd_sin", minutes_ago=10)
        fx.ask("A-1105-7", "other project", project="nv_mag", minutes_ago=10)
        d = views.recall(fx.snap(), "FND_SIN")
        self.assertLessEqual(len(d["capsule"]), 5)
        self.assertIn("fnd_sin: 6 ask(s)", d["capsule"][0])
        self.assertTrue(any(c.startswith("Last done: SPCM kit -> wiki") and "kit saved; new wiki page" in c
                            for c in d["capsule"]), d["capsule"])
        tags = {i["ask_id"]: i["tag"] for i in d["asks"]}
        self.assertEqual(tags, {"A-1105-1": "done", "A-1105-2": "done", "A-1105-3": "parked",
                                "A-1105-4": "running", "A-1105-5": "needs you", "A-1105-6": "open"})
        self.assertEqual([x["item"] for x in d["later"]], ["datasheet check"])
        self.assertTrue(d["next_move"].startswith("A-1105-3: parked"), d["next_move"])
        text = views.recall_text(d)
        self.assertIn("A-1105-4  bring-up kit  [running]", text)
        self.assertIn("later A-1105-2 #1: datasheet check", text)
        self.assertTrue(text.rstrip().splitlines()[-1].startswith("Next move: "))

    def test_recall_text_stays_short(self):
        fx = self.fx
        for k in range(1, 26):
            fx.ask(f"A-1104-{k}", f"old ask {k}", project="big", minutes_ago=3000 - k)
            fx.close_ask(f"A-1104-{k}", minutes_ago=2000 - k)
        fx.ask("A-1105-1", "still open", project="big", minutes_ago=5)
        for k in range(12):
            fx.add(event="later_add", ask_id="A-1105-1", note=f"idea {k}")
        text = views.recall_text(views.recall(fx.snap(), "big"))
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 5 + 1 + 20 + 11 + 1)
        self.assertIn("... 6 older done ask(s) not shown (ck-run recall big --json)", text)
        self.assertIn("A-1105-1  still open  [open]", text)
        self.assertIn("A-1104-25  old ask 25  [done]", text)
        self.assertNotIn("A-1104-6  old ask 6 ", text)
        self.assertIn("  ... 2 more later item(s)", text)

    def test_recall_workspace_alias_and_unknown(self):
        fx = self.fx
        fx.run(None, title="legacy run", minutes_ago=60)
        code, out, err = fx.cli("recall", "Device Fabrication")
        self.assertEqual(code, 0, err)
        self.assertIn("devfab: no asks yet.", out)
        self.assertIn("Runs without an ask in the last 7 days: 1 (last: legacy run", out)
        self.assertIn("Next move: nothing", out)
        code, out, _ = fx.cli("recall", "nothing_here")
        self.assertEqual(code, 0)
        self.assertIn("nothing_here: no asks yet.", out)


class TestScoreboard(FixtureCase):
    def test_rates_minutes_fix_rounds(self):
        fx = self.fx
        a = fx.run(None, alias="sol", lane="lab-code", duration_s=600, accepted=True)
        fx.run(None, alias="sol", lane="lab-code", duration_s=1200, status="partial", accepted=False)
        fx.run(None, alias="sol", lane="lab-code", duration_s=1800)               # not decided
        fx.run(None, alias="sol", lane="lab-code", duration_s=300, parent=a)       # a fix round of `a`
        fx.run(None, agent="claude", alias="opus", model="opus", kind="review", duration_s=420, parent=a)
        fx.run(None, alias="sol", lane="lab-code", minutes_ago=40 * 24 * 60)      # outside 30d
        s = views.scoreboard(fx.snap(), views.parse_since("30d"))
        rows = {(r["lane"], r["model"], r["agent"]): r for r in s["rows"]}
        lab = rows[("lab-code", "sol", "codex")]
        self.assertEqual((lab["runs"], lab["ok"], lab["results"]), (4, 3, 4))
        self.assertEqual((lab["accepted"], lab["decided"]), (1, 2))
        self.assertEqual(lab["median_min"], 15.0)
        self.assertEqual(lab["fix_rounds"], 1)
        rev = rows[("review", "opus", "claude")]
        self.assertEqual((rev["runs"], rev["fix_rounds"]), (1, 0))
        text = views.scoreboard_text(s, "30d")
        self.assertIn("lab-code", text)
        self.assertIn("3/4 75%", text)
        self.assertIn("1/2 50%", text)
        code, out, err = fx.cli("scoreboard", "--json")
        self.assertEqual(code, 0, err)
        self.assertIn("rows", json.loads(out))


class TestLedgerReading(FixtureCase):
    def test_reads_events_without_run_id_and_skips_junk(self):
        fx = self.fx
        fx.ask("A-1105-1", "x")
        with open(ledger.path(fx.runs), "a") as f:
            f.write("not json\n{\"no_event\": 1}\n[1, 2]\n")
        fx.add(event="andon", project="devfab", status="raised", reason="r")
        names = [e["event"] for e in views.read_events(fx.runs)]
        self.assertEqual(names, ["ask_open", "andon"])

    def test_ledger_append_keeps_unknown_fields_in_the_note(self):
        fx = self.fx
        with mock.patch.object(ledger, "KEYS", [k for k in ledger.KEYS if k not in ("project", "reason")]):
            views.ledger_append(fx.runs, event="andon", project="devfab", status="raised", reason="stop", note="n")
        ev = fx.sb.ledger()[-1]
        self.assertEqual(ev["event"], "andon")
        self.assertIn("project=devfab", ev["note"])
        self.assertIn("reason=stop", ev["note"])
        views.ledger_append(fx.runs, event="andon", project="nv", status="cleared", reason="ok")
        ev = fx.sb.ledger()[-1]
        self.assertEqual((ev.get("project"), ev.get("reason")), ("nv", "ok"))


class TestViaCkRun(unittest.TestCase):
    """The real bin/ck-run (cli._parser from E1 registers the E3 modules)."""

    def setUp(self):
        if not cli_registers("board"):
            self.skipTest("cli._parser does not register the E3 modules yet (E1 integration)")
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def test_board_digest_recall_scoreboard(self):
        p = self.sb.ck("board", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sorted(k for k in json.loads(p.stdout) if k != "generated_at"),
                         ["done_today", "needs_you", "running"])
        for args in (("digest", "--since", "7d"), ("recall", "devfab"), ("scoreboard",)):
            p = self.sb.ck(*args)
            self.assertEqual(p.returncode, 0, f"{args}: {p.stderr}")
        self.assertFalse(os.path.exists(os.path.join(self.sb.local, "BOARD.md")))


if __name__ == "__main__":
    unittest.main()
