from __future__ import annotations
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from driftpilot.catalyst.context_assembler import EnrichmentContext

logger = logging.getLogger(__name__)

Sentiment = Literal["positive", "negative", "neutral"]


@dataclass(frozen=True)
class EnrichmentResult:
    sentiment: Sentiment
    priority_modifier: float
    horizon_override: int | None
    confidence: float = 0.5


_DEFAULT = EnrichmentResult(sentiment="neutral", priority_modifier=0.0, horizon_override=None, confidence=0.0)
_VALID_HORIZONS = {60, 240, 1440, 2880}
_VALID_SENTIMENTS = {"positive", "negative", "neutral"}

# Qwen3 is a "thinking" model: responses come wrapped in <think>...</think>{json}.
# We disable thinking via the /no_think tag and strip any residual block.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL)
# Find the first JSON object in the response (after any think block).
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}", flags=re.DOTALL)

_SYSTEM_PROMPT_V1 = (
    "You are a short-term equity analyst. Your job is to predict whether a "
    "financial news headline will move a stock's price UP, DOWN, or have NO "
    "directional impact within the next 60 minutes of trading.\n\n"
    "Focus on the TRADING IMPLICATION, not the tone of the words:\n"
    "- Earnings beat, revenue above consensus, raised guidance → positive (price UP)\n"
    "- Earnings miss, revenue miss, lowered guidance, downgrade → negative (price DOWN)\n"
    "- Analyst raises price target, upgrade, new buy rating → positive\n"
    "- Analyst lowers price target, downgrade, new sell rating → negative\n"
    "- SEC filing (8-A, 8-K), routine regulatory filing → neutral UNLESS the "
    "filing content itself is bullish (new product, acquisition) or bearish (delisting, dilution)\n"
    "- Roundup articles listing multiple stocks (\"12 Stocks Moving...\") → neutral\n"
    "- Stock mentioned in passing in a broader story → neutral\n"
    "- M&A target announced → positive for target, context-dependent for acquirer\n\n"
    "Return ONLY a JSON object with these keys:\n"
    "- \"sentiment\": \"positive\", \"negative\", or \"neutral\"\n"
    "- \"confidence\": float 0.0-1.0 (how confident in the directional call)\n"
    "- \"priority_modifier\": float -0.20 to +0.20 (expected magnitude: "
    "+0.15 = strong beat/upgrade, +0.05 = mild positive, -0.10 = moderate negative, "
    "0.0 = no edge)\n"
    "- \"horizon_override\": 60, 240, 1440, or 2880 if the move will play out "
    "over a different window than the default for this category, else null\n\n"
    "No prose, no markdown, no explanation. JSON only."
)

_SYSTEM_PROMPT_V2 = (
    "You are a short-term equity analyst predicting 60-minute price direction "
    "from financial news. Focus on trading impact, not word tone.\n\n"
    "MAGNITUDE TIERS (use ranges, not fixed anchors):\n"
    "+0.15 to +0.20: Large-cap beat >5% or small-cap beat >3%, with guidance raise\n"
    "+0.08 to +0.14: Clear beat 2-5% on mid-cap, or any beat with hot sector tailwind\n"
    "+0.03 to +0.07: Small beat 1-2%, large-cap, or beat in line with history\n"
    "+0.01 to +0.02: Marginal beat <1%, routine, already priced in\n"
    " 0.00: No directional signal, informational only\n"
    "-0.01 to -0.07: Small miss, minor negative, guidance maintained\n"
    "-0.08 to -0.14: Clear miss, or beat with guidance cut (mixed signal is net negative)\n"
    "-0.15 to -0.20: Large miss, guidance cut, downgrade on high-conviction name\n\n"
    "CONFIDENCE CALIBRATION:\n"
    "0.90-1.00: Numbers clearly in headline, direction unambiguous, large magnitude\n"
    "0.70-0.89: Clear beat/miss but magnitude uncertain, or moderate event\n"
    "0.50-0.69: Directional lean but could go either way\n"
    "0.30-0.49: Weak signal, mostly noise, slight lean\n"
    "0.00-0.29: Coin flip, no meaningful edge\n\n"
    "Use the CONTEXT block to calibrate magnitude. A 0.9% EPS beat on a company "
    "that averages 1.8% surprise is noise — neutral or +0.02 at most. If "
    "headline_cluster_count / prior same-symbol headlines is >0, the headline is "
    "likely already priced in — reduce confidence by 0.2 and magnitude by half. "
    "If VIX > 25, reduce positive magnitude by 30%; fear compresses drift. "
    "Only set horizon_override if the context suggests a materially different "
    "window than 60 minutes.\n\n"
    "Return ONLY JSON with sentiment, confidence, priority_modifier, and "
    "horizon_override. No prose, no markdown, no explanation."
)


def _strip_thinking_and_extract_json(content: str) -> str:
    """Strip Qwen3 <think>...</think> wrapper and return the first JSON object substring."""
    cleaned = _THINK_BLOCK_RE.sub("", content).strip()
    match = _JSON_OBJECT_RE.search(cleaned)
    return match.group(0) if match else cleaned


class QwenEnricher:
    def __init__(
        self,
        base_url: str = "http://192.168.1.166:8000/v1",
        model: str = "Qwen/Qwen3-8B",
        timeout_ms: int = 10000,
        max_tokens: int = 128,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_ms / 1000.0
        self._max_tokens = max_tokens
        self._client = client

    async def enrich(
        self,
        headline: str,
        category: str,
        subcategory: str,
        *,
        context: EnrichmentContext | None = None,
    ) -> EnrichmentResult:
        result, _ = await self.enrich_with_response(
            headline,
            category,
            subcategory,
            context=context,
        )
        return result

    async def enrich_with_response(
        self,
        headline: str,
        category: str,
        subcategory: str,
        *,
        context: EnrichmentContext | None = None,
    ) -> tuple[EnrichmentResult, dict[str, Any]]:
        user_prompt = _build_user_prompt(headline, category, subcategory, context=context)
        system_prompt = _SYSTEM_PROMPT_V2 if context is not None else _SYSTEM_PROMPT_V1
        try:
            client = self._client or httpx.AsyncClient(timeout=self._timeout_s)
            try:
                resp = await asyncio.wait_for(
                    client.post(
                        f"{self._base_url}/chat/completions",
                        json={
                            "model": self._model,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
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
                return _DEFAULT, {}

            payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
            json_str = _strip_thinking_and_extract_json(content)
            data = json.loads(json_str)
            return self._parse(data), data
        except (asyncio.TimeoutError, httpx.RequestError, KeyError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("qwen enrichment failed (%s): %s", type(exc).__name__, str(exc)[:120])
            return _DEFAULT, {}

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

        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        ho_raw = data.get("horizon_override")
        horizon_override = ho_raw if (isinstance(ho_raw, int) and ho_raw in _VALID_HORIZONS) else None

        return EnrichmentResult(
            sentiment=sentiment, priority_modifier=pm,
            horizon_override=horizon_override, confidence=confidence,
        )


_CATEGORY_HINTS: dict[str, str] = {
    "earnings/report": (
        "CATEGORY PRIOR: Earnings reports almost always move a stock. "
        "A beat → positive. A miss → negative. Only neutral if the headline "
        "gives NO indication of beat/miss (rare for earnings reports)."
    ),
    "earnings/beat": (
        "CATEGORY PRIOR: This is explicitly tagged as an earnings beat. "
        "Default sentiment is positive unless the headline mentions negative "
        "guidance or a revenue miss that outweighs the EPS beat."
    ),
    "earnings/miss": (
        "CATEGORY PRIOR: This is explicitly tagged as an earnings miss. "
        "Default sentiment is negative unless there's a strong offset "
        "(raised guidance, one-time charges excluded)."
    ),
    "earnings/guidance_up": (
        "CATEGORY PRIOR: Raised guidance is a strong positive signal. "
        "Default: positive with priority_modifier ≥ +0.08."
    ),
    "earnings/guidance_down": (
        "CATEGORY PRIOR: Lowered guidance is a strong negative signal. "
        "Default: negative with priority_modifier ≤ -0.08."
    ),
    "analyst/target_raise": (
        "CATEGORY PRIOR: Analyst target raise is mildly positive. "
        "Default: positive with priority_modifier +0.03 to +0.08."
    ),
    "analyst/target_cut": (
        "CATEGORY PRIOR: Analyst target cut is mildly negative. "
        "Default: negative with priority_modifier -0.03 to -0.08."
    ),
    "analyst/upgrade": (
        "CATEGORY PRIOR: Analyst upgrade is positive. "
        "Default: positive with priority_modifier +0.05 to +0.12."
    ),
    "analyst/downgrade": (
        "CATEGORY PRIOR: Analyst downgrade is negative. "
        "Default: negative with priority_modifier -0.05 to -0.12."
    ),
    "m_and_a/acquires": (
        "CATEGORY PRIOR: Acquisition target usually gaps up. Acquirer is mixed. "
        "If the headline is about the TARGET company → positive."
    ),
    "filing/8a": (
        "CATEGORY PRIOR: 8-A filings are routine SEC registrations. "
        "Usually neutral unless the filing content signals dilution or a new product. "
        "Read the headline carefully — if it's just a filing notice, neutral."
    ),
    "filing/8k": (
        "CATEGORY PRIOR: 8-K filings can be anything. Read the headline for "
        "the actual content (executive change, material event, etc.)."
    ),
}


def _build_user_prompt(
    headline: str,
    category: str,
    subcategory: str,
    *,
    context: EnrichmentContext | None = None,
) -> str:
    cat_key = f"{category}/{subcategory}"
    hint = _CATEGORY_HINTS.get(cat_key, "")

    if context is None:
        hint_line = f"{hint}\n" if hint else ""
        return (
            f"/no_think Headline: \"{headline}\"\n"
            f"Category: {category}/{subcategory}\n"
            f"{hint_line}"
            f"Symbol context: this headline was tagged to a specific stock. "
            f"Will it move that stock's price in the next 60 minutes?"
        )

    # Build context block, noting which fields have real data vs unknown
    ctx_block = context.to_prompt_block()
    has_data = context.eps_beat_pct is not None or context.market_cap_m is not None

    guidance = (
        "Based on the headline, category prior, AND the context above, "
        "predict the 60-minute price direction."
        if has_data
        else "Context data is limited. Rely heavily on the headline text and "
        "category prior to determine sentiment. Do NOT default to neutral "
        "just because context is missing — the category and headline alone "
        "are often sufficient for a directional call."
    )

    parts = [
        f'/no_think Headline: "{headline}"',
        f"Category: {category}/{subcategory}",
    ]
    if hint:
        parts.append(f"\n{hint}")
    parts.append(f"\nCONTEXT:\n{ctx_block}")
    parts.append(f"\n{guidance}")
    return "\n".join(parts)
