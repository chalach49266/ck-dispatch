@echo off
setlocal
if defined HC_TERMINAL_ID set "WSLENV=HC_TERMINAL_ID:%WSLENV%"
wsl.exe -d Ubuntu --exec bash -lc "exec \"$HOME/.local/bin/ck-open\" cnv \"$@\"" ck-open %*
