# RS-Drift v1.1

## Thesis
Stocks showing relative strength vs SPY between 9:30–10:00 ET exhibit
institutional accumulation that drifts through midday and into the close.
Late entry (10:00 AM) reduces slippage drag relative to 9:30 AM strategies.

## Parameters
See `config.py`. Notable choices:
- 5×$2k slots: concentration over diversification (KNOWN_RISKS #3)
- Mid-price limit entry: avoid spread cost (KNOWN_RISKS #1)
- 1.5% target / 0.75% stop with break-even trigger: 2:1 R:R
- EOD time stop at 15:55 ET (KNOWN_RISKS #2)
- +$125 daily profit cap (KNOWN_RISKS #4)
- SPY heat tightening on 0.5% drop in 5 min (KNOWN_RISKS #5)

## Hypothesis
2:1 reward-risk plus break-even trigger produces positive expectancy at
win rates as low as ~42% before slippage. Required actual win rate
emerges from the report's `edge_ratio` calculation.

## Verdict log

### Run 2026-05-03 — 2024 full-year baseline
- Verdict: **FAIL**
- Report: [`reports/rs_drift_v1/20260503T131306Z_fail.json`](../../../../reports/rs_drift_v1/20260503T131306Z_fail.json)
- Run: 2024-01-01 → 2024-12-31, Databento 1-min bars, full universe
- Run host: DGX (sankerkr@192.168.1.166), wall-clock 1h 16m
- Code: integration branch `refactor/driftpilot-operator` HEAD `1be81c3` (pre-Phase-G)

| Metric | Value | Required | Pass? |
|---|---|---|---|
| `edge_ratio` | **0.597** | ≥ 1.10 | ❌ FAIL |
| `actual_win_rate` | 25.07% | ≥ `breakeven_win_rate` | ❌ |
| `breakeven_win_rate` | 41.98% | — | — |
| `realized_rr` | 1.38 | (design = 2.00) | well under design |
| `realized_avg_winner_pct` | +0.57% | (target = +1.5%) | winners exit early |
| `realized_avg_loser_pct` | −0.41% | (stop = −0.75%) | break-even-stop dragging losers smaller |
| `fill_rate_pct` | 1.0 ⚠ | ≥ 0.50 | placeholder — Phase G not yet deployed; real fill rate would likely be 30–60% and would FAIL gate independently |
| `total_return_pct` | −13.60% | — | — |
| `sharpe` | −34.73 | — | — |
| `max_drawdown_pct` | −13.60% | — | dropping from start to end |
| `total_trades` | 85,363 | — | — |
| `total_pnl` (on 5×$2k = $10k) | −$135,977 | — | strategy bleeds 13.6× starting capital |

### Exit breakdown (the load-bearing diagnostic)
Of 85,363 closed positions:
- **EOD_TIME: 51%** (43,397 trades, avg P&L −0.13%, avg hold 7 min) — the dominant exit. Most positions are held through to 15:55 ET and exit near-flat. The midday-drift thesis does not materialize.
- **TIME (harness default): 22%** (19,109 trades, avg P&L −0.07%, avg hold 176 min) — positions that neither hit target nor stop nor EOD; they expire on the harness's intraday TIME stop. **See Lesson #3** — this is partly an interaction between the signal's custom `evaluate_exit` and the harness's default-rules fallthrough.
- **STOP: 18%** (15,432 trades, avg P&L −1.04%, avg hold 57 min) — the asymmetric stop side fires as designed.
- **TARGET: 9%** (7,415 trades, avg P&L +1.20%, avg hold 91 min) — when winners do hit, they slightly underperform the +1.5% target due to slippage. **The thesis works for 9% of trades.**

### Performance by regime
All three SPY regimes lose money with similar win rates:

| Regime | Trades | Win rate | PnL |
|---|---|---|---|
| GREEN | 43,548 | 24.81% | −$68,141 |
| RED | 31,662 | 24.70% | −$50,567 |
| CAUTION | 10,153 | 27.37% | −$17,270 |

The signal is **not regime-conditional** — it loses uniformly. If only one regime were unprofitable, we could route around it (Phase D AUTO_DETERMINISTIC). RS-Drift loses across all three.

## Lessons learned

1. **The 1.25% RS threshold is not predictive in 2024.** Stocks that show >1.25% relative strength vs SPY in the first 30 minutes do not reliably drift higher through the day. 51% of trades close at near-flat EOD, only 9% hit the +1.5% target. The premise of the strategy — early-morning relative strength persists into midday accumulation — does not survive 2024 measurement. Possible v2 fixes: raise the RS threshold to >2.5%; combine with a volume-confirmation filter; require positive sector breadth.

2. **The break-even trigger collapses the asymmetric R:R.** Spec design called for 2:1 (1.5% target / 0.75% stop). Realized R:R is 1.38. Why: the break-even trigger at +0.75% peak unrealized fires, then the position decays to ~0% net (or worse with SPY-heat tightening), turning would-be +1.0% winners into near-zero exits. This single mechanism shifts breakeven_win_rate from ~33% (pure 2:1) to ~42% (realized 1.38), requiring 9 percentage points more wins than the design assumed. v2 should evaluate disabling the break-even trigger and just letting target/stop run.

3. **Harness-default TIME exits leak through the signal's custom logic.** RS-Drift's spec says hold to EOD (15:55 ET). But the harness applies its default `MAX_HOLD_MINUTES` TIME stop AFTER `signal.evaluate_exit` returns no-exit. 22% of all trades exit on this default TIME stop (avg 176-min hold). Strictly, this contradicts the locked spec. v2 fix: signals with custom `evaluate_exit` should be able to declare "no default TIME stop" — either by raising `MAX_HOLD_MINUTES` per signal or by an explicit suppression flag in `ExitDecision`.

4. **Phase G (mid-price fill wiring) will likely make the verdict worse, not better.** Today fill_rate=1.0 is a placeholder. After Phase G deploys, RS-Drift will fill ~30–60% of attempts because mid-price limits in trending stocks suffer adverse selection (the spec's KNOWN_RISKS #1 prediction). The remaining filled trades are not necessarily *better* — they're just the ones that pulled back to the placement mid, which on a leg-up is where momentum was *fading*. Expect win_rate to stay near 25% while attempted_count rises, dragging fill_rate_pct < 0.50 and triggering FAIL on a second criterion.

5. **No regime gives positive expectancy.** Win rates of 24–27% across GREEN/CAUTION/RED. RS-Drift is not a candidate for the Phase D `AUTO_DETERMINISTIC` router's `operating_envelope` — there is no regime in which it outperforms break-even. It should be marked `verdict=FAIL` in the router and excluded from active rotation.

6. **The signal fires far more than designed.** 85,363 trades on 252 trading days = 339 trades/day average. With 5 slots, that's 67 turnovers per slot per day, far above the spec's intent of "stocks selected at 10:00 hold to EOD". The signal is over-firing because slot-recycling lets it re-enter symbols repeatedly; combined with the high TIME-stop / EOD-flat rate, this means transaction-cost drag dominates the result. v2 candidate fix: limit re-entries on the same symbol per session (already in the spec's KNOWN_RISKS as `MAX_TRADES_PER_SYMBOL_PER_DAY` but not enforced in the current backtest harness).

### Counterfactual analysis: would a smaller take-profit have helped?

A common (correct) intuition: "we're a bot, we should be content with 1–2%
and recycle the slot, not be greedy waiting for 1.5%." We tested this
hypothesis directly against the existing trade log without re-running
the backtest. Result: **the take-profit-at-1% variant produces a WORSE
P&L by ~$9k** ($−145,273 vs the actual $−135,977).

Why: bucketing all 85,363 trades by their peak unrealized gain during the
trade (the moment the trade was most "in the money"):

| Peak unrealized gain | % of trades | Actual avg realized |
|---|---|---|
| ≥ 1.0% | **7.6%** | +1.05% (works as designed) |
| 0.5% – 1.0% | 8.2% | +0.10% (drifted back down) |
| 0.25% – 0.5% | 7.5% | −0.22% |
| **< 0.25%** | **76.8%** | **−0.31%** |

**77% of selected stocks never gain even 0.25% during the trade.** They
drift sideways or chop down, then exit at EOD flat. There is nothing
for a take-profit to capture because nothing moves.

Capping winners at +0.9% net (a +1% take-profit minus slippage) would
shave ~$9k off the total because the 7.6% of trades that DID work
already realized +1.05% on average — the existing exit logic was already
slightly better than a 1% cap. The exit philosophy isn't the bottleneck;
the entry is.

**Lesson 7:** the bot-style "take small gains and recycle" philosophy is
correct in general, but it requires an entry signal that actually picks
stocks that move. RS-Drift picks stocks where 77% never move 0.25%. No
exit-timing logic can save a strategy whose selection is that flat. The
priority for v2 of this signal is **entry quality** (raise the RS
threshold, add volume confirmation, sector breadth), not exit philosophy.

### Recommended next steps for RS-Drift research
- (a) Deploy Phase G and re-run; capture the realistic `fill_rate_pct`. If <50%, the verdict is FAIL on two criteria and the strategy is dead at this configuration.
- (b) Sweep the RS threshold on `[1.5, 2.0, 2.5, 3.0]` to see if a more selective entry has positive expectancy.
- (c) Test the strategy with the break-even trigger DISABLED (Variant C in the locked plan). If the realized R:R recovers toward 2.0, the break-even mechanism is the primary edge-killer.
- (d) Add a sector-breadth confirmation filter (sector must also be net up vs SPY) to reduce the universe.
- (e) **Do not move RS-Drift to live paper trading until at least one of the above tests produces edge_ratio > 1.1.**
