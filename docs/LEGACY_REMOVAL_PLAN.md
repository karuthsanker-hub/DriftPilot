# Legacy Code Removal Plan

**Date:** May 13, 2026  
**Goal:** Remove all `trading_bot` legacy code that DriftPilot no longer uses.  
**Risk:** The dashboard (`src/trading_bot/dashboard/app.py`) currently imports from both systems. Legacy endpoints must be removed from `app.py` first, or the dashboard will fail to start after deleting modules.

---

## Current State

The repository contains two coexisting systems:

| System | Path | Purpose | Status |
|--------|------|---------|--------|
| **DriftPilot** | `src/driftpilot/` | Autonomous catalyst-driven paper-trading | **Active** |
| **trading_bot** | `src/trading_bot/` | Supabase-backed PEAD/momentum scanner | **Legacy, unused** |

**Key finding:** Zero DriftPilot code imports from `trading_bot`. The only consumer of `trading_bot` modules is the dashboard `app.py` (for legacy endpoints) and legacy tests.

---

## Removal Order

The removal must happen in dependency order. Deleting modules before removing their imports from `app.py` will crash the dashboard.

### Step 1: Clean the Dashboard (`app.py`)

Remove all legacy API endpoints, imports, and the `TradingSchedulerService` from `src/trading_bot/dashboard/app.py`.

**Legacy imports to remove:**

```python
# DELETE these imports from app.py
from trading_bot.backtesting import BacktestTrade, run_backtest, run_split_backtest
from trading_bot.config import EnvConfigStore                    # AUDIT — see Step 7
from trading_bot.data.earnings_events import EarningsEventStore
from trading_bot.data.macro_data import FredMacroDataProvider
from trading_bot.data.provider_factory import create_market_data_provider
from trading_bot.data.repositories import StrategyConfigRepository, TradingRepository
from trading_bot.data.supabase_client import create_supabase_client
from trading_bot.diagnostics import run_env_diagnostics
from trading_bot.execution.alpaca_broker import AlpacaBroker
from trading_bot.execution.paper_engine import PaperExecutionEngine
from trading_bot.llm.factory import active_adapter, adapter_for_provider
from trading_bot.llm.models import EveningInput, ProviderName, ProviderSettings, ProviderStatus
from trading_bot.operator import approve_paper_trades, build_top_bets, momentum_rows_to_operator_rows
from trading_bot.scanners.pead_scanner import PEADScanner
from trading_bot.scheduler import TradingSchedulerService
from trading_bot.sentiment import FinBERTSentimentScorer, KeywordSentimentScorer
from trading_bot.settings import load_settings
from trading_bot.strategies.risk import evaluate_daily_pause
from trading_bot.universe import load_pead_universe
```

**Legacy endpoints to remove from `app.py`:**

| Endpoint | Method | What It Does | Why Remove |
|----------|--------|-------------|------------|
| `/api/watchlist` | GET | Supabase watchlist (PEAD) | Supabase-only |
| `/api/momentum-scores` | GET | Supabase momentum scores | Supabase-only |
| `/api/trades` | GET | Supabase trades | Supabase-only |
| `/api/daily-summaries` | GET | Supabase daily summaries | Supabase-only |
| `/api/backtest/trades` | POST | Backtest from Supabase trades | Supabase-only |
| `/api/strategy-config` | GET | Supabase strategy config | Supabase-only |
| `/api/diagnostics` | GET | Legacy env diagnostics | Supabase/Finnhub heavy |
| `/api/universe/pead` | GET | PEAD scanner universe | Legacy scanner |
| `/api/earnings-events` | GET | Legacy CSV earnings events | Legacy data source |
| `/api/earnings-events/import` | POST | Import from Finnhub | Legacy provider |
| `/api/scan-pead` | POST | Run PEAD scan | Legacy scanner |
| `/api/execute-pending` | POST | Execute Supabase pending trades | Legacy execution |
| `/api/operator/top-bets` | GET | Supabase candidates | Legacy operator |
| `/api/operator/open-positions` | GET | Supabase paper positions | Legacy execution |
| `/api/operator/performance` | GET | Supabase trade performance | Legacy metrics |
| `/api/operator/reset-paper-state` | POST | Reset Supabase paper state | Legacy execution |
| `/api/operator/qwen-review` | POST | Qwen review via old LLM layer | Legacy LLM path |
| `/api/operator/approve-paper-trades` | POST | Approve Supabase paper trades | Legacy execution |
| `/api/kill-switch` | POST | Supabase strategy kill switch | Supabase-only |
| `/api/scheduler` | GET | Legacy scheduler status | Legacy scheduler |
| `/api/scheduler/start` | POST | Start legacy scheduler | Legacy scheduler |
| `/api/scheduler/stop` | POST | Stop legacy scheduler | Legacy scheduler |
| `/api/scheduler/run/{job_id}` | POST | Run legacy scheduler job | Legacy scheduler |

**Also remove from `app.py`:**
- `TradingSchedulerService` instantiation in `create_app()`
- `scheduler_service.start()` / `scheduler_service.stop()` in lifespan
- `scheduler_service.status()` in health check
- All Pydantic models used only by legacy endpoints: `PEADScanRequest`, `ExecutePendingRequest`, `BacktestRequest`, `ApprovePaperTradesRequest`, `ResetPaperStateRequest`
- Helper functions: `_scan_dates()`, `_latest_daily_pnl_pct()`, `_operator_price()`, `_operator_candidate_rows()`, `_performance_payload()`, `_trade_with_pnl()`, `_number()`

**Endpoints to KEEP (DriftPilot):**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Dashboard page |
| `/admin` | GET | Admin page |
| `/backtest` | GET | Backtest page |
| `/agents` | GET | Agent dashboard |
| `/api/health` | GET | Health check |
| `/api/operator/state` | GET | DriftPilot operator state |
| `/api/backtest/report` | GET | DriftPilot backtest report |
| `/api/operator/news-ticker` | GET | Catalyst events ticker |
| `/api/catalyst/event/{id}` | GET | Catalyst event detail |
| `/api/operator/diagnostics` | GET | DriftPilot diagnostics |
| `/api/operator/pm-analysis` | GET | PM Analyst latest |
| `/api/operator/pm-analysis/run` | POST | Trigger PM analysis |
| `/api/operator/pm-analysis/history` | GET | PM analysis history |
| `/api/agents/dashboard` | GET | Agent states/decisions |
| `/api/agents/decision/{id}` | GET | Decision detail |
| `/api/agents/export/stats` | GET | Training export stats |
| `/api/admin/state` | GET | Admin state |
| `/api/admin/runtime-config` | GET | Runtime config |
| `/api/admin/runtime-config` | POST | Update runtime config |
| `/api/admin/override/{action}` | POST | Manual overrides |

**Endpoints to AUDIT (decide keep or rewrite):**

| Endpoint | Method | Issue |
|----------|--------|-------|
| `/llm` | GET | LLM settings page — uses legacy `llm/` provider layer |
| `/settings` | GET/POST | LLM provider settings — uses legacy `config.py` |
| `/settings/providers/{p}/health` | GET | Provider health — uses legacy LLM adapters |
| `/api/providers/health` | GET | All providers health — same |
| `/api/operator/settings` | GET/POST | Uses legacy `AppSettings` — consolidate into `DriftPilotSettings` |

---

### Step 2: Remove Legacy Strategy & Scanner Packages

**Delete entirely:**

```
src/trading_bot/strategies/
  ├── __init__.py           # PEAD, momentum, risk evaluation
  ├── pead.py               # PEAD earnings surprise strategy
  ├── momentum.py           # Momentum scoring
  ├── indicators.py         # EMA, ATR, avg volume helpers
  ├── sizing.py             # Position sizing
  └── risk.py               # VIX/drawdown pause logic

src/trading_bot/scanners/
  ├── __init__.py           # PEAD and momentum scanners
  ├── pead_scanner.py       # PEAD earnings scanner
  └── momentum_scanner.py   # Momentum scanner
```

**Why safe:** DriftPilot has its own signal system at `src/driftpilot/signals/` with separate implementations for earnings, momentum, etc.

---

### Step 3: Remove Legacy Execution Package

**Delete:**

```
src/trading_bot/execution/
  ├── paper_engine.py       # Supabase-backed paper execution engine
  ├── risk_gates.py         # Legacy risk gate class
  └── alpaca_broker.py      # Legacy Alpaca wrapper (DriftPilot has broker/alpaca_client.py)
```

**Keep:** `__init__.py` only if needed for package structure. Otherwise delete entire directory.

---

### Step 4: Remove Legacy Data Package

**Delete entirely:**

```
src/trading_bot/data/
  ├── __init__.py
  ├── supabase_client.py        # Supabase connection factory
  ├── repositories.py           # TradingRepository, StrategyConfigRepository (Supabase CRUD)
  ├── market_data.py            # MarketDataProvider protocol, CompanyProfile, EarningsEvent
  ├── provider_factory.py       # create_market_data_provider()
  ├── hybrid_market_data.py     # HybridMarketDataProvider (multi-source)
  ├── replacement_stack.py      # Finnhub + FMP + Alpaca + Polygon provider
  ├── earnings_events.py        # CSV-based earnings event store
  └── macro_data.py             # FRED API (VIX data)
```

**Why safe:** DriftPilot uses:
- SQLite (`driftpilot/storage/repositories.py`) instead of Supabase
- Alpaca REST quotes (`driftpilot/market_data/rest_quotes.py`) instead of Finnhub/FMP/Polygon
- Catalyst DB (`driftpilot/catalyst/db.py`) instead of CSV earnings events
- State machine risk logic instead of FRED VIX checks

---

### Step 5: Remove Legacy Top-Level Modules

**Delete:**

```
src/trading_bot/
  ├── operator.py           # Legacy Supabase operator (build_top_bets, approve_paper_trades)
  ├── scheduler.py          # APScheduler-based PEAD/momentum scan scheduler
  ├── backtesting.py        # Backtest from Supabase trades
  ├── sentiment.py          # KeywordSentimentScorer, FinBERTSentimentScorer
  ├── universe.py           # load_pead_universe() from CSV
  └── cli.py                # Legacy CLI (scan-pead, diagnostics, etc.)
```

**Why safe:**
- DriftPilot operator: `driftpilot/operator.py` + `driftpilot/state_machine.py`
- DriftPilot scheduling: built into the state machine asyncio loop
- DriftPilot backtest: `driftpilot/backtest/replay.py`
- DriftPilot sentiment: Qwen via `driftpilot/catalyst/qwen_enricher.py`
- DriftPilot universe: `driftpilot/catalyst/universe_filter.py`

---

### Step 6: Remove Legacy LLM Service Files

**Delete:**

```
src/trading_bot/llm/
  ├── service.py            # StrategyLLMService (morning/evening analysis)
  ├── prompts.py            # Legacy morning/evening prompts
  └── analysis_prompts.py   # Nightly/weekly/monthly review prompts
```

**Why safe:** DriftPilot has its own LLM client at `driftpilot/agents/llm_client.py` and PM Analyst prompts in `driftpilot/agents/pm_analyst.py`.

---

### Step 7: Audit & Consolidate Shared Infrastructure

These modules are used by dashboard infrastructure and need careful migration:

#### `src/trading_bot/settings.py`

**Problem:** `AppSettings` is imported by dashboard for operator settings endpoints (`/api/operator/settings`). Some fields overlap with `DriftPilotSettings`:

| AppSettings field | DriftPilotSettings equivalent | Action |
|-------------------|-------------------------------|--------|
| `operator_paper_capital` | `paper_capital` | Consolidate |
| `operator_trade_slots` | `trade_slots` | Consolidate |
| `operator_target_pct` | (in runtime_config) | Move to runtime_config |
| `operator_stop_pct` | (in runtime_config) | Move to runtime_config |
| `operator_max_candidates` | Not needed (catalyst-driven) | Remove |
| `operator_refresh_interval_minutes` | Not needed (state machine) | Remove |
| `alpaca_api_key` | `alpaca_key_id` | Already in DriftPilotSettings |
| `alpaca_secret_key` | `alpaca_secret_key` | Already in DriftPilotSettings |

**Action:** Consolidate needed fields into `DriftPilotSettings`, rewrite dashboard operator settings endpoints to use it, then delete `trading_bot/settings.py`.

#### `src/trading_bot/config.py`

**Problem:** `EnvConfigStore` manages .env read/write for the dashboard LLM settings page.

**Action:** Move `EnvConfigStore` into `src/trading_bot/dashboard/config.py` (it's dashboard infrastructure, not trading logic). Alternatively, add .env write capability to `driftpilot/settings.py`.

#### `src/trading_bot/llm/` (base, models, factory, providers)

**Problem:** Powers the dashboard LLM settings page (`/llm`), provider health checks, and Qwen review.

**Decision required:**
- **Option A (quick):** Keep the LLM settings page and its provider layer. Move `llm/` under `dashboard/` since that's its only consumer.
- **Option B (clean):** Remove the LLM settings page entirely. DriftPilot's Qwen config is in `.env` and `runtime_config.json`. The settings page adds complexity for a feature that's rarely used.
- **Recommended:** Option B. Remove the LLM settings page and all of `trading_bot/llm/`. Qwen config lives in DriftPilot settings.

#### `src/trading_bot/diagnostics.py`

**Problem:** `run_env_diagnostics()` checks connectivity to Supabase, Finnhub, FMP, Polygon, FRED, Alpaca, Qwen. Only Alpaca and Qwen are relevant to DriftPilot.

**Action:** Write a new `driftpilot/diagnostics.py` that checks only DriftPilot dependencies (SQLite, Alpaca, Qwen, catalyst DB). Delete `trading_bot/diagnostics.py`.

---

### Step 8: Remove Legacy Tests

**Delete these test files** (all test `trading_bot` modules that will be gone):

```
tests/
  ├── test_diagnostics.py
  ├── test_openai_adapter.py
  ├── test_claude_adapter.py
  ├── test_qwen_adapter.py
  ├── test_llm_models.py
  ├── test_llm_factory.py
  ├── test_llm_service.py
  ├── test_analysis_prompts.py
  ├── test_strategy_pead.py
  ├── test_strategy_momentum.py
  ├── test_strategy_sizing_risk.py
  ├── test_strategy_indicators.py
  ├── test_pead_scanner.py
  ├── test_momentum_scanner.py
  ├── test_operator.py
  ├── test_scheduler.py
  ├── test_backtesting.py
  ├── test_repositories.py
  ├── test_earnings_events.py
  ├── test_execution_engine.py
  ├── test_alpaca_broker.py
  ├── test_settings.py
  ├── test_replacement_stack.py
  └── test_dashboard_settings.py
```

**Verify:** After deletion, run `python -m pytest tests/ -x -q` and confirm all remaining tests pass.

---

### Step 9: Remove Legacy Config & Migration Files

**Delete:**

```
config/pead_universe.csv                # PEAD scanner universe
config/earnings_events.csv              # Legacy CSV earnings store

migrations/
  ├── 001_supabase_schema.sql           # Supabase schema
  ├── 002_watchlist_execution_fields.sql
  ├── 003_watchlist_exited_status.sql
  └── 004_operator_reset_and_candidate_status.sql
```

**Audit:** `migrations/006_agent_layer.sql` — check if it's for Supabase or SQLite agents.

---

### Step 10: Clean `pyproject.toml` Dependencies

**Remove these dependencies:**

| Package | Why Remove |
|---------|-----------|
| `supabase>=2.10.0` | Only used by `trading_bot.data.supabase_client` |
| `fredapi>=0.5.0` | Only used by `trading_bot.data.macro_data` |
| `apscheduler>=3.10.0` | Only used by `trading_bot.scheduler` |

**Remove from optional dependencies:**

| Package | Why Remove |
|---------|-----------|
| `torch>=2.5.0` | Only for FinBERT scorer in `trading_bot.sentiment` |
| `transformers>=4.48.0` | Only for FinBERT scorer in `trading_bot.sentiment` |

**Audit (may still be needed):**

| Package | Used By | Action |
|---------|---------|--------|
| `google-genai` | Legacy Gemini adapter only | Remove if dropping LLM settings page |
| `anthropic` | Legacy Claude adapter only | Remove if dropping LLM settings page |
| `yfinance` | Check if DriftPilot uses it anywhere | Likely remove |
| `openai` | Legacy Qwen adapter AND DriftPilot `agents/llm_client.py` | Keep only if DriftPilot uses it (check — DriftPilot uses httpx directly) |

---

## Removal Summary

### By the Numbers

| Category | Files | Estimated LOC |
|----------|-------|---------------|
| Legacy modules (`src/trading_bot/`) | ~30 files | ~6,000 |
| Legacy endpoints (in `app.py`) | ~25 endpoints | ~500 |
| Legacy tests | ~24 files | ~3,000 |
| Legacy config/migrations | ~6 files | ~200 |
| Legacy dependencies | ~5 packages | — |
| **Total removable** | **~85 files** | **~9,700 LOC** |

### What Remains After Cleanup

```
src/trading_bot/
  └── dashboard/
      ├── __init__.py
      ├── app.py              # ~400 LOC (down from ~1,020)
      └── templates/
          ├── dashboard.html   # Main operator dashboard
          ├── admin.html       # Admin controls
          ├── backtest.html    # DriftPilot backtest display
          └── agents.html      # Agent dashboard

src/driftpilot/               # Unchanged (22,750 LOC)
```

The `trading_bot` package shrinks from ~30 modules to just the dashboard. Consider renaming it to `driftpilot_dashboard` or moving it under `src/driftpilot/web/` for clarity.

---

## Execution Checklist

```
[ ] Step 1:  Remove legacy endpoints from app.py
[ ] Step 1:  Remove TradingSchedulerService from app.py lifespan
[ ] Step 1:  Remove legacy Pydantic models from app.py
[ ] Step 1:  Remove legacy helper functions from app.py
[ ] Step 1:  Verify dashboard starts and all DriftPilot endpoints work
[ ] Step 2:  Delete src/trading_bot/strategies/
[ ] Step 3:  Delete src/trading_bot/execution/ (paper_engine, risk_gates, alpaca_broker)
[ ] Step 4:  Delete src/trading_bot/data/ (entire directory)
[ ] Step 5:  Delete src/trading_bot/operator.py, scheduler.py, backtesting.py, sentiment.py, universe.py, cli.py
[ ] Step 6:  Delete src/trading_bot/llm/service.py, prompts.py, analysis_prompts.py
[ ] Step 7a: Consolidate AppSettings fields into DriftPilotSettings
[ ] Step 7b: Move EnvConfigStore into dashboard/ or remove
[ ] Step 7c: Decide: keep or remove LLM settings page + llm/ providers
[ ] Step 7d: Write driftpilot/diagnostics.py to replace legacy
[ ] Step 8:  Delete 24 legacy test files
[ ] Step 9:  Delete config/pead_universe.csv, config/earnings_events.csv, migrations/001-004
[ ] Step 10: Remove supabase, fredapi, apscheduler from pyproject.toml
[ ] Step 10: Remove torch, transformers from optional deps
[ ] Final:   Run full test suite — all DriftPilot tests must pass
[ ] Final:   Start dashboard — verify all endpoints return 200
[ ] Final:   Run operator for one cycle — verify no import errors
```

---

## Risk Mitigation

1. **Branch first.** Do all removal on a `cleanup/remove-legacy` branch.
2. **Step-by-step commits.** One commit per step so you can bisect if something breaks.
3. **Test after each step.** Run `python -m pytest tests/ -x -q` and `uvicorn trading_bot.dashboard.app:app` after each deletion.
4. **Keep a rollback path.** Don't force-push. The legacy code lives in git history.
5. **Dashboard smoke test.** After Step 1, manually verify every DriftPilot endpoint in the browser before proceeding.
