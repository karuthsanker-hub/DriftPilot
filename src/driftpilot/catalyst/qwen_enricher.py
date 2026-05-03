from __future__ import annotations
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

Sentiment = Literal["positive", "negative", "neutral"]


@dataclass(frozen=True)
class EnrichmentResult:
    sentiment: Sentiment
    priority_modifier: float
    horizon_override: int | None


_DEFAULT = EnrichmentResult(sentiment="neutral", priority_modifier=0.0, horizon_override=None)
_VALID_HORIZONS = {60, 240, 1440, 2880}
_VALID_SENTIMENTS = {"positive", "negative", "neutral"}

# Qwen3 is a "thinking" model: responses come wrapped in <think>...</think>{json}.
# We disable thinking via the /no_think tag and strip any residual block.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL)
# Find the first JSON object in the response (after any think block).
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}", flags=re.DOTALL)


def _strip_thinking_and_extract_json(content: str) -> str:
    """Strip Qwen3 <think>...</think> wrapper and return the first JSON object substring."""
    cleaned = _THINK_BLOCK_RE.sub("", content).strip()
    match = _JSON_OBJECT_RE.search(cleaned)
    return match.group(0) if match else cleaned


class QwenEnricher:
    def __init__(
        self,
        base_url: str = "http://192.168.1.166:8000/v1",
        model: str = "qwen",
        # Realistic for Qwen3-8B with /no_think on DGX: cold ~2s, warm ~0.5s.
        # 3000ms gives margin without making the news pipeline stall.
        timeout_ms: int = 3000,
        max_tokens: int = 128,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_ms / 1000.0
        self._max_tokens = max_tokens
        self._client = client  # injected for tests; if None, create per-call

    async def enrich(self, headline: str, category: str, subcategory: str) -> EnrichmentResult:
        # /no_think tells Qwen3 to emit an empty <think></think> block.
        prompt = (
            "/no_think Classify this financial headline. Return ONLY a JSON object "
            "(no prose, no markdown) with keys "
            "'sentiment' (positive/negative/neutral), "
            "'priority_modifier' (float in [-0.2, +0.2] reflecting headline strength), "
            "'horizon_override' (one of 60, 240, 1440, 2880 if the default category horizon "
            "should be overridden, else null). "
            f"Headline: {headline}. Category: {category}/{subcategory}."
        )

        try:
            client = self._client or httpx.AsyncClient(timeout=self._timeout_s)
            try:
                resp = await asyncio.wait_for(
                    client.post(
                        f"{self._base_url}/chat/completions",
                        json={
                            "model": self._model,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.0,
                            "max_tokens": self._max_tokens,
                        },
                    ),
                    timeout=self._timeout_s,
                )
            finally:
                if self._client is None:
                    await client.aclose()

            if resp.status_code != 200:
                logger.warning("qwen status %d for headline=%r", resp.status_code, headline[:60])
                return _DEFAULT

            payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
            json_str = _strip_thinking_and_extract_json(content)
            data = json.loads(json_str)
            return self._parse(data)
        except (asyncio.TimeoutError, httpx.RequestError, KeyError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("qwen enrichment failed (%s): %s", type(exc).__name__, str(exc)[:120])
            return _DEFAULT

    @staticmethod
    def _parse(data: dict) -> EnrichmentResult:
        sentiment = data.get("sentiment", "neutral")
        if sentiment not in _VALID_SENTIMENTS:
            sentiment = "neutral"

        try:
            pm = float(data.get("priority_modifier", 0.0))
        except (TypeError, ValueError):
            pm = 0.0
        pm = max(-0.2, min(0.2, pm))

        ho_raw = data.get("horizon_override")
        horizon_override = ho_raw if (isinstance(ho_raw, int) and ho_raw in _VALID_HORIZONS) else None

        return EnrichmentResult(sentiment=sentiment, priority_modifier=pm, horizon_override=horizon_override)
