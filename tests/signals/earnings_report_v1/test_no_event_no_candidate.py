from __future__ import annotations

from datetime import datetime, timezone

import pytest

from driftpilot.catalyst.event_bus import CatalystEventBus
from driftpilot.signals.earnings_report_v1 import (
    EarningsReportConfig,
    EarningsReportSignal,
)


@pytest.mark.asyncio
async def test_empty_bus_returns_no_candidates() -> None:
    bus = CatalystEventBus()
    sig = EarningsReportSignal(EarningsReportConfig(), bus)
    await sig.subscribe()

    candidates = await sig.scan(now=datetime(2024, 6, 1, tzinfo=timezone.utc))
    assert candidates == []

    await sig.unsubscribe()
