# DriftPilot Operations

This is the practical runbook for local paper operation.

## Start Services

Dashboard:

```bash
PYTHONPATH=src uv run uvicorn trading_bot.dashboard.app:app \
  --host 127.0.0.1 --port 8000 --reload
```

One synthetic operator cycle:

```bash
PYTHONPATH=src uv run python -m driftpilot.operator --once --mock-stream --env-file .env
```

Continuous synthetic paper loop:

```bash
PYTHONPATH=src uv run python -m driftpilot.operator --mock-stream --env-file .env
```

## Expected Dashboard States

- `BOOT`: loading state, reconciling broker/local state.
- `MARKET_CLOSED`: no scan work; existing state remains visible.
- `REGIME_CHECK`: evaluating SPY market condition.
- `SCANNING`: building candidate queue.
- `ALLOCATING`: filling empty slots under allocator lock.
- `IN_POSITION`: monitoring open positions.
- `EXITING`: exit order/fill in progress.
- `RECYCLING`: slot is being freed for the next candidate.
- `HALTED_RISK`: risk halt, daily loss, or manual pause.
- `HALTED_PDT`: live-only PDT floor halt.
- `ERROR`: feed/broker/operator failure.

## Paper Reset

Use Admin -> Reset paper state when you want a clean paper session.

This writes a state-machine event and clears local paper positions. It should not be used in live mode.

## Backtest Refresh

```bash
PYTHONPATH=src uv run python -m driftpilot.backtest \
  --signal intraday_momentum_v1 \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --bar-root data/bars/databento \
  --output expectancy_report.json
```

Refresh the Backtest tab after the command completes.

## Troubleshooting

Dashboard says `Failed to fetch`:

- Confirm the FastAPI server is running.
- Confirm the browser URL matches the server port.
- Check `/api/operator/state` directly.

Operator stays in `MARKET_CLOSED`:

- The real market clock may be closed.
- Use `--mock-stream` for local synthetic testing.

No candidates:

- Check the active signal.
- Check `config/universe.csv`.
- Check whether SPY bars are stale or missing.

Backtest shows `FAIL`:

- Paper trading is still allowed.
- Live trading remains gated.
- Use the signal registry to test new signals before promotion.

## Useful Commands

```bash
git status --short
PYTHONPATH=src uv run --extra test pytest
uvx ruff check src/driftpilot src/trading_bot/dashboard tests
PYTHONPATH=src uv run --with mypy mypy src/driftpilot src/trading_bot/dashboard
```
