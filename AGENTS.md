# DriftPilot Agent Instructions

## Project context
This is a continuous autonomous intraday paper-trading operator. The
authoritative spec is `REFACTOR_PLAN.md` in the repo root. Read it before
making any decisions.

## Hard rules for all agents
- Do not modify code under `src/trading_bot/` (legacy path) **except
  `src/trading_bot/dashboard/`** which is the active dashboard and may
  be edited freely. All other new code lives under `src/driftpilot/`.
- Every commit message: `phase-N: <module> - <change>`.
- All datetimes are timezone-aware. Time logic comes from
  `src/driftpilot/clock.py` only.
- No silent exception handlers. Every `except` either re-raises, logs,
  or has a comment explaining why suppression is correct.
- No new dependencies without a one-line justification in `pyproject.toml`.
- All tests must pass before considering a phase complete.
- If you encounter a decision not in REFACTOR_PLAN.md, do not improvise.
  Append the question to `BLOCKED.md` and continue with non-blocked work.

## Code style
- Python 3.11+, type-annotated, ruff + mypy clean.
- Async-first for I/O; sync for pure computation.
- Repository pattern for storage; no SQL strings outside repositories.
