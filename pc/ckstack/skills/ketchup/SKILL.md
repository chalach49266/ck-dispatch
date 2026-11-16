---
name: ketchup
description: Tell chalach what changed since their last message and list every call waiting on them, in plain words. Word "recall" alone, or "catch me up".
disable-model-invocation: true
---

# ketchup

**Word**: `recall` with no project, or "catch me up", "what did I miss". **Size** S. Read only; start nothing.

## Gather
1. `ck-run board`: NEEDS YOU, RUNNING, DONE TODAY.
2. `ck-run digest --since 1d`: per project, what is done, how sure, what is open.
3. Your own notes since chalach's last message (finish cards, Later items, questions you relayed).
4. A call waiting on chalach = a choice only chalach can make: an approval, going over a budget, a Later item, a
   flagged run, an andon to clear. Work you can still do yourself is not a call.

## Reply
- First line: what waits for chalach, or "Nothing waits for you."
- What happened: under 100 words. One line per ask chalach made: done, running, failed or not checked, said the
  way you would say it out loud.
- Each call: about 30 words. The call, your pick, the evidence for the pick, the risk (also when the risk is low).
  Never drop a call to save words.
- No run ids, hashes, paths or test counts, unless chalach needs one to act.

## Record
`ck-run ask open --words "<chalach verbatim>" --size S --title "Catch up"`, then
`ck-run ask answered <ask> --summary "<1-3 lines>"`.
