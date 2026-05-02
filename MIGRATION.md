# Migration Notes

## What Changed

- Added `src/driftpilot/` as the new autonomous operator runtime.
- Added SQLite-backed operator state, transitions, slots, positions, orders, fills, candidate queue, daily counters, and stream state.
- Added Alpaca paper/live broker abstraction with SIP stream guard, boot reconciliation, marketable-limit order flow, and live-gate checks.
- Added shared intraday signal math for live and backtest.
- Added slot allocator and paper fill slippage model.
- Added backtest replay/report harness.
- Replaced the Operator page with a read-only autonomous dashboard shell.
- Added Backtest and Admin console views using the same dark operator-console design language.

## Why It Changed

The old workflow was manual: review top candidates, approve trades, and let scheduler jobs manage pieces independently. The target workflow is a continuous state-machine operator that can explain why it is trading or not trading at any moment.

## Legacy Path

Code under `src/trading_bot/` remains for the existing PEAD, LLM, diagnostics, and manual admin harness. New autonomous trading code lives under `src/driftpilot/`.

No legacy trading path has been deleted yet. Obsolete paths should be archived only after the full acceptance suite passes and the dashboard/API migration is reviewed.

## Safety Changes

- Paper-only by default.
- Live mode requires explicit gates.
- PDT floor defaults to `$26,000`.
- Slippage is applied to paper/backtest fills.
- Time stop, target, stop, sector cap, and allocator lock are represented in the new runtime plan.
- Operator UI no longer contains normal manual confirm buttons.

## Phase 12 Databento Backtest Data

- Required cache command: `python scripts/databento_pull.py --start 2024-01-01 --end 2024-12-31 --dataset EQUS.MINI --symbols-file config/sector_map.csv`.
- The cache layout is `data/bars/databento/{symbol}/{year}.parquet` with UTC `timestamp`, `symbol`, `open`, `high`, `low`, `close`, and `volume` columns.
- `EQUS.MINI` is the default Databento dataset because it provides U.S. equities `ohlcv-1m` aggregates and covers NMS stocks. Confirm this remains the intended strategy-validation dataset before treating Phase 12 as complete.
- The default symbol universe is `config/sector_map.csv` plus `SPY` as the market-regime heartbeat. Replace this with a point-in-time universe before relying on production research conclusions.
- Databento point-in-time constituents were not available in this worktree, so generated reports must include the survivorship-bias caveat until a historical constituent source is connected.
- After cache population, run `python -m driftpilot.backtest --start 2024-01-01 --end 2024-12-31 --bar-root data/bars/databento --output expectancy_report.json`.
