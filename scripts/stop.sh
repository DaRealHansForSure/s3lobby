#!/usr/bin/env bash
# Stop every running s3lobby lobby server and web interface of this user.
#
#   scripts/stop.sh           ask them to stop (SIGTERM) and wait until they have
#   scripts/stop.sh --force   also kill (SIGKILL) whatever still runs after the wait
#
# SIGTERM is the clean way: the server tells every player the connection ends and removes its
# UPnP forwardings, exactly as with Ctrl+C. Started as "s3lobby serve" or "python -m s3lobby web"
# alike, from any folder.
set -euo pipefail

# The Python process itself, started as ".../bin/s3lobby serve" or "python -m s3lobby web": a shell
# or tmux wrapper whose command line merely mentions it is left alone and ends with its child.
readonly PROCESS_PATTERN='^[^ ]*python[0-9.]* ([^ ]*/s3lobby|-m s3lobby) (serve|web)( |$)'
readonly WAIT_SECONDS=10
readonly POLL_SECONDS=0.5

find_lobby_processes() {
    pgrep -u "$(id -u)" -f -- "${PROCESS_PATTERN}" || true
}

describe_processes() {
    local pid
    for pid in "$@"; do
        echo "  ${pid}  $(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || echo '(gone)')"
    done
}

wait_until_stopped() {
    local polls_left
    polls_left=$(awk -v wait="${WAIT_SECONDS}" -v poll="${POLL_SECONDS}" 'BEGIN { print int(wait / poll) }')
    while [[ -n "$(find_lobby_processes)" && "${polls_left}" -gt 0 ]]; do
        sleep "${POLL_SECONDS}"
        polls_left=$((polls_left - 1))
    done
}

main() {
    local is_forced=false
    if [[ "${1:-}" == "--force" ]]; then
        is_forced=true
    elif [[ -n "${1:-}" ]]; then
        echo "usage: $0 [--force]" >&2
        exit 2
    fi

    local pids
    mapfile -t pids < <(find_lobby_processes)
    if [[ "${#pids[@]}" -eq 0 ]]; then
        echo "No s3lobby server or web interface is running."
        exit 0
    fi

    echo "Stopping:"
    describe_processes "${pids[@]}"
    kill -TERM "${pids[@]}" 2>/dev/null || true
    wait_until_stopped

    local remaining
    mapfile -t remaining < <(find_lobby_processes)
    if [[ "${#remaining[@]}" -eq 0 ]]; then
        echo "All stopped."
        exit 0
    fi
    if [[ "${is_forced}" == true ]]; then
        echo "Still running after ${WAIT_SECONDS} seconds, killing:"
        describe_processes "${remaining[@]}"
        kill -KILL "${remaining[@]}" 2>/dev/null || true
        exit 0
    fi
    echo "Still running after ${WAIT_SECONDS} seconds:" >&2
    describe_processes "${remaining[@]}" >&2
    echo "Run $0 --force to kill them." >&2
    exit 1
}

main "$@"
