from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus
from driftpilot.signals.base import Candidate, ExitDecision
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
        headline=f"{symbol} reports earnings",
        source="alpaca_news",
        horizon_minutes=60,
        headline_hash=f"hash-{symbol}",
    )


@dataclass
class _Position:
    metadata: dict[str, Any]
    current_price: float


@pytest.mark.asyncio
async def test_signal_protocol_compliance() -> None:
    bus = CatalystEventBus()
    # Protocol compliance test exercises the API surface, not the sentiment
    # gate. require_sentiment=None matches the spike's behavior.
    cfg = EarningsReportConfig(require_sentiment=None)
    sig = EarningsReportSignal(cfg, bus)
    await sig.subscribe()

    assert sig.name == "earnings_report_v1"
    assert sig.version == "1.0.0"

    now = datetime(2024, 6, 1, 14, 30, tzinfo=timezone.utc)
    await bus.publish(_event("AAPL", now - timedelta(minutes=5)))

    candidates = await sig.scan(now=now)
    assert isinstance(candidates, list)
    assert len(candidates) == 1
    assert isinstance(candidates[0], Candidate)
    assert candidates[0].symbol == "AAPL"
    assert candidates[0].allowed is True

    pos = _Position(
        metadata={"entry_ts": now, "entry_price": 100.0},
        current_price=100.5,
    )
    decision = sig.evaluate_exit(pos, now + timedelta(minutes=10))
    assert decision is None or isinstance(decision, ExitDecision)

    await sig.unsubscribe()
