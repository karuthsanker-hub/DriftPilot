#!/usr/bin/env bash
# Launch all 4 v1 signal backtests in parallel, fully detached.
#
# WHY THIS EXISTS:
# Plain `nohup ... &` over SSH does NOT survive SSH disconnect on this DGX
# (no passwordless sudo, no `loginctl enable-linger`, no user systemd bus —
# `systemd-logind` reaps user processes on session end). `setsid -f` solves
# it: it forks the child into a new session with no controlling terminal,
# the immediate parent exits, and the child is reparented to PID 1 (init).
# logind has no session to associate it with, so it survives.
#
# USAGE (from DGX shell, after migrate/deploy):
#   bash scripts/run_all_backtests.sh
#
# OR from Mac terminal (one-shot):
#   ssh sankerkr@192.168.1.166 'bash ~/driftpilot/scripts/run_all_backtests.sh'
#
# OUTPUT:
#   - One log per signal at logs/<signal>_<YYYYMMDD_HHMMSS>.log
#   - Reports written to reports/<signal>/<date>_<verdict>.json on success
#
# MONITOR:
#   ssh sankerkr@192.168.1.166 'pgrep -af driftpilot.backtest'
#   ssh sankerkr@192.168.1.166 'tail -f ~/driftpilot/logs/<signal>_*.log'
#
# Confirm proper detachment by checking PPID=1:
#   ps -eo pid,ppid,etime,pcpu,pmem,rss,cmd | grep driftpilot.backtest

set -u

REPO_DIR="${REPO_DIR:-$HOME/driftpilot}"
PY="${REPO_DIR}/.venv/bin/python3"
PYTHONPATH_VAL="${REPO_DIR}/src"
BAR_ROOT="${BAR_ROOT:-${REPO_DIR}/data/bars/databento}"
START="${START:-2024-01-01}"
END="${END:-2024-12-31}"

cd "${REPO_DIR}"
mkdir -p logs reports
TS="$(date +%Y%m%d_%H%M%S)"

SIGNALS=(stationary_ghost_v1 whale_tail_v1 rs_drift_v1 apex_hunter_v2_2)

for sig in "${SIGNALS[@]}"; do
    LOG="${REPO_DIR}/logs/${sig}_${TS}.log"
    echo "starting $sig -> $LOG"
    PYTHONPATH="${PYTHONPATH_VAL}" setsid -f "${PY}" -u -m driftpilot.backtest \
        --signal "${sig}" \
        --start "${START}" --end "${END}" \
        --bar-root "${BAR_ROOT}" \
        < /dev/null > "${LOG}" 2>&1
done

sleep 2
echo "---- alive ----"
pgrep -af driftpilot.backtest
echo "---- new logs ----"
ls -la "${REPO_DIR}/logs/"*"_${TS}.log" 2>/dev/null
echo
echo "Detachment check (PPID should be 1 for survival):"
ps -eo pid,ppid,etime,cmd 2>&1 | grep -E "PID|driftpilot.backtest" | grep -v "grep -E"
