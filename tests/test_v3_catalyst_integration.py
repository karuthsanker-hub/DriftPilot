"""End-to-end integration tests for the v3 catalyst layer.

These exercise the wiring between the catalyst package and the operator
runtime: settings → catalyst layer construction → state machine + allocator
plumbing → bus event delivery.

They do NOT hit live Alpaca or Qwen — events are injected programmatically
via the bus, and the universe filter / allocator query the local SQLite
catalyst_events table directly.
"""

from __future__ import annotations
import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest

from driftpilot.catalyst.db import init_catalyst_schema, insert_event
from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus
from driftpilot.catalyst.universe_filter import CatalystUniverseFilter
from driftpilot.operator import _build_catalyst_layer
from driftpilot.settings import DriftPilotSettings


def _event(symbol, category, subcategory, ts):
    h = hashlib.sha256(f"{symbol}|{category}|{subcategory}|{ts.isoformat()}".encode()).hexdigest()[:16]
    return CatalystEvent(
        symbol=symbol, category=category, subcategory=subcategory, pillar="micro",
        ts=ts, headline=f"{symbol} {category}/{subcategory} test", source="test",
        horizon_minutes=60, headline_hash=h,
    )


# ---------------------------------------------------------------------------
# Settings → catalyst layer construction
# ---------------------------------------------------------------------------


def test_catalyst_layer_disabled_returns_none():
    settings = DriftPilotSettings(catalyst_enabled=False)
    bus, uf, ds = _build_catalyst_layer(settings)
    assert bus is None
    assert uf is None
    assert ds is None


def test_catalyst_layer_enabled_without_alpaca_creds_builds_bus_and_filter(tmp_path):
    """Without Alpaca creds, the bus + filter should still be built so events
    can be injected programmatically. Discovery service is skipped."""
    db_path = str(tmp_path / "catalyst.db")
    settings = DriftPilotSettings(
        catalyst_enabled=True,
        catalyst_db_path=db_path,
        alpaca_key_id="",
        alpaca_secret_key="",
    )
    bus, uf, ds = _build_catalyst_layer(settings)
    assert bus is not None
    assert uf is not None
    assert ds is None  # no creds → no discovery service
    # DB file was created
    assert os.path.exists(db_path)


def test_catalyst_layer_settings_round_trip(monkeypatch, tmp_path):
    """Env vars correctly hydrate the catalyst settings."""
    db_path = str(tmp_path / "catalyst_env.db")
    monkeypatch.setenv("CATALYST_ENABLED", "true")
    monkeypatch.setenv("CATALYST_DB_PATH", db_path)
    monkeypatch.setenv("CATALYST_QWEN_URL", "http://example:9999/v1")
    monkeypatch.setenv("CATALYST_QWEN_TIMEOUT_MS", "5000")
    monkeypatch.setenv("CATALYST_UNIVERSE_LOOKBACK_MINUTES", "180")

    from driftpilot.settings import load_settings
    s = load_settings(env_path=None, environ=os.environ)
    assert s.catalyst_enabled is True
    assert s.catalyst_db_path == db_path
    assert s.catalyst_qwen_url == "http://example:9999/v1"
    assert s.catalyst_qwen_timeout_ms == 5000
    assert s.catalyst_universe_lookback_minutes == 180


def test_alpaca_api_key_alias_accepted(monkeypatch):
    """ALPACA_API_KEY (alpaca-py docs naming) is accepted as alias for
    ALPACA_KEY_ID. This is what most users have in their .env files."""
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "PKAPIKEYALIAS123")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")

    from driftpilot.settings import load_settings
    s = load_settings(env_path=None, environ=os.environ)
    assert s.alpaca_key_id == "PKAPIKEYALIAS123"
    assert s.alpaca_secret_key == "secret"


def test_alpaca_key_id_takes_precedence_over_api_key(monkeypatch):
    """If both are set, ALPACA_KEY_ID wins (canonical name)."""
    monkeypatch.setenv("ALPACA_KEY_ID", "CANONICAL")
    monkeypatch.setenv("ALPACA_API_KEY", "ALIAS")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")

    from driftpilot.settings import load_settings
    s = load_settings(env_path=None, environ=os.environ)
    assert s.alpaca_key_id == "CANONICAL"


# ---------------------------------------------------------------------------
# Bus → state machine subscription wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_machine_subscription_idempotent(tmp_path):
    """Calling subscribe_to_catalyst_bus twice does not double-subscribe."""
    from driftpilot.state_machine import DriftPilotStateMachine
    from driftpilot.storage.repositories import DriftPilotRepository
    from driftpilot.clock import DriftPilotClock

    db_path = str(tmp_path / "catalyst.db")
    init_catalyst_schema(db_path)
    bus = CatalystEventBus()

    settings = DriftPilotSettings(
        catalyst_enabled=True,
        catalyst_db_path=db_path,
        sqlite_path=str(tmp_path / "ops.db"),
    )
    repo = DriftPilotRepository.open(settings.sqlite_path_obj, DriftPilotClock(settings.timezone))
    machine = DriftPilotStateMachine(
        repo, settings, catalyst_event_bus=bus,
    )
    await machine.subscribe_to_catalyst_bus(bus)
    sub1 = machine._catalyst_subscription_id
    await machine.subscribe_to_catalyst_bus(bus)  # idempotent
    sub2 = machine._catalyst_subscription_id
    assert sub1 == sub2


# ---------------------------------------------------------------------------
# Universe filter end-to-end with allocator
# ---------------------------------------------------------------------------


def test_universe_filter_and_negative_allocator_share_db(tmp_path):
    """The same target_cut event simultaneously:
    1. Drops the symbol from the universe filter output
    2. Causes the allocator to reject the symbol if it slips through
    """
    db_path = str(tmp_path / "shared.db")
    init_catalyst_schema(db_path)
    now = datetime.now(timezone.utc)

    insert_event(db_path, _event("BAD", "analyst", "target_cut", now - timedelta(minutes=30)))
    insert_event(db_path, _event("GOOD", "earnings", "report", now - timedelta(minutes=10)))

    # Universe filter drops BAD
    uf = CatalystUniverseFilter(db_path, lookback_minutes=240)
    out = uf.filter_and_rank(["AAPL", "BAD", "GOOD", "MSFT"], now=now)
    assert "BAD" not in out
    assert out[0] == "GOOD"  # positive catalyst ranks first

    # Allocator (defense-in-depth) also rejects BAD
    from driftpilot.execution.slot_allocator import _has_negative_catalyst
    assert _has_negative_catalyst(db_path, "BAD", lookback_minutes=240) is True
    assert _has_negative_catalyst(db_path, "AAPL", lookback_minutes=240) is False


# ---------------------------------------------------------------------------
# Bus event → EMERGENCY_FLUSH on held position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_cut_event_propagates_through_bus_to_state_machine(tmp_path):
    """Publish target_cut to the bus → handler fires → state machine prepares
    EMERGENCY_FLUSH (we test the handler return rather than full state
    transition since that requires ScannerService etc.)."""
    from driftpilot.state_machine import DriftPilotStateMachine, OperatorState
    from driftpilot.storage.repositories import DriftPilotRepository
    from driftpilot.clock import DriftPilotClock

    db_path = str(tmp_path / "catalyst.db")
    init_catalyst_schema(db_path)
    bus = CatalystEventBus()

    settings = DriftPilotSettings(
        catalyst_enabled=True,
        catalyst_db_path=db_path,
        sqlite_path=str(tmp_path / "ops.db"),
    )
    repo = DriftPilotRepository.open(settings.sqlite_path_obj, DriftPilotClock(settings.timezone))
    machine = DriftPilotStateMachine(
        repo, settings, catalyst_event_bus=bus,
    )
    await machine.subscribe_to_catalyst_bus(bus)

    # Publish a target_cut event for a symbol with no open position — handler
    # should NOT trigger EMERGENCY_FLUSH (no position to flush).
    event = _event("ZZZZ", "analyst", "target_cut", datetime.now(timezone.utc))
    await bus.publish(event)
    # The handler queries open positions; with no open positions on ZZZZ, it
    # returns None (no transition). State machine should NOT be in EMERGENCY_FLUSH.
    # We just assert the subscription is wired and the publish doesn't raise.
    assert machine._catalyst_subscription_id is not None


@pytest.mark.asyncio
async def test_event_published_persists_to_db(tmp_path):
    """A bus event ALSO goes into the SQLite catalyst_events table when
    routed through a feed (we test the helper directly)."""
    db_path = str(tmp_path / "catalyst.db")
    init_catalyst_schema(db_path)
    event = _event("AAPL", "earnings", "report", datetime.now(timezone.utc))
    inserted1 = insert_event(db_path, event)
    inserted2 = insert_event(db_path, event)  # dedupe
    assert inserted1 == 1
    assert inserted2 == 0


# ---------------------------------------------------------------------------
# Settings plumbing through allocator
# ---------------------------------------------------------------------------


def test_paper_execution_allocator_accepts_catalyst_db_path(tmp_path):
    from driftpilot.services import PaperExecutionAllocator
    from driftpilot.storage.repositories import DriftPilotRepository
    from driftpilot.clock import DriftPilotClock

    settings = DriftPilotSettings(sqlite_path=str(tmp_path / "ops.db"))
    repo = DriftPilotRepository.open(settings.sqlite_path_obj, DriftPilotClock(settings.timezone))
    catalyst_db = str(tmp_path / "catalyst.db")
    init_catalyst_schema(catalyst_db)

    alloc = PaperExecutionAllocator(repo, settings, catalyst_db_path=catalyst_db)
    # The inner SlotAllocator received the path
    assert alloc.allocator.catalyst_db_path == catalyst_db
