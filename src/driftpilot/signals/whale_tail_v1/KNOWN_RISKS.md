# Whale-Tail v1.1 — Known Risks

## 1. Distribution Trap (partially mitigated)
High RVOL inside a tight range can be institutional DISTRIBUTION rather
than accumulation — the opposite signal with the same footprint. We
mitigate by rejecting setups whose recent (last 5 min) closes broke below
the 15-bar compression low, but distribution that holds the floor until
the moment of break is indistinguishable from accumulation in our
features. Track loss tail in the report for evidence of this failure mode.

## 2. Microstructure noise on Variant B entry
Variant B (marketable limit at compression high + slippage) crosses the
spread by design. On illiquid names this consumes more of the expected
edge than the modeled `max($0.02, 0.0005 * price)` slippage. v2 should
benchmark Variant A (passive fill at compression high) for selection bias
vs cost.

## 3. ATR-scaled exits and volatility regime
Target/stop scale with ATR_at_entry, so the dollar R varies across
regimes. In an expanding-volatility intraday regime, late entries inherit
inflated ATR and therefore wider stops — a single loss may dominate the
session. Track variance of per-trade R in the verdict log.

## 4. RANGE_POSITION_THRESHOLD unvalidated
0.75 was chosen as a reasonable upper-band cutoff but is a knob. Do NOT
optimize within a single backtest dataset — that is overfitting. Sweep
deferred to v2.

## 5. TIME_STOP_MINUTES unvalidated
60 minutes is reasonable for an intraday absorption thesis but unverified
empirically. v2 should sweep [30, 60, 90].

## 6. Capacity Mirage
Backtest fills assume infinite liquidity at the bar's modeled spread.
Real Whale-Tail entries by definition target stocks where institutions
are trading; if our paper notional is large relative to per-bar volume,
slippage will exceed the model. Capped at $1000 SLOT_NOTIONAL in v1 to
mitigate.

## 7. Sample-Size Risk
RVOL > 3.0 + compression_score < 0.5 + range_position > 0.75 is a
narrow filter; expect few setups per session per universe. Verdict
confidence requires careful trade-count thresholds before declaring
profitability.

## 8. ATR fixture validation — TradingView cross-check pending
ATR is implemented per Wilder's 1978 recurrence and validated against a
hand-computed fixture and invariants. A TradingView (or other reference
platform) cross-check is pending; user owns final verification before
merge per spec.
