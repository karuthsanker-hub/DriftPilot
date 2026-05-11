# Agentic Trader — Requirements & Implementation Plan

**Date:** 2026-05-10  
**Status:** REQUIREMENTS (ready to code)  
**Depends on:** Qwen Enrichment v2 (in progress), RuleBasedRouter (done), Signal Registry (done)

---

## 1. Goals and Success Metrics

### Primary Goal

Add an LLM reasoning layer that makes adaptive decisions the deterministic algorithm cannot (target expansion, early profit-taking, thesis-broken cuts) while never weakening the mechanical guardrails.

### Success Metrics (measured after 4 weeks paper trading)

| Metric | Baseline (algo-only) | Target (with agents) | Measurement |
|--------|---------------------|---------------------|-------------|
| Edge ratio | 1.1x | >= 1.3x | `realized_pnl / (N * slot_value * stop_pct)` |
| Win rate | ~55% | >= 58% | trades closed at profit / total trades |
| Avg winner size | 1.0% (fixed target) | >= 1.3% (dynamic expansion) | agent lets winners run |
| Avg loser size | -1.5% (fixed stop) | < -1.2% (agent cuts early) | agent detects thesis broken |
| Trades stuck >30m at +0.5-0.9% | Held until time stop | Exited with partial profit | agent takes partial |
| Override rate | 0% | 5-15% | % of algo decisions overridden by LLM |
| Override accuracy | N/A | >= 65% win rate on overrides | only count LLM-initiated deviations |
| Guardrail violations | 0 | 0 (absolute) | any violation is a critical bug |
| LLM latency p95 | N/A | < 800ms (Qwen), < 3s (Claude) | end-to-end |
| Fallback rate | N/A | < 5% | % of cycles where LLM was down/slow |

### Anti-Goals

- Agent must NOT make the system harder to debug than deterministic baseline
- Agent must NOT increase trade frequency beyond what the algo generates
- Agent must NOT override algo more than 20% of the time (complexity signal)
- Agent must NOT hold any state that can't survive a restart

### Kill Switch Criteria

If after 2 weeks of paper trading:
- Agent override accuracy < 55% (worse than coin flip) → disable agent
- Agent path edge_ratio < algo-only edge_ratio → disable agent
- Guardrail violations > 0 → disable agent immediately
- Fallback rate > 20% → investigate LLM infrastructure

---

## 2. Architecture

### Agent Topology

```
┌───────────────────────────────────────────────────────────────────────┐
│                     MECHANICAL GUARDRAILS                              │
│   1.5% stop | 5% cap | 60min time stop | 3% daily loss | 10 slots   │
│   Enforced at execution layer. NO agent can override. Period.         │
└───────────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────┼─────────────────────────────────────────┐
│                             │                                         │
│  ┌──────────────────────────▼──────────────────────────────────────┐  │
│  │                   PM AGENT (1 instance)                          │  │
│  │                                                                  │  │
│  │  Runs: every 30 seconds                                         │  │
│  │  LLM: Qwen (fast decisions) + Claude (session adaptation)       │  │
│  │                                                                  │  │
│  │  APPROVES/DENIES:                                               │  │
│  │    • Entry requests from Scanner                                │  │
│  │    • Target raise requests from Slot Agents                     │  │
│  │    • Early cut requests from Slot Agents                        │  │
│  │                                                                  │  │
│  │  ISSUES:                                                        │  │
│  │    • FORCE_EXIT to Slot Agents (portfolio-level risk)           │  │
│  │    • Session parameter adjustments                              │  │
│  │                                                                  │  │
│  │  ASSIGNS: stocks to slots (only PM can do this)                 │  │
│  └────────┬─────────────────────────────────┬──────────────────────┘  │
│           │                                 │                         │
│  ┌────────▼────────┐            ┌───────────▼───────────────────┐    │
│  │  SCANNER (1)     │            │  SLOT AGENTS (10 instances)   │    │
│  │                  │            │                               │    │
│  │  Trigger: each   │            │  Runs: every 30s while active │    │
│  │  catalyst event  │            │  LLM: Qwen only              │    │
│  │                  │            │                               │    │
│  │  Step 1: Router  │            │  Step 1: signal.evaluate_exit │    │
│  │  Step 2: signal  │            │    → if EXIT: execute (no LLM)│    │
│  │    .scan()       │            │    → if HOLD: go to Step 2    │    │
│  │  Step 3: LLM     │            │                               │    │
│  │    override?     │            │  Step 2: LLM evaluates tape   │    │
│  │  Step 4: pitch   │            │    → HOLD (agree with algo)   │    │
│  │    PM            │            │    → REQUEST_RAISE (ask PM)   │    │
│  │                  │            │    → REQUEST_CUT (ask PM)     │    │
│  └──────────────────┘            └───────────────────────────────┘    │
│                                                                       │
│                    ┌─────────────────────────────┐                    │
│                    │    A2A MESSAGE BUS           │                    │
│                    │   SQLite-backed, JSON msgs   │                    │
│                    │   Every msg logged + timed   │                    │
│                    └─────────────────────────────┘                    │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────┼─────────────────────────────────────────┐
│  EXISTING INFRASTRUCTURE    │                                         │
│                             │                                         │
│  SignalProtocol (.scan, .evaluate_exit)                               │
│  RuleBasedRouter (.route)                                            │
│  SlotAllocator (10 slots, sector caps)                               │
│  AlpacaBroker (orders, quotes, positions)                            │
│  CatalystEventBus (event pub/sub)                                    │
│  Dashboard (FastAPI)                                                 │
└───────────────────────────────────────────────────────────────────────┘
```

### Authority Hierarchy

```
Level 1: MECHANICAL GUARDRAILS     — Hard stop 1.5%, cap 5%, time 60min, daily 3%
                                     Enforced at broker/execution layer.
                                     NOTHING overrides these. Not algo. Not LLM.

Level 2: ALGORITHM                 — signal.scan(), signal.evaluate_exit()
                                     The DEFAULT behavior. Runs first every cycle.
                                     If LLM has no opinion → algo's answer stands.

Level 3: LLM AGENT (override)     — Wraps the algo. Can override algo's HOLD.
                                     Cannot override algo's EXIT.
                                     All overrides require PM approval.
                                     If LLM is down → system runs algo-only.
```

### Where Algos Run

| Agent | Algo it runs | LLM does what on top |
|---|---|---|
| Scanner | `Router.route(event)` then `signal.scan(now)` | Override: skip what algo accepts, force-enter what algo misses |
| Slot Agent | `signal.evaluate_exit(position, now)` every 30s | Override: request raise/cut when algo says HOLD |
| PM | None (portfolio-level only) | Approves/denies overrides, forces exits, adapts session |

### Algo-Exit Is Authoritative

When `signal.evaluate_exit()` returns `should_exit=True`:
- Slot Agent executes the exit **immediately**
- No LLM call happens
- No PM approval needed
- The algo's exit rules (profit_take, stop_loss, trailing_stop, time_stop) are AUTHORITATIVE

The LLM is ONLY consulted when the algo says HOLD. The LLM can then say "I disagree, I want to exit/raise/cut" — but must ask PM first.

---

## 3. Agent Specifications

### 3.1 PM Agent (Portfolio Manager)

**Instance count:** 1  
**Loop interval:** 30 seconds  
**LLM:** Qwen for individual decisions, Claude for session adaptation  
**Fallback if LLM down:** Approve entries that pass algo + allocator checks. Deny target raises. No session adaptation.

**Responsibilities:**
1. Process ENTRY_REQUEST messages from Scanner → approve/deny
2. Process TARGET_RAISE_REQUEST from Slot Agents → approve/deny
3. Process EARLY_CUT_REQUEST from Slot Agents → approve/deny
4. Issue FORCE_EXIT when portfolio-level risk dictates
5. Adapt session parameters after patterns emerge (3 wins/losses)
6. Maintain portfolio awareness: sector exposure, correlation, drawdown

**PM decision rules (deterministic, before LLM):**
- Free slot available? If no → deny
- Sector cap hit (3 per sector)? If yes → deny
- Daily loss >= 2.5%? → deny all new entries
- Daily loss >= 3.0%? → force exit all positions
- Override rate > 20% today? → deny all LLM overrides (revert to algo-only)

**PM LLM evaluation (after deterministic checks pass):**
- Should I adjust the target from scanner's suggestion?
- Should I size this smaller (correlation concern)?
- Is the session going well enough to expand?

---

### 3.2 Scanner Agent

**Instance count:** 1  
**Trigger:** Each CatalystEvent from the event bus  
**LLM:** Qwen only  
**Fallback if LLM down:** Emit ENTRY_REQUEST for every candidate that passes algo scan

**Flow:**
```
CatalystEvent arrives
  → Router.route(event) → RoutingDecision
  → if BLOCK: stop (no trade)
  → if ROUTE: signal.scan(now) → list[Candidate]
  → if candidates empty: stop
  → LLM evaluates each candidate: approve / skip / force_enter
  → for each approved: emit ENTRY_REQUEST to PM
```

**Scanner LLM evaluation:**
- Is this headline actually what the classification says? (catch misclassification)
- Is this a duplicate/follow-up of something we already traded today?
- Is the sector context supportive or hostile?
- For force_enter: is there an obvious opportunity the algo's taxonomy doesn't cover?

---

### 3.3 Slot Agent

**Instance count:** 10 (one per slot)  
**Loop interval:** 30 seconds (only when slot has open position)  
**LLM:** Qwen only  
**Fallback if LLM down:** Follow algo exactly (HOLD when algo says HOLD)

**Flow (every 30s while position is open):**
```
signal.evaluate_exit(position, now)
  → if should_exit=True: EXECUTE EXIT IMMEDIATELY (no LLM)
  → if should_exit=False (HOLD):
      → LLM evaluates position state + tape
      → HOLD: do nothing (default, should be >85% of decisions)
      → REQUEST_TARGET_RAISE: emit TARGET_RAISE_REQUEST to PM
      → REQUEST_PARTIAL_PROFIT: emit PARTIAL_PROFIT_REQUEST to PM
      → REQUEST_EARLY_CUT: emit EARLY_CUT_REQUEST to PM
      → await PM response before acting
```

**Slot Agent LLM evaluation:**
- Is momentum building? (volume increasing, new highs, VWAP trending)
- Is the move dying? (stuck, declining volume, spread widening)
- Is the thesis broken? (new negative headline, sector reversal)
- Am I near time stop with small profit? (take partial vs wait)

---

## 4. LLM Prompts (Configurable)

All prompts live in `config/prompts/*.yaml`. Loaded at startup. Hot-reloadable via dashboard admin panel or SIGHUP.

### 4.1 PM Entry Approval Prompt

```yaml
# config/prompts/pm_entry_approval.yaml
version: 1
model: qwen
timeout_ms: 500
max_tokens: 256
fallback_action: approve
temperature: 0.0

system: |
  You are the Portfolio Manager for DriftPilot, an intraday paper-trading system.
  You approve or deny entry requests from the Scanner Agent.
  
  Your job is to catch what the algorithm cannot:
  - Sector crowding (too correlated)
  - Drawdown discipline (tighten after losses)
  - Timing (don't enter in last 30 min of session)
  - Conviction calibration (adjust target based on quality)
  
  HARD RULES (already enforced mechanically, listed for context):
  - Max 10 positions, max 3 per sector, daily loss limit 3%
  
  YOUR ADJUSTABLE PARAMETERS:
  - target_pct: 0.005 to 0.05 (base is 0.01)
  - size_multiplier: 0.5 to 2.0 (base is 1.0)
  
  DECISION FRAMEWORK:
  - approve: entry is sound, algo passed, portfolio can absorb
  - deny: sector too crowded, session going badly, catalyst too weak
  
  Respond JSON only. No markdown. No explanation outside JSON.

user_template: |
  ENTRY REQUEST from Scanner:
  symbol: {symbol}
  signal: {signal_name}
  algo_score: {algo_score}
  catalyst: "{headline}"
  sentiment: {sentiment} | confidence: {confidence} | pm: {priority_mod}
  proposed_target: {target_pct}% | proposed_stop: {stop_pct}%
  
  PORTFOLIO STATE:
  open_slots: {open_slots}/10
  sectors: {sector_exposure}
  daily_pnl: {daily_pnl_pct}%
  consecutive_losses: {consec_losses} | consecutive_wins: {consec_wins}
  minutes_remaining_in_session: {minutes_left}
  last_trade_result: {last_trade_result}

response_schema:
  type: object
  required: [decision, reasoning, target_pct, size_multiplier]
  properties:
    decision:
      type: string
      enum: [approve, deny]
    reasoning:
      type: string
      maxLength: 200
    target_pct:
      type: number
      minimum: 0.005
      maximum: 0.05
    size_multiplier:
      type: number
      minimum: 0.5
      maximum: 2.0
    deny_reason:
      type: string
      enum: [sector_crowded, session_drawdown, weak_catalyst, timing, correlation]
```

### 4.2 PM Session Adaptation Prompt

```yaml
# config/prompts/pm_session_adaptation.yaml
version: 1
model: claude
timeout_ms: 3000
max_tokens: 512
fallback_action: keep_current
temperature: 0.1

system: |
  You are the senior Portfolio Manager reviewing DriftPilot's session performance.
  Based on the session's trade history, adapt trading parameters for the remainder.
  
  You CANNOT change:
  - Stop loss (always 1.5%, mechanical)
  - Max hold (always 60 min, mechanical)
  - Daily loss limit (always 3%, mechanical)
  
  You CAN adjust:
  - base_target_pct: [0.005, 0.05] — how much profit to aim for
  - max_concurrent: [3, 10] — how many positions at once
  - sector_limit: [1, 3] — max same-sector positions
  - entry_cooldown_sec: [0, 300] — pause between entries
  - min_confidence_threshold: [0.5, 0.9] — minimum catalyst confidence to trade
  
  ADAPTATION TRIGGERS (when this prompt fires):
  - 3 consecutive losses
  - 3 consecutive wins in <15 min each
  - Daily PnL crosses -1.5% (warning zone)
  - Daily PnL crosses +2% (expansion zone)
  - VIX spikes >20% intraday
  
  Your reasoning is logged as training data. Be specific.

user_template: |
  SESSION REVIEW — Trigger: {trigger_reason}
  
  TODAY'S TRADES:
  {trades_table}
  
  CURRENT PARAMETERS:
  base_target: {base_target}% | max_concurrent: {max_concurrent}
  sector_limit: {sector_limit} | cooldown: {cooldown}s
  min_confidence: {min_confidence}
  
  SESSION STATS:
  trades: {total_trades} | wins: {wins} | losses: {losses}
  daily_pnl: {daily_pnl}% | time_remaining: {time_remaining}min
  avg_winner: {avg_winner}% | avg_loser: {avg_loser}%
  avg_hold: {avg_hold}min
  
  MARKET:
  regime: {regime} | VIX: {vix} | SPY: {spy_change}%
  
  Recommend adjustments:

response_schema:
  type: object
  required: [adjustments, reasoning, confidence]
  properties:
    adjustments:
      type: object
      properties:
        base_target_pct: {type: number, minimum: 0.005, maximum: 0.05}
        max_concurrent: {type: integer, minimum: 3, maximum: 10}
        sector_limit: {type: integer, minimum: 1, maximum: 3}
        entry_cooldown_sec: {type: integer, minimum: 0, maximum: 300}
        min_confidence_threshold: {type: number, minimum: 0.5, maximum: 0.9}
    reasoning:
      type: string
      maxLength: 500
    confidence:
      type: number
      minimum: 0.0
      maximum: 1.0
```

### 4.3 Scanner Override Prompt

```yaml
# config/prompts/scanner_override.yaml
version: 1
model: qwen
timeout_ms: 500
max_tokens: 256
fallback_action: approve_algo
temperature: 0.0

system: |
  You are the Scanner Agent for DriftPilot. The algorithm has already scanned and
  produced candidates. Your job is to OVERRIDE only when you see something the
  algorithm cannot detect.
  
  WHEN TO SKIP (deny what algo accepted):
  - Headline is misclassified (sounds positive but is actually negative/neutral)
  - Stock already traded today on same catalyst (duplicate headline)
  - Beat magnitude is trivial (<1% EPS beat on large-cap serial beater)
  - Event is >15 minutes old and stock has already moved (priced in)
  
  WHEN TO FORCE-ENTER (approve what algo would miss):
  - Multi-catalyst stacking (beat + raise + buyback in same announcement)
  - Obvious catalyst not in taxonomy (major contract, FDA approval)
  
  DEFAULT: approve the algorithm's decision. You should override <15% of the time.
  If unsure, approve.
  
  Respond JSON only.

user_template: |
  CATALYST:
  symbol: {symbol} | category: {category}/{subcategory}
  headline: "{headline}"
  sentiment: {sentiment} | confidence: {confidence} | pm: {priority_mod}
  event_age: {age_min} min
  
  ALGORITHM SAID: {algo_action}
  candidates: {candidates_json}
  
  CONTEXT:
  already_traded_today: {already_traded}
  same_symbol_headlines_30min: {headline_count}
  sector_30min_performance: {sector_perf}%
  stock_move_since_event: {stock_move_pct}%

response_schema:
  type: object
  required: [action, reasoning]
  properties:
    action:
      type: string
      enum: [approve_algo, skip_all, modify]
    modifications:
      type: array
      items:
        type: object
        properties:
          symbol: {type: string}
          action: {type: string, enum: [approve, skip, force_enter]}
          target_pct_suggestion: {type: number, minimum: 0.005, maximum: 0.05}
    reasoning:
      type: string
      maxLength: 200
```

### 4.4 Slot Exit Override Prompt

```yaml
# config/prompts/slot_exit_override.yaml
version: 1
model: qwen
timeout_ms: 500
max_tokens: 256
fallback_action: hold
temperature: 0.0

system: |
  You are a Slot Agent managing one open position. The algorithm has decided to HOLD.
  You evaluate whether to override.
  
  ACTIONS (only called when algo says HOLD):
  - hold: agree with algorithm. DEFAULT. Use >85% of the time.
  - request_target_raise: momentum is clear, ask PM to raise target
    ONLY request when: price making new highs, volume > 2x avg, already past +1%
  - request_partial_profit: position stuck, ask PM to let you take what's there
    ONLY request when: unrealized > +0.5%, held > 25 min, volume declining
  - request_early_cut: thesis is broken, ask PM to exit before stop loss
    ONLY request when: new negative headline, sector dropped >1%, bid collapsing
  
  REMEMBER:
  - You CANNOT exit without PM approval (except algo-triggered exits)
  - You CANNOT raise your own target
  - If you're unsure → hold. Algo handles the base case correctly.
  - Your override should be RARE and HIGH-CONVICTION
  
  Respond JSON only.

user_template: |
  POSITION:
  symbol: {symbol}
  entry: ${entry_price} | current: ${current_price}
  unrealized: {unrealized_pct}% | target: {target_pct}% | stop: {stop_pct}%
  held: {hold_min} min | max_hold: {max_hold_min} min
  
  PRICE ACTION (last 10 one-min bars):
  closes: {last_10_closes}
  volumes: {last_10_volumes}
  high_since_entry: {high_pct}% | low_since_entry: {low_pct}%
  bars_at_current_level: {consolidation_bars}
  
  VOLUME:
  recent_10min: {recent_vol} | avg_daily: {avg_vol} | ratio: {rvol}x
  
  MARKET:
  sector_move: {sector_move}% | SPY_move: {spy_move}% | VIX: {vix}
  
  NEW HEADLINES SINCE ENTRY:
  {new_headlines}
  
  ALGO SAYS: HOLD (not at target, not at stop, not at time limit)
  YOUR DECISION:

response_schema:
  type: object
  required: [action, reasoning, confidence]
  properties:
    action:
      type: string
      enum: [hold, request_target_raise, request_partial_profit, request_early_cut]
    target_raise_to_pct:
      type: number
      minimum: 0.015
      maximum: 0.05
    reasoning:
      type: string
      maxLength: 200
    confidence:
      type: number
      minimum: 0.0
      maximum: 1.0
```

---

## 5. A2A Message Protocol

### Message Envelope

```json
{
  "msg_id": "uuid4",
  "msg_type": "ENTRY_REQUEST",
  "from_agent": "scanner",
  "to_agent": "pm",
  "correlation_id": "uuid4",
  "timestamp": "2026-05-10T10:35:00.123Z",
  "payload": {},
  "status": "pending"
}
```

### Message Types

| Type | From | To | Purpose |
|---|---|---|---|
| ENTRY_REQUEST | Scanner | PM | "I want to buy this" |
| ENTRY_DECISION | PM | Scanner | "Approved/Denied" |
| ASSIGNMENT | PM | Slot N | "You manage this stock now" |
| TARGET_RAISE_REQUEST | Slot N | PM | "Momentum building, raise target?" |
| TARGET_RAISE_DECISION | PM | Slot N | "Approved to X% / Denied" |
| PARTIAL_PROFIT_REQUEST | Slot N | PM | "Stuck, take partial?" |
| PARTIAL_PROFIT_DECISION | PM | Slot N | "Approved / Denied" |
| EARLY_CUT_REQUEST | Slot N | PM | "Thesis broken, cut now?" |
| EARLY_CUT_DECISION | PM | Slot N | "Approved / Denied, wait" |
| FORCE_EXIT | PM | Slot N | "Exit now, portfolio risk" |
| EXIT_REPORT | Slot N | PM | "Position closed, here's what happened" |
| SESSION_ADAPTATION | PM | All | "New parameters for rest of session" |

### Message TTL

Messages expire after 5 minutes (configurable). An expired ENTRY_REQUEST means the opportunity passed — Scanner doesn't retry. An expired TARGET_RAISE_REQUEST means PM didn't respond in time — Slot Agent continues with current target.

---

## 6. Configuration Schema

### New Settings (extend DriftPilotSettings)

```python
# All env-var configurable, all have sensible defaults

# Master switch
agent_enabled: bool = False                        # AGENT_ENABLED

# Loop intervals
agent_pm_interval_seconds: int = 30                # AGENT_PM_INTERVAL_SECONDS
agent_slot_interval_seconds: int = 30              # AGENT_SLOT_INTERVAL_SECONDS

# LLM endpoints
agent_qwen_url: str = "http://192.168.1.166:8000/v1"
agent_qwen_model: str = "Qwen/Qwen3-8B"
agent_qwen_timeout_ms: int = 500
agent_claude_model: str = "claude-sonnet-4-20250514"
agent_claude_timeout_ms: int = 3000

# Safety
agent_max_override_rate: float = 0.20              # disable overrides if exceeded
agent_override_cooldown_minutes: int = 30          # cooldown after rate exceeded

# Prompts
agent_prompts_dir: str = "config/prompts"

# Storage
agent_message_db_path: str = "data/driftpilot/agent_messages.sqlite3"
agent_message_ttl_seconds: int = 300

# Observability
agent_log_all_decisions: bool = True
agent_training_data_export: bool = True
```

### Hardcoded (NEVER configurable)

```python
MAX_STOP_LOSS_PCT = 0.015           # 1.5%
MAX_PROFIT_CAP_PCT = 0.05           # 5%
MAX_HOLD_MINUTES = 60               # 60 min
DAILY_LOSS_LIMIT_PCT = 0.03         # 3%
MAX_SLOTS = 10                      # 10 positions
MAX_PER_SECTOR = 3                  # 3 same sector
MAX_SIZE_MULTIPLIER = 2.0           # PM can't go above 2x
MIN_SIZE_MULTIPLIER = 0.5           # PM can't go below 0.5x
MIN_HOLD_BEFORE_AGENT_EXIT = 120    # 2 min minimum hold (prevent churn)
```

---

## 7. DB Schema

```sql
-- migrations/006_agent_layer.sql

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT NOT NULL UNIQUE,
    msg_type TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    correlation_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    processed_at TEXT,
    expired_at TEXT,
    CONSTRAINT chk_msg_type CHECK (msg_type IN (
        'ENTRY_REQUEST', 'ENTRY_DECISION', 'ASSIGNMENT',
        'TARGET_RAISE_REQUEST', 'TARGET_RAISE_DECISION',
        'PARTIAL_PROFIT_REQUEST', 'PARTIAL_PROFIT_DECISION',
        'EARLY_CUT_REQUEST', 'EARLY_CUT_DECISION',
        'FORCE_EXIT', 'EXIT_REPORT', 'SESSION_ADAPTATION'
    )),
    CONSTRAINT chk_status CHECK (status IN ('pending', 'acked', 'processed', 'expired'))
);
CREATE INDEX idx_msg_to_status ON agent_messages(to_agent, status, created_at);
CREATE INDEX idx_msg_correlation ON agent_messages(correlation_id);

CREATE TABLE IF NOT EXISTS agent_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    symbol TEXT,
    slot_id INTEGER,
    algo_recommendation TEXT NOT NULL,
    agent_decision TEXT NOT NULL,
    is_override INTEGER NOT NULL DEFAULT 0,
    reasoning TEXT NOT NULL,
    confidence REAL,
    llm_model TEXT NOT NULL,
    llm_latency_ms INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    raw_response TEXT NOT NULL,
    outcome_pnl_pct REAL,
    outcome_correct INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_decisions_agent ON agent_decisions(agent_name, created_at);
CREATE INDEX idx_decisions_override ON agent_decisions(is_override, outcome_correct);

CREATE TABLE IF NOT EXISTS agent_session_params (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    param_name TEXT NOT NULL,
    old_value REAL NOT NULL,
    new_value REAL NOT NULL,
    reason TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_state (
    agent_name TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'idle',
    last_tick_at TEXT,
    consecutive_wins INTEGER NOT NULL DEFAULT 0,
    consecutive_losses INTEGER NOT NULL DEFAULT 0,
    override_count_today INTEGER NOT NULL DEFAULT 0,
    total_decisions_today INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
```

---

## 8. Mechanical Guardrails

Enforced at execution layer (SlotAllocator + Broker), BELOW the agent. No message or decision can bypass.

```python
class GuardrailValidator:
    """Called BEFORE any agent decision is executed.
    Returns the clamped/valid version of the decision.
    Logs a violation if clamping was necessary."""
    
    def validate_entry(self, decision) -> ValidatedEntry:
        assert decision.stop_pct <= 0.015  # clamp
        assert decision.target_pct <= 0.05  # clamp
        assert decision.size_multiplier <= 2.0  # clamp
        assert free_slots > 0
        assert sector_count < 3
        assert daily_pnl > -0.03
        
    def validate_exit(self, decision) -> ValidatedExit:
        assert hold_minutes >= 2  # no churn
        
    def validate_target_raise(self, decision) -> ValidatedRaise:
        assert new_target <= 0.05  # hard cap
        assert new_stop >= 0  # trailing stop can't be negative
```

---

## 9. Failure Modes and Fallback

| Failure | Detection | Fallback | Recovery |
|---------|-----------|----------|----------|
| Qwen offline | httpx timeout 500ms | Follow algo exactly | Auto-retry next tick |
| Qwen returns garbage | JSON parse or schema validation fails | Follow algo | Log + alert |
| Claude offline | httpx timeout 3s | Keep current session params | Retry in 5 min |
| PM agent crashes | No heartbeat for 60s | Auto-approve algo-passed entries | Restart PM |
| Slot agent crashes | No heartbeat for 60s | Run signal.evaluate_exit() only | Restart slot |
| Scanner agent crashes | No events processed 2 cycles | Run algo pipeline without LLM | Restart |
| Override rate >20% | Counter in agent_state | Disable all LLM overrides 30 min | Auto-reset |
| LLM suggests guardrail violation | GuardrailValidator catches | Clamp to valid value | Log as anomaly |
| Message bus saturated | >100 pending messages | Expire all, fresh start | Auto-drain |
| Agent-only: worse P&L than algo | Daily comparison | Disable agent, revert to algo | Manual review |

---

## 10. Test Cases

### Unit Tests (mocked LLM)

```
tests/agents/test_pm_agent.py
  - test_approve_valid_entry
  - test_deny_sector_crowded (3 same sector open)
  - test_deny_session_drawdown (daily PnL < -2.5%)
  - test_tighten_after_3_losses (target reduced)
  - test_expand_after_3_fast_wins (target expanded)
  - test_force_exit_on_drawdown (daily PnL approaching -3%)
  - test_fallback_on_qwen_timeout (auto-approve)
  - test_guardrail_clamp_target_above_5pct
  - test_guardrail_clamp_size_above_2x
  - test_deny_last_30_minutes_of_session

tests/agents/test_scanner_agent.py
  - test_approve_algo_default
  - test_skip_misclassified_headline
  - test_skip_duplicate_headline
  - test_skip_stale_event_priced_in
  - test_force_enter_multi_catalyst
  - test_fallback_follows_algo_on_timeout
  - test_override_rate_limit

tests/agents/test_slot_agent.py
  - test_hold_default (>85% of decisions)
  - test_request_target_raise_strong_momentum
  - test_request_partial_stuck_30_min
  - test_request_early_cut_thesis_broken
  - test_algo_exit_skips_llm_entirely
  - test_fallback_hold_on_timeout
  - test_does_not_act_without_pm_approval
  - test_respects_min_hold_time

tests/agents/test_message_bus.py
  - test_send_receive_roundtrip
  - test_message_expiry_after_ttl
  - test_correlation_id_links_request_response
  - test_concurrent_access_no_deadlock
  - test_agent_polls_only_own_messages

tests/agents/test_guardrail_validator.py
  - test_clamp_stop_above_1_5_pct
  - test_clamp_target_above_5_pct
  - test_clamp_size_above_2x
  - test_reject_entry_when_daily_limit_hit
  - test_reject_entry_when_no_free_slot
  - test_reject_exit_before_min_hold
  - test_guardrail_never_raises_exception (always returns clamped value)

tests/agents/test_llm_client.py
  - test_qwen_call_within_timeout
  - test_qwen_fallback_on_timeout
  - test_qwen_fallback_on_invalid_json
  - test_claude_call_for_session_adaptation
  - test_prompt_loaded_from_yaml
  - test_response_validated_against_schema
  - test_decision_logged_to_db
```

### Integration Tests

```
tests/agents/test_agent_integration.py
  - test_full_entry_flow (catalyst → scanner → PM → slot assignment)
  - test_full_exit_flow (slot evaluation → exit → report to PM)
  - test_target_raise_flow (slot request → PM approve → stop adjusted)
  - test_force_exit_cascade (PM force → all slots exit)
  - test_session_adaptation_flow (3 losses → Claude adapts → params changed)
  - test_guardrail_cannot_be_bypassed (evil LLM → all blocked)
  - test_agent_disabled_reverts_to_algo (AGENT_ENABLED=false → pure algo)
  - test_restart_recovery (agent restarts → picks up from DB state)
```

### Simulation Tests (replay with cached LLM)

```
tests/agents/test_agent_replay.py
  - test_replay_determinism (same inputs + cached LLM = same outputs)
  - test_agent_vs_algo_comparison (100 events, compare edge ratios)
  - test_override_accuracy_tracking (were overrides correct?)
  - test_no_guardrail_violations_in_replay (never violated in 1000 events)
```

---

## 11. Agent Breakdown for Coding

### Wave 1: Foundation (2 parallel agents, no dependencies)

**Agent A: Message Bus + DB Schema**

```
Creates:
  src/driftpilot/agents/__init__.py
  src/driftpilot/agents/models.py          (Pydantic message schemas)
  src/driftpilot/agents/message_bus.py     (SQLite-backed A2A bus)
  migrations/006_agent_layer.sql
  tests/agents/__init__.py
  tests/agents/test_message_bus.py

Patterns to follow:
  src/driftpilot/catalyst/event_bus.py     (async pub/sub pattern)
  src/driftpilot/storage/repositories.py   (SQLite access pattern)
```

**Agent B: LLM Client + Prompt Loader + Guardrails**

```
Creates:
  src/driftpilot/agents/llm_client.py      (dual Qwen + Claude, timeout, fallback)
  src/driftpilot/agents/prompt_loader.py   (YAML config loader, hot-reload)
  src/driftpilot/agents/guardrail_validator.py
  config/prompts/_schema.yaml
  config/prompts/pm_entry_approval.yaml
  config/prompts/pm_session_adaptation.yaml
  config/prompts/scanner_override.yaml
  config/prompts/slot_exit_override.yaml
  tests/agents/test_llm_client.py
  tests/agents/test_prompt_loader.py
  tests/agents/test_guardrail_validator.py

Patterns to follow:
  src/driftpilot/catalyst/qwen_enricher.py (httpx + timeout + JSON parse)
```

### Wave 2: Agents (3 parallel, depends on Wave 1)

**Agent C: PM Agent**

```
Creates:
  src/driftpilot/agents/pm_agent.py
  tests/agents/test_pm_agent.py

Uses: message_bus, llm_client, prompt_loader, guardrail_validator
Reads: slot_allocator (for portfolio state)
```

**Agent D: Scanner Agent**

```
Creates:
  src/driftpilot/agents/scanner_agent.py
  tests/agents/test_scanner_agent.py

Uses: message_bus, llm_client, prompt_loader
Reads: signal_router, signal.scan(), catalyst event_bus
```

**Agent E: Slot Agent**

```
Creates:
  src/driftpilot/agents/slot_agent.py
  tests/agents/test_slot_agent.py

Uses: message_bus, llm_client, prompt_loader, guardrail_validator
Reads: signal.evaluate_exit(), broker (quotes, bars)
```

### Wave 3: Integration (1 agent, depends on Wave 2)

**Agent F: Orchestrator + Operator Wiring**

```
Creates:
  src/driftpilot/agents/orchestrator.py    (lifecycle, tick loop, restart logic)
  tests/agents/test_agent_integration.py
  tests/agents/test_agent_replay.py

Modifies:
  src/driftpilot/operator.py              (add agent startup alongside catalyst layer)
  src/driftpilot/settings.py              (add agent_* settings)
```

### Wave 4: Dashboard + Observability (1 agent, depends on Wave 3)

**Agent G: Agent Decision Panel + Training Export**

```
Creates:
  src/driftpilot/agents/training_exporter.py
  src/driftpilot/dashboard/agent_views.py
  src/trading_bot/dashboard/templates/agents.html
  tests/agents/test_training_exporter.py

Modifies:
  src/trading_bot/dashboard/app.py        (add /agents page + API endpoints)
  src/driftpilot/dashboard/view_models.py (agent decision payloads)
```

### Merge Order

```
Wave 1A (Bus) ──┐
                ├── merge to feature/agent-layer branch
Wave 1B (LLM) ─┘
                     │
Wave 2C (PM) ───────┐│
Wave 2D (Scanner) ──┤├── merge (all depend on Wave 1)
Wave 2E (Slot) ─────┘│
                      │
Wave 3F (Integration) ── merge (depends on all Wave 2)
                      │
Wave 4G (Dashboard) ── merge (depends on Wave 3)
                      │
                      └── feature/agent-layer merged to main
```

---

## 12. Code Review Checklist

Every PR in every wave gets reviewed against:

- [ ] **Guardrails intact:** No path from agent decision to broker order bypasses GuardrailValidator
- [ ] **Timeout + fallback:** Every LLM call has hard timeout AND explicit fallback behavior
- [ ] **Decision logged:** Every LLM decision written to agent_decisions with full prompt + response
- [ ] **Schema validated:** Every LLM response parsed through Pydantic model, invalid → fallback
- [ ] **Override rate checked:** Override counter checked before executing any LLM override
- [ ] **Signals untouched:** No modifications to existing signal scan/exit logic
- [ ] **Prompts external:** All prompt text loaded from config/prompts/*.yaml, not hardcoded
- [ ] **SQL parameterized:** No f-string SQL (injection risk)
- [ ] **Async safe:** No await inside a lock, no unbounded queues
- [ ] **Restartable:** All critical state in DB, no in-memory-only state that survives a cycle
- [ ] **Tests cover failure:** Each test file has timeout, garbage response, and guardrail-violation tests
- [ ] **Backward compatible:** AGENT_ENABLED=false → system behaves exactly like before
- [ ] **Type complete:** mypy clean on all new files
- [ ] **Lint clean:** ruff check passes on all new/modified files

---

## 13. Alpha Validation Framework

### A/B Comparison Protocol

After all agents merge and paper trading begins:

1. **Parallel tracking:** Every cycle, record both:
   - What the algo WOULD have done (deterministic path)
   - What the agent actually DID (LLM-augmented path)

2. **Daily scorecard:**
   ```
   | Metric            | Algo-only | Agent | Delta |
   |-------------------|-----------|-------|-------|
   | Trades taken      |     8     |   7   |  -1   |
   | Win rate          |   62%     |  71%  | +9%   |
   | Avg winner        |  1.0%     | 1.4%  | +0.4% |
   | Avg loser         | -1.5%     | -1.1% | +0.4% |
   | Overrides         |    0      |   3   |  +3   |
   | Override accuracy  |   N/A     |  67%  |       |
   | Daily PnL         | +$42      | +$67  | +$25  |
   ```

3. **Weekly review:** If agent path consistently beats algo path by >0.3% daily → confidence grows. If not → investigate override quality.

4. **Kill switch trigger:** Agent disabled automatically if override accuracy drops below 55% for 3 consecutive days.

### Training Data Export

Every agent decision is exportable as JSONL for future fine-tuning:

```jsonl
{"inputs": {...}, "decision": "hold", "outcome_pnl_pct": 0.012, "was_override": false, "algo_would_have": "hold"}
{"inputs": {...}, "decision": "request_target_raise", "outcome_pnl_pct": 0.019, "was_override": true, "algo_would_have": "exit_at_1pct"}
```

This creates the dataset for fine-tuning a domain-specific model that replaces generic Qwen with a trained trader model (Phase 5, future).
