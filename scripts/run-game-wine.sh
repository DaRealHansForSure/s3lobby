#!/usr/bin/env bash
# Starts a copy of The Settlers III under Wine, for testing the lobby with the real game. Start
# the server first, from the repository root:
#   python3 -m s3lobby serve --dev --verbose --log-file lobby.log
# The game finds the server through the registry key described in README.md.
#
#   run-game-wine.sh --prefix DIR           the Wine prefix (or WINEPREFIX); default build/wine/prefix
#   run-game-wine.sh --game-folder DIR      the game in it (or S3_GAME_FOLDER); default PREFIX/drive_c/S3
#   run-game-wine.sh --monitor NAME         centre on this monitor, remembered for the next starts
#   run-game-wine.sh --window NAME          a second copy: NAME 2 opens a little lower and further right
#   run-game-wine.sh --size 1024x768        another window size
#   run-game-wine.sh --language en          the game's language, a two letter code (game-language.sh)
#   run-game-wine.sh --serial SERIAL        write this serial into the registry before each start
#   run-game-wine.sh --wine-debug           Wine's own messages too (errors and warnings of every part)
#   run-game-wine.sh --foreground           keep the shell until the game ends
#   run-game-wine.sh --stop                 end every copy running in the prefix (wineserver -k)
#
# Each copy runs in a Wine virtual desktop, a normal window on the monitor, and the game fills that
# window completely. The GOG copy draws through cnc-ddraw (ddraw.dll, settings in ddraw.ini next to
# S3.EXE); when that file exists this script sets its borderless full screen mode, which inside a
# Wine desktop fills only the desktop window and scales the game to it. Once the Wine desktop
# window appears, xdotool (if installed) moves it to the middle of the monitor. Monitor names come
# from xrandr --listmonitors; under Wayland a script cannot see which monitor is in use, hence the
# remembered choice; without one the primary monitor is used.
#
# The game starts in the background and the shell is free again at once; Wine's output goes to
# s3-NAME.log next to the prefix. Extra arguments after the options go to the game; the default
# -netmon traces in-game DirectPlay traffic to trace.lis in the game folder.
#
# The GOG copy saves a serial typed into it in a form it cannot read back, so it asks again at the
# next start; --serial puts the value back in the form the game reads (the serial four times).
set -euo pipefail

readonly WINE_DEBUG_CHANNELS="err+all,warn+all,fixme-all"
# Ubuntu keeps wineserver off the PATH.
readonly WINESERVER_PATH="/usr/lib/x86_64-linux-gnu/wine/wineserver"
# Window sizes to choose from, all 4:3 like the game's 800x600 (0x40d25a, 0x40d264): the largest
# that fits WINDOW_SHARE_PERCENT of the monitor, and the primary monitor as well, is used. Wine
# offers a virtual desktop only sizes that fit the primary monitor; it refused 1600x1200 next to a
# 1920x1080 primary ("Failed to set primary display settings") and went full screen instead.
readonly WINDOW_SIZES=("1280x960" "1024x768" "800x600")
readonly WINDOW_SHARE_PERCENT=85
# The window, and the game in it, at this share of the fitting size: 50 gives 640x480 on a
# 2560x1440 monitor (fitting size 1280x960). cnc-ddraw scales the game's 800x600 to the window.
readonly WINDOW_SCALE_PERCENT=50
# A second numbered window opens this much lower and further right than the first.
readonly WINDOW_CASCADE_PIXELS=48
# How long to wait for the Wine desktop window before giving up on moving it.
readonly WINDOW_WAIT_SECONDS=60
# How long GNOME needs to place a new window before a move sticks.
readonly WINDOW_SETTLE_SECONDS=1
# How long to wait for this start's S3.EXE to appear, and how often to look whether it ended.
readonly GAME_START_WAIT_SECONDS=60
readonly GAME_POLL_SECONDS=2
readonly SCRIPTS_FOLDER="$(cd "$(dirname "$0")" && pwd)"
readonly GAME_LANGUAGE_SCRIPT="$SCRIPTS_FOLDER/game-language.sh"
readonly DEFAULT_PREFIX="$SCRIPTS_FOLDER/../build/wine/prefix"
# The game's own registry key; 32 bit, so Wine keeps it under Wow6432Node.
readonly GAME_REGISTRY_KEY='HKLM\SOFTWARE\Bluebyte\Siedler3\1.0\General'
readonly SERIAL_REPEATS=4

window_name="1"
window_size=""
monitor_name=""
is_wine_debug=false
is_foreground=false
is_stop=false
language_code=""
prefix_folder="${WINEPREFIX:-$DEFAULT_PREFIX}"
game_folder="${S3_GAME_FOLDER:-}"
serial_number=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --window)
            window_name="${2:?--window needs a name, for example 1}"
            shift 2
            ;;
        --size)
            window_size="${2:?--size needs WIDTHxHEIGHT, for example 1024x768}"
            shift 2
            ;;
        --monitor)
            monitor_name="${2:?--monitor needs a name from xrandr --listmonitors, for example DP-3}"
            shift 2
            ;;
        --prefix)
            prefix_folder="${2:?--prefix needs a folder}"
            shift 2
            ;;
        --game-folder)
            game_folder="${2:?--game-folder needs a folder}"
            shift 2
            ;;
        --serial)
            serial_number="${2:?--serial needs a serial number}"
            shift 2
            ;;
        --language)
            language_code="${2:?--language needs a two letter code, for example en}"
            shift 2
            ;;
        --wine-debug)
            is_wine_debug=true
            shift
            ;;
        --foreground)
            is_foreground=true
            shift
            ;;
        --stop)
            is_stop=true
            shift
            ;;
        *)
            break
            ;;
    esac
done

WINEPREFIX="$(cd "$prefix_folder" && pwd)"
export WINEPREFIX
# Logs and the remembered monitor live next to the prefix.
here="$(dirname "$WINEPREFIX")"
readonly GAME_FOLDER="${game_folder:-$WINEPREFIX/drive_c/S3}"
readonly DDRAW_INI="$GAME_FOLDER/ddraw.ini"
readonly MONITOR_CHOICE_FILE="$here/.s3-monitor"
if [ ! -f "$GAME_FOLDER/S3.EXE" ]; then
    echo "No S3.EXE in $GAME_FOLDER; give the game's folder with --game-folder" >&2
    exit 1
fi

if [ "$is_stop" = true ]; then
    echo "Stopping every copy in $WINEPREFIX"
    "$WINESERVER_PATH" -k || true
    exit 0
fi

# Prints "LEFT TOP WIDTH HEIGHT" of the monitor named $1, or of the primary monitor when $1 is
# empty. Prints nothing when there is no such monitor.
monitor_geometry() {
    local wanted_name="$1"
    xrandr --listmonitors | awk -v name="$wanted_name" '
        NR > 1 {
            # A line looks like " 0: +*DP-4 1920/530x1080/300+0+360  DP-4"; "*" marks the primary.
            split($3, parts, "[/x+]")
            is_primary = index($2, "*") > 0
            if ((name == "" && is_primary) || (name != "" && $NF == name)) {
                print parts[5], parts[6], parts[1], parts[3]
                exit
            }
        }'
}

# The largest size of WINDOW_SIZES that fits WINDOW_SHARE_PERCENT of the monitor ($1 x $2) and
# all of the primary monitor ($3 x $4), as "WIDTH HEIGHT".
fitting_window_size() {
    local monitor_width="$1" monitor_height="$2" primary_width="$3" primary_height="$4" size width height
    for size in "${WINDOW_SIZES[@]}"; do
        width="${size%x*}"
        height="${size#*x}"
        local fits_monitor=false fits_primary=false
        if [ $((width * 100)) -le $((monitor_width * WINDOW_SHARE_PERCENT)) ] \
            && [ $((height * 100)) -le $((monitor_height * WINDOW_SHARE_PERCENT)) ]; then
            fits_monitor=true
        fi
        if [ "$width" -le "$primary_width" ] && [ "$height" -le "$primary_height" ]; then
            fits_primary=true
        fi
        if [ "$fits_monitor" = true ] && [ "$fits_primary" = true ]; then
            echo "$width $height"
            return
        fi
    done
    echo "800 600"
}

# Sets KEY=VALUE pairs in the [ddraw] section of ddraw.ini, keeping its CRLF line ends and every
# other line as it is.
set_ddraw_settings() {
    python3 - "$DDRAW_INI" "$@" <<'PYTHON'
import sys
path, pairs = sys.argv[1], sys.argv[2:]
wanted = dict(pair.split("=", 1) for pair in pairs)
with open(path, newline="") as file:
    lines = file.read().split("\r\n")
section = ""
for index, line in enumerate(lines):
    if line.startswith("["):
        section = line.strip().lower()
        continue
    key = line.split("=", 1)[0]
    if section == "[ddraw]" and "=" in line and key in wanted:
        lines[index] = f"{key}={wanted.pop(key)}"
if wanted:
    sys.exit(f"ddraw.ini has no {', '.join(wanted)} in its [ddraw] section")
with open(path, "w", newline="") as file:
    file.write("\r\n".join(lines))
PYTHON
}

# Waits for a window titled $1 and moves it to ($2, $3). GNOME places a new window itself right
# after it appears and overrides a move made before that (seen on this machine), so the move waits
# a moment and is repeated once. Runs in the background, so the shell is free while Wine starts.
move_window_when_shown() {
    local title="$1" position_x="$2" position_y="$3" window_id attempt
    window_id="$(timeout "$WINDOW_WAIT_SECONDS" xdotool search --sync --onlyvisible --name "$title" 2>/dev/null | head -n 1)" || return 0
    if [ -z "$window_id" ]; then
        return 0
    fi
    for attempt in 1 2; do
        sleep "$WINDOW_SETTLE_SECONDS"
        xdotool windowmove "$window_id" "$position_x" "$position_y"
    done
}

# The process ids of every running S3.EXE, one per line.
running_game_pids() {
    # The game's own command line starts with S3.EXE (or its Windows path); a Wine desktop's
    # "explorer.exe /desktop=... S3.EXE" only names it further on and must not count.
    pgrep -f '^(S3\.EXE|[A-Za-z]:\\.*\\S3\.EXE)( |$)' || true
}

# Waits for the S3.EXE that this start launched (one that was not running before, given in $2 as
# a list), waits until it ends, and then closes what Wine leaves behind. A Wine desktop stays open
# while any process in it lives, and DirectPlay's dplaysvr.exe, started for a match, outlives the
# game. With no other copy running everything in the prefix ends (wineserver -k); with another
# copy still playing only this copy's desktop window closes, so that copy keeps its DirectPlay.
close_desktop_after_game() {
    local title="$1" pids_before="$2" game_pid="" waited=0 pid
    while [ -z "$game_pid" ] && [ "$waited" -lt "$GAME_START_WAIT_SECONDS" ]; do
        sleep "$GAME_POLL_SECONDS"
        waited=$((waited + GAME_POLL_SECONDS))
        for pid in $(running_game_pids); do
            if ! grep -qx "$pid" <<<"$pids_before"; then
                game_pid="$pid"
            fi
        done
    done
    if [ -z "$game_pid" ]; then
        return 0
    fi
    while kill -0 "$game_pid" 2>/dev/null; do
        sleep "$GAME_POLL_SECONDS"
    done
    if [ -z "$(running_game_pids)" ]; then
        "$WINESERVER_PATH" -k || true
        return 0
    fi
    local window_id
    for window_id in $(xdotool search --name "^$title - Wine Desktop\$" 2>/dev/null); do
        xdotool windowclose "$window_id" || true
    done
}

if [ "$is_wine_debug" = true ]; then
    export WINEDEBUG="$WINE_DEBUG_CHANNELS"
else
    export WINEDEBUG="${WINEDEBUG:--all}"
fi

if [ -n "$monitor_name" ]; then
    echo "$monitor_name" >"$MONITOR_CHOICE_FILE"
elif [ -f "$MONITOR_CHOICE_FILE" ]; then
    monitor_name="$(cat "$MONITOR_CHOICE_FILE")"
fi
geometry="$(monitor_geometry "$monitor_name")"
if [ -z "$geometry" ]; then
    echo "No monitor called '$monitor_name'; these exist:" >&2
    xrandr --listmonitors >&2
    exit 1
fi
read -r monitor_left monitor_top monitor_width monitor_height <<<"$geometry"
read -r _ _ primary_width primary_height <<<"$(monitor_geometry "")"

echo "Wine prefix:  $WINEPREFIX"
echo "WINEDEBUG:    $WINEDEBUG"
echo "Monitor:      ${monitor_name:-primary} (${monitor_width}x${monitor_height} at $monitor_left,$monitor_top)"

if [ -n "$window_size" ]; then
    window_width="${window_size%x*}"
    window_height="${window_size#*x}"
else
    read -r window_width window_height <<<"$(fitting_window_size "$monitor_width" "$monitor_height" "$primary_width" "$primary_height")"
    window_width=$((window_width * WINDOW_SCALE_PERCENT / 100))
    window_height=$((window_height * WINDOW_SCALE_PERCENT / 100))
fi
cascade=0
if [[ "$window_name" =~ ^[0-9]+$ ]] && [ "$window_name" -gt 1 ]; then
    cascade=$(((window_name - 1) * WINDOW_CASCADE_PIXELS))
fi
position_x=$((monitor_left + (monitor_width - window_width) / 2 + cascade))
position_y=$((monitor_top + (monitor_height - window_height) / 2 + cascade))
# cnc-ddraw's borderless full screen mode (windowed and fullscreen together, size 0): it stretches
# the game to fill the screen it sees, which inside a Wine desktop is only the desktop window. A
# plain cnc-ddraw window of the desktop's size draws the game at its own 800x600 and does not
# shrink it (seen on this machine with a 640x480 desktop). The full screen seen with a 1600x1200
# desktop came from Wine refusing that desktop size, not from this mode.
if [ -f "$DDRAW_INI" ]; then
    set_ddraw_settings windowed=true fullscreen=true border=false savesettings=0 width=0 height=0 posX=0 posY=0
fi
desktop_title="S3-$window_name"
echo "Display:      Wine desktop $desktop_title, ${window_width}x${window_height} at $position_x,$position_y, game fills it"
log_file="$here/s3-$window_name.log"

if [ -n "$serial_number" ]; then
    repeated_serial=""
    for _ in $(seq "$SERIAL_REPEATS"); do
        repeated_serial+="$serial_number"
    done
    wine reg add "$GAME_REGISTRY_KEY" /v SingleNumber /t REG_SZ /d "$repeated_serial" /f /reg:32 >/dev/null
    echo "Serial:       written to the registry"
fi

# The game reads its language once at start, so setting it here applies to this copy; a copy
# already running keeps the language it started with.
if [ -n "$language_code" ]; then
    # Assigned first, so an unknown code stops the start (set -e does not see a failure inside echo).
    language_result="$("$GAME_LANGUAGE_SCRIPT" "$GAME_FOLDER" "$language_code")"
    echo "Language:     $language_result"
fi

cd "$GAME_FOLDER"
if [ "$#" -eq 0 ]; then
    set -- -netmon
fi
command=(wine explorer "/desktop=$desktop_title,${window_width}x${window_height}" S3.EXE "$@")
echo "Command:      ${command[*]}"

if [ "$is_foreground" = true ]; then
    echo "Running in the foreground; the shell returns when the game ends."
    exec "${command[@]}"
fi

# setsid detaches the game from this shell, so closing the terminal does not end it either.
pids_before_start="$(running_game_pids)"
setsid "${command[@]}" >"$log_file" 2>&1 </dev/null &
echo "Started:      process $!"
if command -v xdotool >/dev/null; then
    move_window_when_shown "$desktop_title" "$position_x" "$position_y" >/dev/null 2>&1 </dev/null &
    disown
fi
setsid bash -c "$(declare -f running_game_pids close_desktop_after_game); \
    GAME_START_WAIT_SECONDS=$GAME_START_WAIT_SECONDS GAME_POLL_SECONDS=$GAME_POLL_SECONDS \
    WINESERVER_PATH='$WINESERVER_PATH' close_desktop_after_game '$desktop_title' '$pids_before_start'" \
    >/dev/null 2>&1 </dev/null &
echo "Cleanup:      the Wine window closes when the game ends"
echo "Wine output:  $log_file"
echo "Follow it:    tail -f '$log_file'"
echo "Stop it:      $0 --stop   (ends every copy in this prefix)"
