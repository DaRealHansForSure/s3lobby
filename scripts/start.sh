#!/usr/bin/env bash
# Start the s3lobby lobby server and its web interface together, in the background, from this
# checkout. The counterpart of scripts/stop.sh.
#
#   scripts/start.sh                  both, with the database s3lobby.sqlite3 in the checkout
#   scripts/start.sh --dev            both in development mode (password 1234, see README)
#   scripts/start.sh --follow         then show the lobby's log as it grows (Ctrl+C stops only
#                                     the following; both processes keep running)
#   scripts/start.sh --dev --follow   both together
#
# The server writes every datagram to lobby.log; what each process prints goes to
# lobby-console.log and web-console.log. All three are in the checkout and ignored by git.
# Stop both with scripts/stop.sh.
set -euo pipefail

readonly DATABASE="s3lobby.sqlite3"
readonly SERVER_LOG="lobby.log"
readonly SERVER_CONSOLE_LOG="lobby-console.log"
readonly WEB_CONSOLE_LOG="web-console.log"
readonly WEB_ADDRESS="http://127.0.0.1:8080"
# How long to wait before checking that both processes are still running: long enough for a
# start that fails (a port in use, a database error) to have ended.
readonly START_CHECK_SECONDS=2
# The same pattern scripts/stop.sh uses for a running server or web interface.
readonly PROCESS_PATTERN='^[^ ]*python[0-9.]* ([^ ]*/s3lobby|-m s3lobby) (serve|web)( |$)'

main() {
    local development_options=()
    local is_following=false
    local argument
    for argument in "$@"; do
        case "$argument" in
            --dev) development_options=(--dev) ;;
            --follow) is_following=true ;;
            *)
                echo "usage: $0 [--dev] [--follow]" >&2
                exit 2
                ;;
        esac
    done

    local checkout
    checkout="$(cd "$(dirname "$0")/.." && pwd)"
    cd "$checkout"

    if pgrep -u "$(id -u)" -f -- "${PROCESS_PATTERN}" >/dev/null; then
        echo "A server or web interface is running already; stop it first with scripts/stop.sh." >&2
        exit 1
    fi

    # The web interface needs Flask, which lives in the checkout's virtual environment.
    local python="python3"
    if [[ -x .venv/bin/python ]]; then
        python=".venv/bin/python"
    fi

    setsid "$python" -m s3lobby serve "${development_options[@]}" --database "$DATABASE" \
        --verbose --log-file "$SERVER_LOG" >"$SERVER_CONSOLE_LOG" 2>&1 </dev/null &
    local server_pid=$!
    setsid "$python" -m s3lobby web "${development_options[@]}" --database "$DATABASE" \
        >"$WEB_CONSOLE_LOG" 2>&1 </dev/null &
    local web_pid=$!

    sleep "$START_CHECK_SECONDS"
    local has_failed=false
    if ! kill -0 "$server_pid" 2>/dev/null; then
        echo "The lobby server did not start; its output:" >&2
        tail -n 20 "$SERVER_CONSOLE_LOG" >&2
        has_failed=true
    fi
    if ! kill -0 "$web_pid" 2>/dev/null; then
        echo "The web interface did not start; its output:" >&2
        tail -n 20 "$WEB_CONSOLE_LOG" >&2
        has_failed=true
    fi
    if [[ "$has_failed" == true ]]; then
        echo "Stop whatever did start with scripts/stop.sh." >&2
        exit 1
    fi

    local mode="normal mode"
    if [[ ${#development_options[@]} -gt 0 ]]; then
        mode="development mode (password 1234)"
    fi
    echo "Started in $mode, database $DATABASE:"
    echo "  lobby server   process $server_pid, UDP 3344, datagrams in $SERVER_LOG"
    echo "  web interface  process $web_pid, $WEB_ADDRESS"
    echo "Follow the lobby:  tail -f $SERVER_CONSOLE_LOG"
    echo "Stop both:         scripts/stop.sh"

    if [[ "$is_following" == true ]]; then
        echo
        echo "Following $SERVER_CONSOLE_LOG; Ctrl+C stops following, the lobby keeps running."
        # The lobby's own lines (logons, joins, games, ladder); the datagrams stay in $SERVER_LOG.
        # exec: Ctrl+C ends tail alone, the server and the web interface run in their own sessions.
        exec tail -n +1 -f "$SERVER_CONSOLE_LOG"
    fi
}

main "$@"
