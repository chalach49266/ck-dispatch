---
name: correct
description: When the same mistake shows up twice, make it impossible with a check or code instead of more text, and record which check enforces which rule. Use when chalach corrects the bots twice for one thing.
disable-model-invocation: true
---

# correct

**When**: chalach corrects the bots twice for the same thing, or `skills/reflect` finds a repeat. A check beats a rule.
Example: the `"$@"` launch lesson became `ck-run launch`, which writes the `.sh` and the `.out` itself.

## Steps
1. Name the mistake class and its two cases: ask ids, dates, chalach's words.
2. Fix it at the highest level that works:
   1. Remove the wrong way: one supported path (as `ck-run launch` replaced hand-written launch files).
   2. A check that fails and names the fix: a ck-run guard, a mod gate, a schema field.
   3. A test that fails when the behaviour breaks.
   4. Text in a rule file, last, only for judgement calls.
3. Prove the new check fails on the real past mistake (rerun that case).
4. ck-dispatch, the grokbot mod and rule files are protected: write the change as a proposal (a task file for one
   M ask), and chalach OKs it before any agent touches them.
5. Add or update the row in the "Rule and what enforces it" table of `~/PhD_local/ckstack/README.md`. Drop a rule
   once its mistake cannot happen anymore.

## Reply
Each class: the evidence, the level you picked, and why a higher level did not work. One page.
