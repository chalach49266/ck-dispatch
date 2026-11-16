# grokbot

`/grokbot` makes Claude Code stand in for Grok Bot while Grok usage is out. Claude becomes the orchestrator (CK):
it routes each task to claude, codex, cursor or agy through `~/PhD_local/ck-dispatch/bin/ck-run` (always in an
iTerm2 window), and documents every step in `~/PhD_local`.

- `/grokbot` turns the mode on. `/grokbot <task>` turns it on and sends the task. `/grokbot status`, `/grokbot off`.
- Read-only views, in any mode: `/grokbot board`, `/grokbot today`, `/grokbot recall <project>`, `/grokbot rules`,
  `/grokbot models` (they show `ck-run board`, `digest --since 1d`, `recall`, `rules`, `models`).
- `/grokbot` sends the playbook `~/PhD_local/grokbot-mod/GROKBOT.md` (and ckstack's `ck-mode/SKILL.md`) to Claude
  once. While on, every prompt carries a short "[Grok Bot mode ON ...]" reminder (so the mode survives compaction),
  plus a `[ck: ...]` line when the prompt starts with trigger words (`quick`, `do`, `race`, `sol`, ...).
- While on, the status line shows each live run and what needs you: `Grok Bot ON · sol 12m · opus 3m · 1 needs you`.
- Launches made in this session are watched: when one ends, the mod logs it, sends a notification, and wakes
  Claude once. chalach's prompts are logged verbatim to `~/PhD_local/grokbot_router/sessions/<date>_<session>.md`.
  (No system-prompt hook: the built-in guard `cc-plugin-sec-default` blocks user mods there on Team/Enterprise plans.)
- The mode is per session: new sessions and ck-run workers start with it off.

Full docs: `~/PhD_local/grokbot-mod/README.md`.
