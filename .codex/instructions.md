# Codex Instructions — DriftPilot

You are working on **DriftPilot**, an autonomous intraday paper-trading operator. Before doing anything, read `CODEX_HANDOFF.md` in the repo root — it has the full project state, architecture, what's working, what's broken, and what to build next.

## Critical files to read first

1. `CODEX_HANDOFF.md` — **start here**. Current state, open bugs, next work items, architecture summary.
2. `docs/PROJECT_OVERVIEW.md` — component map, state machine, signal registry, ER schema (all mermaid diagrams).
3. `AGENTS.md` — hard rules that apply to all code changes.
4. `REFACTOR_PLAN.md` — the authoritative spec (~1500 lines). Reference when in doubt.
5. `docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md` — the catalyst layer design (v3, the active workstream).

## Hard rules

1. **All new code goes in `src/driftpilot/`.** Do not modify `src/trading_bot/` except the dashboard shell (`src/trading_bot/dashboard/`).
2. **All datetimes are timezone-aware.** Naive datetimes raise `ValueError`. Time logic comes from `src/driftpilot/clock.py` only.
3. **Strategy parameters are locked** in each signal's `config.py`. Do not "improve" or tune parameters.
4. **Slippage formula is constant**: `max($0.02/share, 0.0005 * price)`. Same in paper, live, and backtest.
5. **Same signal code in live and backtest.** No duplicate research math. The signal registry feeds both paths.
6. **No silent exception handlers.** Every `except` re-raises, logs, or has a comment justifying suppression.
7. **No new dependencies** without a one-line justification in `pyproject.toml`.
8. **Live mode is blocked** until the four-criterion live deploy gate passes. Do not bypass.
9. **`relative_volume` MUST exclude the current bar** from the lookback average (lookahead-bias guard).
10. **Tests must pass before any commit.** Run: `PYTHONPATH=src pytest -q`

## Code style

- Python 3.11+, type-annotated.
- Async-first for I/O; sync for pure computation.
- Repository pattern for storage; no SQL strings outside `src/driftpilot/storage/repositories.py`.
- Ruff for linting: `uvx ruff check src/driftpilot src/trading_bot/dashboard tests`
- Mypy for types: `PYTHONPATH=src uv run --with mypy mypy src/driftpilot src/trading_bot/dashboard`

## Project structure

```
src/driftpilot/           # The autonomous operator (active codebase)
  operator.py             # CLI entrypoint (--paper-live, --once, --mock-stream)
  observer.py             # Read-only observer mode (no orders)
  state_machine.py        # BOOT → SCANNING → ALLOCATING → IN_POSITION → EXITING → RECYCLING
  states.py               # OperatorState + BlockedReason enums
  settings.py             # Env-backed runtime settings
  clock.py                # Timezone-aware time owner
  services.py             # Mock/synthetic service builder
  services_live.py        # Live Alpaca service builder
  runtime_config.py       # Admin hot-reload tunables
  regime_detector.py      # SPY-based market regime
  broker/                 # Alpaca paper/live client + live gate
  market_data/            # SIP stream + REST quotes
  signals/                # Signal registry (7 signals registered)
    base.py               # SignalProtocol, Candidate, ExitDecision, BlockedReason
    __init__.py            # Registry + register_signal
    intraday_momentum.py   # v1 reference signal (FAIL)
    earnings_report_v1/    # Catalyst signal (GATED — edge_ratio 1.105)
    analyst_target_raise_v1/ # Catalyst signal (FAIL — consensus already priced in)
    (+ 4 technical signals)
  catalyst/               # News event bus, classifier, Qwen enricher, discovery
  execution/              # SlotAllocator + paper fills
  storage/                # SQLite schema + repositories
  backtest/               # Replay harness + report generation
  dashboard/              # API view models

src/trading_bot/          # Legacy — only dashboard shell is active
  dashboard/app.py        # FastAPI app serving the operator dashboard

config/                   # universe.csv, sector_map.csv
scripts/                  # Operational scripts (databento pull, DGX deploy, analysis)
tests/                    # 109 test files, 511+ passing tests
reports/                  # Backtest verdicts + paper trading day reports
```

## Environment

- Python managed via `uv`. Install: `uv sync --extra test`
- Alpaca paper account for broker + market data
- Qwen3-8B on DGX Spark (192.168.1.166) for catalyst sentiment enrichment
- SQLite for operator state (`data/driftpilot/operator_state.sqlite3`)
- Databento Parquet cache for backtests (`data/bars/databento/{SYMBOL}/{YEAR}.parquet`)

## Running tests

```bash
PYTHONPATH=src pytest -q
```

## Running the operator

```bash
# Synthetic smoke test (no credentials needed)
PYTHONPATH=src python -m driftpilot.operator --once --mock-stream

# Live paper trading (requires .env with Alpaca keys)
CATALYST_ENABLED=true ACTIVE_SIGNAL=earnings_report_v1 \
  python -m driftpilot.operator --paper-live

# Dashboard
PYTHONPATH=src uvicorn trading_bot.dashboard.app:app --port 8000 --reload
```

## When you hit ambiguity

If a decision is not covered by `REFACTOR_PLAN.md` or the signal's locked spec, append the question to `BLOCKED.md` and continue with non-blocked work. Do not improvise architectural decisions.
