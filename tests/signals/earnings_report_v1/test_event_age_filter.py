from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus
from driftpilot.signals.earnings_report_v1 import (
    EarningsReportConfig,
    EarningsReportSignal,
)


def _event(symbol: str, ts: datetime) -> CatalystEvent:
    return CatalystEvent(
        symbol=symbol,
        category="earnings",
        subcategory="report",
        pillar="micro",
        ts=ts,
        headline=f"{symbol} reports",
        source="alpaca_news",
        horizon_minutes=60,
        headline_hash=f"h-{symbol}",
    )


@pytest.mark.asyncio
async def test_stale_events_do_not_produce_candidates() -> None:
    bus = CatalystEventBus()
    # require_sentiment=None so this test exercises ONLY the age filter
    # (the production default require_sentiment="positive" is covered by
    # tests/signals/earnings_report_v1/test_sentiment_gate.py).
    cfg = EarningsReportConfig(max_event_age_minutes=60, require_sentiment=None)
    sig = EarningsReportSignal(cfg, bus)
    await sig.subscribe()

    now = datetime(2024, 6, 1, 14, 30, tzinfo=timezone.utc)

    # Stale: 90 min old
    await bus.publish(_event("STALE", now - timedelta(minutes=90)))
    # Fresh: 30 min old
    await bus.publish(_event("FRESH", now - timedelta(minutes=30)))

    candidates = await sig.scan(now=now)
    symbols = [c.symbol for c in candidates]

    assert "FRESH" in symbols
    assert "STALE" not in symbols

    await sig.unsubscribe()
