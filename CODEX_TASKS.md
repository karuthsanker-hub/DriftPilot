# DriftPilot Codex Task List

> Small, self-contained tasks for Codex agents. Each task should be
> completable in one session. Tasks are ordered by priority.

## Repo Layout

```
src/driftpilot/           # Core operator, signals, agents
src/trading_bot/dashboard/ # FastAPI dashboard + Jinja2 templates (OK to edit)
dgx/                      # Brain server (deploys to DGX Spark)
docs/                     # Design docs
data/driftpilot/          # Runtime data (SQLite DBs, pipeline_log.json)
```

**IMPORTANT:** `AGENTS.md` forbids editing `src/trading_bot/` EXCEPT
`src/trading_bot/dashboard/` which is the active dashboard. Dashboard
tasks (5, 7) target that exception and are safe to work on.

## How to Run Locally

```bash
cd "Trading BOT"
source .venv/bin/activate

# Operator (main trading loop)
python -m driftpilot.services_live

# Dashboard (port 8501)
python -m uvicorn trading_bot.dashboard.app:app --host 0.0.0.0 --port 8501

# Brain server on DGX Spark (port 8077)
cd dgx && bash start_brain.sh
```

## Testing

```bash
python -m pytest tests/ -x -q
```

---

## TASK 1: Fix ATR data flow into candidate features  ✅ DONE

**Priority: HIGH** | **Files:** `src/driftpilot/catalyst/context_assembler.py`, `src/driftpilot/services_live.py`

**Problem:** Pipeline shows ATR = 0.1% for all candidates. The `analyst_target_raise_v1` signal doesn't have proper ATR data flowing through. The `compute_dynamic_bands()` function falls back to `default(1.2%)` for most stocks.

**What to do:**
1. In `context_assembler.py`, the `EnrichmentContext` has an `atr_pct` field but it's not being computed. Add ATR calculation using yfinance historical data:
   - Fetch 20-day daily bars via `yf.Ticker(symbol).history(period="1mo")`
   - Compute ATR: `mean(max(high-low, abs(high-prev_close), abs(low-prev_close)))` over 14 periods
   - Store as `atr_pct = (atr / current_price) * 100`
2. Verify `atr_pct` propagates through `AllocationCandidate.metadata` into `compute_dynamic_bands()`
3. Add a test in `tests/` that verifies ATR calculation for a known stock

**Acceptance:** Pipeline dashboard shows ATR values > 0.1% and varying per stock. Band reasoning shows `ATR(X.X%)` not `default(1.2%)`.

---

## TASK 2: Fix null beta values in candidate features  ✅ DONE

**Priority: HIGH** | **Files:** `src/driftpilot/catalyst/context_assembler.py`, `src/driftpilot/services_live.py`

**Problem:** Pipeline shows beta = `-` (null) for all candidates. Beta is fetched in `context_assembler.py` via `yf.Ticker(symbol).info.get("beta")` but isn't flowing into the signal's candidate features when candidates are built in `services_live.py`.

**What to do:**
1. In `services_live.py`, find where `AllocationCandidate` is constructed from signal candidates
2. The enrichment context's `beta` field needs to be passed into `candidate.metadata["beta"]`
3. Trace the data flow: `context_assembler` enriches -> signal emits candidates -> `services_live.py` builds `AllocationCandidate` -> metadata dict -> `compute_dynamic_bands()`
4. Make sure `metadata.get("beta")` is populated when the candidate reaches `compute_dynamic_bands()`

**Acceptance:** Pipeline dashboard shows beta values (e.g., 1.2, 0.8) for most stocks. Band reasoning includes `beta_profile=high_beta` or `beta_profile=low_beta` where appropriate.

---

## TASK 3: Wire BrainClient into PM Agent  ✅ DONE

**Priority: MEDIUM** | **Files:** `src/driftpilot/agents/pm_agent.py`, `src/driftpilot/agents/brain_client.py`

**Problem:** `BrainClient` exists at `src/driftpilot/agents/brain_client.py` but is not imported or used by `pm_agent.py`. The PM agent makes decisions without consulting the brain's past experience.

**What to do:**
1. In `pm_agent.py`, import `BrainClient` and `BrainQueryResult`
2. Initialize `BrainClient()` in the agent's `__init__` (graceful — if brain is down, it returns empty results)
3. Before the agent makes an entry/exit decision, call `brain_client.query(symbol, context_text)` to get similar past experiences
4. Inject the brain's response into the LLM prompt as context: "Past similar trades: ..." with outcome data
5. After a trade closes, call `brain_client.store_experience(...)` with the trade outcome

**Key constraint:** Brain is optional. If `brain_client.query()` returns `is_fallback=True`, skip the brain context injection. The system must work identically when brain is offline.

**Acceptance:** PM agent logs show `brain_query_ok` when brain is running. Trade decisions include brain context in the LLM prompt. System works normally when brain is offline.

---

## TASK 4: Add EOD reflection trigger  ✅ DONE

**Priority: MEDIUM** | **Files:** `src/driftpilot/services_live.py`, `dgx/brain_server.py`

**Problem:** The brain server has a `/brain/reflect` endpoint that analyzes the day's trades and extracts skills, but nothing triggers it at end of day.

**What to do:**
1. In `services_live.py`, add a method `_trigger_eod_reflection()` on `CatalystScannerService`
2. Call it when market closes (after 4:00 PM ET) or when the operator transitions to `MARKET_CLOSED` state
3. It should:
   - Collect today's closed trades from the positions table
   - POST to `brain_server /brain/reflect` with today's date
   - Log the reflection result (skills created, patterns found)
4. Make it async and non-blocking — reflection failure should not affect the operator
5. Add a guard so it only runs once per day

**Acceptance:** After market close, brain server logs show reflection was triggered. New skills appear in brain DB. Reflection runs exactly once per trading day.

---

## TASK 5: Build /brain dashboard page  🔓 UNBLOCKED

**Priority: LOW** | **Files:** `src/trading_bot/dashboard/app.py`, `src/trading_bot/dashboard/templates/brain.html`

> **Note:** `AGENTS.md` has been updated to allow edits to
> `src/trading_bot/dashboard/`. This task is safe to work on.

**Problem:** No UI to see what the brain has learned. Skills, experiences, and reflections are only in the brain's SQLite DB.

**What to do:**
1. Add `/api/brain/status` endpoint in `app.py` that proxies to brain server's `/brain/stats` and `/brain/skills`
2. Create `templates/brain.html` with sections:
   - **Stats card:** Total experiences, active skills, reflections count, last reflection date
   - **Active Skills table:** skill_id, description, applies_to, confidence, evidence_count, created_at
   - **Recent Experiences:** Last 20 trades stored in brain, with outcome if backfilled
   - **Reflection History:** Date, summary, skills created/retired
3. Add nav link "Brain" to all template nav bars (pipeline.html, index.html, etc.)
4. Style consistent with existing dashboard (dark theme, same CSS variables)

**Design reference:** Look at `pipeline.html` for the styling pattern — use the same CSS classes (`.section`, `.stat-card`, `.tag`, `.mono`, etc.)

**Acceptance:** `/brain` page renders with live data from brain server. Shows graceful "Brain offline" message when brain is unreachable.

---

## TASK 6: Add volume_spike_v1 signal to operator scan loop  ✅ DONE

**Priority: MEDIUM** | **Files:** `src/driftpilot/services_live.py`, `src/driftpilot/signals/volume_spike_v1/signal.py`

**Problem:** `VolumeSpikeV1Signal` exists but may not be registered in the operator's scan loop. The operator currently only runs `analyst_target_raise_v1` and catalyst signals.

**What to do:**
1. Check if `VolumeSpikeV1Signal` is instantiated in `services_live.py`
2. If not, add it as a second signal source alongside the catalyst scanner
3. In the scan loop, run both signals and merge candidates (deduplicate by symbol, take highest score)
4. Volume spike candidates need `signal_name: "volume_spike"` in metadata so the pipeline dashboard can tag them
5. Ensure dynamic bands work for volume spike candidates (they have `rvol` in features but may lack `atr_pct`)

**Acceptance:** Pipeline dashboard shows both `analyst_target_raise` and `volume_spike` tagged candidates. Volume spike candidates have RVOL values displayed.

---

## TASK 7: Add position P&L tracking to pipeline dashboard  🔓 UNBLOCKED

**Priority: LOW** | **Files:** `src/trading_bot/dashboard/app.py`, `src/trading_bot/dashboard/templates/pipeline.html`

> **Note:** `AGENTS.md` has been updated to allow edits to
> `src/trading_bot/dashboard/`. This task is safe to work on.

**Problem:** Open positions table shows entry/stop/target but not current P&L or unrealized gain/loss.

**What to do:**
1. In the `/api/operator/pipeline` endpoint, fetch current prices from Alpaca for open position symbols
2. Add `current_price`, `unrealized_pnl`, `unrealized_pct` to each position in the response
3. In `pipeline.html`, add columns to the Open Positions table:
   - Current price
   - Unrealized P&L ($) with green/red coloring
   - Unrealized % with green/red coloring
   - Time held (from `opened_at` to now)
4. Add a "total unrealized P&L" stat card in the summary bar

**Acceptance:** Open positions show live P&L with color coding. Total unrealized P&L appears in summary stats.

---

## TASK 8: Add tests for compute_dynamic_bands()  ✅ DONE

**Priority: MEDIUM** | **Files:** `tests/test_dynamic_bands.py` (new)

**Problem:** `compute_dynamic_bands()` in `services_live.py` has no unit tests. It's a critical function that determines entry/exit prices.

**What to do:**
1. Create `tests/test_dynamic_bands.py`
2. Import `compute_dynamic_bands` and `DynamicBands` from `services_live.py` (may need to make them importable — extract to a module if needed)
3. Test cases:
   - Default bands when ATR is missing (should use 1.2% default)
   - ATR-based bands: ATR=2% stock should get ~3% stop, ~5% target
   - Drift tax: stock that drifted 3% should have reduced target
   - RVOL conviction boost: RVOL=3x should widen target
   - Beta profile: high beta (>1.5) should have wider bands
   - Catalyst profile: earnings should have wider bands than analyst
   - Time-of-day profile: opening should have wider stops
   - Guardrail clamping: ensure stop never exceeds MAX_STOP_LOSS_PCT (3%)
   - Spread cost deduction
4. Each test should verify both the numeric band values and the reasoning string

**Acceptance:** All tests pass. Coverage for all band adjustment paths.

---

## TASK 9: DGX Brain server — pgvector backend  ✅ DONE & DEPLOYED

**Priority: LOW** | **Files:** `dgx/brain_db_pgvector.py`, `dgx/brain_server.py`, `dgx/start_brain.sh`

PostgreSQL 16 + pgvector running on DGX Spark. `PgVectorBrainDB` class
implemented with identical interface to ChromaDB `BrainDB`. All 12 tests
pass on both backends. Brain server now defaults to pgvector.
`BRAIN_DB_BACKEND=chroma` falls back to ChromaDB for local dev.

---

## TASK 10: Add sector data to candidate features  ✅ DONE

**Priority: LOW** | **Files:** `src/driftpilot/catalyst/context_assembler.py`, `src/driftpilot/services_live.py`

**Problem:** Pipeline dashboard shows "Unknown" for most candidate sectors. The sector data from yfinance isn't flowing through to candidates.

**What to do:**
1. In `context_assembler.py`, ensure `sector` is fetched from `yf.Ticker(symbol).info.get("sector")`
2. Store sector in the enrichment context
3. Pass sector through to `AllocationCandidate.sector` field
4. Also populate the `sector_map` table in operator_state.sqlite3 for guardrail sector-cap checks

**Acceptance:** Pipeline dashboard shows real sector names (Technology, Healthcare, etc.) instead of "Unknown". Guardrail sector cap (MAX_PER_SECTOR=3) works correctly.

---

## Summary

| Task | Description | Status |
|------|-------------|--------|
| 1 | ATR data flow | ✅ Done |
| 2 | Beta values | ✅ Done |
| 3 | BrainClient in PM Agent | ✅ Done |
| 4 | EOD reflection trigger | ✅ Done |
| 5 | /brain dashboard page | 🔓 Unblocked — ready for Codex |
| 6 | Volume spike signal | ✅ Done |
| 7 | P&L tracking dashboard | 🔓 Unblocked — ready for Codex |
| 8 | Dynamic bands tests | ✅ Done (39 tests) |
| 9 | pgvector migration | ✅ Done & deployed |
| 10 | Sector data | ✅ Done |

---

## Deployment Notes

### Local (MacBook)
- Python 3.14 in `.venv`
- Operator + Dashboard run locally
- Alpaca paper trading API (keys in `.env`)
- Qwen LLM on DGX Spark at `http://192.168.x.x:8000`

### DGX Spark
- Brain server runs on DGX Spark (port 8100)
- PostgreSQL 16 + pgvector on port 5432 (auto-starts on boot)
- Requires: psycopg, pgvector, sentence-transformers, FastAPI
- Start: `cd ~/brain && bash start_brain.sh`
- Health check: `curl http://<dgx-ip>:8100/brain/health`
- Backend defaults to pgvector; set `BRAIN_DB_BACKEND=chroma` for ChromaDB fallback

### Environment Variables
```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets
QWEN_URL=http://<dgx-ip>:8000
BRAIN_URL=http://<dgx-ip>:8100
```
