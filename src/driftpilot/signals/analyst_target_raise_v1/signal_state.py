"""TypedDict for analyst_target_raise_v1 per-position scratch state.

Stored on `position.metadata`; runtime contract is unchanged
(plain `dict[str, object]`), the TypedDict is type-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict


class AnalystTargetRaiseState(TypedDict, total=False):
    entry_ts: datetime
    event_ts: datetime
    headline_hash: str
    peak_unrealized_pct: float


__all__ = ["AnalystTargetRaiseState"]
