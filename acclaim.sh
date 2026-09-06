#!/usr/bin/env bash
# Refresh the acclaim corpus from every source that runs unattended.
#
#   ./acclaim.sh                 # every http-transport source
#
# Weekly, not daily: these are annual awards. The per-source `cadence` in
# acclaim.py is what keeps a prize that announces in November from being
# re-fetched every week in March.
#
# Browser-tier sources (Pulitzer, NYT, WSJ) are NOT touched here — they need a
# real Chrome session. `acclaim.py browser-plan` lists what has gone stale;
# docs/harvesting.md has the recipe.
#
# Same main()-wrapper as refresh.sh: bash reads a script lazily, so wrapping
# the body means an edit mid-run cannot resume at a stale byte offset.
set -uo pipefail

main() {
    cd "$(dirname "$(readlink -f "$0")")" || exit 1
    export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

    mkdir -p logs
    local log="logs/acclaim-$(date +%Y-%m).log"

    # One at a time. A backfill can run for half an hour and these all write to
    # the same SQLite file; two writers means 'database is locked', which is
    # exactly how the first NBA run died.
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
