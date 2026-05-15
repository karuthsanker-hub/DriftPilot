# DriftPilot V4 Codex Tasks

> V3 tasks (1-10) complete as of 2026-05-14.
> V4 tasks start at 11. Covers slot fixes, safety hardening, EOD dilution, and learning infrastructure.

## V3 Tasks (Complete)

| Task | Description | Status |
|------|-------------|--------|
| 1 | ATR data flow | ✅ Done |
| 2 | Beta values | ✅ Done |
| 3 | BrainClient in PM Agent | ✅ Done |
| 4 | EOD reflection trigger | ✅ Done |
| 5 | /brain dashboard page | ✅ Done |
| 6 | Volume spike signal | ✅ Done |
| 7 | P&L tracking dashboard | ✅ Done |
| 8 | Dynamic bands tests | ✅ Done |
| 9 | pgvector migration | ✅ Done |
| 10 | Sector data flow | ✅ Done |

---

## V4 Tasks

| Task | Description | Priority | Status |
|------|-------------|----------|--------|
| 11 | Slot lifecycle refactor — canonical free_slot, status fixes | HIGH | ✅ Done |
| 12 | EOD liquidation — realize P&L before market close | HIGH | ✅ Done |
| 13 | Qwen fallback fix — follow_algo instead of hold | HIGH | ✅ Done |
| 14 | ATR band path — activate new ATR-based dynamic bands | HIGH | ✅ Done |
| 15 | Override rate — read actual counts from agent states | MED | ✅ Done |
| 16 | Bid-based P&L — use bid not mid for unrealized exit calc | MED | ✅ Done |
| 17 | Sector cap removal — disabled for day trading | MED | ✅ Done |
| 18 | Candidate ranking — score-first instead of rank-first | MED | ✅ Done |
| 19 | Naive timestamp fix — tolerate legacy DB timestamps | HIGH | ✅ Done |
| 20 | Boot slot refresh — reinitialize ALL slots at boot | HIGH | ✅ Done |
| 21 | Price drift reset — clear baselines after position exit | HIGH | ✅ Done |
| 22 | Lock agent safety — mechanical exits non-vetoable | HIGH | ✅ Done |
| 23 | EOD time-decay dilution (3:15 PM fade) | HIGH | ✅ Done |
| 24 | Reconcile MAX_HOLD_MINUTES to 45 across all signals | MED | ⬜ Todo |
| 25 | Fix DAILY_LOSS_LIMIT_PCT .env value | HIGH | ✅ Done |
| 26 | Shadow learning — counterfactual trade tracking | MED | ⬜ Todo |
| 27 | Update requirements.md with V4 spec | LOW | ⬜ Todo |
| 28 | RESERVED slot timeout in live operator loop | MED | ✅ Done |

---

## V4 Done — Detail

### Task 11: Slot lifecycle refactor
- Created canonical `SlotRepository.free_slot()` — single source of truth
- Fixed "available" vs "EMPTY" status mismatch across allocator and repositories
- Added "AVAILABLE" to `FREE_SLOT_STATUSES` as legacy safety net
- All slot-freeing paths now route through `free_slot()`
- Files: `storage/repositories.py`, `services.py`, `services_live.py`, `slot_allocator.py`

### Task 12: EOD liquidation
- Added `eod_liquidate_all()` to both `PaperPositionMonitor` and `LiveAlpacaPositionMonitor`
- State machine calls it before MARKET_CLOSED transition (guarded by `_eod_liquidated` flag)
- Realizes P&L on all positions, frees slots, handles protective stop cancellation
- Files: `state_machine.py`, `services.py`, `services_live.py`

### Task 13: Qwen fallback fix
- Changed `slot_exit_override.yaml` fallback_action from "hold" to "follow_algo"
- Added "follow_algo" as no-opinion in `_agent_intercept_exits()`
- Qwen timeout no longer vetoes profitable exits
- Files: `config/prompts/slot_exit_override.yaml`, `state_machine.py`

### Task 14: ATR band path activation
- Removed positional price args and legacy-triggering kwargs from `compute_dynamic_bands()` call
- Now passes proper ATR-path params: atr_pct, drift_pct, rvol, beta, catalyst, time_of_day, spread_pct
- New ATR-based bands (drift tax, RVOL boost, beta widening, catalyst widening, time-of-day stop mult, spread cost) now active in production
- Files: `services_live.py`

### Task 15: Override rate fix
- `state_machine_bridge.py` was hardcoding `override_count_today=0` and `total_decisions_today=0`
- Now reads actual counts from `orchestrator.get_agent_states()` — PM override guardrail works
- Files: `agents/state_machine_bridge.py`

### Task 16: Bid-based P&L
- Exit monitor was using mid price `(bid+ask)/2` for unrealized P&L
- Actual exits fill at `bid - offset`, overstating P&L by half the spread
- Now uses bid price for unrealized calculation; stores bid/ask/mid in position metadata
- Files: `services_live.py`

### Task 17: Sector cap removal
- `DEFAULT_MAX_SLOTS_PER_SECTOR` changed from 4 to 0 (disabled)
- Day trading rides momentum — sector balancing is 401k logic
- Configurable via `ALLOCATOR_MAX_SLOTS_PER_SECTOR` env var if needed
- Files: `slot_allocator.py`, `settings.py`, `services.py`, `services_live.py`

### Task 18: Candidate ranking
- `_ranked()` was sorting by `(rank, -score)` — scanner order beat signal quality
- Now sorts by `(-score, rank)` — highest conviction signal gets the slot
- Files: `slot_allocator.py`

### Task 19: Naive timestamp fix
- `datetime_from_storage()` crashed on naive timestamps from legacy DB writes
- Now tolerates them (assumes UTC) instead of crashing the entire allocator
- Write path (`datetime_to_storage`) remains strict — no new naive timestamps
- Files: `clock.py`

### Task 20: Boot slot refresh
- `_initialize_slots()` only created NEW slots, skipping existing ones with stale timestamps
- Now refreshes ALL non-active slots at boot with clean aware timestamps
- Reclaims stale RESERVED (>10min), cleans up excess slots beyond trade_slots
- Files: `state_machine.py`

### Task 21: Price drift baseline reset
- Drift baselines were set once at morning observation and never cleared
- After 30 minutes, every candidate blocked by >3% drift from stale morning price
- Now resets all baselines for a symbol to exit price after position closes
- Added `reset_all_baselines_for_symbol()` to `PriceDriftBaselineRepository`
- Files: `storage/repositories.py`, `services_live.py`

### Task 22: Lock agent safety
- `_agent_intercept_exits()` now treats any algo exit as authoritative
- Agent `hold` actions can no longer veto `PROFIT_TAKE`, `TRAILING_STOP`, or future algo exit reasons
- Agent early cuts still work when the algo says HOLD
- Added regression tests for non-vetoable algo exits and allowed early cuts
- Files: `state_machine.py`, `tests/agents/test_agent_state_machine_integration.py`

### Task 23: EOD time-decay dilution
- Added `apply_eod_dilution()` to paper and live position monitors
- Starting 15:15 ET, every 5-minute bucket tightens remaining stop distance by 10%
- Above-median unrealized winners lock their stop to current bid/current price
- Sectors with 4+ open slots auto-exit the least profitable position
- State machine applies dilution during regular-session cycles and keeps 16:00 `eod_liquidate_all()` as hard backstop
- Added paper and live regression tests for stop tightening and sector dilution exits
- Files: `services.py`, `services_live.py`, `state_machine.py`, `storage/repositories.py`, `tests/test_monitor_decide_execute.py`, `tests/test_services_live.py`

### Task 28: RESERVED slot timeout in live operator loop
- `_reclaim_stale_reserved_slots()` already existed and ran every cycle
- Root cause: reclaim freed slots but allocator immediately re-reserved same symbol (thrashing loop)
- Broker order fails → no position → slot stuck RESERVED → reclaim → re-reserve → repeat
- Fix: insert synthetic closed position (`FAILED_RESERVATION`) on reclaim so reentry cooldown (15 min) blocks re-reservation
- Cooldown check in `_minutes_since_last_exit()` now sees the synthetic close and rejects the candidate
- Files: `state_machine.py`

---

## V4 Todo — Detail

### Task 24: Reconcile MAX_HOLD_MINUTES
- Audit all references: settings, signals, runtime_config, hardcoded values
- Earnings signal has separate `earnings_max_hold_minutes` — align to 45
- Files: `settings.py`, `services_live.py`, signal configs

### Task 25: Fix DAILY_LOSS_LIMIT_PCT
- .env has `DAILY_LOSS_LIMIT_PCT=-2.0` (negative — silently falls back to 0.03)
- Change to `DAILY_LOSS_LIMIT_PCT=0.03` explicitly
- Verify halt logic triggers at -$300 (3% of $10k)
- Add warning log at 80% of limit
- Files: `.env`, `services.py`, `services_live.py`

### Task 26: Shadow learning — counterfactual tracking
- New: `src/driftpilot/learning/counterfactuals.py`
- Record every denied trade (allocator rejection + agent veto) with outcome
- At EOD, backfill what would have happened (fetch close prices)
- SQLite table `counterfactual_trades` — designed for future pgvector migration
- Integration: slot_allocator.py (rejections), state_machine.py (agent veto)

### Task 27: Update requirements.md with V4 spec
- Add sections: Sizing Model, Safety Logic, EOD Dilution, Config Constants, Learning

---

For architecture details, see `docs/LLD.md`.
For agent instructions, see `AGENTS.md` and `.codex/instructions.md`.
