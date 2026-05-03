import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from driftpilot.catalyst.qwen_enricher import (
    EnrichmentResult,
    QwenEnricher,
    _strip_thinking_and_extract_json,
)


def _mock_client(response_json=None, status=200, raise_exc=None, raw_content: str | None = None) -> httpx.AsyncClient:
    client = MagicMock(spec=httpx.AsyncClient)
    if raise_exc is not None:
        client.post = AsyncMock(side_effect=raise_exc)
    else:
        resp = MagicMock()
        resp.status_code = status
        content = raw_content if raw_content is not None else json.dumps(response_json or {})
        resp.json = MagicMock(return_value={
            "choices": [{"message": {"content": content}}]
        })
        client.post = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()
    return client


def test_strip_extracts_json_from_thinking_wrapper() -> None:
    raw = '<think>\n\n</think>\n\n{"sentiment": "positive", "priority_modifier": 0.1, "horizon_override": null}'
    assert _strip_thinking_and_extract_json(raw) == '{"sentiment": "positive", "priority_modifier": 0.1, "horizon_override": null}'


def test_strip_handles_nonempty_think_block() -> None:
    raw = '<think>I am reasoning about this</think>{"sentiment": "negative", "priority_modifier": -0.1, "horizon_override": null}'
    out = _strip_thinking_and_extract_json(raw)
    assert out == '{"sentiment": "negative", "priority_modifier": -0.1, "horizon_override": null}'


def test_strip_handles_no_think_wrapper() -> None:
    raw = '{"sentiment": "neutral", "priority_modifier": 0.0, "horizon_override": null}'
    assert _strip_thinking_and_extract_json(raw) == raw


@pytest.mark.asyncio
async def test_qwen3_thinking_response_parsed() -> None:
    """Smoke: end-to-end against the actual response shape Qwen3-8B emits."""
    qwen3_raw = '<think>\n\n</think>\n\n{"sentiment": "positive", "priority_modifier": 0.15, "horizon_override": 240}'
    client = _mock_client(raw_content=qwen3_raw)
    enricher = QwenEnricher(client=client)
    result = await enricher.enrich("AAPL beats earnings", "earnings", "report")
    assert result == EnrichmentResult("positive", 0.15, 240)


@pytest.mark.asyncio
async def test_valid_qwen_response_parsed() -> None:
    client = _mock_client({"sentiment": "positive", "priority_modifier": 0.15, "horizon_override": 240})
    enricher = QwenEnricher(client=client)
    result = await enricher.enrich("AAPL beats earnings", "earnings", "report")
    assert result == EnrichmentResult("positive", 0.15, 240)


@pytest.mark.asyncio
async def test_timeout_returns_defaults() -> None:
    import asyncio
    client = _mock_client(raise_exc=asyncio.TimeoutError())
    enricher = QwenEnricher(client=client)
    result = await enricher.enrich("h", "c", "s")
    assert result.sentiment == "neutral"
    assert result.priority_modifier == 0.0
    assert result.horizon_override is None


@pytest.mark.asyncio
async def test_connection_error_returns_defaults() -> None:
    client = _mock_client(raise_exc=httpx.ConnectError("boom"))
    enricher = QwenEnricher(client=client)
    result = await enricher.enrich("h", "c", "s")
    assert result.sentiment == "neutral"


@pytest.mark.asyncio
async def test_malformed_json_returns_defaults() -> None:
    client = MagicMock(spec=httpx.AsyncClient)
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"choices": [{"message": {"content": "not json {{"}}]})
    client.post = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()
    enricher = QwenEnricher(client=client)
    result = await enricher.enrich("h", "c", "s")
    assert result.sentiment == "neutral"


@pytest.mark.asyncio
async def test_garbage_values_clamped() -> None:
    client = _mock_client({"sentiment": "happy", "priority_modifier": 99.0, "horizon_override": 12345})
    enricher = QwenEnricher(client=client)
    result = await enricher.enrich("h", "c", "s")
    assert result.sentiment == "neutral"          # invalid sentiment → default
    assert result.priority_modifier == 0.2        # clamped to range
    assert result.horizon_override is None        # invalid → None


@pytest.mark.asyncio
async def test_non_200_returns_defaults() -> None:
    client = _mock_client({}, status=500)
    enricher = QwenEnricher(client=client)
    result = await enricher.enrich("h", "c", "s")
    assert result.sentiment == "neutral"
