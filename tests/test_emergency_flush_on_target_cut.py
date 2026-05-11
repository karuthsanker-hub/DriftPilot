from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from driftpilot.catalyst.event import CatalystEvent
from driftpilot.clock import FixedClock
from driftpilot.settings import DriftPilotSettings
from driftpilot.state_machine import DriftPilotStateMachine
from driftpilot.states import OperatorState
from driftpilot.storage.repositories import DriftPilotRepository


NOW = datetime(2026, 5, 1, 14, 30, tzinfo=UTC)


def _make_event(symbol: str) -> CatalystEvent:
    headline = "Analyst cuts price target"
    h = hashlib.sha256(f"{symbol}-{headline}".encode()).hexdigest()
    return CatalystEvent(
        symbol=symbol,
        category="analyst",
        subcategory="target_cut",
        pillar="micro",
        ts=NOW,
        headline=headline,
        source="test",
        horizon_minutes=240,
        headline_hash=h,
        sentiment="negative",
    )


@pytest.mark.asyncio
async def test_target_cut_event_with_open_position_transitions_to_emergency_flush(tmp_path) -> None:
    repo = DriftPilotRepository.open(tmp_path / "operator.sqlite3", FixedClock(fixed_now=NOW))
    repo.slots.upsert(
        1,
        status="OPEN",
        slot_value=1_000.0,
        symbol="AAPL",
        updated_at=NOW,
    )
    settings = DriftPilotSettings(scan_interval_seconds=30)
    sm = DriftPilotStateMachine(repo, settings, clock=FixedClock(fixed_now=NOW))

    result = await sm.on_analyst_target_cut(_make_event("AAPL"))

    assert result == OperatorState.EMERGENCY_FLUSH
    # Last transition is EXITING (delegated by emergency_flush), but EMERGENCY_FLUSH
    # was recorded immediately before. Verify transition log contains it.
    transitions = repo.transitions.list_latest(limit=5)
    state_values = [t.to_state for t in transitions]
    assert OperatorState.EMERGENCY_FLUSH.value in state_values


@pytest.mark.asyncio
async def test_target_cut_event_no_open_position_no_transition(tmp_path) -> None:
    repo = DriftPilotRepository.open(tmp_path / "operator.sqlite3", FixedClock(fixed_now=NOW))
    repo.slots.upsert(1, status="EMPTY", slot_value=1_000.0, updated_at=NOW)
    settings = DriftPilotSettings(scan_interval_seconds=30)
    sm = DriftPilotStateMachine(repo, settings, clock=FixedClock(fixed_now=NOW))

    result = await sm.on_analyst_target_cut(_make_event("AAPL"))

    assert result is None
    transitions = repo.transitions.list_latest(limit=5)
    assert OperatorState.EMERGENCY_FLUSH.value not in [t.to_state for t in transitions]


def test_emergency_flush_state_value_present() -> None:
    assert OperatorState.EMERGENCY_FLUSH.value == "EMERGENCY_FLUSH"
