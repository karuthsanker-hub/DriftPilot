# DriftPilot — Project Overview

This is the single document that explains the whole project at a glance. Read this first when returning to the codebase. For deeper detail, follow links into [REFACTOR_PLAN.md](../REFACTOR_PLAN.md) (authoritative spec), [ARCHITECTURE.md](ARCHITECTURE.md) (runtime details), and per-signal `README.md` / `KNOWN_RISKS.md` files.

## What DriftPilot Is

A **continuous autonomous intraday paper-trading operator**. One async state-machine loop:

1. Streams Alpaca SIP bars/quotes during market hours.
2. Scans the S&P 1500-ish universe through one of 5 pluggable signal algorithms.
3. Allocates ranked candidates into 10 fixed $1,000 paper-trading slots (or signal-specific slot models).
4. Exits on signal-specific rules (target/stop/time, ATR-scaled, three-stage Ratchet, etc.).
5. Recycles freed slots back into the candidate queue.
6. Persists every state transition to SQLite — the dashboard explains *why* it is or isn't trading.

Live trading is **blocked by default** until a four-criterion live deploy gate passes.

## Top-Level Component Map

```mermaid
flowchart LR
    subgraph "External"
        Alpaca["Alpaca SIP<br/>(bars + quotes + paper broker)"]
        Databento["Databento<br/>(historical 1-min bars)"]
        FRED["FRED API"]
        LLMs["OpenAI / Claude / Gemini / Qwen"]
    end

    subgraph "DriftPilot Runtime — src/driftpilot/"
        Operator["operator.py<br/>(CLI entry)"]
        StateMachine["state_machine.py<br/>BOOT → SCANNING → ALLOCATING → IN_POSITION..."]
        Signals["signals/<br/>5 registered algos"]
        Allocator["execution/<br/>SlotAllocator + paper fills"]
        Broker["broker/<br/>Alpaca client + live gate"]
        Stream["market_data/<br/>SIP stream + cache"]
        Storage["storage/<br/>SQLite repos"]
        Backtest["backtest/<br/>replay + report"]
        Clock["clock.py"]
    end

    subgraph "Dashboard — src/trading_bot/dashboard/"
        FastAPI["FastAPI app<br/>+ Operator/Admin/Backtest/LLM tabs"]
    end

    subgraph "Storage on disk"
        SQLite[("SQLite<br/>operator_state.sqlite3")]
        Parquet[("Parquet<br/>data/bars/databento/{SYM}/{YEAR}.parquet")]
        Reports[("reports/<br/>{signal}/{date}_{verdict}.json")]
    end

    Alpaca <--> Stream
    Alpaca <--> Broker
    Databento --> Parquet

    Stream --> Signals
    Signals --> Allocator
    Allocator --> Broker
    Broker --> Storage
    StateMachine --> Storage
    Storage --> SQLite

    Backtest --> Parquet
    Backtest --> Signals
    Backtest --> Reports

    Operator --> StateMachine
    StateMachine --> Allocator
    Clock --> StateMachine
    Clock --> Backtest

    FastAPI --> Storage
    FastAPI --> Reports
    FastAPI --> LLMs
    FRED --> FastAPI
```

## State-Machine Runtime Flow

```mermaid
stateDiagram-v2
    [*] --> BOOT
    BOOT --> MARKET_CLOSED: market not open
    BOOT --> REGIME_CHECK: market open + reconciled
    BOOT --> ERROR: reconciliation failed
    BOOT --> HALTED_PDT: live mode + equity < $26k
    MARKET_CLOSED --> REGIME_CHECK: market opens
    REGIME_CHECK --> SCANNING: regime allows entries
    REGIME_CHECK --> RECYCLING: open positions, no new entries
    SCANNING --> ALLOCATING: candidates available
    SCANNING --> RECYCLING: no candidates
    ALLOCATING --> IN_POSITION: orders submitted/filled
    ALLOCATING --> ERROR: allocator/broker error
    IN_POSITION --> EXITING: exit trigger fires
    IN_POSITION --> SCANNING: scan interval elapsed (open positions persist)
    EXITING --> RECYCLING: exit fill received
    EXITING --> ERROR: cancel/replace exhausted
    RECYCLING --> SCANNING: slot returned to EMPTY
    SCANNING --> HALTED_RISK: kill switch tripped
    HALTED_RISK --> RECYCLING: existing exits only
    HALTED_PDT --> RECYCLING: live equity floor breach
    ERROR --> BOOT: manual reset
```

## Signal Registry

Five algorithms registered. The active one is selected via `ACTIVE_SIGNAL` env var. The same registry feeds **both** the live runtime and the backtest harness — no duplicate research math.

```mermaid
flowchart TB
    subgraph "src/driftpilot/signals/base.py — the contract"
        SignalProtocol["SignalProtocol<br/>name, version, scan(), evaluate_exit()?"]
        Candidate["Candidate<br/>symbol, score, sector, allowed, blocked_reason"]
        ExitDecision["ExitDecision<br/>should_exit, exit_reason, metadata"]
        BlockedReason["BlockedReason (StrEnum)<br/>30 reason taxonomy"]
    end

    subgraph "Five registered signals"
        IM["intraday_momentum_v1<br/>RVOL+VWAP+15m return<br/>Phase-12 verdict: FAIL"]
        SG["stationary_ghost_v1<br/>Mean reversion +<br/>ADX trend filter"]
        WT["whale_tail_v1<br/>RVOL+compression+ATR<br/>Variant B baseline"]
        RS["rs_drift_v1<br/>Strength vs SPY<br/>+ break-even trigger"]
        AH["apex_hunter_v2_2<br/>EWMLR + 3-stage<br/>Ratchet exit"]
    end

    subgraph "Consumers"
        LiveScanner["Live scanner<br/>(state machine)"]
        ReplayHarness["Backtest replay"]
    end

    SignalProtocol --> IM
    SignalProtocol --> SG
    SignalProtocol --> WT
    SignalProtocol --> RS
    SignalProtocol --> AH

    BlockedReason -.uses.-> IM
    BlockedReason -.uses.-> SG
    BlockedReason -.uses.-> WT
    BlockedReason -.uses.-> RS
    BlockedReason -.uses.-> AH

    Candidate -.emits.-> IM
    Candidate -.emits.-> SG
    Candidate -.emits.-> WT
    Candidate -.emits.-> RS
    Candidate -.emits.-> AH

    ExitDecision -.emits.-> WT
    ExitDecision -.emits.-> RS
    ExitDecision -.emits.-> AH

    LiveScanner --> SignalProtocol
    ReplayHarness --> SignalProtocol
```

### Signal-by-signal one-liner

| Signal | Thesis | Slot model | Custom exit? | Verdict |
|---|---|---|---|---|
| `intraday_momentum_v1` | RVOL>2 + above VWAP + 15m return | 10×$1k | Default T/S/T | **FAIL** (2024) |
| `stationary_ghost_v1` | 2.5σ below 15-bar mean reverts in 20 min when ADX<20 | 10×$1k | Default T/S/T | pending |
| `whale_tail_v1` | High RVOL + compression + upper-range = absorption breakout | 10×$1k | ATR-scaled + dist-trap | pending |
| `rs_drift_v1` | Strength vs SPY 9:30-10:00 drifts through midday | 5×$2k | Break-even + EOD + SPY-heat | pending |
| `apex_hunter_v2_2` | EWMLR institutional drift, ride with Ratchet stop | 10×$1k | Three-stage Ratchet | pending |

## Backtest Pipeline

```mermaid
sequenceDiagram
    participant CLI as "python -m driftpilot.backtest"
    participant Settings as "settings.py"
    participant Registry as "signals/__init__.py"
    participant Replay as "backtest/replay.py"
    participant Signal as "Signal.scan() / .evaluate_exit()"
    participant Slip as "execution/paper_fills.py"
    participant Report as "backtest/report.py"

    CLI->>Settings: load settings, --signal flag
    CLI->>Registry: get_signal(name)
    Registry-->>CLI: SignalProtocol instance
    CLI->>Replay: replay_parquet_cache(...)

    alt signal == intraday_momentum_v1
        Replay->>Replay: vectorized pandas pipeline (fast)
    else any other signal
        Replay->>Replay: per-symbol streaming<br/>+ replay_bars()
        loop every timestamp
            Replay->>Signal: signal.scan(history, quotes, spy_bars)
            Signal-->>Replay: regime + Candidate[]
            Replay->>Slip: entry_fill / exit_fill (apply slippage)
            Replay->>Signal: signal.evaluate_exit(position, latest_bar)
            Signal-->>Replay: ExitDecision
        end
    end

    Replay-->>CLI: ReplayResult (trades, equity_curve)
    CLI->>Report: build_expectancy_report(result)
    Report-->>CLI: report dict + verdict (PASS/GATED/FAIL)
    CLI->>CLI: write reports/{signal}/{date}_{verdict}.json
```

Slippage formula (constant across all signals): `max($0.02/share, 0.0005 * price)`.

## Storage Schema

```mermaid
erDiagram
    operator_state ||--|| state_transitions : "last_transition_id"
    operator_state ||--o| errors : "last_error_id"
    slots ||--o{ positions : "slot_id"
    positions ||--o{ orders : "position_id"
    orders ||--o{ fills : "order_id"
    slots ||--o{ orders : "slot_id"
    daily_counters {
        date date_et PK
        text counter_name PK
        integer counter_value
        text updated_at
    }
    candidate_queue {
        text symbol
        real score
        integer rank
        text status
        text blocked_reason
        text features_json
    }
    recycle_events {
        integer id PK
        integer slot_id
        text reason
        text timestamp
    }
    operator_state {
        integer id PK
        text current_state
        text active_gate
        integer last_transition_id FK
        integer last_error_id FK
        text updated_at
    }
    state_transitions {
        integer id PK
        text from_state
        text to_state
        text reason
        text timestamp
    }
    slots {
        integer slot_id PK
        text status
        text symbol
        integer position_id FK
        real slot_value
    }
    positions {
        integer id PK
        text symbol
        text status
        real entry_price
        real target_price
        real stop_price
        text opened_at
        text closed_at
        text exit_reason
    }
    orders {
        integer id PK
        text symbol
        text side
        text status
        real quantity
        text submitted_at
    }
    fills {
        integer id PK
        integer order_id FK
        real price
        real quantity
        text filled_at
    }
    errors {
        integer id PK
        text severity
        text message
        text raised_at
    }
```

Source: [src/driftpilot/storage/repositories.py](../src/driftpilot/storage/repositories.py).

## Deployment Topology (Mac → DGX)

```mermaid
flowchart LR
    Mac["Mac (M4, 24 GB)<br/>~/Documents/Trading BOT"]
    GitHub["GitHub<br/>karuthsanker-hub/DriftPilot"]
    DGX["DGX Spark<br/>sankerkr@192.168.1.166<br/>~/driftpilot"]

    Mac -->|"git push"| GitHub
    GitHub -->|"git clone / pull"| DGX
    Mac -->|"rsync 1.7 GB cache<br/>scripts/migrate_to_dgx.sh"| DGX
    Mac -->|"scp .env (chmod 600)"| DGX
    Mac -->|"deploy_to_dgx.sh<br/>(code-only, ~30s)"| DGX

    DGX -->|"backtest reports/<br/>via rsync back"| Mac

    Alpaca["Alpaca SIP<br/>(future runtime)"]
    Databento2["Databento<br/>(re-pull if needed)"]
    DGX -.->|"runtime"| Alpaca
    DGX -.->|"data top-up"| Databento2
```

Operational scripts live in [scripts/README.md](../scripts/README.md). Recurring deploys are one line: `bash scripts/deploy_to_dgx.sh`.

## Repository Layout (high-level)

```text
.
├── REFACTOR_PLAN.md          # authoritative spec (~1500 lines)
├── README.md                 # user-facing entry doc
├── AGENTS.md                 # rules for any future code-generation agent
├── MIGRATION.md              # legacy → DriftPilot transition notes
├── docs/
│   ├── PROJECT_OVERVIEW.md   # ← you are here
│   ├── ARCHITECTURE.md       # runtime detail
│   ├── OPERATIONS.md         # runbook
│   └── DOCS_INDEX.md         # index of every .md with status
├── scripts/
│   ├── README.md             # operational scripts catalogue
│   ├── databento_pull.py     # Databento bar puller (used by ↓)
│   ├── pull_databento_2024.sh# convenience wrapper
│   ├── migrate_to_dgx.sh     # one-time DGX bootstrap
│   └── deploy_to_dgx.sh      # recurring DGX deploys
├── src/driftpilot/           # autonomous operator runtime
│   ├── operator.py           # CLI entry
│   ├── state_machine.py
│   ├── states.py             # OperatorState + BlockedReason enums
│   ├── settings.py
│   ├── clock.py              # all timezone-aware time logic
│   ├── broker/               # Alpaca client + live gate
│   ├── market_data/          # SIP stream + bar/quote cache
│   ├── signals/              # signal registry
│   │   ├── base.py           # SignalProtocol, Candidate, ExitDecision
│   │   ├── __init__.py       # registry + register_signal
│   │   ├── intraday_momentum_v1/
│   │   ├── stationary_ghost_v1/
│   │   ├── whale_tail_v1/
│   │   ├── rs_drift_v1/
│   │   └── apex_hunter_v2/
│   ├── execution/            # SlotAllocator + paper fills
│   ├── storage/              # SQLite schema + repositories
│   ├── backtest/             # replay, metrics, report
│   └── dashboard/            # API view models
├── src/trading_bot/          # legacy/manual workflows + dashboard shell
├── tests/                    # 334 passing tests
├── config/
│   ├── universe.csv          # 1500-ish symbols + sector
│   └── sector_map.csv
├── data/                     # gitignored
│   └── bars/databento/{SYM}/{YEAR}.parquet
└── reports/                  # gitignored
    └── {signal_name}/{date}_{verdict}.json
```

## Build, Test, Deploy at a Glance

| Want to... | Run |
|---|---|
| Install deps | `uv sync --extra test` (or `pip install -e ".[test]"`) |
| Run all tests | `PYTHONPATH=src pytest -q` |
| Run a backtest | `python -m driftpilot.backtest --signal <name> --start 2024-01-01 --end 2024-12-31` |
| Pull Databento bars | `bash scripts/pull_databento_2024.sh` |
| Bootstrap DGX | `bash scripts/migrate_to_dgx.sh` |
| Deploy code change to DGX | `git push && bash scripts/deploy_to_dgx.sh` |
| List registered signals | `python -c "from driftpilot.signals import list_signals; print(list_signals())"` |
| Run synthetic operator cycle | `python -m driftpilot.operator --once --mock-stream` |
| Start dashboard | `uvicorn trading_bot.dashboard.app:app --port 8000 --reload` |

## Hard Rules (apply to all future work)

These come from [AGENTS.md](../AGENTS.md) plus per-signal locked specs:

1. **Datetimes are timezone-aware.** Naive raises `ValueError`. Time logic comes from `driftpilot.clock` only.
2. **Strategy parameters are locked** in each signal's spec/`config.py`. Do not "improve" parameters during implementation.
3. **`relative_volume` MUST exclude the current bar** from the lookback average — lookahead-bias unit tests pin this.
4. **ADX = Wilder 1978** formula. Each signal's `KNOWN_RISKS.md` carries a TradingView-cross-check pending note.
5. **Slippage formula is constant**: `max($0.02, 0.0005 * price)`. Same in paper, live, and backtest.
6. **No silent exception handlers.** Every `except` re-raises, logs, or has a comment justifying suppression.
7. **No new dependencies** without a one-line justification in `pyproject.toml`.
8. **Live mode is blocked** until the four-criterion live deploy gate passes.
9. **Do not modify `src/trading_bot/`** except the dashboard shell. New trading code goes in `src/driftpilot/`.
10. **Same signal code in live and backtest.** No separate research math.

## When You Return to This Codebase

Read in this order:

1. This file — orientation.
2. [docs/DOCS_INDEX.md](DOCS_INDEX.md) — what every other doc says, current vs historical.
3. [REFACTOR_PLAN.md](../REFACTOR_PLAN.md) — authoritative spec, especially the "Resolved Decisions" section.
4. The signal you're touching: `src/driftpilot/signals/<name>/README.md` and `KNOWN_RISKS.md`.

If you only have time for one diagram, the **State-Machine Runtime Flow** above is the most load-bearing — it defines what the operator *is*.
