---
name: reflect
description: Once a week, find what the bots should learn from the week's asks and propose at most 12 changes for chalach to approve. Weekly, or when chalach says "reflect".
disable-model-invocation: true
---

# reflect

**When**: once a week, or when chalach says "reflect". Read only. You propose; chalach decides each item.

## Gather
1. `ck-run digest --since 7d`: per project, asks done, runs, agent minutes per model, lowest proof level, open items.
2. `ck-run scoreboard --since 30d`: per lane, model and agent: runs, ok rate, accept rate, median minutes, fix rounds.
3. The week's finish cards (`ck-run ask list --since 7d`), over-budget stops (`ck-run board`), andons
   (`ck-run andon list`), and chalach's corrections in the router PROGRESS.md.

## Find lessons
- A mistake that happened twice (that is a class, not a one-off).
- A lane where a model does better or worse than its neighbour (accept rate, minutes, fix rounds). Use the real
  counts from the scoreboard; too few runs means "not enough data", not a ranking.
- A budget that stopped good work, or one that let a small ask grow.

## Propose
At most 12 items, one page, as a table: lesson | evidence (ask ids, counts) | change | where (file).
- Prefer a check over a rule: code, a test or config first; text last (`skills/correct`).
- Model changes are a proposed line for `~/PhD_local/ck-dispatch/config/models.json` lanes. Never edit it yourself.
- Approved checks go through `skills/correct`. Rejected items are not proposed again without new evidence.

## Never
- Turn a one-off into a rule. Invent counts.
