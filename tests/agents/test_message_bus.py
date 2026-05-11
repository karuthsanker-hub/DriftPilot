"""Tests for A2A message bus."""

from __future__ import annotations

import time

import pytest

from driftpilot.agents.message_bus import MessageBus
from driftpilot.agents.models import AgentMessage, MessageType


@pytest.fixture
def bus(tmp_path):
    """Create a MessageBus with a temporary DB."""
    db_path = tmp_path / "test_agent_messages.sqlite3"
    b = MessageBus(db_path=db_path, ttl_seconds=5)
    b.initialize()
    yield b
    b.close()


def _entry_request(from_agent: str = "scanner", to_agent: str = "pm") -> AgentMessage:
    return AgentMessage(
        msg_type=MessageType.ENTRY_REQUEST,
        from_agent=from_agent,
        to_agent=to_agent,
        payload={"symbol": "AAPL", "signal_name": "earnings_report_v1"},
    )


class TestSendReceiveRoundtrip:
    def test_send_and_poll_retrieves_message(self, bus: MessageBus):
        msg = _entry_request()
        bus.send(msg)

        received = bus.poll("pm")
        assert len(received) == 1
        assert received[0].msg_id == msg.msg_id
        assert received[0].msg_type == MessageType.ENTRY_REQUEST
        assert received[0].payload["symbol"] == "AAPL"

    def test_poll_returns_empty_for_wrong_agent(self, bus: MessageBus):
        bus.send(_entry_request(to_agent="pm"))
        assert bus.poll("scanner") == []

    def test_poll_only_returns_pending_messages(self, bus: MessageBus):
        msg = _entry_request()
        bus.send(msg)
        bus.mark_processed(msg.msg_id)

        assert bus.poll("pm") == []


class TestMessageExpiry:
    def test_messages_expire_after_ttl(self, tmp_path):
        db_path = tmp_path / "test_expire.sqlite3"
        bus = MessageBus(db_path=db_path, ttl_seconds=1)
        bus.initialize()

        bus.send(_entry_request())
        time.sleep(1.1)

        # poll triggers expiry check
        received = bus.poll("pm")
        assert received == []
        bus.close()

    def test_non_expired_messages_survive(self, bus: MessageBus):
        bus.send(_entry_request())
        # TTL is 5s, no sleep
        received = bus.poll("pm")
        assert len(received) == 1


class TestCorrelationId:
    def test_correlation_id_links_request_response(self, bus: MessageBus):
        request = AgentMessage(
            msg_type=MessageType.ENTRY_REQUEST,
            from_agent="scanner",
            to_agent="pm",
            correlation_id="corr-123",
            payload={"symbol": "TSLA"},
        )
        bus.send(request)

        # PM sends response with same correlation_id
        response = AgentMessage(
            msg_type=MessageType.ENTRY_DECISION,
            from_agent="pm",
            to_agent="scanner",
            correlation_id="corr-123",
            payload={"decision": "approve"},
        )
        bus.send(response)

        # Scanner can find the response by correlation
        result = bus.get_response("corr-123")
        assert result is not None
        assert result.msg_type == MessageType.ENTRY_DECISION
        assert result.payload["decision"] == "approve"


class TestAgentPolling:
    def test_agent_polls_only_own_messages(self, bus: MessageBus):
        bus.send(_entry_request(to_agent="pm"))
        bus.send(
            AgentMessage(
                msg_type=MessageType.FORCE_EXIT,
                from_agent="pm",
                to_agent="slot_0",
                payload={"reason": "drawdown"},
            )
        )

        pm_msgs = bus.poll("pm")
        slot_msgs = bus.poll("slot_0")

        assert len(pm_msgs) == 1
        assert pm_msgs[0].msg_type == MessageType.ENTRY_REQUEST
        assert len(slot_msgs) == 1
        assert slot_msgs[0].msg_type == MessageType.FORCE_EXIT

    def test_poll_with_type_filter(self, bus: MessageBus):
        bus.send(_entry_request(to_agent="pm"))
        bus.send(
            AgentMessage(
                msg_type=MessageType.TARGET_RAISE_REQUEST,
                from_agent="slot_0",
                to_agent="pm",
                payload={"symbol": "MSFT"},
            )
        )

        entry_only = bus.poll("pm", msg_types=[MessageType.ENTRY_REQUEST])
        assert len(entry_only) == 1
        assert entry_only[0].msg_type == MessageType.ENTRY_REQUEST


class TestMessageLifecycle:
    def test_ack_then_process(self, bus: MessageBus):
        msg = _entry_request()
        bus.send(msg)

        bus.ack(msg.msg_id)
        # Acked messages don't appear in poll
        assert bus.poll("pm") == []

        bus.mark_processed(msg.msg_id)
        assert bus.poll("pm") == []

    def test_count_pending(self, bus: MessageBus):
        assert bus.count_pending("pm") == 0
        bus.send(_entry_request())
        bus.send(_entry_request())
        assert bus.count_pending("pm") == 2


class TestDecisionLogging:
    def test_log_decision(self, bus: MessageBus):
        row_id = bus.log_decision(
            agent_name="pm",
            decision_type="entry_approval",
            algo_recommendation="approve",
            agent_decision="approve",
            reasoning="strong catalyst, sector clear",
            llm_model="Qwen/Qwen3-8B",
            llm_latency_ms=125,
            prompt_version="1",
            inputs_json={"symbol": "AAPL"},
            raw_response='{"decision":"approve"}',
            symbol="AAPL",
            is_override=False,
            confidence=0.85,
        )
        assert row_id > 0

    def test_override_rate_starts_at_zero(self, bus: MessageBus):
        assert bus.get_override_rate() == 0.0

    def test_override_rate_calculation(self, bus: MessageBus):
        # Log 4 decisions: 1 override, 3 non-override
        for i in range(3):
            bus.log_decision(
                agent_name="scanner",
                decision_type="scan",
                algo_recommendation="approve",
                agent_decision="approve",
                reasoning="test",
                llm_model="qwen",
                llm_latency_ms=100,
                prompt_version="1",
                inputs_json={},
                raw_response="{}",
                is_override=False,
            )
        bus.log_decision(
            agent_name="scanner",
            decision_type="scan",
            algo_recommendation="approve",
            agent_decision="skip",
            reasoning="duplicate",
            llm_model="qwen",
            llm_latency_ms=100,
            prompt_version="1",
            inputs_json={},
            raw_response="{}",
            is_override=True,
        )
        rate = bus.get_override_rate("scanner")
        assert rate == pytest.approx(0.25)


class TestAgentState:
    def test_upsert_agent_state(self, bus: MessageBus):
        bus.update_agent_state("pm", status="running", consecutive_wins=3)
        state = bus.get_agent_state("pm")
        assert state is not None
        assert state["status"] == "running"
        assert state["consecutive_wins"] == 3

    def test_update_existing_state(self, bus: MessageBus):
        bus.update_agent_state("pm", status="running", consecutive_wins=1)
        bus.update_agent_state("pm", status="running", consecutive_wins=2)
        state = bus.get_agent_state("pm")
        assert state["consecutive_wins"] == 2

    def test_missing_agent_returns_none(self, bus: MessageBus):
        assert bus.get_agent_state("nonexistent") is None
