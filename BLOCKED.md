# Blocked Questions

## 2026-05-14: CODEX_TASKS Tasks 5 and 7 target legacy dashboard path

`AGENTS.md` says: "Do not modify code under `src/trading_bot/` (legacy path). New code lives entirely under `src/driftpilot/`."

`CODEX_TASKS.md` Task 5 requires editing `src/trading_bot/dashboard/app.py` and adding `src/trading_bot/dashboard/templates/brain.html`.

`CODEX_TASKS.md` Task 7 requires editing `src/trading_bot/dashboard/app.py` and `src/trading_bot/dashboard/templates/pipeline.html`.

Question: Should these dashboard tasks be re-scoped to a `src/driftpilot/dashboard/` implementation, or is the `src/trading_bot/dashboard/` hard rule intentionally waived for dashboard-only work?
