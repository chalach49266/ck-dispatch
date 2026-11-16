# Playbook: design-loop

**When**: improve one number step by step until a target: AutoNV-MW microwave structures (Rabi frequency, B1 field),
PhotonForge photonics (coupling, loss).
**Trigger**: `loop` (a model word picks the worker, e.g. `loop astra`). **Size** L. **Pattern** loop.
**Lane** `loop`: sol-fast each round; also composer, sonnet; astra judges every few rounds.
**Read first**: `~/PhD_local/grok-bot-skills/microwave-autodesign-loop/SKILL.md`,
`~/PhD_local/grok-bot-skills/photonforge-autodesign-loop/SKILL.md`, and first the tool router:
`~/PhD_local/grok-bot-skills/microwave-solver-selection/SKILL.md` or
`~/PhD_local/grok-bot-skills/photonics-tool-selection/SKILL.md`.
**Proof needed**: local sim (the metric runs a local solver), and the best design as a file chalach can rerun.

## Before the loop (plan card, chalach's OK)
1. The metric: a LOCAL command that prints the number last. Closed form or the quasi-static solver only. No cloud
   credits; FDTD or FEM checks are notebooks chalach runs (`sim-handoff.md`).
2. Check the metric on a known case (a closed-form answer) first. Then freeze it.
3. The target, the max rounds, what may change (one thing per round), what must not change.
4. The first loop runs while chalach watches. Overnight only after that, with the overnight contract: the goal,
   "done means", what the agent may do, an escape hatch ("stuck for N rounds: stop and write why"), the decision log.

## Start
`ck-run ask open --words "<chalach verbatim>" --size L --title "<the goal in your words>" --pattern loop --models sol-fast --project <name> --plan ~/PhD_local/agent_runs/<name>_plan.md`
`ck-run launch <ask>-loop loop --ask <ask> --model sol-fast --prompt-file ~/PhD_local/agent_runs/<ask>-loop_prompt.md --workspace devfab --metric "<local command that prints the number last>" --target ">=<number>" --max <N> --cwd <design folder> --title "<title>"`
- Each round = one run (`--parent` the last round), then the metric. Keep or revert. One row per round in
  `~/PhD_local/agent_runs/<ask>/loop_decisions.tsv` (ts, i, run_id, metric, kept).
- It stops at the target, at N rounds, after 2 rounds without a gain, or when a run exits 2, 4, 5 or 7.
- Budget: each round is one run. Later rounds also count as fixes. For `--pattern loop`, set `runs=` to the max
  rounds plus judge runs, and `fixes=` to at least max rounds minus one, as numbers in the plan budget line.
- Target ops: `>=`, `<=`, `>`, `<`.
- Judge: every few rounds astra (read run) or opus reads `loop_decisions.tsv` and says go on, change approach, or stop.

## Never
- Relax the target to meet it. Stack two changes in one round. Claim a gain without the metric.
- Run the same structure in two solvers. Route first, one field solve per geometry, and check a solver against a
  closed form, not against another solver.

## Done means
- Target met or max reached. The decision log is complete.
- The best design, its metric value, and the command that reproduces it. The next idea goes to `later`.
