# ck-run on Windows + WSL (2026-11-15)

**Since 2026-11-16 the live windows run in the Herdr app: see `../herdr/README.md`.** This tmux layer is the fallback
(its toast and helpers are still used by the Herdr layer).

ck-run was built on the Mac. On the Windows PC it runs inside WSL (Ubuntu 26.04) with no change to `ckdispatch/`:
the Mac-only programs it calls are configurable paths in `config/workspaces.json`, and this folder holds Linux
stand-ins for them.

| Mac | Here | How |
|---|---|---|
| `osascript` → iTerm2 window per ask, tab per run | `osascript` (Python) | tmux server `-L ckrun`: one tmux **session** per ask (`ck<id>`), one tmux **window** per run. Each new session opens a **Windows Terminal** tab (window `ck-run`) attached to it. Window text = `capture-pane`, the resume key = `send-keys`, close = `kill-pane`. |
| `osascript display notification` | same file | a Windows toast (PowerShell, WinRT `ToastNotificationManager`) |
| `caffeinate -i -w <pid>` | `caffeinate` (bash) | a PowerShell helper holds `SetThreadExecutionState(ES_CONTINUOUS\|ES_SYSTEM_REQUIRED)` while `<pid>` lives |

Config (`config/workspaces.json`, Mac original in `config/workspaces_mac_2026-11-15.json.bak`):
`live.osascript`, `live.caffeinate` (absolute: that key is read without `~` expansion), workspace roots under
`{{ONEDRIVE_ROOT}}/...`, `bin_dirs` without Homebrew.
`live.terminal` stays `iTerm2` so ck-run uses the tab / tail / type / close calls that the stand-in serves.

Layout in WSL: `~/PhD_local/` is a Linux folder. `ck-dispatch`, `ckstack`, `grokbot-mod`, `grok-bot-skills` are
symlinks to `{{LOCAL_ROOT}}\`; `agent_runs/` and `grokbot_router/` are Linux-only
(ledger, locks and run folders stay off OneDrive).

## Use

- Watch runs: the Windows Terminal window "ck-run", or `tmux -L ckrun ls` / `tmux -L ckrun attach -t ck<id>`.
  Inside tmux: `Ctrl-b n` / `Ctrl-b p` = next / previous run, `Ctrl-b d` = detach (the run keeps going).
- No Windows Terminal (headless): `export CK_NO_WT=1`.
- Grok Bot mode: start Claude Code **inside Ubuntu** (`wsl -d Ubuntu`, then `claude`). The `/grokbot` mod and
  ckstack are installed there at user scope; the mod's gate watches the Bash tool and `~/PhD_local` paths, as on the Mac.

## Checks (2026-11-15)

- Offline tests (`python3 -I tests/run_all.py` in WSL): 398/399 passed before the one config test was taught the
  Windows path (`tests/test_washu.py`).
- Stand-in checked by hand: window, tab, exists, tail by pane id, typing by tty, close (twice: CKOK then
  CKNOTFOUND), toast, keep-awake start / release on pid exit / release on SIGTERM.
- Not checked yet: a real live run end to end (agents need a login first).

## Known limits

- `~/PhD_local` write scopes: ck-run checks that a local repo's real path is inside `~/PhD_local`. Here the repos
  resolve to `/mnt/c/...`, so an agent run that asks to EDIT ck-dispatch / ckstack / grokbot-mod / grok-bot-skills
  is refused (safe failure). Edits to the OneDrive workspaces are not affected.
- agy (Antigravity CLI) is not installed on this PC.
- WashU HW is not on this PC; its root in the config is a placeholder.
- The code lives on the Windows drive (`/mnt/c`), so file-heavy steps are slower than on the Mac.
