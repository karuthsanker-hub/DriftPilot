# Signal Router + Portfolio Manager — Requirements

**Date:** 2026-05-08  
**Status:** REQUIREMENTS (not built)  
**Prerequisite:** New Qwen prompt deployed (directional price prediction). Weekend backtest re-validation pending.

---

## The problem in one paragraph

We have 7 signals and 9 catalyst categories but no intelligence deciding **which signal to apply to which opportunity**. Today: `MultiSignal` fans out to all active signals and takes whatever candidates come back. The earnings_report_v1 signal sees `earnings/report` events and ignores everything else. Whale_tail sees nothing because it needs bar data, not catalyst events. There's no layer that says "this is a compression-breakout setup on a post-earnings stock — route it to whale_tail with a 60-minute horizon." And there's no layer that says "we already have 3 momentum longs, this 4th one adds correlation risk — skip it."

Two new components fix this:

1. **Signal Router** — given a catalyst event + current market data, pick the right selection algorithm
2. **Portfolio Manager** — given the full portfolio state, optimize entry/exit decisions for total P&L

---

## Part 1: Signal Router

### What it does

The Signal Router sits between the catalyst event bus and the signal registry. When a catalyst event arrives, the router:

1. Looks at the event `(category, subcategory, sentiment, confidence, priority_modifier)`
2. Looks at current market context (regime, VIX proxy, time of day, symbol's recent price action)
3. Decides which signal algorithm(s) to evaluate for this symbol
4. Passes the symbol to those signals with the appropriate horizon

### The thesis matrix

Based on backtests, catalyst validation data, and signal design, here's the mapping:

#### Catalyst → Signal routing rules

| Catalyst Event | Sentiment | Best Signal | Thesis | Horizon |
|---|---|---|---|---|
| `earnings/report` | positive, conf ≥ 0.7 | `earnings_report_v1` | Validated cell: 5.09× @ 60m. Pure drift play — don't need technicals. | 60m |
| `earnings/report` | positive, conf ≥ 0.7 | `whale_tail_v1` (secondary) | Post-earnings compression + high RVOL = institutional absorption. Whale-tail's thesis is STRONGEST here. Check if RVOL > 3.0 and compression present. | 60m |
| `earnings/report` | negative, conf ≥ 0.7 | BLOCK (negative filter) | Validated anti-signal: 0.64× edge ratio. Do NOT go long. | — |
| `analyst/target_raise` | positive, conf ≥ 0.8 | `apex_hunter_v2` | Target raise + EWMLR acceleration = institutional accumulation confirmed by analyst. HARD_EXIT rate should drop on catalyst-quality names. | 60m |
| `analyst/target_raise` | neutral | SKIP | 82% are already positive — neutral on a target_raise means Qwen isn't sure → no edge. | — |
| `analyst/target_cut` | any | BLOCK all longs | Validated: 2.91× absolute move @ 240m. Asymmetric downside. Block the symbol from all long signals for 4 hours. | 240m block |
| `filing/8a` | positive, conf ≥ 0.7 | `filing_8a_v1` | Validated cell: 2.05× @ 60m, N=256. Largest sample. Direct catalyst signal. | 60m |
| `filing/8a` | positive, conf ≥ 0.7 | `stationary_ghost_v1` (secondary) | If the stock pulled back after a positive filing and is now 2.5σ below mean with ADX < 20, mean reversion is a high-conviction play. | 20m |
| `m_and_a/acquires` | positive | `rs_drift_v1` | Acquisition news creates multi-day RS vs SPY — the ONE catalyst that matches RS-Drift's daily thesis. | 1day |
| `earnings/guidance_up` | positive | `earnings_report_v1` | Treat like earnings/report — raised guidance is the strongest positive signal. | 60m |
| `earnings/guidance_down` | any | BLOCK | Guidance down = BLOCK long entries for 4 hours, same as target_cut. | 240m block |
| `analyst/upgrade` | positive | `apex_hunter_v2` | Upgrade + EWMLR acceleration = institutional buying confirmed. | 60m |
| `analyst/downgrade` | any | BLOCK | Confirmed anti-signal (0.41× @ 1day). Block for 1 day. | 1day block |

#### Regime modifiers

| Regime | Modification |
|---|---|
| GREEN (SPY trending up) | All signals eligible. Prefer momentum (apex_hunter, whale_tail). |
| CAUTION (SPY flat/choppy) | Prefer mean-reversion (stationary_ghost). Reduce apex_hunter position sizes. |
| RED (SPY trending down) | Only earnings_report_v1 (catalyst-pure, regime-independent in backtest). Block all technical signals. |

#### Time-of-day routing

| Time (ET) | Eligible Signals | Why |
|---|---|---|
| 09:30–10:00 | earnings_report_v1 only | Opening volatility — technical signals' indicators aren't stable yet. |
| 10:00–10:30 | + rs_drift_v1 | RS-Drift's scan window opens (needs 30-min RS calculation). |
| 10:00–15:00 | + whale_tail_v1, stationary_ghost_v1 | Technical signals' full scan windows. |
| 10:30–14:30 | + apex_hunter_v2 | Apex's scan window (needs 90-min EWMLR warm-up). |
| 14:30–15:45 | earnings_report_v1, filing_8a_v1 only | Too late for technical signals to build full positions. Catalyst-only. |
| 15:45–16:00 | NO new entries | Exit-only period. |

### Signal Router protocol

```python
@dataclass(frozen=True)
class RoutingDecision:
    signal_name: str
    horizon_minutes: int
    conviction: float           # 0.0–1.0 from catalyst + context
    position_size_mult: float   # 0.5–2.0 (default 1.0)
    metadata: dict[str, Any]    # routing reasoning for audit trail

class SignalRouter(Protocol):
    def route(
        self,
        event: CatalystEvent,
        regime: RegimeSnapshot,
        time_et: datetime,
        portfolio_state: PortfolioSnapshot,
    ) -> list[RoutingDecision]:
        """Return 0-N routing decisions. Empty = skip this event."""
        ...
```

### Implementation: two tiers

**Tier 1: RuleBasedRouter** (build first, backtest-friendly)
- Hard-coded lookup table from the thesis matrix above
- Deterministic — same input always produces same output
- Backtestable against the full 2024 Databento + catalyst DB
- No LLM calls — runs in microseconds

**Tier 2: LlmAssistedRouter** (build after Tier 1 proves out)
- Uses Qwen3-8B to evaluate edge cases not in the lookup table
- Prompt: "Given this catalyst event, current regime, and the portfolio state, which of these N signals has the highest expected value for a 60-minute trade?"
- Bounded: can only select from signals in the registry, can only adjust `position_size_mult` in [0.5, 2.0]
- Falls back to RuleBasedRouter on timeout/error
- Logged reasoning chain for post-hoc analysis

---

## Part 2: Portfolio Manager

### What it does

The Portfolio Manager sits above the Signal Router and the slot allocator. It sees the **full portfolio** — all open positions, their P&L, their correlation, the daily P&L, the regime — and makes four decisions:

1. **Score candidates** — given N candidates from the router, re-rank by portfolio fit (not just signal score)
2. **Size positions** — adjust slot value based on conviction and portfolio risk
3. **Override exits** — tighten or loosen exits based on portfolio-level context
4. **Halt entries** — emergency brake when portfolio-level risk limits are hit

### Decision 1: Score candidates by portfolio fit

The Signal Router says "buy AAPL, MSFT, GOOG with whale_tail." The Portfolio Manager asks:

- **Correlation check:** Are AAPL, MSFT, GOOG all mega-cap tech? If we already hold NVDA, adding 3 more tech names is correlated risk. Downrank MSFT and GOOG, keep AAPL (highest catalyst conviction).
- **Sector concentration:** We have 3/10 slots in tech already. Sector cap says max 3. Only 0 slots available for tech. Route the tech candidates to the blocked queue with `SECTOR_CAP_REACHED`.
- **Catalyst freshness:** AAPL's event is 15 minutes old (hot). GOOG's is 200 minutes old (stale). Uprank AAPL.
- **Momentum alignment:** If AAPL is already up 3% today on this catalyst, the easy money is gone. Downrank.
- **Regime fit:** GREEN regime + momentum signal = full conviction. RED regime + momentum signal = reduce size to 0.5×.

Scoring formula:

```
portfolio_score = (
    signal_score                          # from signal's scan()
    × catalyst_freshness_decay            # exp(-age / half_life)
    × regime_alignment                    # 1.0 if signal matches regime, 0.5 if not
    × (1.0 - sector_concentration_pct)    # penalize concentrated sectors
    × (1.0 - correlation_to_portfolio)    # penalize correlated positions
    × conviction_from_qwen               # 0.0–1.0 from enrichment confidence
)
```

### Decision 2: Size positions dynamically

Instead of fixed $1,000 per slot, the Portfolio Manager adjusts:

| Condition | Size multiplier | Rationale |
|---|---|---|
| High conviction (score > 0.8) + GREEN regime | 1.5× | Best-case alignment. More capital on high-conviction. |
| Moderate conviction (0.5–0.8) | 1.0× | Default. |
| Low conviction (< 0.5) but catalyst is fresh | 0.75× | Take the trade but smaller — test the thesis. |
| RED regime, any conviction | 0.5× | Capital preservation. Halve all positions. |
| Portfolio already at +2% today | 0.75× | Protect the day's gains. Reduce new exposure. |
| Portfolio already at −2% today | 0.5× | Defensive. Smaller positions, tighter stops. |
| Daily trade count > 30 | 0.5× | Churning guard — slow down. |

**Hard bounds:** `size_mult ∈ [0.5, 2.0]`. Never exceed 2× the base slot value.

### Decision 3: Override exits (portfolio-level)

The signal says "hold — not at target or stop yet." The Portfolio Manager can override:

| Portfolio condition | Exit override | Rationale |
|---|---|---|
| Portfolio P&L hits +3% today | Tighten all stops to break-even | Lock in the day. Don't give back a great day. |
| Portfolio P&L hits −3% today | Market-exit all positions | Daily loss limit. Hard cut. Already in the allocator but PM enforces too. |
| Single position > +2% unrealized | Activate trailing stop (if not already) | Don't let a big winner become a loser. |
| Single position holding through negative catalyst on same symbol | Immediate exit | Target_cut published on a symbol we hold → exit now, don't wait for stop. |
| 2+ positions in same sector, sector starts dropping | Exit weakest position | Reduce correlation damage. |
| Time > 15:30 ET, position still open | Tighten time stop to 15:45 | Don't hold into close. |

### Decision 4: Halt entries

| Condition | Action |
|---|---|
| Daily realized P&L < −3% of equity | HALT all new entries. Exits only. |
| 8+ of 10 slots filled | Raise conviction threshold to 0.8 (only high-conviction gets the last 2 slots). |
| VIX proxy (SPY 5-min ATR) > 2× 20-day average | HALT momentum signals. Only catalyst-pure signals (earnings_report, filing_8a). |
| 3 consecutive stop-losses in last hour | HALT for 30 minutes. Cool-down. |

### Portfolio Manager protocol

```python
@dataclass(frozen=True)
class PortfolioSnapshot:
    open_positions: list[PositionRecord]
    slots: list[SlotRecord]
    daily_realized_pnl: float
    daily_trade_count: int
    equity: float
    regime: RegimeSnapshot
    sector_exposure: dict[str, int]        # sector → number of open slots
    correlation_matrix: dict[str, float]   # symbol → avg correlation to portfolio

@dataclass(frozen=True)
class ConvictionScore:
    candidate: AllocationCandidate
    portfolio_score: float          # re-ranked score
    size_mult: float                # position sizing multiplier
    reasoning: str                  # human-readable explanation

class PortfolioManager(Protocol):
    def score_candidates(
        self,
        candidates: list[AllocationCandidate],
        portfolio: PortfolioSnapshot,
    ) -> list[ConvictionScore]:
        """Re-rank and size candidates by portfolio fit."""
        ...

    def evaluate_exit_override(
        self,
        position: PositionRecord,
        signal_decision: ExitDecision | None,
        portfolio: PortfolioSnapshot,
    ) -> ExitDecision | None:
        """Override or confirm the signal's exit decision. None = defer to signal."""
        ...

    def should_halt_entries(
        self,
        portfolio: PortfolioSnapshot,
    ) -> tuple[bool, str | None]:
        """Should we stop taking new positions? Returns (halt, reason)."""
        ...
```

---

## Part 3: How it all fits together

### Current architecture (flat)

```
CatalystEventBus → MultiSignal.scan() → SlotAllocator → Broker
                                              ↓
                               MultiSignal.evaluate_exit() → Broker
```

### New architecture (layered)

```
CatalystEventBus
       ↓
  Signal Router
  "which algo for this event?"
       ↓
  [whale_tail, apex_hunter, earnings_report, ...]
  each evaluates the candidate using its own logic
       ↓
  Portfolio Manager
  "re-rank by portfolio fit, size, halt check"
       ↓
  SlotAllocator (unchanged — enforces hard caps)
       ↓
  Broker

  === Exit path ===

  Position Monitor polls each position
       ↓
  Signal.evaluate_exit() (per-position, routed by signal_name in metadata)
       ↓
  Portfolio Manager.evaluate_exit_override()
  "portfolio-level tighten/loosen?"
       ↓
  Broker
```

### Key design constraints

1. **Signal code is unchanged.** Signals don't know about the router or PM. They implement `scan()` and `evaluate_exit()` as before. The router just decides which signals to call and what universe they see.

2. **SlotAllocator is unchanged.** Hard caps (sector, symbol, daily) stay in the allocator. The PM operates above the allocator — it re-ranks and sizes, then the allocator enforces hard limits.

3. **Same code in live and backtest.** The router and PM must be backtestable. The RuleBasedRouter and RuleBasedPortfolioManager are pure functions of state — no LLM calls, no side effects.

4. **LLM is optional and bounded.** The LlmAssistedRouter and LlmPortfolioManager are wrappers around the rule-based versions. They can adjust scores by ±50% and size by [0.5, 2.0]. They cannot bypass hard caps. They have 500ms timeouts with rule-based fallback.

5. **Audit trail.** Every routing decision and PM override is logged with reasoning. Post-hoc analysis can answer "why did we buy AAPL with whale_tail instead of earnings_report?"

---

## Build order

### Phase 1: RuleBasedRouter (1–2 days)
- Implement the thesis matrix as a lookup table
- Wire into operator loop between catalyst bus and signals
- Unit tests: given (event, regime, time), assert correct signal(s) selected
- **Gating criterion:** backtest the router on Jul-Dec 2024 catalyst + bar data. Compare edge_ratio of routed trades vs unrouted (current MultiSignal behavior).

### Phase 2: RuleBasedPortfolioManager (2–3 days)
- Implement scoring formula, sizing, halt logic
- Wire into operator loop between signals and allocator
- Exit override wiring in position monitor
- Unit tests for each decision point
- **Gating criterion:** backtest PM-managed portfolio vs un-managed. Key metric: max drawdown reduction and Sharpe improvement, not just edge_ratio.

### Phase 3: Technical signal integration (1–2 days)
- Wire whale_tail_v1 and apex_hunter_v2 to accept catalyst-filtered universes
- The router passes `(symbol, catalyst_event)` to the technical signal; the signal checks its own technical conditions (RVOL, compression, EWMLR) on just that symbol
- This is the v3 retrofit — technical signals on catalyst-filtered universe

### Phase 4: LLM-assisted tier (after Phase 1–3 prove out in paper)
- LlmAssistedRouter: Qwen evaluates edge cases
- LlmPortfolioManager: Qwen adjusts scores and sizing
- A/B framework: run rule-based and LLM-assisted in parallel, compare decisions, log divergences
- Promote LLM tier only if it demonstrably improves Sharpe over rule-based

### Phase 5: Paper validation (2–3 weeks)
- Run the full stack in paper-live mode
- Daily EOD analysis comparing routed vs unrouted performance
- Weekly review of PM overrides — were the halts and tightenings correct?
- Promotion to live only after the full live deploy gate passes

---

## Success metrics

| Metric | Current (MultiSignal flat) | Target (Router + PM) |
|---|---|---|
| Edge ratio | 1.105 (earnings only) | ≥ 1.15 (blended across routed signals) |
| Daily trade count | 3–15 (earnings-dependent) | 10–30 (multi-signal, multi-catalyst) |
| Max drawdown (day) | −1.05% (Day 2, bug-driven) | < −1.5% (PM halt at −3%) |
| Win rate | 44% (earnings + positive sentiment) | ≥ 42% (blended, lower bar but more trades) |
| Sector concentration | No limit (all slots could be same sector) | Max 3 slots per sector (PM-enforced) |
| Correlation risk | Not measured | PM penalizes correlated additions |
| Signal utilization | 1 of 7 signals active | 3–5 signals active, routed by catalyst |

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Router adds complexity but no edge | Phase 1 gated on backtest. If routed trades don't beat unrouted, don't ship. |
| PM over-manages and kills winners | PM can only TIGHTEN stops, not LOOSEN. Can only reduce size, not increase beyond 2×. |
| LLM hallucination in router/PM | LLM tier is bounded and falls back to rule-based. Cannot bypass hard caps. |
| Technical signals still FAIL on catalyst universe | Phase 3 gated on backtest. If whale_tail on catalyst-filtered universe doesn't beat 1.1 edge_ratio, don't route to it. |
| Backtest overfitting (too many routing rules) | Keep Phase 1 to ≤ 12 routing rules (the thesis matrix). No parameter sweeps. |
| Cold-start on a new trading day | Router uses bootstrapped catalyst DB (2-week lookback). PM initializes to neutral scores. |
