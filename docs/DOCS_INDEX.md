# Docs Index — Status of Every Markdown File

**Last audit:** 2026-05-02. Status legend:

- ✅ **CURRENT** — accurate as of this audit, safe to follow.
- 🟡 **PARTIAL** — accurate in spirit but missing recent additions; safe to read with caveats.
- 📜 **HISTORICAL** — point-in-time artifact (review snapshot, phase-N completion log). Keep as record but don't follow as instruction.
- ❌ **STALE** — predates the current architecture; don't act on its instructions.

## Top-level project docs

| File | Status | Scope | Notes |
|---|---|---|---|
| [README.md](../README.md) | 🟡 PARTIAL | User-facing project intro, quickstart, dashboard, backtest, install | Mentions only `intraday_momentum_v1`. Now 5 signals are registered. Quickstart commands still correct. |
| [REFACTOR_PLAN.md](../REFACTOR_PLAN.md) | ✅ CURRENT | Authoritative refactor spec (1500+ lines), all phases 0–16 | Phase 9 prerequisite update notes that 12-month backtest verdict was FAIL; paper trading still allowed. The plan IS the contract. |
| [AGENTS.md](../AGENTS.md) | ✅ CURRENT | Hard rules for any code-generating agent | All 9 rules still in force. |
| [MIGRATION.md](../MIGRATION.md) | ✅ CURRENT | Legacy `src/trading_bot/` → DriftPilot transition notes; Phase 12 Databento details | Includes the cache layout, dataset = `EQUS.MINI`, schema = `ohlcv-1m`. |

## docs/

| File | Status | Scope | Notes |
|---|---|---|---|
| [docs/PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) | ✅ CURRENT | One-page orientation with mermaid diagrams (component map, state machine, signal registry, backtest pipeline, ER schema, deploy topology) | **Read this first.** |
| [docs/ARCHITECTURE.md](ARCHITECTURE.md) | ✅ CURRENT | Runtime flow detail, state ownership, signal boundary, persistence, live gate, dashboard contract | Mermaid runtime flow diagram included. Slightly older than PROJECT_OVERVIEW (signal boundary now also covers `evaluate_exit`). |
| [docs/OPERATIONS.md](OPERATIONS.md) | ✅ CURRENT | Practical runbook: start services, expected dashboard states, paper reset, troubleshooting | All commands still work. |
| [docs/RESEARCH_PATTERNS.md](RESEARCH_PATTERNS.md) | ✅ CURRENT | Analytical patterns for testing exit-tweak / stop-tweak / regime / cross-signal counterfactuals against existing reports without re-running. Includes a worked example from RS-Drift's FAIL run. | Read before queueing a re-run. |
| [docs/REFACTOR_PLAN_V2_LIVE_OPERATOR.md](REFACTOR_PLAN_V2_LIVE_OPERATOR.md) | ✅ CURRENT | The v2 plan adding live trade tape, emergency stop, regime detection, signal router with three modes. 7 phases, multi-agent orchestration model. |
| [docs/REFACTOR_PLAN_V3_CATALYST_LAYER.md](REFACTOR_PLAN_V3_CATALYST_LAYER.md) | 🟢 ACTIONABLE | Catalyst-driven selection layer plan. Four spikes ran: v1 (within-minute, DEAD — methodology artifact), v2 (daily granularity, 1.085× MARGINAL), v3 (categorized but daily windows still wrong), **v4 horizon-aware (FIRST actionable finding: `analyst/target_raise` 1.37× at 60m, fades by 240m; product_launch confirmed anti-signal at all horizons; filing/8a 1.10× at 2day with N=61)**. The user's twin pushbacks (categorize by news type, test multiple horizons) jointly cracked the analysis. Recommendation: re-run on full 2024 + mid-cap universe to bring N≥20 per cell; if validated, ship v3 with horizon-aware event model. |
| [reports/STATUS.md](../reports/STATUS.md) | ✅ CURRENT | Live aggregator showing all 4 backtests' status + per-signal cards + how-to-read-a-report. The single page to land on when checking 'are the backtests done?' |
| [docs/DOCS_INDEX.md](DOCS_INDEX.md) | ✅ CURRENT | This file. |

## Phase review artifacts (archive)

| File | Status | Scope |
|---|---|---|
| [PHASE_2_REVIEW.md](../PHASE_2_REVIEW.md) | 📜 HISTORICAL | Phase 2 (Broker) review snapshot, dated 2026-05-01. Verdict: APPROVED. |
| [PHASE_3_REVIEW.md](../PHASE_3_REVIEW.md) | 📜 HISTORICAL | Phase 3 (Signals) review snapshot. Verdict: PASS. |
| [PHASE_4_REVIEW.md](../PHASE_4_REVIEW.md) | 📜 HISTORICAL | Phase 4 (Allocator + Paper Fills) review snapshot. Verdict: APPROVED. |
| [OVERNIGHT_SUMMARY.md](../OVERNIGHT_SUMMARY.md) | 📜 HISTORICAL | Status of phases 0-4 captured during a parallel-agent overnight run. Phases since then have been executed and merged. |

## Pre-DriftPilot refactor docs

| File | Status | Scope |
|---|---|---|
| [plan.md](../plan.md) | ❌ STALE | "Trading Bot v4 Implementation Plan" — predates the DriftPilot refactor. References Supabase as primary DB (now SQLite), 6-position cap, scheduler-job execution model. Kept for archaeology. |
| [missing_items_plan.md](../missing_items_plan.md) | 📜 HISTORICAL | Pre-refactor punch list, all items marked "Done" against the legacy `src/trading_bot/` workflow. Kept for archaeology; the v1 it describes is the legacy path. |

## Operations / scripts

| File | Status | Scope |
|---|---|---|
| [scripts/README.md](../scripts/README.md) | ✅ CURRENT | Catalogue of `pull_databento_2024.sh`, `migrate_to_dgx.sh`, `deploy_to_dgx.sh`. Includes typical workflow + DGX SSH key advice. |

## Per-signal docs (`src/driftpilot/signals/<name>/`)

Each new signal package carries a `README.md` (thesis, parameters, hypothesis, verdict log, lessons) and a `KNOWN_RISKS.md` (documented validation concerns from the locked spec).

| Signal | README | KNOWN_RISKS | Status |
|---|---|---|---|
| `stationary_ghost_v1` | [README.md](../src/driftpilot/signals/stationary_ghost_v1/README.md) | [KNOWN_RISKS.md](../src/driftpilot/signals/stationary_ghost_v1/KNOWN_RISKS.md) | ✅ CURRENT — verdict log placeholder pending backtest |
| `whale_tail_v1` | [README.md](../src/driftpilot/signals/whale_tail_v1/README.md) | [KNOWN_RISKS.md](../src/driftpilot/signals/whale_tail_v1/KNOWN_RISKS.md) | ✅ CURRENT — verdict log placeholder pending backtest |
| `rs_drift_v1` | [README.md](../src/driftpilot/signals/rs_drift_v1/README.md) | [KNOWN_RISKS.md](../src/driftpilot/signals/rs_drift_v1/KNOWN_RISKS.md) | ✅ CURRENT — verdict log placeholder pending backtest |
| `apex_hunter_v2_2` | [README.md](../src/driftpilot/signals/apex_hunter_v2/README.md) | [KNOWN_RISKS.md](../src/driftpilot/signals/apex_hunter_v2/KNOWN_RISKS.md) | ✅ CURRENT — verdict log placeholder pending backtest |
| `intraday_momentum_v1` | (no per-signal README) | — | The reference signal predates the per-signal-doc convention. Verdict: **FAIL** captured in [REFACTOR_PLAN.md § Phase 12](../REFACTOR_PLAN.md). |

## Outstanding writing tasks

Tracked here so they don't slip:

1. **README.md** root: add a section listing all 5 registered signals + brief one-liners, replacing the "Current signal: intraday_momentum_v1" line.
2. **Per-signal verdict logs**: each signal's `README.md` has a "Verdict log" placeholder. After the four 2024 backtests land on DGX, populate with `verdict / edge_ratio / win_rate / commit SHA / report path`.
3. **`reports/COMPARISON.md`**: not yet created. Will be written after all four backtests complete (Step 4 of `claude_code_handoff.md`).
4. **`intraday_momentum_v1/README.md` + `KNOWN_RISKS.md`**: backfill so all 5 signals share the same doc convention.
5. **Phase 9-12 closure note**: when Phase 12 backtests complete on DGX, append a closure section to `REFACTOR_PLAN.md` § Implementation Phases or write `PHASE_12_RESULTS.md`.

## How to keep this index honest

When you add or significantly change a `.md` file:

1. Edit the relevant row in this file.
2. If it's a new doc, add a row.
3. If it goes stale, downgrade its status badge with a one-line reason.

Audit cadence: every time the integration branch hits a phase boundary (currently every 1-2 weeks).
