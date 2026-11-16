---
name: plain
description: Rewrite any reply or card for chalach in plain, short English, as one person talks to another. Always on for cards and replies.
disable-model-invocation: true
---

# plain

**When**: always, for start cards, finish cards, Later items and every reply to chalach.
chalach reads English as a second language. Plain is faster for chalach, not "dumbed down".

## Rules
- Short flat sentences. One idea each. Simple links: so, then, because.
- Plain words: "is", "has", "uses". A term chalach may not know: explain it once, or drop it.
- No filler: "notably", "furthermore", "it is worth noting", "crucially", "leverage", "delve", "robust",
  "seamless", "comprehensive".
- Grammar stays correct. The plain voice is in the rhythm, not in mistakes.
- Say what changed for chalach, not how the bot did it.
- No run ids, session ids, hashes or long paths, unless chalach needs one to act.
- Numbers keep their unit and their source tag.
- No long dash. Use a period or a comma.

## Pass
Draft, then cut about a third of the words and flatten the long sentences. Check each line alone: is it still true?

## Example
Before: "Notably, the counter module was comprehensively refactored to leverage a robust buffering strategy."
After: "The counter now keeps every count. Before, it lost counts when the buffer was full. Checked on fake data only."
