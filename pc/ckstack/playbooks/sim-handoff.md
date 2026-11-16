# Playbook: sim-handoff

**When**: a new or redone simulation: MEEP, Tidy3D, openEMS, Palace, Qucs-S, Lumerical. The agent prepares it.
chalach runs it.
**Trigger**: `do` (one notebook, M) or `plan` (a campaign, L). **Pattern** solo.
**Lane** `lab-code`: sol writes the notebook; opus checks the physics, only with a reason.
**Read first** (route first, then the solver skill):
`~/PhD_local/grok-bot-skills/microwave-solver-selection/SKILL.md` (microwave) or
`~/PhD_local/grok-bot-skills/photonics-tool-selection/SKILL.md` (photonics), then one of
`~/PhD_local/grok-bot-skills/{tidy3d,meep,openems,palace,qucs-s}/SKILL.md`.
**Proof needed**: desk check until chalach runs it. After chalach's run: local sim.

## Steps
1. Route first: pick ONE solver for the job and say why. One field solve per geometry. No second solver as a check.
2. Check the setup on an exact or closed-form case first.
3. Write a notebook: cached results, RUN and NPROC switches, a "how to read the result" cell, and a short README.
   Saved results go next to it.
4. The agent may post-process cached results. It does not run the MEEP or FDTD batch, not even a short one. A quick
   local solve goes in the notebook too.
5. Never spend Tidy3D or Flexcompute cloud credits: no `web.run`, upload, start or Batch.
6. Never rebuild a notebook chalach already ran (it wipes the outputs). New notebooks get a new name or a new builder.
7. Code for the Lumerical VM: hand over only the new stage as a small zip, never the whole folder.

## Launch
`ck-run launch <ask>-sim run --model sol --workspace devfab --title "<title>" --ask <ask> --write-scope <sim folder>`

## Done means
- The notebook is ready to run and says what it will show.
- `your_move` = "run notebook <name>" with the expected run time if known (else UNKNOWN).
- `verification` = `desk_check`. No credits used. Anything that depends on the un-run notebook says so.
