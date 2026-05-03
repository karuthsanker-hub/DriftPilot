"""Tests for the live Alpaca paper execution services.

Mocks the Alpaca trading client + quote provider so we exercise the
allocator/monitor wiring without touching the network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from driftpilot.broker.alpaca_client import AlpacaBrokerClient, OrderSubmissionResult
from driftpilot.clock import DriftPilotClock
from driftpilot.market_data.alpaca_stream import MarketQuote
from driftpilot.market_data.rest_quotes import AlpacaRestQuoteProvider
from driftpilot.services_live import (
    LiveAlpacaAllocator,
    LiveAlpacaPositionMonitor,
    build_live_components,
)
from driftpilot.settings import DriftPilotSettings
from driftpilot.storage.repositories import DriftPilotRepository


@pytest.fixture
def settings(tmp_path):
    return DriftPilotSettings(
        sqlite_path=str(tmp_path / "ops.db"),
        alpaca_key_id="test_key",
        alpaca_secret_key="test_secret",
    )


@pytest.fixture
def repo(settings):
    return DriftPilotRepository.open(settings.sqlite_path_obj, DriftPilotClock(settings.timezone))


def test_build_live_components_requires_credentials():
    bad_settings = DriftPilotSettings()  # no creds
    with pytest.raises(RuntimeError, match="LIVE mode requires"):
        build_live_components(MagicMock(), bad_settings)


def test_build_live_components_returns_trio(settings, repo):
    broker, allocator, monitor = build_live_components(repo, settings)
    assert isinstance(broker, AlpacaBrokerClient)
    assert isinstance(allocator, LiveAlpacaAllocator)
    assert isinstance(monitor, LiveAlpacaPositionMonitor)
    assert allocator.broker is broker
    assert monitor.broker is broker


def test_quote_provider_caches_within_ttl():
    """Two calls within TTL → only one downstream API hit."""
    fake_client = MagicMock()
    fake_quote = SimpleNamespace(
        timestamp=datetime.now(timezone.utc),
        bid_price=100.0, ask_price=100.05,
        bid_size=10, ask_size=10,
    )
    fake_client.get_stock_latest_quote = MagicMock(return_value={"AAPL": fake_quote})

    qp = AlpacaRestQuoteProvider(api_key="x", api_secret="y", cache_ttl_s=10.0, client=fake_client)
    q1 = qp.latest_quote("AAPL")
    q2 = qp.latest_quote("AAPL")

    assert q1 is not None
    assert q1.bid_price == 100.0
    assert q1.ask_price == 100.05
    assert q1 == q2
    # Only one underlying call (the second was served from cache)
    assert fake_client.get_stock_latest_quote.call_count == 1


def test_quote_provider_returns_none_on_invalid_quote():
    """Zero-priced quote → None (broker treats as quote_unavailable)."""
    fake_client = MagicMock()
    fake_client.get_stock_latest_quote = MagicMock(
        return_value={"BAD": SimpleNamespace(
            timestamp=datetime.now(timezone.utc),
            bid_price=0.0, ask_price=0.0, bid_size=0, ask_size=0,
        )}
    )
    qp = AlpacaRestQuoteProvider(api_key="x", api_secret="y", client=fake_client)
    assert qp.latest_quote("BAD") is None


def test_quote_provider_handles_api_exception_gracefully():
    fake_client = MagicMock()
    fake_client.get_stock_latest_quote = MagicMock(side_effect=RuntimeError("API down"))
    qp = AlpacaRestQuoteProvider(api_key="x", api_secret="y", client=fake_client)
    assert qp.latest_quote("AAPL") is None  # logged, no raise


@pytest.mark.asyncio
async def test_live_allocator_skips_when_broker_rejects(settings, repo):
    """When broker returns submitted=False, allocator does NOT create a position."""
    from driftpilot.execution.slot_allocator import (
        AllocationCandidate, AllocationResult, SlotAllocation,
    )

    fake_broker = MagicMock(spec=AlpacaBrokerClient)
    fake_broker.submit_entry_order = AsyncMock(return_value=OrderSubmissionResult(
        submitted=False, broker_order_id=None, symbol="AAPL", side="buy",
        quantity=1, order_type="none", limit_price=None, reason="quote_unavailable",
    ))

    allocator = LiveAlpacaAllocator(repo, settings, fake_broker)

    # Stub the inner allocator to return one allocation for AAPL
    fake_alloc_result = AllocationResult(
        allocations=(SlotAllocation(
            symbol="AAPL", slot_id=1, slot_value=1000.0, sector="Tech",
            rank=1, score=1.0, reserved_at=datetime.now(timezone.utc),
        ),),
        rejections=(),
    )
    allocator.allocator = MagicMock()
    allocator.allocator.allocate = AsyncMock(return_value=fake_alloc_result)

    repo.slots.upsert(1, status="OPEN", symbol="AAPL", slot_value=1000, updated_at=datetime.now(timezone.utc))
    candidates = [AllocationCandidate(
        symbol="AAPL", score=1.0, sector="Tech",
        latest_bar_at=datetime.now(timezone.utc),
        metadata={"reference_price": 200.0},
    )]
    result = await allocator.allocate(candidates)

    fake_broker.submit_entry_order.assert_awaited_once()
    # No position should have been created since broker rejected
    open_positions = repo.positions.list_open()
    assert len(open_positions) == 0


@pytest.mark.asyncio
async def test_catalyst_scanner_emits_candidates_with_event_chain(settings, repo):
    """CatalystScannerService translates signal Candidates → AllocationCandidates,
    carrying the catalyst event chain into metadata."""
    from driftpilot.catalyst.event import CatalystEvent
    from driftpilot.catalyst.event_bus import CatalystEventBus
    from driftpilot.clock import DriftPilotClock
    from driftpilot.services_live import CatalystScannerService
    from driftpilot.signals.earnings_report_v1 import (
        EarningsReportConfig,
        EarningsReportSignal,
    )

    bus = CatalystEventBus()
    sig = EarningsReportSignal(EarningsReportConfig(require_sentiment="positive"), bus)
    await sig.subscribe()

    fake_qp = MagicMock(spec=AlpacaRestQuoteProvider)
    fake_qp.latest_quote = MagicMock(return_value=MarketQuote(
        symbol="AAPL", timestamp=datetime.now(timezone.utc),
        bid_price=199.95, ask_price=200.05,
    ))

    scanner = CatalystScannerService(
        signal=sig,
        quote_provider=fake_qp,
        clock=DriftPilotClock(settings.timezone),
    )

    # Publish a positive earnings event; signal admits it
    ev = CatalystEvent(
        symbol="AAPL", category="earnings", subcategory="report", pillar="micro",
        ts=datetime.now(timezone.utc),
        headline="Apple beats Q1 earnings, raises guidance",
        source="alpaca", horizon_minutes=240, headline_hash="aapl_q1_beat",
        sentiment="positive", priority_modifier=0.15,
    )
    await bus.publish(ev)

    result = await scanner.scan()
    assert len(result.candidates) == 1
    ac = result.candidates[0]
    assert ac.symbol == "AAPL"
    assert ac.metadata["sentiment"] == "positive"
    assert ac.metadata["headline_hash"] == "aapl_q1_beat"
    assert "Apple beats" in ac.metadata["headline"]
    assert ac.metadata["reference_price"] == pytest.approx(200.0, rel=1e-3)


@pytest.mark.asyncio
async def test_catalyst_scanner_skips_when_no_quote(settings, repo):
    """No live quote → skip the candidate (don't pass garbage to broker)."""
    from driftpilot.catalyst.event import CatalystEvent
    from driftpilot.catalyst.event_bus import CatalystEventBus
    from driftpilot.clock import DriftPilotClock
    from driftpilot.services_live import CatalystScannerService
    from driftpilot.signals.earnings_report_v1 import (
        EarningsReportConfig,
        EarningsReportSignal,
    )

    bus = CatalystEventBus()
    sig = EarningsReportSignal(EarningsReportConfig(require_sentiment="positive"), bus)
    await sig.subscribe()

    fake_qp = MagicMock(spec=AlpacaRestQuoteProvider)
    fake_qp.latest_quote = MagicMock(return_value=None)  # no quote

    scanner = CatalystScannerService(
        signal=sig,
        quote_provider=fake_qp,
        clock=DriftPilotClock(settings.timezone),
    )

    await bus.publish(CatalystEvent(
        symbol="ILLIQ", category="earnings", subcategory="report", pillar="micro",
        ts=datetime.now(timezone.utc), headline="x", source="t",
        horizon_minutes=240, headline_hash="z", sentiment="positive",
    ))

    result = await scanner.scan()
    assert result.candidates == []


@pytest.mark.asyncio
async def test_live_allocator_records_catalyst_metadata_on_position(settings, repo):
    """Audit contract: the position record must include catalyst event chain
    metadata so the EOD audit script can reconstruct the trade."""
    from driftpilot.execution.slot_allocator import (
        AllocationCandidate, AllocationResult, SlotAllocation,
    )

    fake_broker = MagicMock(spec=AlpacaBrokerClient)
    fake_broker.submit_entry_order = AsyncMock(return_value=OrderSubmissionResult(
        submitted=True, broker_order_id="ord-789", symbol="AAPL", side="buy",
        quantity=5, order_type="limit", limit_price=200.10, reason="filled",
    ))

    allocator = LiveAlpacaAllocator(repo, settings, fake_broker)
    fake_alloc_result = AllocationResult(
        allocations=(SlotAllocation(
            symbol="AAPL", slot_id=1, slot_value=1000.0, sector="Tech",
            rank=1, score=0.15, reserved_at=datetime.now(timezone.utc),
        ),),
        rejections=(),
    )
    allocator.allocator = MagicMock()
    allocator.allocator.allocate = AsyncMock(return_value=fake_alloc_result)

    repo.slots.upsert(1, status="OPEN", symbol="AAPL", slot_value=1000, updated_at=datetime.now(timezone.utc))
    catalyst_ts = datetime(2026, 5, 4, 13, 35, 0, tzinfo=timezone.utc)
    candidates = [AllocationCandidate(
        symbol="AAPL", score=0.15, sector="Tech",
        latest_bar_at=datetime.now(timezone.utc),
        metadata={
            "reference_price": 200.0,
            "catalyst_event_ts": catalyst_ts,
            "headline": "Apple beats Q1 earnings, raises guidance for FY26",
            "headline_hash": "abc12345def67890",
            "sentiment": "positive",
            "event_age_minutes": 12.5,
        },
    )]
    await allocator.allocate(candidates)

    # The position record must carry the catalyst chain metadata
    open_positions = repo.positions.list_open()
    assert len(open_positions) == 1
    pos = open_positions[0]
    md = pos.metadata or {}
    assert md.get("catalyst_sentiment") == "positive"
    assert md.get("catalyst_headline_hash") == "abc12345def67890"
    assert "Apple beats" in (md.get("catalyst_headline") or "")
    assert md.get("catalyst_event_age_min_at_entry") == 12.5
    assert md.get("broker_order_id") == "ord-789"


@pytest.mark.asyncio
async def test_live_monitor_calls_signal_evaluate_exit(settings, repo, monkeypatch):
    """Monitor pulls quote, calls signal.evaluate_exit; if close=True, submits exit."""
    from dataclasses import dataclass

    @dataclass
    class _FakeDecision:
        close: bool
        reason: str

    fake_broker = MagicMock(spec=AlpacaBrokerClient)
    fake_broker.submit_exit_order = AsyncMock(return_value=OrderSubmissionResult(
        submitted=True, broker_order_id="ord-123", symbol="AAPL", side="sell",
        quantity=5, order_type="limit", limit_price=205.0, reason="exit_submitted",
    ))

    fake_qp = MagicMock(spec=AlpacaRestQuoteProvider)
    fake_qp.latest_quote = MagicMock(return_value=MarketQuote(
        symbol="AAPL", timestamp=datetime.now(timezone.utc),
        bid_price=204.95, ask_price=205.05,
    ))

    monitor = LiveAlpacaPositionMonitor(repo, settings, fake_broker, fake_qp)

    # Stub get_signal to return one whose evaluate_exit closes
    fake_signal = MagicMock()
    fake_signal.evaluate_exit = MagicMock(return_value=_FakeDecision(close=True, reason="profit_take"))
    monkeypatch.setattr("driftpilot.services_live.get_signal", lambda _name=None: fake_signal)

    # Create one open position (slot must exist for FK)
    repo.slots.upsert(1, status="OPEN", symbol="AAPL", slot_value=1000, updated_at=datetime.now(timezone.utc))
    pos = repo.positions.create_open(
        symbol="AAPL", quantity=5, entry_price=200.0,
        target_price=202.0, stop_price=197.0, slot_id=1,
        opened_at=datetime.now(timezone.utc),
        metadata={"reference_price": 200.0, "current_price": 200.0,
                  "entry_ts": datetime.now(timezone.utc).isoformat(), "entry_price": 200.0},
    )

    await monitor.monitor_open_positions()

    fake_qp.latest_quote.assert_called_once_with("AAPL")
    fake_signal.evaluate_exit.assert_called_once()
    fake_broker.submit_exit_order.assert_awaited_once()
    # Position should be closed
    assert len(repo.positions.list_open()) == 0
