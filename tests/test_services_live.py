"""Tests for the live Alpaca paper execution services.

Mocks the Alpaca trading client + quote provider so we exercise the
allocator/monitor wiring without touching the network.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from driftpilot.broker.alpaca_client import AlpacaBrokerClient, OrderSubmissionResult
from driftpilot.clock import DriftPilotClock, FixedClock
from driftpilot.market_data.alpaca_stream import MarketQuote
from driftpilot.market_data.rest_quotes import AlpacaRestQuoteProvider
from driftpilot.services_live import (
    LiveAlpacaAllocator,
    LiveAlpacaPositionMonitor,
    LiveBrokerReconciler,
    build_live_components,
    compute_dynamic_bands,
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


@pytest.mark.asyncio
async def test_live_broker_reconciler_reports_broker_failure(settings, repo):
    fake_broker = MagicMock(spec=AlpacaBrokerClient)
    fake_broker.get_open_positions = AsyncMock(side_effect=RuntimeError("alpaca down"))
    reconciler = LiveBrokerReconciler(fake_broker, repo, settings)

    result = await reconciler.reconcile_open_positions()

    assert result.ok is False
    assert result.status == "broker_unavailable"
    assert "alpaca down" in str(result.error)


@pytest.mark.asyncio
async def test_live_broker_reconciler_reports_symbols_when_flattening_local_state(settings, repo):
    fake_broker = MagicMock(spec=AlpacaBrokerClient)
    fake_broker.get_open_positions = AsyncMock(return_value=[
        SimpleNamespace(symbol="AAPL", quantity=3, average_entry_price=200.0),
    ])
    reconciler = LiveBrokerReconciler(fake_broker, repo, settings)

    result = await reconciler.reconcile_open_positions()

    assert result.ok is True
    assert result.broker_symbols == ("AAPL",)
    assert result.metadata["broker_position_count"] == 1


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


def test_dynamic_bands_widen_for_high_beta_and_tighten_for_low_beta():
    high_beta = compute_dynamic_bands(
        100,
        100,
        atr_pct=1.0,
        beta=1.7,
        default_target_pct=0.01,
        default_stop_pct=0.01,
    )
    low_beta = compute_dynamic_bands(
        100,
        100,
        atr_pct=1.0,
        beta=0.6,
        default_target_pct=0.01,
        default_stop_pct=0.01,
    )
    missing_beta = compute_dynamic_bands(
        100,
        100,
        atr_pct=1.0,
        beta=None,
        default_target_pct=0.01,
        default_stop_pct=0.01,
    )

    assert high_beta.stop_pct > missing_beta.stop_pct
    assert high_beta.target_pct > missing_beta.target_pct
    assert low_beta.stop_pct < missing_beta.stop_pct
    assert low_beta.target_pct < missing_beta.target_pct
    assert "beta_profile=high_beta" in high_beta.reasoning
    assert "beta_profile=low_beta" in low_beta.reasoning


def test_dynamic_bands_apply_time_of_day_profiles():
    opening = compute_dynamic_bands(
        100,
        100,
        atr_pct=1.0,
        entry_time=datetime(2026, 5, 14, 13, 45, tzinfo=timezone.utc),
    )
    midday = compute_dynamic_bands(
        100,
        100,
        atr_pct=1.0,
        entry_time=datetime(2026, 5, 14, 16, 30, tzinfo=timezone.utc),
    )
    close = compute_dynamic_bands(
        100,
        100,
        atr_pct=1.0,
        entry_time=datetime(2026, 5, 14, 19, 45, tzinfo=timezone.utc),
    )
    regular = compute_dynamic_bands(
        100,
        100,
        atr_pct=1.0,
        entry_time=datetime(2026, 5, 14, 18, 30, tzinfo=timezone.utc),
    )

    assert opening.stop_pct > regular.stop_pct
    assert close.stop_pct > regular.stop_pct
    assert midday.stop_pct < regular.stop_pct
    assert "time_profile=opening_volatility" in opening.reasoning
    assert "time_profile=midday_quiet" in midday.reasoning
    assert "time_profile=closing_volatility" in close.reasoning


def test_dynamic_bands_apply_catalyst_profiles():
    earnings = compute_dynamic_bands(
        100,
        100,
        atr_pct=1.0,
        category="earnings",
        subcategory="report",
    )
    analyst = compute_dynamic_bands(
        100,
        100,
        atr_pct=1.0,
        category="analyst",
        subcategory="target_raise",
    )

    assert earnings.target_pct > analyst.target_pct
    assert earnings.stop_pct > analyst.stop_pct
    assert "catalyst_profile=earnings/report" in earnings.reasoning
    assert "catalyst_profile=analyst/target_raise" in analyst.reasoning


def test_dynamic_bands_apply_drift_after_profile_adjustments():
    no_drift = compute_dynamic_bands(
        100,
        100,
        atr_pct=1.0,
        beta=1.7,
        category="earnings",
        subcategory="report",
    )
    drifted = compute_dynamic_bands(
        101.2,
        100,
        atr_pct=1.0,
        beta=1.7,
        drift_pct=1.2,
        category="earnings",
        subcategory="report",
    )

    assert drifted.target_pct < no_drift.target_pct
    assert "drift_adj" in drifted.reasoning
    assert drifted.target_pct > drifted.stop_pct


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
    await allocator.allocate(candidates)

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
async def test_catalyst_scanner_reuses_repo_price_drift_baseline_after_restart(tmp_path):
    from driftpilot.services_live import CatalystScannerService
    from driftpilot.signals.base import Candidate

    class StaticSignal:
        name = "earnings_report_v1"

        def scan(self, *, now):
            return [
                Candidate(
                    symbol="DRFT",
                    score=1.0,
                    sector="Tech",
                    allowed=True,
                    features={
                        "headline_hash": "drft_beat",
                        "headline": "DriftCo beats estimates",
                        "sentiment": "positive",
                    },
                )
            ]

    db_path = tmp_path / "ops.db"
    clock = DriftPilotClock("America/New_York")
    first_repo = DriftPilotRepository.open(db_path, clock)
    first_qp = MagicMock()
    first_qp.latest_quote = MagicMock(
        return_value=MarketQuote(
            symbol="DRFT",
            timestamp=datetime.now(timezone.utc),
            bid_price=99.95,
            ask_price=100.05,
        )
    )
    first_scanner = CatalystScannerService(
        signal=StaticSignal(),
        quote_provider=first_qp,
        clock=clock,
        repository=first_repo,
    )

    first_result = await first_scanner.scan()
    assert len(first_result.candidates) == 1
    assert first_result.candidates[0].metadata["first_seen_price"] == pytest.approx(
        100.0
    )

    second_repo = DriftPilotRepository.open(db_path, clock)
    second_qp = MagicMock()
    second_qp.latest_quote = MagicMock(
        return_value=MarketQuote(
            symbol="DRFT",
            timestamp=datetime.now(timezone.utc),
            bid_price=103.95,
            ask_price=104.05,
        )
    )
    second_scanner = CatalystScannerService(
        signal=StaticSignal(),
        quote_provider=second_qp,
        clock=clock,
        repository=second_repo,
    )
    second_scanner._max_price_drift_pct = 3.0

    second_result = await second_scanner.scan()

    assert second_result.candidates == []
    baseline = second_repo.price_drift_baselines.get("DRFT", "drft_beat")
    assert baseline is not None
    assert baseline.first_seen_price == pytest.approx(100.0)
    assert baseline.last_seen_price == pytest.approx(104.0)
    assert baseline.drift_pct == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_catalyst_scanner_rejects_high_atr_from_context(settings, repo, tmp_path):
    """High ATR candidates are blocked before allocation, but missing ATR stays non-fatal."""
    from driftpilot.services_live import CatalystScannerService
    from driftpilot.signals.base import Candidate
    from driftpilot.runtime_config import save_runtime_config

    runtime_config_path = tmp_path / "runtime_config.json"
    save_runtime_config(
        {
            "max_entry_atr_pct": 6.0,
            "high_volatility_slot_multiplier": 0.5,
        },
        runtime_config_path,
    )

    class _Signal:
        name = "test_signal"

        async def scan(self, now=None):
            return [
                Candidate(
                    symbol="TALO",
                    score=1.0,
                    sector="Energy",
                    allowed=True,
                    features={
                        "headline": "TALO moves on catalyst",
                        "headline_hash": "talo-high-atr",
                        "context_json": '{"atr_pct": 8.12}',
                    },
                )
            ]

    fake_qp = MagicMock(spec=AlpacaRestQuoteProvider)
    fake_qp.latest_quote = MagicMock(return_value=MarketQuote(
        symbol="TALO", timestamp=datetime.now(timezone.utc),
        bid_price=49.95, ask_price=50.05,
    ))

    scanner = CatalystScannerService(
        signal=_Signal(),
        quote_provider=fake_qp,
        clock=DriftPilotClock(settings.timezone),
        runtime_config_path=str(runtime_config_path),
        repository=repo,
    )

    result = await scanner.scan()

    assert result.candidates == []
    assert repo.candidate_queue.blocked_reason("TALO") == "high_volatility_atr"


@pytest.mark.asyncio
async def test_catalyst_scanner_reads_atr_context_from_signal_db(settings, repo, tmp_path):
    """Production catalyst candidates can be ATR-filtered via catalyst DB context_json."""
    from driftpilot.runtime_config import save_runtime_config
    from driftpilot.services_live import CatalystScannerService
    from driftpilot.signals.base import Candidate

    runtime_config_path = tmp_path / "runtime_config.json"
    save_runtime_config(
        {
            "max_entry_atr_pct": 6.0,
            "high_volatility_slot_multiplier": 0.5,
        },
        runtime_config_path,
    )
    catalyst_db_path = tmp_path / "catalyst.sqlite3"
    conn = sqlite3.connect(catalyst_db_path)
    try:
        conn.execute(
            """
            CREATE TABLE catalyst_events (
                symbol TEXT,
                headline_hash TEXT,
                event_ts TEXT,
                context_json TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO catalyst_events VALUES (?, ?, ?, ?)",
            (
                "TALO",
                "talo-db-context",
                datetime.now(timezone.utc).isoformat(),
                '{"atr_pct": 8.12}',
            ),
        )
        conn.commit()
    finally:
        conn.close()

    class _Signal:
        name = "earnings_report_v1"
        _db_path = str(catalyst_db_path)

        async def scan(self, now=None):
            return [
                Candidate(
                    symbol="TALO",
                    score=1.0,
                    sector="Energy",
                    allowed=True,
                    features={
                        "headline": "TALO moves on catalyst",
                        "headline_hash": "talo-db-context",
                    },
                )
            ]

    fake_qp = MagicMock(spec=AlpacaRestQuoteProvider)
    fake_qp.latest_quote = MagicMock(return_value=MarketQuote(
        symbol="TALO", timestamp=datetime.now(timezone.utc),
        bid_price=49.95, ask_price=50.05,
    ))

    scanner = CatalystScannerService(
        signal=_Signal(),
        quote_provider=fake_qp,
        clock=DriftPilotClock(settings.timezone),
        runtime_config_path=str(runtime_config_path),
        repository=repo,
    )

    result = await scanner.scan()

    assert result.candidates == []
    assert repo.candidate_queue.blocked_reason("TALO") == "high_volatility_atr"


@pytest.mark.asyncio
async def test_catalyst_scanner_carries_beta_from_context(settings, repo):
    """Beta from enrichment context becomes allocator metadata for dynamic bands."""
    from driftpilot.services_live import CatalystScannerService
    from driftpilot.signals.base import Candidate

    class _Signal:
        name = "earnings_report_v1"

        async def scan(self, now=None):
            return [
                Candidate(
                    symbol="AVGO",
                    score=1.0,
                    sector="Technology",
                    allowed=True,
                    features={
                        "headline": "AVGO beats Q1 earnings",
                        "headline_hash": "avgo-beta-context",
                        "context_json": '{"atr_pct": 2.5, "beta": 1.7}',
                    },
                )
            ]

    fake_qp = MagicMock(spec=AlpacaRestQuoteProvider)
    fake_qp.latest_quote = MagicMock(return_value=MarketQuote(
        symbol="AVGO", timestamp=datetime.now(timezone.utc),
        bid_price=184.95, ask_price=185.05,
    ))

    scanner = CatalystScannerService(
        signal=_Signal(),
        quote_provider=fake_qp,
        clock=DriftPilotClock(settings.timezone),
        repository=repo,
    )

    result = await scanner.scan()

    assert len(result.candidates) == 1
    assert result.candidates[0].metadata["atr_pct"] == pytest.approx(2.5)
    assert result.candidates[0].metadata["beta"] == pytest.approx(1.7)


@pytest.mark.asyncio
async def test_analyst_signal_bootstrap_carries_beta_context_to_scanner(settings, repo, tmp_path):
    """Bootstrapped analyst candidates carry beta from catalyst DB context_json."""
    from driftpilot.catalyst.event_bus import CatalystEventBus
    from driftpilot.services_live import CatalystScannerService
    from driftpilot.signals.analyst_target_raise_v1 import (
        AnalystTargetRaiseConfig,
        AnalystTargetRaiseV1Signal,
    )

    catalyst_db_path = tmp_path / "catalyst.sqlite3"
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(catalyst_db_path)
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
                context_json TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO catalyst_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "AVGO",
                "analyst",
                "target_raise",
                "micro",
                now.isoformat(),
                "AVGO price target raised at Example Bank",
                "alpaca",
                60,
                "avgo-analyst-context",
                "positive",
                0.25,
                '{"atr_pct": 2.2, "beta": 1.65}',
            ),
        )
        conn.commit()
    finally:
        conn.close()

    signal = AnalystTargetRaiseV1Signal(
        AnalystTargetRaiseConfig(require_sentiment="positive"),
        CatalystEventBus(),
        clock=lambda: now,
    )
    assert signal.bootstrap_from_db(str(catalyst_db_path), lookback_minutes=120) == 1

    fake_qp = MagicMock(spec=AlpacaRestQuoteProvider)
    fake_qp.latest_quote = MagicMock(return_value=MarketQuote(
        symbol="AVGO", timestamp=now,
        bid_price=184.95, ask_price=185.05,
    ))
    scanner = CatalystScannerService(
        signal=signal,
        quote_provider=fake_qp,
        clock=DriftPilotClock(settings.timezone),
        repository=repo,
    )

    result = await scanner.scan()

    assert len(result.candidates) == 1
    metadata = result.candidates[0].metadata
    assert metadata["atr_pct"] == pytest.approx(2.2)
    assert metadata["beta"] == pytest.approx(1.65)


@pytest.mark.asyncio
async def test_catalyst_scanner_triggers_eod_reflection_once(settings, repo):
    from driftpilot.clock import FixedClock
    from driftpilot.services_live import CatalystScannerService

    after_close = datetime(2026, 5, 14, 20, 5, tzinfo=timezone.utc)
    clock = FixedClock(settings.timezone, after_close)
    scanner = CatalystScannerService(
        signal=MagicMock(),
        quote_provider=MagicMock(),
        clock=clock,
        repository=repo,
    )
    fake_brain = MagicMock()
    fake_brain.reflect = MagicMock(return_value={
        "status": "ok",
        "experiences_analyzed": 1,
        "skills_created": 2,
        "skills_retired": 0,
    })
    scanner._brain_client = fake_brain

    position = repo.positions.create_open(
        symbol="AAPL",
        quantity=5,
        entry_price=200.0,
        target_price=204.0,
        stop_price=198.0,
        opened_at=datetime(2026, 5, 14, 14, 30, tzinfo=timezone.utc),
        metadata={
            "signal_name": "earnings_report_v1",
            "catalyst_headline_hash": "aapl-earnings",
            "catalyst_sentiment": "positive",
        },
    )
    repo.positions.close(
        position.id,
        exit_reason="TARGET",
        realized_pnl=10.0,
        closed_at=after_close,
        metadata={"exit_price": 202.0},
    )

    task = await scanner._trigger_eod_reflection(after_close, reason="test_market_close")
    assert task is not None
    await task
    second = await scanner._trigger_eod_reflection(after_close, reason="test_market_close")

    fake_brain.reflect.assert_called_once_with("2026-05-14")
    assert second is None
    context = scanner._last_eod_reflection_context
    assert context is not None
    assert context["closed_trade_count"] == 1
    assert context["symbols"] == ["AAPL"]
    assert context["closed_trades"][0]["pnl_pct"] == pytest.approx(1.0)
    assert context["closed_trades"][0]["signal_name"] == "earnings_report_v1"


@pytest.mark.asyncio
async def test_catalyst_scanner_eod_reflection_waits_until_market_close(settings, repo):
    from driftpilot.clock import FixedClock
    from driftpilot.services_live import CatalystScannerService

    before_close = datetime(2026, 5, 14, 19, 59, tzinfo=timezone.utc)
    scanner = CatalystScannerService(
        signal=MagicMock(),
        quote_provider=MagicMock(),
        clock=FixedClock(settings.timezone, before_close),
        repository=repo,
    )
    fake_brain = MagicMock()
    scanner._brain_client = fake_brain

    task = await scanner._trigger_eod_reflection(before_close, reason="too_early")

    assert task is None
    fake_brain.reflect.assert_not_called()
    assert scanner._last_eod_reflection_context is None


@pytest.mark.asyncio
async def test_catalyst_scanner_eod_reflection_runs_without_closed_trades(settings, repo):
    from driftpilot.clock import FixedClock
    from driftpilot.services_live import CatalystScannerService

    after_close = datetime(2026, 5, 14, 20, 5, tzinfo=timezone.utc)
    scanner = CatalystScannerService(
        signal=MagicMock(),
        quote_provider=MagicMock(),
        clock=FixedClock(settings.timezone, after_close),
        repository=repo,
    )
    fake_brain = MagicMock()
    fake_brain.reflect = MagicMock(return_value={
        "status": "ok",
        "experiences_analyzed": 0,
        "skills_created": 0,
        "skills_retired": 0,
    })
    scanner._brain_client = fake_brain

    task = await scanner._trigger_eod_reflection(after_close, reason="no_trades")

    assert task is not None
    await task
    fake_brain.reflect.assert_called_once_with("2026-05-14")
    assert scanner._last_eod_reflection_context is not None
    assert scanner._last_eod_reflection_context["closed_trade_count"] == 0


@pytest.mark.asyncio
async def test_catalyst_scanner_eod_reflection_failure_is_nonfatal(settings, repo, caplog):
    from driftpilot.clock import FixedClock
    from driftpilot.services_live import CatalystScannerService

    after_close = datetime(2026, 5, 14, 20, 10, tzinfo=timezone.utc)
    scanner = CatalystScannerService(
        signal=MagicMock(),
        quote_provider=MagicMock(),
        clock=FixedClock(settings.timezone, after_close),
        repository=repo,
    )
    fake_brain = MagicMock()
    fake_brain.reflect = MagicMock(side_effect=RuntimeError("brain offline"))
    scanner._brain_client = fake_brain

    position = repo.positions.create_open(
        symbol="MSFT",
        quantity=2,
        entry_price=300.0,
        target_price=306.0,
        stop_price=297.0,
        opened_at=datetime(2026, 5, 14, 15, 0, tzinfo=timezone.utc),
    )
    repo.positions.close(
        position.id,
        exit_reason="STOP",
        realized_pnl=-6.0,
        closed_at=after_close,
    )

    with caplog.at_level("WARNING"):
        task = await scanner._trigger_eod_reflection(after_close, reason="test_failure")
        assert task is not None
        await task

    fake_brain.reflect.assert_called_once_with("2026-05-14")
    assert "eod reflection failed for 2026-05-14" in caplog.text


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
        metadata={
            "protective_stop_order_id": "stop-789",
            "protective_stop_price": 198.10,
            "protective_stop_pct": settings.stop_pct,
        },
    ))

    allocator = LiveAlpacaAllocator(repo, settings, fake_broker)
    reserved_at = datetime(2026, 5, 4, 13, 45, tzinfo=timezone.utc)
    fake_alloc_result = AllocationResult(
        allocations=(SlotAllocation(
            symbol="AAPL", slot_id=1, slot_value=1000.0, sector="Tech",
            rank=1, score=0.15, reserved_at=reserved_at,
        ),),
        rejections=(),
    )
    allocator.allocator = MagicMock()
    allocator.allocator.allocate = AsyncMock(return_value=fake_alloc_result)

    repo.slots.upsert(1, status="OPEN", symbol="AAPL", slot_value=1000, updated_at=datetime.now(timezone.utc))
    catalyst_ts = datetime(2026, 5, 4, 13, 35, 0, tzinfo=timezone.utc)
    candidates = [AllocationCandidate(
        symbol="AAPL", score=0.15, sector="Tech",
        latest_bar_at=reserved_at,
        metadata={
            "reference_price": 200.0,
            "catalyst_event_ts": catalyst_ts,
            "headline": "Apple beats Q1 earnings, raises guidance for FY26",
            "headline_hash": "abc12345def67890",
            "sentiment": "positive",
            "event_age_minutes": 12.5,
            "atr_pct": 2.5,
            "beta": 1.7,
            "category": "earnings",
            "subcategory": "report",
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
    assert md.get("protective_stop_order_id") == "stop-789"
    assert md.get("protective_stop_price") == 198.10
    assert md.get("band_beta") == pytest.approx(1.7)
    assert md.get("band_beta_profile") == "high_beta"
    assert md.get("band_catalyst_profile") == "earnings/report"
    assert md.get("band_time_profile") == "open"


@pytest.mark.asyncio
async def test_live_allocator_ignores_invalid_beta_metadata(settings, repo):
    from driftpilot.execution.slot_allocator import (
        AllocationCandidate, AllocationResult, SlotAllocation,
    )

    fake_broker = MagicMock(spec=AlpacaBrokerClient)
    fake_broker.submit_entry_order = AsyncMock(return_value=OrderSubmissionResult(
        submitted=True, broker_order_id="ord-invalid-beta", symbol="BETA", side="buy",
        quantity=5, order_type="limit", limit_price=100.0, reason="filled",
    ))

    allocator = LiveAlpacaAllocator(repo, settings, fake_broker)
    reserved_at = datetime(2026, 5, 14, 18, 30, tzinfo=timezone.utc)
    fake_alloc_result = AllocationResult(
        allocations=(SlotAllocation(
            symbol="BETA", slot_id=1, slot_value=1000.0, sector="Tech",
            rank=1, score=1.0, reserved_at=reserved_at,
        ),),
        rejections=(),
    )
    allocator.allocator = MagicMock()
    allocator.allocator.allocate = AsyncMock(return_value=fake_alloc_result)

    repo.slots.upsert(1, status="OPEN", symbol="BETA", slot_value=1000, updated_at=datetime.now(timezone.utc))
    candidates = [AllocationCandidate(
        symbol="BETA", score=1.0, sector="Tech",
        latest_bar_at=reserved_at,
        metadata={"reference_price": 100.0, "atr_pct": 1.2, "beta": "not-a-number"},
    )]

    await allocator.allocate(candidates)

    pos = repo.positions.list_open()[0]
    assert pos.metadata["band_beta"] is None
    assert pos.metadata["band_beta_profile"] == "unknown"


@pytest.mark.asyncio
async def test_live_allocator_applies_slot_value_multiplier(settings, repo):
    """Allocator uses candidate slot_value_multiplier for quantity and position metadata."""
    from driftpilot.execution.slot_allocator import (
        AllocationCandidate, AllocationResult, SlotAllocation,
    )

    fake_broker = MagicMock(spec=AlpacaBrokerClient)
    fake_broker.submit_entry_order = AsyncMock(return_value=OrderSubmissionResult(
        submitted=True, broker_order_id="ord-half", symbol="JXN", side="buy",
        quantity=2, order_type="limit", limit_price=200.0, reason="filled",
    ))

    allocator = LiveAlpacaAllocator(repo, settings, fake_broker)
    reserved_at = datetime(2026, 5, 14, 18, 30, tzinfo=timezone.utc)
    fake_alloc_result = AllocationResult(
        allocations=(SlotAllocation(
            symbol="JXN", slot_id=1, slot_value=1000.0, sector="Financials",
            rank=1, score=1.0, reserved_at=reserved_at,
        ),),
        rejections=(),
    )
    allocator.allocator = MagicMock()
    allocator.allocator.allocate = AsyncMock(return_value=fake_alloc_result)

    repo.slots.upsert(1, status="OPEN", symbol="JXN", slot_value=1000, updated_at=datetime.now(timezone.utc))
    candidates = [AllocationCandidate(
        symbol="JXN", score=1.0, sector="Financials",
        latest_bar_at=reserved_at,
        metadata={"reference_price": 200.0, "slot_value_multiplier": 0.5},
    )]

    await allocator.allocate(candidates)

    fake_broker.submit_entry_order.assert_awaited_once_with(
        symbol="JXN",
        quantity=2,
        slot_id=1,
        protective_stop_pct=0.009,  # ATR-based: DEFAULT_ATR_PCT(0.012) * ATR_STOP_SCALE(0.75)
    )
    pos = repo.positions.list_open()[0]
    assert pos.quantity == 2
    assert pos.metadata["slot_value_multiplier"] == 0.5
    assert pos.metadata["effective_slot_value"] == 500.0


@pytest.mark.asyncio
async def test_live_monitor_calls_signal_evaluate_exit(settings, repo, monkeypatch):
    """Monitor pulls quote, calls signal.evaluate_exit; if close=True, submits exit."""
    from dataclasses import dataclass

    @dataclass
    class _FakeDecision:
        close: bool
        reason: str

    fake_broker = MagicMock(spec=AlpacaBrokerClient)
    call_order: list[str] = []

    async def _submit_exit_order(**_kwargs):
        call_order.append("submit_exit")
        return OrderSubmissionResult(
            submitted=True, broker_order_id="ord-123", symbol="AAPL", side="sell",
            quantity=5, order_type="limit", limit_price=205.0, reason="exit_submitted",
        )

    async def _cancel_order(_order_id):
        call_order.append("cancel_stop")

    fake_broker.submit_exit_order = AsyncMock(side_effect=_submit_exit_order)
    fake_broker.cancel_order = AsyncMock(side_effect=_cancel_order)
    fake_broker.get_fill_price = AsyncMock(return_value=205.0)
    fake_broker.get_open_positions = AsyncMock(return_value=[])

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
    repo.positions.create_open(
        symbol="AAPL", quantity=5, entry_price=200.0,
        target_price=202.0, stop_price=197.0, slot_id=1,
        opened_at=datetime.now(timezone.utc),
        metadata={"reference_price": 200.0, "current_price": 200.0,
                  "entry_ts": datetime.now(timezone.utc).isoformat(), "entry_price": 200.0,
                  "protective_stop_order_id": "stop-123"},
    )

    await monitor.monitor_open_positions()

    fake_qp.latest_quote.assert_called_once_with("AAPL")
    fake_signal.evaluate_exit.assert_called_once()
    fake_broker.submit_exit_order.assert_awaited_once()
    fake_broker.cancel_order.assert_awaited_once_with("stop-123")
    assert call_order == ["cancel_stop", "submit_exit"]
    # Position should be closed
    assert len(repo.positions.list_open()) == 0


@pytest.mark.asyncio
async def test_live_monitor_reconciles_filled_protective_stop(settings, repo):
    fake_broker = MagicMock(spec=AlpacaBrokerClient)
    fake_broker.get_open_positions = AsyncMock(return_value=[])
    fake_broker.get_fill_price = AsyncMock(return_value=198.0)

    fake_qp = MagicMock(spec=AlpacaRestQuoteProvider)
    fake_qp.latest_quote = MagicMock(return_value=MarketQuote(
        symbol="AAPL", timestamp=datetime.now(timezone.utc),
        bid_price=197.95, ask_price=198.05,
    ))

    monitor = LiveAlpacaPositionMonitor(repo, settings, fake_broker, fake_qp)
    repo.slots.upsert(
        1,
        status="OPEN",
        symbol="AAPL",
        slot_value=1000,
        updated_at=datetime.now(timezone.utc),
    )
    position = repo.positions.create_open(
        symbol="AAPL",
        quantity=5,
        entry_price=200.0,
        target_price=202.0,
        stop_price=198.0,
        slot_id=1,
        opened_at=datetime.now(timezone.utc),
        metadata={
            "protective_stop_order_id": "stop-123",
            "protective_stop_price": 198.0,
        },
    )

    changes = await monitor._reconcile_alpaca_to_local()

    assert changes == 1
    closed = repo.positions.get(position.id)
    assert closed is not None
    assert closed.status == "closed"
    assert closed.exit_reason == "broker_protective_stop_filled"
    assert closed.realized_pnl == pytest.approx(-10.0)
    slot = repo.slots.get(1)
    assert slot is not None
    assert slot.status == "EMPTY"


@pytest.mark.asyncio
async def test_live_monitor_eod_dilution_tightens_stop_metadata(settings, repo):
    fake_broker = MagicMock(spec=AlpacaBrokerClient)
    fake_broker.get_open_positions = AsyncMock(return_value=[])

    fake_qp = MagicMock(spec=AlpacaRestQuoteProvider)
    fake_qp.latest_quote = MagicMock(side_effect=lambda symbol: MarketQuote(
        symbol=symbol,
        timestamp=datetime(2026, 5, 12, 19, 20, tzinfo=timezone.utc),
        bid_price={"AAA": 102.0, "BBB": 101.0, "CCC": 99.0}[symbol],
        ask_price={"AAA": 102.1, "BBB": 101.1, "CCC": 99.1}[symbol],
    ))

    clock = FixedClock(settings.timezone, datetime(2026, 5, 12, 19, 20, tzinfo=timezone.utc))
    monitor = LiveAlpacaPositionMonitor(repo, settings, fake_broker, fake_qp, clock=clock)
    for slot_id, symbol in [(1, "AAA"), (2, "BBB"), (3, "CCC")]:
        repo.slots.upsert(
            slot_id,
            status="OPEN",
            symbol=symbol,
            slot_value=1000,
            updated_at=datetime(2026, 5, 12, 19, 20, tzinfo=timezone.utc),
        )
        repo.positions.create_open(
            symbol=symbol,
            quantity=5,
            entry_price=100.0,
            target_price=102.0,
            stop_price=98.0,
            slot_id=slot_id,
            opened_at=datetime(2026, 5, 12, 18, 30, tzinfo=timezone.utc),
            metadata={"sector": "Tech"},
        )

    result = await monitor.apply_eod_dilution()

    assert result.metadata["active"] is True
    positions = {position.symbol: position for position in repo.positions.list_open()}
    assert positions["AAA"].stop_price == 102.0
    assert positions["BBB"].stop_price == 98.6
    assert positions["CCC"].metadata["eod_dilution_active"] is True
    fake_broker.submit_exit_order.assert_not_called()


@pytest.mark.asyncio
async def test_live_monitor_eod_sector_dilution_exits_least_profitable(settings, repo):
    fake_broker = MagicMock(spec=AlpacaBrokerClient)
    fake_broker.get_open_positions = AsyncMock(return_value=[])
    fake_broker.cancel_order = AsyncMock()
    fake_broker.submit_exit_order = AsyncMock(return_value=OrderSubmissionResult(
        submitted=True,
        broker_order_id="sector-exit",
        symbol="DDD",
        side="sell",
        quantity=5,
        order_type="limit",
        limit_price=98.5,
        reason="eod_sector_dilution",
    ))

    fake_qp = MagicMock(spec=AlpacaRestQuoteProvider)
    prices = {"AAA": 102.0, "BBB": 101.0, "CCC": 100.5, "DDD": 98.5}
    fake_qp.latest_quote = MagicMock(side_effect=lambda symbol: MarketQuote(
        symbol=symbol,
        timestamp=datetime(2026, 5, 12, 19, 20, tzinfo=timezone.utc),
        bid_price=prices[symbol],
        ask_price=prices[symbol] + 0.1,
    ))

    clock = FixedClock(settings.timezone, datetime(2026, 5, 12, 19, 20, tzinfo=timezone.utc))
    monitor = LiveAlpacaPositionMonitor(repo, settings, fake_broker, fake_qp, clock=clock)
    for slot_id, symbol in enumerate(["AAA", "BBB", "CCC", "DDD"], start=1):
        repo.slots.upsert(
            slot_id,
            status="OPEN",
            symbol=symbol,
            slot_value=1000,
            updated_at=datetime(2026, 5, 12, 19, 20, tzinfo=timezone.utc),
        )
        repo.positions.create_open(
            symbol=symbol,
            quantity=5,
            entry_price=100.0,
            target_price=102.0,
            stop_price=98.0,
            slot_id=slot_id,
            opened_at=datetime(2026, 5, 12, 18, 30, tzinfo=timezone.utc),
            metadata={"sector": "Tech", "protective_stop_order_id": "stop-ddd" if symbol == "DDD" else None},
        )

    result = await monitor.apply_eod_dilution()

    assert result.exit_orders == 1
    fake_broker.submit_exit_order.assert_awaited_once()
    assert {position.symbol for position in repo.positions.list_open()} == {"AAA", "BBB", "CCC"}


@pytest.mark.asyncio
async def test_live_monitor_final_drain_exits_all_positions(settings, repo):
    fake_broker = MagicMock(spec=AlpacaBrokerClient)
    fake_broker.cancel_order = AsyncMock()
    fake_broker.submit_exit_order = AsyncMock(return_value=OrderSubmissionResult(
        submitted=True,
        broker_order_id="final-drain-exit",
        symbol="AAPL",
        side="sell",
        quantity=5,
        order_type="limit",
        limit_price=205.0,
        reason="final_drain",
    ))

    fake_qp = MagicMock(spec=AlpacaRestQuoteProvider)
    clock = FixedClock(settings.timezone, datetime(2026, 5, 12, 19, 50, tzinfo=timezone.utc))
    monitor = LiveAlpacaPositionMonitor(repo, settings, fake_broker, fake_qp, clock=clock)
    repo.slots.upsert(
        1,
        status="OPEN",
        symbol="AAPL",
        slot_value=1000,
        updated_at=datetime(2026, 5, 12, 19, 50, tzinfo=timezone.utc),
    )
    repo.positions.create_open(
        symbol="AAPL",
        quantity=5,
        entry_price=200.0,
        target_price=202.0,
        stop_price=198.0,
        slot_id=1,
        opened_at=datetime(2026, 5, 12, 18, 30, tzinfo=timezone.utc),
        metadata={
            "current_price": 204.0,
            "protective_stop_order_id": "stop-aapl",
        },
    )

    result = await monitor.final_drain_all()

    assert result.exit_orders == 1
    assert result.metadata["source"] == "final_drain"
    fake_broker.cancel_order.assert_awaited_once_with("stop-aapl")
    fake_broker.submit_exit_order.assert_awaited_once_with(
        symbol="AAPL",
        quantity=5.0,
        position_id=1,
    )
    assert repo.positions.list_open() == []
    closed = repo.connection.execute(
        "SELECT symbol, exit_reason, metadata_json FROM positions WHERE status = 'closed'"
    ).fetchone()
    assert closed["symbol"] == "AAPL"
    assert closed["exit_reason"] == "FINAL_DRAIN"
    assert "final_drain" in closed["metadata_json"]
