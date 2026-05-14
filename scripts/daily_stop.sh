#!/usr/bin/env bash
# Daily DriftPilot operator shutdown — runs at ~16:05 ET via launchd.
#
# 1. Kill all daemons (catalyst refresh, operator, dashboard)
# 2. Run EOD analysis
# 3. Generate Qwen EOD summary (lessons learned)
# 4. Archive the day's log
#
# Usage: bash scripts/daily_stop.sh

set -euo pipefail
cd "/Users/karuthsanker/Documents/Trading BOT"

PYTHON="./.venv/bin/python"

LOGDATE=$(TZ=America/New_York date +%Y%m%d)
LOGFILE="logs/operator_${LOGDATE}.log"

echo "[$(date)] === daily_stop.sh ===" >> "$LOGFILE"

# ── Load .env if present ────────────────────────────────────────────
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

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

# Kill dashboard on port 8501
DASH_PIDS=$(lsof -ti :8501 2>/dev/null || true)
if [ -n "$DASH_PIDS" ]; then
    echo "[$(date)] stopping dashboard PID=$DASH_PIDS" >> "$LOGFILE"
    kill $DASH_PIDS 2>/dev/null || true
fi

sleep 3  # let operator finish final cycle

# ── EOD analysis (best-effort) ───────────────────────────────────────
echo "[$(date)] running EOD analysis" >> "$LOGFILE"
$PYTHON scripts/analyze_paper_trading_day.py --include-alpaca-snapshot \
    >> "$LOGFILE" 2>&1 || echo "[$(date)] WARNING: EOD analysis failed" >> "$LOGFILE"

# ── Qwen EOD summary (best-effort) ──────────────────────────────────
echo "[$(date)] generating Qwen EOD summary" >> "$LOGFILE"
$PYTHON scripts/eod_qwen_summary.py \
    >> "$LOGFILE" 2>&1 || echo "[$(date)] WARNING: Qwen EOD summary failed" >> "$LOGFILE"

# ── Archive ──────────────────────────────────────────────────────────
cp "$LOGFILE" "logs/archive/" 2>/dev/null || true
echo "[$(date)] day complete, log archived" >> "$LOGFILE"
