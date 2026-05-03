# DriftPilot v3 — Requirements for Codex Handoff

**Date:** 2026-05-03
**Branch:** `main` (just pushed, commit `195cbce`)
**Validation artifact:** [reports/catalyst_horizons_midcap_2024.json](reports/catalyst_horizons_midcap_2024.json)
**Architectural plan:** [docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md](docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md)

---

## What's done (don't redo)

1. **Five-round catalyst spike** — methodology validated on 50 S&P MidCap 400 names × full 2024, 510 events, 4,800 baseline samples. The horizon-aware categorized model is the contract; do not re-run the spike.
2. **Four locked-spec technical signals** — `stationary_ghost_v1`, `whale_tail_v1`, `rs_drift_v1`, `apex_hunter_v2`. All implement the `Signal` Protocol with `scan()` and `evaluate_exit()`. Live in `src/driftpilot/signals/<name>/`. RS-Drift has a verdict (FAIL, edge_ratio=0.597); other 3 backtests are in flight on DGX.
3. **Backtest harness** — `src/driftpilot/backtest/replay.py` with mid-price fills, history-cap memory bounding, locked-spec metrics, verdict gates, baseline lookup for Step-Gate. Reusable, do not modify.
4. **Signal contract** — `src/driftpilot/signals/base.py` defines `Signal`, `Candidate`, `ExitDecision`, `BlockedReason`, `signal_data_dependencies()`, `signal_required_history_minutes()`, `typed_signal_state()`. New signals MUST conform.
5. **State machine** — `src/driftpilot/states.py` (BOOT→SCANNING→ALLOCATING→IN_POSITION→EXITING→RECYCLING). BlockedReason enum is the audit trail; extend it for new blocked reasons.
6. **Validated cells (the source of truth for v3 build):**

   | Category × Horizon | N | Ratio | v3 use |
   |---|---|---|---|
   | `earnings/report` @ 60m | 33 | 5.09× | **First signal to build** |
   | `earnings/report` @ 240m | 33 | 3.23× | Holds at 4h — wider exit OK |
   | `analyst/target_cut` @ 60m–240m | 33-34 | 2.31-2.91× | Negative filter (long-only paper) |
   | `analyst/target_raise` @ 60m | 104 | 1.42× | Long signal #2 |
   | `filing/8a` @ 60m | 256 | 2.05× | Queue prior; high volume / weak edge |

---

## What to build (in order)

### Step 1 — `src/driftpilot/catalyst/` package

Lift the news classifier out of `scripts/catalyst_horizon_spike.py` into a real package.

**Files to create:**

- `src/driftpilot/catalyst/__init__.py`
- `src/driftpilot/catalyst/classifier.py` — port the `_categorize(headline) -> (category, subcategory)` function from the spike script. Must produce the exact same labels used in the validation report, or the validated edge ratios don't apply.
- `src/driftpilot/catalyst/event.py` — `CatalystEvent` dataclass: `symbol: str, category: str, subcategory: str, ts: datetime, headline: str, source: str, horizon_buckets: list[str]`. Frozen, hashable.
- `src/driftpilot/catalyst/event_bus.py` — `CatalystEventBus` class with `subscribe(category, subcategory, callback)` and `publish(event)`. Thread-safe (use `asyncio.Lock` — the runtime is async).
- `src/driftpilot/catalyst/feed_alpaca.py` — async producer that polls Alpaca News (`alpaca-py`) every 30s, classifies, dedupes by `(symbol, ts, headline_hash)`, publishes to bus. Reuse the pagination pattern from the spike: `next_page_token`, `",".join(chunk)`.

**Tests:**
- Round-trip: feed a fixed list of 20 headlines (some I copy from the spike's article corpus) → assert each one classifies into the same category as the spike report.
- Bus: subscribe, publish, callback fires; unsubscribe; callback does NOT fire.
- Dedupe: publishing the same event twice fires the callback once.

**Acceptance:** classifier produces ≥ 95% identical labels to the validation report on the same headline corpus. (Below 95%, the validated ratios don't transfer.)

### Step 2 — `earnings_report_v1` signal (highest-edge cell)

**Path:** `src/driftpilot/signals/earnings_report_v1/`

**Thesis (from validation):** stocks with an `earnings/report` catalyst event in the last 60 minutes show 5.09× the baseline 60m absolute return. Buy on event, hold ≤ 60 minutes, exit on first 1% gain or at horizon, recycle slot.

**Files:**
- `config.py` — `EarningsReportConfig`: `max_hold_minutes=60, profit_take_pct=1.0, stop_loss_pct=1.5, max_event_age_minutes=60`. Frozen dataclass.
- `signal.py` — implements `Signal`. `scan()` queries the bus for `earnings/report` events in the last `max_event_age_minutes`; returns a `Candidate` per symbol. `evaluate_exit()` returns `ExitDecision(close=True, ...)` when:
  1. Time in trade ≥ `max_hold_minutes`, OR
  2. Unrealized pct ≥ `profit_take_pct`, OR
  3. Unrealized pct ≤ `-stop_loss_pct`
- `signal_state.py` — `EarningsReportState(TypedDict)`: `entry_ts, entry_price, peak_unrealized_pct`.
- `features.py` — `compute_age_minutes(event_ts, now)`, helper.
- `exits.py` — pure functions for the 3 exit conditions, unit-testable.
- `README.md` — thesis, parameters, validation evidence (cite the JSON), verdict-log placeholder.
- `KNOWN_RISKS.md` — at minimum: (a) earnings classifier accuracy is the load-bearing assumption, (b) Alpaca news latency could push first-bar entry past the edge window, (c) survivorship bias in the validation universe, (d) 2024 was a particular vol regime.

**Tests (`tests/signals/earnings_report_v1/`):**
- `test_signal_protocol_compliance.py` — instantiate, call `scan()`, call `evaluate_exit()`, all returns conform to types.
- `test_exit_conditions.py` — each of the 3 exits fires independently; precedence (time stop > profit take > stop loss when multiple trigger same bar).
- `test_event_age_filter.py` — events older than `max_event_age_minutes` are not returned by `scan()`.
- `test_no_event_no_candidate.py` — bus empty → `scan()` returns `[]`.

**Backtest gate before merging:** run `python -m driftpilot.backtest --signal earnings_report_v1 --start 2024-01-01 --end 2024-12-31 --universe config/universe.csv` and verify `edge_ratio ≥ 1.5` on the report. (We can be more demanding than 1.1 here because the validated cell is 5.09×; if the backtest comes back at 1.1, something is wrong with the wiring, not the edge.)

### Step 3 — `analyst_target_cut` as a NEGATIVE filter (not a signal)

This category is a strong move (2.31× @ 60m, 2.91× @ 240m) but the asymmetry skews short. On a long-only paper account, the right use is: **suppress long entries when target_cut fired in last 4 hours**.

**Implementation:** add a hook in the slot allocator (`src/driftpilot/allocator.py`) that consults the catalyst bus for `analyst/target_cut` events in the last 240 minutes on the candidate symbol. If present, allocator returns `BlockedReason.CATALYST_NEGATIVE` and the candidate is rejected.

**Add to `BlockedReason` enum** in `src/driftpilot/states.py`: `CATALYST_NEGATIVE = "catalyst_negative"`.

**Tests:**
- Allocator rejects a candidate when `target_cut` event is < 240m old; allows when > 240m or absent.
- BlockedReason enum value present in test_driftpilot_contract_freeze.

**No new signal package for this step.** It's a filter, not a signal.

### Step 4 — `analyst_target_raise_v1` long signal

Same shape as Step 2, different config: `profit_take_pct=0.8` (smaller because the cell is 1.42× not 5.09×), `max_hold_minutes=60`. Subscribes to `analyst/target_raise` only.

**Backtest gate:** `edge_ratio ≥ 1.2`. Lower bar than earnings because the validated ratio is itself lower.

### Step 5 — Universe-filter wiring for the 4 technical signals

Apex / RS-Drift / Whale-Tail / Stationary-Ghost should see a smaller, prioritized universe instead of the raw 1500-symbol list. Add a `CatalystUniverseFilter` that:

- Takes `(symbols, now) -> ranked_subset`
- Ranking: symbols with a recent positive-direction catalyst event (`earnings/report`, `analyst/target_raise`, `filing/8a`) bubble to the top
- Symbols with a recent negative catalyst (`analyst/target_cut`) are dropped entirely
- Symbols with no catalyst in last 4h: kept but ranked below catalyst-bearing names
- Wire into `src/driftpilot/state_machine.py` SCANNING state — the technical signals' `scan()` receives the filtered+ranked list, not the raw universe.

**Hard rule (from [docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md](docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md)):** the technical signals' THRESHOLDS DO NOT CHANGE based on catalyst. The catalyst layer changes WHAT they see, not HOW they decide. Apex's R² threshold is 0.35 catalyst-or-no-catalyst.

**Test:** universe is 1500 symbols, 50 have positive catalyst, 5 have negative — filtered output is exactly 1495 symbols (1500 − 5), with the 50 catalyst-bearing names at the top of the ranking.

---

## Architectural rules (do not violate)

These come from [docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md](docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md) § "Hard architectural rules":

1. **Catalyst is a UNIVERSE FILTER + QUEUE PRIORITY input. NOT an entry-rule modifier.** Don't lower thresholds based on news.
2. **Catalyst exits are BACKUP to technical exits, NOT replacements.** Price stops still run; catalyst exits are extra.
3. **No look-ahead.** Catalyst events are tagged with publish timestamp. Entry can only happen on bars AFTER the publish ts. Backtests must enforce this — replay harness already supports it via `event_ts` filtering.
4. **Long-only paper account.** target_cut is a negative filter, not a short signal.

## Locked-spec gates (for new signals' backtests)

From `src/driftpilot/backtest/report.py`:

- Universal: `edge_ratio ≥ 1.1`
- Plus signal-specific gates if applicable (see existing signals' README for examples)

A new catalyst signal that fails these gates does not ship. No exceptions.

## Where to find things

| Need | Path |
|---|---|
| Signal Protocol contract | [src/driftpilot/signals/base.py](src/driftpilot/signals/base.py) |
| BlockedReason enum | [src/driftpilot/states.py](src/driftpilot/states.py) |
| Backtest harness | [src/driftpilot/backtest/replay.py](src/driftpilot/backtest/replay.py) |
| Existing signal example | [src/driftpilot/signals/rs_drift_v1/](src/driftpilot/signals/rs_drift_v1/) |
| Spike script (reference, do not modify) | [scripts/catalyst_horizon_spike.py](scripts/catalyst_horizon_spike.py) |
| Validation report (canonical) | [reports/catalyst_horizons_midcap_2024.json](reports/catalyst_horizons_midcap_2024.json) |
| Architecture plan | [docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md](docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md) |
| Hard project rules | [AGENTS.md](AGENTS.md) |
| Doc status table | [docs/DOCS_INDEX.md](docs/DOCS_INDEX.md) |

## Environment

- Python 3.11+, async runtime (asyncio)
- Databento bars (1m, EQUS.MINI, ohlcv-1m schema) cached on local disk and DGX
- Alpaca-py for live news polling (paper account, key in `.env`)
- DGX Spark host (sankerkr@192.168.1.166) for compute-heavy backtests; `scripts/migrate_to_dgx.sh` syncs

## Definition of done for the v3 handoff

- [ ] `catalyst/` package exists with classifier, event, bus, alpaca feed
- [ ] `earnings_report_v1` signal merged with `edge_ratio ≥ 1.5` backtest verdict
- [ ] `analyst/target_cut` negative filter wired into allocator with test
- [ ] `analyst_target_raise_v1` signal merged with `edge_ratio ≥ 1.2` backtest verdict
- [ ] `CatalystUniverseFilter` wired into SCANNING state; 4 existing technical signals operate on filtered universe
- [ ] All steps land via the existing reviewer-agent + worktree pattern (see `docs/REFACTOR_PLAN_V2_LIVE_OPERATOR.md` § "agent orchestration model")
- [ ] `docs/DOCS_INDEX.md` updated for any new docs
- [ ] `reports/STATUS.md` updated as new backtests land

## What NOT to do

- Don't re-run the catalyst spike — the validation is the contract.
- Don't add LLM-based news classification yet. The deterministic regex/keyword classifier in the spike achieved the validated ratios; that's the baseline. LLM is a Phase F item from v2 plan, not v3.
- Don't lower the technical signals' thresholds when a catalyst is present. Universe filter only.
- Don't ship a catalyst signal on a cell with N < 30 in the validation report.
- Don't introduce short-side trading. Long-only paper.
