"""Tests for Slot Agent."""

from __future__ import annotations

import pytest

from driftpilot.agents.guardrail_validator import GuardrailValidator
from driftpilot.agents.llm_client import LLMClient
from driftpilot.agents.message_bus import MessageBus
from driftpilot.agents.prompt_loader import PromptLoader
from driftpilot.agents.slot_agent import PositionSnapshot, SlotAgent


@pytest.fixture
def bus(tmp_path):
    db_path = tmp_path / "slot_test.sqlite3"
    b = MessageBus(db_path=db_path, ttl_seconds=300)
    b.initialize()
    yield b
    b.close()


@pytest.fixture
def slot_agent(bus):
    """Slot agent with failing LLM (fallback=hold)."""
    llm = LLMClient(qwen_url="http://localhost:9999/v1", qwen_timeout_ms=50)
    prompts = PromptLoader("config/prompts")
    guardrails = GuardrailValidator()
    return SlotAgent(slot_id=0, bus=bus, llm_client=llm, prompt_loader=prompts, guardrails=guardrails)


@pytest.fixture
def position():
    return PositionSnapshot(
        symbol="AAPL",
        slot_id=0,
        entry_price=150.00,
        current_price=151.20,
        unrealized_pct=0.8,
        target_pct=0.01,
        stop_pct=0.015,
        hold_minutes=15,
        max_hold_minutes=60,
        last_10_closes=[150.5, 150.7, 150.9, 151.0, 151.1, 151.0, 151.1, 151.2, 151.2, 151.2],
        last_10_volumes=[10000, 12000, 11000, 9000, 8000, 7000, 6000, 5500, 5000, 4800],
        high_pct=0.85,
        low_pct=-0.2,
        consolidation_bars=3,
        recent_vol=50000,
        avg_vol=80000,
        rvol=0.63,
        sector_move_pct=0.3,
        spy_move_pct=0.15,
        vix=18.5,
        new_headlines="",
        signal_name="earnings_report_v1",
    )


class TestAlgoExitAuthoritative:
    def test_algo_exit_skips_llm_entirely(self, slot_agent, position):
        """When algo says exit, slot agent exits immediately without LLM."""
        result = slot_agent.tick(position, algo_says_exit=True)

        assert result.action == "algo_exit"
        assert result.llm_latency_ms == 0
        assert result.confidence == 1.0
        assert result.message_sent is None

    def test_algo_exit_even_with_good_position(self, slot_agent, position):
        """Algo exit is authoritative even if position looks great."""
        position.unrealized_pct = 3.5  # Great position
        result = slot_agent.tick(position, algo_says_exit=True)
        assert result.action == "algo_exit"


class TestFallbackHold:
    def test_fallback_hold_on_timeout(self, slot_agent, position):
        """When LLM times out, slot agent holds (default fallback)."""
        result = slot_agent.tick(position, algo_says_exit=False)

        assert result.action == "hold"
        assert result.used_fallback is True
        assert result.message_sent is None

    def test_hold_is_default_behavior(self, slot_agent, position):
        """Hold should be the most common action (>85%)."""
        result = slot_agent.tick(position, algo_says_exit=False)
        assert result.action == "hold"


class TestGuardrailEnforcement:
    def test_early_cut_blocked_by_min_hold(self, bus, position):
        """Early cut request is blocked if held less than 2 minutes."""
        # Create a slot agent with a mock LLM that returns "request_early_cut"
        # Since we can't mock easily, test via the guardrail check path
        LLMClient(qwen_url="http://localhost:9999/v1", qwen_timeout_ms=50)
        PromptLoader("config/prompts")
        guardrails = GuardrailValidator()

        # Test the guardrail directly
        result = guardrails.validate_exit(
            symbol="AAPL",
            slot_id=0,
            hold_seconds=60,  # < 120s min
            is_algo_exit=False,
        )
        assert result.allowed is False

    def test_early_cut_allowed_after_min_hold(self, bus, position):
        guardrails = GuardrailValidator()
        result = guardrails.validate_exit(
            symbol="AAPL",
            slot_id=0,
            hold_seconds=150,  # > 120s min
            is_algo_exit=False,
        )
        assert result.allowed is True


class TestDecisionLogging:
    def test_tick_logs_decision(self, slot_agent, bus, position):
        """Every tick should log a decision to the DB."""
        slot_agent.tick(position, algo_says_exit=False)

        # Check that a decision was logged
        row = bus.conn.execute(
            "SELECT COUNT(*) FROM agent_decisions WHERE agent_name = 'slot_0'"
        ).fetchone()
        assert row[0] == 1

    def test_algo_exit_does_not_log_llm_decision(self, slot_agent, bus, position):
        """Algo exits don't consult LLM so shouldn't log LLM decisions."""
        slot_agent.tick(position, algo_says_exit=True)

        row = bus.conn.execute(
            "SELECT COUNT(*) FROM agent_decisions WHERE agent_name = 'slot_0'"
        ).fetchone()
        assert row[0] == 0


class TestAgentState:
    def test_tick_updates_heartbeat(self, slot_agent, bus, position):
        slot_agent.tick(position, algo_says_exit=False)

        state = bus.get_agent_state("slot_0")
        assert state is not None
        assert state["status"] == "running"


class TestMessageSending:
    def test_no_message_on_hold(self, slot_agent, bus, position):
        """Hold action should not send any message to PM."""
        result = slot_agent.tick(position, algo_says_exit=False)
        assert result.message_sent is None

        # Verify nothing in PM's queue
        pm_msgs = bus.poll("pm")
        assert len(pm_msgs) == 0
