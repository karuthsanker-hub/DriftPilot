# DriftPilot Refactor Plan v2 — Live Operator Console

Status: **DRAFT** for approval. Date: 2026-05-03.

This plan addresses three operator-side gaps the read-only dashboard
exposed:

1. **Trade visibility** — operator can't see WHAT the bot is placing in
   real time. The current dashboard polls `/api/operator/state` and shows
   slots/queue, but there's no live tape of entries/exits as they happen.
2. **Emergency stop** — operator has no big-red-button to halt
   everything (cancel open orders, exit open positions, block new entries)
   when something looks wrong.
3. **Market sensing + signal routing** — current regime is SPY-only
   GREEN/CAUTION/RED. We have four diverse signals (mean reversion,
   absorption breakout, RS drift, EWMLR trend). The operator can't see
   what *kind* of market we're in and which signal is best fit, and the
   active signal is set via env var rather than driven by regime.

Plan is layered so each phase ships visible value alone. Phases A–B are
operator-immediate; C–D are the meta-controller foundation; E–F are
dashboard upgrades. Total effort: 3-5 focused engineering days.

---

## Goal: Three Visible Operator Capabilities

Quoting the user's ask verbatim:

> frontend is read only which is kind of good, but it doesn't provide
> optics into trades it is placing, live feedback is not there.
>
> stop everything button if I have to.
>
> we need to sense what kind of market it is and plug the signal. and
> that needs to be visible.

This plan adds exactly those three things, layered on top of the
existing read-only architecture without breaking it.

---

## Architecture

```mermaid
flowchart LR
    subgraph "Runtime — src/driftpilot/"
        SM["state_machine.py"]
        Sig["signals/<br/>5 algos"]
        Allocator["execution/<br/>SlotAllocator"]
        Broker["broker/<br/>Alpaca client"]
        SQLite[("SQLite<br/>operator_state")]
    end

    subgraph "NEW Phase A: Event Bus"
        EventBus["event_bus.py<br/>(in-process pub/sub)"]
        WSEndpoint["FastAPI WebSocket<br/>/api/operator/stream"]
    end

    subgraph "NEW Phase B: Operator Control"
        StopAPI["POST /api/admin/emergency-stop"]
        StopHandler["StopController<br/>(cancels orders + flat-all)"]
    end

    subgraph "NEW Phase C-D: Meta-Controller"
        Regime2["regime_detector.py<br/>(multi-feature)"]
        Router["signal_router.py<br/>(deterministic)"]
        RoutingDB[("SQLite<br/>routing_decisions")]
    end

    subgraph "Frontend Phase E-F"
        Tape["Live Trade Tape"]
        StopBtn["Big Red STOP"]
        MarketPanel["Market Regime Panel"]
        SlotGrid["Slot Grid<br/>(WS-driven)"]
    end

    SM -->|"transition events"| EventBus
    Allocator -->|"slot/order events"| EventBus
    Broker -->|"fill events"| EventBus
    Regime2 -->|"regime snapshots"| EventBus
    Router -->|"routing decisions"| EventBus
    Router --> RoutingDB

    EventBus --> WSEndpoint
    WSEndpoint --> Tape
    WSEndpoint --> SlotGrid
    WSEndpoint --> MarketPanel

    StopBtn --> StopAPI
    StopAPI --> StopHandler
    StopHandler --> SM
    StopHandler --> Allocator
    StopHandler --> Broker

    Sig --> Router
    Router -->|"select signal"| Sig
```

---

## Phase A — Live Trade Tape & Event Bus  (1–1.5 days)

### Goal
Every state transition / entry / exit / recycle event surfaces in real
time on the dashboard within ~100ms of happening.

### Backend
- `src/driftpilot/event_bus.py`: simple async in-process pub/sub.
  Contract: `publish(topic: str, payload: dict)` and
  `subscribe() -> AsyncIterator[Event]`. No message broker — single
  process operator. Persistence stays in SQLite as today.
- Wire publishers in:
  - `state_machine.py` — every `transition()` publishes `transition`
    event (already written to SQLite; now also broadcast).
  - `execution/slot_allocator.py` — `slot_reserved`, `slot_freed`.
  - `execution/paper_fills.py` and `broker/alpaca_client.py` — `entry_filled`,
    `exit_filled`.
  - `signals/__init__.py` (or a new scanner service) — `candidate_queue_updated`.
- New FastAPI endpoint `/api/operator/stream` (WebSocket): on
  connection, replay last N events from SQLite, then forward live
  events from the bus.

### Event schema (immutable, additive only after v1)
```python
{
    "event_id": "...",            # UUID
    "topic": "transition" | "slot_reserved" | "entry_filled" | ...,
    "timestamp_et": "2026-05-03T10:31:45-04:00",
    "symbol": "AAPL" | None,
    "slot_id": 3 | None,
    "signal": "stationary_ghost_v1" | None,
    "payload": { ... }            # topic-specific
}
```

### Storage
- New table `operator_events` (append-only). Schema:
  `event_id PK, topic, timestamp_et, symbol, slot_id, signal, payload_json, ingested_at`.
- Backend persists events on `publish()` and broadcasts on the bus.
- Retention: TTL 7 days (configurable). After 7 days, archive or drop.

### Frontend
- New `<TradeTape>` component on Operator tab.
- WebSocket subscription on mount; appends events to a virtualized list
  (auto-scroll if at bottom, freeze if user scrolled up).
- Color coding: green for `entry_filled`/`recycle`, red for
  `exit_filled` with negative P&L, blue for `transition`.
- Click an event to jump to its source row in the Admin event log.

### Acceptance
- Synthetic test: trigger 100 events in 1 second; tape renders all 100
  in correct order within 1 second.
- WS disconnect: client reconnects + replays missed events from
  `operator_events` table on reconnect.
- No event lost across a backend restart (persistence).
- Read-only — the tape is purely informational; no buttons.

---

## Phase B — Emergency Stop  (0.5 days)

### Goal
A big red button visible on every page. Click it, all activity halts
within seconds — no confirmation modal can stand between the operator
and the kill switch when it's needed.

### Backend
- New endpoint `POST /api/admin/emergency-stop` with body:
  ```json
  { "reason": "operator initiated", "operator": "karuthsanker", "idempotency_token": "..." }
  ```
- Handler in `src/driftpilot/operator_control.py`:
  1. Set `operator_state.kill_switch_armed = True` in SQLite (single
     atomic write).
  2. Cancel all open entry orders via broker (best-effort, log per-order).
  3. Submit market exits for all open positions via broker.
  4. Transition state machine to new `HALTED_OPERATOR_KILL_SWITCH` state.
  5. Publish `kill_switch_engaged` event on the bus.
  6. Return 202 immediately — broker calls happen async.
- New state in `OperatorState`: `HALTED_OPERATOR_KILL_SWITCH`.
- New endpoint `POST /api/admin/resume-from-halt` to clear the kill switch
  (REQUIRES manual confirmation — not big-red-button territory).

### Frontend
- Persistent floating red **STOP** button bottom-right of every tab.
- Single click → confirmation modal with 3-second cooldown ("hold to
  confirm" pattern is too easy to misclick; explicit confirm + countdown
  is safer).
- After confirm, button turns to "STOPPING…" with spinner; banner across
  top of every tab shows "OPERATOR KILL SWITCH ENGAGED at <time> by
  <user>"; remains until resume action.
- During HALTED_OPERATOR_KILL_SWITCH:
  - All entry-side actions disabled
  - Exits in flight allowed to complete
  - Tape still streams (so operator can watch the unwind)

### Acceptance
- From button click to first cancel-order broker call: < 500ms.
- From button click to all open positions submitted-for-exit: < 2s.
- Resume requires the explicit `/api/admin/resume-from-halt` call;
  killing the daemon and restarting does NOT auto-resume (the SQLite
  flag persists).
- Idempotency token prevents double-clicks from triggering twice.
- Audit log: every kill-switch engagement writes to a dedicated
  `operator_actions` table with operator name + timestamp + reason.

---

## Phase C — Multi-Feature Regime Detector  (1 day)

### Goal
Replace the SPY-only GREEN/CAUTION/RED scalar with a richer regime
classification driven by SPY + universe breadth + volatility +
time-of-day. The four signals each thrive in different regimes; we need
to *name* the current one explicitly.

### Backend
New module `src/driftpilot/regime_detector.py` exporting:

```python
class MarketRegime(StrEnum):
    TREND_BULL_LOW_VOL    = "trend_bull_low_vol"
    TREND_BULL_HIGH_VOL   = "trend_bull_high_vol"
    TREND_BEAR            = "trend_bear"
    RANGE_BOUND           = "range_bound"
    CHOPPY                = "choppy"
    NEWS_SHOCK            = "news_shock"
    OPENING_DRIFT         = "opening_drift"     # 9:30–10:00 ET
    CLOSING_DRIFT         = "closing_drift"     # 15:00–16:00 ET
    UNKNOWN               = "unknown"

@dataclass(frozen=True)
class RegimeSnapshot:
    timestamp_et: datetime
    regime: MarketRegime
    spy_5m_return_pct: float
    spy_15m_return_pct: float
    spy_30m_return_pct: float
    spy_atr_distance_from_vwap: float
    breadth_above_vwap_pct: float       # % of universe above VWAP
    breadth_advance_decline_ratio: float
    realized_volatility_5m: float        # annualized % stdev of SPY 1-min returns
    time_of_day_bucket: str              # "open" | "morning" | "midday" | "afternoon" | "close"
    minutes_until_close: int
    confidence_score: float              # 0-1: how clear the regime read is

def detect_regime(bar_cache, clock) -> RegimeSnapshot: ...
```

### Classification logic (deterministic, v1)
1. If `realized_volatility_5m > 2× rolling-30-day-avg` AND price moved
   >0.5% in 5 min → `NEWS_SHOCK`.
2. Else if `9:30 ≤ time_et < 10:00` → `OPENING_DRIFT`.
3. Else if `time_et >= 15:00` → `CLOSING_DRIFT`.
4. Else if `|spy_30m_return| < 0.1%` AND `breadth_above_vwap_pct ∈ [40, 60]`
   → `RANGE_BOUND`.
5. Else if `spy_30m_return > 0.3%` AND `breadth_above_vwap_pct > 65%`
   AND `realized_volatility_5m < 1× avg` → `TREND_BULL_LOW_VOL`.
6. Else if `spy_30m_return > 0.3%` AND `realized_volatility_5m > 1× avg`
   → `TREND_BULL_HIGH_VOL`.
7. Else if `spy_30m_return < -0.3%` → `TREND_BEAR`.
8. Else → `CHOPPY`.

`confidence_score` = how much the inputs cluster vs straddle thresholds.
Below 0.4 confidence, dashboard shows regime as "uncertain" rather than
the raw label.

### Storage
- New table `regime_snapshots(id, timestamp_et, regime, payload_json)`.
- One row per scanner cycle (every 30s). 7-day retention.

### Acceptance
- Synthetic SPY bars producing each regime → detector returns the
  expected `MarketRegime` value.
- `confidence_score` is between 0 and 1 across all synthetic inputs.
- Backtest-side: `replay_bars` records `regime_snapshots` so reports
  carry the regime distribution of the test period.
- Existing `signals/regime.py` kept for legacy GREEN/CAUTION/RED gates;
  new detector sits beside it.

---

## Phase D — Signal Router  (1 day)

### Goal
Given current regime + each signal's "operating envelope" (which regimes
historically had positive expectancy in its backtest reports), pick the
active signal automatically. Operator can override.

### Inputs
1. Current `RegimeSnapshot` (Phase C).
2. Each signal's `expectancy_report.json` carries an
   `operating_envelope` block (NEW — Phase D adds the field) listing
   regimes where edge_ratio was > 1.1 in that signal's backtest.
3. Operator override (manual signal selection takes precedence).
4. Last routing decision (avoid thrashing — minimum 5-minute dwell time
   before switching).

### Output
```python
@dataclass(frozen=True)
class RoutingDecision:
    timestamp_et: datetime
    active_signal: str | None        # None = NO_SIGNAL_FITS_REGIME
    confidence_score: float
    regime: MarketRegime
    reasoning: str                   # human-readable
    alternatives_considered: list[tuple[str, float]]   # (signal_name, score)
    override_active: bool
    routing_method: str              # "deterministic" | "operator_override" | "no_fit"
```

### Algorithm v1 (deterministic, no LLM)
1. Hard filters first: drop signals whose `verdict == FAIL`, drop signals
   whose `data_dependencies` aren't currently satisfied (missing SPY
   bars, etc.), drop signals outside their scan window.
2. Score remaining signals by `1.0 if regime in operating_envelope else 0.5`
   plus `0.3 * edge_ratio` (favors signals that did well historically).
3. Apply 5-minute dwell time: don't switch unless top-scored signal beats
   current by > 0.2 score margin.
4. If only one survives → pick it. If none survive → emit
   `NO_SIGNAL_FITS_REGIME` decision (active_signal=None, scanner halts
   new entries).

### Storage
- New table `routing_decisions(id, timestamp_et, active_signal,
  confidence, regime, reasoning, alternatives_json, routing_method,
  override_active)`.

### Override API
```
POST /api/admin/signal-override        { "signal": "...", "operator": "...", "reason": "..." }
DELETE /api/admin/signal-override      (resume automatic routing)
```

### Acceptance
- Deterministic test fixture: synthetic regime stream + signal envelopes
  → expected sequence of routing decisions.
- 5-minute dwell test: rapid regime flicker → router does NOT thrash.
- Override test: operator override survives across regime changes
  until explicitly cleared.
- `NO_SIGNAL_FITS_REGIME` halts new entries but does NOT trigger the
  kill switch (existing positions exit normally).

---

## Phase E — Frontend Live Operator Console  (0.5–1 day)

### Goal
Bring all the new backend data to the screen in a way that makes
the dashboard feel like an actual trading desk rather than a polling
report.

### Changes to existing Operator tab
- Top strip:
  - **Regime indicator** (left): big colored chip with regime name +
    confidence dot (filled = high confidence, hollow = uncertain).
    Click → expand panel with the 5 input metrics.
  - **Active signal** (center): name + version + reasoning text from
    last `RoutingDecision`. Yellow border if `override_active`.
  - **STOP button** (right): permanent floating control — see Phase B.
- Slot grid: now WebSocket-driven. Slots animate state changes (color
  flash on entry/exit). Each slot shows last 3 P&L ticks as a sparkline.
- Below grid: **Live Trade Tape** (Phase A). 60% column on desktop.
  Filter chips at top: All / Entries / Exits / Errors.
- Right rail: Recent routing decisions (last 10) — useful for
  understanding "why did the bot just switch from apex to ghost".

### New "Market" panel (a fresh tab or expand-section)
- Big live SPY chart with regime-color stripes
- Breadth widget: % above VWAP, advance/decline
- Realized volatility gauge
- Time-of-day timeline annotated with regime transitions for the
  current session

### WebSocket reconnect behavior
- 1s → 2s → 5s → 10s exponential backoff
- On reconnect, fetch missed events via `GET /api/operator/events?since=<id>`
- Banner if disconnected for > 5s: "feed stale, reconnecting…"

### Acceptance
- Operator can answer "what kind of market is this and what's the bot
  doing" without refreshing the page.
- All four signal name labels appear correctly when router switches.
- Tape latency from event publish to render: < 500ms.
- Disconnected banner appears within 6s of WS drop and disappears
  within 1s of recovery.

---

## Phase F — Documentation, ops scripts, runbook  (0.5 days)

- Update `docs/PROJECT_OVERVIEW.md` with the new component map.
- New `docs/OPERATOR_RUNBOOK.md` covering:
  - When to engage kill switch
  - How to override signal selection
  - How to interpret regime indicators
  - How to recover from `HALTED_OPERATOR_KILL_SWITCH`
- New section in `scripts/README.md` for any new ops scripts.
- Mermaid diagram in `docs/PROJECT_OVERVIEW.md` updated for new
  components.

---

## Out of scope (intentional, defer to v3)

- LLM-based routing (Qwen/Claude). Deterministic v1 first; LLM only if
  deterministic produces clear failures.
- Mobile-friendly dashboard.
- Multi-operator concurrent control (single-operator assumption).
- Audit log streaming to external SIEM.
- Automated kill-switch triggers (drawdown threshold, broker
  disconnection, etc.) — these are part of `HALTED_RISK` state and
  already exist.

---

## Hard rules (consistent with existing repo conventions)

1. Datetimes timezone-aware via `driftpilot.clock`.
2. No new dependencies without one-line justification in `pyproject.toml`.
3. Repository pattern for SQLite (no SQL strings outside repos).
4. Read-only API endpoints stay read-only; only `/api/admin/*` writes.
5. Every operator action writes a row in `operator_actions`.
6. Tests pass before phase ships.
7. Each phase is committed separately so partial rollback is possible.
8. Emergency stop is the ONLY UI control whose latency is critical;
   everything else can take 1–2s.

---

## Implementation order and effort

Sequential by dependency:

| Phase | Description | Effort | Depends on |
|---|---|---|---|
| A | Event bus + Live Trade Tape | 1–1.5d | — |
| B | Emergency Stop | 0.5d | — (parallel to A) |
| C | Multi-feature regime detector | 1d | — (parallel to A/B) |
| D | Signal router | 1d | C |
| E | Frontend integration | 0.5–1d | A, B, C, D |
| F | Documentation | 0.5d | A–E |
| **Total** | | **3–5 days** | |

A and B are parallelizable from day 1. C runs in parallel.
D depends on C. E depends on all of A, B, C, D. F at the end.

---

## Acceptance for v2 complete

- Operator can engage emergency stop and see the system halt within 2s.
- Every entry/exit/transition surfaces on the live tape within 500ms.
- Operator can read the current market regime + active signal at a
  glance from the top strip.
- Active signal switches automatically when regime changes (with 5-min
  dwell), and the switch reasoning is visible.
- Operator can override signal selection and the override persists
  until cleared.
- All four 2024 backtest reports' `operating_envelope` populated and
  used by the router.
- Existing read-only data flow remains functional (the WebSocket
  augments rather than replaces `/api/operator/state`).

---

## Status: DRAFT

Awaiting user approval before implementation. Suggested first phase to
ship: **B (Emergency Stop)** — highest operator anxiety, lowest
implementation cost, immediate win.
