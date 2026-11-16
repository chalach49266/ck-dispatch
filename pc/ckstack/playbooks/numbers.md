# Playbook: numbers

**When**: data analysis and numbers: fits, PSD lines, noise floor, sensitivity, Rabi frequency, coupling, a dose.
**Trigger**: `number` (S: one number explained, see `skills/number`) or `do` (M: an analysis).
**Size and pattern**: S solo. M solo: Sol writes the code; Opus may explain the meaning in a second run. L: split.
**Lane** `numbers`: sol; also opus, grok; opus checks.
**Read first**: `~/PhD_local/grok-bot-skills/nv-magnetometer-code/SKILL.md` (sensitivity, lock-in, ODMR),
`~/PhD_local/grok-bot-skills/phd-wiki-rules/SKILL.md`.
**Proof needed**: each number comes from chalach's data (tag `measured`) or a local sim (tag `simulated`), with the
file. A number is never a guess. The script must rerun.

## Steps
1. Before computing: write the decision the number feeds, and the noise floor. What result would change the decision?
2. Compute from the raw files read-only (copy, never edit). Keep the script and its output; it must rerun.
3. For each number fill `numbers[]`: value, unit, source (file and line or cell), verified, `what_else` (other causes,
   each ruled out with evidence), `limit` (what bounds it: why is it not twice as good?). Add the run count and spread.
4. Say plainly which numbers are measured and which are modelled.
5. A number that goes on a slide, into a paper or an email: one review, `--trigger leaves-lab`.

## Launch
`ck-run launch <ask>-numbers run --model sol --workspace devfab|nv --title "<title>" --ask <ask> --write-scope <analysis folder>`
Opus explains the meaning in a second run (L, or M when no fix run is needed):
`ck-run launch <ask>-meaning run --model opus --mode read --workspace devfab|nv --title "<title>: meaning" --ask <ask>`

## chalach's rules for numbers
- A line in a spectrum: rule out mains (~60 Hz and its harmonics) and known instrument or aliased lines before naming
  a new source.
- Light sources: rank them on the vendor's PSD curve inside the band, at its weakest wavelength. Check every model
  in the product family.
- NV designs: score them with chalach's own metric (`odmr_sensitivity.estimate_sensitivity`, at zero field and on
  the big peaks), fitted to the measured sweeps. Give a paper for each design element.
- No device number from a hand estimate on a slide: simulate it locally, or mark it schematic.

## Done means
- Every number has source, what_else and limit. The code reruns and gives the same numbers.
- `verification` is the lowest true level: the script and its tests ran (`fake_or_tests`), or a local solver ran
  (`local_sim`). Nothing ran: `desk_check`.
