---
name: teach
description: Explain a piece of work so chalach really understands it and can defend it to the advisor and committee. Word "teach".
disable-model-invocation: true
---

# teach

**Word**: `teach`. **Size** S. You answer; read the work's own trail first.
The bots should leave chalach knowing more, not less.

## Steps
1. Pick the 1 to 3 things chalach should walk away with. Choose from why they ask (about to change it, present it,
   debug it, new to it).
2. Start with a plain definition, then tie it to chalach's case ("in your SPCM setup, ...").
3. Give the smallest complete answer first: two or three sentences. Stop. Go deeper only when chalach asks.
4. Three or more moving parts: draw a short series of block diagrams. Each one adds one part to the last.
5. Use the trail: the `you_should_know` lines and `decisions[]` of the runs (`ck-run show <run>`).
6. State the real mechanism. No metaphor in its place.
7. No quizzes, no "the key insight", no wall of text. Plain words (`skills/plain`).

## Weekly "what I learned"
On request: collect the "You should know" lines from the week's finish cards
(`ck-run ask list --since 7d`, then `ck-run ask show <ask>`), grouped by project. One page.

## Record
`ck-run ask open ... --size S`, then `ck-run ask answered <ask> --summary "<1-3 lines>"`.
