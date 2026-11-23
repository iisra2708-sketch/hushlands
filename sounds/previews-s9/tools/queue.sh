#!/bin/bash
# usage: queue.sh world1 world2 ...  -> dispatch each when nothing is pending; exits after ~9 min
end=$(( $(date +%s) + ${QMAX:-540} ))
for w in "$@"; do
  while :; do
    s=$(gh api "repos/iisra2708-sketch/hushlands/actions/workflows/421287290/runs?per_page=3" --jq '[.workflow_runs[]|.status]|join(" ")')
    case "$s" in *pending*|*queued*) ;; *) gh api -X POST repos/iisra2708-sketch/hushlands/actions/workflows/421287290/dispatches -f ref=main -f "inputs[world]=$w" && echo "$(date -u +%H:%M) dispatched $w"; sleep 20; break;; esac
    [ $(date +%s) -gt $end ] && { echo "$(date -u +%H:%M) stop before $w ($s)"; exit 0; }
    sleep 30
  done
done
