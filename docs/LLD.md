# DriftPilot — Low-Level Design Document

**Last updated:** 2026-05-14
**Status:** All 10 Codex tasks complete. System operational in paper trading.

---

## 1. System Overview

DriftPilot is an autonomous intraday paper-trading operator that runs on
two machines:

| Machine | Role | Processes |
|---------|------|-----------|
| **MacBook** | Trading logic, execution, dashboard | Operator, Dashboard (:8501), Alpaca API |
| **DGX Spark** (192.168.1.166) | AI reasoning, memory, LLM | Brain Server (:8100), Qwen3-8B vLLM (:8000), PostgreSQL+pgvector (:5432) |

---

## 2. Architecture Diagram

```mermaid
flowchart TB
    subgraph MacBook["MacBook — Local Machine"]
        subgraph Operator["Operator Loop (driftpilot.operator)"]
            SM["State Machine\n(state_machine.py)"]
            Clock["Clock\n(clock.py)"]
        end

        subgraph Signals["Signal Layer"]
            MS["MultiSignal Router\n(services_live.py)"]
            ATR["analyst_target_raise_v1"]
            VS["volume_spike_v1"]
            ER["earnings_report_v1"]
            MS --> ATR
            MS --> VS
            MS --> ER
        end

        subgraph Catalyst["Catalyst Discovery"]
            RSS["RSS Feeds\n(feed_rss.py)"]
            AlpNews["Alpaca News\n(feed_alpaca.py)"]
            Bus["Event Bus"]
            Classifier["Classifier"]
            CA["Context Assembler\n+ yfinance enrichment\n(sector, beta, ATR)"]
            Enricher["Qwen Enricher"]
            RSS --> Bus
            AlpNews --> Bus
            Bus --> Classifier
            Classifier --> CA
            CA --> Enricher
        end

        subgraph Execution["Execution Layer"]
            DB["Dynamic Bands\n(compute_dynamic_bands)\nATR / Drift / RVOL\nBeta / Catalyst / Time"]
            Alloc["Slot Allocator\n(slot_allocator.py)"]
            PM["PM Agent\n(pm_agent.py)\n+ BrainClient"]
            Guard["Guardrail Validator\n(sector cap, position limits)"]
            DB --> Alloc
            Alloc --> PM
            PM --> Guard
        end

        subgraph Dashboard["Dashboard :8501\n(trading_bot.dashboard)"]
            DPipe["/pipeline — Candidates + P&L"]
            DBrain["/brain — Skills + Reflections"]
            DAdmin["/admin — Diagnostics"]
            DAgents["/agents — Decision logs"]
        end

        subgraph Storage["Local Storage"]
            SQLite["operator_state.sqlite3\nPositions / Slots / Sectors"]
            PipeLog["pipeline_log.json\nScan cycle history"]
            CatDB["catalyst.sqlite3\nHeadlines / Events"]
        end

        Alpaca["Alpaca Paper API\n(paper-api.alpaca.markets)"]
    end

    subgraph DGX["DGX Spark — 192.168.1.166"]
        subgraph Brain["Brain Server :8100\n(brain_server.py)"]
            BAPI["FastAPI\n/brain/health\n/brain/query\n/brain/store\n/brain/reflect\n/brain/skills"]
            Embed["Embedder\n(all-MiniLM-L6-v2)"]
        end

        subgraph BrainDB["Brain Storage"]
            PGV["PostgreSQL + pgvector\nExperiences vector(384)\nSkills vector(384)\nReflections"]
        end

        Qwen["Qwen3-8B vLLM :8000\nLLM for PM decisions\n+ catalyst enrichment\n+ EOD reflection"]

        BAPI --> Embed
        BAPI --> PGV
    end

    SM --> MS
    Enricher --> MS
    MS --> DB
    Guard --> Alpaca
    PM -->|query similar trades| BAPI
    PM -->|store experience| BAPI
    PM -->|LLM decisions| Qwen
    Enricher -->|enrich catalysts| Qwen
    CA -->|sector, beta, ATR| DB
    Dashboard -->|/api/brain/status| BAPI
    Dashboard -->|/api/operator/pipeline| SQLite
    SM -.->|EOD 4pm| BAPI
    Alpaca -->|market data, orders| Operator
```

---

## 3. Trade Lifecycle

```mermaid
sequenceDiagram
    participant OP as Operator
    participant SIG as MultiSignal
    participant CA as Context Assembler
    participant Q as Qwen LLM
    participant DB as Dynamic Bands
    participant PM as PM Agent
    participant BR as Brain Server
    participant ALP as Alpaca

    rect rgb(40, 40, 60)
    Note over OP,ALP: SCAN CYCLE (every 60s)
    OP->>SIG: scan(now)
    SIG->>CA: enrich candidates
    CA->>CA: yfinance: sector, beta, ATR
    CA->>Q: Qwen enrichment prompt
    Q-->>CA: enriched context
    CA-->>SIG: candidates with features
    SIG-->>OP: ranked candidates
    end

    rect rgb(40, 60, 40)
    Note over OP,ALP: ENTRY DECISION
    OP->>DB: compute_dynamic_bands(atr, beta, rvol, catalyst, drift, time)
    DB-->>OP: target_pct, stop_pct, reasoning
    OP->>PM: approve entry?
    PM->>BR: query similar past trades
    BR-->>PM: past experiences + outcomes
    PM->>Q: LLM decision + brain context
    Q-->>PM: APPROVE or DENY
    PM->>ALP: submit limit order
    ALP-->>PM: order filled
    PM->>BR: store experience
    end

    rect rgb(60, 40, 40)
    Note over OP,ALP: POSITION MONITORING
    OP->>ALP: check prices vs bands
    alt Target hit
        OP->>ALP: close (TARGET)
    else Stop hit
        OP->>ALP: close (STOP)
    else EOD
        OP->>ALP: close (TIME)
    end
    OP->>BR: backfill outcome
    end

    rect rgb(50, 40, 50)
    Note over OP,BR: END OF DAY (4:00 PM)
    OP->>BR: POST /brain/reflect
    BR->>Q: analyze trades
    Q-->>BR: patterns + new skills
    BR->>BR: save to pgvector
    end
```

---

## 4. Directory Structure

```
src/driftpilot/                    # Core operator (active codebase)
  operator.py                      # CLI entrypoint (--paper-live, --once)
  state_machine.py                 # BOOT → SCANNING → ALLOCATING → IN_POSITION → EXITING
  services_live.py                 # Live service builder + MultiSignal + DynamicBands
  clock.py                         # Timezone-aware time owner
  settings.py                      # Env-backed settings
  signal_router.py                 # Rule-based candidate routing
  regime_detector.py               # SPY-based market regime

  agents/
    pm_agent.py                    # Portfolio manager — LLM-powered entry/exit decisions
    brain_client.py                # HTTP client for DGX Brain server
    guardrail_validator.py         # Sector cap, position limits, risk checks
    orchestrator.py                # Agent coordination
    llm_client.py                  # Qwen HTTP wrapper

  catalyst/
    context_assembler.py           # yfinance enrichment (sector, beta, ATR, market cap)
    discovery_service.py           # Alpaca News + RSS feed coordination
    classifier.py                  # Headline → catalyst type classifier
    qwen_enricher.py               # Qwen sentiment/context enrichment
    event_bus.py                   # Pub/sub for catalyst events
    db.py                          # catalyst.sqlite3 schema

  signals/
    base.py                        # SignalProtocol, Candidate, ExitDecision
    analyst_target_raise_v1/       # Analyst price target signal
    volume_spike_v1/               # Relative volume spike signal
    earnings_report_v1/            # Earnings catalyst signal
    (+ 4 technical signals)

  execution/
    slot_allocator.py              # Fixed $1k slot allocation under lock
    paper_fills.py                 # Paper trading fill simulation

  broker/
    alpaca_client.py               # Alpaca paper/live API wrapper

  storage/
    repositories.py                # SQLite repository pattern

src/trading_bot/dashboard/         # FastAPI dashboard (OK to edit)
  app.py                           # Routes: /, /pipeline, /brain, /agents, /admin
  templates/
    pipeline.html                  # Candidate cycles + open positions + P&L
    brain.html                     # Brain skills, experiences, reflections
    agents.html                    # Agent decision audit trail
    admin.html                     # System diagnostics
    dashboard.html                 # Main overview
    backtest.html                  # Backtest results

dgx/                               # Brain server (deployed to DGX Spark)
  brain_server.py                  # FastAPI: /brain/health, /query, /store, /reflect, /skills
  brain_db.py                      # ChromaDB backend (fallback)
  brain_db_pgvector.py             # PostgreSQL+pgvector backend (production)
  brain_embedder.py                # sentence-transformers/all-MiniLM-L6-v2
  start_brain.sh                   # Startup script (defaults to pgvector)
  setup_pgvector.sh                # One-time PostgreSQL setup

scripts/
  launchd_start.py                 # macOS launchd startup (9:25 AM Mon-Fri)
  launchd_stop.py                  # macOS launchd shutdown (4:05 PM Mon-Fri)

data/driftpilot/
  operator_state.sqlite3           # Positions, slots, orders, fills, state
  pipeline_log.json                # Scan cycle audit trail
  catalyst.sqlite3                 # Headlines, events, enrichments
```

---

## 5. Component Details

### 5.1 Operator State Machine

States: `BOOT → SCANNING → ALLOCATING → IN_POSITION → EXITING → RECYCLING → SCANNING`

The operator runs one async loop during market hours (9:30 AM – 4:00 PM ET).
Each cycle: check market session → scan signals → allocate candidates →
monitor positions → exit if triggered → recycle slots.

### 5.2 MultiSignal Router

`MultiSignal` in `services_live.py` runs multiple signal scanners in parallel
and merges results. Deduplicates by symbol (keeps highest score). Currently
active signals:

| Signal | Type | Status |
|--------|------|--------|
| `analyst_target_raise_v1` | Catalyst | Active |
| `volume_spike_v1` | Technical + Volume | Active |
| `earnings_report_v1` | Catalyst | Gated (edge_ratio 1.105) |

### 5.3 Context Assembler + Enrichment

`context_assembler.py` enriches each candidate with:

| Field | Source | Fallback |
|-------|--------|----------|
| **sector** | universe.csv → yfinance | "Unknown" |
| **beta** | yfinance `info.get("beta")` | None |
| **ATR %** | yfinance 14-period ATR / price | 1.2% default |
| **market_cap** | yfinance | None |
| **avg_volume** | yfinance | None |
| **sentiment** | Qwen LLM enrichment | neutral |

### 5.4 Dynamic Bands (`compute_dynamic_bands`)

Adaptive stop/target calculation with 8-factor pipeline:

```
1. ATR base         → target = ATR × 0.5, stop = ATR × 0.75
2. Drift tax        → target reduced by 30% of drift (floor 0.2%)
3. RVOL boost       → each 1x above 1.0 adds 10% to target
4. Beta profile     → beta > 1.5 widens both bands 20%
5. Catalyst profile → earnings +40%, analyst +15%, FDA +50%
6. Time-of-day      → open: stop × 1.30, close: stop × 1.10
7. Spread cost      → deducted from target (floor 0.1%)
8. Guardrail clamp  → stop capped at MAX_STOP_LOSS_PCT (3%)
```

Returns `DynamicBands(target_pct, stop_pct, reasoning)` — the reasoning
string traces every adjustment for the dashboard audit trail.

**Test coverage:** 39 tests in `tests/test_dynamic_bands.py` covering all paths.

### 5.5 PM Agent + Brain Integration

`pm_agent.py` makes LLM-powered entry/exit decisions:

1. Receives candidate + dynamic bands + context
2. Queries Brain for similar past trades (`brain_client.query()`)
3. If brain returns results, injects "Past similar trades: ..." into LLM prompt
4. Sends prompt to Qwen → gets APPROVE/DENY + reasoning
5. On trade close, stores experience back to Brain (`brain_client.store_experience()`)
6. Brain is **optional** — if offline, PM Agent works identically without brain context

### 5.6 Brain Server (DGX Spark)

FastAPI server at port 8100 with pgvector backend:

| Endpoint | Purpose |
|----------|---------|
| `GET /brain/health` | Health check + backend info |
| `POST /brain/query` | Find similar past trades (cosine similarity) |
| `POST /brain/store` | Store trade experience with embedding |
| `POST /brain/backfill` | Add outcome to existing experience |
| `POST /brain/reflect` | EOD reflection — analyze day's trades, create skills |
| `GET /brain/skills` | List active skills |
| `GET /brain/stats` | Database statistics |

**Storage:** PostgreSQL 16 with pgvector extension. Tables:

| Table | Key Columns |
|-------|-------------|
| `experiences` | id, embedding vector(384), context JSONB, decision JSONB, outcome JSONB |
| `skills` | id, embedding vector(384), title, rule, confidence, status |
| `reflections` | id, date, summary JSONB, skills_created, skills_retired |

IVFFlat indexes on vector columns for approximate nearest-neighbor search.

**Embedding:** `sentence-transformers/all-MiniLM-L6-v2` (384 dimensions).
**Fallback:** `BRAIN_DB_BACKEND=chroma` switches to ChromaDB for local dev.

### 5.7 EOD Reflection

At market close (4:00 PM ET), the operator triggers `/brain/reflect`:

1. Collects today's closed trades from positions table
2. POSTs to Brain server with today's date
3. Brain sends trades to Qwen for pattern analysis
4. Qwen identifies winning/losing patterns → Brain creates/retires skills
5. Skills stored with embeddings for future similarity queries
6. Runs exactly once per trading day (guard flag)

### 5.8 Dashboard

FastAPI at port 8501 with Jinja2 templates:

| Page | Route | Content |
|------|-------|---------|
| Pipeline | `/pipeline` | Candidate cycles, open positions with live P&L (green/red), band reasoning |
| Brain | `/brain` | Skills table, experiences, reflections, stats. "Brain offline" when DGX down |
| Agents | `/agents` | Agent decision audit trail |
| Admin | `/admin` | System diagnostics, operator state |
| Backtest | `/backtest` | Historical backtest results |

The `/pipeline` page polls `/api/operator/pipeline` which reads from
`pipeline_log.json` (scan cycles) and `operator_state.sqlite3` (positions),
then fetches live Alpaca prices for P&L calculation.

The `/brain` page proxies to the DGX Brain server via `BrainClient`.

---

## 6. Scheduling (macOS launchd)

| Plist | Schedule | Script | Action |
|-------|----------|--------|--------|
| `com.driftpilot.operator` | 9:25 AM Mon-Fri | `scripts/launchd_start.py` | Start operator + dashboard |
| `com.driftpilot.stop` | 4:05 PM Mon-Fri | `scripts/launchd_stop.py` | Kill processes + EOD analysis |

Scripts call Python directly (not bash) to avoid macOS security restrictions.
Child processes use `start_new_session=True` for detachment from launchd.

---

## 7. Brain Learning Loop

```
Day N trade → experience stored (embedding + context + decision)
                ↓
Day N 4:00 PM → EOD reflection → Qwen analyzes all trades
                ↓
Patterns found → skills created (with embedding + rule + confidence)
                ↓
Day N+1 → PM Agent queries brain → similar trades + active skills
                → influences APPROVE/DENY decisions
                ↓
Trade outcome → backfilled to experience → feeds next reflection
```

---

## 8. Environment Variables

```bash
# Alpaca (MacBook .env)
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# LLM (DGX Spark)
QWEN_URL=http://192.168.1.166:8000

# Brain (DGX Spark)
BRAIN_URL=http://192.168.1.166:8100
BRAIN_DB_BACKEND=pgvector              # or "chroma" for local dev
BRAIN_PG_DSN=postgresql://brain:brain@localhost:5432/brain

# Catalyst
CATALYST_ENABLED=true
ACTIVE_SIGNAL=earnings_report_v1
```

---

## 9. How to Run

```bash
# MacBook — Operator
cd "Trading BOT"
source .venv/bin/activate
python -m driftpilot.operator --paper-live

# MacBook — Dashboard
python -m uvicorn trading_bot.dashboard.app:app --host 0.0.0.0 --port 8501

# DGX Spark — Brain Server
ssh sankerkr@192.168.1.166
cd ~/brain && bash start_brain.sh

# DGX Spark — Health Check
curl http://192.168.1.166:8100/brain/health
```

---

## 10. Testing

```bash
# Full test suite
python -m pytest tests/ -x -q

# Dynamic bands tests (39 tests)
python -m pytest tests/test_dynamic_bands.py -v

# Context assembler tests
python -m pytest tests/catalyst/test_context_assembler.py -v

# Brain DB tests (on DGX, both backends)
cd ~/brain
BRAIN_DB_BACKEND=pgvector BRAIN_PG_DSN=postgresql://brain:brain@localhost:5432/brain \
    python -m pytest test_brain_db.py -v
```
