#!/usr/bin/env bash
# Mid-day catalyst refresh — runs periodically during market hours to keep
# the catalyst event pool fresh. Without this, pre-market events loaded at
# 9:25 AM expire after 240 minutes (~1:25 PM), leaving 2.5 hours of dead time.
#
# Pulls the last 2 days of news from Alpaca (idempotent — skips duplicates),
# then enriches any unenriched events with Qwen sentiment.
#
# Usage:
#   bash scripts/midday_catalyst_refresh.sh          # run once
#   bash scripts/midday_catalyst_refresh.sh --loop    # run every 90 min until killed
#
# Schedule alongside operator: launched from daily_operator.sh

set -euo pipefail
cd "/Users/karuthsanker/Documents/Trading BOT"

LOGDATE=$(TZ=America/New_York date +%Y%m%d)
LOGFILE="logs/operator_${LOGDATE}.log"

refresh_once() {
    local START_DATE END_DATE
    START_DATE=$(TZ=America/New_York date -v-2d +%Y-%m-%d 2>/dev/null || date -d "2 days ago" +%Y-%m-%d)
    END_DATE=$(TZ=America/New_York date +%Y-%m-%d)

    echo "[$(date)] [CATALYST-REFRESH] loading events: $START_DATE to $END_DATE" >> "$LOGFILE"
    ./.venv/bin/python scripts/load_2024_catalyst_events.py \
        --start "$START_DATE" --end "$END_DATE" \
        --output data/driftpilot/catalyst_events.sqlite3 \
        >> "$LOGFILE" 2>&1 || {
        echo "[$(date)] [CATALYST-REFRESH] WARNING: news load failed" >> "$LOGFILE"
        return 1
    }

    echo "[$(date)] [CATALYST-REFRESH] enriching with Qwen sentiment" >> "$LOGFILE"
    # Use timeout to prevent Qwen hangs from blocking the refresh loop
    if command -v gtimeout &>/dev/null; then
        TIMEOUT_CMD="gtimeout 180"
    elif command -v timeout &>/dev/null; then
        TIMEOUT_CMD="timeout 180"
    else
        TIMEOUT_CMD=""
    fi
    $TIMEOUT_CMD ./.venv/bin/python scripts/enrich_catalyst_events.py \
        --db data/driftpilot/catalyst_events.sqlite3 \
        --priority-only --concurrency 32 \
        >> "$LOGFILE" 2>&1 || {
        echo "[$(date)] [CATALYST-REFRESH] WARNING: Qwen enrichment failed (DGX down?)" >> "$LOGFILE"
    }

    # Log how many tradeable events exist now
    ./.venv/bin/python -c "
import sqlite3, datetime
conn = sqlite3.connect('data/driftpilot/catalyst_events.sqlite3')
cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=240)).isoformat()
total = conn.execute('SELECT COUNT(*) FROM catalyst_events WHERE event_ts >= ?', (cutoff,)).fetchone()[0]
positive = conn.execute('SELECT COUNT(*) FROM catalyst_events WHERE event_ts >= ? AND sentiment IN (\"positive\", \"bullish\")', (cutoff,)).fetchone()[0]
symbols = conn.execute('SELECT COUNT(DISTINCT symbol) FROM catalyst_events WHERE event_ts >= ? AND sentiment IN (\"positive\", \"bullish\")', (cutoff,)).fetchone()[0]
print(f'[CATALYST-REFRESH] pool: {total} events in 4h window, {positive} positive, {symbols} unique symbols')
conn.close()
" >> "$LOGFILE" 2>&1

    echo "[$(date)] [CATALYST-REFRESH] refresh complete" >> "$LOGFILE"
}

# Main entry
if [ "${1:-}" = "--loop" ]; then
    INTERVAL=${2:-5400}  # default 90 minutes
    echo "[$(date)] [CATALYST-REFRESH] loop mode: interval=${INTERVAL}s" >> "$LOGFILE"
    while true; do
        refresh_once || true
        echo "[$(date)] [CATALYST-REFRESH] sleeping ${INTERVAL}s until next refresh" >> "$LOGFILE"
        sleep "$INTERVAL"
    done
else
    refresh_once
fi
