# DriftPilot Defect Tracker

Updated: 2026-05-13

## Status Key
- FIXED = code change written, syntax checked, tests pass
- CONFIG = runtime_config.json change only (hot-reloadable)
- OPEN = not yet addressed
- WONTFIX = accepted risk or requires architectural change

---

## Defect #1 — Scanner re-emits blocked symbols every cycle
**Status: FIXED**
**Files:** `services_live.py` (CatalystScannerService._refresh_blocked_symbols, scan)
**Root cause:** Scanner sent all signal candidates to the allocator without checking if the allocator would reject them. ~5700 wasted rejections/day — same ~30 symbols quoted and rejected every 30s.
**Fix:** Added `_refresh_blocked_symbols()` pre-filter that queries the DB for symbols in active slots, at day-cap, in consecutive-loss cooldown, or in reentry cooldown. Skips them before fetching quotes.

## Defect #2 — Asymmetric risk/reward (avg loss > avg win)
**Status: CONFIG**
**File:** `data/driftpilot/runtime_config.json`
**Root cause:** `stop_loss_pct=1.5%` vs `profit_take_pct=1.0%` — losses are 50% larger than wins by design.
**Fix:** Changed `stop_loss_pct` to 1.0% (symmetric with profit_take).
**Note:** This does NOT fix the bigger slippage problem (Defect #9). A 1% software stop still leaks 2-8% on volatile names due to polling delay.

## Defect #3 — Trailing stop can never trigger
**Status: CONFIG**
**File:** `data/driftpilot/runtime_config.json`
**Root cause:** `trailing_activation_pct=1.0%`, `trailing_distance_pct=2.0%`. Distance is wider than activation — the fixed stop_loss always fires first.
**Fix:** Changed `trailing_distance_pct` to 0.4%. Once price reaches +1.0% (activation), the trail sits 0.4% below peak, locking in at least ~0.6% gain.

## Defect #4 — Sector mapping broken for reconciled positions
**Status: FIXED**
**Files:** `services_live.py` (LiveAlpacaPositionMonitor._reconcile_alpaca_to_local, _sector_map property)
**Root cause:** Monitor reconciliation used `cand.get("sector", "Unknown")` — if the slot had no candidate metadata (manual trade, prior session), sector defaulted to "Unknown", breaking sector cap enforcement.
**Fix:** Added lazy-loaded `_sector_map` property that reads `config/universe.csv`. Falls back to this when candidate metadata has no sector.

## Defect #5 — Machine-gun re-entry (same symbol re-bought within seconds of exit)
**Status: FIXED**
**Files:** `slot_allocator.py`, `runtime_config.py`, `services_live.py`, `runtime_config.json`
**Root cause:** No cooldown between closing and re-entering the same symbol. ORCL was bought 5 times in 8 minutes, all losses (-$86.32 total).
**Fix:**
  1. New `reentry_cooldown` rejection in `SlotAllocator.allocate()` — reads `min_reentry_minutes` from runtime config, checks `positions.closed_at`.
  2. Scanner pre-filter also blocks recently-exited symbols.
  3. New field `min_reentry_minutes=15` in `RuntimeConfig` dataclass + admin UI metadata.
  4. Added to `runtime_config.json`: `"min_reentry_minutes": 15`.

## Defect #6 — Zombie positions (held far beyond max_hold)
**Status: FIXED**
**File:** `services_live.py` (LiveAlpacaPositionMonitor._process_one_position)
**Root cause:** Boot-reconciled positions have no `entry_ts` in metadata. `signal.evaluate_exit()` returns `None` (can't compute hold time). Without a fallback, positions stay open forever.
**Fix:** Added FAILSAFE TIME-STOP after `evaluate_exit()`: if decision is None and `opened_at` exceeds `max_hold_minutes`, force close with reason `FAILSAFE_TIME_STOP`.

## Defect #7 — Boot reconciliation loses all position metadata
**Status: FIXED**
**Files:** `services_live.py` (LiveBrokerReconciler.reconcile_open_positions, _load_sector_map)
**Root cause:** `LiveBrokerReconciler` only passed `symbol`, `quantity`, `entry_price` to the repo. Missing `entry_ts`, `sector`, `signal_name` → signals couldn't evaluate exits.
**Fix:** Now includes `metadata` dict with `entry_ts` (now), `entry_price`, `sector` (from universe.csv), `signal_name` ("reconciled_boot"). Added `_load_sector_map()` helper.

## Defect #8 — Blocked symbols are permanent within a session
**Status: FIXED**
**File:** `services_live.py` (CatalystScannerService._refresh_blocked_symbols)
**Root cause:** `_blocked_symbols |= active` only added symbols, never removed them. Once a slot freed up, the symbol stayed blocked until midnight or restart. 3 symbols stuck on dashboard.
**Fix:** Rewrote to rebuild `blocked` set from scratch each cycle instead of accumulating.

## Defect #9 — Stop-loss slippage on volatile names (6-8% actual loss on 1% stop)
**Status: OPEN**
**Impact:** $597 lost on the 8 worst trades today. TALO lost 8.12%, JXN lost 7.97%, etc.
**Root cause:** Stop loss is a software stop evaluated on a ~30-second polling cycle. In fast movers, the price drops well past the stop between polls. The exit is a marketable limit that fills at the (already cratered) current price.
**Possible fixes:**
  1. Submit broker-side stop-loss orders at entry time (Alpaca supports this). Requires architectural change to the order flow.
  2. Increase polling frequency (currently ~30s). Needs performance testing.
  3. Exclude high-volatility names from the universe (filter by ATR or beta).
  4. Reduce position size on volatile names.
**Recommendation:** Option 1 (broker stop orders) is the correct long-term fix. Option 3 is the quickest P&L win.

## Defect #10 — analyst_target_raise_v1 is negative EV
**Status: CONFIG (disabled)**
**File:** `data/driftpilot/runtime_config.json`
**Root cause:** Signal has edge_ratio=0.85 (known negative EV from backtest). 146 trades today, net -$2.42. 13 TIME_STOP exits (held 60 min for ~$0). Occupies slots that could run profitable signals.
**Fix:** Removed from `active_signal` in runtime_config. Now running `earnings_report_v1,filing_8a_v1` only.
**Note:** 4 test failures in `tests/signals/analyst_target_raise_v1/` are pre-existing (sentiment gate + config drift). Not blocking since signal is disabled.

## Defect #11 — earnings_report_v1 bought on negative news
**Status: OPEN**
**Impact:** 4 trades, -$123.73. REZI headline: "QuickLogic Posts Downbeat Q1 Results" — clearly negative.
**Root cause:** Unclear. `require_sentiment="positive"` is set. Either:
  1. Qwen classified the headline as positive (classifier bug), or
  2. The sentiment field was null/missing and the filter passed it through.
**Investigation needed:** Query catalyst_events for REZI to check the stored sentiment value.

## Defect #12 — _first_seen_prices drift cache resets on operator restart
**Status: OPEN**
**Impact:** After a restart, the price drift filter starts fresh. A symbol that drifted 8% from its catalyst price gets a new first-seen price at the current (already-drifted) level, bypassing the 3% max_price_drift check.
**Root cause:** `_first_seen_prices` is an in-memory dict. Operator restarted 6+ times today (boot transitions at 02:51, 02:52, 02:55, 02:57, 13:53, 14:16, 14:27, 14:36, 15:15, 17:04).
**Fix:** Persist `_first_seen_prices` to SQLite or load initial prices from the catalyst_events table at startup.

## Feature: PM Analyst (automated issue detection)
**Status: SHIPPED**
**Files:** `agents/pm_analyst.py`, `agents/orchestrator.py`, `agents/factory.py`, `dashboard/app.py`, `dashboard/templates/dashboard.html`
**What it does:** Every 15 minutes, builds a trading snapshot (P&L, per-symbol stats, stuck positions, rapid re-entries, signal breakdown) and sends to Qwen for structured analysis. Results displayed in dashboard as PM briefing with severity-coded issues, signal verdicts, risk level badge. Falls back to deterministic analysis if Qwen is down. Has "ANALYZE NOW" manual trigger button.
**Endpoints:** `GET /api/operator/pm-analysis`, `POST /api/operator/pm-analysis/run`, `GET /api/operator/pm-analysis/history`

---

## Priority for Next Session

### P0 — Must fix before next trading day
1. **Defect #9 (stop slippage):** Add ATR/beta filter to universe or reduce slot_value for volatile names. Broker stop orders are the real fix but need more work.
2. **Defect #11 (sentiment misclassification):** Investigate REZI in catalyst_events DB. If Qwen classified it wrong, tighten the classifier. If sentiment was null, add null-rejection to the signal.
3. **Defect #12 (drift cache reset):** Persist first-seen prices to DB or bootstrap from catalyst_events at startup.

### P1 — Important but not urgent
4. **Test suite:** Fix the 4 pre-existing analyst_target_raise_v1 test failures (events missing sentiment field in test setup).
5. **Entry price accuracy:** Verify that `get_fill_price()` returns actual fill, not order limit. If it's failing silently, P&L calculation is wrong.
6. **max_trades_per_symbol_per_day=5 is too high.** Today's data shows symbols like ORCL, TXN losing money on every single trade (5/5 losses). Consider lowering to 3 or adding a "stop after 2 consecutive losses on same symbol" rule.

### P2 — Nice to have
7. **Dashboard:** Add the drift cache state to diagnostics panel so you can see which symbols have drifted.
8. **Operator stability:** 10+ restarts in one day. Investigate why the operator keeps crashing/restarting.
