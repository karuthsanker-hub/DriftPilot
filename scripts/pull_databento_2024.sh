#!/usr/bin/env bash
# Pull Databento 1-minute bars for 2024 in the background.
# Aborts before any spend if Databento estimates more than $70 (dry-run was $62.21).
# Log lives at data/databento_pull.log; data lands in data/bars/databento/<SYMBOL>/2024.parquet.

set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p data

source .venv/bin/activate

nohup python3 scripts/databento_pull.py \
    --start 2024-01-01 \
    --end 2024-12-31 \
    --max-cost 70 \
    > data/databento_pull.log 2>&1 &

PID=$!
echo "Databento pull started in background. PID: $PID"
echo "Log: data/databento_pull.log"
echo ""
echo "Watch progress:   tail -f \"$(pwd)/data/databento_pull.log\""
echo "Count symbols:    ls data/bars/databento 2>/dev/null | wc -l"
echo "Stop the pull:    kill $PID"
