"""Tests for PM Agent."""

from __future__ import annotations

import pytest

from driftpilot.agents.brain_client import BrainQueryResult
from driftpilot.agents.guardrail_validator import DAILY_LOSS_LIMIT_PCT, GuardrailValidator
from driftpilot.agents.llm_client import LLMClient, LLMResponse
from driftpilot.agents.message_bus import MessageBus
from driftpilot.agents.models import AgentMessage, MessageType
from driftpilot.agents.pm_agent import PMAgent, PortfolioSnapshot
from driftpilot.agents.prompt_loader import PromptConfig, PromptLoader


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
    return PMAgent(bus, llm, prompts, guardrails, brain_client=FakeBrainClient())


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


class FakeBrainClient:
    def __init__(
        self,
        query_result: BrainQueryResult | None = None,
        store_result: str | None = None,
    ) -> None:
        self.query_result = query_result or BrainQueryResult(is_fallback=True)
        self.store_result = store_result
        self.queries: list[dict] = []
        self.stores: list[dict] = []
        self.backfills: list[tuple[str, dict]] = []

    def query(self, context: dict, **kwargs) -> BrainQueryResult:
        self.queries.append({"context": context, "kwargs": kwargs})
        return self.query_result

    def store(
        self,
        context: dict,
        decision: dict,
        exp_type: str = "entry_decision",
        outcome: dict | None = None,
        metadata: dict | None = None,
    ) -> str | None:
        self.stores.append(
            {
                "context": context,
                "decision": decision,
                "exp_type": exp_type,
                "outcome": outcome,
                "metadata": metadata or {},
            }
        )
        return self.store_result

    def backfill(self, experience_id: str, outcome: dict) -> bool:
        self.backfills.append((experience_id, outcome))
        return True


class RecordingLLM:
    def __init__(self) -> None:
        self.prompt: PromptConfig | None = None
        self.template_vars: dict | None = None
        self.rendered_user = ""

    def complete(
        self, prompt: PromptConfig, template_vars: dict
    ) -> LLMResponse:
        self.prompt = prompt
        self.template_vars = template_vars
        self.rendered_user = prompt.render_user(**template_vars)
        return LLMResponse(
            success=True,
            parsed={
                "decision": "approve",
                "reasoning": "brain-aware approval",
                "target_pct": 0.01,
                "size_multiplier": 1.0,
                "confidence": 0.8,
            },
            raw='{"decision":"approve"}',
            model="test-llm",
            latency_ms=1,
        )


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
        """At the daily loss limit, PM force-exits all instead of processing entries."""
        portfolio = PortfolioSnapshot(
            open_slots=3,
            total_slots=10,
            sector_exposure={},
            daily_pnl_pct=-(DAILY_LOSS_LIMIT_PCT + 0.001),
            consecutive_wins=0,
            consecutive_losses=5,
            minutes_left_in_session=120,
            last_trade_result="loss",
            override_count_today=0,
            total_decisions_today=5,
        )
        _send_entry_request(bus)
        result = pm.tick(portfolio)
        # At the daily loss limit the PM force-exits all positions.
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
            daily_pnl_pct=-DAILY_LOSS_LIMIT_PCT,
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


class TestBrainIntegration:
    def test_injects_brain_context_into_entry_prompt(
        self, bus, healthy_portfolio
    ):
        llm = RecordingLLM()
        brain = FakeBrainClient(
            query_result=BrainQueryResult(
                experiences=[
                    {
                        "similarity": 0.91,
                        "context": {
                            "symbol": "MSFT",
                            "signal": "earnings_report_v1",
                            "headline": "MSFT raised guidance",
                        },
                        "decision": {
                            "action": "approve",
                            "reasoning": "clean catalyst",
                        },
                        "outcome": {
                            "pnl_pct": 1.2,
                            "hold_minutes": 18,
                            "exit_reason": "target",
                        },
                    }
                ],
                skills=[
                    {
                        "title": "Prefer clean beats",
                        "rule": "Approve strong guidance raises with high confidence.",
                        "confidence": 0.82,
                    }
                ],
            ),
            store_result="exp-new",
        )
        pm = PMAgent(
            bus,
            llm,
            PromptLoader("config/prompts"),
            GuardrailValidator(),
            brain_client=brain,
        )

        _send_entry_request(bus, symbol="MSFT")
        result = pm.tick(healthy_portfolio)

        assert result.entries_approved == 1
        assert brain.queries[0]["context"]["symbol"] == "MSFT"
        assert brain.queries[0]["context"]["decision_type"] == "entry_decision"
        assert llm.template_vars is not None
        assert "brain_context" in llm.template_vars
        assert "RELEVANT PAST EXPERIENCES" in llm.template_vars["brain_context"]
        assert "ACTIVE TRADING RULES" in llm.template_vars["brain_context"]
        assert "Past similar trades and learned skills" in llm.rendered_user

        responses = bus.poll("scanner", msg_types=[MessageType.ENTRY_DECISION])
        assert responses[0].payload["brain_experience_id"] == "exp-new"
        assert brain.stores[0]["exp_type"] == "entry_decision"

    def test_skips_prompt_injection_when_brain_fallback(
        self, bus, healthy_portfolio
    ):
        llm = RecordingLLM()
        brain = FakeBrainClient(query_result=BrainQueryResult(is_fallback=True))
        pm = PMAgent(
            bus,
            llm,
            PromptLoader("config/prompts"),
            GuardrailValidator(),
            brain_client=brain,
        )

        _send_entry_request(bus)
        result = pm.tick(healthy_portfolio)

        assert result.entries_approved == 1
        assert llm.template_vars is not None
        assert "brain_context" not in llm.template_vars
        assert "Past similar trades and learned skills" not in llm.rendered_user

    def test_backfills_exit_report_when_experience_id_present(
        self, bus, healthy_portfolio
    ):
        brain = FakeBrainClient()
        pm = PMAgent(
            bus,
            RecordingLLM(),
            PromptLoader("config/prompts"),
            GuardrailValidator(),
            brain_client=brain,
        )
        msg = AgentMessage(
            msg_type=MessageType.EXIT_REPORT,
            from_agent="slot_0",
            to_agent="pm",
            payload={
                "symbol": "AAPL",
                "slot_id": 0,
                "exit_reason": "target",
                "pnl_pct": 1.1,
                "hold_minutes": 17,
                "was_override": False,
                "brain_experience_id": "exp-123",
            },
        )
        bus.send(msg)

        result = pm.tick(healthy_portfolio)

        assert result.messages_processed == 1
        assert brain.backfills == [
            (
                "exp-123",
                {
                    "symbol": "AAPL",
                    "exit_reason": "target",
                    "pnl_pct": 1.1,
                    "hold_minutes": 17,
                    "was_override": False,
                    "slot_id": 0,
                    "message_id": msg.msg_id,
                    "correlation_id": None,
                },
            )
        ]
        assert brain.stores == []

    def test_stores_exit_report_when_no_experience_id(
        self, bus, healthy_portfolio
    ):
        brain = FakeBrainClient()
        pm = PMAgent(
            bus,
            RecordingLLM(),
            PromptLoader("config/prompts"),
            GuardrailValidator(),
            brain_client=brain,
        )
        msg = AgentMessage(
            msg_type=MessageType.EXIT_REPORT,
            from_agent="slot_0",
            to_agent="pm",
            payload={
                "symbol": "AAPL",
                "slot_id": 0,
                "exit_reason": "stop",
                "pnl_pct": -0.8,
                "hold_minutes": 11,
            },
        )
        bus.send(msg)

        result = pm.tick(healthy_portfolio)

        assert result.messages_processed == 1
        assert brain.backfills == []
        assert brain.stores[0]["exp_type"] == "exit_report"
        assert brain.stores[0]["outcome"]["pnl_pct"] == -0.8


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
