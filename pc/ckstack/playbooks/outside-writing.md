# Playbook: outside-writing

**When**: anything that leaves the lab: VC and AQS material, slides, a paper, a patent, emails, quotes, purchases.
**Trigger**: `do`, or `deep` for a panel. **Size** L (a short email: M). **Pattern** solo + a review; panel on `deep`.
**Lane** `writing`: opus drafts; also astra, grok; sol checks claims and numbers; grok checks sources.
Opus edits only `~/PhD_local`: it drafts there, or astra or grok write the file inside a OneDrive workspace.
**Read first**: `~/PhD_local/grok-bot-skills/phd-wiki-rules/SKILL.md` (AQS material lives in the NV workspace).
**Proof needed**: every number is measured, simulated locally, or from a paper or datasheet, with its reference.
**Review**: always one, `--trigger leaves-lab`.

## Steps
1. L: a plan card first (outline, which numbers, their sources, a block diagram). chalach OKs, then
   `ck-run ask open ... --size L --plan <file>`.
2. Opus drafts, one page first.
3. Review: `ck-run launch <ask>-review-codex review <run> --by codex --model sol --trigger leaves-lab --ask <ask> --focus "every number and claim: what is its source?"`
   (codex wrote it: review with `--by cursor --model grok` instead). chalach said `deep`: `skills/panel`.
4. Nothing is sent. chalach sends, posts, submits and buys.

## chalach's rules for writing
- A report or plan to discuss: ONE page first (boxed sections, short labels, short source tags). A long study only if
  asked.
- Update decks: a summary version: a title and about 5 slides; titles of 2 to 5 words; one figure per slide; the
  finding in a one-line caption. Keep the detailed deck too.
- VC slides teach, they do not pitch: a calm tone, the physics chain in plain words, no ask and no competitor names
  on slides (those go in the speaker notes).
- No modelled image that could pass for data on a VC slide. Say the claim in words; model numbers go in the notes.
- No hand-estimated device numbers on slides: run a local sim (a notebook in the deck folder) or mark it schematic.
- A redo is a new file (v2). Never overwrite or delete the old one. No "what went wrong" section in the new one.
- Patent: copy every figure and its source data into the manuscript folder, with a provenance list.
- Quote emails: short paste-ready text, then a "why we ask" table, then a check of every assumption the decision
  rests on.

## Done means
- One page first. Every number and claim has a source tag. Nothing was sent.
- `your_move` says what chalach must check or send.
