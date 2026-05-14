#!/usr/bin/env bash
# Daily DriftPilot operator shutdown — meant to run at ~16:05 ET via cron/launchd.
#
# 1. Kill all daemons (catalyst refresh, slot manager, operator)
# 2. Run EOD analysis
# 3. Generate Qwen EOD summary (lessons learned)
# 4. Archive the day's log
#
# Usage: bash scripts/daily_stop.sh

set -euo pipefail
cd "/Users/karuthsanker/Documents/Trading BOT"

LOGDATE=$(TZ=America/New_York date +%Y%m%d)
LOGFILE="logs/operator_${LOGDATE}.log"

echo "[$(date)] === daily_stop.sh ===" >> "$LOGFILE"

# ── Kill all daemons ─────────────────────────────────────────────────
for PIDFILE in logs/catalyst_refresh.pid logs/slot_manager.pid logs/operator.pid; do
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE")
        LABEL=$(basename "$PIDFILE" .pid)
        if kill -0 "$PID" 2>/dev/null; then
            echo "[$(date)] stopping $LABEL PID=$PID" >> "$LOGFILE"
            kill "$PID" 2>/dev/null || true
        fi
        rm -f "$PIDFILE"
    fi
done
sleep 3  # let operator finish final cycle

# ── EOD analysis (best-effort) ───────────────────────────────────────
echo "[$(date)] running EOD analysis" >> "$LOGFILE"
./.venv/bin/python scripts/analyze_paper_trading_day.py --include-alpaca-snapshot \
    >> "$LOGFILE" 2>&1 || echo "[$(date)] WARNING: EOD analysis failed" >> "$LOGFILE"

# ── Qwen EOD summary (best-effort) ──────────────────────────────────
echo "[$(date)] generating Qwen EOD summary" >> "$LOGFILE"
./.venv/bin/python scripts/eod_qwen_summary.py \
    >> "$LOGFILE" 2>&1 || echo "[$(date)] WARNING: Qwen EOD summary failed" >> "$LOGFILE"

# ── Archive ──────────────────────────────────────────────────────────
cp "$LOGFILE" "logs/archive/" 2>/dev/null || true
echo "[$(date)] day complete, log archived" >> "$LOGFILE"
