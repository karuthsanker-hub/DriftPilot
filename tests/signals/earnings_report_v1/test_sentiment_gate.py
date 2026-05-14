"""Production sentiment gate tests for earnings_report_v1.

The validated GATED config (Jul-Dec 2024, edge_ratio=1.105) requires
events to be Qwen-tagged sentiment="positive". This is the runtime
mirror of the SQL `--require-sentiment positive` filter used in the
backtest harness.
"""

from __future__ import annotations
import hashlib
import sqlite3
from datetime import datetime, timezone

import pytest

from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus
from driftpilot.signals.earnings_report_v1 import (
    EarningsReportConfig,
    EarningsReportSignal,
)


def _make_event(
    symbol: str,
    sentiment: str | None,
    priority_modifier: float = 0.10,
    headline: str | None = None,
) -> CatalystEvent:
    h = hashlib.sha256(f"{symbol}|{sentiment}".encode()).hexdigest()[:16]
    return CatalystEvent(
        symbol=symbol,
        category="earnings",
        subcategory="report",
        pillar="micro",
        ts=datetime.now(timezone.utc),
        headline=headline or f"{symbol} beats earnings expectations",
        source="test",
        horizon_minutes=60,
        headline_hash=h,
        sentiment=sentiment,
        priority_modifier=priority_modifier,
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
        await bus.publish(_make_event("AAPL", None, priority_modifier=0.15))
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
    assert cfg.require_positive_priority_modifier is True


@pytest.mark.asyncio
async def test_positive_sentiment_requires_positive_priority_modifier() -> None:
    bus = CatalystEventBus()
    sig = EarningsReportSignal(EarningsReportConfig(), bus)
    await sig.subscribe()
    try:
        await bus.publish(_make_event("ZERO", "positive", priority_modifier=0.0))
        await bus.publish(_make_event("NEG", "positive", priority_modifier=-0.05))
        await bus.publish(_make_event("BEAT", "positive", priority_modifier=0.08))

        candidates = await sig.scan()
        assert {c.symbol for c in candidates} == {"BEAT"}
        assert sig.last_skip_counts["non_positive_priority_modifier"] == 2
    finally:
        await sig.unsubscribe()


@pytest.mark.asyncio
async def test_downbeat_headline_vetoes_faulty_positive_sentiment() -> None:
    bus = CatalystEventBus()
    sig = EarningsReportSignal(EarningsReportConfig(), bus)
    await sig.subscribe()
    try:
        await bus.publish(
            _make_event(
                "REZI",
                "positive",
                priority_modifier=0.20,
                headline="QuickLogic Posts Downbeat Q1 Results...",
            )
        )

        candidates = await sig.scan()
        assert candidates == []
        assert sig.last_skip_counts == {"negative_headline_veto": 1}
    finally:
        await sig.unsubscribe()


@pytest.mark.parametrize(
    "headline",
    [
        "Acme misses Q1 earnings estimates",
        "Acme lowers full-year outlook after Q1 results",
        "Acme cuts guidance after earnings miss",
        "Acme reports revenue below estimates",
        "Acme sees weak guidance for Q2",
        "Acme posts loss in first quarter",
        "Acme widens loss despite higher sales",
    ],
)
@pytest.mark.asyncio
async def test_negative_earnings_headline_veto_terms(headline: str) -> None:
    bus = CatalystEventBus()
    sig = EarningsReportSignal(EarningsReportConfig(), bus)
    await sig.subscribe()
    try:
        await bus.publish(
            _make_event("VETO", "positive", priority_modifier=0.20, headline=headline)
        )

        assert await sig.scan() == []
        assert sig.last_skip_counts == {"negative_headline_veto": 1}
    finally:
        await sig.unsubscribe()


@pytest.mark.asyncio
async def test_positive_beat_still_passes() -> None:
    bus = CatalystEventBus()
    sig = EarningsReportSignal(EarningsReportConfig(), bus)
    await sig.subscribe()
    try:
        await bus.publish(
            _make_event(
                "GOOD",
                "positive",
                priority_modifier=0.12,
                headline="GoodCo beats Q1 estimates and raises guidance",
            )
        )

        candidates = await sig.scan()
        assert [c.symbol for c in candidates] == ["GOOD"]
    finally:
        await sig.unsubscribe()


@pytest.mark.asyncio
async def test_min_confidence_blocks_low_confidence_when_available(tmp_path) -> None:
    db_path = tmp_path / "catalyst.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE catalyst_events (
                symbol TEXT,
                category TEXT,
                subcategory TEXT,
                pillar TEXT,
                event_ts TEXT,
                headline TEXT,
                source TEXT,
                horizon_minutes INTEGER,
                headline_hash TEXT,
                sentiment TEXT,
                priority_modifier REAL,
                confidence REAL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO catalyst_events VALUES (
                'LOWC', 'earnings', 'report', 'micro', ?, 'Low confidence beats',
                'test', 60, 'lowc_hash', 'positive', 0.12, 0.40
            )
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()

    sig = EarningsReportSignal(
        EarningsReportConfig(min_sentiment_confidence=0.80),
        CatalystEventBus(),
    )
    assert sig.bootstrap_from_db(str(db_path)) == 1

    candidates = await sig.scan()
    assert candidates == []
    assert sig.last_skip_counts == {"low_sentiment_confidence": 1}
