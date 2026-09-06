#!/usr/bin/env bash
# Poll the hot-release watchlist and, if it is switched on, place the holds.
#
#   ./hotwatch.sh                        # check + dry-run the holds
#   SHELFWALK_PLACE_HOLDS=1 ./hotwatch.sh   # …and actually place them
#
# Runs far more often than refresh.sh (four-hourly, not daily) because that is
# the timescale a new release's queue moves on: Taipei Story went from record
# creation to 60 holds at SCCL inside a few weeks, and every hour of delay in
# the days around publication is a place in the queue.
#
# Placing holds is deliberately opt-in via the environment, not a flag baked in
# here — see systemd/shelfwalk-hotwatch.service. The dry run prints exactly
# what the live run would do.
#
# Same main()-wrapper trick as refresh.sh: bash reads a script lazily, so
# wrapping the body means an edit mid-run can't resume at a stale byte offset.
set -uo pipefail

main() {
    cd "$(dirname "$(readlink -f "$0")")" || exit 1

    # systemd/cron give a minimal PATH; uv lives in ~/.local/bin
    export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

    mkdir -p logs
    local log="logs/hotwatch-$(date +%Y-%m).log"

    # Never two at once: a check that overlaps a hold placement could read a
    # stale sighting and queue the same title twice at different systems.
    exec 9>>logs/.hotwatch.lock
    if ! flock -n 9; then
        echo "=== $(date -Is) skipped — another hotwatch holds the lock" >>"$log"
        return 0
    fi

    local out rc
    out=$(uv run hotlist.py check --quiet 2>&1)
    rc=$?
    {
        echo "=== $(date -Is) check exit $rc"
        [ -n "$out" ] && echo "$out"
    } >>"$log"

    # exit 10 is hotlist.py's "something moved" — the only time this is worth
    # interrupting anyone over
    if [ "$rc" = 10 ] && [ -n "$out" ]; then
        notify "$out"
    fi

    if [ "${SHELFWALK_PLACE_HOLDS:-0}" = 1 ]; then
        uv run hotlist.py holds --place >>"$log" 2>&1
    else
        uv run hotlist.py holds >>"$log" 2>&1
    fi

    find logs -name 'hotwatch-*.log' -mtime +120 -delete 2>/dev/null
}

# Desktop notification when there is one to send, otherwise just the log. A
# user timer inherits no session bus, so the address has to be reconstructed.
notify() {
    local body="$1"
    command -v notify-send >/dev/null 2>&1 || return 0
    : "${DBUS_SESSION_BUS_ADDRESS:=unix:path=/run/user/$(id -u)/bus}"
    export DBUS_SESSION_BUS_ADDRESS
    notify-send -u normal -i accessories-dictionary \
        "shelfwalk: a watched title moved" "$body" 2>/dev/null || true
}

main "$@"
