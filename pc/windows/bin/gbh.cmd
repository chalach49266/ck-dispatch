@echo off
rem CK shortcut (A-1116-3, 2026-11-16): open the Herdr app and start Claude Code with Grok Bot mode on in it (gb)
rem Script: {{LOCAL_ROOT}}\ck-dispatch\platform\herdr\ck-herdr
wsl.exe -d Ubuntu --exec bash -lc "exec \"$HOME/PhD_local/ck-dispatch/platform/herdr/ck-herdr\" gb \"$@\"" ck-herdr %*
