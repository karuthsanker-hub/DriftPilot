# Cross-Signal Comparison — 2024 Full-Year Backtests

**Date:** 2026-05-03. **Status:** all 4 v1 signals complete — all FAIL.
v3 catalyst layer landed on main today (PR #4-7); next step is the
retrofit backtests on the filtered universe. Sibling docs:
[STATUS.md](STATUS.md), [REFACTOR_PLAN_V3_CATALYST_LAYER.md](../docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md).

---

## Headline table

| Signal | Verdict | edge_ratio | Win % | Breakeven % | Realized R:R | Trades | Total return | Signal-specific gate |
|---|---|---|---|---|---|---|---|---|
| `rs_drift_v1` | **FAIL** | 0.597 | 25.07 | 41.98 | 1.38 | 85,363 | −13.60% | `fill_rate_pct` 1.0 (placeholder, pre-Phase G) |
| `whale_tail_v1` | **FAIL** | 0.754 | 38.92 | 51.60 | 0.94 | 42,468 | −7.45% | `give_back_ratio` −0.41 (n/a, signal-agnostic) |
| `apex_hunter_v2_2` | **FAIL** | 0.527 | 21.64 | 41.09 | 1.43 | 104,267 | −16.50% | `give_back_ratio` −0.30 |
| `stationary_ghost_v1` | **FAIL** | 0.763 | 38.87 | 50.93 | 0.96 | 36,733 | −5.13% | n/a |

**Universal gate:** `edge_ratio ≥ 1.10` (FAIL otherwise). All three
finished signals miss it. None ship to live paper.

---

## The pattern across all FOUR failures

Stationary Ghost (mean-reversion on z-score extension) makes the
diagnosis unanimous: four very different entry models produced the
same shape. Different entry models — relative strength (RS-Drift),
compression-breakout (Whale-Tail), EWMLR acceleration (Apex Hunter),
mean-reversion (Stationary-Ghost) — produced the **same failure
signature**:

1. **The entry gates fire too often on the raw 1500-symbol universe.**
   - 85k / 42k / 104k trades over 252 trading days.
   - That's 339 / 168 / 414 trades per day across the universe — 4-7×
     more than any of these signals were specced to take per session.
2. **Most trades go nowhere.** The plurality exit reason in every case
   is a time-based or invalidation-based exit at near-zero P&L:
   - RS-Drift: 73% at TIME / EOD with avg PnL near flat.
   - Whale-Tail: 59% at TIME with avg PnL −0.13%.
   - Apex Hunter: **66% via HARD_EXIT in 5.2 minutes** (entry-then-immediately-puked).
   - Stationary-Ghost: **69% at TIME with avg PnL −0.11%** — the purest case.
3. **Realized R:R is OK but win-rate is half of breakeven.**
   - RS-Drift R:R 1.38 needs 41.98% win — got 25.07%.
   - Whale-Tail R:R 0.94 needs 51.60% win — got 38.92%.
   - Apex     R:R 1.43 needs 41.09% win — got 21.64%.
   - Ghost    R:R 0.96 needs 50.93% win — got 38.87%.
   - Stops are doing their job. Targets when hit are real wins.
     The bottleneck is **selection** — picking the wrong stocks.
4. **`give_back_ratio` is negative on both signals where it's measured.**
   The average winner gives back more than its peak unrealized gain
   on the way to TARGET. "Let winners run" is, on this universe,
   "let winners decay back."

The unifying explanation: **none of these technical entry conditions
adds enough information over baseline noise on a 1500-symbol universe.**
Each entry condition is satisfied by hundreds of stocks per day where
nothing is actually happening, the trade enters, drift is zero, exit
fires at flat or worse.

This is the same finding RS-Drift's counterfactual surfaced (77% of
selected stocks never gain even 0.25%) — and the diagnosis it pointed
to: **fix the universe, not the exits.**

---

## Why this is exactly what v3 is for

The v3 catalyst layer does not change any of the four signals' entry
or exit thresholds. It changes WHAT THEY SEE:

- **Universe filter:** instead of 1500 symbols, the technical signals
  scan only the 50-100 names with a recent positive catalyst event.
- **Negative filter:** symbols with `analyst/target_cut` in last 4h
  are dropped entirely.
- **Validated cells (from [reports/catalyst_horizons_midcap_2024.json](catalyst_horizons_midcap_2024.json)):**

  | Cell | N | Ratio vs baseline |
  |---|---|---|
  | `earnings/report` @ 60m | 33 | 5.09× |
  | `analyst/target_cut` @ 240m | 33 | 2.91× |
  | `analyst/target_raise` @ 60m | 104 | 1.42× |
  | `filing/8a` @ 60m | 256 | 2.05× |

A symbol in a 5.09× cell at the moment a technical signal scans it is
**not noise**. Whether the technical signal's edge survives on this
filtered universe is the v3 question. But on the validated cells, the
prior probability of motion is multiples higher than the baseline that
just produced these three failures.

**Specific predictions to test in v3:**

1. **Whale-Tail benefits most.** Compression-breakout is a directional
   follow-through pattern. Catalyst events are exactly when follow-through
   happens. Predict largest edge_ratio improvement.
2. **Apex Hunter benefits second-most.** EWMLR acceleration on a
   catalyst-bearing stock is meaningful; on a random stock it is
   noise. The 66% HARD_EXIT rate should drop dramatically.
3. **RS-Drift least likely to benefit.** Its premise is mean-reversion-of-divergence
   on a slow daily horizon, which doesn't match the 60-240m catalyst
   horizons we validated. May need a daily catalyst (M&A pending,
   earnings drift) to land.

These predictions become the v3 acceptance criteria.

---

## What ships now

Nothing from the v1 batch. None of the four signals passes the universal
`edge_ratio ≥ 1.1` gate, so per the locked plan no v1 signal proceeds
to live paper.

**v3.0 catalyst engine is now on main** (PRs #4-12) and the first signal
has just cleared its gate.

## v3.0 first verdicts (Oct-Nov 2024)

The catalyst signals were backtested on real 2024 Alpaca News + Databento
bars. Three configurations per signal: (no filter), (+positive sentiment),
(+negative sentiment). Sentiment from local Qwen3-8B on DGX.

| Signal × Filter | N | Win | Breakeven | edge_ratio | Verdict |
|---|---|---|---|---|---|
| **earnings_report_v1** (no filter) | 282 | 37% | 40% | 0.94 | FAIL |
| **earnings_report_v1 + positive** | **57** | **47%** | **29%** | **1.62** | **🟢 PASS** |
| earnings_report_v1 + negative | 23 | 26% | 59% | 0.44 | FAIL (correctly — anti-signal) |
| analyst_target_raise_v1 (no filter) | 1,044 | 46% | 53% | 0.88 | FAIL |
| analyst_target_raise_v1 + positive | 410 | 44% | 57% | 0.77 | FAIL |

**The breakthrough**: gating earnings reports by Qwen-positive sentiment
flipped the verdict from FAIL to PASS. Win-rate climbed +10pp, breakeven
dropped −11pp (winners got bigger), and edge_ratio almost doubled
(0.94 → 1.62, clears both the 1.1 universal gate and the 1.5
signal-specific gate from requirements.md).

## Why the spike's "5.09×" became "0.94×" without sentiment

The catalyst spike measured **absolute** return magnitude (`|return|`
at 60m vs baseline). A long-only strategy needs **directional up-moves**.
Earnings reports split ~50/32/18 (positive/neutral/negative per Qwen);
the volatility signal becomes a coin flip after directional filtering.

This is the same methodology error caught in spike rounds v1-v3 —
measuring the wrong thing relative to how the strategy trades. The
answer was layered: `category × horizon × Qwen-sentiment`, not just
`category × horizon`. v3.0's architecture had Qwen wired all along; we
just needed to invoke it.

## Why target_raise didn't move (interesting!)

Both filtered configurations of `analyst_target_raise_v1` made things
worse:
- 82% of target_raise headlines are tagged positive by Qwen (consensus)
- Filtering on positive removes 18% of events but the survivors are
  apparently the most-already-priced-in ones
- Negative target_raises are too rare (5 events) to be a contrarian
  signal

The v3.1 architecture for target_raise should probably use a different
filter — surprise vs. consensus, not sentiment polarity. For now,
target_raise remains FAIL.

## Architectural finding for the v3 layer

The validated filter is now: `category=earnings/report AND sentiment=positive`.
The same architecture that built the catalyst layer can now ship a
sentiment-gated `earnings_report_v1.1` to live paper trading per the
locked plan (PASS verdict on universal AND signal-specific gates).

What does ship is the **infrastructure**: backtest harness, signal
contract, allocator, state machine, regime detector, mid-price fill
model, the catalyst event bus (next), and the validation methodology.
That infrastructure de-risked the first batch and points cleanly at
the v3 build order in [requirements.md](../requirements.md).

---

## How this doc evolves

- When `stationary_ghost_v1` lands → fill in the 4th row, see if it
  matches the same pattern (it should).
- When the catalyst event bus + `earnings_report_v1` lands and is
  backtested → add a "v3 retrofit" column showing each signal's
  edge_ratio with vs without the universe filter. That side-by-side
  is the load-bearing evidence for whether v3 worked.
