# v3 Catalyst Horizon Engine — Agent Execution Plan

**Date:** 2026-05-03
**Source of truth:** [requirements.md](../requirements.md)
**Working branch model:** worktree-per-agent, reviewer-gate before merge to main.

---

## Token budget rationale

Goal: get v3.0 shipped with the **fewest agents that still parallelize**. Every spawned agent reads ~5-15 files into context (~5-15k tokens) before writing code. Spawning 20 agents to do 20 small things costs more than 9 agents doing the same work batched.

Rules of this plan:

1. **Combine related artifacts in one agent prompt** when they share the same source files (e.g. classifier + event + DB schema all reference the spike script).
2. **One reviewer agent per wave**, not per task. Reviewer reads only the diff + this plan + `requirements.md`. ~10k tokens regardless of wave size.
3. **No agent reads the giant report JSONs** (some are 80MB). The validation findings live in the doc tables — agents can quote those directly.
4. **No agent re-explores the codebase from scratch.** Each prompt includes the exact file list it should read. If an agent strays into Read-on-tangent-files, that's wasted budget.
5. **Foreground only when downstream depends on the result.** Within a wave, agents run in parallel (single message, multiple `Agent` tool calls).

Estimated total budget for v3.0: **~9 agents × ~15-20k tokens each ≈ 150k tokens** end-to-end. Plus 4 reviewer passes ≈ 40k tokens. **Total ~200k tokens** to ship v3.0 from scratch.

---

## Wave structure

```
Wave 1 (3 parallel): Foundation
  ├── Agent 1A: classifier + event + DB schema  ──┐
  ├── Agent 1B: event_bus                          ├─→ Reviewer 1 → merge to main
  └── Agent 1C: qwen_enricher                     ─┘

Wave 2 (2 parallel): Producers
  ├── Agent 2A: feed_alpaca                       ─┐
  └── Agent 2B: feed_rss + DiscoveryService       ─┴─→ Reviewer 2 → merge to main

Wave 3 (3 parallel): Signals + Negative Filter
  ├── Agent 3A: earnings_report_v1                ─┐
  ├── Agent 3B: analyst_target_raise_v1            ├─→ Reviewer 3 → merge to main
  └── Agent 3C: target_cut filter + EMERGENCY_FLUSH ┘

Wave 4 (1 + backtest): Universe Filter Retrofit
  └── Agent 4A: CatalystUniverseFilter + SCANNING wiring  → Reviewer 4 → merge → trigger 4 backtests on DGX
```

Each wave **must merge to main** before the next wave starts. This prevents agents in later waves from working off stale assumptions and lets us catch contract violations early.

---

## Wave 1 — Foundation (3 parallel agents)

### Agent 1A — `classifier + event + DB schema`

**Token budget target:** ≤ 20k tokens. 

**Files this agent MUST read:**
- `scripts/catalyst_horizon_spike.py` (~470 lines — copy classifier from here)
- `requirements.md` § sections 3, 4, 6 only
- `src/driftpilot/states.py` (to know the existing patterns)

**Files this agent MUST NOT read:**
- Any `reports/*.json`
- Any of the existing signal packages (different scope)

**Prompt:**

> Build the foundation files for the catalyst layer. Three files, scoped tight.
>
> 1. `src/driftpilot/catalyst/__init__.py` — package marker.
> 2. `src/driftpilot/catalyst/event.py` — frozen `CatalystEvent` dataclass with fields: `symbol: str, category: str, subcategory: str, pillar: Literal["micro","meso","macro","alpha"], ts: datetime, headline: str, source: str, horizon_minutes: int, headline_hash: str, sentiment: str | None = None, priority_modifier: float = 0.0`. Hashable. `__post_init__` validates `pillar in {"micro","meso","macro","alpha"}` and `horizon_minutes in {60, 240, 1440, 2880}`.
> 3. `src/driftpilot/catalyst/classifier.py` — port the `_categorize(headline) -> tuple[str, str]` function from `scripts/catalyst_horizon_spike.py`. **Copy the regex patterns and keyword lists EXACTLY.** Do not "improve" them — they produced the validated 5.09×, 2.91×, 1.42× cells. Wrap in a `class CatalystClassifier` with method `classify(headline: str) -> tuple[str, str, str]` returning `(category, subcategory, pillar)` where pillar = "micro" for all currently-validated cells.
> 4. `src/driftpilot/catalyst/db.py` — SQLite schema migration creating the `catalyst_events` table per `requirements.md` § 6. Function `init_catalyst_schema(db_path: str) -> None` and `insert_event(db_path, event: CatalystEvent) -> int`. Idempotent — running twice on the same headline is a no-op.
>
> Tests in `tests/catalyst/`:
> - `test_classifier_round_trip.py` — feed 20 headlines from the spike's article corpus (find them in catalyst_horizon_spike.py output examples or fabricate plausible ones based on the regex patterns); assert at least 19/20 produce the same `(category, subcategory)` as the spike script's `_categorize` function does directly. **Acceptance: ≥ 95% match.**
> - `test_event_validation.py` — bad pillar / bad horizon raise `ValueError`.
> - `test_db_idempotent.py` — inserting same event twice produces 1 row (UNIQUE constraint), `init_catalyst_schema` is safe to call repeatedly.
>
> Do NOT touch any existing signal package. Do NOT modify states.py yet (later wave). Output ONLY the 4 files above + their 3 test files. Run pytest on tests/catalyst/ before reporting done.

### Agent 1B — `event_bus`

**Token budget target:** ≤ 12k tokens. Smallest agent — the bus is ~50 LoC.

**Files this agent MUST read:**
- `src/driftpilot/catalyst/event.py` (after Agent 1A writes it — coordinate timing or stub the import)
- `requirements.md` § section 7 only

**Prompt:**

> Build `src/driftpilot/catalyst/event_bus.py`: an async pub/sub bus.
>
> Interface:
> - `class CatalystEventBus`
> - `async def subscribe(self, category: str | None, subcategory: str | None, callback: Callable[[CatalystEvent], Awaitable[None]]) -> SubscriptionId` — `None` for either field means "wildcard" (subscribe to all categories or all subcategories). Returns an opaque ID.
> - `async def unsubscribe(self, sub_id: SubscriptionId) -> None`
> - `async def publish(self, event: CatalystEvent) -> None` — fans out to all matching subscribers concurrently via `asyncio.gather`. Exceptions in one callback do NOT block others; log and continue.
>
> Use `asyncio.Lock` to guard the subscriber dict. Do NOT use threading.
>
> Tests in `tests/catalyst/test_event_bus.py`:
> - subscribe → publish → callback fires with the event
> - subscribe with wildcard category → all events fire it
> - unsubscribe → no fire
> - exception in one callback → other callbacks still fire (verify with two callbacks where the first raises)
> - publish with zero subscribers → no error
>
> Output ONLY `event_bus.py` + the test file. Run pytest before reporting done.

### Agent 1C — `qwen_enricher`

**Token budget target:** ≤ 15k tokens.

**Files this agent MUST read:**
- `src/driftpilot/catalyst/event.py` (for the dataclass)
- `requirements.md` § section 3
- Check if any existing http client exists: `grep -r "httpx\|aiohttp" src/driftpilot/ -l`

**Prompt:**

> Build `src/driftpilot/catalyst/qwen_enricher.py`: an async client to Qwen running on the local DGX via vllm OpenAI-compatible endpoint at `http://192.168.1.166:8000/v1`.
>
> Interface:
> - `class QwenEnricher`
> - `__init__(base_url="http://192.168.1.166:8000/v1", model="qwen", timeout_ms=500)`
> - `async def enrich(self, headline: str, category: str, subcategory: str) -> EnrichmentResult` where EnrichmentResult is a frozen dataclass with `sentiment: Literal["positive","negative","neutral"], priority_modifier: float, horizon_override: int | None`.
>
> The prompt sent to Qwen should be: "Classify this financial headline. Return JSON with keys 'sentiment' (positive/negative/neutral), 'priority_modifier' (float in [-0.2, +0.2] reflecting headline strength), 'horizon_override' (one of 60, 240, 1440, 2880 if the default category horizon should be overridden, else null). Headline: <headline>. Category: <category>/<subcategory>."
>
> CRITICAL: 500ms hard timeout. On timeout OR connection error OR malformed JSON OR Qwen returns garbage: return `EnrichmentResult(sentiment="neutral", priority_modifier=0.0, horizon_override=None)` — do NOT raise. Log the failure with the headline preview but never let the system crash because Qwen is offline. This is the contract — Qwen is enrichment, not load-bearing.
>
> Tests in `tests/catalyst/test_qwen_enricher.py`:
> - mock httpx with valid Qwen response → returns parsed values
> - mock httpx with timeout → returns defaults, no raise
> - mock httpx with malformed JSON → returns defaults, no raise
> - mock httpx with garbage values (sentiment="happy", priority_modifier=99) → returns defaults, no raise (validation rejects out-of-range)
>
> Use httpx.AsyncClient. Output ONLY `qwen_enricher.py` + the test file. Run pytest before reporting done.

### Reviewer 1

After Agents 1A/1B/1C land, single reviewer pass:

> Review the diff for v3 Wave 1 against `requirements.md` §§ 3, 4, 6, 7. Specifically check:
> 1. Classifier in `classifier.py` matches spike's `_categorize` regex/keyword logic byte-for-byte (any "improvement" is a bug — fail the review).
> 2. CatalystEvent has all 11 fields per spec; pillar/horizon validation present.
> 3. SQLite schema matches the SQL in requirements.md § 6 exactly.
> 4. Event bus uses asyncio.Lock not threading.Lock.
> 5. QwenEnricher's timeout/error fallback is unconditional — there is NO branch that lets a Qwen failure bubble up.
> 6. All 3 test files present and pytest -x passes locally.
> Report PASS / FAIL + 1-line reason.

---

## Wave 2 — Producers (2 parallel agents)

Starts only after Wave 1 merges to main.

### Agent 2A — `feed_alpaca`

**Files this agent MUST read:**
- `src/driftpilot/catalyst/event.py`, `classifier.py`, `event_bus.py`, `db.py`, `qwen_enricher.py`
- `scripts/catalyst_horizon_spike.py` (Alpaca pagination pattern only — lines around `next_page_token`)
- `requirements.md` § section 2

**Prompt:**

> Build `src/driftpilot/catalyst/feed_alpaca.py`: async producer that polls Alpaca News every 30s and publishes `CatalystEvent` objects to the bus + persists to SQLite.
>
> Interface:
> - `class AlpacaNewsFeed`
> - `__init__(api_key, api_secret, classifier, enricher, bus, db_path, poll_interval_s=30)`
> - `async def run(self) -> None` — infinite loop with cancellation support
> - `async def _poll_once(self) -> int` — returns count of new events published
>
> Pagination: use `next_page_token` per the spike script. Symbols are passed as a comma-separated string (`",".join(chunk)`), NOT a list — Alpaca-py's NewsRequest is finicky about this. Response parsing: `news_set.data["news"]` (a dict, not flat list) — same bug fix as in the spike.
>
> For each new article:
> 1. Classify via injected classifier
> 2. Skip if category is "other/generic" with subcategory "" (uncategorizable)
> 3. Compute headline_hash = `hashlib.sha256(symbol + headline).hexdigest()[:16]`
> 4. Build CatalystEvent with horizon_minutes from category default (earnings/report → 240, analyst/* → 60, filing/8a → 60, etc. — table this lookup in a constant `DEFAULT_HORIZON_BY_CATEGORY`).
> 5. Try insert into DB (UNIQUE constraint dedupes silently)
> 6. If insert succeeded: enrich via Qwen (await with timeout already in enricher) → mutate event with results → publish to bus.
>
> Tests in `tests/catalyst/test_feed_alpaca.py`:
> - mock alpaca-py with 3 articles → `_poll_once` returns 3, bus has 3 events
> - same poll twice → second returns 0 (DB dedupe)
> - one article matches "other/generic" → skipped
> - Qwen offline → events still publish with default enrichment
>
> Output ONLY the file + test. pytest before reporting.

### Agent 2B — `feed_rss + DiscoveryService`

**Files this agent MUST read:**
- All of `src/driftpilot/catalyst/` from Wave 1
- `requirements.md` § section 2

**Prompt:**

> Build TWO files:
>
> 1. `src/driftpilot/catalyst/feed_rss.py` — async producer reading RSS from Yahoo Finance, CNBC, Nasdaq. Use `feedparser` (sync, run in `asyncio.to_thread`). For each entry, attempt to extract `(symbol, headline)` — Yahoo's title format is "Headline (TICKER)"; CNBC has the symbol in the description. Use a regex `r'\b([A-Z]{1,5})\b'` against the title and validate against the universe (file `config/universe.csv`, just check membership). Skip entries where no valid ticker is found. Then same flow as feed_alpaca: classify → dedupe → enrich → publish. **Hard rule: any feedparser exception is logged + swallowed; never let an RSS scraper kill the system. Alpaca is load-bearing, RSS is additive.**
>
> 2. `src/driftpilot/catalyst/discovery_service.py` — orchestrator. Owns one AlpacaNewsFeed + one RssNewsFeed. Method `async def start(self) -> None` runs both via `asyncio.gather`, with each feed wrapped in a try/except that logs and restarts the failing feed after 60s without taking down its sibling.
>
> Tests in `tests/catalyst/`:
> - `test_feed_rss_resilience.py` — feedparser raises on first call → `_poll_once` returns 0, no exception bubbles. Second call works → events publish.
> - `test_discovery_service.py` — both feeds start; if one raises during run, the other keeps going.
>
> Read the wave-1 files but do NOT modify them. Output 2 source files + 2 test files. pytest before reporting.

### Reviewer 2

> Review v3 Wave 2 against requirements.md § 2. Specifically:
> 1. Alpaca pagination uses `next_page_token` and `",".join(chunk)` — both bug fixes from the spike.
> 2. RSS feed has try/except around feedparser; one bad feed cannot kill the service.
> 3. DiscoveryService restarts a crashed feed after 60s; healthy feed keeps running.
> 4. Both feeds use the SAME classifier and enricher instances injected from above.
> 5. Tests pass.

---

## Wave 3 — Signals + Negative Filter (3 parallel agents)

Starts only after Wave 2 merges. All three agents share the same signal-package template (look at `src/driftpilot/signals/rs_drift_v1/` as the reference shape).

### Agent 3A — `earnings_report_v1` signal

**Files this agent MUST read:**
- `src/driftpilot/signals/rs_drift_v1/` (the reference signal package — full read)
- `src/driftpilot/signals/base.py` (Signal Protocol)
- `src/driftpilot/catalyst/event_bus.py` (to subscribe)
- `requirements.md` Step 2

**Prompt:**

> Build `src/driftpilot/signals/earnings_report_v1/` matching the structure of `rs_drift_v1/`. Subscribes to the catalyst bus for `(category="earnings", subcategory="report")` events.
>
> Files: `__init__.py, config.py, signal.py, signal_state.py, features.py, exits.py, README.md, KNOWN_RISKS.md` plus `tests/signals/earnings_report_v1/` with at least: `test_signal_protocol_compliance.py, test_exit_conditions.py, test_event_age_filter.py, test_no_event_no_candidate.py`.
>
> Config defaults: `max_hold_minutes=60, profit_take_pct=1.0, stop_loss_pct=1.5, max_event_age_minutes=60`.
>
> `scan()` — return one Candidate per symbol with an active `earnings/report` event in the last `max_event_age_minutes`. The bus is injected via constructor — do NOT poll Alpaca directly.
>
> `evaluate_exit()` — close on (a) time-in-trade ≥ max_hold_minutes, (b) unrealized_pct ≥ profit_take_pct, (c) unrealized_pct ≤ -stop_loss_pct. Precedence: time stop > profit take > stop loss when same bar triggers multiple.
>
> README must cite the validation: 5.09× @ 60m, N=33, from `reports/catalyst_horizons_midcap_2024.json`.
>
> KNOWN_RISKS minimum: classifier accuracy is load-bearing, Alpaca news latency, survivorship bias in validation universe, 2024 vol regime.
>
> pytest before reporting.

### Agent 3B — `analyst_target_raise_v1` signal

> Identical shape to Agent 3A but subscribes to `(category="analyst", subcategory="target_raise")` and uses config `profit_take_pct=0.8, max_hold_minutes=60, stop_loss_pct=1.0`. README cites 1.42× @ 60m, N=104.

### Agent 3C — `target_cut` filter + `EMERGENCY_FLUSH` state

**Files this agent MUST read:**
- `src/driftpilot/states.py` (BlockedReason enum + state machine)
- `src/driftpilot/allocator.py` (slot allocator — find by `grep -l SlotAllocator src/`)
- `src/driftpilot/catalyst/event_bus.py`, `db.py`
- `requirements.md` Step 3 + § 7

**Prompt:**

> Three changes, all small:
>
> 1. **Add to `BlockedReason` enum** (`src/driftpilot/states.py`): `CATALYST_NEGATIVE = "catalyst_negative"` and `CATALYST_AGE_EXCEEDED = "catalyst_age_exceeded"`.
>
> 2. **Add `EMERGENCY_FLUSH` state** to the state machine. Cancels open orders, market-exits all positions next bar, transitions to RECYCLING. Match v2 Phase B Emergency Stop semantics — if v2 Phase B is already implemented, USE THAT STATE; do not duplicate. If not yet implemented, build it minimally here so v2's Phase B can wire its panic button to the same state later.
>
> 3. **Allocator hook** in slot allocator: before approving a candidate, query the catalyst DB (`SELECT 1 FROM catalyst_events WHERE symbol=? AND category='analyst' AND subcategory='target_cut' AND event_ts >= ? LIMIT 1`, with ts = now − 240min). If hit: return `BlockedReason.CATALYST_NEGATIVE`.
>
> 4. **State machine subscription**: at startup, subscribe to bus events `(category="analyst", subcategory="target_cut")`. On event arrival, if any open position is on that symbol, transition to EMERGENCY_FLUSH.
>
> Tests:
> - `tests/test_blocked_reason_catalyst.py` — enum values present, included in contract freeze test.
> - `tests/test_allocator_negative_filter.py` — candidate with target_cut < 240m old → blocked. With target_cut > 240m old → allowed. With no target_cut → allowed.
> - `tests/test_emergency_flush_on_target_cut.py` — held position + target_cut event arrives → state machine flushes within 1 bar.
>
> Do NOT modify any other signal package. Do NOT modify the bus. pytest before reporting.

### Reviewer 3

> Review v3 Wave 3. Check:
> 1. Both new signals match the rs_drift_v1 shape (config.py, signal.py, signal_state.py, features.py, exits.py, README, KNOWN_RISKS).
> 2. Bus subscription is via injected dependency — signals do NOT poll Alpaca directly.
> 3. EMERGENCY_FLUSH is a single state used by both v2 Phase B (operator panic) and v3 catalyst negative shield.
> 4. Allocator hook query uses parameterized SQL (no f-string injection).
> 5. README cites the validation cells with N and ratio.

---

## Wave 4 — Universe Filter Retrofit (1 agent + backtest)

### Agent 4A — `CatalystUniverseFilter` + SCANNING wiring

**Files this agent MUST read:**
- `src/driftpilot/state_machine.py`
- `src/driftpilot/catalyst/db.py`
- One existing signal's `scan()` to see the universe parameter shape
- `requirements.md` Step 5

**Prompt:**

> Build `src/driftpilot/catalyst/universe_filter.py`:
>
> `class CatalystUniverseFilter`
> - `__init__(db_path, lookback_minutes=240)`
> - `def filter_and_rank(self, symbols: list[str], now: datetime) -> list[str]`
>   - Drop any symbol with `analyst/target_cut` < lookback_minutes old
>   - Rank: positive Micro catalyst (`earnings/report`, `analyst/target_raise`, `filing/8a`) sorted ABOVE non-catalyst names
>   - Within catalyst-bearing names: stable sort by event recency (newest first)
>   - Within non-catalyst names: preserve input order
>
> Wire into `src/driftpilot/state_machine.py` SCANNING: technical signals receive the filtered+ranked list, not the raw universe. Hard rule (per requirements.md): the technical signals' THRESHOLDS DO NOT CHANGE. Only the input universe changes.
>
> Tests:
> - 1500 symbols, 50 with positive catalyst, 5 with target_cut → output has 1495 symbols, the 50 catalyst-bearing names at the top.
> - Empty input → empty output, no DB query.
> - Symbol with both positive and negative catalyst → DROPPED (negative wins).
> - DB unreachable → returns input unchanged + logs warning (graceful degradation; raw universe is at least as good as crashing).
>
> pytest before reporting.

### Reviewer 4

> Review the filter and SCANNING wiring. Confirm: technical signals' configs are NOT touched. Filter is a pre-step, not a parameter change.

### Post-merge: re-run the 4 technical-signal backtests on filtered universe

Not an agent task — script invocation:

```bash
ssh sankerkr@192.168.1.166 'cd /home/sankerkr/driftpilot && bash scripts/run_all_backtests.sh'
```

After all 4 land, update `reports/COMPARISON.md` with a "v3-retrofit edge_ratio" column. **The side-by-side is the load-bearing evidence for whether v3 worked.**

---

## Cost-control checklist before spawning each agent

Before clicking "go" on any agent in this plan:

- [ ] Agent prompt includes the explicit file list to read (no open-ended exploration).
- [ ] Agent is told NOT to read `reports/*.json` (those are 50-100MB each).
- [ ] Agent is told to run `pytest` on its own tests before reporting done — saves a round-trip if tests fail.
- [ ] Reviewer agent prompt is < 500 tokens; reviewer reads only the diff + this plan.
- [ ] Within a wave, all agents launched in a SINGLE message with multiple Agent tool calls (parallel).

## When to abort the plan

If Wave 1 produces a classifier that fails the ≥ 95% spike-match acceptance test, **stop**. Re-running on a divergent classifier means the validated edge ratios silently don't transfer. Fix the classifier before going further. This is the most likely place for the plan to break.

If any wave's reviewer returns FAIL, do NOT proceed to the next wave. Iterate the failing agent in-place — that's cheaper than running downstream agents on top of broken foundations.
