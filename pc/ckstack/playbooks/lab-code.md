# Playbook: lab-code

**When**: code that runs lab instruments or reads their data: SPCM and DAQ counting, laser control, the IMU logger,
lock-in and ODMR, instrument control windows.
**Trigger**: `do` (a broken thing: `fix`, see `bug-fix.md`). **Size** M (L when it has parts). **Pattern** pair.
**Lane** `lab-code`: sol writes; astra for the hard part; grok also good; opus checks.
**Read first**: `~/PhD_local/grok-bot-skills/nv-magnetometer-code/SKILL.md` (NV code, lock-in, ODMR, sensitivity),
`~/PhD_local/grok-bot-skills/phd-wiki-rules/SKILL.md` (where notes go).
**Proof needed**: fake data or tests. The hardware test is chalach's move.

## Steps
1. Open the ask (M). Brief = chalach's words + "Do only this" (at most 4 steps).
2. Launch Sol in the code folder:
   `ck-run launch <ask>-lab-code run --model sol --workspace devfab|nv --title "<title>" --ask <ask> --write-scope <code folder>`
3. The brief asks for a fake-data mode or a test harness for the instrument. Build it once and reuse it
   (the SPCM `--fake` idea). Tests run on fake data.
4. Code drives hardware, so it gets one review (trigger `hardware-or-raw`):
   `ck-run launch <ask>-review-claude review <run> --by claude --model opus --trigger hardware-or-raw --ask <ask> --focus "<timing, units, buffers, safety limits>"`
5. A proven problem that blocks the goal: one fix run (`--parent <run>`). Anything else: `ck-run ask later`.
6. Close. `your_move` names the hardware test for chalach.

## chalach's rules for lab code
- Instrument control window: very simple and white, standard buttons, every function of the device, and a
  raw-command box that keeps the safety limits. A disabled button says why. No styled dashboards.
- An NV panel that the DEMO console already draws: port the DEMO's own view (ODMR as normalised z, dips down).
  Check it by clicking through like chalach, not only by API tests. Check that calibrations are used downstream.
- Raw data is read-only. Write new data to a new file.
- Fix the cause of a timeout or a dropped count. No retry loop that hides it.

## Done means
- Fake-data tests pass; the files changed are listed.
- `verification` = `fake_or_tests` (not higher until chalach runs it on the instrument).
- The finish card says the hardware test in "Your move".
