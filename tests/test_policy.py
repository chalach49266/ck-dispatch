"""v4 rules and policy (BUILD_SPEC_v4 10.8): config/policy.json, ck-run rules, ck-run rules --check.

The drift check must find the REAL current drift (git push / pip install allowed by chalach on 2026-11-04, still denied
in config/claude-guard.base.json) and must never change that file. The real guard file is only read; every other
case uses a copy in the test sandbox.
"""
import hashlib
import json
import os
import re
import unittest
from unittest import mock

from helpers import REPO
from test_views import Fixture, FixtureCase, cli_registers

from ckdispatch import policy
from ckdispatch.config import CLAUDE_GUARD_BASE, Config
from ckdispatch.errors import Refused

SPEC = os.path.join(os.path.dirname(REPO), "BUILD_SPEC_v4.md")  # staging only; skipped when it is not there
REQUIRED_IDS = ["D1", "D2", "D3", "D4", "auto-mode", "cursor-allow-all", "agy-edit", "agy-auto-dirs", "auto-readonly",
                "parallel-runs", "auto-claims", "git-push-pip", "word-only-notes", "web-search", "post-review-off",
                "sizes-budgets", "fleet-lanes", "triggers"]


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def flat(text):
    """Whitespace collapsed, markdown quote markers ('> ' at a line start) removed: a quote may wrap lines."""
    return " ".join(re.sub(r"(?m)^\s*>\s?", "", text).split())


class TestPolicyFile(unittest.TestCase):
    def test_shape_ids_dates(self):
        decisions = policy.load()
        ids = [d["id"] for d in decisions]
        self.assertEqual(len(ids), len(set(ids)))
        for rid in REQUIRED_IDS:
            self.assertIn(rid, ids)
        for d in decisions:
            for k in policy.KEYS:
                self.assertIsInstance(d[k], str, (d["id"], k))
                self.assertTrue(d[k].strip(), (d["id"], k))
            self.assertRegex(d["date"], r"^2026-11-0[2-5]$")
        self.assertEqual([d["date"] for d in decisions], sorted(d["date"] for d in decisions))

    def test_words_are_quoted_verbatim(self):
        """chalach's words appear exactly (spaces aside) in README.md, PROGRESS.md or the build spec; words that no
        source quotes are marked UNKNOWN, never made up."""
        texts = []
        for p in (os.path.join(REPO, "README.md"), os.path.join(REPO, "PROGRESS.md"), SPEC):
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    texts.append((p, flat(f.read())))
        checked = 0
        for d in policy.load():
            if d["words"].startswith("UNKNOWN"):
                continue
            hits = [p for p, t in texts if flat(d["words"]) in t]
            if not hits and "BUILD_SPEC" in d.get("source", "") and not os.path.isfile(SPEC):
                continue
            self.assertTrue(hits, f"{d['id']}: words not found verbatim: {d['words']!r}")
            checked += 1
        self.assertGreaterEqual(checked, 8)
        by = {d["id"]: d for d in policy.load()}
        self.assertEqual(by["git-push-pip"]["words"], "yes you can change the ck-run to treat a word only metion and "
                                                      "note and you can always allow those git push and pip install")
        self.assertEqual(by["post-review-off"]["words"],
                         "Seventh no need review, since those one are for simple stupid task anyways.")
        self.assertTrue(by["D1"]["words"].startswith("UNKNOWN"))

    def test_bad_files_are_refused(self):
        fx = Fixture()
        try:
            p = os.path.join(fx.sb.tmp, "policy.json")
            for body in ("{", "{}", '[{"id": "x"}]', '[{"id":"a","date":"d","words":"w","rule":"r","enforced_by":"e"},'
                                                      '{"id":"a","date":"d","words":"w","rule":"r","enforced_by":"e"}]'):
                with open(p, "w") as f:
                    f.write(body)
                with self.assertRaises(Refused, msg=body):
                    policy.load(p)
            with self.assertRaises(Refused):
                policy.load(os.path.join(fx.sb.tmp, "missing.json"))
        finally:
            fx.close()


class PolicyCase(FixtureCase):
    def guard_copy(self, drop=(), add_deny=(), drop_allow=()):
        with open(CLAUDE_GUARD_BASE) as f:
            g = json.load(f)
        g["permissions"]["deny"] = [x for x in g["permissions"]["deny"] if not any(re.match(d, x) for d in drop)]
        g["permissions"]["deny"] += list(add_deny)
        g["permissions"]["allow"] = [x for x in g["permissions"].get("allow", []) if x not in drop_allow]
        p = os.path.join(self.fx.sb.tmp, "guard.json")
        with open(p, "w") as f:
            json.dump(g, f)
        return p

    def set_review(self, enabled: bool):
        with open(self.fx.sb.config) as f:
            c = json.load(f)
        c.setdefault("post_review", {})["enabled"] = enabled
        with open(self.fx.sb.config, "w") as f:
            json.dump(c, f)
        self.fx.cfg = Config()


CLEAN = (r"^Bash\(git push", r"^Bash\(pip3? install", r"^Bash\(uv pip install")
HEADER_OK = "ROLE: x\nHARD RULES: no Tidy3D or Flexcompute cloud jobs; do not open credential files.\nWEB: your built-in " \
            "web search and web fetch tools are allowed (chalach 2026-11-05)."


class TestDrift(PolicyCase):
    def test_real_guard_drift_is_found_and_the_file_is_not_changed(self):
        before = sha(CLAUDE_GUARD_BASE)
        self.set_review(False)
        lines, skipped = policy.drift(policy.Ctx(self.fx.cfg), policy.load())
        texts = [policy.drift_line(x) for x in lines]
        push = [t for t in texts if t.startswith("DRIFT git-push-pip (2026-11-04): git push is allowed") and
                "config/claude-guard.base.json denies Bash(git push:*)" in t]
        pip = [t for t in texts if "config/claude-guard.base.json denies Bash(pip install:*)" in t]
        self.assertEqual(len(push), 1, texts)
        self.assertEqual(len(pip), 1, texts)
        self.assertIn("Bash(pip3 install:*)", pip[0])
        code, out, err = self.fx.cli("rules", "--check")
        self.assertEqual(code, 1, out + err)
        self.assertIn("DRIFT git-push-pip (2026-11-04): git push is allowed (2026-11-04), but "
                      "config/claude-guard.base.json denies Bash(git push:*)", out)
        self.assertEqual(sha(CLAUDE_GUARD_BASE), before, "rules --check must never change claude-guard.base.json")

    def test_no_drift_exit_0_when_code_and_config_agree(self):
        self.set_review(False)
        clean = self.guard_copy(drop=CLEAN)
        three = {k: policy.CHECKS[k] for k in ("git-push-pip", "web-search", "post-review-off")}
        with mock.patch.object(policy, "CLAUDE_GUARD_BASE", clean), \
                mock.patch.object(policy.Ctx, "prompt_text", lambda self: HEADER_OK), \
                mock.patch.object(policy, "CHECKS", three):
            code, out, err = self.fx.cli("rules", "--check")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out.strip(), "No drift: the code and config match the decisions.")

    def test_prompt_hard_rules_drift(self):
        self.set_review(False)
        ctx = policy.Ctx(self.fx.cfg, guard_path=self.guard_copy(drop=CLEAN))
        with mock.patch.object(policy.Ctx, "prompt_text", lambda self: "HARD RULES: no git push, deploy or upload; "
                               "no git clone, venv, pip/npm install or node_modules inside OneDrive; x\nWEB: allowed"):
            lines, _ = policy.drift(ctx, policy.load(), only=["git-push-pip"])
        texts = [x["text"] for x in lines]
        self.assertEqual(texts, ['git push is allowed (2026-11-04), but the agent prompt says "no git push, deploy or '
                                 'upload" (prompt.py HARD RULES)',
                                 'pip install is allowed (2026-11-04), but the agent prompt says "no git clone, venv, '
                                 'pip/npm install or node_modules inside OneDrive" (prompt.py HARD RULES)'])

    def test_web_and_post_review_drift(self):
        self.set_review(True)
        g = self.guard_copy(add_deny=["WebFetch"], drop_allow=["WebSearch"])
        ctx = policy.Ctx(self.fx.cfg, guard_path=g)
        lines, _ = policy.drift(ctx, policy.load(), only=["web-search", "post-review-off"])
        texts = [x["text"] for x in lines]
        self.assertTrue(any("guard.json denies WebFetch" in t for t in texts), texts)
        self.assertTrue(any("does not allow WebSearch" in t for t in texts), texts)
        self.assertTrue(any(t.startswith("the post-run review is off by decision, but") and
                            "post_review.enabled = true" in t for t in texts), texts)
        self.set_review(False)
        lines, _ = policy.drift(policy.Ctx(self.fx.cfg, guard_path=self.guard_copy()), policy.load(),
                                only=["web-search", "post-review-off"])
        self.assertEqual(lines, [])

    def test_budgets_drift_and_skips(self):
        with open(self.fx.sb.config) as f:
            c = json.load(f)
        c.setdefault("asks", {}).setdefault("budgets", {}).setdefault("M", {})["runs"] = 5
        with open(self.fx.sb.config, "w") as f:
            json.dump(c, f)
        self.fx.cfg = Config()
        ctx = policy.Ctx(self.fx.cfg)
        with mock.patch.object(policy.Ctx, "parser", lambda self: None):
            lines, skipped = policy.drift(ctx, policy.load(), only=["sizes-budgets", "s-asks-self", "auto-mode"])
        self.assertEqual(lines, [])  # no parser: these checks cannot run; that is not drift
        self.assertEqual(sorted(s["id"] for s in skipped), ["auto-mode", "s-asks-self", "sizes-budgets"])
        boom = {"git-push-pip": lambda ctx, d: 1 / 0}
        with mock.patch.object(policy, "CHECKS", boom):
            lines, skipped = policy.drift(ctx, policy.load())
        self.assertEqual(lines, [])
        self.assertIn("the check failed: ZeroDivisionError", skipped[0]["why"])
        # the budget comparison itself, with a review command whose --trigger has no fixed choices
        d = next(x for x in policy.load() if x["id"] == "sizes-budgets")
        with mock.patch.object(policy, "_sub", lambda parser, name: object() if name == "review" else None), \
                mock.patch.object(policy, "_opt", lambda parser, flag: mock.Mock(choices=None)):
            found = policy._sizes(ctx, d)
        self.assertIn(f"ask budget M.runs is 5 in {self.fx.sb.config}, approved 2", found)

    def test_fleet_lanes(self):
        d = next(x for x in policy.load() if x["id"] == "fleet-lanes")
        ctx = policy.Ctx(self.fx.cfg, models_path=os.path.join(self.fx.sb.tmp, "none.json"))
        self.assertIn("is missing or not JSON", policy._fleet(ctx, d)[0])
        m = {"aliases": {"sol": {}, "opus": {}}, "lanes": {"quick": {"worker": "self", "also": ["ghost"]}},
             "race_default": ["sol"]}
        p = os.path.join(self.fx.sb.tmp, "models.json")
        with open(p, "w") as f:
            json.dump(m, f)
        found = policy._fleet(policy.Ctx(self.fx.cfg, models_path=p), d)
        self.assertTrue(any("has no lane lab-code" in t for t in found), found)
        self.assertIn("lane quick names ghost, which is not a model alias", found)
        self.assertIn("race_default is ['sol'], approved ['astra', 'opus', 'grok']", found)


class TestRulesTable(PolicyCase):
    def test_table_from_code_and_config(self):
        self.set_review(False)
        code, out, err = self.fx.cli("rules")
        self.assertEqual(code, 0, err)
        head = next(ln for ln in out.splitlines() if ln.strip().startswith("claude"))
        self.assertEqual(head.split(), ["claude", "codex", "cursor", "agy"])
        push = next(ln for ln in out.splitlines() if ln.startswith("git push"))
        self.assertRegex(push, r"^git push\s+NO \(denied\)\s+yes \(a note\)\s+yes \(a note\)\s+yes \(a note\)$")
        review = next(ln for ln in out.splitlines() if ln.startswith("post-run review"))
        self.assertRegex(review, r"^post-run review\s+no\s+no\s+off\s+off$")
        web = next(ln for ln in out.splitlines() if ln.startswith("web search/fetch"))
        self.assertRegex(web, r"^web search/fetch\s+yes\s+yes\s+yes\s+yes$")
        self.assertIn("nv: writes only in NV Project/, AQS/", out)
        self.assertIn("washu: writes only in <course>/solutions/ and <course>/prep/", out)
        # DRIFT comes before the decisions (the mod shows only the first ~60 lines)
        self.assertLess(out.index("DRIFT"), out.index("DECISIONS ("))
        self.assertIn('2026-11-04  git-push-pip: git push and pip install', out)
        self.assertIn('"yes you can change the ck-run to treat a word only metion', out)  # cut at 60 chars
        self.set_review(True)
        with mock.patch.object(policy, "CLAUDE_GUARD_BASE", self.guard_copy(drop=CLEAN)):
            code, out, _ = self.fx.cli("rules")
        push = next(ln for ln in out.splitlines() if ln.startswith("git push"))
        self.assertRegex(push, r"^git push\s+yes\s+yes \(a note\)")
        review = next(ln for ln in out.splitlines() if ln.startswith("post-run review"))
        self.assertRegex(review, r"^post-run review\s+no\s+no\s+yes, auto runs\s+yes, auto runs$")

    def test_models_rows_and_json(self):
        code, out, err = self.fx.cli("rules", "--json")
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        self.assertEqual(sorted(d), ["agents", "decisions", "drift", "skipped", "workspaces"])
        agents = {a["agent"]: a for a in d["agents"]}
        if os.path.isfile(policy.MODELS):
            with open(policy.MODELS) as f:
                m = json.load(f)
            self.assertEqual(agents["claude"]["model_s"], m["tiers"]["claude"][m["size_tier"]["S"]])
            self.assertEqual(agents["codex"]["model_ml"], m["tiers"]["codex"][m["size_tier"]["M"]])
        code, out, _ = self.fx.cli("rules", "--json", "--check")
        self.assertEqual(code, 1 if json.loads(out)["drift"] else 0)


class TestViaCkRun(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        if not cli_registers("rules"):
            self.fx.close()
            self.skipTest("cli._parser does not register ckdispatch.policy yet (E1 integration)")

    def tearDown(self):
        self.fx.close()

    def test_rules_check_exit_1_on_the_real_drift(self):
        before = sha(CLAUDE_GUARD_BASE)
        p = self.fx.sb.ck("rules", "--check")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("denies Bash(git push:*)", p.stdout)
        p = self.fx.sb.ck("rules")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("WHAT EACH AGENT MAY DO NOW", p.stdout)
        self.assertEqual(sha(CLAUDE_GUARD_BASE), before)


if __name__ == "__main__":
    unittest.main()
