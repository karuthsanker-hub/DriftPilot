# Portfolio Controller — design note (not yet built)

**Date:** 2026-05-05
**Status:** design only; defer until v3 catalyst paper trading has 2-3 weeks of data
**Origin:** user observation while watching live paper trades on day 2 — *"is there a fund manager logic or controller missing"*

---

## The gap

DriftPilot today is **9 islands**. Each slot follows the same micro-strategy:

```
catalyst event → buy at next bar → 60min time_stop / 1% take / 1.5% stop → recycle
```

Every slot gets the same $10K. Every position uses the same exit thresholds. The only portfolio-level intervention that exists today is the slot allocator's per-sector cap and per-symbol cap.

What's missing is the layer **above** the slots — the role a real fund manager plays.

## What a fund manager does that DriftPilot doesn't

| Decision | Slot logic does | Slot logic can't |
|---|---|---|
| Position sizing | flat $10K | conviction-weighted (blowout > marginal beat) |
| Correlation | sector cap (3 per sector) | semantic: "9 names all on Q1 earnings narrative — that's one bet, not nine" |
| Regime override | RegimeDetector classifies, doesn't gate | "VIX +30% in 10 min — halt new entries" |
| Asymmetric exits | uniform 1% / −1.5% / 60min | "LTH up 5%, let it run; KKR down 1.8%, cut now" |
| Drawdown discipline | none | "day P&L −2%, reduce sizing to 0.5x" |
| Macro awareness | none | "Fed at 2pm — flatten before announcement" |
| Catalyst quality | binary positive/negative via Qwen | "EPS beat by $0.01 ≠ EPS beat by $0.50" |
| Multi-signal stacking | first match wins | "this name has beat + analyst raise + filing — rank higher" |
| Drift detection | none | "earnings_report's edge has decayed to 1.0× over last 4 weeks" |

## Architecture

A `PortfolioController` Protocol with **4 decision points**:

```python
class PortfolioController(Protocol):
    def score_candidate(
        self, candidate: Candidate,
        portfolio: PortfolioState,
        regime: RegimeSnapshot,
    ) -> ConvictionScore: ...   # 0.0 - 1.0

    def allocate(
        self, candidates: list[Candidate],
        slots_available: int,
        portfolio: PortfolioState,
        regime: RegimeSnapshot,
    ) -> list[AllocationDecision]: ...
    # AllocationDecision = {symbol, size_multiplier, reasoning, ...}

    def evaluate_exit_override(
        self, position: Position,
        base_decision: ExitDecision,
        market_state: MarketState,
    ) -> ExitDecision | None: ...   # None = no override

    def should_halt_new_entries(
        self, portfolio: PortfolioState,
        regime: RegimeSnapshot,
    ) -> tuple[bool, str | None]: ...   # (halt, reason)
```

The state machine calls the controller at:
- `SCANNING` — `score_candidate` per emitted candidate
- `ALLOCATING` — `allocate` to decide which candidates fill which slots and at what size
- `IN_POSITION` (per cycle) — `evaluate_exit_override` after the signal's base exit decision
- All states — `should_halt_new_entries` as a top-level gate

## Two implementations to build, both backtestable

### `RuleBasedPortfolioController` (default)

Codifies what's already implicit, plus what's missing:

- `score_candidate`: returns `0.5 + 0.1 * priority_modifier` (Qwen's existing field)
- `allocate`: equal-weight up to `slot_value`, respect sector + symbol cap, reject if portfolio drawdown < −2%
- `evaluate_exit_override`: returns None — no override, base signal wins (current behavior)
- `should_halt_new_entries`: True if regime is `NEWS_SHOCK` or daily PnL < −3%

**Properties:**
- Fast (<1ms per decision), deterministic, fully backtestable
- This becomes the documented baseline against which any LLM PM is measured

### `LlmPortfolioController` (experiment)

Delegates each decision to Qwen via structured JSON output:

- `score_candidate`: prompt = `(headline, sentiment, sector, current portfolio, regime)` → JSON `{conviction: 0.0-1.0, reasoning: "..."}`
- `allocate`: prompt with all candidates + portfolio → JSON `{allocations: [{symbol, size_mult: 0.5-2.0, reasoning}], rejected: [{symbol, reason}]}`
- `evaluate_exit_override`: prompt = `(position, recent price action, market state)` → JSON `{override: bool, new_target_pct?, new_stop_pct?, reasoning}`
- `should_halt_new_entries`: prompt with regime + headlines → JSON `{halt: bool, reason}`

**Properties:**
- Bounded: `size_mult` clamped to [0.5, 2.0]; can't bypass sector/symbol caps; can't violate slot_value
- Schema-validated outputs (Pydantic), 500ms timeout, fallback to rule-based on any failure
- Logged reasoning chain — every decision traceable to a Qwen prompt + response
- A/B testable: same backtest harness, swap controller, compare edge_ratio

## How to test which is better

The validation we built for v3.0 is the right machinery for this:

1. Re-run `replay_catalyst_signal` on Jul-Dec 2024 with `controller=RuleBasedPortfolioController()`
2. Re-run with `controller=LlmPortfolioController()`
3. Compare:
   - `edge_ratio`
   - Variance of returns (LLM might trade larger and have higher vol)
   - Tail behavior (LLM might cut losers earlier — important)
   - Win rate by sentiment cohort
4. If LLM beats rule-based by ≥ +0.2 edge_ratio at the same N, ship it. Otherwise document and move on.

Important: the LLM is **multiplicative** within bounds. It can size up a high-conviction trade to $20K in a $10K slot world (size_mult=2.0). It can NOT bypass the sector cap or trade a different symbol. Hard guardrails stay in the rule-based controller; LLM operates within them.

## Risks

| Risk | Mitigation |
|---|---|
| Hallucinated confidence → bad sizing | Schema-validated, hard size cap [0.5, 2.0], rule-based fallback |
| Slow → blocks trading cycle | 500ms timeout per decision, async, fallback |
| Non-deterministic → backtests don't replay | Cache LLM responses keyed on (prompt + market state hash) — backtest is replay-deterministic |
| Tail wags dog: LLM overrides the validated edge | Bounds make this impossible: LLM cannot reject within sector cap, cannot exit early without exit_override, cannot bypass halt |
| LLM "reasoning" is post-hoc justification not prediction | Track reasoning → outcome correlation. If reasoning doesn't predict, the LLM is generating noise; drop it. |
| Token cost / latency at scale | Already running Qwen3-8B locally on DGX; marginal cost is GPU time, not tokens. |

## When to actually build this

**Not yet.** Today (day 2 of paper trading) we have N≈10 trades. The MORE important thing is: **does the validated v3 strategy actually generate edge in live paper?** That's a 2-3 week question, not a 2-day question.

Build sequence I'd recommend:

1. **Now (this week)**: continue paper trading day-by-day. Capture the chain in [reports/PAPER_DAY_*.md](../reports/) for each session.
2. **Week 2**: with N≥30 trades, recompute live edge_ratio. Compare to the validated 1.105.
3. **Week 3**: if the live edge holds, build `RuleBasedPortfolioController` (mostly refactoring of what's already implicit). Re-run backtest. Should match — if it doesn't, there's a bug in the refactoring.
4. **Week 4**: build `LlmPortfolioController`. A/B test on 2024 backtest data first. Only ship to live paper if backtest shows ≥+0.2 edge_ratio improvement.
5. **Months 2-3**: paper-trade with LLM controller. Track reasoning vs outcomes. Iterate prompts.
6. **Eventually**: if everything checks out, increase capital deployment from paper to a small real-money allocation.

Each step is gated by the previous step's evidence. **The discipline is: don't add complexity until the simpler thing has measurable signal.**

## What changes in operational terms

If we ship `LlmPortfolioController`, the live operator's logs gain a "REASONING" line per decision:

```
[10:35] SCAN: 14 candidates emitted
[10:35] PM: scored — top 3: AAPL conv=0.92 "blowout EPS, raised guidance, sector tailwind"
                            MSFT conv=0.78 "in-line beat, watching for Azure call notes"
                            NVDA conv=0.65 "modest beat, earnings 2 hours old, partly priced in"
[10:35] PM: allocate — AAPL @ 1.8x ($18K), MSFT @ 1.0x ($10K), NVDA @ 0.7x ($7K)
[10:35] PM: rejected — META "correlation: already long AAPL on same narrative"
                       AMZN "regime: VIX elevated, halting low-conviction names"
[10:35] LIVE: submitting paper buy AAPL qty=82 ...
```

Every entry/exit/skip has a one-line WHY. That's the actual UX win — you can scroll the log and see what the bot was thinking, the same way you'd second-guess a junior trader.

## What we lose

Reproducibility. Today's strategy is fully deterministic — same bars + same news in = same trades out. With LLM in the loop, a re-run of the same day produces *almost* identical decisions but not bit-identical. For a regulated fund this is a problem; for an experimental paper account it's tolerable.

We also gain a debugging cost: when the LLM picks something weird, "why did it do that" is a forensic question, not a code question. Logged reasoning chains help but it's still slower to debug than `if cond: return X`.

## Recommendation

Park this design. Re-read after 2 weeks of clean paper trading. If the v3.0 strategy proves itself live (edge_ratio ≥ 1.1 over N≥50 trades), then this becomes the natural next phase. If it doesn't prove out, we don't need this — we need to fix the underlying signal first.

LLM-based portfolio reasoning is the **right answer** to "what comes after a working signal," not a substitute for one.
