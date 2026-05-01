# Phase 3 Review: Signal Layer

Branch: `refactor/phase-3-signals`
Head: `5be6f1f phase-1: foundation - add order timeouts`
Reviewer mode: custom `reviewer` agent, read-only except this review artifact.

## Verdict

**PASS / approved for Phase 3 completion.**

Phase 3 satisfies the signal-layer acceptance criteria in `REFACTOR_PLAN.md:1072`, and the full pytest suite passes. I found no blocking issues, no unacknowledged deferred Phase 3 work, and no Phase 3 changes under the legacy `src/trading_bot/` path.

## Phase Scope

Compared against `refactor/phase-0-1-foundation..HEAD`, Phase 3 changes are limited to:

- `REFACTOR_PLAN.md`
- `src/driftpilot/signals/__init__.py`
- `src/driftpilot/signals/features.py`
- `src/driftpilot/signals/intraday_momentum.py`
- `src/driftpilot/signals/regime.py`
- `src/driftpilot/signals/scoring.py`
- `tests/test_driftpilot_signals.py`

## Acceptance Criteria

| Criterion | Result | Evidence |
| --- | --- | --- |
| Implement 1-minute bar feature cache | PASS | `BarFeatureCache` stores ordered 1-minute bars and quotes, then computes current features through the shared feature path in `src/driftpilot/signals/features.py:77`. |
| Implement VWAP | PASS | VWAP uses typical price weighted by volume in `src/driftpilot/signals/features.py:102`; the resolved decision is documented in `REFACTOR_PLAN.md:1245`; deterministic coverage is in `tests/test_driftpilot_signals.py:87` and `tests/test_driftpilot_signals.py:110`. |
| Implement RVOL | PASS | RVOL compares the current 1-minute volume with same-minute history across the configured 20-day lookback in `src/driftpilot/signals/features.py:137`; the resolved decision is documented in `REFACTOR_PLAN.md:1243`; coverage is in `tests/test_driftpilot_signals.py:119` and `tests/test_driftpilot_signals.py:133`. |
| Implement 15-minute return | PASS | `return_over_minutes(..., 15)` is implemented in `src/driftpilot/signals/features.py:123` and wired into signal features at `src/driftpilot/signals/features.py:187`; coverage is in `tests/test_driftpilot_signals.py:87`. |
| Implement spread check | PASS | The spread limit is `max(0.02, 0.001 * price)` in `src/driftpilot/signals/features.py:162`; entry rejection is in `src/driftpilot/signals/intraday_momentum.py:61`; coverage is in `tests/test_driftpilot_signals.py:180`. |
| Implement z-score ranking | PASS | Z-scores are recomputed across the current passing pool in `src/driftpilot/signals/scoring.py:21`, then applied by `build_intraday_momentum_queue` in `src/driftpilot/signals/intraday_momentum.py:79`; coverage is in `tests/test_driftpilot_signals.py:162` and `tests/test_driftpilot_signals.py:210`. |
| Implement SPY regime logic; QQQ deferred | PASS | SPY-only regime logic is implemented in `src/driftpilot/signals/regime.py:123`; the Phase 3 plan explicitly defers QQQ at `REFACTOR_PLAN.md:1076` and `REFACTOR_PLAN.md:1244`; coverage is in `tests/test_driftpilot_signals.py:143` and `tests/test_driftpilot_signals.py:153`. |
| Add deterministic synthetic-bar tests | PASS | Synthetic bars and deterministic signal/regime/ranking tests are in `tests/test_driftpilot_signals.py:22`. |

## Anti-Pattern Review

| Anti-pattern / rule | Result | Notes |
| --- | --- | --- |
| Manual confirm as normal path | PASS | Phase 3 signal code adds no operator controls. |
| Supabase as operator source of truth | PASS | No Supabase usage in `src/driftpilot/signals/`. |
| APScheduler as operator runtime | PASS | No scheduler usage in Phase 3 signal code. |
| REST polling inside scan loop | PASS | `scan_intraday_momentum` consumes provided bars and quotes only in `src/driftpilot/signals/intraday_momentum.py:100`. |
| IEX feed for autonomous intraday decisions | PASS | No feed-specific logic in Phase 3 signal code. |
| Mid-price paper fills | N/A | Paper fills are Phase 4. |
| Day-based time stops | N/A | Position exits are Phase 4/6. |
| Independent scanner/entry/exit jobs | PASS | Phase 3 adds pure signal functions, not runtime jobs. |
| Silent exception swallowing | PASS | No `except` blocks exist in Phase 3 signal files. |
| Separate live/backtest signal math | PASS | Public signal package is shared for live scanning and backtest replay in `src/driftpilot/signals/__init__.py:1`; backtest integration lands in a later phase. |
| No legacy `src/trading_bot/` modifications | PASS | The Phase 3 diff against `refactor/phase-0-1-foundation..HEAD` does not modify `src/trading_bot/`. |
| Layering | PASS | Signal modules do not import storage, execution, broker, state machine, or legacy `trading_bot` modules. |

## Checks Run

| Check | Result |
| --- | --- |
| `.venv/bin/pytest tests/test_driftpilot_signals.py` | PASS, 10 passed |
| `.venv/bin/pytest` | PASS, 128 passed, 1 warning |
| `.venv/bin/python -m compileall -q src/driftpilot/signals tests/test_driftpilot_signals.py` | PASS |
| `uvx ruff check src/driftpilot/signals tests/test_driftpilot_signals.py` | PASS |
| `uv run --with mypy --with pytest mypy src/driftpilot/signals tests/test_driftpilot_signals.py` | PASS |
| `uvx ruff check src/driftpilot tests/test_driftpilot_signals.py tests/test_driftpilot_foundation.py` | PASS |
| `uv run --with mypy --with pytest mypy src/driftpilot tests/test_driftpilot_signals.py tests/test_driftpilot_foundation.py` | PASS |
| `git diff --check refactor/phase-0-1-foundation..HEAD` | PASS |

## Issues

No blocking issues found.

## BLOCKED.md

`BLOCKED.md` is absent in this branch.

Acknowledged deferred work:

- None.

Unacknowledged deferred work / bugs:

- None found.
