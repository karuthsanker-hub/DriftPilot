# Backtest Status — 2024 Full-Year Baselines

Last update: 2026-05-03 09:15 ET. Live status doc — updated as each
report lands. Source code: integration branch `refactor/driftpilot-operator`.

This doc is the single page to read when checking "are the v1 signal
backtests done, and what did they say?"

---

## Run summary

| Signal | Status | Verdict | edge_ratio | Win rate / breakeven | Trades | Total return | Report |
|---|---|---|---|---|---|---|---|
| `rs_drift_v1` | ✅ DONE | **FAIL** | 0.597 | 25.07% / 41.98% | 85,363 | −13.60% | [link](rs_drift_v1/20260503T131306Z_fail.json) |
| `whale_tail_v1` | 🔄 running | TBD | — | — | 34,588 (so far) | — | — |
| `apex_hunter_v2_2` | 🔄 running | TBD | — | — | 44,828 (so far) | — | — |
| `stationary_ghost_v1` | 🔄 running | TBD | — | — | 30,774 (so far) | — | — |

`intraday_momentum_v1` (the reference signal) had its run completed in
Phase 12 with verdict **FAIL** before this batch — see
[`expectancy_report.json`](../expectancy_report.json) at repo root.

---

## Run config (common to all 4)

- **Period:** 2024-01-01 → 2024-12-31
- **Universe:** 1,476 symbols from `config/universe.csv` (1,507 attempted, 31 unresolved at Databento — typically delisted/renamed)
- **Bar source:** Databento `EQUS.MINI` `ohlcv-1m` schema
- **Cache:** `data/bars/databento/{SYMBOL}/2024.parquet` (1.7 GB total)
- **Slippage:** `max($0.02, 0.0005 × price)` — same model in paper and backtest
- **Capital:** $10,000 paper notional per signal; per-signal slot model varies
- **Compute:** Mac M4 24 GiB (1 signal) + DGX Spark 119 GiB (3 signals); vllm Qwen-3-8B paused on DGX during run
- **Code:** integration branch `refactor/driftpilot-operator` HEAD `1be81c3`
  (pre-Phase-G — `fill_rate_pct` reads as 1.0 placeholder for any signal
  using mid-price entry; Phase G wiring lands post-this-batch)

---

## Verdict structure (read this first when looking at a report)

Every report contains these load-bearing fields:

```json
{
  "verdict": "PASS" | "GATED" | "FAIL",
  "fail_reason": "<empty string for PASS/GATED>",
  "headline_metrics": {
    "edge_ratio": <actual_win_rate / breakeven_win_rate>,
    "actual_win_rate": ...,
    "breakeven_win_rate": <1 / (1 + realized_rr)>,
    "realized_rr": <|avg_winner| / |avg_loser|>,
    "fill_rate_pct": ...,
    "give_back_ratio": ...,    // Apex Hunter only
    ...
  },
  "diagnostics": {
    "exit_breakdown_detailed": { <reason>: {count, avg_pnl_pct, avg_hold_mins} },
    "performance_by_regime": ...,
    "data_dependency_skips": [],
    ...
  }
}
```

Verdict gates per refactor plan v1.1 § Phase 4:
- `edge_ratio < 1.1` → **FAIL** (universal)
- `fill_rate_pct < 0.50` → FAIL (RS-Drift only — mid-price entry)
- `give_back_ratio < 0.40` → FAIL (Apex Hunter only — Ratchet exit)
- `1.10 ≤ edge_ratio < 1.25` → **GATED**
- `edge_ratio ≥ 1.25` → **PASS**

---

## Per-signal cards

### `rs_drift_v1` — FAIL (edge_ratio=0.597)

**One-line:** the 1.25% relative-strength-vs-SPY threshold by 10:00 ET does not predict +1.5% midday drift in 2024; only 9% of trades hit target, 73% exit at or near flat through EOD/TIME stops.

**Top 3 lessons:**
1. **RS threshold not predictive at 1.25%.** 51% of trades close at EOD with avg P&L −0.13%. Sweep candidates: 1.5% / 2.0% / 2.5% / 3.0%.
2. **Break-even trigger collapses asymmetric R:R from 2.0 → 1.38.** Winners decay back to break-even before hitting target. v2 candidate: disable break-even, let target/stop run.
3. **Harness-default TIME stop leaks through signal's custom `evaluate_exit`** — 22% of trades hit the default 45-min TIME stop, contradicting the spec which says hold to EOD. v2 harness fix: let signals declare "no default TIME stop."

Full lesson set + remediation list: [`src/driftpilot/signals/rs_drift_v1/README.md`](../src/driftpilot/signals/rs_drift_v1/README.md) § Lessons learned.

### `whale_tail_v1` — pending
ETA ~10 min. Signal fired aggressively (~34k trades by Oct 25). Equity at −$51k (~5× starting capital). Verdict pending; expected FAIL on edge_ratio.

### `apex_hunter_v2_2` — pending
ETA ~30 min (slowest — EWMLR per-cycle compute). Already at 44k trades by mid-June. Equity −$62k. Watch the Apex-specific `give_back_ratio` gate independently of edge_ratio.

### `stationary_ghost_v1` — pending
ETA ~15 min. Signal at Nov 1 already, equity −$33k. Watch for the inverted-R:R failure (spec said needs ~75% win rate).

---

## How to read each report

Six fields, in order, tell you the story:

1. **`verdict` + `fail_reason`** — the headline. If FAIL, the reason cites the failed gate.
2. **`headline_metrics.edge_ratio`** — the universal gate. < 1.1 = FAIL, 1.1–1.25 = GATED, ≥ 1.25 = PASS. Anything else is noise around this number.
3. **`headline_metrics.actual_win_rate` vs `breakeven_win_rate`** — the explanation of WHY edge_ratio is what it is. Did we win enough trades for the realized R:R?
4. **`diagnostics.exit_breakdown_detailed`** — counts + avg PnL + avg hold minutes per exit reason. This is the diagnostic load-bearer: if STOP dominates, the entry signal is wrong; if TIME/EOD dominates, the directional thesis didn't materialize; if TARGET dominates with positive avg PnL, the strategy works.
5. **`diagnostics.performance_by_regime`** — does this signal work in any regime? If yes → that regime is its `operating_envelope` for Phase D's router.
6. **Signal-specific gates** — RS-Drift's `fill_rate_pct`, Apex Hunter's `give_back_ratio`. Either can independently FAIL the run regardless of edge_ratio.

---

## Operational notes

- **Reports live under `reports/<signal_name>/<timestamp>_<verdict>.json`.** Each run writes a new timestamped file; reports are not overwritten so historical comparison is automatic.
- **Once all 4 land**, `reports/COMPARISON.md` will be generated with the cross-signal table + a recommendation for which (if any) signal proceeds to live paper trading. Per locked plan: a signal does NOT proceed to live unless `edge_ratio ≥ 1.1` AND any signal-specific gate passes.
- **Phase G mid-price fill wiring** (committed 2026-05-03) is NOT in the running code on DGX/Mac. After this batch lands, redeploy and re-run RS-Drift to capture realistic `fill_rate_pct`. The other 3 signals don't use mid-price entry so they're unaffected.

---

## Update log
- 2026-05-03 09:15 ET — RS-Drift verdict FAIL captured, lessons written, status doc created.
- (next update when whale_tail_v1 lands)
