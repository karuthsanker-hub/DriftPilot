# Day 2 (2026-05-05) — Bugs to fix tomorrow

Captured live during paper trading. Don't fix mid-session — note and move on.

## 1. All exits firing as TIME_STOP, never TARGET or STOP

**Observation:** 9 exits today, distribution: 9 × TIME_STOP, 0 × TARGET, 0 × STOP.

Exit unrealized pcts:
- LTH +11.19% (TIME_STOP) — should have fired TARGET at +1%
- ROK +1.62% (TIME_STOP) — should have fired TARGET at +1%
- MD +1.02% (TIME_STOP) — at threshold
- KKR -3.46% (TIME_STOP) — should have fired STOP at -1.5%
- MPC -2.72% (TIME_STOP) — should have fired STOP at -1.5%
- FTRE -5.61% (TIME_STOP) — should have fired STOP at -1.5%

**Root causes** (probably both):

1. Operator hung 10:23-10:38 ET on stuck Alpaca network calls (fixed mid-session with 5s/8s timeouts, but the hang already happened). During that window no exits could fire. Positions aged past 60min during the hang.

2. Monitor processes exits one-per-state-machine-cycle (~30s). With 9 positions to exit, ~4.5min total. Positions at the back of the queue age past time_stop while the front is being exited. By cycle 5, position 6 has already crossed 60min.

**Net effect today:** +$1,055 LTH winner because we held it through to time_stop instead of profit-taking at +1%. But on average this is bad: when stop_loss should fire at -1.5% and instead we wait for time_stop at -3%+, we lose 2x what we should.

**Fix for tomorrow:**
- Process exits in parallel within one monitor cycle (asyncio.gather)
- OR fire-and-forget exit submission (don't wait for fill in the monitor; reconcile next cycle)
- This caps total monitor time to ~5s (one timeout) regardless of position count

## 2. Per-symbol cap is "per active position" not "per day"

**Observation:** ROK exited at 11:02 with +$158.95. Operator IMMEDIATELY re-bought ROK at 11:02:46.

`MAX_TRADES_PER_SYMBOL_PER_DAY=1` was supposed to prevent this. But the slot allocator's duplicate-symbol check looks at OPEN positions only, not all-day trades. Once ROK closed and freed the slot, it became eligible again.

**Fix for tomorrow:** the slot allocator should check `repository.get_daily_counter(date_et, counter_name=f"trades_{symbol}")` and reject if ≥ `max_trades_per_symbol_per_day`.

## 3. Local realized P&L uses computed mid not actual fill price

**Observation:** local DB shows MXL realized -$73.26 yesterday but Alpaca actual was -$2.73. Pattern: when mid is artificial (wide spread), the computed exit_price diverges from actual fill.

**Fix for tomorrow:** after `submit_exit_order`, query Alpaca for the order's `filled_avg_price` and use that for `realized_pnl` calc. Until then, the EOD audit script should reconcile against Alpaca closed orders.

## 4. Wide bid-ask spread quote filter

**Observation:** BTSG yesterday had bid $52.94 / ask $60.96 — $8 spread on a $53 stock = 15%. Mid is meaningless. Several mid-cap signals are illiquid like this.

**Fix for tomorrow:** in REST quote provider, return None if `(ask-bid)/bid > 5%`. Broker treats None as quote_unavailable and rejects.

## 5. ALKS slot 1 re-allocation at $10K

**Observation:** ALKS allocated 304 shares at $35.11 = $10,673 — 6% over the slot_value=$10,000 budget. Slot allocator's `quantity = slot_value // ref_price` rounds down by share, but if the order fills at higher than ref_price, the actual fill exceeds slot value.

**Fix for tomorrow:** trim to `floor(slot_value × 0.95 / ref_price)` to leave headroom for slippage. Or better: use a quote-aware sizing that subtracts expected slippage.

## 6. classifier "8-a" string-vs-tuple bug still in spike

Documented earlier but worth re-flagging: `("8-a")` in the catalyst spike's filing rule is a 4-character STRING not a 1-element tuple, so iteration walks chars and matches anywhere with "8", "-", or "a". Causes filing/8a to over-match. Surfaces as ~300 noise events per day in the dashboard's catalyst-events feed but doesn't affect trades because earnings_report_v1 doesn't subscribe to filing.

**Fix for tomorrow:** when re-validating the loosened classifier (already a tomorrow task), also fix this to `("8-a",)` as a tuple — but verify edge ratios don't change first.

## 7. Operator-vs-dashboard log_level inconsistency

Dashboard server reads from a different settings load than the operator. Need to standardize the env-file path so the dashboard sees the same paper_capital, slot_value, etc. (Today: hardcoded with explicit env vars on uvicorn launch.)

## Order to fix in

1. **Fix #1 (parallel exits)** — biggest leverage. Asymmetric exits matter when N≥10 trades/day.
2. **Fix #3 (real fill prices)** — needed for correct P&L and audit accuracy.
3. **Fix #2 (per-day symbol cap)** — small impact but the user explicitly asked for it.
4. **Fix #4 (wide spread filter)** — protects against illiquid name landmines.
5. **Fix #5 (sizing headroom)** — small.
6. **Fix #7 (log/settings consistency)** — cleanup.
7. **Fix #6 (classifier tuple)** — only after re-validation.
