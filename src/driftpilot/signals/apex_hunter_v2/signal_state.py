"""Apex Hunter v2.2 - TypedDict for signal_state keys.

The keys this signal stores in `position.metadata` per refactor plan
v1.1 section 3.1 (the opaque signal_state contract). Declared as
TypedDict so mypy catches typos like `metadata['rachet_stage']` at
type-check time. Runtime contract unchanged - the underlying
`dict[str, object]` is the same.
"""
from __future__ import annotations

from typing import TypedDict


class ApexHunterState(TypedDict, total=False):
    ratchet_stage: int
    current_atr_mult: float
    trailing_stop_price: float
    peak_price: float
    peak_unrealized_pct: float
    atr_at_entry: float


__all__ = ["ApexHunterState"]
