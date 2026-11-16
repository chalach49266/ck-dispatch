# CK folder shortcuts (A-1116-2, chalach 2026-11-16): go to the folder, then open Claude Code with Grok Bot mode on
# (CK_GROKBOT_AUTO=1: the grokbot mod, v0.2.1+, turns the mode on at session start).
PHD_WIN="{{PHD_ROOT}}"
_ck_go() { local folder="$1"; shift; cd "$folder" || return; command -v claude >/dev/null && CK_GROKBOT_AUTO=1 claude "$@"; }   # the grokbot mod turns the mode on by itself
cdev() { _ck_go "$PHD_WIN/Device Fabrication" "$@"; }       # Device Fabrication + Claude in grokbot mode
cnv()  { _ck_go "$PHD_WIN/NV Compact Magnetometer" "$@"; }  # NV Compact Magnetometer + Claude in grokbot mode
gb()   { _ck_go ~/PhD_local "$@"; }                          # ~/PhD_local + Claude in grokbot mode
phd()  { cd ~/PhD_local || return; }                    # only go to ~/PhD_local (no Claude)

export PATH="$HOME/.local/bin:$PATH"
