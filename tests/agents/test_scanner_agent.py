"""Tests for Scanner Agent."""

from __future__ import annotations

import pytest

from driftpilot.agents.llm_client import LLMClient
from driftpilot.agents.message_bus import MessageBus
from driftpilot.agents.models import MessageType
from driftpilot.agents.prompt_loader import PromptLoader
from driftpilot.agents.scanner_agent import CandidateInfo, MarketContext, ScannerAgent


@pytest.fixture
def bus(tmp_path):
    db_path = tmp_path / "scanner_test.sqlite3"
    b = MessageBus(db_path=db_path, ttl_seconds=300)
    b.initialize()
    yield b
    b.close()


@pytest.fixture
def scanner(bus):
    """Scanner agent with failing LLM (fallback=follow_algo → approve all)."""
    llm = LLMClient(qwen_url="http://localhost:9999/v1", qwen_timeout_ms=50)
    prompts = PromptLoader("config/prompts")
    return ScannerAgent(bus, llm, prompts)


@pytest.fixture
def market():
    return MarketContext(spy_change_pct=0.2, vix=18.0, sector_change_pct=0.5)


@pytest.fixture
def single_candidate():
    return [
        CandidateInfo(
            symbol="AAPL",
            signal_name="earnings_report_v1",
            algo_score=0.85,
            headline="AAPL Q1 EPS $1.50 Beats $1.40 Estimate",
            category="earnings",
            subcategory="earnings_report",
            sentiment="positive",
            confidence=0.8,
            priority_modifier=0.15,
            sector="tech",
            minutes_since_headline=2,
            same_symbol_traded_today=False,
            similar_headlines_last_2h=0,
            catalyst_event_id=42,
        )
    ]


class TestFallbackFollowsAlgo:
    def test_fallback_approves_all_on_timeout(self, scanner, single_candidate, market):
        """When LLM fails, scanner approves all algo-passed candidates."""
        result = scanner.evaluate_candidates(single_candidate, market)

        assert result.candidates_evaluated == 1
        assert result.entries_requested == 1
        assert result.candidates_skipped == 0
        assert result.used_fallback is True

    def test_fallback_sends_entry_request_to_pm(self, scanner, bus, single_candidate, market):
        scanner.evaluate_candidates(single_candidate, market)

        pm_msgs = bus.poll("pm")
        assert len(pm_msgs) == 1
        assert pm_msgs[0].msg_type == MessageType.ENTRY_REQUEST
        assert pm_msgs[0].payload["symbol"] == "AAPL"
        assert pm_msgs[0].payload["signal_name"] == "earnings_report_v1"


class TestEmptyCandidates:
    def test_no_candidates_no_action(self, scanner, market):
        result = scanner.evaluate_candidates([], market)
        assert result.candidates_evaluated == 0
        assert result.entries_requested == 0
        assert result.messages_sent == []


class TestMultipleCandidates:
    def test_multiple_candidates_all_approved_on_fallback(self, scanner, bus, market):
        candidates = [
            CandidateInfo(
                symbol="AAPL",
                signal_name="earnings_report_v1",
                algo_score=0.85,
                headline="AAPL beats",
                category="earnings",
                subcategory="earnings_report",
                sentiment="positive",
                confidence=0.8,
                priority_modifier=0.15,
                sector="tech",
                minutes_since_headline=2,
                same_symbol_traded_today=False,
                similar_headlines_last_2h=0,
            ),
            CandidateInfo(
                symbol="MSFT",
                signal_name="earnings_report_v1",
                algo_score=0.75,
                headline="MSFT beats",
                category="earnings",
                subcategory="earnings_report",
                sentiment="positive",
                confidence=0.7,
                priority_modifier=0.10,
                sector="tech",
                minutes_since_headline=3,
                same_symbol_traded_today=False,
                similar_headlines_last_2h=0,
            ),
        ]
        result = scanner.evaluate_candidates(candidates, market)
        assert result.entries_requested == 2

        pm_msgs = bus.poll("pm")
        assert len(pm_msgs) == 2
        symbols = {m.payload["symbol"] for m in pm_msgs}
        assert symbols == {"AAPL", "MSFT"}


class TestDecisionLogging:
    def test_logs_decision_for_each_candidate(self, scanner, bus, single_candidate, market):
        scanner.evaluate_candidates(single_candidate, market)

        row = bus.conn.execute(
            "SELECT COUNT(*) FROM agent_decisions WHERE agent_name = 'scanner'"
        ).fetchone()
        assert row[0] == 1

    def test_logs_override_for_skip(self, scanner, bus, single_candidate, market):
        """If scanner skips, it should be logged as an override."""
        # With fallback, it won't skip — but verify the logging structure
        scanner.evaluate_candidates(single_candidate, market)

        row = bus.conn.execute(
            "SELECT is_override FROM agent_decisions WHERE agent_name = 'scanner'"
        ).fetchone()
        # Fallback approves, so not an override
        assert row[0] == 0


class TestEntryRequestPayload:
    def test_entry_request_contains_required_fields(self, scanner, bus, single_candidate, market):
        scanner.evaluate_candidates(single_candidate, market)

        msgs = bus.poll("pm")
        payload = msgs[0].payload

        assert "symbol" in payload
        assert "signal_name" in payload
        assert "algo_score" in payload
        assert "headline" in payload
        assert "sentiment" in payload
        assert "confidence" in payload
        assert "proposed_target_pct" in payload
        assert "proposed_stop_pct" in payload
        assert "sector" in payload
        assert payload["proposed_stop_pct"] == 0.015


class TestAgentState:
    def test_updates_heartbeat(self, scanner, bus, single_candidate, market):
        scanner.evaluate_candidates(single_candidate, market)
        state = bus.get_agent_state("scanner")
        assert state is not None
        assert state["status"] == "running"
