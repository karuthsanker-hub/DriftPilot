#!/usr/bin/env bash
# Daily DriftPilot operator shutdown — meant to run at ~16:05 ET via cron/launchd.
#
# 1. Kill operator
# 2. Run EOD analysis
# 3. Archive the day's log
#
# Usage: bash scripts/daily_stop.sh

set -euo pipefail
cd "/Users/karuthsanker/Documents/Trading BOT"

LOGDATE=$(TZ=America/New_York date +%Y%m%d)
LOGFILE="logs/operator_${LOGDATE}.log"

echo "[$(date)] === daily_stop.sh ===" >> "$LOGFILE"

# Kill operator
if [ -f logs/operator.pid ]; then
    PID=$(cat logs/operator.pid)
    if kill -0 "$PID" 2>/dev/null; then
        echo "[$(date)] stopping operator PID=$PID" >> "$LOGFILE"
        kill "$PID" 2>/dev/null || true
        sleep 3
    fi
    rm -f logs/operator.pid
fi

# EOD analysis (best-effort)
echo "[$(date)] running EOD analysis" >> "$LOGFILE"
./.venv/bin/python scripts/analyze_paper_trading_day.py --include-alpaca-snapshot \
    >> "$LOGFILE" 2>&1 || echo "[$(date)] WARNING: EOD analysis failed" >> "$LOGFILE"

# Archive
cp "$LOGFILE" "logs/archive/" 2>/dev/null || true
echo "[$(date)] day complete, log archived" >> "$LOGFILE"
