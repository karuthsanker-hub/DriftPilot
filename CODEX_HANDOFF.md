# Codex Handoff — DriftPilot Project State

**Date:** 2026-05-14
**Branch:** `main` at latest (all pushed to `origin/main`)
**Status:** All 10 Codex tasks complete. System operational.

## Current Snapshot

- **Working tree:** CLEAN — all task work committed and pushed.
- **All 10 Codex tasks:** DONE (see `CODEX_TASKS.md` for summary).
- **Architecture doc:** `docs/LLD.md` — full system design with diagrams.
- **Agent rules:** `AGENTS.md` — dashboard exception added for `src/trading_bot/dashboard/`.
- **Brain server:** Running on DGX Spark :8100 with pgvector backend.
- **PostgreSQL:** Running on DGX Spark :5432, auto-starts on boot.

## What's Running

| Component | Location | Port | Status |
|-----------|----------|------|--------|
| Operator | MacBook (launchd 9:25 AM) | — | Auto-start Mon-Fri |
| Dashboard | MacBook (launchd 9:25 AM) | 8501 | Auto-start Mon-Fri |
| Brain Server | DGX Spark | 8100 | pgvector backend |
| Qwen vLLM | DGX Spark | 8000 | Qwen3-8B |
| PostgreSQL | DGX Spark | 5432 | Auto-start on boot |

## Key Files

| File | Purpose |
|------|---------|
| `docs/LLD.md` | Full architecture, data flow, component details |
| `CODEX_TASKS.md` | Task completion summary |
| `AGENTS.md` | Hard rules for all agents |
| `.codex/instructions.md` | Codex resume protocol |
| `REFACTOR_PLAN.md` | Original authoritative spec |

## Next Steps (if any)

- Monitor paper trading performance over next weeks
- Tune dynamic band parameters based on trade outcomes
- Brain skills will accumulate — review after 10+ trading days
- Consider migrating dashboard to `src/driftpilot/dashboard/` long-term
