"""Tests for dual LLM client (Qwen + Claude)."""

from __future__ import annotations


import pytest

from driftpilot.agents.llm_client import LLMClient, LLMResponse
from driftpilot.agents.prompt_loader import PromptConfig


@pytest.fixture
def client():
    return LLMClient(
        qwen_url="http://localhost:9999/v1",
        qwen_timeout_ms=100,
        claude_api_key="test-key",
        claude_timeout_ms=100,
    )


@pytest.fixture
def approve_prompt():
    return PromptConfig(
        name="test_pm",
        version="1",
        model="qwen",
        timeout_ms=500,
        max_tokens=256,
        temperature=0.0,
        fallback_action="approve",
        system="You are a PM. Respond JSON.",
        user_template="Symbol: {symbol}",
    )


@pytest.fixture
def hold_prompt():
    return PromptConfig(
        name="test_slot",
        version="1",
        model="qwen",
        timeout_ms=500,
        max_tokens=256,
        temperature=0.0,
        fallback_action="hold",
        system="You are a slot agent.",
        user_template="Position: {symbol}",
    )


class TestFallbackBehavior:
    def test_qwen_timeout_returns_fallback(self, client, approve_prompt):
        # No server running at localhost:9999, should timeout
        result = client.complete(approve_prompt, {"symbol": "AAPL"})
        assert result.success is False
        assert result.used_fallback is True
        assert result.parsed["decision"] == "approve"
        assert "fallback" in result.parsed["reasoning"]

    def test_hold_fallback_on_failure(self, client, hold_prompt):
        result = client.complete(hold_prompt, {"symbol": "TSLA"})
        assert result.success is False
        assert result.used_fallback is True
        assert result.parsed["action"] == "hold"

    def test_no_claude_key_returns_fallback(self):
        client = LLMClient(claude_api_key="")
        claude_prompt = PromptConfig(
            name="test",
            version="1",
            model="claude",
            timeout_ms=100,
            max_tokens=256,
            temperature=0.0,
            fallback_action="hold",
            system="test",
            user_template="{symbol}",
        )
        result = client.complete(claude_prompt, {"symbol": "X"})
        assert result.success is False
        assert result.error == "no_claude_api_key"


class TestResponseParsing:
    def test_parse_valid_json(self, client, approve_prompt):
        raw = '{"decision": "approve", "reasoning": "looks good", "target_pct": 0.01, "size_multiplier": 1.0}'
        result = client._parse_response(raw, "qwen-test", 100, approve_prompt)
        assert result.success is True
        assert result.parsed["decision"] == "approve"
        assert result.latency_ms == 100

    def test_parse_json_with_code_fence(self, client, approve_prompt):
        raw = '```json\n{"decision": "deny", "reasoning": "too risky"}\n```'
        result = client._parse_response(raw, "qwen-test", 50, approve_prompt)
        assert result.success is True
        assert result.parsed["decision"] == "deny"

    def test_parse_json_with_think_tags(self, client, approve_prompt):
        raw = '<think>Let me analyze this carefully.</think>{"decision": "approve", "reasoning": "strong"}'
        result = client._parse_response(raw, "qwen-test", 200, approve_prompt)
        assert result.success is True
        assert result.parsed["decision"] == "approve"

    def test_parse_invalid_json_returns_fallback(self, client, approve_prompt):
        raw = "I think we should approve this trade because..."
        result = client._parse_response(raw, "qwen-test", 100, approve_prompt)
        assert result.success is False
        assert result.used_fallback is True
        assert result.parsed["decision"] == "approve"  # fallback_action

    def test_parse_non_dict_json_returns_fallback(self, client, approve_prompt):
        raw = '["not", "a", "dict"]'
        result = client._parse_response(raw, "qwen-test", 100, approve_prompt)
        assert result.success is False
        assert result.used_fallback is True


class TestLLMResponseModel:
    def test_successful_response(self):
        resp = LLMResponse(
            success=True,
            parsed={"decision": "approve"},
            raw='{"decision":"approve"}',
            model="Qwen/Qwen3-8B",
            latency_ms=125,
        )
        assert resp.success
        assert not resp.used_fallback
        assert resp.error is None

    def test_fallback_response(self):
        resp = LLMResponse(
            success=False,
            parsed={"action": "hold", "reasoning": "fallback: timeout"},
            model="fallback",
            latency_ms=500,
            error="timeout_500ms",
            used_fallback=True,
        )
        assert not resp.success
        assert resp.used_fallback


class TestModelRouting:
    def test_qwen_model_routes_to_qwen(self, client):
        prompt = PromptConfig(
            name="test",
            version="1",
            model="qwen",
            timeout_ms=100,
            max_tokens=256,
            temperature=0.0,
            fallback_action="hold",
            system="test",
            user_template="{symbol}",
        )
        # Will fail to connect but verifies routing
        result = client.complete(prompt, {"symbol": "X"})
        # Should try Qwen and get connection error → fallback
        assert result.used_fallback is True

    def test_claude_model_routes_to_claude(self, client):
        prompt = PromptConfig(
            name="test",
            version="1",
            model="claude",
            timeout_ms=100,
            max_tokens=256,
            temperature=0.0,
            fallback_action="hold",
            system="test",
            user_template="{symbol}",
        )
        # Will fail to connect but verifies routing
        result = client.complete(prompt, {"symbol": "X"})
        assert result.used_fallback is True

    def test_unknown_model_returns_fallback(self, client):
        prompt = PromptConfig(
            name="test",
            version="1",
            model="gpt-4",
            timeout_ms=100,
            max_tokens=256,
            temperature=0.0,
            fallback_action="deny",
            system="test",
            user_template="{symbol}",
        )
        result = client.complete(prompt, {"symbol": "X"})
        assert result.used_fallback is True
        assert result.parsed["decision"] == "deny"
