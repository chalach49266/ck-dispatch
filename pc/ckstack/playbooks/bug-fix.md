# Playbook: bug-fix

**When**: something is broken: a wrong value, a crash, dropped counts, a failing test.
**Trigger**: `fix`. **Size** M (1 run + 1 fix run). **Pattern** solo; a review only with a reason
(`hardware-or-raw` for instrument code).
**Lane** `bug-fix`: sol; astra after "stuck twice"; opus checks.
**Read first**: the project's domain skill, e.g. `~/PhD_local/grok-bot-skills/nv-magnetometer-code/SKILL.md`.
**Proof needed**: fake data or tests: a test that failed on the old code now passes.

## Steps (the brief asks for exactly these)
1. Reproduce: a failing test, or a small script on fake data that shows the bug. No repro, no fix: say what is
   missing (data, hardware, a file) and stop.
2. Name the mechanism in one line before changing code: what goes wrong, where, why. Tag it (`measured` when the
   repro shows it, `guess` when it does not).
3. The smallest fix at the cause. No retry, sleep, broad try/except or skipped test that hides the symptom.
4. Run the failing test again: it passes now. Run the other tests: they still pass. Put both outputs in result.md.
5. Other bugs seen on the way: `later`. Do not fix them now.

## Launch
`ck-run launch <ask>-bug-fix run --model sol --workspace devfab|nv|washu --title "<title>" --ask <ask> --write-scope <code folder>`
(a `~/PhD_local` repo: `cd` there first, `--write-scope <repo>`.)

## When it does not work (escalate, don't multiply)
1. Miss: one retry on the strong tier, `--parent <run>` (the fix run).
2. Stuck twice: one advisor from another family on the top tier (ck-mode section 7), e.g.
   `ck-run launch <ask>-advisor review <run> --by claude --model opus-max --trigger agent-unsure --ask <ask> --focus "stuck: <the error>. Root cause and next step?"`
3. Still stuck: ask chalach. Over budget (exit 7): ask chalach first.

## Done means
- The root cause is named in one line.
- The failing-then-passing test output is in result.md.
- `verification` = `fake_or_tests`; hardware checks are chalach's move.
