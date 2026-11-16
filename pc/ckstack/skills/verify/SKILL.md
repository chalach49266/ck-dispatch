---
name: verify
description: Check an "improved", "fixed" or "lower noise" claim with before and after, the same method, and one verdict. Used inside playbooks.
disable-model-invocation: true
---

# verify

**Word**: none; playbooks use it. Any claim that something got better needs before and after, done the same way.
Examples: the noise floor before and after a shield; the Rabi frequency before and after a loop change; liftoff at
two doses.

## Steps
1. Write the claim so it can fail: the condition, the metric, the threshold.
2. Pick the smallest local check that could prove it wrong.
3. Baseline: the old state (the old code, the last run, the old dose). Same data, same settings.
4. Treatment: the new state, with the same command.
5. Compare the raw outputs (numbers, plots, test output). Keep both files.
6. Give one verdict:
   - VERIFIED: the change is larger than the run-to-run spread.
   - NOT VERIFIED: no change, or the wrong way.
   - INCONCLUSIVE: the spread is larger than the change, or the method changed.

## Proof level
Report the lowest true level for `verification`: `hardware`, `local_sim`, `fake_or_tests`, `desk_check`,
`not_checked`. A reviewer may lower it, never raise it. A unit test shows the code path, not that the instrument works.
