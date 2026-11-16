# ck-run + Grok Bot mode in the Herdr app (2026-11-16, ask A-1116-3)

Herdr runs ck-run and Grok Bot mode together. Herdr (Windows desktop app,
`C:\Users\{{WIN_USER}}\AppData\Local\Programs\herdr-app`) shows terminals that its core, herdr-core (`hc.exe`), runs.
This folder moves ck-run's live windows from tmux + Windows Terminal (`platform/wsl/`) into Herdr.
`ckdispatch/` is unchanged: only `config/workspaces.json` `live.osascript` points here.

| ck-run (Mac words) | Herdr | How |
|---|---|---|
| window per ask | **space** `ck<wid>` ("ck · agent · title") | `hc run --label herdr.space=ck<wid>` |
| tab per run | a **pane** in the ask's one tab `ck<wid>`: an ask's runs sit side by side (split panes) | `--label herdr.tab=ck<wid>`; terminal name `ck<wid>-<n>` |
| session id | hc address (`t12`) | |
| window text (stall watchdog, reply recovery) | `hc read t12 --lines 300` | |
| resume key after sleep | `hc send t12 continue` | text + Enter |
| close tab of a clean run (after 10 min) | `hc kill` + `hc rm` | tab and space close with it |
| notification | Windows toast | from `platform/wsl/osascript` |

Each run is `wsl.exe -d Ubuntu --exec /bin/bash ~/.local/state/ck-osascript/herdr/ck<wid>-<n>.sh` on the Windows
core; the script sets PATH and runs ck-run's `live.sh` command. Runs live in herdr-core, so they keep going when
the Herdr window closes or updates. Herdr detects Claude Code in them (icon, working / done / idle dot).
The Herdr window opens by itself on the first run if it is closed (`CK_NO_HERDR_APP=1`: never touch the window).

## Herdr OR terminal, never both (A-1116-13)

Agent runs stay in the terminal host used by their caller: Herdr or WSL, without duplicate windows.
`config/workspaces.json` `live.osascript` = `platform/auto/osascript`, a router in front of the two scripts:
- **New window**: where the Grok Bot (the caller) runs. ck-run cleans the env, so the router reads its parent
  processes' env: `CK_LIVE_HOST=herdr|wsl` (ck-herdr sets herdr), else `HC_TERMINAL_ID` (herdr-core sets it in
  every Herdr terminal; `gb.cmd`, `cdev.cmd`, `cnv.cmd`, `grokbot.cmd`, `phd.cmd` forward it into WSL via WSLENV)
  = Herdr; else = tmux + Windows Terminal.
- **Follow-ups** go to the side that owns the run: session `%7` = tmux, `t12` = Herdr. Herdr window ids start at
  5000, tmux ones below. An ask window on the other side counts as closed, so the next run opens on this side.
- Force a side for one session: `CK_LIVE_HOST=wsl claude` (or `herdr`). Back to Herdr only: `live.osascript` =
  `platform/herdr/osascript` (backup `config/workspaces_herdr-only_2026-11-16.json.bak`).
- Checked 2026-11-16: race of opus, grok, sol from a WSL terminal = 3 tmux windows, nothing in Herdr; a Herdr
  caller = a Herdr pane (id 5000), no tmux session; read and close by session id work from either side.

## Sub-agents are panes in Grok Bot's tab, stacked on the right (A-1116-30, A-1116-32; replaces A-1116-9)

The layout-wide `ck-agents` column had two faults (found 2026-11-16): its terminal blocks have no pane header (no
status circle, no menu, no close), and a layout shows in every space. Now a run under a Grok Bot is started as its
sub-agent (`hc run --parent`, no tab labels) and then gets a pane with Herdr's `terminal.keep`: the first live run
goes RIGHT of Grok Bot (half the width), each later one DOWN from the sibling pane with the largest share, so the
runs stack top to bottom on the right half (3 runs = 1/2, 1/4, 1/4; 4 runs = equal; Herdr has no resize command).
`terminal.keep` splits the focused pane, so `place_pane` focuses the target first and gives Grok Bot the focus back.
Each pane has Herdr's header (status, menu, close), lives only in Grok Bot's tab, and stays nested under Grok Bot in
the Agents list. Shares are kept in the run files (`share`). `osascript --sync` now only removes an old column.

## Short description in the agent's name (A-1116-29)

Terminal names include a short description so the task is visible at a glance. A run's terminal
name is `ck<wid>-<n>-<agent>-<description>`, e.g. `ck5002-1-codex-pc-setup-kit`: the first words of the ck-run
`--title` (small words, run ids and dates dropped; a review shows `review-...`). hc names allow at most 32 of
a-z 0-9 - _ . (measured), so the description gets the room that is left. Short titles give the best names.

## Grok Bot = the main space; agent runs are its sub-agents (A-1116-6)

Grok Bot owns the main space; Claude, Cursor and Codex runs appear beneath it.
While a Grok Bot runs in Herdr (hc terminal `grokbot-*`, started by `ck-herdr`), every run goes into Grok Bot's
space: one tab per ask (`ck<wid>`), each run a split pane in it, and each run is Grok Bot's **sub-agent**
(`hc run --parent <grokbot> --outlive-parent --no-notify`): the Agents list nests `ck<wid>-<n>-<agent>` under
Grok Bot. Runs keep going if Grok Bot exits; hc types nothing into Grok Bot. No Grok Bot in Herdr: each ask gets
its own space, as before. Several Grok Bots: the newest is the parent (`CK_HERDR_PARENT=<address|name>` picks one).

Move a conversation into Herdr: `gbh --continue` (the last one) or `gbh --resume <id> [--fork-session]`, then
`/exit` the old window.

## Grok Bot left, agents right, equal split (A-1116-9)

Grok Bot occupies the left half. Agent runs share the right half equally and close automatically after the configured delay.
This supersedes the tab-per-ask placement under Grok Bot above:
- A run under Grok Bot gets no tab of its own (no herdr.space/tab labels; only `--parent`). The layout `main-ck`
  gets a column `ck-agents` right of the work area (weight 1 = half the width), one terminal block per live run,
  all weight 1 (equal). Herdr has no pane-size command, so splits of a tab could not be made equal; layout blocks can.
- The column is rebuilt (`osascript --sync`) when a run starts, when ck-run closes one, and when a run closes
  itself. No runs: no column. The runs stay Grok Bot's sub-agents in the Agents list.
- A run's script holds `done` for 5 min (`live.close_tab_after_s` = 300 since A-1116-21; was 10 min): if the agent reports `idle` over it (Codex
  does), `done` is sent again. Nobody used it (never `working` / `blocked`) and status `ok`: after 5 min it leaves
  the column and closes itself. Used, or partial: it stays until ck-run's own closer or you close it.

## Done in the Agents list, and Grok Bot told per run (A-1116-7)

Finished runs show done in the Agents list and notify Grok Bot promptly.
- Each run's script has a small background job: when ck-run writes the run's `result.json`, it sends Herdr
  `OSC 7501 app=<cli>:state=done` (status ok/partial) or `state=error` (anything else) on the run's own terminal.
  The Agents list shows **done** at once (it turns idle once you look, Herdr's rule). Cursor and agy report no
  status themselves, so they also get `state=working` at the start. Herdr app words: claude-code, codex, cursor.
- The grokbot mod (0.2.2) wakes Grok Bot early for each run of a race or panel that ends while others still run
  ("... finished; still running: ..."), then once more when the last one ends (the finish card waits for that).

## Colors in the Agents list (A-1116-8)

Done appears green; a run needing attention appears yellow. Herdr's own files stay as shipped; two clones in
`C:\Users\{{WIN_USER}}\.config\herdr-app\`:
- `layouts/main-ck.json` (clone of `main`, the active layout): `colors.done = #22c55e` (green),
  `colors.blocked = #eab308` (yellow: "Needs you", the ! badge, the status bar count); its Agents block uses
  the mod `agents-ck`.
- `mods/agents-ck/` (clone of `agents` 0.1.16): a finished sub-agent keeps its green done dot (Herdr's own list
  shows a sub-agent's done as an idle grey ring); a sub-agent's `error` shows as needs-you (yellow).
- Back to Herdr's own look: `herdr-app do layout.switch --name main` (or ctrl-alt-shift-escape).
- After a Herdr update, the clone keeps the old Agents code: compare with `mods/agents` if the list looks off.

## Grok Bot mode in Herdr

- Windows (PowerShell, Run box): `gbh` (= ~/PhD_local), `cdevh` (Device Fabrication), `cnvh` (NV Magnetometer).
  `C:\Users\{{WIN_USER}}\.local\bin\*h.cmd` → `ck-herdr <shortcut>`: opens Herdr, starts Claude Code with Grok Bot mode
  in the space "Grok Bot" (tab per folder). Already running: it is shown, not started twice.
- In WSL: `~/PhD_local/ck-dispatch/platform/herdr/ck-herdr [gb|cdev|cnv|ls]`.
- Inside Herdr, a new PowerShell tab with `gb` / `cdev` / `cnv` also works (the old shortcuts).
- Grok Bot then launches agents as usual (`ck-run launch ...`); each shows up as a new space beside it, and the mod
  wakes Grok Bot when a run ends, as before.

## Checks (2026-11-16)

- Offline tests with this config: 399/399 passed (`python3 -I tests/run_all.py`).
- By hand: window, tab, exists (yes / other / no), write, tail, close (CKOK, then CKNOTFOUND).
- End to end: `ck-run launch A-1116-3-smoke run --model sonnet --workspace devfab` ran live in Herdr space
  `ck1001`, result recovered, exit line written (`.out`). `ck-herdr gb` started Grok Bot mode in Herdr.

## Back to tmux + Windows Terminal

Set `live.osascript` back to `~/PhD_local/ck-dispatch/platform/wsl/osascript`
(copy: `config/workspaces_wsl-tmux_2026-11-16.json.bak`).

## Known limits

- ck-run's notes still say "ran in a iTerm2 window" (`live.terminal` stays `iTerm2` so it uses these calls).
- A tty alone cannot find a Herdr run (every `wsl.exe` session has its own `/dev/pts`); ck-run passes the session
  id, so this matters only for very old runs.
- herdr also offers a Linux core inside WSL (`hc machine add --via 'wsl.exe -d Ubuntu -e sh -c' --name wsl`).
  Not used: it needs an install in WSL and adoption of labeled terminals there is untested.
