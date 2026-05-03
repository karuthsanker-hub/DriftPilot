"""The Ratchet rule: trailing_stop_price ONLY MOVES UP, never relaxes.

Even if the recomputation suggests a lower stop (after a pullback or after
a stage transition where new_stop < old_stop), the metadata-stored stop
must stay at the higher level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from driftpilot.signals.apex_hunter_v2.exits import evaluate_exit
from driftpilot.signals.features import MinuteBar


ET = ZoneInfo("America/New_York")


@dataclass
class _FakePosition:
    symbol: str
    entry_at: datetime
    entry_price: float
    metadata: dict[str, Any] = field(default_factory=dict)


def _bar(et_h: int, et_m: int, close: float) -> MinuteBar:
    ts = datetime(2024, 6, 5, et_h, et_m, tzinfo=ET)
    return MinuteBar(symbol="ABC", timestamp=ts, open=close, high=close, low=close, close=close, volume=1000.0)


def test_pullback_does_not_lower_trailing_stop():
    pos = _FakePosition(
        symbol="ABC",
        entry_at=datetime(2024, 6, 5, 10, 30, tzinfo=ET),
        entry_price=100.0,
        metadata={"atr_at_entry": 1.5},
    )
    # Rally to 102 → trailing = 99 (Stage 1)
    evaluate_exit(pos, _bar(10, 31, 102.0), settings=None)
    high_water_stop = pos.metadata["trailing_stop_price"]
    # Pull back to 100.5 — peak_price stays 102, trailing = max(99, 102-3) = 99
    evaluate_exit(pos, _bar(10, 32, 100.5), settings=None)
    assert pos.metadata["trailing_stop_price"] == pytest.approx(high_water_stop, abs=1e-9)


def test_stage_transition_with_lower_recomputed_stop_does_not_relax():
    """Walk peak to +2.1% (Stage 3, mult 0.5). If we then DROP atr_at_entry
    or simulate ATR shrink, recomputation would give a lower stop. Ratchet
    must hold the higher prior stop.

    We force this by mutating metadata directly to artificially raise
    trailing_stop_price above what current atr_mult * atr_at_entry would
    yield, then call evaluate_exit and verify it doesn't drop.
    """
    pos = _FakePosition(
        symbol="ABC",
        entry_at=datetime(2024, 6, 5, 10, 30, tzinfo=ET),
        entry_price=100.0,
        metadata={"atr_at_entry": 1.5},
    )
    evaluate_exit(pos, _bar(10, 31, 102.5), settings=None)  # +2.5% → Stage 3
    # Force a high water mark beyond what recomputation would produce.
    forced_high = pos.metadata["trailing_stop_price"] + 0.5
    pos.metadata["trailing_stop_price"] = forced_high

    # Bar at 102.4: new_stop = 102.5 - 0.5*1.5 = 101.75. Must NOT lower.
    evaluate_exit(pos, _bar(10, 32, 102.4), settings=None)
    assert pos.metadata["trailing_stop_price"] >= forced_high - 1e-9
