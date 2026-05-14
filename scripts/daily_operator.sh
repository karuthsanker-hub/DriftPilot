#!/usr/bin/env bash
# Daily DriftPilot operator launch — meant to run at ~9:25 ET via cron/launchd.
#
# 1. Pre-warm catalyst DB with 2 weeks of news
# 2. Enrich with Qwen sentiment (DGX)
# 3. Kill any stale processes
# 4. Launch operator in paper-live mode
# 5. Launch slot manager (Qwen-powered health monitor)
# 6. Launch mid-day catalyst refresh loop (keeps event pool fresh all day)
#
# Usage: bash scripts/daily_operator.sh
# Schedule: crontab -e → 25 9 * * 1-5 cd "/Users/karuthsanker/Documents/Trading BOT" && bash scripts/daily_operator.sh

set -euo pipefail
cd "/Users/karuthsanker/Documents/Trading BOT"

LOGDATE=$(TZ=America/New_York date +%Y%m%d)
LOGFILE="logs/operator_${LOGDATE}.log"
mkdir -p logs/archive

echo "[$(date)] === daily_operator.sh starting ===" >> "$LOGFILE"

# ── Defect-list guardrails ───────────────────────────────────────────
# Keep daily launch defaults aligned with docs/DEFECTS.md until the broader
# catalyst mix is revalidated in paper-live.
export CATALYST_ENABLED=true
export ACTIVE_SIGNAL="earnings_report_v1,filing_8a_v1"
export MAX_TRADES_PER_SYMBOL_PER_DAY=3
export MAX_HOLD_MINUTES=45
export DAILY_LOSS_LIMIT_PCT=0.03
export OPERATOR_PAPER_CAPITAL=10000
export SCAN_INTERVAL_SECONDS=30

echo "[$(date)] defect guardrails: active_signal=$ACTIVE_SIGNAL trades/sym/day=$MAX_TRADES_PER_SYMBOL_PER_DAY hold=${MAX_HOLD_MINUTES}m loss_limit=${DAILY_LOSS_LIMIT_PCT} scan_interval=${SCAN_INTERVAL_SECONDS}s" >> "$LOGFILE"

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

# ── Pre-warm catalyst DB (2 weeks of Alpaca news) ───────────────────
START_DATE=$(TZ=America/New_York date -v-14d +%Y-%m-%d)
END_DATE=$(TZ=America/New_York date +%Y-%m-%d)
echo "[$(date)] pre-warming catalyst DB: $START_DATE to $END_DATE" >> "$LOGFILE"
./.venv/bin/python scripts/load_2024_catalyst_events.py \
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
$TIMEOUT_CMD ./.venv/bin/python scripts/enrich_catalyst_events.py \
    --db data/driftpilot/catalyst_events.sqlite3 \
    --priority-only --concurrency 32 \
    >> "$LOGFILE" 2>&1 || echo "[$(date)] WARNING: Qwen enrichment failed (DGX down?)" >> "$LOGFILE"

# ── Ensure dashboard is running on :8000 ─────────────────────────────
DASH_PID=$(lsof -ti :8000 2>/dev/null || true)
if [ -z "$DASH_PID" ]; then
    echo "[$(date)] starting dashboard on :8000" >> "$LOGFILE"
    PYTHONPATH=src ./.venv/bin/python -m uvicorn trading_bot.dashboard.app:app \
        --host 127.0.0.1 --port 8000 >> logs/dashboard.log 2>&1 &
    echo "[$(date)] dashboard started PID=$!" >> "$LOGFILE"
else
    echo "[$(date)] dashboard already running PID=$DASH_PID" >> "$LOGFILE"
fi

# ── Launch operator ──────────────────────────────────────────────────
echo "[$(date)] launching operator" >> "$LOGFILE"
./.venv/bin/python -u -m driftpilot.operator \
    --paper-live >> "$LOGFILE" 2>&1 &
echo $! > logs/operator.pid
echo "[$(date)] operator launched PID=$(cat logs/operator.pid)" >> "$LOGFILE"

# ── Launch Slot Manager (Qwen-powered health monitor) ────────────────
echo "[$(date)] launching slot manager" >> "$LOGFILE"
./.venv/bin/python -u scripts/slot_manager.py --daemon --interval 60 \
    >> logs/slot_manager.log 2>&1 &
echo $! > logs/slot_manager.pid
echo "[$(date)] slot manager launched PID=$(cat logs/slot_manager.pid)" >> "$LOGFILE"

# ── Launch Mid-Day Catalyst Refresh Loop ─────────────────────────────
# Runs every 90 minutes (5400s) to pull fresh news from Alpaca and enrich
# with Qwen. Without this, pre-market events expire by 1:25 PM and the
# engine starves for the last 2.5 hours of trading.
echo "[$(date)] launching catalyst refresh loop (interval=5400s)" >> "$LOGFILE"
bash scripts/midday_catalyst_refresh.sh --loop 5400 &
echo $! > logs/catalyst_refresh.pid
echo "[$(date)] catalyst refresh launched PID=$(cat logs/catalyst_refresh.pid)" >> "$LOGFILE"

echo "[$(date)] === all processes launched ===" >> "$LOGFILE"
