"""Harness-level constants for the DriftPilot backtest engine.

Per refactor plan v1.1 § 2 Task 1.4. These are referenced by the harness
validator (Phase 2.1) and by the bar-cache loader (Phase 2.3).
"""

from __future__ import annotations


# Maximum trailing-bar history a signal may request.  Sized for Apex Hunter
# (90-min EWMLR window) plus warm-up; raise only after confirming that
# `MAX_HISTORY_MINUTES * universe_size` still fits the harness memory budget.
MAX_HISTORY_MINUTES: int = 180

# Baseline scanner cycle.  Live runtime uses this; backtests step at bar
# granularity so each tick is effectively SCAN_INTERVAL_SECONDS=60.
SCAN_INTERVAL_SECONDS: int = 30

# The set of data dependencies the harness can satisfy.  Signals declare
# their dependencies via `SignalProtocol.data_dependencies()`; the harness
# rejects signals at startup whose declared deps fall outside this set.
AVAILABLE_DATA_DEPENDENCIES: frozenset[str] = frozenset(
    {
        "per_symbol_bars",
        "spy_bars",
        "atr",
        "daily_volume_history",
    }
)


__all__ = [
    "MAX_HISTORY_MINUTES",
    "SCAN_INTERVAL_SECONDS",
    "AVAILABLE_DATA_DEPENDENCIES",
]
