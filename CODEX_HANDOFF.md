# Codex Handoff — DriftPilot Project State

**Date:** 2026-05-12  
**Branch:** `main` at `327c0b5` (`origin/main` is `4f8ba49`; local branch ahead 16 commits)  
**Latest commits:** `327c0b5` Qwen v2 integrated; `3c454a8` operator wiring; `d4809f7` Wave 4 dashboard/exporter; `f03c342` Wave 3; `f31bb38` Wave 2  
**Paper trading:** Day 2 complete; Day 3 is the first clean session with all bug fixes baked in

## Current Snapshot

- **Working tree:** CLEAN — all Codex Qwen v2 + agent layer work committed. Only `.claude/` and `logs/` untracked (runtime artifacts).
- **Last known full test gate:** `PYTHONPATH=src uv run --extra test pytest -q` passed: `1025 passed, 1 warning in 8.60s`.
- **Agent layer gate:** `PYTHONPATH=src uv run --extra test pytest tests/agents/ -q` passed: `136 passed in 2.53s`. Agent ruff/mypy clean.
- **Qwen v2 targeted gate:** `PYTHONPATH=src uv run --extra test pytest tests/catalyst/test_context_assembler.py tests/catalyst/test_headline_parser.py tests/catalyst/test_qwen_enricher.py tests/catalyst/test_qwen_enricher_v2.py tests/catalyst/test_db_idempotent.py tests/test_dashboard_catalyst_detail.py tests/test_enrichment_pipeline_integration.py tests/backtest/test_catalyst_replay.py -q` passed: `110 passed`.
- **Repo-wide static checks:** `uvx ruff check src/driftpilot src/trading_bot/dashboard scripts tests` passed; `PYTHONPATH=src uv run --with mypy mypy src/driftpilot src/trading_bot/dashboard` passed with two informational notes about unchecked untyped function bodies in `services_live.py`.
- **Instruction update:** `.codex/instructions.md` now contains a cross-agent resume protocol and handoff template. Keep this file and this handoff in sync whenever context is running low.
- **Next agent first command:** `git status --short --branch && git log --oneline --decorate -5`

If you inherit this while another agent is still testing, wait for that result,
then update this snapshot with the exact pass/fail output and remaining files.

---

## What DriftPilot Is

A continuous autonomous intraday paper-trading operator. One async state-machine loop: streams Alpaca SIP bars, scans a stock universe through pluggable signal algorithms, allocates ranked candidates into fixed $1k paper-trading slots, exits on signal-specific rules, recycles freed slots, persists every state transition to SQLite. The dashboard explains *why* it is or isn't trading.

Live trading is blocked by default until a four-criterion live deploy gate passes (12-month backtest positive, 60 paper-days positive + Sharpe > 1.0, equity floor, `LIVE_OK=true`).

---

## Current state of the system

### What works end-to-end

1. **Operator loop** (`python -m driftpilot.operator --paper-live`): boots, reconciles with Alpaca, scans for catalyst events, allocates slots, submits real paper orders to Alpaca, monitors positions, exits on profit_take/stop_loss/trailing_stop/time_stop, recycles slots.
2. **Catalyst event pipeline**: Alpaca News API → regex classifier → Qwen3-8B sentiment enrichment (on DGX) → event bus → signal subscription.
3. **Catalyst signals**: `earnings_report_v1` (GATED, edge_ratio 1.105), `filing_8a_v1` (new broader-flow catalyst signal), and `analyst_target_raise_v1` (FAIL, for observation only).
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

### 3. Qwen Enrichment v2 — Pre-enrichment context pipeline (BUILT, needs DB re-enrichment run)

Full requirements + agent breakdown at `docs/QWEN_ENRICHMENT_V2.md`. The current Qwen prompt produces a 3-bucket classifier (98% of positives get the same +0.15 score). Edge ratio collapsed from 1.6 to 1.0 because marginal "positive" events dilute the signal. Fix: assemble company context (market cap, beat %, earnings history, ATR, VIX) before calling Qwen so the LLM can distinguish a $0.01 beat on a $3B company from a 6.5% beat on a biotech. Dashboard gets a catalyst detail panel showing the full enrichment context + auto-generated warning flags.

**5 agents:** Headline Parser → Context Assembler → Prompt v2 + Enricher → Dashboard Detail Panel → Batch Re-enrichment + Validation. Agent 2 (parser) and Agent 4 (dashboard) can start in parallel. Full spec with test requirements, review checklist, and merge order in the doc.

**2026-05-11 progress:** Headline Parser slice has been implemented and committed:
- New file: `src/driftpilot/catalyst/headline_parser.py`
- New tests: `tests/catalyst/test_headline_parser.py`
- Behavior: extracts EPS actual/estimate/beat %, revenue actual/estimate/beat % in millions, guidance direction (`up`, `down`, `maintained`), and mixed beat-plus-lowered-guidance signals.
- Review agent findings addressed: documented malformed numeric suppression and added a hardcoded corpus of 32 real 2024 catalyst DB headlines plus real guidance headlines.
- Gates at parser merge: parser tests `69 passed`; full pytest `870 passed, 1 warning`; parser ruff and parser mypy clean.

**2026-05-11 progress update:** Phases A/C/D/E are now implemented but not committed:
- Phase A Context Assembler: `src/driftpilot/catalyst/context_assembler.py`, with cached run/symbol context, ATR from Databento parquet, sector lookup, VIX/SPY/sector ETF hooks, headline cluster count, and prompt-block formatting.
- Phase C Prompt v2 + persistence: `QwenEnricher.enrich(..., context=...)`, v2 system/user prompt, `enrich_with_response()`, schema migration for `confidence`, `context_json`, `qwen_response_json`, and enrichment script `--force-re-enrich` / `--dry-run`.
- Phase D Dashboard detail: `_news_ticker()` v2 fields/backward compatibility, `_catalyst_detail()`, `/api/catalyst/event/{id}`, clickable ticker modal with context/Qwen/flags.
- Phase E Batch + validation: enrichment pipeline integration tests, backtest `--min-confidence` and `--min-priority-modifier` filters.
- Full DB re-enrichment has **not** been run. Before running all 23,888 events, back up `data/driftpilot/catalyst_events_2024.sqlite3` and use the dry-run/smoke-run protocol from `docs/QWEN_ENRICHMENT_V2.md`.

### 4. Agentic Trader — Multi-agent position management (THE PRODUCT)

Full implementation spec at `docs/AGENTIC_TRADER_REQUIREMENTS.md`. Vision/architecture at `docs/AGENTIC_TRADER_VISION.md`.

**Architecture:** 3-type multi-agent topology — PM Agent (1) + Scanner Agent (1) + Slot Agents (10). Authority hierarchy: Mechanical Guardrails > Algorithm > LLM Agent. Quant signals (signal.scan(), signal.evaluate_exit()) run FIRST as primary decision-makers; LLM agents provide override layer requiring PM approval.

**Key design decisions:**
- A2A message bus (SQLite-backed, 12 message types)
- All prompts configurable via `config/prompts/*.yaml` (hot-reloadable)
- Guardrails NEVER overridable: 1.5% stop, 5% cap, 60min time stop, 3% daily loss
- Override rate limited to 20%, auto-disable if exceeded
- Every decision logged with prompt + response + outcome for fine-tuning

**Build plan:** 4 waves — **ALL 4 WAVES COMPLETE** + operator wiring:
- ✅ Wave 1: Message Bus + Guardrail Engine + LLM Client + Prompt Loader (60 tests)
- ✅ Wave 2: PM Agent + Scanner Agent + Slot Agent (28 tests)
- ✅ Wave 3: Orchestrator + lifecycle management (16 tests)
- ✅ Wave 4: Dashboard views + training data exporter (27 tests)
- ✅ Operator wiring: factory + settings + operator boot/stop (5 tests)

**Commits:**
- `ebc5777`: Wave 1 — models.py, message_bus.py, guardrail_validator.py, llm_client.py, prompt_loader.py, 4 YAML prompts, migration 006
- `f31bb38`: Wave 2 — pm_agent.py, scanner_agent.py, slot_agent.py
- `f03c342`: Wave 3 — orchestrator.py
- `d4809f7`: Wave 4 — training_exporter.py, agent_views.py, agents.html, app.py endpoints, settings.py AGENT_* vars
- `3c454a8`: Operator wiring — factory.py, operator.py start/stop, test_factory.py

**Test counts:** 136 agent tests across 10 test files. 1025 project total. All gates pass.
- Agent is disabled by default (`AGENT_ENABLED=false`). Set to true + configure AGENT_QWEN_URL to activate.
- Dashboard at `/agents` shows live agent states, override rate gauge, decision feed, message bus activity.
- Training data export via `TrainingExporter(db_path).export_jsonl(output)` with filters.

**Remaining work:**
- Integration test: replay a day with agents enabled vs disabled, compare edge ratio
- Wire `tick_pm`/`tick_scanner`/`tick_slot` into state machine scan/monitor cycles (orchestrator starts/stops but ticks are not yet called from the state machine loop)

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

Signals are selected via `ACTIVE_SIGNAL` / runtime config. Multi-signal mode can
run comma-separated catalyst signals in parallel, e.g.
`earnings_report_v1,filing_8a_v1`.

| Signal | Type | Verdict | Notes |
|--------|------|---------|-------|
| `earnings_report_v1` | Catalyst | **GATED** (1.137 Oct-Nov, 1.007 Jul-Dec) | Edge collapsed after Qwen re-enrichment. Needs v2 prompt with context. |
| `filing_8a_v1` | Catalyst | **FAIL** (0.816 positive, 0.812 unfiltered) | No edge with current enrichment. Needs v2 context pipeline. |
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
| `docs/QWEN_ENRICHMENT_V2.md` | Enrichment v2 context pipeline + agents | CURRENT |
| `docs/AGENTIC_TRADER_VISION.md` | LLM trading agent — the product vision | CURRENT |
| `docs/PORTFOLIO_CONTROLLER_DESIGN.md` | Portfolio controller (superseded by Agentic Trader) | SUPERSEDED |

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
