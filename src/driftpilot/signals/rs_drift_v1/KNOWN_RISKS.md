# RS-Drift v1.1 — Known Risks

These concerns are documented intentionally and tested as-specified.
If v1 backtest fails, these become candidate fixes for v2.

## 1. Mid-price limit entry adverse selection
Mid-price limits get adverse selection: filled when trend reverses,
unfilled when trend continues. RS-Drift's entry condition (above ORH
with positive RS) means the stock is actively trending up at the
moment of order placement. A mid-price limit is BELOW the current
ask. It will only fill when the price ticks down — which means the
move is fading.

Mitigation: `fill_rate_pct` is a verdict gate. If <50%, FAIL. Variant A
(marketable limit) deferred until baseline justifies it. v1 harness may
not yet implement true mid-price-fill simulation; flagged as a deferred
harness concern.

## 2. EOD time stop with intraday entry (5+ hour holds)
Holding from 10:30 AM to 15:55 PM means 5+ hour holds in a strategy
that thinks of itself as intraday. The thesis (institutional drift
continues through midday) may be sound for the first 2-3 hours and
noise after. Track `average_hold_minutes` and exit-reason distribution
by time bucket. If most TARGET exits happen before 13:00 ET, the EOD
time stop is holding stale positions for no reason. Shorter time stop
(13:30) deferred to v2.

## 3. 5×$2k slots vs 10×$1k
Concentration cuts both ways. Fewer trades = less slippage drag, more
sample-size variance. With 5 slots and ~250 trading days, expect
roughly 800-1500 entry attempts depending on fill rate. If
fill_rate × 800 < 500 trades, statistical confidence on win rate is
weak.

## 4. +$125 daily profit cap (asymmetric)
Caps the day's winners while letting losers run to -$100. Asymmetric
exit on aggregate P&L destroys edge over time IF the trades after
+$125 are net positive on average.
`daily_circuit_breaker_diagnostics.avg_remaining_session_pnl_after_profit_cap`
is the diagnostic. If positive, the cap is leaving money on the table.

## 5. SPY heat tightening
0.25% effective stop on a position that may already have moved more
than 0.25% from entry. Heat trigger could cause immediate stop-out on
positions that would have recovered.

## 6. ORH + post-10:00 VWAP filter overlap
Both filters demand "stock is currently trending up at 10:00." May be
redundant. Variant with VWAP filter disabled deferred to v2.

## 7. Sector cap is allocator-side
Sector cap was added in v1.1 as defensive standardization. NOT in
original RS-Drift design. Track `sector_cap_reached` count in the report.

## 8. ADV lookahead-bias
`adv_20day(daily_bars, current_date)` MUST exclude `current_date` from
the average. Including it is silent lookahead bias. Enforced by
`tests/signals/rs_drift_v1/test_adv_excludes_current_date.py`.

## 9. Mid-price fill simulation deferred
The harness currently models entry fills via the standard slippage
formula `max($0.02, 0.0005 * price)`. True mid-price-limit simulation
(distinguishing filled from missed signals) is a harness change beyond
this signal's scope. v1 reports may overstate fill rate as a result.
