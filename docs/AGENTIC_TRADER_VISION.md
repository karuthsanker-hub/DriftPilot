# Agentic Trader — The Non-Deterministic Layer

**Date:** 2026-05-10  
**Status:** VISION + REQUIREMENTS  
**Depends on:** Qwen Enrichment v2, RuleBasedRouter (Phase 1 done), Signal Registry, Slot Allocator

---

## The big picture in one paragraph

Everything we've built so far is plumbing — signals, router, enrichment, slots, exits. They're deterministic: "if earnings beat + positive sentiment, route to earnings_report_v1, hold 60 min, exit at 1%." A human trader doesn't work like that. A human trader watches REGN gap up 3% on an earnings beat, sees it still climbing, and says "this is running — let it ride, raise my target to 3%." Or watches PBH sit flat at +0.8% for 25 minutes and says "this isn't going anywhere — take the 0.8% and move on." That decision-making layer is the agent. It's the fund manager, not the algorithm.

---

## What the agent does

### Core loop

```
Every 30 seconds:
  1. CHECK OPEN POSITIONS — is each one on track for 1%?
  2. CHECK INCOMING CATALYSTS — any new enriched events worth trading?
  3. DECIDE — for each position: hold, adjust target, take profit, cut loss
  4. DECIDE — for each new catalyst: trade it, skip it, wait for better entry
  5. ACT — submit/modify/cancel orders
```

### The 1% rule

**The base target is always 1%.** Every trade enters with a 1% profit target. The agent's job is to:

- **Protect the 1%.** If a position hits 1%, the default is: take it. Cash it. Move on.
- **Expand when momentum is clear.** If a position hits 1% and is still accelerating (volume increasing, price making new highs every minute, sector running), the agent can raise the target to 2%, then 3%, up to a hard cap of 5%. But it must set a trailing stop at the current level — never give back the 1%.
- **Cut early when stuck.** If a position is at +0.5% to +0.9% and has been there for 30+ minutes with declining volume, take the partial profit. Don't wait for the full 1% if the move is dying.
- **Cut losses fast.** The deterministic stop loss is 1.5%. The agent can cut earlier if it sees the thesis is broken (e.g., a second headline reverses the catalyst, or the sector starts selling off hard).

### Decision 1: What to trade

When an enriched catalyst arrives, the agent has three choices:

**A. Route to a signal algorithm.**
The RuleBasedRouter says "this is an earnings beat → earnings_report_v1." The agent can accept the router's recommendation, which means the signal's scan/exit logic runs the trade mechanically. The agent monitors but doesn't intervene unless something changes.

**B. Override with direct conviction.**
The agent reads the enriched context (market cap, beat %, earnings history, sector momentum, VIX) and says: "This is a 15% EPS beat on a $800M small-cap in a hot sector with low VIX — I don't need an algorithm, I'm buying this with a limit order 0.1% above the ask and setting a 2% target." Direct entry, no signal algorithm.

**C. Skip.**
"This is a $0.01 beat on a $50B company with no sector tailwind — pass." Even if the router says ROUTE, the agent can override to skip.

### Decision 2: How to manage the position

Once in a position, every 30 seconds the agent evaluates:

| Signal | What the agent sees | Decision |
|---|---|---|
| **Momentum building** | Price making new 1-min highs, volume > 2x avg, VWAP trending up | Raise target: 1% → 2% → 3%. Set trailing stop at current. |
| **Hit 1%, still running** | +1.0% reached, bid still above VWAP, order flow positive | Let it ride. Trail stop at +0.7%. Raise cap to 3%. |
| **Hit 1%, stalling** | +1.0% reached, volume dropping, price consolidating | Take the 1%. Cash out. |
| **Stuck at +0.5-0.9%** | 30+ minutes in position, flat, low volume | Take partial profit. Better to bank +0.6% than wait for a reversal. |
| **Reversal signal** | New negative headline on held stock, sector ETF drops 1%, VIX spikes | Emergency exit. Don't wait for stop loss. |
| **Approaching time stop** | 50 minutes in (10 min left on 60-min horizon) | If profitable: take profit now. If underwater: let time stop trigger. |

### Decision 3: Dynamic sizing (future)

Phase 1 is fixed $1k slots. The agent's future capability:

- **High conviction → larger position.** If the enrichment context shows a 10%+ beat on a small-cap in a running sector, the agent allocates 2x the slot size.
- **Low conviction → smaller position or skip.** A marginal beat with mixed signals gets a half-size position or gets skipped entirely.
- **Portfolio heat management.** If 4 of 5 open positions are in the same sector, the agent skips the next same-sector catalyst regardless of quality.

This is Phase 2+. Not for initial build.

---

## Architecture — Multi-Agent Topology

### The Trading Desk Metaphor

Think of a real trading desk:

- **The PM (Portfolio Manager)** sits in the middle. Sees all 10 books. Decides allocation, risk, when to size up or pull back. Talks to everyone.
- **10 Traders (Slot Agents)** each manage one position. They watch their stock, read the tape, decide hold/exit/trail. They ask the PM for permission to change targets.
- **The Scanner** watches the news wire. When something comes in, it evaluates and pitches the PM: "REGN just beat by 6.5%, small-cap, hot sector — I want to buy it."
- **The PM decides:** "Approved — give it to Slot 3, it's free. But we're heavy in biotech already, so set target at 1% not 2%."

That's the architecture. Three agent types, clear hierarchy, structured communication.

### Agent Topology

```
                         ┌─────────────────────────────────────────┐
                         │         PORTFOLIO MANAGER (PM)           │
                         │                                         │
                         │  THE BOSS. One instance. Runs every     │
                         │  30 seconds. Sees all 10 slots,         │
                         │  portfolio P&L, sector exposure,         │
                         │  daily stats.                           │
                         │                                         │
                         │  Decides:                               │
                         │  • Approve/deny new entries             │
                         │  • Approve/deny target raises           │
                         │  • Force exits (portfolio-level risk)   │
                         │  • Assign stocks to slots               │
                         │  • Adjust session-level parameters      │
                         │                                         │
                         └────┬────────────┬───────────────┬───────┘
                              │            │               │
                    ┌─────────▼──┐   ┌─────▼─────┐  ┌─────▼─────┐
                    │  SCANNER   │   │  SLOT 1   │  │  SLOT 2   │ ... ×10
                    │  AGENT     │   │  AGENT    │  │  AGENT    │
                    │            │   │           │  │           │
                    │  Watches   │   │  Manages  │  │  Manages  │
                    │  catalyst  │   │  NVDA     │  │  (empty)  │
                    │  feed.     │   │           │  │           │
                    │  Pitches   │   │  Watches  │  │  Awaiting │
                    │  PM when   │   │  bars,    │  │  assign-  │
                    │  something │   │  volume,  │  │  ment     │
                    │  is worth  │   │  quotes.  │  │  from PM  │
                    │  trading.  │   │           │  │           │
                    │            │   │  Decides: │  │           │
                    │  Uses:     │   │  HOLD     │  │           │
                    │  • Enrich- │   │  TAKE     │  │           │
                    │    ment v2 │   │  RAISE    │  │           │
                    │  • Router  │   │  CUT      │  │           │
                    │  • Context │   │           │  │           │
                    └────────────┘   └───────────┘  └───────────┘
```

### Communication Protocol (A2A Messages)

Every communication between agents is a structured JSON message logged to the `agent_messages` table. This is the audit trail AND the training data.

#### Scanner → PM: "I want to buy"

```json
{
  "from": "scanner",
  "to": "pm",
  "type": "ENTRY_REQUEST",
  "ts": "2026-05-10T10:32:15.000Z",
  "payload": {
    "symbol": "REGN",
    "catalyst": {
      "headline": "REGN Q1 Adj. EPS $9.47 Beats $8.89...",
      "category": "earnings/report",
      "sentiment": "positive",
      "confidence": 0.88,
      "priority_modifier": 0.14,
      "eps_beat_pct": 6.5,
      "revenue_beat_pct": 3.5,
      "market_cap_m": 98000,
      "sector": "Health Care"
    },
    "router_recommendation": {
      "signal": "earnings_report_v1",
      "action": "ROUTE",
      "conviction": 0.85
    },
    "scanner_opinion": {
      "action": "DIRECT_ENTRY",
      "reasoning": "6.5% beat on $98B biotech, guidance raised, sector hot. Router says use earnings_v1 but this is strong enough for direct entry with 1.5% target.",
      "suggested_target_pct": 1.5,
      "suggested_entry": "limit_at_ask"
    }
  }
}
```

#### PM → Scanner: "Approved" or "Denied"

```json
{
  "from": "pm",
  "to": "scanner",
  "type": "ENTRY_DECISION",
  "ts": "2026-05-10T10:32:15.200Z",
  "payload": {
    "decision": "APPROVED",
    "assigned_slot": 3,
    "adjustments": {
      "target_pct": 1.0,
      "reasoning": "Approved but reducing target to 1% — already 2 health care positions open. Take the 1% and free the slot."
    }
  }
}
```

Or denied:

```json
{
  "from": "pm",
  "to": "scanner",
  "type": "ENTRY_DECISION",
  "payload": {
    "decision": "DENIED",
    "reason": "sector_concentration",
    "reasoning": "Already 3/10 slots in Health Care. Max is 3. Skip until one exits."
  }
}
```

#### PM → Slot Agent: "You are assigned this stock"

```json
{
  "from": "pm",
  "to": "slot_3",
  "type": "ASSIGNMENT",
  "payload": {
    "symbol": "REGN",
    "entry_strategy": "limit_at_ask",
    "target_pct": 1.0,
    "stop_pct": 1.5,
    "max_hold_minutes": 60,
    "context": { ... full enrichment context ... },
    "pm_notes": "Health care heavy today. Take the 1% and free up. Don't get greedy."
  }
}
```

#### Slot Agent → PM: "Requesting target raise"

```json
{
  "from": "slot_3",
  "to": "pm",
  "type": "TARGET_RAISE_REQUEST",
  "ts": "2026-05-10T10:47:22.000Z",
  "payload": {
    "symbol": "REGN",
    "current_unrealized_pct": 1.2,
    "minutes_held": 15,
    "request": {
      "new_target_pct": 2.0,
      "trailing_stop_pct": 0.8,
      "reasoning": "Hit 1% in 15 min. Still climbing — last 5 bars all green, volume 3.2x average, bid wall moving up. Momentum is real."
    },
    "bars_summary": {
      "last_5_closes": [151.20, 151.45, 151.68, 151.90, 152.15],
      "volume_ratio": 3.2,
      "vwap_distance_pct": 0.8
    }
  }
}
```

#### PM → Slot Agent: "Approved with trail"

```json
{
  "from": "pm",
  "to": "slot_3",
  "type": "TARGET_RAISE_APPROVED",
  "payload": {
    "new_target_pct": 2.0,
    "trailing_stop_pct": 0.8,
    "pm_notes": "Approved. But if it stalls at +1.5% for 10 minutes, take it. Don't give back the 1%."
  }
}
```

#### Slot Agent → PM: "Position exited"

```json
{
  "from": "slot_3",
  "to": "pm",
  "type": "EXIT_REPORT",
  "payload": {
    "symbol": "REGN",
    "exit_reason": "TAKE_PROFIT",
    "entry_price": 150.00,
    "exit_price": 152.85,
    "realized_pct": 1.9,
    "hold_minutes": 28,
    "reasoning": "Hit +1.9%, momentum fading — volume dropped below average, spread widening. Took profit before it reverses."
  }
}
```

#### PM → Slot Agent: "Force exit" (PM overrides)

```json
{
  "from": "pm",
  "to": "slot_3",
  "type": "FORCE_EXIT",
  "payload": {
    "reason": "portfolio_risk",
    "reasoning": "VIX just spiked 15% in 5 minutes. Exiting all positions above +0.5% to lock in gains. Market regime shifting to RED.",
    "exit_type": "market"
  }
}
```

### Agent Lifecycle

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          TIME AXIS →                                     │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  PM:      [────── runs every 30s ─── evaluates all slots ─── risk ──]  │
│           │                                                             │
│  Scanner: [── watching catalyst feed ── event arrives ── evaluates ──]  │
│           │                    │                                         │
│           │                    ▼                                         │
│           │            ENTRY_REQUEST → PM                               │
│           │                    │                                         │
│           │                    ▼                                         │
│           │         PM decides: APPROVED, Slot 3                        │
│           │                    │                                         │
│           │                    ▼                                         │
│  Slot 3:  [─ idle ─]  [── ASSIGNED: buy REGN ── watching ── watching ─] │
│                        │         │          │           │                │
│                        │         ▼          ▼           ▼                │
│                        │     +0.5%      +1.0%       +1.2%               │
│                        │     HOLD       HOLD        REQUEST_RAISE        │
│                        │                             │                   │
│                        │                             ▼                   │
│                        │                    PM: "Approved to 2%"         │
│                        │                             │                   │
│                        │                             ▼                   │
│                        │                         +1.9% TAKE_PROFIT       │
│                        │                             │                   │
│                        │                             ▼                   │
│                        │                    EXIT_REPORT → PM             │
│                        │                             │                   │
│  Slot 3:              [── idle again, awaiting next assignment ─────]    │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Where the Quant Algos Live — The Critical Integration

The signal algorithms (`earnings_report_v1`, `filing_8a_v1`, `whale_tail`, etc.) are NOT decorative. They are the **primary decision-makers**. The LLM agents are the override layer.

```
CATALYST ARRIVES
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│  SCANNER AGENT                                           │
│                                                          │
│  Step 1: RuleBasedRouter.route(event)                    │  ← DETERMINISTIC
│           → "ROUTE to earnings_report_v1, conviction 0.85"│
│                                                          │
│  Step 2: earnings_report_v1.scan(symbol, event)          │  ← ALGORITHM RUNS
│           → Candidate(score=0.14, allowed=True,          │
│              features={eps_beat: 6.5%, age: 3min...})    │
│                                                          │
│  Step 3: LLM evaluates the algo's output + context       │  ← NON-DETERMINISTIC
│           "Algo says yes with score 0.14. Context shows  │    (OVERRIDE LAYER)
│            6.5% beat, small-cap, hot sector. I agree —   │
│            but suggest 1.5% target instead of default 1%"│
│                                                          │
│  Step 4: Pitch PM with BOTH algo recommendation and      │
│           LLM opinion                                    │
└──────────────────────────────────────────────────────────┘

PM ASSIGNS STOCK TO SLOT
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│  SLOT AGENT (every 30 seconds)                           │
│                                                          │
│  Step 1: signal.evaluate_exit(position, now)             │  ← ALGORITHM RUNS
│           → ExitDecision(should_exit=False) or           │
│           → ExitDecision(should_exit=True,               │
│              reason="profit_take")                       │
│                                                          │
│  Step 2: IF algo says exit → EXIT (no LLM needed)       │  ← ALGO IS AUTHORITY
│           IF algo says hold → LLM evaluates whether      │    on its own rules
│           to OVERRIDE                                    │
│                                                          │
│  Step 3: LLM reads tape context (bars, volume, spread)  │  ← NON-DETERMINISTIC
│           "Algo says hold (not at target yet). But I see │    (OVERRIDE LAYER)
│            momentum building — volume 3x, new highs.     │
│            Request PM to raise target."                  │
│           OR                                             │
│           "Algo says hold. But stock stuck 30 min, volume│
│            dying. Request PM to let me take partial."    │
│                                                          │
│  DEFAULT: If LLM is down or uncertain → FOLLOW ALGO     │
└──────────────────────────────────────────────────────────┘
```

**The hierarchy of authority:**

```
1. MECHANICAL GUARDRAILS   (hard stop, cap, time stop)     — NEVER overridden
2. ALGORITHM               (scan, evaluate_exit)           — DEFAULT behavior
3. LLM AGENT               (override algo when it sees more) — REQUIRES PM APPROVAL
```

The LLM doesn't replace the algo — it **wraps** it. The algo runs first, produces its output, and the LLM decides whether to accept or override. If the LLM has no strong opinion (or is down), the algo's answer stands.

**Concrete example — Slot Agent monitoring REGN:**

```
t=0:   earnings_report_v1.evaluate_exit(regn_position, now)
       → should_exit=False (unrealized +0.4%, target is 1.0%)
       → Algo says: HOLD

       LLM sees: bars trending up, volume 2.5x, sector green
       → LLM agrees: HOLD (no override needed)

t=30s: earnings_report_v1.evaluate_exit(regn_position, now)
       → should_exit=False (unrealized +0.9%, target is 1.0%)
       → Algo says: HOLD (not at target yet)

       LLM sees: volume just spiked to 4x, making new highs, bid wall moving up
       → LLM DISAGREES: "This is running. I want to RAISE_TARGET to 2%."
       → Sends request to PM

t=60s: earnings_report_v1.evaluate_exit(regn_position, now)
       → should_exit=True (unrealized +1.0%, reason="profit_take")
       → Algo says: EXIT at 1%

       But PM already approved raise to 2%, so algo's 1% target is overridden.
       Trailing stop set at +0.7%.
       → LLM says: HOLD (following the raised target now)

t=90s: unrealized +1.9%, momentum fading
       LLM says: TAKE_PROFIT (below the 2% target, but reasoning is sound)
       → This is a TAKE at the slot agent's discretion (above original 1% target)
```

**Which agent uses which algo:**

| Agent | Algo used | How |
|---|---|---|
| **Scanner** | `RuleBasedRouter.route(event)` | Determines which algo to consult |
| **Scanner** | `signal.scan(now)` | Runs the algo's candidate filtering — is this stock tradeable? |
| **Slot Agent** | `signal.evaluate_exit(position, now)` | Runs the algo's exit logic every 30s — the DEFAULT |
| **PM** | None directly | PM doesn't run algos — it makes portfolio-level decisions on top of algo + LLM outputs |

**The algo is the baseline. The LLM is the alpha on top.**

If we turned off all LLM agents, the system would revert to exactly what we have today: router → signal → mechanical entry/exit. The agents add:
- Smarter target expansion (algo doesn't do this)
- Early profit-taking when stuck (algo waits for time stop)
- Skipping weak catalysts the algo would accept
- Direct entries for obvious setups the algo isn't designed for

### Who Does What — Clear Responsibilities

| Agent | Singleton? | Loop interval | Algo it runs | LLM decisions | Cannot do |
|---|---|---|---|---|---|
| **PM** | Yes, 1 instance | 30 seconds | None | Approve entries, approve raises, force exits, assign slots, adapt session params | Execute orders directly (delegates to slot agents) |
| **Scanner** | Yes, 1 instance | Event-driven (on each catalyst) | `Router.route()` + `signal.scan()` | Override algo recommendation (skip what algo accepts, accept what algo skips) | Buy or sell anything. Can only request. |
| **Slot Agent** | 10 instances | 30 seconds (when active) | `signal.evaluate_exit()` | Override algo hold (request raise, request cut), but FOLLOWS algo exit | Raise own target without PM approval. Enter new positions. |

### The PM is the Single Point of Control

This is critical. The PM is NOT just a coordinator — it's the decision-maker:

1. **Only PM assigns stocks to slots.** Scanner recommends, PM decides.
2. **Only PM approves target raises.** Slot agent requests, PM approves/denies.
3. **PM can force-exit any slot** at any time for portfolio-level reasons.
4. **PM sets the session mood** — after 3 losses, PM tightens all targets to 0.8%, reduces to high-conviction-only.
5. **PM manages correlation** — won't approve a 4th tech stock even if scanner loves it.

Slot agents are autonomous on **HOLD** and **TAKE_PROFIT at or above target**. They don't need PM permission to:
- Hold (do nothing)
- Take profit at the assigned target (1% = take, no permission needed)
- Take profit above the approved target (if PM approved raise to 2% and it hits 2%, just take it)

They DO need PM permission to:
- Raise target above what was assigned
- Cut early (below target but above 0) — PM might say "wait, sector is about to turn"
- Violate any other constraint

### Agent State Machine (Per Slot Agent)

```
IDLE  →  ASSIGNED  →  ENTERING  →  MONITORING  →  EXITING  →  IDLE
                                       │
                                       ├── (every 30s) evaluate position
                                       │     └── HOLD: stay in MONITORING
                                       │     └── TAKE_PROFIT: → EXITING
                                       │     └── REQUEST_RAISE: msg PM, stay in MONITORING
                                       │     └── CUT_EARLY: msg PM, await approval
                                       │
                                       ├── (PM force-exit received): → EXITING
                                       │
                                       └── (hard stop / time stop hit): → EXITING (mechanical, no LLM)
```

### Where LLM Calls Happen

| Agent | When | LLM | Latency budget | Fallback if LLM fails |
|---|---|---|---|---|
| Scanner | New catalyst arrives | Qwen (fast) | 500ms | Accept router recommendation blindly |
| PM | Entry decision | Qwen or Claude | 1s | Apply deterministic rules (sector cap, slot available → approve) |
| PM | Target raise decision | Qwen | 500ms | Deny all raises (conservative fallback) |
| PM | Session adaptation | Claude (smart) | 3s | Keep default parameters |
| Slot Agent | Position evaluation (every 30s) | Qwen (fast) | 500ms | HOLD (do nothing is safest default) |
| Slot Agent | Exit reasoning | Qwen | 500ms | If at/above target: TAKE. If below: HOLD. |

**Total LLM calls per 30-second cycle:**
- PM: 1 call (portfolio summary evaluation)
- Active slot agents: 1 call each (up to 10)
- Scanner: 0-3 calls (depends on catalyst volume)
- **Worst case: ~14 Qwen calls per 30s = ~0.5 calls/second on local DGX** (trivial load)

### Slot Assignment Flow (Who Picks the Stock)

```
1. Catalyst event arrives (enriched with v2 context)
         │
         ▼
2. Scanner Agent evaluates:
   - Is this worth trading? (uses enrichment context)
   - Which algo? (uses RuleBasedRouter recommendation)
   - Or direct entry? (scanner's own judgment)
   - What target? (based on beat magnitude, sector, VIX)
         │
         ▼
3. Scanner → PM: ENTRY_REQUEST
   "I want to buy REGN. Router says earnings_v1.
    I say direct entry with 1.5% target because beat is 6.5%."
         │
         ▼
4. PM evaluates:
   - Is there a free slot?
   - Is sector cap hit?
   - Is daily loss limit hit?
   - How's the session going? (should I be aggressive or conservative?)
   - Do I agree with scanner's target, or should I adjust?
         │
         ├── NO free slot → DENIED (wait or skip)
         ├── Sector cap hit → DENIED
         ├── Session going badly → DENIED or reduced target
         ▼
5. PM → APPROVED
   - Assigns to Slot N (picks the free slot)
   - Sets target (may adjust scanner's suggestion)
   - Sets constraints (max hold, hard stop, notes for slot agent)
         │
         ▼
6. PM → Slot N Agent: ASSIGNMENT message
   "You are now managing REGN. Entry at ask. Target 1%.
    Stop 1.5%. Max 60 min. Note: sector heavy, take the 1% and free up."
         │
         ▼
7. Slot N Agent: enters position, starts monitoring loop
```

### Communication Infrastructure

```python
# Every agent message goes through a central bus (SQLite-backed)
class AgentBus:
    async def send(self, message: AgentMessage) -> None:
        """Write to agent_messages table + notify recipient."""
        
    async def receive(self, agent_id: str) -> list[AgentMessage]:
        """Read pending messages for this agent."""
    
    def log_decision(self, agent_id: str, decision: Decision) -> None:
        """Audit log — every LLM decision with full reasoning."""
```

**Messages are persisted, not fire-and-forget.** This means:
- Full audit trail of every trade decision
- Can replay the session to understand what happened
- Training data for fine-tuning: (context, decision, outcome)
- Dashboard shows the conversation between agents in real-time

### DB Schema for Agent Communication

```sql
CREATE TABLE agent_messages (
    id INTEGER PRIMARY KEY,
    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    from_agent TEXT NOT NULL,      -- "pm", "scanner", "slot_1"..."slot_10"
    to_agent TEXT NOT NULL,
    message_type TEXT NOT NULL,    -- "ENTRY_REQUEST", "ENTRY_DECISION", "ASSIGNMENT", etc.
    payload_json TEXT NOT NULL,
    acknowledged_at TIMESTAMP,     -- when recipient processed it
    outcome_json TEXT              -- filled after trade closes (for training data)
);

CREATE TABLE agent_decisions (
    id INTEGER PRIMARY KEY,
    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    agent_id TEXT NOT NULL,
    decision_type TEXT NOT NULL,   -- "ENTRY_EVAL", "POSITION_MONITOR", "TARGET_RAISE", etc.
    inputs_json TEXT NOT NULL,     -- what the LLM saw
    llm_response_json TEXT NOT NULL, -- raw LLM output
    action_taken TEXT NOT NULL,    -- "HOLD", "TAKE_PROFIT", "APPROVED", etc.
    reasoning TEXT,                -- extracted reasoning
    latency_ms INTEGER,           -- how long the LLM call took
    outcome_json TEXT              -- filled after trade closes
);
```

### Dashboard: Agent Conversation View

The dashboard gets a new panel showing the live agent conversation:

```
┌─────────────────────────────────────────────────────────────────────┐
│  AGENT LOG                                                     LIVE │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  10:32:15  SCANNER → PM                                            │
│  "REGN earnings beat 6.5%, $98B biotech. Want to buy. Direct      │
│   entry, target 1.5%."                                             │
│                                                                     │
│  10:32:15  PM → SCANNER                                            │
│  "Approved. Slot 3. But target 1% — already 2 health care open."  │
│                                                                     │
│  10:32:16  PM → SLOT 3                                             │
│  "Assigned REGN. Limit at ask. Target 1%. Take it and move on."   │
│                                                                     │
│  10:32:18  SLOT 3: ENTERED @ $150.05                               │
│                                                                     │
│  10:47:22  SLOT 3 → PM                                             │
│  "REGN +1.2% in 15 min. Volume 3.2x. Requesting raise to 2%."    │
│                                                                     │
│  10:47:22  PM → SLOT 3                                             │
│  "Approved 2%. Trail at 0.8%. If stalls 10 min at +1.5%, take."   │
│                                                                     │
│  10:59:48  SLOT 3: EXIT @ $152.85 (+1.9%, 28 min)                 │
│  "Momentum fading, volume dropped. Taking +1.9%."                 │
│                                                                     │
│  10:59:48  SLOT 3 → PM                                             │
│  "Exited REGN +1.9%. Slot free."                                  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### LLM integration

The agent uses Qwen3-8B (local, fast, free) or Claude (API, smarter, costs money) for decisions:

**Fast decisions (Qwen, <500ms):**
- "Should I take profit now?" — position state + 5 recent bars + current quote → yes/no
- "Is this catalyst worth trading?" — enriched context → trade/skip/wait
- "Is momentum still building?" — last 10 bars + volume profile → yes/fading/reversing

**Complex decisions (Claude, ~2s):**
- "I have 4 open positions, a new catalyst just arrived, and VIX is spiking — what do I do?" — full portfolio state → structured action plan
- "This stock has been at +0.8% for 35 minutes — the signal says hold but the chart says it's dying. What would a trader do?" — requires judgment

**No LLM (deterministic fallback):**
- If LLM is down or too slow, fall back to the existing signal-based exit rules
- The 1.5% stop loss is ALWAYS enforced mechanically, never overridden by LLM
- The 5% hard cap is ALWAYS enforced mechanically

---

## The non-deterministic parts — what makes this an "agent"

### 1. Reading the tape

A deterministic system sees: price=150.20, entry=149.50, unrealized=+0.47%.

An agent sees: "Price has been in a 150.10-150.25 range for 12 minutes. Volume is declining. The bid keeps getting hit but bouncing. There's a wall of offers at 150.30. The sector ETF just rolled over. This stock is coiling — it's going to either break 150.30 and rip to 151, or lose 150.10 and drop. Given the declining volume and sector weakness, the downside is more likely. Take the 0.47% now."

That's the LLM reading price action context and making a judgment call. The inputs are structured (bars, L2 quotes, sector ETF), but the decision is non-deterministic.

### 2. Narrative reasoning

"NVDA beat by 15% and raised guidance. But last quarter they also beat by 12% and the stock sold off 3% because the market expected even more. Is this beat 'enough'? The enrichment context shows last 4 surprises: +14%, +12%, +18%, +15%. This 15% beat is in line with history — the market expects it. This isn't a surprise, it's confirmation. Reduce conviction, set target at 0.8% not 1%."

No rule-based system captures that. The agent reads the earnings history in the context block and reasons about expectations.

### 3. Adapting intra-session

"I've taken 3 trades today, all hit 1% within 15 minutes. The market is trending hard. Today is not a 1% day — it's a 2% day. Raise default targets to 1.5% for the rest of the session."

Or: "I've taken 4 trades, 3 hit stop loss. The market is choppy. Reduce position sizes by half and only trade high-conviction catalysts for the rest of the day."

The agent observes its own performance within the session and adapts. A deterministic system can't do this.

### 4. Dynamic code generation (future)

"I keep seeing a pattern where stocks gap up 2%+ pre-market on earnings but then sell off in the first 15 minutes before resuming the drift. I should wait 15 minutes after the open before entering on pre-market gap-ups."

The agent identifies a recurring pattern, formulates a hypothesis, writes a filter function, backtests it on recent data, and if it improves edge, deploys it as a new rule. This is the most advanced capability — the agent writes its own code.

---

## Implementation phases

### Phase 1: Position Monitor Agent (build this first)

**What:** An LLM agent that monitors open positions every 30 seconds and decides: hold / take profit / adjust target.

**Does NOT do:** Entry decisions. Phase 1 entries still come from the existing signal pipeline + router.

**Inputs per position:**
- Entry price, current price, unrealized %
- Time in position (minutes)
- Last 10 one-minute bars (OHLCV)
- Current bid/ask spread
- Volume vs average volume
- Sector ETF current change %
- Any new headlines on this symbol since entry

**Decision space:**
- `HOLD` — keep current target
- `TAKE_PROFIT` — exit now at market
- `RAISE_TARGET` — increase profit target by 0.5%, set trailing stop at current unrealized - 0.3%
- `CUT_EARLY` — exit now, don't wait for stop loss (thesis broken)

**Rules the agent CANNOT override:**
- Hard stop loss at 1.5% — always enforced mechanically
- Hard profit cap at 5% — always take at 5%
- Time stop at 60 minutes — always enforced
- Minimum position hold: 2 minutes (prevent churn)

**Logging:** Every decision logged with: timestamp, position_id, decision, reasoning (LLM output), inputs snapshot. This creates a training dataset for future model fine-tuning.

### Phase 2: Entry Agent

**What:** Agent decides whether to trade a new catalyst and which algo to use (or direct entry).

**Added capability:** Can override router's recommendation. Can submit limit orders directly without signal pipeline.

### Phase 3: Session Adaptation

**What:** Agent observes its own P&L curve intra-day and adjusts default parameters (target size, position sizing, sector limits).

### Phase 4: Dynamic Strategy Generation

**What:** Agent identifies recurring patterns in its trade log, formulates hypotheses, writes filter functions, backtests them, and deploys. Self-improving.

---

## Interaction with existing system

### What stays deterministic

| Component | Stays as-is | Why |
|---|---|---|
| Stop loss (1.5%) | Yes | Safety net. Never trust LLM with downside risk. |
| Profit hard cap (5%) | Yes | Greed kills. Mechanical ceiling. |
| Time stop (60 min) | Yes | Prevents overnight exposure. |
| Slot allocator (10 × $1k) | Yes (Phase 1) | Fixed sizing until agent proves itself. |
| Sector caps | Yes | Concentration risk is mechanical. |
| Daily loss limit (3%) | Yes | Risk management is never non-deterministic. |
| Router (Phase 1) | Stays as fallback | Agent can override, but router is the auto-pilot. |

### What becomes agent-controlled

| Component | Agent controls | Phase |
|---|---|---|
| Profit target adjustment | 1% → up to 5% based on momentum | Phase 1 |
| Early exit (partial profit) | Take +0.6% if stuck 30+ min | Phase 1 |
| Early exit (thesis broken) | Cut before stop loss if reversal signal | Phase 1 |
| Entry decision | Trade/skip/direct on new catalyst | Phase 2 |
| Default target for session | Adapt based on intra-day performance | Phase 3 |
| New filter rules | Write and deploy code from pattern recognition | Phase 4 |

---

## LLM prompt architecture

### Position monitor prompt (Phase 1)

```
You are a professional intraday trader managing a live position.
Your base profit target is 1%. You can raise it up to 5% if momentum
is strong, or take partial profit earlier if the move is dying.

POSITION:
- Symbol: {symbol}
- Entry: ${entry_price} at {entry_time} ({minutes_held} min ago)
- Current: ${current_price} ({unrealized_pct}%)
- Target: {current_target}% | Stop: {stop_loss}%

RECENT BARS (last 10 minutes, 1-min OHLCV):
{bars_table}

MARKET CONTEXT:
- Bid: ${bid} ({bid_size}) | Ask: ${ask} ({ask_size}) | Spread: {spread_pct}%
- Volume last 10m: {recent_vol} vs avg: {avg_vol} ({rvol}x)
- Sector ETF ({sector_etf}): {sector_change}% today
- VIX: {vix}

NEW HEADLINES SINCE ENTRY:
{new_headlines or "None"}

TRADE HISTORY TODAY:
- Trades: {trades_today} | Wins: {wins} | Losses: {losses}
- Session P&L: {session_pnl}%

Decide one action:
- HOLD: keep current target, no changes
- TAKE_PROFIT: exit now at market (explain why)
- RAISE_TARGET: set new target to X% and trailing stop at Y% (explain the momentum signal)
- CUT_EARLY: exit now before stop loss (explain what changed)

Return JSON:
{
  "action": "HOLD" | "TAKE_PROFIT" | "RAISE_TARGET" | "CUT_EARLY",
  "new_target_pct": null or float,
  "trailing_stop_pct": null or float,
  "reasoning": "one sentence"
}
```

### Entry decision prompt (Phase 2)

```
You are a professional intraday trader. A new catalyst event just arrived.
Decide whether to trade it, and if so, how.

CATALYST:
{enriched_context_block}

ROUTER RECOMMENDATION:
- Signal: {router_signal} | Conviction: {conviction} | Action: {router_action}

PORTFOLIO STATE:
- Open positions: {n_open} / 10 slots
- Sectors exposed: {sectors}
- Session P&L: {session_pnl}%
- Trades today: {trades_today}

MARKET:
- SPY: {spy_change}% | VIX: {vix}
- Regime: {regime}

Decide:
- ACCEPT_ROUTE: follow the router's recommendation ({router_signal})
- DIRECT_ENTRY: buy {symbol} directly at limit ${price} with target {target}%
- SKIP: pass on this catalyst (explain why)
- WAIT: interesting but not yet — watch for {condition}

Return JSON:
{
  "action": "ACCEPT_ROUTE" | "DIRECT_ENTRY" | "SKIP" | "WAIT",
  "limit_price": null or float,
  "target_pct": 1.0,
  "reasoning": "one sentence"
}
```

---

## Success metrics

| Metric | Current (deterministic) | Target (with agent) |
|---|---|---|
| Win rate | 41-47% | > 55% |
| Avg winner | +1.0% (fixed target) | +1.2-1.5% (dynamic expansion) |
| Avg loser | -1.5% (fixed stop) | -1.0% (early cuts) |
| Trades stuck > 30 min at +0.5-0.9% | Held until time stop | Exited with partial profit |
| Daily P&L consistency | High variance | Lower variance (session adaptation) |
| Edge ratio | 1.0-1.1 | > 1.5 |

---

## Risk guardrails

The agent is powerful but constrained. These are hard-coded, non-negotiable, not agent-overridable:

1. **Hard stop at 1.5%.** If position drops 1.5%, sell immediately. No LLM can override this.
2. **Hard cap at 5%.** If position gains 5%, sell immediately. Greed protection.
3. **Time stop at 60 min.** Position auto-exits at 60 minutes regardless of P&L.
4. **Daily loss limit 3%.** After 3% portfolio loss in a day, no new entries. Existing positions manage to exit only.
5. **No overnight positions.** Everything flat by 15:55 ET.
6. **No short selling.** Long-only until the system proves itself.
7. **Paper trading first.** Agent runs in paper mode for minimum 30 days before any live consideration.
8. **Every decision logged.** Full reasoning chain stored for every trade/hold/exit decision. No black-box trading.
9. **Kill switch.** Human can halt all trading instantly via dashboard.
10. **Max position size $1k.** Fixed until agent demonstrates consistent profitability over 60+ days.

---

## Technology stack

| Component | Choice | Reasoning |
|---|---|---|
| Fast decisions | Qwen3-8B on DGX (local) | <500ms latency, free, good enough for position monitoring |
| Complex decisions | Claude API (Sonnet) | Better reasoning for portfolio-level decisions, ~2s acceptable |
| Fallback | Deterministic rules | If both LLMs are down, signals + router handle everything |
| Real-time data | Alpaca SIP stream | Already integrated, sub-second quotes and bars |
| Execution | Alpaca paper API | Already integrated, order submission + modification |
| State | SQLite | Already integrated, add agent_decisions table |
| Dashboard | Existing FastAPI | Add agent decision log panel, position monitor view |

---

## Relationship to other components

```
QWEN ENRICHMENT V2          →  Feeds enriched catalysts to the agent
  (context + prompt)             with full context block

RULE-BASED ROUTER            →  Agent's auto-pilot / default recommendation
  (Phase 1, done)                Agent can accept or override

SIGNAL REGISTRY              →  Agent's toolbox of algorithms
  (earnings, filing, etc.)       Agent picks which tool to use

SLOT ALLOCATOR               →  Agent's risk constraints
  (10 slots, sector caps)        Agent operates within these limits

DASHBOARD                    →  Agent's control panel
  (catalyst detail panel)        Shows agent decisions + reasoning

POSITION MONITOR             →  Agent's core loop
  (every 30s evaluation)         The non-deterministic heartbeat
```

The agent doesn't replace any of these — it sits on top and uses them. If the agent is turned off, the system reverts to fully deterministic operation (router + signals + mechanical exits). The agent adds alpha on top.
