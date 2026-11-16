---
name: recall
description: Catch chalach up on one project in at most 5 lines, then one line per ask with one tag, then the next move. Word "recall <project>".
disable-model-invocation: true
---

# recall

**Word**: `recall <project>`, e.g. `recall fnd_sin`. **Size** S. You answer; no agent. (`recall` alone: `skills/ketchup`.)

## Steps
1. After a restart: `ck-run reattach` (finishes runs whose watcher died; safe to run again).
2. `ck-run recall <project>`: up to 5 capsule lines, one line per ask with one tag, then the next move.
3. `ck-run board`: what needs chalach right now.
4. Add only what the ledger cannot know: chalach's words and decisions from the router PROGRESS.md.
5. Do not redo work. Trust the trail; check a claim on disk only when the next move depends on it.
6. Project name unknown: `ck-run ask list --since 7d` shows the names in use.

## Reply
- Capsule: at most 5 lines. What this work is, and where it stands.
- Asks: one line each, one tag: `running`, `done`, `parked`, `needs you`, `later`.
- Waits for you: each item, with your pick.
- Next move: one line, concrete.

## Record
`ck-run ask open --words "<chalach verbatim>" --size S --title "Catch up on <project>" --project <project>`, then
`ck-run ask answered <ask> --summary "<1-3 lines>"`.
