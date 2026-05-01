# DriftPilot

DriftPilot is being refactored into a continuous autonomous intraday paper-trading operator. Paper mode is the default. Live execution is gated and must pass the configured live-deploy checklist before orders are allowed.

## How To Run Paper

1. Configure Alpaca paper credentials in `.env`.
2. Keep `MODE=paper`.
3. Start the dashboard:

```bash
uv run uvicorn trading_bot.dashboard.app:create_app --factory --reload
```

4. Open `http://127.0.0.1:8000/`.

The autonomous runtime code lives under `src/driftpilot/`. The legacy manual PEAD workflow remains available from Admin while the migration finishes.

## How To Read The Dashboard

- **Operator** renders `/api/operator/state` as a read-only console: state, regime, heartbeat, slots, ranked queue, recycle log, and equity curve.
- **Backtest** renders `/api/backtest/report` from `expectancy_report.json` when present, otherwise from mock data shaped like the real report.
- **Admin** renders `/api/admin/state`: system health, manual override controls, broker reconciliation status, event log, and safe configuration.
- **LLM** remains the provider settings page. LLMs are not part of the trading loop in this refactor.

## How Live Deploy Works

`MODE=live` is rejected unless all live-gate predicates pass:

- 12-month backtest expectancy after costs is positive.
- 60 paper-trading days have positive cumulative P&L and Sharpe > 1.0.
- Account equity is at least `EQUITY_FLOOR + LIVE_EQUITY_BUFFER`.
- `LIVE_OK=true` is explicitly set.

Paper mode ignores the PDT floor for trading, but the operator still reports what would have happened.

## Backtest

Run the Phase 5 harness with cached 1-minute bars:

```bash
python -m driftpilot.backtest --start 2024-01-01 --end 2024-12-31
```

It writes `expectancy_report.json`, reuses live signal code, and applies the same slippage formula as paper fills: `max(0.02, 0.0005 * price)`.
