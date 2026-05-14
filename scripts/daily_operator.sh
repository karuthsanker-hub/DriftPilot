#!/usr/bin/env bash
# Daily DriftPilot operator launch — runs at ~9:25 ET via launchd.
#
# 1. Pre-warm catalyst DB with 2 weeks of news
# 2. Enrich with Qwen sentiment (DGX)
# 3. Kill any stale processes
# 4. Launch dashboard on :8501
# 5. Launch operator (services_live)
# 6. Launch mid-day catalyst refresh loop
#
# Usage: bash scripts/daily_operator.sh

set -euo pipefail
cd "/Users/karuthsanker/Documents/Trading BOT"

# Use the venv python explicitly
PYTHON="./.venv/bin/python"

LOGDATE=$(TZ=America/New_York date +%Y%m%d)
LOGFILE="logs/operator_${LOGDATE}.log"
mkdir -p logs/archive

echo "[$(date)] === daily_operator.sh starting ===" >> "$LOGFILE"

# ── Load .env if present ────────────────────────────────────────────
if [ -f .env ]; then
    set -a
    source .env
    set +a
    echo "[$(date)] loaded .env" >> "$LOGFILE"
fi

# ── Defect-list guardrails ───────────────────────────────────────────
export CATALYST_ENABLED=true
export MAX_TRADES_PER_SYMBOL_PER_DAY=3
export MAX_HOLD_MINUTES=45
export DAILY_LOSS_LIMIT_PCT=0.03
export OPERATOR_PAPER_CAPITAL=10000
export SCAN_INTERVAL_SECONDS=30

echo "[$(date)] guardrails: trades/sym/day=$MAX_TRADES_PER_SYMBOL_PER_DAY hold=${MAX_HOLD_MINUTES}m loss_limit=${DAILY_LOSS_LIMIT_PCT} scan_interval=${SCAN_INTERVAL_SECONDS}s" >> "$LOGFILE"

# ── Kill stale processes ─────────────────────────────────────────────
for PIDFILE in logs/operator.pid logs/slot_manager.pid logs/catalyst_refresh.pid; do
    if [ -f "$PIDFILE" ]; then
        OLD_PID=$(cat "$PIDFILE")
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "[$(date)] killing stale process PID=$OLD_PID ($PIDFILE)" >> "$LOGFILE"
            kill "$OLD_PID" 2>/dev/null || true
            sleep 1
        fi
        rm -f "$PIDFILE"
    fi
done

# Also kill any stale dashboard
DASH_PIDS=$(lsof -ti :8501 2>/dev/null || true)
if [ -n "$DASH_PIDS" ]; then
    echo "[$(date)] killing stale dashboard PID=$DASH_PIDS" >> "$LOGFILE"
    kill $DASH_PIDS 2>/dev/null || true
    sleep 1
fi

# ── Pre-warm catalyst DB (2 weeks of Alpaca news) ───────────────────
START_DATE=$(TZ=America/New_York date -v-14d +%Y-%m-%d)
END_DATE=$(TZ=America/New_York date +%Y-%m-%d)
echo "[$(date)] pre-warming catalyst DB: $START_DATE to $END_DATE" >> "$LOGFILE"
$PYTHON scripts/load_2024_catalyst_events.py \
    --start "$START_DATE" --end "$END_DATE" \
    --output data/driftpilot/catalyst_events.sqlite3 \
    >> "$LOGFILE" 2>&1 || echo "[$(date)] WARNING: catalyst pre-warm failed" >> "$LOGFILE"

# ── Enrich with Qwen sentiment ──────────────────────────────────────
echo "[$(date)] enriching with Qwen sentiment" >> "$LOGFILE"
if command -v gtimeout &>/dev/null; then
    TIMEOUT_CMD="gtimeout 300"
elif command -v timeout &>/dev/null; then
    TIMEOUT_CMD="timeout 300"
else
    TIMEOUT_CMD=""
fi
$TIMEOUT_CMD $PYTHON scripts/enrich_catalyst_events.py \
    --db data/driftpilot/catalyst_events.sqlite3 \
    --priority-only --concurrency 32 \
    >> "$LOGFILE" 2>&1 || echo "[$(date)] WARNING: Qwen enrichment failed (DGX down?)" >> "$LOGFILE"

# ── Start dashboard on :8501 ─────────────────────────────────────────
echo "[$(date)] starting dashboard on :8501" >> "$LOGFILE"
PYTHONPATH=src $PYTHON -m uvicorn trading_bot.dashboard.app:app \
    --host 0.0.0.0 --port 8501 >> logs/dashboard.log 2>&1 &
echo "[$(date)] dashboard started PID=$!" >> "$LOGFILE"

# ── Launch operator (services_live) ──────────────────────────────────
echo "[$(date)] launching operator (services_live)" >> "$LOGFILE"
$PYTHON -u -m driftpilot.services_live >> "$LOGFILE" 2>&1 &
echo $! > logs/operator.pid
echo "[$(date)] operator launched PID=$(cat logs/operator.pid)" >> "$LOGFILE"

# ── Launch Mid-Day Catalyst Refresh Loop ─────────────────────────────
echo "[$(date)] launching catalyst refresh loop (interval=5400s)" >> "$LOGFILE"
bash scripts/midday_catalyst_refresh.sh --loop 5400 &
echo $! > logs/catalyst_refresh.pid
echo "[$(date)] catalyst refresh launched PID=$(cat logs/catalyst_refresh.pid)" >> "$LOGFILE"

echo "[$(date)] === all processes launched ===" >> "$LOGFILE"
