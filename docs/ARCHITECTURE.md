# DriftPilot Architecture

> **See [`docs/LLD.md`](LLD.md) for the comprehensive low-level design
> document with full diagrams, component details, and data flow.**

DriftPilot separates strategy research from execution safety. Signals can
change; the operator, allocator, broker, storage, and dashboard contracts
remain stable.

## Quick Links

- **Full architecture:** [`docs/LLD.md`](LLD.md)
- **Agent rules:** [`AGENTS.md`](../AGENTS.md)
- **Task status:** [`CODEX_TASKS.md`](../CODEX_TASKS.md) — all 10 complete
- **Original spec:** [`REFACTOR_PLAN.md`](../REFACTOR_PLAN.md)
