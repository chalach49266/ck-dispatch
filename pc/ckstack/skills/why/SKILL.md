---
name: why
description: Find where a choice came from and show its source tag (paper, datasheet, measured, simulated, chalach, guess). Word "why".
disable-model-invocation: true
---

# why

**Word**: `why`. **Size** S. **Playbook**: `playbooks/research-scout.md` when the web or many files are needed.
Show whether each choice comes from evidence or a guess.

## Steps
1. Pin the choice: what was chosen, where (file, slide, design, run card), and when.
2. Read the trail first: `decisions[]` in the run's result.json (`ck-run show <run>`, `ck-run thread <run>`),
   the wiki, `git log` of the file, the router PROGRESS.md (chalach's words).
3. Tag it:
   - `paper`: DOI plus figure or equation.
   - `datasheet`: document plus page.
   - `measured`: the data file.
   - `simulated`: the notebook or solver output.
   - `chalach`: their words and the date.
   - `guess`: the agent chose it with no source.
4. No source found: say "no source found; this was a guess". Do not invent a reason after the fact.
5. Needs the web or more than about 10 files: one scout run (`playbooks/research-scout.md`).

## Reply
The choice, its tag and reference, how sure. A guess on a choice that matters: offer a check as a Later item
(`ck-run ask later <ask> "<check>"`) or a new ask. Do not start it.

## Record
S asks: `ck-run ask open ... --size S` then `ck-run ask answered <ask> --summary "<1-3 lines>"` (ck-mode section 3).
