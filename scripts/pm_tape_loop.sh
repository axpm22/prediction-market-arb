#!/usr/bin/env bash
# Duty-cycled Polymarket WS tape recorder, run to a deadline.
#
#   ./scripts/pm_tape_loop.sh [MINUTES] [RECORD_SECS] [PERIOD_SECS]
#   defaults: 4320 (3 days), 1800s recorded per 3600s period  -> ~41 GB
#
# Why a shell loop and not one long python call: pm_tape.record() has NO
# reconnect — arb/pm_tape.py breaks out of its receive loop on any socket drop
# and returns NORMALLY. A single long call would end at the first disconnect
# and report success. Each cycle here is a SEPARATE PROCESS, which also
# survives what an in-process try/except cannot: OOM, a hard interpreter
# crash, a wedged socket.
#
# The inner loop is a per-period BUDGET, not a single call, so a drop at 00:05
# resumes and keeps filling that period's 1800s instead of forfeiting the hour.
# Recording is duty-cycled rather than continuous because the cross-day split
# needs day DIVERSITY, not volume: the n>=600 bar is cleared in ~4 hours.
set -u
cd "$(dirname "$0")/.." || exit 1

MINUTES="${1:-4320}"
RECORD="${2:-1800}"
PERIOD="${3:-3600}"
LOG="logs/pm_tape_loop.log"
mkdir -p logs

END=$(( $(date +%s) + MINUTES * 60 ))
printf '=== pm-tape loop START %s minutes=%s record=%ss/period=%ss head=%s ===\n' \
  "$(date -u +%FT%TZ)" "$MINUTES" "$RECORD" "$PERIOD" "$(git rev-parse --short HEAD)" >> "$LOG"

while [ "$(date +%s)" -lt "$END" ]; do
  NOW=$(date +%s)
  PERIOD_END=$(( (NOW / PERIOD + 1) * PERIOD ))     # next period boundary
  ACC=0

  # Fill this period's recording budget, resuming across drops.
  while [ "$ACC" -lt "$RECORD" ]; do
    NOW=$(date +%s)
    [ "$NOW" -ge "$PERIOD_END" ] && break
    [ "$NOW" -ge "$END" ] && break
    WANT=$(( RECORD - ACC ))
    [ $(( PERIOD_END - NOW )) -lt "$WANT" ] && WANT=$(( PERIOD_END - NOW ))
    [ $(( END - NOW )) -lt "$WANT" ] && WANT=$(( END - NOW ))
    [ "$WANT" -lt 30 ] && break
    T0=$(date +%s)
    PYTHONPATH="$PWD" .venv/bin/python scripts/pm_tape_cycle.py "$WANT" >> "$LOG" 2>&1
    RC=$?
    ELAPSED=$(( $(date +%s) - T0 ))
    if [ "$RC" -eq 2 ]; then
      # Exit 2 = the socket was alive but silent (see pm_tape_cycle.py). Do NOT
      # credit it: an uncredited dead cycle is retried inside the same period,
      # so the run resumes on its own once the feed comes back instead of
      # booking 30 minutes of nothing as a completed budget.
      printf '{"ts": "%s", "dead_cycle_secs": %s, "not_credited": true}\n' \
        "$(date -u +%FT%TZ)" "$ELAPSED" >> "$LOG"
      sleep 10
    else
      ACC=$(( ACC + ELAPSED ))
      sleep 2   # do not hot-spin if the venue is refusing every connection
    fi
  done

  # Idle out the rest of the period.
  NOW=$(date +%s)
  [ "$NOW" -ge "$END" ] && break
  if [ "$PERIOD_END" -gt "$NOW" ]; then
    SLEEP=$(( PERIOD_END - NOW ))
    [ $(( END - NOW )) -lt "$SLEEP" ] && SLEEP=$(( END - NOW ))
    printf '{"ts": "%s", "idle_secs": %s, "recorded_secs": %s}\n' \
      "$(date -u +%FT%TZ)" "$SLEEP" "$ACC" >> "$LOG"
    sleep "$SLEEP"
  fi
done

printf '=== pm-tape loop END %s ===\n' "$(date -u +%FT%TZ)" >> "$LOG"
