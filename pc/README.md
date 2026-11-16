# Same setup on a new Windows PC

This kit copies CK's agent framework, Grok Bot mode, shortcuts, Herdr layout,
and Claude plugin preferences. It does not carry your old chats or lab files.

Do these steps in order:

- Install WSL with Ubuntu. Open Ubuntu and finish creating your Linux user.
  In Ubuntu, install the basic tools with `sudo apt update`, then
  `sudo apt install git python3 tmux`. Install Windows Terminal too.
- Install Herdr app and herdr-core in their normal Windows locations.
  Open Herdr once. The kit expects their programs under your Windows user’s
  `AppData\Local\Programs` folder. Custom locations can use `CK_HC` and `CK_HERDR_APP`.
- Install Tailscale and sign in yourself. This kit has no device addresses or keys.
- Install the agent CLIs in Ubuntu: `claude`, `codex`, `cursor-agent` (or `agent`),
  and `agy`. Sign in to each one yourself. Check that each command opens.
  The dispatcher expects the Cursor command `agent`; if your installer only provides
  `cursor-agent`, add a link named `agent` in `~/.local/bin` pointing to that executable.
- Restore your PhD project folders from your own backup or OneDrive.
  Keep the workspace rule files and Design Library with those projects.
  They are needed for project work and are not included here.
- Install Git for Windows.
  The skills repository is private: setup opens a GitHub sign-in window for it once.
  In Windows Command Prompt, clone outside OneDrive:

  ```bat
  git clone https://github.com/chalach49266/ck-dispatch "%USERPROFILE%\ck-kit"
  cd /d "%USERPROFILE%\ck-kit\pc"
  setup.cmd --dry-run
  setup.cmd
  ```

  You can also open the cloned `pc` folder in File Explorer and double-click `setup.cmd`.
- Enter your Windows user and the full PhD folder when asked. Use the WSL path,
  for example `/mnt/c/Users/YOUR_WINDOWS_USER/Documents/PhD`.
  If your OneDrive folder has spaces, enter the full path with spaces.
  Setup copies the framework into `~/PhD_local` on the Linux side. It uses no
  links into OneDrive. Existing links at the install destinations must be moved
  aside yourself before installing.
- Let setup finish. It copies Windows shortcuts and Herdr files, adds the Windows
  shortcut folder to your user PATH, and runs the Ubuntu setup. The Ubuntu setup
  installs the shell shortcuts, clones the separate skills repository, adds Claude
  marketplaces, installs the listed plugins, copies preferences, and runs offline tests.
  Superpowers is installed only for the restored Device Fabrication folder.
- Restart Herdr and open a new terminal. Setup switches Herdr to the `main-ck` layout
  (green dot = done, yellow = needs you). If it did not, pick `main-ck` in Herdr's settings.
  Windows may need a new sign-in before other apps see the changed user PATH.

Check that it works:

```bash
ck-run models
ck-run --help
```

Normal start (the way this PC is used): open Herdr, make a space, and type `cdev` or `cnv`
in its PowerShell tab. Claude opens in that project folder with Grok Bot mode on.
Agents it starts open as panes in that tab: the first one on the right half, the next ones
stacked below it. Each pane has a header with the agent's name, a status circle, and a close
button. A finished clean pane closes itself after 5 minutes; a pane with a problem stays.
`gb` opens `~/PhD_local` the same way. `gbh`, `cdevh`, `cnvh` start a Grok Bot space in
Herdr for you. In a plain Windows Terminal, the same commands send agents to Windows Terminal
windows instead (never to both).

For a smoke launch, ask Grok Bot: "Run a smoke test with opus, grok and sol." Check that the
three agents open as panes right of Grok Bot, show "done" in Herdr's Agents list, and close
themselves about 5 minutes later. This uses your signed-in agent accounts.

Ubuntu setup options:

```bash
bash setup.sh --dry-run
bash setup.sh --yes --win-user YOUR_WINDOWS_USER --phd-root '/full/WSL/PhD/path'
bash setup.sh --yes --no-plugins --no-tests
```

`WIN_USER` and `PHD_ROOT` environment variables also work. Without a Windows user
value, direct Ubuntu setup falls back to the Linux user; correct it if they differ.
`--no-plugins` is the offline option: it skips both the skills clone and plugin
commands. Store preferences are still copied. `--no-tests` skips the offline suite.
Dry-run prints the file copies, path changes, clone, plugin commands, and tests;
it creates nothing and runs no external install commands.

Running setup again refreshes kit files, keeps unrelated files, keeps an existing
skills clone, and does not repeat the shell startup line or the Windows PATH entry.
It refuses conflicting symlinks or marketplaces. It does not reset your repositories.
The coursework workspace uses generic course folders; edit its installed config
if you need that workspace. The packaged tests use generic course names too.

Not included: agent logins, keys, passwords, Tailscale identity, chats, old runs,
ledgers, personal folder paths, lab files, Python environments, or old reports.
The protected `AGENTS.md` and `CLAUDE.md` files were not copied or changed.
The heatmap notification history is cleared; the skin and token-weather settings
are kept. This checkout contains code snapshots, not the source Git histories.
Claude plugins and the separate skills repository are fetched when you install,
so their future versions may differ. This is a portable setup, not a disk backup.

The setup scripts contain no PowerShell scripts. The existing terminal bridge
uses Windows PowerShell inline for notifications and keeping Windows awake.
A blank Windows installation and a live agent smoke launch still need a real-PC check.
