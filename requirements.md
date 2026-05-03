# DriftPilot v3 — The Catalyst Horizon Engine

**Date:** 2026-05-03
**Branch:** `main`
**Validation artifact:** [reports/catalyst_horizons_midcap_2024.json](reports/catalyst_horizons_midcap_2024.json)
**Cross-signal failure analysis:** [reports/COMPARISON.md](reports/COMPARISON.md)
**Architectural plan:** [docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md](docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md)

---

## 1. Objective

Replace blind 1500-symbol technical scanning with a **Reason-First Selection Layer**: every position is opened only if a validated real-world catalyst exists for that symbol, and is force-exited if a negative catalyst lands while held. v1 backtests showed all four technical signals failing on the raw universe (edge_ratio 0.527-0.754) with the same diagnostic — selection bottleneck, not exit philosophy. v3 fixes the universe.

## 2. Discovery architecture (the "pipes")

Multi-source ingestion into one normalized `CatalystEvent` stream.

- **Primary:** **Alpaca News API** — real-time, ticker-tagged. Polled every 30s. This is the source of every event in the validation report; it is the contract source.
- **Secondary:** RSS aggregators (`fin-news` GitHub library or equivalent) scraping Yahoo Finance, CNBC, Nasdaq headlines. Adds breadth for symbols Alpaca doesn't tag.
- **Unification:** `DiscoveryService` normalizes both sources into `CatalystEvent`, dedupes by `(symbol, headline_hash, ts_bucket=±60s)` so the same story from two pipes counts once.

Failure handling: if RSS scrapers throw (sites change layout often), the system MUST fall back cleanly to Alpaca-only. Alpaca is load-bearing; RSS is additive.

## 3. Classification: deterministic primary, Qwen as second-pass

This is the load-bearing decision. Get it wrong and the validated edge ratios silently don't transfer.

**Primary classifier (deterministic):** port the regex/keyword `_categorize(headline) -> (category, subcategory)` from [scripts/catalyst_horizon_spike.py](scripts/catalyst_horizon_spike.py) into `src/driftpilot/catalyst/classifier.py`. This is the function that produced the validated 5.09×, 2.91×, 2.05×, 1.42× cells. Replacing it without re-running the spike on the new classifier breaks the contract.

**Acceptance:** the production classifier MUST produce ≥ 95% identical labels to the spike on the same headline corpus. Below 95%, validation doesn't apply.

**Qwen on DGX as second-pass enrichment (NOT primary):** for each event the deterministic classifier emits, send the headline to local Qwen for additional fields:

- `sentiment`: `positive` | `negative` | `neutral` (currently inferred crudely from subcategory — `target_cut`=negative; Qwen can disambiguate ambiguous headlines)
- `priority_score_modifier`: float in [-0.2, +0.2] reflecting headline strength within its category (e.g. "blowout earnings beat" vs "earnings missed by penny")
- `horizon_override`: optional `60m | 240m | 1day` if Qwen disagrees with the category-default horizon

These enrichments are **additive metadata only**. They do not change which events fire, only how they're ranked within their category. If Qwen is offline (it has been paused for backtests in the past), the system runs on the deterministic labels alone — same behavior as the validation report.

## 4. The ABCD catalyst taxonomy

Organizing model for the event stream. **Only [A] is data-validated.** [B][C][D] are hypotheses that need their own spikes before they ship.

### [A] Micro (corporate, single-ticker) — VALIDATED, ship in v3.0

The only pillar with edge_ratios from the spike. Build order is from the table.

| Cell | N | Ratio | v3 use |
|---|---|---|---|
| `earnings/report` @ 60m | 33 | **5.09×** | First long signal: `earnings_report_v1` |
| `earnings/report` @ 240m | 33 | **3.23×** | Hold extension allowed up to 240m |
| `analyst/target_cut` @ 60m–240m | 33-34 | **2.31-2.91×** | Negative filter (long-only). Hard exit if held; 4h block on new entries. |
| `analyst/target_raise` @ 60m | 104 | **1.42×** | Second long signal: `analyst_target_raise_v1` |
| `filing/8a` @ 60m | 256 | **2.05×** | Universe-filter rank booster (high N, weak edge — useful as priority not standalone) |

### [B] Meso (industry / sector ripple) — HYPOTHESIS, spike before shipping

Hypothesis: when a sector leader moves > X% on news, peer stocks in the same GICS sector show elevated 60m volatility. Not validated.

**Pre-build requirement:** add a Meso spike to `scripts/` modeled on `catalyst_horizon_spike.py`. Bucket events by `(leader_symbol, sector, peer_symbol, horizon)`. Verify N ≥ 20 per cell with ratio ≥ 1.5 before any code is written. The Meso spec ("if leader drops > 3%, halt sector entries") is currently opinion-coded; replace with data-coded threshold from the spike.

### [C] Macro (global / cross-asset) — PARTIAL OVERLAP with regime detector

Existing [src/driftpilot/regime_detector.py](src/driftpilot/regime_detector.py) already classifies `NEWS_SHOCK` from cross-asset volatility (VIX spike, SPY gap). Macro catalyst (FOMC, CPI, geopolitical) should feed this detector, not duplicate it.

**v3 wiring:** Macro events from the catalyst classifier (FOMC dates, CPI release timestamps, war/conflict keywords) bump `RegimeDetector` into `NEWS_SHOCK` for a configurable cooldown. The state machine already has gating on `NEWS_SHOCK`; extend it to flush all positions and block new entries during the cooldown. Do not invent a separate "RED regime."

### [D] Alpha (exogenous shocks: weather, disaster, supply) — HYPOTHESIS, spike before shipping

The proposed mapping ("hurricane → boost HD/LOW/GNRC") is hand-coded. The validated approach in v3 is data-coded only. Two paths:

1. Run a spike: bucket NOAA/USGS event categories × peer-symbol returns at 60m / 240m / 1day. Ship if ratios validate.
2. Skip [D] for v3.0; revisit in v3.1 once we see how the validated cells perform live.

**Default:** path 2.

## 5. Horizon-aware execution logic

Every catalyst event carries explicit horizons. Execution honors them:

- **Long entry on positive Micro catalyst** (`earnings/report`, `analyst/target_raise`):
  - Entry within 60m of event publish, not after.
  - Hold ≤ 60m for `target_raise` (validated 60m=1.42×, fades to 0.97× by 1day).
  - Hold ≤ 240m for `earnings/report` (validated 240m=3.23×, still strong).
  - Profit take + hard stop run as normal price-based exits.

- **Negative shield on `analyst/target_cut`:**
  - **Hard exit** any open long position on the symbol within next bar.
  - **4-hour entry block** on the symbol (allocator returns `BlockedReason.CATALYST_NEGATIVE`).
  - Block lifts at `event_ts + 240m`.

- **Macro `NEWS_SHOCK` (when wired in v3.1):** flush all positions, block all entries until regime exits NEWS_SHOCK.

**Universe filter (the only place catalyst affects priority):** symbols with a recent positive-direction Micro catalyst sort to the top of the candidate queue for the four technical signals. This is **rank-based**, not score-based. No "+20% boost." A catalyst-bearing name simply appears earlier in the list. The technical signals' thresholds do not change.

## 6. Storage: `catalyst_events` SQLite table

```sql
CREATE TABLE catalyst_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts        TIMESTAMP NOT NULL,    -- publish time (UTC)
    ingested_ts     TIMESTAMP NOT NULL,    -- when DiscoveryService received it
    symbol          TEXT NOT NULL,
    category        TEXT NOT NULL,         -- e.g. "earnings", "analyst", "filing"
    subcategory     TEXT NOT NULL,         -- e.g. "report", "target_cut"
    pillar          TEXT NOT NULL,         -- "micro" | "meso" | "macro" | "alpha"
    sentiment       TEXT,                  -- Qwen enrichment, NULL if Qwen offline
    priority_modifier REAL DEFAULT 0,      -- Qwen enrichment, defaults to 0
    horizon_minutes INTEGER NOT NULL,      -- 60, 240, 1440, 2880
    headline        TEXT NOT NULL,
    headline_hash   TEXT NOT NULL,         -- for dedupe
    source          TEXT NOT NULL,         -- "alpaca" | "yahoo_rss" | "cnbc_rss" | ...
    UNIQUE(symbol, headline_hash, event_ts)
);
CREATE INDEX idx_catalyst_symbol_ts ON catalyst_events(symbol, event_ts);
CREATE INDEX idx_catalyst_active ON catalyst_events(event_ts, category, subcategory);
```

Decay query (used by allocator and universe filter):

```sql
SELECT * FROM catalyst_events
WHERE symbol = ?
  AND event_ts >= ?  -- now - max_horizon_minutes
ORDER BY event_ts DESC;
```

## 7. State machine integration

Two new states added to [src/driftpilot/states.py](src/driftpilot/states.py):

- **`CATALYST_SCAN`** — entered on each `SCANNING` cycle; queries the bus + DB for active catalysts on each candidate symbol; emits a `CatalystAnnotation` per symbol that the allocator and signal `scan()` can read.
- **`EMERGENCY_FLUSH`** — entered when `analyst/target_cut` lands on a held symbol, OR `RegimeDetector → NEWS_SHOCK`. Cancels open orders, market-exits all positions next bar, transitions to `RECYCLING` with a cooldown timer.

`EMERGENCY_FLUSH` is the **same state** as v2 plan's Phase B Emergency Stop ([docs/REFACTOR_PLAN_V2_LIVE_OPERATOR.md](docs/REFACTOR_PLAN_V2_LIVE_OPERATOR.md) § Phase B). Implement once, used by both the operator panic button and the catalyst negative shield.

`BlockedReason` additions:
- `CATALYST_NEGATIVE = "catalyst_negative"` (target_cut window)
- `CATALYST_AGE_EXCEEDED = "catalyst_age_exceeded"` (event > horizon old)
- `MACRO_SHOCK_HALT = "macro_shock_halt"` (NEWS_SHOCK regime, v3.1)

---

## What's done (don't redo)

1. **Five-round catalyst spike** — methodology validated on 50 mid-caps × full 2024, 510 events, 4,800 baseline samples. The horizon × category model is the contract.
2. **Four locked-spec technical signals** — all FAIL on raw universe (see [reports/COMPARISON.md](reports/COMPARISON.md)). Code is correct; the bottleneck is selection. v3 universe filter is the fix.
3. **Backtest harness** — supports event-timestamp filtering for catalyst signals. Reusable, do not modify.
4. **Signal contract** — [src/driftpilot/signals/base.py](src/driftpilot/signals/base.py). New catalyst signals MUST conform.
5. **Regime detector** — [src/driftpilot/regime_detector.py](src/driftpilot/regime_detector.py). Macro events feed this; do not duplicate.

## What to build (in order — v3.0)

### Step 1 — `src/driftpilot/catalyst/` package

Files:
- `__init__.py`
- `event.py` — `CatalystEvent` frozen dataclass: `symbol, category, subcategory, pillar, ts, headline, source, horizon_minutes, sentiment=None, priority_modifier=0.0`. Hashable.
- `classifier.py` — port `_categorize` from spike. **No edits to logic** — copy it. Add unit tests that round-trip ≥ 20 known headlines from the spike corpus to assert label preservation. Acceptance: ≥ 95% match.
- `qwen_enricher.py` — async client to local Qwen on DGX (reuse the existing vllm endpoint at `http://192.168.1.166:8000/v1`). Returns `{sentiment, priority_modifier, horizon_override}`. **MUST have a 500ms timeout and a clean fallback** to default values if Qwen is unreachable. Tests assert behavior with Qwen mocked-down.
- `event_bus.py` — async pub/sub: `subscribe(category, subcategory, callback)`, `publish(event)`. Uses `asyncio.Lock`.
- `feed_alpaca.py` — async producer polling Alpaca News every 30s. Reuses pagination pattern from spike. Classifies → enriches → publishes → persists to SQLite.
- `feed_rss.py` — async producer polling RSS sources. Same downstream flow. **Hard-fails clean** when sources error.
- `discovery_service.py` — orchestrator that owns both feeds + dedupe + DB write.
- `db.py` — `catalyst_events` schema + CRUD helpers. Migration script to add the table to the existing SQLite DB.

Tests:
- Round-trip classifier on 20 spike-corpus headlines (≥ 95% match).
- Bus subscribe/publish/unsubscribe.
- Dedupe: same headline from Alpaca + Yahoo within 60s window → single event.
- Qwen offline → enricher returns defaults, no crash.
- DB: insert + decay query returns recent events only.

### Step 2 — `earnings_report_v1` signal (highest-edge cell, 5.09×)

Path: `src/driftpilot/signals/earnings_report_v1/`

Thesis: stocks with `earnings/report` catalyst in last 60m show 5.09× baseline 60m absolute return. Buy on event, hold ≤ 60m, exit on first 1% gain or at horizon.

Files: `config.py` (`EarningsReportConfig`: `max_hold_minutes=60, profit_take_pct=1.0, stop_loss_pct=1.5, max_event_age_minutes=60`), `signal.py`, `signal_state.py`, `features.py`, `exits.py`, `README.md` (cite validation report), `KNOWN_RISKS.md`.

Backtest gate: `edge_ratio ≥ 1.5`. (Higher than universal 1.1 because the validated cell is 5.09×; if backtest comes back at 1.1, wiring is wrong.)

### Step 3 — `analyst/target_cut` negative filter + EMERGENCY_FLUSH wiring

Allocator hook: when a candidate symbol has `analyst/target_cut` event < 240m old, return `BlockedReason.CATALYST_NEGATIVE`.

State machine: when `analyst/target_cut` lands on a HELD symbol, transition to `EMERGENCY_FLUSH` (shared state with v2 Phase B). Tests assert flush-on-event-arrival and 4h block on re-entry.

### Step 4 — `analyst_target_raise_v1` signal (1.42×, N=104)

Same shape as Step 2. Config: `profit_take_pct=0.8, max_hold_minutes=60, stop_loss_pct=1.0`.

Backtest gate: `edge_ratio ≥ 1.2`.

### Step 5 — Universe filter wiring for the 4 technical signals

`CatalystUniverseFilter` injected into SCANNING:
- Drop symbols with `analyst/target_cut` < 240m old (negative shield extends to technical signals too).
- Rank symbols with positive Micro catalyst (`earnings/report`, `analyst/target_raise`, `filing/8a`) above non-catalyst names.
- Symbols with no catalyst kept, ranked below.

**Hard rule:** thresholds inside Apex / RS-Drift / Whale-Tail / Stationary-Ghost DO NOT change. The filter changes WHAT they see, not HOW they decide.

After Step 5, re-run all four technical signal backtests on filtered universe. Predicted improvements (from [reports/COMPARISON.md](reports/COMPARISON.md)): whale_tail > apex > stationary_ghost > rs_drift. The side-by-side becomes the load-bearing evidence for whether v3 worked.

## What to defer (v3.1+)

- **[B] Meso pillar** — needs sector-ripple spike. Don't build the "halt sector on leader -3%" rule until data validates the threshold.
- **[C] Macro / regime integration** — Macro events feeding `NEWS_SHOCK` can ship in v3.1 once the [A] cells are live and stable.
- **[D] Alpha pillar (weather/disaster)** — needs its own spike. Path 2 = skip.
- **Industry-specific sensitivity** ("IT/SaaS prioritizes Meso") — opinion-coded, not data-coded. Defer.

---

## Architectural rules (do not violate)

These come from [docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md](docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md) § "Hard architectural rules":

1. **Catalyst is a UNIVERSE FILTER + RANK-BASED PRIORITY input. NOT an entry-rule modifier.** Don't lower thresholds based on news. Rank only — no percent boosts on technical-signal scores.
2. **Catalyst exits are BACKUP to technical exits, NOT replacements.** Price stops still run; catalyst-driven flush is in addition.
3. **No look-ahead.** Entry can only happen on bars AFTER `event_ts`. Backtests must enforce this.
4. **Long-only paper account.** `target_cut` is a negative filter, not a short signal.
5. **Deterministic classifier is the contract source.** Qwen enriches; it does not classify primarily. If Qwen output is used as the category label, the validated edge ratios silently stop applying.
6. **N ≥ 30 in validation report** is the bar to ship a catalyst signal. Don't ship on small-sample cells (e.g. `macro/fomc` N=2, even though ratio is 3.85×).

## Locked-spec gates (universal)

From [src/driftpilot/backtest/report.py](src/driftpilot/backtest/report.py):

- `edge_ratio ≥ 1.1` (universal, FAIL otherwise)
- Signal-specific gates per signal's README (e.g. `fill_rate_pct ≥ 0.50` for mid-price entry signals)

A new catalyst signal that fails these does not ship. No exceptions.

## Where to find things

| Need | Path |
|---|---|
| Signal Protocol contract | [src/driftpilot/signals/base.py](src/driftpilot/signals/base.py) |
| BlockedReason enum | [src/driftpilot/states.py](src/driftpilot/states.py) |
| Backtest harness | [src/driftpilot/backtest/replay.py](src/driftpilot/backtest/replay.py) |
| Existing signal example | [src/driftpilot/signals/rs_drift_v1/](src/driftpilot/signals/rs_drift_v1/) |
| Regime detector | [src/driftpilot/regime_detector.py](src/driftpilot/regime_detector.py) |
| Spike script (reference, do not modify) | [scripts/catalyst_horizon_spike.py](scripts/catalyst_horizon_spike.py) |
| Validation report (canonical) | [reports/catalyst_horizons_midcap_2024.json](reports/catalyst_horizons_midcap_2024.json) |
| Cross-signal failure analysis | [reports/COMPARISON.md](reports/COMPARISON.md) |
| Architecture plan | [docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md](docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md) |
| Hard project rules | [AGENTS.md](AGENTS.md) |
| Doc status table | [docs/DOCS_INDEX.md](docs/DOCS_INDEX.md) |

## Environment

- Python 3.11+, async runtime (asyncio)
- SQLite for `catalyst_events` (existing DB at `data/driftpilot.db`)
- Databento bars (1m, EQUS.MINI, ohlcv-1m schema) cached locally and on DGX
- Alpaca-py for primary news + paper trading (key in `.env`)
- RSS via `fin-news` or feedparser (additive only)
- DGX Spark host (`sankerkr@192.168.1.166`) running Qwen via vllm at `:8000` for enrichment
- DGX also handles compute-heavy backtests (`scripts/migrate_to_dgx.sh`)

## Definition of done for v3.0

- [ ] `src/driftpilot/catalyst/` package: classifier (≥95% spike match), event bus, Alpaca feed, RSS feed, Qwen enricher (with timeout fallback), DiscoveryService, SQLite schema migration
- [ ] `earnings_report_v1` signal merged with `edge_ratio ≥ 1.5` backtest verdict
- [ ] `analyst/target_cut` negative filter wired into allocator AND `EMERGENCY_FLUSH` triggered on held-symbol cuts
- [ ] `analyst_target_raise_v1` signal merged with `edge_ratio ≥ 1.2` backtest verdict
- [ ] `CatalystUniverseFilter` wired into SCANNING; 4 existing technical signals re-backtested on filtered universe
- [ ] [reports/COMPARISON.md](reports/COMPARISON.md) updated with v3-retrofit edge_ratio column for the 4 technical signals
- [ ] [docs/DOCS_INDEX.md](docs/DOCS_INDEX.md) updated for any new docs
- [ ] All steps land via reviewer-agent + worktree pattern (see [docs/REFACTOR_PLAN_V2_LIVE_OPERATOR.md](docs/REFACTOR_PLAN_V2_LIVE_OPERATOR.md) § "agent orchestration model")

## What NOT to do

- Don't re-run the catalyst spike — the validation IS the contract.
- Don't replace the deterministic classifier with Qwen as primary — that breaks the contract.
- Don't lower technical signals' thresholds when a catalyst is present. Universe filter only.
- Don't ship a catalyst signal on a cell with N < 30 in the validation report.
- Don't introduce short-side trading. Long-only paper.
- Don't build [B] Meso, [D] Alpha, or industry-specific routing without spike data first.
- Don't invent a "RED regime" — Macro feeds the existing `RegimeDetector → NEWS_SHOCK`.
- Don't apply percent-based "+20% priority" boosts. Rank only.
