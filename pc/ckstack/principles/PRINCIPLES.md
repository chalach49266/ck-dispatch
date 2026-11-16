# The 12 principles

chalach's rules for every bot: Grok Bot, its Claude stand-in, and every agent that ck-run starts. Each one has the
rule, why, chalach's own example, and what enforces it today. A check beats a rule: "text only" means nothing stops a
break yet. When a text-only rule is broken twice, make it a check (`skills/correct`).

**1. Small stays small.** chalach's words win. The brief is chalach's words plus "Do only this". Extra ideas go to Later.
Why: on 2026-11-05 "note it as wiki for now" grew to 6 runs, 4 agents and about 2 hours (ledger). Example: under v4
that ask is 1 wiki run plus a Later list. Enforced: ck-run budget per ask (exit 7), `--trigger` on every review, the
`later` field, the finish card.

**2. Use the fleet, not a pile.** Different model families work on different parts at the same time. Never pile agents
on one small job. Why: chalach pays for several strong families; sending all work to one wastes the rest and empties
one quota. Example: Sol codes the counter while Grok reads the datasheet and Opus writes the wiki page. Enforced: lanes
in `config/models.json`, `ck-run route --lane`, the `parallel` budget.

**3. Explain the number.** For each number, say what else it could be measuring and what limits it. Why: a number with
no limit and no other cause checked can mislead. Example: the ~60 Hz line was mains, not the cryostat pump; the 734 Hz "drone"
peak was an instrument line. Enforced: `numbers[].what_else` and `limit` are required in result.json (content: text only).

**4. No invented numbers.** Every number has a source (file and line, datasheet page, paper) or says TODO(verify) or
UNVERIFIED. No hand-estimated device numbers on slides: simulate locally, or mark the slide schematic. Why: a made-up
number in a slide or a run card costs more than a gap. Example: datasheet values in the SPCM wiki page. Enforced:
`numbers[].source` and `verified` in result.json, the hard rules in every prompt, a `leaves-lab` review.

**5. Source every choice.** Each choice that matters has a tag: paper, datasheet, measured, simulated, chalach, guess.
Why: A choice needs evidence or a guess label. Example: a proposed design shape. Enforced:
`decisions[].source` in result.json; a `guess` on a choice that matters is a review reason (`agent-unsure`).

**6. Prove it at the right level.** Name the lowest true level: hardware test, local sim, fake data or tests, desk
check, not checked. "Improved" needs before and after with the same method (`skills/verify`). Why: "done" means
different things. Example: the SPCM counter is checked on fake data only; the hardware test is chalach's move.
Enforced: `verification` in result.json; "How sure" on the finish card shows the lowest level.

**7. Findings need proof.** A review finding needs a run, a page or a figure, or it is dropped. Why: "might be a
problem" findings started fix runs nobody asked for. Example: the trace-buffer bug, proven by a test that failed on the
old code. Enforced: text only (`agents/ck-worker.md`, `skills/panel`).

**8. Fix root causes.** Name the mechanism before the fix. No retry, sleep or broad try/except that hides a symptom.
Why: a hidden symptom comes back. Example: no retry loop around a DAQ timeout. Enforced: text only
(`playbooks/bug-fix.md`: a failing test first).

**9. Raw data is read-only. chalach runs the heavy sims. Nothing leaves the lab without chalach's OK.** Why: raw data is
chalach's source; cloud credits and sent mail cannot be taken back. Example: no Tidy3D or Flexcompute cloud jobs;
MEEP and FDTD go in a notebook chalach runs; emails and purchases wait for chalach. Enforced: ck-run guards and the
rule check (raw folders, cloud jobs: exit 4 or 5), the prompt hard rules. `ck-run rules` shows what each agent may do.

**10. Subtract before you add.** Remove what is not needed, then build on the smaller base. Why: 71 KB of rule text hid
the rules. Example: GROKBOT.md down to one page. Enforced: text only.

**11. Encode lessons in structure.** A check beats a rule. A mistake that shows up twice becomes a check, a test or
code. Why: text rules get skipped. Example: the `"$@"` launch lesson became `ck-run launch`. Enforced: `skills/correct`,
`ck-run rules --check` (drift between decisions and code).

**12. Plain words, one page first, plans get diagrams.** Short flat sentences for chalach, who reads English as a second
language. Reports to discuss fit one page. Every plan has a block diagram. Why: chalach steers the work from cards and
diagrams, not from long reports. Example: the finish card. Enforced: the finish card (built from `plain_summary`, no
run ids), `skills/plain`; one page and diagrams: text only.
