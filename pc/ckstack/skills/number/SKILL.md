---
name: number
description: Explain one number. Say what else it could be measuring, what limits it, and how sure it is. Word "number".
disable-model-invocation: true
---

# number

**Word**: `number`. **Size** S. **Playbook**: `playbooks/numbers.md` (an analysis is M).
Example: check whether a spectral peak is interference or the signal you intended to measure.

## Steps
1. Write the decision the number feeds, and the noise floor, before trusting the number.
2. Where it came from: the file and line or cell, the method, the run count, the spread.
3. What else could it be measuring? Rule out each one with evidence:
   mains (~60 Hz and harmonics), an instrument or aliased line, a gain or unit error, a cached or skipped step,
   a setting left on default, noise larger than the effect.
4. What limits it? Why is it not twice as good? Name the bound: the instrument, the linewidth, the fit, the noise,
   a datasheet limit. A guess from reading code is not a limit.
5. Measured or modelled? Say it plainly.

## Result fields
Each number in `numbers[]`: value, unit, source (file and line or cell, or UNVERIFIED), verified, `what_else`,
`limit`. Never invent a number: TODO(verify).

## Reply
The number with its unit and source. What else it could be (ruled out or still open). What limits it. How sure
(the proof level). At most one paragraph.

## Record
`ck-run ask open ... --size S`, then `ck-run ask answered <ask> --summary "<1-3 lines>"`. Needs code to run: one fast
run (`playbooks/quick-answer.md`, "Needs more than reading").
