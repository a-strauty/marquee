#!/usr/bin/env bash
# Install (or re-apply) the touch-panel dashboard into ML4W's Hyprland config.
#
# Idempotent. Re-run after any ML4W update. Pass --check to dry-run.
#
# IMPORTANT: ML4W 2.10.1 runs Hyprland's *Lua* config
# (~/.config/hypr/hyprland.lua). The .conf tree is dead legacy and is NOT
# parsed - editing it silently does nothing. Everything here targets Lua.
#
# Persistence model:
#   * ~/.config/{hypr,waybar,ml4w} are symlinks into the ML4W dotfiles tree
#   * .config/ml4w/settings is in config.dotinst's "restore" list
#   * custom.lua is a user file; if an update ever replaces it, re-run this
#   * all real config lives in dashboard.lua, outside the dotfiles tree

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYPR="$HOME/.config/hypr"
WAYBAR="$HOME/.config/waybar"
ML4WSET="$HOME/.config/ml4w/settings"

if [[ -f "$HERE/config.env" ]]; then
    set -a; . "$HERE/config.env"; set +a
fi

MONITOR="${DASH_MONITOR:-HDMI-A-3}"
WORKSPACE="${DASH_WORKSPACE:-panel}"
BAR_OUTPUTS="${DASH_BAR_OUTPUTS:-[\"DP-1\", \"DP-2\"]}"
THEME_SRC="${DASH_THEME_SRC:-ml4w-minimal}"
THEME_DST="${DASH_THEME_DST:-adam-minimal}"

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

# --uninstall: unhook cleanly, so a move/removal can't leave a broken dofile
if [[ "${1:-}" == "--uninstall" ]]; then
    printf '\n\033[1m:: Uninstalling\033[0m\n'
    CUSTOM="$HOME/.config/hypr/custom.lua"
    if [[ -f "$CUSTOM" ]] && grep -q 'dashboard\.lua' "$CUSTOM"; then
        cp -a "$CUSTOM" "$HERE/.custom.lua.bak.$(date +%Y%m%d%H%M%S)"
        python3 "$HERE/hook.py" "$CUSTOM" --remove >/dev/null
        printf '  removed dofile hook from ~/.config/hypr/custom.lua\n'
    else
        printf '  no hook present\n'
    fi
    if [[ -f "$ML4WSET/waybar-theme.sh" ]] && grep -q "$THEME_DST" "$ML4WSET/waybar-theme.sh"; then
        printf '/%s;/%s' "$THEME_SRC" "$THEME_SRC" > "$ML4WSET/waybar-theme.sh"
        printf '  waybar theme reverted to %s\n' "$THEME_SRC"
    fi
    hyprctl reload >/dev/null 2>&1 && printf '  hyprland reloaded\n'
    [[ -x "$WAYBAR/launch.sh" ]] && { "$WAYBAR/launch.sh" >/dev/null 2>&1 & printf '  waybar relaunching\n'; }
    printf '\nLeft alone: config.env, ~/.config/dashboard/spotify.json,\n'
    printf 'and the %s waybar theme folder. Delete those by hand if you want.\n\n' "$THEME_DST"
    exit 0
fi

# --detect: print what this machine actually has, to fill in config.env
if [[ "${1:-}" == "--detect" ]]; then
    printf '\n\033[1m:: Detected on this machine\033[0m\n'
    printf '\n  Monitors (hyprctl monitors):\n'
    hyprctl monitors -j 2>/dev/null | python3 -c '
import json,sys
for m in json.load(sys.stdin):
    print("    {:<12} {}x{} @ {},{}".format(m["name"], m["width"], m["height"], m["x"], m["y"]))' 2>/dev/null || echo "    (hyprctl unavailable)"
    printf '\n  Touch devices (hyprctl devices):\n'
    hyprctl devices -j 2>/dev/null | python3 -c '
import json,sys
t=json.load(sys.stdin).get("touch",[])
print("\n".join("    "+d.get("name","?") for d in t) if t else "    (none)")' 2>/dev/null || echo "    (hyprctl unavailable)"
    printf '\n  Network interfaces (up, non-virtual):\n'
    ip -br link 2>/dev/null | awk '$2=="UP" && $1!~/^(lo|docker|veth|br-|virbr)/ {print "    "$1}'
    printf '\n  Mounted filesystems:\n'
    df -h --output=target,pcent -x tmpfs -x devtmpfs -x efivarfs 2>/dev/null | tail -n +2 | awk '{print "    "$1"  ("$2" used)"}'
    printf '\n  Waybar themes:\n'
    ls -1 "$WAYBAR/themes" 2>/dev/null | grep -v assets | sed 's/^/    /'
    printf '\nPut these into config.env (cp config.env.example config.env).\n\n'
    exit 0
fi

say()   { printf '  %s\n' "$*"; }
head_() { printf '\n\033[1m:: %s\033[0m\n' "$*"; }
would() { [[ $CHECK -eq 1 ]] && { say "WOULD: $*"; return 0; }; return 1; }
ts()    { date +%Y%m%d%H%M%S; }

# --------------------------------------------------------------------
head_ "1/4  Sanity: is this a Lua-config ML4W?"
if [[ ! -f "$HYPR/hyprland.lua" ]]; then
    say "ERROR: $HYPR/hyprland.lua not found."
    say "This install targets ML4W's Lua config. Aborting."
    exit 1
fi
say "found hyprland.lua"
if grep -q "$MONITOR" "$HYPR/monitors.lua" 2>/dev/null; then
    say "monitors.lua already defines $MONITOR (loaded via require(\"monitors\"))"
else
    say "WARNING: $MONITOR not found in ~/.config/hypr/monitors.lua"
    say "         Re-run nwg-displays and Apply to regenerate it."
fi

# --------------------------------------------------------------------
head_ "2/4  Hook dashboard.lua into custom.lua"
CUSTOM="$HYPR/custom.lua"
# pcall so a moved or deleted dashboard folder degrades quietly instead
# of raising a Lua config error on every Hyprland reload.
HOOK="if not pcall(dofile, \"$HERE/dashboard.lua\") then print(\"[dashboard] not loaded\") end"
if [[ -f "$CUSTOM" ]] && grep -qF "$HOOK" "$CUSTOM" \
   && [[ $(grep -c 'dashboard\.lua' "$CUSTOM") -eq 1 ]]; then
    say "already hooked in ${CUSTOM/#$HOME/\~}"
elif ! would "write dofile hook into ${CUSTOM/#$HOME/\~} (replacing any stale one)"; then
    [[ -f "$CUSTOM" ]] && cp -a "$CUSTOM" "$HERE/.custom.lua.bak.$(ts)"
    STALE=$(grep -c 'dashboard\.lua' "$CUSTOM" 2>/dev/null || echo 0)
    python3 "$HERE/hook.py" "$CUSTOM" "$MONITOR" "$HOOK" >/dev/null
    if [[ "$STALE" -gt 0 ]]; then
        say "replaced $STALE stale hook(s) - folder moved?"
    else
        say "hooked into ${CUSTOM/#$HOME/\~}"
    fi
fi

# --------------------------------------------------------------------
head_ "3/4  Waybar: custom theme so the bar skips the panel"
# ~/.config/waybar is NOT in the restore list, and launch.sh sed-edits the
# ACTIVE theme's config on every start. So we use our own theme folder.
SRCTHEME="$WAYBAR/themes/$THEME_SRC"
DSTTHEME="$WAYBAR/themes/$THEME_DST"
THEMEFILE="$ML4WSET/waybar-theme.sh"
WANTTHEME="/$THEME_DST;/$THEME_DST"

if [[ ! -d "$SRCTHEME" && ! -d "$DSTTHEME" ]]; then
    say "SKIP: neither $THEME_SRC nor $THEME_DST present"
elif ! would "ensure $THEME_DST theme with \"output\": $BAR_OUTPUTS"; then
    [[ -d "$DSTTHEME" ]] || { cp -a "$SRCTHEME" "$DSTTHEME"; say "created $THEME_DST"; }
    if grep -q '"output"' "$DSTTHEME/config"; then
        say '"output" already set'
    else
        python3 - "$DSTTHEME/config" "$MONITOR" "$BAR_OUTPUTS" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); lines = p.read_text().splitlines()
for i, l in enumerate(lines):
    if l.strip() == "{":
        lines.insert(i + 1, '    // Keep the bar off the {} dashboard panel'.format(sys.argv[2]))
        lines.insert(i + 2, '    "output": {},'.format(sys.argv[3]))
        break
p.write_text("\n".join(lines) + "\n")
PY
        say "added \"output\": $BAR_OUTPUTS"
    fi
    # Per-output workspaces. The included modules.json defaults to
    # all-outputs:true, which makes the DP bars enumerate the panel monitor
    # too and render its named workspace as a raw negative id (-1337).
    # Keys defined in the theme config override the included file, so the
    # shipped modules.json is left untouched.
    # NB: ignore-list does NOT apply to hyprland/workspaces.
    # Match the *definition*, not the reference in "modules-left".
    if grep -q '"hyprland/workspaces"[[:space:]]*:' "$DSTTHEME/config"; then
        say "workspaces override already present"
    else
        python3 -c 'import sys,pathlib
cfg=pathlib.Path(sys.argv[1]); blk=pathlib.Path(sys.argv[2]).read_text()
t=cfg.read_text(); a="    // Updates Count"
if a in t and "\"hyprland/workspaces\":" not in t: cfg.write_text(t.replace(a, blk+a, 1))
' "$DSTTHEME/config" "$HERE/waybar-workspaces.jsonc"
        say "added per-output workspaces override"
    fi
    sed -i "s/^theme_name=.*/theme_name=\"${THEME_DST}\"/" "$DSTTHEME/config.sh" 2>/dev/null || true
    if [[ "$(cat "$THEMEFILE" 2>/dev/null || echo)" == "$WANTTHEME" ]]; then
        say "theme already active"
    else
        printf '%s' "$WANTTHEME" > "$THEMEFILE"
        say "activated $WANTTHEME"
    fi
fi

# --------------------------------------------------------------------
head_ "4/4  Reload"
if would "hyprctl reload, relaunch waybar, focus the panel workspace"; then
    :
else
    hyprctl reload >/dev/null 2>&1 && say "hyprland reloaded"
    [[ -x "$WAYBAR/launch.sh" ]] && { "$WAYBAR/launch.sh" >/dev/null 2>&1 & say "waybar relaunching"; }
    # A stale workspace can squat on the panel; put 'panel' in front.
    ACTIVE=$(hyprctl monitors -j 2>/dev/null \
        | python3 -c "import json,sys;print(next((m['activeWorkspace']['name'] for m in json.load(sys.stdin) if m['name']==sys.argv[1]),''))" "$MONITOR" 2>/dev/null || echo)
    if [[ -n "$ACTIVE" && "$ACTIVE" != "$WORKSPACE" ]]; then
        FOCUS=$(hyprctl activewindow -j 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin).get('address',''))" 2>/dev/null || echo)
        hyprctl dispatch "hl.dsp.focus({ monitor = \"$MONITOR\" })" >/dev/null 2>&1
        hyprctl dispatch "hl.dsp.focus({ workspace = \"name:$WORKSPACE\" })" >/dev/null 2>&1
        [[ -n "$FOCUS" ]] && hyprctl dispatch "hl.dsp.focus({ window = \"address:$FOCUS\" })" >/dev/null 2>&1
        say "moved panel monitor from workspace '$ACTIVE' to '$WORKSPACE'"
    else
        say "panel workspace already in front"
    fi
fi

printf '\n\033[1mDone.\033[0m Re-run after any ML4W update.\n'
[[ $CHECK -eq 1 ]] && printf 'Dry run (--check); nothing was written.\n'
exit 0
