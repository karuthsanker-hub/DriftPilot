# Qwen Enrichment v2 — Pre-Enrichment Context Pipeline

**Date:** 2026-05-10  
**Status:** REQUIREMENTS  
**Depends on:** Qwen3-8B on DGX Spark (192.168.1.166:8000), existing API keys (FMP, Finnhub, FRED, Alpaca)

---

## The problem in one paragraph

The current Qwen enrichment prompt sends a raw headline + category to the LLM and asks it to predict 60-minute price direction. Qwen has no context about the company, the magnitude of the event, or market conditions. Result: 98% of positive earnings events get the same score (`+0.15`) regardless of whether it's a 0.9% EPS beat on a $3B consumer staples company or a 6.5% beat on a $100B biotech. The enrichment produces a ternary classifier (positive/negative/neutral) instead of a conviction-weighted score. This destroyed the edge when re-validated — earnings_report_v1 dropped from edge=1.6 to edge=1.0 because 152 marginal "positive" events diluted the signal.

---

## What a quant analyst has on their desk

Before a quant acts on a headline, they already know:

1. **How big is the company?** — Market cap, float, avg volume. A $200M micro-cap moves differently than a $500B mega-cap.
2. **How big is the beat?** — EPS beat %, revenue beat %. A $0.01 beat (0.9%) is noise. A $0.58 beat (6.5%) is actionable.
3. **Does this company routinely beat?** — Last 4 quarters of earnings surprise %. A serial 5% beater getting 5% again is priced in.
4. **How volatile is this stock normally?** — 20-day ATR as % of price. A 1% beat on a stock that moves 4% daily is nothing.
5. **What time is it?** — Pre-market headline has different drift profile than mid-session.
6. **Is this the first headline or a repeat?** — The 3rd headline about the same earnings is already priced in.
7. **What's the market doing?** — VIX level, SPY change today. A beat in a risk-off tape drifts less.
8. **What sector is this, and is the sector hot?** — Momentum-on-momentum vs. relief bounce.

The current prompt provides **none of this**. We need to assemble this context block before calling Qwen.

---

## Current state (v1)

### What gets sent to Qwen today

**System prompt:** Generic rules ("earnings beat → positive"). Three example magnitudes (+0.15, +0.05, -0.10) that Qwen anchors on.

**User prompt:**
```
/no_think Headline: "{headline}"
Category: {category}/{subcategory}
Symbol context: this headline was tagged to a specific stock.
Will it move that stock's price in the next 60 minutes?
```

### What comes back

```json
{"sentiment": "positive", "priority_modifier": 0.15, "confidence": 0.8, "horizon_override": null}
```

### What gets persisted

Only `sentiment`, `priority_modifier`, `horizon_override`. **Confidence is discarded** — no column in DB.

### Distribution (23,888 events, new prompt)

| sentiment | priority_modifier | count | % of bucket |
|---|---|---|---|
| positive | +0.15 | 9,227 | 97.9% |
| positive | +0.10 | 132 | 1.4% |
| positive | +0.05 | 65 | 0.7% |
| negative | -0.15 | 2,382 | 58.1% |
| negative | -0.10 | 1,708 | 41.6% |
| neutral | 0.00 | 9,339 | 90.1% |
| neutral | +0.05 | 1,018 | 9.8% |

**Problem:** No gradient within each bucket. Cannot distinguish strong from weak signals.

---

## Target state (v2)

### Pre-enrichment context block

Before calling Qwen, assemble a context object for each headline:

```
CONTEXT:
- Market cap: $2.8B | Avg volume: 320K/day | Sector: Consumer Staples
- 20-day ATR: $1.85 (3.3% of price)
- EPS beat: $0.01 (0.9%) | Revenue beat: $1.7M (0.6%)
- Guidance: maintained
- Last 4 earnings surprises: +2.1%, +1.8%, -0.3%, +3.5% (avg +1.8%)
- Headlines for PBH in last 30min: 0 (first headline)
- Time: 08:02 ET (pre-market, 88 min to open)
- SPY today: +0.3% | VIX: 14.2
- Sector ETF (XLP) 5-day: -0.1%
```

### Redesigned prompt

The system prompt must:

1. **Tell Qwen to use the numbers**, not just the words. "A 0.9% EPS beat on a stock that routinely beats by 1.8% is priced in — neutral."
2. **Define magnitude tiers explicitly** with numeric ranges, not 3 example anchors.
3. **Define confidence calibration** — what 0.3 vs 0.7 vs 0.95 means in terms of expected hit rate.
4. **Handle mixed signals** — "beats EPS but lowers guidance" = negative despite the beat.
5. **Account for staleness** — "if this is the 3rd headline, the move is likely done."
6. **Account for company size** — "a 3% beat on a $500B company moves less than 3% on a $500M company."

### What gets persisted (schema change)

Add `confidence REAL DEFAULT NULL` column to `catalyst_events` table. Update `_update_row()` in enrichment script to write it.

### What the output distribution should look like

Instead of 98% at +0.15, we want a real spread:

| priority_modifier | meaning | rough % of positives |
|---|---|---|
| +0.15 to +0.20 | Strong: big beat, small cap, fresh headline, hot sector | ~10-15% |
| +0.08 to +0.14 | Moderate: clear beat, mid-cap, no headwinds | ~25-30% |
| +0.03 to +0.07 | Mild: small beat, large cap, or serial beater | ~30-35% |
| +0.01 to +0.02 | Marginal: in-line with history, priced in | ~20-25% |

This gives the router and backtest a gradient to filter on.

---

## Data sources for context block

### Already integrated — no new API needed

| Field | Source | Code location | Call pattern |
|---|---|---|---|
| Market cap | FMP `company_profile()` | `src/trading_bot/data/replacement_stack.py:90-126` | 1 call per symbol, cacheable |
| Avg daily volume | FMP `company_profile()` | Same call | Same call |
| Sector | `config/universe.csv` | Local CSV lookup | Free, instant |
| Last 4 earnings surprises | Finnhub | `replacement_stack.py:54-70` | 1 call per symbol |
| VIX | FRED `current_vix()` | `src/trading_bot/data/macro_data.py:14-40` | 1 call per enrichment run |
| SPY daily change | Alpaca or YFinance | `spy_premarket_change_pct()` | 1 call per run |
| 20-day ATR | Databento parquet | `data/bars/databento/{SYMBOL}/2024.parquet` | Local file read, compute on the fly |
| Headline cluster count | Catalyst events DB | SQL query on `catalyst_events` | Local query |
| Event timestamp / time to open | DB column `event_ts` | Already available | Free |

### Need to add — free sources

| Field | Source | Cost | Notes |
|---|---|---|---|
| Float / shares outstanding | YFinance `info['floatShares']` | Free | Add to company profile fetch, cache per symbol |
| Sector ETF 5-day return | YFinance daily history | Free | 11 sector ETFs (XLK, XLF, XLP, etc.), 1 call each per run |

### Not available / skip for now

| Field | Why skip |
|---|---|
| Short interest | Paid data (Ortex/S3). Not worth it for v2. |
| Options flow / gamma exposure | Paid. Phase 3+. |
| Institutional ownership changes | 13F is quarterly, too stale for 60-min signals. |

---

## EPS/revenue beat parsing

Many headlines contain the actual numbers:

```
"REGN Q1 Adj. EPS $9.47 Beats $8.89 Estimate, Sales $3.605B Beat $3.483B Estimate"
```

We can parse this **before** calling Qwen:

- EPS actual: $9.47, estimate: $8.89 → beat: +6.5%
- Revenue actual: $3.605B, estimate: $3.483B → beat: +3.5%

This is a regex/NLP extraction step, not an LLM call. If parsing fails (ambiguous headline), pass `null` and let Qwen infer from the text.

---

## Architecture

### Pipeline flow

```
headline + category/subcategory + symbol + event_ts
        │
        ▼
┌─────────────────────────┐
│  Context Assembler      │
│                         │
│  1. Company profile     │  ← FMP (cached per symbol)
│  2. Earnings history    │  ← Finnhub (cached per symbol)
│  3. ATR from bars       │  ← Databento parquet (local)
│  4. Parse EPS/rev beat  │  ← Regex on headline text
│  5. Headline cluster    │  ← SQL count on catalyst DB
│  6. Market snapshot     │  ← FRED + Alpaca (cached per run)
│  7. Sector ETF perf     │  ← YFinance (cached per run)
│                         │
└────────────┬────────────┘
             │ context block (structured text)
             ▼
┌─────────────────────────┐
│  Qwen Enricher v2       │
│                         │
│  System prompt (v2)     │
│  + headline             │
│  + context block        │
│                         │
│  → sentiment            │
│  → priority_modifier    │  (real gradient, not 3 buckets)
│  → confidence           │  (calibrated, persisted)
│  → horizon_override     │
│                         │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│  DB writer              │
│                         │
│  catalyst_events:       │
│    sentiment             │
│    priority_modifier     │
│    confidence  (NEW)     │
│    horizon_minutes       │
└─────────────────────────┘
```

### Caching strategy

Most context is per-symbol or per-run, not per-headline:

| Scope | Data | Cache TTL |
|---|---|---|
| Per enrichment run | VIX, SPY change, sector ETF returns | Fetch once at start |
| Per symbol | Market cap, avg volume, float, sector, earnings history, ATR | Fetch once per symbol, reuse across all headlines for that symbol |
| Per headline | EPS/revenue beat parse, headline cluster count | Compute per headline (fast, local) |

For batch re-enrichment of 23,888 events across ~2,000 unique symbols:
- ~2,000 FMP calls (company profile) — within free tier if batched across hours
- ~2,000 Finnhub calls (earnings surprise) — 60/min free tier = ~33 minutes
- ~2,000 parquet reads (ATR) — local, fast
- 1 FRED call, 1 SPY call, 11 sector ETF calls — negligible
- 23,888 Qwen calls — same as v1, ~56 minutes at current throughput

**Total enrichment time estimate: ~90 minutes** (bottleneck is Finnhub rate limit, parallelizable with Qwen calls)

### Rate limit management

| API | Free tier limit | Strategy |
|---|---|---|
| FMP | 250 calls/day (free), 300/min (starter) | Batch across multiple days, or use starter tier |
| Finnhub | 60 calls/min | Semaphore with 1-second spacing |
| FRED | 120 calls/min | One call per run, no issue |
| YFinance | No formal limit | Respectful spacing, cache aggressively |
| Qwen (local) | No limit | 16 concurrent as today |

If FMP's 250/day free tier is the bottleneck for 2,000 symbols:
- Option A: Batch over 8 days (250/day × 8 = 2,000)
- Option B: Use YFinance `info` dict as fallback (market_cap, avg_volume, float all available)
- Option C: Upgrade FMP to starter tier ($14/mo)

**Recommended: Option B.** YFinance for bulk historical enrichment, FMP for live enrichment (low volume, well within free tier).

---

## DB schema change

```sql
ALTER TABLE catalyst_events ADD COLUMN confidence REAL DEFAULT NULL;
```

Update `_update_row()` in `scripts/enrich_catalyst_events.py` to persist confidence:

```sql
UPDATE catalyst_events
SET sentiment = ?, priority_modifier = ?, confidence = ?,
    horizon_minutes = COALESCE(?, horizon_minutes)
WHERE id = ?
```

---

## Dashboard visibility — Catalyst Event Detail Panel

### Current state

The dashboard (`src/trading_bot/dashboard/templates/dashboard.html`) shows a scrolling **CATALYST FEED** ticker at the top. Each event displays:

```
10:32  REGN  earnings/report  positive  "REGN Q1 Adj. EPS $9.47 Beats..."  •
```

That's it. No context about **why** it was classified positive, how big the beat was, or whether the company is a mega-cap or micro-cap. You cannot detect issues like "this is a $0.01 beat on a $3B company — why is it +0.15?"

### What's needed

The dashboard needs a **Catalyst Event Detail Panel** — click any ticker item and see the full enrichment context that was sent to Qwen, plus Qwen's response:

```
┌─────────────────────────────────────────────────────────────────┐
│  PBH  •  earnings/report  •  positive  •  pm: +0.15  •  conf: 0.82  │
├─────────────────────────────────────────────────────────────────┤
│  HEADLINE                                                       │
│  "PBH Q2 Adj. EPS $1.09 Beats $1.08 Estimate, Sales $283.8M   │
│   Beat $282.1M Estimate"                                        │
├─────────────────────────────────────────────────────────────────┤
│  CONTEXT SENT TO QWEN                                           │
│  Market cap: $2.8B  |  Avg volume: 320K  |  Sector: Cons.Staples│
│  20-day ATR: $1.85 (3.3%)                                      │
│  EPS beat: $0.01 (0.9%)  |  Revenue beat: $1.7M (0.6%)         │
│  Guidance: maintained                                           │
│  Last 4 surprises: +2.1%, +1.8%, -0.3%, +3.5% (avg +1.8%)     │
│  Headlines for PBH in last 30min: 0 (first)                    │
│  Time: 08:02 ET (pre-market, 88 min to open)                   │
│  Market: SPY +0.3%, VIX 14.2, XLP -0.1% (5d)                  │
├─────────────────────────────────────────────────────────────────┤
│  QWEN RESPONSE                                                  │
│  sentiment: positive  |  confidence: 0.82  |  pm: +0.15        │
│  horizon_override: null                                         │
├─────────────────────────────────────────────────────────────────┤
│  ⚠ FLAG: EPS beat 0.9% is below avg historical surprise 1.8%   │
│  ⚠ FLAG: Revenue beat 0.6% — marginal                          │
│  → Expected: neutral or positive +0.03, not +0.15              │
└─────────────────────────────────────────────────────────────────┘
```

### Requirements

1. **Persist the full context block** alongside each enrichment. Add a `context_json TEXT` column to `catalyst_events`. The context assembler writes the structured context as JSON before calling Qwen. The dashboard reads it back.

2. **Persist Qwen's raw response** for auditability. Add a `qwen_response_json TEXT` column. Store the full JSON Qwen returned (sentiment, confidence, pm, horizon_override).

3. **Clickable ticker items** — clicking a catalyst event in the scrolling ticker opens a detail panel (modal or slide-out) showing headline, context block, Qwen response, and auto-generated flags.

4. **Auto-generated flags** — the dashboard computes warning flags client-side:
   - EPS beat % below historical average surprise → "marginal beat"
   - Revenue beat < 1% → "noise-level revenue"
   - Market cap > $50B and beat < 2% → "mega-cap, small beat"
   - Headline cluster > 2 in last 30 min → "stale / repeated"
   - confidence < 0.5 → "low confidence"
   - priority_modifier at boundary values (exactly 0.15 or -0.15) → "possible anchor bias"

5. **News ticker enhancement** — the scrolling ticker itself should show confidence and pm inline:
   ```
   10:32  REGN  earnings/report  positive  0.82  +0.15  "REGN Q1..."  •
   ```

6. **API endpoint** — new endpoint `/api/catalyst/event/{id}` returning the full event with context_json and qwen_response_json for the detail panel.

### DB schema additions

```sql
ALTER TABLE catalyst_events ADD COLUMN confidence REAL DEFAULT NULL;
ALTER TABLE catalyst_events ADD COLUMN context_json TEXT DEFAULT NULL;
ALTER TABLE catalyst_events ADD COLUMN qwen_response_json TEXT DEFAULT NULL;
```

### Files to modify

| File | Change |
|---|---|
| `src/driftpilot/catalyst/qwen_enricher.py` | Accept + format context block in prompt |
| `scripts/enrich_catalyst_events.py` | Call context assembler, persist context_json + qwen_response_json + confidence |
| `src/driftpilot/dashboard/view_models.py` | `_news_ticker()` returns confidence, context_json; new `_catalyst_detail()` function |
| `src/trading_bot/dashboard/app.py` | New `/api/catalyst/event/{id}` endpoint |
| `src/trading_bot/dashboard/templates/dashboard.html` | Clickable ticker items, detail panel modal, inline confidence/pm display |

---

## Validation plan

### A/B comparison

1. Back up current DB: `catalyst_events_2024.sqlite3.bak-v1-prompt`
2. Re-enrich all 23,888 events with v2 pipeline
3. Compare distributions: v1 (3-bucket) vs v2 (gradient)
4. Run backtest on Oct-Nov window with various `priority_modifier` thresholds:
   - All positives (pm > 0)
   - Strong positives (pm ≥ 0.10)
   - High confidence (confidence ≥ 0.7 AND pm ≥ 0.08)
5. Target: recover edge_ratio ≥ 1.5 on the filtered subset

### Success criteria

| Metric | v1 (current) | v2 target |
|---|---|---|
| priority_modifier distinct values (positive bucket) | 3 | ≥ 15 |
| Edge ratio (earnings, positive, Oct-Nov) | 1.137 | ≥ 1.5 |
| Edge ratio (earnings, strong positive, Oct-Nov) | N/A | ≥ 2.0 |
| Confidence correlation with actual outcome | Unknown | > 0.2 Spearman |
| Stop-loss rate (strong positive) | 25% | ≤ 18% |

### Failure modes

- Qwen still clusters despite better prompt → try few-shot examples in prompt
- Context assembly too slow for live enrichment → pre-cache company profiles at startup
- FMP rate limit blocks batch enrichment → fall back to YFinance
- ATR computation on 2,000 symbols too slow → precompute and cache as CSV

---

## Implementation phases

### Phase A: Context Assembler (no prompt change yet)

Build `ContextAssembler` class that takes (symbol, headline, event_ts, category, subcategory) and returns a structured context block. Test it produces correct output for known symbols. No Qwen integration yet.

### Phase B: Headline parser

Regex extraction of EPS actual/estimate, revenue actual/estimate, guidance direction from headline text. Test on 100 sample headlines from the DB.

### Phase C: Prompt v2 + confidence persistence

Redesign system prompt with magnitude tiers, calibrated confidence rubric, and instructions to use context block. Add `confidence` column to DB. Wire context block into `QwenEnricher.enrich()`.

### Phase D: Batch re-enrichment + validation

Re-enrich full 2024 DB. Run backtest suite. Validate edge recovery. Compare v1 vs v2 distributions.

---

## Agent breakdown

Five agents, strict ownership boundaries. Each agent reads this doc + CODEX_HANDOFF.md before starting. No agent touches another agent's files without code review approval.

### Agent 1: Context Assembler (Phase A)

**Owns:** `src/driftpilot/catalyst/context_assembler.py` (NEW)

**Job:** Build a `ContextAssembler` class that takes `(symbol, headline, event_ts, category, subcategory)` and returns a structured context dict. Must be callable both in live enrichment (single event, real-time API calls) and batch re-enrichment (23k events, cached lookups).

**Data sources to wire:**
- Market cap + avg volume → YFinance `Ticker(symbol).info` for batch, FMP for live (already integrated in `src/trading_bot/data/replacement_stack.py:90-126`)
- Sector → `config/universe.csv` local lookup
- Earnings surprise history (last 4 Qs) → Finnhub (already in `replacement_stack.py:54-70`)
- 20-day ATR → compute from Databento parquet at `data/bars/databento/{SYMBOL}/2024.parquet`
- VIX → FRED (already in `src/trading_bot/data/macro_data.py:14-40`)
- SPY daily change → Alpaca or YFinance (already have `spy_premarket_change_pct()`)
- Sector ETF 5-day return → YFinance daily history for XLK, XLF, XLP, XLV, XLI, XLY, XLE, XLB, XLRE, XLC, XLU
- Headline cluster count → SQL `SELECT COUNT(*) FROM catalyst_events WHERE symbol = ? AND event_ts BETWEEN ? AND ?`
- Time to market open → compute from `event_ts`

**Caching contract:**
- `cache_run_context()` — call once per enrichment run, fetches VIX + SPY + sector ETFs
- `cache_symbol_context(symbol)` — call once per symbol, fetches market cap + volume + earnings history + ATR
- `build_context(symbol, headline, event_ts, category, subcategory)` — per-headline, uses caches + computes cluster count + time

**Output format:**
```python
@dataclass(frozen=True)
class EnrichmentContext:
    market_cap_m: float | None         # millions
    avg_volume: int | None             # shares/day
    sector: str | None
    atr_pct: float | None              # ATR as % of price
    eps_beat_pct: float | None         # parsed from headline
    revenue_beat_pct: float | None     # parsed from headline
    guidance_direction: str | None     # "up" / "down" / "maintained" / None
    last_4_surprises: list[float]      # [+2.1, +1.8, -0.3, +3.5]
    headline_cluster_count: int        # how many headlines for this symbol in last 30 min
    minutes_to_open: int | None        # None if during market hours
    spy_change_pct: float | None
    vix: float | None
    sector_etf_5d_pct: float | None

    def to_prompt_block(self) -> str:
        """Format as the CONTEXT: text block for Qwen prompt."""
        ...
```

**Tests:** `tests/catalyst/test_context_assembler.py`
- Test `to_prompt_block()` produces expected text for known inputs
- Test caching — second call for same symbol doesn't hit API
- Test missing data gracefully returns None fields (not crash)
- Test ATR computation against known parquet data
- Test headline cluster count with seeded DB

**Constraints:**
- No Qwen calls. This agent doesn't touch the prompt or enricher.
- Must work without API keys (mock/stub for tests)
- YFinance for batch, FMP for live — configurable via constructor param

---

### Agent 2: Headline Parser (Phase B)

**Owns:** `src/driftpilot/catalyst/headline_parser.py` (NEW)

**Job:** Regex extraction of structured financial data from headline text. Parse EPS actual/estimate, revenue actual/estimate, guidance direction. Called by Context Assembler to populate `eps_beat_pct`, `revenue_beat_pct`, `guidance_direction`.

**Patterns to handle:**

```
# EPS patterns
"Adj. EPS $9.47 Beats $8.89 Estimate"     → eps_actual=9.47, eps_est=8.89, beat=+6.5%
"EPS $1.09 Beats $1.08 Estimate"           → eps_actual=1.09, eps_est=1.08, beat=+0.9%
"GAAP EPS $3.59 Beats $3.39 Estimate"      → eps_actual=3.59, eps_est=3.39, beat=+5.9%
"EPS $0.12 Misses $0.15 Estimate"          → eps_actual=0.12, eps_est=0.15, beat=-20.0%

# Revenue patterns
"Sales $3.605B Beat $3.483B Estimate"      → rev_actual=3605M, rev_est=3483M, beat=+3.5%
"Sales $283.785M Beat $282.093M Estimate"  → rev_actual=283.8M, rev_est=282.1M, beat=+0.6%
"Revenue $1.2B Missed $1.3B Estimate"      → rev_actual=1200M, rev_est=1300M, beat=-7.7%

# Guidance patterns
"Raises FY25 Revenue Growth"               → guidance="up"
"Lowers FY Guidance"                        → guidance="down"
"Reaffirms Full-Year Outlook"              → guidance="maintained"
"Raises FY2026 GAAP EPS Guidance from $19.76-$20.22 to $20.08-$20.44" → guidance="up"

# Mixed signal
"Q1 EPS Beats ... Lowers FY Guidance"      → beat + guidance="down" (beat-and-lower)

# Unparseable
"Progressive's November Surge: EPS Soars 49%" → eps_beat_pct=None (no estimate to compare)
```

**Output:**
```python
@dataclass(frozen=True)
class HeadlineParsed:
    eps_actual: float | None
    eps_estimate: float | None
    eps_beat_pct: float | None
    revenue_actual_m: float | None     # always in millions
    revenue_estimate_m: float | None
    revenue_beat_pct: float | None
    guidance_direction: str | None      # "up" / "down" / "maintained" / None
    is_mixed_signal: bool               # beat + lower guidance
```

**Tests:** `tests/catalyst/test_headline_parser.py`
- Parametrized test with ≥30 real headlines from the catalyst DB (pull with SQL, hardcode in test)
- Test each pattern group: EPS beat, EPS miss, revenue beat, revenue miss, guidance up/down/maintained, mixed signal, unparseable
- Test dollar amount parsing: $3.605B → 3605.0M, $283.785M → 283.785M, $1.2T → 1200000M
- Test edge cases: negative EPS, zero estimate, missing estimate

**Constraints:**
- Pure regex/string parsing — no LLM calls, no API calls
- If parsing fails, return None fields — never guess
- Must handle both "Beats" and "Beat" (singular/plural), "Misses" and "Missed"

---

### Agent 3: Prompt v2 + Enricher Update (Phase C)

**Owns:** `src/driftpilot/catalyst/qwen_enricher.py` (MODIFY)

**Job:** Redesign the system prompt to use the context block, define magnitude tiers with numeric ranges, add calibrated confidence rubric. Wire the `EnrichmentContext.to_prompt_block()` into the user prompt. Add confidence to DB persistence.

**System prompt v2 requirements:**
1. Keep the role framing ("short-term equity analyst predicting 60-min price direction")
2. Replace the 3-example anchor with a **magnitude tier table**:
   ```
   MAGNITUDE TIERS (use these ranges, not fixed values):
   +0.15 to +0.20: Large-cap beat >5% or small-cap beat >3%, with guidance raise
   +0.08 to +0.14: Clear beat 2-5% on mid-cap, or any beat with hot sector tailwind
   +0.03 to +0.07: Small beat 1-2%, large-cap, or beat in line with history
   +0.01 to +0.02: Marginal beat <1%, routine, already priced in
    0.00:           No directional signal, informational only
   -0.01 to -0.07: Small miss, minor negative, guidance maintained
   -0.08 to -0.14: Clear miss, or beat with guidance cut (mixed signal → net negative)
   -0.15 to -0.20: Large miss, guidance cut, downgrade on high-conviction name
   ```
3. Add a **confidence calibration rubric**:
   ```
   CONFIDENCE CALIBRATION:
   0.90-1.00: Numbers clearly in headline, direction unambiguous, large magnitude
   0.70-0.89: Clear beat/miss but magnitude uncertain, or moderate event
   0.50-0.69: Directional lean but could go either way (small beat, mixed signals)
   0.30-0.49: Weak signal, mostly noise, slight lean
   0.00-0.29: Essentially a coin flip, no meaningful edge
   ```
4. Add instruction: **"Use the CONTEXT block to calibrate magnitude. A 0.9% EPS beat on a company that averages 1.8% surprise is noise — neutral or +0.02 at most."**
5. Add instruction: **"If headline_cluster_count > 0, this headline is likely already priced in — reduce confidence by 0.2 and magnitude by half."**
6. Add instruction: **"If VIX > 25, reduce positive magnitude by 30% — fear compresses drift."**
7. Keep `horizon_override` but make it clearer: only override if the CONTEXT suggests a different time horizon than 60 minutes.

**User prompt v2:**
```
/no_think Headline: "{headline}"
Category: {category}/{subcategory}

CONTEXT:
{context.to_prompt_block()}

Based on the headline AND the context above, predict the 60-minute price direction.
```

**Enricher changes:**
- `enrich()` signature: add `context: EnrichmentContext | None = None` parameter
- If context provided, inject `context.to_prompt_block()` into user prompt
- If context is None (backward compat), use old minimal prompt
- Return `EnrichmentResult` unchanged (already has confidence field)

**DB persistence changes in `scripts/enrich_catalyst_events.py`:**
- `_update_row()` now writes confidence, context_json, qwen_response_json
- Add `--force-re-enrich` flag to re-process events that already have sentiment (for v1→v2 migration)

**Schema migration:**
```sql
ALTER TABLE catalyst_events ADD COLUMN confidence REAL DEFAULT NULL;
ALTER TABLE catalyst_events ADD COLUMN context_json TEXT DEFAULT NULL;
ALTER TABLE catalyst_events ADD COLUMN qwen_response_json TEXT DEFAULT NULL;
```

**Tests:** `tests/catalyst/test_qwen_enricher_v2.py`
- Test prompt assembly with context block included
- Test prompt assembly without context (backward compat)
- Test that `_parse()` still handles v1-format responses
- Test confidence field is extracted and bounded 0-1
- Mock Qwen API response and verify full pipeline

**Constraints:**
- Do NOT change `EnrichmentResult` dataclass (already has confidence)
- Must remain backward compatible — `enrich(headline, category, subcategory)` still works without context
- temperature=0.0 stays (deterministic)

---

### Agent 4: Dashboard (Phase E — parallel with Agents 1-3)

**Owns:** Dashboard files only

**Job:** Add catalyst event detail panel to the dashboard. Make ticker items clickable. Show enrichment context, Qwen response, and auto-generated flags.

**Files:**
- `src/driftpilot/dashboard/view_models.py` — modify `_news_ticker()`, add `_catalyst_detail(event_id)`
- `src/trading_bot/dashboard/app.py` — add `/api/catalyst/event/{id}` endpoint
- `src/trading_bot/dashboard/templates/dashboard.html` — clickable ticker, detail modal

**`_news_ticker()` changes:**
- Add `confidence`, `context_json` to the SELECT query and return payload
- Handle missing columns gracefully (v1 DBs won't have them)

**New `_catalyst_detail(event_id)` function:**
- Query single event by id with all columns including context_json, qwen_response_json
- Parse context_json and compute auto-flags:
  - `eps_beat_pct < avg_historical_surprise` → "marginal beat"
  - `revenue_beat_pct < 1.0` → "noise-level revenue"
  - `market_cap_m > 50000 and eps_beat_pct < 2.0` → "mega-cap small beat"
  - `headline_cluster_count > 2` → "stale / repeated"
  - `confidence < 0.5` → "low confidence"
  - `priority_modifier in (0.15, -0.15, 0.10, -0.10)` → "possible anchor bias"

**New `/api/catalyst/event/{id}` endpoint:**
```python
@app.get("/api/catalyst/event/{event_id}")
def catalyst_event_detail(event_id: int):
    return _catalyst_detail(event_id)
```

**Dashboard HTML changes:**
1. Ticker items get `data-event-id` attribute and click handler
2. Click opens a modal with three sections: Headline, Context, Qwen Response + Flags
3. Ticker inline display adds confidence and pm values:
   ```
   10:32  REGN  earnings/report  positive  0.82  +0.15  "REGN Q1..."
   ```
4. Color-code pm by magnitude (dark green for high, light green for low, amber for marginal)
5. Flag badges shown as colored pills below the Qwen response section

**Tests:** `tests/test_dashboard_catalyst_detail.py`
- Test `_catalyst_detail()` returns correct structure
- Test auto-flag generation for known edge cases
- Test graceful handling of events with no context_json (v1 events)
- Test `_news_ticker()` backward compat with DBs missing confidence column

**Constraints:**
- No changes to the operator loop or signal code
- Must degrade gracefully if context_json is NULL (v1 events show "enriched without context" message)
- Keep existing ticker working — enhancement only, no breaking changes
- Use existing dashboard CSS variables (var(--green), var(--red), var(--amber), var(--panel), etc.)

---

### Agent 5: Batch Re-enrichment + Validation (Phase D)

**Owns:** `scripts/enrich_catalyst_events.py` (MODIFY), `scripts/run_catalyst_signal_backtest.py` (MODIFY)

**Job:** Wire Agents 1-3 together in the enrichment script. Run batch re-enrichment of all 23,888 events. Run backtest suite. Validate edge recovery.

**Depends on:** Agents 1, 2, 3 merged and passing tests.

**Enrichment script changes:**
1. Import `ContextAssembler` and `HeadlineParser`
2. Before the Qwen enrichment loop:
   - Call `assembler.cache_run_context()` once (fetches VIX, SPY, sector ETFs)
   - Pre-cache symbol contexts for all unique symbols in batch
3. For each headline:
   - `parsed = parser.parse(headline)`
   - `context = assembler.build_context(symbol, headline, event_ts, category, subcategory, parsed=parsed)`
   - `result = await enricher.enrich(headline, category, subcategory, context=context)`
   - Write `result.sentiment`, `result.priority_modifier`, `result.confidence`, `context.to_json()`, Qwen raw response to DB
4. Add `--force-re-enrich` flag (re-processes events that already have sentiment)
5. Add `--dry-run` flag (assembles context + prints prompt, doesn't call Qwen)

**Backtest script changes:**
- Add `--min-confidence` filter (e.g., `--min-confidence 0.7`)
- Add `--min-priority-modifier` filter (e.g., `--min-priority-modifier 0.08`)
- Filters applied in the replay SQL query, not post-hoc

**Validation protocol:**
1. Back up: `cp catalyst_events_2024.sqlite3 catalyst_events_2024.sqlite3.bak-v1-prompt`
2. Schema migration: add 3 columns
3. Dry run 10 events: `--limit 10 --dry-run` — verify context block looks correct
4. Smoke run 100 events: `--limit 100 --force-re-enrich` — verify distribution has spread
5. Full re-enrichment: `--force-re-enrich --concurrency 16`
6. Distribution check: query distinct pm values, verify ≥ 15 for positive bucket
7. Backtest matrix:
   ```
   earnings_report_v1 --start 2024-10-01 --end 2024-11-30 --require-sentiment positive
   earnings_report_v1 --start 2024-10-01 --end 2024-11-30 --require-sentiment positive --min-confidence 0.7
   earnings_report_v1 --start 2024-10-01 --end 2024-11-30 --require-sentiment positive --min-confidence 0.7 --min-priority-modifier 0.08
   earnings_report_v1 --start 2024-07-01 --end 2024-12-31 --require-sentiment positive --min-confidence 0.7
   filing_8a_v1 --start 2024-07-01 --end 2024-12-31 --require-sentiment positive --min-confidence 0.7
   ```
8. Success: at least one filter combination yields edge_ratio ≥ 1.5

**Tests:** `tests/test_enrichment_pipeline_integration.py`
- Integration test: mock Qwen, real context assembler with mock data sources, verify full pipeline
- Test `--force-re-enrich` actually re-processes existing events
- Test `--dry-run` doesn't write to DB
- Test `--min-confidence` filter in backtest script

**Constraints:**
- Do NOT run full re-enrichment without backing up the DB first
- Rate limit Finnhub at 55 calls/min (leave headroom on 60/min limit)
- Rate limit YFinance at 2 calls/sec (avoid throttling)
- Log distribution summary after every 500 events
- All backtest results go to `reports/` directory

---

## Code review protocol

Each agent's PR gets reviewed against these checks before merge:

### Review checklist

1. **No side effects outside owned files** — agent only modifies files listed in its "Owns" section
2. **Tests pass** — `PYTHONPATH=src pytest tests/ -q` all green
3. **Lint clean** — `uvx ruff check` on modified files
4. **Backward compatibility** — existing enrichment pipeline still works without context (Agent 3 specifically)
5. **No hardcoded API keys** — all secrets from env vars
6. **Caching works** — verify second call for same symbol doesn't hit network (Agent 1)
7. **Graceful degradation** — missing data produces None, not crash (all agents)
8. **Dashboard still loads** — with v1 DB (no new columns), no JS errors (Agent 4)

### Merge order

```
Agent 2 (Headline Parser)     ─┐
                                ├─→ Agent 1 (Context Assembler) ─→ Agent 3 (Prompt v2) ─→ Agent 5 (Batch + Validation)
Agent 4 (Dashboard)  ──────────┘
```

- Agent 2 and Agent 4 have no dependencies — can start immediately and in parallel
- Agent 1 depends on Agent 2 (imports HeadlineParser)
- Agent 3 depends on Agent 1 (imports ContextAssembler)
- Agent 5 depends on Agents 1, 2, 3 all merged
- Agent 4 can merge independently anytime (dashboard changes are additive)

### Test matrix (run after all agents merge)

```bash
# Unit tests
PYTHONPATH=src pytest tests/catalyst/test_headline_parser.py -q
PYTHONPATH=src pytest tests/catalyst/test_context_assembler.py -q
PYTHONPATH=src pytest tests/catalyst/test_qwen_enricher_v2.py -q
PYTHONPATH=src pytest tests/test_dashboard_catalyst_detail.py -q

# Integration
PYTHONPATH=src pytest tests/test_enrichment_pipeline_integration.py -q

# Full suite (must not regress)
PYTHONPATH=src pytest -q

# Lint
uvx ruff check src/driftpilot/catalyst/ src/trading_bot/dashboard/ tests/

# Smoke: dry-run enrichment on 5 events
python scripts/enrich_catalyst_events.py --db data/driftpilot/catalyst_events_2024.sqlite3 --limit 5 --dry-run --force-re-enrich
```

---

## Open questions

1. **FMP free tier vs YFinance for bulk enrichment?** YFinance is simpler but slower and less reliable. FMP is cleaner but rate-limited on free tier.
2. **Should we parse EPS/revenue numbers ourselves or let Qwen do it with the context?** Parsing ourselves is more reliable but adds code complexity. Qwen can probably extract beat % from the headline text if told to.
3. **Do we need float for v2 or is market cap + avg volume sufficient?** Float matters for squeeze dynamics but may be overkill for a 60-minute drift signal.
4. **Should confidence gate the router directly?** e.g., RuleBasedRouter only routes if confidence ≥ 0.6. This creates a dependency between enrichment quality and routing — may be premature.
5. **Historical context for backtesting:** When re-enriching 2024 events, we need the market cap / VIX / SPY *as of that date*, not today's values. Do we have historical snapshots, or do we use current values as approximation?
