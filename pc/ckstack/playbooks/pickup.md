# Playbook: pickup

**When**: a new session, a restart, "what is the progress", `recall <project>`, or `recall` alone.
**Trigger**: `recall`. **Size** S. **Pattern**: you answer, no agent.
**Lane** `quick`. **Read first**: nothing new; the trail is the input.
**Proof needed**: what the ledger and the files say. Check a claim on disk only when it matters.

## Steps
1. After a restart: `ck-run reattach`. It finishes runs whose watcher died. It is safe to run again.
2. `ck-run board`: what needs chalach, what runs, what is done today.
3. With a project: `ck-run recall <project>` (at most 5 capsule lines, one line per ask with one tag, the next move).
   Without a project: `skills/ketchup` (what changed since chalach's last message).
4. Read the router PROGRESS.md for chalach's words and decisions. The ledger holds the run state.
5. Do not redo finished work. The trail is the truth, not something to re-derive.
6. Record: `ck-run ask open --words "<chalach verbatim>" --size S --title "Catch up on <project>" --project <project>`,
   then `ck-run ask answered <ask> --summary "<1-3 lines>"`.

## Reply
- Capsule: at most 5 lines.
- Asks: one line each with one tag: `running`, `done`, `parked`, `needs you`, `later`.
- Waits for you: each item with your pick.
- Next move: one line.

## Done means
- chalach knows where things stand in one screen. Nothing was redone.
