"""Production sentiment gate tests for earnings_report_v1.

The validated GATED config (Jul-Dec 2024, edge_ratio=1.105) requires
events to be Qwen-tagged sentiment="positive". This is the runtime
mirror of the SQL `--require-sentiment positive` filter used in the
backtest harness.
"""

from __future__ import annotations
import hashlib
from datetime import datetime, timezone

import pytest

from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus
from driftpilot.signals.earnings_report_v1 import (
    EarningsReportConfig,
    EarningsReportSignal,
)


def _make_event(symbol: str, sentiment: str | None) -> CatalystEvent:
    h = hashlib.sha256(f"{symbol}|{sentiment}".encode()).hexdigest()[:16]
    return CatalystEvent(
        symbol=symbol,
        category="earnings",
        subcategory="report",
        pillar="micro",
        ts=datetime.now(timezone.utc),
        headline=f"{symbol} beats earnings expectations",
        source="test",
        horizon_minutes=60,
        headline_hash=h,
        sentiment=sentiment,
    )


@pytest.mark.asyncio
async def test_sentiment_gate_admits_only_matching_sentiment() -> None:
    bus = CatalystEventBus()
    sig = EarningsReportSignal(
        EarningsReportConfig(require_sentiment="positive"), bus
    )
    await sig.subscribe()
    try:
        await bus.publish(_make_event("AAPL", "positive"))
        await bus.publish(_make_event("MSFT", "negative"))
        await bus.publish(_make_event("NVDA", "neutral"))

        candidates = await sig.scan()
        symbols = {c.symbol for c in candidates}
        assert symbols == {"AAPL"}
    finally:
        await sig.unsubscribe()


@pytest.mark.asyncio
async def test_sentiment_gate_excludes_null_sentiment() -> None:
    """Events with sentiment=None (Qwen offline / not yet enriched) are
    EXCLUDED when require_sentiment is set. Qwen is the gate."""
    bus = CatalystEventBus()
    sig = EarningsReportSignal(
        EarningsReportConfig(require_sentiment="positive"), bus
    )
    await sig.subscribe()
    try:
        await bus.publish(_make_event("AAPL", None))
        candidates = await sig.scan()
        assert candidates == []
    finally:
        await sig.unsubscribe()


@pytest.mark.asyncio
async def test_disabling_gate_admits_all() -> None:
    """require_sentiment=None reverts to spike behavior — admit all events."""
    bus = CatalystEventBus()
    sig = EarningsReportSignal(
        EarningsReportConfig(require_sentiment=None), bus
    )
    await sig.subscribe()
    try:
        await bus.publish(_make_event("AAPL", "positive"))
        await bus.publish(_make_event("MSFT", "negative"))
        await bus.publish(_make_event("NVDA", None))
        candidates = await sig.scan()
        assert {c.symbol for c in candidates} == {"AAPL", "MSFT", "NVDA"}
    finally:
        await sig.unsubscribe()


@pytest.mark.asyncio
async def test_default_config_gates_to_positive() -> None:
    """The validated GATED config requires positive sentiment by default.
    This test guards against regression to the un-gated v3.0 default."""
    cfg = EarningsReportConfig()
    assert cfg.require_sentiment == "positive"
