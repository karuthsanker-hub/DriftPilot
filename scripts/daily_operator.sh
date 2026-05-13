#!/usr/bin/env bash
# Daily DriftPilot operator launch — meant to run at ~9:25 ET via cron/launchd.
#
# 1. Pre-warm catalyst DB with 2 weeks of news
# 2. Enrich with Qwen sentiment (DGX)
# 3. Kill any stale operator
# 4. Launch operator in paper-live mode
#
# Usage: bash scripts/daily_operator.sh
# Schedule: crontab -e → 25 9 * * 1-5 cd "/Users/karuthsanker/Documents/Trading BOT" && bash scripts/daily_operator.sh

set -euo pipefail
cd "/Users/karuthsanker/Documents/Trading BOT"

LOGDATE=$(TZ=America/New_York date +%Y%m%d)
LOGFILE="logs/operator_${LOGDATE}.log"
mkdir -p logs/archive

echo "[$(date)] === daily_operator.sh starting ===" >> "$LOGFILE"

# Kill stale operator if running
if [ -f logs/operator.pid ]; then
    OLD_PID=$(cat logs/operator.pid)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[$(date)] killing stale operator PID=$OLD_PID" >> "$LOGFILE"
        kill "$OLD_PID" 2>/dev/null || true
        sleep 2
    fi
    rm -f logs/operator.pid
fi

# Pre-warm catalyst DB (2 weeks of Alpaca news)
START_DATE=$(TZ=America/New_York date -v-14d +%Y-%m-%d)
END_DATE=$(TZ=America/New_York date +%Y-%m-%d)
echo "[$(date)] pre-warming catalyst DB: $START_DATE to $END_DATE" >> "$LOGFILE"
./.venv/bin/python scripts/load_2024_catalyst_events.py \
    --start "$START_DATE" --end "$END_DATE" \
    --output data/driftpilot/catalyst_events.sqlite3 \
    >> "$LOGFILE" 2>&1 || echo "[$(date)] WARNING: catalyst pre-warm failed" >> "$LOGFILE"

# Enrich with Qwen sentiment (best-effort — operator works without it)
echo "[$(date)] enriching with Qwen sentiment" >> "$LOGFILE"
# macOS doesn't ship `timeout`; use perl one-liner as fallback
if command -v gtimeout &>/dev/null; then
    TIMEOUT_CMD="gtimeout 300"
elif command -v timeout &>/dev/null; then
    TIMEOUT_CMD="timeout 300"
else
    TIMEOUT_CMD=""
fi
$TIMEOUT_CMD ./.venv/bin/python scripts/enrich_catalyst_events.py \
    --db data/driftpilot/catalyst_events.sqlite3 \
    --priority-only --concurrency 32 \
    >> "$LOGFILE" 2>&1 || echo "[$(date)] WARNING: Qwen enrichment failed (DGX down?)" >> "$LOGFILE"

# Ensure dashboard is running on :8000
DASH_PID=$(lsof -ti :8000 2>/dev/null || true)
if [ -z "$DASH_PID" ]; then
    echo "[$(date)] starting dashboard on :8000" >> "$LOGFILE"
    PYTHONPATH=src ./.venv/bin/python -m uvicorn trading_bot.dashboard.app:app \
        --host 127.0.0.1 --port 8000 >> logs/dashboard.log 2>&1 &
    echo "[$(date)] dashboard started PID=$!" >> "$LOGFILE"
else
    echo "[$(date)] dashboard already running PID=$DASH_PID" >> "$LOGFILE"
fi

# Launch operator
echo "[$(date)] launching operator" >> "$LOGFILE"
CATALYST_ENABLED=true ACTIVE_SIGNAL="earnings_report_v1,filing_8a_v1" \
    ./.venv/bin/python -u -m driftpilot.operator \
    --paper-live >> "$LOGFILE" 2>&1 &
echo $! > logs/operator.pid
echo "[$(date)] operator launched PID=$(cat logs/operator.pid)" >> "$LOGFILE"
