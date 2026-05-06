# Codex Handoff — DriftPilot Project State

**Date:** 2026-05-06  
**Branch:** `main` at `44f186c`  
**Test count:** 511 passing (109 test files)  
**Paper trading:** Day 2 complete, Day 3 is the first clean session with all bug fixes baked in

---

## What DriftPilot Is

A continuous autonomous intraday paper-trading operator. One async state-machine loop: streams Alpaca SIP bars, scans a stock universe through pluggable signal algorithms, allocates ranked candidates into fixed $1k paper-trading slots, exits on signal-specific rules, recycles freed slots, persists every state transition to SQLite. The dashboard explains *why* it is or isn't trading.

Live trading is blocked by default until a four-criterion live deploy gate passes (12-month backtest positive, 60 paper-days positive + Sharpe > 1.0, equity floor, `LIVE_OK=true`).

---

## Current state of the system

### What works end-to-end

1. **Operator loop** (`python -m driftpilot.operator --paper-live`): boots, reconciles with Alpaca, scans for catalyst events, allocates slots, submits real paper orders to Alpaca, monitors positions, exits on profit_take/stop_loss/trailing_stop/time_stop, recycles slots.
2. **Catalyst event pipeline**: Alpaca News API → regex classifier → Qwen3-8B sentiment enrichment (on DGX) → event bus → signal subscription.
3. **Two catalyst signals**: `earnings_report_v1` (GATED, edge_ratio 1.105) and `analyst_target_raise_v1` (FAIL, for observation only).
4. **Five technical signals**: all FAIL on the raw 1500-symbol universe. Architecture exists for them to be re-tested on catalyst-filtered universe (v3 retrofit — not yet done).
5. **Backtest harness**: replay Databento Parquet bars through the same signal code used in live. Full 2024 year backtested for all 7 signals.
6. **Dashboard**: FastAPI with Operator/Admin/Backtest/LLM tabs. Shows live Alpaca equity, slots, candidate queue, P&L, admin tunables.
7. **DGX deployment**: `scripts/deploy_to_dgx.sh` for code, `scripts/migrate_to_dgx.sh` for initial bootstrap.

### Paper trading results so far

| Day | Date | P&L | Trades | Notes |
|-----|------|-----|--------|-------|
| 1 | 2026-05-04 | +$46.24 | 6 | First live day, bugs surfaced but lucky outcomes |
| 2 | 2026-05-05 | −$1,047.57 | 18 | Bug-discovery day — 3 bugs found & fixed live. SPHR re-bought 4× on same headline |

Day 2 lost money because bugs were being fixed mid-session. All 6 fixes shipped by 11:35 ET. Day 3 onward is the first clean session.

### Bugs fixed (already shipped)

| Bug | Fix commit | Description |
|-----|-----------|-------------|
| Slot leak across days | `d7f8be3` | Slots weren't freed when positions closed |
| Broker call hangs | `62c6c0e` | No timeouts on Alpaca calls — operator hung 15 min |
| Sequential exits | `48a7529` | Monitor processed positions one-by-one (~4.5 min); now parallel (~5s) |
| `unrealized_pct=0` | `e23b077` | Signal always saw 0% gain/loss — profit_take/stop_loss never fired |
| Per-symbol cap | `9f1eed0` | Only checked open positions, not closed-today. Same symbol re-bought 4× |
| Trailing stop metadata | `88fded5` | `peak_unrealized_pct` read from position metadata for trailing stop |

---

## Open bugs (not yet fixed)

These are filed in the Day 2 report (`reports/PAPER_DAY_2026-05-05.md`) and need to be addressed:

### Bug #11 — Bootstrap-on-enrich (HIGH PRIORITY)

**Problem:** Signal's `_active_events` dict doesn't refresh from the catalyst DB after operator startup. Late-enriched events (Qwen finishes enrichment after operator boots) never get traded.

**Expected behavior:** When a new catalyst event is enriched with sentiment, the signal should pick it up on the next scan cycle without requiring an operator restart.

**Where to look:** `src/driftpilot/signals/earnings_report_v1/signal.py` — the `_active_events` cache initialization. The catalyst discovery service (`src/driftpilot/catalyst/discovery_service.py`) may need a periodic refresh or the signal needs to re-query the DB each scan cycle.

### Bug #3 — Real-fill PnL (MEDIUM)

**Problem:** Local realized P&L uses computed mid-price, not Alpaca's actual `filled_avg_price`. The Alpaca dashboard shows different P&L than the operator's internal tracking.

**Where to look:** `src/driftpilot/execution/slot_allocator.py` and `src/driftpilot/services_live.py` — wherever fills are recorded. The broker client (`src/driftpilot/broker/alpaca_client.py`) returns fill data; it needs to flow through to position P&L.

### Bug #4 — Wide-spread quote filter (MEDIUM)

**Problem:** No filter for illiquid names with wide bid-ask spreads. The operator can enter a position where slippage on exit wipes out any potential profit.

**Expected behavior:** Before submitting an entry order, check that the bid-ask spread is below a threshold (e.g., 0.5% of mid-price). Reject with `BlockedReason.WIDE_SPREAD` if not.

**Where to look:** `src/driftpilot/execution/slot_allocator.py` (entry gate), `src/driftpilot/signals/base.py` (add `WIDE_SPREAD` to `BlockedReason` enum if not already there).

### Bug #5 — Classifier validation (LOW)

**Problem:** Q1-2026 phrases ("Beats $X Estimate") were added to the regex classifier live during paper trading. Should run a fresh spike on 2024 data to ensure the validated edge ratio still applies with the loosened classifier.

**Where to look:** `src/driftpilot/catalyst/classifier.py`, `scripts/run_catalyst_signal_backtest.py`

---

## What to build next (priority order)

### 1. Fix open bugs (#11, #3, #4)

Bug #11 (bootstrap-on-enrich) is the highest priority — it means the operator misses trades that arrive after boot. Fix this before the next paper trading session.

### 2. Run Day 3+ paper trading and collect clean data

After bug fixes, the system needs 2-3 weeks of clean paper trading data to validate the earnings_report_v1 signal in production. The backtest showed edge_ratio=1.105 over 185 trades (Jul-Dec 2024). Paper trading validates this on live data.

### 3. Portfolio controller layer (DEFERRED — needs paper evidence)

Design doc exists at `docs/PORTFOLIO_CONTROLLER_DESIGN.md`. This is the "fund manager" layer above the slots — score candidates, allocate, override exits, halt. Two implementations planned: rule-based (deterministic) and LLM-driven (Qwen). **Do not build until 2-3 weeks of paper trading data exists.**

### 4. V3 retrofit backtests (technical signals on catalyst-filtered universe)

The 4 technical signals (whale_tail, apex_hunter, rs_drift, stationary_ghost) all FAIL on the raw 1500-symbol universe. The v3 catalyst layer can filter the universe to only catalyst-bearing stocks. Re-run backtests on filtered universe to see if edge appears. Predictions in `reports/COMPARISON.md`:
- Whale-Tail benefits most (directional follow-through + catalyst events)
- Apex Hunter second (EWMLR acceleration meaningful on catalyst stocks)
- RS-Drift least likely (slow daily horizon vs 60-240m catalyst windows)

### 5. Target-raise v3.1 — surprise-vs-consensus filter

`analyst_target_raise_v1` FAIL because 82% of target-raise headlines are already positive (consensus). The v3.1 hypothesis: use surprise-vs-consensus instead of sentiment polarity as the filter. Not yet designed.

---

## Architecture summary

### State machine flow

```
BOOT → REGIME_CHECK → SCANNING → ALLOCATING → IN_POSITION → EXITING → RECYCLING → SCANNING
                                                                                      ↑
MARKET_CLOSED ←──────────────────────────────────────────────────────────────── (market closes)
ERROR ← (any failure) → BOOT (manual reset)
HALTED_RISK ← (kill switch) → RECYCLING (exits only)
```

### Signal registry

7 signals registered, selected via `ACTIVE_SIGNAL` env var:

| Signal | Type | Verdict | Notes |
|--------|------|---------|-------|
| `earnings_report_v1` | Catalyst | **GATED** (1.105) | Active paper trading signal. Positive sentiment gate required. |
| `analyst_target_raise_v1` | Catalyst | FAIL (0.85) | Subscribed for observation only |
| `intraday_momentum_v1` | Technical | FAIL | Reference signal, Phase 12 |
| `whale_tail_v1` | Technical | FAIL (0.754) | Best technical signal candidate for v3 retrofit |
| `stationary_ghost_v1` | Technical | FAIL (0.763) | Mean-reversion |
| `apex_hunter_v2_2` | Technical | FAIL (0.527) | 66% HARD_EXIT in 5 min — entry too loose |
| `rs_drift_v1` | Technical | FAIL (0.597) | RS vs SPY drift |

### Storage

SQLite at `data/driftpilot/operator_state.sqlite3`. Tables: `operator_state`, `state_transitions`, `slots`, `positions`, `orders`, `fills`, `daily_counters`, `candidate_queue`, `recycle_events`, `errors`. Schema defined in `src/driftpilot/storage/repositories.py`.

### Catalyst pipeline

```
Alpaca News API → CatalystFeed (polling) → Classifier (regex) → EventBus
    → Qwen3-8B enrichment (DGX, async) → CatalystDB (SQLite)
    → DiscoveryService → Signal subscription (earnings_report_v1 subscribes to earnings/report)
```

### External dependencies

| Service | Purpose | Config |
|---------|---------|--------|
| Alpaca Paper API | Broker + market data + news | `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` in `.env` |
| Qwen3-8B on DGX | Sentiment enrichment | `http://192.168.1.166:8000/v1` (vllm) |
| Databento | Historical bars for backtest | `DATABENTO_API_KEY` in `.env`, cached to Parquet |
| FRED API | Macro data (dashboard only) | `FRED_API_KEY` in `.env` |

---

## Key contracts (do not break)

1. **SignalProtocol** (`src/driftpilot/signals/base.py`): `scan()` returns `list[Candidate]`, optional `evaluate_exit()` returns `ExitDecision`. All signals implement this.
2. **SlotAllocator** (`src/driftpilot/execution/slot_allocator.py`): manages 10 fixed slots, enforces per-symbol day cap, sector cap, daily loss limit.
3. **DriftPilotRepository** (`src/driftpilot/storage/repositories.py`): all SQLite access goes through this. No raw SQL elsewhere.
4. **AlpacaClient** (`src/driftpilot/broker/alpaca_client.py`): abstracts paper vs live. Live gate checks are here.
5. **BlockedReason** (`src/driftpilot/signals/base.py` or `states.py`): 30-reason taxonomy for why a candidate was rejected. Dashboard displays these.

---

## Test structure

```
tests/
  backtest/       # Replay harness, metrics, report generation
  catalyst/       # Event bus, classifier, discovery, enrichment
  signals/        # Per-signal unit tests (features, exits, scan)
  (root)          # Allocator, broker, state machine, settings, storage, dashboard
```

Run all: `PYTHONPATH=src pytest -q`
Run one: `PYTHONPATH=src pytest tests/catalyst/ -q`

---

## Operational commands cheat sheet

```bash
# Install
uv sync --extra test

# Tests
PYTHONPATH=src pytest -q

# Lint
uvx ruff check src/driftpilot src/trading_bot/dashboard tests

# Type check
PYTHONPATH=src uv run --with mypy mypy src/driftpilot src/trading_bot/dashboard

# Smoke test (no credentials)
PYTHONPATH=src python -m driftpilot.operator --once --mock-stream

# Paper trading
CATALYST_ENABLED=true ACTIVE_SIGNAL=earnings_report_v1 \
  python -m driftpilot.operator --paper-live

# Observer (read-only, no orders)
CATALYST_ENABLED=true python -m driftpilot.observer --print-every-s 30

# Dashboard
PYTHONPATH=src uvicorn trading_bot.dashboard.app:app --port 8000 --reload

# Backtest a signal
PYTHONPATH=src python -m driftpilot.backtest --signal earnings_report_v1 \
  --start 2024-07-01 --end 2024-12-31

# Analyze a paper trading day
python scripts/analyze_paper_trading_day.py --include-alpaca-snapshot

# Deploy to DGX
bash scripts/deploy_to_dgx.sh

# Enrich catalyst events with Qwen
python scripts/enrich_catalyst_events.py --priority-only --concurrency 32
```

---

## Doc index (what to read and when)

| Doc | When to read | Status |
|-----|-------------|--------|
| `CODEX_HANDOFF.md` | First thing | CURRENT (you're here) |
| `docs/PROJECT_OVERVIEW.md` | Architecture orientation | CURRENT |
| `AGENTS.md` | Before writing any code | CURRENT |
| `REFACTOR_PLAN.md` | When in doubt about a decision | CURRENT (authoritative) |
| `docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md` | Working on catalyst signals | CURRENT |
| `docs/RUNBOOK_LIVE_PAPER.md` | Running paper trading | CURRENT |
| `reports/COMPARISON.md` | Understanding backtest results | CURRENT |
| `reports/STATUS.md` | Checking backtest verdicts | CURRENT |
| `reports/PAPER_DAY_2026-05-05.md` | Understanding Day 2 bugs | CURRENT |
| `docs/ARCHITECTURE.md` | Deep runtime detail | CURRENT |
| `docs/OPERATIONS.md` | Running services locally | CURRENT |
| `docs/PORTFOLIO_CONTROLLER_DESIGN.md` | Portfolio controller (DEFERRED) | DESIGN-ONLY |

---

## Risk envelope (paper account)

- Account: Alpaca paper at `https://paper-api.alpaca.markets`
- Equity: ~$99k (started at $100k, Day 1 +$46, Day 2 −$1,048)
- Slots: 10 × $1,000 = $10k max notional exposure
- Per-trade: catalyst event drives entry; profit_take=1.0%, stop_loss=1.5%, max_hold=60min
- Trailing stop: peak − 2% (activates after +0.5%)
- Per-symbol day cap: 1 (was 3, tightened after SPHR incident)
- Daily loss limit: 3% of equity
- `target_cut` on a held name → `EMERGENCY_FLUSH` → market-exit
