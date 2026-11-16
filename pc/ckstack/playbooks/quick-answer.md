# Playbook: quick-answer

**When**: a question, a status, an explanation. chalach says "quick", "short", "for now", "simple" or "no review".
**Trigger**: `quick` (or a plain question with no trigger). **Size** S. **Pattern** solo.
**Lane** `quick`: you answer; also sonnet, composer (one fast run when reading is not enough).
**Read first**: `~/PhD_local/grok-bot-skills/phd-wiki-rules/SKILL.md` (a workspace question: the wiki first).
**Proof needed**: desk check is fine. Say that it is a desk check.

## Steps
1. Open: `ck-run ask open --words "<chalach verbatim>" --size S --title "<the question in your words>" --project <name>`.
2. Read what you need: the wiki first, at most about 10 files. Status questions: `ck-run board`, `ck-run recall <project>`.
3. Answer in at most one paragraph. Give each fact its source tag: paper, datasheet, measured, simulated, chalach, guess.
4. Record: `ck-run ask answered <ask> --summary "<1-3 lines>"`.

## Needs more than reading
More than about 10 files, code to run, or a value to compute: one fast run, read mode.
- Brief `~/PhD_local/agent_runs/<ask>-quick_prompt.md` (ck-mode section 4), at most 2 steps.
- `ck-run launch <ask>-quick run --model sonnet --mode read --workspace devfab|nv|washu --title "<title>" --ask <ask>`
  (or `--model composer`).
- Answer from its summary, then `ck-run ask close <ask> --done yes|partial|no`.

## Done means
- One paragraph, sources named, nothing changed on disk.
- No review. Only if chalach said "check": one review with `--trigger you-asked`.

## Not S
- A number that will leave the lab (slide, paper, email): use `numbers` or `outside-writing`.
- An edit to more than one small file, or a plan: M or L.
- "all 3 agents" from chalach on one question: a race (`skills/race`), still one merged answer.
