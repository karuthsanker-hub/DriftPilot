# Research Patterns — analyzing backtests without re-running them

This is a growing catalogue of analytical patterns for getting more value
out of the backtest reports we already have, before spending hours
re-running.

The principle: every backtest report at
`reports/<signal>/<timestamp>_<verdict>.json` carries a full per-trade log
plus `peak_unrealized_pct` and entry/exit metadata on every trade. That
data answers many "what if we tweaked this?" questions without burning
another 1–2 hour run.

When a verdict comes back FAIL, the natural reflex is to think "let me
change parameter X and re-run." A surprising fraction of those tweaks can
be tested in seconds against the existing trade log. **Always test the
counterfactual against the existing data first.** Only re-run when the
counterfactual says the change might help.

---

## Pattern 1 — Take-profit counterfactual ("what if we exited earlier?")

### When to use
After any FAIL verdict where the failure was on the equity / win-rate
side, NOT on a structural gate (fill_rate / give_back_ratio).

### What it answers
- Would taking profit at +1% (instead of +1.5%) have helped?
- Would taking profit at +0.5% have helped?
- What fraction of trades ever touched a meaningful peak gain at all?

### What it CANNOT answer
- Anything about trades that DIDN'T happen (a different entry rule
  picks different stocks; we have no data for those).
- Whether a different ATR-scaled or volatility-adaptive target works
  (would need re-run with the new logic).

### How to run it

The trade rows in every report carry `peak_unrealized_pct` (the
highest unrealized return % the position saw before exiting). Bucket the
trades by peak and compute the average realized return per bucket. If
many trades have high peaks but low realized returns, exit-timing is
the bottleneck. If most trades have peak < 0.25%, the **entry signal**
is the bottleneck and no take-profit logic can save it.

```python
import json

r = json.load(open("reports/<signal>/<latest>.json"))
trades = r["trades"]

buckets = {
    "peak >= 1%":           ([], lambda p: p >= 0.01),
    "peak in [0.5%, 1%)":   ([], lambda p: 0.005 <= p < 0.01),
    "peak in [0.25%, 0.5%)":([], lambda p: 0.0025 <= p < 0.005),
    "peak < 0.25%":         ([], lambda p: p < 0.0025),
}
for t in trades:
    peak = float(t.get("peak_unrealized_pct", 0.0))
    realized = float(t.get("return_pct", 0.0))
    for name, (rows, fn) in buckets.items():
        if fn(peak):
            rows.append(realized)
            break

for name, (rows, _) in buckets.items():
    n = len(rows)
    avg = (sum(rows) / n * 100) if n else 0
    print(f"{name:<25}  count={n:>6}  pct={n/len(trades)*100:>5.1f}%  avg_realized={avg:>+6.3f}%")
```

### Read of the result

If the bottom bucket ("peak < 0.25%") holds **>50% of trades**, the
entry signal isn't producing real movers — exit tweaks won't save it.
Direct work to entry quality (raise the entry threshold, add a
confirmation filter, narrow the universe).

If the top bucket holds many trades but their `avg_realized` is
**less than the cap** you'd impose, take-profit-earlier doesn't help
either — winners are already exiting at or near their natural cap.

If the top bucket holds many trades and their `avg_realized` is
**substantially less than the peak** they touched (e.g., peak 1.2% but
realized 0.4%), THEN winners are decaying back before exit and an
earlier take-profit will help. That's when re-running with the smaller
target makes sense.

### Worked example: rs_drift_v1 (2026-05-03 run)

```
peak >= 1%             count=  6,465  pct=  7.6%  avg_realized=+1.052%
peak in [0.5%, 1%)     count=  7,012  pct=  8.2%  avg_realized=+0.101%
peak in [0.25%, 0.5%)  count=  6,368  pct=  7.5%  avg_realized=-0.223%
peak < 0.25%           count= 65,518  pct= 76.8%  avg_realized=-0.308%
```

Reading: 76.8% of trades never moved more than 0.25%. The 7.6% that
DID hit +1% were already exiting near the +1% peak (avg +1.05%
realized). A take-profit cap at +1% would shave winners and miss the
problem. Conclusion: the entry is broken; fix that before any exit
tweak.

Full record: [`src/driftpilot/signals/rs_drift_v1/README.md` § Counterfactual analysis](../src/driftpilot/signals/rs_drift_v1/README.md).

---

## Pattern 2 — Hold-time analysis ("are we holding too long or too short?")

### When to use
When `exit_breakdown_detailed` shows TIME or EOD exits dominate, AND
the average hold for those exits is high.

### What it answers
- If we cut the max hold from 45 min to 20 min, would we have left
  big winners on the table or just cut dead trades faster?

### How

Filter trades where `exit_reason` is `TIME` or `EOD_TIME`. Bin by
`hold_minutes`. For each bin, look at the realized P&L distribution.

If bin "[15, 20) min" has avg P&L ≈ 0 with most trades barely above
or below zero, those trades are dead — cutting earlier would just
recycle the slot without losing meaningful gains. If bin "[120, 180)
min" has a non-trivial number of winners that exited at +0.5% to +1%,
cutting too aggressively would discard them.

(Pattern is partially baked into `exit_breakdown_detailed.avg_hold_mins`
in the diagnostics block; richer analysis is per-trade.)

---

## Pattern 3 — Stop-distance counterfactual ("would a tighter / looser stop help?")

### When to use
When STOP exits are a meaningful slice of the breakdown and their
`avg_pnl_pct` is at the configured stop level (suggesting they're
hitting cleanly with no gap risk).

### What it answers
- If we tighten the stop from −1.5% to −1.0%, do we save losses on
  the trades that hit −1.0% but rescue trades that would have come
  back from −1.0% to break even?

### How

For every STOP-exited trade, look at its `peak_unrealized_pct` (the
high water mark before it dropped). If a non-trivial fraction of stop
exits had peaks below the proposed tighter stop level, those would have
exited under the new rule too — savings would be real. If most stop
exits had peaks WAY above the new stop level, the tighter stop would
have stopped them out before they ever got there — savings are
unrelated to the drop in stop level itself.

---

## Pattern 4 — Regime-conditional analysis ("does this work in some regimes?")

### When to use
Always, when filling out a signal's `operating_envelope` for the
Phase D AUTO_DETERMINISTIC router.

### What it answers
- Even though edge_ratio is overall < 1.1, is there a regime in which
  the signal has positive expectancy? If yes, the router can route to
  this signal during that regime only.

### How

`performance_by_regime` in the report carries per-regime trade count,
win rate, expectancy. If any regime has `win_rate > breakeven_win_rate`
*for that regime's realized R:R*, that regime joins the
`operating_envelope`.

Worked example (rs_drift_v1):
```
GREEN:    43,548 trades, win_rate 24.81%, expectancy_per_trade -$1.56
CAUTION:  10,153 trades, win_rate 27.37%, expectancy_per_trade -$1.70
RED:      31,662 trades, win_rate 24.70%, expectancy_per_trade -$1.60
```

All three regimes lose ~uniformly. RS-Drift's `operating_envelope`
is empty: it should be excluded from AUTO_DETERMINISTIC routing.

---

## Pattern 5 — Cross-signal correlation ("are our signals diversified?")

### When to use
After multiple signals' reports exist. Useful for portfolio-level
reasoning even when each signal individually FAILs.

### What it answers
- Do the signals trade the same stocks at the same time?
- Are their P&Ls correlated, or is the failure mode different per
  signal?

### How

Join trade logs by symbol + entry_at. If two signals overlap heavily,
the meta-controller has less to work with — they're effectively the
same bet. If their failure timestamps cluster (e.g., both lose money
in the same week), the cause is probably common (regime / market
event), not strategy-specific.

(Pattern is most useful once 4 reports exist. Today only RS-Drift has
landed; revisit after others land.)

---

## How to add a new pattern to this doc

1. Hit a question that smells like "I should re-run with X tweaked."
2. Stop. Look at what's already in the latest report. Can you answer
   it from the existing trade log + diagnostics?
3. If yes, write the pattern up here with the "When / What / How / Read"
   structure above. Worked example optional but valuable.
4. Commit alongside the analysis output (in the relevant signal's
   README, like rs_drift_v1's § Counterfactual analysis).

The goal: every "what if we…" reflex first checks for an existing
answer before queuing compute.
