"""Tests for PM Agent."""

from __future__ import annotations

import pytest

from driftpilot.agents.guardrail_validator import GuardrailValidator
from driftpilot.agents.llm_client import LLMClient
from driftpilot.agents.message_bus import MessageBus
from driftpilot.agents.models import AgentMessage, MessageType
from driftpilot.agents.pm_agent import PMAgent, PortfolioSnapshot
from driftpilot.agents.prompt_loader import PromptLoader


@pytest.fixture
def bus(tmp_path):
    db_path = tmp_path / "pm_test.sqlite3"
    b = MessageBus(db_path=db_path, ttl_seconds=300)
    b.initialize()
    yield b
    b.close()


@pytest.fixture
def pm(bus):
    """PM agent with failing LLM (always uses fallback=approve)."""
    llm = LLMClient(qwen_url="http://localhost:9999/v1", qwen_timeout_ms=50)
    prompts = PromptLoader("config/prompts")
    guardrails = GuardrailValidator()
    return PMAgent(bus, llm, prompts, guardrails)


@pytest.fixture
def healthy_portfolio():
    return PortfolioSnapshot(
        open_slots=3,
        total_slots=10,
        sector_exposure={"tech": 1, "healthcare": 1, "finance": 1},
        daily_pnl_pct=0.005,
        consecutive_wins=2,
        consecutive_losses=0,
        minutes_left_in_session=120,
        last_trade_result="win",
        override_count_today=1,
        total_decisions_today=10,
    )


def _send_entry_request(bus: MessageBus, symbol: str = "AAPL", sector: str = "tech"):
    msg = AgentMessage(
        msg_type=MessageType.ENTRY_REQUEST,
        from_agent="scanner",
        to_agent="pm",
        payload={
            "symbol": symbol,
            "signal_name": "earnings_report_v1",
            "algo_score": 0.85,
            "headline": f"{symbol} Q1 EPS Beats",
            "sentiment": "positive",
            "confidence": 0.8,
            "priority_modifier": 0.15,
            "proposed_target_pct": 0.01,
            "proposed_stop_pct": 0.015,
            "sector": sector,
        },
    )
    msg.correlation_id = msg.msg_id
    bus.send(msg)
    return msg


class TestEntryApproval:
    def test_approve_valid_entry(self, pm, bus, healthy_portfolio):
        _send_entry_request(bus)
        result = pm.tick(healthy_portfolio)
        assert result.entries_approved == 1
        assert result.entries_denied == 0

    def test_deny_sector_crowded(self, pm, bus):
        portfolio = PortfolioSnapshot(
            open_slots=5,
            total_slots=10,
            sector_exposure={"tech": 3},  # At cap
            daily_pnl_pct=0.0,
            consecutive_wins=0,
            consecutive_losses=0,
            minutes_left_in_session=120,
            last_trade_result="none",
            override_count_today=0,
            total_decisions_today=5,
        )
        _send_entry_request(bus, sector="tech")
        result = pm.tick(portfolio)
        assert result.entries_denied == 1
        assert result.entries_approved == 0

    def test_deny_session_drawdown(self, pm, bus):
        """At -3%+ daily loss, PM force-exits all instead of processing entries."""
        portfolio = PortfolioSnapshot(
            open_slots=3,
            total_slots=10,
            sector_exposure={},
            daily_pnl_pct=-0.031,  # Beyond 3% limit
            consecutive_wins=0,
            consecutive_losses=5,
            minutes_left_in_session=120,
            last_trade_result="loss",
            override_count_today=0,
            total_decisions_today=5,
        )
        _send_entry_request(bus)
        result = pm.tick(portfolio)
        # At -3% the PM force-exits all positions (higher priority than entry processing)
        assert result.force_exits_issued == 10
        assert result.entries_approved == 0

    def test_deny_last_30_minutes(self, pm, bus):
        portfolio = PortfolioSnapshot(
            open_slots=5,
            total_slots=10,
            sector_exposure={},
            daily_pnl_pct=0.01,
            consecutive_wins=2,
            consecutive_losses=0,
            minutes_left_in_session=25,  # < 30 min
            last_trade_result="win",
            override_count_today=0,
            total_decisions_today=5,
        )
        _send_entry_request(bus)
        result = pm.tick(portfolio)
        assert result.entries_denied == 1

    def test_deny_no_free_slots(self, pm, bus):
        portfolio = PortfolioSnapshot(
            open_slots=10,
            total_slots=10,
            sector_exposure={},
            daily_pnl_pct=0.0,
            consecutive_wins=0,
            consecutive_losses=0,
            minutes_left_in_session=120,
            last_trade_result="none",
            override_count_today=0,
            total_decisions_today=5,
        )
        _send_entry_request(bus)
        result = pm.tick(portfolio)
        assert result.entries_denied == 1


class TestForceExit:
    def test_force_exit_on_daily_limit(self, pm, bus):
        portfolio = PortfolioSnapshot(
            open_slots=5,
            total_slots=10,
            sector_exposure={},
            daily_pnl_pct=-0.03,  # At limit
            consecutive_wins=0,
            consecutive_losses=5,
            minutes_left_in_session=120,
            last_trade_result="loss",
            override_count_today=0,
            total_decisions_today=10,
        )
        result = pm.tick(portfolio)
        assert result.force_exits_issued == 10  # All slots get force exit


class TestTargetRaise:
    def test_approve_high_confidence_raise(self, pm, bus, healthy_portfolio):
        msg = AgentMessage(
            msg_type=MessageType.TARGET_RAISE_REQUEST,
            from_agent="slot_0",
            to_agent="pm",
            payload={
                "symbol": "AAPL",
                "slot_id": 0,
                "current_target_pct": 0.01,
                "proposed_target_pct": 0.025,
                "unrealized_pct": 1.5,
                "reasoning": "strong momentum",
                "confidence": 0.85,
            },
        )
        msg.correlation_id = msg.msg_id
        bus.send(msg)

        result = pm.tick(healthy_portfolio)
        assert result.raises_approved == 1

    def test_deny_low_confidence_raise(self, pm, bus, healthy_portfolio):
        msg = AgentMessage(
            msg_type=MessageType.TARGET_RAISE_REQUEST,
            from_agent="slot_0",
            to_agent="pm",
            payload={
                "symbol": "AAPL",
                "slot_id": 0,
                "current_target_pct": 0.01,
                "proposed_target_pct": 0.025,
                "unrealized_pct": 0.8,
                "reasoning": "maybe momentum",
                "confidence": 0.5,  # Below 0.7 threshold
            },
        )
        msg.correlation_id = msg.msg_id
        bus.send(msg)

        result = pm.tick(healthy_portfolio)
        assert result.raises_denied == 1


class TestFallbackBehavior:
    def test_fallback_approves_on_qwen_timeout(self, pm, bus, healthy_portfolio):
        """When LLM times out, PM uses fallback=approve for entries."""
        _send_entry_request(bus)
        result = pm.tick(healthy_portfolio)
        # Should approve because fallback_action for pm_entry_approval is "approve"
        assert result.entries_approved == 1


class TestOverrideRateLimit:
    def test_deny_when_override_rate_exceeded(self, pm, bus):
        portfolio = PortfolioSnapshot(
            open_slots=3,
            total_slots=10,
            sector_exposure={},
            daily_pnl_pct=0.0,
            consecutive_wins=0,
            consecutive_losses=0,
            minutes_left_in_session=120,
            last_trade_result="none",
            override_count_today=5,  # 5/20 = 25% > 20%
            total_decisions_today=20,
        )
        _send_entry_request(bus)
        result = pm.tick(portfolio)
        assert result.entries_denied == 1
