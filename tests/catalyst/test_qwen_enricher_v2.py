from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from driftpilot.catalyst.context_assembler import EnrichmentContext
from driftpilot.catalyst.qwen_enricher import QwenEnricher, _build_user_prompt


def _client_with_qwen_response(raw: dict) -> httpx.AsyncClient:
    client = MagicMock(spec=httpx.AsyncClient)
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"choices": [{"message": {"content": json.dumps(raw)}}]})
    client.post = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()
    return client


def _context() -> EnrichmentContext:
    return EnrichmentContext(
        market_cap_m=100_000,
        avg_volume=1_000_000,
        sector="Health Care",
        eps_beat_pct=6.5,
        revenue_beat_pct=3.5,
        guidance_direction="up",
        last_4_surprises=[2.1, 1.8, -0.3, 3.5],
        headline_cluster_count=1,
        spy_change_pct=0.2,
        vix=18.0,
    )


def test_v2_prompt_includes_context_block() -> None:
    prompt = _build_user_prompt(
        "REGN Q1 EPS $9.47 Beats $8.89 Estimate",
        "earnings",
        "report",
        context=_context(),
    )

    assert "CONTEXT:" in prompt
    assert "EPS beat/miss: +6.50%" in prompt
    assert "Prior same-symbol headlines in last 30m: 1" in prompt


def test_v1_prompt_remains_backward_compatible_without_context() -> None:
    prompt = _build_user_prompt("AAPL beats earnings", "earnings", "report")

    assert "Symbol context: this headline was tagged" in prompt
    assert "CONTEXT:" not in prompt


@pytest.mark.asyncio
async def test_enrich_with_context_uses_v2_prompt_and_returns_raw_response() -> None:
    client = _client_with_qwen_response(
        {
            "sentiment": "positive",
            "priority_modifier": 0.123,
            "confidence": 0.82,
            "horizon_override": None,
        }
    )
    enricher = QwenEnricher(client=client)

    result, raw = await enricher.enrich_with_response(
        "REGN Q1 EPS $9.47 Beats $8.89 Estimate",
        "earnings",
        "report",
        context=_context(),
    )

    assert result.sentiment == "positive"
    assert result.priority_modifier == pytest.approx(0.123)
    assert result.confidence == pytest.approx(0.82)
    assert raw["confidence"] == pytest.approx(0.82)
    payload = client.post.await_args.kwargs["json"]
    assert "MAGNITUDE TIERS" in payload["messages"][0]["content"]
    assert "CONTEXT:" in payload["messages"][1]["content"]


def test_parse_handles_v1_response_without_confidence() -> None:
    result = QwenEnricher._parse(
        {"sentiment": "positive", "priority_modifier": 0.15, "horizon_override": 240}
    )

    assert result.sentiment == "positive"
    assert result.priority_modifier == pytest.approx(0.15)
    assert result.horizon_override == 240
    assert result.confidence == pytest.approx(0.5)


def test_confidence_is_clamped() -> None:
    high = QwenEnricher._parse({"sentiment": "positive", "priority_modifier": 0.1, "confidence": 2})
    low = QwenEnricher._parse({"sentiment": "negative", "priority_modifier": -0.1, "confidence": -1})

    assert high.confidence == 1.0
    assert low.confidence == 0.0
