"""Tests for PaperPositionMonitor decide/execute split."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from driftpilot.clock import FixedClock
from driftpilot.services import ExitDecision, PaperPositionMonitor
from driftpilot.settings import DriftPilotSettings
from driftpilot.storage.repositories import DriftPilotRepository

NOW = datetime(2026, 5, 12, 14, 30, tzinfo=UTC)


def _setup(tmp_path, scenario="TARGET"):
    """Create a repo with one open position and return (repo, monitor)."""
    settings = DriftPilotSettings(
        trade_slots=2, slot_value=1000, max_hold_minutes=60,
    )
    clock = FixedClock(fixed_now=NOW)
    repo = DriftPilotRepository.open(tmp_path / "test.sqlite3", clock)

    # Create slot + position
    repo.slots.upsert(1, status="OPEN", slot_value=1000, updated_at=NOW)
    repo.positions.create_open(
        symbol="AAPL",
        quantity=100,
        entry_price=150.0,
        target_price=151.5,
        stop_price=149.0,
        opened_at=NOW - timedelta(minutes=5),
        slot_id=1,
        metadata={"scenario": scenario, "current_price": 150.5},
    )

    monitor = PaperPositionMonitor(repo, settings, clock=clock)
    return repo, monitor


class TestDecidePhase:
    def test_decide_returns_exit_decisions(self, tmp_path):
        repo, monitor = _setup(tmp_path, scenario="TARGET")
        decisions = monitor.decide()

        assert len(decisions) == 1
        d = decisions[0]
        assert isinstance(d, ExitDecision)
        assert d.position.symbol == "AAPL"
        # With scenario=TARGET and age > 1 min, algo says exit
        assert d.exit_reason == "TARGET"
        assert d.reference_price == 151.5

    def test_decide_returns_hold_for_fresh_position(self, tmp_path):
        settings = DriftPilotSettings(
            trade_slots=2, slot_value=1000, max_hold_minutes=60,
        )
        clock = FixedClock(fixed_now=NOW)
        repo = DriftPilotRepository.open(tmp_path / "test.sqlite3", clock)
        repo.slots.upsert(1, status="OPEN", slot_value=1000, updated_at=NOW)
        repo.positions.create_open(
            symbol="AAPL",
            quantity=100,
            entry_price=150.0,
            target_price=151.5,
            stop_price=149.0,
            opened_at=NOW,  # opened just now — under 1 minute
            slot_id=1,
            metadata={"scenario": "TARGET", "current_price": 150.5},
        )
        monitor = PaperPositionMonitor(repo, settings, clock=clock)
        decisions = monitor.decide()

        assert len(decisions) == 1
        assert decisions[0].exit_reason is None  # HOLD

    def test_decide_does_not_execute(self, tmp_path):
        repo, monitor = _setup(tmp_path)
        decisions = monitor.decide()

        # Position should still be open after decide
        assert len(repo.positions.list_open()) == 1
        assert decisions[0].exit_reason == "TARGET"


class TestExecutePhase:
    def test_execute_closes_position(self, tmp_path):
        repo, monitor = _setup(tmp_path)
        decisions = monitor.decide()

        result = asyncio.run(monitor.execute(decisions))

        assert result.exit_orders == 1
        assert result.recycled_slots == 1
        assert len(repo.positions.list_open()) == 0

    def test_execute_skips_holds(self, tmp_path):
        repo, monitor = _setup(tmp_path)
        decisions = monitor.decide()

        # Override the decision to HOLD
        hold_decisions = [
            ExitDecision(
                position=decisions[0].position,
                exit_reason=None,  # HOLD
                reference_price=decisions[0].reference_price,
            )
        ]

        result = asyncio.run(monitor.execute(hold_decisions))
        assert result.exit_orders == 0
        assert len(repo.positions.list_open()) == 1

    def test_execute_records_agent_override(self, tmp_path):
        repo, monitor = _setup(tmp_path)
        decisions = monitor.decide()

        # Override with agent action
        agent_decisions = [
            ExitDecision(
                position=decisions[0].position,
                exit_reason="AGENT_CUT",
                reference_price=decisions[0].reference_price,
                overridden_by_agent=True,
                agent_action="request_early_cut",
            )
        ]

        result = asyncio.run(monitor.execute(agent_decisions))
        assert result.exit_orders == 1


class TestMonitorBackcompat:
    def test_monitor_calls_decide_then_execute(self, tmp_path):
        repo, monitor = _setup(tmp_path)
        result = asyncio.run(monitor.monitor())

        # Same behavior as before
        assert result.exit_orders == 1
        assert result.recycled_slots == 1
        assert len(repo.positions.list_open()) == 0
