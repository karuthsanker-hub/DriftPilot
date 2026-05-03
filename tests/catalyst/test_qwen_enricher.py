import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from driftpilot.catalyst.qwen_enricher import EnrichmentResult, QwenEnricher


def _mock_client(response_json=None, status=200, raise_exc=None) -> httpx.AsyncClient:
    client = MagicMock(spec=httpx.AsyncClient)
    if raise_exc is not None:
        client.post = AsyncMock(side_effect=raise_exc)
    else:
        resp = MagicMock()
        resp.status_code = status
        resp.json = MagicMock(return_value={
            "choices": [{"message": {"content": json.dumps(response_json or {})}}]
        })
        client.post = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()
    return client


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
