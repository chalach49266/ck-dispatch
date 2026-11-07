"""v4 asks (BUILD_SPEC_v4 section 5): ids, budgets per size, race / --parallel / --plan budgets, the gate, park,
close (idempotent, finish card), later, answered, list, show, and the `ck-run ask ...` commands. Stub CLIs only;
runs are simulated with ledger events and result.json files in the sandbox runs folder."""
import io
import json
import os
import re
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta
from unittest import mock

from helpers import Sandbox

from ckdispatch import asks, cli, guards, ledger
from ckdispatch.config import Config
from ckdispatch.errors import Refused

RUN_ID_RX = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}-[a-z0-9]+-[a-z0-9_]+-[a-z]+-[0-9a-f]{4}")


class Base(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.env = mock.patch.dict(os.environ, {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config})
        self.env.start()
        self.cfg = Config()
        self.n = 0

    def tearDown(self):
        self.env.stop()
        self.sb.cleanup()

    def ask(self, size="M", **kw):
        kw.setdefault("words", "chalach: please do it")
        kw.setdefault("title", "Test goal")
        return asks.open_ask(self.cfg, size=size, **kw)["ask_id"]

    def fake_run(self, ask_id, *, kind="dispatch", status="ok", agent="codex", parent=None, v4=None, summary=None,
                 alias="sol"):
        """A finished run in the ledger (dispatch/review + result) with its result.json."""
        self.n += 1
        rid = f"2026-11-05T{120000 + self.n:06d}-devfab-general-{agent}-{self.n:04x}"
        rd = os.path.join(self.sb.runs, rid)
        os.makedirs(rd)
        ar = {"run_id": rid, "status": "ok", "summary": summary or [f"Stub summary of {rid}"]}
        ar.update(v4 or {})
        with open(os.path.join(rd, "result.json"), "w") as f:
            json.dump({"run_id": rid, "status": status, "agent_result": ar,
                       "summary_lines": summary or [f"Stub summary of {rid}"]}, f)
        with open(os.path.join(rd, "summary.md"), "w") as f:
            f.write("\n".join(summary or ["s"]) + "\n")
        base = dict(run_id=rid, ask_id=ask_id, agent=agent, workspace="devfab", title="t", mode="read",
                    parent_run_id=parent, model_alias=alias)
        ledger.append(self.sb.runs, event=kind, status="dispatched", **base)
        ledger.append(self.sb.runs, event="result", status=status, summary_path=os.path.join(rd, "summary.md"),
                      duration_s=60, **base)
        return rid

    def spec(self, ask_id, kind="dispatch", **kw):
        d = dict(ask_id=ask_id, kind=kind, trigger=None, over=None, parent_run_id=None, tier=None,
                 model_explicit=False, title="t")
        d.update(kw)
        return type("S", (), d)()

    def events(self, name):
        return [e for e in ledger.load(self.sb.runs) if e.get("event") == name]


class TestIdsAndBudgets(Base):
    def test_ask_id_format_and_order(self):
        mmdd = datetime.now().strftime("%m%d")
        a, b = self.ask("S"), self.ask("M")
        self.assertEqual((a, b), (f"A-{mmdd}-1", f"A-{mmdd}-2"))
        self.assertTrue(asks.valid_id(a))
        for bad in ("A-1105", "A-1105-0", "a-1105-1", "A-11050-1", "", None, "A-1105-1 x"):
            self.assertFalse(asks.valid_id(bad), bad)
        self.assertIs(asks.open, asks.open_ask)  # the contract name
        ev = self.events("ask_open")[0]
        self.assertEqual((ev["words"], ev["size"], ev["pattern"], ev["title"], ev["run_id"]),
                         ("chalach: please do it", "S", "solo", "Test goal", None))

    def test_budgets_per_size_match_the_spec(self):
        want = {"S": (1, 1, 0, 0, 15, 600, 180), "M": (2, 1, 1, 1, 45, 1800, 300), "L": (6, 3, 2, 2, 180, 7200, 480)}
        for size, w in want.items():
            with self.subTest(size=size):
                b = asks.budget_for(self.cfg, size)
                self.assertEqual(tuple(b[k] for k in asks.BUDGET_KEYS), w)
                st = asks.need(self.cfg, self.ask(size))
                self.assertEqual(tuple(st["budget"][k] for k in asks.BUDGET_KEYS), w)  # stored in ask_open

    def test_race_parallel_and_plan_budgets(self):
        b = asks.budget_for(self.cfg, "S", "race")
        self.assertEqual((b["runs"], b["parallel"]), (3, 3))  # race_default has 3 models
        b = asks.budget_for(self.cfg, "S", "race", ["astra", "opus"])
        self.assertEqual((b["runs"], b["parallel"]), (2, 2))
        b = asks.budget_for(self.cfg, "L", "race", ["astra", "opus"])
        self.assertEqual((b["runs"], b["parallel"]), (6, 2))
        st = asks.need(self.cfg, self.ask("S", parallel=3, because="all 3 agents"))
        self.assertEqual((st["budget"]["runs"], st["budget"]["parallel"], st["because"]), (3, 3, "all 3 agents"))
        with self.assertRaises(Refused):
            self.ask("S", parallel=3)  # no --because
        with self.assertRaises(Refused):
            self.ask("S", because="words")  # --because without --parallel
        plan = os.path.join(self.sb.tmp, "plan.md")
        with open(plan, "w") as f:
            f.write("# Plan\n- step 1\n- budget: runs=8 parallel=3 reviews=2 fixes=3 wall_min=240\n")
        st = asks.need(self.cfg, self.ask("L", plan=plan))
        self.assertEqual((st["budget"]["runs"], st["budget"]["fixes"], st["budget"]["wall_min"]), (8, 3, 240))
        self.assertEqual(st["plan"], plan)
        with self.assertRaises(Refused):
            self.ask("M", plan=plan)  # L only
        with open(plan, "w") as f:
            f.write("budget: runs=8 timeout=99\n")
        with self.assertRaises(Refused):
            self.ask("L", plan=plan)

    def test_open_checks_its_inputs(self):
        for kw in ({"words": " "}, {"title": ""}, {"pattern": "swarm"}, {"project": "fnd sin/x"},
                   {"models": "astra,gpt9"}):
            with self.subTest(kw=kw):
                with self.assertRaises(Refused):
                    self.ask("M", **kw)
        with self.assertRaises(Refused):
            asks.open_ask(self.cfg, words="w", size="XL", title="t")
        st = asks.need(self.cfg, self.ask("M", models="astra+opus", project="fnd_sin", pattern="pair"))
        self.assertEqual((st["models"], st["project"], st["pattern"]), (["astra", "opus"], "fnd_sin", "pair"))

    def test_config_budgets_override_the_code_defaults(self):
        self.sb.set_section("asks", budgets={"S": {"runs": 2, "wall_min": 20}})
        b = asks.budget_for(Config(), "S")
        self.assertEqual((b["runs"], b["wall_min"], b["parallel"]), (2, 20, 1))


class TestGate(Base):
    def test_no_ask(self):
        self.assertIsNone(asks.gate(self.cfg, self.spec(None)))
        self.sb.set_section("asks", required=True)
        with self.assertRaises(Refused) as c:
            asks.gate(Config(), self.spec(None, title="MMI width"))
        self.assertIn("ck-run ask open --words", str(c.exception))
        self.assertIn("--title 'MMI width'", str(c.exception))
        self.assertNotIsInstance(c.exception, asks.OverBudget)

    def test_unknown_and_closed_asks_refuse_exit_2(self):
        with self.assertRaises(Refused) as c:
            asks.gate(self.cfg, self.spec("A-0101-9"))
        self.assertNotIsInstance(c.exception, asks.OverBudget)
        a = self.ask("M")
        asks.close(self.cfg, a)
        with self.assertRaises(Refused) as c:
            asks.gate(self.cfg, self.spec(a, over="chalach: go"))  # --over never opens a closed ask
        self.assertIn("closed; open a new ask", str(c.exception))
        self.assertNotIsInstance(c.exception, asks.OverBudget)

    def test_runs_budget_and_the_message(self):
        a = self.ask("S")
        self.assertEqual(asks.gate(self.cfg, self.spec(a)), {"over": False})
        self.fake_run(a)
        with self.assertRaises(asks.OverBudget) as c:
            asks.gate(self.cfg, self.spec(a), base={"run_id": "R1", "agent": "codex"})
        self.assertEqual(str(c.exception), f"ask {a} (S) used 1/1 runs. To go over, add --over \"<chalach's words>\"")
        ev = self.events("over_budget")[-1]
        self.assertEqual((ev["ask_id"], ev["run_id"], ev["status"], ev["reason"]), (a, "R1", "refused", "used 1/1 runs"))
        out = asks.gate(self.cfg, self.spec(a, over="chalach 18:02: ok one more"), base={"run_id": "R2"})
        self.assertEqual(out["approved"], "chalach 18:02: ok one more")
        ev = self.events("over_budget")[-1]
        self.assertEqual((ev["run_id"], ev["status"], ev["over"]), ("R2", "approved", "chalach 18:02: ok one more"))
        n = len(self.events("over_budget"))
        with self.assertRaises(asks.OverBudget):
            asks.gate(self.cfg, self.spec(a), dry_run=True)  # a dry run writes nothing
        self.assertEqual(len(self.events("over_budget")), n)

    def test_fixes_count_only_parents_in_the_same_ask(self):
        a = self.ask("M")
        first = self.fake_run(a)
        other = self.fake_run(self.ask("M"))
        self.assertEqual(asks.gate(self.cfg, self.spec(a, parent_run_id=first)), {"over": False})  # 1st fix
        self.fake_run(a, parent=first)
        u = asks.usage(self.cfg, a)
        self.assertEqual((u["runs"], u["fixes"]), (2, 1))
        b = self.ask("L")
        r1 = self.fake_run(b)
        self.fake_run(b, parent=r1)
        self.fake_run(b, parent=r1)
        with self.assertRaises(asks.OverBudget) as c:
            asks.gate(self.cfg, self.spec(b, parent_run_id=r1))
        self.assertIn("used 2/2 fixes", str(c.exception))
        self.assertEqual(asks.gate(self.cfg, self.spec(b, parent_run_id=other)), {"over": False})  # not a fix here

    def test_loop_asks_obey_the_same_fix_budget(self):
        a = self.ask("L", pattern="loop")
        prev = self.fake_run(a)
        for _ in range(2):
            prev = self.fake_run(a, parent=prev)
        with self.assertRaisesRegex(asks.OverBudget, "used 2/2 fixes"):
            asks.gate(self.cfg, self.spec(a, parent_run_id=prev))

    def test_parallel_counts_live_runs(self):
        a = self.ask("M")
        live = [{"run_id": "2026-11-05T120000-devfab-general-codex-abcd", "ask_id": a, "kind": "dispatch"}]
        with mock.patch.object(asks.claims, "active", return_value=(live, [])):
            with self.assertRaises(asks.OverBudget) as c:
                asks.gate(self.cfg, self.spec(a))
            self.assertIn("has 1/1 runs live at once", str(c.exception))
            u = asks.usage(self.cfg, a)  # a registered run whose dispatch event is not written yet counts as used
            self.assertEqual((u["runs"], u["live"]), (1, 1))

    def test_wall_time_parks_the_ask(self):
        old = (datetime.now().astimezone() - timedelta(minutes=50)).isoformat(timespec="seconds")
        ledger.append(self.sb.runs, event="ask_open", ask_id="A-0101-1", ts=old, size="S", pattern="solo",
                      title="old ask", words="w", budget=asks.budget_for(self.cfg, "S"))
        with self.assertRaises(asks.OverBudget) as c:
            asks.gate(self.cfg, self.spec("A-0101-1"))
        self.assertIn("/15 min", str(c.exception))
        parks = self.events("ask_park")
        self.assertEqual([(e["ask_id"], e["reason"]) for e in parks], [("A-0101-1", "over time")])
        with self.assertRaises(asks.OverBudget) as c:  # parked now; no second park event
            asks.gate(self.cfg, self.spec("A-0101-1"))
        self.assertIn("is parked (over time)", str(c.exception))
        self.assertEqual(len(self.events("ask_park")), 1)

    def test_wall_time_override_parks_and_keeps_all_the_words(self):
        old = (datetime.now().astimezone() - timedelta(minutes=50)).isoformat(timespec="seconds")
        ledger.append(self.sb.runs, event="ask_open", ask_id="A-0101-1", ts=old, size="S", pattern="solo",
                      title="old ask", words="w", budget=asks.budget_for(self.cfg, "S"))
        words = "One more, with this context: " + "extra context " * 100
        self.assertTrue(asks.gate(self.cfg, self.spec("A-0101-1", over=words))["over"])
        self.assertEqual(self.events("over_budget")[-1]["over"], words.strip())
        self.assertEqual(asks.need(self.cfg, "A-0101-1")["status"], "parked")
        self.assertEqual(self.events("ask_park")[-1]["reason"], "over time")

    def test_review_reasons(self):
        a = self.ask("M")
        with self.assertRaises(Refused) as c:
            asks.gate(self.cfg, self.spec(a, kind="review"))
        self.assertIn("needs --trigger", str(c.exception))
        self.assertNotIsInstance(c.exception, asks.OverBudget)
        self.assertEqual(asks.gate(self.cfg, self.spec(a, kind="review", trigger="leaves-lab")), {"over": False})
        self.fake_run(a, kind="review", agent="claude")
        with self.assertRaises(asks.OverBudget) as c:
            asks.gate(self.cfg, self.spec(a, kind="review", trigger="hardware-or-raw"))
        self.assertIn("used 1/1 reviews", str(c.exception))
        s = self.ask("S")
        for t in ("leaves-lab", "hardware-or-raw", "agent-unsure"):
            with self.assertRaises(Refused) as c:
                asks.gate(self.cfg, self.spec(s, kind="review", trigger=t))
            self.assertNotIsInstance(c.exception, asks.OverBudget)
            self.assertIn("you-asked", str(c.exception))
        self.assertEqual(asks.gate(self.cfg, self.spec(s, kind="review", trigger="you-asked")), {"over": False})
        self.fake_run(s, kind="review", agent="claude")
        with self.assertRaises(asks.OverBudget):
            asks.gate(self.cfg, self.spec(s, kind="review", trigger="you-asked"))
        with self.assertRaises(Refused):
            asks.gate(self.cfg, self.spec(a, kind="dispatch", trigger="you-asked"))  # a trigger is for reviews

    def test_top_tier_needs_words_for_s_and_m(self):
        m, l, race = self.ask("M"), self.ask("L"), self.ask("S", pattern="race")
        with self.assertRaises(asks.OverBudget) as c:
            asks.gate(self.cfg, self.spec(m, tier="top"))
        self.assertIn("top-tier", str(c.exception))
        self.assertTrue(asks.gate(self.cfg, self.spec(m, tier="top", over="chalach: use astra"))["over"])
        self.assertEqual(asks.gate(self.cfg, self.spec(m, tier="top", model_explicit=True)), {"over": False})
        self.assertEqual(asks.gate(self.cfg, self.spec(l, tier="top")), {"over": False})
        self.assertEqual(asks.gate(self.cfg, self.spec(race, tier="top")), {"over": False})

    def test_parked_ask_needs_words(self):
        a = self.ask("M")
        asks.park(self.cfg, a, "waits for the datasheet")
        self.assertEqual(asks.need(self.cfg, a)["status"], "parked")
        with self.assertRaises(asks.OverBudget) as c:
            asks.gate(self.cfg, self.spec(a))
        self.assertIn("is parked (waits for the datasheet)", str(c.exception))
        self.assertTrue(asks.gate(self.cfg, self.spec(a, over="chalach: go on"))["over"])


class TestCards(Base):
    def test_start_card(self):
        st = asks.need(self.cfg, self.ask("M", models="sol,opus", project="fnd_sin", title="SPCM kit -> wiki"))
        card = asks.start_card(st)
        lines = card.splitlines()
        self.assertLessEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith(f"{st['ask_id']}  size M  pattern solo  project fnd_sin"))
        self.assertEqual(lines[1], "Goal: SPCM kit -> wiki")
        self.assertEqual(lines[2], "Models: sol, opus")
        self.assertEqual(lines[3], "Budget: 2 runs incl. 1 fix, 1 review (only with a reason), 1 at a time, 45 min "
                                   "(30 min per run)")
        s = asks.start_card(asks.need(self.cfg, self.ask("S")))
        self.assertIn("no review (only if you ask)", s)
        self.assertEqual(len(s.splitlines()), 3)

    def test_finish_card_merges_the_v4_fields(self):
        a = self.ask("M", title="SPCM kit -> wiki")
        r1 = self.fake_run(a, v4={"plain_summary": ["Saved the kit next to your script.", "Wrote one wiki page."],
                                  "verification": "fake_or_tests", "you_should_know": ["The SPCM sends one pulse per photon."],
                                  "your_move": "Try script 01 on the lab PC.", "later": ["datasheet check"]})
        self.fake_run(a, kind="review", agent="claude", alias="opus",
                      v4={"plain_summary": ["The review agrees."], "verification": "desk_check",
                          "you_should_know": ["The DAQ counts on a counter input.", "A third line that is cut."],
                          "your_move": "nothing", "later": ["3 possible fixes in spcm_counter.py", "datasheet check"]})
        asks.later_add(self.cfg, a, "read the AQRH manual")
        out = asks.close(self.cfg, a)
        card = out["card"].splitlines()
        self.assertLessEqual(len(card), asks.CARD_MAX_LINES)
        self.assertTrue(card[0].startswith(f"{a}  SPCM kit -> wiki   DONE   1 run + 1 review"), card[0])
        text = "\n".join(card)
        self.assertNotRegex(text, RUN_ID_RX)
        self.assertIn("Changed: Saved the kit next to your script.", text)
        self.assertIn("How sure: desk check", text)  # the lowest level of the runs
        self.assertIn("The SPCM sends one pulse per photon.", text)
        self.assertIn("Your move: Try script 01 on the lab PC.", text)
        self.assertIn("Later (3): 1) datasheet check; 2) 3 possible fixes in spcm_counter.py; 3) read the AQRH manual",
                      text)
        self.assertEqual(out["status"], "done")
        acc = self.events("accept")
        self.assertEqual({e["run_id"]: e["accepted"] for e in acc}[r1], True)
        self.assertTrue(all(e["ask_id"] == a for e in acc))

    def test_close_is_idempotent_and_card_has_8_lines_at_most(self):
        a = self.ask("L", title="x" * 300)
        self.fake_run(a, v4={"plain_summary": ["one", "two", "three"], "verification": "hardware",
                             "you_should_know": ["k1", "k2"], "your_move": "m", "later": ["l1"]})
        bad = self.fake_run(a, status="flagged")
        failed = self.fake_run(a, status="failed")
        first = asks.close(self.cfg, a, done="partial", note="ok for now")
        self.assertLessEqual(len(first["card"].splitlines()), 8)
        self.assertIn("PARTIAL", first["card"])
        self.assertIn("1 run flagged by the rule check", first["card"])
        acc = {e["run_id"]: e["accepted"] for e in self.events("accept")}
        self.assertNotIn(bad, acc)       # flagged: chalach decides
        self.assertIs(acc[failed], False)
        again = asks.close(self.cfg, a, done="yes")
        self.assertTrue(again["already"])
        self.assertEqual(again["card"], first["card"])
        self.assertEqual(len(self.events("ask_close")), 1)
        self.assertEqual(len(self.events("accept")), len(acc))
        self.assertEqual(asks.need(self.cfg, a)["status"], "closed")
        with self.assertRaises(Refused):
            asks.close(self.cfg, a, done="maybe")

    def test_default_done_and_old_results_without_v4(self):
        a = self.ask("M")
        self.fake_run(a, status="partial", summary=["Old-style summary line"])
        out = asks.close(self.cfg, a)
        self.assertEqual(out["status"], "partial")
        self.assertIn("Changed: Old-style summary line", out["card"])
        self.assertIn("How sure: not checked", out["card"])
        b = self.ask("M")
        self.assertEqual(asks.close(self.cfg, b)["status"], "not_done")

    def test_answered_closes_the_ask(self):
        a = self.ask("S", title="SPCM wiring answer")
        out = asks.answered(self.cfg, a, "Wire the SPCM TTL out to PFI0.\nCount on ctr0.\nline 3\nline 4")
        self.assertEqual(out["status"], "done")
        self.assertIn("Changed: Wire the SPCM TTL out to PFI0.", out["card"])
        self.assertIn("How sure: desk check", out["card"])
        ev = self.events("answered")[0]
        self.assertEqual(len(ev["note"].splitlines()), 3)
        self.assertEqual(asks.need(self.cfg, a)["status"], "closed")
        with self.assertRaises(Refused):
            asks.answered(self.cfg, a, "again")

    def test_later_list_and_list_asks(self):
        a = self.ask("M")
        self.assertEqual(asks.later_list(self.cfg, a), [])
        self.assertEqual(asks.later_add(self.cfg, a, "item one"), ["item one"])
        self.fake_run(a, v4={"later": ["from the agent", "Item One"]})
        self.assertEqual(asks.later_list(self.cfg, a), ["from the agent", "Item One"])  # no duplicates
        b = self.ask("S")
        asks.close(self.cfg, b)
        self.assertEqual([x["ask_id"] for x in asks.list_asks(self.cfg)], [a, b])
        self.assertEqual([x["ask_id"] for x in asks.list_asks(self.cfg, open_only=True)], [a])
        self.assertEqual(asks.list_asks(self.cfg, since="1d")[0]["usage"]["runs"], 1)
        with self.assertRaises(Refused):
            asks.list_asks(self.cfg, since="soon")
        self.assertIn(a, asks.show(self.cfg, a))


class TestAskCli(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def test_commands(self):
        p = self.sb.ck("ask", "open", "--words", "just use this and note it as wiki for now", "--size", "M",
                       "--title", "SPCM kit -> wiki", "--project", "fnd_sin")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout.splitlines()
        a = out[0]
        self.assertTrue(asks.valid_id(a), out)
        self.assertLessEqual(len(out) - 1, 4)
        self.assertTrue(out[1].startswith(a))
        p = self.sb.ck("ask", "later", a, "fix the yolo default and the auto-review text")  # text, not a flag
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("1) fix the yolo default", p.stdout)
        self.assertIn("1) fix the yolo", self.sb.ck("ask", "later", a).stdout)
        p = self.sb.ck("ask", "park", a, "--reason", "waits for chalach")
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.sb.ck("ask", "list", "--open")
        self.assertIn(f"{a}", p.stdout)
        self.assertIn("parked", p.stdout)
        self.assertEqual(json.loads(self.sb.ck("ask", "list", "--json").stdout)[0]["ask_id"], a)
        p = self.sb.ck("ask", "show", a)
        self.assertIn("chalach's words: just use this and note it as wiki for now", p.stdout)
        self.assertEqual(json.loads(self.sb.ck("ask", "show", a, "--json").stdout)["status"], "parked")
        p = self.sb.ck("ask", "close", a, "--done", "no", "--note", "chalach stopped it")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("NOT DONE", p.stdout)
        p2 = self.sb.ck("ask", "close", a)
        self.assertEqual((p2.returncode, p2.stdout), (0, p.stdout))
        self.assertIn("already closed", p2.stderr)
        b = self.sb.open_ask("S", "quick answer", "how do I wire it?")
        p = self.sb.ck("ask", "answered", b, "--summary", "Use PFI0.")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("DONE", p.stdout)
        for bad in (["ask", "open", "--words", "w", "--size", "XL", "--title", "t"],
                    ["ask", "open", "--words", "", "--size", "S", "--title", "t"],
                    ["ask", "close", "A-1105"], ["ask", "show", "A-0101-77"], ["ask", "park", b, "--reason", "x"]):
            with self.subTest(bad=bad):
                self.assertEqual(self.sb.ck(*bad).returncode, 2)

    def test_free_text_flags_pass_the_ck_guard(self):
        guards.check_ck_argv(["ask", "open", "--words", "do it --yolo, dangerously fast", "--size", "S",
                              "--title", "t", "--because", "all 3 agents, auto"])
        guards.check_ck_argv(["run", "--agent", "codex", "--over", "chalach: ok, bypass the budget, auto"])
        with self.assertRaises(Refused):
            guards.check_ck_argv(["ask", "open", "--yolo"])  # flags are still checked

    def test_main_maps_over_budget_to_exit_7(self):
        env = {"HOME": self.sb.home, "CK_DISPATCH_CONFIG": self.sb.config}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stderr(err), \
                mock.patch.object(asks, "open_ask", side_effect=asks.OverBudget("ask A-1105-1 (S) used 1/1 runs")):
            code = cli.main(["ask", "open", "--words", "w", "--size", "S", "--title", "t"])
        self.assertEqual(code, 7)
        self.assertIn("ck-run: OVER BUDGET: ask A-1105-1 (S) used 1/1 runs", err.getvalue())


if __name__ == "__main__":
    unittest.main()
