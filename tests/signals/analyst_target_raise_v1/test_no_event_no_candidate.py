"""With no events on the bus, scan() returns []."""

from __future__ import annotations

from driftpilot.catalyst.event_bus import CatalystEventBus
from driftpilot.signals.analyst_target_raise_v1 import (
    AnalystTargetRaiseConfig,
    AnalystTargetRaiseV1Signal,
)


def test_no_events_no_candidates() -> None:
    bus = CatalystEventBus()
    sig = AnalystTargetRaiseV1Signal(AnalystTargetRaiseConfig(), bus)
    assert sig.scan() == []
