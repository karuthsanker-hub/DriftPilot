# DriftPilot — System Design & Architecture Document

**Version:** 1.0  
**Date:** May 13, 2026  
**Authors:** Karuth Sanker, Claude (Anthropic)  
**Status:** Production (Paper Trading)  
**Codebase:** ~22,750 LOC Python · 195 commits · 98 source files

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Overview](#2-system-overview)
3. [Architecture](#3-architecture)
4. [Data Flow — End to End](#4-data-flow--end-to-end)
5. [Component Deep Dive](#5-component-deep-dive)
6. [Database Schema](#6-database-schema)
7. [Signal System](#7-signal-system)
8. [Agent Layer](#8-agent-layer)
9. [Dashboard & Observability](#9-dashboard--observability)
10. [Configuration & Hot-Reload](#10-configuration--hot-reload)
11. [Development Timeline](#11-development-timeline)
12. [Lessons Learned](#12-lessons-learned)
13. [What We Should Have Done From the Start](#13-what-we-should-have-done-from-the-start)
14. [Known Defects & Technical Debt](#14-known-defects--technical-debt)
15. [Low-Level Technical Reference](#15-low-level-technical-reference)

---

## 1. Executive Summary

DriftPilot is an autonomous intraday paper-trading system that monitors real-time market catalyst events (earnings, filings, analyst actions), classifies them using a local Qwen3-8B LLM, routes them through signal-specific entry/exit logic, and executes paper trades via the Alpaca API. A multi-agent layer (PM, Scanner, Slot agents) provides LLM-powered oversight, while a FastAPI dashboard provides real-time visibility.

**Core thesis:** Trade short-term price dislocations caused by catalyst events (earnings surprises, SEC filings, analyst upgrades) before the market fully prices them in.

**Key numbers:**
- 10 concurrent position slots, $1,000 per slot
- 30-second operator cycle time
- ~240 catalyst events per 4-hour window
- Qwen enrichment latency: ~200-500ms per event
- Target hold time: 15-60 minutes
- Risk: ±1% symmetric stop/profit-take

---

## 2. System Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                         DriftPilot Operator                         │
│                                                                      │
│  ┌─────────┐    ┌──────────┐    ┌──────────┐    ┌──────────────┐   │
│  │ Catalyst │───▶│  Signal   │───▶│   Slot   │───▶│    Alpaca    │   │
│  │  Layer   │    │  Router   │    │ Allocator│    │ Broker Client│   │
│  └─────────┘    └──────────┘    └──────────┘    └──────────────┘   │
│       │                                               │              │
│       │              ┌──────────┐                     │              │
│       └─────────────▶│  SQLite  │◀────────────────────┘              │
│                      │ Operator │                                    │
│                      │    DB    │                                    │
│                      └──────────┘                                    │
│                           │                                          │
│  ┌─────────┐    ┌────────┴─────────┐    ┌──────────────────────┐   │
│  │  Agent   │    │    Dashboard     │    │    PM Analyst        │   │
│  │Orchestr. │    │   (FastAPI)      │    │  (Qwen periodic)     │   │
│  └─────────┘    └──────────────────┘    └──────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘

External:
  - Alpaca Markets API (news feed, quotes, paper trading)
  - Qwen3-8B (local, 192.168.1.166:8000 via vLLM/OpenAI-compat)
  - SQLite (operator state, catalyst events, agent messages, PM analysis)
```

---

## 3. Architecture

### 3.1 Layered Architecture

```
Layer 5: Dashboard & Observability
  └─ FastAPI + Jinja2 templates, REST API, real-time polling

Layer 4: Agent Layer (LLM-powered oversight)
  └─ PM Agent, Scanner Agent, Slot Agents, PM Analyst
  └─ Guardrails, Message Bus, Training Exporter

Layer 3: Execution Layer
  └─ Slot Allocator, Broker Client, Paper Fills
  └─ Position Monitor, Exit Signal Evaluation

Layer 2: Signal & Catalyst Layer
  └─ Catalyst Discovery, Qwen Enrichment, Event Bus
  └─ Signal Router, MultiSignal fan-out/fan-in
  └─ Individual signals (earnings_report_v1, filing_8a_v1, etc.)

Layer 1: Foundation
  └─ State Machine, Clock, Settings, Runtime Config
  └─ SQLite Repositories, Market Data Adapters
```

### 3.2 State Machine

The operator runs as a finite state machine with a ~30-second cycle:

```
BOOT ──▶ RECONCILIATION ──▶ REGIME_CHECK ──▶ SCANNING ──▶ MONITORING
  │                                              │              │
  │                                              │              │
  └──────────────────── HALTED_RISK ◀────────────┴──────────────┘
                            │
                       MARKET_CLOSED
```

| State | Purpose | Duration |
|-------|---------|----------|
| `BOOT` | Initialize connections, load config | ~1s |
| `RECONCILIATION` | Sync local state with Alpaca broker positions | ~2s |
| `REGIME_CHECK` | Evaluate SPY regime (GREEN/AMBER/RED) | ~1s |
| `SCANNING` | Poll catalyst events, run signals, emit candidates | ~5-10s |
| `MONITORING` | Check open positions, evaluate exits | ~5-10s |
| `HALTED_RISK` | Daily loss limit hit, kill switch, or error | Until manual resume |
| `MARKET_CLOSED` | Outside market hours | Until 9:30 ET |

### 3.3 Process Model

```
driftpilot.operator (main process)
  ├── asyncio event loop
  │   ├── state_machine.run_once()      [every ~30s]
  │   ├── catalyst.discovery_service     [Alpaca news WebSocket]
  │   └── market_data.alpaca_stream      [quote WebSocket]
  │
  ├── PM Analyst                         [every 15 min, sync via Qwen HTTP]
  └── Agent Orchestrator                 [every tick, sync via Qwen HTTP]
      ├── PM Agent tick
      ├── Scanner Agent tick
      └── Slot Agent ticks (per position)

dashboard (separate process)
  └── uvicorn FastAPI app                [port 8000]
      └── reads from same SQLite DB (WAL mode)
```

---

## 4. Data Flow — End to End

### 4.1 Entry Flow (Catalyst → Position)

```
Step 1: CATALYST DISCOVERY
  Alpaca News WebSocket
    → headline: "Apple beats Q1 earnings, raises guidance"
    → CatalystEvent(symbol=AAPL, category=earnings, subcategory=report)

Step 2: QWEN ENRICHMENT
  CatalystEvent → QwenEnricher
    → HTTP POST to Qwen3-8B (local)
    → prompt: "Classify this headline. Is it positive/negative/neutral?"
    → response: {sentiment: "positive", confidence: 0.92, direction: "up"}
    → stored in catalyst_events SQLite table

Step 3: EVENT BUS DISPATCH
  QwenEnricher → CatalystEventBus.publish(enriched_event)
    → all subscribed signals receive the event
    → EarningsReportSignal, Filing8ASignal, etc.

Step 4: SIGNAL EVALUATION
  EarningsReportSignal.evaluate_entry(event)
    → check: sentiment == "positive"? ✓
    → check: event_age < 240 min? ✓
    → check: confidence > threshold? ✓
    → emit Candidate(symbol=AAPL, score=0.92)

Step 5: SCANNER SERVICE (CatalystScannerService)
  Signal.pending_candidates → CatalystScannerService.scan()
    → pre-filter: is AAPL in blocked_symbols? (active slot, day-cap, cooldown)
    → if not blocked: fetch live quote from Alpaca REST
    → price drift check: |current - reference| < 3%?
    → emit AllocationCandidate(symbol=AAPL, score=0.92, sector=Tech, metadata={...})

Step 6: SLOT ALLOCATOR
  SlotAllocator.allocate([AAPL_candidate, ...])
    → rejection pipeline (in order):
        1. negative_catalyst    → skip if sentiment negative
        2. stale_bar            → skip if last bar too old
        3. duplicate_symbol     → skip if already in a slot
        4. max_trades_per_day   → skip if symbol hit daily cap
        5. consecutive_loss     → skip if 3+ consecutive losses on symbol
        6. reentry_cooldown     → skip if exited < 15 min ago
        7. sector_cap           → skip if sector has 4+ positions
        8. no_free_slot         → skip if all 10 slots occupied
    → if passed: reserve slot, return SlotAllocation

Step 7: BROKER EXECUTION
  LiveAlpacaAllocator.allocate(candidates)
    → for each SlotAllocation:
        → AlpacaBrokerClient.submit_entry_order(AAPL, qty=5, limit=200.10)
        → Alpaca paper API creates order
        → wait for fill confirmation
        → create position record in SQLite
        → store catalyst metadata (headline, sentiment, hash) on position

Step 8: AGENT OVERLAY (optional)
  AgentOrchestrator.tick_scanner(candidates, market_context)
    → ScannerAgent evaluates via Qwen LLM
    → can approve, veto, or re-rank candidates
    → subject to guardrails (max 20% override rate)
```

### 4.2 Exit Flow (Position → Close)

```
Step 1: POSITION MONITORING
  LiveAlpacaPositionMonitor.monitor_open_positions()
    → for each open position:
        → fetch latest quote (Alpaca REST, cached 2s TTL)
        → compute unrealized P&L

Step 2: EXIT SIGNAL EVALUATION
  signal.evaluate_exit(current_price, entry_price, hold_minutes, ...)
    → check profit_take: unrealized >= +1.0%? → PROFIT_TAKE
    → check stop_loss: unrealized <= -1.0%? → STOP_LOSS
    → check trailing_stop: if activated (+1.0%), distance 0.4% from peak
    → check time_stop: hold_minutes >= 60? → TIME_STOP
    → if none triggered: return HOLD

Step 3: FAILSAFE TIME-STOP
  If signal returns None (no metadata, reconciled position):
    → check: hold_minutes > max_hold_minutes?
    → if yes: force close with reason FAILSAFE_TIME_STOP

Step 4: AGENT OVERLAY (optional)
  AgentOrchestrator.tick_slot(slot_id, position_snapshot, algo_says_exit)
    → SlotAgent evaluates via Qwen LLM
    → can agree, early-cut, or veto exit
    → subject to guardrails

Step 5: BROKER EXECUTION
  AlpacaBrokerClient.submit_exit_order(AAPL, qty=5, limit=205.00)
    → Alpaca paper API creates exit order
    → wait for fill
    → get_fill_price() for actual execution price
    → close position in SQLite: realized_pnl, exit_reason, closed_at
    → free slot: status=EMPTY, ready for next candidate

Step 6: POST-TRADE
  → daily_counters incremented
  → recycle_events logged (slot freed, new candidate can enter)
  → PM Analyst picks it up on next 15-min analysis cycle
```

### 4.3 Qwen Enrichment Flow (Detail)

```
Raw Headline
  │
  ▼
ContextAssembler
  → gather: recent headlines for same symbol (dedup)
  → gather: sector context, market regime
  → build structured prompt (v2 format)
  │
  ▼
QwenEnricher._enrich_one()
  → HTTP POST http://192.168.1.166:8000/v1/chat/completions
  → model: Qwen/Qwen3-8B
  → system prompt: "You are a financial analyst..."
  → user prompt: headline + context + /no_think suffix
  → max_tokens: 256, temperature: 0.1
  │
  ▼
Response Parsing
  → strip ```json fences
  → strip <think>...</think> blocks
  → JSON.parse → {sentiment, confidence, direction, reasoning}
  │
  ▼
CatalystEvent updated
  → sentiment = "positive" / "negative" / "neutral"
  → confidence = 0.0 - 1.0
  → priority_modifier = ±0.15 (boosts/penalizes score)
  → stored in catalyst_events table
```

---

## 5. Component Deep Dive

### 5.1 Catalyst Layer (`src/driftpilot/catalyst/`)

| Module | Purpose | LOC |
|--------|---------|-----|
| `event.py` | CatalystEvent dataclass | ~80 |
| `event_bus.py` | Pub/sub for catalyst events | ~120 |
| `feed_alpaca.py` | Alpaca News API WebSocket consumer | ~150 |
| `feed_rss.py` | RSS feed polling (backup) | ~100 |
| `headline_parser.py` | Extract symbol, category from raw headline | ~80 |
| `classifier.py` | Rule-based pre-classification | ~100 |
| `qwen_enricher.py` | Qwen LLM enrichment (sentiment, confidence) | ~200 |
| `context_assembler.py` | Build enrichment context (recent headlines, sector) | ~375 |
| `universe_filter.py` | Filter events to tradeable universe | ~100 |
| `db.py` | Catalyst SQLite persistence | ~120 |
| `discovery_service.py` | Orchestrates feed → parse → enrich → publish | ~200 |

**Data model:**
```python
@dataclass
class CatalystEvent:
    symbol: str
    category: str           # "earnings", "filing", "analyst", "insider"
    subcategory: str         # "report", "8-A", "target_raise", etc.
    pillar: str              # "micro" (company) or "macro" (market)
    ts: datetime
    headline: str
    source: str              # "alpaca", "rss"
    horizon_minutes: int     # how long the signal is valid
    headline_hash: str       # dedup key
    sentiment: str | None    # from Qwen: "positive"/"negative"/"neutral"
    confidence: float | None # from Qwen: 0.0-1.0
    priority_modifier: float # ±0.15 score adjustment
```

### 5.2 Signal System (`src/driftpilot/signals/`)

Each signal is a self-contained module with:
```
signals/earnings_report_v1/
  ├── __init__.py
  ├── config.py      # EarningsReportConfig dataclass
  ├── signal.py      # EarningsReportSignal (entry/exit logic)
  ├── features.py    # Feature extraction helpers
  ├── exits.py       # Exit decision logic
  └── signal_state.py # Per-symbol tracking state
```

**Signal registry:**

| Signal | Status | Strategy |
|--------|--------|----------|
| `earnings_report_v1` | **Active** | Buy on positive earnings surprise |
| `filing_8a_v1` | **Active** | Buy on 8-A filing (typically acquisitions) |
| `analyst_target_raise_v1` | Disabled | Buy on analyst price target raise |
| `intraday_momentum_v1` | Backtest only | RVOL + VWAP breakout |
| `apex_hunter_v2` | Backtest only | Volume spike + range expansion |
| `whale_tail_v1` | Backtest only | Institutional accumulation |
| `rs_drift_v1` | Backtest only | Relative strength drift |
| `stationary_ghost_v1` | Backtest only | Mean reversion on overextension |

**MultiSignal pattern:**
```python
# signal_router.py orchestrates fan-out/fan-in
active_signals = "earnings_report_v1,filing_8a_v1"
# Each signal subscribes to the event bus
# Each evaluates independently
# Results are merged by the scanner service
```

### 5.3 Execution Layer (`src/driftpilot/execution/`)

**SlotAllocator** — The gatekeeper:
```python
# Rejection pipeline (evaluated in order, first reject wins)
REJECTION_ORDER = [
    "negative_catalyst",          # sentiment != positive
    "stale_bar",                  # last bar > 5 min old
    "duplicate_symbol",           # already in a slot
    "max_trades_per_symbol_per_day",  # daily cap (default 5)
    "consecutive_loss_cooldown",  # 3+ consecutive losses
    "reentry_cooldown",           # exited < 15 min ago
    "sector_cap_reached",         # sector has 4+ positions
    "no_free_slot",               # all 10 slots occupied
]
```

**AlpacaBrokerClient** — Paper + live execution:
- Live gate: blocks real money unless backtest passed, paper passed, LIVE_OK flag set
- PDT guard: blocks entries when equity < $25,000 + buffer
- Entry: marketable limit order (mid + slippage)
- Exit: marketable limit order (mid - slippage)
- Fill polling: async wait for order fill confirmation

### 5.4 State Machine (`src/driftpilot/state_machine.py`)

615 LOC. The `run_once()` method executes one complete operator cycle:

```python
async def run_once(self) -> OperatorState:
    # 1. Check market hours
    if not session.is_open:
        return MARKET_CLOSED

    # 2. Agent tick: PM oversight
    self._tick_agents_pm()
    self._tick_analyst()          # PM Analyst (15-min throttle)

    # 3. Monitor open positions
    if self.position_monitor:
        decisions = self.position_monitor.decide()
        decisions = self._agent_intercept_exits(decisions)
        self.position_monitor.execute(decisions)

    # 4. Scan for new candidates
    if self.scanner:
        scan_result = await self.scanner.scan()
        if scan_result.candidates:
            alloc_result = await self.allocator.allocate(scan_result.candidates)
            self._agent_intercept_entries(alloc_result)

    # 5. Reconcile with broker
    # 6. Check risk limits
    # 7. Log state transition
```

### 5.5 Storage Layer (`src/driftpilot/storage/repositories.py`)

1,720 LOC. Repository pattern over SQLite with WAL mode for concurrent dashboard reads.

**Key repositories:**
```
DriftPilotRepository
  ├── .state        → OperatorStateRepo (current state, last transition)
  ├── .transitions  → StateTransitionRepo (full audit log)
  ├── .slots        → SlotRepo (10 trading slots, status tracking)
  ├── .positions    → PositionRepo (open/closed positions, P&L)
  ├── .orders       → OrderRepo (broker orders, fill tracking)
  ├── .fills        → FillRepo (execution fills)
  ├── .daily_counters → DailyCounterRepo (trades_total, trades_per_symbol)
  ├── .daily_pnl    → DailyPnLRepo (end-of-day P&L records)
  └── .errors       → ErrorRepo (structured error logging)
```

---

## 6. Database Schema

### 6.1 Operator Database (`state/operator.sqlite3`)

```sql
-- Core state
operator_state     (current_state, last_transition_id, metadata_json, updated_at)
state_transitions  (id, from_state, to_state, reason, metadata_json, timestamp)

-- Trading
slots              (slot_id PK, status, symbol, position_id, slot_value, metadata_json, updated_at)
positions          (id PK, symbol, quantity, entry_price, target_price, stop_price,
                    status, slot_id FK, exit_reason, realized_pnl, metadata_json,
                    opened_at, closed_at)
orders             (id PK, position_id FK, symbol, side, quantity, order_type,
                    limit_price, status, broker_order_id, metadata_json, created_at)
fills              (id PK, order_id FK, price, quantity, filled_at)

-- Analytics
candidate_queue    (id PK, symbol, score, sector, status, metadata_json, created_at)
recycle_events     (id PK, slot_id, from_symbol, to_symbol, exit_reason, pnl_pct, timestamp)
daily_pnl          (date_et PK, realized_pnl, trade_count, win_count, loss_count)
daily_counters     (date_et, counter_name) PK, counter_value)

-- Infrastructure
live_gate_evaluations (id PK, criteria_json, passed, timestamp)
errors             (id PK, component, message, metadata_json, timestamp)
allocator_state    (key PK, value_json, updated_at)
universe           (symbol PK, sector, market_cap, avg_volume, updated_at)
sector_map         (symbol PK, sector, source, updated_at)
```

### 6.2 Catalyst Database (`data/driftpilot/catalyst_events.sqlite3`)

```sql
catalyst_events    (id PK, symbol, category, subcategory, pillar, headline,
                    headline_hash UNIQUE, source, sentiment, confidence,
                    priority_modifier, direction, reasoning,
                    context_json, qwen_response_json, ts, enriched_at)
```

### 6.3 Agent Database (`data/driftpilot/agent_messages.sqlite3`)

```sql
agent_messages     (id PK, from_agent, to_agent, msg_type, payload_json,
                    created_at, expires_at, consumed)
agent_states       (agent_name PK, status, metadata_json, updated_at)
agent_decisions    (id PK, agent_name, decision_type, input_json, output_json,
                    model, latency_ms, overridden, outcome, created_at)
```

### 6.4 PM Analysis (in operator DB)

```sql
pm_analysis        (id PK, created_at, analysis_json, model, latency_ms, snapshot_json)
```

---

## 7. Signal System

### 7.1 Signal Lifecycle

```
                    CatalystEvent
                         │
                    ┌────▼────┐
                    │ Signal  │
                    │Subscribe│  (event_bus subscription)
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │Evaluate │
                    │  Entry  │  (config-driven gates)
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │ Pending │
                    │Candidate│  (in signal._candidates queue)
                    └────┬────┘
                         │
              ┌──────────▼──────────┐
              │ CatalystScannerSvc  │
              │   blocked_symbols   │  (pre-filter)
              │   live_quote        │  (Alpaca REST)
              │   price_drift       │  (max 3%)
              └──────────┬──────────┘
                         │
                    ┌────▼────┐
                    │Allocat° │
                    │Candidate│  (to SlotAllocator)
                    └─────────┘
```

### 7.2 earnings_report_v1 (Active)

**Entry criteria:**
- Category: `earnings/report`
- Sentiment: `positive` (from Qwen enrichment)
- Event age: < 240 minutes
- Confidence: > threshold (default 0.5)
- Quote available and within 3% of reference price

**Exit rules:**
- Profit take: +1.0%
- Stop loss: -1.0%
- Trailing stop: activates at +1.0%, distance 0.4%
- Time stop: 60 minutes max hold

**Configuration (hot-reloadable via runtime_config.json):**
```json
{
  "earnings_profit_take_pct": 1.0,
  "earnings_stop_loss_pct": 1.0,
  "earnings_max_hold_minutes": 60,
  "earnings_require_sentiment": "positive",
  "earnings_trailing_enabled": "true",
  "earnings_trailing_activation_pct": 1.0,
  "earnings_trailing_distance_pct": 0.4,
  "earnings_max_event_age_minutes": 240
}
```

### 7.3 filing_8a_v1 (Active)

**Entry criteria:**
- Category: `filing/8-A` (SEC 8-A filings, typically related to new securities/acquisitions)
- Sentiment: `positive`
- Similar config structure to earnings

### 7.4 Signal Router

`signal_router.py` (624 LOC) manages the MultiSignal fan-out:

```python
# Runtime config determines active signals
active_signal = "earnings_report_v1,filing_8a_v1"

# Each signal subscribes to relevant event categories
# Scanner service iterates all active signals
# Candidates from all signals are merged and ranked by score
```

---

## 8. Agent Layer

### 8.1 Architecture

```
AgentOrchestrator
  │
  ├── PMAgent (portfolio-level oversight)
  │   └── Evaluates: sector concentration, P&L, risk limits
  │   └── Can issue force-exit directives
  │
  ├── ScannerAgent (entry approval)
  │   └── Evaluates: candidate quality, market context
  │   └── Can approve, veto, or re-rank candidates
  │
  ├── SlotAgents[0..9] (per-position management)
  │   └── Evaluates: individual exit decisions
  │   └── Can agree, early-cut, or veto exits
  │
  └── PMAnalyst (periodic analysis — independent)
      └── Every 15 min: snapshot → Qwen → structured analysis → dashboard
      └── Works even when agents are disabled
```

### 8.2 LLM Routing

```
All agent LLM calls → LLMClient
  ├── Primary: Qwen3-8B (local, ~200ms, free)
  │   URL: http://192.168.1.166:8000/v1/chat/completions
  │   Timeout: 500ms (agents), 10s (analyst)
  │
  └── Fallback: Claude Sonnet (API, ~2s, paid)
      Only used if Qwen is down and Claude key is configured
```

### 8.3 Guardrails

The agent layer is constrained by deterministic guardrails:

```python
GuardrailValidator:
  - max_override_rate: 20% (agents can't override algo > 20% of the time)
  - position_hard_limits: respect broker constraints always
  - kill_switch: human can halt all agent activity
  - daily_reset: override counters reset at midnight ET
```

### 8.4 PM Analyst

The PM Analyst is a separate concern from the agent PM Agent. It provides automated observability:

```
Every 15 minutes:
  1. Build TradingSnapshot from operator DB
     - Total trades, W/L, win rate, P&L
     - Per-symbol breakdown (top 10 by |P&L|)
     - Stuck positions (held > 60 min)
     - Exit reason distribution
     - Signal P&L breakdown
     - Rapid re-entry detection
     - Slot utilization

  2. Send to Qwen with structured prompt
     → "Identify problems, flag issues, recommend fixes"
     → Response: JSON with issues[], signal_verdict{}, risk_level

  3. Store in pm_analysis table

  4. Dashboard reads latest analysis
     → Severity-coded issue cards
     → Signal verdict badges (effective/marginal/harmful)
     → Risk level badge
     → Top/worst performer highlights
```

---

## 9. Dashboard & Observability

### 9.1 Dashboard Architecture

```
FastAPI (port 8000)
  ├── GET /                          → Main operator dashboard
  ├── GET /admin                     → Admin controls
  ├── GET /backtest                  → Backtest results
  ├── GET /agents                    → Agent dashboard
  ├── GET /llm                       → LLM settings
  │
  ├── GET /api/operator/state        → Full operator state JSON
  ├── GET /api/operator/diagnostics  → Catalyst, scanner, signal, slot stats
  ├── GET /api/operator/news-ticker  → Recent catalyst events
  ├── GET /api/operator/pm-analysis  → Latest PM Analyst report
  ├── POST /api/operator/pm-analysis/run → Trigger immediate analysis
  ├── GET /api/catalyst/event/{id}   → Catalyst event detail + Qwen response
  │
  ├── GET /api/admin/state           → Admin state (slots, positions, config)
  ├── GET /api/admin/runtime-config  → Hot-reloadable config values
  ├── POST /api/admin/runtime-config → Update config (takes effect next cycle)
  ├── POST /api/admin/override/{act} → Manual overrides (pause, flat, reconcile)
  │
  └── GET /api/agents/dashboard      → Agent states, decisions, override rate
```

### 9.2 Dashboard Panels

The main dashboard (`/`) displays:

1. **Top bar:** Operator state, regime, session time, equity, P&L, trade count
2. **PM Analyst panel:** Risk badge, summary, severity-coded issues, signal verdicts
3. **Position slots:** 10-slot grid showing symbol, P&L %, entry/current price, hold time
4. **Equity curve:** Today's equity over time
5. **Recent trades:** Last 20 closed trades with P&L and exit reason
6. **Catalyst pool health:** Event counts, enrichment rate, sentiment distribution
7. **Scanner pipeline:** Scan cycles, acceptance rate, rejection breakdown
8. **Signal P&L:** Per-signal trade count, win rate, total P&L
9. **Per-symbol P&L:** Top/bottom symbols by P&L
10. **Slot utilization:** Visual grid of slot status
11. **Right rail:** Ranked candidate queue, state event log
12. **News ticker:** Scrolling catalyst feed with click-to-drill-down

---

## 10. Configuration & Hot-Reload

### 10.1 Configuration Layers

```
Layer 1: .env file (requires restart)
  MODE=paper
  DRIFTPILOT_SQLITE_PATH=state/operator.sqlite3
  OPERATOR_TRADE_SLOTS=10
  ALPACA_KEY_ID=...
  ALPACA_SECRET_KEY=...
  AGENT_ENABLED=false
  AGENT_QWEN_URL=http://192.168.1.166:8000/v1

Layer 2: runtime_config.json (hot-reload, ~30s)
  active_signal, slot_value, stop/profit percentages,
  trailing stop config, reentry cooldown, sector caps,
  signal-specific parameters

Layer 3: config/universe.csv (manual update)
  Symbol → Sector mapping for sector cap enforcement
```

### 10.2 Hot-Reload Mechanism

```python
# CatalystScannerService._maybe_hot_reload()
# Called every scan cycle (~30s)
# Reads runtime_config.json mtime
# If changed: reload config, update signal parameters
# No restart needed for:
#   - Changing profit/stop percentages
#   - Enabling/disabling trailing stops
#   - Changing max hold time
#   - Switching active signals
#   - Adjusting reentry cooldown
```

---

## 11. Development Timeline

### Phase 0: Planning (Week 1)
- Defined refactor spec separating DriftPilot from legacy trading_bot
- Designed state machine, signal architecture, slot model

### Phase 1: Foundation (Week 1-2)
- SQLite persistence with timezone-aware datetime handling
- Settings management with .env loading
- Clock abstraction (real + fixed for testing)
- Repository pattern with 15 tables

### Phase 2: Broker Integration (Week 2)
- Alpaca client for paper trading
- Quote providers (WebSocket stream + REST fallback)
- Paper fills with slippage model

### Phase 3: Signal Layer (Week 2-3)
- Base signal protocol
- intraday_momentum_v1 (first signal, backtest only)
- Regime detector (SPY-based RED/AMBER/GREEN)
- Feature extraction framework

### Phase 4: Slot Allocator (Week 3)
- 10-slot model with rejection pipeline
- Sector caps, daily limits, duplicate prevention
- Paper fill simulation

### Phase 5: State Machine (Week 3-4)
- Finite state machine with full cycle
- Market hours detection
- Risk halting

### Phase 6: Catalyst Engine (Week 4-5)
- Alpaca News WebSocket integration
- Headline parsing and classification
- Qwen enrichment pipeline (v1, then v2)
- Event bus pub/sub

### Phase 7: Catalyst Signals (Week 5)
- earnings_report_v1
- filing_8a_v1
- analyst_target_raise_v1 (later disabled)
- MultiSignal fan-out/fan-in

### Phase 8: Live Paper Trading (Week 5-6)
- LiveAlpacaAllocator, LiveAlpacaPositionMonitor
- Boot reconciliation
- CatalystScannerService with blocked-symbol pre-filter

### Phase 9: Agent Layer (Week 6-7, 4 waves)
- Wave 1: Message Bus, Guardrails, LLM Client, Prompt Loader
- Wave 2: PM Agent, Scanner Agent, Slot Agent
- Wave 3: Orchestrator lifecycle management
- Wave 4: Dashboard views, training exporter

### Phase 10: Dashboard (Ongoing)
- FastAPI + Jinja2 templates
- Real-time polling (5s state, 15s diagnostics, 30s news)
- Admin controls and runtime config editor

### Phase 11: Production Hardening (Week 7+, Current)
- 12 defects identified and tracked
- 8 defects fixed (blocked symbols, slippage, zombie positions, etc.)
- PM Analyst for automated issue detection
- Operator stability improvements

---

## 12. Lessons Learned

### 12.1 Architecture Lessons

**1. Software stop-losses are inadequate for volatile names.**
We learned this the hard way: a 1% software stop on a 30-second polling cycle resulted in 6-8% actual losses on volatile names (TALO -8.12%, JXN -7.97%). The price dropped well past the stop between polls. **Broker-side stop orders are mandatory for production.** This should have been the default from day one.

**2. In-memory caches are landmines in a restarting system.**
The `_first_seen_prices` drift cache cleared on every operator restart. After 10+ restarts in one day, symbols that had drifted 8% from their catalyst price got fresh "first-seen" prices, bypassing the drift filter entirely. **Any state that affects trading decisions must be persisted.**

**3. Boot reconciliation is a first-class problem.**
When the operator restarts, it must reconstruct the full state from the broker. Our initial reconciliation only passed symbol/quantity/price — missing entry_ts, sector, and signal_name. This caused zombie positions (no metadata → signal can't evaluate exit → position held forever). **Reconciliation must preserve the full metadata contract.**

**4. Accumulation-only sets cause permanent state drift.**
`_blocked_symbols |= active_symbols` only adds, never removes. Once a slot freed up, the symbol stayed blocked until midnight. **Always rebuild derived state from source of truth, never accumulate incrementally.**

**5. The rejection pipeline order matters.**
We originally had sector_cap before reentry_cooldown. This meant a symbol could re-enter immediately after exit if the sector cap hadn't been reached. The correct order is: duplicate → day_cap → consecutive_loss → reentry_cooldown → sector_cap → no_free_slot. **Order rejections from cheapest/fastest to most expensive.**

### 12.2 Signal Lessons

**6. Negative EV signals occupy slots and destroy capital.**
`analyst_target_raise_v1` had edge_ratio=0.85 (negative EV from backtest) but was running in production. 146 trades, net -$2.42, 13 TIME_STOP exits. Each TIME_STOP means the position sat for 60 minutes doing nothing — blocking a slot that could have run a profitable signal. **Never deploy a signal without positive backtest expectancy.**

**7. Sentiment classification is a critical dependency.**
We trusted Qwen's sentiment classification without verification. It classified "QuickLogic Posts Downbeat Q1 Results" as positive (or null), leading to a -$123.73 loss on REZI. **Always verify LLM outputs against ground truth. Build a sentiment accuracy dashboard.**

**8. Asymmetric risk/reward is a configuration error.**
Stop_loss at 1.5% vs profit_take at 1.0% means each loss is 50% larger than each win. Even with 60% win rate, the system loses money. **Risk/reward must be symmetric or skewed in your favor.**

### 12.3 Operational Lessons

**9. You need observability before you need features.**
We built 7 signals, a 4-wave agent layer, and a catalyst engine before we had proper diagnostics. When things went wrong in production, we had to write forensic SQL queries to figure out what happened. The PM Analyst should have been one of the first things built. **Instrument first, optimize second.**

**10. Machine-gun re-entry is the most expensive bug.**
ORCL was bought 5 times in 8 minutes, all losses (-$86.32 total). Without a reentry cooldown, the system keeps re-entering on the same catalyst event. **Every trading system needs a cooldown mechanism from day one.**

**11. 10+ operator restarts per day indicates systemic instability.**
Each restart clears in-memory state, triggers reconciliation edge cases, and creates data discontinuities. **Investigate root causes of crashes before adding features.**

**12. Configuration defaults must be conservative.**
`max_trades_per_symbol_per_day=5` was too high. Data showed symbols like ORCL and TXN losing on every single trade (5/5 losses). **Default to 3 or add "stop after 2 consecutive losses on same symbol."**

### 12.4 Process Lessons

**13. Track defects from the start.**
We built a significant system without a defect tracker. When the user said "you wrote code full of defects and you are not writing it down," they were right. DEFECTS.md should have existed from phase 1. **Every bug fix should update a living document.**

**14. Write tests before production deployment.**
Several bugs (sector cap defaults, daily loss limit defaults, missing AsyncMock returns) were only caught when we ran the test suite post-deployment. **Test suite must pass before any code reaches the operator.**

**15. Hot-reload is not a substitute for architecture.**
We rely heavily on runtime_config.json hot-reload to patch issues without restarts. But some fixes (broker-side stops, persistent caches) require architectural changes that hot-reload can't address. **Use hot-reload for tuning, not for fixing structural problems.**

---

## 13. What We Should Have Done From the Start

### 13.1 Day-One Requirements (Missed)

| What | Why | Impact of Missing It |
|------|-----|---------------------|
| Broker-side stop orders | Software stops are unreliable on polling cycles | $597 in excess slippage on 8 worst trades |
| Reentry cooldown | Catalyst events persist longer than positions | $86 lost on ORCL machine-gun (5 trades, 8 minutes) |
| Persistent drift cache | Operator restarts 10+ times/day | Drift filter bypassed after every restart |
| Sentiment verification | LLM classification is probabilistic | 4 trades on negative news, -$123 |
| Full reconciliation metadata | Boot loses state | 3 zombie positions held 490+ minutes |
| PM Analyst / observability | Can't fix what you can't see | Defects accumulated silently for hours |
| DEFECTS.md tracker | Process accountability | No institutional memory of what broke and why |
| Conservative defaults | Markets punish aggressive defaults | 5 trades/symbol/day was too permissive |

### 13.2 Correct Build Order (Retrospective)

If starting over, we would build in this order:

```
Phase 1: Foundation + Observability
  ├── SQLite persistence ✓
  ├── Settings + hot-reload ✓
  ├── PM Analyst (observability from day one!) ✗ built in Phase 11
  └── DEFECTS.md tracker ✗ built in Phase 11

Phase 2: Broker with Safety Defaults
  ├── Alpaca client ✓
  ├── Broker-side stop orders ✗ still not done
  ├── Reentry cooldown built-in ✗ built in Phase 11
  └── Conservative daily limits (3 per symbol, not 5) ✗

Phase 3: One Signal, End-to-End
  ├── earnings_report_v1 only ✓
  ├── Full test coverage before deployment ✗
  ├── Sentiment verification pipeline ✗
  └── One week of paper trading observation before adding more

Phase 4: Scale Signals
  ├── Only add signals with positive backtest expectancy ✗
  ├── A/B test each signal independently ✗
  └── Kill signals immediately when negative EV confirmed ✓ (eventually)

Phase 5: Agent Layer
  ├── Only after manual operation is stable ✓
  ├── Guardrails before capabilities ✓
  └── Training data pipeline from day one ✓
```

### 13.3 Testing Strategy (Retrospective)

We should have enforced:
1. **Pre-commit:** All tests pass before any commit
2. **Pre-deploy:** Backtest expectancy check for every active signal
3. **Post-deploy:** 15-minute automated health check (PM Analyst)
4. **Daily:** EOD report with trade-by-trade forensics
5. **Weekly:** Signal performance review, disable underperformers

---

## 14. Known Defects & Technical Debt

### 14.1 Open Defects

| # | Severity | Title | Status |
|---|----------|-------|--------|
| 9 | P0 | Stop-loss slippage (6-8% actual on 1% stop) | OPEN |
| 11 | P0 | Sentiment misclassification (bought negative news) | OPEN |
| 12 | P0 | Drift cache resets on operator restart | OPEN |

### 14.2 Fixed Defects

| # | Title | Fix |
|---|-------|-----|
| 1 | Scanner re-emits blocked symbols every cycle | Pre-filter with `_refresh_blocked_symbols()` |
| 2 | Asymmetric risk/reward (avg loss > avg win) | Config: stop_loss 1.5% → 1.0% |
| 3 | Trailing stop can never trigger | Config: trailing_distance 2.0% → 0.4% |
| 4 | Sector mapping broken for reconciled positions | Added `_sector_map` from universe.csv |
| 5 | Machine-gun re-entry (same symbol re-bought immediately) | Added `reentry_cooldown` to allocator |
| 6 | Zombie positions (held far beyond max_hold) | Added `FAILSAFE_TIME_STOP` |
| 7 | Boot reconciliation loses all metadata | Added full metadata to reconciliation |
| 8 | Blocked symbols are permanent within session | Rebuild set from scratch each cycle |
| 10 | analyst_target_raise_v1 is negative EV | Disabled in runtime config |

### 14.3 Technical Debt

1. **No broker-side stop orders** — Most impactful debt item
2. **4 pre-existing test failures** in analyst_target_raise_v1 — test events missing sentiment field
3. **`_first_seen_prices` not persisted** — cleared on restart
4. **`get_fill_price()` accuracy unverified** — may return order limit, not actual fill
5. **No automated backtest gate** — signals can be enabled without proving positive EV
6. **Dashboard reads SQLite directly** — no caching layer, may cause WAL contention under load
7. **No circuit breaker for Qwen** — if Qwen is slow, enrichment backs up

---

## 15. Low-Level Technical Reference

### 15.1 File Map

```
src/driftpilot/                         # Core package (22,750 LOC)
├── __init__.py
├── operator.py                         # CLI entry point, asyncio loop
├── state_machine.py                    # FSM: run_once() cycle (615 LOC)
├── states.py                           # OperatorState enum, BlockedReason enum
├── settings.py                         # DriftPilotSettings from .env (189 LOC)
├── runtime_config.py                   # Hot-reloadable runtime_config.json
├── clock.py                            # DriftPilotClock, FixedClock, tz utilities
├── observer.py                         # Observer pattern for state changes
├── regime_detector.py                  # SPY regime (RED/AMBER/GREEN)
├── signal_router.py                    # MultiSignal fan-out/fan-in (624 LOC)
├── services.py                         # Paper/mock service implementations
├── services_live.py                    # Live Alpaca service implementations (1,422 LOC)
│
├── storage/
│   └── repositories.py                 # SQLite repository pattern (1,720 LOC)
│
├── broker/
│   └── alpaca_client.py                # Alpaca API wrapper (792 LOC)
│
├── market_data/
│   ├── alpaca_stream.py                # WebSocket market data (336 LOC)
│   └── rest_quotes.py                  # REST quote provider with cache
│
├── execution/
│   ├── slot_allocator.py               # 10-slot allocator + rejection pipeline (458 LOC)
│   └── paper_fills.py                  # Slippage model for paper trading
│
├── catalyst/
│   ├── event.py                        # CatalystEvent dataclass
│   ├── event_bus.py                    # Pub/sub event bus
│   ├── feed_alpaca.py                  # Alpaca News WebSocket
│   ├── feed_rss.py                     # RSS feed polling
│   ├── headline_parser.py              # Headline → symbol + category
│   ├── classifier.py                   # Rule-based pre-classification
│   ├── qwen_enricher.py               # Qwen LLM sentiment enrichment
│   ├── context_assembler.py            # Build enrichment context (375 LOC)
│   ├── universe_filter.py              # Filter to tradeable universe
│   ├── db.py                           # Catalyst SQLite persistence
│   └── discovery_service.py            # Feed → parse → enrich → publish
│
├── signals/
│   ├── base.py                         # Signal protocol
│   ├── features.py                     # MinuteBar, feature extraction
│   ├── scoring.py                      # Score normalization
│   ├── regime.py                       # Regime data types
│   ├── intraday_momentum.py            # Entry filter for momentum
│   ├── earnings_report_v1/             # ACTIVE: earnings surprise signal
│   ├── filing_8a_v1/                   # ACTIVE: 8-A filing signal
│   ├── analyst_target_raise_v1/        # DISABLED: analyst upgrade signal
│   ├── apex_hunter_v2/                 # Backtest: volume spike
│   ├── whale_tail_v1/                  # Backtest: institutional accumulation
│   ├── rs_drift_v1/                    # Backtest: relative strength
│   └── stationary_ghost_v1/            # Backtest: mean reversion
│
├── agents/
│   ├── orchestrator.py                 # Agent lifecycle + tick routing
│   ├── factory.py                      # Build orchestrator from settings
│   ├── pm_agent.py                     # Portfolio Manager agent (426 LOC)
│   ├── scanner_agent.py                # Entry approval agent
│   ├── slot_agent.py                   # Per-position exit agent
│   ├── pm_analyst.py                   # Periodic Qwen analysis (543 LOC)
│   ├── state_machine_bridge.py         # Adapter: state machine ↔ agents (357 LOC)
│   ├── llm_client.py                   # Qwen/Claude HTTP client
│   ├── message_bus.py                  # SQLite-backed message queue (423 LOC)
│   ├── guardrail_validator.py          # Override rate limits
│   ├── prompt_loader.py                # Load prompts from config/prompts/
│   ├── market_data_adapter.py          # Market data for agent context
│   ├── training_exporter.py            # Export decisions for fine-tuning (387 LOC)
│   └── models.py                       # Shared agent data types
│
├── backtest/
│   ├── replay.py                       # Bar-by-bar backtest engine (1,151 LOC)
│   ├── catalyst_replay.py              # Catalyst-driven backtest
│   ├── metrics.py                      # Sharpe, drawdown, etc.
│   ├── report.py                       # HTML/JSON backtest reports
│   ├── constants.py                    # Backtest defaults
│   ├── baseline_lookup.py              # SPY baseline comparison
│   └── limit_fill.py                   # Limit order fill simulation
│
└── dashboard/
    ├── view_models.py                  # Build dashboard JSON (1,035 LOC)
    └── agent_views.py                  # Agent dashboard JSON

src/trading_bot/dashboard/
├── app.py                              # FastAPI application (~1,020 LOC)
└── templates/
    ├── dashboard.html                  # Main operator dashboard
    ├── admin.html                      # Admin controls
    ├── backtest.html                   # Backtest results
    ├── agents.html                     # Agent dashboard
    └── settings.html                   # LLM settings

tests/
├── test_driftpilot_foundation.py       # Schema, settings, datetime tests
├── test_driftpilot_acceptance.py       # End-to-end acceptance tests
├── test_services_live.py               # Live service mocked tests
├── test_driftpilot_phase4_execution.py # Allocator tests
└── tests/signals/                      # Per-signal test suites
```

### 15.2 Key Data Types

```python
# Allocation pipeline
@dataclass
class AllocationCandidate:
    symbol: str
    score: float
    sector: str
    latest_bar_at: datetime
    rank: int = 0
    metadata: dict = field(default_factory=dict)

@dataclass
class SlotAllocation:
    symbol: str
    slot_id: int
    slot_value: float
    sector: str
    rank: int
    score: float
    reserved_at: datetime

@dataclass
class AllocationRejection:
    symbol: str
    reason: str          # from REJECTION_ORDER
    metadata: dict

@dataclass
class AllocationResult:
    allocations: tuple[SlotAllocation, ...]
    rejections: tuple[AllocationRejection, ...]

# Broker
@dataclass
class OrderSubmissionResult:
    submitted: bool
    broker_order_id: str | None
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: float | None
    reason: str

# Market data
@dataclass
class MarketQuote:
    symbol: str
    timestamp: datetime
    bid_price: float
    ask_price: float

# PM Analyst
@dataclass
class TradingSnapshot:
    timestamp: str
    total_trades: int
    open_positions: int
    closed_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    total_pnl: float
    avg_win: float
    avg_loss: float
    symbol_pnl: list[dict]
    stuck_positions: list[dict]
    exit_reasons: dict[str, dict]
    signal_pnl: dict[str, dict]
    rapid_reentries: list[dict]
    slots_empty: int
    slots_active: int
    total_slots: int
    active_signals: str
```

### 15.3 Environment Variables

```bash
# Core
MODE=paper                                    # paper | live
DRIFTPILOT_SQLITE_PATH=state/operator.sqlite3
DRIFTPILOT_TIMEZONE=America/New_York

# Trading
OPERATOR_TRADE_SLOTS=10
MAX_TRADES_PER_DAY=50
MAX_TRADES_PER_SYMBOL_PER_DAY=5
DAILY_LOSS_LIMIT_PCT=0.05
PAPER_CAPITAL=30000

# Alpaca
ALPACA_KEY_ID=PK...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# Catalyst
CATALYST_ENABLED=true
CATALYST_DB_PATH=data/driftpilot/catalyst_events.sqlite3
CATALYST_QWEN_URL=http://192.168.1.166:8000/v1
CATALYST_QWEN_MODEL=Qwen/Qwen3-8B

# Agents
AGENT_ENABLED=false
AGENT_QWEN_URL=http://192.168.1.166:8000/v1
AGENT_QWEN_MODEL=Qwen/Qwen3-8B
AGENT_DB_PATH=data/driftpilot/agent_messages.sqlite3
```

### 15.4 API Response Examples

**GET /api/operator/pm-analysis**
```json
{
  "status": "ok",
  "analysis": {
    "summary": "Session: 42 closed, 25W/17L, P&L +$23.45. 2 issues found.",
    "pnl_status": "winning",
    "issues": [
      {
        "severity": "critical",
        "title": "3 zombie positions: MAS, GNW, ON",
        "detail": "Held beyond max_hold. Reconciled without metadata.",
        "recommendation": "Restart operator with FAILSAFE_TIME_STOP fix."
      },
      {
        "severity": "warning",
        "title": "Asymmetric risk: avg loss $8 > avg win $5",
        "detail": "Losses are larger than wins. Stop loss may be slipping.",
        "recommendation": "Check stop_loss_pct config. Consider broker-side stops."
      }
    ],
    "top_performers": ["AAPL", "MSFT"],
    "worst_performers": ["TALO", "JXN", "ORCL"],
    "signal_verdict": {
      "earnings_report_v1": "effective",
      "filing_8a_v1": "marginal"
    },
    "stuck_position_action": "force_close",
    "risk_level": "high",
    "_meta": {
      "analyzed_at": "2026-05-13T18:15:00+00:00",
      "latency_ms": 342
    }
  }
}
```

### 15.5 Running the System

```bash
# Start the operator (main trading loop)
cd "Trading BOT"
source .venv/bin/activate
python -m driftpilot.operator

# Start the dashboard (separate terminal)
uvicorn trading_bot.dashboard.app:app --host 0.0.0.0 --port 8000

# Run tests
python -m pytest tests/ -x -q

# Daily cron (automated)
scripts/daily_operator.sh    # launches at 09:25 ET
scripts/daily_stop.sh        # stops at 16:05 ET
```

### 15.6 Dependency Graph (Runtime)

```
Alpaca API ──┬── News WebSocket ──▶ DiscoveryService ──▶ EventBus
             │
             ├── Quote REST ──▶ AlpacaRestQuoteProvider ──▶ Scanner/Monitor
             │
             └── Trading API ──▶ AlpacaBrokerClient ──▶ Allocator/Monitor

Qwen (local) ──┬── Enrichment ──▶ QwenEnricher ──▶ catalyst_events DB
               │
               ├── Agent calls ──▶ LLMClient ──▶ PM/Scanner/Slot Agents
               │
               └── PM Analyst ──▶ PMAnalyst ──▶ pm_analysis DB

SQLite ──┬── operator.sqlite3 ──▶ State Machine, Dashboard
         │
         ├── catalyst_events.sqlite3 ──▶ Enricher, Scanner, Dashboard
         │
         ├── agent_messages.sqlite3 ──▶ Agent Bus, Dashboard
         │
         └── pm_analysis (in operator DB) ──▶ Dashboard
```

---

*This document reflects the system as of May 13, 2026, after 195 commits and 12 identified defects (8 fixed, 3 open, 1 disabled). DriftPilot is in active paper-trading operation with automated cron scheduling and real-time dashboard monitoring.*
