#!/usr/bin/env bash
# Run all 4 v1 signal backtests SEQUENTIALLY, one at a time.
#
# WHY THIS EXISTS:
# `replay_parquet_cache_generic` currently calls `pd.concat(frames)` on every
# symbol's parquet before calling replay_bars — which means each process
# holds the full year-long universe DataFrame in RAM (~5-10 GB peak per
# process). On DGX-Spark with vllm Qwen3-8B running, 4 in parallel exhausts
# RAM and triggers silent OOM kills. Sequential runs give each backtest the
# full RAM budget and finish reliably (slower wall-clock, but they finish).
#
# A streaming per-symbol loader fix is tracked separately; until that lands,
# this is the durable path.
#
# USAGE:
#   ssh sankerkr@192.168.1.166 'bash ~/driftpilot/scripts/run_sequential_backtests.sh'
#
# SURVIVAL:
# The outer loop is spawned via setsid -f so it's reparented to PID 1 and
# survives SSH disconnect. It launches each backtest as a foreground child
# of the loop, waits for it, then moves on.
#
# OUTPUT:
#   ~/driftpilot/logs/sequential_<TS>.log     — outer loop log
#   ~/driftpilot/logs/<signal>_<TS>.log       — per-signal log
#   ~/driftpilot/reports/<signal>/<date>_<verdict>.json on success

set -u

REPO_DIR="${REPO_DIR:-$HOME/driftpilot}"
PY="${REPO_DIR}/.venv/bin/python3"
PYTHONPATH_VAL="${REPO_DIR}/src"
BAR_ROOT="${BAR_ROOT:-${REPO_DIR}/data/bars/databento}"
START="${START:-2024-01-01}"
END="${END:-2024-12-31}"

cd "${REPO_DIR}"
mkdir -p logs reports

SIGNALS=(stationary_ghost_v1 whale_tail_v1 rs_drift_v1 apex_hunter_v2_2)
OUTER_TS="$(date +%Y%m%d_%H%M%S)"
OUTER_LOG="${REPO_DIR}/logs/sequential_${OUTER_TS}.log"

# Body that runs sequentially. Spawned via setsid -f so it detaches.
_run_loop() {
    cd "${REPO_DIR}"
    {
        echo "=== sequential backtest run started at $(date -Is) ==="
        echo "PID $$"
        for sig in "${SIGNALS[@]}"; do
            sig_ts="$(date +%Y%m%d_%H%M%S)"
            sig_log="${REPO_DIR}/logs/${sig}_${sig_ts}.log"
            echo "[$(date -Is)] starting $sig -> $sig_log"
            PYTHONPATH="${PYTHONPATH_VAL}" "${PY}" -u -m driftpilot.backtest \
                --signal "${sig}" \
                --start "${START}" --end "${END}" \
                --bar-root "${BAR_ROOT}" \
                > "${sig_log}" 2>&1
            rc=$?
            echo "[$(date -Is)] $sig exited rc=$rc"
            if [ "$rc" -ne 0 ]; then
                echo "[$(date -Is)] WARN: $sig failed; continuing to next signal"
            fi
        done
        echo "=== sequential backtest run completed at $(date -Is) ==="
    } >> "${OUTER_LOG}" 2>&1
}

if [ "${1:-}" = "--inner" ]; then
    # Detached body
    _run_loop
    exit 0
fi

# Outer entry: detach the loop body so SSH disconnect doesn't kill it.
echo "Spawning detached sequential runner. Outer log: ${OUTER_LOG}"
setsid -f bash "$0" --inner < /dev/null > /dev/null 2>&1
sleep 1
echo "---- detached loop alive? ----"
pgrep -af "$0 --inner" || pgrep -af "run_sequential_backtests.sh --inner" | head -1 || echo "  (race; check OUTER_LOG)"
echo "---- outer log so far ----"
cat "${OUTER_LOG}" 2>/dev/null || echo "  (empty)"
echo
echo "Tail with: ssh sankerkr@192.168.1.166 'tail -f ${OUTER_LOG}'"
