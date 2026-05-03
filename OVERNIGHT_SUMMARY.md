> 📜 **HISTORICAL — review snapshot.** Captures Phases 0–4 status during a
> parallel-agent overnight run. Phases 5–16 plus the four-signal registry have
> all landed since this snapshot. Current state: see
> [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md) and
> [docs/DOCS_INDEX.md](docs/DOCS_INDEX.md).

---

# DriftPilot Overnight Summary

Run scope: Phases 0-4 only.

Stop condition reached: **Phases 0-4 are complete and reviewed. Phase 5 was not started.**

## Branches And SHAs

| Phase | Branch | Status | Current SHA |
|---|---|---|---|
| Phase 0 | `refactor/phase-0-1-foundation` | complete | current branch head |
| Phase 1 | `refactor/phase-0-1-foundation` | complete | current branch head |
| Phase 2 | `refactor/phase-2-broker` | complete / reviewed | `83d5e0a` |
| Phase 3 | `refactor/phase-3-signals` | complete / reviewed | `407e1f1` |
| Phase 4 | `refactor/phase-4-allocator` | complete / reviewed | `2fc861b` |

## Per-Phase Status

### Phase 0: Plan And Baseline

Status: **complete**

Completed:

- Created `REFACTOR_PLAN.md`.
- Added `AGENTS.md`.
- Updated resolved decisions for RVOL, SPY-only v1 regime, and typical-price VWAP.

Relevant commits:

- `e25ed30 phase-0: plan - add refactor spec and agent instructions`
- `6ce7b8a phase-0: plan - resolve signal decisions`

### Phase 1: Foundation

Status: **complete**

Completed:

- Added `src/driftpilot/settings.py`.
- Added `src/driftpilot/clock.py`.
- Added SQLite schema and repositories, including `daily_counters`.
- Added order timeout settings, live gate flags, and `$26,000` PDT floor default.
- Restored previously ignored legacy `src/trading_bot/data` modules required by existing tests.

Latest foundation commit:

- `phase-1: summary - update overnight status`

Checks:

- `uv run --extra test pytest tests/test_driftpilot_foundation.py -q` — PASS, 6 passed

### Phase 2: Broker

Status: **complete / reviewed**

Branch/worktree:

- `refactor/phase-2-broker`
- `/Users/karuthsanker/Documents/driftpilot-worktrees/phase-2-broker`

Completed:

- Alpaca broker client.
- SIP-only Alpaca stream guard.
- Two-tier subscription routing with persisted discovery shard cursor.
- Marketable-limit entry and exit order paths.
- Exit cancel-replace-emergency-market fallback.
- Boot reconciliation against broker open positions.
- Full live gate enforcement before live orders.
- Quote-unavailable and emergency fallback transition logging.

Latest SHA:

- `83d5e0a phase-2: review - approve broker fixes`

Checks:

- `uv run --extra test pytest tests/test_driftpilot_phase2.py tests/test_driftpilot_foundation.py -q` — PASS, 15 passed, 1 warning
- `uv run --extra test pytest -q` — PASS, 127 passed, 1 warning
- `uvx --with python-dotenv --with pytest mypy src/driftpilot tests/test_driftpilot_phase2.py tests/test_driftpilot_foundation.py` — PASS
- `uvx ruff check src/driftpilot/broker src/driftpilot/market_data src/driftpilot/storage tests/test_driftpilot_phase2.py` — PASS
- `git diff --check refactor/phase-0-1-foundation...HEAD` — PASS

Review:

- `PHASE_2_REVIEW.md` committed and approved.

### Phase 3: Signals

Status: **complete / reviewed**

Branch/worktree:

- `refactor/phase-3-signals`
- `/Users/karuthsanker/Documents/driftpilot-worktrees/phase-3-signals`

Completed:

- Bar feature cache.
- Typical-price VWAP.
- RVOL using current 1-minute volume divided by same-minute-of-day 20-day average.
- 15-minute return.
- SPY-only v1 regime logic.
- Z-score ranking.
- Intraday momentum entry filter.

Latest SHA:

- `407e1f1 phase-3: review - preserve approved signal branch`

Checks:

- `uv run --extra test pytest tests/test_driftpilot_signals.py tests/test_driftpilot_foundation.py -q` — PASS, 16 passed
- `uv run --extra test pytest -q` — PASS, 128 passed, 1 warning
- `uvx --with python-dotenv --with pytest mypy src/driftpilot tests/test_driftpilot_signals.py tests/test_driftpilot_foundation.py` — PASS
- `uvx ruff check src/driftpilot/signals tests/test_driftpilot_signals.py` — PASS
- `git diff --check refactor/phase-0-1-foundation...HEAD` — PASS

Review:

- `PHASE_3_REVIEW.md` committed and approved.

### Phase 4: Allocator And Paper Fills

Status: **complete / reviewed**

Branch/worktree:

- `refactor/phase-4-allocator`
- `/Users/karuthsanker/Documents/driftpilot-worktrees/phase-4-allocator`

Completed:

- Async slot allocator with lock.
- Sector cap, duplicate guard, stale candidate guard, and reserve-before-submit behavior.
- Persistent allocator state.
- Persistent candidate queue blocked reasons.
- Paper fill slippage formula and SQLite fill persistence.
- Removed invalid `AVAILABLE` free-slot status.

Latest SHA:

- `2fc861b phase-4: review - approve allocator fixes`

Checks:

- `uv run --extra test pytest tests/test_driftpilot_phase4_execution.py tests/test_driftpilot_foundation.py -q` — PASS, 11 passed
- `uv run --extra test pytest -q` — PASS, 123 passed, 1 warning
- `uvx --with python-dotenv --with pytest mypy src/driftpilot tests/test_driftpilot_phase4_execution.py tests/test_driftpilot_foundation.py` — PASS
- `uvx ruff check src/driftpilot/execution src/driftpilot/storage tests/test_driftpilot_phase4_execution.py` — PASS
- `git diff --check refactor/phase-0-1-foundation...HEAD` — PASS

Review:

- `PHASE_4_REVIEW.md` committed and approved.

## BLOCKED.md

No active `BLOCKED.md` items remain in the Phase 2, Phase 3, or Phase 4 worktrees.

## Deviations

- Reviewer subagents could not complete because the account hit the Codex usage limit. Phase 2 and Phase 4 reviews were completed locally and written to `PHASE_2_REVIEW.md` and `PHASE_4_REVIEW.md`.
- The legacy `src/trading_bot/data` package was restored because existing non-DriftPilot tests depended on it and it had previously been hidden by a broad `.gitignore` `data/` rule. No new DriftPilot code was placed under `src/trading_bot/`.

## Phase 5

Phase 5 was **not started**.
