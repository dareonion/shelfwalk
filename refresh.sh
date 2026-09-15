#!/usr/bin/env bash
# Re-check every want-list title at the default systems, regenerate the reports,
# and commit (optionally push) them. Run by the systemd user timer; safe by hand.
#
#   ./refresh.sh                                  # refresh + commit the reports
#   SHELFWALK_PUSH=1 ./refresh.sh                 # …and push
#   SHELFWALK_ARGS="--system sccl --limit 2" ./refresh.sh    # quick smoke test
#
# Body in main(): bash reads a script lazily, so an edit mid-run can't resume
# at a stale byte offset.
set -uo pipefail

main() {
    cd "$(dirname "$(readlink -f "$0")")" || exit 1

    # systemd/cron give a minimal PATH; uv lives in ~/.local/bin
    export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

    mkdir -p logs
    local log="logs/refresh-$(date +%Y-%m-%d).log"

    # One refresh at a time: a full pass takes ~30 min, so a daily timer plus a
    # hand-run (or a slow night) could otherwise stack two scrapes on one DB.
    exec 9>>logs/.refresh.lock
    if ! flock -n 9; then
        echo "=== $(date -Is) skipped — another refresh holds the lock" >>"$log"
        return 0
    fi

    {
        echo "=== $(date -Is) refresh starting ${SHELFWALK_ARGS:+(args: $SHELFWALK_ARGS)}"
        # shellcheck disable=SC2086  # deliberate word-splitting of the args knob
        uv run bayarea_lookup.py ${SHELFWALK_ARGS:-}
        echo "=== $(date -Is) lookup exit $?"
    } >>"$log" 2>&1

    # Commit only the generated reports (the files carrying report.py's banner),
    # never hand-written docs or anything else that happens to be staged.
    local reports
    mapfile -t reports < <(git grep -l 'AUTO-GENERATED from shelfwalk.db' -- '*.md')
    if [ "${#reports[@]}" -gt 0 ] && ! git diff --quiet -- "${reports[@]}"; then
        git commit -q -m "Refresh availability $(date +%Y-%m-%d)" \
            -- "${reports[@]}" >>"$log" 2>&1 \
            && echo "=== committed regenerated reports" >>"$log"
    else
        # normal overnight: the libraries were shut, so nothing moved
        echo "=== no report changes" >>"$log"
    fi

    # Push whatever is unpushed, not just this run's commit — a refresh that
    # committed while the network was down should get carried up the next day.
    if [ "${SHELFWALK_PUSH:-0}" = 1 ]; then
        local ahead
        ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
        if [ "$ahead" -gt 0 ]; then
            if git push -q >>"$log" 2>&1; then
                echo "=== pushed $ahead commit(s)" >>"$log"
            else
                echo "=== PUSH FAILED — $ahead commit(s) still local" >>"$log"
            fi
        fi
    fi

    find logs -name 'refresh-*.log' -mtime +30 -delete 2>/dev/null
    echo "=== $(date -Is) done" >>"$log"
}

main "$@"
