---
name: panel
description: Two or three reviewers with different lenses check one run at the same time; only findings with proof survive. Word "deep".
disable-model-invocation: true
---

# panel

**Word**: `deep`. **Use**: writing that leaves the lab, L steps, or when chalach says deep.
**Lenses**: claude (Opus) = physics and claims; codex (Sol) = code and numbers; cursor (Grok) = sources.
**Budget**: every reviewer is one review. An L ask holds 2. For 3, put `reviews=3` in the plan's budget line, or ask
chalach to go over (`--over "<chalach's words>"`).

## Steps
1. Leave out the agent that did the run (ck-run refuses a review by the same agent).
2. Start: `ck-run panel <run> --by codex,claude,cursor --trigger <reason> --ask <ask>`
   Without `--focus` each reviewer gets its own lens; a `--focus` text replaces the lens for all of them.
   For a codex run, drop codex: `--by claude,cursor`. One review per agent is launched (slug `<ask>-panel-<agent>`).
   Watch each `.out`.
3. The reviewers run on the tier the ask's size sets. chalach said `deep` and wants the top tier: launch single
   reviews instead, e.g. `ck-run launch <ask>-review-claude review <run> --by claude --model opus-max --trigger <reason> --ask <ask> --focus "<lens>"`
   (and `--by codex --model astra`).
4. Merge:
   - verdicts first (AGREE or DISAGREE per reviewer);
   - keep only findings with proof: a command and its output, a datasheet page, a paper figure;
   - merge findings with one root cause;
   - count the dropped findings and say why.
5. A proven finding that blocks the goal: one fix run inside the budget. The rest: `ck-run ask later <ask> "<item>"`.

## Never
- Pass on a finding with no proof. Start fix runs from "might be a problem".
