"""Tests for PaperPositionMonitor decide/execute split."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from driftpilot.clock import FixedClock
from driftpilot.services import ExitDecision, PaperPositionMonitor
from driftpilot.settings import DriftPilotSettings
from driftpilot.storage.repositories import DriftPilotRepository

NOW = datetime(2026, 5, 12, 14, 30, tzinfo=UTC)
EOD_FADE_NOW = datetime(2026, 5, 12, 19, 20, tzinfo=UTC)  # 15:20 ET


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


class TestEodDilution:
    def test_before_1515_et_is_inactive(self, tmp_path):
        repo, monitor = _setup(tmp_path, scenario="HOLD")

        result = asyncio.run(monitor.apply_eod_dilution())

        assert result.metadata["active"] is False
        assert result.metadata["reason"] == "before_1515_et"
        assert repo.positions.list_open()[0].stop_price == 149.0

    def test_tightens_stops_and_locks_above_median_winner(self, tmp_path):
        settings = DriftPilotSettings(trade_slots=3, slot_value=1000)
        clock = FixedClock(fixed_now=EOD_FADE_NOW)
        repo = DriftPilotRepository.open(tmp_path / "eod.sqlite3", clock)
        monitor = PaperPositionMonitor(repo, settings, clock=clock)

        for slot_id, symbol, current_price in [
            (1, "AAA", 102.0),
            (2, "BBB", 101.0),
            (3, "CCC", 99.0),
        ]:
            repo.slots.upsert(slot_id, status="OPEN", slot_value=1000, updated_at=EOD_FADE_NOW)
            repo.positions.create_open(
                symbol=symbol,
                quantity=10,
                entry_price=100.0,
                target_price=102.0,
                stop_price=98.0,
                opened_at=EOD_FADE_NOW - timedelta(minutes=20),
                slot_id=slot_id,
                metadata={"scenario": "HOLD", "current_price": current_price, "sector": "Tech"},
            )

        result = asyncio.run(monitor.apply_eod_dilution())

        assert result.metadata["active"] is True
        assert result.metadata["step_count"] == 2
        positions = {position.symbol: position for position in repo.positions.list_open()}
        assert positions["AAA"].stop_price == 102.0  # above median winner locks to bid/current
        assert positions["BBB"].stop_price == 98.6   # 20% of 3-point distance tightened
        assert positions["CCC"].stop_price == 98.2   # 20% of 1-point distance tightened
        assert positions["AAA"].metadata["eod_dilution_active"] is True

    def test_exits_least_profitable_when_sector_has_four_slots(self, tmp_path):
        settings = DriftPilotSettings(trade_slots=4, slot_value=1000)
        clock = FixedClock(fixed_now=EOD_FADE_NOW)
        repo = DriftPilotRepository.open(tmp_path / "eod-sector.sqlite3", clock)
        monitor = PaperPositionMonitor(repo, settings, clock=clock)

        for slot_id, symbol, current_price in [
            (1, "AAA", 102.0),
            (2, "BBB", 101.0),
            (3, "CCC", 100.5),
            (4, "DDD", 98.5),
        ]:
            repo.slots.upsert(slot_id, status="OPEN", slot_value=1000, updated_at=EOD_FADE_NOW)
            repo.positions.create_open(
                symbol=symbol,
                quantity=10,
                entry_price=100.0,
                target_price=102.0,
                stop_price=98.0,
                opened_at=EOD_FADE_NOW - timedelta(minutes=20),
                slot_id=slot_id,
                metadata={"scenario": "HOLD", "current_price": current_price, "sector": "Tech"},
            )

        result = asyncio.run(monitor.apply_eod_dilution())

        assert result.exit_orders == 1
        assert result.recycled_slots == 1
        assert {position.symbol for position in repo.positions.list_open()} == {"AAA", "BBB", "CCC"}
        closed = repo.connection.execute(
            "SELECT symbol, exit_reason FROM positions WHERE status = 'closed'"
        ).fetchone()
        assert tuple(closed) == ("DDD", "EOD_SECTOR_DILUTION")


class TestFinalDrain:
    def test_final_drain_closes_every_open_position(self, tmp_path):
        repo, monitor = _setup(tmp_path, scenario="HOLD")

        result = asyncio.run(monitor.final_drain_all())

        assert result.exit_orders == 1
        assert result.recycled_slots == 1
        assert result.metadata["source"] == "final_drain"
        assert len(repo.positions.list_open()) == 0
        closed = repo.connection.execute(
            "SELECT symbol, exit_reason FROM positions WHERE status = 'closed'"
        ).fetchone()
        assert tuple(closed) == ("AAPL", "FINAL_DRAIN")
