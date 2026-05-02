# DriftPilot Operator Refactor Plan

## Goal

Refactor DriftPilot from the current manual, Supabase-backed, job-scheduled paper workflow into a continuous autonomous intraday paper-trading operator.

The new operator must:

- Run as one async state-machine loop during market hours.
- Stream Alpaca SIP bar/quote data over WebSocket.
- Maintain 10 fixed $1,000 slots from a $10,000 paper allocation.
- Enter long-only positions from a ranked intraday momentum queue.
- Exit every position on +1% target, -1% stop, or `MAX_HOLD_MINUTES = 45`.
- Recycle freed slots back into the candidate queue.
- Persist every state transition and position transition to local SQLite.
- Reconcile with Alpaca on boot before trusting local state.
- Keep paper mode as the default.
- Reject `MODE=live` unless all live gate criteria pass.

This document is a planning artifact only. No refactor code has been written yet.

## Current Repo Audit

### Current Shape

The repo currently implements a manual/semi-automated trading app:

- `src/trading_bot/operator.py`
  - Builds projected "top bets" from watchlist/momentum rows.
  - Requires selected IDs and manual approval.
  - Computes simple $1,000-ish allocation, target, stop, and projected P&L.
  - Does not own a durable autonomous state machine.

- `src/trading_bot/scheduler.py`
  - Uses APScheduler jobs for PEAD scans, pending entries, candidate refresh, and position monitoring.
  - Jobs are independent and hold implicit state through Supabase rows and in-memory scheduler state.
  - Current refresh cycle is minute-based REST work, not WebSocket-driven bar streaming.

- `src/trading_bot/execution/paper_engine.py`
  - Executes pending watchlist rows and manages exits.
  - Uses target/stop and a day-based time exit.
  - Does not apply realistic slippage.
  - Does not implement slot allocation, sector caps, PDT guard, boot reconciliation, or minute-based time stops.

- `src/trading_bot/execution/alpaca_broker.py`
  - Submits Alpaca paper market orders.
  - Explicitly blocks live mode with `RuntimeError("Live mode is blocked in v1")`.
  - Cancels conflicting open orders to reduce wash-trade rejections.
  - Does not yet expose account equity, positions reconciliation, fill tracking, or unified paper/live mode selection.

- `src/trading_bot/scanners/momentum_scanner.py`
  - Current momentum scanner is daily/fundamental momentum.
  - Uses 3-month/6-month returns plus earnings/fundamental scoring.
  - Not the requested intraday signal based on RVOL, VWAP, 15-minute return, spread, and SPY regime.

- `src/trading_bot/data/replacement_stack.py`
  - Uses Finnhub, FMP, Polygon, Alpaca REST, and retry/backoff.
  - Alpaca data calls currently use REST and `feed=iex`.
  - Historical bars are daily, not 1-minute intraday.

- `src/trading_bot/backtesting.py`
  - Small trade-list metrics engine.
  - Does not replay 1-minute bars.
  - Does not share signal code with live.
  - Does not generate `expectancy_report.json`.

- `src/trading_bot/dashboard/app.py`
  - FastAPI/Jinja dashboard with Operator, Admin, and LLM routes.
  - Operator routes still support top-bets, manual approval, reset, open positions, and performance.
  - Dashboard is not yet a read-only renderer of a state machine.

- `src/trading_bot/data/repositories.py`
  - Repository layer targets Supabase tables: `trades`, `daily_summary`, `watchlist`, `strategy_config`, `momentum_scores`.
  - New stack requires local SQLite for state and DuckDB/Parquet for bar history.

### Current Tests To Preserve Or Replace

- Preserve useful tests around settings, broker order intent mapping, diagnostics, LLM adapters, and dashboard API error handling where still applicable.
- Replace or rewrite tests that assume:
  - Supabase is the primary state store.
  - Manual approval is the normal operator path.
  - APScheduler jobs drive entry/exit.
  - Daily momentum scores are the operator candidate source.
  - Time stops are day-based.

## Target Architecture

### Proposed Package Layout

Add a new top-level runtime package while keeping existing PEAD/LLM code intact where possible:

```text
src/driftpilot/
  __init__.py
  settings.py
  state_machine.py
  states.py
  clock.py
  logging.py

  broker/
    alpaca_client.py
    models.py
    reconciliation.py
    live_gate.py

  market_data/
    alpaca_stream.py
    bars.py
    quotes.py
    universe.py
    sector_map.py

  signals/
    intraday_momentum.py
    regime.py
    features.py
    scoring.py

  execution/
    slot_allocator.py
    paper_fills.py
    position_monitor.py
    orders.py
    risk.py

  storage/
    sqlite.py
    schema.sql
    repositories.py
    parquet_cache.py

  backtest/
    __main__.py
    replay.py
    metrics.py
    report.py

  dashboard/
    view_models.py
```

Keep the existing `src/trading_bot/llm/` module for the LLM tab for now. The LLM layer stays out of the trading loop.

### Migration Strategy

Do not try to mutate the current manual path in place. Build the autonomous operator beside it, then switch the Operator tab to render the new state.

1. Keep current Admin and LLM tabs.
2. Move old manual operator functions behind Admin/manual override if still useful.
3. Introduce the new `driftpilot` runtime as the source of truth for autonomous paper trading.
4. Switch `/api/operator/*` endpoints to read from the new SQLite-backed state.
5. Retire Supabase from the operator path.

## State Machine Design

### States

Implement exactly these states:

- `BOOT`
- `MARKET_CLOSED`
- `REGIME_CHECK`
- `SCANNING`
- `ALLOCATING`
- `IN_POSITION`
- `EXITING`
- `RECYCLING`
- `HALTED_PDT`
- `HALTED_RISK`
- `ERROR`

Keep `ALLOCATING` as a separate state.

Rationale:

- allocation is a capital-moving critical section, not just scanner bookkeeping
- it makes allocator-lock contention visible in the event log
- it gives the dashboard a clear explanation when candidates exist but slots are being reserved/submitted
- it separates candidate discovery from slot reservation and order intent creation

### Transition Rules

Every transition must write:

```text
from_state
to_state
reason
timestamp
metadata_json
```

to SQLite before the dashboard sees it.

The dashboard must answer "why isn't it trading?" from:

- current state
- active gate predicate
- last transition reason
- last error, if any

### Runtime Loop

The state machine is one async event loop. Services do not run independently:

- Market data stream feeds an in-memory bar cache.
- Scanner computes features every `SCAN_INTERVAL_SECONDS = 30`.
- `driftpilot/clock.py` owns all time and timezone logic.
- All datetimes are timezone-aware.
- Allocator receives ranked candidates only from the state machine.
- Position monitor evaluates exits only when driven by the state machine.
- Exit handler submits exits, waits for fills, persists result, and returns control to the loop.

This replaces APScheduler for the operator loop.

### Data Freshness

SPY is the heartbeat for the operator.

If the latest SPY 1-minute bar is more than 60 seconds old:

- transition to `ERROR`
- block new entries
- continue attempting safe exits where broker connectivity allows
- show `ERROR: SPY bar stale, market data stream unhealthy`
- retry market-data subscription with backoff

## Storage Plan

### SQLite

Use SQLite for all operator state:

- `operator_state`
- `state_transitions`
- `slots`
- `positions`
- `orders`
- `fills`
- `candidate_queue`
- `recycle_events`
- `daily_pnl`
- `daily_counters`
- `live_gate_evaluations`
- `errors`
- `allocator_state`
- `universe`
- `sector_map`

SQLite writes must happen on every state/position/allocator transition.

### Daily Counters

Persist kill-switch counters in SQLite:

```text
daily_counters(date_et, counter_name, counter_value, updated_at)
```

Rules:

- primary key is `(date_et, counter_name)`
- counters reset only when the America/New_York calendar date changes
- counters do not reset on process restart
- `MAX_TRADES_PER_DAY` and `MAX_TRADES_PER_SYMBOL_PER_DAY` read from these counters before any new entry
- every increment is persisted before the related order transition is considered complete

### Parquet / DuckDB

Use Parquet for cached 1-minute historical bars:

```text
data/bars/databento/{symbol}/{year}.parquet
```

Use DuckDB only for querying/reporting over Parquet. Do not use DuckDB as the operator's live state database.

### Supabase

Remove Supabase from the operator execution path. Existing Supabase code can remain for legacy/admin views during migration, but the new operator should not require cloud DB connectivity.

## Broker And Market Data

### Alpaca

Alpaca becomes the single broker and live/paper market data source:

- Paper trading now.
- Same code path for live later.
- Use `MODE=paper` by default.
- Use `MODE=live` only if live deploy gate passes.
- Use Alpaca Algo Trader Plus SIP WebSocket feed for bars and quotes.

Required broker methods:

- `get_account()`
- `get_open_positions()`
- `get_open_orders()`
- `submit_entry_order()`
- `submit_exit_order()`
- `cancel_order()`
- `close_position()`
- `stream_order_updates()`

Required market data methods:

- `subscribe_bars(symbols)`
- `subscribe_quotes(symbols)`
- `latest_bar(symbol)`
- `latest_quote(symbol)`
- `session_bars(symbol)`

Phase 2 must verify Alpaca SIP WebSocket subscription limits against the full universe size before implementation is considered complete.

Use a two-tier subscription model:

- Always-on tier:
  - SPY
  - QQQ
  - all open positions
  - top `N` candidates from the previous cycle's ranking, default `N = 50`
- Discovery tier:
  - remaining eligible universe symbols
  - rotated by shard for discovery scans only

If the universe is larger than the supported subscription set or connection budget, shard only the discovery tier:

- Always-on symbols must not be evicted by shard rotation.
- Rotate lower-priority discovery shards on scanner cycles.
- Persist each shard cursor so restart does not repeatedly starve the same symbols.
- Surface `universe_partially_streamed` in the dashboard when sharding is active.
- Never use per-symbol REST polling as a substitute for stream sharding.

Allocator freshness rule:

- the allocator must reject any candidate whose latest bar is older than `SCAN_INTERVAL_SECONDS * 2`
- rejected candidates remain visible in the queue with blocked reason `stale_bar`
- this is separate from the global SPY heartbeat guard, which transitions the whole operator to `ERROR`

### Order Type

Default order type is marketable limit, not market.

Entry fallback rules:

- For long entry, submit a buy limit at `ask + slippage_allowance`.
- If quote is stale or missing, skip the candidate and log `quote_unavailable`.
- If not filled within `ENTRY_LIMIT_TIMEOUT_SECONDS`, cancel and recycle the slot.

Exit fallback rules:

- For long exit, submit a sell limit at `bid - slippage_allowance`.
- If quote is stale but the stop is breached on the latest bar, allow emergency market exit in paper and live.
- If an exit limit is not filled within `EXIT_LIMIT_TIMEOUT_SECONDS`, cancel and replace once.
- If the replacement exit order is still not filled within another `EXIT_LIMIT_TIMEOUT_SECONDS`, use emergency market exit.
- Every fallback is logged to the state transition and order logs.

### Paper Fill Slippage

Paper fills must never use mid-price.

Slippage:

```text
slippage = max(0.02, 0.0005 * price)
```

For long entries:

```text
fill_price = reference_price + slippage
```

For long exits:

```text
fill_price = reference_price - slippage
```

Persist applied slippage on every fill.

## Universe

### Required Universe

Universe is S&P 1500 minus exclusions, refreshed weekly.

Filters:

- `min_price = 5`
- `min_avg_daily_volume = 1_000_000`
- exclude ETFs
- exclude ADRs
- exclude leveraged products

Known v1 gap:

- do not exclude symbols with a halt in the last 5 trading days for v1
- log this as a known limitation in `MIGRATION.md` and dashboard caveats
- TODO: add Nasdaq Trading Halts RSS ingestion and exclude symbols halted in the last 5 trading days

### Sector Mapping

Use a static sector mapping first:

```text
config/sector_map.csv
```

Columns:

```text
symbol,gics_sector,gics_industry
```

Alpaca asset metadata can enrich this later, but the allocator must not depend on a live sector lookup during slot allocation.

## Signal Layer

### One Source Of Truth

The live scanner and backtest replay must call the same signal functions from:

```text
src/driftpilot/signals/
```

No separate research math.

### Intraday Entry Filter

A symbol enters the queue only if all are true:

- `RVOL >= 2.0`
- `price > session_vwap`
- `15m_return >= 0.5%`
- `spread <= max(0.02, 0.001 * price)`
- SPY regime filter allows entry

### Ranking

```text
score = 0.4 * zscore(rvol)
      + 0.3 * zscore(15m_return)
      + 0.3 * zscore(distance_above_vwap_pct)
```

Z-scores are recomputed across the current passing candidate pool every scanner cycle.

### Regime

Compute every scanner cycle:

- `spy_5m_return`
- `spy_15m_return`
- `spy_vwap_distance`
- SPY ATR distance from VWAP

Regimes:

- `GREEN`: SPY above VWAP and `spy_5m_return > -0.1%`
- `CAUTION`: SPY below VWAP but `spy_5m_return > -0.3%`
- `RED`: `spy_5m_return < -0.3%` or SPY broken below 1.5x ATR from VWAP

Entry gates:

- `GREEN`: all valid entries allowed
- `CAUTION`: require `relative_strength > 0.5%`
- `RED`: require `relative_strength > 1.0%` and positive own 15-minute return

## Slot Allocation

### Slot Model

Default:

```text
OPERATOR_CAPITAL = 10000
SLOT_COUNT = 10
SLOT_NOTIONAL = 1000
TARGET_PCT = 0.01
STOP_PCT = 0.01
MAX_HOLD_MINUTES = 45
MAX_SLOTS_PER_SECTOR = 3
```

Each slot is one of:

- `EMPTY`
- `RESERVED`
- `ENTERING`
- `OPEN`
- `EXITING`
- `RECYCLING`
- `ERROR`

Time stop behavior:

- time stop is evaluated only from `OPEN`
- if an exit order is already in flight, time stop does not fire again
- `EXITING` owns its cancel/replace/emergency-market sequence until the slot returns to `RECYCLING` or `ERROR`

### Allocator Lock

All allocation must go through `SlotAllocator`.

`SlotAllocator` responsibilities:

- Hold an async lock.
- Read free slots.
- Read ranked candidates.
- Enforce no duplicate symbol.
- Enforce sector cap.
- Reserve slots before order submission.
- Persist allocator state before releasing lock.
- Never double-allocate when two slots free at the same time.

## Risk And Gates

### PDT Guard

Hard constants:

```text
EQUITY_FLOOR = 26000
LIVE_EQUITY_BUFFER = 1000
```

Live mode:

- Before new entries, check current equity.
- If `equity < EQUITY_FLOOR`, transition to `HALTED_PDT`.
- Existing positions continue to exit.

Paper mode:

- Do not block entries.
- Log what would have happened under live PDT.

### Live Deploy Gate

Reject `MODE=live` unless all are true:

1. Last 12-month backtest expectancy after costs is positive.
2. 60 paper-trading days completed with positive cumulative P&L and Sharpe > 1.0.
3. Alpaca account equity is at least `EQUITY_FLOOR + 1000`.
4. `LIVE_OK=true`.

Log the live gate evaluation on every boot.

### Daily Risk Halt

Hard kill-switch constants:

```text
MAX_TRADES_PER_DAY = 50
MAX_TRADES_PER_SYMBOL_PER_DAY = 3
DAILY_LOSS_LIMIT_PCT = 0.03
```

When any kill switch is hit:

- Transition to `HALTED_RISK`.
- Block new entries.
- Continue exits.
- Show red halt banner.

Kill-switch counters are persisted in `daily_counters` and reset only when `date_et` changes.

## Backtest Harness

Create:

```text
src/driftpilot/backtest/
```

Command:

```bash
python -m driftpilot.backtest --start 2024-01-01 --end 2024-12-31
```

Responsibilities:

- Replay 1-minute Databento Parquet bars.
- Use the exact same signal code as live.
- Use the exact same slippage model as paper.
- Use the same slot allocator rules.
- Use the same target/stop/time exits.
- Use point-in-time index constituents when available.
- If point-in-time constituents are unavailable, explicitly document survivorship bias in `expectancy_report.json`.
- Emit:
  - per-trade log
  - daily P&L
  - win rate
  - average trade duration
  - expectancy per dollar
  - Sharpe
  - max drawdown
  - `expectancy_report.json`

Live deploy gate consumes `expectancy_report.json`.

## Dashboard Refactor

Keep:

- Operator tab
- Admin tab
- Backtest tab
- LLM tab
- Paper-mode badge

Change the Operator tab into a read-only autonomous control room.

### UX Direction

Use the new `driftpilot_dashboard.jsx` operator-console design as the target Operator tab reference.
Use `driftpilot_backtest_tab.jsx` as the target Backtest tab reference.

The dashboard should feel like a professional trading desk tool, not a retail trading app:

- dark, dense, status-first operator console
- compact information density suitable for 6.5 hours of market monitoring
- monospace numeric columns for prices, P&L, scores, RVOL, and timestamps
- no marketing hero sections, no oversized cards, no decorative gradients
- color has semantic meaning only: state, regime, P&L, halt/error, paper/live
- primary glance path: state -> regime -> feed freshness -> equity/P&L -> slots -> queue
- unified nav across `Operator`, `Admin`, `Backtest`, and `LLM`
- every tab uses the same shell, typography, spacing, border, badge, and chart language

Typography target:

- body: IBM Plex Sans or equivalent serious operations font
- numbers/symbols: JetBrains Mono or equivalent trading-console mono
- tabular numerals enabled wherever possible

Mode visibility:

- `PAPER` badge is always visible.
- If `MODE=live`, the header gets an additional red left/accent border.
- The badge alone is not sufficient for live-mode awareness.

Feed heartbeat:

- show SIP feed freshness in the top strip, for example `SIP feed · 0.4s`
- turn heartbeat red when the SPY freshness guard fails
- stale heartbeat must align with the backend `ERROR` transition

Visual consistency requirement:

- Operator, Admin, Backtest, and LLM must look like one product.
- The Backtest JSX establishes the mature portfolio/demo aesthetic.
- The Operator tab should match the Backtest tab's nav, run/config strip density, metric cards, chart panels, and table styling.
- Admin must be an operator maintenance console, not a form dump.
- LLM can remain functionally unchanged during this refactor, but its shell/nav/theme should match the other tabs.

### Operator Tab Fields

Header strip:

- current state
- regime indicator
- market clock
- equity vs PDT floor in live mode
- daily P&L
- daily trade count
- win rate today
- scanner cycle interval
- SIP feed freshness

Header behavior:

- `GREEN`, `CAUTION`, and `RED` regime indicators are color-coded.
- `CAUTION` and `RED` render a full-width banner explaining the restriction.
- In live mode, equity within `$1,000` of the PDT floor turns amber.
- If the data feed is stale, the header shows a red stale-feed/error condition.

Slot grid:

- 10 fixed cards
- 5 by 2 desktop layout where width permits
- symbol or `EMPTY`
- slot number
- slot state badge: `OPEN`, `RESERVED`, `EXITING TARGET`, `EXITING STOP`, `EMPTY`
- sector or industry label
- entry price
- current price
- P&L %
- time in position
- exit reason
- slippage applied
- empty reason such as `Awaiting candidate` or `Sector cap reached`

Slot hierarchy:

- symbol and P&L percentage are the most visually prominent fields
- entry/current price and time/slippage are secondary
- empty slots must still explain why they are empty

Ranked queue:

- top 20 candidates
- score
- RVOL
- VWAP distance
- 15-minute return
- sector
- blocked reason
- queue status badge: `Q`, `RES`, `CAP`, or equivalent
- total scanned count for the cycle

Queue behavior:

- sector-cap-blocked rows remain visible but dimmed
- reserved rows show which symbol is being submitted
- the queue must explain why a high-ranked candidate is not entering

Recycle log:

- last 20 recycle events
- format: `[time] slot N freed (SYMBOL exited at +X% via TARGET/STOP/TIME) -> filled with SYMBOL2`
- pending recycle events are allowed and should show `pending -> -`
- filled recycle events show the replacement symbol

Halt banner:

- `PDT floor breached`
- `RED regime, no qualifying candidates`
- `Daily loss limit hit`
- `Market closed - opens in HH:MM`
- `ERROR: broker disconnected, retrying`
- `ERROR: SPY bar stale, market data stream unhealthy`

Equity curve:

- intraday P&L sparkline
- today plus last 5 days
- current equity
- best trade
- worst trade
- recycle count
- average hold time

### Operator API View Model

The frontend should be able to build the entire Operator tab from one endpoint:

```text
GET /api/operator/state
```

Response shape:

```text
{
  mode,
  state,
  state_reason,
  regime,
  market_clock,
  heartbeat,
  equity,
  daily_pnl,
  slots,
  ranked_queue,
  recycle_log,
  equity_curve,
  halt_banner,
  last_transition
}
```

The frontend may mock this exact shape while backend Phase 1-6 are in progress. When SQLite repositories are ready, the endpoint should render from:

- `operator_state`
- `state_transitions`
- `slots`
- `positions`
- `candidate_queue`
- `recycle_events`
- `daily_pnl`

The dashboard remains a renderer of backend state. It must not contain trading decisions, allocation decisions, or hidden client-side risk logic.

### Backtest Tab

Add a Backtest tab for the refactor-era app. This is the primary research/demo screen when showing the project externally.

Use the `driftpilot_backtest_tab.jsx` layout and information hierarchy as the reference.

Top run/config strip:

- period
- universe name and count
- slippage model
- slot configuration
- bars replayed
- run duration
- re-run backtest action

Verdict block:

- live deploy gate verdict at the top
- verdict states: `PASS`, `GATED`, or `FAIL`
- `GATED` is the expected honest state until paper-trading and live-account gates pass
- four live-gate criteria shown as a transparent checklist:
  - 12-month backtest expectancy after costs > 0
  - 60 paper days with cumulative P&L > 0 and Sharpe > 1.0
  - account equity >= `$27,000` (`$26,000` PDT floor + `$1,000` buffer)
  - `LIVE_OK=true`

Headline metrics:

- latest `expectancy_report.json`
- live gate backtest criterion pass/fail
- total return
- SPY return comparison
- expectancy per dollar
- Sharpe
- SPY Sharpe comparison
- max drawdown
- SPY drawdown comparison
- win rate
- trade count
- survivorship-bias warning when point-in-time constituents were unavailable

Required analysis panels:

- equity curve: DriftPilot vs SPY benchmark plus drawdown
- drawdown analysis: max drawdown, longest drawdown, days to recover, average win/loss, best/worst trade
- trade return distribution histogram showing target/stop/time-stop shape
- performance by regime: GREEN, CAUTION, RED rows with trades, win rate, expectancy, contribution
- slippage waterfall: gross return -> slippage/fees/costs -> net return
- exit reason breakdown: TARGET, STOP, TIME with count, percentage, average hold, average P&L
- monthly returns
- caveats/limitations

The slippage waterfall is load-bearing. It must clearly show how much gross edge was eaten by execution costs. This is the dashboard proof that the project rejects mid-price fantasy fills.

Caveats section:

- survivorship bias if point-in-time constituents were unavailable
- slippage is modeled unless measured fills are available
- no outage simulation unless test data includes feed/broker failures
- limited regime stress unless explicitly run
- tax accounting excluded
- sample-size warning for one-year tests

Future-but-planned Backtest capabilities:

- compare runs mode with two verdict blocks side-by-side and metric diffs
- collapsed symbol-level performance table showing top/bottom P&L contributors
- rolling 60-day Sharpe chart to show edge stability or decay

The Backtest tab is read-only for reports in this refactor. Running new backtests can remain CLI-first, with a dashboard action allowed to trigger the CLI/server job later.

### Backtest API View Model

The frontend should render the Backtest tab from:

```text
GET /api/backtest/report
```

Response shape:

```text
{
  run_config,
  verdict,
  live_gate_criteria,
  headline_metrics,
  equity_curve,
  drawdown_analysis,
  return_distribution,
  performance_by_regime,
  slippage_waterfall,
  exit_breakdown,
  monthly_returns,
  caveats,
  survivorship_bias_note,
  generated_at
}
```

This endpoint reads `expectancy_report.json` plus any richer artifacts emitted by `driftpilot.backtest`.

### Mock-To-Real Wiring

Frontend implementation should start against mocked JSON matching `/api/operator/state`, `/api/backtest/report`, and `/api/admin/state`.

Backend integration is complete only when:

- mock data can be removed without changing component structure
- each visible field has a SQLite-backed source or a documented backend-computed field
- loading, empty, stale, halted, and error states are represented
- the screenshot-level operator console can be rendered from live backend state
- the Backtest screen can be rendered from `expectancy_report.json` artifacts without hardcoded metrics
- all tabs share one product shell and theme

### Admin Tab

Add an Admin tab using the same visual system as Operator and Backtest.

Admin sections:

- system health
- manual override controls
- broker reconciliation status
- state machine event log
- configuration editor

System health:

- SQLite status
- Alpaca broker status
- Alpaca SIP feed status
- Databento cache status
- clock/timezone status
- latest SPY bar age
- backtest report availability
- live gate status

Manual override controls:

- pause scanning
- resume scanning
- flat all positions
- reset paper state
- cancel open orders
- force broker reconciliation
- restart data stream

Manual override rules:

- controls are explicit Admin-only actions
- destructive actions require confirmation
- every override writes a state-machine event
- normal operator entries/exits never require manual approval

Broker reconciliation status:

- Alpaca open positions
- local SQLite positions
- mismatches
- last reconciliation timestamp
- reconciliation action taken
- unresolved conflicts

State machine event log:

- latest transitions with timestamp, from-state, to-state, reason, and metadata
- filter by severity/state
- errors are visible, never swallowed

Configuration editor:

- mode and live gate flags
- slot count/notional
- target/stop/time-stop
- sector cap
- kill switches
- scan interval
- data feed settings
- SQLite/bar cache paths
- read-only display for secrets

### Admin API View Model

The frontend should render Admin from:

```text
GET /api/admin/state
```

Response shape:

```text
{
  system_health,
  manual_overrides,
  broker_reconciliation,
  event_log,
  configuration,
  live_gate,
  last_updated_at
}
```

Admin actions:

```text
POST /api/admin/pause-scanning
POST /api/admin/resume-scanning
POST /api/admin/flat-all
POST /api/admin/cancel-open-orders
POST /api/admin/reconcile-broker
POST /api/admin/restart-data-stream
POST /api/admin/configuration
```

Normal entry/exit must not require a human confirmation button.

## Configuration Changes

Add:

```text
MODE=paper
LIVE_OK=false
EQUITY_FLOOR=26000
LIVE_EQUITY_BUFFER=1000
OPERATOR_CAPITAL=10000
SLOT_COUNT=10
SLOT_NOTIONAL=1000
TARGET_PCT=0.01
STOP_PCT=0.01
MAX_HOLD_MINUTES=45
MAX_SLOTS_PER_SECTOR=3
MAX_TRADES_PER_DAY=50
MAX_TRADES_PER_SYMBOL_PER_DAY=3
DAILY_LOSS_LIMIT_PCT=0.03
SCAN_INTERVAL_SECONDS=30
ENTRY_LIMIT_TIMEOUT_SECONDS=30
EXIT_LIMIT_TIMEOUT_SECONDS=15
ALPACA_DATA_FEED=sip
DATABENTO_API_KEY=
SQLITE_PATH=data/driftpilot.sqlite
BAR_CACHE_DIR=data/bars/databento
SECTOR_MAP_PATH=config/sector_map.csv
```

Deprecate for the autonomous operator:

- `SUPABASE_URL`
- `SUPABASE_KEY`
- `OPERATOR_REFRESH_INTERVAL_MINUTES`
- `OPERATOR_UNIVERSE_REFRESH_INTERVAL_MINUTES`
- `OPERATOR_MONITOR_INTERVAL_MINUTES`
- PEAD-specific operator caps

Keep old env keys where legacy Admin/LLM screens still need them.

## Implementation Phases

### Current Implementation Snapshot

Status as of the latest refactor pass:

- Phase 0 complete: plan, agent instructions, and resolved signal decisions are in the repo.
- Phase 1 complete: `src/driftpilot/settings.py`, `clock.py`, SQLite schema, repositories, daily counters, and foundation tests exist.
- Phase 2 complete: Alpaca broker client, SIP stream adapter, two-tier subscription model, order fallback rules, boot reconciliation, and live-gate blocking exist.
- Phase 3 complete: shared signal layer exists with typical-price VWAP, same-minute RVOL, SPY-only v1 regime, and z-score ranking.
- Phase 4 complete: slot allocator, sector cap, duplicate/stale guards, paper fill slippage, and fill persistence exist.
- Phase 5 complete as a harness: backtest replay, metrics, report writer, and CLI exist. Real Databento/Parquet data is still needed to produce the first real `expectancy_report.json`.
- Phase 6 complete as a runtime skeleton: async state machine, transition logging, market-clock handling, retry metadata, and SPY stale-bar guard exist. It is not yet wired to a real continuous Alpaca stream/process.
- Phase 7a complete: Operator tab is now a read-only operator-console shell backed by `/api/operator/state`, with mock fallback and SQLite read model support.
- Phase 7b complete: Backtest tab and `/api/backtest/report` exist, reading `expectancy_report.json` when present and mock-shaped data otherwise.
- Phase 7c complete: Admin tab and `/api/admin/state` exist with system health, broker reconciliation status, event log, safe config, and manual override shell.
- Phase 8 complete: README, MIGRATION notes, and acceptance coverage exist.

Important remaining gap:

The components are built, but the production paper operator is not yet one real running loop. The next work must connect:

```text
Alpaca SIP stream -> bar/quote cache -> scanner -> allocator -> entry order/paper fill
-> position monitor -> target/stop/time exit -> recycler -> SQLite -> dashboard
```

### Revised Next Sequence

Before building more runtime infrastructure, validate the strategy against real historical data:

1. Verify the full suite and the eight acceptance tests pass with no skips.
2. Run Phase 12 next: pull/cache Databento data, run the 12-month replay, and generate a real `expectancy_report.json`.
3. Inspect the slippage waterfall and after-cost expectancy.
4. If the report verdict is `GATED`, meaning backtest expectancy is positive but paper-trading/live gates remain pending, continue with Phase 9, then Phase 10, then Phase 11.
5. If the report verdict is `FAIL`, stop runtime buildout and revisit the signal layer before investing in more infrastructure.

Rationale: do not build production infrastructure around a strategy that does not survive realistic execution costs.

### Phase 0: Safety Freeze

- Add a branch and commit current state before refactor.
- Keep current app runnable.
- Add this plan to repo.

### Phase 1: Core Models And SQLite

- Add state, slot, position, order, fill, candidate, regime, and transition models.
- Add SQLite schema and repository.
- Add tests for schema creation and state persistence.

### Phase 2: Alpaca Broker/Data Layer

- Build unified Alpaca paper/live client.
- Add account, positions, orders, and order-update methods.
- Add Alpaca SIP WebSocket bar/quote stream adapter.
- Verify Alpaca SIP WebSocket subscription limits against the checked-in universe size.
- Add two-tier subscription model and discovery-tier sharding if the universe cannot fit in one connection/subscription budget.
- Implement marketable-limit order submission and documented fallback rules.
- Add boot reconciliation logic.
- Keep live order submission blocked by live gate.

### Phase 3: Signal Layer

- Implement 1-minute bar feature cache.
- Implement VWAP, RVOL, 15-minute return, spread check, z-score ranking.
- Implement SPY regime logic. QQQ is deferred to v2.
- Add deterministic synthetic-bar tests.

### Phase 4: Slot Allocator And Paper Fills

- Implement `SlotAllocator` with async lock.
- Implement sector cap and duplicate-symbol guard.
- Implement slippage model.
- Implement entry/exit fill persistence.
- Add concurrency, sector cap, and slippage tests.

### Phase 5: Backtest Harness

- Add Databento pull/cache command.
- Add Parquet replay.
- Reuse live signal, allocator, slippage, and exit logic.
- Prefer point-in-time index constituents when available.
- If point-in-time constituents are unavailable, write a survivorship-bias note into `expectancy_report.json`.
- Generate `expectancy_report.json`.

Backtest comes before state machine implementation so the live gate, signal code, slippage, allocator, and exit logic can be validated before they run continuously.

### Phase 6: State Machine Runtime

- Implement the async state machine.
- Wire scanner, allocator, position monitor, exit handler, and recycler as driven services.
- Add state transition logging.
- Add error handling with retry/backoff.
- Add market-clock handling.
- Add SPY freshness guard: stale SPY bar older than 60 seconds transitions to `ERROR`.

### Phase 7a: Dashboard Shell And Operator

- Build the shared operator-console shell, nav, typography, badges, chart panels, tables, and paper/live mode treatment.
- Replace Operator manual UI with live state renderer.
- Add visible Operator status, halt reasons, slot grid, candidate queue, recycle log, and equity curve.
- Move manual controls out of Operator.

Phase 7a acceptance:

- Operator renders from mocked `/api/operator/state`.
- Operator renders from real `/api/operator/state` without component structure changes.
- Operator explains every non-trading condition with state, gate, or blocked reason.
- Operator has no manual confirm button for normal entries/exits.
- Paper/live badge and live-mode accent are always visible.

Loading / Empty / Stale / Error states:

- Header strip:
  - loading: skeleton metrics and `BOOT`
  - empty: market closed countdown when no session data exists
  - stale: feed heartbeat red with stale seconds
  - error: red state and last error reason
- Slot grid:
  - loading: fixed 10-card skeleton
  - empty: `EMPTY` cards with reason such as `Awaiting candidate`
  - stale: slot data remains visible with stale price marker
  - error: affected slot shows `ERROR` and event-log reference
- Ranked queue:
  - loading: table skeleton
  - empty: `No qualifying candidates`
  - stale: candidate rows blocked as `stale_bar`
  - error: scanner error banner with retry status
- Recycle log:
  - loading: ledger skeleton
  - empty: `No recycle events today`
  - stale: last event remains visible with stale timestamp marker
  - error: event write/read error visible
- Equity curve:
  - loading: chart skeleton
  - empty: flat baseline with no-trades label
  - stale: chart annotated at last fresh point
  - error: chart panel shows source error

### Phase 7b: Backtest Tab

- Add Backtest tab from `expectancy_report.json` artifacts.
- Render verdict, live-gate checklist, headline metrics, equity/drawdown charts, distribution, regime performance, slippage waterfall, exit breakdown, monthly returns, and caveats.

Phase 7b acceptance:

- Backtest renders from mocked `/api/backtest/report`.
- Backtest renders from real report artifacts without hardcoded metrics.
- Verdict can show `PASS`, `GATED`, or `FAIL`.
- Slippage waterfall reconciles gross return to net return.
- Survivorship-bias caveat appears when the report indicates point-in-time constituents were unavailable.

### Phase 7c: Admin Tab

- Add Admin tab with system health, manual override controls, broker reconciliation status, state machine event log, and configuration editor.
- Move manual controls to Admin.
- Make Operator, Admin, Backtest, and LLM share the same operator-console shell/theme.
- Keep LLM business logic unchanged.

Phase 7c acceptance:

- Admin renders from mocked `/api/admin/state`.
- Admin renders from real backend state without component structure changes.
- Destructive controls require confirmation.
- Every override action writes a state-machine event.
- Broker reconciliation mismatch is visible and actionable.
- Configuration editor never displays raw secrets.

### Phase 8: Acceptance Tests And Migration Docs

- Add all eight required acceptance tests.
- Add README sections:
  - How to run paper
  - How to read the dashboard
  - How live deploy works
- Add `MIGRATION.md`.
- Remove or archive obsolete operator manual paths only after tests pass.

### Phase 9: Real Runtime Wiring

Prerequisite: Phase 12 must produce `verdict = GATED` or better. If Phase 12 produces `FAIL`, Phase 9 is blocked.

Goal: turn the implemented parts into one runnable autonomous paper process.

- Add `src/driftpilot/operator.py` or equivalent `python -m driftpilot.operator` command.
- Instantiate settings, clock, SQLite repository, Alpaca stream, broker client, scanner service, allocator, paper fill engine, and position monitor.
- Drive everything through `DriftPilotStateMachine`; scanner, allocator, monitor, and exit handler must not run independent loops.
- On boot:
  - evaluate live gate
  - initialize 10 slots
  - reconcile against Alpaca open positions
  - subscribe to the always-on stream tier
- During market hours:
  - maintain WebSocket bar/quote cache
  - run scanner on `SCAN_INTERVAL_SECONDS`
  - allocate free slots from ranked candidates
  - submit entry orders or paper fills depending on mode
  - persist every state/position/order/fill transition
- Outside market hours:
  - transition to `MARKET_CLOSED`
  - show next-open countdown in dashboard state
- Add a small CLI smoke test mode that runs one deterministic cycle without hitting Alpaca.

Phase 9 acceptance:

- `python -m driftpilot.operator --once --mock-stream` writes SQLite state visible in `/api/operator/state`.
- With no candidates, dashboard shows a clear non-trading reason, not mock data.
- With injected candidates, allocator reserves slots and dashboard shows those slots.
- Boot reconciliation event appears in Admin event log.
- No Supabase access is required for the DriftPilot operator path.

### Phase 10: Scanner Service And Candidate Queue Persistence

Prerequisite: Phase 9 runtime command exists and Phase 12 did not fail.

Goal: make the ranked queue real and continuously refreshed.

- Implement scanner service that consumes the WebSocket-backed `BarFeatureCache`.
- Persist top candidates to `candidate_queue` every scanner cycle.
- Persist blocked candidates with reason:
  - `stale_bar`
  - `spread_too_wide`
  - `below_rvol`
  - `below_vwap`
  - `below_15m_return`
  - `regime_rejected`
  - `sector_cap_reached`
  - `duplicate_symbol`
- Add industry/sector classification from checked-in CSV or `sector_map`.
- Keep queue size configurable; default dashboard shows top 20, backend may persist top 100.
- Add data freshness status for SPY and each candidate.

Phase 10 acceptance:

- Synthetic bar stream produces deterministic ranked queue rows in SQLite.
- RED regime only persists relative-strength survivors as allocatable.
- Sector-cap-blocked candidates remain visible with `sector_cap_reached`.
- Candidate queue API/dashboard can distinguish loading, empty, stale, and error states.

### Phase 11: Entry, Exit, And Recycling Services

Prerequisite: Phase 9 runtime and Phase 10 scanner persistence are working against synthetic or real stream input.

Goal: complete the capital loop.

- Convert slot reservations into entry orders/fills.
- Create/update positions, orders, and fills from entry results.
- Monitor open positions only when driven by the state machine.
- Exit branches:
  - `TARGET` at `+target_pct`
  - `STOP` at `-stop_pct`
  - `TIME` at `MAX_HOLD_MINUTES`
- If an exit order is already in flight, do not fire duplicate time-stop/target/stop exits.
- On exit fill:
  - update realized P&L
  - mark slot `RECYCLING`
  - write recycle event
  - return slot to `EMPTY`
  - allow allocator to refill on next cycle
- Persist daily counters:
  - `MAX_TRADES_PER_DAY`
  - `MAX_TRADES_PER_SYMBOL_PER_DAY`
  - daily loss limit

Phase 11 acceptance:

- A synthetic position exits on target and recycles its slot.
- A synthetic position exits on stop and recycles its slot.
- A flat synthetic position exits exactly at `MAX_HOLD_MINUTES`.
- Capital deployed never exceeds configured paper capital.
- Duplicate opposite-side orders are not submitted when an exit is already open.
- Daily counters survive process restart and reset only on ET date change.

### Phase 12: Real Backtest Data And First Expectancy Report

Goal: replace mock Backtest tab data with a real report and make the go/no-go decision before runtime buildout.

- Add Databento pull/cache command for 1-minute bars.
- Store cache under `data/bars/databento/` as gitignored Parquet.
- Document required Databento dataset/symbol universe.
- Run first 12-month replay.
- Generate `expectancy_report.json`.
- Confirm Backtest tab renders the real report without component changes.
- If point-in-time constituents are unavailable, keep survivorship-bias caveat in the report.
- Treat the report as the strategy-validation gate:
  - `GATED`: positive after-cost expectancy; continue to Phase 9.
  - `FAIL`: negative after-cost expectancy; stop and revisit signals.

Phase 12 acceptance:

- `python -m driftpilot.backtest --start 2024-01-01 --end 2024-12-31` runs against real cached bars.
- `expectancy_report.json` includes trades, daily P&L, Sharpe, max drawdown, expectancy, slippage waterfall, regime performance, and caveats.
- Live gate reads the real backtest criterion from this report.
- If expectancy is negative after costs, verdict is `FAIL`, not `PASS`.

### Phase 13: Admin Override Actions

Goal: make Admin controls real and auditable.

- Implement backend endpoints for:
  - pause scanning
  - resume scanning
  - flat all positions
  - force broker reconciliation
  - reset paper state
- Every override writes a state-machine transition/event.
- Destructive actions require confirmation in UI and server-side idempotency token.
- Flat-all uses broker/paper execution path, not direct DB mutation.
- Configuration editor remains safe: never render raw secrets.

Phase 13 acceptance:

- Pause scanning blocks new entries but allows exits.
- Resume scanning restarts candidate allocation.
- Flat all positions submits/records exits and transitions to safe state.
- Admin event log shows who/what/when/reason for every override.

### Phase 14: Full Paper Soak Test

Goal: prove the autonomous loop works through a realistic session before any live discussion.

- Run one full market session in paper mode.
- Track:
  - number of scan cycles
  - candidates scanned
  - entries submitted
  - exits submitted
  - recycle events
  - realized/unrealized P&L
  - API errors/retries
  - stale feed events
- Verify dashboard explains all idle/halt/error periods.
- Export a daily operator report from SQLite.

Phase 14 acceptance:

- No capital over-deployment.
- No duplicate symbol allocations.
- No sector cap violations.
- No untimed positions.
- No silent errors.
- Dashboard state stays fresh for the entire session.
- End-of-day report reconciles SQLite positions/orders/fills with Alpaca paper account.

### Phase 15: Legacy Path Retirement

Goal: simplify after the new operator proves itself.

- Move old manual PEAD operator pages/actions to `legacy/` or keep them Admin-only.
- Remove Supabase from the autonomous operator path.
- Remove APScheduler from the DriftPilot operator runtime.
- Keep LLM tab/settings only for future research/review workflows.
- Update README and MIGRATION with final runbooks.

Phase 15 acceptance:

- Operator tab has no manual-confirm workflow.
- DriftPilot runtime starts without Supabase credentials.
- Legacy tests are either moved to legacy scope or replaced with DriftPilot runtime tests.
- README describes the new architecture as the default path.

## Acceptance Test Mapping

1. Crash recovery test
   - Test `BOOT` reconciliation from mocked Alpaca open positions.
   - Verify SQLite slot state is corrected and runtime resumes `IN_POSITION`.

2. Concurrency test
   - Simulate two slots freeing together.
   - Verify two distinct candidates allocated under one allocator lock.

3. Regime test
   - Inject synthetic RED SPY bars.
   - Verify only relative-strength candidates survive.
   - Verify dashboard payload shows RED and explanation.

4. PDT guard test
   - Run live-simulation mode with equity below floor.
   - Verify new entries blocked and exits still permitted.

5. Slippage test
   - Run identical trade through backtest and paper fill model.
   - Verify both paths apply the same slippage formula: `max(0.02, 0.0005 * price)`.

6. Time stop test
   - Open position with flat price.
   - Verify exit fires at `MAX_HOLD_MINUTES`.

7. Sector cap test
   - Inject five same-sector ranked candidates.
   - Verify only three allocate and remaining queue rows show `sector cap reached`.

8. Live gate test
   - Set `MODE=live` without satisfying criteria.
   - Verify boot fails with a clear unmet-criteria list.

## Anti-Patterns To Remove

- Manual confirm as the normal path.
- Supabase as operator state source of truth.
- APScheduler as the operator runtime.
- REST polling inside the scan loop.
- IEX feed for autonomous intraday decisions.
- Mid-price paper fills.
- Day-based time stops for intraday positions.
- Independent scanner/entry/exit jobs with implicit shared state.
- Silent exception swallowing.
- Separate live and backtest signal math.

## Resolved Decisions

1. Universe source: checked-in CSV.
2. Historical bar cache: `data/bars/databento/`, gitignored.
3. Supabase admin views: hidden after SQLite operator migration.
4. PEAD workflow: moved to `legacy/`.
5. RVOL = current 1-minute volume / average volume at the same minute-of-day across the last 20 trading days.
6. Regime is SPY-only for v1. QQQ is deferred to v2.
7. VWAP uses typical price `(H + L + C) / 3` weighted by volume.

## Recommended First Coding Slice After Approval

Start with the smallest vertical foundation:

1. Add `driftpilot.settings`.
2. Add SQLite schema/repository.
3. Add state/slot/position models.
4. Add transition logging.
5. Add tests proving state persists and reloads.

Do not begin with UI. The dashboard should render state after the state model is stable.
