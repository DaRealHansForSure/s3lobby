#!/usr/bin/env bash
# Shows or changes the language a copy of The Settlers III believes it has, for testing the lobby
# with games of several languages on one machine.
#
#   scripts/game-language.sh GAME_FOLDER            show the language
#   scripts/game-language.sh GAME_FOLDER en         make the copy an English one
#   scripts/game-language.sh GAME_FOLDER --restore  put the original file back
#
# The game takes its language from its voice archive, not from the registry: the sound loader
# (0x5fe760) reads the u32 at offset 0x14 of each Snd\siedler3_NN.dat header and keeps the first
# that is not 0xffffffff (the effects archive siedler3_00 has 0xffffffff, the German voices of the
# GOG copy 0). The game copies it into its settings (0x498142), shows the texts of that language
# from the text archive, and reports it in its patch check, which the lobby uses for the chat
# themes of one language. The voices themselves stay in the language they were recorded in.
#
# Only the voice file's language field changes; the first change keeps the original as
# siedler3_NN.dat.orig, which --restore puts back. Use it on a copy of the game made for testing,
# never on an installation others rely on.
set -euo pipefail

# The language index of each two letter code: the order of the game's text archive
# (constants.GAME_LANGUAGES).
declare -rA LANGUAGE_INDEX_BY_CODE=(
    [de]=0
    [en]=1
    [it]=2
    [fr]=3
    [pl]=4
    [es]=5
    [ko]=6
    [ja]=7
)
readonly LANGUAGE_CODES_IN_ORDER=(de en it fr pl es ko ja)
# Every sound archive starts with this u32; anything else is not a file this script understands.
readonly SOUND_ARCHIVE_MAGIC=$((0x11544))
readonly LANGUAGE_FIELD_OFFSET=20
readonly LANGUAGE_FIELD_BYTES=4
readonly NO_LANGUAGE=$((0xffffffff))
readonly BACKUP_SUFFIX=".orig"

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

# Prints the u32 (little endian) at byte offset $2 of file $1.
read_u32() {
    od --address-radix=n --format=u4 --skip-bytes="$2" --read-bytes="$LANGUAGE_FIELD_BYTES" "$1" | tr -d ' '
}

# Writes the u32 $3 (little endian) at byte offset $2 of file $1, changing nothing else.
write_u32() {
    local file="$1" offset="$2" value="$3" escaped=""
    local byte_index
    for byte_index in 0 1 2 3; do
        escaped+="$(printf '\\x%02x' $(((value >> (8 * byte_index)) & 0xff)))"
    done
    printf "$escaped" | dd of="$file" bs=1 seek="$offset" conv=notrunc status=none
}

# The voice archive: the sound archive whose language field is not NO_LANGUAGE. The names differ
# in case between copies (Siedler3_00.dat next to siedler3_01.dat in the GOG copy).
find_voice_archive() {
    local sound_folder="$1" file
    while IFS= read -r file; do
        if [ "$(read_u32 "$file" 0)" != "$SOUND_ARCHIVE_MAGIC" ]; then
            continue
        fi
        if [ "$(read_u32 "$file" "$LANGUAGE_FIELD_OFFSET")" != "$NO_LANGUAGE" ]; then
            echo "$file"
            return
        fi
    done < <(find "$sound_folder" -maxdepth 1 -iname 'siedler3_*.dat' | sort)
}

if [ "$#" -lt 1 ]; then
    usage
fi
game_folder="$1"
wanted="${2:-}"

sound_folder="$(find "$game_folder" -maxdepth 1 -type d -iname snd | head -n 1)"
if [ -z "$sound_folder" ]; then
    echo "No Snd folder in $game_folder" >&2
    exit 1
fi

if [ "$wanted" = "--restore" ]; then
    restored=false
    for backup in "$sound_folder"/*"$BACKUP_SUFFIX"; do
        if [ -f "$backup" ]; then
            mv "$backup" "${backup%"$BACKUP_SUFFIX"}"
            echo "Restored ${backup%"$BACKUP_SUFFIX"}"
            restored=true
        fi
    done
    if [ "$restored" = false ]; then
        echo "Nothing to restore: no $BACKUP_SUFFIX file in $sound_folder"
    fi
    exit 0
fi

voice_archive="$(find_voice_archive "$sound_folder")"
if [ -z "$voice_archive" ]; then
    echo "No voice archive with a language in $sound_folder" >&2
    exit 1
fi
current_index="$(read_u32 "$voice_archive" "$LANGUAGE_FIELD_OFFSET")"
current_code="${LANGUAGE_CODES_IN_ORDER[$current_index]:-index $current_index}"

if [ -z "$wanted" ]; then
    echo "$current_code ($voice_archive)"
    exit 0
fi

wanted_index="${LANGUAGE_INDEX_BY_CODE[$wanted]:-}"
if [ -z "$wanted_index" ]; then
    echo "Unknown language '$wanted'; known: ${LANGUAGE_CODES_IN_ORDER[*]}" >&2
    exit 1
fi
if [ "$wanted_index" = "$current_index" ]; then
    echo "Already $wanted ($voice_archive)"
    exit 0
fi
if [ ! -f "$voice_archive$BACKUP_SUFFIX" ]; then
    cp -p "$voice_archive" "$voice_archive$BACKUP_SUFFIX"
fi
write_u32 "$voice_archive" "$LANGUAGE_FIELD_OFFSET" "$wanted_index"
echo "Language $current_code -> $wanted ($voice_archive; the game reads it at start)"
