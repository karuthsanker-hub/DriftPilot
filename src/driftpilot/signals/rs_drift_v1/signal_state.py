"""RS-Drift v1 - TypedDict for signal_state keys.

The keys this signal stores in `position.metadata` per refactor plan
v1.1 section 3.1 (the opaque signal_state contract). Declared as
TypedDict so mypy catches typos at type-check time. Runtime contract
unchanged - the underlying `dict[str, object]` is the same.
"""
from __future__ import annotations

from typing import TypedDict


class RsDriftState(TypedDict, total=False):
    break_even_triggered: bool
    effective_stop_pct: float
    peak_unrealized_pct: float
    spy_heat_triggered_during_position: bool
    atr_at_entry: float


__all__ = ["RsDriftState"]
