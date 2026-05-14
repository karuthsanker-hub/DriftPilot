# Feature: Market Index Guardrails + Volume-Spike Signal

## Overview

Two complementary features to improve DriftPilot's risk management and trade selection:

1. **Market Index Guardrails** — Monitor SPY, DIA (Dow), QQQ (NASDAQ) in real time. Pause or throttle new entries when broad indices are selling off. Protects the portfolio from opening long catalyst trades into a falling market.

2. **Volume-Spike Signal** — Detect symbols with abnormal intraday volume surges and trade the momentum. High relative volume is the single strongest predictor of short-term continuation after a catalyst. This gives us a second entry criteria beyond headline sentiment.

---

## Part 1: Market Index Guardrails

### Strategy

The current system opens long positions purely on catalyst events (earnings, filings, analyst upgrades). It has **zero market-level awareness** — it will happily buy into a -3% SPY selloff. This is the single biggest unhedged risk.

The guardrail is a **three-tier circuit breaker** based on real-time index performance:

| Tier | Condition | Action |
|------|-----------|--------|
| **GREEN** | All three indices > -0.5% from open | Normal trading. No restrictions. |
| **CAUTION** | Any index between -0.5% and -1.5% from open | Reduce new entries to 50% of normal rate. Require higher catalyst score (2x threshold). Tighten stop losses by 25%. |
| **RED** | Any index > -1.5% from open, OR 2+ indices > -1.0% | **Pause all new entries.** Existing positions continue with tightened stops. Resume when conditions return to CAUTION for 5+ minutes. |

Why these thresholds:
- A -0.5% intraday move happens ~30% of trading days — frequent enough to be meaningful, rare enough not to constantly trigger
- A -1.5% move happens ~5% of days — this is a genuine risk-off event where long catalyst momentum evaporates
- Using all three indices prevents false triggers from single-stock sector rotation (e.g., tech selloff doesn't kill a healthcare earnings trade if DIA is flat)

### Data Flow

```
AlpacaSIPStream (already exists)
  → Subscribe to SPY, DIA, QQQ minute bars (SPY already subscribed)
  → Feed into new IndexGuardrail class
  → IndexGuardrail computes tier every bar update
  → SlotAllocator checks tier before allocating
  → RuntimeConfig holds thresholds (hot-reloadable via /admin)
```

### Requirements

#### REQ-G1: Subscribe to DIA and QQQ bars
- **File**: `src/driftpilot/market_data/alpaca_stream.py`
- **Change**: Add `"DIA"` and `"QQQ"` to the `ALWAYS_ON_SYMBOLS` set alongside `"SPY"`
- **Test**: Unit test confirming all three symbols are in the subscription list

#### REQ-G2: IndexGuardrail class
- **New file**: `src/driftpilot/guardrails/index_guardrail.py`
- **Class**: `IndexGuardrail`
  - Constructor takes thresholds (caution_pct, red_pct, red_multi_pct, resume_minutes) with defaults from strategy table above
  - `update(symbol: str, bar: MarketBar)` — called on every bar for SPY/DIA/QQQ. Stores latest bar and session open price.
  - `tier() -> Literal["GREEN", "CAUTION", "RED"]` — computes current tier from stored bars
  - `effective_since() -> datetime` — when the current tier started (needed for the 5-minute resume rule)
  - `summary() -> dict` — returns `{"tier": "GREEN", "spy_pct": -0.3, "dia_pct": 0.1, "qqq_pct": -0.8}` for logging/dashboard
- **State**: Stores the session open price for each index (first bar after 9:30 ET) and latest bar
- **Test**: Unit tests for each tier transition, including the resume hysteresis (RED → must stay CAUTION for 5 min before GREEN)

#### REQ-G3: Integrate guardrail into SlotAllocator
- **File**: `src/driftpilot/execution/slot_allocator.py`
- **Change**: Add `index_guardrail: IndexGuardrail | None = None` parameter to constructor
- **Logic in `allocate()`**:
  - If guardrail is None, skip (backward compatible)
  - If tier is RED: reject ALL candidates with reason `"market_guardrail_red"`
  - If tier is CAUTION: reject candidates with `score < caution_min_score` (default 0.15) with reason `"market_guardrail_caution"`
- **New rejection reason**: Add `"market_guardrail_red"` and `"market_guardrail_caution"` to AllocationRejection
- **Test**: Unit tests for GREEN (all pass), CAUTION (low-score rejected, high-score passes), RED (all rejected)

#### REQ-G4: Wire guardrail in operator
- **File**: `src/driftpilot/operator.py`
- **Change**: Instantiate `IndexGuardrail` and pass to `SlotAllocator`. Connect `AlpacaSIPStream` bar callbacks to `guardrail.update()`.
- **Prerequisite**: REQ-G1 (DIA/QQQ subscriptions) must be done first

#### REQ-G5: Hot-reloadable thresholds
- **File**: `src/driftpilot/runtime_config.py`
- **Change**: Add fields:
  - `guardrail_caution_pct: float = 0.5` (% drop from open to trigger CAUTION)
  - `guardrail_red_pct: float = 1.5` (% drop from open to trigger RED)
  - `guardrail_red_multi_pct: float = 1.0` (% drop threshold when 2+ indices are down)
  - `guardrail_resume_minutes: int = 5` (minutes in CAUTION before returning to GREEN)
  - `guardrail_caution_min_score: float = 0.15` (minimum catalyst score in CAUTION)
  - `guardrail_enabled: bool = True` (kill switch)
- **Test**: Verify defaults load correctly and hot-reload picks up changes

#### REQ-G6: Dashboard display
- **File**: `src/trading_bot/dashboard/app.py` (or wherever the dashboard API lives)
- **Change**: Add a `/api/guardrail` endpoint returning `IndexGuardrail.summary()`
- **Frontend**: Show a colored badge (green/yellow/red) with SPY/DIA/QQQ change percentages

#### REQ-G7: Slot Manager integration
- **File**: `scripts/slot_manager.py`
- **Change**: Include guardrail tier in the health report sent to Qwen. The slot manager should log warnings when the tier changes (GREEN→CAUTION, CAUTION→RED, etc.)

---

## Part 2: Volume-Spike Signal

### Strategy

Volume precedes price. When a stock suddenly trades 3-5x its normal volume in a short window, it means institutional flow is moving the name. Combined with a catalyst event, this is the highest-conviction setup for catching 1%+ moves.

The signal works in two modes:

**Mode A: Volume Gate on Catalyst Entries (enhancement to existing signals)**
- Before opening a catalyst-driven position, check if the symbol's current volume rate is elevated
- Require RVOL > 2.0 (2x normal volume for this time of day) to enter
- This filters out "dead" catalysts where the news dropped but nobody is trading on it

**Mode B: Pure Volume-Spike Scanner (new standalone signal)**
- Scan all symbols with active minute bars for sudden volume surges
- Trigger: Volume in the last 5 minutes > 5x the average 5-minute volume for that time of day
- Additional filter: Price must be moving in the same direction as volume (not just a large block crossing at the bid)
- Hold: 15-30 minutes (momentum window)
- Exit: Trailing stop at 0.5%, hard stop at -0.8%, time stop at 30 minutes

Why this works:
- Volume spikes are observable before price fully adjusts — institutions can't hide 5x normal volume
- The 5-minute window filters out single-print noise (one large block) vs. sustained flow
- Combined with directional price confirmation, this avoids the "volume at the bid" trap (large seller creating volume but price declining)

### Data Flow

```
AlpacaSIPStream
  → Minute bars for watched symbols (already delivered)
  → Feed into VolumeTracker
  → VolumeTracker maintains rolling 5-min volume + 20-day baseline
  → Mode A: SlotAllocator queries VolumeTracker before entry
  → Mode B: VolumeSpike signal.scan() returns Candidates from VolumeTracker
```

### Requirements

#### REQ-V1: Wire AlpacaSIPStream as bar_provider
- **File**: `src/driftpilot/operator.py` (line ~335, existing TODO)
- **Change**: Connect `AlpacaSIPStream` to the operator's bar delivery pipeline so live bars are available to signals and the allocator
- **This is a prerequisite** for both Mode A and Mode B

#### REQ-V2: VolumeTracker class
- **New file**: `src/driftpilot/market_data/volume_tracker.py`
- **Class**: `VolumeTracker`
  - `update(symbol: str, bar: MarketBar)` — called on every minute bar. Maintains:
    - Rolling 5-minute volume sum for each symbol
    - Session cumulative volume per symbol
    - 20-day same-time-of-day average volume (loaded from DB or bar history at startup)
  - `rvol_now(symbol: str) -> float` — real-time relative volume: (session volume so far) / (expected volume by this time of day). Returns 1.0 if no history.
  - `volume_spike(symbol: str, window_minutes: int = 5, threshold: float = 5.0) -> bool` — True if recent volume exceeds threshold * baseline
  - `spike_candidates(threshold: float = 5.0) -> list[tuple[str, float]]` — all symbols currently spiking, sorted by spike magnitude
  - `price_direction(symbol: str, window_minutes: int = 5) -> float` — % price change over the volume window (positive = buyers winning)
- **Bootstrap**: Load 20-day intraday volume profiles from Alpaca historical bars API at startup. Store in `data/driftpilot/volume_profiles.sqlite3`
- **Test**: Unit tests with synthetic bar data for spike detection, direction confirmation, and baseline computation

#### REQ-V3: Volume gate on catalyst entries (Mode A)
- **File**: `src/driftpilot/execution/slot_allocator.py`
- **Change**: Add `volume_tracker: VolumeTracker | None = None` parameter
- **Logic in `allocate()`**:
  - If volume_tracker is None, skip (backward compatible)
  - For each candidate, check `rvol_now(symbol)`. If RVOL < `min_entry_rvol` (default 2.0), reject with reason `"low_volume"`
  - Hot-reloadable threshold via RuntimeConfig
- **New rejection reason**: `"low_volume"`
- **Test**: Candidate with RVOL 1.5 rejected, RVOL 3.0 accepted

#### REQ-V4: Volume-Spike signal (Mode B)
- **New directory**: `src/driftpilot/signals/volume_spike_v1/`
- **Files**: `__init__.py`, `config.py`, `signal.py`, `exits.py`
- **Signal class**: `VolumeSpikeSignal`
  - No bus subscription needed — pulls from VolumeTracker directly
  - `scan(now)`: Calls `volume_tracker.spike_candidates()`, filters by:
    - `volume_spike() == True` (5x threshold)
    - `price_direction() > 0.1%` (buyers winning)
    - Not already in an active position
    - Price > $5 (avoid penny stocks)
    - Spread < 0.5% (liquid enough to exit cleanly)
  - Returns `Candidate` per qualifying symbol with features including spike_magnitude, rvol, price_direction
  - `evaluate_exit()`: Time stop at 30min, trailing stop at 0.5% from peak, hard stop at -0.8%
- **Config** (`VolumeSpikeConfig`):
  - `spike_threshold: float = 5.0` (5x normal volume)
  - `spike_window_minutes: int = 5`
  - `min_price_direction_pct: float = 0.1`
  - `max_hold_minutes: int = 30`
  - `profit_take_pct: float = 1.0`
  - `stop_loss_pct: float = 0.8`
  - `trailing_activation_pct: float = 0.5`
  - `trailing_distance_pct: float = 0.3`
  - `min_price: float = 5.0`
  - `max_spread_pct: float = 0.5`
- **Test**: Synthetic volume data → signal fires when spike + direction align, doesn't fire on volume-at-bid

#### REQ-V5: Volume profile bootstrap
- **New file**: `scripts/bootstrap_volume_profiles.py`
- **Purpose**: Pull 20 days of 1-minute bars from Alpaca for the trading universe (~1500 symbols) and compute time-of-day volume profiles
- **Output**: `data/driftpilot/volume_profiles.sqlite3` with schema:
  ```sql
  CREATE TABLE volume_profiles (
    symbol TEXT,
    minute_of_day INTEGER,  -- 0-389 (9:30=0, 15:59=389)
    avg_volume REAL,
    median_volume REAL,
    stddev_volume REAL,
    sample_days INTEGER,
    PRIMARY KEY (symbol, minute_of_day)
  );
  ```
- **Integration**: `daily_operator.sh` runs this at startup (after catalyst warm-up, before operator launch)
- **Test**: Verify profile computation against known bar data

#### REQ-V6: Hot-reloadable volume config
- **File**: `src/driftpilot/runtime_config.py`
- **Change**: Add fields:
  - `volume_gate_enabled: bool = False` (off by default until validated)
  - `volume_gate_min_rvol: float = 2.0`
  - `volume_spike_signal_enabled: bool = False`
  - `volume_spike_threshold: float = 5.0`
  - `volume_spike_window_minutes: int = 5`
- **Note**: Both features ship disabled by default. Enable via `/admin` after paper validation.

#### REQ-V7: Add volume_spike_v1 to signal registry
- **File**: `src/driftpilot/operator.py` (in `_build_signal()`)
- **Change**: Add `volume_spike_v1` case that constructs `VolumeSpikeSignal` with `VolumeTracker`
- **Activation**: Add to `ACTIVE_SIGNAL` env var when ready (e.g., `"earnings_report_v1,filing_8a_v1,analyst_target_raise_v1,volume_spike_v1"`)

---

## Implementation Order

**Phase 1 — Guardrails (protect what we have)**
1. REQ-G1: Subscribe DIA/QQQ bars
2. REQ-G2: IndexGuardrail class
3. REQ-G5: RuntimeConfig thresholds
4. REQ-G3: Allocator integration
5. REQ-G4: Operator wiring
6. REQ-G6: Dashboard display
7. REQ-G7: Slot Manager integration

**Phase 2 — Volume infrastructure**
1. REQ-V1: Wire AlpacaSIPStream (unblocks everything)
2. REQ-V5: Bootstrap volume profiles
3. REQ-V2: VolumeTracker class

**Phase 3 — Volume features**
1. REQ-V3: Volume gate on catalyst entries (Mode A — low risk, immediate value)
2. REQ-V6: RuntimeConfig fields
3. REQ-V4: Volume-Spike signal (Mode B — new signal, needs paper validation)
4. REQ-V7: Signal registry integration

---

## Dependencies

```
REQ-G1 ──→ REQ-G2 ──→ REQ-G3 ──→ REQ-G4
                  └──→ REQ-G5      │
                  └──→ REQ-G6      │
                  └──→ REQ-G7      │
                                   │
REQ-V1 ──→ REQ-V2 ──→ REQ-V3 ────┘ (both plug into allocator)
       └──→ REQ-V5      └──→ REQ-V4 ──→ REQ-V7
                         └──→ REQ-V6
```

## Validation Plan

- **Guardrails**: Backtest the 2024 catalyst trades with index overlay. Measure: how many losing trades would have been avoided by RED tier? Expected: 30-50% of -1%+ losers occur on RED days.
- **Volume Gate (Mode A)**: Backtest catalyst entries with/without RVOL>2 filter. Measure: win rate improvement. Expected: win rate improves 5-10pp when filtering for high volume.
- **Volume Spike (Mode B)**: Paper trade for 2 weeks before going live. Measure: hit rate on 1% target within 30min. Expected: 40%+ hit rate on 5x volume spikes with positive direction.
