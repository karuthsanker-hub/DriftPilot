# DriftPilot

DriftPilot is a research-grade autonomous intraday **paper-trading operator**. It scans a stock universe, ranks candidates with a pluggable signal, fills paper capital slots, exits on target/stop/time rules, recycles freed capital, and explains every state transition in an operator dashboard.

The project is intentionally paper-first. Live trading is blocked unless the live deploy gate passes.

> Current status: the active signal, `intraday_momentum_v1`, failed the 2024 after-cost backtest. Paper trading is still allowed with a warning so the operator loop can be observed, but live trading remains gated.

## What It Does

- Runs a state-machine operator for intraday paper trading.
- Maintains SQLite state for slots, positions, orders, fills, candidate queues, counters, and transitions.
- Uses Alpaca abstractions for paper/live broker integration and market data.
- Applies realistic paper/backtest slippage: `max($0.02/share, 5 bps of price)`.
- Enforces target, stop, time stop, sector cap, daily counters, and live deploy gates.
- Reuses the same signal code in backtest and runtime through a signal registry.
- Provides a dark operator-console dashboard with Operator, Admin, Backtest, and LLM tabs.

## Safety Model

DriftPilot defaults to paper mode.

Live mode is rejected unless all live-gate checks pass:

- 12-month after-cost backtest expectancy is positive.
- 60 paper-trading days have positive cumulative P&L and Sharpe > 1.0.
- Account equity is at least `EQUITY_FLOOR + LIVE_EQUITY_BUFFER`.
- `LIVE_OK=true` is explicitly set.

Paper mode may run a losing signal with a warning. Live mode may not.

## Repository Map

```text
src/driftpilot/
  operator.py                  # CLI entrypoint for the autonomous loop
  state_machine.py             # BOOT -> SCANNING -> ALLOCATING -> IN_POSITION...
  settings.py                  # env-backed runtime settings
  clock.py                     # timezone-aware time owner
  broker/alpaca_client.py      # Alpaca paper/live client and live gate
  market_data/alpaca_stream.py # SIP stream subscription model
  signals/                     # signal registry and signal implementations
  execution/                   # slot allocator and paper fill slippage
  storage/                     # SQLite schema and repositories
  backtest/                    # replay, metrics, and report generation
  dashboard/view_models.py     # API payloads for the dashboard

src/trading_bot/
  dashboard/                   # FastAPI dashboard shell and legacy admin APIs
  llm/                         # OpenAI/Claude/Gemini/Qwen provider adapters
  data/, strategies/, scanners/ # legacy/manual workflow support

config/
  universe.csv                 # current v1 universe

reports/
  <signal_name>/               # versioned backtest reports
```

## Quick Start

### 1. Install

```bash
uv sync --extra test
```

### 2. Configure

Create or update `.env`:

```env
MODE=paper
DRIFTPILOT_SQLITE_PATH=data/driftpilot/operator_state.sqlite3
ACTIVE_SIGNAL=intraday_momentum_v1

ALPACA_KEY_ID=your-paper-key
ALPACA_SECRET_KEY=your-paper-secret
ALPACA_DATA_FEED=sip

# Optional historical data
DATABENTO_API_KEY=db-...
```

### 3. Start The Dashboard

```bash
PYTHONPATH=src uv run uvicorn trading_bot.dashboard.app:app \
  --host 127.0.0.1 --port 8000 --reload
```

Open:

```text
http://127.0.0.1:8000/
```

### 4. Run A Synthetic Paper Cycle

In another terminal:

```bash
PYTHONPATH=src uv run python -m driftpilot.operator --once --mock-stream --env-file .env
```

Continuous synthetic paper loop:

```bash
PYTHONPATH=src uv run python -m driftpilot.operator --mock-stream --env-file .env
```

## Dashboard Tabs

- **Operator**: current state, regime, heartbeat, slots, candidate queue, P&L, recycle log, and state events.
- **Admin**: system health, broker reconciliation, manual overrides, event log, and safe configuration.
- **Backtest**: live-deploy gate, signal name/version, metrics, slippage waterfall, regime performance, equity curve, and caveats.
- **LLM**: provider settings for OpenAI, Claude, Gemini, and Qwen. LLMs are not currently part of the trading loop.

## Backtesting

Run a cached-bar replay:

```bash
PYTHONPATH=src uv run python -m driftpilot.backtest \
  --signal intraday_momentum_v1 \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --bar-root data/bars/databento
```

By default, reports are written to:

```text
reports/<signal_name>/<timestamp>_<verdict>.json
```

To overwrite the dashboard report:

```bash
PYTHONPATH=src uv run python -m driftpilot.backtest \
  --signal intraday_momentum_v1 \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --bar-root data/bars/databento \
  --output expectancy_report.json
```

## Pulling Databento Bars

```bash
PYTHONPATH=src uv run python scripts/databento_pull.py \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --dataset EQUS.MINI \
  --symbols-file config/universe.csv
```

The script performs a Databento cost check before the real pull.

## Signal Registry

Signals live under `src/driftpilot/signals/`.

The active signal is selected with:

```env
ACTIVE_SIGNAL=intraday_momentum_v1
```

Current signal:

- `intraday_momentum_v1`

Adding a new signal should not change the operator, allocator, broker, or dashboard contract. Implement `SignalProtocol`, register it, and run the same backtest harness with `--signal`.

## LLM Provider Adapters

The legacy dashboard includes provider-neutral LLM settings for:

- OpenAI
- Claude
- Gemini
- Qwen

These adapters are retained for future research/review workflows. The autonomous trading loop does not use LLM output for entry or exit decisions.

## Tests

Run all tests:

```bash
PYTHONPATH=src uv run --extra test pytest
```

Static checks:

```bash
uvx ruff check src/driftpilot src/trading_bot/dashboard tests
PYTHONPATH=src uv run --with mypy mypy src/driftpilot src/trading_bot/dashboard
```

Last verified locally:

```text
173 passed, 1 warning
```

## Important Caveats

- This is not financial advice.
- The current signal is not profitable in the included 2024 after-cost backtest.
- The v1 universe is current-constituent based and includes survivorship-bias caveats.
- Paper fills are modeled, not guaranteed to match live execution.
- Live trading should stay disabled until the full live gate passes and paper soak data supports promotion.

## Key Docs

- [REFACTOR_PLAN.md](REFACTOR_PLAN.md): authoritative operator/refactor plan.
- [MIGRATION.md](MIGRATION.md): what changed from the legacy workflow.
- [refactor.plan](refactor.plan): signal/algorithm swap plan.
- [AGENTS.md](AGENTS.md): agent instructions for future implementation work.
