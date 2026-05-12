"""Tests for state_machine_bridge — adapts state-machine types to agent types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from driftpilot.agents.state_machine_bridge import (
    tick_pm_from_repo,
    tick_scanner_from_candidates,
    tick_slots_from_positions,
)


# ── Helpers ──────────────────────────────────────────────────────────────

@dataclass
class FakePositionRecord:
    id: int
    symbol: str
    status: str = "open"
    quantity: int = 100
    entry_price: float = 10.0
    target_price: float = 11.0
    stop_price: float = 9.5
    opened_at: datetime = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
    closed_at: datetime | None = None
    slot_id: int | None = 1
    realized_pnl: float = 0.0
    metadata: dict[str, Any] | None = None


@dataclass
class FakeAllocationCandidate:
    symbol: str
    score: float
    sector: str = "Technology"
    latest_bar_at: datetime | None = None
    metadata: dict[str, Any] | None = None


@dataclass
class FakeSlot:
    slot_id: int
    status: str = "OPEN"


@dataclass
class FakeSettings:
    paper_capital: float = 100_000.0
    max_hold_minutes: int = 60
    active_signal: str = "earnings_report_v1"
    timezone: str = "US/Eastern"


def _mock_orchestrator(running: bool = True) -> MagicMock:
    orch = MagicMock()
    orch.running = running
    orch.tick_pm.return_value = 1
    orch.tick_slot.return_value = MagicMock(action="HOLD")
    orch.tick_scanner.return_value = 2
    return orch


# ── tick_pm_from_repo ────────────────────────────────────────────────────

class TestTickPm:
    def test_returns_zero_when_no_orchestrator(self):
        assert tick_pm_from_repo(None, MagicMock(), FakeSettings()) == 0

    def test_returns_zero_when_not_running(self):
        orch = _mock_orchestrator(running=False)
        assert tick_pm_from_repo(orch, MagicMock(), FakeSettings()) == 0

    def test_builds_snapshot_and_calls_tick_pm(self):
        orch = _mock_orchestrator()
        repo = MagicMock()
        repo.slots.list_all.return_value = [FakeSlot(1), FakeSlot(2, "EMPTY")]
        repo.positions.list_open.return_value = [
            FakePositionRecord(1, "AAPL", metadata={"sector": "Technology"}),
        ]
        row = {"pnl": 150.0}
        repo.connection.execute.return_value.fetchone.return_value = row
        repo.connection.execute.return_value.fetchall.return_value = []

        with patch("driftpilot.clock.DriftPilotClock") as MockClock:
            mock_clock = MockClock.return_value
            et_now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
            mock_clock.to_et.return_value = et_now
            result = tick_pm_from_repo(orch, repo, FakeSettings())

        assert result == 1
        orch.tick_pm.assert_called_once()
        snapshot = orch.tick_pm.call_args[0][0]
        assert snapshot.open_slots == 1  # only the OPEN slot
        assert snapshot.total_slots == 2
        assert snapshot.sector_exposure == {"Technology": 1}

    def test_catches_exceptions(self):
        orch = _mock_orchestrator()
        orch.tick_pm.side_effect = RuntimeError("boom")
        repo = MagicMock()
        repo.slots.list_all.return_value = []
        repo.positions.list_open.return_value = []
        repo.connection.execute.return_value.fetchone.return_value = {"pnl": 0}
        repo.connection.execute.return_value.fetchall.return_value = []

        with patch("driftpilot.clock.DriftPilotClock"):
            result = tick_pm_from_repo(orch, repo, FakeSettings())
        assert result == 0


# ── tick_slots_from_positions ────────────────────────────────────────────

class TestTickSlots:
    def test_returns_empty_when_no_orchestrator(self):
        assert tick_slots_from_positions(None, [], {}, FakeSettings()) == {}

    def test_returns_empty_when_not_running(self):
        orch = _mock_orchestrator(running=False)
        assert tick_slots_from_positions(orch, [], {}, FakeSettings()) == {}

    def test_skips_positions_without_slot_id(self):
        orch = _mock_orchestrator()
        pos = FakePositionRecord(1, "AAPL", slot_id=None)
        result = tick_slots_from_positions(orch, [pos], {}, FakeSettings())
        assert result == {}
        orch.tick_slot.assert_not_called()

    def test_builds_snapshot_and_calls_tick_slot(self):
        orch = _mock_orchestrator()
        pos = FakePositionRecord(
            1, "AAPL", slot_id=3,
            metadata={"current_price": 10.5, "signal_name": "test_signal"},
        )
        exit_decisions = {1: ("TARGET", 11.0)}
        result = tick_slots_from_positions(orch, [pos], exit_decisions, FakeSettings())

        assert result == {3: "HOLD"}
        orch.tick_slot.assert_called_once()
        call_args = orch.tick_slot.call_args
        assert call_args[0][0] == 3  # slot_id
        snapshot = call_args[0][1]
        assert snapshot.symbol == "AAPL"
        assert snapshot.current_price == 10.5
        assert call_args[0][2] is True  # algo_says_exit

    def test_algo_hold_passes_false(self):
        orch = _mock_orchestrator()
        pos = FakePositionRecord(1, "AAPL", slot_id=2)
        exit_decisions = {1: (None, 10.0)}  # None = HOLD
        tick_slots_from_positions(orch, [pos], exit_decisions, FakeSettings())
        assert orch.tick_slot.call_args[0][2] is False

    def test_catches_per_position_exceptions(self):
        orch = _mock_orchestrator()
        orch.tick_slot.side_effect = RuntimeError("boom")
        pos = FakePositionRecord(1, "AAPL", slot_id=1)
        result = tick_slots_from_positions(orch, [pos], {}, FakeSettings())
        assert result == {}  # error caught, no crash


# ── tick_scanner_from_candidates ─────────────────────────────────────────

class TestTickScanner:
    def test_returns_zero_when_no_orchestrator(self):
        assert tick_scanner_from_candidates(None, [], None, {}) == 0

    def test_returns_zero_when_not_running(self):
        orch = _mock_orchestrator(running=False)
        assert tick_scanner_from_candidates(orch, [], None, {}) == 0

    def test_returns_zero_when_no_candidates(self):
        orch = _mock_orchestrator()
        assert tick_scanner_from_candidates(orch, [], None, {}) == 0

    def test_converts_candidates_and_calls_tick_scanner(self):
        orch = _mock_orchestrator()
        cand = FakeAllocationCandidate(
            "TSLA", 0.85, "Technology",
            metadata={
                "signal_name": "earnings_report_v1",
                "headline": "TSLA beats earnings",
                "category": "earnings",
                "sentiment": "positive",
                "confidence": 0.9,
            },
        )
        meta = {"spy_return_5m": -0.3, "vix": 22.0}
        result = tick_scanner_from_candidates(orch, [cand], "bull", meta)

        assert result == 2
        orch.tick_scanner.assert_called_once()
        agent_cands = orch.tick_scanner.call_args[0][0]
        assert len(agent_cands) == 1
        assert agent_cands[0].symbol == "TSLA"
        assert agent_cands[0].algo_score == 0.85
        assert agent_cands[0].sentiment == "positive"
        market = orch.tick_scanner.call_args[0][1]
        assert market.vix == 22.0

    def test_catches_exceptions(self):
        orch = _mock_orchestrator()
        orch.tick_scanner.side_effect = RuntimeError("boom")
        cand = FakeAllocationCandidate("AAPL", 0.5)
        result = tick_scanner_from_candidates(orch, [cand], None, {})
        assert result == 0
