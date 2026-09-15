#!/usr/bin/env bash
# Refresh the acclaim corpus from every source that runs unattended.
#
#   ./acclaim.sh                 # every http-transport source
#
# Weekly, not daily: these are annual awards. The per-source `cadence` in
# acclaim.py is what keeps a prize that announces in November from being
# re-fetched every week in March.
#
# Browser-tier sources (Pulitzer, PEN, Douban, NYT, WSJ) need a real Chrome
# session and are not refreshed here; `acclaim.py browser-plan` lists them and
# docs/harvesting.md has the recipes.
#
# Body in main(): bash reads a script lazily, so an edit mid-run can't resume
# at a stale byte offset.
set -uo pipefail

main() {
    cd "$(dirname "$(readlink -f "$0")")" || exit 1
    export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

    mkdir -p logs
    local log="logs/acclaim-$(date +%Y-%m).log"

    # One writer at a time: a backfill can run for half an hour, and a second
    # writer on the same SQLite file gets 'database is locked'.
    exec 9>>logs/.acclaim.lock
    if ! flock -n 9; then
        echo "=== $(date -Is) skipped — another acclaim run holds the lock" >>"$log"
        return 0
    fi

    {
        echo "=== $(date -Is) acclaim refresh"
        uv run acclaim.py pull --all
        echo "=== exit $?"
        uv run acclaim.py browser-plan
    } >>"$log" 2>&1

    find logs -name 'acclaim-*.log' -mtime +180 -delete 2>/dev/null
}

main "$@"
