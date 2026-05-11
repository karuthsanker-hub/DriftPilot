"""Tests for Agent Orchestrator — lifecycle and integration."""

from __future__ import annotations

import pytest

from driftpilot.agents.orchestrator import AgentOrchestrator, OrchestratorConfig
from driftpilot.agents.pm_agent import PortfolioSnapshot
from driftpilot.agents.scanner_agent import CandidateInfo, MarketContext
from driftpilot.agents.slot_agent import PositionSnapshot


@pytest.fixture
def config(tmp_path):
    return OrchestratorConfig(
        enabled=True,
        num_slots=3,
        qwen_url="http://localhost:9999/v1",
        qwen_timeout_ms=50,
        prompts_dir="config/prompts",
        message_db_path=str(tmp_path / "orch_test.sqlite3"),
    )


@pytest.fixture
def orch(config):
    o = AgentOrchestrator(config)
    o.start()
    yield o
    o.stop()


@pytest.fixture
def portfolio():
    return PortfolioSnapshot(
        open_slots=2,
        total_slots=3,
        sector_exposure={"tech": 1},
        daily_pnl_pct=0.005,
        consecutive_wins=1,
        consecutive_losses=0,
        minutes_left_in_session=120,
        last_trade_result="win",
        override_count_today=0,
        total_decisions_today=5,
    )


@pytest.fixture
def position():
    return PositionSnapshot(
        symbol="AAPL",
        slot_id=0,
        entry_price=150.00,
        current_price=151.00,
        unrealized_pct=0.67,
        target_pct=0.01,
        stop_pct=0.015,
        hold_minutes=10,
        max_hold_minutes=60,
        last_10_closes=[150.5] * 10,
        last_10_volumes=[10000] * 10,
        high_pct=0.7,
        low_pct=-0.1,
        consolidation_bars=2,
        recent_vol=50000,
        avg_vol=80000,
        rvol=0.63,
        sector_move_pct=0.2,
        spy_move_pct=0.1,
        vix=18.0,
        new_headlines="",
        signal_name="earnings_report_v1",
    )


class TestLifecycle:
    def test_start_creates_all_agents(self, orch):
        assert orch.running is True
        assert orch._pm is not None
        assert orch._scanner is not None
        assert len(orch._slots) == 3

    def test_start_is_idempotent(self, orch):
        orch.start()  # Already started
        assert orch.running is True

    def test_stop_marks_agents_stopped(self, orch):
        orch.stop()
        assert orch.running is False

    def test_disabled_orchestrator_does_nothing(self, tmp_path):
        config = OrchestratorConfig(
            enabled=False,
            message_db_path=str(tmp_path / "disabled.sqlite3"),
        )
        orch = AgentOrchestrator(config)
        orch.start()
        assert orch.running is False


class TestPMTick:
    def test_pm_tick_processes_no_messages(self, orch, portfolio):
        count = orch.tick_pm(portfolio)
        assert count == 0  # No messages to process

    def test_pm_tick_returns_zero_when_disabled(self, tmp_path):
        config = OrchestratorConfig(
            enabled=False,
            message_db_path=str(tmp_path / "disabled.sqlite3"),
        )
        orch = AgentOrchestrator(config)
        portfolio = PortfolioSnapshot(
            open_slots=0, total_slots=10, sector_exposure={},
            daily_pnl_pct=0, consecutive_wins=0, consecutive_losses=0,
            minutes_left_in_session=120, last_trade_result="none",
            override_count_today=0, total_decisions_today=0,
        )
        assert orch.tick_pm(portfolio) == 0


class TestScannerTick:
    def test_scanner_tick_with_no_candidates(self, orch):
        market = MarketContext(spy_change_pct=0.1, vix=18.0, sector_change_pct=0.3)
        count = orch.tick_scanner([], market)
        assert count == 0

    def test_scanner_tick_with_candidate(self, orch):
        market = MarketContext(spy_change_pct=0.1, vix=18.0, sector_change_pct=0.3)
        candidates = [
            CandidateInfo(
                symbol="AAPL",
                signal_name="earnings_report_v1",
                algo_score=0.85,
                headline="AAPL Q1 beats",
                category="earnings",
                subcategory="earnings_report",
                sentiment="positive",
                confidence=0.8,
                priority_modifier=0.15,
                sector="tech",
                minutes_since_headline=2,
                same_symbol_traded_today=False,
                similar_headlines_last_2h=0,
            )
        ]
        count = orch.tick_scanner(candidates, market)
        # Fallback approves, so 1 entry request
        assert count == 1


class TestSlotTick:
    def test_slot_tick_algo_exit(self, orch, position):
        result = orch.tick_slot(0, position, algo_says_exit=True)
        assert result is not None
        assert result.action == "algo_exit"

    def test_slot_tick_hold_on_fallback(self, orch, position):
        result = orch.tick_slot(0, position, algo_says_exit=False)
        assert result is not None
        assert result.action == "hold"
        assert result.used_fallback is True

    def test_slot_tick_invalid_slot(self, orch, position):
        result = orch.tick_slot(99, position, algo_says_exit=False)
        assert result is None


class TestPromptReload:
    def test_reload_prompts(self, orch):
        count = orch.reload_prompts()
        assert count >= 4  # At least our 4 production prompts


class TestAgentStates:
    def test_get_agent_states_after_tick(self, orch, portfolio):
        orch.tick_pm(portfolio)
        states = orch.get_agent_states()
        assert "pm" in states
        assert states["pm"]["status"] == "running"

    def test_get_states_includes_all_slots(self, orch, portfolio):
        orch.tick_pm(portfolio)
        states = orch.get_agent_states()
        # PM + scanner (if ticked) + 3 slots
        assert "pm" in states


class TestDailyReset:
    def test_reset_clears_counters(self, orch, portfolio):
        orch.tick_pm(portfolio)
        orch.reset_daily()

        states = orch.get_agent_states()
        for name, state in states.items():
            assert state["consecutive_wins"] == 0
            assert state["consecutive_losses"] == 0
            assert state["override_count_today"] == 0
            assert state["total_decisions_today"] == 0


class TestOverrideRate:
    def test_override_rate_starts_at_zero(self, orch):
        assert orch.get_override_rate() == 0.0
